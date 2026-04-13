"""Entry point for `python -m stop_design_audit`."""

from __future__ import annotations

from pathlib import Path


def run(hooks_dir: Path | None = None) -> None:
    """Initialize paths and run the main hook logic."""
    from stop_design_audit.config import init_paths

    if hooks_dir is None:
        # When run as `python -m stop_design_audit`, default to CWD
        hooks_dir = Path.cwd()
    init_paths(hooks_dir)

    from stop_design_audit.main import main

    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        from stop_design_audit.exit_helpers import allow_stop, log

        log(f"FATAL: {e}")
        allow_stop(f"Fatal error: {e}")


if __name__ == "__main__":
    run()
