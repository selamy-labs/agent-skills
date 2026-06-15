"""Tests for tools.tf_module_layout — the Terraform module layout lint."""

from __future__ import annotations

from pathlib import Path

from tools.tf_module_layout import lint_module, lint_tree

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestLintModuleGood:
    """A well-structured root module should pass cleanly."""

    def test_root_module_passes(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-good", is_root=True)
        assert errors == []

    def test_child_module_passes(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-good" / "modules" / "child")
        assert errors == []


class TestLintModuleUnexpectedFile:
    """Unexpected .tf filenames must be rejected."""

    def test_unexpected_file_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-unexpected")
        assert any("unexpected file" in e and "network.tf" in e for e in errors), errors


class TestLintModuleMissingFile:
    """Missing required files must be reported."""

    def test_missing_versions_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-missing")
        assert any("missing required file" in e and "versions.tf" in e for e in errors), errors


class TestLintModuleProviderInChild:
    """Provider/backend blocks in non-root modules must be rejected."""

    def test_provider_block_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-provider")
        assert any("provider configuration" in e for e in errors), errors

    def test_backend_block_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-provider")
        assert any("backend configuration" in e for e in errors), errors


class TestLintTree:
    """lint_tree should walk subdirectories and aggregate errors."""

    def test_good_tree_passes(self) -> None:
        errors = lint_tree(
            FIXTURES / "tf-layout-good",
            root_module=FIXTURES / "tf-layout-good",
        )
        assert errors == []

    def test_bad_tree_collects_errors(self) -> None:
        errors = lint_tree(FIXTURES / "tf-layout-bad-unexpected")
        assert len(errors) >= 1
        assert any("network.tf" in e for e in errors)
