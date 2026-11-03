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
import re
import sys
from pathlib import Path
from typing import Any

try:
    from hcl2.api import loads as hcl2_loads
except ModuleNotFoundError:
    for _sp in (Path(__file__).resolve().parents[1] / ".venv" / "lib").glob("python*/site-packages"):
        if _sp.is_dir() and str(_sp) not in sys.path:
            sys.path.insert(0, str(_sp))
    from hcl2.api import loads as hcl2_loads

CORE_REQUIRED_FILES = {"main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"}
ROOT_ONLY_FILES = {"providers.tf", "backend.tf"}
PROHIBITED_DIR_NAMES = {"envs", "environments"}
ALLOWED_NON_TF_FILES = {"README.md", "LICENSE", ".terraform.lock.hcl"}

TF_FILENAME_RE = re.compile(r"^[a-z0-9_]+\.tf$")


def _preprocess_fallback_hcl(text: str) -> str:
    """Fallback normalization for HCL2 inner quotes inside unescaped ${...} interpolations."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?m)^([ \t]*[a-zA-Z0-9_.-]+)[ \t]+$", r"\1", text)
    text = "".join(c if ord(c) < 128 else f"u{ord(c):04x}" for c in text)
    text = re.sub(r"((?<!\$)\$\{[\s\S]*?\})", lambda m: m.group(0).replace(r"\"", '"'), text)
    if not text.endswith("\n"):
        text += "\n"
    return text


_preprocess_hcl2_content = _preprocess_fallback_hcl


def _fallback_scan_blocks(text: str) -> tuple[bool, bool]:
    has_provider = bool(re.search(r'(?m)^[ \t]*provider[ \t]+("[^"]*"|[a-zA-Z0-9_-]+)[ \t]*\{', text))
    has_backend = bool(re.search(r'(?m)^[ \t]*(backend|cloud)[ \t]+("[^"]*"|[a-zA-Z0-9_-]+)[ \t]*\{', text))
    if not has_backend and bool(re.search(r"(?m)^[ \t]*terraform[ \t]*\{", text)):
        if "backend" in text or "cloud" in text:
            has_backend = True
    return has_provider, has_backend


def _inspect_parsed_terraform_blocks(tf_blocks: list[Any]) -> bool:
    """Check if any terraform block contains backend or cloud configuration."""
    for tf_block in tf_blocks:
        if isinstance(tf_block, dict) and ("backend" in tf_block or "cloud" in tf_block):
            return True
    return False


def _inspect_parsed_hcl(parsed: dict[str, Any]) -> tuple[bool, bool]:
    has_provider = bool(parsed.get("provider", []))
    has_backend = bool(parsed.get("backend", [])) or bool(parsed.get("cloud", []))
    if _inspect_parsed_terraform_blocks(parsed.get("terraform", [])):
        has_backend = True
    return has_provider, has_backend


def _parse_hcl2_content(content: str) -> tuple[bool, bool, bool]:
    """Parse HCL2 text using structured python-hcl2 parser.

    Returns (has_syntax_violation, has_provider, has_backend).
    """
    try:
        parsed = hcl2_loads(content)
        if isinstance(parsed, dict):
            has_provider, has_backend = _inspect_parsed_hcl(parsed)
            return False, has_provider, has_backend
    except Exception:
        pass

    try:
        normalized = _preprocess_fallback_hcl(content)
        parsed = hcl2_loads(normalized)
        if isinstance(parsed, dict):
            has_provider, has_backend = _inspect_parsed_hcl(parsed)
            return False, has_provider, has_backend
    except Exception:
        pass

    p, b = _fallback_scan_blocks(content)
    return True, p, b


def scan_tf_file(content: str) -> tuple[bool, bool]:
    """Scan HCL text for active provider or backend blocks."""
    _, has_provider, has_backend = _parse_hcl2_content(content)
    return (has_provider, has_backend)


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
        content = tf_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{tf_path}: unreadable file ({exc})")
        return
    has_violation, has_provider, has_backend = _parse_hcl2_content(content)
    if has_violation:
        errors.append(f"{tf_path}: unclosed or malformed heredoc in child module")
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


def self_test() -> int:
    """Run internal regression self-tests using python-hcl2 parser."""
    hcl_test = 'provider "aws" { region = "us-east-1" }\nterraform { backend "s3" { bucket = "b" } }\n'
    syntax_err1, prov1, back1 = _parse_hcl2_content(hcl_test)
    assert not syntax_err1 and prov1 and back1, "Structured HCL2 parser must detect provider and backend"

    comments_test = '/* provider "aws" {} */\n# provider "google" {}\n// backend "s3" {}\n'
    syntax_err2, prov2, back2 = _parse_hcl2_content(comments_test)
    assert not syntax_err2 and not prov2 and not back2, "Comments must not trigger false positives"

    syntax_err3, _, _ = _parse_hcl2_content("locals { bad_syntax =")
    assert syntax_err3, "Syntax errors must be flagged as violation"

    unicode_hcl = 'resource "aws_s3_bucket" "\u092c\u093e\u0932\u094d\u091f\u0940" { bucket = "b" }\n'
    syntax_err4, prov4, _ = _parse_hcl2_content(unicode_hcl)
    assert not syntax_err4 and not prov4, "Unicode identifier HCL2 must parse cleanly"

    as_ts_r8_001_cases = (
        'locals {\n  x = "%{if var.env == \\"prod\\"}yes%{endif}"\n}\n',
        (
            'locals {\n  \u092c\u093e\u0932\u094d\u091f\u0940 = "b"\n'
            '  x = "${local.\u092c\u093e\u0932\u094d\u091f\u0940}"\n}\n'
        ),
        'locals {\n  x = "${<<E\u0301OF\nhello\nE\u0301OF\n}"\n}\n',
        'locals {\n  x = "${join(\\"\\", [<<\u2118OF\n{\n\u2118OF\n])}"\n}\n',
        "locals {\n  x = <<EOF\nhello\nEOF   \n}\n",
        'locals {\r\n  x = "hello"\r\n}\r\n',
        'locals {\n  x = "${replace("hello", "\\"", "")}" \n}\n',
        'locals {\n  x = "$${replace(\\"hello\\", \\"h\\", \\"j\\")}"\n}\n',
    )
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        mod_p = Path(td)
        for req in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod_p / req).write_text("# req\n", encoding="utf-8")
        main_tf = mod_p / "main.tf"
        for case in as_ts_r8_001_cases:
            violation, _, _ = _parse_hcl2_content(case)
            assert not violation, f"AS-TS-R8-001 case must not trigger hcl2 violation: {case!r}"
            main_tf.write_text(case, encoding="utf-8")
            errs = lint_module(mod_p, is_root=False)
            assert errs == [], f"AS-TS-R8-001 case must succeed under child module lint: {case!r}, got: {errs}"

    prov6, back6 = scan_tf_file(hcl_test)
    assert prov6 and back6, "scan_tf_file must detect provider and backend"

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
