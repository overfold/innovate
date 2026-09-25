"""Shared test helpers for the Maintain test suite.

All external calls (Codex, git worktree creation, GitHub CLI) are mocked so
tests are deterministic and run without network access or a real repository.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from supervisor.db import DB
from supervisor.runner import Supervisor

RUNNER_MODULE = "supervisor.runner"
GIT_MODULE = "supervisor.git"


def _db() -> DB:
    """In-memory SQLite database."""
    from supervisor.db import _SCHEMA

    db = DB.__new__(DB)
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
    fid, _ = db.upsert_finding(
        {
            "fingerprint": fp,
            "area": area,
            "severity": "high",
            "confidence": "high",
            "file_path": None,
            "line_range": None,
            "title": title,
            "description": "a description",
            "commit_hash": "abc1234",
        }
    )
    return dict(db.get_finding(fid))


def _make_fake_wt(tmp_path):
    """Return a real temp dir to stand in for the worktree."""
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


def _fix_finding_setup(tmp_path):
    """Return (sup, db, finding, worktree) for _fix_finding tests."""
    db = _db()
    f = _open_finding(db)
    sup = Supervisor(_cfg(), db)
    wt = _make_fake_wt(tmp_path)
    return sup, db, f, wt
