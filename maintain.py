#!/usr/bin/env python3
"""
maintain.py - Autonomous repository maintenance supervisor

Runs a continuous audit-repair-verify-PR-merge lifecycle over a configured
GitHub repository using the Codex CLI as the auditing, coding, and review
agent.  The supervisor owns all state, queuing, deduplication, and stopping
logic; Codex is only invoked for AI-powered analysis and code changes.

Usage:
    python maintain.py run                # one full iteration
    python maintain.py run-continuous     # loop until exhausted
    python maintain.py status             # show queue and progress
    python maintain.py audit <area>       # manual single-area audit
    python maintain.py findings           # list open, deferred, and blocked findings
    python maintain.py reset              # clear all state (asks for confirmation)

Prerequisites:
    - Codex CLI installed and authenticated  (npm install -g @openai/codex)
    - GitHub CLI installed and authenticated (gh auth login)
    - config.toml filled in with repo.owner and repo.name
    - Either repo.path pointing to an existing local clone, or leave repo.path
      empty to let Maintain clone and manage the repository automatically under
      ~/.maintain/workspaces/<owner>/<name> (configurable via repo.workspace).
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


from supervisor.config import load_config
from supervisor.db import DB
from supervisor.git import (
    clone_repo,
    create_audit_worktree,
    remove_audit_worktree,
)
from supervisor.phases import _STAGES, phase_audit
from supervisor.runner import Supervisor, cmd_findings, cmd_status

_MUTATING_CMDS = {"run", "run-continuous", "audit", "repair"}


def _parse_remote_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """Parse SSH or HTTPS remote URL into (host, owner/repo), lowercased.

    Returns None if the URL is not a recognized SSH or HTTPS Git remote.
    Both components are lowercased for case-insensitive comparison.
    """
    url = remote_url.strip()
    # SSH: git@host:owner/repo[.git]
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1).lower(), m.group(2).lower()
    # HTTPS: https://host/owner/repo[.git]
    m = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1).lower(), m.group(2).lower()
    return None


def _managed_workspace_root(cfg: dict) -> Path:
    """Return the root directory used for Maintain-managed clones.

    Honours repo.workspace when set; otherwise ~/.maintain/workspaces.
    """
    ws = cfg["repo"].get("workspace", "").strip()
    if ws:
        return Path(ws).expanduser().resolve()
    return (Path.home() / ".maintain" / "workspaces").resolve()


def _resolve_repo_path(cfg: dict) -> None:
    """Populate cfg["repo"]["path"] from the managed workspace when it is empty.

    Also records cfg["repo"]["_managed"] = True when the path was originally
    absent (Maintain owns the clone), False when explicitly configured.
    Safe to call multiple times; provenance is captured on the first call only.

    Called for all commands so that every code path (including read-only
    commands like status and findings) references the correct local path.
    Does not clone.
    """
    originally_empty = not bool(cfg["repo"].get("path", "").strip())
    # Record provenance once; subsequent calls must not overwrite it.
    cfg["repo"].setdefault("_managed", originally_empty)

    if not originally_empty:
        return
    owner = cfg["repo"].get("owner", "").strip()
    name = cfg["repo"].get("name", "").strip()
    if not owner or not name:
        return
    cfg["repo"]["path"] = str((_managed_workspace_root(cfg) / owner / name).resolve())


def _validate_config(cfg: dict, config_path: str) -> None:
    """Abort with a clear error if the config is unsafe for mutating commands.

    Validation runs in two phases so that repository acquisition (clone) only
    happens after all pure configuration checks pass.

    Phase 1 — pure config: owner/name, default branch, audit areas, budget,
    model strings, verify.setup_cmd, and required binaries.  No filesystem
    side effects.

    Phase 2 — repository: clone a managed workspace when absent, then verify
    the directory is a git repo whose origin points at owner/name.
    """
    if not Path(config_path).exists():
        sys.exit(
            f"Config file not found: {config_path}\n"
            "Copy config.toml and fill in repo.owner and repo.name "
            "before running maintenance commands."
        )

    # Resolve managed path (pure string computation) and capture provenance.
    _resolve_repo_path(cfg)
    is_managed: bool = cfg["repo"].get("_managed", False)

    errors: list[str] = []

    # ── Phase 1: pure config checks ────────────────────────────────────────
    owner = cfg["repo"]["owner"].strip()
    name = cfg["repo"]["name"].strip()
    if not owner:
        errors.append("repo.owner is empty")
    if not name:
        errors.append("repo.name is empty")

    default_branch = cfg["repo"]["default_branch"].strip()
    if not default_branch:
        errors.append("repo.default_branch is empty")

    if not cfg.get("audit_areas"):
        errors.append("audit_areas is empty — nothing to audit")

    for key in (
        "max_audits_per_area", "max_fixes_per_run", "max_review_rounds",
        "max_consecutive_failures", "codex_call_budget", "max_repair_attempts",
    ):
        val = cfg["budget"].get(key)
        if not isinstance(val, int) or val <= 0:
            errors.append(
                f"budget.{key} must be a positive integer (got {val!r})"
            )

    codex_model = cfg["codex"].get("model")
    if not isinstance(codex_model, str) or not codex_model.strip():
        errors.append(
            f"codex.model must be a non-empty string (got {codex_model!r})"
        )

    for stage in _STAGES:
        val = cfg["codex"].get(f"{stage}_model")
        if val is not None and not isinstance(val, str):
            errors.append(
                f"codex.{stage}_model must be a string or unset (got {val!r})"
            )

    setup_cmd = cfg["verify"].get("setup_cmd", "")
    if not isinstance(setup_cmd, str):
        errors.append(
            f"verify.setup_cmd must be a string (got {type(setup_cmd).__name__})"
        )

    for binary, hint in [
        ("git", "Install Git from: https://git-scm.com"),
        (cfg["codex"]["cmd"], "Install with: npm install -g @openai/codex"),
        ("gh", "Install from: https://cli.github.com"),
    ]:
        if not shutil.which(binary):
            errors.append(
                f"Required binary not found in PATH: '{binary}'. {hint}"
            )

    if errors:
        sys.exit(
            "Configuration errors — aborting before making any changes:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    # ── Phase 2: repository acquisition ────────────────────────────────────
    # All pure config is valid; safe to touch the filesystem now.
    repo_path_str = cfg["repo"].get("path", "").strip()
    if not repo_path_str:
        # owner/name were missing (caught above) — nothing to check.
        return

    repo_path = Path(repo_path_str).resolve()

    if is_managed:
        workspace_root = _managed_workspace_root(cfg)
        if not repo_path.is_relative_to(workspace_root):
            sys.exit(
                "Configuration errors — aborting before making any changes:\n"
                f"  • Resolved managed path {repo_path} escapes the workspace"
                f" root {workspace_root} — check repo.owner, repo.name, and"
                f" repo.workspace for path-traversal components"
            )

    if not repo_path.exists():
        if is_managed:
            clone_url = f"https://github.com/{owner}/{name}"
            print(f"Cloning {clone_url} → {repo_path} …", flush=True)
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                clone_repo(clone_url, repo_path)
            except RuntimeError as exc:
                sys.exit(
                    f"Failed to clone {clone_url} into {repo_path}: {exc}"
                )
        else:
            sys.exit(
                "Configuration errors — aborting before making any changes:\n"
                f"  • repo.path does not exist: {repo_path}"
            )

    r = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        sys.exit(
            (
                f"Managed workspace at {repo_path} is not a git repository;"
                f" remove it to allow Maintain to reclone"
                if is_managed else
                "Configuration errors — aborting before making any changes:\n"
                f"  • repo.path is not a Git repository: {repo_path}"
            )
        )

    if not (owner and name):
        return

    r2 = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        sys.exit(
            "Configuration errors — aborting before making any changes:\n"
            "  • Git remote 'origin' is not configured"
        )

    remote_url = r2.stdout.strip()
    parsed = _parse_remote_owner_repo(remote_url)
    expected_path = f"{owner}/{name}".lower()
    if parsed is None:
        sys.exit(
            "Configuration errors — aborting before making any changes:\n"
            f"  • Git remote 'origin' URL is not a recognized"
            f" SSH or HTTPS remote: {remote_url!r}"
        )
    if parsed[0] != "github.com":
        sys.exit(
            "Configuration errors — aborting before making any changes:\n"
            f"  • Git remote 'origin' host is {parsed[0]!r},"
            f" expected 'github.com'"
        )
    if parsed[1] != expected_path:
        sys.exit(
            "Configuration errors — aborting before making any changes:\n"
            f"  • Git remote 'origin' ({remote_url!r}) points to"
            f" '{parsed[1]}', expected '{expected_path}' —"
            f" check repo.owner and repo.name"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.toml",
        help="Config file path (default: config.toml; use .json on Python < 3.11)",
    )
    parser.add_argument(
        "--db", default=".maintain.db",
        help="SQLite state database (default: .maintain.db)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    sub = parser.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("run", help="Run one full maintenance iteration")

    run_cont = sub.add_parser(
        "run-continuous",
        help="Loop until exhausted or blocked; resets per-run budget each iteration",
    )
    run_cont.add_argument(
        "--max-iterations", type=int, default=50,
        help="Hard limit on iterations (default: 50)",
    )

    sub.add_parser(
        "repair",
        help="Repair queued findings without running audits",
    )

    sub.add_parser("status", help="Show queue, PRs, and area exhaustion")
    sub.add_parser("findings", help="List open, deferred, and blocked findings")

    audit_p = sub.add_parser("audit", help="Manually audit one area")
    audit_p.add_argument("area", help="e.g. correctness, security, tests")

    sub.add_parser("reset", help="Clear all state (asks for confirmation)")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    _resolve_repo_path(cfg)

    if args.cmd in _MUTATING_CMDS:
        _validate_config(cfg, args.config)

    db = DB(Path(args.db))
    try:
        if args.cmd == "run":
            result = Supervisor(cfg, db).run_once()
            print(f"\nRun result: {result}")

        elif args.cmd == "repair":
            result = Supervisor(cfg, db).run_repair()
            print(f"\nRepair result: {result}")

        elif args.cmd == "run-continuous":
            sup = Supervisor(cfg, db)
            for i in range(1, args.max_iterations + 1):
                logging.getLogger("supervisor").info(
                    "──── Iteration %d/%d ────", i, args.max_iterations
                )
                result = sup.run_once()
                logging.getLogger("supervisor").info("Iteration result: %s", result)
                if result == "exhausted":
                    logging.getLogger("supervisor").info(
                        "Repository exhausted — stopping."
                    )
                    break
                if result == "blocked":
                    logging.getLogger("supervisor").warning(
                        "Repository has unresolved blocked findings —"
                        " human review required.  Stopping."
                    )
                    break
                if result == "setup_error":
                    logging.getLogger("supervisor").error(
                        "Setup command failed — fix verify.setup_cmd before"
                        " running again.  Stopping."
                    )
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
                sys.exit(f"Unknown area '{args.area}'.  Known: {known}")
            repo = Path(cfg["repo"]["path"]).resolve()
            main_branch = cfg["repo"]["default_branch"]
            audit_wt = create_audit_worktree(repo, main_branch)
            try:
                audit_cfg = {**cfg, "repo": {**cfg["repo"], "path": str(audit_wt)}}
                sup = Supervisor(cfg, db)
                new = phase_audit(audit_cfg, db, area_cfg, sup.ctr)
                print(f"\n{new} new findings in area '{args.area}'")
                for f in db.open_findings(args.area):
                    loc = f" ({f['file_path']})" if f["file_path"] else ""
                    print(f"  [{f['severity']:8}] {f['title']}{loc}")
            finally:
                remove_audit_worktree(repo, audit_wt)

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
