"""Read-only repository discovery and explicit, configured check execution (schema 1)."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def discover(path, base=None):
    root = Path(git(path, "rev-parse", "--show-toplevel"))
    head = git(root, "rev-parse", "HEAD")
    changed = git(root, "status", "--porcelain=v1", "-z")
    result = {
        "schema_version": 1,
        "root": str(root),
        "head": head,
        "dirty": bool(changed),
        "status_porcelain": changed,
        "tools": {name: shutil.which(name) for name in ("codegraph", "complexipy", "ruff", "node", "npm")},
        "codegraph": "indexed" if (root / ".codegraph").is_dir() else "not_indexed",
        "checks": [],
        "limits": ["Discovery does not qualify code or execute CI YAML."],
    }
    if base:
        result["base"] = git(root, "rev-parse", "--verify", base + "^{commit}")
        result["committed_changed_files"] = git(root, "diff", "--name-only", "-z", result["base"], head)
    manifest = root / ".repository-preflight.json"
    if manifest.exists():
        result["checks"] = load_checks(manifest)
    else:
        result["limits"].append("No configured checks; repository validation remains pending.")
    return result


def load_checks(path):
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("checks"), list):
        raise ValueError("Unsupported preflight manifest")
    checks = manifest["checks"]
    names = set()
    for check in checks:
        name = check["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("Check names must be unique nonempty strings")
        names.add(name)
        if check.get("scope") not in {"worktree", "index", "HEAD", "hosted"}:
            raise ValueError("Check requires explicit input scope")
        if check["scope"] == "hosted":
            continue
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
            raise ValueError("Local check requires nonempty argv; shell source is unsupported")
    return checks


def run_check(report, name, destination):
    check = next(c for c in report["checks"] if c["name"] == name)
    if check["scope"] == "hosted":
        return {"name": name, "status": "pending_hosted", "scope": "hosted"}
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = git(report["root"], "status", "--porcelain=v1", "-z")
    with destination.open("w") as log:
        try:
            run = subprocess.run(
                check["argv"], cwd=report["root"], stdout=log, stderr=subprocess.STDOUT, timeout=300, check=False
            )
            status = "passed" if run.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
    current_head = git(report["root"], "rev-parse", "HEAD")
    current_status = git(report["root"], "status", "--porcelain=v1", "-z")
    stable = report["head"] == current_head and before == current_status
    return {
        "name": name,
        "status": status,
        "head": report["head"],
        "scope": check["scope"],
        "log": str(destination),
        "identity_stable": stable,
        "limits": "HEAD/status comparison is not a content receipt; mutable inputs require fresh validation.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("--base")
    parser.add_argument("--run", help="Explicitly execute one reviewed manifest entry")
    parser.add_argument("--log", help="Detailed output path, required with --run")
    args = parser.parse_args()
    if args.run and not args.log:
        parser.error("--run requires --log")
    report = discover(args.repository, args.base)
    if args.run:
        report["result"] = run_check(report, args.run, args.log)
    print(json.dumps(report))
    return int(bool(args.run) and report["result"]["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
