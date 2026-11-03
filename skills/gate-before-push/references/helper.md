# Repository preflight, version 1

From this skill directory run:

```sh
python3 scripts/repository_preflight.py /path/to/repository --base main
```

Discovery is read-only: root, HEAD, dirty/untracked status, optional resolved base
and committed changed paths, available executable paths and local CodeGraph-index
presence. It does not initialize/sync CodeGraph or run arbitrary CI YAML. A present
index is not proof of freshness. Use the existing CodeGraph startup workflow.

Repository owners can check in `.repository-preflight.json`:

```json
{"schema_version":1,"checks":[
  {"name":"unit","scope":"worktree","argv":["python3","-m","pytest"]},
  {"name":"integration","scope":"hosted"}
]}
```

Scopes: `worktree`, `index`, `HEAD`, `hosted`. Commands are argv arrays, executed
without a shell from the repository root only after explicit
`--run unit --log /absolute/path/unit.log`. Review manifest commands first;
repository commands execute with normal caller authority. Missing configuration
means pending validation. Hosted checks remain pending and exit nonzero.
Each run is bounded to five minutes. Logs stay in the requested artifact file.
Executable absence, Git failure and malformed manifests fail the command.

HEAD/status stability is diagnostic, not a content hash or reusable certificate.
Changes to file contents, index, configuration, tools or environment invalidate
applicable prior results. Select check scope and relevant consumers using the
repository's actual policy. Discovery and passing checks do not prove correctness.
