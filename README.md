# maintain

An autonomous maintenance agent that continuously audits and repairs a GitHub repository. It uses **Codex CLI** as its AI engine and owns the state machine, finding queue, deduplication, and stopping logic itself.

Focus: correctness, security, reliability, tests, documentation accuracy, dead code, and maintainability. No new features, speculative redesigns, or cosmetic changes without concrete justification.

---

## Prerequisites

| Tool | Install |
|------|---------|
| Python ≥ 3.11 | system package manager |
| [Codex CLI](https://github.com/openai/codex) | `npm install -g @openai/codex` |
| [GitHub CLI](https://cli.github.com) | `brew install gh` or `apt install gh` |
| Git | already present on most systems |

Authenticate both CLIs before running:

```bash
codex login          # or set OPENAI_API_KEY
gh auth login
```

---

## Quick start

```bash
# 1. Clone or navigate to this repo.
cd /path/to/maintain

# 2. Copy the example config and fill in your repo details.
cp config.toml config.local.toml
$EDITOR config.local.toml   # set repo.owner and repo.name at minimum

# 3. Run a single maintenance pass.
python maintain.py --config config.local.toml run

# 4. (Optional) Run continuously until the repository is exhausted.
python maintain.py --config config.local.toml run-continuous
```

The SQLite state database (`.maintain.db` by default) is created automatically on first run. If `repo.path` is left empty, maintain clones and reuses the target repository automatically.

---

## Commands

```
python maintain.py run                 # one full pass over all audit areas
python maintain.py run-continuous      # loop until exhausted or budget exceeded
python maintain.py repair              # repair queued findings without auditing
python maintain.py status              # show queue, recent PRs, area exhaustion
python maintain.py findings            # list open/paused findings (prioritised)
python maintain.py audit <area>        # manually trigger one area's audit
python maintain.py reset               # clear all state (confirms before deleting)
```

Global flags accepted before the command:

```
--config PATH      config file (default: config.toml)
--db PATH          SQLite database (default: .maintain.db)
--log-level LEVEL  DEBUG | INFO | WARNING | ERROR  (default: INFO)
```

`repair` processes the existing queue without running a new audit — useful to drain findings from a previous run or resume an interrupted one. `run` both audits and repairs.

---

## Further reading

- [Configuration reference](docs/configuration.md) — all config options, managed clone
- [Internals](docs/internals.md) — lifecycle, state database, exhaustion tracking, crash recovery, finding revalidation, safety guarantees
