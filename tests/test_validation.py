"""Tests for the validation phase: phase_validate, state machine, persistence,
and the distinction between validation, revalidation, and verification."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from supervisor.runner import Supervisor
from supervisor.phases import REVIEW_APPROVED
from tests.helpers import _cfg, _db, _open_finding, RUNNER_MODULE


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
