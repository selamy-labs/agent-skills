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

    def test_blocks_internal_hosts(self) -> None:
        text = "Contact dev" + "@" + "localhost or api" + "." + "selamy" + "." + "dev."
        findings = findings_for_text(text, "README.md")
        self.assertGreaterEqual(len(findings), 2)

    def test_allows_generic_skill_language(self) -> None:
        text = "Use reproducible validation, observable traces, and source-backed examples."
        findings = findings_for_text(text, "skills/generic/SKILL.md")
        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
