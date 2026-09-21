from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "repo": {
        "owner": "",
        "name": "",
        "path": ".",
        "default_branch": "main",
        "commit_trailer": "",
        "pr_footer": "",
    },
    "codex": {
        "cmd": "codex",
        "model": "o4-mini",
        "timeout": 300,
        "audit_flags": ["exec"],
        "repair_flags": ["exec", "--sandbox", "workspace-write"],
        # Per-stage model overrides; None means fall back to `model`.
        "audit_model": None,
        "revalidate_model": None,
        "validate_model": None,
        "repair_model": None,
        "verify_model": None,
        "review_model": None,
    },
    "verify": {
        "setup_cmd": "",
        "test_cmd": "",
        "ci_wait_timeout": 600,
        "allow_no_ci": False,
    },
    "budget": {
        "max_audits_per_area": 3,
        "max_fixes_per_run": 20,
        "max_review_rounds": 4,
        "max_consecutive_failures": 5,
        "codex_call_budget": 100,
        "max_repair_attempts": 3,
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
        {
            "name": "documentation",
            "description": (
                "missing or inaccurate docstrings, incorrect README claims, "
                "undocumented public APIs, stale usage examples, "
                "misleading inline comments"
            ),
        },
    ],
}


def load_config(path: Path) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    _deep_merge(cfg, DEFAULT_CONFIG)

    if not path.exists():
        import logging
        logging.getLogger("supervisor").warning(
            "Config file not found at %s — running with built-in defaults. "
            "Set repo.owner / repo.name / repo.path before running 'run'.",
            path,
        )
        return cfg

    try:
        if sys.version_info >= (3, 11):
            import tomllib
            with open(path, "rb") as f:
                user_cfg = tomllib.load(f)
        elif path.suffix == ".json":
            with open(path) as f:
                user_cfg = json.load(f)
        else:
            sys.exit(
                f"Error: Python < 3.11 cannot parse TOML. "
                f"Rename {path} to config.json and use JSON syntax, "
                "or upgrade Python to 3.11+."
            )
    except Exception as exc:
        sys.exit(f"Error: cannot parse config file {path}: {exc}")

    _deep_merge(cfg, user_cfg)
    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
