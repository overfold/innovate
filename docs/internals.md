# Internals

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

## State database

Everything is stored in a local SQLite file (`.maintain.db`).

| Table | Contents |
|-------|----------|
| `findings` | Every finding ever discovered: fingerprint, area, severity, confidence, file, status, linked PR |
| `audit_runs` | Log of every Codex audit call: area, commit hash, new/total findings, status |
| `prs` | PR metadata: branch, PR number, URL, review rounds, merged timestamp |
| `sweeps` | Completed full-sweep records (used for exhaustion tracking) |
| `kv` | Freeform key/value supervisor state |

Findings are deduplicated by a SHA-256 fingerprint of `area + file_path + title` (first 20 hex chars). Re-running an audit that returns the same finding does not create a duplicate row.

**Finding statuses**

| Status | Meaning |
|--------|---------|
| `open` | Awaiting a fix |
| `in_progress` | Fix is being prepared or is in an open PR |
| `fixed` | PR merged successfully |
| `rejected` | Discarded (repair produced no changes, or manually rejected) |
| `stale` | Revalidation determined the finding no longer applies to current HEAD |
| `deferred` | Per-run Codex budget hit during review; PR closed, retried next run |
| `blocked` | Review-round budget exhausted without approval; requires human review |

**PR statuses**

| Status | Meaning |
|--------|---------|
| `open` | PR is open and being reviewed |
| `deferred` | Codex budget hit; will be closed and retried next run |
| `merged` | Successfully merged |
| `closed` | Closed (clean retry or deferred cleanup) |
| `failed` | CI failed or other unrecoverable error |

---

## Exhaustion and HEAD tracking

Clean-audit exhaustion is tied to the HEAD commit the audit ran against.

When a PR is merged, maintain immediately restarts the sweep from a freshly fetched `origin/<default_branch>`. An area's streak resets to zero if the most-recent audit was at an older commit, ensuring merged code is always re-audited before any area is declared exhausted.

`run_once()` stops and reports one of three terminal states:

| State | Meaning |
|-------|---------|
| `exhausted` | Every area is clean at the current HEAD; no unresolved findings |
| `blocked` | Every area is clean, but one or more findings could not be resolved autonomously and require human review |
| `budget` | The run stopped early because a Codex, fix, or failure budget was reached; re-running will continue from the queued state |

---

## Crash recovery and worktrees

All repair work happens in isolated git worktrees — the configured repository checkout is never modified directly. If maintain is killed mid-repair, the main checkout is always left clean.

On the next run, `startup_reconcile()` scans for findings left in `in_progress` or `deferred` state and reconciles them against GitHub:

- **in_progress, PR merged** → mark finding `fixed`.
- **in_progress, PR closed** → requeue finding as `open` for a clean retry.
- **in_progress, PR open** → close the stale PR and requeue (the next run produces a fresh branch and PR from current HEAD).
- **in_progress, GitHub API error** → leave state untouched; retry next run (fail-closed — never misclassify an API failure as a known state).
- **deferred** → close the existing PR and requeue as `open` so the next run produces a fresh branch with the full review-round budget.

Audits always run against a freshly-fetched read-only worktree at exactly `origin/<default_branch>`.

---

## Finding revalidation

Before a repair is attempted, any queued finding that was discovered at a different HEAD than the current one is revalidated:

1. If the finding's file no longer exists → **stale**, discarded.
2. Codex is asked whether the issue still applies to the current state of the file → if not → **stale**, discarded.
3. On revalidation error → finding is skipped for this run (conservative).

This prevents attempting to fix issues that an earlier PR already resolved.

---

## Safety guarantees

- **Fail-closed throughout** — Codex errors, invalid JSON, missing CI checks, and timed-out CI waits all block action; they never constitute approval.
- **Never weakens tests or CI** — if `test_cmd` fails, the fix is discarded.
- **Setup failures abort without penalty** — if `setup_cmd` exits non-zero the run aborts, the finding is requeued, and its repair-attempt count is not incremented.
- **Never merges with failing CI** — only an explicit GitHub "success" result permits a merge (or `allow_no_ci = true` with no checks present).
- **Every fix is on its own branch and PR** — no direct pushes to the trunk.
- **Unrelated findings stay separate** — the repair prompt instructs Codex to make the minimal targeted change.
- **Hard budget limits** — `codex_call_budget`, `max_fixes_per_run`, and `max_consecutive_failures` prevent runaway billing or infinite loops.
- **Review-budget exhaustion ≠ approval** — when `max_review_rounds` is exhausted with unresolved comments, the PR is closed and the finding is marked `blocked`, not merged.
- **Confirmations for destructive CLI operations** — `reset` asks before deleting state.
- **No hardcoded attribution** — commit trailers and PR footers are opt-in via `repo.commit_trailer` and `repo.pr_footer`; nothing is added by default.
- **Config parse errors are fatal** — a malformed `config.toml` causes an immediate exit rather than silently continuing with defaults.

---

## Running continuously

```bash
# Run every night at 02:00
0 2 * * * cd /path/to/maintain && \
    python maintain.py --config config.local.toml run \
    >> /var/log/maintain.log 2>&1
```

Or keep it running in the foreground until the repo is exhausted:

```bash
python maintain.py --config config.local.toml \
    run-continuous --max-iterations 200
```
