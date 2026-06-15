"""Terraform/OpenTofu module layout lint.

Validates that Terraform modules follow the canonical file layout:

  Required in every module:
    main.tf, variables.tf, outputs.tf, locals.tf, versions.tf

  Optional (exactly one allowed extra):
    data.tf

  Root modules only (is_root=True):
    providers.tf, backend.tf

  Modules must NOT contain provider or backend configuration blocks.

Designed as a reusable tool consumed by CI, pre-commit, and agent skills.
"""

from __future__ import annotations

import re
from pathlib import Path

REQUIRED_FILES = {"main.tf", "variables.tf", "outputs.tf", "locals.tf", "versions.tf"}
OPTIONAL_FILES = {"data.tf"}
ROOT_ONLY_FILES = {"providers.tf", "backend.tf"}
ALL_ALLOWED = REQUIRED_FILES | OPTIONAL_FILES | ROOT_ONLY_FILES

PROVIDER_BLOCK_RE = re.compile(r'^\s*provider\s+"[^"]+"\s*\{', re.MULTILINE)
BACKEND_BLOCK_RE = re.compile(r'^\s*backend\s+"[^"]+"\s*\{', re.MULTILINE)


def lint_module(module_dir: Path, *, is_root: bool = False) -> list[str]:
    """Lint a single Terraform module directory. Returns a list of error strings."""
    errors: list[str] = []

    tf_files = {p.name for p in module_dir.glob("*.tf")}

    # Check for unexpected files.
    allowed = ALL_ALLOWED if is_root else REQUIRED_FILES | OPTIONAL_FILES
    unexpected = tf_files - allowed
    for name in sorted(unexpected):
        errors.append(f"{module_dir}: unexpected file {name!r} (allowed: {sorted(allowed)})")

    # Check required files are present.
    missing = REQUIRED_FILES - tf_files
    for name in sorted(missing):
        errors.append(f"{module_dir}: missing required file {name!r}")

    # Non-root modules must not contain provider or backend blocks.
    if not is_root:
        for tf_path in sorted(module_dir.glob("*.tf")):
            try:
                content = tf_path.read_text()
            except OSError:
                continue
            if PROVIDER_BLOCK_RE.search(content):
                errors.append(f"{tf_path}: provider configuration in non-root module")
            if BACKEND_BLOCK_RE.search(content):
                errors.append(f"{tf_path}: backend configuration in non-root module")

    return errors


def lint_tree(root: Path, *, root_module: Path | None = None) -> list[str]:
    """Walk a directory tree and lint all Terraform modules found.

    A directory is considered a module if it contains at least one .tf file.
    *root_module* marks which directory is the root module (gets provider/backend
    allowance). If None, the *root* directory itself is treated as root module.
    """
    if root_module is None:
        root_module = root

    errors: list[str] = []
    for dirpath in sorted(root.rglob("*")):
        if not dirpath.is_dir():
            continue
        if any(part.startswith(".") for part in dirpath.relative_to(root).parts):
            continue
        if list(dirpath.glob("*.tf")):
            errors.extend(lint_module(dirpath, is_root=(dirpath == root_module)))

    # Also lint root itself if it has .tf files.
    if list(root.glob("*.tf")):
        errors.extend(lint_module(root, is_root=(root == root_module)))

    return errors
