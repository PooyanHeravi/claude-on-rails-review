"""Exit helpers — every exit MUST go through allow_stop() or block_with_message().

Raw sys.exit(0) is forbidden outside these helpers.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from stop_design_audit.config import DEBUG_FILE


def log(msg: str) -> None:
    """Write debug message to log file."""
    try:
        with open(DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass  # Never let logging break the hook


def allow_stop(reason: str = "") -> None:
    """Terminal exit — allow Claude to stop. No stdout."""
    if reason:
        log(f"Allowing stop: {reason}")
    sys.exit(0)


def block_with_message(reason: str) -> None:
    """Block stop, inject message into conversation. Claude continues.

    NEVER use this on terminal/completion paths — it will loop.
    """
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


# EXIT_PATH_REGISTRY: Documents every exit and its expected behavior.
EXIT_PATH_REGISTRY = {
    "hook_skipped": {"type": "allow", "line_hint": "main()"},
    "stdin_error": {"type": "allow", "line_hint": "main()"},
    "no_transcript": {"type": "allow", "line_hint": "main()"},
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
    "subagent_all_passed_max": {"type": "allow", "line_hint": "run_subagent_mode()"},
    "subagent_all_passed": {"type": "block", "line_hint": "run_subagent_mode()"},
    "subagent_dispatch": {"type": "block", "line_hint": "run_subagent_mode()"},
    "subagent_pending_continue": {
        "type": "block",
        "line_hint": "handle_subagent_pending()",
    },
    "subagent_pending_allow": {
        "type": "allow",
        "line_hint": "handle_subagent_pending()",
    },
    "subagent_timeout": {"type": "block", "line_hint": "handle_subagent_pending()"},
    "subagent_passed": {"type": "block", "line_hint": "process_subagent_results()"},
    "subagent_passed_max": {"type": "allow", "line_hint": "process_subagent_results()"},
    "subagent_deep_failure": {
        "type": "block",
        "line_hint": "process_subagent_results()",
    },
    "fatal_error": {"type": "allow", "line_hint": "__main__.py"},
}
