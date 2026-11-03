import copy
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "preflight.py"
SPEC = importlib.util.spec_from_file_location("preflight", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
NOW = "2026-11-03T01:00:00Z"
HEAD = "a" * 40


def row(stage="implementing"):
    return {
        "stage": stage,
        "owner": "coordinator",
        "next_action": "Deliver current bounded slice",
        "observed_at": NOW,
        "last_progress_at": NOW,
        "evidence": ["receipt.json"],
    }


def worker(identity="worker-a", worktree="/tmp/work", paths=None):
    return {
        "id": identity,
        "model": "approved-implementation-model",
        "role": "implementation",
        "user": "worker-owner",
        "worktree": worktree,
        "write_paths": paths or ["src"],
        "sandbox": True,
        "permission_bypass": False,
    }


def ready_row():
    result = row("merge_ready")
    result.update(
        {
            "revision": HEAD,
            "reviews": [
                {
                    "role": role,
                    "reviewer": role + "-session",
                    "revision": HEAD,
                    "verdict": "SHIP",
                    "evidence": [role + ".json"],
                }
                for role in ["reviewer-a", "reviewer-b", "reviewer-c"]
            ],
            "ci": {"revision": HEAD, "status": "success", "evidence": ["run/123"]},
            "branch_protection": {"verified": True, "evidence": ["ruleset.json"]},
            "approval": {"authority": "primary-approval.md"},
        }
    )
    return result


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.policy = {
            "expected_projects": ["foundation", "product"],
            "implementer_model": "approved-implementation-model",
            "required_review_roles": ["reviewer-a", "reviewer-b", "reviewer-c"],
        }
        self.doc = {
            "state": "active",
            "model": "coordinator-model",
            "wake_owner": "native-schedule",
            "updatedAt": NOW,
            "nextAction": "Advance admitted projects",
            "delivery_control": {
                "schema_version": 1,
                "observed_at": NOW,
                "directives": [],
                "pending_inputs": [],
                "projects": {"foundation": row(), "product": row("discovery")},
            },
        }
        self.actions = {"prioritized_operator_unblock_decisions": [], "pending_actions": []}

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def run_audit(self):
        self.write("coordinator-status.json", self.doc)
        self.write("policy.json", self.policy)
        self.write("local-actions.json", self.actions)
        return MODULE.audit(self.root, self.root / "policy.json", NOW)

    def codes(self, result):
        return {v["code"] for v in result["diagnostics"]}

    def test_complete_current_rows_pass_without_authorizing_work(self):
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["authorizes_actions"])
        self.assertEqual(result["scope"], "structural_evidence_only")
        self.assertEqual(
            result["coordinator_sha256"],
            hashlib.sha256((self.root / "coordinator-status.json").read_bytes()).hexdigest(),
        )

    def set_control(self, value):
        self.policy["required_controls"] = ["precommit"]
        for project in self.doc["delivery_control"]["projects"].values():
            project["revision"] = HEAD
            project["controls"] = {"precommit": copy.deepcopy(value)}

    def test_required_control_absence_is_visible(self):
        self.policy["required_controls"] = ["precommit"]
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("CONTROL_MISSING", self.codes(result))
        self.assertTrue(any("precommit" in d["message"] for d in result["diagnostics"]))

    def test_control_enforced_claim_needs_current_full_revision(self):
        self.set_control(
            {
                "status": "enforced",
                "owner": "owner",
                "next_action": "Maintain gate",
                "observed_at": NOW,
                "evidence": ["run/123"],
            }
        )
        self.assertIn("CONTROL_ENFORCEMENT_UNPROVEN", self.codes(self.run_audit()))

    def test_control_enforced_claim_cannot_reuse_old_revision(self):
        self.set_control(
            {
                "status": "enforced",
                "owner": "owner",
                "next_action": "Maintain gate",
                "observed_at": NOW,
                "evidence": ["run/123"],
                "revision": "b" * 40,
            }
        )
        self.assertIn("CONTROL_ENFORCEMENT_UNPROVEN", self.codes(self.run_audit()))

    def test_current_control_record_passes_structurally(self):
        self.set_control(
            {
                "status": "enforced",
                "owner": "owner",
                "next_action": "Maintain gate",
                "observed_at": NOW,
                "evidence": ["run/123"],
                "revision": HEAD,
            }
        )
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["projects"][0]["controls"], [{"id": "precommit", "status": "enforced"}])
        self.assertFalse(result["authorizes_actions"])

    def test_control_na_needs_specific_reason_and_evidence(self):
        self.set_control(
            {
                "status": "not_applicable",
                "owner": "owner",
                "observed_at": NOW,
                "reason": "Documentation-only artifact has no deployment stage",
                "evidence": ["scope.json"],
            }
        )
        self.assertEqual(self.run_audit()["exit_code"], 0)
        del self.doc["delivery_control"]["projects"]["foundation"]["controls"]["precommit"]["reason"]
        self.assertIn("CONTROL_NA_UNJUSTIFIED", self.codes(self.run_audit()))

    def test_model_and_review_configuration_are_explicit(self):
        for key in ["implementer_model", "required_review_roles"]:
            old = self.policy.pop(key)
            self.assertEqual(self.run_audit()["exit_code"], 2)
            self.policy[key] = old

    def test_legacy_membership_does_not_count_as_current_coverage(self):
        del self.doc["delivery_control"]
        self.doc["initiatives"] = {"foundation": {"status": "running"}, "product": {"status": "running"}}
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(sum(d["code"] == "COVERAGE_MISSING" for d in result["diagnostics"]), 2)

    def test_missing_action_collections_is_not_empty_queue(self):
        self.actions = {"updatedAt": NOW}
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("ACTION_COLLECTIONS_MISSING", self.codes(result))

    def test_human_collection_without_items_is_valid(self):
        self.actions = {"pending_actions": [{"id": "review-1", "status": "ready_for_mac_bridge"}]}
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["local_actions"]["ready"][0]["primary_authority_match_required"])

    def test_missing_action_file_is_unknown_not_a_passing_empty_queue(self):
        self.run_audit()
        (self.root / "local-actions.json").unlink()
        result = MODULE.audit(self.root, self.root / "policy.json", NOW)
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("ACTION_FILE_MISSING", self.codes(result))
        self.assertEqual(result["local_actions"]["state"], "not_observed")

    def test_wait_with_old_progress_does_not_create_starvation(self):
        foundation = self.doc["delivery_control"]["projects"]["foundation"]
        foundation.update(
            stage="waiting",
            last_progress_at="2026-11-02T10:00:00Z",
            wait={"until": "2026-11-03T02:00:00Z", "reason": "Advancing CI", "evidence": ["run/123"]},
        )
        self.assertEqual(self.run_audit()["exit_code"], 0)

    def test_expired_wait_requires_outcome_check(self):
        foundation = self.doc["delivery_control"]["projects"]["foundation"]
        foundation.update(
            stage="waiting", wait={"until": "2026-11-03T00:59:00Z", "reason": "CI", "evidence": ["run/123"]}
        )
        self.assertIn("WAIT_DUE", self.codes(self.run_audit()))

    def test_accountable_blocker_is_not_implementation_starvation(self):
        foundation = self.doc["delivery_control"]["projects"]["foundation"]
        foundation.update(
            stage="blocked",
            last_progress_at="2026-11-02T10:00:00Z",
            blocker={"owner": "Patrick", "reason": "Prepared live-apply decision", "evidence": ["saved-plan.json"]},
        )
        self.assertEqual(self.run_audit()["exit_code"], 0)

    def test_red_revision_cannot_reuse_prior_green_review(self):
        foundation = ready_row()
        foundation["reviews"][0]["revision"] = "b" * 40
        self.doc["delivery_control"]["projects"]["foundation"] = foundation
        self.assertIn("REVIEW_NOT_CURRENT", self.codes(self.run_audit()))

    def test_same_reviewer_cannot_fill_three_independent_roles(self):
        foundation = ready_row()
        for review in foundation["reviews"]:
            review["reviewer"] = "same-session"
        self.doc["delivery_control"]["projects"]["foundation"] = foundation
        self.assertIn("REVIEW_NOT_INDEPENDENT", self.codes(self.run_audit()))

    def test_merge_needs_ci_protection_and_authority_references(self):
        foundation = ready_row()
        foundation["ci"]["evidence"] = []
        foundation["branch_protection"]["evidence"] = []
        foundation["approval"] = {}
        self.doc["delivery_control"]["projects"]["foundation"] = foundation
        self.assertTrue({"CI_NOT_CURRENT", "PROTECTION_UNPROVEN", "AUTHORITY_MISSING"} <= self.codes(self.run_audit()))

    def test_fresh_root_does_not_refresh_project_progress(self):
        self.doc["delivery_control"]["projects"]["product"]["last_progress_at"] = "2026-11-02T10:00:00Z"
        self.assertIn("PROGRESS_AGING", self.codes(self.run_audit()))

    def test_naive_malformed_and_future_observations_fail_closed(self):
        for value in ["not-a-time", "2026-11-03T01:00:00", "2026-11-03T01:06:00Z", None]:
            with self.subTest(value=value):
                self.doc["delivery_control"]["projects"]["foundation"]["observed_at"] = value
                result = self.run_audit()
                self.assertEqual(result["exit_code"], 2)
                self.assertIn("TIMESTAMP_INVALID", self.codes(result))

    def test_obsolete_aliases_are_reported_and_newest_root_selected(self):
        self.doc["coordinator"] = {"updatedAt": "2026-11-02T22:00:00Z", "state": "idle", "next": "Old task"}
        self.doc["updated_at"] = "2026-11-02T20:00:00Z"
        result = self.run_audit()
        self.assertEqual(result["coordinator"]["state"], "active")
        self.assertIn("ALIAS_DRIFT", self.codes(result))

    def test_fresh_root_timestamp_does_not_refresh_nested_fields(self):
        self.doc["coordinator"] = {"updatedAt": "2026-11-02T22:00:00Z"}
        for key in ["state", "model", "wake_owner", "nextAction"]:
            self.doc["coordinator"][key] = self.doc.pop(key)
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("COORDINATOR_FIELD_STALE", self.codes(result))
        self.assertEqual(result["coordinator"]["field_sources"]["state"]["observed_at"], "2026-11-02T22:00:00+00:00")

    def test_workers_with_disjoint_paths_can_share_observed_worktree(self):
        projects = self.doc["delivery_control"]["projects"]
        projects["foundation"]["workers"] = [worker(paths=["service"])]
        projects["product"]["workers"] = [worker("worker-b", paths=["web"])]
        self.assertEqual(self.run_audit()["exit_code"], 0)

    def test_overlapping_writer_paths_are_flagged(self):
        projects = self.doc["delivery_control"]["projects"]
        projects["foundation"]["workers"] = [worker(paths=["src"])]
        projects["product"]["workers"] = [worker("worker-b", paths=["src/api"])]
        self.assertIn("WRITER_CONFLICT", self.codes(self.run_audit()))

    def test_symlinked_worktree_cannot_hide_conflict(self):
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        projects = self.doc["delivery_control"]["projects"]
        projects["foundation"]["workers"] = [worker(worktree=str(real))]
        projects["product"]["workers"] = [worker("worker-b", worktree=str(alias))]
        self.assertIn("WRITER_CONFLICT", self.codes(self.run_audit()))

    def test_nested_worktrees_cannot_hide_overlapping_writers(self):
        projects = self.doc["delivery_control"]["projects"]
        projects["foundation"]["workers"] = [worker(worktree="/tmp/parent", paths=["src"])]
        projects["product"]["workers"] = [worker("worker-b", worktree="/tmp/parent/src", paths=["api"])]
        self.assertIn("WRITER_CONFLICT", self.codes(self.run_audit()))

    def test_unrelated_glob_scopes_do_not_conflict(self):
        projects = self.doc["delivery_control"]["projects"]
        projects["foundation"]["workers"] = [worker(worktree="/tmp/one", paths=["src/*"])]
        projects["product"]["workers"] = [worker("worker-b", worktree="/tmp/two", paths=["src/*"])]
        self.assertNotIn("WRITER_CONFLICT", self.codes(self.run_audit()))

    def test_relative_worktree_is_invalid(self):
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [worker(worktree="relative")]
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("WORKTREE_INVALID", self.codes(result))

    def test_permissions_are_explicit_booleans(self):
        for key in ["sandbox", "permission_bypass"]:
            for value in [None, "false", 0]:
                with self.subTest(key=key, value=value):
                    w = worker()
                    w[key] = value
                    self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [w]
                    result = self.run_audit()
                    self.assertEqual(result["exit_code"], 2)
                    self.assertIn("WORKER_PERMISSIONS_INVALID", self.codes(result))

    def test_unproven_isolation_and_cross_uid_scope_are_flagged(self):
        w = worker(worktree="/home/other-owner/work/repo")
        w.update(permission_bypass=True, sandbox=False)
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [w]
        self.assertTrue(
            {"SANDBOX_UNPROVEN", "BYPASS_AUTHORITY_MISSING", "CROSS_USER_PATH"} <= self.codes(self.run_audit())
        )

    def test_disallowed_implementation_model_does_not_match_policy(self):
        w = worker()
        w["model"] = "unapproved-model"
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [w]
        self.assertIn("IMPLEMENTER_MODEL", self.codes(self.run_audit()))

    def test_duplicate_ready_ids_are_deduplicated(self):
        request = {"id": "a", "status": "ready_for_execution", "revision": HEAD}
        self.actions = {"items": [request], "pending_actions": [copy.deepcopy(request)]}
        self.assertEqual(len(self.run_audit()["local_actions"]["ready"]), 1)

    def test_conflicting_action_ids_are_quarantined(self):
        self.actions = {
            "items": [{"id": "a", "status": "ready_for_local", "revision": HEAD}],
            "pending_actions": [{"id": "a", "status": "ready_for_local", "revision": "b" * 40}],
        }
        result = self.run_audit()
        self.assertIn("ACTION_ID_CONFLICT", self.codes(result))
        self.assertEqual(result["local_actions"]["ready"], [])

    def test_completed_exact_revision_suppresses_ready_but_different_revision_does_not(self):
        self.actions = {"items": [{"id": "a", "status": "ready_for_local", "revision": HEAD}]}
        self.write("local-action-results.json", {"results": [{"id": "a", "status": "completed", "revision": HEAD}]})
        self.assertEqual(self.run_audit()["local_actions"]["ready"], [])
        self.write("local-action-results.json", {"results": [{"id": "a", "status": "completed", "revision": "b" * 40}]})
        self.assertEqual(len(self.run_audit()["local_actions"]["ready"]), 1)

    def test_unrecognized_result_schema_is_not_silently_empty(self):
        self.write("local-action-results.json", {"history": []})
        self.assertIn("RESULT_SCHEMA_UNKNOWN", self.codes(self.run_audit()))

    def test_directive_digest_and_applied_receipt_both_required(self):
        content = b"User authority for a bounded task\n"
        (self.root / "direction.md").write_bytes(content)
        directive = {"path": "direction.md", "sha256": hashlib.sha256(content).hexdigest()}
        self.policy["directives"] = [directive]
        self.assertIn("DIRECTIVE_NOT_RECORDED", self.codes(self.run_audit()))
        self.doc["delivery_control"]["directives"] = [directive]
        self.assertEqual(self.run_audit()["exit_code"], 0)
        (self.root / "direction.md").write_bytes(content + b"changed")
        self.assertIn("DIRECTIVE_CHANGED", self.codes(self.run_audit()))

    def test_queued_input_defers_additional_input(self):
        self.doc["delivery_control"]["pending_inputs"] = [
            {"id": "dispatch-1", "status": "queued", "evidence": ["transport.json"]}
        ]
        self.assertTrue(self.run_audit()["defer_input"])

    def test_malformed_pending_input_defers_conservatively(self):
        for pending in [[{}], [{"id": "dispatch-1", "status": "unknown", "evidence": ["transport.json"]}], "queued"]:
            with self.subTest(pending=pending):
                self.doc["delivery_control"]["pending_inputs"] = pending
                result = self.run_audit()
                self.assertEqual(result["exit_code"], 2)
                self.assertTrue(result["defer_input"])
                self.assertIn("PENDING_INPUTS_INVALID", self.codes(result))

    def test_missing_pending_input_collection_remains_unknown(self):
        del self.doc["delivery_control"]["pending_inputs"]
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 2)
        self.assertTrue(result["defer_input"])
        self.assertIn("PENDING_INPUTS_INVALID", self.codes(result))

    def test_audit_does_not_mutate_files_or_echo_directive_contents(self):
        self.run_audit()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        result = MODULE.audit(self.root, self.root / "policy.json", NOW)
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        self.assertEqual(before, after)
        self.assertNotIn("Advance admitted projects", json.dumps(result))

    def test_malformed_json_returns_schema_error(self):
        self.run_audit()
        (self.root / "coordinator-status.json").write_text("{")
        result = MODULE.audit(self.root, self.root / "policy.json", NOW)
        self.assertEqual(result["exit_code"], 2)

    def test_oversized_json_and_governing_files_are_rejected(self):
        self.run_audit()
        oversized = b" " * (MODULE.MAX_INPUT_BYTES + 1)
        (self.root / "coordinator-status.json").write_bytes(oversized)
        result = MODULE.audit(self.root, self.root / "policy.json", NOW)
        self.assertEqual(result["exit_code"], 2)
        (self.root / "direction.md").write_bytes(oversized)
        self.policy["directives"] = [{"path": "direction.md", "sha256": hashlib.sha256(oversized).hexdigest()}]
        self.assertIn("DIRECTIVE_MISSING", self.codes(self.run_audit()))


if __name__ == "__main__":
    unittest.main()
