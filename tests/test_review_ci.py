"""Tests for the review/CI subsystem: phase_review_loop, CI status helpers,
_resume_paused_reviews, and the CI integration paths through the review loop."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

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
from tests.helpers import _cfg, _db, _open_finding, RUNNER_MODULE


# ── phase_review_loop unit tests ───────────────────────────────────────────────

class TestPhaseReviewLoop:
    """Review loop outcome constants."""

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


# ── review loop protocol ──────────────────────────────────────────────────────

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


# ── gh_ci_status unit tests ────────────────────────────────────────────────────

class TestGhCiStatus:
    """Unit tests for gh_ci_status() exit-code and bucket handling."""

    def _run(self, returncode: int, stdout: str) -> str:
        from supervisor.git import gh_ci_status
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

    def test_cancel_bucket_returns_api_error(self):
        status = self._run(0, '[{"bucket":"cancel"}]')
        assert status == "api_error"

    def test_empty_checks_returns_no_checks(self):
        status = self._run(0, "[]")
        assert status == "no_checks"

    def test_mixed_pass_skipping_returns_success(self):
        status = self._run(0, '[{"bucket":"pass"},{"bucket":"skipping"}]')
        assert status == "success"


# ── gh_get_failed_ci_logs ─────────────────────────────────────────────────────

class TestGhGetFailedCiLogs:
    """Unit tests for gh_get_failed_ci_logs fail-open/fail-closed behaviour."""

    def _run(self, responses: list) -> "str | None":
        """Call gh_get_failed_ci_logs with mocked subprocess.run calls."""
        from supervisor.git import gh_get_failed_ci_logs

        call_iter = iter(responses)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            rc, stdout = next(call_iter)
            r.returncode = rc
            r.stdout = stdout
            return r

        with patch("supervisor.git.subprocess.run", side_effect=fake_run):
            return gh_get_failed_ci_logs("org", "repo", 1)

    def test_all_log_fetches_fail_returns_none(self):
        """When runs exist but every log-fetch call fails, return None (fail-closed)."""
        # pr view → headRefOid
        head_resp = (0, '{"headRefOid": "abc"}')
        # run list → two failed runs
        runs_resp = (0, '[{"databaseId": 1}, {"databaseId": 2}]')
        # both gh run view calls fail
        log1_resp = (1, "")
        log2_resp = (1, "")
        result = self._run([head_resp, runs_resp, log1_resp, log2_resp])
        assert result is None

    def test_some_log_fetches_succeed_returns_logs(self):
        """When at least one log fetch succeeds, return the concatenated output."""
        head_resp = (0, '{"headRefOid": "abc"}')
        runs_resp = (0, '[{"databaseId": 1}, {"databaseId": 2}]')
        log1_resp = (1, "")          # first fetch fails
        log2_resp = (0, "log line")  # second succeeds
        result = self._run([head_resp, runs_resp, log1_resp, log2_resp])
        assert result is not None
        assert "log line" in result

    def test_no_failed_runs_returns_empty_string(self):
        """When the run list is empty, return '' (not None — no infrastructure error)."""
        head_resp = (0, '{"headRefOid": "abc"}')
        runs_resp = (0, "[]")
        result = self._run([head_resp, runs_resp])
        assert result == ""


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

    def test_ci_failure_reviewer_clears_failure_returns_ci_uncertain(self):
        """approve → CI failure → reviewer gets CI evidence and approves → REVIEW_CI_UNCERTAIN.

        When the reviewer sees CI failure evidence and still approves, they've
        declared the failure unrelated — but we cannot merge while CI is red.
        Return REVIEW_CI_UNCERTAIN so the PR enters ci_paused and
        _resume_paused_reviews re-checks once CI clears.
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

        assert outcome == REVIEW_CI_UNCERTAIN
        # CI is polled exactly once (round 1 fails); round 2 sees ci_evidence
        # already set and returns REVIEW_CI_UNCERTAIN without re-polling.
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


# ── _resume_paused_reviews ────────────────────────────────────────────────────

class TestResumePausedReviews:
    """Tests for Supervisor._resume_paused_reviews."""

    def _sup_with_paused_pr(self, pr_number: int = 20):
        """Return (sup, finding, pr_id) with one ci_paused PR."""
        db = _db()
        sup = Supervisor(_cfg(), db)
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        db.update_pr(pr_id, pr_number=pr_number, branch="fix/branch",
                     pr_url=f"https://gh/{pr_number}", status="ci_paused")
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)
        return sup, f, pr_id

    def test_no_paused_prs_returns_false(self):
        """_resume_paused_reviews returns False and does nothing when no ci_paused PRs."""
        db = _db()
        sup = Supervisor(_cfg(), db)
        _open_finding(db)  # open, not in_progress
        with patch(f"{RUNNER_MODULE}.wait_for_ci") as mock_ci:
            result = sup._resume_paused_reviews("abc1234")
        mock_ci.assert_not_called()
        assert result is False

    def test_base_advanced_closes_before_ci_check(self):
        """Base advanced → close + requeue immediately, no CI poll or log collection."""
        sup, f, pr_id = self._sup_with_paused_pr(20)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="different_sha"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close, \
             patch(f"{RUNNER_MODULE}.wait_for_ci") as mock_ci, \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs") as mock_logs, \
             patch(f"{RUNNER_MODULE}.create_branch_worktree") as mock_wt:
            result = sup._resume_paused_reviews("abc1234")
        mock_close.assert_called_once_with("org", "repo", 20)
        mock_ci.assert_not_called()
        mock_logs.assert_not_called()
        mock_wt.assert_not_called()
        assert result is False
        assert sup.db.get_finding(f["id"])["status"] == "open"
        assert sup.db.get_pr(pr_id)["status"] == "closed"

    def test_base_sha_fetch_fails_leaves_paused(self):
        """gh_pr_base_sha failure → leave ci_paused, increment failures, no CI poll."""
        sup, f, pr_id = self._sup_with_paused_pr(33)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha",
                   side_effect=GitHubAPIError("timeout")), \
             patch(f"{RUNNER_MODULE}.wait_for_ci") as mock_ci:
            result = sup._resume_paused_reviews("abc1234")
        mock_ci.assert_not_called()
        assert result is False
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"
        assert sup.ctr["consecutive_failures"] == 1

    def test_base_advanced_close_fails_preserves_db(self):
        """Base advanced + gh_close_pr fails → DB left untouched (fail-closed)."""
        sup, f, pr_id = self._sup_with_paused_pr(30)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="different_sha"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr", side_effect=GitHubAPIError("net")):
            result = sup._resume_paused_reviews("abc1234")
        assert result is False
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.ctr["consecutive_failures"] == 1

    def test_ci_passes_base_fresh_merges_returns_true(self):
        """Base fresh + CI success → merge, mark fixed, return True."""
        sup, f, pr_id = self._sup_with_paused_pr(21)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="success"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr") as mock_merge:
            result = sup._resume_paused_reviews("abc1234")
        mock_merge.assert_called_once_with("org", "repo", 21)
        assert result is True
        assert sup.db.get_finding(f["id"])["status"] == "fixed"
        assert sup.db.get_pr(pr_id)["status"] == "merged"

    def test_ci_uncertain_leaves_paused_returns_false(self):
        """Base fresh + uncertain CI leaves PR ci_paused and returns False."""
        sup, f, pr_id = self._sup_with_paused_pr(22)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="timeout"):
            result = sup._resume_paused_reviews("abc1234")
        assert result is False
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"

    def test_ci_failure_stale_base_closes_not_reviews(self):
        """Regression: stale base + CI failure → close/requeue, no logs/review/checkout."""
        sup, f, pr_id = self._sup_with_paused_pr(34)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="stale_sha"), \
             patch(f"{RUNNER_MODULE}.gh_close_pr") as mock_close, \
             patch(f"{RUNNER_MODULE}.wait_for_ci") as mock_ci, \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs") as mock_logs, \
             patch(f"{RUNNER_MODULE}.phase_review_loop") as mock_review, \
             patch(f"{RUNNER_MODULE}.create_branch_worktree") as mock_wt:
            result = sup._resume_paused_reviews("abc1234")
        mock_close.assert_called_once_with("org", "repo", 34)
        mock_ci.assert_not_called()
        mock_logs.assert_not_called()
        mock_review.assert_not_called()
        mock_wt.assert_not_called()
        assert result is False
        assert sup.db.get_finding(f["id"])["status"] == "open"
        assert sup.db.get_pr(pr_id)["status"] == "closed"

    def test_ci_failure_log_retrieval_fails_leaves_paused(self):
        """Base fresh + CI failure but log retrieval returns None → leave ci_paused."""
        sup, f, pr_id = self._sup_with_paused_pr(23)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value=None):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"
        assert sup.ctr["consecutive_failures"] == 1

    def test_ci_failure_branch_fetch_fails_leaves_paused(self, tmp_path):
        """CI failure → create_branch_worktree raises (fetch fails) → ci_paused, failure bump, phase_review_loop not called."""
        sup, f, pr_id = self._sup_with_paused_pr(33)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree",
                   side_effect=subprocess.CalledProcessError(1, "git fetch")) as mock_wt, \
             patch(f"{RUNNER_MODULE}.phase_review_loop") as mock_review:
            sup._resume_paused_reviews("abc1234")
        mock_wt.assert_called_once()
        mock_review.assert_not_called()
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"
        assert sup.ctr["consecutive_failures"] == 1

    def test_ci_failure_empty_logs_leaves_paused_no_failure_count(self):
        """CI failure but logs == '' (no failed runs) → leave ci_paused, no failure bump."""
        sup, f, pr_id = self._sup_with_paused_pr(24)
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value=""):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"
        assert sup.ctr["consecutive_failures"] == 0

    def test_ci_failure_review_loop_approved_merges_returns_true(self, tmp_path):
        """CI failure → logs → REVIEW_APPROVED → merge → return True."""
        sup, f, pr_id = self._sup_with_paused_pr(25)
        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_APPROVED), \
             patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.gh_merge_pr") as mock_merge:
            result = sup._resume_paused_reviews("abc1234")
        mock_merge.assert_called_once_with("org", "repo", 25)
        assert result is True
        assert sup.db.get_finding(f["id"])["status"] == "fixed"

    def test_ci_failure_review_loop_ci_uncertain_leaves_paused(self, tmp_path):
        """CI failure → review loop returns REVIEW_CI_UNCERTAIN → leave ci_paused."""
        sup, f, pr_id = self._sup_with_paused_pr(26)
        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_CI_UNCERTAIN):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"

    def test_ci_failure_review_loop_failed_error_closes_and_requeues(self, tmp_path):
        """CI failure → REVIEW_FAILED_ERROR → close PR, requeue."""
        sup, f, pr_id = self._sup_with_paused_pr(27)
        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_FAILED_ERROR), \
             patch(f"{RUNNER_MODULE}.gh_close_pr"):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_finding(f["id"])["status"] == "open"
        assert sup.db.get_pr(pr_id)["status"] == "closed"

    def test_ci_failure_review_loop_failed_error_close_fails_preserves_db(self, tmp_path):
        """REVIEW_FAILED_ERROR + gh_close_pr fails → DB left untouched (fail-closed)."""
        sup, f, pr_id = self._sup_with_paused_pr(31)
        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_FAILED_ERROR), \
             patch(f"{RUNNER_MODULE}.gh_close_pr", side_effect=GitHubAPIError("net")):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"

    def test_ci_failure_review_loop_paused_budget_blocks(self, tmp_path):
        """CI failure → REVIEW_PAUSED_BUDGET → close PR, mark blocked."""
        sup, f, pr_id = self._sup_with_paused_pr(28)
        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_PAUSED_BUDGET), \
             patch(f"{RUNNER_MODULE}.gh_close_pr"):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_finding(f["id"])["status"] == "blocked"
        assert sup.db.get_pr(pr_id)["status"] == "closed"

    def test_ci_failure_review_loop_paused_budget_close_fails_preserves_db(self, tmp_path):
        """REVIEW_PAUSED_BUDGET + gh_close_pr fails → DB left untouched (fail-closed)."""
        sup, f, pr_id = self._sup_with_paused_pr(32)
        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_PAUSED_BUDGET), \
             patch(f"{RUNNER_MODULE}.gh_close_pr", side_effect=GitHubAPIError("net")):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_pr(pr_id)["status"] == "ci_paused"
        assert sup.db.get_finding(f["id"])["status"] == "in_progress"

    def test_ci_failure_review_loop_deferred_budget_defers(self, tmp_path):
        """CI failure → REVIEW_DEFERRED_BUDGET → defer PR and finding."""
        sup, f, pr_id = self._sup_with_paused_pr(29)
        wt = tmp_path / "wt"
        wt.mkdir()
        with patch(f"{RUNNER_MODULE}.gh_pr_base_sha", return_value="abc1234"), \
             patch(f"{RUNNER_MODULE}.wait_for_ci", return_value="failure"), \
             patch(f"{RUNNER_MODULE}.gh_get_failed_ci_logs", return_value="::error::"), \
             patch(f"{RUNNER_MODULE}.create_branch_worktree", return_value=wt), \
             patch(f"{RUNNER_MODULE}.remove_branch_worktree"), \
             patch(f"{RUNNER_MODULE}.phase_review_loop", return_value=REVIEW_DEFERRED_BUDGET):
            sup._resume_paused_reviews("abc1234")
        assert sup.db.get_finding(f["id"])["status"] == "deferred"
        assert sup.db.get_pr(pr_id)["status"] == "deferred"


# ── review_rounds cumulativity ────────────────────────────────────────────────

class TestPhaseReviewLoopCumulativeRounds:
    """Regression tests: review_rounds is cumulative across ci_paused retries."""

    def _db_and_cfg(self):
        db = _db()
        cfg = _cfg()
        cfg["budget"]["max_review_rounds"] = 3
        cfg["verify"]["ci_wait_timeout"] = 0
        cfg["verify"]["allow_no_ci"] = False
        return db, cfg

    def _make_pr_with_rounds(self, db, rounds_used: int):
        """Return (pr_id, finding) with review_rounds already set."""
        f = _open_finding(db)
        pr_id = db.create_pr("maint/correctness/ts")
        db.update_pr(pr_id, pr_number=10, branch="fix/b",
                     pr_url="https://gh/10", review_rounds=rounds_used, status="ci_paused")
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)
        return pr_id, f

    def test_rounds_budget_exhausted_returns_paused_budget(self):
        """When review_rounds already == max_review_rounds, loop body never runs."""
        from supervisor.phases import phase_review_loop, REVIEW_PAUSED_BUDGET

        db, cfg = self._db_and_cfg()
        pr_id, f = self._make_pr_with_rounds(db, rounds_used=3)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        with patch("supervisor.phases.run_codex") as mock_codex:
            outcome = phase_review_loop(
                cfg, db, pr_id, 10, [dict(f)], "fix/b", ctr,
            )

        mock_codex.assert_not_called()
        assert outcome == REVIEW_PAUSED_BUDGET

    def test_partial_rounds_used_continues_from_next_round(self):
        """With 2 of 3 rounds used, loop starts at round 3 and can still approve."""
        from supervisor.phases import phase_review_loop, REVIEW_APPROVED

        db, cfg = self._db_and_cfg()
        pr_id, f = self._make_pr_with_rounds(db, rounds_used=2)
        ctr = {"codex_calls": 0, "consecutive_failures": 0}

        with patch("supervisor.phases.parse_json",
                   return_value={"verdict": "approve", "summary": "ok", "comments": []}), \
             patch("supervisor.phases.run_codex", return_value=""), \
             patch("supervisor.phases.full_diff", return_value="diff"), \
             patch("supervisor.phases.wait_for_ci", return_value="success"):
            outcome = phase_review_loop(
                cfg, db, pr_id, 10, [dict(f)], "fix/b", ctr,
            )

        assert outcome == REVIEW_APPROVED
