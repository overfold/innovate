"""Tests for the repair phase, repair command (run_repair), and
the rejected-at-HEAD convergence path that leads to 'blocked'."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from supervisor.runner import Supervisor
from supervisor.phases import REVIEW_APPROVED
from tests.helpers import _cfg, _db, _open_finding, RUNNER_MODULE


# ── phase_repair unit tests ────────────────────────────────────────────────────

class TestPhaseRepair:
    """phase_repair now works in whatever directory cfg points to."""

    def test_repair_no_changes_marks_rejected(self, tmp_path):
        from supervisor.phases import phase_repair

        db = _db()
        f = _open_finding(db)
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)

        with patch("supervisor.phases.run_codex"), \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.full_diff", return_value=""):
            ok = phase_repair(cfg, db, [f], {"codex_calls": 0, "consecutive_failures": 0})

        assert not ok
        assert db.get_finding(f["id"])["status"] == "rejected"

    def test_repair_codex_error_increments_failures(self, tmp_path):
        from supervisor.phases import phase_repair
        from supervisor.codex import CodexError

        db = _db()
        f = _open_finding(db)
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        with patch("supervisor.phases.run_codex",
                   side_effect=CodexError("boom")), \
             patch("supervisor.phases.current_commit", return_value="abc"):
            ok = phase_repair(cfg, db, [f], ctr)

        assert not ok
        assert ctr["consecutive_failures"] == 1

    def test_repair_success_commits(self, tmp_path):
        from supervisor.phases import phase_repair

        db = _db()
        f = _open_finding(db)
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 3}

        with patch("supervisor.phases.run_codex"), \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.full_diff", return_value="diff content"), \
             patch("supervisor.phases._git") as mock_git:
            ok = phase_repair(cfg, db, [f], ctr)

        assert ok
        # Repair alone must NOT reset consecutive_failures; only a full
        # end-to-end success (merge) resets it.
        assert ctr["consecutive_failures"] == 3
        # add -A and commit should both be called
        calls = [c.args[1] for c in mock_git.call_args_list]
        assert "add" in calls
        assert "commit" in calls


# ── malformed Codex output ─────────────────────────────────────────────────────

class TestMalformedCodexOutput:
    def test_malformed_audit_output_counts_as_failed(self):
        from supervisor.phases import phase_audit

        db = _db()
        cfg = _cfg()
        cfg["repo"]["path"] = "/fake"
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        area = {"name": "correctness", "description": "bugs"}

        with patch("supervisor.phases.run_codex", return_value="not json at all"), \
             patch("supervisor.phases.current_commit", return_value="abc"):
            new = phase_audit(cfg, db, area, ctr)

        assert new == 0
        assert ctr["consecutive_failures"] == 1

    def test_audit_with_missing_title_skips_finding(self):
        from supervisor.phases import phase_audit

        db = _db()
        cfg = _cfg()
        cfg["repo"]["path"] = "/fake"
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        area = {"name": "correctness", "description": "bugs"}
        bad_output = '{"findings": [{"description": "no title here"}]}'

        with patch("supervisor.phases.run_codex", return_value=bad_output), \
             patch("supervisor.phases.parse_json",
                   return_value={"findings": [{"description": "no title here"}]}), \
             patch("supervisor.phases.current_commit", return_value="abc"):
            new = phase_audit(cfg, db, area, ctr)

        assert new == 0  # skipped, not crashed


# ── repair attempts / HEAD-pinning → blocked ──────────────────────────────────

class TestRejectedHeadPinningRepair:
    """Repair-level rejected-HEAD tests (rejection increments, cap marks blocked)."""

    def test_repair_no_changes_increments_consecutive_failures(self, tmp_path):
        from supervisor.phases import phase_repair

        db = _db()
        f = _open_finding(db)
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        with patch("supervisor.phases.run_codex"), \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.full_diff", return_value=""):
            ok = phase_repair(cfg, db, [f], ctr)

        assert not ok
        assert ctr["consecutive_failures"] == 1
        assert db.get_finding(f["id"])["repair_attempts"] == 1
        assert db.get_finding(f["id"])["rejected_at_head"] == "abc"

    def test_repair_attempts_cap_marks_blocked(self, tmp_path):
        """After max_repair_attempts rejections, _fix_finding escalates to blocked."""
        db = _db()
        f = _open_finding(db)
        sup = Supervisor(_cfg(), db)
        max_attempts = sup.cfg["budget"]["max_repair_attempts"]  # 3
        wt = tmp_path / "wt"
        wt.mkdir()

        # Pre-seed repair_attempts to max-1 so the next rejection hits the cap.
        db._conn.execute(
            "UPDATE findings SET repair_attempts=? WHERE id=?",
            (max_attempts - 1, f["id"]),
        )
        db._conn.commit()

        def fake_repair_no_changes(cfg, db_, findings, ctr):
            db_.mark_finding(
                findings[0]["id"], "rejected",
                reason="no changes", head="abc1234",
            )
            ctr["consecutive_failures"] += 1
            return False

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", side_effect=fake_repair_no_changes):
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "blocked"
        assert db.open_findings() == []


# ── rejected-at-HEAD convergence ───────────────────────────────────────────────

class TestRejectedAtHeadConvergence:
    """A finding rejected at the current HEAD must drive run_once to 'blocked'."""

    def test_rejected_at_head_terminates_blocked(self, tmp_path):
        """State machine: audit→reject→reaudit (N times)→ run_once returns 'blocked'.

        With max_audits_per_area=2, after the initial rejection the supervisor
        needs two more audits with new_findings==0 before the area is exhausted.
        At that point rejected_at_head_findings returns the stuck finding and
        run_once must return 'blocked' rather than 'done'.
        """
        from supervisor.db import _fingerprint

        HEAD = "deadbeef1234"
        db = _db()
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)
        cfg["budget"]["max_audits_per_area"] = 2  # small for speed

        fp = _fingerprint("correctness", None, "stubborn bug")
        finding_proto = {
            "fingerprint": fp,
            "area": "correctness",
            "severity": "high",
            "confidence": "high",
            "file_path": None,
            "line_range": None,
            "title": "stubborn bug",
            "description": "always present",
            "commit_hash": HEAD,
        }

        audit_wt = tmp_path / "audit_wt"
        audit_wt.mkdir()

        def fake_phase_audit(cfg_, db_, area, ctr):
            """Re-reports the finding every audit; is_new=False once rejected."""
            _, is_new = db_.upsert_finding(finding_proto)
            new_count = 1 if is_new else 0
            run_id = db_.start_audit(area["name"], HEAD)
            db_.finish_audit(run_id, new_count, 1)  # total_found always 1
            return new_count

        def fake_repair_no_changes(cfg_, db_, findings, ctr):
            db_.mark_finding(
                findings[0]["id"], "rejected", reason="no changes", head=HEAD
            )
            ctr["consecutive_failures"] += 1
            return False

        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.create_audit_worktree", return_value=audit_wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value=HEAD), \
             patch(f"{RUNNER_MODULE}.phase_audit", side_effect=fake_phase_audit), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", side_effect=fake_repair_no_changes), \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"):

            sup = Supervisor(cfg, db)

            # Run 1: audit finds X new, repair rejects it.
            result1 = sup.run_once()
            assert result1 == "done"
            fid = db.open_findings() or db._conn.execute(
                "SELECT id FROM findings LIMIT 1"
            ).fetchone()
            assert db._conn.execute(
                "SELECT status FROM findings LIMIT 1"
            ).fetchone()["status"] == "rejected"

            # Run 2: X re-reported (not new), no repair attempt.  streak=1 < max=2.
            result2 = sup.run_once()
            assert result2 == "done"

            # Run 3: X re-reported again.  streak=2 == max → all areas exhausted.
            # rejected_at_head_findings returns X → must return 'blocked'.
            result3 = sup.run_once()
            assert result3 == "blocked"


# ── repair command ─────────────────────────────────────────────────────────────

class TestRepairCommand:
    """run_repair: processes queued findings without auditing."""

    def test_repair_processes_findings_without_audit(self, tmp_path):
        """repair drives existing findings through the full fix lifecycle
        and never calls phase_audit."""
        db = _db()
        f = _open_finding(db)
        wt = tmp_path / "wt"
        wt.mkdir()

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=lambda repo, main: wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit") as mock_audit, \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            result = sup.run_repair()

        assert result == "done"
        mock_audit.assert_not_called()
        assert db.get_finding(f["id"])["status"] == "fixed"

    def test_repair_revalidates_stale_findings(self, tmp_path):
        """repair triggers revalidation when a finding's commit_hash differs from HEAD."""
        db = _db()
        f = _open_finding(db)
        db._conn.execute(
            "UPDATE findings SET commit_hash='oldhead' WHERE id=?", (f["id"],)
        )
        db._conn.commit()
        wt = tmp_path / "wt"
        wt.mkdir()

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=lambda repo, main: wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="newhead"), \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_revalidate",
                   return_value="stale") as mock_reval:
            result = sup.run_repair()

        assert result == "done"
        mock_reval.assert_called_once()
        assert db.get_finding(f["id"])["status"] == "stale"

    def test_repair_restarts_after_merge(self, tmp_path):
        """A successful merge causes run_repair to re-fetch HEAD and process
        remaining findings against the new code."""
        db = _db()
        f1 = _open_finding(db, "first bug")
        f2 = _open_finding(db, "second bug")
        wt = tmp_path / "wt"
        wt.mkdir()

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        create_wt_calls = {"n": 0}

        def make_audit_wt(repo, main):
            create_wt_calls["n"] += 1
            return wt

        # First call to phase_repair succeeds (merges f1); second fails (f2 stays open).
        repair_calls = {"n": 0}

        def fake_repair(cfg, db_, findings, ctr):
            repair_calls["n"] += 1
            return repair_calls["n"] == 1

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=make_audit_wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", side_effect=fake_repair), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            result = sup.run_repair()

        assert result == "done"
        # Two audit worktrees: one for the merge pass, one for the follow-up pass.
        assert create_wt_calls["n"] == 2
        assert db.get_finding(f1["id"])["status"] == "fixed"

    def test_repair_empty_queue_exits_cleanly(self, capsys):
        """repair exits immediately with a clear message when there are no
        queued findings, without touching the network or creating worktrees."""
        db = _db()
        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        with patch(f"{RUNNER_MODULE}.create_audit_worktree") as mock_wt:
            result = sup.run_repair()

        assert result == "done"
        mock_wt.assert_not_called()
        assert "No queued findings" in capsys.readouterr().out

    def test_repair_revalidates_stale_blocked_when_no_open_findings(self, tmp_path):
        """repair must not exit early when the only queued finding is blocked
        at an older HEAD — that finding needs revalidation against the new HEAD."""
        db = _db()
        f = _open_finding(db)
        # Finding was blocked at HEAD A; current HEAD will be HEAD B.
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted", head="headA")
        wt = tmp_path / "wt"
        wt.mkdir()

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=lambda repo, main: wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="headB"), \
             patch(f"{RUNNER_MODULE}.phase_revalidate",
                   return_value="valid") as mock_reval:
            result = sup.run_repair()

        assert result == "done"
        # phase_revalidate must have been called for the stale blocked finding.
        mock_reval.assert_called_once()
        # blocked_at_head should now be refreshed to the new HEAD.
        assert db.get_finding(f["id"])["blocked_at_head"] == "headB"

    def test_repair_never_advances_audit_streak_or_declares_exhausted(
        self, tmp_path
    ):
        """repair never calls phase_audit, adds no audit_run records, and
        never returns 'exhausted' or 'blocked'."""
        db = _db()
        f = _open_finding(db)
        wt = tmp_path / "wt"
        wt.mkdir()

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=lambda repo, main: wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit") as mock_audit, \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            result = sup.run_repair()

        assert result not in ("exhausted", "blocked")
        mock_audit.assert_not_called()
        audit_run_count = db._conn.execute(
            "SELECT COUNT(*) AS n FROM audit_runs"
        ).fetchone()["n"]
        assert audit_run_count == 0

    def test_repair_revalidates_stale_rejected_and_retries(self, tmp_path):
        """repair must revalidate findings rejected at an older HEAD and retry
        them when still valid at the new HEAD.

        Regression test for the case where the only actionable finding is
        rejected at HEAD A and the current HEAD is B — repair must not report
        'No queued findings' and must revalidate, reopen, and retry the finding.
        """
        db = _db()
        f = _open_finding(db)
        # Finding was rejected at HEAD A; repair produced no changes there.
        db.mark_finding(f["id"], "rejected", reason="no changes produced", head="headA")

        wt = tmp_path / "wt"
        wt.mkdir()

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=lambda repo, main: wt) as mock_wt, \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="headB"), \
             patch(f"{RUNNER_MODULE}.phase_revalidate",
                   return_value="valid") as mock_reval, \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="headB"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            result = sup.run_repair()

        assert result == "done"
        # A worktree must have been created — the early-exit guard must not fire.
        mock_wt.assert_called()
        # phase_revalidate must have been called (at minimum by
        # _revalidate_stale_rejected; _fix_finding also calls it since
        # commit_hash differs from current HEAD).
        assert mock_reval.call_count >= 1
        # After revalidation confirms the finding is still valid at headB,
        # it is reopened and repaired.
        assert db.get_finding(f["id"])["status"] == "fixed"


# ── run_repair early-exit with ci_paused ─────────────────────────────────────

class TestRunRepairCIPausedEarlyExit:
    """run_repair must not exit early when ci_paused work is the only remaining work."""

    def test_run_repair_does_not_exit_when_only_ci_paused(self, tmp_path):
        """When open/blocked/rejected findings are all absent but a ci_paused PR exists,
        run_repair proceeds to _resume_paused_reviews rather than returning 'done'."""
        db = _db()
        sup = Supervisor(_cfg(), db)
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        db.update_pr(pr_id, pr_number=40, branch="fix/b",
                     pr_url="https://gh/40", status="ci_paused")
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        audit_wt = tmp_path / "wt"
        audit_wt.mkdir()

        resume_called = {"n": 0}

        def fake_resume(head):
            resume_called["n"] += 1
            return False  # no merge, so loop exits

        with patch(f"{RUNNER_MODULE}.create_audit_worktree", return_value=audit_wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch.object(sup, "_resume_paused_reviews", side_effect=fake_resume), \
             patch.object(sup, "_revalidate_stale_rejected"), \
             patch.object(sup, "_revalidate_stale_blocked"), \
             patch.object(sup, "_fix_queue", return_value="done"), \
             patch(f"{RUNNER_MODULE}.Supervisor.startup_reconcile"):
            result = sup.run_repair()

        assert resume_called["n"] >= 1, "_resume_paused_reviews was never called"
        assert result == "done"
