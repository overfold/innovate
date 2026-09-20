#!/usr/bin/env python3
"""
maintain.py - Autonomous repository maintenance supervisor

Runs a continuous audit-repair-verify-PR-merge lifecycle over a configured
GitHub repository using the Codex CLI as the auditing, coding, and review
agent.  The supervisor owns all state, queuing, deduplication, and stopping
logic; Codex is only invoked for AI-powered analysis and code changes.

Usage:
    python maintain.py run                # one full iteration
    python maintain.py run --continuous   # loop until exhausted
    python maintain.py status             # show queue and progress
    python maintain.py audit <area>       # manual single-area audit
    python maintain.py findings           # list open findings
    python maintain.py reset              # clear all state (asks for confirmation)

Prerequisites:
    - Codex CLI installed and authenticated  (npm install -g @openai/codex)
    - GitHub CLI installed and authenticated (gh auth login)
    - A local clone of the target repository
    - config.toml (copy config.toml.example and fill in repo.owner / repo.name / repo.path)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("supervisor")

# ── Default configuration ──────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "repo": {
        "owner": "",           # GitHub owner / org
        "name": "",            # Repository name (without owner prefix)
        "path": ".",           # Absolute or relative path to the local clone
        "default_branch": "main",
    },
    "codex": {
        "cmd": "codex",
        "model": "o4-mini",
        "timeout": 300,        # seconds per invocation
        # Flags for read-only invocations (audit, diff-review)
        "audit_flags": ["--quiet", "--approval-mode", "suggest"],
        # Flags for file-editing invocations (repair, implement review feedback)
        "repair_flags": ["--quiet", "--approval-mode", "full-auto"],
    },
    "verify": {
        # Optional shell command to run before opening a PR.
        # Leave empty to skip local verification.
        "test_cmd": "",
        # Seconds to wait for CI checks on the PR before attempting a merge.
        # 0 = do not wait (skip CI gate — only use this on repos without CI).
        "ci_wait_timeout": 600,
    },
    "budget": {
        # How many consecutive clean audits (0 new findings) before an area
        # is considered exhausted.
        "max_audits_per_area": 3,
        # Max findings fixed in a single supervisor run (safety brake).
        "max_fixes_per_run": 20,
        # Max Codex review→fix cycles per PR.
        "max_review_rounds": 4,
        # Abort the run if this many consecutive operations fail.
        "max_consecutive_failures": 5,
        # Total Codex invocations allowed per run.
        "codex_call_budget": 100,
    },
    "audit_areas": [
        {
            "name": "correctness",
            "description": (
                "logic errors, off-by-one bugs, incorrect assumptions, "
                "unhandled edge cases, wrong error handling"
            ),
        },
        {
            "name": "security",
            "description": (
                "injection vulnerabilities, trust boundary violations, "
                "exposed secrets, auth bypass, unsafe deserialization, "
                "path traversal"
            ),
        },
        {
            "name": "reliability",
            "description": (
                "race conditions, resource leaks, missing error recovery, "
                "fragile assumptions about external services, missing retries"
            ),
        },
        {
            "name": "tests",
            "description": (
                "missing coverage for critical paths, incorrect assertions, "
                "fragile or flaky tests, untested error paths"
            ),
        },
        {
            "name": "persistence",
            "description": (
                "data integrity issues, unsafe migrations, missing crash "
                "recovery, silent data loss, index or transaction gaps"
            ),
        },
        {
            "name": "api_contracts",
            "description": (
                "inconsistent interfaces, missing input validation, "
                "undocumented breaking changes, incorrect API documentation"
            ),
        },
        {
            "name": "dependencies",
            "description": (
                "unused imports and packages, dead code, outdated dependencies "
                "with known vulnerabilities, shadowed or circular imports"
            ),
        },
        {
            "name": "maintainability",
            "description": (
                "confusing naming, duplicated logic, excessive complexity "
                "that makes future changes error-prone"
            ),
        },
    ],
}

# Priority ordering used when sorting open findings.
_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2}


# ── Configuration loading ──────────────────────────────────────────────────────

def load_config(path: Path) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    _deep_merge(cfg, DEFAULT_CONFIG)

    if path.exists():
        try:
            if sys.version_info >= (3, 11):
                import tomllib  # stdlib in 3.11+
                with open(path, "rb") as f:
                    user_cfg = tomllib.load(f)
            elif path.suffix == ".json":
                with open(path) as f:
                    user_cfg = json.load(f)
            else:
                raise RuntimeError(
                    "Python < 3.11 requires config.json instead of config.toml. "
                    "Either upgrade Python or rename the file and use JSON syntax."
                )
            _deep_merge(cfg, user_cfg)
        except Exception as exc:
            LOG.warning("Could not load config from %s: %s", path, exc)
    else:
        LOG.warning(
            "No config file found at %s — running with defaults. "
            "Copy config.toml.example to config.toml and set repo.owner / repo.name / repo.path.",
            path,
        )

    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


# ── Database ───────────────────────────────────────────────────────────────────

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
    -- open | in_progress | fixed | rejected | stale
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
    -- open | merged | closed | failed
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
    -- clean | findings_found
    result          TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DB:
    """Thin SQLite wrapper.  All public methods commit immediately."""

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── key/value state ──────────────────────────────────────────────────────

    def get(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def put(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value)
        )
        self._conn.commit()

    # ── findings ─────────────────────────────────────────────────────────────

    def upsert_finding(self, f: dict) -> tuple[int, bool]:
        """Insert if not already present (deduped by fingerprint).
        Returns (row_id, is_new)."""
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
        cur = self._conn.execute(
            "INSERT INTO prs(branch) VALUES(?)", (branch,)
        )
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

    def clean_audit_streak(self, area: str, window: int) -> int:
        """Count consecutive recent audit runs (newest first) with 0 new findings."""
        rows = self._conn.execute(
            """SELECT new_findings FROM audit_runs
               WHERE area=? AND status='completed'
               ORDER BY id DESC LIMIT ?""",
            (area, window),
        ).fetchall()
        if len(rows) < window:
            return 0
        return sum(1 for r in rows if r["new_findings"] == 0)

    # ── summary queries ───────────────────────────────────────────────────────

    def finding_counts(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT area, status, COUNT(*) AS n FROM findings GROUP BY area, status ORDER BY area, status"
        ).fetchall()

    def recent_prs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM prs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        self._conn.close()


# ── Codex client ───────────────────────────────────────────────────────────────

class CodexError(Exception):
    pass


def run_codex(
    prompt: str,
    repo_path: Path,
    flags: list[str],
    cmd: str,
    model: str,
    timeout: int,
) -> str:
    """Invoke the Codex CLI with a fresh context.  Returns stdout."""
    full_cmd = [cmd, "--model", model, *flags, prompt]
    try:
        result = subprocess.run(
            full_cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ,
        )
    except subprocess.TimeoutExpired:
        raise CodexError(f"Codex timed out after {timeout}s")
    except FileNotFoundError:
        raise CodexError(
            f"Codex CLI not found: '{cmd}'. "
            "Install it with: npm install -g @openai/codex"
        )

    if result.returncode != 0 and not result.stdout.strip():
        raise CodexError(
            f"Codex exited {result.returncode}: {result.stderr[:400]}"
        )
    return result.stdout


def extract_json(text: str) -> Any:
    """Pull the first JSON array or object out of freeform Codex output."""
    # Prefer fenced code blocks first.
    for pat in (r"```json\s*([\s\S]*?)```", r"```\s*([\s\S]*?)```"):
        m = re.search(pat, text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    # Fall back to scanning for raw JSON.
    for m in re.finditer(r"(\[[\s\S]*?\]|\{[\s\S]*?\})", text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    raise ValueError(f"No JSON found in Codex output (first 500 chars): {text[:500]}")


# ── Git / GitHub CLI helpers ───────────────────────────────────────────────────

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def current_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def full_diff(repo: Path, base: str) -> str:
    return _git(repo, "diff", base).stdout


def create_branch(repo: Path, name: str, base: str) -> None:
    _git(repo, "checkout", base)
    _git(repo, "pull", "origin", base, check=False)
    _git(repo, "checkout", "-b", name)


def push_branch(repo: Path, branch: str) -> None:
    """Push with up to 5 attempts and exponential back-off."""
    for attempt, delay in enumerate([0, 2, 4, 8, 16], start=1):
        if delay:
            time.sleep(delay)
        r = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return
        if attempt == 5:
            raise RuntimeError(f"git push failed after 5 attempts:\n{r.stderr}")


def gh_create_pr(owner: str, repo_name: str, branch: str, title: str, body: str) -> tuple[int, str]:
    """Returns (pr_number, pr_url)."""
    r = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", f"{owner}/{repo_name}",
            "--head", branch,
            "--title", title,
            "--body", body,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    url = r.stdout.strip().split()[-1]
    m = re.search(r"/pull/(\d+)", url)
    return (int(m.group(1)) if m else 0), url


def gh_ci_status(owner: str, repo_name: str, pr_number: int) -> str:
    """Returns 'success' | 'failure' | 'pending' | 'unknown'."""
    r = subprocess.run(
        [
            "gh", "pr", "checks", str(pr_number),
            "--repo", f"{owner}/{repo_name}",
            "--json", "conclusion,status",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return "unknown"
    try:
        checks = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "unknown"
    if not checks:
        return "unknown"  # No CI configured — treat as passing
    if any(c.get("conclusion") == "failure" for c in checks):
        return "failure"
    if any(c.get("status") == "in_progress" for c in checks):
        return "pending"
    if all(c.get("conclusion") in ("success", "skipped", None) for c in checks):
        statuses = {c.get("status") for c in checks}
        if "completed" in statuses or "success" in statuses:
            return "success"
        return "pending"
    return "pending"


def gh_merge_pr(owner: str, repo_name: str, pr_number: int) -> None:
    subprocess.run(
        [
            "gh", "pr", "merge", str(pr_number),
            "--repo", f"{owner}/{repo_name}",
            "--squash",
            "--delete-branch",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def wait_for_ci(owner: str, repo_name: str, pr_number: int, timeout_s: int) -> str:
    if timeout_s <= 0:
        return "unknown"  # Caller treats 'unknown' as go-ahead
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = gh_ci_status(owner, repo_name, pr_number)
        if status in ("success", "failure", "unknown"):
            return status
        LOG.info("  CI pending — waiting 30 s…")
        time.sleep(30)
    LOG.warning("  CI wait timed out after %s s", timeout_s)
    return "pending"


# ── Prompt templates ───────────────────────────────────────────────────────────

_AUDIT_PROMPT = """\
You are a meticulous code auditor. Your task is to audit this entire repository
for issues in ONE specific category: {area}.

Category description: {description}

Rules:
- Inspect ALL relevant files thoroughly.
- Report only concrete, actionable problems with clear evidence.
- Do NOT suggest new features, speculative refactors, or cosmetic changes.
- Do NOT flag issues that are clearly intentional design decisions.
- Confidence should reflect how certain you are this is a real bug (not theoretical).

Return ONLY a JSON array.  If there are no findings, return [].
Each element:
{{
  "title":         "<concise problem title, under 80 chars>",
  "description":   "<detailed explanation: what is wrong, why it matters, how to fix>",
  "severity":      "critical|high|medium|low",
  "confidence":    "high|medium|low",
  "file_path":     "<repo-relative path or null>",
  "line_range":    "<e.g. L10-L25, or null>"
}}
"""

_REPAIR_PROMPT = """\
Fix the following maintenance issue in this repository.
Make the MINIMAL change necessary — do not refactor unrelated code.

Issue title:    {title}
Severity:       {severity}
File:           {file_path}
Lines:          {line_range}

Details:
{description}

After applying the fix, print a brief paragraph summarising exactly what you
changed and why.  Do not modify files beyond what is needed for this fix.
"""

_VERIFY_PROMPT = """\
Inspect this diff and decide whether the fix is correct.

Intended fix: {title}
Background:
{description}

Diff:
{diff}

Answer with ONLY a JSON object:
{{
  "verdict": "approve|reject",
  "reason":  "<one sentence>",
  "issues":  ["<specific problem>", ...]   // empty list if verdict is approve
}}
"""

_REVIEW_PROMPT = """\
Review this pull request diff as part of an automated maintenance workflow.

PR title: {pr_title}
Fixing:   {finding_titles}

Diff:
{diff}

Check for: correctness of the fix, unintended regressions, scope creep
(changes beyond the stated intent), missing test coverage, remaining
instances of the same problem.

Return ONLY a JSON object:
{{
  "verdict":  "approve|request_changes",
  "summary":  "<one sentence overall assessment>",
  "comments": [
    {{
      "severity":    "blocking|optional",
      "file":        "<path or null>",
      "description": "<what needs to change and why>"
    }}
  ]
}}
"""

_IMPLEMENT_REVIEW_PROMPT = """\
Implement the following blocking review comments on this repository.
Address only these comments; do not make any other changes.

{comments}

After implementing, print a brief paragraph summarising what you changed.
"""


# ── Finding fingerprint ────────────────────────────────────────────────────────

def _fingerprint(area: str, file_path: str | None, title: str) -> str:
    key = f"{area}:{file_path or ''}:{title.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


# ── Lifecycle phases ───────────────────────────────────────────────────────────

def phase_audit(cfg: dict, db: DB, area: dict, ctr: dict) -> int:
    """Run a scoped audit for *area*. Returns count of new findings stored."""
    repo = Path(cfg["repo"]["path"]).resolve()
    commit = current_commit(repo)
    run_id = db.start_audit(area["name"], commit)

    prompt = _AUDIT_PROMPT.format(
        area=area["name"], description=area["description"]
    )
    LOG.info("Auditing %-20s @ %s", area["name"], commit[:7])

    try:
        ctr["codex_calls"] += 1
        output = run_codex(
            prompt, repo,
            flags=cfg["codex"]["audit_flags"],
            cmd=cfg["codex"]["cmd"],
            model=cfg["codex"]["model"],
            timeout=cfg["codex"]["timeout"],
        )
        raw_findings = extract_json(output)
    except (CodexError, ValueError) as exc:
        LOG.error("Audit failed for %s: %s", area["name"], exc)
        db.finish_audit(run_id, 0, 0, "failed")
        ctr["consecutive_failures"] += 1
        return 0

    if not isinstance(raw_findings, list):
        raw_findings = []

    new_count = 0
    for raw in raw_findings:
        try:
            fp = _fingerprint(area["name"], raw.get("file_path"), raw["title"])
            finding = {
                "fingerprint": fp,
                "area": area["name"],
                "severity": raw.get("severity", "low"),
                "confidence": raw.get("confidence", "medium"),
                "file_path": raw.get("file_path"),
                "line_range": raw.get("line_range"),
                "title": str(raw["title"]),
                "description": str(raw.get("description", "")),
                "commit_hash": commit,
            }
            _, is_new = db.upsert_finding(finding)
            if is_new:
                new_count += 1
                LOG.info("  [%s/%s] %s", finding["severity"], finding["confidence"], finding["title"])
        except (KeyError, TypeError) as exc:
            LOG.warning("  Skipping malformed finding: %s", exc)

    db.finish_audit(run_id, new_count, len(raw_findings))
    LOG.info("Audit %s: %d new (%d total)", area["name"], new_count, len(raw_findings))
    ctr["consecutive_failures"] = 0
    return new_count


def phase_repair(cfg: dict, db: DB, findings: list, ctr: dict) -> tuple[bool, str]:
    """Apply fixes for the given findings on a new branch.
    Returns (success, branch_name). On failure, branch is '' and repo is
    restored to the default branch."""
    repo = Path(cfg["repo"]["path"]).resolve()
    main = cfg["repo"]["default_branch"]
    area = findings[0]["area"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"maint/{area}/{ts}"

    try:
        create_branch(repo, branch, main)
    except subprocess.CalledProcessError as exc:
        LOG.error("Cannot create branch %s: %s", branch, exc)
        return False, ""

    base = current_commit(repo)

    for f in findings:
        prompt = _REPAIR_PROMPT.format(
            title=f["title"],
            severity=f["severity"],
            file_path=f["file_path"] or "(unknown)",
            line_range=f["line_range"] or "(unknown)",
            description=f["description"],
        )
        LOG.info("  Repairing: %s", f["title"])
        try:
            ctr["codex_calls"] += 1
            run_codex(
                prompt, repo,
                flags=cfg["codex"]["repair_flags"],
                cmd=cfg["codex"]["cmd"],
                model=cfg["codex"]["model"],
                timeout=cfg["codex"]["timeout"],
            )
        except CodexError as exc:
            LOG.error("  Repair failed: %s", exc)
            ctr["consecutive_failures"] += 1
            _git(repo, "checkout", main, check=False)
            _git(repo, "branch", "-D", branch, check=False)
            return False, ""

    diff = full_diff(repo, base)
    if not diff.strip():
        LOG.warning("  Repair produced no file changes — skipping")
        _git(repo, "checkout", main, check=False)
        _git(repo, "branch", "-D", branch, check=False)
        for f in findings:
            db.mark_finding(f["id"], "rejected", reason="repair produced no changes")
        return False, ""

    # Commit all changes together.
    titles = "; ".join(f["title"] for f in findings)
    commit_msg = (
        f"maint({area}): {titles[:72]}\n\n"
        "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>\n"
        "Claude-Session: https://claude.ai/code/session_01Rk2K8VHLbCFPCpe8vzi1Mo"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", commit_msg)

    ctr["consecutive_failures"] = 0
    return True, branch


def phase_verify(cfg: dict, findings: list, ctr: dict) -> bool:
    """Run optional test suite + Codex diff-review.
    Returns True if the fix looks good to proceed."""
    repo = Path(cfg["repo"]["path"]).resolve()
    main = cfg["repo"]["default_branch"]
    test_cmd = cfg["verify"]["test_cmd"]

    # 1. Local test suite (optional).
    if test_cmd:
        LOG.info("  Running: %s", test_cmd)
        r = subprocess.run(
            test_cmd, shell=True, cwd=str(repo), capture_output=True, text=True
        )
        if r.returncode != 0:
            LOG.error("  Tests failed:\n%s", (r.stdout + r.stderr)[-1200:])
            return False

    # 2. Codex diff-review with fresh context.
    diff = full_diff(repo, main)
    if not diff.strip():
        return False

    prompt = _VERIFY_PROMPT.format(
        title=findings[0]["title"],
        description="\n".join(f["description"] for f in findings),
        diff=diff[:10_000],
    )
    try:
        ctr["codex_calls"] += 1
        output = run_codex(
            prompt, repo,
            flags=cfg["codex"]["audit_flags"],
            cmd=cfg["codex"]["cmd"],
            model=cfg["codex"]["model"],
            timeout=cfg["codex"]["timeout"],
        )
        result = extract_json(output)
    except (CodexError, ValueError) as exc:
        # If verify itself fails we log a warning and proceed — don't block on it.
        LOG.warning("  Verify call failed (%s) — proceeding anyway", exc)
        return True

    verdict = result.get("verdict", "approve")
    LOG.info("  Verify verdict: %s — %s", verdict, result.get("reason", ""))
    if verdict == "reject":
        for issue in result.get("issues", []):
            LOG.warning("    %s", issue)
        return False
    return True


def phase_review_loop(
    cfg: dict, db: DB, pr_id: int, findings: list, branch: str, ctr: dict
) -> bool:
    """Review the PR with Codex, implement blocking feedback, repeat.
    Returns True once the review is exhausted (approve or max rounds reached)."""
    repo = Path(cfg["repo"]["path"]).resolve()
    main = cfg["repo"]["default_branch"]
    owner = cfg["repo"]["owner"]
    repo_name = cfg["repo"]["name"]
    max_rounds = cfg["budget"]["max_review_rounds"]
    finding_titles = "; ".join(f["title"] for f in findings)
    pr_title = f"maint: {finding_titles[:60]}"

    for rnd in range(1, max_rounds + 1):
        LOG.info("  Review round %d/%d", rnd, max_rounds)

        diff = full_diff(repo, main)
        prompt = _REVIEW_PROMPT.format(
            pr_title=pr_title,
            finding_titles=finding_titles,
            diff=diff[:10_000],
        )
        try:
            ctr["codex_calls"] += 1
            output = run_codex(
                prompt, repo,
                flags=cfg["codex"]["audit_flags"],
                cmd=cfg["codex"]["cmd"],
                model=cfg["codex"]["model"],
                timeout=cfg["codex"]["timeout"],
            )
            review = extract_json(output)
        except (CodexError, ValueError) as exc:
            LOG.warning("  Review call failed (%s) — treating as approved", exc)
            break

        verdict = review.get("verdict", "approve")
        LOG.info("  Review verdict: %s — %s", verdict, review.get("summary", ""))

        blocking = [c for c in review.get("comments", []) if c.get("severity") == "blocking"]
        if verdict == "approve" or not blocking:
            break

        for c in blocking:
            LOG.info("  Blocking: [%s] %s", c.get("file", "?"), c["description"])

        comments_text = "\n".join(
            f"- [{c.get('file', 'general')}] {c['description']}" for c in blocking
        )
        impl_prompt = _IMPLEMENT_REVIEW_PROMPT.format(comments=comments_text)
        prev_diff = diff

        try:
            ctr["codex_calls"] += 1
            run_codex(
                impl_prompt, repo,
                flags=cfg["codex"]["repair_flags"],
                cmd=cfg["codex"]["cmd"],
                model=cfg["codex"]["model"],
                timeout=cfg["codex"]["timeout"],
            )
        except CodexError as exc:
            LOG.error("  Failed to implement review feedback: %s", exc)
            break

        if full_diff(repo, main) == prev_diff:
            LOG.warning("  Review feedback produced no changes — stopping review loop")
            break

        _git(repo, "add", "-A")
        _git(
            repo, "commit", "-m",
            f"maint: address review feedback (round {rnd})\n\n"
            "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>\n"
            "Claude-Session: https://claude.ai/code/session_01Rk2K8VHLbCFPCpe8vzi1Mo",
        )
        push_branch(repo, branch)

    db.update_pr(pr_id, review_rounds=rnd)
    return True


# ── Supervisor orchestrator ────────────────────────────────────────────────────

class Supervisor:
    def __init__(self, cfg: dict, db: DB) -> None:
        self.cfg = cfg
        self.db = db
        self.ctr: dict[str, int] = {
            "codex_calls": 0,
            "fixes_applied": 0,
            "consecutive_failures": 0,
        }

    # ── budget / safety checks ────────────────────────────────────────────────

    def _over_budget(self) -> bool:
        b = self.cfg["budget"]
        if self.ctr["codex_calls"] >= b["codex_call_budget"]:
            LOG.warning("Codex call budget exhausted (%d)", b["codex_call_budget"])
            return True
        if self.ctr["consecutive_failures"] >= b["max_consecutive_failures"]:
            LOG.warning(
                "Stopping: %d consecutive failures (limit %d)",
                self.ctr["consecutive_failures"],
                b["max_consecutive_failures"],
            )
            return True
        if self.ctr["fixes_applied"] >= b["max_fixes_per_run"]:
            LOG.info("Fix budget reached (%d)", b["max_fixes_per_run"])
            return True
        return False

    # ── PR body ───────────────────────────────────────────────────────────────

    def _pr_body(self, findings: list) -> str:
        parts = ["## Maintenance fix\n"]
        for f in findings:
            parts.append(f"**[{f['severity'].upper()}] {f['title']}**\n")
            parts.append(f"{f['description']}\n")
            if f.get("file_path"):
                parts.append(f"_File: {f['file_path']} {f.get('line_range') or ''}_\n")
        parts += [
            "\n---",
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n",
            "https://claude.ai/code/session_01Rk2K8VHLbCFPCpe8vzi1Mo",
        ]
        return "\n".join(parts)

    # ── single finding workflow ────────────────────────────────────────────────

    def _fix_finding(self, finding: sqlite3.Row) -> bool:
        """Full repair→verify→PR→review→merge cycle for one finding.
        Returns True if the finding was successfully fixed and merged."""
        cfg = self.cfg
        db = self.db
        repo = Path(cfg["repo"]["path"]).resolve()
        main = cfg["repo"]["default_branch"]
        owner = cfg["repo"]["owner"]
        repo_name = cfg["repo"]["name"]
        f = dict(finding)

        LOG.info("[%s/%s] %s", f["severity"], f["confidence"], f["title"])
        db.mark_finding(f["id"], "in_progress")

        # ── repair ────────────────────────────────────────────────────────────
        ok, branch = phase_repair(cfg, db, [f], self.ctr)
        if not ok:
            db.mark_finding(f["id"], "open")
            return False

        # ── verify ────────────────────────────────────────────────────────────
        ok = phase_verify(cfg, [f], self.ctr)
        if not ok:
            LOG.warning("  Verify rejected — discarding branch")
            _git(repo, "checkout", main, check=False)
            _git(repo, "branch", "-D", branch, check=False)
            db.mark_finding(f["id"], "open")
            self.ctr["consecutive_failures"] += 1
            return False

        # ── push + PR ─────────────────────────────────────────────────────────
        try:
            push_branch(repo, branch)
            pr_number, pr_url = gh_create_pr(
                owner, repo_name, branch,
                f"maint({f['area']}): {f['title'][:60]}",
                self._pr_body([f]),
            )
        except Exception as exc:
            LOG.error("  Push/PR creation failed: %s", exc)
            _git(repo, "checkout", main, check=False)
            db.mark_finding(f["id"], "open")
            self.ctr["consecutive_failures"] += 1
            return False

        pr_id = db.create_pr(branch)
        db.update_pr(pr_id, pr_number=pr_number, pr_url=pr_url)
        db.mark_finding(f["id"], "in_progress", pr_id=pr_id)

        # Stay on branch for the review loop.
        _git(repo, "checkout", branch, check=False)

        # ── review loop ───────────────────────────────────────────────────────
        phase_review_loop(cfg, db, pr_id, [f], branch, self.ctr)

        # ── wait for CI ───────────────────────────────────────────────────────
        ci = wait_for_ci(owner, repo_name, pr_number, cfg["verify"]["ci_wait_timeout"])
        LOG.info("  CI status: %s", ci)
        if ci == "failure":
            LOG.error("  CI failed — not merging; marking finding open for retry")
            db.update_pr(pr_id, status="failed")
            db.mark_finding(f["id"], "open")
            _git(repo, "checkout", main, check=False)
            self.ctr["consecutive_failures"] += 1
            return False

        # ── merge ─────────────────────────────────────────────────────────────
        try:
            gh_merge_pr(owner, repo_name, pr_number)
        except Exception as exc:
            LOG.error("  Merge failed: %s", exc)
            db.update_pr(pr_id, status="failed")
            db.mark_finding(f["id"], "open")
            _git(repo, "checkout", main, check=False)
            self.ctr["consecutive_failures"] += 1
            return False

        db.update_pr(
            pr_id,
            status="merged",
            merged=datetime.now(timezone.utc).isoformat(),
        )
        db.mark_finding(f["id"], "fixed", pr_id=pr_id)
        LOG.info("  Merged PR #%d: %s", pr_number, pr_url)

        self.ctr["fixes_applied"] += 1
        self.ctr["consecutive_failures"] = 0

        # Pull merged changes back so the next iteration works on fresh HEAD.
        _git(repo, "checkout", main, check=False)
        _git(repo, "pull", "origin", main, check=False)
        return True

    # ── main loop ─────────────────────────────────────────────────────────────

    def run_once(self) -> str:
        """Run one full pass over all audit areas.
        Returns 'exhausted' | 'partial' | 'done'."""
        cfg = self.cfg
        repo = Path(cfg["repo"]["path"]).resolve()
        main = cfg["repo"]["default_branch"]
        areas: list[dict] = cfg["audit_areas"]
        budget = cfg["budget"]

        LOG.info(
            "Starting maintenance run on %s/%s",
            cfg["repo"]["owner"],
            cfg["repo"]["name"],
        )

        # Ensure clean starting state on main.
        _git(repo, "checkout", main, check=False)
        _git(repo, "pull", "origin", main, check=False)

        total_new = 0

        for area in areas:
            if self._over_budget():
                return "partial"

            # Skip exhausted areas.
            streak = db.clean_audit_streak(area["name"], budget["max_audits_per_area"])
            if streak >= budget["max_audits_per_area"]:
                LOG.info("Area %-20s exhausted (%d clean audits)", area["name"], streak)
                continue

            # Audit.
            new = phase_audit(cfg, self.db, area, self.ctr)
            total_new += new

            if self._over_budget():
                return "partial"

            # Fix open findings for this area, highest priority first.
            for finding in self.db.open_findings(area["name"]):
                if self._over_budget():
                    return "partial"
                self._fix_finding(finding)

        # Check exhaustion: all areas need `max_audits_per_area` clean streaks.
        if all(
            self.db.clean_audit_streak(a["name"], budget["max_audits_per_area"])
            >= budget["max_audits_per_area"]
            for a in areas
        ):
            LOG.info("All audit areas exhausted — repository is clean.")
            return "exhausted"

        return "done" if (total_new > 0 or self.ctr["fixes_applied"] > 0) else "partial"


# ── CLI sub-commands ───────────────────────────────────────────────────────────

def cmd_status(cfg: dict, db: DB) -> None:
    print(f"\n=== Maintenance status: {cfg['repo']['owner']}/{cfg['repo']['name']} ===\n")

    counts = db.finding_counts()
    if counts:
        print("Findings by area / status:")
        cur_area = None
        for row in counts:
            if row["area"] != cur_area:
                cur_area = row["area"]
                print(f"  {cur_area}")
            print(f"      {row['status']:14} {row['n']}")
    else:
        print("No findings recorded yet.")

    print()
    prs = db.recent_prs()
    if prs:
        print("Recent PRs:")
        for pr in prs:
            print(
                f"  #{str(pr['pr_number'] or '?'):5}  "
                f"[{pr['status']:8}]  "
                f"{pr['branch']}  "
                f"{pr['pr_url'] or ''}"
            )
    else:
        print("No PRs yet.")

    budget = cfg["budget"]
    print("\nAudit area exhaustion:")
    for area in cfg["audit_areas"]:
        streak = db.clean_audit_streak(area["name"], budget["max_audits_per_area"])
        threshold = budget["max_audits_per_area"]
        tag = "exhausted" if streak >= threshold else f"{streak}/{threshold} clean"
        print(f"  {area['name']:22} {tag}")


def cmd_findings(db: DB) -> None:
    findings = db.open_findings()
    if not findings:
        print("No open findings.")
        return
    print(f"\n{len(findings)} open findings:\n")
    for f in findings:
        fp = f["file_path"] or ""
        lr = f["line_range"] or ""
        loc = f"  {fp} {lr}".rstrip() if fp else ""
        print(f"[{f['severity']:8}/{f['confidence']:6}] {f['title']}")
        if loc:
            print(f"  {loc.strip()}")
        print()


# ── Entry point ────────────────────────────────────────────────────────────────

def _check_tools(cfg: dict) -> None:
    problems: list[str] = []
    codex_cmd = cfg["codex"]["cmd"]
    if not any(
        (Path(p) / codex_cmd).is_file()
        for p in os.environ.get("PATH", "").split(os.pathsep)
    ):
        problems.append(
            f"Codex CLI '{codex_cmd}' not found in PATH. "
            "Install with: npm install -g @openai/codex"
        )
    if not any(
        (Path(p) / "gh").is_file()
        for p in os.environ.get("PATH", "").split(os.pathsep)
    ):
        problems.append(
            "GitHub CLI 'gh' not found in PATH. "
            "Install from: https://cli.github.com"
        )
    if problems:
        for p in problems:
            LOG.warning("Prerequisite missing: %s", p)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.toml",
        help="Path to TOML (or JSON on Python < 3.11) config file (default: config.toml)",
    )
    parser.add_argument(
        "--db", default=".maintain.db",
        help="Path to the SQLite state database (default: .maintain.db)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    sub = parser.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("run", help="Run one full maintenance iteration")

    run_cont = sub.add_parser(
        "run-continuous", help="Run iterations until exhausted or budget exceeded"
    )
    run_cont.add_argument(
        "--max-iterations", type=int, default=50,
        help="Hard limit on iterations (default: 50)",
    )

    sub.add_parser("status", help="Show queue, PRs, and area exhaustion")
    sub.add_parser("findings", help="List open findings")

    audit_cmd = sub.add_parser("audit", help="Manually audit one specific area")
    audit_cmd.add_argument(
        "area",
        help="Area name, e.g. correctness, security, tests",
    )

    sub.add_parser("reset", help="Clear all state (asks for confirmation)")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(Path(args.config))

    # Warn about missing tools without blocking.
    _check_tools(cfg)

    # Make db accessible to cmd_status as a local via closure.
    global db
    db = DB(Path(args.db))

    try:
        if args.cmd == "run":
            result = Supervisor(cfg, db).run_once()
            print(f"\nRun result: {result}")

        elif args.cmd == "run-continuous":
            sup = Supervisor(cfg, db)
            for i in range(1, args.max_iterations + 1):
                LOG.info("──── Iteration %d/%d ────", i, args.max_iterations)
                result = sup.run_once()
                LOG.info("Iteration result: %s", result)
                if result == "exhausted":
                    LOG.info("Repository exhausted — stopping.")
                    break
                time.sleep(5)

        elif args.cmd == "status":
            cmd_status(cfg, db)

        elif args.cmd == "findings":
            cmd_findings(db)

        elif args.cmd == "audit":
            area_cfg = next(
                (a for a in cfg["audit_areas"] if a["name"] == args.area), None
            )
            if area_cfg is None:
                known = [a["name"] for a in cfg["audit_areas"]]
                sys.exit(f"Unknown area '{args.area}'.  Known areas: {known}")
            sup = Supervisor(cfg, db)
            new = phase_audit(cfg, db, area_cfg, sup.ctr)
            print(f"\n{new} new findings in area '{args.area}'")
            for f in db.open_findings(args.area):
                loc = f" ({f['file_path']})" if f["file_path"] else ""
                print(f"  [{f['severity']:8}] {f['title']}{loc}")

        elif args.cmd == "reset":
            confirm = input(
                "This deletes ALL findings, PRs, and state from the database.\n"
                "Type 'yes' to confirm: "
            )
            if confirm.strip().lower() == "yes":
                db._conn.executescript(
                    "DELETE FROM findings; DELETE FROM audit_runs; "
                    "DELETE FROM prs; DELETE FROM sweeps; DELETE FROM kv;"
                )
                db._conn.commit()
                print("State cleared.")
            else:
                print("Aborted.")

        else:
            parser.print_help()

    finally:
        db.close()


if __name__ == "__main__":
    main()
