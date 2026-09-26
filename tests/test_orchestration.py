"""Tests for supervisor orchestration: _fix_finding lifecycle and run_once return values."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from supervisor.git import GitHubAPIError
from supervisor.phases import (
    REVIEW_APPROVED,
    REVIEW_CI_UNCERTAIN,
    REVIEW_DEFERRED_BUDGET,
    REVIEW_FAILED_ERROR,
    REVIEW_PAUSED_BUDGET,
)
from supervisor.runner import Supervisor

from tests.helpers import _cfg, _db, _open_finding, _make_fake_wt, RUNNER_MODULE


# ── _fix_finding / worktree lifecycle ──────────────────────────────────────────

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
        """A merge refreshes HEAD and resumes at the next audit area."""
        db = _db()
        f = _open_finding(db)
        wt = tmp_path / "wt"
        wt.mkdir()
        call_count = {"n": 0}
        audited = []

        cfg = _cfg()
        cfg["audit_areas"] = [
            {"name": "correctness", "description": "bugs"},
            {"name": "security", "description": "vulnerabilities"},
            {"name": "reliability", "description": "failures"},
        ]

        def make_audit_wt():
            call_count["n"] += 1
            return wt

        sup = Supervisor(cfg, db)
        sup.startup_reconcile = lambda: None

        def fake_audit(cfg, db_, area, ctr):
            audited.append(area["name"])
            return 0

        with patch(f"{RUNNER_MODULE}.create_audit_worktree",
                   side_effect=lambda repo, main: make_audit_wt()), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit",
                   side_effect=["abc1234", "def5678"]), \
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
            result = sup.run_once()

        # create_audit_worktree called twice: once per pass
        assert call_count["n"] == 2
        assert result == "done"
        assert audited[:2] == ["correctness", "security"]
        # Finding is fixed
        assert db.get_finding(f["id"])["status"] == "fixed"

    def test_audit_cursor_persists_across_run_budget_restart(self, tmp_path):
        """A run stopped after a merge resumes its next invocation at N+1."""
        db = _db()
        f = _open_finding(db)
        wt = tmp_path / "wt"
        wt.mkdir()
        audited = []

        cfg = _cfg()
        cfg["audit_areas"] = [
            {"name": "correctness", "description": "bugs"},
            {"name": "security", "description": "vulnerabilities"},
            {"name": "reliability", "description": "failures"},
        ]
        cfg["budget"]["max_fixes_per_run"] = 1

        def fake_audit(cfg_, db_, area, ctr):
            audited.append(area["name"])
            return 0

        with patch(f"{RUNNER_MODULE}.create_audit_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_audit_worktree"), \
             patch(f"{RUNNER_MODULE}.current_commit",
                   side_effect=["abc1234", "def5678", "def5678"]), \
             patch(f"{RUNNER_MODULE}.phase_audit", side_effect=fake_audit), \
             patch(f"{RUNNER_MODULE}.create_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_validate", return_value="valid"), \
             patch(f"{RUNNER_MODULE}.phase_repair", return_value=True), \
             patch(f"{RUNNER_MODULE}.push_branch"), \
             patch(f"{RUNNER_MODULE}.gh_create_pr",
                   return_value=(78, "https://gh/78")), \
             patch(f"{RUNNER_MODULE}.phase_review_loop",
                   return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr"):
            first = Supervisor(cfg, db)
            first.startup_reconcile = lambda: None
            assert first.run_once() == "budget"
            assert db.get("audit_cursor") == "security"

            restarted = Supervisor(cfg, db)
            restarted.startup_reconcile = lambda: None
            assert restarted.run_once() == "done"

        assert audited[:2] == ["correctness", "security"]
        assert db.get_finding(f["id"])["status"] == "fixed"
