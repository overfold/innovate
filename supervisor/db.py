from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

_SEV_RANK  = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT    UNIQUE NOT NULL,
    area            TEXT    NOT NULL,
    severity        TEXT    NOT NULL,
    confidence      TEXT    NOT NULL,
    file_path       TEXT,
    line_range      TEXT,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    -- open | in_progress | fixed | rejected | stale | deferred | blocked
    -- 'deferred': per-run Codex budget hit; auto-resumes next run.
    -- 'blocked':  review rounds exhausted without convergence; not retried.
    -- 'stale':    finding no longer applies at current HEAD.
    status          TEXT    NOT NULL DEFAULT 'open',
    discovered      TEXT    NOT NULL DEFAULT (datetime('now')),
    commit_hash     TEXT,
    resolved        TEXT,
    pr_id           INTEGER,
    reject_reason   TEXT,
    blocked_at_head  TEXT,   -- commit SHA where this finding was last confirmed blocked
    rejected_at_head TEXT,   -- commit SHA where repair last produced no changes
    repair_attempts  INTEGER NOT NULL DEFAULT 0,
    -- 'invalid': audit claim disproved at validated_at_head (HEAD-pinned, no repair)
    -- 'uncertain': could not confirm; blocked for human review (same as 'blocked')
    validation_verdict  TEXT,   -- valid | invalid | uncertain | NULL (not yet validated)
    validated_at_head   TEXT,   -- HEAD at which the last validation was performed
    validation_reason   TEXT,   -- validator's one-sentence conclusion
    validation_evidence TEXT    -- validator's concrete code evidence
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
    -- open | review_approved | ci_paused | deferred | merged | closed | failed
    -- 'ci_paused': reviewer approved but CI was still red at the time;
    --              _resume_paused_reviews() re-checks on the next run.
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
        # Migrations for columns added after the initial schema.
        for _migration in [
            "ALTER TABLE findings ADD COLUMN blocked_at_head TEXT",
            "ALTER TABLE findings ADD COLUMN rejected_at_head TEXT",
            "ALTER TABLE findings ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE findings ADD COLUMN validation_verdict TEXT",
            "ALTER TABLE findings ADD COLUMN validated_at_head TEXT",
            "ALTER TABLE findings ADD COLUMN validation_reason TEXT",
            "ALTER TABLE findings ADD COLUMN validation_evidence TEXT",
        ]:
            try:
                self._conn.execute(_migration)
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise

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
        """Insert or re-open a regressed finding.  Returns (row_id, is_new).

        If a finding with the same fingerprint already exists in a terminal
        state (fixed/stale/rejected) it is reopened with refreshed metadata and
        treated as new, so the regression is acted on.  Findings that are still
        open, in_progress, or paused are left untouched.
        """
        row = self._conn.execute(
            "SELECT id, status, rejected_at_head, validated_at_head"
            " FROM findings WHERE fingerprint=?",
            (f["fingerprint"],),
        ).fetchone()
        if row:
            was_rejected = row["status"] == "rejected"
            was_invalid  = row["status"] == "invalid"
            if was_rejected:
                # Rejection is terminal for that HEAD: only reopen once HEAD changes.
                rah = row["rejected_at_head"]
                fch = f.get("commit_hash")
                if rah and fch and rah == fch:
                    return row["id"], False  # same HEAD — leave rejected
                # Different (or unknown) HEAD: fall through to reopen below.
            elif was_invalid:
                # Invalidation is terminal for that HEAD: only reopen once HEAD changes.
                iah = row["validated_at_head"]
                fch = f.get("commit_hash")
                if iah and fch and iah == fch:
                    return row["id"], False  # same HEAD — leave invalid
                # Different (or unknown) HEAD: fall through to reopen below.
            elif row["status"] not in ("fixed", "stale"):
                return row["id"], False
            # Reopen: fixed/stale regression, or rejected/invalid finding at a new HEAD.
            common_args = (
                f.get("commit_hash"),
                f.get("severity"),
                f.get("confidence"),
                f.get("file_path"),
                f.get("line_range"),
                f.get("description"),
                row["id"],
            )
            if was_rejected:
                # Preserve repair_attempts: HEAD changed but the counter accumulates
                # across HEADs so the blocked cap is eventually reachable.
                self._conn.execute(
                    """UPDATE findings
                       SET status='open', resolved=NULL, pr_id=NULL,
                           reject_reason=NULL, commit_hash=?,
                           severity=?, confidence=?, file_path=?,
                           line_range=?, description=?,
                           rejected_at_head=NULL,
                           validation_verdict=NULL, validated_at_head=NULL,
                           validation_reason=NULL, validation_evidence=NULL
                       WHERE id=?""",
                    common_args,
                )
            elif was_invalid:
                # Invalid finding rediscovered at a new HEAD: fresh slate, re-validate.
                self._conn.execute(
                    """UPDATE findings
                       SET status='open', resolved=NULL, pr_id=NULL,
                           reject_reason=NULL, commit_hash=?,
                           severity=?, confidence=?, file_path=?,
                           line_range=?, description=?,
                           validation_verdict=NULL, validated_at_head=NULL,
                           validation_reason=NULL, validation_evidence=NULL,
                           repair_attempts=0
                       WHERE id=?""",
                    common_args,
                )
            else:
                # Genuine fixed/stale regression: fresh repair slate.
                self._conn.execute(
                    """UPDATE findings
                       SET status='open', resolved=NULL, pr_id=NULL,
                           reject_reason=NULL, commit_hash=?,
                           severity=?, confidence=?, file_path=?,
                           line_range=?, description=?,
                           rejected_at_head=NULL, repair_attempts=0,
                           validation_verdict=NULL, validated_at_head=NULL,
                           validation_reason=NULL, validation_evidence=NULL
                       WHERE id=?""",
                    common_args,
                )
            self._conn.commit()
            return row["id"], True
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
        """Return actionable findings (open + deferred), highest-priority first."""
        if area:
            rows = self._conn.execute(
                "SELECT * FROM findings WHERE status IN ('open','deferred') AND area=?",
                (area,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM findings WHERE status IN ('open','deferred')"
            ).fetchall()
        return sorted(
            rows,
            key=lambda r: (
                _SEV_RANK.get(r["severity"], 9),
                _CONF_RANK.get(r["confidence"], 9),
            ),
        )

    def blocked_findings(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM findings WHERE status='blocked' ORDER BY id"
        ).fetchall()

    def any_rejected_findings(self) -> bool:
        """Return True if any findings have status='rejected'."""
        return self._conn.execute(
            "SELECT 1 FROM findings WHERE status='rejected' LIMIT 1"
        ).fetchone() is not None

    def rejected_at_head_findings(self, head: str) -> list[sqlite3.Row]:
        """Return findings rejected at exactly *head* — unresolvable until HEAD changes."""
        return self._conn.execute(
            "SELECT * FROM findings WHERE status='rejected' AND rejected_at_head=?"
            " ORDER BY id",
            (head,),
        ).fetchall()

    def stale_blocked_findings(self, current_head: str) -> list[sqlite3.Row]:
        """Return blocked findings recorded at a different (or unknown) HEAD."""
        return self._conn.execute(
            "SELECT * FROM findings WHERE status='blocked'"
            " AND (blocked_at_head IS NULL OR blocked_at_head != ?)"
            " ORDER BY id",
            (current_head,),
        ).fetchall()

    def stale_rejected_findings(self, current_head: str) -> list[sqlite3.Row]:
        """Return rejected findings recorded at a different (or unknown) HEAD."""
        return self._conn.execute(
            "SELECT * FROM findings WHERE status='rejected'"
            " AND (rejected_at_head IS NULL OR rejected_at_head != ?)"
            " ORDER BY id",
            (current_head,),
        ).fetchall()

    def reopen_rejected_finding(self, fid: int) -> None:
        """Reopen a rejected finding for another repair attempt at a new HEAD.

        Preserves repair_attempts so max_repair_attempts can eventually
        convert repeatedly-failing findings to blocked.
        """
        self._conn.execute(
            """UPDATE findings
               SET status='open', resolved=NULL, pr_id=NULL,
                   reject_reason=NULL, rejected_at_head=NULL,
                   validation_verdict=NULL, validated_at_head=NULL,
                   validation_reason=NULL, validation_evidence=NULL
               WHERE id=? AND status='rejected'""",
            (fid,),
        )
        self._conn.commit()

    def deferred_findings(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM findings WHERE status='deferred' ORDER BY id"
        ).fetchall()

    def in_progress_findings(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM findings WHERE status='in_progress' ORDER BY id"
        ).fetchall()

    def set_validation(
        self, fid: int, verdict: str, head: str,
        reason: str = "", evidence: str = "",
    ) -> None:
        """Record a validation result without changing the finding status."""
        self._conn.execute(
            "UPDATE findings SET validation_verdict=?, validated_at_head=?,"
            " validation_reason=?, validation_evidence=? WHERE id=?",
            (verdict, head, reason or "", evidence or "", fid),
        )
        self._conn.commit()

    def mark_finding(
        self,
        fid: int,
        status: str,
        pr_id: int | None = None,
        reason: str = "",
        head: str | None = None,
    ) -> None:
        if status == "rejected":
            self._conn.execute(
                """UPDATE findings
                   SET status=?, resolved=datetime('now'), pr_id=?, reject_reason=?,
                       blocked_at_head=NULL, rejected_at_head=?,
                       repair_attempts=repair_attempts + 1
                   WHERE id=?""",
                (status, pr_id, reason, head, fid),
            )
        elif status == "invalid":
            # HEAD-pinned: same-HEAD rediscovery leaves the finding invalid;
            # a new HEAD clears validation state and reopens it.
            self._conn.execute(
                """UPDATE findings
                   SET status=?, resolved=datetime('now'), pr_id=?, reject_reason=?,
                       validation_verdict='invalid', validated_at_head=?,
                       blocked_at_head=NULL
                   WHERE id=?""",
                (status, pr_id, reason, head, fid),
            )
        elif status == "blocked":
            self._conn.execute(
                """UPDATE findings
                   SET status=?, resolved=datetime('now'), pr_id=?, reject_reason=?,
                       blocked_at_head=?
                   WHERE id=?""",
                (status, pr_id, reason, head, fid),
            )
        else:
            self._conn.execute(
                """UPDATE findings
                   SET status=?, resolved=datetime('now'), pr_id=?, reject_reason=?,
                       blocked_at_head=NULL
                   WHERE id=?""",
                (status, pr_id, reason, fid),
            )
        self._conn.commit()

    def refresh_blocked_head(self, fid: int, head: str) -> None:
        """Update blocked_at_head without touching any other finding fields."""
        self._conn.execute(
            "UPDATE findings SET blocked_at_head=? WHERE id=? AND status='blocked'",
            (head, fid),
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

    def find_pr_by_branch(self, branch: str) -> sqlite3.Row | None:
        """Return the most-recent PR row for *branch*, or None."""
        return self._conn.execute(
            "SELECT * FROM prs WHERE branch=? ORDER BY id DESC LIMIT 1", (branch,)
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

    def get_finding(self, fid: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM findings WHERE id=?", (fid,)
        ).fetchone()

    def clean_audit_streak(self, area: str, window: int, current_head: str) -> int:
        """Count recent audits with no new findings for *area* at *current_head*.

        A finding rejected at the current HEAD is terminal for that HEAD and
        does not block the streak.  Only open, in_progress, or deferred findings
        (all of which are actively being worked or queued) block the streak.
        All audits counted must be at exactly *current_head*.
        """
        active = self._conn.execute(
            """SELECT COUNT(*) AS n FROM findings
               WHERE area=? AND status IN ('open', 'in_progress', 'deferred')""",
            (area,),
        ).fetchone()
        if active["n"] > 0:
            return 0

        rows = self._conn.execute(
            """SELECT new_findings FROM audit_runs
               WHERE area=? AND status='completed' AND commit_hash=?
               ORDER BY id DESC LIMIT ?""",
            (area, current_head, window),
        ).fetchall()
        if not rows:
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
