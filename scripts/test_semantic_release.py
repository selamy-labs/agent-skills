#!/usr/bin/env python3
from __future__ import annotations

import unittest

import public_api_stability
import semantic_release as release
import validate_pr_title


class SemanticReleasePolicyTest(unittest.TestCase):
    def test_fix_plans_patch(self) -> None:
        commits = [release.Commit("abc1234", "fix: preserve scanner output", "")]
        self.assertEqual(release.bump_for(commits, has_existing_tag=True), "patch")
        self.assertEqual(release.next_version((0, 1, 2), "patch"), (0, 1, 3))

    def test_feat_plans_minor(self) -> None:
        commits = [
            release.Commit("abc1234", "fix: preserve scanner output", ""),
            release.Commit("def5678", "feat: add validation report", ""),
        ]
        self.assertEqual(release.bump_for(commits, has_existing_tag=True), "minor")
        self.assertEqual(release.next_version((0, 1, 2), "minor"), (0, 2, 0))

    def test_breaking_change_plans_major(self) -> None:
        commits = [release.Commit("abc1234", "feat!: replace manifest format", "")]
        self.assertEqual(release.bump_for(commits, has_existing_tag=True), "major")
        self.assertEqual(release.next_version((0, 1, 2), "major"), (1, 0, 0))

    def test_uppercase_type_does_not_release(self) -> None:
        commits = [release.Commit("abc1234", "Feat: add validation report", "")]
        self.assertIsNone(release.bump_for(commits, has_existing_tag=True))
        self.assertIsNone(public_api_stability.HEADER_RE.match("Feat!: remove skill"))

    def test_lowercase_breaking_change_footer_does_not_release(self) -> None:
        commits = [release.Commit("abc1234", "feat: replace manifest format", "breaking change: incompatible")]
        self.assertEqual(release.bump_for(commits, has_existing_tag=True), "minor")

    def test_non_conventional_title_does_not_release(self) -> None:
        commits = [release.Commit("abc1234", "Add validation report", "")]
        self.assertIsNone(release.bump_for(commits, has_existing_tag=True))

    def test_pr_title_lint_rejects_bad_release_inputs(self) -> None:
        self.assertEqual(validate_pr_title.validate("fix: correct release plan"), [])
        self.assertTrue(validate_pr_title.validate("Fix: correct release plan"))
        self.assertTrue(validate_pr_title.validate("correct release plan"))
        self.assertTrue(validate_pr_title.validate("feat: replace manifest", "breaking change: incompatible"))


if __name__ == "__main__":
    unittest.main()
