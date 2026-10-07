"""Entry point for `python -m stop_design_audit`."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run(hooks_dir: Path | None = None) -> None:
    """Initialize paths and config, then run the main hook logic.

    Any error — bad input, bad config, or a bug — exits loudly through
    exit_helpers.fail_loud, never via a silent allow.
    """
    from stop_design_audit import config, exit_helpers

    if hooks_dir is None:
        # When run as `python -m stop_design_audit`, default to CWD
        hooks_dir = Path.cwd()

    try:
        config.init_paths(hooks_dir)
        input_data = json.loads(sys.stdin.read(), strict=False)
        if not isinstance(input_data, dict):
            raise ValueError("hook input is not a JSON object")
        exit_helpers.STOP_HOOK_ACTIVE = bool(input_data.get("stop_hook_active"))

        config.validate_env()
        config.load_overrides()

        # Import after overrides are applied: modules read config at import.
        from stop_design_audit.agents import load_agent_definitions

        load_agent_definitions()

        from stop_design_audit.main import main

        main(input_data)
    except SystemExit:
        raise
    except config.ConfigError as e:
        exit_helpers.fail_loud("invalid config: " + "; ".join(e.errors))
    except Exception as e:
        exit_helpers.fail_loud(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    run()
