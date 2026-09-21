from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import DB
from .git import (
    GitHubAPIError,
    ci_permits_merge,
    create_audit_worktree,
    create_worktree,
    current_commit,
    gh_close_pr,
    gh_create_pr,
    gh_find_pr_by_branch,
    gh_merge_pr,
    gh_pr_base_sha,
    gh_pr_state,
    push_branch,
    remove_audit_worktree,
    remove_worktree,
    wait_for_ci,
)
from .phases import (
    REVIEW_APPROVED,
    REVIEW_DEFERRED_BUDGET,
    REVIEW_FAILED_ERROR,
    REVIEW_PAUSED_BUDGET,
    phase_audit,
    phase_repair,
    phase_revalidate,
    phase_review_loop,
    phase_validate,
    phase_verify,
    run_setup,
)

LOG = logging.getLogger("supervisor")


class WorktreeSetupError(Exception):
    """Raised when setup_cmd fails in a repair worktree.

    Signals an infrastructure failure: the run is aborted but the finding
    is requeued without penalty and its repair-attempt count is not incremented.
    """


class Supervisor:
    def __init__(self, cfg: dict, db: DB) -> None:
        self.cfg = cfg
        self.db = db
        self.ctr: dict[str, int] = {
            "codex_calls": 0,
            "fixes_applied": 0,
            "consecutive_failures": 0,
        }

    def _over_budget(self) -> bool:
        b = self.cfg["budget"]
        if self.ctr["codex_calls"] >= b["codex_call_budget"]:
            LOG.warning("Codex call budget exhausted (%d)", b["codex_call_budget"])
            return True
        if self.ctr["consecutive_failures"] >= b["max_consecutive_failures"]:
            LOG.warning(
                "Stopping: %d consecutive failures (limit %d)",
                self.ctr["consecutive_failures"],
                b["max_consecutive_failures"],
            )
            return True
        if self.ctr["fixes_applied"] >= b["max_fixes_per_run"]:
            LOG.info("Fix budget reached (%d)", b["max_fixes_per_run"])
            return True
        return False

    def _pr_body(self, findings: list) -> str:
        parts = ["## Maintenance fix\n"]
        for f in findings:
            parts.append(f"**[{f['severity'].upper()}] {f['title']}**\n")
            parts.append(f"{f['description']}\n")
            if f.get("file_path"):
                parts.append(
                    f"_File: {f['file_path']} {f.get('line_range') or ''}_\n"
                )
        footer = self.cfg["repo"].get("pr_footer", "")
        if footer:
            parts += ["\n---", footer]
        return "\n".join(parts)

    def startup_reconcile(self) -> None:
        """Reconcile in_progress and deferred findings against GitHub state.

        Called at the start of every run_once() to recover from a previous crash
        or unexpected termination.

        in_progress: reconcile against actual GitHub PR state.
        deferred: Codex budget ran out mid-review — the existing PR is closed so
                  the next run retries from a clean branch.

        GitHub API errors are fail-closed: an unknown/unreachable state is left
        untouched rather than being mis-classified as "closed" or "not found".
        """
        db = self.db
        cfg = self.cfg
        owner = cfg["repo"]["owner"]
        repo_name = cfg["repo"]["name"]

        # ── deferred findings: close stale PR and requeue cleanly ─────────────
        deferred_rows = db.deferred_findings()
        if deferred_rows:
            LOG.info("Requeueing %d deferred finding(s)…", len(deferred_rows))
        for finding in deferred_rows:
            pr_row = db.get_pr(finding["pr_id"]) if finding["pr_id"] else None
            if pr_row and pr_row["pr_number"]:
                LOG.info(
                    "  Closing deferred PR #%d for finding %d (%s)",
                    pr_row["pr_number"], finding["id"], finding["title"],
                )
                try:
                    gh_close_pr(owner, repo_name, pr_row["pr_number"])
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d — leaving deferred: %s",
                        pr_row["pr_number"], exc,
                    )
                    continue
                db.update_pr(pr_row["id"], status="closed")
            db.mark_finding(finding["id"], "open")

        # ── in_progress findings: crash recovery ──────────────────────────────
        rows = db.in_progress_findings()
        if not rows:
            return

        LOG.info("Reconciling %d in_progress finding(s)…", len(rows))
        for finding in rows:
            pr_row = db.get_pr(finding["pr_id"]) if finding["pr_id"] else None

            # Case 1: no PR record at all — finding marked in_progress before
            # the branch was even created. Requeue cleanly.
            if pr_row is None:
                LOG.info(
                    "  Finding %d (%s): no PR record — requeueing",
                    finding["id"], finding["title"],
                )
                db.mark_finding(finding["id"], "open")
                continue

            pr_number = pr_row["pr_number"]

            # Case 2: PR record exists but no pr_number — crash between
            # gh pr create and the DB update. Search GitHub by branch.
            if not pr_number:
                branch = pr_row["branch"]
                LOG.info(
                    "  Finding %d (%s): PR record has no number"
                    " — searching GitHub for branch %s",
                    finding["id"], finding["title"], branch,
                )
                try:
                    found = gh_find_pr_by_branch(owner, repo_name, branch)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  GitHub API error searching for branch %s — leaving"
                        " state untouched: %s", branch, exc,
                    )
                    continue  # fail-closed: do not requeue or create duplicate
                if found:
                    pr_number, pr_url = found
                    db.update_pr(pr_row["id"], pr_number=pr_number, pr_url=pr_url)
                    LOG.info(
                        "  Backfilled PR #%d from branch %s", pr_number, branch
                    )
                else:
                    # GitHub PR was never created; requeue.
                    LOG.info(
                        "  No open PR found for branch %s — requeueing", branch
                    )
                    db.mark_finding(finding["id"], "open")
                    continue

            # Case 3: pr_number is known — check its GitHub state.
            try:
                state = gh_pr_state(owner, repo_name, pr_number)
            except GitHubAPIError as exc:
                LOG.warning(
                    "  GitHub API error for PR #%d — leaving state untouched: %s",
                    pr_number, exc,
                )
                continue  # fail-closed: leave in_progress, retry next run
            LOG.info(
                "  Finding %d (%s): PR #%d is %s",
                finding["id"], finding["title"], pr_number, state,
            )
            if state == "merged":
                db.update_pr(pr_row["id"], status="merged")
                db.mark_finding(finding["id"], "fixed", pr_id=pr_row["id"])
            elif state == "closed":
                db.update_pr(pr_row["id"], status="closed")
                db.mark_finding(finding["id"], "open")
            else:  # open — close and requeue for a clean retry
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d — leaving in_progress"
                        " for next reconcile: %s",
                        pr_number, exc,
                    )
                    continue
                db.update_pr(pr_row["id"], status="closed")
                db.mark_finding(finding["id"], "open")

    def _fix_finding(self, finding: sqlite3.Row, current_head: str) -> bool:
        """Full repair→verify→PR→review→CI→merge cycle for one finding.

        Uses a disposable git worktree so the main checkout is never touched
        and a crash can never leave the repo dirty.

        Returns True if the finding was successfully fixed and merged.
        """
        cfg = self.cfg
        db = self.db
        repo = Path(cfg["repo"]["path"]).resolve()
        main = cfg["repo"]["default_branch"]
        owner = cfg["repo"]["owner"]
        repo_name = cfg["repo"]["name"]
        f = dict(finding)

        LOG.info("[%s/%s] %s", f["severity"], f["confidence"], f["title"])

        # Generate branch name before touching anything so it's stable
        # for crash-recovery lookups.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"maint/{f['area']}/{ts}"

        # Persist the branch in the DB and link it to the finding *before*
        # any external side effects, so startup_reconcile can discover an
        # already-created GitHub PR on this branch even if we crash mid-way.
        pr_id = db.create_pr(branch)
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        wt_path = None
        try:
            # Create the repair worktree at the exact audited SHA so the diff
            # base and the code under repair are always the same commit.
            wt_path = create_worktree(repo, branch, current_head)
            wt_cfg = {
                **cfg,
                "repo": {
                    **cfg["repo"],
                    "path": str(wt_path),
                    "base_sha": current_head,  # pins verify_diff / review diffs
                },
            }

            # Run setup command in the worktree before any Codex calls.
            # A non-zero exit is an infrastructure failure: requeue the finding
            # without penalty and abort the run.
            if not run_setup(wt_cfg, wt_path):
                LOG.error(
                    "  Setup command failed — aborting run"
                    " (finding requeued, repair count unchanged)"
                )
                db.mark_finding(f["id"], "open")
                raise WorktreeSetupError("setup_cmd failed in worktree")

            # Revalidate if HEAD has moved since the finding was recorded.
            # Runs inside the worktree so it sees the exact audited commit.
            if f.get("commit_hash") and f["commit_hash"] != current_head:
                LOG.info(
                    "  Finding is from %s, current HEAD is %s — revalidating",
                    f["commit_hash"][:7],
                    current_head[:7],
                )
                rv = phase_revalidate(wt_cfg, f, self.ctr)
                if rv == "stale":
                    db.mark_finding(
                        f["id"], "stale", reason="no longer applies at current HEAD"
                    )
                    return False
                if rv == "error":
                    LOG.warning("  Revalidation failed — skipping this run")
                    self.ctr["consecutive_failures"] += 1
                    return False

            # Validate the finding before repair: confirm the audit claim is real.
            # Use a persisted verdict for this exact HEAD when available (crash-safe
            # resume).  phase_validate persists verdict+reason+evidence before returning,
            # so any of valid/invalid/uncertain cached at current_head is authoritative.
            finding_state = db.get_finding(f["id"])
            cached = (
                finding_state["validation_verdict"]
                if finding_state["validated_at_head"] == current_head
                   and finding_state["validation_verdict"] in ("valid", "invalid", "uncertain")
                else None
            )
            if cached is not None:
                LOG.info(
                    "  Reusing cached validation verdict %s at %s",
                    cached, current_head[:7],
                )
                val = cached
            else:
                LOG.info("  Validating finding at %s", current_head[:7])
                val = phase_validate(wt_cfg, f, self.ctr, db=db)

            if val == "invalid":
                LOG.info(
                    "  Validation: invalid — finding disproved at %s",
                    current_head[:7],
                )
                db.mark_finding(f["id"], "invalid", head=current_head)
                return False
            elif val == "uncertain":
                LOG.warning("  Validation: uncertain — blocking for human review")
                db.mark_finding(
                    f["id"], "blocked",
                    pr_id=pr_id,
                    reason="validation uncertain — requires human review",
                    head=current_head,
                )
                return False
            elif val == "error":
                LOG.warning("  Validation call failed — requeueing finding")
                db.mark_finding(f["id"], "open")
                self.ctr["consecutive_failures"] += 1
                return False
            elif val == "deferred":
                LOG.info(
                    "  Validation deferred — Codex budget exhausted, requeueing"
                )
                db.mark_finding(f["id"], "open")
                return False
            # else: valid — proceed to repair

            # Repair.
            if not phase_repair(wt_cfg, db, [f], self.ctr):
                current = db.get_finding(f["id"])
                if current and current["status"] == "rejected":
                    max_attempts = cfg["budget"].get("max_repair_attempts", 3)
                    if current["repair_attempts"] >= max_attempts:
                        LOG.warning(
                            "  Repair attempt cap (%d) reached — marking blocked",
                            max_attempts,
                        )
                        db.mark_finding(
                            f["id"], "blocked",
                            pr_id=pr_id,
                            reason=(
                                f"repair produced no changes after "
                                f"{max_attempts} attempt(s)"
                            ),
                            head=current_head,
                        )
                else:
                    db.mark_finding(f["id"], "open")
                return False

            # Verify.
            if not phase_verify(wt_cfg, [f], self.ctr):
                LOG.warning("  Verify rejected — discarding worktree")
                db.mark_finding(f["id"], "open")
                self.ctr["consecutive_failures"] += 1
                return False

            # Push branch — recorded in DB before gh pr create so reconcile
            # can find this PR if we crash between push and DB update.
            try:
                push_branch(wt_path, branch)
            except Exception as exc:
                LOG.error("  Push failed: %s", exc)
                db.mark_finding(f["id"], "open")
                self.ctr["consecutive_failures"] += 1
                return False

            # Create GitHub PR.  If this succeeds but we crash before updating
            # the DB, startup_reconcile will find it via gh_find_pr_by_branch.
            try:
                pr_number, pr_url = gh_create_pr(
                    owner, repo_name, branch,
                    f"maint({f['area']}): {f['title'][:60]}",
                    self._pr_body([f]),
                    base=main,
                )
            except Exception as exc:
                LOG.error("  PR creation failed: %s", exc)
                db.mark_finding(f["id"], "open")
                self.ctr["consecutive_failures"] += 1
                return False

            db.update_pr(pr_id, pr_number=pr_number, pr_url=pr_url)

            # Review loop.
            outcome = phase_review_loop(wt_cfg, db, pr_id, [f], branch, self.ctr)

            if outcome == REVIEW_FAILED_ERROR:
                LOG.error("  Review loop failed — closing PR and re-queuing finding")
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.error(
                        "  Failed to close PR #%d: %s"
                        " — leaving in_progress for startup_reconcile",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    return False
                db.update_pr(pr_id, status="closed")
                db.mark_finding(f["id"], "open")
                self.ctr["consecutive_failures"] += 1
                return False

            if outcome == REVIEW_DEFERRED_BUDGET:
                LOG.warning(
                    "  Codex budget hit during review — deferring finding to next run"
                )
                db.update_pr(pr_id, status="deferred")
                db.mark_finding(f["id"], "deferred", pr_id=pr_id)
                return False

            if outcome == REVIEW_PAUSED_BUDGET:
                LOG.warning(
                    "  Review rounds exhausted after %d rounds — marking blocked",
                    cfg["budget"]["max_review_rounds"],
                )
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.error(
                        "  Failed to close PR #%d: %s"
                        " — leaving in_progress for startup_reconcile",
                        pr_number, exc,
                    )
                    return False
                db.update_pr(pr_id, status="closed")
                db.mark_finding(
                    f["id"], "blocked",
                    pr_id=pr_id,
                    reason=(
                        f"review rounds exhausted after "
                        f"{cfg['budget']['max_review_rounds']} rounds"
                    ),
                    head=current_head,
                )
                return False

            # outcome == REVIEW_APPROVED — check CI then merge.
            allow_no_ci = cfg["verify"]["allow_no_ci"]
            ci = wait_for_ci(
                owner, repo_name, pr_number,
                cfg["verify"]["ci_wait_timeout"],
                allow_no_ci,
            )
            LOG.info("  CI status: %s", ci)

            if not ci_permits_merge(ci, allow_no_ci):
                if ci in ("failure",):
                    # Definite CI failure: close the PR so it can't be merged
                    # accidentally, then requeue the finding for a fresh attempt.
                    LOG.error(
                        "  CI failed — closing PR #%d and re-queuing finding",
                        pr_number,
                    )
                    try:
                        gh_close_pr(owner, repo_name, pr_number)
                    except GitHubAPIError as exc:
                        LOG.error(
                            "  Failed to close PR #%d: %s"
                            " — leaving in_progress for startup_reconcile",
                            pr_number, exc,
                        )
                        self.ctr["consecutive_failures"] += 1
                        return False
                    db.update_pr(pr_id, status="closed")
                    db.mark_finding(f["id"], "open")
                    self.ctr["consecutive_failures"] += 1
                else:
                    # Uncertain outcome (timeout, api_error): leave in_progress
                    # so startup_reconcile can retry safely on the next run.
                    LOG.error(
                        "  CI result '%s' is uncertain (allow_no_ci=%s)"
                        " — leaving in_progress for startup_reconcile",
                        ci, allow_no_ci,
                    )
                    self.ctr["consecutive_failures"] += 1
                return False

            # Freshness gate: reject if the base branch has advanced since we
            # audited (the patch was never tested against the new commits).
            try:
                base_oid = gh_pr_base_sha(owner, repo_name, pr_number)
            except GitHubAPIError as exc:
                LOG.error(
                    "  Cannot verify base freshness: %s"
                    " — leaving in_progress for startup_reconcile",
                    exc,
                )
                self.ctr["consecutive_failures"] += 1
                return False

            if base_oid != current_head:
                LOG.warning(
                    "  Base branch advanced to %s since audit at %s"
                    " — closing PR and requeueing",
                    base_oid[:7], current_head[:7],
                )
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.error(
                        "  Failed to close PR #%d: %s"
                        " — leaving in_progress for startup_reconcile",
                        pr_number, exc,
                    )
                    return False
                db.update_pr(pr_id, status="closed")
                db.mark_finding(f["id"], "open")
                return False

            # Merge.
            try:
                gh_merge_pr(owner, repo_name, pr_number)
            except Exception as exc:
                LOG.error(
                    "  Merge result uncertain: %s"
                    " — leaving in_progress for startup_reconcile",
                    exc,
                )
                self.ctr["consecutive_failures"] += 1
                return False

            db.update_pr(
                pr_id,
                status="merged",
                merged=datetime.now(timezone.utc).isoformat(),
            )
            db.mark_finding(f["id"], "fixed", pr_id=pr_id)
            LOG.info("  Merged PR #%d: %s", pr_number, pr_url)

            self.ctr["fixes_applied"] += 1
            self.ctr["consecutive_failures"] = 0
            return True

        finally:
            if wt_path is not None:
                remove_worktree(repo, branch, wt_path)

    def _fix_queue(self, findings: list, head: str) -> str:
        """Attempt to fix each finding in priority order.

        Returns:
          'merged'      — one fix was merged; caller should restart at a fresh HEAD
          'budget'      — a budget limit was reached
          'setup_error' — setup_cmd failed in a repair worktree
          'done'        — all findings processed, no merge occurred
        """
        for finding in findings:
            if self._over_budget():
                return "budget"
            try:
                result = self._fix_finding(finding, head)
            except WorktreeSetupError:
                return "setup_error"
            if result:
                return "merged"
        return "done"

    def _revalidate_stale_blocked(self, head: str, audit_cfg: dict) -> None:
        """Revalidate blocked findings recorded at a different HEAD."""
        for finding in self.db.stale_blocked_findings(head):
            if self._over_budget():
                break
            rv = phase_revalidate(audit_cfg, dict(finding), self.ctr)
            if rv == "stale":
                LOG.info(
                    "  Blocked finding no longer applies at %s — stale: %s",
                    head[:7], finding["title"],
                )
                self.db.mark_finding(
                    finding["id"], "stale",
                    reason=f"no longer applies at HEAD {head[:7]}",
                )
            elif rv == "valid":
                self.db.refresh_blocked_head(finding["id"], head)
            else:  # error
                self.ctr["consecutive_failures"] += 1

    def run_once(self) -> str:
        """Run one full sweep over all audit areas.

        A successful merge terminates the current sweep and restarts it from a
        freshly fetched origin/<default_branch>, ensuring all subsequent audits
        and revalidations see the merged code.

        Returns one of:
          'exhausted'   — all areas clean, no blocked findings
          'blocked'     — all areas clean, but unresolved blocked findings remain
          'budget'      — stopped early because a budget limit was reached
          'setup_error' — setup_cmd failed; run aborted, finding requeued without penalty
          'done'        — progress made (fixes applied or new findings) but not exhausted
        """
        # Reset per-run counters so run-continuous can't permanently wedge.
        self.ctr["codex_calls"] = 0
        self.ctr["fixes_applied"] = 0
        self.ctr["consecutive_failures"] = 0

        # Recover from crashes (in_progress) and clean up deferred PRs.
        self.startup_reconcile()

        cfg = self.cfg
        repo = Path(cfg["repo"]["path"]).resolve()
        main = cfg["repo"]["default_branch"]
        areas: list[dict] = cfg["audit_areas"]
        budget = cfg["budget"]

        LOG.info(
            "Starting maintenance run on %s/%s",
            cfg["repo"]["owner"],
            cfg["repo"]["name"],
        )

        total_new = 0
        last_head = ""

        # Outer loop: restart the sweep on each successful merge so that all
        # subsequent audits see the updated code at the new HEAD.
        while True:
            # Fetch origin/<main> fail-closed and create a read-only audit
            # worktree at exactly that SHA.  Never audit from a stale or
            # wrong-branch checkout.
            try:
                audit_wt = create_audit_worktree(repo, main)
            except subprocess.CalledProcessError as exc:
                LOG.error(
                    "Cannot fetch origin/%s — aborting run: %s",
                    main, exc.stderr.strip() if exc.stderr else exc,
                )
                return "budget"

            merged_this_pass = False
            try:
                head = current_commit(audit_wt)
                last_head = head
                audit_cfg = {**cfg, "repo": {**cfg["repo"], "path": str(audit_wt)}}

                for area in areas:
                    if self._over_budget():
                        return "budget"

                    streak = self.db.clean_audit_streak(
                        area["name"], budget["max_audits_per_area"], head
                    )
                    if streak >= budget["max_audits_per_area"]:
                        LOG.info(
                            "Area %-20s exhausted (%d clean audits at %s)",
                            area["name"], streak, head[:7],
                        )
                        continue

                    new = phase_audit(audit_cfg, self.db, area, self.ctr)
                    total_new += new

                    if self._over_budget():
                        return "budget"

                    outcome = self._fix_queue(
                        self.db.open_findings(area["name"]), head
                    )
                    if outcome in ("budget", "setup_error"):
                        return outcome
                    if outcome == "merged":
                        merged_this_pass = True
                        break

                # Revalidate blocked findings from an earlier HEAD.
                # Skip when a merge happened this pass — the sweep will restart
                # at a fresh HEAD and revalidate on that pass instead.
                if not merged_this_pass:
                    self._revalidate_stale_blocked(head, audit_cfg)

            finally:
                remove_audit_worktree(repo, audit_wt)

            if not merged_this_pass:
                break

            LOG.info(
                "Merge occurred — restarting sweep from freshly fetched origin/%s",
                main,
            )

        if last_head and all(
            self.db.clean_audit_streak(a["name"], budget["max_audits_per_area"], last_head)
            >= budget["max_audits_per_area"]
            for a in areas
        ):
            if self.db.blocked_findings() or self.db.rejected_at_head_findings(last_head):
                LOG.info(
                    "All audit areas exhausted at %s — unresolvable findings remain"
                    " (blocked or rejected at this HEAD).",
                    last_head[:7],
                )
                return "blocked"
            LOG.info(
                "All audit areas exhausted at %s — repository is clean.",
                last_head[:7],
            )
            return "exhausted"

        return "done"

    def run_repair(self) -> str:
        """Process existing queued findings without running audits.

        Fetches origin/<default_branch> HEAD, loads actionable findings from
        the DB in priority order, and drives each through the full repair
        lifecycle (revalidate → validate → repair → verify → PR → review →
        CI → merge).

        After a successful merge the HEAD is refreshed and remaining findings
        are revalidated against the new code.  Stale blocked findings are
        revalidated on each pass.

        Audit areas are never invoked, clean-audit streaks are not advanced,
        and the repository is never declared exhausted or blocked — this
        command processes what is already known.

        Returns one of:
          'done'        — all queued findings processed (some may still be open)
          'budget'      — stopped early because a budget limit was reached
          'setup_error' — setup_cmd failed; finding requeued without penalty
        """
        self.ctr["codex_calls"] = 0
        self.ctr["fixes_applied"] = 0
        self.ctr["consecutive_failures"] = 0

        self.startup_reconcile()

        cfg = self.cfg
        repo = Path(cfg["repo"]["path"]).resolve()
        main = cfg["repo"]["default_branch"]

        LOG.info(
            "Starting repair run on %s/%s",
            cfg["repo"]["owner"],
            cfg["repo"]["name"],
        )

        if not self.db.open_findings():
            print("No queued findings to repair.")
            return "done"

        while True:
            try:
                audit_wt = create_audit_worktree(repo, main)
            except subprocess.CalledProcessError as exc:
                LOG.error(
                    "Cannot fetch origin/%s — aborting run: %s",
                    main, exc.stderr.strip() if exc.stderr else exc,
                )
                return "budget"

            merged_this_pass = False
            try:
                head = current_commit(audit_wt)
                audit_cfg = {**cfg, "repo": {**cfg["repo"], "path": str(audit_wt)}}

                findings = self.db.open_findings()
                outcome = self._fix_queue(findings, head)
                if outcome in ("budget", "setup_error"):
                    return outcome
                merged_this_pass = (outcome == "merged")

                if not merged_this_pass:
                    self._revalidate_stale_blocked(head, audit_cfg)

            finally:
                remove_audit_worktree(repo, audit_wt)

            if not merged_this_pass:
                break

            LOG.info(
                "Merge occurred — refreshing HEAD from origin/%s and"
                " reconsidering remaining findings",
                main,
            )

        return "done"


# ── CLI display commands ───────────────────────────────────────────────────────

def cmd_status(cfg: dict, db: DB) -> None:
    print(f"\n=== Maintenance status: {cfg['repo']['owner']}/{cfg['repo']['name']} ===\n")

    counts = db.finding_counts()
    if counts:
        print("Findings by area / status:")
        cur_area = None
        for row in counts:
            if row["area"] != cur_area:
                cur_area = row["area"]
                print(f"  {cur_area}")
            print(f"      {row['status']:14} {row['n']}")
    else:
        print("No findings recorded yet.")

    print()
    prs = db.recent_prs()
    if prs:
        print("Recent PRs:")
        for pr in prs:
            print(
                f"  #{str(pr['pr_number'] or '?'):5}  "
                f"[{pr['status']:8}]  "
                f"{pr['branch']}  "
                f"{pr['pr_url'] or ''}"
            )
    else:
        print("No PRs yet.")

    budget = cfg["budget"]
    repo_path = Path(cfg["repo"]["path"]).resolve()
    try:
        head = current_commit(repo_path)
    except Exception:
        head = ""

    print("\nAudit area exhaustion (HEAD-tied):")
    for area in cfg["audit_areas"]:
        streak = db.clean_audit_streak(
            area["name"], budget["max_audits_per_area"], head
        )
        threshold = budget["max_audits_per_area"]
        tag = (
            f"exhausted @ {head[:7]}"
            if streak >= threshold
            else f"{streak}/{threshold} clean @ {head[:7] or 'unknown'}"
        )
        print(f"  {area['name']:22} {tag}")


def cmd_findings(db: DB) -> None:
    open_f = db.open_findings()
    blocked_f = db.blocked_findings()

    if not open_f and not blocked_f:
        print("No open or blocked findings.")
        return

    if open_f:
        print(f"\n{len(open_f)} open/deferred findings:\n")
        for f in open_f:
            loc = f" ({f['file_path']})" if f["file_path"] else ""
            tag = " [deferred]" if f["status"] == "deferred" else ""
            print(f"  [{f['severity']:8}/{f['confidence']:6}] {f['title']}{loc}{tag}")

    if blocked_f:
        print(f"\n{len(blocked_f)} blocked findings (review rounds exhausted):\n")
        for f in blocked_f:
            reason = f["reject_reason"] or ""
            print(f"  [{f['severity']:8}] {f['title']}  — {reason}")
