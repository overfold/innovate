# Configuration reference

## Managed clone

When `repo.path` is empty (the default), maintain creates and reuses a clone at `~/.maintain/workspaces/<owner>/<name>`:

- **First run** — the repository is cloned automatically before the audit starts.
- **Subsequent runs** — the existing clone is reused. The audit worktree always fetches `origin/<default_branch>` fail-closed before auditing.
- **Explicit path** — set `repo.path` to an existing local clone to use that checkout instead. Maintain will never auto-clone into, replace, or delete an explicitly configured path; it still fetches and creates temporary git worktrees from it.

Maintain fails closed if the managed path exists but is not a valid git repository, or if its remote does not match `repo.owner`/`repo.name`.

---

## `[repo]`

| Key | Description |
|-----|-------------|
| `owner` | GitHub owner / org (e.g. `my-company`) |
| `name` | Repository name without the owner prefix |
| `path` | Absolute (or relative to cwd) path to an existing local clone. **Optional** — leave empty to use the managed clone under `workspace`. When set, the directory must exist and its `origin` remote must match `owner`/`name`. |
| `workspace` | Root directory for maintain-managed clones. Each repository is stored at `<workspace>/<owner>/<name>`. Default: `~/.maintain/workspaces`. Ignored when `path` is set explicitly. |
| `default_branch` | Trunk branch; PRs are opened against it (default: `main`) |
| `commit_trailer` | Optional one-line trailer appended to every automated commit message (default: empty) |
| `pr_footer` | Optional text appended to every automated PR body (default: empty) |

---

## `[codex]`

| Key | Description |
|-----|-------------|
| `cmd` | Codex binary name (default: `codex`) |
| `model` | Default model identifier passed via `--model` (default: `o4-mini`) |
| `timeout` | Seconds before a Codex invocation is killed (default: `300`) |
| `audit_flags` | Flags for read-only calls — analysis, diff-review, revalidation. Default: `["exec"]` |
| `repair_flags` | Flags for file-editing calls — repair, implement review feedback. Default: `["exec", "--sandbox", "workspace-write"]` |
| `audit_model` | Model for the scoped audit stage. Falls back to `model` when absent. |
| `revalidate_model` | Model for the stale-check stage. Falls back to `model`. |
| `validate_model` | Model for the finding-validation stage. Falls back to `model`. |
| `repair_model` | Model for the repair stage. Falls back to `model`. |
| `verify_model` | Model for the diff-review stage (run after repair, before opening a PR). Falls back to `model`. |
| `review_model` | Model for the PR-review stage, including implementing blocking feedback. Falls back to `model`. |

`--model` is inserted automatically after the first element of `audit_flags` / `repair_flags` when that element is the `exec` subcommand. Configs that only set `model` continue to work unchanged.

---

## `[verify]`

| Key | Description |
|-----|-------------|
| `setup_cmd` | Shell command run inside each newly created repair worktree before Codex attempts a fix. Leave empty to skip. A non-zero exit aborts the run; see [Worktree setup](#worktree-setup). |
| `test_cmd` | Shell command run inside the worktree before opening a PR. Non-zero exit discards the fix and requeues the finding. Leave empty to skip. |
| `ci_wait_timeout` | Seconds to wait for GitHub CI after the PR is pushed. `0` skips the CI wait (implies `allow_no_ci`). |
| `allow_no_ci` | Set to `true` only for repos that genuinely have no CI. When `false` (the default), the supervisor blocks merges when no CI checks are found, when the GitHub API returns an error, or when the wait times out. |

### Worktree setup

When maintain creates a repair worktree it contains a bare checkout of the repository. If your project needs compiled dependencies, generated files, or a specific set of CLI tools, set `verify.setup_cmd` to install them before Codex runs.

```toml
# Node.js
setup_cmd = "npm ci"

# Python
setup_cmd = "pip install -e .[dev]"

# Custom bootstrap script
setup_cmd = "./scripts/bootstrap.sh"
```

**Recommended: [Mise](https://mise.jdx.dev)**

With a `mise.toml` checked into your repository, a single `mise install` restores the exact tool versions each worktree needs:

```toml
[verify]
setup_cmd = "mise install"
test_cmd  = "mise run verify"
```

**Failure semantics**

A non-zero exit from `setup_cmd`, or a setup command that leaves the worktree dirty, is treated as an infrastructure failure:

- `run_once` returns `"setup_error"` and `run-continuous` stops.
- The finding is requeued as open.
- Its repair-attempt count is not incremented.

Add build artifacts, caches, and installed packages to `.gitignore` — `phase_repair` commits with `git add -A` and any unignored files setup creates would contaminate the patch.

---

## `[budget]`

| Key | Default | Description |
|-----|---------|-------------|
| `max_audits_per_area` | 3 | Consecutive clean audits (at the same HEAD) before an area is exhausted |
| `max_fixes_per_run` | 20 | Max findings fixed per invocation of `run` |
| `max_review_rounds` | 4 | Max review→fix cycles per PR |
| `max_consecutive_failures` | 5 | Abort run after this many back-to-back failures |
| `codex_call_budget` | 100 | Total Codex invocations allowed per run |

---

## `[[audit_areas]]`

Each block defines one scoped audit. The `description` is injected verbatim into the Codex audit prompt, so be specific about what to look for.

The nine default areas (correctness, security, reliability, tests, persistence, api\_contracts, dependencies, maintainability, documentation) are compiled into `maintain.py` and used when no `[[audit_areas]]` entries appear in the config file. Adding even one `[[audit_areas]]` block **replaces all defaults**, so copy all nine if you only want to add one.

```toml
[[audit_areas]]
name        = "observability"
description = """
Missing structured logging for error paths, metrics without units,
traces that lose context across async boundaries, log statements
that expose sensitive data.
"""
```

The `description` drives what Codex looks for — a precise, concrete description yields better findings.
