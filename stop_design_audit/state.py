"""ReviewState dataclass — replaces 15+ loose variables with one typed object."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from stop_design_audit.config import (
    LOG_TRUNCATE_LENGTH,
    RETRY_INITIAL_DELAY,
    ROUND_ID_LENGTH,
    STATE_EXPIRY,
    get_state_file,
)
from stop_design_audit.exit_helpers import log

SAVE_REPLACE_ATTEMPTS = 5


# Persisted fields: name -> (accepted JSON types, default). One table drives
# loading and validation, so a mistyped field is caught up front (and the
# file quarantined) instead of failing later on every stop.
_PERSISTED_FIELDS: dict[str, tuple[tuple[type, ...], object]] = {
    "session_id": ((str,), ""),
    "last_total_diff": ((int,), 0),
    "last_files_seen": ((list,), []),
    "tier": ((str,), ""),
    "auto_continue_count": ((int,), 0),
    "fail_count": ((int,), 0),
    "round_id": ((str,), ""),
    "passed_agents": ((list,), []),
    "completed": ((bool,), False),
    "violation_history": ((dict,), {}),
    "review_attempts": ((int,), 0),
    "delegated_pending": ((bool,), False),
    "delegated_dispatch_time": ((str,), ""),
    "delegated_blocked_once": ((bool,), False),
    "subagent_pending": ((bool,), False),
    "subagent_dispatch_time": ((str,), ""),
    "review_agents": ((list,), []),
    "review_diff_chars": ((int,), 0),
    "review_file_count": ((int,), 0),
}
_LIST_OF_STR_FIELDS = {"last_files_seen", "passed_agents", "review_agents"}


def _validated_fields(data: dict) -> dict[str, object]:
    """Typed field values from a state dict. Raises ValueError on any mistype."""
    out: dict[str, object] = {}
    for name, (types, default) in _PERSISTED_FIELDS.items():
        value = data.get(name, default)
        # bool is an int subclass: an int field must not accept True/False
        if not isinstance(value, types) or (
            bool not in types and isinstance(value, bool)
        ):
            raise ValueError(f"field {name!r} has type {type(value).__name__}")
        if name in _LIST_OF_STR_FIELDS and not all(isinstance(v, str) for v in value):
            raise ValueError(f"field {name!r} must be a list of strings")
        out[name] = set(value) if name == "last_files_seen" else value
    return out


def _quarantine(state_file: Path, error: Exception) -> ValueError:
    """Move a corrupt state file aside so it is reported once, not on every stop.

    Returns the ValueError to raise; it always names the corruption, and says
    so if the move itself failed (e.g. Windows sharing violation).
    """
    aside = state_file.with_suffix(f".corrupt-{int(time.time())}")
    for attempt in range(SAVE_REPLACE_ATTEMPTS):
        try:
            os.replace(state_file, aside)
            return ValueError(
                f"corrupt state file moved to {aside.name}: {error}. "
                "Any pending review round in it was NOT reviewed."
            )
        except PermissionError:
            time.sleep(RETRY_INITIAL_DELAY * (attempt + 1))
        except OSError as move_error:
            return ValueError(
                f"corrupt state file {state_file.name}: {error}. Could not move it "
                f"aside ({move_error}); delete it to recover. NOT reviewed."
            )
    return ValueError(
        f"corrupt state file {state_file.name}: {error}. Could not move it aside "
        "(file in use); delete it to recover. NOT reviewed."
    )


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
    review_agents: list[str] = field(default_factory=list)
    review_diff_chars: int = 0
    review_file_count: int = 0

    # Transient flags (not persisted)
    is_new_session: bool = False
    is_stale: bool = False

    @classmethod
    def from_file(cls, session_hash: str) -> ReviewState:
        """Load state from disk. Fresh state if the file is missing.

        A corrupt state file raises — resetting silently would drop a
        pending review round.
        """
        state_file = get_state_file(session_hash)
        obj = cls(session_hash=session_hash)
        if not state_file.exists():
            return obj

        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            fields = _validated_fields(data)
            timestamp_str = data.get("timestamp")
            if timestamp_str is not None and not isinstance(timestamp_str, str):
                raise ValueError("'timestamp' must be a string")
            saved_at = datetime.fromisoformat(timestamp_str) if timestamp_str else None
        except ValueError as e:  # JSONDecodeError is a ValueError
            raise _quarantine(state_file, e) from e

        for name, value in fields.items():
            setattr(obj, name, value)

        if saved_at is not None:
            age_seconds = (datetime.now() - saved_at).total_seconds()
            if age_seconds > STATE_EXPIRY:
                log(
                    f"State stale ({age_seconds:.0f}s > {STATE_EXPIRY}s) - preserving position"
                )
                obj.is_stale = True

        return obj

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
        elif self.is_stale and self.subagent_pending:
            # A pending round's edits were baselined at dispatch: keep the
            # round so it is consumed (or surfaced as UNREVIEWED), not dropped.
            log("Stale state with a pending round - keeping the round")
            self.session_id = session_key
        elif self.is_stale:
            # Stale — preserve diff baseline, reset review counters
            log("Stale state - preserving diff, resetting review counters")
            self.session_id = session_key
            self._reset_review_counters()
        else:
            # Continuing session
            self.session_id = session_key

    def start_turn(self) -> None:
        """Reset the per-turn loop bounds at the first stop of a user turn.

        auto_continue_count / fail_count / completed bound CONSECUTIVE
        hook-forced continuations. Carried across turns they would disable
        review for the rest of the session once exhausted.
        """
        self.auto_continue_count = 0
        self.fail_count = 0
        self.completed = False

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
        self.review_agents = []
        self.review_diff_chars = 0
        self.review_file_count = 0

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
            "review_agents": self.review_agents,
            "review_diff_chars": self.review_diff_chars,
            "review_file_count": self.review_file_count,
        }
        # Atomic replace: a killed hook must not leave a torn state file.
        # Errors propagate: a failed state write must not pass silently.
        state_file = get_state_file(self.session_hash)
        tmp = state_file.with_suffix(f".tmp{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            for attempt in range(SAVE_REPLACE_ATTEMPTS):
                try:
                    os.replace(tmp, state_file)
                    break
                except PermissionError:
                    # Windows: another process (e.g. a scavenger) has it open
                    if attempt == SAVE_REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(RETRY_INITIAL_DELAY * (attempt + 1))
        finally:
            tmp.unlink(missing_ok=True)


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
