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
    create_branch_worktree,
    create_worktree,
    current_commit,
    gh_close_pr,
    gh_create_pr,
    gh_find_pr_by_branch,
    gh_get_failed_ci_logs,
    gh_merge_pr,
    gh_pr_base_sha,
    gh_pr_state,
    push_branch,
    remove_audit_worktree,
    remove_branch_worktree,
    remove_worktree,
    wait_for_ci,
)
from .phases import (
    REVIEW_APPROVED,
    REVIEW_CI_UNCERTAIN,
    REVIEW_DEFERRED_BUDGET,
    REVIEW_FAILED_ERROR,
    REVIEW_PAUSED_BUDGET,
    phase_audit,
    phase_repair,
    phase_revalidate,
    phase_review_loop,
    phase_validate,
)

LOG = logging.getLogger("supervisor")


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

        in_progress: reconcile against actual GitHub PR state.  Existing open
                     PRs are preserved and resumed after HEAD is established.
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
            else:  # open — resume the existing PR after HEAD is established
                LOG.info(
                    "  Preserving open PR #%d for crash recovery (status=%s)",
                    pr_number, pr_row["status"],
                )

    def _fix_finding(self, finding: sqlite3.Row, current_head: str) -> bool:
        """Full repair→PR→review→CI→merge cycle for one finding.

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

            # Review loop (includes CI polling; REVIEW_APPROVED means CI passed).
            outcome = phase_review_loop(wt_cfg, db, pr_id, pr_number, [f], branch, self.ctr)

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

            if outcome == REVIEW_CI_UNCERTAIN:
                # CI returned an ambiguous result (timeout, api_error, no_checks when
                # not allowed). Do not make code changes; mark the PR ci_paused so
                # startup_reconcile can re-poll CI on the next run.
                LOG.warning(
                    "  CI result uncertain — marking PR ci_paused for startup_reconcile"
                )
                db.update_pr(pr_id, status="ci_paused")
                self.ctr["consecutive_failures"] += 1
                return False

            # outcome == REVIEW_APPROVED — CI already passed inside phase_review_loop.
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
          'merged' — one fix was merged; caller should restart at a fresh HEAD
          'budget' — a budget limit was reached
          'done'   — all findings processed, no merge occurred
        """
        for finding in findings:
            if self._over_budget():
                return "budget"
            if self._fix_finding(finding, head):
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

    def _resume_open_reviews(self, head: str) -> bool:
        """Resume ordinary in-progress PRs that survived a previous crash.

        PR creation is an external side effect, so once a PR exists we keep its
        branch and continue the persisted review state instead of closing it and
        starting the same finding again on a new timestamped branch.

        A PR whose review already approved the code is merged directly after the
        normal freshness check.  Otherwise the review loop resumes from its
        persisted review_rounds count.

        Returns True if a PR was merged (caller must refetch HEAD and restart
        the sweep), False otherwise.
        """
        db = self.db
        cfg = self.cfg
        owner = cfg["repo"]["owner"]
        repo_name = cfg["repo"]["name"]
        repo = Path(cfg["repo"]["path"]).resolve()

        resumable = [
            f for f in db.in_progress_findings()
            if f["pr_id"] and db.get_pr(f["pr_id"])
            and db.get_pr(f["pr_id"])["status"] in ("open", "review_approved")
            and db.get_pr(f["pr_id"])["pr_number"]
        ]
        if not resumable:
            return False

        LOG.info("Resuming %d open PR(s) after crash…", len(resumable))

        for finding in resumable:
            if self._over_budget():
                break

            pr_row = db.get_pr(finding["pr_id"])
            pr_id = pr_row["id"]
            pr_number = pr_row["pr_number"]
            branch = pr_row["branch"]

            # The base must still match the HEAD this pass is operating on.
            try:
                base_oid = gh_pr_base_sha(owner, repo_name, pr_number)
            except GitHubAPIError as exc:
                LOG.warning(
                    "  Cannot verify base freshness for PR #%d: %s"
                    " — leaving in_progress",
                    pr_number, exc,
                )
                self.ctr["consecutive_failures"] += 1
                continue
            if base_oid != head:
                LOG.warning(
                    "  Base advanced for PR #%d — closing and requeueing",
                    pr_number,
                )
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d: %s — leaving for next reconcile",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                db.update_pr(pr_id, status="closed")
                db.mark_finding(finding["id"], "open")
                continue

            if pr_row["status"] == "review_approved":
                outcome = REVIEW_APPROVED
            else:
                wt_path = None
                try:
                    wt_path = create_branch_worktree(repo, branch)
                    wt_cfg = {
                        **cfg,
                        "repo": {
                            **cfg["repo"],
                            "path": str(wt_path),
                            "base_sha": head,
                        },
                    }
                    outcome = phase_review_loop(
                        wt_cfg, db, pr_id, pr_number,
                        [dict(finding)], branch, self.ctr,
                    )
                except subprocess.CalledProcessError as exc:
                    LOG.warning(
                        "  Branch fetch failed for PR #%d: %s — leaving in_progress",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                finally:
                    if wt_path is not None:
                        remove_branch_worktree(repo, wt_path)

            if outcome == REVIEW_APPROVED:
                # Re-check immediately before merge in case the base moved while
                # review or CI was running.
                try:
                    base_oid = gh_pr_base_sha(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Cannot verify base freshness for PR #%d: %s"
                        " — leaving review_approved",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                if base_oid != head:
                    try:
                        gh_close_pr(owner, repo_name, pr_number)
                    except GitHubAPIError as exc:
                        LOG.warning(
                            "  Failed to close PR #%d: %s — leaving for next reconcile",
                            pr_number, exc,
                        )
                        self.ctr["consecutive_failures"] += 1
                        continue
                    db.update_pr(pr_id, status="closed")
                    db.mark_finding(finding["id"], "open")
                    continue
                try:
                    gh_merge_pr(owner, repo_name, pr_number)
                except Exception as exc:
                    LOG.warning(
                        "  Merge uncertain for PR #%d: %s — leaving review_approved",
                        pr_number, exc,
                    )
                    db.update_pr(pr_id, status="review_approved")
                    self.ctr["consecutive_failures"] += 1
                    continue
                db.update_pr(
                    pr_id,
                    status="merged",
                    merged=datetime.now(timezone.utc).isoformat(),
                )
                db.mark_finding(finding["id"], "fixed", pr_id=pr_id)
                self.ctr["fixes_applied"] += 1
                self.ctr["consecutive_failures"] = 0
                return True

            if outcome == REVIEW_CI_UNCERTAIN:
                db.update_pr(pr_id, status="ci_paused")

            elif outcome == REVIEW_FAILED_ERROR:
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d: %s — leaving for next reconcile",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                db.update_pr(pr_id, status="closed")
                db.mark_finding(finding["id"], "open")
                self.ctr["consecutive_failures"] += 1

            elif outcome == REVIEW_PAUSED_BUDGET:
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d: %s — leaving for next reconcile",
                        pr_number, exc,
                    )
                    continue
                db.update_pr(pr_id, status="closed")
                db.mark_finding(
                    finding["id"], "blocked", pr_id=pr_id,
                    reason="review rounds exhausted after crash recovery",
                    head=head,
                )

            elif outcome == REVIEW_DEFERRED_BUDGET:
                db.update_pr(pr_id, status="deferred")
                db.mark_finding(finding["id"], "deferred", pr_id=pr_id)

        return False

    def _resume_paused_reviews(self, head: str) -> bool:
        """Drive ci_paused PRs through the full review/CI loop.

        Called at the start of each pass after HEAD is established.  A
        ci_paused PR is one whose last review approved the code but CI was
        still red at the time (REVIEW_CI_UNCERTAIN returned).  We now re-check:
        - CI passed → verify base freshness → merge.
        - CI still uncertain/pending → leave ci_paused for the next run.
        - CI definitively failed → collect logs → run phase_review_loop with
          the evidence so the reviewer can request repairs or confirm the
          failure is unrelated again (which itself returns REVIEW_CI_UNCERTAIN,
          keeping the PR in ci_paused for another pass).

        Returns True if a PR was merged (caller must refetch HEAD and restart
        the sweep), False otherwise.
        """
        db = self.db
        cfg = self.cfg
        owner = cfg["repo"]["owner"]
        repo_name = cfg["repo"]["name"]
        repo = Path(cfg["repo"]["path"]).resolve()
        ci_wait = cfg["verify"]["ci_wait_timeout"]
        allow_no_ci = cfg["verify"]["allow_no_ci"]

        paused_findings = [
            f for f in db.in_progress_findings()
            if f["pr_id"] and db.get_pr(f["pr_id"])
            and db.get_pr(f["pr_id"])["status"] == "ci_paused"
        ]
        if not paused_findings:
            return False

        LOG.info("Resuming %d ci_paused PR(s)…", len(paused_findings))
        merged = False

        for finding in paused_findings:
            if self._over_budget():
                break
            pr_row = db.get_pr(finding["pr_id"])
            pr_id = pr_row["id"]
            pr_number = pr_row["pr_number"]
            branch = pr_row["branch"]

            # Base-freshness gate — must pass before any CI inspection, log
            # collection, or branch modification.  A stale PR must be closed
            # and requeued rather than reviewed against a different base.
            try:
                base_oid = gh_pr_base_sha(owner, repo_name, pr_number)
            except GitHubAPIError as exc:
                LOG.warning(
                    "  Cannot verify base freshness for PR #%d: %s"
                    " — leaving ci_paused",
                    pr_number, exc,
                )
                self.ctr["consecutive_failures"] += 1
                continue
            if base_oid != head:
                LOG.warning(
                    "  Base advanced for PR #%d — closing and requeueing",
                    pr_number,
                )
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d: %s — leaving for next reconcile",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                db.update_pr(pr_id, status="closed")
                db.mark_finding(finding["id"], "open")
                continue

            ci = wait_for_ci(owner, repo_name, pr_number, ci_wait, allow_no_ci)
            LOG.info("  ci_paused PR #%d: CI is now %s", pr_number, ci)

            if ci_permits_merge(ci, allow_no_ci):
                try:
                    gh_merge_pr(owner, repo_name, pr_number)
                except Exception as exc:
                    LOG.warning(
                        "  Merge uncertain for PR #%d: %s — leaving ci_paused",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                db.update_pr(pr_id, status="merged", merged=datetime.now(timezone.utc).isoformat())
                db.mark_finding(finding["id"], "fixed", pr_id=pr_id)
                self.ctr["fixes_applied"] += 1
                self.ctr["consecutive_failures"] = 0
                return True  # restart sweep at new HEAD

            if ci != "failure":
                LOG.warning("  CI status %r is uncertain — leaving ci_paused", ci)
                continue

            # CI definitively failed — collect logs and enter the full review loop.
            logs = gh_get_failed_ci_logs(owner, repo_name, pr_number)
            if not logs:
                LOG.warning(
                    "  CI failed but no retrievable logs for PR #%d — leaving ci_paused",
                    pr_number,
                )
                if logs is None:
                    self.ctr["consecutive_failures"] += 1
                continue

            LOG.info(
                "  CI failed for PR #%d — entering review loop with CI evidence",
                pr_number,
            )
            wt_path = None
            try:
                wt_path = create_branch_worktree(repo, branch)
                wt_cfg = {**cfg, "repo": {**cfg["repo"], "path": str(wt_path), "base_sha": head}}
                outcome = phase_review_loop(
                    wt_cfg, db, pr_id, pr_number, [dict(finding)], branch, self.ctr,
                    initial_ci_evidence=logs,
                )
            except subprocess.CalledProcessError as exc:
                LOG.warning(
                    "  Branch fetch failed for PR #%d: %s — leaving ci_paused",
                    pr_number, exc,
                )
                self.ctr["consecutive_failures"] += 1
                continue
            finally:
                if wt_path is not None:
                    remove_branch_worktree(repo, wt_path)

            if outcome == REVIEW_APPROVED:
                try:
                    base_oid = gh_pr_base_sha(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.error(
                        "  Cannot verify base freshness for PR #%d: %s"
                        " — leaving in_progress",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                if base_oid != head:
                    try:
                        gh_close_pr(owner, repo_name, pr_number)
                    except GitHubAPIError as exc:
                        LOG.warning(
                            "  Failed to close PR #%d: %s — leaving for next reconcile",
                            pr_number, exc,
                        )
                        self.ctr["consecutive_failures"] += 1
                        continue
                    db.update_pr(pr_id, status="closed")
                    db.mark_finding(finding["id"], "open")
                    continue
                try:
                    gh_merge_pr(owner, repo_name, pr_number)
                except Exception as exc:
                    LOG.error("  Merge uncertain for PR #%d: %s", pr_number, exc)
                    self.ctr["consecutive_failures"] += 1
                    continue
                db.update_pr(pr_id, status="merged", merged=datetime.now(timezone.utc).isoformat())
                db.mark_finding(finding["id"], "fixed", pr_id=pr_id)
                self.ctr["fixes_applied"] += 1
                self.ctr["consecutive_failures"] = 0
                return True  # restart sweep at new HEAD

            elif outcome == REVIEW_CI_UNCERTAIN:
                db.update_pr(pr_id, status="ci_paused")

            elif outcome == REVIEW_FAILED_ERROR:
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d: %s — leaving for next reconcile",
                        pr_number, exc,
                    )
                    self.ctr["consecutive_failures"] += 1
                    continue
                db.update_pr(pr_id, status="closed")
                db.mark_finding(finding["id"], "open")
                self.ctr["consecutive_failures"] += 1

            elif outcome == REVIEW_PAUSED_BUDGET:
                try:
                    gh_close_pr(owner, repo_name, pr_number)
                except GitHubAPIError as exc:
                    LOG.warning(
                        "  Failed to close PR #%d: %s — leaving for next reconcile",
                        pr_number, exc,
                    )
                    continue
                db.update_pr(pr_id, status="closed")
                db.mark_finding(
                    finding["id"], "blocked", pr_id=pr_id,
                    reason="review rounds exhausted after ci_paused retry",
                    head=head,
                )

            elif outcome == REVIEW_DEFERRED_BUDGET:
                db.update_pr(pr_id, status="deferred")
                db.mark_finding(finding["id"], "deferred", pr_id=pr_id)

        return merged

    def _revalidate_stale_rejected(self, head: str, audit_cfg: dict) -> None:
        """Revalidate rejected findings recorded at a different HEAD.

        A finding rejected at an older HEAD is reconsidered: if it no longer
        applies it is marked stale; if it still applies it is reopened for
        another repair attempt (preserving repair_attempts so
        max_repair_attempts can eventually convert unfixable findings to blocked).
        """
        for finding in self.db.stale_rejected_findings(head):
            if self._over_budget():
                break
            rv = phase_revalidate(audit_cfg, dict(finding), self.ctr)
            if rv == "stale":
                LOG.info(
                    "  Rejected finding no longer applies at %s — stale: %s",
                    head[:7], finding["title"],
                )
                self.db.mark_finding(
                    finding["id"], "stale",
                    reason=f"no longer applies at HEAD {head[:7]}",
                )
            elif rv == "valid":
                LOG.info(
                    "  Rejected finding still valid at %s — reopening: %s",
                    head[:7], finding["title"],
                )
                self.db.reopen_rejected_finding(finding["id"])
            else:  # error
                self.ctr["consecutive_failures"] += 1

    def run_once(self) -> str:
        """Run one full sweep over all audit areas.

        A successful merge terminates the current sweep and restarts it from a
        freshly fetched origin/<default_branch>, ensuring all subsequent audits
        and revalidations see the merged code.

        Returns one of:
          'exhausted' — all areas clean, no blocked findings
          'blocked'   — all areas clean, but unresolved blocked findings remain
          'budget'    — stopped early because a budget limit was reached
          'done'      — progress made (fixes applied or new findings) but not exhausted
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

                merged_this_pass = self._resume_paused_reviews(head)
                if not merged_this_pass:
                    merged_this_pass = self._resume_open_reviews(head)
                if merged_this_pass:
                    LOG.info(
                        "Recovered PR merged — restarting sweep from freshly"
                        " fetched origin/%s",
                        main,
                    )
                else:
                    if self._over_budget():
                        return "budget"

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
                        if outcome == "budget":
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
        lifecycle (revalidate → validate → repair → PR → review → CI → merge).

        After a successful merge the HEAD is refreshed and remaining findings
        are revalidated against the new code.  Stale blocked findings are
        revalidated on each pass.

        Audit areas are never invoked, clean-audit streaks are not advanced,
        and the repository is never declared exhausted or blocked — this
        command processes what is already known.

        Returns one of:
          'done'   — all queued findings processed (some may still be open)
          'budget' — stopped early because a budget limit was reached
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

        def _has_resumable_reviews() -> bool:
            return any(
                f["pr_id"] and self.db.get_pr(f["pr_id"])
                and self.db.get_pr(f["pr_id"])["status"]
                    in ("open", "review_approved", "ci_paused")
                for f in self.db.in_progress_findings()
            )

        if (not self.db.open_findings()
                and not self.db.blocked_findings()
                and not self.db.any_rejected_findings()
                and not _has_resumable_reviews()):
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

                merged_this_pass = self._resume_paused_reviews(head)
                if not merged_this_pass:
                    merged_this_pass = self._resume_open_reviews(head)
                if merged_this_pass:
                    LOG.info(
                        "Recovered PR merged — restarting from freshly"
                        " fetched origin/%s",
                        main,
                    )
                else:
                    if self._over_budget():
                        return "budget"

                    # Revalidate rejected findings from an earlier HEAD before
                    # loading the queue: any that are still valid are reopened
                    # immediately so they are picked up by open_findings() below.
                    self._revalidate_stale_rejected(head, audit_cfg)

                    findings = self.db.open_findings()
                    outcome = self._fix_queue(findings, head)
                    if outcome == "budget":
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
