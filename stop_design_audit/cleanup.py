"""Stale file cleanup, log rotation, and abandoned review scavenging."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from stop_design_audit.config import (
    DEBUG_FILE,
    MAX_DEBUG_LOG_BYTES,
    MAX_METRICS_LINES,
    METRICS_FILE,
    STALE_FILE_CLEANUP_AGE,
    STATE_DIR,
)
from stop_design_audit.exit_helpers import log


def cleanup_stale_files() -> None:
    """Remove stale state/results files and rotate logs."""
    now = time.time()

    try:
        for pattern in (
            "stop-hook-state-*.json",
            "review-results-*.json",
            "coordinator-instructions-*.json",
            "subagent-instructions-*.json",
        ):
            for f in STATE_DIR.glob(pattern):
                try:
                    if now - f.stat().st_mtime > STALE_FILE_CLEANUP_AGE:
                        f.unlink()
                        log(f"Cleaned up stale file: {f.name}")
                except Exception:
                    pass

        # Rotate metrics file
        if METRICS_FILE.exists():
            try:
                size = METRICS_FILE.stat().st_size
                if size > 0:
                    lines = METRICS_FILE.read_text(encoding="utf-8").splitlines()
                    if len(lines) > MAX_METRICS_LINES:
                        kept = lines[-MAX_METRICS_LINES:]
                        METRICS_FILE.write_text(
                            "\n".join(kept) + "\n", encoding="utf-8"
                        )
                        log(f"Rotated metrics: {len(lines)} -> {len(kept)} lines")
            except Exception:
                pass

        # Truncate debug log if too large
        if DEBUG_FILE.exists():
            try:
                if DEBUG_FILE.stat().st_size > MAX_DEBUG_LOG_BYTES:
                    content = DEBUG_FILE.read_bytes()
                    truncated = content[-(MAX_DEBUG_LOG_BYTES // 2) :]
                    nl = truncated.find(b"\n")
                    if nl != -1:
                        truncated = truncated[nl + 1 :]
                    DEBUG_FILE.write_bytes(b"[...truncated...]\n" + truncated)
                    log("Truncated debug log")
            except Exception:
                pass

    except Exception:
        pass  # Never let cleanup break the hook


def scavenge_abandoned_reviews(current_session_hash: str) -> None:
    """Scan state files from OTHER sessions for abandoned pending reviews.

    When a conversation ends before the hook collects subagent/delegated results,
    the metric is lost. This function retroactively logs those as "abandoned".

    Called once per hook invocation, after cleanup_stale_files().
    Only processes state files from sessions other than the current one.
    """
    from stop_design_audit.metrics import log_review_metrics
    from stop_design_audit.results import read_review_results

    try:
        for state_file in STATE_DIR.glob("stop-hook-state-*.json"):
            try:
                # Extract session hash from filename
                # Format: stop-hook-state-{hash}.json
                fname = state_file.stem  # stop-hook-state-abc123
                file_hash = fname.replace("stop-hook-state-", "")

                # Skip current session
                if file_hash == current_session_hash:
                    continue

                content = state_file.read_text(encoding="utf-8")
                if not content.strip():
                    continue
                data = json.loads(content)

                # Only interested in pending reviews
                is_pending = data.get("subagent_pending") or data.get(
                    "delegated_pending"
                )
                if not is_pending:
                    continue

                # Check if this state is old enough to be considered abandoned
                # (at least 10 minutes — gives the background agent time to finish)
                timestamp_str = data.get("timestamp", "")
                if timestamp_str:
                    try:
                        ts = datetime.fromisoformat(timestamp_str)
                        age_seconds = (datetime.now() - ts).total_seconds()
                        if age_seconds < 600:  # 10 minutes
                            continue
                    except (ValueError, TypeError):
                        pass

                round_id = data.get("round_id", "")
                tier = data.get("tier", "unknown")
                session_id = data.get("session_id", "")
                fail_count = data.get("fail_count", 0)

                if not round_id:
                    continue

                # Try to extract results from the transcript
                outcome = "abandoned"
                if session_id and Path(session_id).exists():
                    results = read_review_results(session_id, file_hash, mode="inline")
                    if results.get("round_id") == round_id:
                        # Results were there but never collected!
                        agents = results.get("agents", {})
                        has_failures = any(
                            isinstance(d, dict) and d.get("status") == "fail"
                            for d in agents.values()
                        )
                        outcome = "scavenged_fail" if has_failures else "scavenged_pass"
                        log(
                            f"Scavenged results for round {round_id} from {file_hash}: {outcome}"
                        )

                # Log the metric
                log_review_metrics(
                    tier=tier,
                    diff_chars=0,  # Not available from state file
                    file_count=0,
                    agents=[],
                    outcome=outcome,
                    fail_count=fail_count,
                    session_id=file_hash,
                )
                log(
                    f"Scavenged abandoned review: round={round_id}, tier={tier}, outcome={outcome}"
                )

                # Mark as no longer pending so we don't scavenge again
                data["subagent_pending"] = False
                data["delegated_pending"] = False
                data["completed"] = True
                state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

            except Exception as e:
                log(f"Scavenger error on {state_file.name}: {e}")
                continue

    except Exception:
        pass  # Never let scavenger break the hook
