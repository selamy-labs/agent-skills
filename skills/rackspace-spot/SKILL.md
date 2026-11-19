---
name: rackspace-spot
description: Diagnose Rackspace Spot cloudspaces, SpotNodePool resources, and market pricing using official spotctl CLI or spot-go-sdk. Use when investigating wonCount=0, spot node eviction, 404 wrong-org cloudspace errors, or bid rates safely within OpenTofu IaC boundaries.
---

# Rackspace Spot Diagnosis

Use this skill for read-only diagnosis of Rackspace Spot cloudspaces, spot node
pools, and market pricing using the official `spotctl` CLI or `spot-go-sdk`.

## 1. Official Sources & Pinned Installation

Source tooling directly from official Rackspace Spot repositories:
- CLI repository: `https://github.com/rackspace-spot/spotctl` (pin **`v0.2.0`**, commit `6efb9bb764d57195b9dfd6b42581a52837899897`, `https://github.com/rackspace-spot/spotctl/releases/tag/v0.2.0`)
- Go SDK repository: `https://github.com/rackspace-spot/spot-go-sdk` (pin **`v0.2.0`**, commit `ef90b998c8d8773e0807517c51f918a7d78f6dba`; Go `1.23.5`)

### Installation-Owner Provisioning & Verified Executable Binding
The installation owner provisions `spotctl` onto authorized dev-box or jump-box
user accounts using this verified recipe. It isolates staging in a private
subshell (`0700` directory cleaned on subshell exit without altering caller traps),
enforces a fail-closed checksum gate for `spotctl v0.2.0`, and exports `SPOTCTL_BIN`:

```sh
set -eu

# Detect operating system and architecture
OS="$(uname -s)"
ARCH="$(uname -m)"

case "${OS}_${ARCH}" in
  Linux_x86_64)
    ASSET="spotctl-linux-amd64"
    EXPECTED_SHA="2add5c920524bdb9ac3f78eca9d6c66382ee1b70d43934e7387b1fe51853f278"
    ;;
  Linux_aarch64|Linux_arm64)
    ASSET="spotctl-linux-arm64"
    EXPECTED_SHA="dff5021d00d45a379dc4144129139b6d6073e10d4bb23e90ac675eeb1f06d25b"
    ;;
  Darwin_x86_64)
    ASSET="spotctl-darwin-amd64"
    EXPECTED_SHA="b1320ee9fdab622051e3b1dfc858097492f08f1cd5c08f5f34734872515980d8"
    ;;
  Darwin_arm64)
    ASSET="spotctl-darwin-arm64"
    EXPECTED_SHA="9f5eb1077b2918b8cb6d0a7bb3f0ec0390584d7d88d379fa52efcc70280e6505"
    ;;
  *)
    echo "Unsupported platform: ${OS}_${ARCH}" >&2
    exit 1
    ;;
esac

# Stage, verify checksum, and install inside an isolated subshell so STAGE_DIR EXIT trap cannot clobber caller traps
(
  STAGE_DIR="$(mktemp -d)"
  chmod 0700 "$STAGE_DIR"
  trap 'rm -rf "$STAGE_DIR"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  curl --fail --location --proto '=https' --tlsv1.2 \
    "https://github.com/rackspace-spot/spotctl/releases/download/v0.2.0/${ASSET}" \
    --output "${STAGE_DIR}/spotctl"

  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "$EXPECTED_SHA" "${STAGE_DIR}/spotctl" | sha256sum -c -
  else
    printf '%s  %s\n' "$EXPECTED_SHA" "${STAGE_DIR}/spotctl" | shasum -a 256 -c -
  fi

  mkdir -p "$HOME/.local/bin"
  install -m 0755 "${STAGE_DIR}/spotctl" "$HOME/.local/bin/spotctl"
)

export SPOTCTL_BIN="$HOME/.local/bin/spotctl"

# Verify non-network CLI help via the bound verified absolute executable
"$SPOTCTL_BIN" --version
"$SPOTCTL_BIN" cloudspaces get --help
```

### Persistent Fleet Installation
Persistent fleet installation across runner hosts or containers must be
declared in source-owned infrastructure provisioning (such as container image
dockerfiles, Packer templates, or host-level provisioning scripts). Having the
binary installed grants no token access. Never perform ad-hoc unverified binary
downloads on live production hosts.

## 2. Mandatory `~/.spot_config` Lifecycle & Authentication

### Mandatory Config File Contract
`spotctl v0.2.0` always reads `~/.spot_config` using Go's `os.UserHomeDir()`.
There is **no** alternative config-path flag in the reviewed CLI loader:
```yaml
org: ""
refreshToken: "<Rackspace Spot OAuth refresh token>"
accessToken: ""
region: ""
```
- A nonempty YAML `refreshToken` is required before SDK initialization, even
  when `--org` is supplied on the command line (the JSON emitted by `jq` in
  the recipe below is valid YAML 1.2 accepted by `spotctl`'s YAML config loader).
- Leave `org: ""` and `region: ""` blank in `~/.spot_config`: `organizations list`
  needs neither, and org-scoped commands pass `--org "$SPOT_ORG_NAME"` explicitly.
- A blank `accessToken` triggers OAuth exchange at `/oauth/token`.
- Environment-only bootstrap does **not** satisfy `spotctl` CLI startup: while
  the underlying SDK reads `SPOT_REFRESH_TOKEN` and `SPOT_ACCESS_TOKEN`, the CLI
  loader requires `~/.spot_config` with a nonempty `refreshToken` before passing
  control to the SDK. `SPOT_ORG` and `SPOT_REGION` are not consumed by the CLI.

### Hazards of `spotctl configure`
Do **not** run `spotctl configure` interactively:
- It reads three newline-terminated values (`org`, `refreshToken`, `region`) from
  standard input without masking interactive token entry.
- It overwrites any existing `~/.spot_config`.
- It does not repair insecure permissions on an existing file.

### Prohibited Credential Actions
- **Never repurpose HOME**: Do not manipulate `HOME` to redirect the config
  path, as this breaks other user processes and tools.
- **Never overwrite existing config**: Refuse execution if a preexisting
  `~/.spot_config` is found.
- **Never supply tokens in command arguments**: Never provide tokens in `argv`,
  command options, shell tracing (`set -x`), or chat transcripts.
- **Never print raw config or run standalone secret commands**: Standalone
  secret retrieval commands (such as `gcloud secrets versions access`) print the
  secret payload to `stdout`; they must never be executed directly to a
  terminal or script log.

### Supported Ephemeral Credential Lifecycle
When an operator or worker is authorized to execute read-only checks, execute
the following credential recipe under `bash`. It enforces the following core guarantees:
- **Single-shell execution requirement**: Because the `EXIT` trap removes `~/.spot_config` and `$SPOT_ERR_LOG` when the shell process terminates, you **must** execute the Section 2 credential bootstrap together with the selected Section 3 modular diagnostic pipeline(s) inside **one single `bash` invocation** (append the Section 3 `pipefail`/env preamble and only the query pipeline(s) needed for your step immediately after the `if ...SPOT_PRODUCER_OK... fi` block so cleanup runs automatically when the diagnostic commands finish).
- **No overwrite of preexisting config**: Verifies that no preexisting `$HOME/.spot_config` (file or symlink) exists and fails closed without modifying it.
- **Exclusive mode-0600 inode reservation**: Created exclusively under `umask 077 && set -C`, with device:inode captured and verified.
- **Automatic cleanup & token-redacted error classification**: An `EXIT` trap removes the ephemeral configuration and error log, protected from terminating signals during cleanup (`trap '' HUP INT TERM`), classifies non-empty `$SPOT_ERR_LOG` on non-zero exit using allowlisted error categories without printing raw `stderr` lines or tokens, and emits `cleanup_verified`.

```sh
set -eu
if [ -e "$HOME/.spot_config" ] || [ -L "$HOME/.spot_config" ]; then
  printf "refusing: ~/.spot_config already exists\n" >&2
  exit 1
fi

SPOT_ERR_LOG=""
SPOT_PRODUCER_OK=""
CONFIG_INODE=""
CONFIG_OWNED=0
cleanup_spot_config() {
  EXIT_STATUS=$?
  trap '' HUP INT TERM
  if [ "${CONFIG_OWNED:-0}" -eq 1 ]; then
    if [ -e "$HOME/.spot_config" ] || [ -L "$HOME/.spot_config" ]; then
      CURRENT_INODE="$(stat -c '%d:%i' "$HOME/.spot_config" 2>/dev/null || stat -f '%d:%i' "$HOME/.spot_config" 2>/dev/null || true)"
      if [ -z "$CONFIG_INODE" ] || { [ -n "$CURRENT_INODE" ] && [ "$CURRENT_INODE" = "$CONFIG_INODE" ]; }; then
        rm -f "$HOME/.spot_config" 2>/dev/null || true
      fi
    fi
    if [ ! -e "$HOME/.spot_config" ] && [ ! -L "$HOME/.spot_config" ]; then
      printf 'cleanup_verified: ~/.spot_config removed (inode %s)\n' "${CONFIG_INODE:-unrecorded}" >&2
    else
      printf 'cleanup_error: ~/.spot_config still present\n' >&2
      [ "$EXIT_STATUS" -ne 0 ] || EXIT_STATUS=1
    fi
  fi
  if [ "$EXIT_STATUS" -ne 0 ] && [ -n "${SPOT_ERR_LOG:-}" ] && [ -s "$SPOT_ERR_LOG" ]; then
    SPOT_ERR_CLASSES=""
    grep -Eqi 'refresh token is required|missing refresh' "$SPOT_ERR_LOG" 2>/dev/null && SPOT_ERR_CLASSES="${SPOT_ERR_CLASSES:+$SPOT_ERR_CLASSES,}missing_refresh_token" || true
    grep -Eqi '(^|[^0-9])401([^0-9]|$)|unauthorized|invalid_grant' "$SPOT_ERR_LOG" 2>/dev/null && SPOT_ERR_CLASSES="${SPOT_ERR_CLASSES:+$SPOT_ERR_CLASSES,}http_401_unauthorized" || true
    grep -Eqi '(^|[^0-9])403([^0-9]|$)|forbidden' "$SPOT_ERR_LOG" 2>/dev/null && SPOT_ERR_CLASSES="${SPOT_ERR_CLASSES:+$SPOT_ERR_CLASSES,}http_403_forbidden" || true
    grep -Eqi '(^|[^0-9])404([^0-9]|$)|not found' "$SPOT_ERR_LOG" 2>/dev/null && SPOT_ERR_CLASSES="${SPOT_ERR_CLASSES:+$SPOT_ERR_CLASSES,}http_404_not_found" || true
    grep -Eqi '(^|[^0-9])429([^0-9]|$)|rate limit|too many requests' "$SPOT_ERR_LOG" 2>/dev/null && SPOT_ERR_CLASSES="${SPOT_ERR_CLASSES:+$SPOT_ERR_CLASSES,}http_429_rate_limited" || true
    grep -Eqi '(^|[^0-9])50[0-9]([^0-9]|$)|internal server error|bad gateway|service unavailable' "$SPOT_ERR_LOG" 2>/dev/null && SPOT_ERR_CLASSES="${SPOT_ERR_CLASSES:+$SPOT_ERR_CLASSES,}http_5xx_upstream" || true
    printf 'spot_diagnostic_error: exit=%s classes=%s\n' "$EXIT_STATUS" "${SPOT_ERR_CLASSES:-unclassified_stderr}" >&2
  fi
  rm -f "${SPOT_PRODUCER_OK:-}" 2>/dev/null || true
  SPOT_PRODUCER_OK=""
  rm -f "${SPOT_ERR_LOG:-}" 2>/dev/null || true
  SPOT_ERR_LOG=""
  exit "$EXIT_STATUS"
}
trap cleanup_spot_config EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

SPOT_ERR_LOG="$(umask 077 && mktemp)" || exit 1

# Protect exclusive creation + inode capture from mid-sequence interrupts
trap '' HUP INT TERM
if ( umask 077 && set -C && : > "$HOME/.spot_config" ); then
  CONFIG_OWNED=1
else
  printf "refusing: exclusive ~/.spot_config creation failed\n" >&2
  exit 1
fi
CONFIG_INODE="$(stat -c '%d:%i' "$HOME/.spot_config" 2>/dev/null || stat -f '%d:%i' "$HOME/.spot_config" 2>/dev/null || true)"
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -z "$CONFIG_INODE" ]; then
  printf "error: failed to capture ~/.spot_config inode\n" >&2
  exit 1
fi

SPOT_PRODUCER_OK="$(umask 077 && mktemp)" || exit 1

if (
  # Example secret-store producer; any secret CLI writing the single-line refresh token to stdout works here.
  if gcloud secrets versions access "<PINNED_SECRET_VERSION>" \
    --secret="<SPOT_REFRESH_TOKEN_SECRET>" \
    --project="<PROJECT_ID>" 2>>"$SPOT_ERR_LOG"; then
    printf 'ok\n' > "$SPOT_PRODUCER_OK"
  fi
) | (
  umask 077
  jq -Rs \
    '(sub("[\r\n]+$"; "")) as $tok
     | if ($tok | length) == 0 or ($tok | test("[\u0000-\u001f\u007f]")) then
         ("error: invalid or multiline secret payload\n" | halt_error(1))
       else
         {org: "", refreshToken: $tok, accessToken: "", region: ""}
       end' > "$HOME/.spot_config"
) && [ -s "$SPOT_PRODUCER_OK" ]; then
  rm -f "$SPOT_PRODUCER_OK" 2>/dev/null || true
  SPOT_PRODUCER_OK=""
else
  rm -f "${SPOT_PRODUCER_OK:-}" 2>/dev/null || true
  SPOT_PRODUCER_OK=""
  printf 'error: secret producer or serializer failed\n' >&2
  exit 1
fi

# Run selected Section 3 diagnostic pipeline(s) here in this same bash invocation before EXIT runs cleanup_spot_config.
```

## 3. Read-Only Cloudspace Diagnosis & Whitelisted Output

Diagnostic operations must strictly query existing state without mutating
resources. Run diagnostic pipelines in `bash` (or a POSIX shell supporting
`pipefail`), and guard pipelines with a fail-closed pipefail check that aborts if run under a shell lacking pipefail.
Every command must append `stderr` to a private log file (`2>>"$SPOT_ERR_LOG"`),
require the verified absolute executable `"$SPOTCTL_BIN"`, and pipe `stdout`
through an allowlisted `jq` projection before saving or displaying.

### Exact Verified CLI Commands & Allowlisted Projections
Select only the pipeline(s) needed for the current step and append them (after the `pipefail` + environment preamble below) inside the single Section 2 `bash` invocation. Run `#1` alone first when discovering `SPOT_ORG_NAME` before setting `SPOT_ORG_NAME`, `SPOT_CLOUDSPACE_NAME`, `SPOT_SERVERCLASS_NAME`, or `SPOT_REGION_NAME` for commands `#2`–`#6`:

```sh
set -eu
(set -o pipefail) 2>/dev/null || {
  printf "error: pipefail is required; run diagnostic pipelines with bash\n" >&2
  exit 1
}
set -o pipefail

# Absolute path to verified spotctl binary (in a separate shell after Section 1, export SPOTCTL_BIN="$HOME/.local/bin/spotctl")
: "${SPOTCTL_BIN:?export SPOTCTL_BIN to the Section 1 verified binary before running}"

# Require private log destination established by Section 2 credential bootstrap
: "${SPOT_ERR_LOG:?run Section 3 in the same bash invocation as the Section 2 credential bootstrap}"

# 1. Organization discovery (requires no org/region bound in ~/.spot_config; handles Go nil-slice null)
"$SPOTCTL_BIN" organizations list --output json 2>>"$SPOT_ERR_LOG" |
  jq '(. // []) | map({name,id})'

# 2. Cloudspace list for an explicitly bound organization
# (retains default JSON output; handles Go nil-slice .cloudspaces=null)
: "${SPOT_ORG_NAME:?set SPOT_ORG_NAME (discover via command #1 organizations list)}"

"$SPOTCTL_BIN" cloudspaces list --org "$SPOT_ORG_NAME" 2>>"$SPOT_ERR_LOG" |
  jq '{cloudspaces: [(.cloudspaces // [])[] | {name,org,region,status}]}'

# 3. Cloudspace detail inspection
: "${SPOT_CLOUDSPACE_NAME:?set SPOT_CLOUDSPACE_NAME}"

"$SPOTCTL_BIN" cloudspaces get --name "$SPOT_CLOUDSPACE_NAME" \
  --org "$SPOT_ORG_NAME" --output json 2>>"$SPOT_ERR_LOG" |
  jq '{name,org,region,creationTimestamp,kubernetesVersion,status,
       assignedServerCount: ((.assignedServers // {}) | length),
       assignedServers: [(.assignedServers // {} | .[]) |
         {clusterRole,serverClassName,state}]}'

# 4. Spot node pool list and bid inspection
# (prefer list --cloudspace --org because 'nodepools spot get --name' does not register --org in v0.2.0;
# normalize omitted zero desired/wonCount and null empty list; preserve missing/null bidPrice as null)
"$SPOTCTL_BIN" nodepools spot list --cloudspace "$SPOT_CLOUDSPACE_NAME" \
  --org "$SPOT_ORG_NAME" --output json 2>>"$SPOT_ERR_LOG" |
  jq '(. // []) | map({name,org,cloudspace,serverClass,
       desired: (.desired // 0),wonCount: (.wonCount // 0),
       bidPrice: (.bidPrice // null),status})'

# 5. Server class market pricing inspection
# (preserve missing/null currentMarketPrice as null, never zero)
: "${SPOT_SERVERCLASS_NAME:?set SPOT_SERVERCLASS_NAME}"
"$SPOTCTL_BIN" pricing get --serverclass "$SPOT_SERVERCLASS_NAME" \
  --output json 2>>"$SPOT_ERR_LOG" |
  jq '{serverClassName,displayName,category,region,
       currentMarketPrice: (.currentMarketPrice // null),cpu,memory}'

# 6. Region & server class detail inspection (--name is required; positional args fail)
: "${SPOT_REGION_NAME:?set SPOT_REGION_NAME}"
"$SPOTCTL_BIN" regions get --name "$SPOT_REGION_NAME" --output json 2>>"$SPOT_ERR_LOG" |
  jq '{name,description}'

"$SPOTCTL_BIN" serverclasses get --name "$SPOT_SERVERCLASS_NAME" --output json 2>>"$SPOT_ERR_LOG" |
  jq '{name,displayName,category,region,onDemandPricePerHour: (.onDemandPricePerHour // null),resources,availability}'
```

### CLI Flags & `spot-go-sdk v0.2.0` (`ef90b998c8d8773e0807517c51f918a7d78f6dba`) Quirks
- Read-only CLI entrypoints: `spotctl organizations list`, `spotctl cloudspaces list`, `spotctl cloudspaces get --name`, `spotctl nodepools spot list --cloudspace`, `spotctl pricing get --serverclass`, `spotctl regions get --name`, `spotctl serverclasses get --name`.
- `spotctl regions get --name` and `spotctl serverclasses get --name`: require `--name`; positional names fail. `Region` omits `country`/`provider`; `ServerClass` omits `flavorType`/`onDemandPricing` (`api/v1/types.go`, `api/v1/regions.go`, `api/v1/serverclasses.go`).
- `spotctl nodepools spot get` (`spotctl v0.2.0` `cmd/nodepools.go#L87-L88`): omits `--org` in Cobra flag registration (falls back to `~/.spot_config`); prefer `spotctl nodepools spot list --cloudspace "<CLOUDSPACE>" --org "<ORG>"` (`SpotNodePool` struct tags in `spot-go-sdk` `api/v1/types.go`).
- `PriceDetails` struct tags (`api/v1/types.go#L126-L134`, `api/v1/pricing.go#L52-L68`): `serverClassName`, `displayName`, `category`, `region`, `currentMarketPrice`, `cpu`, `memory`.

### Redaction & Excluded Side-Effect Operations
- `cloudspaces get`/`list` emit no tokens or certificates, but `--output json/table/yaml` does **not** redact internal operational fields (`preEmptionWebhookURL`, `apiServerEndpoint`, assigned-server `IP`, free-text `message`/annotations) or raw HTTP API errors on `stderr`. Always pipe stdout through the allowlisted `jq` projections above, capture `stderr` in `$SPOT_ERR_LOG`, and inspect only the redacted `spot_diagnostic_error` classes emitted by `cleanup_spot_config`.
- **Exclude `spotctl cloudspaces get-config`** (`spotctl v0.2.0` `cmd/cloudspaces.go#L103, #L486-L501`): it calls `GetCloudspaceConfig` and ignores `--help`'s filename wording (`Output file name (default: <cloudspace_name>.yaml)`), writing kubeconfig credentials with mode `0644` to `filepath.Join(os.Getenv("HOME"), ".kube", name+".yaml")` by default or `fileName + "/" + name + ".yaml"` when `--file` is supplied (treating `--file` as a directory prefix). Output redirection does not suppress those side effects; any separately authorized recovery use requires a mode-`0700` directory deleted on completion.

## 4. Diagnostic Interpretation & SDK Fallback

### Diagnostic Interpretation Rules
1. **SDK `Autoscaling` struct omission**: In `spot-go-sdk v0.2.0`
   (`api/v1/spotnodepool.go#L43-L57`), both `ListSpotNodePools` and `GetSpotNodePool`
   omit assignment of the returned `Autoscaling` block, returning `false`/`0`
   defaults regardless of live autoscaling state. Obtain autoscaling config from
   OpenTofu IaC, the provider console, or an authorized raw official HTTP API response.
2. **Provider state vs. in-cluster readiness**: A `wonCount > 0` or `configured`/`running`
   server state does not guarantee Kubernetes `Ready`, ARC registration, or job pickup.
   Correlate with in-cluster `kubectl` checks (using your cluster's workload namespace, e.g. `-n arc-runners` for Actions Runner Controller pools):
   ```console
   kubectl get nodes -o wide
   kubectl get pods -n arc-runners -o wide
   kubectl get autoscalinglisteners -A
   ```
   Separate unassigned jobs and terminal job cancellations from substrate bid evictions.
3. **Currency prefix & `NOT_CAPTURED` price rule**: `spot-go-sdk v0.2.0` prefixes
   `bidPrice` and `currentMarketPrice` strings with `$` (`$0.0800`, `$0.0450`) or
   emits `"N/A"`. Never reject `$`, and always classify `null` or `"N/A"` price
   projections as **`NOT_CAPTURED` / unknown**, never `0`.

### Go SDK Fallback (`spot-go-sdk`)
When typed programmatic telemetry is needed, pin `github.com/rackspace-spot/spot-go-sdk v0.2.0`
(`import v1 "github.com/rackspace-spot/spot-go-sdk/api/v1"`):
- Supply credentials via protected `stdin` directly into process memory and load them into the Go client configuration (or parse the mode-`0600` ephemeral `~/.spot_config` explicitly in Go — `spot-go-sdk` does not auto-load `~/.spot_config`); never use exported env vars (`SPOT_REFRESH_TOKEN`, `SPOT_ACCESS_TOKEN`) or `argv`.
- Project structs before logging and remember the same `Autoscaling` mapping omission applies.

## 5. Repairs As IaC (OpenTofu Boundary)

`spotctl` and `spot-go-sdk` diagnostic commands are strictly **read-only**; never run imperative `create`/`update`/`delete` during diagnosis. Route bid adjustments, `SpotNodePool` scaling, server-class changes, or cloudspace recreation through declarative OpenTofu changes: formulate the diff, review `tofu plan -out=tfplan`, and obtain separate explicit live-apply authority.

## 6. Realistic Negative Cases & Guardrails

1. **Mismatched Organization or Region**: Empty `cloudspaces list` or 404 on `get` often means wrong org/region rather than deletion. Run Section 3 command #1 (`organizations list`, `jq '(. // []) | map({name,id})'`), bind `SPOT_ORG_NAME`, and verify commands #2–#4 (`cloudspaces list`/`get`, `nodepools spot list`) and #6 (`regions get`) before reporting absence.
2. **Missing or Expired Credentials**: Fail closed on `refresh token is required` or 401 Unauthorized; record missing/expired reference and never scrape raw keys or attempt bypasses.
3. **Sensitive Command Output & Credential Generation**: Enforce Section 3 `pipefail` + `jq` projections + `$SPOT_ERR_LOG` redaction for raw output/`stderr`, and reject unapproved `cloudspaces get-config` per Section 3.
4. **Imperative Bid or Pool Mutation Without Authority**: Refuse `spotctl nodepools spot update` or other imperative mutations and route changes through Section 5 OpenTofu `tofu plan`.
