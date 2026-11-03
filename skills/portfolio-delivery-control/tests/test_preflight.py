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
    result = {
        "stage": stage,
        "owner": "coordinator",
        "next_action": "Deliver current bounded slice",
        "observed_at": NOW,
        "last_progress_at": NOW,
        "evidence": ["receipt.json"],
    }
    if stage in ("implementing", "review", "implementation", "reviewing"):
        result["work_item"] = {
            "claim_token": "token-active",
            "claim_expires_at": "2026-11-03T02:00:00Z",
            "revision": HEAD,
        }
    return result


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
                    "model": role + "-model",
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
            "reviewer_models": ["reviewer-a-model", "reviewer-b-model", "reviewer-c-model"],
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

    def run_preflight(self, pane_capture_file=None):
        self.write("coordinator-status.json", self.doc)
        self.write("policy.json", self.policy)
        self.write("local-actions.json", self.actions)
        return MODULE.run_preflight(self.root, self.root / "policy.json", NOW, pane_capture_file=pane_capture_file)

    def run_audit(self):
        return self.run_preflight()

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
        self.assertEqual(sum(d["code"] == "LEGACY_UNVERIFIED" for d in result["diagnostics"]), 2)
        self.assertEqual(result["summary"]["legacy_unverified"], 2)
        self.assertFalse(result["authorizes_actions"])

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
            blocker={
                "owner": "Patrick",
                "reason": "Prepared live-apply decision",
                "evidence": ["saved-plan.json"],
                "recheck_at": "2026-11-03T02:00:00Z",
            },
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

    def test_acceptance_case_1_five_legacy_and_three_absent(self):
        projects = [
            "bff",
            "software_factory",
            "matchpoint",
            "infrastructure",
            "polymarket",
            "veinroute",
            "tradestream",
            "emg_academy",
        ]
        self.policy["expected_projects"] = projects
        self.doc["delivery_control"]["projects"] = {}
        self.doc["initiatives"] = {
            "bff": {"status": "in_progress", "observed_at": NOW},
            "software_factory": {"status": "in_progress"},
            "matchpoint": {"status": "in_progress"},
            "infrastructure": {"status": "in_progress"},
            "polymarket": {"status": "in_progress"},
        }
        result = self.run_audit()
        self.assertEqual(result["summary"]["legacy_unverified"], 5)
        self.assertEqual(result["summary"]["absent"], 3)
        self.assertEqual(result["summary"]["normalized"], 0)
        self.assertEqual(result["summary"]["diagnostic_counts"]["attention"], 5)
        self.assertEqual(result["summary"]["diagnostic_counts"]["schema_error"], 3)
        self.assertFalse(result["authorizes_actions"])
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["status"], "schema_error")
        legacy_rows = [p for p in result["projects"] if p["coverage"] == "legacy_unverified"]
        absent_rows = [p for p in result["projects"] if p["coverage"] == "absent"]
        self.assertEqual(len(legacy_rows), 5)
        self.assertEqual(len(absent_rows), 3)
        for row in legacy_rows:
            self.assertTrue(row["source_pointer"].startswith("/initiatives/"))
            self.assertEqual(len(row["source_sha256"]), 64)
        bff_row = next(p for p in legacy_rows if p["project"] == "bff")
        self.assertEqual(bff_row["observed_at"], NOW)
        sf_row = next(p for p in legacy_rows if p["project"] == "software_factory")
        self.assertIsNone(sf_row["observed_at"])

    def test_acceptance_case_2_fresh_root_does_not_refresh_legacy_facts(self):
        self.policy["expected_projects"] = ["foundation", "product"]
        self.doc["updatedAt"] = NOW
        del self.doc["delivery_control"]["projects"]["foundation"]
        del self.doc["delivery_control"]["projects"]["product"]
        self.doc["initiatives"] = {
            "foundation": {"status": "running", "updated_at": "2026-10-01T00:00:00Z"},
            "product": {"status": "running"},
        }
        result = self.run_audit()
        proj_map = {p["project"]: p for p in result["projects"]}
        self.assertEqual(proj_map["foundation"]["observed_at"], "2026-10-01T00:00:00Z")
        self.assertIsNone(proj_map["product"]["observed_at"])
        self.assertFalse(result["authorizes_actions"])
        self.assertEqual(result["summary"]["legacy_unverified"], 2)

    def test_acceptance_case_3_malformed_normalized_row_remains_schema_error(self):
        self.doc["delivery_control"]["projects"]["foundation"] = "malformed-string"
        self.doc["initiatives"] = {"foundation": {"status": "healthy", "owner": "coordinator"}}
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["status"], "schema_error")
        self.assertIn("PROJECT_INVALID", self.codes(result))
        self.assertNotIn("LEGACY_UNVERIFIED", self.codes(result))
        self.assertEqual(result["summary"]["normalized"], 1)

    def test_acceptance_case_4_missing_or_queued_input_defers_dispatch_legacy_projection_preserves(self):
        self.doc["delivery_control"]["pending_inputs"] = [
            {"id": "dispatch-1", "status": "queued", "evidence": ["transport.json"]}
        ]
        self.doc["delivery_control"]["projects"] = {}
        self.doc["initiatives"] = {"foundation": {"status": "running"}, "product": {"status": "running"}}
        result = self.run_audit()
        self.assertTrue(result["defer_input"])
        self.assertEqual(result["summary"]["legacy_unverified"], 2)
        self.assertFalse(result["authorizes_actions"])

    def test_acceptance_case_5_one_normalized_plus_n_legacy_no_duplicates(self):
        self.policy["expected_projects"] = ["foundation", "product", "third"]
        self.doc["delivery_control"]["projects"] = {"foundation": row()}
        self.doc["initiatives"] = {
            "foundation": {"status": "legacy-foundation"},
            "product": {"status": "legacy-product"},
            "third": {"status": "legacy-third"},
        }
        result = self.run_audit()
        self.assertEqual(result["summary"]["normalized"], 1)
        self.assertEqual(result["summary"]["legacy_unverified"], 2)
        self.assertEqual(result["summary"]["absent"], 0)
        self.assertEqual(len(result["projects"]), 3)
        self.assertEqual([p["project"] for p in result["projects"]], ["foundation", "product", "third"])
        self.assertEqual(result["projects"][0]["coverage"], "normalized")
        self.assertEqual(result["projects"][1]["coverage"], "legacy_unverified")
        self.assertEqual(result["projects"][2]["coverage"], "legacy_unverified")

    def test_acceptance_case_6_sparse_missing_controls_remain_explicit_gaps(self):
        self.policy["required_controls"] = ["precommit", "build_test"]
        self.doc["delivery_control"]["projects"]["foundation"]["controls"] = {
            "precommit": {
                "status": "implementing",
                "owner": "dev",
                "next_action": "setup",
                "observed_at": NOW,
                "evidence": ["log.txt"],
            }
        }
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("CONTROL_MISSING", self.codes(result))
        self.assertIn("CONTROL_GAP", self.codes(result))
        self.assertTrue(any("build_test" in d["message"] for d in result["diagnostics"]))
        self.assertTrue(any("precommit" in d["message"] for d in result["diagnostics"]))

    def test_acceptance_case_7_blocker_aging_and_recheck_deadlines(self):
        foundation = self.doc["delivery_control"]["projects"]["foundation"]
        now_time = "2026-11-03T01:10:00Z"

        # 7a: Month-old blocker without recheck_at emits attention (BLOCKER_STALE)
        foundation.update(
            stage="blocked",
            last_progress_at="2026-10-01T00:00:00Z",
            blocker={
                "owner": "Patrick",
                "reason": "Waiting on decision",
                "evidence": ["memo.md"],
                "since": "2026-10-01T00:00:00Z",
            },
        )
        self.write("coordinator-status.json", self.doc)
        self.write("policy.json", self.policy)
        self.write("local-actions.json", self.actions)
        result = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("BLOCKER_STALE", {d["code"] for d in result["diagnostics"]})

        # 7b: Documented future recheck_at passes with 0 diagnostics
        foundation["blocker"]["recheck_at"] = "2026-11-03T02:00:00Z"
        self.write("coordinator-status.json", self.doc)
        result = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["diagnostics"], [])

        # 7c: Expired recheck_at emits attention (BLOCKER_RECHECK_DUE)
        foundation["blocker"]["recheck_at"] = "2026-11-03T01:00:00Z"
        self.write("coordinator-status.json", self.doc)
        result = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("BLOCKER_RECHECK_DUE", {d["code"] for d in result["diagnostics"]})

        # 7d: Malformed recheck_at emits schema_error
        foundation["blocker"]["recheck_at"] = "invalid-date"
        self.write("coordinator-status.json", self.doc)
        result = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("BLOCKER_INVALID", {d["code"] for d in result["diagnostics"]})

    def test_acceptance_case_8_repeated_reads_are_deterministic_and_pure(self):
        self.run_audit()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        res1 = MODULE.audit(self.root, self.root / "policy.json", NOW)
        res2 = MODULE.audit(self.root, self.root / "policy.json", NOW)
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(res1, res2)

    def test_legacy_sources_list_and_root_projects_lookup(self):
        self.policy["expected_projects"] = [
            "p_list_id",
            "p_list_name",
            "p_list_proj",
            "p_list_key",
            "p_root_dict",
            "p_root_list",
        ]
        self.doc["delivery_control"]["projects"] = {}
        self.doc["initiatives"] = [
            {"id": "p_list_id", "observed_at": NOW},
            {"name": "p_list_name"},
            {"project": "p_list_proj"},
            {"key": "p_list_key"},
            "non-dict-item",
        ]
        self.doc["projects"] = {
            "p_root_dict": {"status": "ok"},
        }
        result = self.run_audit()
        proj_map = {p["project"]: p for p in result["projects"]}
        self.assertEqual(proj_map["p_list_id"]["source_pointer"], "/initiatives/0")
        self.assertEqual(proj_map["p_list_name"]["source_pointer"], "/initiatives/1")
        self.assertEqual(proj_map["p_list_proj"]["source_pointer"], "/initiatives/2")
        self.assertEqual(proj_map["p_list_key"]["source_pointer"], "/initiatives/3")
        self.assertEqual(proj_map["p_root_dict"]["source_pointer"], "/projects/p_root_dict")
        self.assertEqual(proj_map["p_root_list"]["coverage"], "absent")

    def test_legacy_root_projects_list_lookup(self):
        self.policy["expected_projects"] = ["p_root_list_item"]
        self.doc["delivery_control"]["projects"] = {}
        self.doc["projects"] = [{"id": "p_root_list_item", "status": "ok"}]
        result = self.run_audit()
        self.assertEqual(result["projects"][0]["source_pointer"], "/projects/0")
        self.assertEqual(result["projects"][0]["coverage"], "legacy_unverified")

    def test_malformed_projects_container_in_delivery_control(self):
        self.doc["delivery_control"]["projects"] = "not-a-dict"
        self.doc["initiatives"] = {"foundation": {"status": "running"}}
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("PROJECTS_INVALID", self.codes(result))
        self.assertIn("PROJECT_INVALID", self.codes(result))
        self.assertEqual(result["summary"]["normalized"], 0)

    def test_blocker_timestamp_variations(self):
        foundation = self.doc["delivery_control"]["projects"]["foundation"]
        foundation.update(
            stage="blocked",
            blocker={"owner": "dev", "reason": "wait", "evidence": ["e.txt"], "until": "2026-11-03T02:00:00Z"},
        )
        self.assertEqual(self.run_audit()["exit_code"], 0)
        foundation["blocker"] = {
            "owner": "dev",
            "reason": "wait",
            "evidence": ["e.txt"],
            "recheck_by": "2026-11-03T02:00:00Z",
        }
        self.assertEqual(self.run_audit()["exit_code"], 0)
        foundation["blocker"] = {"owner": "dev", "reason": "wait", "evidence": ["e.txt"], "blocked_at": NOW}
        self.assertEqual(self.run_audit()["exit_code"], 0)
        foundation["blocker"] = {"owner": "dev", "reason": "wait", "evidence": ["e.txt"], "since": NOW}
        self.assertEqual(self.run_audit()["exit_code"], 0)
        del foundation["last_progress_at"]
        foundation["blocker"] = {"owner": "dev", "reason": "wait", "evidence": ["e.txt"]}
        self.assertIn("BLOCKER_STALE", self.codes(self.run_audit()))
        foundation["blocker"] = {"owner": "dev"}
        self.assertIn("BLOCKER_INVALID", self.codes(self.run_audit()))

    def test_cli_main_json_and_markdown(self):
        import io
        from unittest.mock import patch

        self.run_audit()
        out_json = io.StringIO()
        with patch("sys.stdout", out_json):
            code = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 0)
        parsed = json.loads(out_json.getvalue())
        self.assertIn("summary", parsed)

        out_md = io.StringIO()
        with patch("sys.stdout", out_md):
            code = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--format",
                    "markdown",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("Structural preflight", out_md.getvalue())

        out_err = io.StringIO()
        with patch("sys.stdout", out_err):
            code = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    "malformed-now",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("INPUT_INVALID", out_err.getvalue())

    def test_additional_edge_cases_for_coverage(self):
        # 1. Top-level doc not a dict
        self.write("coordinator-status.json", "not a dict")
        result = MODULE.audit(self.root, self.root / "policy.json", NOW)
        self.assertEqual(result["exit_code"], 2)

        # 2. Nested coordinator not a dict
        self.write("coordinator-status.json", self.doc)
        self.doc["coordinator"] = "not a dict"
        self.assertIn("COORDINATOR_INVALID", self.codes(self.run_audit()))

        # 3. Non-string coordinator field
        self.doc["coordinator"] = {}
        self.doc["model_id"] = 12345
        self.assertIn("COORDINATOR_FIELD_INVALID", self.codes(self.run_audit()))

        # 4. Reviews not a list
        foundation = ready_row()
        foundation["reviews"] = "not a list"
        self.doc["delivery_control"]["projects"]["foundation"] = foundation
        self.assertIn("REVIEWS_INVALID", self.codes(self.run_audit()))

        # 5. Missing or duplicate reviewer role
        foundation["reviews"] = [
            {"role": "reviewer-a", "reviewer": "r1", "revision": HEAD, "verdict": "SHIP", "evidence": ["e.txt"]}
        ]
        self.assertIn("REVIEW_MISSING_OR_DUPLICATE", self.codes(self.run_audit()))

        # 6. Reviewer identity missing
        foundation["reviews"] = [
            {"role": role, "reviewer": "", "revision": HEAD, "verdict": "SHIP", "evidence": ["e.txt"]}
            for role in ["reviewer-a", "reviewer-b", "reviewer-c"]
        ]
        self.assertIn("REVIEW_IDENTITY_MISSING", self.codes(self.run_audit()))

        # 7. Revision invalid in merge checks
        foundation = ready_row()
        foundation["revision"] = "not-40-hex"
        self.doc["delivery_control"]["projects"]["foundation"] = foundation
        self.assertIn("REVISION_INVALID", self.codes(self.run_audit()))

        # 8. Worker write scope escape
        self.doc["delivery_control"]["projects"]["foundation"] = row()
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [
            worker(worktree="/tmp/work", paths=["/tmp/other"])
        ]
        self.assertIn("WORKTREE_SCOPE_ESCAPE", self.codes(self.run_audit()))

        # 9. Workers not a list
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = "not a list"
        self.assertIn("WORKERS_INVALID", self.codes(self.run_audit()))

        # 10. Worker invalid (missing required fields)
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [{}]
        self.assertIn("WORKER_INVALID", self.codes(self.run_audit()))

        # 11. Wait invalid: missing reason/evidence or invalid deadline
        self.doc["delivery_control"]["projects"]["foundation"] = row("waiting")
        self.doc["delivery_control"]["projects"]["foundation"]["wait"] = {"reason": "ci"}
        self.assertIn("WAIT_INVALID", self.codes(self.run_audit()))
        self.doc["delivery_control"]["projects"]["foundation"]["wait"] = {
            "reason": "ci",
            "evidence": ["e.txt"],
            "until": "bad-date",
        }
        self.assertIn("WAIT_INVALID", self.codes(self.run_audit()))

        # 12. Project fields invalid
        self.doc["delivery_control"]["projects"]["foundation"] = {"stage": "invalid_stage"}
        self.assertIn("PROJECT_FIELDS_INVALID", self.codes(self.run_audit()))

        # 13. Controls not a mapping
        self.doc["delivery_control"]["projects"]["foundation"] = row()
        self.policy["required_controls"] = ["precommit"]
        self.doc["delivery_control"]["projects"]["foundation"]["controls"] = "not a dict"
        self.assertIn("CONTROLS_INVALID", self.codes(self.run_audit()))

        # 14. Control invalid: not a dict, missing owner, or evidence not list
        self.doc["delivery_control"]["projects"]["foundation"]["controls"] = {"precommit": "not a dict"}
        self.assertIn("CONTROL_INVALID", self.codes(self.run_audit()))
        self.doc["delivery_control"]["projects"]["foundation"]["controls"] = {
            "precommit": {"status": "enforced", "owner": "o", "evidence": "not a list"}
        }
        self.assertIn("CONTROL_INVALID", self.codes(self.run_audit()))

        # 15. Control unknown status & missing next_action
        self.doc["delivery_control"]["projects"]["foundation"]["controls"] = {
            "precommit": {"status": "unknown_status", "owner": "o", "observed_at": NOW}
        }
        self.assertIn("CONTROL_GAP", self.codes(self.run_audit()))
        self.doc["delivery_control"]["projects"]["foundation"]["controls"] = {
            "precommit": {"status": "implementing", "owner": "o", "observed_at": NOW}
        }
        self.assertIn("CONTROL_INVALID", self.codes(self.run_audit()))

        # 16. Collection records not a list
        self.write("local-action-results.json", {"results": "not a list"})
        self.assertIn("RESULT_COLLECTION_INVALID", self.codes(self.run_audit()))

        # 17. Result ID invalid
        self.write("local-action-results.json", {"results": [{"status": "completed", "id": 123}]})
        self.assertIn("RESULT_ID_INVALID", self.codes(self.run_audit()))

        # 18. Action missing ID or invalid revision
        self.actions = {"items": [{"status": "ready_for_execution"}]}
        self.assertIn("ACTION_ID_MISSING", self.codes(self.run_audit()))
        self.actions = {"items": [{"id": "act-1", "status": "ready", "revision": 123}]}
        self.assertIn("ACTION_REVISION_INVALID", self.codes(self.run_audit()))

        # 19. Contract invalid schema_version
        self.doc["delivery_control"]["schema_version"] = 2
        self.assertIn("CONTRACT_INVALID", self.codes(self.run_audit()))

        # 20. Directives invalid list / objects
        self.doc["delivery_control"]["schema_version"] = 1
        self.doc["delivery_control"]["directives"] = "not a list"
        self.assertIn("DIRECTIVES_INVALID", self.codes(self.run_audit()))

        # 21. Directive invalid path / sha
        self.doc["delivery_control"]["directives"] = []
        self.policy["directives"] = [{"path": "", "sha256": "bad"}]
        self.assertIn("DIRECTIVE_INVALID", self.codes(self.run_audit()))

        # 22. Policy invalid controls and bounds
        self.policy["directives"] = []
        self.policy["required_controls"] = ["ok", 123]
        self.assertIn("POLICY_INVALID", self.codes(self.run_audit()))
        self.policy["required_controls"] = []
        self.policy["max_snapshot_age_minutes"] = -1
        self.assertIn("POLICY_INVALID", self.codes(self.run_audit()))

    def test_reviewer_workers_and_artifact_exports_no_false_positives(self):
        self.policy["reviewer_models"] = ["approved-reviewer-model"]
        self.policy["banned_models"] = ["banned-reviewer-model"]
        rev_worker = {
            "id": "reviewer-1",
            "model": "approved-reviewer-model",
            "role": "review",
            "user": "worker-owner",
            "worktree": "/tmp/work",
            "write_paths": [],
            "artifact_exports": [
                "/tmp/review-export.json",
                "/home/worker-owner/review-receipt.json",
            ],
            "sandbox": True,
            "permission_bypass": False,
        }
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [rev_worker]
        result = self.run_audit()
        self.assertNotIn("IMPLEMENTER_MODEL", self.codes(result))
        self.assertNotIn("WORKTREE_SCOPE_ESCAPE", self.codes(result))
        self.assertNotIn("REVIEWER_MODEL_INVALID", self.codes(result))

        # Reviewer with unapproved model raises REVIEWER_MODEL_INVALID
        rev_worker["model"] = "unapproved-model"
        self.assertIn("REVIEWER_MODEL_INVALID", self.codes(self.run_audit()))

        # Reviewer with banned model raises REVIEWER_MODEL_INVALID
        rev_worker["model"] = "banned-reviewer-model"
        self.assertIn("REVIEWER_MODEL_INVALID", self.codes(self.run_audit()))

        # Reset model to approved
        rev_worker["model"] = "approved-reviewer-model"

        # Cross-user path in artifact_exports raises CROSS_USER_PATH
        rev_worker["artifact_exports"] = ["/home/other-user/review.json"]
        self.assertIn("CROSS_USER_PATH", self.codes(self.run_audit()))

        # Write paths outside worktree still raises WORKTREE_SCOPE_ESCAPE
        rev_worker["artifact_exports"] = ["/tmp/review.json"]
        rev_worker["write_paths"] = ["/tmp/outside-worktree"]
        self.assertIn("WORKTREE_SCOPE_ESCAPE", self.codes(self.run_audit()))

        # Malformed artifact_exports raises WORKER_INVALID
        rev_worker["write_paths"] = ["/tmp/work/review"]
        rev_worker["artifact_exports"] = "not-a-list"
        self.assertIn("WORKER_INVALID", self.codes(self.run_audit()))
        rev_worker["artifact_exports"] = [""]
        self.assertIn("WORKER_INVALID", self.codes(self.run_audit()))

    def test_duplicate_admission_key_rejected_across_active_projects(self):
        p1 = self.doc["delivery_control"]["projects"]["foundation"]
        p2 = self.doc["delivery_control"]["projects"]["product"]
        p1["work_item"] = {"admission_key": "task-shared-42", "issue_or_task": "task-1"}
        p2["work_item"] = {"admission_key": "task-shared-42", "issue_or_task": "task-2"}
        result = self.run_audit()
        self.assertIn("DUPLICATE_ADMISSION", self.codes(result))

        # If one project is done, duplicate admission does not conflict
        p1["stage"] = "done"
        result2 = self.run_audit()
        self.assertNotIn("DUPLICATE_ADMISSION", self.codes(result2))

    def test_stale_or_missing_claim_token_on_active_rows(self):
        p = self.doc["delivery_control"]["projects"]["foundation"]
        p["stage"] = "implementing"

        # Missing claim_token
        p["work_item"] = {"issue_or_task": "task-1", "admission_key": "key-1"}
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(self.run_audit()))

        # Empty claim_token
        p["work_item"]["claim_token"] = ""
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(self.run_audit()))

        # Expired claim_token via expires_at
        p["work_item"]["claim_token"] = "valid-tok"
        p["work_item"]["claim_expires_at"] = "2026-11-03T00:59:00Z"
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(self.run_audit()))

        # Stale claim via status or flag
        p["work_item"]["claim_expires_at"] = "2026-11-03T02:00:00Z"
        p["work_item"]["stale"] = True
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(self.run_audit()))

        # Stale claim via old claim_observed_at
        p["work_item"]["stale"] = False
        p["work_item"]["claim_observed_at"] = "2026-10-01T00:00:00Z"
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(self.run_audit()))

        # Valid claim token with future expiry passes
        p["work_item"]["claim_observed_at"] = NOW
        self.assertNotIn("STALE_OR_MISSING_CLAIM", self.codes(self.run_audit()))

    def test_changed_revision_after_review_and_review_guards(self):
        p = self.doc["delivery_control"]["projects"]["foundation"]
        p["revision"] = HEAD
        p["work_item"] = {
            "requested_transition": "review",
            "revision": HEAD,
            "claim_token": "tok-1",
        }
        # Reviews on an older revision
        p["reviews"] = [
            {"role": "reviewer-a", "reviewer": "m1", "revision": "b" * 40, "verdict": "SHIP", "evidence": ["r1.json"]},
            {"role": "reviewer-b", "reviewer": "m2", "revision": "b" * 40, "verdict": "SHIP", "evidence": ["r2.json"]},
            {"role": "reviewer-c", "reviewer": "m3", "revision": "b" * 40, "verdict": "SHIP", "evidence": ["r3.json"]},
        ]
        result = self.run_audit()
        self.assertIn("REVISION_CHANGED_AFTER_REVIEW", self.codes(result))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(result))

        # Reviews with < 3 distinct reviewer models
        self.policy["reviewer_models"] = ["model-1", "model-2", "model-3"]
        p["reviews"] = [
            {
                "role": "reviewer-a",
                "reviewer": "same",
                "model": "model-1",
                "revision": HEAD,
                "verdict": "SHIP",
                "evidence": ["r1.json"],
            },
            {
                "role": "reviewer-b",
                "reviewer": "same",
                "model": "model-1",
                "revision": HEAD,
                "verdict": "SHIP",
                "evidence": ["r2.json"],
            },
            {
                "role": "reviewer-c",
                "reviewer": "diff",
                "model": "model-2",
                "revision": HEAD,
                "verdict": "SHIP",
                "evidence": ["r3.json"],
            },
        ]
        res_guards = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_guards))

        # 3 distinct models on exact revision passes review transition guard
        p["reviews"] = [
            {
                "role": "reviewer-a",
                "reviewer": "rev-1",
                "model": "model-1",
                "revision": HEAD,
                "verdict": "SHIP",
                "evidence": ["r1.json"],
            },
            {
                "role": "reviewer-b",
                "reviewer": "rev-2",
                "model": "model-2",
                "revision": HEAD,
                "verdict": "SHIP",
                "evidence": ["r2.json"],
            },
            {
                "role": "reviewer-c",
                "reviewer": "rev-3",
                "model": "model-3",
                "revision": HEAD,
                "verdict": "SHIP",
                "evidence": ["r3.json"],
            },
        ]
        self.assertNotIn("TRANSITION_GUARD_FAILED", self.codes(self.run_audit()))

    def test_stage_transition_guards_admission_implementation_and_merge(self):
        p = self.doc["delivery_control"]["projects"]["foundation"]

        # Admission guard: missing issue_or_task or admission_key
        p["work_item"] = {"requested_transition": "admission"}
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(self.run_audit()))
        p["work_item"] = {"requested_transition": "admission", "issue_or_task": "task-1", "admission_key": "k-1"}
        self.assertNotIn("TRANSITION_GUARD_FAILED", self.codes(self.run_audit()))

        # Implementation guard: missing worker or claim token
        p["work_item"] = {"requested_transition": "implementation"}
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(self.run_audit()))
        p["workers"] = [worker()]
        p["work_item"] = {"requested_transition": "implementation", "claim_token": "tok-1"}
        self.assertNotIn("TRANSITION_GUARD_FAILED", self.codes(self.run_audit()))

        # Merge transition: forged/insufficient completion evidence & missing mandatory gates
        ready = ready_row()
        ready["work_item"] = {"requested_transition": "merge", "claim_token": "tok-1"}
        ready["evidence"] = ["TODO: add evidence"]
        self.doc["delivery_control"]["projects"]["foundation"] = ready
        res_merge = self.run_audit()
        self.assertIn("INSUFFICIENT_COMPLETION_EVIDENCE", self.codes(res_merge))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_merge))

        # Missing mandatory gate (ci missing)
        ready["evidence"] = ["verified-receipt.json"]
        ready["ci"] = {}
        res_gate = self.run_audit()
        self.assertIn("MISSING_MANDATORY_GATE", self.codes(res_gate))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_gate))

        # Unenforced required control in policy
        ready["ci"] = {"revision": HEAD, "status": "success", "evidence": ["run/123"]}
        self.policy["required_controls"] = ["precommit"]
        ready["controls"] = {"precommit": {"status": "implementing", "owner": "dev", "next_action": "fix"}}
        res_ctrl = self.run_audit()
        self.assertIn("MISSING_MANDATORY_GATE", self.codes(res_ctrl))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_ctrl))

        # Full passing merge transition
        ready["controls"] = {
            "precommit": {
                "status": "enforced",
                "owner": "dev",
                "next_action": "maintain",
                "revision": HEAD,
                "evidence": ["lint.txt"],
            }
        }
        res_ok = self.run_audit()
        self.assertNotIn("MISSING_MANDATORY_GATE", self.codes(res_ok))
        self.assertNotIn("INSUFFICIENT_COMPLETION_EVIDENCE", self.codes(res_ok))
        self.assertNotIn("TRANSITION_GUARD_FAILED", self.codes(res_ok))

        # not_before in the future blocks transition
        ready["work_item"]["not_before"] = "2026-11-03T02:00:00Z"
        res_nb = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_nb))

    def test_compact_projection_structure_and_byte_reduction(self):
        import io
        from unittest.mock import patch

        # Set up a realistic portfolio with multiple gaps and blockers
        self.policy["required_controls"] = ["c1", "c2", "c3", "c4", "c5"]
        for p in self.doc["delivery_control"]["projects"].values():
            p["controls"] = {
                f"c{i}": {
                    "status": "implementing",
                    "owner": "dev",
                    "next_action": "work",
                    "observed_at": NOW,
                    "evidence": ["e.txt"],
                }
                for i in range(1, 6)
            }
        self.doc["delivery_control"]["projects"]["product"].update(
            stage="blocked",
            blocker={"owner": "Patrick", "reason": "Wait", "evidence": ["b.txt"], "recheck_at": "2026-11-03T02:00:00Z"},
        )
        self.doc["delivery_control"]["pending_inputs"] = [{"id": "disp-1", "status": "queued", "evidence": ["tr.json"]}]
        result = self.run_audit()
        self.assertIn("compact_projection", result)
        compact = result["compact_projection"]
        self.assertIn("changed_or_ready_projects", compact)
        self.assertIn("time_critical_blockers", compact)
        self.assertIn("pending_intake", compact)
        self.assertIn("aggregated_gaps", compact)

        # Verify ready projects and time critical blockers
        self.assertEqual(len(compact["changed_or_ready_projects"]), 1)
        self.assertEqual(compact["changed_or_ready_projects"][0]["project"], "foundation")
        self.assertEqual(len(compact["time_critical_blockers"]), 1)
        self.assertEqual(compact["time_critical_blockers"][0]["project"], "product")
        self.assertTrue(compact["pending_intake"]["defer_input"])

        # Aggregated gaps grouped by (project, status) with drilldown pointers
        self.assertTrue(len(compact["aggregated_gaps"]) >= 1)
        first_gap = compact["aggregated_gaps"][0]
        self.assertIn("count", first_gap)
        self.assertIn("drilldown_pointers", first_gap)
        self.assertTrue(first_gap["count"] > 1)
        self.assertTrue(first_gap["drilldown_pointers"][0].startswith("/delivery_control/projects/"))

        # Verify significant byte and line count reduction vs full output
        full_json = json.dumps(result, indent=2, sort_keys=True)
        compact_json = json.dumps(compact, indent=2, sort_keys=True)
        self.assertLess(len(compact_json.encode("utf-8")), len(full_json.encode("utf-8")) * 0.7)
        self.assertLess(len(compact_json.splitlines()), len(full_json.splitlines()) * 0.7)

        # Verify CLI --compact works in json and markdown
        out_json = io.StringIO()
        with patch("sys.stdout", out_json):
            code = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--compact",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 1)
        parsed = json.loads(out_json.getvalue())
        self.assertIn("aggregated_gaps", parsed)
        self.assertNotIn("diagnostics", parsed)

        out_md = io.StringIO()
        with patch("sys.stdout", out_md):
            code = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--compact",
                    "--format",
                    "markdown",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("Compact projection", out_md.getvalue())

    def test_stageguards_coverage_edge_cases(self):
        p = self.doc["delivery_control"]["projects"]["foundation"]

        # 1. work_item is not a dict
        p["work_item"] = "not-a-dict"
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        # 2. Invalid claim_expires_at timestamp
        p["stage"] = "implementing"
        p["work_item"] = {"claim_token": "tok-1", "claim_expires_at": "bad-date"}
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        # 3. Invalid not_before timestamp
        p["work_item"] = {"requested_transition": "admission", "not_before": "bad-date"}
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        # 4. Unknown requested_transition
        p["work_item"] = {"requested_transition": "unknown-stage"}
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        # 5. Policy invalid reviewer_models / banned_models
        del p["work_item"]
        self.policy["reviewer_models"] = "not-a-list"
        self.assertIn("POLICY_INVALID", self.codes(self.run_audit()))
        self.policy["reviewer_models"] = ["good", 123]
        self.assertIn("POLICY_INVALID", self.codes(self.run_audit()))
        self.policy["reviewer_models"] = None
        self.policy["banned_models"] = "not-a-list"
        self.assertIn("POLICY_INVALID", self.codes(self.run_audit()))
        self.policy["banned_models"] = []

        # 6. Blocker on non-blocked stage
        p["stage"] = "ready"
        p["blocker"] = {"owner": "dev", "reason": "non-blocking advisory", "evidence": ["info.txt"]}
        self.assertEqual(self.run_audit()["exit_code"], 0)

        # 7. Merge transition guard missing branch protection and approval
        ready = ready_row()
        ready["work_item"] = {"requested_transition": "merge", "claim_token": "tok-1"}
        ready["branch_protection"] = {"verified": False, "evidence": []}
        ready["approval"] = {}
        self.doc["delivery_control"]["projects"]["foundation"] = ready
        self.assertIn("MISSING_MANDATORY_GATE", self.codes(self.run_audit()))

        # 8. Review transition without any reviews
        ready["work_item"]["requested_transition"] = "review"
        ready["reviews"] = []
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(self.run_audit()))

    def test_json_pointer_escaping_and_round_trip(self):
        # Round trip test with project IDs containing / and ~
        # RFC 6901: ~ -> ~0, / -> ~1
        test_cases = [
            ("owner/repo", {"status": "ok", "repo": "main"}, "/initiatives/owner~1repo"),
            ("a~1b", {"status": "ok", "tag": "test"}, "/initiatives/a~01b"),
            ("deep/~complex/proj", {"status": "active"}, "/initiatives/deep~1~0complex~1proj"),
        ]
        self.doc["delivery_control"]["projects"] = {}
        self.doc["initiatives"] = {pid: data for pid, data, _ in test_cases}
        self.policy["expected_projects"] = [pid for pid, _, _ in test_cases]
        result = self.run_audit()
        proj_map = {p["project"]: p for p in result["projects"]}

        for pid, data, expected_pointer in test_cases:
            p = proj_map[pid]
            self.assertEqual(p["coverage"], "legacy_unverified")
            self.assertEqual(p["source_pointer"], expected_pointer)

            # RFC 6901 decode the pointer to retrieve exact source object from doc
            tokens = expected_pointer.split("/")[1:]
            decoded_tokens = [tok.replace("~1", "/").replace("~0", "~") for tok in tokens]
            self.assertEqual(decoded_tokens[0], "initiatives")
            self.assertEqual(decoded_tokens[1], pid)

            source_obj = self.doc["initiatives"][decoded_tokens[1]]
            self.assertEqual(source_obj, data)
            self.assertEqual(p["source_sha256"], MODULE.canonical_sha256(source_obj))

    def test_legacy_exact_known_sha256_digest(self):
        # Exact known sha256 to kill unsorted JSON and pointer-digest mutants
        # canonical JSON of {"b": 2, "a": 1} is b'{"a":1,"b":2}'
        entry = {"b": 2, "a": 1}
        expected_sha = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
        self.assertEqual(expected_sha, "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777")

        self.doc["delivery_control"]["projects"] = {}
        self.doc["initiatives"] = {"test_proj": entry}
        self.policy["expected_projects"] = ["test_proj"]
        result = self.run_audit()
        p = result["projects"][0]
        self.assertEqual(p["source_sha256"], "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777")
        # Ensure it didn't hash the pointer
        pointer_sha = hashlib.sha256(b"/initiatives/test_proj").hexdigest()
        self.assertNotEqual(p["source_sha256"], pointer_sha)
        # Ensure it didn't hash unsorted json
        unsorted_sha = hashlib.sha256(b'{"b":2,"a":1}').hexdigest()
        self.assertNotEqual(p["source_sha256"], unsorted_sha)

    def test_legacy_null_and_non_dict_entries(self):
        # Legacy entry with value None or non-dict must be legacy_unverified (not absent/PROJECT_MISSING)
        self.doc["delivery_control"]["projects"] = {}
        self.doc["initiatives"] = {
            "null_entry": None,
            "string_entry": "just-a-string",
            "int_entry": 42,
        }
        self.policy["expected_projects"] = ["null_entry", "string_entry", "int_entry"]
        result = self.run_audit()
        self.assertEqual(result["summary"]["legacy_unverified"], 3)
        self.assertEqual(result["summary"]["absent"], 0)
        self.assertNotIn("PROJECT_MISSING", self.codes(result))

        proj_map = {p["project"]: p for p in result["projects"]}
        self.assertEqual(proj_map["null_entry"]["coverage"], "legacy_unverified")
        self.assertEqual(proj_map["null_entry"]["source_pointer"], "/initiatives/null_entry")
        self.assertEqual(proj_map["null_entry"]["source_sha256"], hashlib.sha256(b"null").hexdigest())
        self.assertIsNone(proj_map["null_entry"]["observed_at"])
        self.assertIsNone(proj_map["null_entry"]["age_seconds"])

        self.assertEqual(proj_map["string_entry"]["coverage"], "legacy_unverified")
        self.assertEqual(proj_map["string_entry"]["source_pointer"], "/initiatives/string_entry")
        self.assertEqual(proj_map["string_entry"]["source_sha256"], hashlib.sha256(b'"just-a-string"').hexdigest())

    def test_legacy_observed_at_validation_and_age_seconds(self):
        # Valid timestamp -> computes age_seconds; invalid -> observed_at_valid: False, age_seconds: None
        now_time = "2026-11-03T01:00:00Z"
        self.doc["delivery_control"]["projects"] = {}
        self.doc["initiatives"] = {
            "valid_date": {"status": "ok", "observed_at": "2026-11-03T00:00:00Z"},
            "bad_string_date": {"status": "ok", "observed_at": "not-a-date"},
            "non_string_date": {"status": "ok", "observed_at": 123456789},
            "no_date": {"status": "ok"},
        }
        self.policy["expected_projects"] = ["valid_date", "bad_string_date", "non_string_date", "no_date"]
        self.write("coordinator-status.json", self.doc)
        self.write("policy.json", self.policy)
        self.write("local-actions.json", self.actions)
        result = MODULE.audit(self.root, self.root / "policy.json", now_time)
        proj_map = {p["project"]: p for p in result["projects"]}

        # 1 hour ago = 3600 seconds
        self.assertEqual(proj_map["valid_date"]["observed_at"], "2026-11-03T00:00:00Z")
        self.assertEqual(proj_map["valid_date"]["age_seconds"], 3600)
        self.assertNotIn("observed_at_valid", proj_map["valid_date"])

        # bad string date
        self.assertEqual(proj_map["bad_string_date"]["observed_at"], "not-a-date")
        self.assertIsNone(proj_map["bad_string_date"]["age_seconds"])
        self.assertFalse(proj_map["bad_string_date"]["observed_at_valid"])

        # non-string date
        self.assertEqual(proj_map["non_string_date"]["observed_at"], 123456789)
        self.assertIsNone(proj_map["non_string_date"]["age_seconds"])
        self.assertFalse(proj_map["non_string_date"]["observed_at_valid"])

        # no date
        self.assertIsNone(proj_map["no_date"]["observed_at"])
        self.assertIsNone(proj_map["no_date"]["age_seconds"])
        self.assertNotIn("observed_at_valid", proj_map["no_date"])

    def test_blocker_on_non_blocked_and_done_rows(self):
        # PDC-01: blocker null or {} on non-blocked row must return 0 blocker diagnostics
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "implementing"
        f["blocker"] = None
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("BLOCKER_INVALID", self.codes(result))

        f["blocker"] = {}
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("BLOCKER_INVALID", self.codes(result))

        # PDC-01: done row with historical blocker must return 0 blocker diagnostics
        f["stage"] = "done"
        f.pop("work_item", None)
        f["blocker"] = {
            "owner": "old-owner",
            "reason": "old-reason",
            "evidence": ["old.txt"],
            "since": "2026-01-01T00:00:00Z",
        }
        result = self.run_audit()
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("BLOCKER_STALE", self.codes(result))
        self.assertNotIn("BLOCKER_INVALID", self.codes(result))

    def test_blocker_timestamp_aliases_on_stale_row(self):
        # PDC-02: Test each blocker timestamp alias (since, blocked_at, observed_at, last_progress_at fallback)
        # on a row whose last_progress_at is stale
        now_time = "2026-11-03T01:00:00Z"
        stale_time = "2026-11-02T12:00:00Z"  # 13 hours ago (stale)
        fresh_time = "2026-11-03T00:50:00Z"  # 10 minutes ago (fresh)
        self.write("policy.json", self.policy)
        self.write("local-actions.json", self.actions)
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "blocked"
        f["last_progress_at"] = stale_time

        # 1. 'since' alias fresh
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "since": fresh_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertNotIn("BLOCKER_STALE", self.codes(res))

        # 2. 'blocked_at' alias fresh
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "blocked_at": fresh_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertNotIn("BLOCKER_STALE", self.codes(res))

        # 3. 'observed_at' alias fresh
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "observed_at": fresh_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertNotIn("BLOCKER_STALE", self.codes(res))

        # 4. last_progress_at fallback when blocker has no timestamp
        f["last_progress_at"] = fresh_time
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"]}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertNotIn("BLOCKER_STALE", self.codes(res))

        # 5. last_progress_at fallback when stale -> emits BLOCKER_STALE
        f["last_progress_at"] = stale_time
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"]}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_STALE", self.codes(res))

        # 6. 'since' alias stale -> emits BLOCKER_STALE
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "since": stale_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_STALE", self.codes(res))

        # 7. 'blocked_at' alias stale -> emits BLOCKER_STALE
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "blocked_at": stale_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_STALE", self.codes(res))

        # 8. 'observed_at' alias stale -> emits BLOCKER_STALE
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "observed_at": stale_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_STALE", self.codes(res))

    def test_blocker_recheck_aliases_and_null_skipping(self):
        # PDC-02 & PDC-07: Test each recheck alias (recheck_at, recheck_by, until) on a stale row
        # and recheck_at: null alongside valid until
        now_time = "2026-11-03T01:00:00Z"
        stale_time = "2026-11-02T12:00:00Z"
        future_time = "2026-11-03T03:00:00Z"  # 2h in future (within 168h horizon)
        past_time = "2026-11-03T00:50:00Z"  # 10m in past
        self.write("policy.json", self.policy)
        self.write("local-actions.json", self.actions)

        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "blocked"
        f["last_progress_at"] = stale_time

        # 1. recheck_at future -> 0 diagnostics
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "recheck_at": future_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["diagnostics"], [])

        # 2. recheck_by future -> 0 diagnostics
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "recheck_by": future_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["diagnostics"], [])

        # 3. until future -> 0 diagnostics
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "until": future_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["diagnostics"], [])

        # 4. recheck_at: null alongside valid until -> must skip null and use until
        f["blocker"] = {
            "owner": "dev",
            "reason": "r",
            "evidence": ["e.txt"],
            "recheck_at": None,
            "until": future_time,
        }
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["diagnostics"], [])

        # 5. recheck_by expired -> BLOCKER_RECHECK_DUE
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "recheck_by": past_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 1)
        self.assertIn("BLOCKER_RECHECK_DUE", self.codes(res))

        # 6. until expired -> BLOCKER_RECHECK_DUE
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"], "until": past_time}
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 1)
        self.assertIn("BLOCKER_RECHECK_DUE", self.codes(res))

    def test_blocker_and_recheck_exact_boundary_instants(self):
        # PDC-02: exact boundary instants
        # age == max_age (60m) vs age > max_age
        # recheck == now vs recheck < now vs recheck > now
        now_time = "2026-11-03T01:00:00Z"
        self.policy["max_progress_age_minutes"] = 60
        self.write("local-actions.json", self.actions)
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "blocked"

        # Boundary 1: age == max_age exactly (3600 seconds ago) -> not stale (0 diagnostics)
        f["last_progress_at"] = "2026-11-03T00:00:00Z"
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"]}
        self.write("coordinator-status.json", self.doc)
        self.write("policy.json", self.policy)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertNotIn("BLOCKER_STALE", self.codes(res))

        # Boundary 2: age == max_age + 1 second (3601 seconds ago) -> stale
        f["last_progress_at"] = "2026-11-02T23:59:59Z"
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_STALE", self.codes(res))

        # Boundary 3: recheck == now exactly -> BLOCKER_RECHECK_DUE
        f["blocker"]["recheck_at"] = "2026-11-03T01:00:00Z"
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_RECHECK_DUE", self.codes(res))

        # Boundary 4: recheck == now - 1 second -> BLOCKER_RECHECK_DUE
        f["blocker"]["recheck_at"] = "2026-11-03T00:59:59Z"
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_RECHECK_DUE", self.codes(res))

        # Boundary 5: recheck == now + 1 second -> active future (0 diagnostics)
        f["blocker"]["recheck_at"] = "2026-11-03T01:00:01Z"
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["diagnostics"], [])

    def test_recheck_horizon_and_policy_configuration(self):
        # PDC-04: max_recheck_horizon_hours (default 168 = 7 days)
        # A stale blocker with recheck_at > horizon emits BLOCKER_RECHECK_HORIZON_EXCEEDED
        now_time = "2026-11-03T01:00:00Z"
        stale_time = "2026-10-01T00:00:00Z"  # Month old blocker
        self.write("local-actions.json", self.actions)
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "blocked"
        f["last_progress_at"] = stale_time

        # Far-future date 9999-12-31 on month-old blocker
        f["blocker"] = {
            "owner": "dev",
            "reason": "r",
            "evidence": ["e.txt"],
            "recheck_at": "9999-12-31T00:00:00Z",
        }
        self.write("coordinator-status.json", self.doc)
        self.write("policy.json", self.policy)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertEqual(res["exit_code"], 1)
        self.assertIn("BLOCKER_RECHECK_HORIZON_EXCEEDED", self.codes(res))

        # Exact horizon boundary: 168 hours = 7 days = 2026-11-10T01:00:00Z
        # Exactly 168h future: NOT exceeded
        f["blocker"]["recheck_at"] = "2026-11-10T01:00:00Z"
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertNotIn("BLOCKER_RECHECK_HORIZON_EXCEEDED", self.codes(res))
        self.assertEqual(res["exit_code"], 0)

        # 168h + 1s future: exceeded
        f["blocker"]["recheck_at"] = "2026-11-10T01:00:01Z"
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_RECHECK_HORIZON_EXCEEDED", self.codes(res))

        # Configurable horizon in policy: 48 hours
        self.policy["max_recheck_horizon_hours"] = 48
        self.write("policy.json", self.policy)
        # 50 hours in future (2026-11-05T03:00:00Z) -> exceeded
        f["blocker"]["recheck_at"] = "2026-11-05T03:00:00Z"
        self.write("coordinator-status.json", self.doc)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("BLOCKER_RECHECK_HORIZON_EXCEEDED", self.codes(res))

        # Invalid policy max_recheck_horizon_hours -> POLICY_INVALID and fallback to 168
        self.policy["max_recheck_horizon_hours"] = -5
        self.write("policy.json", self.policy)
        res = MODULE.audit(self.root, self.root / "policy.json", now_time)
        self.assertIn("POLICY_INVALID", self.codes(res))

    def test_single_parse_of_last_progress_at_on_blocked_row(self):
        # PDC-07: Invalid last_progress_at on a blocked row must emit TIMESTAMP_INVALID only once
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "blocked"
        f["last_progress_at"] = "not-a-date"
        f["blocker"] = {"owner": "dev", "reason": "r", "evidence": ["e.txt"]}
        res = self.run_audit()
        timestamp_invalids = [d for d in res["diagnostics"] if d["code"] == "TIMESTAMP_INVALID"]
        self.assertEqual(len(timestamp_invalids), 1)

    def test_cli_markdown_summary_counts(self):
        # AS-PREFLIGHT-01 & PDC-06: Include summary counts in markdown output
        import io
        from unittest.mock import patch

        self.run_audit()
        out_md = io.StringIO()
        with patch("sys.stdout", out_md):
            code = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--format",
                    "markdown",
                ]
            )
        self.assertEqual(code, 0)
        output = out_md.getvalue()
        self.assertIn("Structural preflight:", output)
        self.assertIn("Summary: normalized=2, legacy_unverified=0, absent=0", output)
        self.assertIn("schema_error=0, attention=0", output)

    def test_r2_pr2_05_and_as_pr152_r2_003_rfc6901_escaping_round_trip(self):
        # RFC 6901 pointer escaping round-trip test for owner/repo project IDs and control IDs with ~ and /
        proj = "selamy-labs/agent-skills"
        cid = "spec~1/core"
        escaped_proj = MODULE.escape_json_pointer(proj)
        escaped_cid = MODULE.escape_json_pointer(cid)
        pointer = f"/delivery_control/projects/{escaped_proj}/controls/{escaped_cid}"
        self.assertEqual(escaped_proj, "selamy-labs~1agent-skills")
        self.assertEqual(escaped_cid, "spec~01~1core")
        self.assertEqual(pointer, "/delivery_control/projects/selamy-labs~1agent-skills/controls/spec~01~1core")

        # Round-trip resolution
        parts = pointer.split("/")[1:]
        decoded = [p.replace("~1", "/").replace("~0", "~") for p in parts]
        self.assertEqual(decoded[0], "delivery_control")
        self.assertEqual(decoded[1], "projects")
        self.assertEqual(decoded[2], proj)
        self.assertEqual(decoded[3], "controls")
        self.assertEqual(decoded[4], cid)

    def test_r2_as_pr152_r2_002_and_pr2_02_revision_requirements(self):
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "review"
        f["revision"] = None
        f["work_item"] = {"requested_transition": "review", "revision": None, "claim_token": "tok-1"}
        # None == None must fail: requires valid 40-hex SHA
        res = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res))

        # Disagreement between work_item.revision and row.revision
        f["revision"] = HEAD
        f["work_item"]["revision"] = "b" * 40
        res_disagree = self.run_audit()
        self.assertIn("REVISION_CHANGED_AFTER_REVIEW", self.codes(res_disagree))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_disagree))

        # Non-40-hex revision on review transition
        f["revision"] = "short-rev"
        f["work_item"]["revision"] = "short-rev"
        res_short = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_short))

        # check_mandatory_gates fails closed if controls is None or non-dict
        self.policy["required_controls"] = ["ctrl-1", "ctrl-2"]
        f["stage"] = "merge_ready"
        f["revision"] = HEAD
        f["controls"] = None
        missing = MODULE.check_mandatory_gates(f, self.policy, HEAD)
        self.assertIn("control:ctrl-1", missing)
        self.assertIn("control:ctrl-2", missing)

        # Enforced control requires matching revision and evidence
        f["controls"] = {
            "ctrl-1": {"status": "enforced", "revision": "wrong-sha", "evidence": ["ev.txt"]},
            "ctrl-2": {"status": "not_applicable", "reason": "", "evidence": []},
        }
        missing_bad = MODULE.check_mandatory_gates(f, self.policy, HEAD)
        self.assertIn("control:ctrl-1", missing_bad)
        self.assertIn("control:ctrl-2", missing_bad)

        # Valid control passes check_mandatory_gates
        f["controls"] = {
            "ctrl-1": {"status": "enforced", "revision": HEAD, "evidence": ["ev.txt"]},
            "ctrl-2": {"status": "not_applicable", "reason": "Not needed", "evidence": ["ev.txt"]},
        }
        missing_good = MODULE.check_mandatory_gates(f, self.policy, HEAD)
        self.assertNotIn("control:ctrl-1", missing_good)
        self.assertNotIn("control:ctrl-2", missing_good)

    def test_r2_pr2_02_stage_transition_rejected_on_invalid_and_falsy(self):
        f = self.doc["delivery_control"]["projects"]["foundation"]
        for bad_val in ("", 0, [], {}, "unknown-stage", "bogus"):
            f["work_item"] = {"requested_transition": bad_val}
            res = self.run_audit()
            self.assertIn("STAGE_TRANSITION_REJECTED", self.codes(res))

        # requested_transition on row itself
        f["work_item"] = {}
        f["requested_transition"] = ""
        self.assertIn("STAGE_TRANSITION_REJECTED", self.codes(self.run_audit()))

        # Conflicting requested_transition between row and work_item
        f["requested_transition"] = "implementation"
        f["work_item"] = {"requested_transition": "review"}
        self.assertIn("STAGE_TRANSITION_REJECTED", self.codes(self.run_audit()))

    def test_r2_pr2_02_auto_enforce_stage_guards(self):
        f = self.doc["delivery_control"]["projects"]["foundation"]
        # Stage review auto-enforces guard_review even without requested_transition
        f["stage"] = "review"
        f["revision"] = HEAD
        f["reviews"] = []
        f.pop("work_item", None)
        res_rev = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_rev))

        # Stage merge_ready auto-enforces guard_merge_or_complete
        f["stage"] = "merge_ready"
        f["reviews"] = []
        res_merge = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_merge))

        # Stage done with work_item auto-enforces guard_merge_or_complete
        f["stage"] = "done"
        f["work_item"] = {"issue_or_task": "task-done"}
        f["reviews"] = []
        res_done = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_done))

    def test_r2_pr2_03_malformed_evidence_and_null_workers_do_not_crash(self):
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "merge_ready"
        f["revision"] = HEAD
        f["work_item"] = {"requested_transition": "merge", "claim_token": "tok-1"}
        # Number in row evidence must not raise AttributeError
        f["evidence"] = [123]
        res1 = self.run_audit()
        self.assertIn("INSUFFICIENT_COMPLETION_EVIDENCE", self.codes(res1))

        # Object in work_item evidence must not raise AttributeError
        f["evidence"] = []
        f["work_item"]["evidence"] = [{"nested": "bad"}]
        res2 = self.run_audit()
        self.assertIn("INSUFFICIENT_COMPLETION_EVIDENCE", self.codes(res2))

        # workers: null on implementation transition must not raise TypeError
        f["stage"] = "implementing"
        f["workers"] = None
        f["work_item"] = {"requested_transition": "implementation", "claim_token": "tok-1"}
        res3 = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res3))

    def test_r2_pr2_04_compact_projection_bounded_and_self_sufficient(self):
        # 900 KB blocker reason must be truncated to <= 240 chars with ...
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["stage"] = "blocked"
        f["blocker"] = {
            "owner": "Patrick",
            "reason": "BigReason" * 100000,
            "evidence": ["e.txt"],
            "recheck_at": "2026-11-03T02:00:00Z",
        }
        f["work_item"] = {"claim_token": "secret-claim-token-123", "issue_or_task": "t-1"}
        # Add absent project
        self.policy["expected_projects"] = ["foundation", "absent_proj"]
        res = self.run_audit()
        compact = res["compact_projection"]

        # Check self-sufficient top-level fields
        self.assertIn("status", compact)
        self.assertIn("exit_code", compact)
        self.assertIn("summary", compact)
        self.assertIn("authorizes_actions", compact)
        self.assertFalse(compact["authorizes_actions"])
        self.assertIn("transition_failures", compact)

        # Check bounded string length
        found_blocker = compact["time_critical_blockers"][0]
        self.assertTrue(found_blocker["reason"].endswith("..."))
        self.assertLessEqual(len(found_blocker["reason"]), 240)

        # Check claim_token is omitted/redacted from projects and compact
        full_json = json.dumps(res)
        compact_json = json.dumps(compact)
        self.assertNotIn("secret-claim-token-123", compact_json)
        self.assertNotIn("secret-claim-token-123", full_json)

        # Absent project appears in aggregated_gaps
        gaps = compact["aggregated_gaps"]
        absent_gaps = [g for g in gaps if g["project"] == "absent_proj"]
        self.assertEqual(len(absent_gaps), 1)
        self.assertEqual(absent_gaps[0]["status"], "absent")

    def test_r2_pr2_06_review_worker_write_paths_and_models(self):
        rev_worker = {
            "id": "rev-1",
            "model": "approved-reviewer-model",
            "role": "review",
            "user": "worker-owner",
            "worktree": "/tmp/work",
            "write_paths": ["/tmp/work/src"],
            "artifact_exports": ["/tmp/review.json"],
            "sandbox": True,
            "permission_bypass": False,
        }
        self.policy["reviewer_models"] = ["approved-reviewer-model"]
        self.doc["delivery_control"]["projects"]["foundation"]["workers"] = [rev_worker]
        # Non-empty write_paths on review worker must raise WORKER_INVALID
        res = self.run_audit()
        self.assertIn("WORKER_INVALID", self.codes(res))

        # Review worker with empty policy reviewer_models must raise REVIEWER_MODEL_INVALID
        rev_worker["write_paths"] = []
        self.policy["reviewer_models"] = []
        res2 = self.run_audit()
        self.assertIn("REVIEWER_MODEL_INVALID", self.codes(res2))

    def test_r2_pr2_07_eight_surviving_guard_mutants(self):
        ready = ready_row()
        self.policy["reviewer_models"] = ["reviewer-a-model", "reviewer-b-model", "reviewer-c-model"]
        self.policy["banned_models"] = ["banned-reviewer-model"]
        for r in ready["reviews"]:
            r["model"] = r["role"] + "-model"
        ready["work_item"] = {"requested_transition": "merge", "claim_token": "tok-1"}
        self.doc["delivery_control"]["projects"]["foundation"] = ready

        # 1. Unanimity mutant: ignoring verdict (one review has REVISE)
        ready["reviews"][0]["verdict"] = "REVISE"
        res1 = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res1))
        ready["reviews"][0]["verdict"] = "SHIP"

        # 2. Revision mutant: review revision mismatch
        ready["reviews"][0]["revision"] = "c" * 40
        res2 = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res2))
        ready["reviews"][0]["revision"] = HEAD

        # 3. CI revision not compared
        ready["ci"]["revision"] = "d" * 40
        res3 = self.run_audit()
        self.assertIn("MISSING_MANDATORY_GATE", self.codes(res3))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res3))
        ready["ci"]["revision"] = HEAD

        # 4. Approval gate removed / empty authority
        ready["approval"]["authority"] = ""
        res4 = self.run_audit()
        self.assertIn("MISSING_MANDATORY_GATE", self.codes(res4))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res4))
        ready["approval"]["authority"] = "primary-approval.md"

        # 5. Protection gate removed / verified False
        ready["branch_protection"]["verified"] = False
        res5 = self.run_audit()
        self.assertIn("MISSING_MANDATORY_GATE", self.codes(res5))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res5))
        ready["branch_protection"]["verified"] = True

        # 6. Implementer model not required in guard_implementation
        f = self.doc["delivery_control"]["projects"]["product"]
        f["workers"] = [
            {
                "id": "w1",
                "role": "implementation",
                "model": "wrong-model",
                "user": "u",
                "worktree": "/tmp/work",
                "write_paths": ["src"],
                "sandbox": True,
                "permission_bypass": False,
            }
        ]
        f["work_item"] = {"requested_transition": "implementation", "claim_token": "tok-1"}
        res6 = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res6))

        # 7. Banned models ignored for reviewers
        rev_banned = {
            "id": "rev-b",
            "model": "banned-reviewer-model",
            "role": "review",
            "user": "worker-owner",
            "worktree": "/tmp/work",
            "write_paths": [],
            "artifact_exports": ["/tmp/review.json"],
            "sandbox": True,
            "permission_bypass": False,
        }
        f["workers"] = [rev_banned]
        res7 = self.run_audit()
        self.assertIn("REVIEWER_MODEL_INVALID", self.codes(res7))

        # 8. Gap status not grouped: same project with 'implementing' and 'gap' controls
        f_row = self.doc["delivery_control"]["projects"]["foundation"]
        self.policy["required_controls"] = ["ctrl-impl", "ctrl-gap"]
        f_row["controls"] = {
            "ctrl-impl": {
                "status": "implementing",
                "owner": "dev",
                "next_action": "w",
                "observed_at": NOW,
                "evidence": ["e.txt"],
            },
            "ctrl-gap": {
                "status": "gap",
                "owner": "dev",
                "next_action": "w",
                "observed_at": NOW,
                "evidence": ["e.txt"],
            },
        }
        res8 = self.run_audit()
        gaps = res8["compact_projection"]["aggregated_gaps"]
        statuses = {g["status"] for g in gaps if g["project"] == "foundation"}
        self.assertIn("implementing", statuses)
        self.assertIn("gap", statuses)

    def test_r2_pdc_02_surviving_blocker_mutants(self):
        f = self.doc["delivery_control"]["projects"]["foundation"]
        # Mutant A: non-blocked row with blocker: None or {} raises 0 diagnostics,
        # but stage: "blocked" with blocker: None or {} MUST raise BLOCKER_INVALID
        f["stage"] = "ready"
        f["blocker"] = None
        res_ready = self.run_audit()
        self.assertNotIn("BLOCKER_INVALID", self.codes(res_ready))

        f["stage"] = "blocked"
        f["blocker"] = None
        res_blocked = self.run_audit()
        self.assertIn("BLOCKER_INVALID", self.codes(res_blocked))

        # Mutant B: Fresh blocker (since: NOW) with recheck 200h in future does NOT raise horizon exceeded,
        # but STALE blocker (since: 2h ago) with recheck 200h in future DOES raise horizon exceeded
        f["stage"] = "blocked"
        f["blocker"] = {
            "owner": "Patrick",
            "reason": "Wait",
            "evidence": ["e.txt"],
            "since": NOW,
            "recheck_at": "2026-11-12T00:00:00Z",  # > 168h in future
        }
        res_fresh = self.run_audit()
        self.assertNotIn("BLOCKER_RECHECK_HORIZON_EXCEEDED", self.codes(res_fresh))

        f["blocker"]["since"] = "2026-11-02T12:00:00Z"  # 13 hours ago (stale, > 60m policy)
        res_stale = self.run_audit()
        self.assertIn("BLOCKER_RECHECK_HORIZON_EXCEEDED", self.codes(res_stale))

    def test_r2_pr2_08_work_item_attempt_admission_key_and_leetspeak_evidence(self):
        f = self.doc["delivery_control"]["projects"]["foundation"]
        f["work_item"] = {"issue_or_task": "t1", "admission_key": "k1", "attempt": 0}
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        f["work_item"]["attempt"] = -2
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        f["work_item"]["attempt"] = "first"
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        f["work_item"]["attempt"] = 1
        f["work_item"]["admission_key"] = ""
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        f["work_item"]["admission_key"] = 123
        self.assertIn("WORK_ITEM_INVALID", self.codes(self.run_audit()))

        # Leetspeak forged evidence detection: "pl4ceholder.json"
        ready = ready_row()
        ready["evidence"] = ["pl4ceholder.json"]
        ready["work_item"] = {"requested_transition": "merge", "claim_token": "tok-1"}
        self.doc["delivery_control"]["projects"]["foundation"] = ready
        res = self.run_audit()
        self.assertIn("INSUFFICIENT_COMPLETION_EVIDENCE", self.codes(res))
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res))

    def test_r2_pr2_09_and_pdc_03_coverage_counts_invalid(self):
        self.doc["delivery_control"]["projects"] = "not-a-dict"
        res = self.run_audit()
        summary = res["summary"]
        self.assertIn("invalid", summary)
        self.assertEqual(summary["invalid"], 2)
        self.assertIn("coverage_counts", summary)
        self.assertEqual(summary["coverage_counts"]["invalid"], 2)

    def test_r2_pr2_09_and_pdc_06_legacy_observed_at_skips_null_and_flags_future(self):
        del self.doc["delivery_control"]["projects"]["product"]
        # Null first time key hides valid later key: should skip null and use updated_at
        self.doc["initiatives"] = {
            "product": {
                "id": "product",
                "observedAt": None,
                "updated_at": "2026-11-03T00:50:00Z",
            }
        }
        res = self.run_audit()
        p = next(p for p in res["projects"] if p["project"] == "product")
        self.assertEqual(p["coverage"], "legacy_unverified")
        self.assertEqual(p["observed_at"], "2026-11-03T00:50:00Z")
        self.assertNotIn("observed_at_valid", p)  # valid by default

        # Future timestamp: must set observed_at_valid = False and negative age_seconds
        self.doc["initiatives"]["product"]["updated_at"] = "2026-11-03T05:00:00Z"
        res_future = self.run_audit()
        p_future = next(p for p in res_future["projects"] if p["project"] == "product")
        self.assertFalse(p_future["observed_at_valid"])
        self.assertLess(p_future["age_seconds"], 0)

    def test_derive_pending_inputs_from_pane_queued_when_acceptance_in_prompt_text(self):
        pane_text = (
            "[dispatch:agy-20261103T023817Z-205531-23683] "
            "Acknowledge this exact turn first by emitting "
            "[dispatch-accepted:agy-20261103T023817Z-205531-23683]."
        )
        derived = MODULE.derive_pending_inputs_from_pane(pane_text, NOW, "pane_evidence.txt")
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["id"], "agy-20261103T023817Z-205531-23683")
        self.assertEqual(derived[0]["status"], "queued")
        self.assertEqual(derived[0]["observed_at"], NOW)
        self.assertEqual(derived[0]["evidence"], ["pane_evidence.txt"])

        pane_file = self.root / "pane_capture.txt"
        pane_file.write_text(pane_text)
        res = self.run_preflight(pane_capture_file=pane_file)
        self.assertTrue(res["defer_input"])
        compact_intake = res["compact_projection"]["pending_intake"]
        self.assertTrue(compact_intake["defer_input"])
        self.assertEqual(len(compact_intake["pending_inputs"]), 1)
        self.assertEqual(compact_intake["pending_inputs"][0]["id"], "agy-20261103T023817Z-205531-23683")

    def test_derive_pending_inputs_from_pane_cleared_on_standalone_acceptance_line(self):
        pane_text = (
            "[dispatch:agy-20261103T023817Z-205531-23683] "
            "Acknowledge this exact turn first by emitting "
            "[dispatch-accepted:agy-20261103T023817Z-205531-23683].\n"
            "[dispatch-end:agy-20261103T023817Z-205531-23683]\n"
            "[dispatch-accepted:agy-20261103T023817Z-205531-23683]\n"
        )
        derived = MODULE.derive_pending_inputs_from_pane(pane_text, NOW, "pane_evidence.txt")
        self.assertEqual(derived, [])

        pane_file = self.root / "pane_capture.txt"
        pane_file.write_text(pane_text)
        res = self.run_preflight(pane_capture_file=pane_file)
        self.assertFalse(res["defer_input"])
        compact_intake = res["compact_projection"]["pending_intake"]
        self.assertFalse(compact_intake["defer_input"])
        self.assertEqual(compact_intake["pending_inputs"], [])

    def test_derive_pending_inputs_from_pane_ignores_prose_placeholders(self):
        pane_text = (
            "When [dispatch:agy-...] sits queued in your composer, "
            "record it and clear it when [dispatch-accepted:agy-...] is emitted. "
            "Also ignore [dispatch:agy-invalid] and [dispatch:other-12345]."
        )
        derived = MODULE.derive_pending_inputs_from_pane(pane_text, NOW, "pane_evidence.txt")
        self.assertEqual(derived, [])

        pane_file = self.root / "pane_capture.txt"
        pane_file.write_text(pane_text)
        res = self.run_preflight(pane_capture_file=pane_file)
        self.assertFalse(res["defer_input"])

    def test_pane_capture_file_missing_and_fail_closed_via_run_preflight_and_cli(self):
        missing_file = self.root / "missing_pane_capture.txt"
        res = self.run_preflight(pane_capture_file=missing_file)
        self.assertTrue(res["defer_input"])
        self.assertIn("PENDING_INPUT_UNKNOWN", self.codes(res))

        # CLI execution with missing pane capture file: fails closed with defer_input: true
        import contextlib
        import io

        self.write("coordinator-status.json", self.doc)
        self.write("policy.json", self.policy)
        self.write("local-actions.json", self.actions)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--pane-capture-file",
                    str(missing_file),
                ]
            )
        self.assertEqual(code, 1)  # attention exit code due to PENDING_INPUT_UNKNOWN
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["defer_input"])
        self.assertIn("PENDING_INPUT_UNKNOWN", {d["code"] for d in payload["diagnostics"]})

        # CLI execution with valid pane capture file containing queued dispatch
        queued_pane = self.root / "queued_pane.txt"
        queued_pane.write_text("[dispatch:agy-20261103T023817Z-205531-23683] Please handle.")
        buf_q = io.StringIO()
        with contextlib.redirect_stdout(buf_q):
            code_q = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--pane-capture-file",
                    str(queued_pane),
                ]
            )
        self.assertEqual(code_q, 0)
        payload_q = json.loads(buf_q.getvalue())
        self.assertTrue(payload_q["defer_input"])
        self.assertEqual(len(payload_q["compact_projection"]["pending_intake"]["pending_inputs"]), 1)

        # CLI execution with valid pane capture file where dispatch is accepted
        accepted_pane = self.root / "accepted_pane.txt"
        accepted_pane.write_text(
            "[dispatch:agy-20261103T023817Z-205531-23683] Please handle.\n"
            "[dispatch-end:agy-20261103T023817Z-205531-23683]\n"
            "   [dispatch-accepted:agy-20261103T023817Z-205531-23683]   \n"
        )
        buf_a = io.StringIO()
        with contextlib.redirect_stdout(buf_a):
            code_a = MODULE.main(
                [
                    "--control-dir",
                    str(self.root),
                    "--policy",
                    str(self.root / "policy.json"),
                    "--now",
                    NOW,
                    "--pane-capture-file",
                    str(accepted_pane),
                ]
            )
        self.assertEqual(code_a, 0)
        payload_a = json.loads(buf_a.getvalue())
        self.assertFalse(payload_a["defer_input"])
        self.assertEqual(payload_a["compact_projection"]["pending_intake"]["pending_inputs"], [])

    def test_derive_pending_inputs_deduplication_ordering_and_types(self):
        # Missing pending_inputs contract key when pane_capture_file is passed: fails closed
        del self.doc["delivery_control"]["pending_inputs"]
        test_pane = self.root / "test_pane_no_pending.txt"
        test_pane.write_text("[dispatch:agy-20261103T023817Z-205531-23683] Hello.")
        res_no_pending = self.run_preflight(pane_capture_file=test_pane)
        self.assertTrue(res_no_pending["defer_input"])
        self.assertIn("PENDING_INPUTS_INVALID", self.codes(res_no_pending))

        # Direct call to build_compact_projection without status/summary
        compact_direct = MODULE.build_compact_projection([], MODULE.Audit(NOW), {}, False)
        self.assertEqual(compact_direct["status"], "pass")
        self.assertEqual(compact_direct["exit_code"], 0)

        # Non-string pane text returns empty list
        self.assertEqual(MODULE.derive_pending_inputs_from_pane(None, NOW, "ref"), [])
        self.assertEqual(MODULE.derive_pending_inputs_from_pane(12345, NOW, "ref"), [])

        # Multiple dispatches in pane: deduplicated in order of appearance
        pane_text = (
            "[dispatch:agy-20261103T010000Z-111111-00001] First\n"
            "[dispatch-end:agy-20261103T010000Z-111111-00001]\n"
            "[dispatch:agy-20261103T020000Z-222222-00002] Second\n"
            "[dispatch-end:agy-20261103T020000Z-222222-00002]\n"
            "[dispatch:agy-20261103T010000Z-111111-00001] Repeat of first\n"
            "[dispatch-end:agy-20261103T010000Z-111111-00001]\n"
            "[dispatch:agy-20261103T030000Z-333333-00003] Third\n"
            "[dispatch-end:agy-20261103T030000Z-333333-00003]\n"
            "[dispatch-accepted:agy-20261103T020000Z-222222-00002]\n"
        )
        derived = MODULE.derive_pending_inputs_from_pane(pane_text, NOW, "capture.txt")
        self.assertEqual(len(derived), 2)
        self.assertEqual(derived[0]["id"], "agy-20261103T010000Z-111111-00001")
        self.assertEqual(derived[1]["id"], "agy-20261103T030000Z-333333-00003")

        # Merging with pre-existing items in contract["pending_inputs"]
        self.doc["delivery_control"]["pending_inputs"] = [
            {"id": "existing-task", "status": "consumed", "evidence": ["hist.json"]},
            {"id": "agy-20261103T010000Z-111111-00001", "status": "completed", "evidence": ["old.json"]},
        ]
        pane_file = self.root / "multi_pane.txt"
        pane_file.write_text(pane_text)
        res = self.run_preflight(pane_capture_file=pane_file)
        self.assertTrue(res["defer_input"])
        pending_list = res["compact_projection"]["pending_intake"]["pending_inputs"]
        ids = [p["id"] for p in pending_list]
        self.assertIn("existing-task", ids)
        self.assertIn("agy-20261103T010000Z-111111-00001", ids)
        self.assertIn("agy-20261103T030000Z-333333-00003", ids)
        entry_1 = next(p for p in pending_list if p["id"] == "agy-20261103T010000Z-111111-00001")
        self.assertEqual(entry_1["status"], "completed")
        self.assertEqual(entry_1["evidence"], ["old.json"])

    def test_r4_sol61_001_and_pdc_r4_02_merge_and_review_model_independence(self):
        ready = ready_row()
        ready["work_item"] = {"requested_transition": "merge", "claim_token": "tok-1"}
        self.doc["delivery_control"]["projects"]["foundation"] = ready

        # 1. Missing policy reviewer_models fails closed
        self.policy.pop("reviewer_models", None)
        res_no_models = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_no_models))

        # 2. Empty policy reviewer_models fails closed
        self.policy["reviewer_models"] = []
        res_empty_models = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_empty_models))

        # 3. Reviews with no model field fail closed
        self.policy["reviewer_models"] = ["reviewer-a-model", "reviewer-b-model", "reviewer-c-model"]
        for r in ready["reviews"]:
            r.pop("model", None)
        res_missing_review_models = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_missing_review_models))

        # 4. Same model across all required roles rejects
        for r in ready["reviews"]:
            r["model"] = "reviewer-a-model"
        res_same_model = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_same_model))

        # 5. Distinct reviewer models and distinct sessions pass
        for r, m in zip(
            ready["reviews"],
            ["reviewer-a-model", "reviewer-b-model", "reviewer-c-model"],
        ):
            r["model"] = m
        res_valid = self.run_audit()
        self.assertNotIn("TRANSITION_GUARD_FAILED", self.codes(res_valid))

        # 6. Review transition guard with duplicate reviewer sessions rejects
        ready["stage"] = "review"
        ready["work_item"]["requested_transition"] = "review"
        for r in ready["reviews"]:
            r["reviewer"] = "single-session"
        res_single_session = self.run_audit()
        self.assertIn("TRANSITION_GUARD_FAILED", self.codes(res_single_session))

    def test_r4_sol61_002_active_stage_claim_requirement_when_work_item_absent(self):
        p = self.doc["delivery_control"]["projects"]["foundation"]
        p["stage"] = "review"
        p.pop("work_item", None)

        # 1. Stage review with omitted work_item emits STALE_OR_MISSING_CLAIM
        res_absent = self.run_audit()
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res_absent))
        claim_diag = next(d for d in res_absent["diagnostics"] if d["code"] == "STALE_OR_MISSING_CLAIM")
        self.assertEqual(claim_diag["message"], "Active stage 'review' requires work_item with active claim_token")

        # 2. Stage review with work_item={} emits STALE_OR_MISSING_CLAIM
        p["work_item"] = {}
        res_empty = self.run_audit()
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res_empty))

        # 3. Stage review with valid claim token passes
        p["work_item"] = {"claim_token": "valid-tok"}
        res_valid = self.run_audit()
        self.assertNotIn("STALE_OR_MISSING_CLAIM", self.codes(res_valid))

        # 4. Stage review with expired claim token emits STALE_OR_MISSING_CLAIM
        p["work_item"]["claim_expires_at"] = "2026-11-03T00:00:00Z"
        res_expired = self.run_audit()
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res_expired))

        # 5. Stage implementation with omitted work_item emits STALE_OR_MISSING_CLAIM
        p["stage"] = "implementation"
        p.pop("work_item", None)
        res_impl_absent = self.run_audit()
        self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res_impl_absent))

    def _resolve_pointer(self, ptr):
        source = self.doc
        for token in ptr.split("/")[1:]:
            source = source[token.replace("~1", "/").replace("~0", "~")]
        return source

    def _collect_pointer_cids(self, gaps):
        found_cids = set()
        for g in gaps:
            for ptr in g["drilldown_pointers"]:
                source = self._resolve_pointer(ptr)
                self.assertIn("status", source)
                self.assertEqual(source["status"], "gap")
                if "/controls/" in ptr:
                    cid = ptr.split("/controls/")[1].replace("~1", "/").replace("~0", "~")
                    found_cids.add(cid)
        return found_cids

    def test_r4_sol61_003_and_004_compact_control_gap_aggregation_and_exact_pointers(self):
        # 1. Exact control IDs containing colons, leading/trailing whitespace, slashes, tildes
        special_ids = ["quality:lint", " surrounded ", "spec~1/core"]
        self.policy["required_controls"] = special_ids
        for pid, r in self.doc["delivery_control"]["projects"].items():
            r["controls"] = {
                cid: {"status": "gap", "owner": "dev", "observed_at": NOW, "next_action": "fix"} for cid in special_ids
            }
        res = self.run_audit()
        compact = res["compact_projection"]
        gaps = compact["aggregated_gaps"]
        found_cids = self._collect_pointer_cids(gaps)
        for cid in special_ids:
            self.assertIn(cid, found_cids)

    def test_r4_sol61_003_and_004_missing_required_controls_in_gaps(self):
        # 2. Missing required controls are visible in aggregated_gaps
        self.policy["required_controls"] = ["missing-ctrl-1", "missing-ctrl-2"]
        for r in self.doc["delivery_control"]["projects"].values():
            r.pop("controls", None)
        res_missing = self.run_audit()
        compact_missing = res_missing["compact_projection"]
        missing_gaps = [g for g in compact_missing["aggregated_gaps"] if g["status"] == "missing"]
        self.assertTrue(len(missing_gaps) >= 1)
        pointers = [p for g in missing_gaps for p in g["drilldown_pointers"]]
        self.assertTrue(any("missing-ctrl-1" in p for p in pointers))
        self.assertTrue(any("missing-ctrl-2" in p for p in pointers))

    def test_r4_pdc_r4_01_pane_acceptance_inside_prompt_block_and_soft_wrapped(self):
        disp = "agy-20261103T010000Z-prompt-probe"

        # 1. Acceptance on its own line INSIDE prompt block does not accept dispatch
        prompt_with_acceptance_inside = (
            f"[dispatch:{disp}]\n"
            "Please follow these instructions.\n"
            "First acknowledge with:\n"
            f"[dispatch-accepted:{disp}]\n"
            "Then begin work.\n"
            f"[dispatch-end:{disp}]\n"
        )
        derived = MODULE.derive_pending_inputs_from_pane(prompt_with_acceptance_inside, NOW, "pane.txt")
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["id"], disp)
        self.assertEqual(derived[0]["status"], "queued")

        # 2. Standalone acceptance AFTER dispatch-end accepts dispatch
        agent_accepted = prompt_with_acceptance_inside + f"\n[dispatch-accepted:{disp}]\nWorking on task..."
        derived_accepted = MODULE.derive_pending_inputs_from_pane(agent_accepted, NOW, "pane.txt")
        self.assertEqual(derived_accepted, [])

        # 3. Soft-wrapped dispatch and dispatch-end tokens across lines
        wrapped_pane = (
            "[dispatch:agy-20261103T010000\nZ-soft-wrapped]\n"
            "Some task\n"
            "[dispatch-end:agy-20261103T010000\nZ-soft-wrapped]\n"
            "[dispatch-accepted:agy-20261103T010000\nZ-soft-wrapped]\n"
        )
        derived_wrapped = MODULE.derive_pending_inputs_from_pane(wrapped_pane, NOW, "pane.txt")
        self.assertEqual(derived_wrapped, [])

        # 4. Soft-wrapped dispatch not accepted remains queued
        wrapped_unaccepted = (
            "[dispatch:agy-20261103T010000\nZ-soft-wrapped-queue]\n"
            "Some task\n"
            "[dispatch-end:agy-20261103T010000\nZ-soft-wrapped-queue]\n"
        )
        derived_unacc = MODULE.derive_pending_inputs_from_pane(wrapped_unaccepted, NOW, "pane.txt")
        self.assertEqual(len(derived_unacc), 1)
        self.assertEqual(derived_unacc[0]["id"], "agy-20261103T010000Z-soft-wrapped-queue")

    def test_r4_pdc_r4_03_pane_derived_does_not_overwrite_terminal_status(self):
        disp = "agy-20261103T010000Z-terminal-test"
        pane_text = f"[dispatch:{disp}]\nDo something\n[dispatch-end:{disp}]\n"

        for term_status in ["consumed", "completed", "read_verified", "failed"]:
            pending = [{"id": disp, "status": term_status, "evidence": ["terminal-receipt.json"]}]
            derived = MODULE.derive_pending_inputs_from_pane(pane_text, NOW, "pane.txt")
            self.assertEqual(len(derived), 1)
            MODULE.merge_derived_pending_inputs(pending, derived)
            self.assertEqual(pending[0]["status"], term_status)
            self.assertEqual(pending[0]["evidence"], ["terminal-receipt.json"])

        malformed_pending = [{"id": [], "status": "queued", "evidence": ["input.json"]}]
        MODULE.merge_derived_pending_inputs(malformed_pending, derived)
        self.assertEqual(len(malformed_pending), 2)

    def test_r4_pdc_r4_04_historical_review_revision_filtering(self):
        ready = ready_row()
        ready["work_item"] = {"requested_transition": "merge", "claim_token": "tok-1"}
        ready["reviews"].insert(
            0,
            {
                "role": "reviewer-a",
                "reviewer": "reviewer-a-session",
                "model": "reviewer-a-model",
                "revision": "c" * 40,
                "verdict": "REVISE",
                "evidence": ["old-rev.json"],
            },
        )
        self.doc["delivery_control"]["projects"]["foundation"] = ready
        res = self.run_audit()
        self.assertNotIn("REVIEW_MISSING_OR_DUPLICATE", self.codes(res))
        self.assertNotIn("TRANSITION_GUARD_FAILED", self.codes(res))
        self.assertEqual(res["exit_code"], 0)

    def test_r4_pdc_r4_05a_oversize_pane_capture_file(self):
        oversize_pane = self.root / "oversize_pane.txt"
        oversize_pane.write_bytes(b"A" * (2 * 1024 * 1024 + 100))

        with self.assertRaises(ValueError):
            MODULE.bounded_bytes(oversize_pane)

        res = self.run_preflight(pane_capture_file=oversize_pane)
        self.assertTrue(res["defer_input"])
        self.assertIn("PENDING_INPUT_UNKNOWN", self.codes(res))

    def test_r6_sol61_001_unclosed_prompt_does_not_self_accept(self):
        disp = "agy-20261103T023817Z-205531-23683"
        disp2 = "agy-20261103T023817Z-205531-99999"

        # Case A: Unclosed prompt with embedded acceptance instruction line remains queued
        pane_unclosed = f"[dispatch:{disp}] Do X. Acknowledge first with:\n[dispatch-accepted:{disp}]\nThen continue."
        derived = MODULE.derive_pending_inputs_from_pane(pane_unclosed, NOW, "pane.txt")
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["id"], disp)
        self.assertEqual(derived[0]["status"], "queued")

        # Case B: Unclosed prompt ending right after embedded acceptance line
        pane_unclosed_final = f"[dispatch:{disp}] Do X. Acknowledge first with:\n[dispatch-accepted:{disp}]"
        derived_final = MODULE.derive_pending_inputs_from_pane(pane_unclosed_final, NOW, "pane.txt")
        self.assertEqual(len(derived_final), 1)
        self.assertEqual(derived_final[0]["id"], disp)
        self.assertEqual(derived_final[0]["status"], "queued")

        # Case C: Closed prompt with acceptance INSIDE instructions block remains queued
        pane_closed_inside = (
            f"[dispatch:{disp}] Do X. Acknowledge first with:\n"
            f"[dispatch-accepted:{disp}]\n"
            "Then continue.\n"
            f"[dispatch-end:{disp}]"
        )
        derived_inside = MODULE.derive_pending_inputs_from_pane(pane_closed_inside, NOW, "pane.txt")
        self.assertEqual(len(derived_inside), 1)
        self.assertEqual(derived_inside[0]["id"], disp)
        self.assertEqual(derived_inside[0]["status"], "queued")

        # Case D: Closed prompt followed by standalone acceptance AFTER dispatch-end is cleared
        pane_accepted = f"[dispatch:{disp}] Do X.\n[dispatch-end:{disp}]\n[dispatch-accepted:{disp}]\n"
        derived_accepted = MODULE.derive_pending_inputs_from_pane(pane_accepted, NOW, "pane.txt")
        self.assertEqual(derived_accepted, [])

        # Case E: Unclosed prompt for disp followed by closed & accepted prompt for disp2
        pane_mixed = (
            f"[dispatch:{disp}] Unclosed task\n"
            f"[dispatch:{disp2}] Closed task\n"
            f"[dispatch-end:{disp2}]\n"
            f"[dispatch-accepted:{disp2}]\n"
        )
        derived_mixed = MODULE.derive_pending_inputs_from_pane(pane_mixed, NOW, "pane.txt")
        self.assertEqual(len(derived_mixed), 1)
        self.assertEqual(derived_mixed[0]["id"], disp)

        # Case F: Standalone acceptance token appearing before any dispatch start
        pane_orphaned = f"[dispatch-accepted:{disp}]\n[dispatch:{disp}] Prompt"
        derived_orphaned = MODULE.derive_pending_inputs_from_pane(pane_orphaned, NOW, "pane.txt")
        self.assertEqual(len(derived_orphaned), 1)
        self.assertEqual(derived_orphaned[0]["id"], disp)

        # Case G: Acceptance for disp appearing inside unclosed prompt of disp2
        pane_embedded_other = (
            f"[dispatch:{disp}] Task\n"
            f"[dispatch-end:{disp}]\n"
            f"[dispatch:{disp2}] Second unclosed task mentioning:\n"
            f"[dispatch-accepted:{disp}]\n"
        )
        # Here disp was closed, but the only acceptance for it is trapped inside disp2 unclosed block
        # Even so, disp's end is before acc_pos, but acc_pos is inside disp2's unclosed block
        derived_other = MODULE.derive_pending_inputs_from_pane(pane_embedded_other, NOW, "pane.txt")
        self.assertTrue(any(d["id"] == disp2 for d in derived_other))

    def test_r6_sol61_002_active_stage_claims(self):
        # Canonical implementing and review stages requiring active claim
        for stg in ["implementing", "review"]:
            with self.subTest(stage=stg, test="absent_work_item"):
                self.doc["delivery_control"]["projects"]["foundation"] = {
                    "stage": stg,
                    "owner": "coordinator",
                    "next_action": "Deliver",
                    "observed_at": NOW,
                    "last_progress_at": NOW,
                    "evidence": ["receipt.json"],
                }
                res = self.run_audit()
                self.assertEqual(res["exit_code"], 1)
                self.assertEqual(res["status"], "attention")
                self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res))

            with self.subTest(stage=stg, test="null_work_item"):
                self.doc["delivery_control"]["projects"]["foundation"] = {
                    "stage": stg,
                    "owner": "coordinator",
                    "next_action": "Deliver",
                    "observed_at": NOW,
                    "last_progress_at": NOW,
                    "evidence": ["receipt.json"],
                    "work_item": None,
                }
                res = self.run_audit()
                self.assertEqual(res["exit_code"], 1)
                self.assertEqual(res["status"], "attention")
                self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res))

            with self.subTest(stage=stg, test="empty_work_item"):
                self.doc["delivery_control"]["projects"]["foundation"] = {
                    "stage": stg,
                    "owner": "coordinator",
                    "next_action": "Deliver",
                    "observed_at": NOW,
                    "last_progress_at": NOW,
                    "evidence": ["receipt.json"],
                    "work_item": {},
                }
                res = self.run_audit()
                self.assertEqual(res["exit_code"], 1)
                self.assertEqual(res["status"], "attention")
                # Exactly one STALE_OR_MISSING_CLAIM (verifying no duplicate check from NB-02)
                claim_diags = [d for d in res["diagnostics"] if d["code"] == "STALE_OR_MISSING_CLAIM"]
                self.assertEqual(len(claim_diags), 1)

            with self.subTest(stage=stg, test="expired_claim"):
                self.doc["delivery_control"]["projects"]["foundation"] = {
                    "stage": stg,
                    "owner": "coordinator",
                    "next_action": "Deliver",
                    "observed_at": NOW,
                    "last_progress_at": NOW,
                    "evidence": ["receipt.json"],
                    "work_item": {
                        "claim_token": "token-xyz",
                        "claim_expires_at": "2026-11-03T00:00:00Z",
                    },
                }
                res = self.run_audit()
                self.assertEqual(res["exit_code"], 1)
                self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res))

            with self.subTest(stage=stg, test="non_dict_work_item"):
                self.doc["delivery_control"]["projects"]["foundation"] = {
                    "stage": stg,
                    "owner": "coordinator",
                    "next_action": "Deliver",
                    "observed_at": NOW,
                    "last_progress_at": NOW,
                    "evidence": ["receipt.json"],
                    "work_item": "not-a-dict",
                }
                res = self.run_audit()
                self.assertEqual(res["exit_code"], 2)
                self.assertIn("STALE_OR_MISSING_CLAIM", self.codes(res))
                self.assertIn("WORK_ITEM_INVALID", self.codes(res))

    def test_r6_coverage_hardening(self):
        # 1. is_forged_evidence_item with non-string
        self.assertTrue(MODULE.is_forged_evidence_item(12345))
        self.assertTrue(MODULE.is_forged_evidence_item(None))

        # 2. review_checks_compat and review_checks called with list
        compat_diags = MODULE.review_checks(
            [{"role": "reviewer-a", "reviewer": "r-a", "model": "m-a", "verdict": "SHIP", "evidence": ["a.json"]}],
            {"required_review_roles": ["reviewer-a"]},
            HEAD,
        )
        self.assertIsInstance(compat_diags, list)

        # 3. check_single_role_review duplicate matches on target_rev
        identities = []
        audit = MODULE.Audit(NOW)
        MODULE.check_single_role_review(
            audit,
            "foundation",
            [
                {
                    "role": "reviewer-a",
                    "reviewer": "r1",
                    "model": "m1",
                    "revision": HEAD,
                    "verdict": "SHIP",
                    "evidence": ["1.json"],
                },
                {
                    "role": "reviewer-a",
                    "reviewer": "r2",
                    "model": "m2",
                    "revision": HEAD,
                    "verdict": "SHIP",
                    "evidence": ["2.json"],
                },
            ],
            "reviewer-a",
            HEAD,
            identities,
        )
        self.assertIn("REVIEW_MISSING_OR_DUPLICATE", [d["code"] for d in audit.diagnostics])

        # 4. worker_checks and worker_identity branches
        audit_w = MODULE.Audit(NOW)
        MODULE.check_worker_role_model(audit_w, "proj", {"role": "other", "model": "diff"}, "target-model")
        self.assertIn("IMPLEMENTER_MODEL", [d["code"] for d in audit_w.diagnostics])

        audit_w2 = MODULE.Audit(NOW)
        MODULE.worker_identity(
            audit_w2,
            "proj",
            {
                "id": "w1",
                "model": "m1",
                "role": "review",
                "user": "u1",
                "worktree": "/w",
                "write_paths": None,
                "artifact_exports": [123],
            },
            {},
        )
        self.assertIn("WORKER_INVALID", [d["code"] for d in audit_w2.diagnostics])

        # 5. check_work_item_fields edge cases (non-dict, non-positive attempt, non-string admission_key)
        audit_wi = MODULE.Audit(NOW)
        MODULE.check_work_item_fields(audit_wi, "proj", "not-dict")
        self.assertIn("WORK_ITEM_INVALID", [d["code"] for d in audit_wi.diagnostics])

        audit_wi2 = MODULE.Audit(NOW)
        MODULE.check_work_item_fields(audit_wi2, "proj", {"attempt": -1, "admission_key": ""})
        self.assertIn("WORK_ITEM_INVALID", [d["code"] for d in audit_wi2.diagnostics])

        # 6. is_valid_review_entry edge cases
        self.assertIsNone(
            MODULE.is_valid_review_entry({"revision": HEAD, "verdict": "MAYBE", "evidence": ["e"]}, HEAD, ["m1"], [])
        )
        self.assertIsNone(
            MODULE.is_valid_review_entry(
                {"revision": HEAD, "verdict": "SHIP", "evidence": ["e"], "model": "banned"}, HEAD, ["m1"], ["banned"]
            )
        )
        self.assertIsNone(
            MODULE.is_valid_review_entry(
                {"revision": HEAD, "verdict": "SHIP", "evidence": ["e"], "model": "m2"}, HEAD, ["m1"], []
            )
        )

        # 7. check_role_ship_review edge case: non-string reviewer
        seen_rev = set()
        seen_mod = set()
        self.assertFalse(
            MODULE.check_role_ship_review(
                [{"role": "r", "revision": HEAD, "verdict": "SHIP", "evidence": ["e"], "reviewer": "", "model": "m1"}],
                "r",
                HEAD,
                {"m1"},
                seen_rev,
                seen_mod,
            )
        )

        # 8. check_single_control_gate with non-dict
        self.assertFalse(MODULE.check_single_control_gate("invalid", HEAD))

        # 9. merge_single_derived_input edge cases
        pending_list = [{"id": "item-1", "status": "queued"}]
        existing_by_id = {"item-1": pending_list[0]}
        MODULE.merge_single_derived_input(pending_list, existing_by_id, "non-dict")
        MODULE.merge_single_derived_input(pending_list, existing_by_id, {"id": ""})
        MODULE.merge_single_derived_input(
            pending_list, existing_by_id, {"id": "item-1", "status": "queued", "note": "updated"}
        )
        self.assertEqual(pending_list[0].get("note"), "updated")

        # 10. parse_gap_diagnostic and collect_gap_diagnostic_groups
        a_diag = MODULE.Audit(NOW)
        a_diag.add("CONTROL_GAP", "Control missing_key: gap; extra info", "foundation")
        a_diag.add("CONTROL_GAP", "unparseable gap message", "foundation")
        groups = MODULE.collect_gap_diagnostic_groups(a_diag)
        self.assertIn(("foundation", "gap"), groups)

        # 11. print_compact_markdown with transition failures and aggregated gaps
        import contextlib
        import io

        buf = io.StringIO()
        report = {
            "status": "attention",
            "compact_projection": {
                "status": "attention",
                "exit_code": 1,
                "summary": {"invalid": 0, "absent": 0, "legacy_unverified": 0, "normalized": 1},
                "changed_or_ready_projects": [{"project": "foundation", "stage": "ready"}],
                "time_critical_blockers": [{"project": "foundation", "recheck_at": "2026-11-03T02:00:00Z"}],
                "transition_failures": [
                    {
                        "project": "foundation",
                        "stage": "implementing",
                        "code": "REVIEW_MISSING",
                        "message": "Missing reviews",
                    }
                ],
                "aggregated_gaps": [
                    {
                        "project": "foundation",
                        "status": "gap",
                        "count": 1,
                        "control_ids": ["lint"],
                        "drilldown_pointers": ["/projects/foundation/controls/lint"],
                    }
                ],
                "pending_intake": {"defer_input": True, "pending_inputs": [{"id": "disp-1", "status": "queued"}]},
            },
        }
        with contextlib.redirect_stdout(buf):
            MODULE.print_compact_markdown(report)
        self.assertIn("Transition failure: foundation", buf.getvalue())
        self.assertIn("Gaps: foundation gap: count=1", buf.getvalue())

        # 12. resolve_requested_transition_str branches
        self.assertIsNone(MODULE.resolve_requested_transition_str(None, None))
        self.assertIsNone(MODULE.resolve_requested_transition_str({"requested_transition": 123}, None))
        self.assertEqual(MODULE.resolve_requested_transition_str({"requested_transition": "merge"}, None), "merge")
        self.assertEqual(MODULE.resolve_requested_transition_str(None, {"requested_transition": "review"}), "review")


if __name__ == "__main__":
    unittest.main()
