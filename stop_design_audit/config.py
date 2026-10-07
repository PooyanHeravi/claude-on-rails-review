"""All configuration constants, env var resolution, and overlay loading.

Config errors are fatal: ``load_overrides`` and ``validate_env`` raise
``ConfigError`` listing every problem found, and the entry point surfaces it
loudly instead of running with a half-applied config.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from stop_design_audit import __version__


class ConfigError(Exception):
    """Invalid hook configuration (overrides file, env vars, or review config)."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


# =============================================================================
# File Paths (set by __main__.py before main() runs)
#
# CONFIG_DIR holds user-authored config (hook-overrides.json, checklist files).
# STATE_DIR holds runtime artifacts (state, payloads, results, logs, metrics)
# and is the only directory the hook writes to.
# =============================================================================
CONFIG_DIR: Path = Path(__file__).parent.parent  # default; overridden at startup
STATE_DIR: Path = CONFIG_DIR / "state"
DEBUG_FILE: Path = STATE_DIR / "stop-hook-debug.log"
METRICS_FILE: Path = STATE_DIR / "stop-hook-metrics.jsonl"


def init_paths(hooks_dir: Path) -> None:
    """Set CONFIG_DIR/STATE_DIR and derived paths. Called once from __main__.py."""
    global CONFIG_DIR, STATE_DIR, DEBUG_FILE, METRICS_FILE
    CONFIG_DIR = hooks_dir
    STATE_DIR = hooks_dir / "state"
    STATE_DIR.mkdir(exist_ok=True)
    DEBUG_FILE = STATE_DIR / "stop-hook-debug.log"
    METRICS_FILE = STATE_DIR / "stop-hook-metrics.jsonl"


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


def get_results_file(session_hash: str, round_id: str, agent_id: str) -> Path:
    """Results file for one reviewer in one round (written by the reviewer)."""
    return STATE_DIR / f"review-results-{session_hash}-{round_id}-{agent_id}.json"


def get_agent_mode_results_file(session_hash: str) -> Path:
    """Results file for agent mode with results_mode='file' (all agents, one file)."""
    return STATE_DIR / f"review-results-{session_hash}.json"


def get_reviewer_prompt_file(session_hash: str, round_id: str, agent_id: str) -> Path:
    """Prompt file for one reviewer in one round (written by the hook)."""
    return STATE_DIR / f"review-prompt-{session_hash}-{round_id}-{agent_id}.md"


def get_coordinator_instructions_file(session_hash: str) -> Path:
    """Get session-specific coordinator instructions file path (for delegated mode)."""
    return STATE_DIR / f"coordinator-instructions-{session_hash}.json"


# =============================================================================
# Review Mode
# =============================================================================
REVIEW_MODES = ("agent", "delegated", "api", "subagent")
RESULTS_MODES = ("inline", "file")
REVIEW_MODE = "subagent"
RESULTS_MODE = "inline"  # agent/delegated modes only; subagent mode always uses files

# =============================================================================
# Reviewer
# =============================================================================
REVIEWER_MODEL = "opus"
FIXER_MODEL = "sonnet"
# Path (relative to CONFIG_DIR, or absolute) of a project review checklist.
# Empty = the package's built-in generic checklist.
REVIEW_CHECKLIST_FILE = ""

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
TIERS = ("skip", "quick", "standard", "deep")
THRESHOLD_TIERS = ("skip", "quick", "standard")
REVIEW_TIERS = ("quick", "standard", "deep")

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
# Auto-Continue Settings (loop bounds for a blocking Stop hook)
# =============================================================================
MAX_AUTO_CONTINUES = 3
MAX_FAIL_RETRIES = 3
MAX_REVIEW_ATTEMPTS = 2
STATE_EXPIRY = 3600  # 1 hour
DELEGATED_TIMEOUT = 300  # 5 minutes

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
SEVERITY_ORDER = ["critical", "high", "medium", "low"]
AUTO_FIX_LEVELS = ("none", "critical", "high", "medium", "all")
DEEP_AUTO_FIX = "high"

# =============================================================================
# Critical Patterns & Agent IDs
# =============================================================================
CRITICAL_PATTERNS: list[str] = []

# One reviewer per tier by default. The tier still scales the reviewer's
# effort and the payload, not the number of agents.
AGENT_IDS: dict[str, list[str]] = {
    "quick": ["reviewer"],
    "standard": ["reviewer"],
    "deep": ["reviewer"],
}

# =============================================================================
# API Mode Settings
# =============================================================================
API_DIFF_THRESHOLD = 500


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
    "balanced": {},  # Current defaults — no overrides needed
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
        "reviewer_model": "sonnet",
    },
}

# Maps JSON key → expected Python type. Each key's module global is its
# upper-cased name (review_mode → REVIEW_MODE).
_CONFIG_KEYS: dict[str, type] = {
    "review_mode": str,
    "results_mode": str,
    "deep_auto_fix": str,
    "reviewer_model": str,
    "fixer_model": str,
    "review_checklist_file": str,
    "tier_thresholds": dict,
    "tier_file_limits": dict,
    "max_auto_continues": int,
    "max_fail_retries": int,
    "max_review_attempts": int,
    "state_expiry": int,
    "delegated_timeout": int,
    "excluded_extensions": set,
    "excluded_filenames": set,
    "excluded_paths": list,
    "critical_patterns": list,
    "agent_ids": dict,
    "api_diff_threshold": int,
}

# Integer keys where 0 is meaningful; every other integer must be >= 1
# (0 loop bounds / expiries / timeouts would skip or abandon every review).
_ZERO_ALLOWED_INT_KEYS = {"api_diff_threshold"}

# Allowed values for enumerated string keys.
_ENUM_KEYS: dict[str, tuple[str, ...]] = {
    "review_mode": REVIEW_MODES,
    "results_mode": RESULTS_MODES,
    "deep_auto_fix": AUTO_FIX_LEVELS,
}

# Keys that are metadata, not config values
_META_KEYS = {"preset", "extra_agent_definitions", "_doc", "_comment"}

OVERRIDES_FILENAME = "hook-overrides.json"


def _validate_value(key: str, value: object) -> list[str]:
    """Return a list of errors for one config key/value pair."""
    expected = _CONFIG_KEYS[key]
    if expected is set or expected is list:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return [f"'{key}' must be a list of strings"]
        return []
    if expected is int:
        # bool is an int subclass; reject it explicitly. Loop bounds of 0
        # would make every round UNREVIEWED or skip review entirely.
        minimum = 0 if key in _ZERO_ALLOWED_INT_KEYS else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            return [f"'{key}' must be an integer >= {minimum}, got {value!r}"]
        return []
    if expected is str:
        if not isinstance(value, str):
            return [f"'{key}' must be a string, got {type(value).__name__}"]
        if key in _ENUM_KEYS and value not in _ENUM_KEYS[key]:
            return [f"'{key}' must be one of {list(_ENUM_KEYS[key])}, got {value!r}"]
        if key in ("reviewer_model", "fixer_model") and not value.strip():
            return [f"'{key}' must not be empty"]
        return []
    # dict keys
    if not isinstance(value, dict):
        return [f"'{key}' must be an object, got {type(value).__name__}"]
    if key in ("tier_thresholds", "tier_file_limits"):
        errors = []
        if set(value) != set(THRESHOLD_TIERS):
            errors.append(
                f"'{key}' must define exactly {list(THRESHOLD_TIERS)}, got {sorted(value)}"
            )
        for tier, n in value.items():
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                errors.append(f"'{key}.{tier}' must be a non-negative integer")
        return errors
    if key == "agent_ids":
        errors = []
        for tier, agents in value.items():
            if tier not in REVIEW_TIERS:
                errors.append(f"'agent_ids' has unknown tier {tier!r}")
            elif (
                not isinstance(agents, list)
                or not agents
                or not all(isinstance(a, str) for a in agents)
            ):
                errors.append(f"'agent_ids.{tier}' must be a non-empty list of strings")
        return errors
    return []


def _apply_config(overrides: dict) -> list[str]:
    """Validate and apply config overrides to module globals. Returns errors.

    Nothing is applied if any key is invalid.
    """
    errors: list[str] = []
    plain: dict[str, object] = {}
    appends: dict[str, list] = {}

    for key, value in overrides.items():
        if key in _META_KEYS:
            continue
        base_key = key[1:] if key.startswith("+") else key
        if base_key not in _CONFIG_KEYS:
            errors.append(f"unknown key '{key}'")
            continue
        if key.startswith("+") and _CONFIG_KEYS[base_key] not in (list, set):
            errors.append(f"'{key}': append syntax only applies to list keys")
            continue
        key_errors = _validate_value(base_key, value)
        if key_errors:
            errors.extend(key_errors)
            continue
        if key.startswith("+"):
            appends[base_key] = value  # type: ignore[assignment]
        else:
            plain[key] = value

    if errors:
        return errors

    g = globals()
    for key, value in plain.items():
        name = key.upper()
        if key == "agent_ids":
            # merge per-tier, don't replace the entire dict
            g[name].update(value)
        elif _CONFIG_KEYS[key] is set:
            g[name] = set(value)  # type: ignore[arg-type]
        elif _CONFIG_KEYS[key] is list:
            g[name] = list(value)  # type: ignore[arg-type]
        else:
            g[name] = value
    for key, value in appends.items():
        name = key.upper()
        if _CONFIG_KEYS[key] is set:
            g[name] = g[name] | set(value)
        else:
            g[name] = g[name] + [v for v in value if v not in g[name]]
    return []


# =============================================================================
# Config Overlay (hook-overrides.json)
# =============================================================================
def read_overrides_file() -> dict:
    """Read hook-overrides.json from CONFIG_DIR. Missing file = {}; invalid = ConfigError."""
    override_path = CONFIG_DIR / OVERRIDES_FILENAME
    if not override_path.exists():
        return {}
    try:
        data = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError([f"{override_path}: cannot parse: {e}"]) from e
    if not isinstance(data, dict):
        raise ConfigError([f"{override_path}: top level must be an object"])
    return data


def load_overrides() -> None:
    """Merge hook-overrides.json into config. Raises ConfigError on any problem.

    Merge order: defaults → preset → explicit overrides → env vars (downstream).
    """
    overrides = read_overrides_file()
    if not overrides:
        return

    errors: list[str] = []
    preset_name = overrides.get("preset")
    if preset_name is not None:
        if preset_name not in PRESETS:
            errors.append(f"unknown preset {preset_name!r} (known: {sorted(PRESETS)})")
        else:
            errors.extend(_apply_config(PRESETS[preset_name]))
    if not errors:
        errors.extend(_apply_config(overrides))

    extra = overrides.get("extra_agent_definitions", {})
    if not isinstance(extra, dict):
        errors.append("'extra_agent_definitions' must be an object")

    if errors:
        raise ConfigError([f"{OVERRIDES_FILENAME}: {e}" for e in errors])

    if REVIEW_CHECKLIST_FILE and not resolve_checklist_path().is_file():
        raise ConfigError(
            [f"review_checklist_file not found: {resolve_checklist_path()}"]
        )


def resolve_checklist_path() -> Path:
    """Absolute path of the configured project checklist file."""
    p = Path(REVIEW_CHECKLIST_FILE)
    return p if p.is_absolute() else CONFIG_DIR / p


def validate_env() -> None:
    """Reject invalid values in the hook's environment variables."""
    errors = []
    checks = {
        REVIEW_MODE_ENV: REVIEW_MODES,
        DEEP_AUTO_FIX_ENV: AUTO_FIX_LEVELS,
        FORCE_TIER_ENV: REVIEW_TIERS,
    }
    for env_name, allowed in checks.items():
        raw = os.environ.get(env_name, "").strip()
        if raw and raw.lower() not in allowed:
            errors.append(f"{env_name}={raw!r} must be one of {list(allowed)}")
    skip = os.environ.get(SKIP_HOOK_ENV, "").strip()
    if skip and skip not in ("0", "1"):
        errors.append(f"{SKIP_HOOK_ENV}={skip!r} must be '0' or '1'")
    if errors:
        raise ConfigError(errors)


def effective_review_mode() -> str:
    return os.environ.get(REVIEW_MODE_ENV, "").strip().lower() or REVIEW_MODE


def effective_deep_auto_fix() -> str:
    return os.environ.get(DEEP_AUTO_FIX_ENV, "").strip().lower() or DEEP_AUTO_FIX


def config_fingerprint() -> str:
    """SHA-256 over the effective config + package version (provenance)."""
    g = globals()
    snapshot = {
        key: sorted(g[key.upper()]) if _CONFIG_KEYS[key] is set else g[key.upper()]
        for key in _CONFIG_KEYS
    }
    snapshot["review_mode"] = effective_review_mode()
    snapshot["deep_auto_fix"] = effective_deep_auto_fix()
    snapshot["force_tier"] = os.environ.get(FORCE_TIER_ENV, "").strip().lower()
    snapshot["version"] = __version__
    snapshot["extra_agent_definitions"] = read_overrides_file().get(
        "extra_agent_definitions", {}
    )
    review_config = CONFIG_DIR.parent / "review-config.json"
    if review_config.exists():
        snapshot["review_config_sha256"] = hashlib.sha256(
            review_config.read_bytes()
        ).hexdigest()
    if REVIEW_CHECKLIST_FILE:
        snapshot["checklist_sha256"] = hashlib.sha256(
            resolve_checklist_path().read_bytes()
        ).hexdigest()
    blob = json.dumps(snapshot, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
