"""Stale file cleanup, log rotation, and abandoned review scavenging."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from stop_design_audit import config
from stop_design_audit.config import (
    MAX_DEBUG_LOG_BYTES,
    MAX_METRICS_LINES,
    STALE_FILE_CLEANUP_AGE,
)
from stop_design_audit.exit_helpers import log


def cleanup_stale_files() -> None:
    """Remove stale state/results files and rotate logs."""
    now = time.time()

    try:
        for pattern in (
            "stop-hook-state-*.json",
            "review-results-*.json",
            "review-prompt-*.md",
            "scavenged-*.marker",
            "coordinator-instructions-*.json",
        ):
            for f in config.STATE_DIR.glob(pattern):
                try:
                    if now - f.stat().st_mtime > STALE_FILE_CLEANUP_AGE:
                        f.unlink()
                        log(f"Cleaned up stale file: {f.name}")
                except OSError as e:
                    log(f"WARNING: could not remove stale file {f.name}: {e}")

        # Rotate metrics file
        if config.METRICS_FILE.exists():
            try:
                size = config.METRICS_FILE.stat().st_size
                if size > 0:
                    lines = config.METRICS_FILE.read_text(encoding="utf-8").splitlines()
                    if len(lines) > MAX_METRICS_LINES:
                        kept = lines[-MAX_METRICS_LINES:]
                        config.METRICS_FILE.write_text(
                            "\n".join(kept) + "\n", encoding="utf-8"
                        )
                        log(f"Rotated metrics: {len(lines)} -> {len(kept)} lines")
            except OSError as e:
                log(f"WARNING: metrics rotation failed: {e}")

        # Truncate debug log if too large
        if config.DEBUG_FILE.exists():
            try:
                if config.DEBUG_FILE.stat().st_size > MAX_DEBUG_LOG_BYTES:
                    content = config.DEBUG_FILE.read_bytes()
                    truncated = content[-(MAX_DEBUG_LOG_BYTES // 2) :]
                    nl = truncated.find(b"\n")
                    if nl != -1:
                        truncated = truncated[nl + 1 :]
                    config.DEBUG_FILE.write_bytes(b"[...truncated...]\n" + truncated)
                    log("Truncated debug log")
            except OSError as e:
                log(f"WARNING: debug log truncation failed: {e}")

    except OSError as e:
        log(f"WARNING: stale-file cleanup failed: {e}")


def _scavenge_subagent_outcome(
    session_hash: str, round_id: str, agent_ids: list[str]
) -> str:
    """Outcome of an uncollected subagent round, from its results files."""
    from stop_design_audit.results import ResultsError, read_reviewer_result

    statuses = []
    for agent_id in agent_ids:
        try:
            result = read_reviewer_result(session_hash, round_id, agent_id)
        except ResultsError:
            return "invalid_results"
        if result is None:
            return "abandoned"
        statuses.append(result["status"])
    if not statuses:
        return "abandoned"
    return "scavenged_fail" if "fail" in statuses else "scavenged_pass"


def _scavenge_marker(session_hash: str, round_id: str) -> Path:
    return config.STATE_DIR / f"scavenged-{session_hash}-{round_id}.marker"


def scavenge_abandoned_reviews(current_session_hash: str) -> None:
    """Record metrics for review rounds other sessions left pending.

    READ-ONLY with respect to other sessions' state: their files are never
    modified (all worktrees share STATE_DIR, so the owner may still be live).
    A round is considered abandoned once its state is older than
    STATE_EXPIRY; a marker file prevents scoring it twice. If the owner
    returns later, it still consumes the round itself.
    """
    from stop_design_audit.metrics import log_review_metrics
    from stop_design_audit.results import read_review_results

    for state_file in config.STATE_DIR.glob("stop-hook-state-*.json"):
        file_hash = state_file.stem.replace("stop-hook-state-", "")
        if file_hash == current_session_hash:
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # Possibly mid-write by its owner; the owner reports corruption.
            log(f"WARNING: scavenger skipped unreadable {state_file.name}: {e}")
            continue
        if not isinstance(data, dict):
            log(f"WARNING: scavenger skipped non-object {state_file.name}")
            continue
        if not (data.get("subagent_pending") or data.get("delegated_pending")):
            continue
        round_id = data.get("round_id", "")
        if not round_id or _scavenge_marker(file_hash, round_id).exists():
            continue
        try:
            age = (
                datetime.now() - datetime.fromisoformat(data["timestamp"])
            ).total_seconds()
        except (KeyError, TypeError, ValueError):
            log(f"WARNING: scavenger skipped {state_file.name}: no valid timestamp")
            continue
        if age < config.STATE_EXPIRY:
            continue

        outcome = "abandoned"
        if data.get("subagent_pending"):
            outcome = _scavenge_subagent_outcome(
                file_hash, round_id, data.get("review_agents", [])
            )
        else:
            session_id = data.get("session_id", "")
            if session_id and Path(session_id).exists():
                results = read_review_results(session_id, file_hash, mode="inline")
                if results.get("round_id") == round_id:
                    has_failures = any(
                        isinstance(d, dict) and d.get("status") == "fail"
                        for d in results.get("agents", {}).values()
                    )
                    outcome = "scavenged_fail" if has_failures else "scavenged_pass"

        log_review_metrics(
            tier=data.get("tier", "unknown"),
            diff_chars=data.get("review_diff_chars", 0),
            file_count=data.get("review_file_count", 0),
            agents=data.get("review_agents", []),
            outcome=outcome,
            fail_count=data.get("fail_count", 0),
            session_id=file_hash,
        )
        _scavenge_marker(file_hash, round_id).touch()
        log(f"Scavenged round {round_id} from {file_hash}: {outcome}")
