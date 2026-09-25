"""Tests for managed-clone / _resolve_repo_path behavior."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import _cfg


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
        assert (
            "escapes" in str(exc_info.value).lower()
            or "traversal" in str(exc_info.value).lower()
            or "workspace" in str(exc_info.value).lower()
        )

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
        assert (
            "escapes" in str(exc_info.value).lower()
            or "workspace" in str(exc_info.value).lower()
        )
