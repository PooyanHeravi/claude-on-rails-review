"""Review metrics logging (JSONL format)."""

from __future__ import annotations

import json
from datetime import datetime

from stop_design_audit import __version__, config


def log_review_metrics(
    tier: str,
    diff_chars: int,
    file_count: int,
    agents: list[str],
    outcome: str,
    fail_count: int,
    session_id: str,
) -> None:
    """Append review metrics to JSONL file for analysis.

    Every record carries the hook version and config fingerprint
    (provenance). Write errors propagate to the entry point.
    """
    metric = {
        "timestamp": datetime.now().isoformat(),
        "version": __version__,
        "config_fingerprint": config.config_fingerprint(),
        "tier": tier,
        "diff_chars": diff_chars,
        "file_count": file_count,
        "agents": agents,
        "outcome": outcome,
        "fail_count": fail_count,
        "session_id": session_id[:8] if len(session_id) >= 8 else session_id,
    }
    with open(config.METRICS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(metric) + "\n")
