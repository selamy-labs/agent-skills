#!/usr/bin/env python3
"""Legacy test runner kept for CI backward compat. Real tests live in tests/."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import public_api_stability, validate_pr_title
from scripts import semantic_release as release


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

    def test_evaluate_check_runs_fails_closed_on_missing_or_failed(self) -> None:
        target_sha = "abc1234"
        # 1. Missing check fails closed
        data_missing = [{"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha}]
        passed, reasons = release.evaluate_check_runs(data_missing, expected_sha=target_sha)
        self.assertFalse(passed)
        self.assertTrue(any("missing required check" in r for r in reasons))

        # 2. In-progress check fails closed
        data_in_progress: list[dict[str, Any]] = [
            {"name": "Skill Quality And Privacy", "status": "in_progress", "conclusion": None, "head_sha": target_sha},
            {"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha},
        ]
        passed, reasons = release.evaluate_check_runs(data_in_progress, expected_sha=target_sha)
        self.assertFalse(passed)

        # 3. Cancelled check fails closed
        data_cancelled = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "cancelled",
                "head_sha": target_sha,
            },
            {"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha},
        ]
        passed, reasons = release.evaluate_check_runs(data_cancelled, expected_sha=target_sha)
        self.assertFalse(passed)

        # 4. Failed check fails closed
        data_failed = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
            },
            {"name": "Tests", "status": "completed", "conclusion": "success", "head_sha": target_sha},
        ]
        passed, reasons = release.evaluate_check_runs(data_failed, expected_sha=target_sha)
        self.assertFalse(passed)

        # 5. All required checks successful passes
        data_success = [
            {
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = release.evaluate_check_runs(data_success, expected_sha=target_sha)
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

    def test_evaluate_check_runs_string_ids_sorted_numerically(self) -> None:
        target_sha = "abc1234"
        data_failing_later = [
            {
                "id": "9",
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "10",
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "1",
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = release.evaluate_check_runs(data_failing_later, expected_sha=target_sha)
        self.assertFalse(passed)
        self.assertTrue(any("did not succeed" in r for r in reasons))

        data_passing_later = [
            {
                "id": "9",
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "10",
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
            {
                "id": "1",
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
            },
        ]
        passed, reasons = release.evaluate_check_runs(data_passing_later, expected_sha=target_sha)
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

    def test_evaluate_check_runs_with_check_suite_filter(self) -> None:
        target_sha = "abc1234"
        data = [
            {
                "id": 1,
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
                "check_suite": {"id": 100},
            },
            {
                "id": 2,
                "name": "Skill Quality And Privacy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
                "check_suite": {"id": 100},
            },
            {
                "id": 3,
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": target_sha,
                "app": {"slug": "github-actions"},
                "check_suite": {"id": 200},
            },
        ]
        passed, reasons = release.evaluate_check_runs(data, expected_sha=target_sha, expected_check_suite_id=100)
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

        passed, reasons = release.evaluate_check_runs(data, expected_sha=target_sha, expected_check_suite_id=200)
        self.assertFalse(passed)

    def test_public_api_stability_unmarked_removal_fails(self) -> None:
        with (
            patch.object(public_api_stability, "latest_semver_tag", return_value="v0.1.0"),
            patch.object(public_api_stability, "skill_names_at", side_effect=[{"skill-a", "skill-b"}, {"skill-a"}]),
            patch.object(public_api_stability, "has_breaking_marker", return_value=False),
        ):
            self.assertEqual(public_api_stability.main(), 1)

    def test_public_api_stability_marked_removal_passes(self) -> None:
        with (
            patch.object(public_api_stability, "latest_semver_tag", return_value="v0.1.0"),
            patch.object(public_api_stability, "skill_names_at", side_effect=[{"skill-a", "skill-b"}, {"skill-a"}]),
            patch.object(public_api_stability, "has_breaking_marker", return_value=True),
        ):
            self.assertEqual(public_api_stability.main(), 0)

    def test_public_api_stability_no_removal_passes(self) -> None:
        with (
            patch("public_api_stability.latest_semver_tag", return_value="v0.1.0"),
            patch("public_api_stability.skill_names_at", side_effect=[{"skill-a"}, {"skill-a", "skill-b"}]),
        ):
            self.assertEqual(public_api_stability.main(), 0)

    def test_public_api_stability_no_tag_skips(self) -> None:
        with patch("public_api_stability.latest_semver_tag", return_value=None):
            self.assertEqual(public_api_stability.main(), 0)


if __name__ == "__main__":
    unittest.main()
