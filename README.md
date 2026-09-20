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

---

## Quick start

```bash
# 1. Clone or navigate to this repo.
cd /path/to/supervisor

# 2. Copy the example config and fill in your repo details.
cp config.toml config.local.toml
$EDITOR config.local.toml   # set repo.owner, repo.name, repo.path

# 3. Run a single maintenance iteration.
python maintain.py --config config.local.toml run

# 4. (Optional) Run continuously until the repository is exhausted.
python maintain.py --config config.local.toml run-continuous
```

The SQLite state database (`.maintain.db` by default) is created automatically
on first run and accumulates findings, audit history, and PR records across
runs.

---

## Commands

```
python maintain.py run                 # one full pass over all audit areas
python maintain.py run-continuous      # loop until exhausted or budget exceeded
python maintain.py status              # show queue, recent PRs, area exhaustion
python maintain.py findings            # list open findings (prioritised)
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
┌─────────────┐
│  Scoped audit│  Codex inspects one area deeply and returns all findings.
└──────┬──────┘  Findings are fingerprinted and deduplicated in the DB.
       │
┌──────▼──────┐
│Select finding│  Highest-severity/confidence open finding is selected.
└──────┬──────┘
       │
┌──────▼──────┐
│    Repair   │  Fresh Codex context applies the minimal fix on a new branch.
└──────┬──────┘
       │
┌──────▼──────┐
│    Verify   │  Optional test suite + fresh Codex diff-review.
└──────┬──────┘  Reject → discard branch, re-queue finding.
       │
┌──────▼──────┐
│  PR + review│  Push branch, open PR, fresh Codex reviews the diff.
│    loop     │  Blocking comments → implement, push, re-review.
└──────┬──────┘  Repeat up to max_review_rounds times.
       │
┌──────▼──────┐
│   CI gate   │  Wait for GitHub CI (configurable timeout).
└──────┬──────┘  Failure → mark PR failed, re-queue finding.
       │
┌──────▼──────┐
│    Merge    │  gh pr merge --squash --delete-branch
└──────┬──────┘
       │
       └─► Pull main, move to next finding, then next area.
           After all areas: re-audit to catch regressions.
           After max_audits_per_area consecutive clean audits per area → exhausted.
```

---

## Configuration reference

### `[repo]`

| Key | Description |
|-----|-------------|
| `owner` | GitHub owner / org (e.g. `my-company`) |
| `name` | Repository name without the owner prefix |
| `path` | Absolute (or relative to cwd) path to the local clone |
| `default_branch` | Trunk branch; PRs are opened against it (default: `main`) |

### `[codex]`

| Key | Description |
|-----|-------------|
| `cmd` | Codex binary name (default: `codex`) |
| `model` | Model identifier passed to `--model` (default: `o4-mini`) |
| `timeout` | Seconds before a Codex invocation is killed (default: `300`) |
| `audit_flags` | Flags for read-only calls — analysis, diff-review, verify |
| `repair_flags` | Flags for file-editing calls — repair, implement review feedback |

### `[verify]`

| Key | Description |
|-----|-------------|
| `test_cmd` | Shell command run inside the repo before opening a PR. Non-zero exit discards the fix. Leave empty to skip. |
| `ci_wait_timeout` | Seconds to wait for GitHub CI after the PR is pushed. `0` skips the gate (only for repos without CI). |

### `[budget]`

| Key | Default | Description |
|-----|---------|-------------|
| `max_audits_per_area` | 3 | Consecutive clean audits before an area is exhausted |
| `max_fixes_per_run` | 20 | Max findings fixed per invocation of `run` |
| `max_review_rounds` | 4 | Max review→fix cycles per PR |
| `max_consecutive_failures` | 5 | Abort run after this many back-to-back failures |
| `codex_call_budget` | 100 | Total Codex invocations allowed per run |

### `[[audit_areas]]`

Each block defines one scoped audit. The `description` is injected verbatim
into the Codex audit prompt, so be specific about what to look for.

The eight default areas (correctness, security, reliability, tests, persistence,
api\_contracts, dependencies, maintainability) are compiled into `maintain.py`
and used when no `[[audit_areas]]` entries appear in the config file. Adding
even one `[[audit_areas]]` block in `config.toml` **replaces** all defaults, so
copy all eight if you only want to add one.

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
| `stale` | Marked stale after a re-audit invalidated the assumption (future feature) |

---

## Safety guarantees

* **Never weakens tests or CI** — if `test_cmd` fails, the fix is discarded.
* **Never merges with failing CI** — the supervisor waits for checks and skips
  the merge on failure.
* **Every fix is on its own branch and PR** — no direct pushes to the trunk.
* **Unrelated findings stay separate** — the repair prompt instructs Codex to
  make the minimal targeted change.
* **Hard budget limits** — `codex_call_budget`, `max_fixes_per_run`, and
  `max_consecutive_failures` prevent runaway billing or infinite loops.
* **Exhaustion detection** — the supervisor stops when every audit area produces
  no new findings for `max_audits_per_area` consecutive runs.
* **Confirmations for destructive CLI operations** — `reset` asks before
  deleting state.

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
