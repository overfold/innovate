"""Tests for supervisor.db — state persistence, exhaustion streaks, and HEAD-pinning."""
from __future__ import annotations

import pytest

from tests.helpers import _db, _open_finding


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
        from unittest.mock import patch
        from supervisor.runner import Supervisor
        from tests.helpers import _cfg, RUNNER_MODULE

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
