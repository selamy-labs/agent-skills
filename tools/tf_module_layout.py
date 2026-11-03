r"""Terraform/OpenTofu module layout lint.

Validates that Terraform modules follow Google's canonical file layout:

  Required core files in every module:
    main.tf, variables.tf, outputs.tf, versions.tf, README.md

  Permitted resource and configuration files:
    Logical resource files matching [a-z0-9_]+\.tf (e.g. network.tf,
    firewall.tf, iam.tf, storage.tf, compute.tf, locals.tf, data.tf)

  Root modules only (is_root=True):
    providers.tf, backend.tf, and reviewed .tfvars files

  Prohibited in modules:
    Environment root directories (envs/, environments/), .tfstate files.
    Child modules must NOT contain active provider/backend blocks or .tfvars.

Designed as a reusable tool consumed by CI, pre-commit, and agent skills.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CORE_REQUIRED_FILES = {"main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"}
ROOT_ONLY_FILES = {"providers.tf", "backend.tf"}
PROHIBITED_DIR_NAMES = {"envs", "environments"}
ALLOWED_NON_TF_FILES = {"README.md", "LICENSE", ".terraform.lock.hcl"}

TF_FILENAME_RE = re.compile(r"^[a-z0-9_]+\.tf$")

HCL2JSON_VERSION = "0.6.9"
HCL2JSON_SHA256: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "b609e37094948c0b77cd8c6c0baaf8af2bd168c685c6e3334d671cc9692c34a5",
    ("linux", "aarch64"): "cb2ccf59a830829fe122e8dfb85d6ef2500aceda5af45a7c1052a4d2c46541f9",
    ("darwin", "x86_64"): "4e5dd67f8295d7c1d34b7c98b4d1d9d811805fc2878250e1d3771a9ade834759",
    ("darwin", "arm64"): "f7aa91e95bf5da5006de866aad6c757b4378f8f016387673f6425d5276e4fc11",
}


def _download_hcl2json(dest: Path) -> str:
    import hashlib
    import platform
    import urllib.request

    os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    arch = platform.machine()
    arch_suffix = "amd64" if arch == "x86_64" else "arm64"
    asset_name = f"hcl2json_{os_name}_{arch_suffix}"
    url = f"https://github.com/tmccombs/hcl2json/releases/download/v{HCL2JSON_VERSION}/{asset_name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    dest.chmod(0o755)
    plat_key = ("linux" if sys.platform.startswith("linux") else sys.platform, arch)
    expected = HCL2JSON_SHA256.get(plat_key)
    if expected:
        actual = hashlib.sha256(dest.read_bytes()).hexdigest()
        if actual != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"hcl2json checksum mismatch: expected {expected}, got {actual}")
    return str(dest)


def _find_hcl2json_binary() -> str:
    candidates = [
        shutil.which("hcl2json"),
        str(Path(sys.prefix) / "bin" / "hcl2json"),
        str(Path.home() / ".local" / "bin" / "hcl2json"),
        str(Path(__file__).resolve().parent / "bin" / "hcl2json"),
        "/usr/local/bin/hcl2json",
        "/tmp/hcl2json",
    ]
    for c in candidates:
        if c:
            p = Path(c)
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    cache_path = Path.home() / ".cache" / "hcl2json" / f"hcl2json-{HCL2JSON_VERSION}"
    if cache_path.is_file() and os.access(cache_path, os.X_OK):
        return str(cache_path)
    return _download_hcl2json(cache_path)


def _inspect_parsed_terraform_blocks(tf_blocks: list[Any]) -> bool:
    """Check if any terraform block contains backend or cloud configuration."""
    for tf_block in tf_blocks:
        if isinstance(tf_block, dict) and ("backend" in tf_block or "cloud" in tf_block):
            return True
    return False


def _inspect_parsed_hcl(parsed: dict[str, Any]) -> tuple[bool, bool]:
    has_provider = bool(parsed.get("provider"))
    has_backend = bool(parsed.get("backend")) or bool(parsed.get("cloud"))
    tf_val = parsed.get("terraform")
    if isinstance(tf_val, list):
        if _inspect_parsed_terraform_blocks(tf_val):
            has_backend = True
    elif isinstance(tf_val, dict):
        if "backend" in tf_val or "cloud" in tf_val:
            has_backend = True
    return has_provider, has_backend


def _parse_hcl_content(content: str) -> tuple[bool, bool, bool]:
    """Parse HCL text using authoritative hcl2json parser.

    Returns (has_syntax_violation, has_provider, has_backend).
    Any unparseable content fails closed as a syntax violation.
    """
    clean_text = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not clean_text.endswith("\n"):
        clean_text += "\n"

    try:
        bin_path = _find_hcl2json_binary()
        proc = subprocess.run(
            [bin_path],
            input=clean_text.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            return True, False, False
        parsed = json.loads(proc.stdout)
        if not isinstance(parsed, dict):
            return True, False, False
        has_provider, has_backend = _inspect_parsed_hcl(parsed)
        return False, has_provider, has_backend
    except Exception:
        return True, False, False


_parse_hcl2_content = _parse_hcl_content


def scan_tf_file(content: str) -> tuple[bool, bool, bool]:
    """Scan HCL text for syntax violations and active provider or backend blocks."""
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
        content = tf_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{tf_path}: unreadable file ({exc})")
        return
    has_violation, has_provider, has_backend = _parse_hcl2_content(content)
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
    errors = _check_required_files(module_dir)
    errors.extend(_check_prohibited_dirs(module_dir))
    errors.extend(_check_files(module_dir, is_root=is_root))
    if not is_root:
        errors.extend(_check_child_blocks(module_dir))
    return errors


def _is_skipped_dir(dirpath: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in dirpath.relative_to(root).parts)


def lint_tree(root: Path, *, root_module: Path | None = None) -> list[str]:
    """Walk a directory tree and lint all Terraform modules found.

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
    syntax_err1, prov1, back1 = _parse_hcl2_content(hcl_test)
    assert not syntax_err1 and prov1 and back1, "Structured HCL2 parser must detect provider and backend"

    comments_test = '/* provider "aws" {} */\n# provider "google" {}\n// backend "s3" {}\n'
    syntax_err2, prov2, back2 = _parse_hcl2_content(comments_test)
    assert not syntax_err2 and not prov2 and not back2, "Comments must not trigger false positives"

    syntax_err3, _, _ = _parse_hcl2_content("locals { bad_syntax =")
    assert syntax_err3, "Syntax errors must be flagged as violation"

    unicode_hcl = 'resource "aws_s3_bucket" "\u092c\u093e\u0932\u094d\u091f\u0940" { bucket = "b" }\n'
    syntax_err4, prov4, _ = _parse_hcl2_content(unicode_hcl)
    assert not syntax_err4 and not prov4, "Unicode identifier in string literal must parse cleanly"


def _self_test_advanced(hcl_test: str) -> None:
    violation6, prov6, back6 = scan_tf_file(hcl_test)
    assert not violation6 and prov6 and back6, "scan_tf_file must detect provider and backend without violation"

    bom_case = '\ufeffprovider "aws" {}\n'
    v_bom, p_bom, _ = scan_tf_file(bom_case)
    assert not v_bom and p_bom, "UTF-8 BOM must be stripped and detect provider"

    unterminated_case = "locals {\n  x = <<EOF\n}\n"
    v_unterminated, _, _ = scan_tf_file(unterminated_case)
    assert v_unterminated, "Unterminated heredoc with balanced braces must fail as syntax violation"

    sol_empty = 'locals {\n  x = <<EOF\nEOF\n}\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n'
    syntax_err_sol, prov_sol, _ = _parse_hcl_content(sol_empty)
    assert not syntax_err_sol and prov_sol, "Empty repeated heredocs must detect provider"

    sol_unclosed = 'locals {\n  a = <<EOF\nprovider "aws" {}\n'
    syntax_err_unc, _, _ = _parse_hcl_content(sol_unclosed)
    assert syntax_err_unc, "Unclosed heredoc must fail closed"

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


def self_test() -> int:
    """Run internal regression self-tests using authoritative hcl2json parser."""
    hcl_test = 'provider "aws" {\n  region = "us-east-1"\n}\nterraform {\n  backend "s3" {\n    bucket = "b"\n  }\n}\n'
    _self_test_basic(hcl_test)

    valid_cases = (
        'locals {\n  x = "%{if var.env == "prod"}yes%{endif}"\n}\n',
        'locals {\r\n  x = "hello"\r\n}\r\n',
        'locals {\n  x = "${replace("hello", "\\"", "")}" \n}\n',
        'locals {\n  x = "$${replace(\\"hello\\", \\"h\\", \\"j\\")}"\n}\n',
        'locals {\n  script = <<EOT\nif (ok) {\n}\nprovider "aws" {}\nEOT\n}\n',
    )
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        mod_p = Path(td)
        for req in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod_p / req).write_text("# req\n", encoding="utf-8")
        main_tf = mod_p / "main.tf"
        for case in valid_cases:
            violation, _, _ = _parse_hcl_content(case)
            assert not violation, f"Valid case must not trigger violation: {case!r}"
            main_tf.write_text(case, encoding="utf-8")
            errs = lint_module(mod_p, is_root=False)
            assert errs == [], f"Valid case must succeed under child module lint: {case!r}, got: {errs}"

    unparseable_cases = (
        "locals {\n  x = <<EOF\n}\n",
        'locals {\n  a = <<EOF\nprovider "aws" {}\n',
        "locals {\n  x = <<EOF\u00a0\nhello\nEOF\u00a0\n}\n",
        'locals {\n  x = "\n}\n',
        "locals { bad_syntax =",
    )
    for unparseable in unparseable_cases:
        syntax_err, _, _ = _parse_hcl_content(unparseable)
        assert syntax_err, f"Unparseable input must fail closed as parse violation: {unparseable!r}"

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
    )
    with tempfile.TemporaryDirectory() as td:
        mod_p = Path(td)
        for req in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod_p / req).write_text("# req\n", encoding="utf-8")
        main_tf = mod_p / "main.tf"
        for adv_case in adversarial_provider_cases:
            main_tf.write_text(adv_case, encoding="utf-8")
            errs = lint_module(mod_p, is_root=False)
            assert any("provider configuration" in e or "unparseable HCL (refused)" in e for e in errs), (
                f"Adversarial provider block must be detected: {adv_case!r}, got: {errs}"
            )

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
