"""Unit tests for tools.privacy — privacy scanner denylist and patterns."""

from __future__ import annotations

from pathlib import Path

from tools.privacy import BLOCKED_TERMS, SECRET_PATTERNS, SILENCE_PHRASES, findings_for_text, iter_text_files, scan_all
from tools.privacy.loader import load_denylist


class TestOrgProductTerms:
    def test_blocks_matchpoint(self) -> None:
        text = "This mentions " + "Match" + "point" + " in a skill."
        assert findings_for_text(text, "skills/example/SKILL.md")

    def test_blocks_tradestream(self) -> None:
        text = "Powered by " + "Trade" + "stream" + " engine."
        assert findings_for_text(text, "skills/example/SKILL.md")

    def test_blocks_speedforge(self) -> None:
        text = "Run on " + "Speed" + "forge" + " CI."
        assert findings_for_text(text, "skills/example/SKILL.md")


class TestPersonVendorTerms:
    def test_blocks_ylopo(self) -> None:
        text = "Integrates with " + "Ylo" + "po" + " platform."
        assert findings_for_text(text, "README.md")

    def test_blocks_gohighlevel(self) -> None:
        text = "Use " + "GoHigh" + "Level" + " API."
        assert findings_for_text(text, "README.md")


class TestInternalProcessTerms:
    def test_blocks_codex_q(self) -> None:
        text = "Use " + "codex" + "-q" + " to fetch a directive."
        assert findings_for_text(text, "README.md")

    def test_blocks_ralph(self) -> None:
        text = "The " + "ral" + "ph" + " loop runs autonomously."
        assert findings_for_text(text, "README.md")

    def test_blocks_tokenomics(self) -> None:
        text = "Check " + "token" + "omics" + " state."
        assert findings_for_text(text, "README.md")


class TestInternalCounterpartLeaks:
    def test_blocks_internal_agent_skills(self) -> None:
        text = "The " + "internal" + "-agent-skills repo has the rest."
        assert findings_for_text(text, "README.md")

    def test_blocks_public_subset(self) -> None:
        text = "This is the " + "public" + " subset of our skills."
        assert findings_for_text(text, "README.md")

    def test_blocks_private_skills(self) -> None:
        text = "Copy " + "private" + " skills from a separate source."
        assert findings_for_text(text, "README.md")

    def test_blocks_internal_counterpart(self) -> None:
        text = "The " + "internal" + " counterpart owns this workflow."
        assert findings_for_text(text, "README.md")


class TestCredentialPatterns:
    def test_blocks_github_classic_token(self) -> None:
        text = "ghp_" + "A" * 30
        assert findings_for_text(text, "README.md")

    def test_blocks_github_fine_grained_token(self) -> None:
        text = "github_pat_" + "B" * 30
        assert findings_for_text(text, "README.md")

    def test_blocks_api_key(self) -> None:
        text = "sk-" + "C" * 30
        assert findings_for_text(text, "README.md")

    def test_blocks_private_key_pem(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----"
        assert findings_for_text(text, "README.md")

    def test_blocks_internal_service_host(self) -> None:
        text = "api" + "." + "selamy" + "." + "dev"
        assert findings_for_text(text, "README.md")

    def test_blocks_localhost_identity(self) -> None:
        text = "dev" + "@" + "localhost"
        assert findings_for_text(text, "README.md")


class TestSilencePhrases:
    def test_blocks_do_not_mention(self) -> None:
        # Construct dynamically to avoid self-scan
        text = "do not" + " mention this to anyone"
        assert any("silence phrase" in f for f in findings_for_text(text, "README.md"))

    def test_blocks_never_reveal(self) -> None:
        text = "never" + " reveal the source"
        assert any("silence phrase" in f for f in findings_for_text(text, "README.md"))

    def test_blocks_keep_quiet(self) -> None:
        text = "keep" + " quiet about internals"
        assert any("silence phrase" in f for f in findings_for_text(text, "README.md"))


class TestNegativeCases:
    def test_allows_generic_skill_language(self) -> None:
        text = "Use reproducible validation, observable traces, and source-backed examples."
        assert findings_for_text(text, "skills/generic/SKILL.md") == []

    def test_allows_common_programming_terms(self) -> None:
        text = "Parse the JSON response and validate the schema fields."
        assert findings_for_text(text, "skills/example/SKILL.md") == []

    def test_scanner_file_self_excluded(self) -> None:
        text = "matchpoint tradestream ralph tokenomics"
        assert findings_for_text(text, "scripts/privacy_scan.py") == []
        assert findings_for_text(text, "tools/privacy.py") == []
        assert findings_for_text(text, "tools/privacy/__init__.py") == []

    def test_denylist_file_self_excluded(self) -> None:
        text = "matchpoint tradestream ralph tokenomics"
        assert findings_for_text(text, "tools/privacy/denylist.yml") == []


class TestIterTextFiles:
    def test_finds_md_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("hello")
        (tmp_path / "b.py").write_text("pass")
        (tmp_path / "c.png").write_bytes(b"\x89PNG")
        files = iter_text_files(tmp_path)
        suffixes = {f.suffix for f in files}
        assert ".md" in suffixes
        assert ".py" in suffixes
        assert ".png" not in suffixes

    def test_skips_git_dir(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("gitconfig")
        (tmp_path / "real.md").write_text("content")
        files = iter_text_files(tmp_path)
        assert all(".git" not in f.parts for f in files)


class TestDenylistIntegrity:
    """Verify the denylist.yml is loaded and drives all scanner lists."""

    def test_denylist_loads_all_categories(self) -> None:
        denylist_path = Path(__file__).resolve().parents[1] / "tools" / "privacy" / "denylist.yml"
        data = load_denylist(denylist_path)
        assert "org_product" in data
        assert "business_person" in data
        assert "internal_process" in data
        assert "counterpart_leak" in data
        assert "silence_phrase" in data
        assert "secret_patterns" in data

    def test_blocked_terms_match_denylist(self) -> None:
        assert len(BLOCKED_TERMS) == 4
        assert "org/product" in BLOCKED_TERMS
        assert "matchpoint" in BLOCKED_TERMS["org/product"]

    def test_secret_patterns_loaded(self) -> None:
        labels = {label for label, _ in SECRET_PATTERNS}
        assert "GitHub classic token" in labels
        assert "internal service host" in labels

    def test_silence_phrases_loaded(self) -> None:
        assert "do not mention" in SILENCE_PHRASES
        assert len(SILENCE_PHRASES) >= 8


class TestScanAll:
    def test_clean_dir_passes(self, tmp_path: Path) -> None:
        (tmp_path / "clean.md").write_text("Normal content here.")
        assert scan_all(tmp_path) == []

    def test_dirty_dir_catches_violations(self, tmp_path: Path) -> None:
        (tmp_path / "leak.md").write_text("matchpoint integration guide")
        findings = scan_all(tmp_path)
        assert findings
