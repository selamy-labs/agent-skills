"""Tests for tools.tf_module_layout — the Terraform module layout lint."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.tf_module_layout import lint_module, lint_tree, scan_tf_file

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestLintModuleGood:
    """A well-structured root or child module should pass cleanly."""

    def test_root_module_passes(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-good", is_root=True)
        assert errors == []

    def test_child_module_passes(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-good" / "modules" / "child")
        assert errors == []

    def test_valid_module_with_network_tf_passes(self) -> None:
        """Requirement 1a: A valid module containing network.tf passes with 0 errors."""
        errors = lint_module(FIXTURES / "tf-layout-good-network")
        assert errors == [], f"Expected 0 errors for module with network.tf, got: {errors}"

    def test_valid_module_with_multiple_logical_tf_files(self, tmp_path: Path) -> None:
        """Google style guide allows logical grouping: network.tf, iam.tf, firewall.tf, etc."""
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core file\n", encoding="utf-8")
        logical_files = (
            "network.tf",
            "firewall.tf",
            "iam.tf",
            "dns.tf",
            "storage.tf",
            "compute.tf",
            "locals.tf",
            "data.tf",
        )
        for logical in logical_files:
            (tmp_path / logical).write_text("# logical file\n", encoding="utf-8")

        errors = lint_module(tmp_path)
        assert errors == [], f"Expected 0 errors for logical resource files, got: {errors}"


class TestLintModuleUnexpectedFile:
    """Unexpected files must be rejected."""

    def test_unexpected_file_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-unexpected")
        assert any("unexpected file" in e and "foo.txt" in e for e in errors), errors


class TestLintModuleCommentedProvider:
    """Requirement 1b: Commented provider/backend blocks must not be flagged."""

    def test_block_commented_provider_passes(self, tmp_path: Path) -> None:
        """A main.tf containing /* provider "google" { project = "x" } */ passes with 0 errors."""
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            '/* provider "google" { project = "x" } */\n/*\nprovider "aws" {\n  region = "us-east-1"\n}\n*/\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for block-commented provider, got: {errors}"

    def test_line_commented_provider_passes(self, tmp_path: Path) -> None:
        """Line-commented providers (# and //) must not trigger false positive."""
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            '# provider "google" { project = "x" }\n'
            '// provider "google" { project = "x" }\n'
            '// backend "gcs" { bucket = "b" }\n'
            '# backend "s3" { bucket = "b" }\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for line-commented provider/backend, got: {errors}"

    def test_comment_stripping_preserves_strings(self) -> None:
        """Verify comments inside and outside strings are handled correctly."""
        hcl = 'val = "https://example.com#anchor"\n# real comment\n/* block */\n'
        violation, prov, back = scan_tf_file(hcl)
        assert not violation and not prov and not back


class TestLintModuleProviderInChild:
    """Requirement 1c: Uncommented provider/backend blocks in child modules must be rejected."""

    def test_provider_block_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-provider", is_root=False)
        assert any("provider configuration" in e for e in errors), errors

    def test_backend_block_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-provider", is_root=False)
        assert any("backend configuration" in e for e in errors), errors

    def test_uncommented_provider_in_custom_module_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text('provider "google" {\n  project = "demo"\n}\n', encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_uncommented_backend_in_child_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text('backend "gcs" {\n  bucket = "my-bucket"\n}\n', encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("backend configuration in non-root module" in e for e in errors), errors

    def test_single_line_backend_in_child_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text('backend "gcs" { bucket = "b" }\n', encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("backend configuration in non-root module" in e for e in errors), errors

    def test_unquoted_provider_label_in_child_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text('provider google {\n  project = "demo"\n}\n', encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_heredoc_with_provider_and_backend_passes(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'output "example" {\n  value = <<-EOF\nprovider "aws" {}\nbackend "s3" {}\nEOF\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for heredoc containing provider/backend, got: {errors}"

    def test_variable_and_output_names_do_not_falsely_flag(self, tmp_path: Path) -> None:
        for name in ("versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "variables.tf").write_text('variable "backend" {\n  type = string\n}\n', encoding="utf-8")
        (tmp_path / "outputs.tf").write_text('output "provider" {\n  value = "aws"\n}\n', encoding="utf-8")
        (tmp_path / "main.tf").write_text("# main\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for variables/outputs named backend/provider, got: {errors}"

    def test_comment_with_heredoc_opener_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            '# <<EOF\nprovider "google" {\n  project = "demo"\n}\n# EOF\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_block_comment_with_heredoc_opener_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            '/* <<EOF */\nprovider "google" {\n  project = "demo"\n}\n/* EOF */\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_heredoc_with_delimiter_prefix_in_body_passes(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'output "example" {\n  value = <<-EOF\nline 1\nEOF_PREFIX is not the delimiter\nline 2\nEOF\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for heredoc containing delimiter prefix, got: {errors}"

    def test_multiline_parenthesized_expression_before_backend_in_child_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        hcl_content = (
            'terraform {\n  required_version = (\n    ">= 1.0.0"\n  )\n'
            '  backend "gcs" {\n    bucket = "my-bucket"\n  }\n}\n'
        )
        (tmp_path / "main.tf").write_text(hcl_content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("backend configuration in non-root module" in e for e in errors), errors

    def test_multiline_bracketed_expression_before_backend_in_child_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'terraform {\n  experiments = [\n    "example",\n  ]\n  backend "s3" {\n    bucket = "my-bucket"\n  }\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("backend configuration in non-root module" in e for e in errors), errors

    def test_hyphenated_heredoc_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  script = <<-END-TEXT\nhello\nEND-TEXT\n}\nprovider "google" {\n  project = "demo"\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_indented_heredoc_terminator_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = <<EOF\nhello\n  EOF\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_string_template_interpolation_braces_do_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  script = "${join("{", ["hello"])}"\n}\nprovider "google" {\n  project = "demo"\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unicode_heredoc_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = <<-ПОСТАВЩИК\n{\nПОСТАВЩИК\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_unicode_first_char_heredoc_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = <<-ÉOF\n{\nÉOF\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_unicode_tail_heredoc_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = <<-EOFé\n{\nEOFé\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_nested_heredoc_in_interpolation_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = "${<<EOF\n{\nEOF\n}"\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_nested_heredoc_with_quote_in_interpolation_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = "${<<EOF\n"\nEOF\n}"\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_unicode_combining_mark_heredoc_delimiter_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<E\u0301OF\nhello\nE\u0301OF\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_unicode_connector_punctuation_heredoc_delimiter_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<E\u203fOF\nhello\nE\u203fOF\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_devanagari_combining_mark_heredoc_delimiter_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<\u0905\u0902\u0924\nhello\n\u0905\u0902\u0924\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_scan_tf_file_detects_providers_with_unicode_continuations(self) -> None:
        case_combining = 'locals {\n  note = <<E\u0301OF\nhello\nE\u0301OF\n}\nprovider "google" {}\n'
        assert scan_tf_file(case_combining) == (False, True, False)

        case_connector = 'locals {\n  note = <<E\u203fOF\nhello\nE\u203fOF\n}\nprovider "google" {}\n'
        assert scan_tf_file(case_connector) == (False, True, False)

        case_devanagari = (
            'locals {\n  note = <<\u0905\u0902\u0924\nhello\n\u0905\u0902\u0924\n}\nprovider "google" {}\n'
        )
        assert scan_tf_file(case_devanagari) == (False, True, False)

    def test_nested_unicode_heredoc_in_interpolation_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = "${<<E\u0301OF\nhello\nE\u0301OF\n}"\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_unclosed_heredoc_fails_closed_and_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<EOF\nhello\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors, "Expected errors for unclosed heredoc with provider"
        assert any("unparseable HCL (refused)" in e for e in errors) and not any(
            "provider configuration" in e for e in errors
        ), errors

    def test_unclosed_heredoc_without_provider_fails_closed(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            "locals {\n  note = <<EOF\nhello\n}\n",
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors, "Expected errors for unclosed heredoc"
        assert any("unparseable HCL (refused)" in e for e in errors), errors

    def test_valid_unicode_continuation_heredoc_without_provider_passes(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        main_tf_content = (
            "locals {\n"
            "  a = <<-E\u0301OF\nhello\n  E\u0301OF\n"
            "  b = <<-E\u203fOF\nhello\n  E\u203fOF\n"
            "  c = <<-\u0905\u0902\u0924\nhello\n  \u0905\u0902\u0924\n"
            "}\n"
        )
        (tmp_path / "main.tf").write_text(main_tf_content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected clean pass for valid unicode continuation heredoc, got: {errors}"

    def test_unicode_heredoc_without_provider_passes(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            "locals {\n  x = <<-ПОСТАВЩИК\nhello\nПОСТАВЩИК\n}\n",
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected clean pass for valid unicode heredoc, got: {errors}"

    def test_nested_heredoc_without_provider_passes(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = "${<<EOF\nhello\nEOF\n}"\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for nested heredoc without provider, got: {errors}"

    def test_string_template_without_provider_passes(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  script = "${join("provider", ["hello"])}"\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for template string without provider, got: {errors}"

    def test_cloud_backend_in_child_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'terraform {\n  cloud {\n    organization = "my-org"\n  }\n}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("backend configuration in non-root module" in e for e in errors), errors

    def test_unreadable_file_reported_as_error(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_bytes(b"\x80\x81\x82\xff")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unreadable file" in e for e in errors), errors

    def test_heredoc_opener_with_trailing_chars_rejected(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text("locals {\n  x = <<EOF # comment\nhello\nEOF\n}\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors), errors

    def test_heredoc_opener_with_trailing_spaces_rejected(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text("locals {\n  x = <<EOF   \nhello\nEOF\n}\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors), errors

    def test_multiline_interp_with_eof_line_does_not_prematurely_close_heredoc(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = (
            'locals {\n  obj = { EOF = "val" }\n  x = <<EOF\n${local.obj.\nEOF\n}\n{{\nEOF\n}\nprovider "google" {}\n'
        )
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_nested_heredoc_with_outer_delim_in_body_does_not_prematurely_close(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = 'locals {\n  x = <<OUTER\n${<<INNER\nOUTER\nINNER\n}\n{{\nOUTER\n}\nprovider "google" {}\n'
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_block_comment_in_interp_with_eof_does_not_prematurely_close(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = 'locals {\n  x = <<EOF\n${/*\nEOF\n*/ "ok"}\n{{\nEOF\n}\nprovider "google" {}\n'
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_unclosed_nested_heredoc_in_quoted_string_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text('locals {\n  x = "${<<EOF\nhello\n}"\n}\n', encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors), errors

    def test_unicode_other_id_in_nested_heredoc_detects_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = 'locals { x = "${join(\\"\\", [<<\u2118OF\n{\n\u2118OF\n])}" }\nprovider "google" {}\n'
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors) and not any(
            "provider configuration" in e for e in errors
        ), errors

    def test_self_test_cli(self) -> None:
        from tools.tf_module_layout import main as tf_main

        rc = tf_main(["--self-test"])
        assert rc == 0


class TestHclStructuredParser:
    """Validate hclblocks structured parsing integration."""

    def test_block_header_inline_comments(self) -> None:
        from tools.tf_module_layout import scan_tf_file

        for snippet in (
            'provider /* inline */ "aws" {}\n',
            'provider/**/"aws" {}\n',
            'provider /* comment */ /* another */ "aws" {}\n',
        ):
            violation, has_prov, has_back = scan_tf_file(snippet)
            assert not violation and has_prov and not has_back, f"inline header comments parse cleanly: {snippet!r}"

    def test_utf8_bom_handling(self) -> None:
        from tools.tf_module_layout import scan_tf_file

        v1, p1, b1 = scan_tf_file('\ufeffprovider "aws" {}\n')
        assert not v1 and p1 and not b1

        v2, p2, b2 = scan_tf_file('\ufeffterraform {\n  backend "s3" {}\n}\n')
        assert not v2 and not p2 and b2

    def test_unterminated_heredoc_with_balanced_braces_fails_closed(self) -> None:
        from tools.tf_module_layout import scan_tf_file

        for snippet in ("locals {\n  x = <<EOF\n}\n", "locals {\n  x = <<-EOF\n}\n"):
            violation, _, _ = scan_tf_file(snippet)
            assert violation, f"Unterminated heredoc must fail as syntax violation: {snippet!r}"

    def test_parse_hcl_content_detects_blocks(self) -> None:
        from tools.tf_module_layout import _parse_hcl_content

        hcl = 'provider "google" {}\nterraform {\n  backend "gcs" {\n    bucket = "b"\n  }\n}\n'
        violation, prov, back = _parse_hcl_content(hcl)
        assert not violation
        assert prov
        assert back

    def test_parse_hcl_content_syntax_error(self) -> None:
        from tools.tf_module_layout import _parse_hcl_content

        violation, _, _ = _parse_hcl_content("locals { x =")
        assert violation

    def test_syntax_error_in_child_module_flagged(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text("locals {\n  bad_syntax =\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors), errors

    @pytest.mark.parametrize(
        "case_name,content",
        [
            ("directive_quotes", 'locals {\n  x = "%{if var.env == "prod"}yes%{endif}"\n}\n'),
            ("crlf_content", 'locals {\r\n  x = "hello"\r\n}\r\n'),
        ],
    )
    def test_as_ts_r8_001_reproduction_cases_pass_parser_and_child_lint(
        self, tmp_path: Path, case_name: str, content: str
    ) -> None:
        from tools.tf_module_layout import _parse_hcl_content

        violation, has_prov, has_back = _parse_hcl_content(content)
        assert not violation, f"Expected syntax_violation == False for {case_name}"
        assert not has_prov, f"Expected has_provider == False for {case_name}"
        assert not has_back, f"Expected has_backend == False for {case_name}"

        sub_mod = tmp_path / case_name
        sub_mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (sub_mod / name).write_text("# core\n", encoding="utf-8")
        (sub_mod / "main.tf").write_text(content, encoding="utf-8")

        errors = lint_module(sub_mod, is_root=False)
        assert errors == [], f"Expected errors == [] for {case_name}, got: {errors}"

    def test_odd_identifiers_and_unparseable_content_fail_closed(self) -> None:
        from tools.tf_module_layout import _parse_hcl_content

        for snippet in (
            'locals {\n  x = "${join(\\"\\", [<<\u2118OF\n{\n\u2118OF\n])}"\n}\n',
            "locals {\n  x = <<EOF\u00a0\nhello\nEOF\u00a0\n}\n",
            "locals {\n  x = <<EOF\n}\n",
            "locals {\n  bad_syntax =\n",
        ):
            violation, _, _ = _parse_hcl_content(snippet)
            assert violation, f"Expected violation == True for unparseable snippet: {snippet!r}"


class TestLintModuleProhibitedAndUnexpected:
    """Prohibited directories, files, state, and unexpected filenames must be rejected."""

    def test_root_only_files_rejected_in_child(self, tmp_path: Path) -> None:
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "providers.tf").write_text("# providers\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("providers.tf" in e and "root module only" in e for e in errors), errors

    def test_invalid_tf_filename_rejected(self, tmp_path: Path) -> None:
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "Invalid-Name.tf").write_text("# invalid\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unexpected file" in e and "Invalid-Name.tf" in e for e in errors), errors

    def test_tfvars_rejected_in_child_module(self, tmp_path: Path) -> None:
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "terraform.tfvars").write_text("x = 1\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("prohibited file 'terraform.tfvars'" in e for e in errors), errors

    def test_tfvars_allowed_in_root_module(self, tmp_path: Path) -> None:
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "apps.auto.tfvars").write_text('app_id = "reviewed-app"\n', encoding="utf-8")
        (tmp_path / "terraform.tfvars").write_text('env = "prod"\n', encoding="utf-8")
        errors = lint_module(tmp_path, is_root=True)
        assert errors == [], f"Expected 0 errors for tfvars in root module, got: {errors}"

    def test_state_files_rejected(self, tmp_path: Path) -> None:
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "terraform.tfstate").write_text("{}\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("prohibited state file 'terraform.tfstate'" in e for e in errors), errors

    def test_prohibited_envs_directory_rejected(self, tmp_path: Path) -> None:
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "envs").mkdir()
        errors = lint_module(tmp_path, is_root=False)
        assert any("prohibited environment directory 'envs'" in e for e in errors), errors


class TestLintModuleMissingFile:
    """Missing core files must be reported."""

    def test_missing_versions_fails(self) -> None:
        errors = lint_module(FIXTURES / "tf-layout-bad-missing")
        assert any("missing required file" in e and "versions.tf" in e for e in errors), errors

    def test_missing_readme_fails(self, tmp_path: Path) -> None:
        for name in ("main.tf", "variables.tf", "outputs.tf", "versions.tf"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        errors = lint_module(tmp_path)
        assert any("missing required file 'README.md'" in e for e in errors), errors


class TestLintTree:
    """lint_tree should walk subdirectories and aggregate errors."""

    def test_good_tree_passes(self) -> None:
        errors = lint_tree(
            FIXTURES / "tf-layout-good",
            root_module=FIXTURES / "tf-layout-good",
        )
        assert errors == []

    def test_bad_tree_collects_errors(self, tmp_path: Path) -> None:
        sub = tmp_path / "submodule"
        sub.mkdir()
        (sub / "main.tf").write_text('provider "google" {}\n', encoding="utf-8")
        errors = lint_tree(tmp_path)
        assert len(errors) >= 1
        assert any("missing required file" in e for e in errors)

    def test_bad_tree_fixture_collects_unexpected_file_error(self) -> None:
        errors = lint_tree(FIXTURES / "tf-layout-bad-unexpected")
        assert len(errors) >= 1
        assert any("unexpected file" in e and "foo.txt" in e for e in errors)


class TestTfModuleLayoutEdgeBranches:
    def test_lint_tree_skips_hidden_dirs(self, tmp_path: Path) -> None:
        hidden = tmp_path / ".terraform"
        hidden.mkdir()
        (hidden / "main.tf").write_text("# ignore\n", encoding="utf-8")
        assert lint_tree(tmp_path) == []

    def test_main_cli_entrypoint(self, tmp_path: Path) -> None:
        from tools.tf_module_layout import main

        assert main(["--self-test"]) == 0
        assert main(["nonexistent-path-12345"]) == 1

        bad_dir = tmp_path / "bad_mod"
        bad_dir.mkdir()
        (bad_dir / "main.tf").write_text("# main\n", encoding="utf-8")
        (bad_dir / "foo.txt").write_text("bad\n", encoding="utf-8")
        assert main([str(bad_dir)]) == 1

        empty_dir = tmp_path / "empty_dir"
        empty_dir.mkdir()
        assert main([str(empty_dir)]) == 0


class TestSol61HclCases:
    """Regression tests for Sol's cases in AS-TS-R15-SOL61-02."""

    def test_escaped_quote_interpolation_in_child_module(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod1"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = "${replace("hello", "\\"", "")}" \n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert errors == [], f"Expected 0 errors for escaped quote interpolation, got: {errors}"

    def test_escaped_interpolation_in_child_module(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod2"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = "$${replace(\\"hello\\", \\"h\\", \\"j\\")}"\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert errors == [], f"Expected 0 errors for escaped interpolation, got: {errors}"


class TestSol61R18HclCases:
    """Regression tests for Sol's cases in AS-TS-R18-SOL61-01."""

    def test_empty_repeated_heredoc_detects_child_provider(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_empty_heredoc"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = <<EOF\nEOF\n}\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            f"Expected provider error, got: {errors}"
        )

    def test_empty_repeated_heredoc_detects_child_backend(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_empty_heredoc_backend"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = (
            "locals {\n  x = <<-EOF\nEOF\n}\n"
            'terraform {\n  backend "s3" {\n    bucket = "b"\n  }\n}\n'
            "locals {\n  y = <<-EOF\nEOF\n}\n"
        )
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors), f"Expected backend error, got: {errors}"

    def test_unicode_delimiter_collision_detects_provider(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_unicode_collision"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  a = <<é\nhello\né\n  b = <<u00e9\nworld\nu00e9\n}\nprovider "aws" {}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            f"Expected provider error, got: {errors}"
        )

    def test_unclosed_heredoc_fails_closed(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_unclosed"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  a = <<EOF\nprovider "aws" {}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors), f"Expected unparseable HCL error, got: {errors}"


class TestArgonR19HclCases:
    """Regression tests for Argon's cases in ARGON-TS-R19-001."""

    def test_trailing_non_ascii_whitespace_heredoc_detects_provider(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_argon_heredoc_prov"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = (
            'locals {\n  x = <<EOF\nhello\nEOF\u00a0\n}\nprovider "aws" {}\nlocals {\n  y = <<EOF\nworld\nEOF\n}\n'
        )
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            f"Expected provider error, got: {errors}"
        )

    def test_trailing_non_ascii_whitespace_heredoc_detects_backend(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_argon_heredoc_back"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = (
            "locals {\n"
            "  x = <<EOF\n"
            "hello\n"
            "EOF\u00a0\n"
            "}\n"
            'terraform {\n  backend "s3" {\n    bucket = "b"\n  }\n}\n'
            "locals {\n"
            "  y = <<EOF\n"
            "world\n"
            "EOF\n"
            "}\n"
        )
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors), f"Expected backend error, got: {errors}"

    def test_template_directive_quoted_strings_detects_provider(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_argon_template_prov"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = (
            'locals {\n  x = "%{if var.a == "/*"}a%{endif}"\n}\n'
            'provider "aws" {}\n'
            'locals {\n  y = "%{if var.a == "*/z = "}b%{endif}"\n}\n'
        )
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), (
            f"Expected provider error, got: {errors}"
        )

    def test_template_directive_quoted_strings_detects_backend(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_argon_template_back"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = (
            'locals {\n  x = "%{if var.a == "/*"}a%{endif}"\n}\n'
            'terraform {\n  backend "s3" {\n    bucket = "b"\n  }\n}\n'
            'locals {\n  y = "%{if var.a == "*/z = "}b%{endif}"\n}\n'
        )
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors), f"Expected backend error, got: {errors}"


class TestR20ReviewerProbes:
    """Regression tests for R20 findings: AS-TS-R20-01 (Opus), AS-TS-R20-SOL61-01 (Sol), ARGON-TS-R20-001."""

    def test_opus_escaped_percent_detects_provider(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_opus_prov"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = "%%{ "\n}\nprovider "aws" {}\nlocals {\n  y = "a" # " }"\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration" in e for e in errors), f"Expected provider error, got: {errors}"

    def test_opus_escaped_percent_detects_backend(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_opus_back"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = "%%{ "\n}\nterraform {\n  backend "s3" {}\n}\nlocals {\n  y = "a" # " }"\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors), f"Expected backend error, got: {errors}"

    def test_sol_multiline_directive_detects_provider(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_sol_multi_prov"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = (
            'locals {\n  x = "%{if\n  var.a == "/*"}a%{endif}"\n}\n'
            'provider "aws" {}\n'
            'locals {\n  y = "%{if\n  var.a == "*/z = "}b%{endif}"\n}\n'
        )
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), (
            f"Expected provider error, got: {errors}"
        )

    def test_sol_multiline_directive_detects_backend(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_sol_multi_back"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = (
            'locals {\n  x = "%{if\n  var.a == "/*"}a%{endif}"\n}\n'
            'terraform {\n  backend "s3" {}\n}\n'
            'locals {\n  y = "%{if\n  var.a == "*/z = "}b%{endif}"\n}\n'
        )
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors), f"Expected backend error, got: {errors}"

    def test_sol_empty_heredoc_detects_provider(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_sol_empty_prov"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = <<EOF\nEOF\n  y = <<EOF\nEOF\n}\nprovider "aws" {}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration" in e for e in errors), f"Expected provider error, got: {errors}"

    def test_sol_empty_heredoc_detects_backend(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_sol_empty_back"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = <<EOF\nEOF\n  y = <<EOF\nEOF\n}\nterraform {\n  backend "s3" {}\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors), f"Expected backend error, got: {errors}"

    def test_sol_heredoc_harmless_passes(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_sol_harmless"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  script = <<EOT\nif (ok) {\n}\nprovider "aws" {}\nEOT\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert errors == [], f"Harmless provider inside heredoc must not error, got: {errors}"

    def test_commented_empty_heredoc_passes(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_commented_empty"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = "/*\nlocals {\n  x = <<EOF\nEOF\n}\n*/\nlocals {\n  ok = true\n}\n"
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert errors == [], f"Commented empty heredoc must pass, got: {errors}"

    def test_intervening_comment_detects_provider(self, tmp_path: Path) -> None:
        """ARGON-TS-R21-002 / SOL61-05: Intervening comment after heredoc does not hide provider."""
        mod = tmp_path / "mod_intervening"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = <<EOF\nEOF\n}\n# comment\nprovider "aws" {}\nlocals {\n  y = <<EOF\nEOF\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_nospace_provider_header_detects_provider(self, tmp_path: Path) -> None:
        """ARGON-TS-R21-002 / SOL61-05: provider"aws"{} without space detects provider."""
        mod = tmp_path / "mod_nospace"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  x = <<EOF\nEOF\n}\nprovider"aws"{}\nlocals {\n  y = <<EOF\nEOF\n}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_provider_attribute_collision_fails_closed(self, tmp_path: Path) -> None:
        """SOL61-02: provider "aws" {} followed by provider = {} must detect provider."""
        mod = tmp_path / "mod_prov_coll"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'provider "aws" {}\nprovider = {}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("provider configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_terraform_attribute_collision_fails_closed(self, tmp_path: Path) -> None:
        """SOL61-02: terraform { backend "s3" {} } followed by terraform = {} must detect backend."""
        mod = tmp_path / "mod_tf_coll"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'terraform {\n  backend "s3" {}\n}\nterraform = {}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_sol61_01_empty_list_attribute_preserves_backend(self, tmp_path: Path) -> None:
        """AS-TS-R23-SOL61-01: terraform = [] must not erase backend block."""
        mod = tmp_path / "mod_tf_empty_list"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'terraform {\n  backend "s3" {}\n}\nterraform = []\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_sol61_01_obj_list_attribute_preserves_backend(self, tmp_path: Path) -> None:
        """AS-TS-R23-SOL61-01: terraform = [{...}] must not erase backend block."""
        mod = tmp_path / "mod_tf_obj_list"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'terraform {\n  backend "s3" {}\n}\nterraform = [{required_version = ">= 1.0"}]\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_argon23_001_empty_dict_list_attribute_preserves_backend(self, tmp_path: Path) -> None:
        """ARGON-TS-R23-001: terraform = [{}] must not erase backend block."""
        mod = tmp_path / "mod_argon_empty_dict_list"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'terraform {\n  backend "s3" {\n    bucket = "b"\n  }\n}\nterraform = [{}]\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_argon23_001_int_list_attribute_preserves_backend(self, tmp_path: Path) -> None:
        """ARGON-TS-R23-001: terraform = [1] must not erase backend block."""
        mod = tmp_path / "mod_argon_int_list"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'terraform {\n  backend "s3" {\n    bucket = "b"\n  }\n}\nterraform = [1]\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_argon23_001_cloud_block_with_empty_list_preserves_backend(self, tmp_path: Path) -> None:
        """ARGON-TS-R23-001: terraform = [] must not erase cloud block."""
        mod = tmp_path / "mod_argon_cloud_empty_list"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'terraform {\n  cloud {\n    organization = "org"\n  }\n}\nterraform = []\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("backend configuration" in e for e in errors) and not any("unparseable HCL" in e for e in errors), (
            errors
        )

    def test_sol61_02_file_level_bare_cr_refused(self, tmp_path: Path) -> None:
        """AS-TS-R23-SOL61-02: File reading preserves raw bytes without universal newline conversion."""
        mod = tmp_path / "mod_bare_cr_file"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_bytes(b"# core\n")
        (mod / "bare_cr.tf").write_bytes(b"locals {\r x = 1\r}\r")
        errors = lint_module(mod, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors), errors

    def test_sol61_02_file_level_double_bom_refused(self, tmp_path: Path) -> None:
        """AS-TS-R23-SOL61-02: File reading preserves double BOM without stripping."""
        mod = tmp_path / "mod_double_bom_file"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_bytes(b"# core\n")
        (mod / "double_bom.tf").write_bytes(b'\xef\xbb\xbf\xef\xbb\xbfterraform {\n  backend "s3" {}\n}\n')
        errors = lint_module(mod, is_root=False)
        assert any("unparseable HCL (refused)" in e for e in errors), errors

    @pytest.mark.parametrize(
        "env_var",
        ["HCL_PARSER_PATH", "HCL2JSON_PATH", "HCLBLOCKS_PATH"],
    )
    def test_env_overrides_ignored_no_hijacking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_var: str
    ) -> None:
        """TS24-01 / AS-TS-R24-SOL61-01: Environment variables cannot select or hijack the helper executable."""
        fake = tmp_path / "fake_parser"
        fake.write_text('#!/bin/sh\necho \'{"valid":true,"blocks":[]}\'\n')
        fake.chmod(0o755)
        sidecar = fake.with_suffix(".sha256")
        sidecar.write_text("somehash  fake_parser\n")
        monkeypatch.setenv(env_var, str(fake))

        v, p, b = scan_tf_file('provider "aws" {}\n')
        assert not v and p and not b, f"Provider must be detected; {env_var} must be completely ignored"

    def test_missing_go_toolchain_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TS24 decision: If Go toolchain is missing, fail closed as syntax violation."""
        import shutil

        import tools.tf_module_layout as tml

        monkeypatch.setattr(tml, "_COMPILED_HELPER_PATH", None)
        monkeypatch.setattr(shutil, "which", lambda cmd: None)

        v, p, b = scan_tf_file('provider "aws" {}\n')
        assert v and not p and not b, "Missing Go toolchain must fail closed as syntax violation"

    def test_hclblocks_build_failure_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TS24 decision: If building helper fails, fail closed as syntax violation."""
        import tools.tf_module_layout as tml

        def _fail_build(hclblocks_dir: Path, dest: Path) -> None:
            raise RuntimeError("Build error simulated")

        monkeypatch.setattr(tml, "_COMPILED_HELPER_PATH", None)
        monkeypatch.setattr(tml, "_build_helper_process", _fail_build)

        v, p, b = scan_tf_file('provider "aws" {}\n')
        assert v and not p and not b, "Helper build failure must fail closed as syntax violation"

    def test_sol61_04_installer_failure_exits_nonzero(self) -> None:
        """AS-TS-R23-SOL61-04: Installer returns 1 on unwritable or unsafe target."""
        from scripts.install_hcl_parser import install_parser

        with pytest.raises(Exception):
            install_parser(Path("/tmp/hclblocks_cannot_install_here"))

    def test_installer_builds_and_verifies_cleanly(self) -> None:
        """TS24-02 / AS-TS-R24-SOL61-02: Installer provisions into a private 0700 directory and verifies."""
        import shutil

        from scripts.install_hcl_parser import install_parser

        dest_dir = Path.home() / ".cache" / f"test_hclblocks_{os.getpid()}"
        dest = dest_dir / "hclblocks"
        try:
            rc = install_parser(dest)
            assert rc == 0
            assert dest.is_file()
            st = dest_dir.stat()
            assert (st.st_mode & 0o077) == 0, f"Parent directory must be private 0700, got {oct(st.st_mode)}"
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_installer_fails_closed_when_go_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Installer fails closed when go compiler is missing."""
        import shutil

        from scripts.install_hcl_parser import install_parser

        dest = Path.home() / ".cache" / f"test_hclblocks_missing_go_{os.getpid()}" / "hclblocks"
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        with pytest.raises(RuntimeError, match="Go compiler"):
            install_parser(dest)

    def test_bare_cr_and_double_bom_fail_closed(self) -> None:
        """SOL61-03: Bare carriage returns and double UTF-8 BOM must fail closed as syntax violations."""
        v_cr, _, _ = scan_tf_file("locals {\r x = 1\r}\r")
        assert v_cr, "Bare CR without LF must fail closed"

        v_bom, _, _ = scan_tf_file("\ufeff\ufefflocals { x = 1 }\n")
        assert v_bom, "Double UTF-8 BOM must fail closed"
