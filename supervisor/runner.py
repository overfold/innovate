from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import DB
from .git import (
    ci_permits_merge,
    create_worktree,
    current_commit,
    gh_close_pr,
    gh_create_pr,
    gh_find_pr_by_branch,
    gh_merge_pr,
    gh_pr_state,
    push_branch,
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
    phase_verify,
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
        """Reconcile in_progress findings against Git/GitHub state.

        Called at the start of every run_once() to recover from a previous crash
        or unexpected termination that left findings stuck in in_progress.

        Handles the PR-creation gap: if the DB has a branch recorded but no
        pr_number yet (crash between gh pr create and the DB update), we search
        GitHub for an open PR on that branch and backfill the record rather than
        orphaning the PR and creating a duplicate.
        """
        db = self.db
        cfg = self.cfg
        owner = cfg["repo"]["owner"]
        repo_name = cfg["repo"]["name"]

        rows = db.in_progress_findings()
        if not rows:
            return

        LOG.info("Reconciling %d in_progress finding(s)…", len(rows))
        for finding in rows:
            pr_row = db.get_pr(finding["pr_id"]) if finding["pr_id"] else None

            # Case 1: no PR record at all — finding was marked in_progress before
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
                found = gh_find_pr_by_branch(owner, repo_name, branch)
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
            state = gh_pr_state(owner, repo_name, pr_number)
            LOG.info(
                "  Finding %d (%s): PR #%d is %s",
                finding["id"], finding["title"], pr_number, state,
            )
            if state == "merged":
                db.update_pr(pr_row["id"], status="merged")
                db.mark_finding(finding["id"], "fixed", pr_id=pr_row["id"])
            elif state in ("closed", "unknown"):
                db.update_pr(pr_row["id"], status="closed")
                db.mark_finding(finding["id"], "open")
            else:  # open — close and requeue for a clean retry
                gh_close_pr(owner, repo_name, pr_number)
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

        # Revalidate if HEAD has moved since the finding was recorded.
        if f.get("commit_hash") and f["commit_hash"] != current_head:
            LOG.info(
                "  Finding is from %s, current HEAD is %s — revalidating",
                f["commit_hash"][:7],
                current_head[:7],
            )
            rv = phase_revalidate(cfg, f, self.ctr)
            if rv == "stale":
                db.mark_finding(
                    f["id"], "stale", reason="no longer applies at current HEAD"
                )
                return False
            if rv == "error":
                LOG.warning("  Revalidation failed — skipping this run")
                self.ctr["consecutive_failures"] += 1
                return False

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
            # Create a fresh, isolated worktree — crash-safe by construction.
            wt_path = create_worktree(repo, branch, main)
            wt_cfg = {**cfg, "repo": {**cfg["repo"], "path": str(wt_path)}}

            # Repair.
            if not phase_repair(wt_cfg, db, [f], self.ctr):
                current = db.get_finding(f["id"])
                if not current or current["status"] != "rejected":
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
                gh_close_pr(owner, repo_name, pr_number)
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
                db.update_pr(pr_id, status="closed")
                gh_close_pr(owner, repo_name, pr_number)
                db.mark_finding(
                    f["id"], "blocked",
                    pr_id=pr_id,
                    reason=(
                        f"review rounds exhausted after "
                        f"{cfg['budget']['max_review_rounds']} rounds"
                    ),
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
                LOG.error(
                    "  CI result '%s' blocks merge (allow_no_ci=%s)"
                    " — re-queuing finding",
                    ci, allow_no_ci,
                )
                db.update_pr(pr_id, status="failed")
                db.mark_finding(f["id"], "open")
                self.ctr["consecutive_failures"] += 1
                return False

            # Merge.
            try:
                gh_merge_pr(owner, repo_name, pr_number)
            except Exception as exc:
                LOG.error("  Merge failed: %s", exc)
                db.update_pr(pr_id, status="failed")
                db.mark_finding(f["id"], "open")
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

    def run_once(self) -> str:
        """Run one full pass over all audit areas.

        Returns 'exhausted' | 'done' | 'partial'.
        """
        # Reset per-run counters so run-continuous can't permanently wedge
        # once a budget limit from a previous iteration is hit.
        self.ctr["codex_calls"] = 0
        self.ctr["fixes_applied"] = 0
        self.ctr["consecutive_failures"] = 0

        # Recover from any crash/restart that left findings in_progress.
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

        # Pull latest main — safe because the main checkout is never modified
        # by _fix_finding (all work happens in isolated worktrees).
        subprocess.run(
            ["git", "pull", "origin", main],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )

        total_new = 0

        for area in areas:
            if self._over_budget():
                return "partial"

            head = current_commit(repo)
            streak = self.db.clean_audit_streak(
                area["name"], budget["max_audits_per_area"], head
            )
            if streak >= budget["max_audits_per_area"]:
                LOG.info(
                    "Area %-20s exhausted (%d clean audits at %s)",
                    area["name"], streak, head[:7],
                )
                continue

            new = phase_audit(cfg, self.db, area, self.ctr)
            total_new += new

            if self._over_budget():
                return "partial"

            for finding in self.db.open_findings(area["name"]):
                if self._over_budget():
                    return "partial"
                head = current_commit(repo)
                self._fix_finding(finding, head)

        head = current_commit(repo)
        if all(
            self.db.clean_audit_streak(a["name"], budget["max_audits_per_area"], head)
            >= budget["max_audits_per_area"]
            for a in areas
        ):
            LOG.info("All audit areas exhausted at %s — repository is clean.", head[:7])
            return "exhausted"

        return "done" if (total_new > 0 or self.ctr["fixes_applied"] > 0) else "partial"


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
