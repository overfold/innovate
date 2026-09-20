from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import DB
from .git import (
    _git,
    ci_permits_merge,
    current_commit,
    gh_close_pr,
    gh_create_pr,
    gh_merge_pr,
    push_branch,
    wait_for_ci,
)
from .phases import (
    REVIEW_APPROVED,
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

    def _fix_finding(self, finding: sqlite3.Row, current_head: str) -> bool:
        """Full repair→verify→PR→review→CI→merge cycle for one finding.

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

        db.mark_finding(f["id"], "in_progress")

        # Repair.
        ok, branch = phase_repair(cfg, db, [f], self.ctr)
        if not ok:
            # phase_repair marks the finding rejected when no changes were produced.
            # Re-read to avoid overwriting that with 'open'.
            current = db.get_finding(f["id"])
            if not current or current["status"] != "rejected":
                db.mark_finding(f["id"], "open")
            return False

        # Verify.
        if not phase_verify(cfg, [f], self.ctr):
            LOG.warning("  Verify rejected — discarding branch")
            _git(repo, "checkout", main, check=False)
            _git(repo, "branch", "-D", branch, check=False)
            db.mark_finding(f["id"], "open")
            self.ctr["consecutive_failures"] += 1
            return False

        # Push + open PR.
        try:
            push_branch(repo, branch)
            pr_number, pr_url = gh_create_pr(
                owner, repo_name, branch,
                f"maint({f['area']}): {f['title'][:60]}",
                self._pr_body([f]),
            )
        except Exception as exc:
            LOG.error("  Push/PR creation failed: %s", exc)
            _git(repo, "checkout", main, check=False)
            db.mark_finding(f["id"], "open")
            self.ctr["consecutive_failures"] += 1
            return False

        pr_id = db.create_pr(branch)
        db.update_pr(pr_id, pr_number=pr_number, pr_url=pr_url)
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        _git(repo, "checkout", branch, check=False)

        # Review loop.
        outcome = phase_review_loop(cfg, db, pr_id, [f], branch, self.ctr)

        if outcome == REVIEW_FAILED_ERROR:
            LOG.error("  Review loop failed — closing PR and re-queuing finding")
            gh_close_pr(owner, repo_name, pr_number)
            db.update_pr(pr_id, status="closed")
            db.mark_finding(f["id"], "open")
            _git(repo, "checkout", main, check=False)
            self.ctr["consecutive_failures"] += 1
            return False

        if outcome == REVIEW_PAUSED_BUDGET:
            LOG.warning(
                "  Review budget exhausted — PR #%d left open for human review",
                pr_number,
            )
            db.update_pr(pr_id, status="paused")
            db.mark_finding(
                f["id"], "paused",
                pr_id=pr_id,
                reason=(
                    f"review budget exhausted after "
                    f"{cfg['budget']['max_review_rounds']} rounds"
                ),
            )
            _git(repo, "checkout", main, check=False)
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
                "  CI result '%s' blocks merge (allow_no_ci=%s) — re-queuing finding",
                ci, allow_no_ci,
            )
            db.update_pr(pr_id, status="failed")
            db.mark_finding(f["id"], "open")
            _git(repo, "checkout", main, check=False)
            self.ctr["consecutive_failures"] += 1
            return False

        # Merge.
        try:
            gh_merge_pr(owner, repo_name, pr_number)
        except Exception as exc:
            LOG.error("  Merge failed: %s", exc)
            db.update_pr(pr_id, status="failed")
            db.mark_finding(f["id"], "open")
            _git(repo, "checkout", main, check=False)
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

        _git(repo, "checkout", main, check=False)
        _git(repo, "pull", "origin", main, check=False)
        return True

    def run_once(self) -> str:
        """Run one full pass over all audit areas.

        Returns 'exhausted' | 'done' | 'partial'.
        """
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

        _git(repo, "checkout", main, check=False)
        _git(repo, "pull", "origin", main, check=False)

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
    paused_f = db.paused_findings()

    if not open_f and not paused_f:
        print("No open or paused findings.")
        return

    if open_f:
        print(f"\n{len(open_f)} open findings:\n")
        for f in open_f:
            loc = f" ({f['file_path']})" if f["file_path"] else ""
            print(f"  [{f['severity']:8}/{f['confidence']:6}] {f['title']}{loc}")

    if paused_f:
        print(f"\n{len(paused_f)} paused findings (PR open, awaiting human review):\n")
        for f in paused_f:
            reason = f["reject_reason"] or ""
            print(f"  [{f['severity']:8}] {f['title']}  — {reason}")
