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


def current_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def full_diff(repo: Path, base: str) -> str:
    return _git(repo, "diff", base).stdout


def create_branch(repo: Path, name: str, base: str) -> None:
    _git(repo, "checkout", base)
    _git(repo, "pull", "origin", base, check=False)
    _git(repo, "checkout", "-b", name)


def create_worktree(repo: Path, branch: str, base: str) -> Path:
    """Create a git worktree on a new branch and return its path.

    The worktree is an isolated checkout — crashes here leave the main repo
    untouched. Always pair with remove_worktree() in a finally block.
    Raises subprocess.CalledProcessError if the fetch fails.
    """
    _git(repo, "fetch", "origin", base)  # fail-closed
    wt_dir = Path(tempfile.mkdtemp(prefix="maintain-wt-"))
    _git(repo, "worktree", "add", "-b", branch, str(wt_dir), f"origin/{base}")
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
    return state if state in ("open", "merged", "closed") else "closed"


def gh_close_pr(owner: str, repo_name: str, pr_number: int) -> None:
    subprocess.run(
        ["gh", "pr", "close", str(pr_number), "--repo", f"{owner}/{repo_name}"],
        capture_output=True,
        text=True,
        check=False,
    )


def gh_ci_status(owner: str, repo_name: str, pr_number: int) -> str:
    """Returns one of: 'success' | 'failure' | 'pending' | 'no_checks' | 'api_error'.

    'no_checks'  — the PR exists but has no CI checks configured.
    'api_error'  — could not reach the API or parse its response.

    Both are blocking by default; set verify.allow_no_ci=true to permit
    merging when there are no checks.
    """
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
        return "api_error"
    try:
        checks = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "api_error"
    if not checks:
        return "no_checks"
    if any(c.get("conclusion") == "failure" for c in checks):
        return "failure"
    if any(c.get("status") == "in_progress" for c in checks):
        return "pending"
    if all(c.get("conclusion") in ("success", "skipped", None) for c in checks):
        statuses = {c.get("status") for c in checks}
        if "completed" in statuses:
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
