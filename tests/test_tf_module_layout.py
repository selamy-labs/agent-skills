"""Tests for tools.tf_module_layout — the Terraform module layout lint."""

from __future__ import annotations

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
        prov, back = scan_tf_file(hcl)
        assert not prov and not back


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
        (tmp_path / "main.tf").write_text('terraform { backend "gcs" { bucket = "b" } }\n', encoding="utf-8")
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
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unicode_first_char_heredoc_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = <<-ÉOF\n{\nÉOF\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unicode_tail_heredoc_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = <<-EOFé\n{\nEOFé\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_nested_heredoc_in_interpolation_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = "${<<EOF\n{\nEOF\n}"\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_nested_heredoc_with_quote_in_interpolation_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = "${<<EOF\n"\nEOF\n}"\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unicode_combining_mark_heredoc_delimiter_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<E\u0301OF\nhello\nE\u0301OF\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unicode_connector_punctuation_heredoc_delimiter_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<E\u203fOF\nhello\nE\u203fOF\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_devanagari_combining_mark_heredoc_delimiter_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<\u0905\u0902\u0924\nhello\n\u0905\u0902\u0924\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_scan_tf_file_detects_providers_with_unicode_continuations(self) -> None:
        case_combining = 'locals {\n  note = <<E\u0301OF\nhello\nE\u0301OF\n}\nprovider "google" {}\n'
        assert scan_tf_file(case_combining) == (True, False)

        case_connector = 'locals {\n  note = <<E\u203fOF\nhello\nE\u203fOF\n}\nprovider "google" {}\n'
        assert scan_tf_file(case_connector) == (True, False)

        case_devanagari = (
            'locals {\n  note = <<\u0905\u0902\u0924\nhello\n\u0905\u0902\u0924\n}\nprovider "google" {}\n'
        )
        assert scan_tf_file(case_devanagari) == (True, False)

    def test_nested_unicode_heredoc_in_interpolation_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  x = "${<<E\u0301OF\nhello\nE\u0301OF\n}"\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unclosed_heredoc_fails_closed_and_does_not_hide_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            'locals {\n  note = <<EOF\nhello\n}\nprovider "google" {}\n',
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors, "Expected errors for unclosed heredoc with provider"
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unclosed_heredoc_without_provider_fails_closed(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            "locals {\n  note = <<EOF\nhello\n}\n",
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors, "Expected errors for unclosed heredoc"
        assert any("unclosed or malformed heredoc" in e for e in errors), errors

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
        assert errors == []

    def test_unicode_heredoc_without_provider_passes(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text(
            "locals {\n  x = <<-ПОСТАВЩИК\nhello\nПОСТАВЩИК\n}\n",
            encoding="utf-8",
        )
        errors = lint_module(tmp_path, is_root=False)
        assert errors == [], f"Expected 0 errors for unicode heredoc without provider, got: {errors}"

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
        assert any("unclosed or malformed heredoc" in e for e in errors), errors

    def test_heredoc_opener_with_trailing_spaces_rejected(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text("locals {\n  x = <<EOF   \nhello\nEOF\n}\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unclosed or malformed heredoc" in e for e in errors), errors

    def test_multiline_interp_with_eof_line_does_not_prematurely_close_heredoc(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = (
            'locals {\n  obj = { EOF = "val" }\n  x = <<EOF\n${local.obj.\nEOF\n}\n{{\nEOF\n}\nprovider "google" {}\n'
        )
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_nested_heredoc_with_outer_delim_in_body_does_not_prematurely_close(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = 'locals {\n  x = <<OUTER\n${<<INNER\nOUTER\nINNER\n}\n{{\nOUTER\n}\nprovider "google" {}\n'
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_block_comment_in_interp_with_eof_does_not_prematurely_close(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = 'locals {\n  x = <<EOF\n${/*\nEOF\n*/ "ok"}\n{{\nEOF\n}\nprovider "google" {}\n'
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_unclosed_nested_heredoc_in_quoted_string_fails(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text('locals {\n  x = "${<<EOF\nhello\n}"\n}\n', encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unclosed or malformed heredoc" in e for e in errors), errors

    def test_unicode_other_id_in_nested_heredoc_detects_provider(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        content = 'locals { x = "${join(\\"\\", [<<\u2118OF\n{\n\u2118OF\n])}" }\nprovider "google" {}\n'
        (tmp_path / "main.tf").write_text(content, encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("provider configuration in non-root module" in e for e in errors), errors

    def test_self_test_cli(self) -> None:
        from tools.tf_module_layout import main as tf_main

        rc = tf_main(["--self-test"])
        assert rc == 0


class TestHcl2StructuredParser:
    """Validate python-hcl2 structured parsing integration."""

    def test_normalize_unicode_identifiers(self) -> None:
        from tools.tf_module_layout import _preprocess_hcl2_content

        sample = (
            '\u092a\u094d\u0930\u0926\u093e\u0924\u093e "google" {\n  bucket = "hello \u092c"\n  # comment \u092c\n}\n'
        )
        normalized = _preprocess_hcl2_content(sample)
        assert "\u092a\u094d\u0930\u0926\u093e\u0924\u093e" in normalized
        assert "# comment" in normalized

    def test_parse_hcl2_content_detects_blocks(self) -> None:
        from tools.tf_module_layout import _parse_hcl2_content

        hcl = 'provider "google" {}\nterraform { backend "gcs" { bucket = "b" } }\n'
        violation, prov, back = _parse_hcl2_content(hcl)
        assert not violation
        assert prov
        assert back

    def test_parse_hcl2_content_syntax_error(self) -> None:
        from tools.tf_module_layout import _parse_hcl2_content

        violation, _, _ = _parse_hcl2_content("locals { x =")
        assert violation

    def test_syntax_error_in_child_module_flagged(self, tmp_path: Path) -> None:
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (tmp_path / name).write_text("# core\n", encoding="utf-8")
        (tmp_path / "main.tf").write_text("locals {\n  bad_syntax =\n", encoding="utf-8")
        errors = lint_module(tmp_path, is_root=False)
        assert any("unclosed or malformed heredoc in child module" in e for e in errors), errors

    @pytest.mark.parametrize(
        "case_name,content",
        [
            ("directive_quotes", 'locals {\n  x = "%{if var.env == \\"prod\\"}yes%{endif}"\n}\n'),
            (
                "unicode_in_string_interp",
                (
                    'locals {\n  \u092c\u093e\u0932\u094d\u091f\u0940 = "b"\n'
                    '  x = "${local.\u092c\u093e\u0932\u094d\u091f\u0940}"\n}\n'
                ),
            ),
            ("nested_combining_heredoc_interp", 'locals {\n  x = "${<<E\u0301OF\nhello\nE\u0301OF\n}"\n}\n'),
            (
                "nested_other_id_heredoc_interp",
                'locals {\n  x = "${join(\\"\\", [<<\u2118OF\n{\n\u2118OF\n])}"\n}\n',
            ),
            ("heredoc_trailing_whitespace", "locals {\n  x = <<EOF\nhello\nEOF   \n}\n"),
            ("crlf_content", 'locals {\r\n  x = "hello"\r\n}\r\n'),
        ],
    )
    def test_as_ts_r8_001_reproduction_cases_pass_hcl2_and_child_lint(
        self, tmp_path: Path, case_name: str, content: str
    ) -> None:
        from tools.tf_module_layout import _parse_hcl2_content

        violation, has_prov, has_back = _parse_hcl2_content(content)
        assert not violation, f"Expected hcl2_violation == False for {case_name}"
        assert not has_prov, f"Expected has_provider == False for {case_name}"
        assert not has_back, f"Expected has_backend == False for {case_name}"

        sub_mod = tmp_path / case_name
        sub_mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (sub_mod / name).write_text("# core\n", encoding="utf-8")
        (sub_mod / "main.tf").write_text(content, encoding="utf-8")

        errors = lint_module(sub_mod, is_root=False)
        assert errors == [], f"Expected errors == [] for {case_name}, got: {errors}"


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
        assert any("provider configuration" in e for e in errors), f"Expected provider error, got: {errors}"

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
        assert any("provider configuration" in e for e in errors), f"Expected provider error, got: {errors}"

    def test_unclosed_heredoc_fails_closed(self, tmp_path: Path) -> None:
        mod = tmp_path / "mod_unclosed"
        mod.mkdir()
        for name in ("variables.tf", "outputs.tf", "versions.tf", "README.md"):
            (mod / name).write_text("# core\n", encoding="utf-8")
        snippet = 'locals {\n  a = <<EOF\nprovider "aws" {}\n'
        (mod / "main.tf").write_text(snippet, encoding="utf-8")
        errors = lint_module(mod, is_root=False)
        assert any("unclosed or malformed heredoc" in e for e in errors), (
            f"Expected malformed heredoc error, got: {errors}"
        )
        assert any("provider configuration" in e for e in errors), f"Expected provider error, got: {errors}"
