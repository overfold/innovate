AUDIT_PROMPT = """\
You are a meticulous code auditor.  Audit this entire repository for issues in
ONE specific category: {area}.

Category description: {description}

Rules:
- Inspect ALL relevant files thoroughly.
- Report only concrete, actionable problems with clear evidence.
- Do NOT suggest new features, speculative refactors, or cosmetic changes.
- Do NOT flag issues that are clearly intentional design decisions.
- Confidence should reflect how certain you are this is a real bug.

Return ONLY a JSON object with a "findings" array.  If there are no findings,
return {{"findings": []}}.
Each element of "findings":
{{
  "title":       "<concise problem title, under 80 chars>",
  "description": "<detailed: what is wrong, why it matters, how to fix>",
  "severity":    "critical|high|medium|low",
  "confidence":  "high|medium|low",
  "file_path":   "<repo-relative path or null>",
  "line_range":  "<e.g. L10-L25, or null>"
}}
"""

REVALIDATE_PROMPT = """\
A code finding was recorded when the repository was at a different commit.
Determine whether it still applies to the current state of the code.

Finding:
  Area:        {area}
  Title:       {title}
  File:        {file_path}
  Lines:       {line_range}
  Description:
{description}

Inspect the current repository carefully.  Check whether:
1. The named file still exists (if one was specified).
2. The specific problem described still exists in the current code.

Return ONLY a JSON object:
{{
  "still_applies": true|false,
  "reason":        "<one sentence explaining your conclusion>"
}}
"""

VALIDATE_PROMPT = """\
A code audit reported the following finding.  Your task is to INDEPENDENTLY
VALIDATE whether this finding is correct.  Do NOT propose or implement any fix.

Finding:
  Area:        {area}
  Title:       {title}
  File:        {file_path}
  Lines:       {line_range}
  Description:
{description}

Inspect the repository carefully at its current state.  Challenge the audit claim:
1. Can the described behavior actually occur given the code paths?
2. Do existing invariants, guards, or surrounding code already prevent the issue?
3. Could the finding misunderstand the intended behaviour?
4. Does the claimed impact actually follow from the code?

Do NOT suggest or implement any fix.  Report only whether the issue is real.

Return ONLY a JSON object:
{{
  "verdict":  "valid|invalid|uncertain",
  "reason":   "<one-sentence conclusion>",
  "evidence": "<concrete code facts supporting the verdict>"
}}

Use "valid" only when you are confident the issue exists as described.
Use "invalid" when you are confident the issue cannot occur or was misidentified.
Use "uncertain" when you cannot determine with confidence whether the issue exists.
"""

REPAIR_PROMPT = """\
Fix the following maintenance issue in this repository.
Make the MINIMAL change necessary — do not modify unrelated code.

Issue title:    {title}
Severity:       {severity}
File:           {file_path}
Lines:          {line_range}

Details:
{description}

After applying the fix, print a brief paragraph summarising exactly what you
changed and why.  Do not modify files beyond what is needed for this fix.
"""

VERIFY_PROMPT = """\
Inspect the repository and decide whether the fix is correct.

Intended fix: {title}
Background:
{description}

Run `git diff {base}` in the repository to inspect the complete set of changes.

Return ONLY a JSON object:
{{
  "verdict": "approve|reject",
  "reason":  "<one sentence>",
  "issues":  ["<specific problem>"]   // empty list if verdict is approve
}}
"""

REVIEW_PROMPT = """\
Review this pull request as part of an automated maintenance workflow.

PR title: {pr_title}
Fixing:   {finding_titles}

Run `git diff {base}` in the repository to inspect the complete set of changes.

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

Rules: if verdict is "request_changes", comments MUST contain at least one
entry with "severity": "blocking".  If verdict is "approve", comments must be
an empty list.
"""

IMPLEMENT_REVIEW_PROMPT = """\
Implement the following blocking review comments on this repository.
Address only these specific comments; do not make any other changes.

{comments}

After implementing, print a brief paragraph summarising what you changed.
"""
