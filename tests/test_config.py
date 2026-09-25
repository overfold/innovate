"""Tests for configuration parsing, model selection, and validation."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import _cfg, _db, _open_finding


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
