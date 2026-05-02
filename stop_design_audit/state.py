"""ReviewState dataclass — replaces 15+ loose variables with one typed object."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from stop_design_audit.config import (
    LOG_TRUNCATE_LENGTH,
    ROUND_ID_LENGTH,
    STATE_EXPIRY,
    get_state_file,
)
from stop_design_audit.exit_helpers import log


@dataclass
class ReviewState:
    """Per-session state persisted between hook invocations."""

    session_id: str = ""
    session_hash: str = ""
    last_total_diff: int = 0
    last_files_seen: set[str] = field(default_factory=set)
    tier: str = ""
    auto_continue_count: int = 0
    fail_count: int = 0
    round_id: str = ""
    passed_agents: list[str] = field(default_factory=list)
    completed: bool = False
    violation_history: dict[str, dict] = field(default_factory=dict)
    review_attempts: int = 0
    delegated_pending: bool = False
    delegated_dispatch_time: str = ""
    delegated_blocked_once: bool = False
    subagent_pending: bool = False
    subagent_dispatch_time: str = ""
    subagent_blocked_once: bool = False

    # Transient flags (not persisted)
    is_new_session: bool = False
    is_stale: bool = False

    @classmethod
    def from_file(cls, session_hash: str) -> ReviewState:
        """Load state from disk. Returns fresh state if file missing or invalid."""
        state_file = get_state_file(session_hash)
        obj = cls(session_hash=session_hash)

        if not state_file.exists():
            return obj

        try:
            content = state_file.read_text()
            if not content.strip():
                return obj

            data = json.loads(content)
            if not isinstance(data, dict):
                log("State file is not a dict")
                return obj

            # Populate from file
            obj.session_id = data.get("session_id", "")
            obj.last_total_diff = data.get("last_total_diff", 0)
            obj.last_files_seen = set(data.get("last_files_seen", []))
            obj.tier = data.get("tier", "")
            obj.auto_continue_count = data.get("auto_continue_count", 0)
            obj.fail_count = data.get("fail_count", 0)
            obj.round_id = data.get("round_id", "")
            obj.passed_agents = data.get("passed_agents", [])
            obj.completed = data.get("completed", False)
            obj.violation_history = data.get("violation_history", {})
            obj.review_attempts = data.get("review_attempts", 0)
            obj.delegated_pending = data.get("delegated_pending", False)
            obj.delegated_dispatch_time = data.get("delegated_dispatch_time", "")
            obj.delegated_blocked_once = data.get("delegated_blocked_once", False)
            obj.subagent_pending = data.get("subagent_pending", False)
            obj.subagent_dispatch_time = data.get("subagent_dispatch_time", "")
            obj.subagent_blocked_once = data.get("subagent_blocked_once", False)
            # Legacy ``stash_count_at_dispatch`` field is intentionally
            # ignored — the count-based stash auto-recovery it powered
            # was retired (it conflated user stashes with subagent
            # stashes and corrupted unrelated working trees). The new
            # protection lives in the global PreToolUse hook
            # ``~/.claude/hooks/block-subagent-stash.py`` which reports
            # subagent stashes to the main Claude session without
            # mutating the working tree.

            # Check staleness
            timestamp_str = data.get("timestamp")
            if timestamp_str:
                try:
                    ts = datetime.fromisoformat(timestamp_str)
                    age_seconds = (datetime.now() - ts).total_seconds()
                    if age_seconds > STATE_EXPIRY:
                        log(
                            f"State stale ({age_seconds:.0f}s > {STATE_EXPIRY}s) - preserving position"
                        )
                        obj.is_stale = True
                except ValueError:
                    pass

            return obj

        except json.JSONDecodeError as e:
            log(f"State file is malformed JSON: {e}")
            return cls(session_hash=session_hash)
        except Exception as e:
            log(f"Error reading state file: {e}")
            return cls(session_hash=session_hash)

    def detect_session(self, transcript_path: str) -> None:
        """Detect if this is a new session, stale session, or continuing session.

        Resets appropriate counters based on detection.
        """
        session_key = transcript_path
        old_session_id = self.session_id

        log(
            f"Comparing old='{old_session_id[-LOG_TRUNCATE_LENGTH:] if old_session_id else ''}' "
            f"vs new='{session_key[-LOG_TRUNCATE_LENGTH:] if session_key else ''}'"
        )

        if old_session_id != session_key:
            # New session — reset everything
            log("New session detected - resetting all counters")
            self.is_new_session = True
            self.session_id = session_key
            self.last_total_diff = 0
            self.last_files_seen = set()
            self._reset_review_counters()
        elif self.is_stale:
            # Stale — preserve diff baseline, reset review counters
            log("Stale state - preserving diff, resetting review counters")
            self.session_id = session_key
            self._reset_review_counters()
        else:
            # Continuing session
            self.session_id = session_key

    def _reset_review_counters(self) -> None:
        """Reset review-specific counters."""
        self.auto_continue_count = 0
        self.fail_count = 0
        self.round_id = ""
        self.passed_agents = []
        self.completed = False
        self.tier = ""
        self.violation_history = {}
        self.review_attempts = 0
        self.delegated_pending = False
        self.delegated_dispatch_time = ""
        self.delegated_blocked_once = False
        self.subagent_pending = False
        self.subagent_dispatch_time = ""
        self.subagent_blocked_once = False

    def new_round(self) -> str:
        """Generate a new round_id and return it."""
        self.round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]
        self.passed_agents = []
        self.review_attempts = 0
        return self.round_id

    def save(self) -> None:
        """Persist state to disk."""
        data = {
            "session_id": self.session_id,
            "last_total_diff": self.last_total_diff,
            "last_files_seen": sorted(self.last_files_seen),
            "timestamp": datetime.now().isoformat(),
            "tier": self.tier,
            "auto_continue_count": self.auto_continue_count,
            "fail_count": self.fail_count,
            "round_id": self.round_id,
            "passed_agents": self.passed_agents,
            "completed": self.completed,
            "violation_history": self.violation_history,
            "review_attempts": self.review_attempts,
            "delegated_pending": self.delegated_pending,
            "delegated_dispatch_time": self.delegated_dispatch_time,
            "delegated_blocked_once": self.delegated_blocked_once,
            "subagent_pending": self.subagent_pending,
            "subagent_dispatch_time": self.subagent_dispatch_time,
            "subagent_blocked_once": self.subagent_blocked_once,
        }
        state_file = get_state_file(self.session_hash)
        try:
            state_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log(f"Error saving state: {e}")


def update_violation_history(
    history: dict[str, dict],
    review_results: dict,
) -> dict[str, dict]:
    """Update violation history from review results."""
    agents_results = review_results.get("agents", {})

    for agent_id, agent_data in agents_results.items():
        if not isinstance(agent_data, dict):
            continue
        issues = agent_data.get("issues", [])
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            file = issue.get("file", "")
            category = issue.get("category", "unknown")
            if not file:
                continue
            if file not in history:
                history[file] = {}
            if category not in history[file]:
                history[file][category] = 0
            history[file][category] += 1

    return history
