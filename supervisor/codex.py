from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class CodexError(Exception):
    pass


def _build_codex_cmd(
    cmd: str, model: str, flags: list[str], prompt: str
) -> list[str]:
    """Construct the full argv for a Codex invocation.

    When *flags* begins with a subcommand (e.g. "exec") rather than an option
    flag, --model is inserted after the subcommand so the CLI sees:
        codex exec --model <m> [other flags] <prompt>
    rather than:
        codex --model <m> exec [other flags] <prompt>
    """
    if flags and not flags[0].startswith("-"):
        return [cmd, flags[0], "--model", model, *flags[1:], prompt]
    return [cmd, "--model", model, *flags, prompt]


def run_codex(
    prompt: str,
    repo_path: Path,
    flags: list[str],
    cmd: str,
    model: str,
    timeout: int,
) -> str:
    """Invoke Codex with a fresh context.  Returns stdout.  Raises CodexError."""
    full_cmd = _build_codex_cmd(cmd, model, flags, prompt)
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
            "Install with: npm install -g @openai/codex"
        )

    if result.returncode != 0 and not result.stdout.strip():
        raise CodexError(
            f"Codex exited {result.returncode}: {result.stderr[:400]}"
        )
    return result.stdout


def extract_json(text: str) -> Any:
    """Pull the first JSON array or object out of freeform Codex output."""
    for pat in (r"```json\s*([\s\S]*?)```", r"```\s*([\s\S]*?)```"):
        m = re.search(pat, text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    for m in re.finditer(r"(\[[\s\S]*?\]|\{[\s\S]*?\})", text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    raise ValueError(f"No JSON in Codex output (first 400 chars): {text[:400]}")
