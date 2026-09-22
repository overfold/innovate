"""Tests for the maintain.py supervisor state machine.

All external calls (Codex, git worktree creation, GitHub CLI) are mocked so
tests are deterministic and run without network access or a real repository.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from supervisor.db import DB
from supervisor.git import GitHubAPIError
from supervisor.phases import (
    REVIEW_APPROVED,
    REVIEW_CI_UNCERTAIN,
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
            "max_repair_attempts": 3,
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


# ── _parse_remote_owner_repo unit tests ───────────────────────────────────────

class TestParseRemoteOwnerRepo:
    def setup_method(self):
        from maintain import _parse_remote_owner_repo
        self.parse = _parse_remote_owner_repo

    def test_ssh_github(self):
        assert self.parse("git@github.com:owner/repo.git") == ("github.com", "owner/repo")

    def test_ssh_github_no_dot_git(self):
        assert self.parse("git@github.com:owner/repo") == ("github.com", "owner/repo")

    def test_https_github(self):
        assert self.parse("https://github.com/owner/repo.git") == ("github.com", "owner/repo")

    def test_https_github_no_dot_git(self):
        assert self.parse("https://github.com/owner/repo") == ("github.com", "owner/repo")

    def test_non_github_host_ssh(self):
        host, path = self.parse("git@gitlab.com:owner/repo.git")
        assert host == "gitlab.com"
        assert path == "owner/repo"

    def test_non_github_host_https(self):
        host, path = self.parse("https://example.com/owner/repo.git")
        assert host == "example.com"
        assert path == "owner/repo"

    def test_unrecognized_url_returns_none(self):
        assert self.parse("not-a-url") is None

    def test_lowercased(self):
        host, path = self.parse("git@GitHub.COM:Owner/Repo.git")
        assert host == "github.com"
        assert path == "owner/repo"

    def test_trailing_slash_stripped(self):
        host, path = self.parse("https://github.com/owner/repo/")
        assert host == "github.com"
        assert path == "owner/repo"


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
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(42, "https://gh/42")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
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
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=False):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        rm_wt.assert_called_once()  # cleanup still runs

    def test_pr_record_created_before_push(self, tmp_path):
        """DB PR record (with branch) must exist before push_branch is called."""
        sup, db, f, wt = self._setup(tmp_path)
        pr_id_at_push = []

        def check_pr_exists_before_push(path, branch):
            row = db.find_pr_by_branch(branch)
            pr_id_at_push.append(row["id"] if row else None)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch",
                   side_effect=check_pr_exists_before_push), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(1, "https://gh/1")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):

            sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert pr_id_at_push[0] is not None, "PR record must exist before push"

    def test_push_failure_requeues_finding(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
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
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
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
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   side_effect=lambda *a, **kw: calls.append(kw) or (1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):

            sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert calls[0]["base"] == "trunk"

    def test_review_failed_error_closes_pr(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
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
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
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
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
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

    def test_review_ci_uncertain_leaves_in_progress(self, tmp_path):
        """REVIEW_CI_UNCERTAIN → leave finding/PR in_progress for startup_reconcile."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(15, "https://gh/15")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_CI_UNCERTAIN):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.ctr["consecutive_failures"] == 1

    def test_merge_failure_leaves_in_progress(self, tmp_path):
        """Merge command failure is uncertain — leave in_progress for startup_reconcile."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(14, "https://gh/14")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr",
                   side_effect=Exception("merge conflict")):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.ctr["consecutive_failures"] == 1

    def test_freshness_api_error_leaves_in_progress(self, tmp_path):
        """gh_pr_base_sha API error → leave finding in_progress for startup_reconcile."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(42, "https://gh/42")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha",
                   side_effect=GitHubAPIError("network error")):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.ctr["consecutive_failures"] == 1

    def test_freshness_base_advanced_closes_and_requeues(self, tmp_path):
        """Base branch advanced since audit → close PR and requeue finding."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(42, "https://gh/42")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha",
                   return_value="newhead999"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_close.assert_called_once_with("org", "repo", 42)
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
                                 "comments": []}), \
             patch("supervisor.phases.wait_for_ci", return_value="success"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

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
             patch("supervisor.phases.push_branch"), \
             patch("supervisor.phases._git"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_PAUSED_BUDGET

    def test_codex_budget_hit_returns_deferred(self):
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        cfg["budget"]["codex_call_budget"] = 100
        ctr = {"codex_calls": 100, "consecutive_failures": 0}  # already at limit

        # full_diff would fail without a real repo; budget check fires before it
        outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

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
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

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
    def test_gh_pr_state_api_error_leaves_state_untouched(self):
        """During reconcile, a GitHub API error → leave finding in_progress (fail-closed)."""
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        db.update_pr(pr_id, pr_number=5, pr_url="https://gh/5")
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_pr_state",
                   side_effect=GitHubAPIError("transient error")):
            sup.startup_reconcile()

        # State must not change — we cannot know the real PR state
        assert db.get_finding(f["id"])["status"] == "in_progress"

    def test_gh_find_pr_by_branch_api_error_leaves_state_untouched(self):
        """If the GitHub branch search fails, leave state untouched (fail-closed)."""
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        # No pr_number — simulates crash gap
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_find_pr_by_branch",
                   side_effect=GitHubAPIError("network error")):
            sup.startup_reconcile()

        # State must not change — we cannot know whether a PR exists
        assert db.get_finding(f["id"])["status"] == "in_progress"

    def test_gh_find_pr_by_branch_not_found_requeues(self):
        """If GitHub search returns None (no PR), finding is requeued cleanly."""
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_find_pr_by_branch", return_value=None):
            sup.startup_reconcile()

        assert db.get_finding(f["id"])["status"] == "open"

    def test_gh_close_pr_failure_leaves_in_progress_open_pr(self):
        """If gh_close_pr raises during reconcile of an open PR, leave in_progress (fail-closed)."""
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        db.update_pr(pr_id, pr_number=9, pr_url="https://gh/9")
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr",
                   side_effect=GitHubAPIError("network failure")):
            sup.startup_reconcile()

        assert db.get_finding(f["id"])["status"] == "in_progress"

    def test_gh_close_pr_failure_review_error_leaves_in_progress(self, tmp_path):
        """If gh_close_pr fails after REVIEW_FAILED_ERROR, finding stays in_progress."""
        sup, db, f, wt = TestFixFinding()._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(10, "https://gh/10")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_FAILED_ERROR), \
             patch(f"{RUNNER_MODULE}.gh_close_pr",
                   side_effect=GitHubAPIError("transient")):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        # Close failed → DB state must not advance; leave in_progress for reconcile
        assert db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.ctr["consecutive_failures"] == 1

    def test_gh_close_pr_failure_paused_budget_leaves_in_progress(self, tmp_path):
        """If gh_close_pr fails after REVIEW_PAUSED_BUDGET, finding stays in_progress."""
        sup, db, f, wt = TestFixFinding()._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(12, "https://gh/12")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_PAUSED_BUDGET), \
             patch(f"{RUNNER_MODULE}.gh_close_pr",
                   side_effect=GitHubAPIError("transient")):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "in_progress"


# ── gh_ci_status unit tests ────────────────────────────────────────────────────

class TestGhCiStatus:
    """Unit tests for gh_ci_status() exit-code and bucket handling."""

    def _run(self, returncode: int, stdout: str) -> str:
        from supervisor.git import gh_ci_status
        import subprocess
        mock_result = MagicMock()
        mock_result.returncode = returncode
        mock_result.stdout = stdout
        with patch("supervisor.git.subprocess.run", return_value=mock_result):
            return gh_ci_status("org", "repo", 42)

    def test_returncode_8_always_returns_pending(self):
        """Exit code 8 is authoritative for 'pending'; bucket content is ignored."""
        assert self._run(8, '[{"bucket":"pending"}]') == "pending"
        assert self._run(8, '[{"bucket":"pass"}]') == "pending"
        assert self._run(8, "[]") == "pending"

    def test_nonzero_exit_other_than_8_returns_api_error(self):
        status = self._run(1, "")
        assert status == "api_error"

    def test_unknown_bucket_returns_api_error(self):
        """An unknown bucket value must not silently become success."""
        status = self._run(0, '[{"bucket":"unknown_future_value"}]')
        assert status == "api_error"

    def test_null_bucket_returns_api_error(self):
        """A null/missing bucket must not silently become success."""
        status = self._run(0, '[{"bucket":null}]')
        assert status == "api_error"

    def test_fail_bucket_returns_failure(self):
        status = self._run(0, '[{"bucket":"fail"}]')
        assert status == "failure"

    def test_cancel_bucket_returns_failure(self):
        status = self._run(0, '[{"bucket":"cancel"}]')
        assert status == "failure"

    def test_empty_checks_returns_no_checks(self):
        status = self._run(0, "[]")
        assert status == "no_checks"

    def test_mixed_pass_skipping_returns_success(self):
        status = self._run(0, '[{"bucket":"pass"},{"bucket":"skipping"}]')
        assert status == "success"


# ── deferred reconciliation ───────────────────────────────────────────────────

class TestDeferredReconcile:
    """startup_reconcile must close deferred PRs and requeue findings as open."""

    def test_deferred_with_pr_is_closed_and_requeued(self):
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/deferred-branch")
        db.update_pr(pr_id, pr_number=99, pr_url="https://gh/99")
        db.mark_finding(f["id"], "deferred", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:
            sup.startup_reconcile()

        mock_close.assert_called_once_with("org", "repo", 99)
        assert db.get_pr(pr_id)["status"] == "closed"
        assert db.get_finding(f["id"])["status"] == "open"

    def test_deferred_without_pr_is_requeued(self):
        """Deferred finding with no linked PR → just requeue as open."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "deferred")

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:
            sup.startup_reconcile()

        mock_close.assert_not_called()
        assert db.get_finding(f["id"])["status"] == "open"

    def test_gh_close_pr_failure_deferred_leaves_deferred(self):
        """If gh_close_pr raises during deferred reconcile, finding stays deferred."""
        db = _db()
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/deferred-branch")
        db.update_pr(pr_id, pr_number=99, pr_url="https://gh/99")
        db.mark_finding(f["id"], "deferred", pr_id=pr_id)

        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.gh_close_pr",
                   side_effect=GitHubAPIError("network error")):
            sup.startup_reconcile()

        assert db.get_finding(f["id"])["status"] == "deferred"
        assert db.get_pr(pr_id)["status"] == "open"  # DB not updated either

    def test_deferred_finding_not_in_open_findings_before_reconcile(self):
        """Verify deferred findings ARE returned by open_findings() (so the loop picks them up)."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "deferred")
        # Before reconcile, deferred is still in open_findings
        assert len(db.open_findings()) == 1
        assert db.open_findings()[0]["status"] == "deferred"


# ── run_once return values ─────────────────────────────────────────────────────

class TestRunOnceReturnValues:
    def test_blocked_findings_prevent_exhausted_return(self):
        """run_once returns 'blocked' (not 'exhausted') when blocked findings exist."""
        db = _db()
        f = _open_finding(db)
        # head="abc1234" matches current_commit mock so stale-blocked revalidation
        # skips this finding (blocked_at_head == head).
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted", head="abc1234")

        # Record enough clean audits to hit the streak threshold
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None  # skip real reconcile
        with patch(f"{RUNNER_MODULE}.create_audit_worktree") as mock_wt, \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit", return_value=0):
            mock_wt.return_value = "/tmp/fake-wt"
            result = sup.run_once()

        assert result == "blocked"

    def test_no_blocked_findings_returns_exhausted(self):
        """run_once returns 'exhausted' when all areas are clean and no blocked findings."""
        db = _db()
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None
        with patch(f"{RUNNER_MODULE}.create_audit_worktree") as mock_wt, \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit", return_value=0):
            mock_wt.return_value = "/tmp/fake-wt"
            result = sup.run_once()

        assert result == "exhausted"

    def test_fetch_failure_returns_budget(self):
        """If origin/<main> cannot be fetched, run_once returns 'budget'."""
        db = _db()
        sup = Supervisor(_cfg(), db)
        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=subprocess.CalledProcessError(1, "git fetch")):
            sup.startup_reconcile = lambda: None
            result = sup.run_once()

        assert result == "budget"

    def test_stale_blocked_finding_revalidated_stale(self):
        """run_once marks a blocked finding stale when revalidation says it's gone."""
        db = _db()
        f = _open_finding(db)
        # Blocked at old head — will be revalidated at "abc1234"
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted", head="oldhead")
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None
        with patch(f"{RUNNER_MODULE}.create_audit_worktree") as mock_wt, \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit", return_value=0), \
             patch(f"{RUNNER_MODULE}.phase_revalidate", return_value="stale"):
            mock_wt.return_value = "/tmp/fake-wt"
            result = sup.run_once()

        assert result == "exhausted"
        assert db.get_finding(f["id"])["status"] == "stale"

    def test_stale_blocked_finding_still_present_refreshes_head(self):
        """run_once keeps a finding blocked but updates blocked_at_head when revalidation says valid."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted", head="oldhead")
        # Enough clean audits so the area registers as exhausted.
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None
        with patch(f"{RUNNER_MODULE}.create_audit_worktree") as mock_wt, \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit", return_value=0), \
             patch(f"{RUNNER_MODULE}.phase_revalidate", return_value="valid"):
            mock_wt.return_value = "/tmp/fake-wt"
            result = sup.run_once()

        assert result == "blocked"
        row = db.get_finding(f["id"])
        assert row["status"] == "blocked"
        assert row["blocked_at_head"] == "abc1234"

    def test_merge_triggers_sweep_restart(self, tmp_path):
        """A successful fix+merge should cause a second pass (restart)."""
        db = _db()
        f = _open_finding(db)
        wt = tmp_path / "wt"
        wt.mkdir()
        call_count = {"n": 0}

        def make_audit_wt():
            call_count["n"] += 1
            return wt

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None

        # First pass: one open finding → gets fixed → merge returns True
        # Second pass: no open findings, area is exhausted → returns exhausted
        pass_counter = {"n": 0}

        def fake_audit(cfg, db_, area, ctr):
            pass_counter["n"] += 1
            if pass_counter["n"] == 1:
                # Finding already in DB from before; return 0 new
                return 0
            return 0

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=lambda repo, main: make_audit_wt()), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit", side_effect=fake_audit), \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(77, "https://gh/77")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            # Record 3 clean audits for the second pass to hit exhaustion
            for _ in range(3):
                run_id = db.start_audit("correctness", "abc1234")
                db.finish_audit(run_id, 0, 0)
            result = sup.run_once()

        # create_audit_worktree called twice: once per pass
        assert call_count["n"] == 2
        # Finding is fixed
        assert db.get_finding(f["id"])["status"] == "fixed"


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

    def test_stale_blocked_findings_returns_different_head(self):
        """stale_blocked_findings returns findings blocked at a different HEAD."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted", head="oldhead")
        rows = db.stale_blocked_findings("newhead")
        assert len(rows) == 1
        assert rows[0]["id"] == f["id"]

    def test_stale_blocked_findings_excludes_same_head(self):
        """stale_blocked_findings skips findings already validated at current HEAD."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted", head="abc1234")
        assert db.stale_blocked_findings("abc1234") == []

    def test_stale_blocked_findings_includes_null_head(self):
        """Findings blocked before blocked_at_head existed are treated as stale."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked", reason="rounds exhausted")  # no head
        assert len(db.stale_blocked_findings("abc1234")) == 1

    def test_refresh_blocked_head_updates_column(self):
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked", head="oldhead")
        db.refresh_blocked_head(f["id"], "newhead")
        row = db.get_finding(f["id"])
        assert row["blocked_at_head"] == "newhead"
        assert row["status"] == "blocked"

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


# ── rejected finding HEAD-pinning ──────────────────────────────────────────────

class TestRejectedHeadPinning:
    """Rejected findings must stay rejected at the same HEAD but reopen at a new one."""

    def _fp(self, f):
        from supervisor.db import _fingerprint
        return _fingerprint(f["area"], f["file_path"], f["title"])

    def test_rejected_at_same_head_not_reopened(self):
        db = _db()
        f = _open_finding(db, "no-fix bug")
        db.mark_finding(f["id"], "rejected", reason="no changes", head="abc1234")
        assert db.get_finding(f["id"])["rejected_at_head"] == "abc1234"
        assert db.get_finding(f["id"])["repair_attempts"] == 1

        _, is_new = db.upsert_finding({**f, "fingerprint": self._fp(f), "commit_hash": "abc1234"})

        assert not is_new
        assert db.get_finding(f["id"])["status"] == "rejected"

    def test_rejected_at_different_head_reopened(self):
        db = _db()
        f = _open_finding(db, "no-fix bug")
        db.mark_finding(f["id"], "rejected", reason="no changes", head="abc1234")

        _, is_new = db.upsert_finding({**f, "fingerprint": self._fp(f), "commit_hash": "newhead9"})

        assert is_new
        assert db.get_finding(f["id"])["status"] == "open"
        assert db.get_finding(f["id"])["repair_attempts"] == 1  # preserved across HEAD change
        assert db.get_finding(f["id"])["rejected_at_head"] is None

    def test_rejected_null_head_is_reopened(self):
        """Rejected with no HEAD recorded (old row) is always reopened."""
        db = _db()
        f = _open_finding(db, "legacy bug")
        db.mark_finding(f["id"], "rejected", reason="no changes")  # head=None

        _, is_new = db.upsert_finding({**f, "fingerprint": self._fp(f), "commit_hash": "anyhead"})

        assert is_new
        assert db.get_finding(f["id"])["status"] == "open"

    def test_repair_attempts_incremented_on_rejection(self):
        db = _db()
        f = _open_finding(db, "stubborn bug")
        db.mark_finding(f["id"], "rejected", reason="no changes", head="h1")
        assert db.get_finding(f["id"])["repair_attempts"] == 1
        # Reopening at a new HEAD preserves the counter so the blocked cap is reachable.
        from supervisor.db import _fingerprint
        fp = _fingerprint(f["area"], f["file_path"], f["title"])
        db.upsert_finding({**f, "fingerprint": fp, "commit_hash": "h2"})
        assert db.get_finding(f["id"])["repair_attempts"] == 1  # preserved
        # Rejecting at the new HEAD increments from 1 to 2.
        db.mark_finding(f["id"], "rejected", reason="no changes", head="h2")
        assert db.get_finding(f["id"])["repair_attempts"] == 2

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


# ── review loop: empty blocking comments ──────────────────────────────────────

class TestReviewLoopProtocol:
    def _db_and_cfg(self):
        db = _db()
        pr_id = db.create_pr("branch")
        db.update_pr(pr_id, pr_number=1, pr_url="u")
        return db, pr_id, _cfg()

    def test_request_changes_no_blocking_is_fail_closed(self):
        """request_changes with zero blocking comments is an inconsistent response — fail-closed."""
        from supervisor.phases import phase_review_loop, REVIEW_FAILED_ERROR

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        optional_only = {
            "verdict": "request_changes",
            "summary": "minor nit",
            "comments": [{"severity": "optional", "file": None, "description": "style"}],
        }
        with patch("supervisor.phases.parse_json", return_value=optional_only), \
             patch("supervisor.phases.run_codex", return_value="") as mock_codex, \
             patch("supervisor.phases.full_diff", return_value="diff"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_FAILED_ERROR
        # Only one Codex call: the review itself; no implement call.
        assert mock_codex.call_count == 1

    def test_approve_with_no_comments_returns_approved(self):
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        with patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok", "comments": []}), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.wait_for_ci", return_value="success"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_APPROVED


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


# ── phase_validate unit tests ──────────────────────────────────────────────────

class TestPhaseValidate:
    """phase_validate: state-machine and fail-closed behaviour."""

    def _cfg_and_finding(self, tmp_path):
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)
        f = _open_finding(_db())
        return cfg, f

    def test_valid_returns_valid(self, tmp_path):
        from supervisor.phases import phase_validate
        cfg, f = self._cfg_and_finding(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        valid_json = '{"verdict":"valid","reason":"issue confirmed","evidence":"line 42"}'
        with patch("supervisor.phases.run_codex", return_value=valid_json), \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "valid", "reason": "ok", "evidence": "e"}):
            result = phase_validate(cfg, f, ctr)
        assert result == "valid"
        assert ctr["codex_calls"] == 1

    def test_invalid_returns_invalid(self, tmp_path):
        from supervisor.phases import phase_validate
        cfg, f = self._cfg_and_finding(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        with patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "invalid", "reason": "no", "evidence": "e"}):
            result = phase_validate(cfg, f, ctr)
        assert result == "invalid"

    def test_uncertain_returns_uncertain(self, tmp_path):
        from supervisor.phases import phase_validate
        cfg, f = self._cfg_and_finding(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        with patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "uncertain", "reason": "?", "evidence": "e"}):
            result = phase_validate(cfg, f, ctr)
        assert result == "uncertain"

    def test_codex_error_returns_error_fail_closed(self, tmp_path):
        from supervisor.phases import phase_validate
        from supervisor.codex import CodexError
        cfg, f = self._cfg_and_finding(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        with patch("supervisor.phases.run_codex", side_effect=CodexError("boom")):
            result = phase_validate(cfg, f, ctr)
        assert result == "error"

    def test_parse_error_returns_error_fail_closed(self, tmp_path):
        from supervisor.phases import phase_validate
        cfg, f = self._cfg_and_finding(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        with patch("supervisor.phases.run_codex", return_value="not json at all"):
            result = phase_validate(cfg, f, ctr)
        assert result == "error"

    def test_unexpected_verdict_returns_error(self, tmp_path):
        from supervisor.phases import phase_validate
        cfg, f = self._cfg_and_finding(tmp_path)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        with patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "maybe", "reason": "?", "evidence": "e"}):
            result = phase_validate(cfg, f, ctr)
        assert result == "error"

    def test_budget_exhausted_returns_deferred(self, tmp_path):
        from supervisor.phases import phase_validate
        cfg, f = self._cfg_and_finding(tmp_path)
        cfg["budget"]["codex_call_budget"] = 0
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        with patch("supervisor.phases.run_codex") as mock_run:
            result = phase_validate(cfg, f, ctr)
        mock_run.assert_not_called()
        assert result == "deferred"

    def test_validate_does_not_propose_repair(self, tmp_path):
        """Validate uses audit_flags (read-only), not repair_flags."""
        from supervisor.phases import phase_validate
        cfg, f = self._cfg_and_finding(tmp_path)
        cfg["codex"]["audit_flags"] = ["exec", "--read-only"]
        cfg["codex"]["repair_flags"] = ["exec"]
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        captured_flags = []
        def capture_run(prompt, repo, flags, cmd, model, timeout, output_schema=None):
            captured_flags.extend(flags)
            return '{"verdict":"valid","reason":"r","evidence":"e"}'
        with patch("supervisor.phases.run_codex", side_effect=capture_run):
            phase_validate(cfg, f, ctr)
        assert "--read-only" in captured_flags

    def test_persists_reason_and_evidence_when_db_provided(self, tmp_path):
        """When db is provided, phase_validate stores reason and evidence in the DB."""
        from supervisor.phases import phase_validate
        cfg, _ = self._cfg_and_finding(tmp_path)
        db = _db()
        f = _open_finding(db)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}
        with patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "valid", "reason": "confirmed bug",
                                 "evidence": "line 99 is unguarded"}), \
             patch("supervisor.phases.current_commit", return_value="abc1234"):
            result = phase_validate(cfg, f, ctr, db=db)
        assert result == "valid"
        row = db.get_finding(f["id"])
        assert row["validation_verdict"] == "valid"
        assert row["validated_at_head"] == "abc1234"
        assert row["validation_reason"] == "confirmed bug"
        assert row["validation_evidence"] == "line 99 is unguarded"


# ── validation state-machine tests (via _fix_finding) ─────────────────────────

class TestValidationStateMachine:
    """Validation phase gates repair; state is persisted in SQLite."""

    def _setup(self, tmp_path):
        db = _db()
        f = _open_finding(db)
        sup = Supervisor(_cfg(), db)
        wt = tmp_path / "wt"
        wt.mkdir()
        return sup, db, f, wt

    def test_valid_finding_proceeds_to_repair(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)
        repair_called = []

        def record_repair(cfg, db_, findings, ctr):
            repair_called.append(True)
            return True

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", side_effect=record_repair), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert repair_called, "phase_repair must be called for a valid finding"

    def test_invalid_finding_terminates_no_repair(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="invalid"), \
             patch(f"{RUNNER_MODULE}.phase_repair") as mock_repair:
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_repair.assert_not_called()
        row = db.get_finding(f["id"])
        assert row["status"] == "invalid"
        assert row["validated_at_head"] == "abc1234"
        assert row["validation_verdict"] == "invalid"

    def test_uncertain_finding_blocked_no_repair(self, tmp_path):
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="uncertain"), \
             patch(f"{RUNNER_MODULE}.phase_repair") as mock_repair:
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_repair.assert_not_called()
        assert db.get_finding(f["id"])["status"] == "blocked"
        assert "uncertain" in db.get_finding(f["id"])["reject_reason"]

    def test_validation_error_requeues_finding(self, tmp_path):
        """Codex/parse/schema error during validation → open (retryable) + failure counter."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="error"), \
             patch(f"{RUNNER_MODULE}.phase_repair") as mock_repair:
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_repair.assert_not_called()
        assert db.get_finding(f["id"])["status"] == "open"
        assert sup.ctr["consecutive_failures"] == 1

    def test_validation_deferred_requeues_no_failure(self, tmp_path):
        """Budget exhaustion during validation → open (retryable), no failure increment."""
        sup, db, f, wt = self._setup(tmp_path)

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="deferred"), \
             patch(f"{RUNNER_MODULE}.phase_repair") as mock_repair:
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_repair.assert_not_called()
        assert db.get_finding(f["id"])["status"] == "open"
        assert sup.ctr["consecutive_failures"] == 0

    def test_valid_cached_at_head_skips_codex(self, tmp_path):
        """Already validated as valid at current HEAD → phase_validate not called again."""
        sup, db, f, wt = self._setup(tmp_path)
        # Pre-seed: validated as valid at the same HEAD
        db.set_validation(f["id"], "valid", "abc1234")

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate") as mock_val, \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        mock_val.assert_not_called()

    def test_valid_at_different_head_not_cached(self, tmp_path):
        """Validated as valid at a different HEAD → re-validate at current HEAD."""
        sup, db, f, wt = self._setup(tmp_path)
        # finding commit_hash matches current_head so revalidation is skipped,
        # isolating the validation-cache check.
        db._conn.execute(
            "UPDATE findings SET commit_hash='newhead1' WHERE id=?", (f["id"],)
        )
        db._conn.commit()
        db.set_validation(f["id"], "valid", "oldhead000")

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid") as mock_val, \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="newhead1"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            sup._fix_finding(db.get_finding(f["id"]), "newhead1")

        mock_val.assert_called_once()


# ── validation DB / HEAD-pinning tests ────────────────────────────────────────

class TestValidationDB:
    """DB-level: invalid HEAD-pinning, set_validation, and exhaustion semantics."""

    def _fp(self, f):
        from supervisor.db import _fingerprint
        return _fingerprint(f["area"], f["file_path"], f["title"])

    def test_invalid_at_same_head_not_reopened(self):
        db = _db()
        f = _open_finding(db, "false alarm")
        db.mark_finding(f["id"], "invalid", head="abc1234")
        row = db.get_finding(f["id"])
        assert row["status"] == "invalid"
        assert row["validated_at_head"] == "abc1234"
        assert row["validation_verdict"] == "invalid"

        _, is_new = db.upsert_finding({**f, "fingerprint": self._fp(f),
                                       "commit_hash": "abc1234"})
        assert not is_new
        assert db.get_finding(f["id"])["status"] == "invalid"

    def test_invalid_at_different_head_reopened(self):
        """Same finding at a new HEAD → reopen for re-validation."""
        db = _db()
        f = _open_finding(db, "false alarm")
        db.mark_finding(f["id"], "invalid", head="abc1234")

        _, is_new = db.upsert_finding({**f, "fingerprint": self._fp(f),
                                       "commit_hash": "newhead9"})
        assert is_new
        row = db.get_finding(f["id"])
        assert row["status"] == "open"
        assert row["validation_verdict"] is None
        assert row["validated_at_head"] is None

    def test_invalid_null_head_reopened(self):
        """Invalid with no HEAD recorded is always reopened (legacy safety)."""
        db = _db()
        f = _open_finding(db, "legacy false alarm")
        db.mark_finding(f["id"], "invalid")  # head=None

        _, is_new = db.upsert_finding({**f, "fingerprint": self._fp(f),
                                       "commit_hash": "anyhead"})
        assert is_new
        assert db.get_finding(f["id"])["status"] == "open"

    def test_invalid_findings_not_in_open_findings(self):
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "invalid", head="abc1234")
        assert db.open_findings() == []

    def test_invalid_findings_dont_block_exhaustion_streak(self):
        """Invalid findings don't count as active → don't reset exhaustion streak."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "invalid", head="abc1234")
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)
        assert db.clean_audit_streak("correctness", 3, "abc1234") == 3

    def test_invalid_findings_dont_prevent_exhausted_return(self):
        """run_once returns 'exhausted' even with invalid findings at the same HEAD."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "invalid", head="abc1234")
        for _ in range(3):
            run_id = db.start_audit("correctness", "abc1234")
            db.finish_audit(run_id, 0, 0)

        sup = Supervisor(_cfg(), db)
        sup.startup_reconcile = lambda: None
        with patch(f"{RUNNER_MODULE}.create_audit_worktree") as mock_wt, \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_audit", return_value=0):
            mock_wt.return_value = "/tmp/fake-wt"
            result = sup.run_once()

        assert result == "exhausted"

    def test_set_validation_stores_verdict_head_reason_evidence(self):
        db = _db()
        f = _open_finding(db)
        db.set_validation(f["id"], "valid", "abc1234",
                          reason="issue confirmed", evidence="line 42 shows bug")
        row = db.get_finding(f["id"])
        assert row["validation_verdict"] == "valid"
        assert row["validated_at_head"] == "abc1234"
        assert row["validation_reason"] == "issue confirmed"
        assert row["validation_evidence"] == "line 42 shows bug"
        assert row["status"] == "open"  # status unchanged

    def test_set_validation_overwrite(self):
        """set_validation can be called multiple times; last value wins."""
        db = _db()
        f = _open_finding(db)
        db.set_validation(f["id"], "valid", "head1", reason="r1", evidence="e1")
        db.set_validation(f["id"], "uncertain", "head2", reason="r2", evidence="e2")
        row = db.get_finding(f["id"])
        assert row["validation_verdict"] == "uncertain"
        assert row["validated_at_head"] == "head2"
        assert row["validation_reason"] == "r2"
        assert row["validation_evidence"] == "e2"

    def test_uncertain_finding_becomes_blocked_not_open(self):
        """Uncertain validation → blocked, not open."""
        db = _db()
        f = _open_finding(db)
        db.mark_finding(f["id"], "blocked",
                        reason="validation uncertain — requires human review",
                        head="abc1234")
        assert db.open_findings() == []
        assert len(db.blocked_findings()) == 1

    def test_reopened_invalid_finding_clears_validation_state(self):
        """When an invalid finding is reopened at a new HEAD, all validation state is wiped."""
        db = _db()
        f = _open_finding(db, "clearedstate")
        db.set_validation(f["id"], "invalid", "head1",
                          reason="not a bug", evidence="code is guarded")
        db.mark_finding(f["id"], "invalid", head="head1")

        db.upsert_finding({**f, "fingerprint": self._fp(f), "commit_hash": "head2"})
        row = db.get_finding(f["id"])
        assert row["validation_verdict"] is None
        assert row["validated_at_head"] is None
        assert row["validation_reason"] is None
        assert row["validation_evidence"] is None


# ── validation persistence / crash-restart ────────────────────────────────────

class TestValidationPersistence:
    """Crash-restart: validated findings at the same HEAD skip re-validation."""

    def test_crash_after_validate_before_repair_skips_revalidation(self, tmp_path):
        """If validated=valid at current HEAD was persisted, the next run skips
        the Codex validation call and goes straight to repair."""
        db = _db()
        f = _open_finding(db)
        # Simulate: validation was run and persisted, then process crashed before repair.
        db.set_validation(f["id"], "valid", "abc1234")

        sup = Supervisor(_cfg(), db)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate") as mock_val, \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is True
        mock_val.assert_not_called()

    def test_new_head_after_crash_reruns_validation(self, tmp_path):
        """Cached validation at old HEAD is NOT reused when HEAD changes."""
        db = _db()
        f = _open_finding(db)
        # commit_hash matches current_head so revalidation is skipped, letting
        # us test the validation-cache logic in isolation.
        db._conn.execute(
            "UPDATE findings SET commit_hash='newhead2' WHERE id=?", (f["id"],)
        )
        db._conn.commit()
        db.set_validation(f["id"], "valid", "oldhead1")

        sup = Supervisor(_cfg(), db)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid") as mock_val, \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr", return_value=(1, "u")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="newhead2"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            result = sup._fix_finding(db.get_finding(f["id"]), "newhead2")

        assert result is True
        mock_val.assert_called_once()

    def test_crash_after_invalid_verdict_skips_revalidation(self, tmp_path):
        """Cached invalid verdict at current HEAD → mark invalid without Codex call."""
        db = _db()
        f = _open_finding(db)
        db.set_validation(f["id"], "invalid", "abc1234",
                          reason="not a bug", evidence="guard exists")

        sup = Supervisor(_cfg(), db)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate") as mock_val, \
             patch(f"{RUNNER_MODULE}.phase_repair") as mock_repair:
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_val.assert_not_called()
        mock_repair.assert_not_called()
        row = db.get_finding(f["id"])
        assert row["status"] == "invalid"
        assert row["validated_at_head"] == "abc1234"

    def test_crash_after_uncertain_verdict_skips_revalidation(self, tmp_path):
        """Cached uncertain verdict at current HEAD → mark blocked without Codex call."""
        db = _db()
        f = _open_finding(db)
        db.set_validation(f["id"], "uncertain", "abc1234",
                          reason="unclear", evidence="ambiguous path")

        sup = Supervisor(_cfg(), db)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate") as mock_val, \
             patch(f"{RUNNER_MODULE}.phase_repair") as mock_repair:
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_val.assert_not_called()
        mock_repair.assert_not_called()
        row = db.get_finding(f["id"])
        assert row["status"] == "blocked"
        assert "uncertain" in row["reject_reason"]


# ── validate vs revalidate vs verify distinction ──────────────────────────────

class TestValidationPhaseDistinction:
    """Confirm validation is distinct from revalidation and verification."""

    def test_revalidation_stale_skips_validation(self, tmp_path):
        """If revalidation marks a finding stale, validation is never called."""
        db = _db()
        f = _open_finding(db)
        db._conn.execute(
            "UPDATE findings SET commit_hash='oldhead' WHERE id=?", (f["id"],)
        )
        db._conn.commit()

        sup = Supervisor(_cfg(), db)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_revalidate", return_value="stale"), \
             patch(f"{RUNNER_MODULE}.phase_validate") as mock_val:
            result = sup._fix_finding(db.get_finding(f["id"]), "newhead")

        assert result is False
        mock_val.assert_not_called()
        assert db.get_finding(f["id"])["status"] == "stale"


# ── per-stage model configuration ─────────────────────────────────────────────

class TestPerStageModel:
    """Each stage uses its own model when configured; falls back to codex.model."""

    def _cfg_with_stage_model(self, stage: str, model: str) -> dict:
        cfg = _cfg()
        cfg["codex"][f"{stage}_model"] = model
        return cfg

    def _ctr(self):
        return {"codex_calls": 0, "consecutive_failures": 0}

    def test_audit_uses_stage_model(self, tmp_path):
        from supervisor.phases import phase_audit

        db = _db()
        cfg = self._cfg_with_stage_model("audit", "o1")
        cfg["repo"]["path"] = str(tmp_path)
        area = {"name": "correctness", "description": "bugs"}

        with patch("supervisor.phases.run_codex",
                   return_value='{"findings":[]}') as mock_codex, \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.parse_json", return_value={"findings": []}):
            phase_audit(cfg, db, area, self._ctr())

        assert mock_codex.call_args.kwargs["model"] == "o1"

    def test_audit_falls_back_to_default_model(self, tmp_path):
        from supervisor.phases import phase_audit

        db = _db()
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)
        area = {"name": "correctness", "description": "bugs"}

        with patch("supervisor.phases.run_codex",
                   return_value='{"findings":[]}') as mock_codex, \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.parse_json", return_value={"findings": []}):
            phase_audit(cfg, db, area, self._ctr())

        assert mock_codex.call_args.kwargs["model"] == "o4-mini"

    def test_revalidate_uses_stage_model(self, tmp_path):
        from supervisor.phases import phase_revalidate

        cfg = self._cfg_with_stage_model("revalidate", "o1-mini")
        cfg["repo"]["path"] = str(tmp_path)
        finding = {
            "area": "correctness", "title": "bug", "file_path": None,
            "line_range": None, "description": "desc",
        }

        with patch("supervisor.phases.run_codex", return_value="") as mock_codex, \
             patch("supervisor.phases.parse_json",
                   return_value={"still_applies": True, "reason": ""}):
            phase_revalidate(cfg, finding, self._ctr())

        assert mock_codex.call_args.kwargs["model"] == "o1-mini"

    def test_validate_uses_stage_model(self, tmp_path):
        from supervisor.phases import phase_validate

        cfg = self._cfg_with_stage_model("validate", "o3")
        cfg["repo"]["path"] = str(tmp_path)
        finding = {
            "area": "correctness", "title": "bug", "file_path": None,
            "line_range": None, "description": "desc",
        }

        with patch("supervisor.phases.run_codex", return_value="") as mock_codex, \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "valid", "reason": "", "evidence": ""}):
            phase_validate(cfg, finding, self._ctr())

        assert mock_codex.call_args.kwargs["model"] == "o3"

    def test_repair_uses_stage_model(self, tmp_path):
        from supervisor.phases import phase_repair

        db = _db()
        f = _open_finding(db)
        cfg = self._cfg_with_stage_model("repair", "o1")
        cfg["repo"]["path"] = str(tmp_path)

        with patch("supervisor.phases.run_codex") as mock_codex, \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.full_diff", return_value="diff content"), \
             patch("supervisor.phases._git"):
            phase_repair(cfg, db, [f], self._ctr())

        assert mock_codex.call_args.kwargs["model"] == "o1"

    def test_review_uses_stage_model_for_review_call(self):
        from supervisor.phases import phase_review_loop

        db = _db()
        pr_id = db.create_pr("branch")
        db.update_pr(pr_id, pr_number=1, pr_url="u")
        f = _open_finding(db)
        cfg = self._cfg_with_stage_model("review", "o3-mini")
        ctr = self._ctr()

        with patch("supervisor.phases.run_codex", return_value="") as mock_codex, \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok",
                                 "comments": []}), \
             patch("supervisor.phases.wait_for_ci", return_value="success"):
            phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert mock_codex.call_args.kwargs["model"] == "o3-mini"

    def test_review_uses_stage_model_for_implement_call(self):
        from supervisor.phases import phase_review_loop

        db = _db()
        pr_id = db.create_pr("branch")
        db.update_pr(pr_id, pr_number=1, pr_url="u")
        f = _open_finding(db)
        cfg = self._cfg_with_stage_model("review", "o3-mini")
        cfg["budget"]["max_review_rounds"] = 1
        ctr = self._ctr()

        request_changes = {
            "verdict": "request_changes",
            "summary": "fix it",
            "comments": [{"severity": "blocking", "description": "bad code"}],
        }
        # Two codex calls per round: review then implement.
        call_models = []

        def capture_codex(*args, **kwargs):
            call_models.append(kwargs.get("model"))
            return ""

        with patch("supervisor.phases.run_codex",
                   side_effect=capture_codex), \
             patch("supervisor.phases.full_diff",
                   side_effect=["diff_before", "diff_after"]), \
             patch("supervisor.phases.parse_json", return_value=request_changes), \
             patch("supervisor.phases.push_branch"), \
             patch("supervisor.phases._git"):
            phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        # Both the review call and the implement call must use the review model.
        assert all(m == "o3-mini" for m in call_models), call_models

    def test_null_stage_model_falls_back_to_default(self, tmp_path):
        """Explicitly setting a stage model to None still falls back to codex.model."""
        from supervisor.phases import phase_audit

        db = _db()
        cfg = _cfg()
        cfg["codex"]["audit_model"] = None
        cfg["repo"]["path"] = str(tmp_path)
        area = {"name": "correctness", "description": "bugs"}

        with patch("supervisor.phases.run_codex",
                   return_value='{"findings":[]}') as mock_codex, \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.parse_json", return_value={"findings": []}):
            phase_audit(cfg, db, area, self._ctr())

        assert mock_codex.call_args.kwargs["model"] == "o4-mini"

    def test_different_stages_can_use_different_models(self, tmp_path):
        """audit_model and repair_model are independent."""
        from supervisor.phases import phase_audit, phase_repair

        db = _db()
        cfg = _cfg()
        cfg["codex"]["audit_model"] = "o1"
        cfg["codex"]["repair_model"] = "o3"
        cfg["repo"]["path"] = str(tmp_path)
        area = {"name": "correctness", "description": "bugs"}

        audit_models = []
        repair_models = []

        def capture(*args, **kwargs):
            audit_models.append(kwargs.get("model"))
            return '{"findings":[]}'

        with patch("supervisor.phases.run_codex", side_effect=capture), \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.parse_json", return_value={"findings": []}):
            phase_audit(cfg, db, area, self._ctr())

        f = _open_finding(db)

        def capture_repair(*args, **kwargs):
            repair_models.append(kwargs.get("model"))
            return ""

        with patch("supervisor.phases.run_codex", side_effect=capture_repair), \
             patch("supervisor.phases.current_commit", return_value="abc"), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases._git"):
            phase_repair(cfg, db, [f], self._ctr())

        assert audit_models == ["o1"]
        assert repair_models == ["o3"]


# ── model_for_stage helper ─────────────────────────────────────────────────────

class TestModelForStage:
    """model_for_stage returns the stage model or falls back to codex.model."""

    def test_returns_stage_model_when_set(self):
        from supervisor.phases import model_for_stage
        cfg = _cfg()
        cfg["codex"]["repair_model"] = "o1"
        assert model_for_stage(cfg, "repair") == "o1"

    def test_falls_back_when_none(self):
        from supervisor.phases import model_for_stage
        cfg = _cfg()
        cfg["codex"]["repair_model"] = None
        assert model_for_stage(cfg, "repair") == "o4-mini"

    def test_falls_back_when_empty_string(self):
        from supervisor.phases import model_for_stage
        cfg = _cfg()
        cfg["codex"]["repair_model"] = ""
        assert model_for_stage(cfg, "repair") == "o4-mini"

    def test_falls_back_when_key_absent(self):
        from supervisor.phases import model_for_stage
        cfg = _cfg()
        # _cfg() does not include stage model keys
        assert model_for_stage(cfg, "audit") == "o4-mini"

    def test_each_stage_independent(self):
        from supervisor.phases import model_for_stage
        cfg = _cfg()
        cfg["codex"]["audit_model"] = "o1"
        cfg["codex"]["repair_model"] = "o3"
        assert model_for_stage(cfg, "audit") == "o1"
        assert model_for_stage(cfg, "repair") == "o3"
        assert model_for_stage(cfg, "verify") == "o4-mini"  # unset → fallback


# ── model config validation ────────────────────────────────────────────────────

class TestModelConfigValidation:
    """Invalid model values are caught at preflight, not during a Codex call."""

    def _run_validate(self, codex_overrides: dict, tmp_path):
        """Return the SystemExit message, or None if validation passes."""
        from maintain import _validate_config

        cfg_file = tmp_path / "config.toml"
        cfg_file.touch()

        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path)
        cfg["codex"].update(codex_overrides)

        owner, name = cfg["repo"]["owner"], cfg["repo"]["name"]
        remote_url = f"https://github.com/{owner}/{name}.git"

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = remote_url if "remote" in cmd else ".git"
            return r

        with patch("maintain.shutil.which", return_value="/usr/bin/tool"), \
             patch("maintain.subprocess.run", side_effect=fake_run):
            try:
                _validate_config(cfg, str(cfg_file))
                return None
            except SystemExit as exc:
                return str(exc.args[0])

    def test_valid_model_passes(self, tmp_path):
        assert self._run_validate({"model": "o4-mini"}, tmp_path) is None

    def test_empty_model_fails(self, tmp_path):
        msg = self._run_validate({"model": ""}, tmp_path)
        assert msg is not None
        assert "codex.model" in msg

    def test_whitespace_only_model_fails(self, tmp_path):
        msg = self._run_validate({"model": "   "}, tmp_path)
        assert msg is not None
        assert "codex.model" in msg

    def test_integer_model_fails(self, tmp_path):
        msg = self._run_validate({"model": 123}, tmp_path)
        assert msg is not None
        assert "codex.model" in msg

    def test_integer_stage_model_fails(self, tmp_path):
        msg = self._run_validate({"repair_model": 123}, tmp_path)
        assert msg is not None
        assert "codex.repair_model" in msg

    def test_bool_stage_model_fails(self, tmp_path):
        msg = self._run_validate({"audit_model": True}, tmp_path)
        assert msg is not None
        assert "codex.audit_model" in msg

    def test_string_stage_model_passes(self, tmp_path):
        assert self._run_validate({"repair_model": "o1"}, tmp_path) is None

    def test_none_stage_model_passes(self, tmp_path):
        assert self._run_validate({"repair_model": None}, tmp_path) is None

    def test_empty_string_stage_model_passes(self, tmp_path):
        """Empty string is valid — it means use the fallback codex.model."""
        assert self._run_validate({"repair_model": ""}, tmp_path) is None

    def test_multiple_invalid_stage_models_all_reported(self, tmp_path):
        msg = self._run_validate({"audit_model": 1, "review_model": False}, tmp_path)
        assert msg is not None
        assert "codex.audit_model" in msg
        assert "codex.review_model" in msg


# ── managed-clone / _resolve_repo_path ────────────────────────────────────────

class TestManagedClone:
    """_resolve_repo_path and _validate_config with managed-workspace cloning."""

    def _cfg_no_path(self, **overrides) -> dict:
        cfg = _cfg()
        cfg["repo"]["path"] = ""
        cfg["repo"].update(overrides)
        return cfg

    def _make_cfg_file(self, tmp_path) -> str:
        p = tmp_path / "config.toml"
        p.touch()
        return str(p)

    # ── _resolve_repo_path ──────────────────────────────────────────────────

    def test_resolve_sets_managed_path_when_path_empty(self, tmp_path):
        from maintain import _resolve_repo_path
        cfg = self._cfg_no_path(workspace=str(tmp_path / "ws"))
        _resolve_repo_path(cfg)
        assert cfg["repo"]["path"] == str(tmp_path / "ws" / "org" / "repo")

    def test_resolve_default_workspace(self):
        from maintain import _resolve_repo_path
        cfg = self._cfg_no_path()
        _resolve_repo_path(cfg)
        expected = str(Path.home() / ".maintain" / "workspaces" / "org" / "repo")
        assert cfg["repo"]["path"] == expected

    def test_resolve_no_op_when_path_already_set(self, tmp_path):
        from maintain import _resolve_repo_path
        cfg = _cfg()
        original = cfg["repo"]["path"]
        cfg["repo"]["workspace"] = str(tmp_path / "ws")
        _resolve_repo_path(cfg)
        assert cfg["repo"]["path"] == original

    def test_resolve_no_op_without_owner(self, tmp_path):
        from maintain import _resolve_repo_path
        cfg = self._cfg_no_path(owner="", workspace=str(tmp_path / "ws"))
        _resolve_repo_path(cfg)
        assert cfg["repo"]["path"] == ""

    def test_resolve_no_op_without_name(self, tmp_path):
        from maintain import _resolve_repo_path
        cfg = self._cfg_no_path(name="", workspace=str(tmp_path / "ws"))
        _resolve_repo_path(cfg)
        assert cfg["repo"]["path"] == ""

    def test_resolve_custom_workspace_via_tilde(self, tmp_path, monkeypatch):
        from maintain import _resolve_repo_path
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = self._cfg_no_path(workspace="~/myws")
        _resolve_repo_path(cfg)
        assert cfg["repo"]["path"] == str(tmp_path / "myws" / "org" / "repo")

    # ── _validate_config: managed clone on first run ────────────────────────

    def test_validate_clones_managed_path(self, tmp_path):
        from maintain import _validate_config, _resolve_repo_path
        cfg = self._cfg_no_path(workspace=str(tmp_path / "ws"))
        cfg_file = self._make_cfg_file(tmp_path)
        _resolve_repo_path(cfg)

        cloned_path = tmp_path / "ws" / "org" / "repo"
        remote_url = "https://github.com/org/repo.git"

        def fake_clone(url, dest):
            assert url == "https://github.com/org/repo"
            assert Path(dest) == cloned_path
            cloned_path.mkdir(parents=True, exist_ok=True)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = remote_url if "remote" in cmd else ".git"
            return r

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.clone_repo", side_effect=fake_clone) as mock_clone, \
             patch("maintain.subprocess.run", side_effect=fake_run):
            _validate_config(cfg, cfg_file)  # must not raise

        mock_clone.assert_called_once()

    def test_validate_reuses_existing_managed_path(self, tmp_path):
        from maintain import _validate_config, _resolve_repo_path
        cfg = self._cfg_no_path(workspace=str(tmp_path / "ws"))
        cfg_file = self._make_cfg_file(tmp_path)

        managed_path = tmp_path / "ws" / "org" / "repo"
        managed_path.mkdir(parents=True)
        _resolve_repo_path(cfg)

        remote_url = "https://github.com/org/repo.git"

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = remote_url if "remote" in cmd else ".git"
            return r

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.clone_repo") as mock_clone, \
             patch("maintain.subprocess.run", side_effect=fake_run):
            _validate_config(cfg, cfg_file)

        mock_clone.assert_not_called()

    def test_validate_clone_failure_aborts(self, tmp_path):
        from maintain import _validate_config, _resolve_repo_path
        cfg = self._cfg_no_path(workspace=str(tmp_path / "ws"))
        cfg_file = self._make_cfg_file(tmp_path)
        _resolve_repo_path(cfg)

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.clone_repo", side_effect=RuntimeError("repository not found")), \
             pytest.raises(SystemExit) as exc_info:
            _validate_config(cfg, cfg_file)

        assert "Failed to clone" in str(exc_info.value)
        assert "repository not found" in str(exc_info.value)

    def test_validate_managed_path_not_git_repo_fails_closed(self, tmp_path):
        from maintain import _validate_config, _resolve_repo_path
        cfg = self._cfg_no_path(workspace=str(tmp_path / "ws"))
        cfg_file = self._make_cfg_file(tmp_path)

        managed_path = tmp_path / "ws" / "org" / "repo"
        managed_path.mkdir(parents=True)
        _resolve_repo_path(cfg)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if "rev-parse" in cmd:
                r.returncode = 128
                r.stdout = r.stderr = ""
            else:
                r.returncode = 0
                r.stdout = ".git"
            return r

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.subprocess.run", side_effect=fake_run), \
             pytest.raises(SystemExit) as exc_info:
            _validate_config(cfg, cfg_file)

        msg = str(exc_info.value)
        assert "not a git repository" in msg.lower()
        assert "remove it" in msg.lower()

    def test_validate_managed_wrong_remote_fails_closed(self, tmp_path):
        from maintain import _validate_config, _resolve_repo_path
        cfg = self._cfg_no_path(workspace=str(tmp_path / "ws"))
        cfg_file = self._make_cfg_file(tmp_path)

        managed_path = tmp_path / "ws" / "org" / "repo"
        managed_path.mkdir(parents=True)
        _resolve_repo_path(cfg)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = "https://github.com/evil-corp/repo.git" if "remote" in cmd else ".git"
            return r

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.subprocess.run", side_effect=fake_run), \
             pytest.raises(SystemExit) as exc_info:
            _validate_config(cfg, cfg_file)

        assert "evil-corp/repo" in str(exc_info.value)

    # ── _validate_config: explicit path (existing behaviour preserved) ──────

    def test_validate_explicit_missing_path_errors_no_clone(self, tmp_path):
        from maintain import _validate_config
        cfg = _cfg()
        cfg["repo"]["path"] = str(tmp_path / "nonexistent")
        cfg_file = self._make_cfg_file(tmp_path)

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.clone_repo") as mock_clone, \
             pytest.raises(SystemExit) as exc_info:
            _validate_config(cfg, cfg_file)

        assert "does not exist" in str(exc_info.value)
        mock_clone.assert_not_called()

    def test_validate_explicit_path_inside_workspace_not_cloned(self, tmp_path):
        """An explicit repo.path that happens to sit inside the workspace root
        must be treated as explicitly configured — provenance wins over location."""
        from maintain import _validate_config
        ws_root = tmp_path / "ws"
        # Explicit path is inside the workspace root but NOT created on disk.
        explicit_path = ws_root / "org" / "repo"
        cfg = _cfg()
        cfg["repo"]["path"] = str(explicit_path)  # explicitly set
        cfg["repo"]["workspace"] = str(ws_root)
        cfg_file = self._make_cfg_file(tmp_path)

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.clone_repo") as mock_clone, \
             pytest.raises(SystemExit) as exc_info:
            _validate_config(cfg, cfg_file)

        assert "does not exist" in str(exc_info.value)
        mock_clone.assert_not_called()

    # ── managed_workspace_root ──────────────────────────────────────────────

    def test_managed_workspace_root_default(self):
        from maintain import _managed_workspace_root
        cfg = _cfg()
        cfg["repo"]["workspace"] = ""
        root = _managed_workspace_root(cfg)
        assert root == Path.home() / ".maintain" / "workspaces"

    def test_managed_workspace_root_custom(self, tmp_path):
        from maintain import _managed_workspace_root
        cfg = _cfg()
        cfg["repo"]["workspace"] = str(tmp_path / "custom")
        root = _managed_workspace_root(cfg)
        assert root == (tmp_path / "custom").resolve()

    def test_validate_managed_path_traversal_rejected(self, tmp_path):
        """owner/name containing .. must not escape the workspace root."""
        from maintain import _validate_config, _resolve_repo_path
        ws = tmp_path / "ws"
        ws.mkdir()
        cfg = self._cfg_no_path(workspace=str(ws))
        cfg["repo"]["owner"] = "../evil"
        cfg["repo"]["name"] = "repo"
        _resolve_repo_path(cfg)
        cfg_file = self._make_cfg_file(tmp_path)

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.clone_repo") as mock_clone, \
             pytest.raises(SystemExit) as exc_info:
            _validate_config(cfg, cfg_file)

        mock_clone.assert_not_called()
        assert "escapes" in str(exc_info.value).lower() or "traversal" in str(exc_info.value).lower() or "workspace" in str(exc_info.value).lower()

    def test_validate_managed_symlink_escape_rejected(self, tmp_path):
        """A symlink under the workspace that resolves outside it must be rejected."""
        import os
        from maintain import _validate_config, _resolve_repo_path
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # Create a symlink inside ws that points outside
        link = ws / "escape"
        link.symlink_to(outside)

        cfg = self._cfg_no_path(workspace=str(ws))
        cfg["repo"]["owner"] = "escape"
        cfg["repo"]["name"] = "repo"
        _resolve_repo_path(cfg)
        cfg_file = self._make_cfg_file(tmp_path)

        with patch("maintain.shutil.which", return_value="/usr/bin/git"), \
             patch("maintain.clone_repo") as mock_clone, \
             pytest.raises(SystemExit) as exc_info:
            _validate_config(cfg, cfg_file)

        mock_clone.assert_not_called()
        assert "escapes" in str(exc_info.value).lower() or "workspace" in str(exc_info.value).lower()


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


# ── CI integration tests ───────────────────────────────────────────────────────

class TestCIIntegration:
    """Regression tests for the unified reviewer+CI loop in phase_review_loop."""

    def _db_and_cfg(self):
        db = _db()
        pr_id = db.create_pr("branch")
        db.update_pr(pr_id, pr_number=1, pr_url="u")
        return db, pr_id, _cfg()

    def _ctr(self):
        return {"codex_calls": 0, "consecutive_failures": 0}

    def test_review_approval_ci_success_merges(self):
        """review approve + CI success → REVIEW_APPROVED."""
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = self._ctr()

        with patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok", "comments": []}), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.wait_for_ci", return_value="success"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_APPROVED

    def test_ci_failure_reviewer_gets_evidence_then_merges(self):
        """approve → CI failure → reviewer gets CI evidence and approves → REVIEW_APPROVED.

        When the reviewer sees CI failure evidence and still approves, they've
        explicitly cleared the failure as unrelated.  CI is NOT re-polled; the
        reviewer's approval is authoritative.
        """
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        cfg["budget"]["max_review_rounds"] = 3
        f = _open_finding(db)
        ctr = self._ctr()

        ci_calls = {"n": 0}

        def fake_ci(*args, **kwargs):
            ci_calls["n"] += 1
            return "failure"

        with patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok", "comments": []}), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.wait_for_ci", side_effect=fake_ci), \
             patch("supervisor.phases.gh_get_failed_ci_logs", return_value="::error:: test failed"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_APPROVED
        # CI is polled exactly once (round 1 fails), then skipped in round 2
        # because the reviewer saw the evidence and still approved.
        assert ci_calls["n"] == 1

    def test_ci_driven_edits_require_fresh_review(self):
        """CI failure → reviewer sees evidence → requests changes → implementation →
        fresh review (no CI evidence) → CI success → REVIEW_APPROVED.

        This is the key regression test for the CI-feedback-driven edit path:
        proves that after a CI-triggered code change, the modified revision
        receives a fresh review before CI can permit merge.
        """
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        cfg["budget"]["max_review_rounds"] = 4
        f = _open_finding(db)
        ctr = self._ctr()

        review_call = {"n": 0}
        parse_responses = [
            # Round 1: approve (no CI evidence yet)
            {"verdict": "approve", "summary": "lgtm", "comments": []},
            # Round 2: reviewer sees CI evidence → requests changes
            {"verdict": "request_changes", "summary": "ci failure is related",
             "comments": [{"severity": "blocking", "file": None,
                           "description": "fix the broken test"}]},
            # Round 3: fresh review after implementation → approve
            {"verdict": "approve", "summary": "all good", "comments": []},
        ]

        def fake_parse(output):
            resp = parse_responses[min(review_call["n"], len(parse_responses) - 1)]
            review_call["n"] += 1
            return resp

        ci_call = {"n": 0}

        def fake_ci(*args, **kwargs):
            ci_call["n"] += 1
            return "failure" if ci_call["n"] == 1 else "success"

        with patch("supervisor.phases.parse_json", side_effect=fake_parse), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff",
                   side_effect=["diff1", "diff2", "diff3", "diff4"]), \
             patch("supervisor.phases.push_branch"), \
             patch("supervisor.phases._git"), \
             patch("supervisor.phases.wait_for_ci", side_effect=fake_ci), \
             patch("supervisor.phases.gh_get_failed_ci_logs",
                   return_value="::error:: flaky_test failed"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_APPROVED
        assert review_call["n"] == 3   # all three rounds reviewed
        # CI polled twice: failing in round 1, then succeeding in round 3
        # (round 2 requests changes and implements, so no CI poll there).
        assert ci_call["n"] == 2

    def test_repeated_review_requests_exhaust_budget_and_block(self):
        """Reviewer requests changes every round → rounds exhausted → REVIEW_PAUSED_BUDGET."""
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        cfg["budget"]["max_review_rounds"] = 2
        f = _open_finding(db)
        ctr = self._ctr()

        with patch("supervisor.phases.parse_json",
                   return_value={"verdict": "request_changes", "summary": "still bad",
                                 "comments": [{"severity": "blocking", "file": None,
                                               "description": "still needs work"}]}), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff",
                   side_effect=["diff1", "diff2", "diff3", "diff4"]), \
             patch("supervisor.phases.push_branch"), \
             patch("supervisor.phases._git"):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        # Both rounds request changes and implement; budget exhausted → blocked.
        assert outcome == REVIEW_PAUSED_BUDGET

    def test_uncertain_ci_state_preserves_pr_no_code_changes(self):
        """CI timeout/api_error → REVIEW_CI_UNCERTAIN; no code changes made."""
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = self._ctr()

        git_calls = []

        def track_git(repo, *args, **kwargs):
            git_calls.append(args)

        with patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok", "comments": []}), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.wait_for_ci", return_value="timeout"), \
             patch("supervisor.phases._git", side_effect=track_git):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_CI_UNCERTAIN
        # No git commits or pushes should have been made.
        assert not any("commit" in str(c) for c in git_calls)

    def test_ci_log_retrieval_failure_is_uncertain(self):
        """CI fails but log retrieval returns None → REVIEW_CI_UNCERTAIN (not passed to reviewer)."""
        from supervisor.phases import phase_review_loop

        db, pr_id, cfg = self._db_and_cfg()
        f = _open_finding(db)
        ctr = self._ctr()

        with patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok", "comments": []}), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.wait_for_ci", return_value="failure"), \
             patch("supervisor.phases.gh_get_failed_ci_logs", return_value=None):
            outcome = phase_review_loop(cfg, db, pr_id, 1, [f], "branch", ctr)

        assert outcome == REVIEW_CI_UNCERTAIN


class TestStartupReconcileCIPaused:
    """Regression tests for startup_reconcile handling of ci_paused PRs (Issue 1)."""

    def _sup(self):
        return Supervisor(_cfg(), _db())

    def _make_ci_paused_pr(self, sup, pr_number: int = 10):
        """Create a finding + ci_paused PR row and return (finding, pr_id)."""
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/ts")
        sup.db.update_pr(pr_id, pr_number=pr_number, pr_url=f"https://gh/{pr_number}",
                         status="ci_paused")
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)
        return f, pr_id

    def test_ci_paused_ci_now_passes_merges(self):
        """ci_paused PR whose CI now passes is merged in startup_reconcile."""
        sup = self._sup()
        f, pr_id = self._make_ci_paused_pr(sup, pr_number=10)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="success"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr") as mock_merge:
            sup.startup_reconcile()

        mock_merge.assert_called_once_with("org", "repo", 10)
        assert sup.db.get_finding(f["id"])["status"] == "fixed"
        assert sup.db.get_pr(pr_id)["status"] == "merged"

    def test_ci_paused_ci_now_fails_closes_and_requeues(self):
        """ci_paused PR whose CI now definitively fails is closed and finding requeued."""
        sup = self._sup()
        f, pr_id = self._make_ci_paused_pr(sup, pr_number=11)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:
            sup.startup_reconcile()

        mock_close.assert_called_once_with("org", "repo", 11)
        assert sup.db.get_finding(f["id"])["status"] == "open"
        assert sup.db.get_pr(pr_id)["status"] == "closed"

    def test_ci_paused_ci_still_uncertain_left_alone(self):
        """ci_paused PR with still-uncertain CI is left untouched."""
        sup = self._sup()
        f, pr_id = self._make_ci_paused_pr(sup, pr_number=12)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="timeout"):
            sup.startup_reconcile()

        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"

    def test_ci_paused_merge_failure_left_for_next_run(self):
        """ci_paused PR that passes CI but fails to merge is left ci_paused for next run."""
        sup = self._sup()
        f, pr_id = self._make_ci_paused_pr(sup, pr_number=13)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="success"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr",
                   side_effect=Exception("merge conflict")):
            sup.startup_reconcile()

        # Left ci_paused — next run will retry
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"

    def test_fix_finding_marks_pr_ci_paused_on_uncertain_ci(self, tmp_path):
        """_fix_finding marks PR ci_paused (not just in_progress) when CI is uncertain."""
        db = _db()
        f = _open_finding(db)
        sup = Supervisor(_cfg(), db)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(20, "https://gh/20")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_CI_UNCERTAIN):
            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        pr_row = db.find_pr_by_branch(db.get_finding(f["id"])["pr_id"] and
                                       db.get_pr(db.get_finding(f["id"])["pr_id"])["branch"]
                                       or "")
        # The PR should be marked ci_paused, not plain open
        prs = db.recent_prs(1)
        assert prs[0]["status"] == "ci_paused"
