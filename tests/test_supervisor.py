"""Tests for the maintain.py supervisor state machine.

All external calls (Codex, git worktree creation, GitHub CLI) are mocked so
tests are deterministic and run without network access or a real repository.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from supervisor.db import DB
from supervisor.phases import (
    REVIEW_APPROVED,
    REVIEW_DEFERRED_BUDGET,
    REVIEW_FAILED_ERROR,
    REVIEW_PAUSED_BUDGET,
)
from supervisor.runner import Supervisor


# ── helpers ────────────────────────────────────────────────────────────────────

def _db() -> DB:
    """In-memory SQLite database."""
    db = DB.__new__(DB)
    import sqlite3
    from supervisor.db import _SCHEMA
    db._conn = sqlite3.connect(":memory:")
    db._conn.row_factory = sqlite3.Row
    db._conn.executescript(_SCHEMA)
    db._conn.commit()
    return db


def _cfg(**overrides) -> dict:
    base = {
        "repo": {
            "owner": "org",
            "name": "repo",
            "path": "/fake/repo",
            "default_branch": "main",
            "commit_trailer": "",
            "pr_footer": "",
        },
        "codex": {
            "cmd": "codex",
            "model": "o4-mini",
            "timeout": 10,
            "audit_flags": ["exec"],
            "repair_flags": ["exec"],
        },
        "verify": {
            "test_cmd": "",
            "ci_wait_timeout": 0,
            "allow_no_ci": True,
        },
        "budget": {
            "max_audits_per_area": 3,
            "max_fixes_per_run": 20,
            "max_review_rounds": 2,
            "max_consecutive_failures": 5,
            "codex_call_budget": 100,
        },
        "audit_areas": [{"name": "correctness", "description": "bugs"}],
    }
    base.update(overrides)
    return base


def _open_finding(db: DB, title: str = "Test bug", area: str = "correctness") -> dict:
    from supervisor.db import _fingerprint
    fp = _fingerprint(area, None, title)
    fid, _ = db.upsert_finding({
        "fingerprint": fp,
        "area": area,
        "severity": "high",
        "confidence": "high",
        "file_path": None,
        "line_range": None,
        "title": title,
        "description": "a description",
        "commit_hash": "abc1234",
    })
    return dict(db.get_finding(fid))


# ── DB unit tests ──────────────────────────────────────────────────────────────

class TestDB:
    def test_upsert_new_finding(self):
        db = _db()
        f = _open_finding(db)
        assert f["status"] == "open"

    def test_upsert_deduplication(self):
        db = _db()
        f = _open_finding(db, "dup bug")
        fid2, is_new = db.upsert_finding({
            **f,
            "fingerprint": f["fingerprint"],
        })
        assert not is_new
        assert fid2 == f["id"]

    def test_regression_reopens_fixed_finding(self):
        db = _db()
        f = _open_finding(db, "fixed regression")
        db.mark_finding(f["id"], "fixed")
        row = db.get_finding(f["id"])
        assert row["status"] == "fixed"

        from supervisor.db import _fingerprint
        fp = _fingerprint(f["area"], f["file_path"], f["title"])
        _, is_new = db.upsert_finding({**f, "fingerprint": fp})
        assert is_new
        assert db.get_finding(f["id"])["status"] == "open"

    def test_open_findings_includes_deferred(self):
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "deferred")
        assert len(db.open_findings()) == 1
        assert db.open_findings()[0]["status"] == "deferred"

    def test_open_findings_excludes_blocked(self):
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked")
        assert db.open_findings() == []

    def test_blocked_findings_returns_blocked(self):
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted")
        rows = db.blocked_findings()
        assert len(rows) == 1
        assert rows[0]["reject_reason"] == "rounds exhausted"

    def test_clean_audit_streak_excludes_deferred(self):
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "deferred")
        streak = db.clean_audit_streak("correctness", 3, "abc1234")
        assert streak == 0  # deferred counts as active

    def test_find_pr_by_branch(self):
        db = _db()
        pr_id = db.create_pr("maint/correctness/20240101-120000")
        db.update_pr(pr_id, pr_number=42, pr_url="https://github.com/o/r/pull/42")
        row = db.find_pr_by_branch("maint/correctness/20240101-120000")
        assert row is not None
        assert row["pr_number"] == 42


# ── startup_reconcile tests ────────────────────────────────────────────────────

RUNNER_MODULE = "supervisor.runner"
GIT_MODULE = "supervisor.git"


class TestStartupReconcile:
    def _sup(self):
        return Supervisor(_cfg(), _db())

    def test_no_in_progress_is_noop(self):
        sup = self._sup()
        # Should complete without error
        sup.startup_reconcile()
        assert sup.db.in_progress_findings() == []

    def test_in_progress_no_pr_id_requeued(self):
        sup = self._sup()
        f = _open_finding(sup.db)
        sup.db.mark_finding(f["id"], "in_progress")
        # No pr_id set → should requeue
        sup.startup_reconcile()
        assert sup.db.get_finding(f["id"])["status"] == "open"

    def test_in_progress_pr_merged(self):
        sup = self._sup()
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/ts")
        sup.db.update_pr(pr_id, pr_number=7, pr_url="https://gh/7")
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="merged"):
            sup.startup_reconcile()

        assert sup.db.get_finding(f["id"])["status"] == "fixed"
        assert sup.db.get_pr(pr_id)["status"] == "merged"

    def test_in_progress_pr_closed_requeued(self):
        sup = self._sup()
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/ts")
        sup.db.update_pr(pr_id, pr_number=8, pr_url="https://gh/8")
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="closed"):
            sup.startup_reconcile()

        assert sup.db.get_finding(f["id"])["status"] == "open"

    def test_in_progress_open_pr_closed_and_requeued(self):
        """Open PR on restart is closed and finding requeued."""
        sup = self._sup()
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/ts")
        sup.db.update_pr(pr_id, pr_number=9, pr_url="https://gh/9")
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:
            sup.startup_reconcile()

        mock_close.assert_called_once_with("org", "repo", 9)
        assert sup.db.get_finding(f["id"])["status"] == "open"

    # --- PR creation gap ---

    def test_crash_after_pr_create_before_db_link(self):
        """Branch recorded in DB but no pr_number: reconcile backfills via GitHub search."""
        sup = self._sup()
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/20240101")
        # pr_number is NULL — simulates crash between gh pr create and DB update
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        with patch(f"{RUNNER_MODULE}.gh_find_pr_by_branch",
                   return_value=(55, "https://gh/55")) as mock_find, \
             patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr"):
            sup.startup_reconcile()

        mock_find.assert_called_once_with("org", "repo", "maint/correctness/20240101")
        # pr_number should now be recorded
        assert sup.db.get_pr(pr_id)["pr_number"] == 55
        # open PR closed and finding requeued
        assert sup.db.get_finding(f["id"])["status"] == "open"

    def test_crash_after_pr_create_github_not_found(self):
        """Branch recorded but GitHub finds no matching PR: requeue without orphan."""
        sup = self._sup()
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/20240102")
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        with patch(f"{RUNNER_MODULE}.gh_find_pr_by_branch", return_value=None):
            sup.startup_reconcile()

        assert sup.db.get_finding(f["id"])["status"] == "open"


# ── _fix_finding / worktree lifecycle ──────────────────────────────────────────

def _make_fake_wt(tmp_path):
    """Return a real temp dir to stand in for the worktree."""
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


class TestFixFinding:
    """Test the _fix_finding cycle with all external calls mocked."""

    def _setup(self, tmp_path):
        db = _db()
        f = _open_finding(db)
        sup = Supervisor(_cfg(), db)
        wt = _make_fake_wt(tmp_path)
        return sup, db, f, wt

    def test_happy_path_fixed_and_merged(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree") as rm_wt, \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(42, "https://gh/42")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is True
        assert db.get_finding(f["id"])["status"] == "fixed"
        assert sup.ctr["fixes_applied"] == 1
        rm_wt.assert_called_once()

    def test_worktree_removed_on_repair_failure(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree") as rm_wt, \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=False):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        rm_wt.assert_called_once()  # cleanup still runs

    def test_worktree_removed_on_verify_failure(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree") as rm_wt, \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=False):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        rm_wt.assert_called_once()
        assert db.get_finding(f["id"])["status"] == "open"

    def test_pr_record_created_before_push(self, tmp_path):
        """DB PR record (with branch) must exist before push_branch is called."""
        sup, db, f, wt = self._setup(tmp_path)
        pr_id_at_push = []

        def check_pr_exists_before_push(path, branch):
            row = db.find_pr_by_branch(branch)
            pr_id_at_push.append(row["id"] if row else None)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch",
                   side_effect=check_pr_exists_before_push), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(1, "https://gh/1")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):

            sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert pr_id_at_push[0] is not None, "PR record must exist before push"

    def test_push_failure_requeues_finding(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch",
                   side_effect=RuntimeError("network error")):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "open"
        assert sup.ctr["consecutive_failures"] == 1

    def test_gh_create_pr_failure_requeues(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   side_effect=Exception("gh error")):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "open"

    def test_gh_create_pr_uses_configured_base(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)
        # Override default_branch to something non-default
        sup.cfg = {**sup.cfg, "repo": {**sup.cfg["repo"], "default_branch": "trunk"}}
        calls = []

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   side_effect=lambda *a, **kw: calls.append(kw) or (1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):

            sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert calls[0]["base"] == "trunk"

    def test_review_failed_error_closes_pr(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(10, "https://gh/10")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_FAILED_ERROR), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_close.assert_called_once_with("org", "repo", 10)
        assert db.get_finding(f["id"])["status"] == "open"

    def test_review_deferred_budget_marks_deferred_not_blocked(self, tmp_path):
        """Codex budget exhaustion mid-review → deferred, not blocked."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(11, "https://gh/11")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_DEFERRED_BUDGET):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "deferred"
        # Deferred should be picked up again next run
        assert len(db.open_findings()) == 1

    def test_review_paused_budget_marks_blocked(self, tmp_path):
        """Review rounds exhausted → blocked (not deferred, not paused)."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(12, "https://gh/12")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_PAUSED_BUDGET), \
             patch(f"{RUNNER_MODULE}.gh_close_pr"):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "blocked"
        # Blocked should NOT be retried
        assert db.open_findings() == []

    def test_ci_failure_requeues_finding(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(13, "https://gh/13")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "open"

    def test_merge_failure_requeues_finding(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(14, "https://gh/14")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr",
                   side_effect=Exception("merge conflict")):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "open"

    def test_stale_finding_on_revalidation(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)
        # Give a different commit hash so revalidation is triggered
        db._conn.execute(
            "UPDATE findings SET commit_hash='oldhead' WHERE id=?", (f["id"],)
        )
        db._conn.commit()

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_revalidate", return_value="stale"):

            result = sup._fix_finding(db.get_finding(f["id"]), "newhead")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "stale"


# ── phases unit tests ──────────────────────────────────────────────────────────

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
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        with patch("supervisor.phases.run_codex"), \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.full_diff", return_value="diff content"), \
             patch("supervisor.phases._git") as mock_git:
            ok = phase_repair(cfg, db, [f], ctr)

        assert ok
        assert ctr["consecutive_failures"] == 0
        # add -A and commit should both be called
        calls = [c.args[1] for c in mock_git.call_args_list]
        assert "add" in calls
        assert "commit" in calls


class TestPhaseReviewLoop:
    """Review loop outcome constants."""

    def _base_mocks(self):
        return {
            "full_diff": patch("supervisor.phases.full_diff", return_value="diff"),
            "run_codex": patch("supervisor.phases.run_codex", return_value=""),
        }

    def _db_and_cfg(self):
        db = _db()
        pr_id = db.create_pr("branch")
        db.update_pr(pr_id, pr_number=1, pr_url="u")
        return db, pr_id, _cfg()

    def test_approved_on_first_round(self):
        from supervisor.phases import phase_review_loop, parse_json
        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        approved_json = '{"verdict":"approve","summary":"ok","comments":[]}'
        with patch("supervisor.phases.run_codex", return_value=approved_json), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok",
                                 "comments": []}):
            outcome = phase_review_loop(cfg, db, pr_id, [f], "branch", ctr)

        assert outcome == REVIEW_APPROVED

    def test_rounds_exhausted_returns_paused_budget(self):
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        cfg["budget"]["max_review_rounds"] = 1

        request_changes = {
            "verdict": "request_changes",
            "summary": "bad",
            "comments": [{"severity": "blocking", "description": "fix it"}],
        }
        with patch("supervisor.phases.parse_json", return_value=request_changes), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff",
                   side_effect=["diff_before", "diff_after"]), \
             patch("supervisor.phases.run_tests", return_value=True), \
             patch("supervisor.phases.verify_diff", return_value=True), \
             patch("supervisor.phases.push_branch"), \
             patch("supervisor.phases._git"):
            outcome = phase_review_loop(cfg, db, pr_id, [f], "branch", ctr)

        assert outcome == REVIEW_PAUSED_BUDGET

    def test_codex_budget_hit_returns_deferred(self):
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        cfg["budget"]["codex_call_budget"] = 100
        ctr = {"codex_calls": 100, "consecutive_failures": 0}  # already at limit

        # full_diff would fail without a real repo; budget check fires before it
        outcome = phase_review_loop(cfg, db, pr_id, [f], "branch", ctr)

        assert outcome == REVIEW_DEFERRED_BUDGET

    def test_codex_error_returns_failed_error(self):
        from supervisor.phases import phase_review_loop
        from supervisor.codex import CodexError

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        with patch("supervisor.phases.run_codex",
                   side_effect=CodexError("boom")), \
             patch("supervisor.phases.full_diff", return_value="diff"):
            outcome = phase_review_loop(cfg, db, pr_id, [f], "branch", ctr)

        assert outcome == REVIEW_FAILED_ERROR


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


# ── GitHub API failure paths ───────────────────────────────────────────────────

class TestGitHubAPIFailures:
    def test_gh_pr_state_api_error_treated_as_closed(self):
        """During reconcile, an API error on pr state → requeue finding."""
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        db.update_pr(pr_id, pr_number=5, pr_url="https://gh/5")
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="unknown"):
            sup.startup_reconcile()

        assert db.get_finding(f["id"])["status"] == "open"

    def test_gh_find_pr_by_branch_api_error_requeues(self):
        """If GitHub search fails during reconcile, finding is requeued."""
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        # No pr_number
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_find_pr_by_branch", return_value=None):
            sup.startup_reconcile()

        assert db.get_finding(f["id"])["status"] == "open"


# ── exhaustion logic ───────────────────────────────────────────────────────────

class TestExhaustion:
    def test_deferred_findings_prevent_exhaustion(self):
        """An area with a deferred finding is not considered exhausted."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "deferred")
        # Record several clean audit runs at this HEAD
        for _ in range(5):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)

        streak = db.clean_audit_streak("correctness", 3, "abc1234")
        assert streak == 0  # deferred counts as active, streak reset

    def test_fixed_finding_does_not_prevent_exhaustion(self):
        """A fixed finding does not block exhaustion counting."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "fixed")
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)

        streak = db.clean_audit_streak("correctness", 3, "abc1234")
        assert streak == 3

    def test_regression_resets_exhaustion(self):
        """A finding reopened after being fixed resets the exhaustion streak."""
        db = _db()
        from supervisor.db import _fingerprint
        fp = _fingerprint("correctness", None, "Regression bug")
        fid, _ = db.upsert_finding({
            "fingerprint": fp, "area": "correctness",
            "severity": "high", "confidence": "high",
            "file_path": None, "line_range": None,
            "title": "Regression bug", "description": "desc",
            "commit_hash": "abc1234",
        })
        db.mark_finding(fid, "fixed")
        # Accumulate clean audits
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)
        assert db.clean_audit_streak("correctness", 3, "abc1234") == 3

        # Regression: re-open the same finding
        db.upsert_finding({
            "fingerprint": fp, "area": "correctness",
            "severity": "high", "confidence": "high",
            "file_path": None, "line_range": None,
            "title": "Regression bug", "description": "updated desc",
            "commit_hash": "def5678",
        })
        assert db.clean_audit_streak("correctness", 3, "abc1234") == 0
