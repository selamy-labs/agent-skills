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
import hashlib
import json
import os
import platform
import re
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
HCL2JSON_SHA256: dict[str, str] = {
    "linux_amd64": "b609e37094948c0b77cd8c6c0baaf8af2bd168c685c6e3334d671cc9692c34a5",
    "linux_arm64": "83ec38fa1583ea7dd5ceaa7fe63df0b3bc7beea6552554e2079bb178553251ca",
    "darwin_amd64": "dc1b8ef09746e8f498bc5eb7d949ad507a7ec5b3648fa3982cb97940251785cf",
    "darwin_arm64": "ec9306b47c1f8682e0717203ce893b8e5a60e0a5c0e181c4e85d9967657b98d2",
}


def _get_platform_key() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    arch = arch_map.get(machine)
    if system in ("linux", "darwin") and arch in ("amd64", "arm64"):
        return f"{system}_{arch}"
    return None


def get_hcl2json_binary_path() -> Path:
    custom = os.environ.get("HCL2JSON_PATH")
    if custom:
        return Path(custom)
    venv_bin = Path(sys.prefix) / "bin" / "hcl2json"
    if venv_bin.is_file():
        return venv_bin
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "tools" / "bin" / "hcl2json"


def _check_binary_path_safety(bin_path: Path) -> Path:
    resolved = bin_path.resolve()
    if not resolved.is_file():
        raise RuntimeError(f"hcl2json binary not found at {resolved}. Run scripts/install_hcl_parser.py")
    if resolved.parent in (Path("/tmp"), Path("/var/tmp")):
        raise RuntimeError(f"Refusing to execute shared /tmp binary: {resolved}")
    try:
        st = resolved.stat()
        if st.st_mode & 0o002:
            raise RuntimeError(f"Refusing to execute world-writable binary: {resolved}")
    except OSError as exc:
        raise RuntimeError(f"Cannot stat binary {resolved}: {exc}")
    return resolved


def _verify_and_get_hcl2json_binary() -> str:
    platform_key = _get_platform_key()
    if not platform_key or platform_key not in HCL2JSON_SHA256:
        raise RuntimeError(
            f"Unsupported platform for hcl2json: system={platform.system()}, machine={platform.machine()}"
        )
    expected_sha256 = HCL2JSON_SHA256[platform_key]

    bin_path = get_hcl2json_binary_path()
    resolved = _check_binary_path_safety(bin_path)

    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"Cannot read binary {resolved}: {exc}")

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"hcl2json SHA-256 mismatch for {resolved}: expected {expected_sha256}, got {actual_sha256}")
    return str(resolved)


def _inspect_provider_block(parsed: dict[str, Any]) -> tuple[bool, bool]:
    """Check provider block structure and detect collisions. Returns (has_collision, has_provider)."""
    if "provider" not in parsed:
        return False, False
    val = parsed["provider"]
    if isinstance(val, dict) and len(val) > 0 and all(isinstance(v, list) for v in val.values()):
        return False, True
    return True, False


def _inspect_single_tf_block(blk: Any) -> tuple[bool, bool]:
    if not isinstance(blk, dict):
        return False, False
    has_backend = "cloud" in blk
    if "backend" in blk:
        if isinstance(blk["backend"], dict):
            has_backend = True
        else:
            return True, True
    return False, has_backend


def _inspect_terraform_block(parsed: dict[str, Any]) -> tuple[bool, bool]:
    """Check terraform block structure and backend configuration. Returns (has_collision, has_backend)."""
    if "terraform" not in parsed:
        return False, False
    tf_val = parsed["terraform"]
    if not isinstance(tf_val, list):
        return True, False
    has_backend = False
    for blk in tf_val:
        coll, back = _inspect_single_tf_block(blk)
        if coll:
            return True, True
        if back:
            has_backend = True
    return False, has_backend


def _inspect_parsed_hcl(parsed: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Inspect parsed HCL structure for blocks and attribute collisions.

    Returns (has_collision, has_provider, has_backend).
    """
    prov_coll, has_provider = _inspect_provider_block(parsed)
    tf_coll, has_backend = _inspect_terraform_block(parsed)

    if "backend" in parsed:
        b_val = parsed["backend"]
        if isinstance(b_val, dict) and len(b_val) > 0 and all(isinstance(v, list) for v in b_val.values()):
            has_backend = True
        else:
            tf_coll = True

    if "cloud" in parsed:
        has_backend = True

    has_collision = prov_coll or tf_coll
    return has_collision, has_provider, has_backend


def _parse_hcl_content(content: str) -> tuple[bool, bool, bool]:
    """Parse raw HCL text using authoritative hcl2json parser.

    Returns (has_syntax_violation, has_provider, has_backend).
    Any unparseable content, safety error, or block-attribute collision fails closed as a violation.
    """
    try:
        bin_path = _verify_and_get_hcl2json_binary()
        proc = subprocess.run(
            [bin_path],
            input=content.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            return True, False, False
        parsed = json.loads(proc.stdout)
        if not isinstance(parsed, dict):
            return True, False, False
        has_collision, has_provider, has_backend = _inspect_parsed_hcl(parsed)
        if has_collision:
            return True, has_provider, has_backend
        return False, has_provider, has_backend
    except Exception:
        return True, False, False


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
    has_violation, has_provider, has_backend = _parse_hcl_content(content)
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
    syntax_err1, prov1, back1 = _parse_hcl_content(hcl_test)
    assert not syntax_err1 and prov1 and back1, "Structured HCL2 parser must detect provider and backend"

    comments_test = '/* provider "aws" {} */\n# provider "google" {}\n// backend "s3" {}\n'
    syntax_err2, prov2, back2 = _parse_hcl_content(comments_test)
    assert not syntax_err2 and not prov2 and not back2, "Comments must not trigger false positives"

    syntax_err3, _, _ = _parse_hcl_content("locals { bad_syntax =")
    assert syntax_err3, "Syntax errors must be flagged as violation"

    unicode_hcl = 'resource "aws_s3_bucket" "\u092c\u093e\u0932\u094d\u091f\u0940" { bucket = "b" }\n'
    syntax_err4, prov4, _ = _parse_hcl_content(unicode_hcl)
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

    sol_intervening = 'locals {\n  x = <<EOF\nEOF\n}\n# comment\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n'
    v_int, p_int, _ = scan_tf_file(sol_intervening)
    assert not v_int and p_int, "Intervening comment must not hide provider"

    sol_nospace = 'locals {\n  x = <<EOF\nEOF\n}\nprovider"aws"{}\nlocals {\n  y = <<EOF\nEOF\n}\n'
    v_ns, p_ns, _ = scan_tf_file(sol_nospace)
    assert not v_ns and p_ns, "No-space provider header must detect provider"

    collision_prov = 'provider "aws" {}\nprovider = {}\n'
    v_cp, p_cp, _ = scan_tf_file(collision_prov)
    assert v_cp or p_cp, "Provider block/attribute collision must fail closed"

    collision_tf = 'terraform {\n  backend "s3" {}\n}\nterraform = {}\n'
    v_ctf, _, b_ctf = scan_tf_file(collision_tf)
    assert v_ctf or b_ctf, "Terraform block/attribute collision must fail closed"

    bare_cr = "locals {\r x = 1\r}\r"
    v_bcr, _, _ = scan_tf_file(bare_cr)
    assert v_bcr, "Bare CR must be refused as syntax violation"

    double_bom = "\ufeff\ufefflocals { x = 1 }\n"
    v_dbom, _, _ = scan_tf_file(double_bom)
    assert v_dbom, "Double UTF-8 BOM must be refused as syntax violation"


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
        "locals {\r x = 1\r}\r",
        "\ufeff\ufefflocals { x = 1 }\n",
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
        'provider "aws" {}\nprovider = {}\n',
        'terraform {\n  backend "s3" {}\n}\nterraform = {}\n',
        'locals {\n  x = <<EOF\nEOF\n}\n# comment\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n',
        'locals {\n  x = <<EOF\nEOF\n}\nprovider"aws"{}\nlocals {\n  y = <<EOF\nEOF\n}\n',
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
