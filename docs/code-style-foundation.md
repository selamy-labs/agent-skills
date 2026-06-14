# Code Style Foundation

This is a language-agnostic baseline for keeping code small, readable,
reproducible, and enforceable by ordinary open-source tools.

## Universal Rules

- Format with the ecosystem default formatter before arguing about style.
- Keep functions shallow: prefer early returns and guard clauses over nested
  `else` blocks.
- Replace long conditional chains with table, map, enum, or strategy dispatch
  when cases are named and stable.
- Use enums or tagged unions for behavior modes. Avoid boolean flags in CLIs or
  public APIs when more than one behavior axis exists.
- Enforce complexity budgets in CI. Start with a cyclomatic or cognitive
  complexity threshold near 10 and ratchet locally when a repository is already
  cleaner.
- Treat line coverage as a floor, not the product goal. Every feature should
  have at least one named behavior test or acceptance fixture for the outcome
  it promises.
- Publish findings as review annotations or PR comments when that shortens
  feedback, but keep the status check as the merge gate.

## Blessed Toolchains

| Scope | Formatter | Lint / Static Analysis | Type / Coverage Gate |
| --- | --- | --- | --- |
| Cross-language | EditorConfig for whitespace, final newline, charset, and end-of-line defaults | reviewdog or native annotations for PR feedback | Repository status checks remain authoritative |
| Python | Ruff formatter | Ruff lint with `C901` or equivalent complexity rules enabled | Pyright for fast standards-based typing; keep mypy when a repo already depends on plugins; coverage.py with feature fixtures |
| TypeScript / JavaScript | Biome for low-customization repos; Prettier where established | ESLint with `@typescript-eslint` type-aware rules for typed apps; Biome lint for simpler packages | TypeScript `tsc --noEmit`; Vitest or Jest coverage thresholds plus feature tests |
| Go | `gofmt` and `goimports` | `golangci-lint` with complexity linters such as `gocyclo` or `gocognit` | `go test ./...` with package coverage where meaningful |
| Java | `google-java-format` | Checkstyle for style and complexity; SpotBugs for bug patterns | Build-tool test and coverage gates |
| Kotlin | ktlint | detekt for complexity and code-smell rules | Gradle test and coverage gates |
| Shell | shfmt | ShellCheck | Shell scripts must have smoke tests for non-trivial behavior |

## Source Lineage

- EditorConfig official site: file format and editor plugin ecosystem for
  consistent styles across editors, https://editorconfig.org/.
- EditorConfig specification: `.editorconfig` parsing and UTF-8 requirements,
  https://spec.editorconfig.org/.
- Ruff configuration and formatter docs: Black-compatible formatting defaults
  and explicit McCabe complexity rule selection, https://docs.astral.sh/ruff/.
- Pyright project: standards-based Python type checker designed for high
  performance, https://github.com/microsoft/pyright.
- mypy project: optional static typing for Python with PEP 484 annotations,
  https://mypy-lang.org/.
- Biome docs: formatter and linter for web languages with Prettier-compatible
  formatting goals, https://biomejs.dev/.
- ESLint max-depth docs and rules reference: nesting and complexity controls,
  https://eslint.org/docs/latest/rules/max-depth.
- typescript-eslint typed linting docs: type-aware lint rules use TypeScript's
  checker for deeper findings, https://typescript-eslint.io/getting-started/typed-linting/.
- Vitest coverage docs and Jest configuration docs: coverage providers and
  thresholds, https://vitest.dev/guide/coverage.html and
  https://jestjs.io/docs/configuration.
- Go Effective Go: Go code in standard packages is formatted with `gofmt`,
  https://go.dev/doc/effective_go.
- golangci-lint linters docs: `gocyclo` and `gocognit` complexity support,
  https://golangci-lint.run/docs/linters/.
- ShellCheck and shfmt sources: shell static analysis and formatting,
  https://www.shellcheck.net/ and https://github.com/mvdan/sh.
- google-java-format, Checkstyle, SpotBugs, ktlint, and detekt sources:
  Java/Kotlin formatting, complexity, and bug-pattern checks,
  https://github.com/google/google-java-format,
  https://checkstyle.sourceforge.io/checks/metrics/cyclomaticcomplexity.html,
  https://spotbugs.github.io/, https://github.com/pinterest/ktlint, and
  https://detekt.dev/docs/intro/.

## Open Questions

- Pytype remains useful in some existing Python codebases, but new Python
  baselines should prefer Pyright unless a repository already has a better
  reason to keep mypy or another checker.
- Biome is the default for simple web packages. Type-aware TypeScript apps still
  need ESLint with typescript-eslint unless Biome's rule surface is enough for
  that repository.
- Reviewdog is a feedback channel, not the policy source. Native GitHub
  annotations are acceptable when they provide the same diff-local visibility.
