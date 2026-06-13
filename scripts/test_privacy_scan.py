#!/usr/bin/env python3
from __future__ import annotations

import unittest

from privacy_scan import findings_for_text


class PrivacyScanTests(unittest.TestCase):
    def test_blocks_private_names_case_insensitively(self) -> None:
        text = "This mentions " + "Match" + "point" + " in a public skill."
        findings = findings_for_text(text, "skills/example/SKILL.md")
        self.assertTrue(findings)

    def test_blocks_internal_process_terms(self) -> None:
        text = "Use " + "codex" + "-q" + " to fetch a directive."
        findings = findings_for_text(text, "README.md")
        self.assertTrue(findings)

    def test_blocks_internal_counterpart_leaks(self) -> None:
        examples = [
            "The " + "internal" + "-agent-skills repo has the rest.",
            "This is the " + "public" + " subset of our skills.",
            "Copy " + "private" + " skills from a separate source.",
            "The " + "internal" + " counterpart owns this workflow.",
        ]
        for text in examples:
            with self.subTest(text=text):
                findings = findings_for_text(text, "README.md")
                self.assertTrue(findings)

    def test_blocks_internal_hosts(self) -> None:
        text = "Contact dev" + "@" + "localhost or api" + "." + "selamy" + "." + "dev."
        findings = findings_for_text(text, "README.md")
        self.assertGreaterEqual(len(findings), 2)

    def test_allows_generic_skill_language(self) -> None:
        text = "Use reproducible validation, observable traces, and source-backed examples."
        findings = findings_for_text(text, "skills/generic/SKILL.md")
        self.assertEqual([], findings)

    def test_allows_speedforge_only_as_workflow_runner_label(self) -> None:
        label = "speed" + "for" + "ge"
        text = f"jobs:\n  test:\n    runs-on: {label}\n"
        findings = findings_for_text(text, ".github/workflows/quality.yaml")
        self.assertEqual([], findings)

    def test_blocks_speedforge_in_public_workflow_comments(self) -> None:
        label = "speed" + "for" + "ge"
        text = f"jobs:\n  test:\n    runs-on: {label}\n    # {label} product note\n"
        findings = findings_for_text(text, ".github/workflows/quality.yaml")
        self.assertTrue(findings)


if __name__ == "__main__":
    unittest.main()
