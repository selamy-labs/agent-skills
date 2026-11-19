"""Contract and behavioral qualification tests for skills/rackspace-spot/SKILL.md."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_SHELL_CASES: tuple[tuple[str, bool], ...] = (
    ("dash", False),
    ("dash", True),
    ("bash", False),
    ("bash", True),
)

# Upstream Go struct JSON tags from rackspace-spot/spot-go-sdk @ ef90b998c8d8773e0807517c51f918a7d78f6dba
# Reference files: api/v1/types.go, api/v1/spotnodepool.go, api/v1/pricing.go
SPOT_GO_SDK_V020_STRUCT_JSON_TAGS: dict[str, tuple[str, ...]] = {
    "Organization": ("name", "id"),
    "CloudSpaceList": ("cloudspaces",),
    "CloudSpace": (
        "name",
        "org",
        "region",
        "creationTimestamp",
        "kubernetesVersion",
        "status",
        "assignedServers",
    ),
    "AssignedServer": ("clusterRole", "serverClassName", "state"),
    "SpotNodePool": (
        "name",
        "org",
        "cloudspace",
        "serverClass",
        "desired",
        "wonCount",
        "bidPrice",
        "status",
        "autoscaling",
    ),
    "PriceDetails": (
        "serverClassName",
        "displayName",
        "category",
        "region",
        "currentMarketPrice",
        "cpu",
        "memory",
    ),
    "Region": ("name", "description"),
    "ServerClass": (
        "name",
        "displayName",
        "category",
        "region",
        "onDemandPricePerHour",
        "resources",
        "availability",
    ),
    "Resource": ("cpu", "memory"),
}


def _extract_sh_blocks(skill_text: str) -> list[str]:
    """Return all fenced ```sh code blocks in skill_text."""
    return re.findall(r"```sh\n(.*?)```", skill_text, re.DOTALL)


def _extract_jq_filter(sec3_block: str, command_prefix: str) -> str:
    """Extract jq filter argument following command_prefix in Section 3 shell block."""
    pos = sec3_block.find(command_prefix)
    assert pos != -1, f"Command {command_prefix} not found in Section 3 shell block"
    jq_pos = sec3_block.find("jq '", pos)
    assert jq_pos != -1, f"jq not found after {command_prefix}"
    start = jq_pos + 4
    end = sec3_block.find("'", start)
    assert end != -1, "Closing quote for jq filter not found"
    return sec3_block[start:end]


def _run_jq_filter(filter_str: str, input_data: Any) -> Any:
    """Execute jq filter with input_data and return parsed JSON."""
    input_text = "null" if input_data is None else json.dumps(input_data)
    proc = subprocess.run(
        ["jq", filter_str],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _prepare_shell_script(script: str, *, no_e: bool) -> str:
    """Return script intact or with its unique set -eu line disabled for no-errexit testing."""
    if not no_e:
        return script
    assert script.count("set -eu") == 1
    mutated = script.replace("set -eu", ":", 1)
    assert mutated != script
    return mutated


def _write_full_spotctl_stub(stub_bin: Path) -> Path:
    """Create a strict stub spotctl validating exact v0.2.0 flags and returning fixture JSON."""
    stub_spotctl = stub_bin / "spotctl"
    stub_script = (
        "#!/bin/sh\n"
        'if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n'
        "  printf 'spotctl v0.2.0\\n'\n"
        "  exit 0\n"
        "fi\n"
        'cmd="$1 $2"\n'
        'case "$cmd" in\n'
        '  "organizations list")\n'
        '    [ "$3" = "--output" ] && [ "$4" = "json" ] || exit 21\n'
        '    printf \'[{"id": "org-123", "name": "org-alpha", "secretField": "hide"}]\\n\'\n'
        "    ;;\n"
        '  "cloudspaces list")\n'
        '    [ "$3" = "--org" ] && [ "$4" = "org-alpha" ] && [ $# -eq 4 ] || exit 22\n'
        '    printf \'{"cloudspaces": [{"name": "cs-1", "org": "org-alpha", '
        '"region": "us-central", "status": "Running", "rawApi": "drop"}]}\\n\'\n'
        "    ;;\n"
        '  "cloudspaces get")\n'
        '    if [ "$#" -eq 3 ] && [ "$3" = "--help" ]; then exit 0; fi\n'
        '    [ "$3" = "--name" ] && [ "$4" = "cs-1" ] '
        '&& [ "$5" = "--org" ] && [ "$6" = "org-alpha" ] '
        '&& [ "$7" = "--output" ] && [ "$8" = "json" ] || exit 23\n'
        '    printf \'{"name": "cs-1", "org": "org-alpha", "region": "us-central", '
        '"creationTimestamp": "2026-11-08T12:00:00Z", "kubernetesVersion": "1.30.2", '
        '"status": "Running", "assignedServers": {"s1": {"clusterRole": "worker", '
        '"serverClassName": "c1.small", "state": "running", "IP": "redacted-ip"}}, '
        '"preEmptionWebhookURL": "https://hooks.internal/p"}\\n\'\n'
        "    ;;\n"
        '  "nodepools spot")\n'
        '    [ "$3" = "list" ] && [ "$4" = "--cloudspace" ] && [ "$5" = "cs-1" ] '
        '&& [ "$6" = "--org" ] && [ "$7" = "org-alpha" ] '
        '&& [ "$8" = "--output" ] && [ "$9" = "json" ] || exit 24\n'
        '    printf \'[{"name": "np-1", "org": "org-alpha", "cloudspace": "cs-1", '
        '"serverClass": "c1.small", "bidPrice": "$0.0800", "status": "Fulfilled"}]\\n\'\n'
        "    ;;\n"
        '  "pricing get")\n'
        '    [ "$3" = "--serverclass" ] && [ "$4" = "c1.small" ] '
        '&& [ "$5" = "--output" ] && [ "$6" = "json" ] || exit 25\n'
        '    printf \'{"serverClassName": "c1.small", "displayName": "Compute", '
        '"category": "General", "region": "us-central", "currentMarketPrice": "$0.0450", '
        '"cpu": "2", "memory": "4Gi"}\\n\'\n'
        "    ;;\n"
        '  "regions get")\n'
        '    [ "$3" = "--name" ] && [ "$4" = "us-central-dfw-1" ] '
        '&& [ "$5" = "--output" ] && [ "$6" = "json" ] || exit 26\n'
        '    printf \'{"name": "us-central-dfw-1", "description": "Dallas"}\\n\'\n'
        "    ;;\n"
        '  "serverclasses get")\n'
        '    [ "$3" = "--name" ] && [ "$4" = "c1.small" ] '
        '&& [ "$5" = "--output" ] && [ "$6" = "json" ] || exit 27\n'
        '    printf \'{"name": "c1.small", "displayName": "Compute", "category": "General", '
        '"region": "us-central", "onDemandPricePerHour": "$0.1200", '
        '"resources": {"cpu": "2", "memory": "4Gi"}, "availability": "Available"}\\n\'\n'
        "    ;;\n"
        "  *)\n"
        "    exit 29\n"
        "    ;;\n"
        "esac\n"
    )
    stub_spotctl.write_text(stub_script, encoding="utf-8")
    stub_spotctl.chmod(0o755)
    return stub_spotctl


class TestRackspaceSpotSkill:
    """AS-SPOT-S1: Qualification tests for skills/rackspace-spot/SKILL.md."""

    @pytest.fixture
    def skill_path(self) -> Path:
        path = REPO_ROOT / "skills" / "rackspace-spot" / "SKILL.md"
        assert path.is_file(), f"Missing skill file at {path}"
        return path

    @pytest.fixture
    def skill_text(self, skill_path: Path) -> str:
        return skill_path.read_text(encoding="utf-8")

    def test_rackspace_spot_skill_passes_all_static_gates(self, skill_path: Path, skill_text: str) -> None:
        """Verify SKILL.md and this test file satisfy naming, privacy, and security static gates."""
        from tools.naming import validate_skill
        from tools.privacy import findings_for_text as privacy_findings
        from tools.security import findings_for_text as security_findings

        self_text = Path(__file__).read_text(encoding="utf-8")
        assert validate_skill(skill_text, skill_path, set()) == []
        assert privacy_findings(skill_text, "skills/rackspace-spot/SKILL.md") == []
        assert security_findings(skill_text, "skills/rackspace-spot/SKILL.md") == []
        assert privacy_findings(self_text, "tests/test_rackspace_spot.py") == []
        assert security_findings(self_text, "tests/test_rackspace_spot.py") == []

    def test_rackspace_spot_skill_documents_required_contracts(self, skill_text: str) -> None:
        """Verify SKILL.md contains required upstream sources, checksum verification, commands, and boundaries."""
        assert "https://github.com/rackspace-spot/spotctl" in skill_text
        assert "https://github.com/rackspace-spot/spot-go-sdk" in skill_text
        assert "v0.2.0" in skill_text
        assert "6efb9bb764d57195b9dfd6b42581a52837899897" in skill_text
        assert "ef90b998c8d8773e0807517c51f918a7d78f6dba" in skill_text
        assert "Use when" in skill_text

        expected_digests = (
            "2add5c920524bdb9ac3f78eca9d6c66382ee1b70d43934e7387b1fe51853f278",
            "dff5021d00d45a379dc4144129139b6d6073e10d4bb23e90ac675eeb1f06d25b",
            "b1320ee9fdab622051e3b1dfc858097492f08f1cd5c08f5f34734872515980d8",
            "9f5eb1077b2918b8cb6d0a7bb3f0ec0390584d7d88d379fa52efcc70280e6505",
        )
        for digest in expected_digests:
            assert digest in skill_text

        sh_blocks = _extract_sh_blocks(skill_text)
        assert len(sh_blocks) == 3
        assert "set -C" in sh_blocks[1]
        assert "~/.spot_config" in skill_text
        assert "spotctl configure" in skill_text
        assert "refreshToken" in skill_text
        assert "jq -Rs" in sh_blocks[1]
        assert "cleanup_verified" in sh_blocks[1]
        assert "cat <<EOF" not in skill_text

        required_cli_patterns = (
            "spotctl organizations list",
            "spotctl cloudspaces list",
            "spotctl cloudspaces get --name",
            "spotctl nodepools spot list --cloudspace",
            "spotctl pricing get --serverclass",
            "spotctl regions get --name",
            "spotctl serverclasses get --name",
        )
        for pattern in required_cli_patterns:
            assert pattern in skill_text

        assert "cloudspaces get-config" in skill_text
        assert "Autoscaling" in skill_text
        assert "wonCount" in skill_text
        assert "OpenTofu" in skill_text
        assert "tofu plan" in skill_text
        assert "Mismatched Organization or Region" in skill_text
        assert "Missing or Expired Credentials" in skill_text
        assert "Sensitive Command Output" in skill_text
        assert "Imperative Bid or Pool Mutation Without Authority" in skill_text

    def test_rackspace_spot_skill_negative_controls(self, skill_path: Path, skill_text: str) -> None:
        """Verify validate_skill and privacy scanner fail when negative controls are injected."""
        from tools.naming import validate_skill
        from tools.privacy import findings_for_text as privacy_findings

        bloated_desc = " ".join(["word"] * 46)
        bloated_text = re.sub(r"description: .*", f"description: {bloated_desc}", skill_text, count=1)
        naming_errors = validate_skill(bloated_text, skill_path, set())
        assert any("description should stay concise (<=45 words)" in err for err in naming_errors)

        leaked_text = skill_text + "\npass" + " show rackspace/token\n"
        privacy_errors = privacy_findings(leaked_text, "skills/rackspace-spot/SKILL.md")
        assert any("blocked secret/path pattern" in err for err in privacy_errors)

    def _verify_cloudspaces_get_projection(self, sec3_block: str) -> None:
        flt = _extract_jq_filter(sec3_block, "cloudspaces get --name")
        cs_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["CloudSpace"]
        srv_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["AssignedServer"]
        payload = {
            "name": "test-cloudspace",
            "org": "test-org",
            "region": "us-east",
            "creationTimestamp": "2026-11-08T12:00:00Z",
            "kubernetesVersion": "1.30.2",
            "status": "Running",
            "assignedServers": {
                "srv-1": {
                    "clusterRole": "worker",
                    "serverClassName": "c1.small",
                    "state": "configured",
                    "IP": "redacted-ip",
                }
            },
            "preEmptionWebhookURL": "https://hooks.internal/preempt",
            "apiServerEndpoint": "https://k8s.internal:6443",
            "annotations": {"test": "val"},
        }
        res = _run_jq_filter(flt, payload)
        assert res == {
            "name": "test-cloudspace",
            "org": "test-org",
            "region": "us-east",
            "creationTimestamp": "2026-11-08T12:00:00Z",
            "kubernetesVersion": "1.30.2",
            "status": "Running",
            "assignedServerCount": 1,
            "assignedServers": [{"clusterRole": "worker", "serverClassName": "c1.small", "state": "configured"}],
        }
        assert all(k in cs_tags for k in res if k != "assignedServerCount")
        assert all(k in srv_tags for k in res["assignedServers"][0])
        assert "IP" not in res["assignedServers"][0]

        nil_srv = _run_jq_filter(flt, {**payload, "assignedServers": None})
        assert nil_srv["assignedServerCount"] == 0
        assert nil_srv["assignedServers"] == []

    def _verify_nodepools_list_projection(self, sec3_block: str) -> None:
        flt = _extract_jq_filter(sec3_block, "nodepools spot list --cloudspace")
        np_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["SpotNodePool"]
        payload = [
            {
                "name": "pool-1",
                "org": "test-org",
                "cloudspace": "test-cloudspace",
                "serverClass": "c1.small",
                "bidPrice": "$0.0800",
                "status": "Fulfilled",
                "autoscaling": {"enabled": False, "minNodes": 0, "maxNodes": 0},
            },
            {
                "name": "pool-2",
                "org": "test-org",
                "cloudspace": "test-cloudspace",
                "serverClass": "c1.medium",
                "status": "Pending",
            },
        ]
        res = _run_jq_filter(flt, payload)
        assert res == [
            {
                "name": "pool-1",
                "org": "test-org",
                "cloudspace": "test-cloudspace",
                "serverClass": "c1.small",
                "desired": 0,
                "wonCount": 0,
                "bidPrice": "$0.0800",
                "status": "Fulfilled",
            },
            {
                "name": "pool-2",
                "org": "test-org",
                "cloudspace": "test-cloudspace",
                "serverClass": "c1.medium",
                "desired": 0,
                "wonCount": 0,
                "bidPrice": None,
                "status": "Pending",
            },
        ]
        assert all(k in np_tags for k in res[0])
        assert _run_jq_filter(flt, None) == []

    def _verify_pricing_get_projection(self, sec3_block: str) -> None:
        flt = _extract_jq_filter(sec3_block, "pricing get --serverclass")
        pricing_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["PriceDetails"]
        base: dict[str, Any] = {
            "serverClassName": "c1.small",
            "displayName": "Compute Small",
            "category": "General",
            "region": "us-east",
            "cpu": "2",
            "memory": "4Gi",
        }
        payload_base = {**base, "internalSku": "secret-sku"}
        res1 = _run_jq_filter(flt, {**payload_base, "currentMarketPrice": "$0.0450"})
        assert res1 == {**base, "currentMarketPrice": "$0.0450"}
        assert all(k in pricing_tags for k in res1)

        res2 = _run_jq_filter(flt, payload_base)
        assert res2 == {**base, "currentMarketPrice": None}
        assert all(k in pricing_tags for k in res2)

    def _verify_organizations_list_projection(self, sec3_block: str) -> None:
        flt = _extract_jq_filter(sec3_block, "organizations list")
        org_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["Organization"]
        payload = [
            {org_tags[0]: "org-alpha", org_tags[1]: "id-123", "internalSecret": "hide-me"},
            {org_tags[0]: "org-beta", org_tags[1]: "id-456", "token": "drop-me"},
        ]
        res = _run_jq_filter(flt, payload)
        assert res == [
            {"name": "org-alpha", "id": "id-123"},
            {"name": "org-beta", "id": "id-456"},
        ]
        assert all(k in org_tags for k in res[0])
        assert _run_jq_filter(flt, None) == []

    def _verify_cloudspaces_list_projection(self, sec3_block: str) -> None:
        flt = _extract_jq_filter(sec3_block, "cloudspaces list")
        list_wrapper = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["CloudSpaceList"][0]
        cs_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["CloudSpace"]
        payload = {
            list_wrapper: [
                {
                    "name": "cs-1",
                    "org": "test-org",
                    "region": "us-central",
                    "status": "Running",
                    "rawApiEndpoint": "https://secret.internal",
                }
            ],
            "topSecret": "classified",
        }
        res = _run_jq_filter(flt, payload)
        assert res == {
            "cloudspaces": [
                {
                    "name": "cs-1",
                    "org": "test-org",
                    "region": "us-central",
                    "status": "Running",
                }
            ]
        }
        assert all(k in cs_tags for k in res["cloudspaces"][0])
        assert _run_jq_filter(flt, {list_wrapper: None}) == {"cloudspaces": []}

    def _verify_regions_get_projection(self, sec3_block: str) -> None:
        flt = _extract_jq_filter(sec3_block, "regions get --name")
        reg_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["Region"]
        payload = {
            "name": "us-central-dfw-1",
            "description": "Dallas-Fort Worth",
            "internalEndpoint": "https://secret.internal",
            "adminZone": "zone-a",
        }
        res = _run_jq_filter(flt, payload)
        assert res == {"name": "us-central-dfw-1", "description": "Dallas-Fort Worth"}
        assert all(k in reg_tags for k in res)

    def _verify_serverclasses_get_projection(self, sec3_block: str) -> None:
        flt = _extract_jq_filter(sec3_block, "serverclasses get --name")
        sc_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["ServerClass"]
        res_tags = SPOT_GO_SDK_V020_STRUCT_JSON_TAGS["Resource"]
        base: dict[str, Any] = {
            "name": "c1.small",
            "displayName": "Compute Small",
            "category": "General",
            "region": "us-central",
            "resources": {"cpu": "2", "memory": "4Gi"},
            "availability": "Available",
            "billingSku": "sku-xyz",
        }
        res1 = _run_jq_filter(flt, {**base, "onDemandPricePerHour": "$0.1200"})
        assert res1 == {
            "name": "c1.small",
            "displayName": "Compute Small",
            "category": "General",
            "region": "us-central",
            "onDemandPricePerHour": "$0.1200",
            "resources": {"cpu": "2", "memory": "4Gi"},
            "availability": "Available",
        }
        assert all(k in sc_tags for k in res1)
        assert all(k in res_tags for k in res1["resources"])
        res2 = _run_jq_filter(flt, base)
        assert res2["onDemandPricePerHour"] is None

    def test_rackspace_spot_jq_projections_reproduce_f1_and_preserve_sdk_contracts(self, skill_text: str) -> None:
        """Verify all 7 Section 3 jq projections handle nil-slices, strip sensitive data, and reproduce F1."""
        sec3_block = _extract_sh_blocks(skill_text)[2]
        self._verify_organizations_list_projection(sec3_block)
        self._verify_cloudspaces_list_projection(sec3_block)
        self._verify_cloudspaces_get_projection(sec3_block)
        self._verify_nodepools_list_projection(sec3_block)
        self._verify_pricing_get_projection(sec3_block)
        self._verify_regions_get_projection(sec3_block)
        self._verify_serverclasses_get_projection(sec3_block)

    def _verify_section3_pipefail_behavior(self, sec3_block: str, tmp_path: Path) -> None:
        """Verify Section 3 pipefail fails closed when unsupported (exit 1) and propagates error (exit 7)."""
        stub_bin = tmp_path / "pipefail_stub_bin"
        stub_bin.mkdir(parents=True, exist_ok=True)
        invoked_marker = tmp_path / "spotctl_invoked"
        stub_spotctl = stub_bin / "spotctl"
        stub_spotctl.write_text('#!/bin/sh\n: > "$SPOT_INVOKED_MARKER"\nexit 7\n', encoding="utf-8")
        stub_spotctl.chmod(0o755)

        extracted_sec3 = sec3_block.split("# 2. Cloudspace list")[0]
        assert "organizations list" in extracted_sec3
        tmp_sub = tmp_path / "pipefail_tmp"
        tmp_sub.mkdir(parents=True, exist_ok=True)
        env = {
            "SPOTCTL_BIN": str(stub_spotctl),
            "SPOT_ERR_LOG": str(tmp_sub / "pipefail_err.log"),
            "SPOT_INVOKED_MARKER": str(invoked_marker),
            "TMPDIR": str(tmp_sub),
            "PATH": "/usr/bin:/bin",
        }
        assert extracted_sec3.count("(set -o pipefail)") == 1
        unsupported_probe = extracted_sec3.replace("(set -o pipefail)", "(set -o unsupported_pipefail_probe)", 1)
        res_unsupported = subprocess.run(["bash", "-c", unsupported_probe], env=env, capture_output=True, text=True)
        assert res_unsupported.returncode == 1
        assert "pipefail is required" in res_unsupported.stderr
        assert not invoked_marker.exists()

        dash_has_pipefail = subprocess.run(["dash", "-c", "(set -o pipefail)"], capture_output=True).returncode == 0
        res_dash = subprocess.run(["dash", "-c", extracted_sec3], env=env, capture_output=True, text=True)
        if dash_has_pipefail:
            assert res_dash.returncode == 7
        else:
            assert res_dash.returncode == 1
            assert "pipefail is required" in res_dash.stderr
        invoked_marker.unlink(missing_ok=True)

        res_bash = subprocess.run(["bash", "-c", extracted_sec3], env=env, capture_output=True, text=True)
        assert res_bash.returncode == 7
        assert invoked_marker.exists()

        env_no_err = {
            "SPOTCTL_BIN": str(stub_spotctl),
            "SPOT_INVOKED_MARKER": str(invoked_marker),
            "TMPDIR": str(tmp_sub),
            "PATH": "/usr/bin:/bin",
        }
        res_no_err = subprocess.run(["bash", "-c", extracted_sec3], env=env_no_err, capture_output=True, text=True)
        assert res_no_err.returncode != 0
        assert "run Section 3 in the same bash invocation as the Section 2 credential bootstrap" in res_no_err.stderr

    def _verify_combined_all_sections_execution(self, sh_blocks: list[str], tmp_path: Path) -> None:
        """Execute Section 1 (checksum ok) + Section 2 + all 7 Section 3 commands in one bash process."""
        stub_bin = tmp_path / "combined_stub_bin"
        self._create_test_stubs(stub_bin)
        _write_full_spotctl_stub(stub_bin)

        fake_bin = "#!/bin/sh\nexec " + str(stub_bin / "spotctl") + ' "$@"\n'
        valid_sha = hashlib.sha256(fake_bin.encode()).hexdigest()
        official_linux_sha = "2add5c920524bdb9ac3f78eca9d6c66382ee1b70d43934e7387b1fe51853f278"
        assert sh_blocks[0].count(official_linux_sha) == 1
        sec1_ok = sh_blocks[0].replace(official_linux_sha, valid_sha, 1)

        home = tmp_path / "combined_home"
        home.mkdir(parents=True, exist_ok=True)
        tmp_sub = tmp_path / "combined_tmp"
        tmp_sub.mkdir(parents=True, exist_ok=True)

        env = {
            "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
            "HOME": str(home),
            "TMPDIR": str(tmp_sub),
            "CURL_PAYLOAD": fake_bin,
            "SPOT_ORG_NAME": "org-alpha",
            "SPOT_CLOUDSPACE_NAME": "cs-1",
            "SPOT_SERVERCLASS_NAME": "c1.small",
            "SPOT_REGION_NAME": "us-central-dfw-1",
        }
        full_script = sec1_ok + "\n" + sh_blocks[1] + "\n" + sh_blocks[2]
        res = subprocess.run(["bash", "-c", full_script], capture_output=True, text=True, env=env)
        assert res.returncode == 0, res.stderr
        assert '"org-alpha"' in res.stdout
        assert '"us-central-dfw-1"' in res.stdout
        assert '"onDemandPricePerHour": "$0.1200"' in res.stdout
        assert "cleanup_verified" in res.stderr
        assert "cleanup_error" not in res.stderr
        assert not (home / ".spot_config").exists()
        assert list(tmp_sub.iterdir()) == []

        env_discover = {
            "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
            "HOME": str(home),
            "TMPDIR": str(tmp_sub),
            "CURL_PAYLOAD": fake_bin,
        }
        res_disc = subprocess.run(["bash", "-c", full_script], capture_output=True, text=True, env=env_discover)
        assert res_disc.returncode != 0
        assert '"org-alpha"' in res_disc.stdout
        assert "set SPOT_ORG_NAME" in res_disc.stderr
        assert list(tmp_sub.iterdir()) == []

    def test_rackspace_spot_cli_invocations_enforce_f2_f3_f4_contracts(self, skill_text: str, tmp_path: Path) -> None:
        """Verify shell syntax, all 7 CLI commands, pipefail gates, and trap cleanup across blocks."""
        sh_blocks = _extract_sh_blocks(skill_text)
        assert len(sh_blocks) == 3, f"Expected exactly 3 sh blocks in SKILL.md, got {len(sh_blocks)}"
        for i, block in enumerate(sh_blocks):
            assert "cloudspaces get-config" not in block, f"Block {i} contains cloudspaces get-config"
            syntax = subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True)
            assert syntax.returncode == 0, f"Block {i} failed bash -n: {syntax.stderr}"

        sec3_block = sh_blocks[2]
        assert "trap " not in sec3_block, "Section 3 must not set trap that clobbers Section 2 EXIT trap"
        assert ': "${SPOTCTL_BIN:?' in sec3_block
        assert ': "${SPOT_ERR_LOG:?' in sec3_block
        assert "(set -o pipefail) 2>/dev/null ||" in sec3_block
        assert "set -o pipefail" in sec3_block
        self._verify_section3_pipefail_behavior(sec3_block, tmp_path)

        sec4_idx = skill_text.find("## 4. Diagnostic Interpretation")
        sec4_text = skill_text[sec4_idx:]
        assert "Supply credentials via protected `stdin` directly into process memory" in sec4_text

        self._verify_combined_all_sections_execution(sh_blocks, tmp_path)

    def _create_test_stubs(self, stub_bin: Path) -> None:
        stub_bin.mkdir(parents=True, exist_ok=True)
        uname_sh = stub_bin / "uname"
        uname_sh.write_text(
            '#!/bin/sh\nif [ "$1" = "-s" ]; then echo Linux; elif [ "$1" = "-m" ]; then echo x86_64; fi\n',
            encoding="utf-8",
        )
        uname_sh.chmod(0o755)

        curl_sh = stub_bin / "curl"
        curl_sh.write_text(
            "#!/bin/sh\n"
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--output" ]; then\n'
            '    printf "%s" "$CURL_PAYLOAD" > "$2"\n'
            "    exit 0\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            "exit 1\n",
            encoding="utf-8",
        )
        curl_sh.chmod(0o755)

        gcloud_sh = stub_bin / "gcloud"
        gcloud_sh.write_text('#!/bin/sh\necho "tok_test_123"\n', encoding="utf-8")
        gcloud_sh.chmod(0o755)

    def _verify_install_checksum_gate(self, skill_text: str, tmp_path: Path) -> None:
        install_script = _extract_sh_blocks(skill_text)[0]
        stub_bin = tmp_path / "stub_bin"
        self._create_test_stubs(stub_bin)
        path_env = f"{stub_bin}:{os.environ.get('PATH', '')}"

        home_bad = tmp_path / "home_bad"
        tmp_bad = tmp_path / "tmp_bad"
        home_bad.mkdir()
        tmp_bad.mkdir()
        env_bad = dict(os.environ, PATH=path_env, HOME=str(home_bad), TMPDIR=str(tmp_bad), CURL_PAYLOAD="corrupted")
        res_bad = subprocess.run(["sh", "-c", install_script], capture_output=True, env=env_bad)
        assert res_bad.returncode != 0
        assert not (home_bad / ".local" / "bin" / "spotctl").exists()
        assert list(tmp_bad.iterdir()) == []

        home_ok = tmp_path / "home_ok"
        tmp_ok = tmp_path / "tmp_ok"
        home_ok.mkdir()
        tmp_ok.mkdir()
        valid_payload = "#!/bin/sh\necho spotctl-v0.2.0\n"
        valid_sha = hashlib.sha256(valid_payload.encode()).hexdigest()
        official_linux_sha = "2add5c920524bdb9ac3f78eca9d6c66382ee1b70d43934e7387b1fe51853f278"
        assert install_script.count(official_linux_sha) == 1
        ok_script = install_script.replace(official_linux_sha, valid_sha, 1)
        env_ok = dict(os.environ, PATH=path_env, HOME=str(home_ok), TMPDIR=str(tmp_ok), CURL_PAYLOAD=valid_payload)
        res_ok = subprocess.run(["sh", "-c", ok_script], capture_output=True, text=True, env=env_ok)
        assert res_ok.returncode == 0
        installed_bin = home_ok / ".local" / "bin" / "spotctl"
        assert installed_bin.exists()
        assert stat.S_IMODE(installed_bin.stat().st_mode) == 0o755
        assert "spotctl-v0.2.0" in res_ok.stdout
        assert list(tmp_ok.iterdir()) == []

        stub_sig = tmp_path / "stub_bin_sig"
        self._create_test_stubs(stub_sig)
        (stub_sig / "curl").write_text('#!/bin/sh\nkill -TERM "$PPID"\nsleep 1\n', encoding="utf-8")
        (stub_sig / "curl").chmod(0o755)
        home_sig = tmp_path / "home_sig"
        tmp_sig = tmp_path / "tmp_sig"
        home_sig.mkdir()
        tmp_sig.mkdir()
        env_sig = dict(
            os.environ, PATH=f"{stub_sig}:{os.environ.get('PATH', '')}", HOME=str(home_sig), TMPDIR=str(tmp_sig)
        )
        res_sig = subprocess.run(["sh", "-c", install_script], capture_output=True, env=env_sig)
        assert res_sig.returncode != 0
        assert not (home_sig / ".local" / "bin" / "spotctl").exists()
        assert list(tmp_sig.iterdir()) == []

    def _verify_scenario_a_preexisting_config(self, script: str, tmp_path: Path, path_env: str) -> None:
        """Scenario A: Preexisting regular file or dangling symlink guard refuses overwrite."""
        tmp_sub = tmp_path / "tmp_a"
        tmp_sub.mkdir()
        for i, (sh, no_e) in enumerate(_SHELL_CASES):
            pre_home = tmp_path / f"pre_home_{i}"
            pre_home.mkdir()
            pre_cfg = pre_home / ".spot_config"
            pre_cfg.write_text("org: existing\n", encoding="utf-8")
            orig_inode = pre_cfg.stat().st_ino
            env = {"PATH": path_env, "HOME": str(pre_home), "TMPDIR": str(tmp_sub)}
            res = subprocess.run(
                [sh, "-c", _prepare_shell_script(script, no_e=no_e)], capture_output=True, text=True, env=env
            )
            assert res.returncode != 0
            assert pre_cfg.read_text(encoding="utf-8") == "org: existing\n"
            assert pre_cfg.stat().st_ino == orig_inode
            assert "refusing: ~/.spot_config already exists" in res.stderr

        # Dangling symlink must also be refused fail-closed
        sym_home = tmp_path / "sym_home"
        sym_home.mkdir()
        sym_cfg = sym_home / ".spot_config"
        sym_cfg.symlink_to(sym_home / "nonexistent-target")
        sym_res = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": path_env, "HOME": str(sym_home), "TMPDIR": str(tmp_sub)},
        )
        assert sym_res.returncode != 0
        assert sym_cfg.is_symlink()
        assert "refusing: ~/.spot_config already exists" in sym_res.stderr

    def _verify_scenario_b1_empty_failing_gcloud(self, script: str, tmp_path: Path, path_env: str) -> None:
        """Scenario B1: Empty-failing gcloud stub in dash and bash (set -eu and no-e) cleans up on EXIT."""
        stub_bin = tmp_path / "stub_bin_fail_b1"
        stub_bin.mkdir()
        (stub_bin / "gcloud").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (stub_bin / "gcloud").chmod(0o755)
        tmp_sub = tmp_path / "tmp_b1"
        tmp_sub.mkdir()
        env_path = f"{stub_bin}:{path_env}"

        for i, (sh, no_e) in enumerate(_SHELL_CASES):
            home = tmp_path / f"fail_home_b1_{i}"
            home.mkdir()
            env = {"PATH": env_path, "HOME": str(home), "TMPDIR": str(tmp_sub)}
            res = subprocess.run(
                [sh, "-c", _prepare_shell_script(script, no_e=no_e)], capture_output=True, text=True, env=env
            )
            assert res.returncode != 0
            assert not (home / ".spot_config").exists()
            assert "cleanup_verified" in res.stderr

    def _verify_scenario_b2_nonempty_failing_gcloud(self, script: str, tmp_path: Path, path_env: str) -> None:
        """Scenario B2: Nonempty failing gcloud fails closed without echoing secret."""
        tmp_sub = tmp_path / "tmp_b2"
        tmp_sub.mkdir()
        payloads = [
            ("partial_token", "#!/bin/sh\nprintf 'partial_token\\n'\nexit 1\n"),
            ("sentinel_bypass", "#!/bin/sh\nprintf 'PARTIAL-LINE\\n__SPOT_OK__\\n'\nexit 1\n"),
        ]
        for name, gcloud_code in payloads:
            stub_bin = tmp_path / f"stub_bin_fail_b2_{name}"
            stub_bin.mkdir(parents=True, exist_ok=True)
            (stub_bin / "gcloud").write_text(gcloud_code, encoding="utf-8")
            (stub_bin / "gcloud").chmod(0o755)
            env_path = f"{stub_bin}:{path_env}"

            for i, (sh, no_e) in enumerate(_SHELL_CASES):
                home = tmp_path / f"fail_home_b2_{name}_{i}"
                home.mkdir(parents=True, exist_ok=True)
                env = {"PATH": env_path, "HOME": str(home), "TMPDIR": str(tmp_sub)}
                res = subprocess.run(
                    [sh, "-c", _prepare_shell_script(script, no_e=no_e)], capture_output=True, text=True, env=env
                )
                assert res.returncode != 0
                assert not (home / ".spot_config").exists()
                for forbidden in ("partial_token", "PARTIAL-LINE", "__SPOT_OK__"):
                    assert forbidden not in res.stderr
                    assert forbidden not in res.stdout
                assert "error: secret producer or serializer failed" in res.stderr
                assert "cleanup_verified" in res.stderr

    def _verify_scenario_b3_multiline_secret_refusal(self, script: str, tmp_path: Path, path_env: str) -> None:
        """Scenario B3: Multiline and control-character payloads fail closed without echoing secret."""
        tmp_sub = tmp_path / "tmp_b3"
        tmp_sub.mkdir()
        payloads = [
            ("multiline", "#!/bin/sh\nprintf 'secret_line_one\\nsecret_line_two\\n'\nexit 0\n", "secret_line_"),
            ("interior_cr", "#!/bin/sh\nprintf 'abc\\rdef\\n'\nexit 0\n", "abc"),
        ]
        for name, gcloud_code, leak_prefix in payloads:
            stub_bin = tmp_path / f"stub_bin_fail_b3_{name}"
            stub_bin.mkdir(parents=True, exist_ok=True)
            (stub_bin / "gcloud").write_text(gcloud_code, encoding="utf-8")
            (stub_bin / "gcloud").chmod(0o755)
            env_path = f"{stub_bin}:{path_env}"

            for i, (sh, no_e) in enumerate(_SHELL_CASES):
                home = tmp_path / f"fail_home_b3_{name}_{i}"
                home.mkdir(parents=True, exist_ok=True)
                env = {"PATH": env_path, "HOME": str(home), "TMPDIR": str(tmp_sub)}
                res = subprocess.run(
                    [sh, "-c", _prepare_shell_script(script, no_e=no_e)], capture_output=True, text=True, env=env
                )
                assert res.returncode != 0
                assert not (home / ".spot_config").exists()
                assert leak_prefix not in res.stderr
                assert leak_prefix not in res.stdout
                assert "error: invalid or multiline secret payload" in res.stderr
                assert "cleanup_verified" in res.stderr

    def _verify_scenario_c_clean_lifecycle(self, script: str, tmp_path: Path, path_env: str) -> None:
        """Scenario C: Clean ephemeral lifecycle creates mode 0600 config and removes on EXIT."""
        tmp_sub = tmp_path / "tmp_c"
        tmp_sub.mkdir()
        for i, (sh, no_e) in enumerate(_SHELL_CASES):
            home = tmp_path / f"fresh_home_{i}"
            home.mkdir()
            env = {"PATH": path_env, "HOME": str(home), "TMPDIR": str(tmp_sub)}
            probe_script = (
                _prepare_shell_script(script, no_e=no_e)
                + '\nstat -c "%a" "$HOME/.spot_config"\ncat "$HOME/.spot_config"\n'
            )
            res = subprocess.run([sh, "-c", probe_script], capture_output=True, text=True, env=env)
            assert res.returncode == 0
            lines = res.stdout.strip().splitlines()
            assert lines[0].strip() == "600"
            cfg_data = json.loads("\n".join(lines[1:]))
            assert cfg_data["org"] == ""
            assert cfg_data["region"] == ""
            assert cfg_data["refreshToken"] == "tok_test_123"
            assert not (home / ".spot_config").exists()
            assert "cleanup_verified: ~/.spot_config removed" in res.stderr

    def _verify_scenario_d_signals(self, script: str, tmp_path: Path, path_env: str) -> None:
        """Scenario D: HUP, INT, and TERM signal delivery terminates immediately and cleans up."""
        tmp_sub = tmp_path / "tmp_d"
        tmp_sub.mkdir()
        cases = [
            (sig_name, sig_code, sh, no_e)
            for sig_name, sig_code in (("TERM", 143), ("INT", 130), ("HUP", 129))
            for sh, no_e in _SHELL_CASES
        ]
        sig_anchor = "trap 'exit 143' TERM\nif [ -z \"$CONFIG_INODE\" ]; then"
        for i, (sig_name, sig_code, sh, no_e) in enumerate(cases):
            home = tmp_path / f"sig_home_{i}"
            home.mkdir()
            env = {"PATH": path_env, "HOME": str(home), "TMPDIR": str(tmp_sub)}
            base_script = _prepare_shell_script(script, no_e=no_e)
            assert base_script.count(sig_anchor) == 1
            injection = f'\nkill -{sig_name} $$\nfor j in $(seq 1 20); do : > "$HOME/steal_$j"; done\n'
            probe_sig = base_script.replace(
                sig_anchor,
                f"trap 'exit 143' TERM{injection}\nif [ -z \"$CONFIG_INODE\" ]; then",
                1,
            )
            res = subprocess.run([sh, "-c", probe_sig], capture_output=True, text=True, env=env)
            assert res.returncode == sig_code
            assert not (home / ".spot_config").exists()
            assert not list(home.glob("steal_*"))
            assert "cleanup_verified" in res.stderr
            assert "cleanup_error" not in res.stderr

    def _verify_scenario_e_stat_failure(self, script: str, tmp_path: Path) -> None:
        """Scenario E: Inode-capture failure cleans up empty 0600 file on EXIT."""
        stub_bin = tmp_path / "stub_bin_stat_e"
        stub_bin.mkdir()
        (stub_bin / "stat").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (stub_bin / "stat").chmod(0o755)
        tmp_sub = tmp_path / "tmp_e"
        tmp_sub.mkdir()
        env_path = f"{stub_bin}:/usr/bin:/bin"

        for i, (sh, no_e) in enumerate(_SHELL_CASES):
            home = tmp_path / f"stat_fail_home_{i}"
            home.mkdir()
            env = {"PATH": env_path, "HOME": str(home), "TMPDIR": str(tmp_sub)}
            res = subprocess.run(
                [sh, "-c", _prepare_shell_script(script, no_e=no_e)], capture_output=True, text=True, env=env
            )
            assert res.returncode != 0
            assert "failed to capture ~/.spot_config inode" in res.stderr
            assert not (home / ".spot_config").exists()
            assert "cleanup_verified: ~/.spot_config removed (inode unrecorded)" in res.stderr

    def _verify_scenario_f_unmasked_sensitivity(
        self, sh: str, probe_script: str, tmp_path: Path, path_env: str, case_idx: int
    ) -> None:
        """Prove regression sensitivity: disabling cleanup signal mask causes signal to abort cleanup."""
        assert probe_script.count("  trap '' HUP INT TERM\n") == 1
        unmasked_script = probe_script.replace("  trap '' HUP INT TERM\n", "  :\n", 1)
        assert unmasked_script != probe_script

        unmasked_home = tmp_path / f"sig_cleanup_unmasked_{case_idx}"
        unmasked_home.mkdir(parents=True, exist_ok=True)
        tmp_sub = tmp_path / f"tmp_f_unmasked_{case_idx}"
        tmp_sub.mkdir(parents=True, exist_ok=True)
        env = {"PATH": path_env, "HOME": str(unmasked_home), "TMPDIR": str(tmp_sub)}
        unmasked_res = subprocess.run([sh, "-c", unmasked_script], capture_output=True, text=True, env=env)
        assert (unmasked_home / ".spot_config").exists()
        assert "cleanup_verified" not in unmasked_res.stderr

    def _verify_scenario_f_signal_during_exit_cleanup(self, script: str, tmp_path: Path, path_env: str) -> None:
        """Scenario F: HUP, INT, and TERM inside cleanup_spot_config are masked and config is removed cleanly."""
        cases = [(sig, sh, no_e) for sig in ("TERM", "INT", "HUP") for sh, no_e in _SHELL_CASES]
        anchor = "cleanup_spot_config() {\n  EXIT_STATUS=$?\n  trap '' HUP INT TERM"
        for i, (sig_name, sh, no_e) in enumerate(cases):
            home = tmp_path / f"sig_cleanup_home_{i}"
            home.mkdir(parents=True, exist_ok=True)
            tmp_sub = tmp_path / f"tmp_f_{i}"
            tmp_sub.mkdir(parents=True, exist_ok=True)
            env = {"PATH": path_env, "HOME": str(home), "TMPDIR": str(tmp_sub)}
            base_script = _prepare_shell_script(script, no_e=no_e)
            assert base_script.count(anchor) == 1, f"Expected exactly 1 match for {anchor!r}"
            probe_script = base_script.replace(anchor, f"{anchor}\n  kill -{sig_name} $$\n", 1)

            res = subprocess.run([sh, "-c", probe_script], capture_output=True, text=True, env=env)
            assert res.returncode == 0
            assert not (home / ".spot_config").exists()
            assert "cleanup_verified" in res.stderr
            assert "cleanup_error" not in res.stderr

            self._verify_scenario_f_unmasked_sensitivity(sh, probe_script, tmp_path, path_env, i)

    def _verify_scenario_g1_marker_unlink(self, sec2_block: str, tmp_path: Path) -> None:
        """Subcase G1: Temp marker unlink failure does not abort credential cleanup or mask return code."""
        stub_bin = tmp_path / "stub_bin_g1"
        stub_bin.mkdir(parents=True, exist_ok=True)
        rm_stub = (
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            "    -*|*.spot_config) ;;\n"
            "    *) printf 'rm: cannot remove temp file: Operation not permitted\\n' >&2; exit 1 ;;\n"
            "  esac\n"
            "done\n"
            'exec /bin/rm "$@"\n'
        )
        (stub_bin / "rm").write_text(rm_stub, encoding="utf-8")
        (stub_bin / "rm").chmod(0o755)
        tmp_sub = tmp_path / "tmp_g1"
        tmp_sub.mkdir()
        env_path = f"{stub_bin}:{os.environ.get('PATH', '')}"
        modes = [("normal", "#!/bin/sh\necho tok_123\n", 0), ("fail_producer", "#!/bin/sh\nexit 1\n", 1)]

        for mode_name, gcloud_code, expected_rc in modes:
            (stub_bin / "gcloud").write_text(gcloud_code, encoding="utf-8")
            (stub_bin / "gcloud").chmod(0o755)
            for i, (sh, no_e) in enumerate(_SHELL_CASES):
                home = tmp_path / f"g1_home_{mode_name}_{i}"
                home.mkdir(parents=True, exist_ok=True)
                env = {"PATH": env_path, "HOME": str(home), "TMPDIR": str(tmp_sub)}
                run_script = _prepare_shell_script(sec2_block, no_e=no_e)
                res = subprocess.run([sh, "-c", run_script], capture_output=True, text=True, env=env)
                assert res.returncode == expected_rc, res.stderr
                assert not (home / ".spot_config").exists()
                assert "cleanup_verified: ~/.spot_config removed" in res.stderr

    def _verify_scenario_g2_stat_failure(self, sec2_block: str, tmp_path: Path) -> None:
        """Subcase G2: Cleanup-time stat failure reports cleanup_error and preserves file for fail-closed."""
        for i, (sh, no_e) in enumerate(_SHELL_CASES):
            case_dir = tmp_path / f"g2_case_{i}"
            stub_bin = case_dir / "bin"
            home = case_dir / "home"
            tmp_sub = case_dir / "tmp"
            for d in (stub_bin, home, tmp_sub):
                d.mkdir(parents=True, exist_ok=True)
            cnt_file = case_dir / "stat_count"

            (stub_bin / "gcloud").write_text("#!/bin/sh\necho tok_123\n", encoding="utf-8")
            (stub_bin / "gcloud").chmod(0o755)
            stat_script = (
                "#!/bin/sh\n"
                f'cnt_file="{cnt_file}"\n'
                'cnt=0\n[ -f "$cnt_file" ] && cnt=$(cat "$cnt_file")\n'
                'cnt=$((cnt + 1))\necho "$cnt" > "$cnt_file"\n'
                'if [ "$cnt" -eq 1 ]; then exec /bin/stat "$@"; else exit 1; fi\n'
            )
            (stub_bin / "stat").write_text(stat_script, encoding="utf-8")
            (stub_bin / "stat").chmod(0o755)

            env = {"PATH": f"{stub_bin}:{os.environ.get('PATH', '')}", "HOME": str(home), "TMPDIR": str(tmp_sub)}
            res = subprocess.run(
                [sh, "-c", _prepare_shell_script(sec2_block, no_e=no_e)], capture_output=True, text=True, env=env
            )
            assert res.returncode != 0
            assert "cleanup_error: ~/.spot_config still present" in res.stderr
            assert "cleanup_verified" not in res.stderr
            assert (home / ".spot_config").exists()

    def _verify_scenario_h_redacted_err_log_classification(
        self, sec2_block: str, tmp_path: Path, path_env: str
    ) -> None:
        """Scenario H: Non-zero exit emits token-redacted error classes before unlinking SPOT_ERR_LOG."""
        cases = [
            (
                "not_found_404",
                "HTTP 404 cloudspace missing token=tok_live_secret_999\n",
                7,
                "spot_diagnostic_error: exit=7 classes=http_404_not_found",
                "tok_live_secret_999",
            ),
            (
                "auth_401",
                "Error: refresh token is required (401 Unauthorized) secret=tok_refresh_leak_888\n",
                1,
                "spot_diagnostic_error: exit=1 classes=missing_refresh_token,http_401_unauthorized",
                "tok_refresh_leak_888",
            ),
            (
                "unclassified",
                "unexpected socket timeout payload=tok_unknown_777\n",
                9,
                "spot_diagnostic_error: exit=9 classes=unclassified_stderr",
                "tok_unknown_777",
            ),
        ]
        for label, err_msg, exit_code, expected_class, secret_str in cases:
            home = tmp_path / f"err_home_{label}"
            tmp_sub = tmp_path / f"err_tmp_{label}"
            home.mkdir(parents=True, exist_ok=True)
            tmp_sub.mkdir(parents=True, exist_ok=True)
            err_script = sec2_block + f'\nprintf %s {shlex.quote(err_msg)} >> "$SPOT_ERR_LOG"\nexit {exit_code}\n'
            env = {"PATH": path_env, "HOME": str(home), "TMPDIR": str(tmp_sub)}
            res = subprocess.run(["bash", "-c", err_script], capture_output=True, text=True, env=env)
            assert res.returncode == exit_code
            assert expected_class in res.stderr
            assert secret_str not in res.stderr
            assert secret_str not in res.stdout
            assert list(tmp_sub.iterdir()) == []

    def _verify_ephemeral_config_lifecycle(self, skill_text: str, tmp_path: Path) -> None:
        config_script = _extract_sh_blocks(skill_text)[1]
        stub_bin = tmp_path / "stub_bin"
        self._create_test_stubs(stub_bin)
        path_env = f"{stub_bin}:{os.environ.get('PATH', '')}"

        self._verify_scenario_a_preexisting_config(config_script, tmp_path, path_env)
        self._verify_scenario_b1_empty_failing_gcloud(config_script, tmp_path, path_env)
        self._verify_scenario_b2_nonempty_failing_gcloud(config_script, tmp_path, path_env)
        self._verify_scenario_b3_multiline_secret_refusal(config_script, tmp_path, path_env)
        self._verify_scenario_c_clean_lifecycle(config_script, tmp_path, path_env)
        self._verify_scenario_d_signals(config_script, tmp_path, path_env)
        self._verify_scenario_e_stat_failure(config_script, tmp_path)
        self._verify_scenario_f_signal_during_exit_cleanup(config_script, tmp_path, path_env)
        self._verify_scenario_g1_marker_unlink(config_script, tmp_path)
        self._verify_scenario_g2_stat_failure(config_script, tmp_path)
        self._verify_scenario_h_redacted_err_log_classification(config_script, tmp_path, path_env)

    def test_rackspace_spot_install_and_ephemeral_config_lifecycle_f4_f6(self, skill_text: str, tmp_path: Path) -> None:
        """Verify checksum gate fails closed and ephemeral config lifecycle enforces ownership and cleanup."""
        self._verify_install_checksum_gate(skill_text, tmp_path)
        self._verify_ephemeral_config_lifecycle(skill_text, tmp_path)
