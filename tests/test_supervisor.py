# This file has been split into smaller modules. Tests now live in:
#
#   tests/test_db.py           — database/state persistence
#   tests/test_reconcile.py    — startup_reconcile / crash recovery
#   tests/test_validation.py   — phase_validate, validation state machine
#   tests/test_repair.py       — phase_repair, rejected-HEAD logic
#   tests/test_review_ci.py    — phase_review_loop, CI integration
#   tests/test_orchestration.py — _fix_finding lifecycle, run_once return values
#   tests/test_config.py       — model selection, config validation
#   tests/test_managed_clone.py — managed-clone / _resolve_repo_path
