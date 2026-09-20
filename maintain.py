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
    python maintain.py findings           # list open/paused findings
    python maintain.py reset              # clear all state (asks for confirmation)

Prerequisites:
    - Codex CLI installed and authenticated  (npm install -g @openai/codex)
    - GitHub CLI installed and authenticated (gh auth login)
    - A local clone of the target repository
    - config.toml filled in with repo.owner / repo.name / repo.path
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from supervisor.config import load_config
from supervisor.db import DB
from supervisor.phases import phase_audit
from supervisor.runner import Supervisor, cmd_findings, cmd_status


def _check_tools(cfg: dict) -> None:
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for binary, install_hint in [
        (cfg["codex"]["cmd"], "Install with: npm install -g @openai/codex"),
        ("gh", "Install from: https://cli.github.com"),
    ]:
        if not any((Path(d) / binary).is_file() for d in path_dirs):
            logging.getLogger("supervisor").warning(
                "Prerequisite not found: '%s'.  %s", binary, install_hint
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
        "run-continuous", help="Loop until exhausted or budget exceeded"
    )
    run_cont.add_argument(
        "--max-iterations", type=int, default=50,
        help="Hard limit on iterations (default: 50)",
    )

    sub.add_parser("status", help="Show queue, PRs, and area exhaustion")
    sub.add_parser("findings", help="List open and paused findings")

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
    _check_tools(cfg)

    db = DB(Path(args.db))
    try:
        if args.cmd == "run":
            result = Supervisor(cfg, db).run_once()
            print(f"\nRun result: {result}")

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
