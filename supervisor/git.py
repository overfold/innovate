from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path

LOG = logging.getLogger("supervisor")


class GitHubAPIError(Exception):
    """Raised when a GitHub CLI call fails or returns unparseable output."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def clone_repo(url: str, dest: Path) -> None:
    """Clone url into dest, which must not already exist.

    Raises RuntimeError if git clone exits non-zero.
    """
    result = subprocess.run(
        ["git", "clone", "--", url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git clone failed")


def current_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def full_diff(repo: Path, base: str) -> str:
    return _git(repo, "diff", base).stdout


def create_branch(repo: Path, name: str, base: str) -> None:
    _git(repo, "checkout", base)
    _git(repo, "pull", "origin", base, check=False)
    _git(repo, "checkout", "-b", name)


def create_worktree(repo: Path, branch: str, start_point: str) -> Path:
    """Create a git worktree on a new branch rooted at start_point (a commit SHA).

    The caller must ensure start_point is already in the local object store
    (guaranteed when create_audit_worktree was called first on the same run).
    Always pair with remove_worktree() in a finally block.
    """
    wt_dir = Path(tempfile.mkdtemp(prefix="maintain-wt-"))
    _git(repo, "worktree", "add", "-b", branch, str(wt_dir), start_point)
    return wt_dir


def create_audit_worktree(repo: Path, base: str) -> Path:
    """Fetch origin/<base> (fail-closed) and create a detached-HEAD audit worktree.

    Raises subprocess.CalledProcessError if the fetch or worktree creation fails.
    Always pair with remove_audit_worktree() in a finally block.
    """
    _git(repo, "fetch", "origin", base)  # fail-closed
    wt_dir = Path(tempfile.mkdtemp(prefix="maintain-audit-"))
    _git(repo, "worktree", "add", "--detach", str(wt_dir), f"origin/{base}")
    return wt_dir


def remove_audit_worktree(repo: Path, wt_path: Path) -> None:
    """Remove a detached audit worktree (no branch to delete)."""
    _git(repo, "worktree", "remove", "--force", str(wt_path), check=False)


def remove_worktree(repo: Path, branch: str, wt_path: Path) -> None:
    """Remove a worktree and delete its local branch."""
    _git(repo, "worktree", "remove", "--force", str(wt_path), check=False)
    _git(repo, "branch", "-D", branch, check=False)


def create_branch_worktree(repo: Path, branch: str) -> Path:
    """Check out an existing remote branch into a fresh temporary worktree.

    Raises subprocess.CalledProcessError if the fetch fails so callers never
    proceed with stale or absent local branch state.
    """
    _git(repo, "fetch", "origin", branch)  # fail-closed: raises on network/auth error
    # Force-reset the local tracking ref to the just-fetched remote state so an
    # accidentally lingering local branch cannot shadow the fetch result.
    _git(repo, "branch", "-f", branch, f"origin/{branch}")
    wt_dir = Path(tempfile.mkdtemp(prefix="maintain-wt-"))
    _git(repo, "worktree", "add", str(wt_dir), branch)
    return wt_dir


def remove_branch_worktree(repo: Path, wt_path: Path) -> None:
    """Remove a branch worktree without deleting the underlying branch."""
    _git(repo, "worktree", "remove", "--force", str(wt_path), check=False)


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


def gh_create_pr(
    owner: str,
    repo_name: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> tuple[int, str]:
    """Returns (pr_number, pr_url)."""
    r = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", f"{owner}/{repo_name}",
            "--head", branch,
            "--base", base,
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


def gh_find_pr_by_branch(
    owner: str, repo_name: str, branch: str
) -> tuple[int, str] | None:
    """Search for an open PR whose head branch matches *branch*.

    Returns (pr_number, pr_url) if an open PR was found, or None if there
    is genuinely no open PR for that branch.
    Raises GitHubAPIError on CLI failure or unparseable output — callers
    must not treat an API failure as "no PR exists."
    """
    r = subprocess.run(
        [
            "gh", "pr", "list",
            "--repo", f"{owner}/{repo_name}",
            "--head", branch,
            "--state", "open",
            "--json", "number,url",
            "--limit", "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise GitHubAPIError(
            f"gh pr list failed (exit {r.returncode}): {r.stderr.strip()}"
        )
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubAPIError(f"gh pr list returned non-JSON: {r.stdout!r}") from exc
    if not items:
        return None
    item = items[0]
    return item.get("number", 0), item.get("url", "")


def gh_pr_state(owner: str, repo_name: str, pr_number: int) -> str:
    """Return 'open', 'merged', or 'closed'.

    Raises GitHubAPIError on CLI failure or unparseable output — callers must
    not treat an API failure as a known PR state.
    """
    r = subprocess.run(
        [
            "gh", "pr", "view", str(pr_number),
            "--repo", f"{owner}/{repo_name}",
            "--json", "state,mergedAt",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise GitHubAPIError(
            f"gh pr view #{pr_number} failed (exit {r.returncode}): {r.stderr.strip()}"
        )
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubAPIError(
            f"gh pr view #{pr_number} returned non-JSON: {r.stdout!r}"
        ) from exc
    if data.get("mergedAt"):
        return "merged"
    state = data.get("state", "").lower()
    if state not in ("open", "merged", "closed"):
        raise GitHubAPIError(
            f"gh pr view #{pr_number}: unexpected state {state!r}"
        )
    return state


def gh_pr_base_sha(owner: str, repo_name: str, pr_number: int) -> str:
    """Return the current base-branch OID for the PR.

    Raises GitHubAPIError on CLI failure, unparseable output, or missing field.
    Used as a freshness gate immediately before merge: if the base has advanced
    since the audit, the patch was never tested against the new commits.
    """
    r = subprocess.run(
        [
            "gh", "pr", "view", str(pr_number),
            "--repo", f"{owner}/{repo_name}",
            "--json", "baseRefOid",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise GitHubAPIError(
            f"gh pr view #{pr_number} failed (exit {r.returncode}): {r.stderr.strip()}"
        )
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubAPIError(
            f"gh pr view #{pr_number} returned non-JSON: {r.stdout!r}"
        ) from exc
    oid = data.get("baseRefOid", "")
    if not oid:
        raise GitHubAPIError(
            f"gh pr view #{pr_number}: baseRefOid missing or empty"
        )
    return oid


def gh_close_pr(owner: str, repo_name: str, pr_number: int) -> None:
    r = subprocess.run(
        ["gh", "pr", "close", str(pr_number), "--repo", f"{owner}/{repo_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise GitHubAPIError(
            f"gh pr close #{pr_number} failed (exit {r.returncode}): {r.stderr.strip()}"
        )


def gh_ci_status(owner: str, repo_name: str, pr_number: int) -> str:
    """Returns one of: 'success' | 'failure' | 'pending' | 'no_checks' | 'api_error'.

    'no_checks'  — the PR exists but has no CI checks configured.
    'api_error'  — could not reach the API or parse its response.

    Both are blocking by default; set verify.allow_no_ci=true to permit
    merging when there are no checks.

    Uses the `bucket` field that `gh pr checks` normalises into one of:
    pass, fail, pending, skipping, cancel.
    """
    r = subprocess.run(
        [
            "gh", "pr", "checks", str(pr_number),
            "--repo", f"{owner}/{repo_name}",
            "--json", "bucket",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit code 8 is authoritative: checks are still pending.
    # Any other non-zero code is a genuine CLI/API failure.
    if r.returncode == 8:
        return "pending"
    if r.returncode != 0:
        return "api_error"
    try:
        checks = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "api_error"
    if not checks:
        return "no_checks"
    buckets = {c.get("bucket") for c in checks}
    if "fail" in buckets:
        return "failure"
    if "cancel" in buckets:
        return "api_error"
    if "pending" in buckets:
        return "pending"
    if buckets <= {"pass", "skipping"}:
        return "success"
    return "api_error"


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


def gh_get_failed_ci_logs(owner: str, repo_name: str, pr_number: int) -> str | None:
    """Return log output from all failed CI runs for the current PR head.

    Returns None on any API or parse failure (fail-closed: callers must treat
    None as an infrastructure error, not as an absence of CI evidence).
    Returns "" if the API call succeeded but no failed runs were found.
    Returns concatenated log output (up to 6 000 chars total) on success.
    """
    r = subprocess.run(
        [
            "gh", "pr", "view", str(pr_number),
            "--repo", f"{owner}/{repo_name}",
            "--json", "headRefOid",
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    sha = data.get("headRefOid", "")
    if not sha:
        return None

    r = subprocess.run(
        [
            "gh", "run", "list",
            "--repo", f"{owner}/{repo_name}",
            "--commit", sha,
            "--status", "failure",
            "--json", "databaseId",
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    try:
        runs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not runs:
        return ""

    cap = 6000
    parts: list[str] = []
    used = 0
    for entry in runs:
        if used >= cap:
            break
        run_id = entry.get("databaseId")
        if not run_id:
            continue
        r = subprocess.run(
            [
                "gh", "run", "view", str(run_id),
                "--repo", f"{owner}/{repo_name}",
                "--log-failed",
            ],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            continue
        chunk = f"=== run {run_id} ===\n{r.stdout}"
        remaining = cap - used
        if len(chunk) > remaining:
            chunk = chunk[-remaining:]
        parts.append(chunk)
        used += len(chunk)

    if parts:
        return "\n".join(parts)
    # runs were listed but every log-fetch call failed — infrastructure error
    return None


def wait_for_ci(
    owner: str,
    repo_name: str,
    pr_number: int,
    timeout_s: int,
    allow_no_ci: bool,
) -> str:
    """Poll CI until a terminal state is reached.

    Returns 'success', 'failure', 'no_checks', 'api_error', or 'timeout'.
    Only 'success' (or 'no_checks' when allow_no_ci=True) permits a merge.
    """
    if timeout_s <= 0:
        return gh_ci_status(owner, repo_name, pr_number)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = gh_ci_status(owner, repo_name, pr_number)
        if status != "pending":
            return status
        LOG.info("  CI pending — waiting 30 s…")
        time.sleep(30)

    LOG.warning("  CI wait timed out after %d s", timeout_s)
    return "timeout"


def ci_permits_merge(ci_status: str, allow_no_ci: bool) -> bool:
    if ci_status == "success":
        return True
    if ci_status == "no_checks" and allow_no_ci:
        return True
    return False
