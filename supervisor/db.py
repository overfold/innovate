from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

_SEV_RANK  = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY,
    fingerprint   TEXT    UNIQUE NOT NULL,
    area          TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    confidence    TEXT    NOT NULL,
    file_path     TEXT,
    line_range    TEXT,
    title         TEXT    NOT NULL,
    description   TEXT    NOT NULL,
    -- open | in_progress | fixed | rejected | stale | paused
    -- 'paused': PR hit the review-round limit; awaiting human review.
    -- 'stale':  finding no longer applies at current HEAD.
    status        TEXT    NOT NULL DEFAULT 'open',
    discovered    TEXT    NOT NULL DEFAULT (datetime('now')),
    commit_hash   TEXT,
    resolved      TEXT,
    pr_id         INTEGER,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS audit_runs (
    id           INTEGER PRIMARY KEY,
    area         TEXT    NOT NULL,
    commit_hash  TEXT,
    started      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed    TEXT,
    new_findings INTEGER DEFAULT 0,
    total_found  INTEGER DEFAULT 0,
    -- running | completed | failed
    status       TEXT    NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS prs (
    id            INTEGER PRIMARY KEY,
    branch        TEXT    NOT NULL,
    pr_number     INTEGER,
    pr_url        TEXT,
    -- open | paused | merged | closed | failed
    status        TEXT    NOT NULL DEFAULT 'open',
    created       TEXT    NOT NULL DEFAULT (datetime('now')),
    merged        TEXT,
    review_rounds INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sweeps (
    id              INTEGER PRIMARY KEY,
    started         TEXT NOT NULL DEFAULT (datetime('now')),
    completed       TEXT,
    areas_covered   INTEGER DEFAULT 0,
    findings_fixed  INTEGER DEFAULT 0,
    prs_merged      INTEGER DEFAULT 0,
    result          TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _fingerprint(area: str, file_path: str | None, title: str) -> str:
    key = f"{area}:{file_path or ''}:{title.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


class DB:
    """Thin SQLite wrapper.  All public methods commit immediately."""

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── key/value state ──────────────────────────────────────────────────────

    def get(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def put(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value)
        )
        self._conn.commit()

    # ── findings ─────────────────────────────────────────────────────────────

    def upsert_finding(self, f: dict) -> tuple[int, bool]:
        """Insert if fingerprint is new.  Returns (row_id, is_new)."""
        row = self._conn.execute(
            "SELECT id FROM findings WHERE fingerprint=?", (f["fingerprint"],)
        ).fetchone()
        if row:
            return row["id"], False
        cur = self._conn.execute(
            """INSERT INTO findings
               (fingerprint, area, severity, confidence, file_path, line_range,
                title, description, commit_hash)
               VALUES
               (:fingerprint,:area,:severity,:confidence,:file_path,:line_range,
                :title,:description,:commit_hash)""",
            f,
        )
        self._conn.commit()
        return cur.lastrowid, True

    def open_findings(self, area: str | None = None) -> list[sqlite3.Row]:
        """Return open findings, highest-priority first."""
        if area:
            rows = self._conn.execute(
                "SELECT * FROM findings WHERE status='open' AND area=?", (area,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM findings WHERE status='open'"
            ).fetchall()
        return sorted(
            rows,
            key=lambda r: (
                _SEV_RANK.get(r["severity"], 9),
                _CONF_RANK.get(r["confidence"], 9),
            ),
        )

    def paused_findings(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM findings WHERE status='paused' ORDER BY id"
        ).fetchall()

    def mark_finding(
        self,
        fid: int,
        status: str,
        pr_id: int | None = None,
        reason: str = "",
    ) -> None:
        self._conn.execute(
            """UPDATE findings
               SET status=?, resolved=datetime('now'), pr_id=?, reject_reason=?
               WHERE id=?""",
            (status, pr_id, reason, fid),
        )
        self._conn.commit()

    # ── PRs ──────────────────────────────────────────────────────────────────

    def create_pr(self, branch: str) -> int:
        cur = self._conn.execute("INSERT INTO prs(branch) VALUES(?)", (branch,))
        self._conn.commit()
        return cur.lastrowid

    def update_pr(self, pr_id: int, **kwargs: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        self._conn.execute(
            f"UPDATE prs SET {sets} WHERE id=?", (*kwargs.values(), pr_id)
        )
        self._conn.commit()

    def get_pr(self, pr_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM prs WHERE id=?", (pr_id,)
        ).fetchone()

    # ── audit runs ───────────────────────────────────────────────────────────

    def start_audit(self, area: str, commit: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO audit_runs(area, commit_hash) VALUES(?,?)", (area, commit)
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_audit(
        self, run_id: int, new: int, total: int, status: str = "completed"
    ) -> None:
        self._conn.execute(
            """UPDATE audit_runs
               SET completed=datetime('now'), new_findings=?, total_found=?, status=?
               WHERE id=?""",
            (new, total, status, run_id),
        )
        self._conn.commit()

    def clean_audit_streak(self, area: str, window: int, current_head: str) -> int:
        """Count consecutive recent clean audits (0 new findings) for *area*.

        Exhaustion is HEAD-tied: the streak is only non-zero when the MOST
        RECENT audit for this area was performed at *current_head*.  If HEAD
        has moved since the last audit, the streak resets to 0.
        """
        rows = self._conn.execute(
            """SELECT new_findings, commit_hash FROM audit_runs
               WHERE area=? AND status='completed'
               ORDER BY id DESC LIMIT ?""",
            (area, window),
        ).fetchall()
        if not rows:
            return 0
        if rows[0]["commit_hash"] != current_head:
            return 0
        return sum(1 for r in rows if r["new_findings"] == 0)

    # ── summary queries ───────────────────────────────────────────────────────

    def finding_counts(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT area, status, COUNT(*) AS n "
            "FROM findings GROUP BY area, status ORDER BY area, status"
        ).fetchall()

    def recent_prs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM prs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
