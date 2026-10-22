"""Behavior checks for queue/claim safety; historical cases test routing, not bug detection."""

import importlib.util
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "review_monitor", ROOT / "skills/github-pr-shepherding/scripts/review_monitor.py"
)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def attempt_claim(path):
    try:
        return monitor.claim(path, 242, "h", "test-owner")
    except ValueError:
        return None


class ReviewMonitorTest(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "number": 242,
            "title": "Delivery contracts",
            "body": "Scope",
            "state": "open",
            "draft": False,
            "head": {"sha": "h"},
            "base": {"sha": "b"},
        }
        self.previous = {
            "number": 242,
            "head_sha": "h",
            "base_sha": "b",
            "completed": True,
            "completed_code_review": True,
            "metadata_fingerprint": monitor.fingerprint(self.pr),
            "source_verdict": "REVISE",
            "findings": [{"title": "Forbidden owner commit"}],
        }
        self.ledger = {
            "schema_version": 1,
            "repository": "example/repo",
            "review_actor": "reviewer",
            "head_evaluations": [self.previous],
            "in_progress": [],
            "reviews": [],
        }

    def test_unchanged_review_is_skipped_but_findings_remain(self):
        self.previous.update(submitted=True, review_id=17)
        row = monitor.detect(self.published_observation(), self.ledger)[0]
        self.assertEqual(row["decision"], "skip_unchanged")
        self.assertEqual(row["publication_state"], "published")
        self.assertEqual(len(row["unresolved_findings"]), 1)

    def published_observation(self):
        return {
            "242": {
                "pr": self.pr,
                "reviews": [
                    {
                        "id": 17,
                        "commit_id": "h",
                        "state": "COMMENTED",
                        "user": {"login": "reviewer"},
                        "html_url": "https://github.com/example/repo/pull/242#pullrequestreview-17",
                    }
                ],
            }
        }

    def test_local_completed_assessment_is_queued_for_publication(self):
        row = monitor.detect({"242": {"pr": self.pr, "reviews": []}}, self.ledger)[0]
        self.assertEqual(row["decision"], "publication_pending")
        self.assertEqual(row["publication_state"], "pending")
        self.assertTrue(row["assessment_complete"])
        self.assertEqual(row["unresolved_findings"], self.previous["findings"])

    def test_claimed_publication_without_native_receipt_does_not_skip(self):
        self.previous.update(submitted=True, review_id=17, review_url="local-claim")
        row = monitor.detect({"242": {"pr": self.pr, "reviews": []}}, self.ledger)[0]
        self.assertEqual(row["decision"], "publication_pending")

    def test_missing_review_history_requires_refresh_not_assumed_publication(self):
        row = monitor.detect({"242": {"pr": self.pr}}, self.ledger)[0]
        self.assertEqual(row["decision"], "refresh_native_history")
        self.assertEqual(row["publication_state"], "unknown")

    def test_native_current_review_without_linked_assessment_is_reconciled(self):
        row = monitor.detect(self.published_observation(), self.ledger)[0]
        self.assertEqual(row["decision"], "reconcile_native_history_before_publication")

    def test_old_commit_and_bot_reviews_do_not_publish_current_assessment(self):
        observation = self.published_observation()
        wrong_commit = deepcopy(observation["242"]["reviews"][0])
        wrong_commit["commit_id"] = "old-head"
        bot = deepcopy(observation["242"]["reviews"][0])
        bot["user"]["login"] = "other-reviewer"
        observation["242"]["reviews"] = [wrong_commit, bot]
        self.previous.update(submitted=True, review_id=17)
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["decision"],
            "publication_pending",
        )

    def test_pending_native_review_is_reconciled_without_duplicate_submission(self):
        observation = self.published_observation()
        observation["242"]["reviews"][0]["state"] = "PENDING"
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["decision"],
            "reconcile_native_history_before_publication",
        )
        self.assertEqual(
            monitor.detect(
                observation, {"head_evaluations": [], "repository": "example/repo", "review_actor": "reviewer"}
            )[0]["decision"],
            "reconcile_native_history_before_review",
        )

    def test_dismissed_review_still_has_a_publication_receipt(self):
        observation = self.published_observation()
        observation["242"]["reviews"][0]["state"] = "DISMISSED"
        self.previous.update(submitted=True, review_id=17)
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["publication_state"],
            "published",
        )

    def test_local_metadata_reassessment_does_not_inherit_old_publication(self):
        old = deepcopy(self.previous)
        old.update(submitted=True, review_id=17, metadata_fingerprint="older-metadata")
        self.ledger["head_evaluations"].insert(0, old)
        row = monitor.detect(self.published_observation(), self.ledger)[0]
        self.assertEqual(row["decision"], "reconcile_native_history_before_publication")

    def test_conflicting_review_id_cannot_be_rescued_by_an_old_url(self):
        observation = self.published_observation()
        self.previous.update(
            submitted=True,
            review_id=99,
            review_url=observation["242"]["reviews"][0]["html_url"],
        )
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["decision"],
            "reconcile_native_history_before_publication",
        )

    def test_legacy_url_only_receipt_requires_matching_native_review(self):
        observation = self.published_observation()
        self.previous.update(submitted=True, review_url=observation["242"]["reviews"][0]["html_url"])
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["publication_state"],
            "published",
        )

    def test_full_first_page_requires_pagination_before_new_publication(self):
        history = [
            {
                "id": n,
                "commit_id": "old",
                "state": "COMMENTED",
                "user": {"login": "reviewer"},
            }
            for n in range(100)
        ]
        observation = {"242": {"pr": self.pr, "reviews": history}}
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["decision"],
            "refresh_native_history",
        )
        observation["242"]["reviews_complete"] = True
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["decision"],
            "publication_pending",
        )

    def test_explicit_incomplete_history_is_not_an_absence_receipt(self):
        observation = {"242": {"pr": self.pr, "reviews": [], "reviews_complete": False}}
        self.assertEqual(
            monitor.detect(observation, self.ledger)[0]["decision"],
            "refresh_native_history",
        )

    def test_draft_local_assessment_is_never_queued_for_publication(self):
        self.pr["draft"] = True
        self.assertEqual(
            monitor.detect({"242": {"pr": self.pr, "reviews": []}}, self.ledger)[0]["decision"],
            "skip_not_ready",
        )

    def test_publication_queue_preserves_assessment_without_source_rereview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            monitor.write(path, self.ledger)
            rows = monitor.detect({"242": {"pr": self.pr, "reviews": []}}, self.ledger)
            monitor.observe(path, rows, "snapshot.json")
            recorded = monitor.read(path)
            self.assertEqual(recorded["head_evaluations"], self.ledger["head_evaluations"])
            self.assertEqual(recorded["monitor_pilot"]["queue"][0]["decision"], "publication_pending")

    def test_metadata_only_change_does_not_repeat_source_review(self):
        self.pr["body"] += " Revised validation"
        self.assertEqual(monitor.classify(self.pr, [self.previous]), "metadata_only")

    def test_base_change_requires_broader_review(self):
        self.pr["base"]["sha"] = "new-base"
        self.assertEqual(monitor.classify(self.pr, [self.previous]), "base_changed_expand_review")

    def test_green_followup_carries_findings_and_rejects_stale_checks(self):
        self.pr["head"]["sha"] = "new-head"
        observation = {
            "pr": self.pr,
            "checks": {"check_runs": [{"head_sha": "h", "conclusion": "success"}]},
        }
        row = monitor.detect({"242": observation}, self.ledger)[0]
        self.assertEqual(row["decision"], "delta_review")
        self.assertEqual(len(row["unresolved_findings"]), 1)
        self.assertFalse(row["ci"]["fresh"])

    def test_draft_is_never_queued_for_submission(self):
        self.pr["draft"] = True
        self.assertEqual(monitor.classify(self.pr, []), "skip_not_ready")

    def test_native_review_without_ledger_requires_reconciliation(self):
        observation = {
            "pr": self.pr,
            "reviews": [
                {
                    "id": 1,
                    "commit_id": "h",
                    "state": "CHANGES_REQUESTED",
                    "user": {"login": "reviewer"},
                }
            ],
        }
        row = monitor.detect(
            {"242": observation}, {"head_evaluations": [], "repository": "example/repo", "review_actor": "reviewer"}
        )[0]
        self.assertEqual(row["decision"], "reconcile_native_history_before_review")

    def test_parallel_claims_admit_only_one_and_wrong_token_cannot_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ledger.json")
            monitor.write(path, self.ledger)
            with ProcessPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt_claim, [path, path]))
            tokens = [token for token in results if token]
            self.assertEqual(len(tokens), 1)
            with self.assertRaises(ValueError):
                monitor.release(path, "wrong-token")
            monitor.release(path, tokens[0])
            self.assertEqual(monitor.read(path)["in_progress"], [])

    def test_observation_preserves_reviews_evaluations_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            original = dict(**self.ledger, unknown={"preserved": True})
            monitor.write(path, original)
            monitor.observe(
                path,
                monitor.detect({"242": {"pr": self.pr}}, original),
                "snapshot.json",
            )
            actual = monitor.read(path)
            for key, value in original.items():
                self.assertEqual(actual[key], value)


if __name__ == "__main__":
    unittest.main()
