"""All configuration constants, env var resolution, and overlay loading."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

# =============================================================================
# File Paths (set by __main__.py before main() runs)
# =============================================================================
STATE_DIR: Path = Path(__file__).parent.parent  # default; overridden at startup
DEBUG_FILE: Path = STATE_DIR / "stop-hook-debug.log"
METRICS_FILE: Path = STATE_DIR / "stop-hook-metrics.jsonl"


def init_paths(hooks_dir: Path) -> None:
    """Set STATE_DIR and derived paths, then load overrides. Called once from __main__.py."""
    global STATE_DIR, DEBUG_FILE, METRICS_FILE
    STATE_DIR = hooks_dir
    DEBUG_FILE = STATE_DIR / "stop-hook-debug.log"
    METRICS_FILE = STATE_DIR / "stop-hook-metrics.jsonl"
    load_overrides()


# =============================================================================
# Internal Constants
# =============================================================================
SESSION_HASH_LENGTH = 12
ROUND_ID_LENGTH = 8
MAX_PREVIEW_CHARS = 500
MAX_FILES_IN_PROMPT = 20
LOG_TRUNCATE_LENGTH = 40
RETRY_INITIAL_DELAY = 0.1
RETRY_BACKOFF_FACTOR = 2
MAX_TRANSCRIPT_CHARS_FOR_API = 50000
STALE_FILE_CLEANUP_AGE = 86400  # 24 hours
MAX_METRICS_LINES = 5000
MAX_DEBUG_LOG_BYTES = 1_000_000  # 1 MB
MAX_VIOLATION_FILES = 5
STATUS_PASS = "pass"
STATUS_FAIL = "fail"

# =============================================================================
# Session-Specific File Helpers
# =============================================================================


def get_session_hash(transcript_path: str) -> str:
    """Generate a short hash from transcript path for session-specific files."""
    return hashlib.md5(transcript_path.encode()).hexdigest()[:SESSION_HASH_LENGTH]


def get_state_file(session_hash: str) -> Path:
    """Get session-specific state file path."""
    return STATE_DIR / f"stop-hook-state-{session_hash}.json"


def get_results_file(session_hash: str) -> Path:
    """Get session-specific results file path (for file mode)."""
    return STATE_DIR / f"review-results-{session_hash}.json"


def get_coordinator_instructions_file(session_hash: str) -> Path:
    """Get session-specific coordinator instructions file path (for delegated mode)."""
    return STATE_DIR / f"coordinator-instructions-{session_hash}.json"


# =============================================================================
# Review Mode
# =============================================================================
REVIEW_MODE = "subagent"  # "agent" | "delegated" | "api" | "subagent"
RESULTS_MODE = "inline"  # "inline" | "file"

# =============================================================================
# File Filtering
# =============================================================================
EXCLUDED_EXTENSIONS = {
    ".json",
    ".md",
    ".txt",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".lock",
    ".sum",
}

EXCLUDED_FILENAMES = {
    "LICENSE",
    "LICENCE",
    "Makefile",
    "Dockerfile",
    "Procfile",
    "Gemfile",
    "Rakefile",
    "Vagrantfile",
    "Brewfile",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".editorconfig",
}

EXCLUDED_PATHS = [
    "/tests/fixtures/",
    "/test/fixtures/",
    "/__pycache__/",
    "/.pytest_cache/",
    "/node_modules/",
    "/.venv/",
    "/venv/",
    "/build/",
    "/dist/",
    "/.git/",
    "/coverage/",
    "/.coverage",
    "/htmlcov/",
]

# =============================================================================
# Tier Thresholds
# =============================================================================
TIER_THRESHOLDS = {
    "skip": 500,
    "quick": 5000,
    "standard": 20000,
}

TIER_FILE_LIMITS = {
    "skip": 1,
    "quick": 3,
    "standard": 6,
}

# =============================================================================
# Auto-Continue Settings
# =============================================================================
MAX_AUTO_CONTINUES = 3
MAX_FAIL_RETRIES = 3
MAX_REVIEW_ATTEMPTS = 2
STATE_EXPIRY = 3600  # 1 hour
DELEGATED_TIMEOUT = 300  # 5 minutes
SUBAGENT_TIMEOUT = 300  # 5 minutes

# =============================================================================
# Environment Variable Names
# =============================================================================
FORCE_TIER_ENV = "CLAUDE_HOOK_FORCE_TIER"
SKIP_HOOK_ENV = "CLAUDE_HOOK_SKIP"
DEEP_AUTO_FIX_ENV = "CLAUDE_HOOK_DEEP_AUTO_FIX"
REVIEW_MODE_ENV = "CLAUDE_HOOK_REVIEW_MODE"

# =============================================================================
# Deep Review Auto-Fix
# =============================================================================
DEEP_AUTO_FIX = "high"  # "none" | "critical" | "high" | "medium" | "all"

# =============================================================================
# Critical Patterns & Agent IDs
# =============================================================================
CRITICAL_PATTERNS: list[str] = []

AGENT_IDS: dict[str, list[str]] = {
    "quick": ["explore_haiku"],
    "standard": ["explore_haiku", "general_haiku", "bug_hunter", "integration_checker"],
    "deep": ["explore_haiku", "general_haiku", "bug_hunter", "integration_checker", "general_opus"],
}

# =============================================================================
# API Mode Settings
# =============================================================================
API_DIFF_THRESHOLD = 500

# Severity hierarchy for deep auto-fix threshold
SEVERITY_ORDER = ["critical", "high", "medium", "low"]


# =============================================================================
# Presets
# =============================================================================
PRESETS: dict[str, dict] = {
    "strict": {
        "tier_thresholds": {"skip": 0, "quick": 500, "standard": 3000},
        "tier_file_limits": {"skip": 0, "quick": 1, "standard": 3},
        "max_auto_continues": 1,
        "deep_auto_fix": "none",
    },
    "balanced": {},  # Current hardcoded defaults — no overrides needed
    "relaxed": {
        "tier_thresholds": {"skip": 1000, "quick": 5000, "standard": 20000},
        "tier_file_limits": {"skip": 3, "quick": 5, "standard": 10},
        "max_auto_continues": 5,
        "deep_auto_fix": "high",
    },
    "minimal": {
        "tier_thresholds": {"skip": 3000, "quick": 10000, "standard": 50000},
        "tier_file_limits": {"skip": 5, "quick": 10, "standard": 20},
        "max_auto_continues": 10,
        "deep_auto_fix": "all",
        "agent_ids": {
            "quick": ["explore_haiku"],
            "standard": ["explore_haiku"],
            "deep": ["explore_haiku", "general_haiku"],
        },
    },
}

# Maps JSON key → expected Python type (for validation and set conversion)
_CONFIG_KEYS: dict[str, type] = {
    "review_mode": str,
    "results_mode": str,
    "deep_auto_fix": str,
    "tier_thresholds": dict,
    "tier_file_limits": dict,
    "max_auto_continues": int,
    "max_fail_retries": int,
    "max_review_attempts": int,
    "state_expiry": int,
    "subagent_timeout": int,
    "delegated_timeout": int,
    "excluded_extensions": set,
    "excluded_filenames": set,
    "excluded_paths": list,
    "critical_patterns": list,
    "agent_ids": dict,
    "api_diff_threshold": int,
}

# Keys that are metadata, not config values
_META_KEYS = {"preset", "extra_agent_definitions", "_doc", "_comment"}


def _log_override(msg: str) -> None:
    """Log during override loading. Cannot import exit_helpers (circular)."""
    try:
        with open(DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] OVERRIDE: {msg}\n")
    except Exception:
        pass


def _apply_config(overrides: dict) -> None:
    """Apply a dict of config overrides to module-level globals.

    Handles type conversion (JSON array → set), per-tier dict merge for agent_ids,
    and +prefixed keys for append semantics on list/set types.
    """
    global REVIEW_MODE, RESULTS_MODE, DEEP_AUTO_FIX
    global TIER_THRESHOLDS, TIER_FILE_LIMITS
    global MAX_AUTO_CONTINUES, MAX_FAIL_RETRIES, MAX_REVIEW_ATTEMPTS
    global STATE_EXPIRY, SUBAGENT_TIMEOUT, DELEGATED_TIMEOUT
    global EXCLUDED_EXTENSIONS, EXCLUDED_FILENAMES, EXCLUDED_PATHS
    global CRITICAL_PATTERNS, AGENT_IDS, API_DIFF_THRESHOLD

    for key, value in overrides.items():
        # Skip meta keys and +-prefixed keys (handled below)
        if key in _META_KEYS or key.startswith("+"):
            continue

        if key not in _CONFIG_KEYS:
            continue  # Unknown keys warned separately

        expected_type = _CONFIG_KEYS[key]

        # agent_ids: merge per-tier, don't replace entire dict
        if key == "agent_ids":
            for tier, agents in value.items():
                AGENT_IDS[tier] = agents
            continue

        # Set types: convert JSON array to set
        if expected_type is set:
            if not isinstance(value, list):
                _log_override(
                    f"Skipping {key}: expected list, got {type(value).__name__}"
                )
                continue
            value = set(value)

        # Int types: validate
        if expected_type is int and not isinstance(value, int):
            _log_override(f"Skipping {key}: expected int, got {type(value).__name__}")
            continue

        # Apply to the matching global
        if key == "review_mode":
            REVIEW_MODE = value
        elif key == "results_mode":
            RESULTS_MODE = value
        elif key == "deep_auto_fix":
            DEEP_AUTO_FIX = value
        elif key == "tier_thresholds":
            TIER_THRESHOLDS = value
        elif key == "tier_file_limits":
            TIER_FILE_LIMITS = value
        elif key == "max_auto_continues":
            MAX_AUTO_CONTINUES = value
        elif key == "max_fail_retries":
            MAX_FAIL_RETRIES = value
        elif key == "max_review_attempts":
            MAX_REVIEW_ATTEMPTS = value
        elif key == "state_expiry":
            STATE_EXPIRY = value
        elif key == "subagent_timeout":
            SUBAGENT_TIMEOUT = value
        elif key == "delegated_timeout":
            DELEGATED_TIMEOUT = value
        elif key == "excluded_extensions":
            EXCLUDED_EXTENSIONS = value
        elif key == "excluded_filenames":
            EXCLUDED_FILENAMES = value
        elif key == "excluded_paths":
            EXCLUDED_PATHS = value
        elif key == "critical_patterns":
            CRITICAL_PATTERNS = value
        elif key == "api_diff_threshold":
            API_DIFF_THRESHOLD = value

    # Handle +-prefixed keys (append semantics)
    for key, value in overrides.items():
        if not key.startswith("+"):
            continue
        base_key = key[1:]
        if base_key not in _CONFIG_KEYS:
            _log_override(f"Unknown append key: {key}")
            continue
        if not isinstance(value, list):
            _log_override(f"Skipping {key}: expected list, got {type(value).__name__}")
            continue

        expected_type = _CONFIG_KEYS[base_key]
        if expected_type is set:
            # Append to set
            if base_key == "excluded_extensions":
                EXCLUDED_EXTENSIONS = EXCLUDED_EXTENSIONS | set(value)
            elif base_key == "excluded_filenames":
                EXCLUDED_FILENAMES = EXCLUDED_FILENAMES | set(value)
        elif expected_type is list:
            # Append to list (deduplicate)
            if base_key == "excluded_paths":
                EXCLUDED_PATHS.extend(v for v in value if v not in EXCLUDED_PATHS)
            elif base_key == "critical_patterns":
                CRITICAL_PATTERNS.extend(v for v in value if v not in CRITICAL_PATTERNS)


# =============================================================================
# Config Overlay (hook-overrides.json)
# =============================================================================
def load_overrides() -> None:
    """Load hook-overrides.json from STATE_DIR if it exists, merging into config.

    Merge order: hardcoded defaults → preset → explicit overrides → env vars (downstream).
    """
    override_path = STATE_DIR / "hook-overrides.json"
    if not override_path.exists():
        return
    try:
        overrides = json.loads(override_path.read_text(encoding="utf-8"))
    except Exception as e:
        _log_override(f"Failed to parse hook-overrides.json: {e}")
        return

    # Step 1: Apply preset if specified
    preset_name = overrides.get("preset")
    if preset_name:
        if preset_name not in PRESETS:
            _log_override(f"Unknown preset '{preset_name}', ignoring")
        else:
            _log_override(f"Applying preset: {preset_name}")
            _apply_config(PRESETS[preset_name])

    # Step 2: Apply explicit overrides (preset values are the base, these win)
    _apply_config(overrides)

    # Step 3: Warn on unknown keys
    known = _META_KEYS | set(_CONFIG_KEYS.keys())
    # Also allow +-prefixed variants of list/set keys
    known |= {f"+{k}" for k, t in _CONFIG_KEYS.items() if t in (list, set)}
    unknown = set(overrides.keys()) - known
    for key in unknown:
        _log_override(f"Unknown key in hook-overrides.json: '{key}'")

    # Extra agent definitions are loaded in agents.py
