# Repository Maintenance Supervisor

An autonomous maintenance agent that continuously audits and repairs a
configured GitHub repository. It uses the **Codex CLI** as its AI engine for
all analysis and code changes while owning the state machine, finding queue,
deduplication, and stopping logic itself.

Focus: correctness, security, reliability, tests, documentation accuracy, dead
code, and maintainability. No new features, speculative redesigns, or cosmetic
changes without concrete justification.

---

## Prerequisites

| Tool | Install |
|------|---------|
| Python ≥ 3.11 | system package manager |
| [Codex CLI](https://github.com/openai/codex) | `npm install -g @openai/codex` |
| [GitHub CLI](https://cli.github.com) | `brew install gh` or `apt install gh` |
| Git | already present on most systems |

Authenticate both CLIs before running:

```bash
codex login          # or set OPENAI_API_KEY
gh auth login
```

A local clone of the target repository is **optional**: if `repo.path` is left
empty Maintain clones and reuses it automatically (see
[Managed clone](#managed-clone) below).

---

## Quick start

```bash
# 1. Clone or navigate to this repo.
cd /path/to/supervisor

# 2. Copy the example config and fill in your repo details.
cp config.toml config.local.toml
$EDITOR config.local.toml   # set repo.owner and repo.name at minimum

# 3. Run a single maintenance iteration.
#    Maintain clones the target repository automatically on first run.
python maintain.py --config config.local.toml run

# 4. (Optional) Run continuously until the repository is exhausted.
python maintain.py --config config.local.toml run-continuous
```

The SQLite state database (`.maintain.db` by default) is created automatically
on first run and accumulates findings, audit history, and PR records across
runs.

### Managed clone

When `repo.path` is empty (the default), Maintain creates and reuses a clone
at `~/.maintain/workspaces/<owner>/<name>`:

* **First run** — the repository is cloned automatically before the audit
  starts; no manual `git clone` step required.
* **Subsequent runs** — the existing clone is reused.  The audit worktree
  always fetches `origin/<default_branch>` fail-closed before auditing, so
  Maintain always operates from the authoritative remote state.
* **Explicit path** — set `repo.path` to an existing local clone to use that
  checkout instead.  Maintain will never auto-clone into, replace, or delete
  an explicitly configured path; it still fetches and creates temporary git
  worktrees from it during normal operation.

The workspace root is configurable via `repo.workspace`:

```toml
[repo]
owner     = "my-company"
name      = "my-repo"
# workspace = "/data/maintain-workspaces"   # optional; default: ~/.maintain/workspaces
```

Maintain fails closed if the managed path exists but is not a valid git
repository, or if its remote does not match `repo.owner`/`repo.name`.

---

## Commands

```
python maintain.py run                 # one full pass over all audit areas
python maintain.py run-continuous      # loop until exhausted or budget exceeded
python maintain.py status              # show queue, recent PRs, area exhaustion
python maintain.py findings            # list open/paused findings (prioritised)
python maintain.py audit <area>        # manually trigger one area's audit
python maintain.py reset               # clear all state (confirms before deleting)
```

Global flags accepted before the command:

```
--config PATH      config file (default: config.toml)
--db PATH          SQLite database (default: .maintain.db)
--log-level LEVEL  DEBUG | INFO | WARNING | ERROR  (default: INFO)
```

---

## Lifecycle

```
┌─────────────────┐
│ startup_reconcile│  On every start: reconcile in_progress findings against
└──────┬──────────┘  GitHub state; close deferred PRs and requeue findings.
       │
┌──────▼──────────┐
│  fetch + audit  │  Fetch origin/<main> fail-closed; create a read-only
│    worktree     │  git worktree at that exact SHA. All audits run there.
└──────┬──────────┘
       │
┌──────▼──────┐
│ Scoped audit │  Codex inspects one area deeply and returns all findings.
└──────┬──────┘  Findings are fingerprinted and deduplicated in the DB.
       │
┌──────▼──────┐
│  Revalidate  │  Before repair, check queued findings against current HEAD.
└──────┬──────┘  Stale findings (file deleted, issue gone) are discarded.
       │
┌──────▼──────┐
│Select finding│  Highest-severity/confidence open finding is selected.
└──────┬──────┘
       │
┌──────▼──────┐
│    Repair   │  Fresh Codex context applies the minimal fix on a new branch
└──────┬──────┘  in an isolated worktree (main checkout is never modified).
       │
┌──────▼──────┐
│    Verify   │  Optional test suite + fresh Codex diff-review.
└──────┬──────┘  Reject → discard branch, re-queue finding.
       │
┌──────▼──────┐
│  PR + review│  Push branch, open PR, fresh Codex reviews the diff.
│    loop     │  Blocking comments → implement, commit, run tests, diff-verify,
└──────┬──────┘  then review again.  Up to max_review_rounds times.
       │         Budget exhausted → defer to next run (close PR, requeue).
       │         Rounds exhausted → close PR, mark finding "blocked".
┌──────▼──────┐
│   CI gate   │  Wait for GitHub CI (configurable timeout).
└──────┬──────┘  Only an explicit success permits merge.
       │         Failure/timeout/no-CI → mark PR failed, re-queue finding.
┌──────▼──────┐
│    Merge    │  gh pr merge --squash --delete-branch
└──────┬──────┘
       │
       └─► Restart sweep from freshly fetched origin/<main>.
           After all areas: re-audit to catch regressions.
           After max_audits_per_area consecutive clean audits per area
           at the same HEAD → area exhausted.
           All areas exhausted, no blocked findings → EXHAUSTED (clean).
           All areas exhausted, blocked findings remain → BLOCKED.
           Budget limit reached → BUDGET (restart next run).
```

---

## Configuration reference

### `[repo]`

| Key | Description |
|-----|-------------|
| `owner` | GitHub owner / org (e.g. `my-company`) |
| `name` | Repository name without the owner prefix |
| `path` | Absolute (or relative to cwd) path to an existing local clone. **Optional** — leave empty to use the managed clone under `workspace`. When set, the directory must exist and its `origin` remote must match `owner`/`name`. |
| `workspace` | Root directory for Maintain-managed clones. Each repository is stored at `<workspace>/<owner>/<name>`. Default: `~/.maintain/workspaces`. Ignored when `path` is set explicitly. |
| `default_branch` | Trunk branch; PRs are opened against it (default: `main`) |
| `commit_trailer` | Optional one-line trailer appended to every automated commit message (default: empty — no trailer added) |
| `pr_footer` | Optional text appended to every automated PR body (default: empty) |

### `[codex]`

| Key | Description |
|-----|-------------|
| `cmd` | Codex binary name (default: `codex`) |
| `model` | Default model identifier passed via `--model` (default: `o4-mini`) |
| `timeout` | Seconds before a Codex invocation is killed (default: `300`) |
| `audit_flags` | Flags for read-only calls — analysis, diff-review, revalidation. Default: `["exec"]` |
| `repair_flags` | Flags for file-editing calls — repair, implement review feedback. Default: `["exec", "--sandbox", "workspace-write"]` |
| `audit_model` | Model for the scoped audit stage. Falls back to `model` when absent. |
| `revalidate_model` | Model for the stale-check stage (run before repair when the finding HEAD differs). Falls back to `model`. |
| `validate_model` | Model for the finding-validation stage (confirms a finding before repair). Falls back to `model`. |
| `repair_model` | Model for the repair stage (applies the code fix). Falls back to `model`. |
| `verify_model` | Model for the diff-review stage (run after repair, before opening a PR). Falls back to `model`. |
| `review_model` | Model for the PR-review stage, including implementing blocking feedback. Falls back to `model`. |

`--model` is inserted automatically after the first element of `audit_flags` /
`repair_flags` when that element is the `exec` subcommand.

Configs that only set `model` continue to work unchanged — every stage falls
back to `model` when its stage-specific key is absent.

### `[verify]`

| Key | Description |
|-----|-------------|
| `setup_cmd` | Shell command run inside each newly created repair worktree before Codex attempts a fix. Use it to install dependencies or otherwise prepare the environment. Leave empty to skip (the default). A non-zero exit aborts the run; see [Worktree setup](#worktree-setup) below. |
| `test_cmd` | Shell command run inside the worktree before opening a PR. Non-zero exit discards the fix and requeues the finding. Leave empty to skip. |
| `ci_wait_timeout` | Seconds to wait for GitHub CI after the PR is pushed. `0` skips the CI wait (implies `allow_no_ci`). |
| `allow_no_ci` | Set to `true` only for repos that genuinely have no CI. When `false` (the default), the supervisor blocks merges when no CI checks are found, when the GitHub API returns an error, or when the wait times out. Only an explicit **success** status permits a merge. |

### Worktree setup

When Maintain creates a repair worktree it contains a bare checkout of the
repository. If your project needs compiled dependencies, generated files, or
a specific set of CLI tools, set `verify.setup_cmd` to install them before
Codex runs.

`setup_cmd` is completely generic — use whatever command your project requires:

```toml
# Node.js
setup_cmd = "npm ci"

# Python
setup_cmd = "pip install -e .[dev]"

# Custom bootstrap script
setup_cmd = "./scripts/bootstrap.sh"
```

**Recommended pattern — [Mise](https://mise.jdx.dev)**

Mise manages per-project tool versions and tasks. With a `mise.toml` checked
into your repository, a single `mise install` restores the exact tool versions
(Node, Go, Python, Rust, …) each worktree needs:

```toml
[verify]
setup_cmd = "mise install"
test_cmd  = "mise run verify"
```

This makes every worktree reproducible regardless of what is installed
system-wide on the machine running Maintain. Mise is only a recommendation;
`npm ci`, a bootstrap script, direct package installation, or any other
command works equally well.

**Failure semantics**

A non-zero exit from `setup_cmd`, or a setup command that leaves the worktree
dirty (modified tracked files or new untracked non-ignored files), is treated
as an infrastructure failure, not a finding defect:

* `run_once` returns `"setup_error"` and **`run-continuous` stops**.
* The finding is **requeued** as open.
* Its **repair-attempt count is not incremented**.
* The failure command and output are **logged** at ERROR level.

The worktree cleanliness check exists because `phase_repair` commits with
`git add -A`. Any files setup creates that are not covered by `.gitignore`
would contaminate the patch. Add build artifacts, caches, and installed
packages to `.gitignore` as you would for any other commit.

### `[budget]`

| Key | Default | Description |
|-----|---------|-------------|
| `max_audits_per_area` | 3 | Consecutive clean audits (at the same HEAD) before an area is exhausted |
| `max_fixes_per_run` | 20 | Max findings fixed per invocation of `run` |
| `max_review_rounds` | 4 | Max review→fix cycles per PR |
| `max_consecutive_failures` | 5 | Abort run after this many back-to-back failures |
| `codex_call_budget` | 100 | Total Codex invocations allowed per run |

### `[[audit_areas]]`

Each block defines one scoped audit. The `description` is injected verbatim
into the Codex audit prompt, so be specific about what to look for.

The nine default areas (correctness, security, reliability, tests, persistence,
api\_contracts, dependencies, maintainability, documentation) are compiled into
`maintain.py` and used when no `[[audit_areas]]` entries appear in the config
file. Adding even one `[[audit_areas]]` block in `config.toml` **replaces** all
defaults, so copy all nine if you only want to add one.

---

## State database schema

The supervisor stores everything in a local SQLite file (`.maintain.db`).

| Table | Contents |
|-------|----------|
| `findings` | Every finding ever discovered: fingerprint, area, severity, confidence, file, status, linked PR |
| `audit_runs` | Log of every Codex audit call: area, commit hash, new/total findings, status |
| `prs` | PR metadata: branch, PR number, URL, review rounds, merged timestamp |
| `sweeps` | Completed full-sweep records (used for exhaustion tracking) |
| `kv` | Freeform key/value supervisor state |

Findings are deduplicated by a SHA-256 fingerprint of `area + file_path + title`
(first 20 hex chars). Re-running an audit that returns the same finding does not
create a duplicate row.

Finding statuses:

| Status | Meaning |
|--------|---------|
| `open` | Awaiting a fix |
| `in_progress` | Fix is being prepared or is in an open PR |
| `fixed` | PR merged successfully |
| `rejected` | Discarded (repair produced no changes, or manually rejected) |
| `stale` | Revalidation determined the finding no longer applies to current HEAD |
| `deferred` | Per-run Codex budget hit during review; PR closed, retried next run |
| `blocked` | Review-round budget exhausted without approval; requires human review |

PR statuses:

| Status | Meaning |
|--------|---------|
| `open` | PR is open and being reviewed |
| `deferred` | Codex budget hit; will be closed and retried next run |
| `merged` | Successfully merged |
| `closed` | Closed (clean retry or deferred cleanup) |
| `failed` | CI failed or other unrecoverable error |

---

## Exhaustion and HEAD tracking

Clean-audit exhaustion is **tied to the HEAD commit** the audit ran against.

When a PR is merged, the supervisor **immediately restarts the sweep** from a
freshly fetched `origin/<default_branch>`. This guarantees that all subsequent
audits and revalidations see the merged code — no finding is ever evaluated
against stale code from before the merge.

An area's streak resets to zero if the most-recent audit was at an older commit,
ensuring merged code is always re-audited before any area is declared exhausted.

`run_once()` stops and reports one of three terminal states:

| State | Meaning |
|-------|---------|
| `exhausted` | Every area is clean at the current HEAD; no unresolved findings |
| `blocked` | Every area is clean, but one or more findings could not be resolved autonomously and require human review |
| `budget` | The run stopped early because a Codex, fix, or failure budget was reached; re-running will continue from the queued state |

---

## Crash recovery and worktrees

All repair and fix work happens in **isolated git worktrees** — the configured
repository checkout is never modified directly. If the supervisor is killed
mid-repair, the main checkout is always left clean.

On the next run, `startup_reconcile()` scans for findings left in `in_progress`
or `deferred` state and reconciles them against GitHub:

- **in_progress, PR merged** → mark finding `fixed`.
- **in_progress, PR closed** → requeue finding as `open` for a clean retry.
- **in_progress, PR open** → close the stale PR and requeue (the next run will
  produce a fresh branch and PR from current HEAD).
- **in_progress, GitHub API error** → leave state untouched; retry next run
  (fail-closed — never misclassify an API failure as a known state).
- **deferred** → close the existing PR and requeue as `open` so the next run
  produces a fresh branch with the full review-round budget.

Audits always run against a freshly-fetched read-only worktree at exactly
`origin/<default_branch>` — stale or wrong-branch local checkouts never
influence what the supervisor sees.

---

## Finding revalidation

Before a repair is attempted, any queued finding that was discovered at a
different HEAD than the current one is **revalidated**:

1. If the finding's file no longer exists → **stale**, discarded.
2. Codex is asked whether the issue still applies to the current state of the
   file → if not → **stale**, discarded.
3. On revalidation error → finding is skipped for this run (conservative).

This prevents the supervisor from attempting to fix issues that an earlier PR
already resolved or that no longer exist.

---

## Safety guarantees

* **Fail-closed throughout** — Codex errors, invalid JSON, missing CI checks,
  and timed-out CI waits all block action; they never constitute approval.
* **Never weakens tests or CI** — if `test_cmd` fails, the fix is discarded.
* **Setup failures abort without penalty** — if `setup_cmd` exits non-zero the run aborts, the finding is requeued, and its repair-attempt count is not incremented.
* **Never merges with failing CI** — only an explicit GitHub "success" result
  permits a merge (or `allow_no_ci = true` with no checks present).
* **Every fix is on its own branch and PR** — no direct pushes to the trunk.
* **Unrelated findings stay separate** — the repair prompt instructs Codex to
  make the minimal targeted change.
* **Hard budget limits** — `codex_call_budget`, `max_fixes_per_run`, and
  `max_consecutive_failures` prevent runaway billing or infinite loops.
* **Review-budget exhaustion ≠ approval** — when `max_review_rounds` is
  exhausted with unresolved comments, the PR is **closed** and the finding is
  marked `blocked`, not merged.
* **Exhaustion detection** — the supervisor stops when every audit area produces
  no new findings for `max_audits_per_area` consecutive runs at the current HEAD.
* **Confirmations for destructive CLI operations** — `reset` asks before
  deleting state.
* **No hardcoded attribution** — commit trailers and PR footers are opt-in via
  `repo.commit_trailer` and `repo.pr_footer`; nothing is added by default.
* **Config parse errors are fatal** — a malformed `config.toml` causes an
  immediate exit rather than silently continuing with defaults.

---

## Running continuously (e.g. as a cron job)

```bash
# Run every night at 02:00
0 2 * * * cd /path/to/supervisor && \
    python maintain.py --config config.local.toml run \
    >> /var/log/maintain.log 2>&1
```

Or keep it running in the foreground until the repo is exhausted:

```bash
python maintain.py --config config.local.toml \
    run-continuous --max-iterations 200
```

---

## Extending audit areas

Add a section to your config for a custom area:

```toml
[[audit_areas]]
name        = "observability"
description = """
Missing structured logging for error paths, metrics without units,
traces that lose context across async boundaries, log statements
that expose sensitive data.
"""
```

The `description` drives what Codex looks for, so investing in a precise,
concrete description yields better findings.
