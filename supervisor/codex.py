from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class CodexError(Exception):
    pass


# JSON schemas for --output-schema structured output.
# All schemas satisfy Structured Outputs restrictions:
#   - root is an object
#   - additionalProperties: false on every object
#   - every property is listed in required (optional fields are nullable)
AUDIT_SCHEMA: str = json.dumps({
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title":       {"type": "string"},
                    "description": {"type": "string"},
                    "severity":    {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "confidence":  {"type": "string", "enum": ["high", "medium", "low"]},
                    "file_path":   {"type": ["string", "null"]},
                    "line_range":  {"type": ["string", "null"]},
                },
                "required": [
                    "title", "description", "severity", "confidence",
                    "file_path", "line_range",
                ],
            },
        },
    },
    "required": ["findings"],
})

REVALIDATE_SCHEMA: str = json.dumps({
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "still_applies": {"type": "boolean"},
        "reason":        {"type": "string"},
    },
    "required": ["still_applies", "reason"],
})

VERIFY_SCHEMA: str = json.dumps({
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "reason":  {"type": ["string", "null"]},
        "issues":  {"type": ["array", "null"], "items": {"type": "string"}},
    },
    "required": ["verdict", "reason", "issues"],
})

VALIDATE_SCHEMA: str = json.dumps({
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict":  {"type": "string", "enum": ["valid", "invalid", "uncertain"]},
        "reason":   {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["verdict", "reason", "evidence"],
})

REVIEW_SCHEMA: str = json.dumps({
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict":  {"type": "string", "enum": ["approve", "request_changes"]},
        "summary":  {"type": ["string", "null"]},
        "comments": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity":    {"type": "string", "enum": ["blocking", "optional"]},
                    "file":        {"type": ["string", "null"]},
                    "description": {"type": "string"},
                },
                "required": ["severity", "file", "description"],
            },
        },
    },
    "required": ["verdict", "summary", "comments"],
})


def _build_codex_cmd(
    cmd: str,
    model: str,
    flags: list[str],
    prompt: str,
    output_schema_path: str | None = None,
) -> list[str]:
    """Construct the full argv for a Codex invocation.

    When *flags* begins with a subcommand (e.g. "exec") rather than an option
    flag, --model is inserted after the subcommand so the CLI sees:
        codex exec --model <m> [other flags] <prompt>
    rather than:
        codex --model <m> exec [other flags] <prompt>

    *output_schema_path* must be a filesystem path to a JSON Schema file;
    Codex does not accept an inline schema string.
    """
    schema_flags = ["--output-schema", output_schema_path] if output_schema_path else []
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
    """Invoke Codex with a fresh context.  Returns stdout.  Raises CodexError.

    When *output_schema* is provided it is written to a temporary file whose
    path is passed to Codex via --output-schema; the file is removed afterwards.
    """
    schema_file: str | None = None
    try:
        if output_schema:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as fh:
                fh.write(output_schema)
                schema_file = fh.name

        full_cmd = _build_codex_cmd(cmd, model, flags, prompt, schema_file)
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
    finally:
        if schema_file:
            try:
                os.unlink(schema_file)
            except OSError:
                pass


def parse_json(text: str) -> Any:
    """Parse JSON from Codex structured output.  Raises ValueError on failure."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from Codex: {exc} (output: {text[:400]})")
