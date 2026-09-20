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
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(42, "https://gh/42")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
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
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
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
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
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

    def test_ci_failure_closes_pr_and_requeues(self, tmp_path):
        """Definite CI failure → close the PR, then requeue finding."""
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
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        mock_close.assert_called_once_with("org", "repo", 13)
        assert db.get_finding(f["id"])["status"] == "open"

    def test_ci_timeout_leaves_in_progress(self, tmp_path):
        """CI timeout → leave finding/PR in_progress for startup_reconcile."""
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
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="timeout"):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.ctr["consecutive_failures"] == 1

    def test_ci_api_error_leaves_in_progress(self, tmp_path):
        """CI api_error → leave finding/PR in_progress for startup_reconcile."""
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
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="api_error"):

            result = sup._fix_finding(db.get_finding(f["id"]), "abc1234")

        assert result is False
        assert db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.ctr["consecutive_failures"] == 1

    def test_merge_failure_leaves_in_progress(self, tmp_path):
        """Merge command failure is uncertain — leave in_progress for startup_reconcile."""
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
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(42, "https://gh/42")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
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
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(42, "https://gh/42")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
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
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
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
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
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
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.phase_verify", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(77, "https://gh/77")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="no_checks"), \
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
            outcome = phase_review_loop(cfg, db, pr_id, [f], "branch", ctr)

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
             patch("supervisor.phases.full_diff", return_value="diff"):
            outcome = phase_review_loop(cfg, db, pr_id, [f], "branch", ctr)

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
