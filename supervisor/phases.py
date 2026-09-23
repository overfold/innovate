from __future__ import annotations

import logging
from pathlib import Path

from .codex import (
    AUDIT_SCHEMA,
    REVALIDATE_SCHEMA,
    REVIEW_SCHEMA,
    VALIDATE_SCHEMA,
    CodexError,
    parse_json,
    run_codex,
)
from .db import DB, _fingerprint
from .git import (
    _git,
    ci_permits_merge,
    current_commit,
    full_diff,
    gh_get_failed_ci_logs,
    push_branch,
    wait_for_ci,
)
from .prompts import (
    AUDIT_PROMPT,
    IMPLEMENT_REVIEW_PROMPT,
    REPAIR_PROMPT,
    REVALIDATE_PROMPT,
    REVIEW_CI_PROMPT,
    REVIEW_PROMPT,
    VALIDATE_PROMPT,
)

LOG = logging.getLogger("supervisor")

_STAGES = ("audit", "revalidate", "validate", "repair", "review")


def model_for_stage(cfg: dict, stage: str) -> str:
    """Return the Codex model for *stage*, falling back to ``codex.model`` when
    the stage-specific key is absent, ``None``, or an empty string."""
    return cfg["codex"].get(f"{stage}_model") or cfg["codex"]["model"]


def _invoke_codex(cfg: dict, ctr: dict, prompt: str, repo: Path, **kwargs) -> str:
    """Atomically check the budget, increment the call counter, and run Codex.

    Raises CodexError if the per-run call budget is already exhausted, making
    the budget a hard limit regardless of where the call originates.
    """
    budget = cfg["budget"]["codex_call_budget"]
    if ctr["codex_calls"] >= budget:
        raise CodexError(f"Codex call budget ({budget}) exhausted")
    ctr["codex_calls"] += 1
    return run_codex(prompt, repo, **kwargs)


# Review-loop outcome constants returned by phase_review_loop.
REVIEW_APPROVED        = "approved"
REVIEW_FAILED_ERROR    = "failed_error"    # Codex call failed or parse error
REVIEW_PAUSED_BUDGET   = "paused_budget"   # Review rounds exhausted (blocked)
REVIEW_DEFERRED_BUDGET = "deferred_budget" # Per-run Codex budget hit; resume next run
REVIEW_CI_UNCERTAIN    = "ci_uncertain"    # CI state unknown; leave PR for startup_reconcile


# ── Lifecycle phases ───────────────────────────────────────────────────────────

def phase_audit(cfg: dict, db: DB, area: dict, ctr: dict) -> int:
    """Run a scoped Codex audit for *area*.  Returns count of new findings stored."""
    repo = Path(cfg["repo"]["path"]).resolve()
    commit = current_commit(repo)
    run_id = db.start_audit(area["name"], commit)

    prompt = AUDIT_PROMPT.format(area=area["name"], description=area["description"])
    LOG.info("Auditing %-20s @ %s", area["name"], commit[:7])

    try:
        output = _invoke_codex(
            cfg, ctr, prompt, repo,
            flags=cfg["codex"]["audit_flags"],
            cmd=cfg["codex"]["cmd"],
            model=model_for_stage(cfg, "audit"),
            timeout=cfg["codex"]["timeout"],
            output_schema=AUDIT_SCHEMA,
        )
        parsed = parse_json(output)
    except (CodexError, ValueError) as exc:
        LOG.error("Audit failed for %s: %s", area["name"], exc)
        db.finish_audit(run_id, 0, 0, "failed")
        ctr["consecutive_failures"] += 1
        return 0

    raw_findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if not isinstance(raw_findings, list):
        LOG.error(
            "Audit %s: unexpected output structure — treating as failed",
            area["name"],
        )
        db.finish_audit(run_id, 0, 0, "failed")
        ctr["consecutive_failures"] += 1
        return 0

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
                LOG.info(
                    "  [%s/%s] %s",
                    finding["severity"], finding["confidence"], finding["title"],
                )
        except (KeyError, TypeError) as exc:
            LOG.warning("  Skipping malformed finding: %s", exc)

    db.finish_audit(run_id, new_count, len(raw_findings))
    LOG.info("Audit %s: %d new (%d total)", area["name"], new_count, len(raw_findings))
    ctr["consecutive_failures"] = 0
    return new_count


def phase_revalidate(cfg: dict, finding: dict, ctr: dict) -> str:
    """Check whether a queued finding still applies at the current HEAD.

    Returns 'valid' | 'stale' | 'error'.

    'error' means the revalidation call itself failed; the caller should skip
    this finding for the current run and increment consecutive_failures.
    """
    repo = Path(cfg["repo"]["path"]).resolve()

    if finding.get("file_path"):
        fp = repo / finding["file_path"]
        if not fp.exists():
            LOG.info(
                "  File no longer exists (%s) — marking stale", finding["file_path"]
            )
            return "stale"

    prompt = REVALIDATE_PROMPT.format(
        area=finding["area"],
        title=finding["title"],
        file_path=finding["file_path"] or "(no specific file)",
        line_range=finding["line_range"] or "(no specific lines)",
        description=finding["description"],
    )
    try:
        output = _invoke_codex(
            cfg, ctr, prompt, repo,
            flags=cfg["codex"]["audit_flags"],
            cmd=cfg["codex"]["cmd"],
            model=model_for_stage(cfg, "revalidate"),
            timeout=cfg["codex"]["timeout"],
            output_schema=REVALIDATE_SCHEMA,
        )
        result = parse_json(output)
    except (CodexError, ValueError) as exc:
        LOG.warning("  Revalidation call failed: %s", exc)
        return "error"

    still_applies = result.get("still_applies", True)  # conservative default
    reason = result.get("reason", "")
    status = "valid" if still_applies else "stale"
    LOG.info("  Revalidation: %s — %s", status, reason)
    return status


def phase_validate(cfg: dict, finding: dict, ctr: dict, *, db: DB | None = None) -> str:
    """Independently validate an audit finding at the exact audited HEAD.

    Uses a fresh Codex context.  Does NOT propose or implement any fix.
    Returns 'valid' | 'invalid' | 'uncertain' | 'error' | 'deferred'.

    'deferred' means the Codex call budget was exhausted; the caller should
    requeue the finding for the next run without counting it as a failure.
    'error' means any other Codex/schema/parse failure; fail-closed.

    When *db* is provided the verdict, reason, and evidence are persisted via
    db.set_validation before returning, so crash-restarts at the same HEAD
    skip the Codex call and proceed directly to repair.
    """
    repo = Path(cfg["repo"]["path"]).resolve()

    prompt = VALIDATE_PROMPT.format(
        area=finding["area"],
        title=finding["title"],
        file_path=finding.get("file_path") or "(no specific file)",
        line_range=finding.get("line_range") or "(no specific lines)",
        description=finding.get("description", ""),
    )
    try:
        output = _invoke_codex(
            cfg, ctr, prompt, repo,
            flags=cfg["codex"]["audit_flags"],
            cmd=cfg["codex"]["cmd"],
            model=model_for_stage(cfg, "validate"),
            timeout=cfg["codex"]["timeout"],
            output_schema=VALIDATE_SCHEMA,
        )
        result = parse_json(output)
    except CodexError as exc:
        if "exhausted" in str(exc):
            LOG.info("  Validation deferred — Codex call budget exhausted")
            return "deferred"
        LOG.warning("  Validation call failed: %s — fail-closed", exc)
        return "error"
    except ValueError as exc:
        LOG.warning("  Validation call failed: %s — fail-closed", exc)
        return "error"

    verdict = result.get("verdict")
    if verdict not in ("valid", "invalid", "uncertain"):
        LOG.warning("  Validation returned unexpected verdict %r — fail-closed", verdict)
        return "error"

    reason = result.get("reason", "")
    evidence = result.get("evidence", "")
    LOG.info("  Validation verdict: %s — %s", verdict, reason)
    if evidence:
        LOG.info("  Evidence: %.200s", evidence)

    if db is not None:
        head = current_commit(repo)
        db.set_validation(finding["id"], verdict, head, reason=reason, evidence=evidence)

    return verdict


def phase_repair(cfg: dict, db: DB, findings: list, ctr: dict) -> bool:
    """Apply fixes inside the current worktree checkout.

    The caller is responsible for creating the worktree/branch and for
    cleaning it up on failure. Returns True if changes were committed.
    """
    repo = Path(cfg["repo"]["path"]).resolve()
    area = findings[0]["area"]
    base = current_commit(repo)

    for f in findings:
        prompt = REPAIR_PROMPT.format(
            title=f["title"],
            severity=f["severity"],
            file_path=f["file_path"] or "(unknown)",
            line_range=f["line_range"] or "(unknown)",
            description=f["description"],
        )
        LOG.info("  Repairing: %s", f["title"])
        try:
            _invoke_codex(
                cfg, ctr, prompt, repo,
                flags=cfg["codex"]["repair_flags"],
                cmd=cfg["codex"]["cmd"],
                model=model_for_stage(cfg, "repair"),
                timeout=cfg["codex"]["timeout"],
            )
        except CodexError as exc:
            LOG.error("  Repair failed: %s", exc)
            ctr["consecutive_failures"] += 1
            return False

    diff = full_diff(repo, base)
    if not diff.strip():
        LOG.warning("  Repair produced no file changes — skipping")
        for f in findings:
            db.mark_finding(f["id"], "rejected", reason="repair produced no changes", head=base)
        ctr["consecutive_failures"] += 1
        return False

    titles = "; ".join(f["title"] for f in findings)
    commit_msg = f"maint({area}): {titles[:72]}"
    trailer = cfg["repo"].get("commit_trailer", "")
    if trailer:
        commit_msg += f"\n\n{trailer}"
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", commit_msg)

    return True


def phase_review_loop(
    cfg: dict,
    db: DB,
    pr_id: int,
    pr_number: int,
    findings: list,
    branch: str,
    ctr: dict,
    *,
    initial_ci_evidence: str | None = None,
) -> str:
    """Review the PR, implement feedback, poll CI, and repeat within max_review_rounds.

    Full loop per round:
        review → [implement blocking feedback → push →] poll CI
        → CI passes: REVIEW_APPROVED
        → CI fails:  collect logs → loop back with CI evidence (next round)
        → CI uncertain: REVIEW_CI_UNCERTAIN

    CI-driven code changes always receive a fresh review before CI is polled again.
    All review passes (code review and CI-triggered) share the max_review_rounds budget.

    Returns one of REVIEW_APPROVED, REVIEW_FAILED_ERROR, REVIEW_PAUSED_BUDGET,
    REVIEW_DEFERRED_BUDGET, or REVIEW_CI_UNCERTAIN.

    REVIEW_PAUSED_BUDGET means rounds were exhausted without merge; the PR is
    left open and the caller marks the finding blocked.
    REVIEW_FAILED_ERROR means a Codex call or parse failed; caller should close
    the PR and re-queue the finding.
    REVIEW_CI_UNCERTAIN means CI returned an ambiguous result; the PR is left
    in_progress for startup_reconcile to retry on the next run.
    """
    repo = Path(cfg["repo"]["path"]).resolve()
    base = cfg["repo"].get("base_sha") or cfg["repo"]["default_branch"]
    owner = cfg["repo"]["owner"]
    repo_name = cfg["repo"]["name"]
    max_rounds = cfg["budget"]["max_review_rounds"]
    ci_timeout = cfg["verify"]["ci_wait_timeout"]
    allow_no_ci = cfg["verify"]["allow_no_ci"]
    finding_titles = "; ".join(f["title"] for f in findings)
    pr_title = f"maint: {finding_titles[:60]}"
    codex_budget = cfg["budget"]["codex_call_budget"]

    # Log output from a prior failed CI run, supplied to the reviewer next round.
    ci_evidence: str | None = initial_ci_evidence

    # Resume from the persisted round count so the lifetime budget is shared
    # across all runs (including ci_paused retries).
    pr_row = db.get_pr(pr_id)
    rounds_already_used = pr_row["review_rounds"] if pr_row else 0

    for rnd in range(rounds_already_used + 1, max_rounds + 1):
        LOG.info("  Review round %d/%d", rnd, max_rounds)

        if ctr["codex_calls"] >= codex_budget:
            LOG.warning(
                "  Codex call budget (%d) reached — deferring to next run",
                codex_budget,
            )
            db.update_pr(pr_id, review_rounds=rnd - 1)
            return REVIEW_DEFERRED_BUDGET

        diff = full_diff(repo, base)
        if ci_evidence is not None:
            prompt = REVIEW_CI_PROMPT.format(
                pr_title=pr_title,
                finding_titles=finding_titles,
                base=base,
                ci_logs=ci_evidence,
            )
        else:
            prompt = REVIEW_PROMPT.format(
                pr_title=pr_title,
                finding_titles=finding_titles,
                base=base,
            )

        try:
            output = _invoke_codex(
                cfg, ctr, prompt, repo,
                flags=cfg["codex"]["audit_flags"],
                cmd=cfg["codex"]["cmd"],
                model=model_for_stage(cfg, "review"),
                timeout=cfg["codex"]["timeout"],
                output_schema=REVIEW_SCHEMA,
            )
            review = parse_json(output)
        except (CodexError, ValueError) as exc:
            LOG.error("  Review call failed: %s — fail-closed", exc)
            db.update_pr(pr_id, review_rounds=rnd)
            return REVIEW_FAILED_ERROR

        # Default to request_changes, not approve (fail-closed).
        verdict = review.get("verdict", "request_changes")
        LOG.info("  Review verdict: %s — %s", verdict, review.get("summary", ""))

        blocking = [
            c for c in (review.get("comments") or [])
            if c.get("severity") == "blocking"
        ]

        if verdict == "approve" and not blocking:
            # If the reviewer saw CI failure evidence and approved, they cleared the
            # failure as unrelated — but we still cannot merge while CI is red.
            # Return REVIEW_CI_UNCERTAIN so the PR enters ci_paused and the next
            # _resume_paused_reviews pass re-checks whether CI has cleared.
            if ci_evidence is not None:
                db.update_pr(pr_id, review_rounds=rnd)
                return REVIEW_CI_UNCERTAIN

            # No prior CI evidence — poll CI before permitting merge.
            ci = wait_for_ci(owner, repo_name, pr_number, ci_timeout, allow_no_ci)
            LOG.info("  CI status: %s", ci)

            if ci_permits_merge(ci, allow_no_ci):
                db.update_pr(pr_id, review_rounds=rnd)
                return REVIEW_APPROVED

            if ci == "failure":
                # Definite CI failure: collect logs and pass to reviewer next round.
                logs = gh_get_failed_ci_logs(owner, repo_name, pr_number)
                if logs is None:
                    # API failure retrieving CI logs — treat as uncertain state.
                    LOG.warning(
                        "  CI failed but log retrieval failed"
                        " — treating as uncertain infrastructure state"
                    )
                    db.update_pr(pr_id, review_rounds=rnd)
                    return REVIEW_CI_UNCERTAIN
                ci_evidence = logs
                LOG.info(
                    "  CI failed — collecting evidence for reviewer"
                    " (%d/%d rounds used, %d remaining)",
                    rnd, max_rounds, max_rounds - rnd,
                )
                continue  # advance rnd, reviewer sees ci_evidence next iteration

            # Uncertain CI (timeout, api_error, no_checks when not allowed):
            # do not make code changes; preserve the PR for startup_reconcile.
            LOG.warning(
                "  CI status %r is uncertain — leaving PR open for startup_reconcile",
                ci,
            )
            db.update_pr(pr_id, review_rounds=rnd)
            return REVIEW_CI_UNCERTAIN

        if not blocking:
            # request_changes with no blocking comments is an inconsistent
            # schema response — fail-closed rather than silently approve.
            LOG.error(
                "  %s with no blocking comments — inconsistent review (fail-closed)",
                verdict,
            )
            db.update_pr(pr_id, review_rounds=rnd)
            return REVIEW_FAILED_ERROR

        for c in blocking:
            LOG.info("  Blocking: [%s] %s", c.get("file", "general"), c["description"])

        comments_text = "\n".join(
            f"- [{c.get('file', 'general')}] {c['description']}" for c in blocking
        )
        impl_prompt = IMPLEMENT_REVIEW_PROMPT.format(comments=comments_text)
        prev_diff = diff

        if ctr["codex_calls"] >= codex_budget:
            LOG.warning(
                "  Codex call budget (%d) reached before implementing feedback"
                " — deferring to next run",
                codex_budget,
            )
            db.update_pr(pr_id, review_rounds=rnd)
            return REVIEW_DEFERRED_BUDGET

        try:
            _invoke_codex(
                cfg, ctr, impl_prompt, repo,
                flags=cfg["codex"]["repair_flags"],
                cmd=cfg["codex"]["cmd"],
                model=model_for_stage(cfg, "review"),
                timeout=cfg["codex"]["timeout"],
            )
        except CodexError as exc:
            LOG.error("  Failed to implement review feedback: %s", exc)
            db.update_pr(pr_id, review_rounds=rnd)
            return REVIEW_FAILED_ERROR

        if full_diff(repo, base) == prev_diff:
            LOG.warning("  Review feedback produced no changes — fail-closed")
            db.update_pr(pr_id, review_rounds=rnd)
            return REVIEW_FAILED_ERROR

        commit_msg = f"maint: address review feedback (round {rnd})"
        trailer = cfg["repo"].get("commit_trailer", "")
        if trailer:
            commit_msg += f"\n\n{trailer}"
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", commit_msg)

        push_branch(repo, branch)

        # Clear CI evidence: the new push starts a fresh CI run.
        ci_evidence = None

    db.update_pr(pr_id, review_rounds=max_rounds)
    LOG.warning("  Review round budget (%d) exhausted without approval", max_rounds)
    return REVIEW_PAUSED_BUDGET
