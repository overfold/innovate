from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


class CodexError(Exception):
    pass


# JSON schemas for --output-schema structured output.
AUDIT_SCHEMA: str = json.dumps({
    "type": "array",
    "items": {
        "type": "object",
        "required": ["title", "severity", "confidence"],
        "properties": {
            "title":       {"type": "string"},
            "description": {"type": "string"},
            "severity":    {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "confidence":  {"type": "string", "enum": ["high", "medium", "low"]},
            "file_path":   {"type": ["string", "null"]},
            "line_range":  {"type": ["string", "null"]},
        },
    },
})

REVALIDATE_SCHEMA: str = json.dumps({
    "type": "object",
    "required": ["still_applies", "reason"],
    "properties": {
        "still_applies": {"type": "boolean"},
        "reason":        {"type": "string"},
    },
})

VERIFY_SCHEMA: str = json.dumps({
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "reason":  {"type": "string"},
        "issues":  {"type": "array", "items": {"type": "string"}},
    },
})

REVIEW_SCHEMA: str = json.dumps({
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "summary": {"type": "string"},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "description"],
                "properties": {
                    "severity":    {"type": "string", "enum": ["blocking", "optional"]},
                    "file":        {"type": ["string", "null"]},
                    "description": {"type": "string"},
                },
            },
        },
    },
})


def _build_codex_cmd(
    cmd: str,
    model: str,
    flags: list[str],
    prompt: str,
    output_schema: str | None = None,
) -> list[str]:
    """Construct the full argv for a Codex invocation.

    When *flags* begins with a subcommand (e.g. "exec") rather than an option
    flag, --model is inserted after the subcommand so the CLI sees:
        codex exec --model <m> [other flags] <prompt>
    rather than:
        codex --model <m> exec [other flags] <prompt>
    """
    schema_flags = ["--output-schema", output_schema] if output_schema else []
    if flags and not flags[0].startswith("-"):
        return [cmd, flags[0], "--model", model, *flags[1:], *schema_flags, prompt]
    return [cmd, "--model", model, *flags, *schema_flags, prompt]


def run_codex(
    prompt: str,
    repo_path: Path,
    flags: list[str],
    cmd: str,
    model: str,
    timeout: int,
    output_schema: str | None = None,
) -> str:
    """Invoke Codex with a fresh context.  Returns stdout.  Raises CodexError."""
    full_cmd = _build_codex_cmd(cmd, model, flags, prompt, output_schema)
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

    if result.returncode != 0:
        raise CodexError(
            f"Codex exited {result.returncode}: {result.stderr[:400]}"
        )
    return result.stdout


def parse_json(text: str) -> Any:
    """Parse JSON from Codex structured output.  Raises ValueError on failure."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from Codex: {exc} (output: {text[:400]})")
