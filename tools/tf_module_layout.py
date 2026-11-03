#!/usr/bin/env python3
"""Terraform Module Layout Linter.

Enforces canonical file layout and separation of concerns for Terraform
modules according to the repository specification:

A Terraform module is a directory containing one or more .tf files.

Canonical files (root and submodules):
  - variables.tf  - Inputs only: variable declarations (and validation)
  - outputs.tf    - Outputs only: output declarations
  - versions.tf   - Requirements: terraform constraints, provider requirements
  - README.md     - Documentation: module overview, usage, interface

Allowed files (optional):
  - main.tf       - Primary resource graph: resource and data blocks
  - locals.tf     - Local values only: locals declarations
  - <name>.tf     - Domain-specific resource groupings (must be [a-z0-9_]+.tf)

Prohibited in all modules:
  - terraform.tfvars, *.auto.tfvars (caller concerns, not module concerns)
  - *.tfstate, *.tfstate.backup (state files must never be checked in)
  - envs/, environments/, live/ (orchestration belongs outside modules)

Root module exception:
  - providers.tf is permitted ONLY in the root module (or root-level versions.tf)
  - provider and backend blocks are prohibited in submodules (child modules)

Self-contained: no external dependencies beyond the standard library and
authoritative structure-preserving hclblocks parser helper.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CORE_REQUIRED_FILES = {"main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"}
ROOT_ONLY_FILES = {"providers.tf", "backend.tf"}
PROHIBITED_DIR_NAMES = {"envs", "environments"}
ALLOWED_NON_TF_FILES = {"README.md", "LICENSE", ".terraform.lock.hcl"}

TF_FILENAME_RE = re.compile(r"^[a-z0-9_]+\.tf$")

_COMPILED_HELPER_PATH: Path | None = None


def _build_helper_process(hclblocks_dir: Path, dest: Path) -> None:
    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    env["GOFLAGS"] = "-mod=readonly"
    env["GOTOOLCHAIN"] = "local"
    env["GOWORK"] = "off"
    env.pop("GONOSUMDB", None)
    cmd = ["go", "build", "-trimpath", "-ldflags=-s -w -buildid=", "-o", str(dest), "."]
    res = subprocess.run(cmd, cwd=str(hclblocks_dir), env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to build hclblocks from source:\n{res.stderr}")


def _get_or_build_hcl_helper() -> Path:
    """Compile in-repo hclblocks helper into a private 0700 directory once per process."""
    global _COMPILED_HELPER_PATH
    if _COMPILED_HELPER_PATH is not None and _COMPILED_HELPER_PATH.is_file():
        return _COMPILED_HELPER_PATH

    go_bin = shutil.which("go")
    if not go_bin:
        raise RuntimeError("Go toolchain ('go') not found on PATH; required to run hclblocks helper")

    repo_root = Path(__file__).resolve().parents[1]
    hclblocks_dir = repo_root / "tools" / "hclblocks"
    if not (hclblocks_dir / "main.go").is_file():
        raise RuntimeError(f"hclblocks source not found at {hclblocks_dir}")

    tmp_dir = tempfile.mkdtemp(prefix="agent-skills-hclblocks-")
    os.chmod(tmp_dir, 0o700)
    dest = Path(tmp_dir) / "hclblocks"

    try:
        _build_helper_process(hclblocks_dir, dest)
        dest.chmod(0o700)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
    _COMPILED_HELPER_PATH = dest
    return dest


def _inspect_terraform_children(nested: list[Any]) -> bool:
    for child in nested:
        if isinstance(child, dict) and child.get("type") in ("backend", "cloud"):
            return True
    return False


def _inspect_single_block(block: Any) -> tuple[bool, bool, bool]:
    """Inspect one block: returns (has_violation, has_provider, has_backend)."""
    if not isinstance(block, dict):
        return (False, False, False)
    b_type = block.get("type", "")
    labels = block.get("labels") or []
    nested = block.get("blocks") or []

    if b_type == "provider":
        return (False, True, False)
    if b_type == "terraform":
        return (bool(labels), False, _inspect_terraform_children(nested))
    if b_type in ("backend", "cloud"):
        return (False, False, True)
    return (False, False, False)


def _inspect_parsed_blocks(blocks: list[dict[str, Any]]) -> tuple[bool, bool, bool]:
    """Inspect parsed HCL structure for blocks.

    Returns (has_violation, has_provider, has_backend).
    """
    has_provider = False
    has_backend = False
    has_violation = False

    for block in blocks:
        viol, prov, back = _inspect_single_block(block)
        has_violation = has_violation or viol
        has_provider = has_provider or prov
        has_backend = has_backend or back

    return (has_violation, has_provider, has_backend)


def _parse_hcl_content(content: str | bytes) -> tuple[bool, bool, bool]:
    """Parse raw HCL content using in-repo hclblocks helper compiled from source.

    Returns (has_syntax_violation, has_provider, has_backend).
    Any unparseable content, safety error, or invalid block fails closed as a violation.
    """
    raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    try:
        helper_bin = _get_or_build_hcl_helper()
        proc = subprocess.run(
            [str(helper_bin)],
            input=raw_bytes,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            return True, False, False
        data = json.loads(proc.stdout.decode("utf-8"))
        if not isinstance(data, dict) or not data.get("valid", False):
            return True, False, False
        blocks = data.get("blocks") or []
        if not isinstance(blocks, list):
            return True, False, False
        return _inspect_parsed_blocks(blocks)
    except Exception:
        return True, False, False


def scan_tf_file(content: str | bytes) -> tuple[bool, bool, bool]:
    """Scan Terraform file content for violations, provider blocks, and backend blocks.

    Returns (has_violation, has_provider, has_backend).
    """
    return _parse_hcl_content(content)


def _check_required_files(module_dir: Path) -> list[str]:
    """Verify the 5 core module files are present."""
    errors: list[str] = []
    for name in sorted(CORE_REQUIRED_FILES):
        if not (module_dir / name).is_file():
            errors.append(f"{module_dir}: missing required file {name!r}")
    return errors


def _check_prohibited_dirs(module_dir: Path) -> list[str]:
    """Check for prohibited environment root directories."""
    errors: list[str] = []
    for dir_name in sorted(PROHIBITED_DIR_NAMES):
        if (module_dir / dir_name).is_dir():
            errors.append(f"{module_dir}: prohibited environment directory {dir_name!r}")
    return errors


def _check_tf_filename(file_path: Path, name: str, *, is_root: bool) -> str | None:
    if not TF_FILENAME_RE.match(name):
        return f"{file_path.parent}: unexpected file {name!r} (must match [a-z0-9_]+\\.tf)"
    if not is_root and name in ROOT_ONLY_FILES:
        return f"{file_path.parent}: unexpected file {name!r} (root module only)"
    return None


def _check_single_file(file_path: Path, *, is_root: bool) -> str | None:
    """Check an individual file for prohibited state/tfvars, invalid name, or unexpected file."""
    name = file_path.name
    if name in ALLOWED_NON_TF_FILES:
        return None
    if name.endswith(".tfvars") or name.endswith(".tfvars.json"):
        if not is_root:
            return f"{file_path.parent}: prohibited file {name!r} (not permitted in child modules)"
        return None
    if ".tfstate" in name:
        return f"{file_path.parent}: prohibited state file {name!r}"
    if name.endswith(".tf"):
        return _check_tf_filename(file_path, name, is_root=is_root)
    return f"{file_path.parent}: unexpected file {name!r}"


def _check_files(module_dir: Path, *, is_root: bool) -> list[str]:
    """Check all files in the module directory."""
    errors: list[str] = []
    for entry in sorted(module_dir.iterdir()):
        if entry.is_file():
            err = _check_single_file(entry, is_root=is_root)
            if err:
                errors.append(err)
    return errors


def _check_child_tf_file(tf_path: Path, errors: list[str]) -> None:
    try:
        content_bytes = tf_path.read_bytes()
        _ = content_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{tf_path}: unreadable file ({exc})")
        return
    has_violation, has_provider, has_backend = _parse_hcl_content(content_bytes)
    if has_violation:
        errors.append(f"{tf_path}: unparseable HCL (refused)")
    if has_provider:
        errors.append(f"{tf_path}: provider configuration in non-root module")
    if has_backend:
        errors.append(f"{tf_path}: backend configuration in non-root module")


def _check_child_blocks(module_dir: Path) -> list[str]:
    """Check that non-root modules do not contain provider or backend blocks."""
    errors: list[str] = []
    for tf_path in sorted(module_dir.glob("*.tf")):
        _check_child_tf_file(tf_path, errors)
    return errors


def lint_module(module_dir: Path, *, is_root: bool = False) -> list[str]:
    """Lint a single Terraform module directory. Returns a list of error strings."""
    if not module_dir.is_dir():
        return [f"{module_dir}: not a directory"]

    errors = _check_required_files(module_dir)
    errors.extend(_check_prohibited_dirs(module_dir))
    errors.extend(_check_files(module_dir, is_root=is_root))

    if not is_root:
        errors.extend(_check_child_blocks(module_dir))

    return errors


def _is_skipped_dir(dirpath: Path, root: Path) -> bool:
    rel = dirpath.relative_to(root)
    return any(part.startswith(".") for part in rel.parts)


def lint_tree(root: Path, *, root_module: Path | None = None) -> list[str]:
    """Recursively find and lint all Terraform modules under *root*.

    A directory is considered a module if it contains at least one .tf file.
    *root_module* marks which directory is the root module (gets provider/backend
    allowance). If None, the *root* directory itself is treated as root module.
    """
    target_root = root if root_module is None else root_module
    errors: list[str] = []
    for dirpath in sorted(root.rglob("*")):
        if dirpath.is_dir() and not _is_skipped_dir(dirpath, root) and list(dirpath.glob("*.tf")):
            errors.extend(lint_module(dirpath, is_root=(dirpath == target_root)))

    if list(root.glob("*.tf")):
        errors.extend(lint_module(root, is_root=(root == target_root)))

    return errors


def _self_test_basic(hcl_test: str) -> None:
    syntax_err1, prov1, back1 = _parse_hcl_content(hcl_test)
    assert not syntax_err1 and prov1 and back1, "Structured HCL parser must detect provider and backend"

    comments_test = '/* provider "aws" {} */\n# provider "google" {}\n// backend "s3" {}\n'
    syntax_err2, prov2, back2 = _parse_hcl_content(comments_test)
    assert not syntax_err2 and not prov2 and not back2, "Comments must not trigger false positives"

    syntax_err3, _, _ = _parse_hcl_content("locals { bad_syntax =")
    assert syntax_err3, "Syntax errors must be flagged as violation"

    unicode_hcl = 'resource "aws_s3_bucket" "\u092c\u093e\u0932\u094d\u091f\u0940" { bucket = "b" }\n'
    syntax_err4, prov4, _ = _parse_hcl_content(unicode_hcl)
    assert not syntax_err4 and not prov4, "Unicode identifier in string literal must parse cleanly"


def _self_test_directives_and_strings() -> None:
    opus_escape = 'locals {\n  x = "%%{ "\n}\nprovider "aws" {}\nlocals {\n  y = "a" # " }"\n}\n'
    v_esc, p_esc, _ = scan_tf_file(opus_escape)
    assert not v_esc and p_esc, "%%{ escape must detect provider"

    sol_multiline = (
        'locals {\n  x = "%{if\n var.a == "/*"}a%{endif}"\n}\n'
        'provider "aws" {}\n'
        'locals {\n  y = "%{if\n var.a == "*/z = "}b%{endif}"\n}\n'
    )
    v_sml, p_sml, _ = scan_tf_file(sol_multiline)
    assert not v_sml and p_sml, "Multiline template directive must detect provider"

    sol_heredoc_harmless = 'locals {\n  script = <<EOT\nif (ok) {\n}\nprovider "aws" {}\nEOT\n}\n'
    v_hh, p_hh, _ = scan_tf_file(sol_heredoc_harmless)
    assert not v_hh and not p_hh, "Harmless string with provider keyword must not trigger violation"


def _self_test_syntax_variations() -> None:
    sol_intervening = 'locals {\n  x = <<EOF\nEOF\n}\n# comment\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n'
    v_int, p_int, _ = scan_tf_file(sol_intervening)
    assert not v_int and p_int, "Intervening comment must not hide provider"

    sol_nospace = 'locals {\n  x = <<EOF\nEOF\n}\nprovider"aws"{}\nlocals {\n  y = <<EOF\nEOF\n}\n'
    v_ns, p_ns, _ = scan_tf_file(sol_nospace)
    assert not v_ns and p_ns, "No-space provider header must detect provider"

    bare_cr = "locals {\r x = 1\r}\r"
    v_bcr, _, _ = scan_tf_file(bare_cr)
    assert v_bcr, "Bare CR must be refused as syntax violation"

    double_bom = "\ufeff\ufefflocals { x = 1 }\n"
    v_dbom, _, _ = scan_tf_file(double_bom)
    assert v_dbom, "Double UTF-8 BOM must be refused as syntax violation"


def _self_test_attribute_collisions() -> None:
    collision_prov = 'provider "aws" {}\nprovider = {}\n'
    v_cp, p_cp, _ = scan_tf_file(collision_prov)
    assert not v_cp and p_cp, "Provider block/attribute collision must detect provider"

    collision_tf = 'terraform {\n  backend "s3" {}\n}\nterraform = {}\n'
    v_ctf, _, b_ctf = scan_tf_file(collision_tf)
    assert not v_ctf and b_ctf, "Terraform block/attribute collision must detect backend"

    sol_tf_empty_list = 'terraform {\n  backend "s3" {}\n}\nterraform = []\n'
    v_el, _, b_el = scan_tf_file(sol_tf_empty_list)
    assert not v_el and b_el, "terraform = [] must not erase backend block"

    sol_tf_obj_list = 'terraform {\n  backend "s3" {}\n}\nterraform = [{required_version = ">= 1.0"}]\n'
    v_ol, _, b_ol = scan_tf_file(sol_tf_obj_list)
    assert not v_ol and b_ol, "terraform = [{...}] must not erase backend block"


def _self_test_advanced(hcl_test: str) -> None:
    violation6, prov6, back6 = scan_tf_file(hcl_test)
    assert not violation6 and prov6 and back6, "scan_tf_file must detect provider and backend without violation"

    bom_case = '\ufeffprovider "aws" {}\n'
    v_bom, p_bom, _ = scan_tf_file(bom_case)
    assert not v_bom and p_bom, "UTF-8 BOM must be supported and detect provider"

    unterminated_case = "locals {\n  x = <<EOF\n}\n"
    v_unterminated, _, _ = scan_tf_file(unterminated_case)
    assert v_unterminated, "Unterminated heredoc with balanced braces must fail as syntax violation"

    sol_empty = 'locals {\n  x = <<EOF\nEOF\n}\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n'
    syntax_err_sol, prov_sol, _ = _parse_hcl_content(sol_empty)
    assert not syntax_err_sol and prov_sol, "Empty repeated heredocs must detect provider"

    sol_unclosed = 'locals {\n  a = <<EOF\nprovider "aws" {}\n'
    syntax_err_unc, _, _ = _parse_hcl_content(sol_unclosed)
    assert syntax_err_unc, "Unclosed heredoc must fail closed"

    _self_test_directives_and_strings()
    _self_test_syntax_variations()
    _self_test_attribute_collisions()


def _self_test_valid_cases(mod_p: Path) -> None:
    valid_cases = (
        'locals {\n  x = "%{if var.env == "prod"}yes%{endif}"\n}\n',
        'locals {\r\n  x = "hello"\r\n}\r\n',
        'locals {\n  x = "${replace("hello", "\\"", "")}" \n}\n',
        'locals {\n  x = "$${replace(\\"hello\\", \\"h\\", \\"j\\")}"\n}\n',
        'locals {\n  script = <<EOT\nif (ok) {\n}\nprovider "aws" {}\nEOT\n}\n',
    )
    main_tf = mod_p / "main.tf"
    for case in valid_cases:
        violation, _, _ = _parse_hcl_content(case)
        assert not violation, f"Valid case must not trigger violation: {case!r}"
        main_tf.write_text(case, encoding="utf-8")
        errs = lint_module(mod_p, is_root=False)
        assert errs == [], f"Valid case must succeed under child module lint: {case!r}, got: {errs}"


def _self_test_unparseable_cases() -> None:
    unparseable_cases = (
        "locals {\n  x = <<EOF\n}\n",
        'locals {\n  a = <<EOF\nprovider "aws" {}\n',
        "locals {\n  x = <<EOF\u00a0\nhello\nEOF\u00a0\n}\n",
        'locals {\n  x = "\n}\n',
        "locals { bad_syntax =",
        "locals {\r x = 1\r}\r",
        "\ufeff\ufefflocals { x = 1 }\n",
    )
    for unparseable in unparseable_cases:
        syntax_err, _, _ = _parse_hcl_content(unparseable)
        assert syntax_err, f"Unparseable input must fail closed as parse violation: {unparseable!r}"


def _self_test_adversarial_provider_cases(mod_p: Path) -> None:
    adversarial_provider_cases = (
        'locals {\n  x = <<EOF\nhello\nEOF\u00a0\n}\nprovider "aws" {}\nlocals {\n  y = <<EOF\nworld\nEOF\n}\n',
        'locals {\n  x = <<EOF\nEOF\n}\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n',
        'locals {\n  x = "%%{ "\n}\nprovider "aws" {}\nlocals {\n  y = "a" # " }"\n}\n',
        (
            'locals {\n  x = "%{if\n var.a == "/*"}a%{endif}"\n}\n'
            'provider "aws" {}\n'
            'locals {\n  y = "%{if\n var.a == "*/z = "}b%{endif}"\n}\n'
        ),
        (
            'locals {\n  x = "%{if var.a == "/*"}a%{endif}"\n}\n'
            'provider "aws" {}\n'
            'locals {\n  y = "%{if var.a == "*/z = "}b%{endif}"\n}\n'
        ),
        'provider "aws" {}\nprovider = {}\n',
        'locals {\n  x = <<EOF\nEOF\n}\n# comment\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n',
        'locals {\n  x = <<EOF\nEOF\n}\nprovider"aws"{}\nlocals {\n  y = <<EOF\nEOF\n}\n',
    )
    main_tf = mod_p / "main.tf"
    for adv_case in adversarial_provider_cases:
        main_tf.write_text(adv_case, encoding="utf-8")
        errs = lint_module(mod_p, is_root=False)
        assert any("provider configuration" in e for e in errs), (
            f"Adversarial provider block must be detected: {adv_case!r}, got: {errs}"
        )
        assert not any("unparseable HCL" in e for e in errs), (
            f"Valid adversarial case must not trigger unparseable HCL error: {adv_case!r}, got: {errs}"
        )


def _self_test_adversarial_backend_cases(mod_p: Path) -> None:
    adversarial_backend_cases = (
        'terraform {\n  backend "s3" {}\n}\nterraform = {}\n',
        'terraform {\n  backend "s3" {}\n}\nterraform = []\n',
        'terraform {\n  backend "s3" {}\n}\nterraform = [{required_version = ">= 1.0"}]\n',
    )
    main_tf = mod_p / "main.tf"
    for adv_case in adversarial_backend_cases:
        main_tf.write_text(adv_case, encoding="utf-8")
        errs = lint_module(mod_p, is_root=False)
        assert any("backend configuration" in e for e in errs), (
            f"Adversarial backend block must be detected: {adv_case!r}, got: {errs}"
        )
        assert not any("unparseable HCL" in e for e in errs), (
            f"Valid adversarial backend case must not trigger unparseable HCL error: {adv_case!r}, got: {errs}"
        )


def self_test() -> int:
    """Run internal regression self-tests using authoritative hclblocks parser."""
    hcl_test = 'provider "aws" {\n  region = "us-east-1"\n}\nterraform {\n  backend "s3" {\n    bucket = "b"\n  }\n}\n'
    _self_test_basic(hcl_test)

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        mod_p = Path(td)
        for req in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod_p / req).write_text("# req\n", encoding="utf-8")
        _self_test_valid_cases(mod_p)
        _self_test_adversarial_provider_cases(mod_p)
        _self_test_adversarial_backend_cases(mod_p)

    _self_test_unparseable_cases()
    _self_test_advanced(hcl_test)

    print("tf_module_layout: all self-tests passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for terraform module layout linter."""
    parser = argparse.ArgumentParser(description="Lint Terraform/OpenTofu module layout.")
    parser.add_argument("--self-test", action="store_true", help="Run internal self-tests.")
    parser.add_argument("path", nargs="?", default=".", help="Directory to lint.")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"Path does not exist: {target}", file=sys.stderr)
        return 1

    errors = lint_tree(target) if target.is_dir() else []
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
