"""Tests for startup_reconcile, deferred reconciliation, open-PR crash recovery,
and GitHub API failure paths during reconciliation."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from supervisor.git import GitHubAPIError
from supervisor.phases import REVIEW_APPROVED
from supervisor.runner import Supervisor
from tests.helpers import _cfg, _db, _fix_finding_setup, _open_finding, RUNNER_MODULE


# ── startup_reconcile tests ────────────────────────────────────────────────────

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

    def test_in_progress_open_pr_preserved_for_resume(self):
        """Open PR on restart stays linked to the finding for crash recovery."""
        sup = self._sup()
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/ts")
        sup.db.update_pr(pr_id, pr_number=9, pr_url="https://gh/9")
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close:
            sup.startup_reconcile()

        mock_close.assert_not_called()
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.db.get_finding(f["id"])["pr_id"] == pr_id
        assert sup.db.get_pr(pr_id)["status"] == "open"

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
        # pr_number should now be recorded and the existing PR preserved.
        assert sup.db.get_pr(pr_id)["pr_number"] == 55
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.db.get_pr(pr_id)["status"] == "open"

    def test_crash_after_pr_create_github_not_found(self):
        """Branch recorded but GitHub finds no matching PR: requeue without orphan."""
        sup = self._sup()
        f = _open_finding(sup.db)
        pr_id = sup.db.create_pr("maint/correctness/20240102")
        sup.db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        with patch(f"{RUNNER_MODULE}.gh_find_pr_by_branch", return_value=None):
            sup.startup_reconcile()

        assert sup.db.get_finding(f["id"])["status"] == "open"


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


# ── GitHub API failure paths during reconciliation ────────────────────────────

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
        from supervisor.phases import REVIEW_FAILED_ERROR

        sup, db, f, wt = _fix_finding_setup(tmp_path)

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
        from supervisor.phases import REVIEW_PAUSED_BUDGET

        sup, db, f, wt = _fix_finding_setup(tmp_path)

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


# ── startup_reconcile: ci_paused PRs ─────────────────────────────────────────

class TestStartupReconcileCIPaused:
    """startup_reconcile leaves ci_paused PRs alone; _resume_paused_reviews handles them."""

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

    def test_ci_paused_open_pr_left_untouched_by_startup_reconcile(self):
        """startup_reconcile never touches ci_paused PRs — _resume_paused_reviews owns them."""
        sup = self._sup()
        f, pr_id = self._make_ci_paused_pr(sup, pr_number=10)

        with patch(f"{RUNNER_MODULE}.gh_pr_state", return_value="open"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci") as mock_ci:
            sup.startup_reconcile()

        # wait_for_ci must NOT have been called for this ci_paused PR
        mock_ci.assert_not_called()
        # PR and finding state are unchanged
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"

    def test_fix_finding_marks_pr_ci_paused_on_uncertain_ci(self, tmp_path):
        """_fix_finding marks PR ci_paused (not just in_progress) when CI is uncertain."""
        from supervisor.phases import REVIEW_CI_UNCERTAIN

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


# ── open-PR crash recovery ────────────────────────────────────────────────────

class TestResumeOpenReviews:
    """Crash recovery resumes an existing open PR instead of replacing it."""

    def _sup_with_open_pr(self, pr_number: int = 50, status: str = "open"):
        db = _db()
        sup = Supervisor(_cfg(), db)
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/original")
        db.update_pr(
            pr_id,
            pr_number=pr_number,
            pr_url=f"https://gh/{pr_number}",
            status=status,
        )
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)
        return sup, f, pr_id

    def test_open_pr_resumes_existing_branch_and_merges(self, tmp_path):
        from pathlib import Path

        sup, f, pr_id = self._sup_with_open_pr(50)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt) as mock_wt, \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr") as mock_merge, \
             patch(f"{RUNNER_MODULE}.gh_create_pr") as mock_create:
            result = sup._resume_open_reviews("abc1234")

        assert result is True
        mock_wt.assert_called_once_with(Path("/fake/repo"), "maint/correctness/original")
        mock_create.assert_not_called()
        mock_merge.assert_called_once_with("org", "repo", 50)
        assert sup.db.get_finding(f["id"])["status"] == "fixed"
        assert sup.db.get_pr(pr_id)["status"] == "merged"

    def test_review_approved_crash_state_skips_review_and_retries_merge(self):
        sup, f, pr_id = self._sup_with_open_pr(51, status="review_approved")

        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop") as mock_review, \
             patch(f"{RUNNER_MODULE}.create_branch_worktree") as mock_wt, \
             patch(f"{RUNNER_MODULE}.gh_merge_pr") as mock_merge:
            result = sup._resume_open_reviews("abc1234")

        assert result is True
        mock_review.assert_not_called()
        mock_wt.assert_not_called()
        mock_merge.assert_called_once_with("org", "repo", 51)
        assert sup.db.get_finding(f["id"])["status"] == "fixed"
        assert sup.db.get_pr(pr_id)["status"] == "merged"

    def test_merge_failure_persists_review_approved_for_next_run(self, tmp_path):
        sup, f, pr_id = self._sup_with_open_pr(52)
        wt = tmp_path / "wt"
        wt.mkdir()

        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr", side_effect=RuntimeError("network")):
            result = sup._resume_open_reviews("abc1234")

        assert result is False
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.db.get_pr(pr_id)["status"] == "review_approved"

    def test_stale_open_pr_is_closed_and_requeued(self):
        sup, f, pr_id = self._sup_with_open_pr(53)

        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="new-head"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close, \
             patch(f"{RUNNER_MODULE}.phase_review_loop") as mock_review:
            result = sup._resume_open_reviews("abc1234")

        assert result is False
        mock_close.assert_called_once_with("org", "repo", 53)
        mock_review.assert_not_called()
        assert sup.db.get_finding(f["id"])["status"] == "open"
        assert sup.db.get_pr(pr_id)["status"] == "closed"
