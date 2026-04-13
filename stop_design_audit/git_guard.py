"""Git working tree guard — detect and recover from agent-caused stashes."""

from __future__ import annotations

import subprocess

from stop_design_audit.exit_helpers import log
from stop_design_audit.state import ReviewState


def get_stash_count() -> int:
    """Return the number of git stash entries, or -1 on error."""
    try:
        result = subprocess.run(
            ["git", "stash", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return -1
        lines = [line for line in result.stdout.strip().splitlines() if line]
        return len(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return -1


def check_and_recover_stash(state: ReviewState) -> None:
    """Compare current stash count to stored count. Pop if an agent stashed.

    Called early in main() on each hook invocation to detect stashes
    created by review agents between invocations.
    """
    current_count = get_stash_count()
    if current_count == -1:
        return

    if state.stash_count_at_dispatch == -1:
        state.stash_count_at_dispatch = current_count
        return

    if current_count <= state.stash_count_at_dispatch:
        state.stash_count_at_dispatch = current_count
        return

    new_stashes = current_count - state.stash_count_at_dispatch
    log(
        f"WARNING: Git stash count increased by {new_stashes} "
        f"({state.stash_count_at_dispatch} -> {current_count}). "
        f"A review agent likely ran 'git stash'. Attempting recovery..."
    )

    try:
        result = subprocess.run(
            ["git", "stash", "pop"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            log("Stash recovery successful: git stash pop succeeded")
        else:
            log(
                f"Stash recovery failed (merge conflict?): {result.stderr.strip()}. "
                f"Manual recovery needed: run 'git stash pop' or 'git stash drop'"
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f"Stash recovery error: {e}. Manual recovery needed: run 'git stash pop'")

    state.stash_count_at_dispatch = get_stash_count()
