"""Exit helpers — every exit MUST go through one of the helpers below.

Raw sys.exit(0) is forbidden outside these helpers.

- allow_stop:          silent allow (nothing for the user to know)
- warn_and_allow:      allow, but show the user a visible systemMessage
- block_with_message:  block; Claude continues with the given instruction
- fail_loud:           hook/config error — block once so Claude relays it,
                       then (stop_hook_active) allow with a visible warning
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import NoReturn

from stop_design_audit import config

HOOK_NAME = "stop-design-audit"

# Set by the entry point from the hook input; True when this stop is already
# a continuation forced by a previous Stop-hook block.
STOP_HOOK_ACTIVE = False


def log(msg: str) -> None:
    """Write debug message to log file."""
    try:
        with open(config.DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass  # Logging is best-effort; it must never break the hook


def allow_stop(reason: str = "") -> NoReturn:
    """Terminal exit — allow Claude to stop. No stdout."""
    if reason:
        log(f"Allowing stop: {reason}")
    sys.exit(0)


def warn_and_allow(message: str) -> NoReturn:
    """Terminal exit — allow the stop but show the user a warning."""
    log(f"Allowing stop with warning: {message}")
    print(json.dumps({"systemMessage": f"[{HOOK_NAME}] {message}"}))
    sys.exit(0)


def block_with_message(reason: str) -> NoReturn:
    """Block stop, inject message into conversation. Claude continues.

    NEVER use this on terminal/completion paths — it will loop.
    """
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def fail_loud(error: str) -> NoReturn:
    """Hook or config error. Never allows the stop silently.

    First stop: block so Claude tells the user. If the stop is already a
    hook-forced continuation, allow it (no loop) with a visible warning.
    """
    log(f"FAIL LOUD: {error}")
    message = (
        f"[{HOOK_NAME}] hook error — code review did NOT run: {error} "
        f"(log: {config.DEBUG_FILE})"
    )
    payload: dict[str, str] = {"systemMessage": message}
    if not STOP_HOOK_ACTIVE:
        payload["decision"] = "block"
        payload["reason"] = (
            f"{message}\nTell the user about this error verbatim, then end your turn. "
            "Do not try to fix the hook yourself unless asked."
        )
    print(json.dumps(payload))
    sys.exit(0)


# EXIT_PATH_REGISTRY: Documents every exit and its expected behavior.
EXIT_PATH_REGISTRY = {
    "hook_skipped": {"type": "allow", "line_hint": "main()"},
    "no_code_modified": {"type": "allow", "line_hint": "main()"},
    "all_passed_max": {"type": "allow", "line_hint": "handle_all_passed()"},
    "all_passed_continue": {"type": "block", "line_hint": "handle_all_passed()"},
    "deep_auto_fix": {"type": "block", "line_hint": "handle_deep_failure()"},
    "deep_plan_agent": {"type": "block", "line_hint": "handle_deep_failure()"},
    "deep_completed": {"type": "allow", "line_hint": "main()"},
    "no_incremental_continue": {"type": "allow", "line_hint": "_handle_zero_diff()"},
    "no_incremental_default": {"type": "allow", "line_hint": "_handle_zero_diff()"},
    "skip_max_continues": {"type": "allow", "line_hint": "_handle_skip_tier()"},
    "skip_consecutive": {"type": "allow", "line_hint": "_handle_skip_tier()"},
    "skip_continue": {"type": "block", "line_hint": "_handle_skip_tier()"},
    "completed": {"type": "allow", "line_hint": "check_completion_guards()"},
    "max_continues": {"type": "allow", "line_hint": "check_completion_guards()"},
    "max_fails": {"type": "block", "line_hint": "check_completion_guards()"},
    "zero_diff_max_fails": {"type": "block", "line_hint": "_handle_zero_diff()"},
    "review_instructions": {"type": "block", "line_hint": "_run_agent_mode()"},
    "api_max_continues": {"type": "allow", "line_hint": "_run_api_mode()"},
    "api_clean": {"type": "block", "line_hint": "_run_api_mode()"},
    "api_violations": {"type": "block", "line_hint": "_run_api_mode()"},
    "delegated_dispatch": {"type": "block", "line_hint": "_run_delegated_mode()"},
    "delegated_pending_continue": {
        "type": "block",
        "line_hint": "_handle_delegated_pending()",
    },
    "delegated_pending_allow": {
        "type": "allow",
        "line_hint": "_handle_delegated_pending()",
    },
    "delegated_timeout": {"type": "block", "line_hint": "_handle_delegated_pending()"},
    "subagent_passed_max_notes": {"type": "warn", "line_hint": "_handle_pass()"},
    "subagent_passed_max": {"type": "allow", "line_hint": "_handle_pass()"},
    "subagent_passed": {"type": "block", "line_hint": "_handle_pass()"},
    "subagent_fail_report_only": {"type": "block", "line_hint": "_handle_fail()"},
    "subagent_fail_fix": {"type": "block", "line_hint": "_handle_fail()"},
    "subagent_results_rerequest": {
        "type": "block",
        "line_hint": "handle_subagent_pending()",
    },
    "subagent_results_unreviewed": {
        "type": "warn",
        "line_hint": "handle_subagent_pending()",
    },
    "subagent_dispatch": {"type": "block", "line_hint": "run_subagent_mode()"},
    "config_error": {"type": "fail_loud", "line_hint": "__main__.py"},
    "fatal_error": {"type": "fail_loud", "line_hint": "__main__.py"},
}
