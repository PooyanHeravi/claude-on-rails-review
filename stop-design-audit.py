#!/usr/bin/env python3
"""
Claude Code Stop Hook - Smart Tiered Code Review

A stop hook for Claude Code that triggers automatic code reviews based on
the amount of code changed since the last review. Designed to catch issues
early without interrupting flow for small changes.

## How It Works

1. **Per-Session Tracking**: Each Claude Code session is tracked independently.
   Counters reset when a new session starts (detected via transcript_path).

2. **Incremental Diff**: Only counts changes since the last hook firing,
   not cumulative totals. This means small iterative changes won't trigger
   large reviews.

3. **Tiered Reviews**: Review depth scales with change size:
   - skip:     <500 chars, 1 file      → no review
   - quick:    <3000 chars, ≤3 files   → 1 lightweight agent
   - standard: <15000 chars, ≤6 files  → 3 agents
   - deep:     ≥15000 chars or >6 files → 4 agents (includes senior reviewer)

4. **Integration Checker**: Added when changes span 2+ top-level directories OR
   touch 2+ critical patterns (services/*, proto/*, etc.) to check API contracts,
   service boundaries, and cross-module consistency.

## Configuration

Edit the constants in the "Configuration" section to customize:
- TIER_THRESHOLDS: Character thresholds for each tier
- TIER_FILE_LIMITS: File count limits for each tier
- CRITICAL_PATTERNS: Paths that trigger integration_checker when 2+ matched
- MAX_AUTO_CONTINUES: How many passes before stopping
- EXCLUDED_EXTENSIONS: File types to ignore (.md, .json, etc.)
- EXCLUDED_PATHS: Directories/paths to ignore (fixtures, caches, etc.)

## State Files (created in same directory as this script)

- stop-hook-state.json: Session state (counters, position, files seen)
- stop-hook-debug.log: Debug output for troubleshooting
- review-results.json: Agent results for multi-agent coordination

## Usage

Add to .claude/settings.json:
{
  "hooks": {
    "stop": [{
      "command": "python .claude/hooks/stop-design-audit.py",
      "timeout": 30000
    }]
  }
}

## Hook Protocol

For Stop hooks:
- exit 0 with no output = allow stop
- exit 0 with {"decision": "block", "reason": "..."} = continue with instructions

## License

MIT - Feel free to use and modify for your projects.
"""
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path


def get_session_hash(transcript_path: str) -> str:
    """Generate a short hash from transcript path for session-specific files.

    This allows multiple simultaneous Claude Code sessions in the same project
    to have separate state files, avoiding race conditions.
    """
    return hashlib.md5(transcript_path.encode()).hexdigest()[:SESSION_HASH_LENGTH]

# =============================================================================
# Configuration (v1.1 - incremental tracking fix)
# =============================================================================
# Customize these values for your workflow.

# -----------------------------------------------------------------------------
# File Paths (relative to this script's directory)
# -----------------------------------------------------------------------------
STATE_DIR = Path(__file__).parent
DEBUG_FILE = STATE_DIR / "stop-hook-debug.log"
METRICS_FILE = STATE_DIR / "stop-hook-metrics.jsonl"

# -----------------------------------------------------------------------------
# Internal Constants
# -----------------------------------------------------------------------------
SESSION_HASH_LENGTH = 12
ROUND_ID_LENGTH = 8
MAX_PREVIEW_CHARS = 300
MAX_FILES_IN_PROMPT = 20
LOG_TRUNCATE_LENGTH = 40
RETRY_INITIAL_DELAY = 0.1
RETRY_BACKOFF_FACTOR = 2
MAX_TRANSCRIPT_CHARS_FOR_API = 50000
MAX_VIOLATION_FILES = 5
STATUS_PASS = "pass"
STATUS_FAIL = "fail"


def get_state_file(session_hash: str) -> Path:
    """Get session-specific state file path."""
    return STATE_DIR / f"stop-hook-state-{session_hash}.json"


def get_results_file(session_hash: str) -> Path:
    """Get session-specific results file path (for file mode)."""
    return STATE_DIR / f"review-results-{session_hash}.json"


# -----------------------------------------------------------------------------
# Review Mode
# -----------------------------------------------------------------------------
# "agent" = Use Claude Code Task subagents (recommended, no API key needed)
# "api"   = Call Anthropic API directly (requires ANTHROPIC_API_KEY env var)
REVIEW_MODE = "agent"

# -----------------------------------------------------------------------------
# Results Delivery Mode
# -----------------------------------------------------------------------------
# "inline" = Claude outputs results with markers in response (no file write permissions needed)
# "file"   = Claude writes results to JSON file (requires Write permission in settings.local.json)
#
# For "file" mode, add this to .claude/settings.local.json:
#   "Write(.claude/hooks/review-results-*.json)"
RESULTS_MODE = "inline"

# -----------------------------------------------------------------------------
# File Filtering
# -----------------------------------------------------------------------------
# Extensions to exclude from review (non-code files)
EXCLUDED_EXTENSIONS = {
    ".json", ".md", ".txt",
    ".yml", ".yaml",
    ".toml", ".ini", ".cfg",
    ".lock", ".sum",
}

# Extensionless files to exclude (non-code files without extensions)
EXCLUDED_FILENAMES = {
    "LICENSE", "LICENCE", "Makefile", "Dockerfile", "Procfile",
    "Gemfile", "Rakefile", "Vagrantfile", "Brewfile",
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig",
}

# -----------------------------------------------------------------------------
# Excluded Paths - Review and Customize
# -----------------------------------------------------------------------------
# Patterns to exclude from review (in addition to EXCLUDED_EXTENSIONS).
# Uses substring matching (case-insensitive, forward slashes).
#
# These are common patterns - add or remove based on your project structure.
#
EXCLUDED_PATHS = [
    # Test fixtures and caches
    "/tests/fixtures/",
    "/test/fixtures/",
    "/__pycache__/",
    "/.pytest_cache/",
    # Dependencies
    "/node_modules/",
    "/.venv/",
    "/venv/",
    # Build output
    "/build/",
    "/dist/",
    # Git
    "/.git/",
    # Coverage
    "/coverage/",
    "/.coverage",
    "/htmlcov/",
    # Add your project-specific exclusions below:
]

# -----------------------------------------------------------------------------
# Tier Thresholds (based on INCREMENTAL diff since last hook firing)
# -----------------------------------------------------------------------------
# Review tier is determined by characters changed AND files touched.
# Both conditions must be met for a tier (except deep, which is the fallback).
TIER_THRESHOLDS = {
    "skip": 500,       # <500 chars: no review needed
    "quick": 5000,     # 500-5000 chars: lightweight review
    "standard": 20000, # 5000-20000 chars: standard review
    # ≥20000 chars: deep review (implicit)
}

TIER_FILE_LIMITS = {
    "skip": 1,      # 1 file: may skip if diff also small
    "quick": 3,     # ≤3 files: quick review
    "standard": 6,  # ≤6 files: standard review
    # >6 files: deep review (implicit)
}

# -----------------------------------------------------------------------------
# Auto-Continue Settings
# -----------------------------------------------------------------------------
MAX_AUTO_CONTINUES = 3  # How many passes before allowing stop
MAX_FAIL_RETRIES = 3    # How many retry rounds when agents find issues
STATE_EXPIRY = 3600     # Seconds - reset state if older than this (1 hour)

# -----------------------------------------------------------------------------
# Environment Variable Overrides
# -----------------------------------------------------------------------------
# Set CLAUDE_HOOK_FORCE_TIER=deep|standard|quick to bypass threshold checks
# Useful for testing the review system without needing large diffs
FORCE_TIER_ENV = "CLAUDE_HOOK_FORCE_TIER"

# -----------------------------------------------------------------------------
# Metrics Tracking (JSONL format, one event per line)
# -----------------------------------------------------------------------------
# Each line is a JSON object with:
# {
#   "timestamp": "2026-01-09T14:23:45",
#   "tier": "quick|standard|deep",
#   "diff_chars": 1500,
#   "file_count": 2,
#   "agents": ["explore_haiku", "general_haiku"],
#   "outcome": "pass|fail",
#   "fail_count": 0,
#   "session_id": "transcript_path_hash"
# }

# -----------------------------------------------------------------------------
# Critical File Patterns - CUSTOMIZE FOR YOUR PROJECT
# -----------------------------------------------------------------------------
# Changes to these paths trigger deeper review, regardless of size.
# Uses substring matching (case-insensitive, forward slashes).
#
# >>> IMPORTANT: Replace these with patterns relevant to YOUR codebase! <<<
#
# Examples for different project types:
#   Django:  ["/views/", "/serializers/", "/management/", "/migrations/"]
#   React:   ["/components/", "/hooks/", "/contexts/", "/store/"]
#   Node.js: ["/routes/", "/controllers/", "/middleware/", "/models/"]
#   CLI:     ["/commands/", "/core/", "/plugins/"]
#
CRITICAL_PATTERNS = [
    # Add your project's critical paths here. Examples:
    # "/api/", "/routes/", "/models/", "/migrations/"
]

# -----------------------------------------------------------------------------
# Agent Definitions
# -----------------------------------------------------------------------------
# Agents spawned for each review tier.
# integration_checker is added dynamically when changes span 2+ directories.
AGENT_IDS = {
    "quick": ["explore_haiku"],
    "standard": ["explore_haiku", "general_haiku", "bug_hunter"],
    "deep": ["explore_haiku", "general_haiku", "bug_hunter", "general_sonnet"],
}

# -----------------------------------------------------------------------------
# API Mode Settings (fallback, only used when REVIEW_MODE = "api")
# -----------------------------------------------------------------------------
API_DIFF_THRESHOLD = 500  # chars - below uses Haiku, above uses Sonnet


def log(msg: str) -> None:
    """Write debug message to log file."""
    try:
        with open(DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass  # Never let logging break the hook


# =============================================================================
# Transcript Parsing (Incremental)
# =============================================================================


def normalize_path(path: str) -> str:
    """Normalize path separators to forward slashes for cross-platform consistency."""
    return path.replace("\\", "/")


def _should_exclude_file(file_path: str) -> bool:
    """Check if file should be excluded from review.

    Returns True if file matches EXCLUDED_EXTENSIONS, EXCLUDED_FILENAMES, or EXCLUDED_PATHS.
    """
    path = Path(file_path)

    # Check filename (for extensionless files like LICENSE, Makefile)
    if path.name in EXCLUDED_FILENAMES:
        return True

    # Check extension
    suffix = path.suffix.lower()
    if suffix in EXCLUDED_EXTENSIONS:
        return True

    # Check path patterns
    normalized = normalize_path(file_path.lower())
    for pattern in EXCLUDED_PATHS:
        if pattern in normalized:
            return True

    return False


def _calculate_edit_diff(tool_input: dict) -> tuple[int, str | None]:
    """Calculate net character diff for an Edit tool call.

    Edit replaces old_string with new_string, so net change is the difference.

    Args:
        tool_input: The "input" field from a tool_use event.

    Returns:
        (net_chars, file_path) - net_chars can be negative for deletions.
        Returns (0, None) if file should be excluded.
    """
    file_path = tool_input.get("file_path", "")

    if not file_path or _should_exclude_file(file_path):
        return 0, None

    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    net_chars = len(new_string) - len(old_string)

    return net_chars, file_path


def _calculate_write_diff(tool_input: dict) -> tuple[int, str | None]:
    """Calculate character count for a Write tool call.

    Write creates/overwrites a file, so we count the full content length.

    Args:
        tool_input: The "input" field from a tool_use event.

    Returns:
        (chars, file_path) - always positive or zero.
        Returns (0, None) if file should be excluded.
    """
    file_path = tool_input.get("file_path", "")

    if not file_path or _should_exclude_file(file_path):
        return 0, None

    content = tool_input.get("content", "")
    return len(content), file_path


def _extract_tool_uses(event: dict) -> list[dict]:
    """Extract tool_use objects from a transcript event.

    Handles two transcript formats used by Claude Code:
    1. Direct: {"type": "tool_use", "name": "Edit", "input": {...}}
    2. Nested: {"message": {"content": [{"type": "tool_use", ...}]}}

    Args:
        event: A single line parsed from the JSONL transcript.

    Returns:
        List of tool_use dictionaries found in the event.
    """
    tool_uses = []

    # Format 1: Direct tool_use at top level
    if event.get("type") == "tool_use":
        tool_uses.append(event)

    # Format 2: Nested in message.content[]
    content_list = event.get("message", {}).get("content", [])
    if isinstance(content_list, list):
        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_uses.append(item)

    return tool_uses


def parse_transcript_total(transcript_path: str) -> dict:
    """Parse entire transcript to calculate TOTAL cumulative diff.

    This parses the full transcript from the beginning and returns the
    total cumulative diff. To get incremental diff, subtract last_total_diff.

    Args:
        transcript_path: Path to the JSONL transcript file.

    Returns:
        {
            "tools": set[str],           # All tool names used in transcript
            "total_diff_chars": int,     # Cumulative character change
            "files_modified": set[str],  # All code files touched
            "edit_count": int,           # Total Edit operations
            "write_count": int,          # Total Write operations
        }
    """
    result: dict = {
        "tools": set(),
        "total_diff_chars": 0,
        "files_modified": set(),
        "edit_count": 0,
        "write_count": 0,
    }

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                tool_uses = _extract_tool_uses(event)
                for tool_data in tool_uses:
                    tool_name = tool_data.get("name") or tool_data.get("tool_name")
                    if tool_name:
                        result["tools"].add(tool_name)

                    tool_input = tool_data.get("input", {})
                    if not isinstance(tool_input, dict):
                        continue

                    if tool_name == "Edit":
                        chars, file_path = _calculate_edit_diff(tool_input)
                        result["total_diff_chars"] += chars
                        if file_path:
                            result["files_modified"].add(file_path)
                            result["edit_count"] += 1

                    elif tool_name == "Write":
                        chars, file_path = _calculate_write_diff(tool_input)
                        result["total_diff_chars"] += chars
                        if file_path:
                            result["files_modified"].add(file_path)
                            result["write_count"] += 1

    except FileNotFoundError:
        log(f"Transcript file not found: {transcript_path}")
    except PermissionError:
        log(f"Permission denied reading transcript: {transcript_path}")
    except Exception as e:
        log(f"Error parsing transcript: {e}")

    return result


def get_last_tool_used(transcript_path: str) -> str | None:
    """Get the name of the last tool used in the transcript.

    Reads transcript and returns the most recent tool_use name.
    Returns None if no tools found or on error.
    """
    last_tool = None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tool_uses = _extract_tool_uses(event)
                for tool_data in tool_uses:
                    tool_name = tool_data.get("name") or tool_data.get("tool_name")
                    if tool_name:
                        last_tool = tool_name  # Keep updating - last one wins
    except Exception as e:
        log(f"Error getting last tool: {e}")
    return last_tool


def extract_changed_hunks(
    transcript_path: str,
    start_position: int,
    files: set[str],
    max_preview_chars: int = 500
) -> dict[str, str]:
    """Extract actual code changes from Edit/Write tool calls.

    Args:
        transcript_path: Path to JSONL transcript
        start_position: Byte offset to start from (incremental)
        files: Set of files to extract hunks for
        max_preview_chars: Max chars per file preview

    Returns:
        {file_path: "preview of changes..."}
    """
    hunks = {}

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            f.seek(start_position)

            for line in f:
                if not line.strip():
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                tool_uses = _extract_tool_uses(event)
                for tool_data in tool_uses:
                    tool_name = tool_data.get("name") or tool_data.get("tool_name")
                    tool_input = tool_data.get("input", {})

                    if not isinstance(tool_input, dict):
                        continue

                    file_path = tool_input.get("file_path", "")
                    if file_path not in files:
                        continue

                    if tool_name == "Edit":
                        old_str = tool_input.get("old_string", "")
                        new_str = tool_input.get("new_string", "")

                        # Create diff preview
                        if len(old_str) > 100:
                            old_preview = old_str[:100] + "..."
                        else:
                            old_preview = old_str

                        if len(new_str) > 100:
                            new_preview = new_str[:100] + "..."
                        else:
                            new_preview = new_str

                        hunk = f"- Old: {old_preview}\n+ New: {new_preview}"

                        # Append to existing hunks for this file
                        if file_path in hunks:
                            hunks[file_path] += f"\n\n{hunk}"
                        else:
                            hunks[file_path] = hunk

                    elif tool_name == "Write":
                        content = tool_input.get("content", "")
                        preview = content[:max_preview_chars]
                        if len(content) > max_preview_chars:
                            preview += "..."

                        hunks[file_path] = f"+ New file/overwrite:\n{preview}"

    except Exception as e:
        log(f"Error extracting hunks: {e}")

    # Truncate each hunk to max_preview_chars
    for file_path in hunks:
        if len(hunks[file_path]) > max_preview_chars:
            hunks[file_path] = hunks[file_path][:max_preview_chars] + "\n... (truncated)"

    return hunks


# =============================================================================
# Tier Classification
# =============================================================================


def should_run_integration_review(files: list[str]) -> tuple[bool, set[str], set[str]]:
    """Check if changes span multiple top-level directories OR multiple critical patterns.

    Integration checker is added when:
    1. Changes span 2+ top-level directories (api/, services/, frontend/, etc.)
    2. Changes touch 2+ critical patterns (e.g., /api/ AND /routes/)

    Args:
        files: List of file paths modified.

    Returns:
        (needs_review, top_level_dirs, matched_critical_patterns)
    """
    # Check top-level directory spanning
    top_level_dirs: set[str] = set()
    for f in files:
        normalized = normalize_path(f)
        parts = normalized.split("/")
        if len(parts) > 1:
            top_level_dirs.add(parts[0])

    # Check critical pattern spanning
    matched_patterns: set[str] = set()
    for f in files:
        f_normalized = normalize_path(f.lower())
        for pattern in CRITICAL_PATTERNS:
            if pattern in f_normalized:
                matched_patterns.add(pattern)
                break  # Only count each file once

    needs_review = len(top_level_dirs) >= 2 or len(matched_patterns) >= 2
    return needs_review, top_level_dirs, matched_patterns


def load_review_config() -> dict:
    """Load review config from .claude/review-config.json.

    Returns:
        Full config dict, or {} if file doesn't exist
    """
    config_path = Path(__file__).parent.parent / "review-config.json"

    if not config_path.exists():
        log("No review-config.json found")
        return {}

    try:
        return json.loads(config_path.read_text())
    except Exception as e:
        log(f"Error loading review config: {e}")
        return {}


def load_module_boundaries() -> dict:
    """Load module boundary rules from config file.

    Returns:
        Module rules dict, or {} if file doesn't exist
    """
    config = load_review_config()
    return config.get("module_boundaries", {})


def check_import_violations(
    files: list[str],
    hunks: dict[str, str],
    module_rules: dict
) -> list[str]:
    """Check for module boundary import violations.

    Args:
        files: List of modified files
        hunks: Code hunks extracted from transcript
        module_rules: Rules from load_module_boundaries()

    Returns:
        List of violation messages
    """
    violations = []

    if not module_rules:
        return violations

    for file_path in files:
        # Determine module from file path
        normalized = normalize_path(file_path)
        parts = normalized.split("/", 1)
        if len(parts) < 2:
            continue

        module = parts[0]
        rules = module_rules.get(module)

        if not rules:
            continue

        # Get code content from hunks (or skip if not available)
        code = hunks.get(file_path, "")
        if not code:
            continue

        # Check for forbidden imports
        forbidden = rules.get("forbidden_imports", [])
        for forbidden_module in forbidden:
            # Check various import patterns
            patterns = [
                f"from {forbidden_module}",
                f"import {forbidden_module}",
            ]

            for pattern in patterns:
                if pattern in code:
                    communication = rules.get("communication", "See architecture docs")
                    violations.append(
                        f"{file_path}: Imports from '{forbidden_module}' (forbidden). {communication}"
                    )
                    break  # Only report once per file

    return violations


def classify_review_tier(
    incremental_diff: int,
    incremental_file_count: int,
) -> str:
    """Classify review tier based on INCREMENTAL changes since last hook.

    This function determines how thorough a review is needed based on
    how much code changed since the last hook firing (not cumulative).

    Tier is based purely on size and file count. Critical file detection
    is handled separately via integration_checker agent.

    Environment variable override: Set CLAUDE_HOOK_FORCE_TIER=deep|standard|quick
    to bypass threshold checks (useful for testing).

    Args:
        incremental_diff: Net character change since last hook (can be negative).
        incremental_file_count: Number of NEW files modified since last hook.

    Returns:
        "skip", "quick", "standard", or "deep"
    """
    # Check for config file override first (most reliable)
    config = load_review_config()
    config_tier = config.get("force_tier", "").lower()
    if config_tier in ("deep", "standard", "quick"):
        log(f"Tier forced via review-config.json: {config_tier}")
        return config_tier

    # Check for environment variable override (backup method)
    forced_tier = os.environ.get(FORCE_TIER_ENV, "").lower()
    if forced_tier in ("deep", "standard", "quick"):
        log(f"Tier forced via {FORCE_TIER_ENV}: {forced_tier}")
        return forced_tier

    # Use absolute value for tier classification
    # (deletions are as significant as additions)
    abs_diff = abs(incremental_diff)

    # Tier by size AND file count (both conditions must be met)
    for tier in ["skip", "quick", "standard"]:
        if abs_diff < TIER_THRESHOLDS[tier] and incremental_file_count <= TIER_FILE_LIMITS[tier]:
            return tier

    return "deep"


def detect_file_contexts(files: list[str]) -> set[str]:
    """Detect special file contexts that need targeted review.

    Returns set of context tags like: 'proto', 'grpc_service', 'database', 'api_routes'
    """
    contexts = set()

    for f in files:
        f_lower = normalize_path(f.lower())

        # Protocol buffers
        if f_lower.endswith('.proto'):
            contexts.add('proto')

        # gRPC service implementations
        if '_service.py' in f_lower or 'grpc_service.py' in f_lower:
            contexts.add('grpc_service')

        # Database migrations/models
        if '/migrations/' in f_lower or '/models/' in f_lower:
            contexts.add('database')

        # API routes
        if '/routes/' in f_lower or '/api/' in f_lower:
            contexts.add('api_routes')

        # Frontend
        if f_lower.endswith(('.tsx', '.jsx', '.ts', '.js')) and '/frontend/' in f_lower:
            contexts.add('frontend')

    return contexts


# Agent definitions with their check instructions
AGENT_DEFINITIONS = {
    "explore_haiku": {
        "subagent_type": "Explore",
        "model": "haiku",
        "checks": "code smells, obvious bugs, hardcoded values, missing error handling",
        "context_checks": {
            "proto": "Check field numbering, message compatibility, enum values",
            "database": "Check migration reversibility, index definitions",
        }
    },
    "general_haiku": {
        "subagent_type": "general-purpose",
        "model": "haiku",
        "checks": "silent failures (return None/[]), missing validation, security issues (SQL injection, XSS)",
        "context_checks": {
            "api_routes": "Check input validation, error responses, authentication",
            "grpc_service": "Check error handling, request validation, streaming edge cases",
        }
    },
    "bug_hunter": {
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "checks": "null/undefined access, off-by-one errors, race conditions, resource leaks, unhandled edge cases",
        "context_checks": {
            "frontend": "Check React hooks dependencies, state update batching, memory leaks",
            "database": "Check transaction boundaries, connection leaks, deadlocks",
        }
    },
    "general_sonnet": {
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "checks": "service boundary violations, API contract breaks, architecture issues",
        "context_checks": {
            "grpc_service": "Check service contracts, backward compatibility",
        }
    },
    "integration_checker": {
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "checks": "API contract consistency, cross-module imports, shared state mutations, event/callback mismatches",
        "context_checks": {
            "proto": "Check cross-service message dependencies, breaking changes",
        }
    },
    "plan_agent": {
        "subagent_type": "Plan",
        "description": "Creates structured remediation plans for review failures",
    },
}


def format_files_grouped(files: list[str], max_files: int = MAX_FILES_IN_PROMPT) -> str:
    """Format file list grouped by top-level directory.

    Handles both relative and absolute paths, converting absolute paths
    to be relative to the current working directory. Truncates to max_files.

    Args:
        files: List of file paths.
        max_files: Maximum files to include (default MAX_FILES_IN_PROMPT).

    Returns:
        Formatted string with files grouped by directory.
    """
    if not files:
        return "(no files)"

    truncated = len(files) > max_files
    files_to_show = sorted(files)[:max_files]

    cwd = normalize_path(os.getcwd())

    # Group files by top-level directory
    groups: dict[str, list[str]] = {}
    for f in files_to_show:
        normalized = normalize_path(f)

        # Convert absolute paths to relative
        if os.path.isabs(f):
            try:
                # Try to make it relative to cwd
                rel = normalize_path(os.path.relpath(f))
                # If relpath goes up (..), just use the filename
                if rel.startswith(".."):
                    normalized = os.path.basename(f)
                else:
                    normalized = rel
            except (ValueError, OSError):
                # If relpath fails (e.g., different drives on Windows), use basename
                normalized = os.path.basename(f)

        parts = normalized.split("/", 1)

        if len(parts) == 1:
            # File in root
            top_dir = "."
            rel_path = parts[0]
        else:
            top_dir = parts[0]
            rel_path = parts[1]

        if top_dir not in groups:
            groups[top_dir] = []
        groups[top_dir].append(rel_path)

    # Format output
    lines = []
    for top_dir in sorted(groups.keys()):
        lines.append(f"  {top_dir}/")
        for file in sorted(groups[top_dir]):
            lines.append(f"    - {file}")

    if truncated:
        lines.append(f"  ...and {len(files) - max_files} more files")

    return "\n".join(lines)


def get_review_instructions(
    tier: str,
    diff_size: int,
    total_file_count: int,
    new_file_count: int,
    file_list: str,
    pending_agents: list[str],
    passed_agents: list[str],
    round_id: str,
    auto_continue_count: int,
    fail_count: int,
    file_contexts: set[str],
    session_hash: str,
    integration_context: dict | None = None,
    files: list[str] = None,
    code_hunks: dict[str, str] = None,
    violation_history: dict[str, dict] = None,
    import_violations: list[str] = None,
) -> str:
    """Generate tier-specific review instructions for Claude.

    Args:
        tier: Review tier (quick/standard/deep).
        diff_size: Incremental character diff since last hook firing.
        total_file_count: Total files modified in this segment.
        new_file_count: Number of NEW files (not seen before in session).
        file_list: Formatted list of files for display.
        pending_agents: Agents that still need to run.
        passed_agents: Agents that have already passed.
        round_id: Current review round UUID.
        auto_continue_count: Number of successful passes this session.
        fail_count: Number of failed reviews this round.
        file_contexts: Set of file context tags (proto, api_routes, etc.).
        integration_context: Dict with "dirs" and "patterns" sets for integration checker.
        files: Raw file list for module counting.

    Returns:
        Formatted instructions string for Claude.
    """
    tier_label = tier.upper()

    # Build header with file stats and module count
    if files:
        top_dirs = set()
        for f in files:
            parts = normalize_path(f).split("/", 1)
            if len(parts) > 1:
                top_dirs.add(parts[0])
        module_count = len(top_dirs)
    else:
        module_count = 0

    if new_file_count < total_file_count:
        file_display = f"{total_file_count} files ({new_file_count} new) across {module_count} module(s)"
    else:
        file_display = f"{total_file_count} file(s) across {module_count} module(s)"
    header_parts = [f"{tier_label}_REVIEW: +{diff_size} chars, {file_display}"]
    if round_id:
        header_parts.append(f"[Round: {round_id}]")
    if fail_count > 0:
        header_parts.append(f"[Retry {fail_count}/{MAX_FAIL_RETRIES}]")
    header = " ".join(header_parts)

    # Show passed agents
    passed_section = ""
    if passed_agents:
        passed_lines = [f"  ✓ {agent_id}: PASSED (skip)" for agent_id in passed_agents]
        passed_section = "\nPrevious results:\n" + "\n".join(passed_lines) + "\n"

    # Build agent instructions for pending agents only
    agent_instructions = []
    for i, agent_id in enumerate(pending_agents, 1):
        defn = AGENT_DEFINITIONS.get(agent_id, {})
        base_checks = defn.get('checks', 'general code quality')

        # Add context-specific checks if applicable
        context_checks = defn.get('context_checks', {})
        context_additions = [context_checks[ctx] for ctx in file_contexts if ctx in context_checks]

        # Add integration context if this is integration_checker
        if agent_id == "integration_checker" and integration_context:
            dirs = integration_context.get("dirs", set())
            patterns = integration_context.get("patterns", set())

            if len(dirs) >= 2:
                context_additions.append(f"Cross-directory: {', '.join(sorted(dirs))}")
            if len(patterns) >= 2:
                context_additions.append(f"Critical patterns: {', '.join(sorted(patterns))}")

        if context_additions:
            full_checks = f"{base_checks}. ALSO: {', '.join(context_additions)}"
        else:
            full_checks = base_checks

        agent_instructions.append(
            f"{i}. subagent_type='{defn.get('subagent_type', 'general-purpose')}', "
            f"model='{defn.get('model', 'haiku')}' [ID: {agent_id}]\n"
            f"   Check: {full_checks}"
        )

    if len(pending_agents) > 1:
        spawn_note = f"Spawn {len(pending_agents)} Task agents IN PARALLEL (single message, multiple tool calls):"
    else:
        spawn_note = "Spawn 1 Task agent:"

    agents_section = spawn_note + "\n\n" + "\n\n".join(agent_instructions)

    # Add violation history section if applicable
    history_section = ""
    if violation_history and files:
        problem_files = [f for f in files if f in violation_history]
        if problem_files:
            history_section = "\n\nFiles with previous violations:"
            for file in problem_files[:MAX_VIOLATION_FILES]:
                categories = violation_history[file]
                total = sum(categories.values())
                top_category = max(categories.items(), key=lambda x: x[1])[0]
                history_section += f"\n  - {file}: {total}x violations (most common: {top_category})"

    # Status note with new terminology
    status_note = f"\n\n[Auto-continue {auto_continue_count + 1} of {MAX_AUTO_CONTINUES}] If no issues found, continue with implementation."

    # Results instruction - mode-aware (inline markers or file write)
    pending_agents_list = ", ".join(f'"{a}"' for a in pending_agents)

    # Concise schema - models know JSON and severity levels
    results_schema = f'''Report: [{pending_agents_list}]
Format: {{"status": "pass"|"fail", "issues": [{{"file": "...", "line": N, "severity": "critical|high|medium|low", "description": "..."}}]}}
Fail if: critical issue OR 2+ high issues'''

    if RESULTS_MODE == "file":
        # File mode: write results to JSON file
        results_instruction = f'''

Write results to .claude/hooks/review-results-{session_hash}.json:
{{"round_id": "{round_id}", "agents": {{"<agent_id>": {{"status": "...", "issues": [...]}}}}}}

{results_schema}'''
    else:
        # Inline mode: output results with markers in response
        results_instruction = f'''

Output results (no files):
<!--REVIEW_RESULTS_START-->
{{"round_id": "{round_id}", "agents": {{"<agent_id>": {{"status": "...", "issues": []}}}}}}
<!--REVIEW_RESULTS_END-->

{results_schema}'''

    # Build module boundary violations section
    violations_section = ""
    if import_violations:
        violations_section = "\n\nModule Boundary Violations Detected:"
        for violation in import_violations:
            violations_section += f"\n  ⚠️  {violation}"

    # Build code hunks section
    hunks_section = ""
    if code_hunks:
        hunks_section = "\n\nCode Changes Preview:"
        for file, hunk in code_hunks.items():
            hunks_section += f"\n\n{file}:\n{hunk}"

    # For deep reviews, tell Claude to stop after reporting (not fix)
    if tier == "deep":
        post_review_instruction = "After reviews complete, report findings. DO NOT FIX. Stop and wait for user."
    else:
        post_review_instruction = "After reviews complete, report findings and fix any violations."

    return f"""{header}
{passed_section}
{agents_section}
{history_section}

Files:
{file_list}
{violations_section}
{hunks_section}

{post_review_instruction}{status_note}{results_instruction}"""


# =============================================================================
# State Management (Per-Chat Tracking)
# =============================================================================
#
# State file schema:
# {
#   "session_id": str,               # transcript_path - unique per chat (stays constant)
#   "last_total_diff": int,          # cumulative diff at last hook run (for incremental calc)
#   "last_files_seen": list[str],    # files modified as of last hook firing
#   "timestamp": str,                # ISO format, for staleness check
#   "tier": str,                     # last review tier
#   "auto_continue_count": int,      # successful passes this chat
#   "fail_count": int,               # failures this round
#   "round_id": str,                 # current review round UUID
#   "passed_agents": list[str],      # agents that passed this round
#   "completed": bool,               # review cycle completed
#   "violation_history": dict,       # {file: {category: count}} - track problem files
# }
#
# Incremental diff calculation:
#   1. Parse ENTIRE transcript to get current_total_diff
#   2. Load last_total_diff from state
#   3. incremental_diff = current_total_diff - last_total_diff
#   4. Save current_total_diff as new last_total_diff


def get_review_state(session_hash: str) -> dict:
    """Load session state from file.

    Args:
        session_hash: Hash of transcript_path for session-specific file.

    Returns empty dict if:
    - File doesn't exist
    - File is malformed JSON

    Note: Staleness is now checked separately to preserve session_id and
    last_total_diff even when review state is stale. This allows
    incremental diff calculation to work correctly.

    Returns:
        State dictionary with optional "is_stale" flag, or empty dict if invalid.
    """
    state_file = get_state_file(session_hash)
    if not state_file.exists():
        return {}

    try:
        content = state_file.read_text()
        if not content.strip():
            return {}

        state = json.loads(content)

        # Validate it's a dict
        if not isinstance(state, dict):
            log("State file is not a dict")
            return {}

        # Check staleness - but still return state with a flag
        # This preserves session_id and position for incremental parsing
        timestamp_str = state.get("timestamp")
        if timestamp_str:
            try:
                ts = datetime.fromisoformat(timestamp_str)
                age_seconds = (datetime.now() - ts).total_seconds()
                if age_seconds > STATE_EXPIRY:
                    log(f"State stale ({age_seconds:.0f}s > {STATE_EXPIRY}s) - preserving position")
                    state["is_stale"] = True
            except ValueError:
                # Invalid timestamp format, continue with state anyway
                pass

        return state

    except json.JSONDecodeError as e:
        log(f"State file is malformed JSON: {e}")
        return {}
    except Exception as e:
        log(f"Error reading state file: {e}")
        return {}


def save_review_state(
    session_hash: str,
    session_id: str,
    last_total_diff: int,
    last_files_seen: list[str],
    tier: str,
    auto_continue_count: int,
    fail_count: int,
    round_id: str,
    passed_agents: list[str],
    completed: bool = False,
    violation_history: dict[str, dict] = None,
) -> None:
    """Save session state to file.

    Args:
        session_hash: Hash of transcript_path for session-specific file.
        session_id: The transcript_path, used to detect new sessions.
        last_total_diff: Cumulative total diff at last hook run (for incremental calc).
        last_files_seen: List of code files modified up to this point.
        tier: Current review tier (skip/quick/standard/deep).
        auto_continue_count: Number of successful review passes this session.
        fail_count: Number of failed reviews this round.
        round_id: Current review round UUID.
        passed_agents: List of agent IDs that passed this round.
        completed: Whether the review cycle is complete.
        violation_history: Dict of {file: {category: count}} tracking violations.
    """
    state = {
        "session_id": session_id,
        "last_total_diff": last_total_diff,
        "last_files_seen": last_files_seen,
        "timestamp": datetime.now().isoformat(),
        "tier": tier,
        "auto_continue_count": auto_continue_count,
        "fail_count": fail_count,
        "round_id": round_id,
        "passed_agents": passed_agents,
        "completed": completed,
        "violation_history": violation_history or {},
    }

    state_file = get_state_file(session_hash)
    try:
        state_file.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"Error saving state: {e}")
        # Don't let state saving break the hook


def update_violation_history(
    history: dict[str, dict],
    review_results: dict
) -> dict[str, dict]:
    """Update violation history from review results.

    Args:
        history: Existing history {file: {category: count}}
        review_results: Results from read_review_results()

    Returns:
        Updated history dict
    """
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

            # Initialize file entry
            if file not in history:
                history[file] = {}

            # Increment category count
            if category not in history[file]:
                history[file][category] = 0
            history[file][category] += 1

    return history


# =============================================================================
# Plan Subagent Functions
# =============================================================================


def get_plan_agent_instructions(
    session_hash: str,
    review_results: dict,
    files: list[str],
    violation_history: dict[str, dict],
    code_hunks: dict[str, str],
) -> str:
    """Generate instructions for spawning the Plan subagent.

    Args:
        session_hash: Session identifier.
        review_results: Results from the failed review (agents and issues).
        files: List of files that were reviewed.
        violation_history: History of violations per file.
        code_hunks: Code snippets from the changes.

    Returns:
        Instructions string for Claude to spawn the Plan subagent.
    """
    # Extract issues from review results
    all_issues = []
    for agent_id, agent_data in review_results.get("agents", {}).items():
        if isinstance(agent_data, dict):
            issues = agent_data.get("issues", [])
            for issue in issues:
                if isinstance(issue, dict):
                    issue_copy = issue.copy()
                    issue_copy["found_by"] = agent_id
                    all_issues.append(issue_copy)

    # Group issues by severity
    critical_issues = [i for i in all_issues if i.get("severity") == "critical"]
    high_issues = [i for i in all_issues if i.get("severity") == "high"]
    medium_issues = [i for i in all_issues if i.get("severity") == "medium"]
    low_issues = [i for i in all_issues if i.get("severity") == "low"]

    # Format issues for prompt
    issues_summary = f"""
Issues Found ({len(all_issues)} total):
- Critical: {len(critical_issues)}
- High: {len(high_issues)}
- Medium: {len(medium_issues)}
- Low: {len(low_issues)}

Detailed Issues:
"""
    for issue in all_issues:
        severity = issue.get("severity", "unknown").upper()
        file = issue.get("file", "unknown")
        desc = issue.get("description", "No description")
        found_by = issue.get("found_by", "")
        issues_summary += f"\n- [{severity}] {file}: {desc}"
        if found_by:
            issues_summary += f" (found by {found_by})"

    # Build files context
    files_context = "\n".join([f"  - {f}" for f in files])

    plan_prompt = f"""You are a senior software architect creating a remediation plan.

## Context
A deep code review found {len(all_issues)} issues that need to be fixed.

## Files Under Review
{files_context}

{issues_summary}

## Your Task
Create a structured remediation plan that:
1. Prioritizes fixes by severity (critical first)
2. Groups related issues that can be fixed together
3. Identifies dependencies between fixes
4. Estimates complexity for each fix group
5. Suggests a safe implementation order

## Output Format
Return your plan as structured text (do NOT write to any file). Include:

1. SUMMARY: 1-2 sentence overview
2. FIX GROUPS: For each group:
   - Group name and priority (critical/high/medium/low)
   - Issues included
   - Files affected
   - Approach to fix
   - Complexity (simple/moderate/complex)
   - Dependencies on other groups
3. IMPLEMENTATION ORDER: Numbered list of groups in order
4. NOTES: Any important considerations

IMPORTANT:
- Do NOT write any files - return plan as text output only
- Do NOT implement any fixes - only create the plan
- Be specific about which files need changes
- Consider side effects and test requirements
"""

    return f"""PLAN REQUIRED: Deep review failed with {len(all_issues)} issue(s).

Before implementing fixes, a remediation plan must be created.

Spawn 1 Task agent to create the plan:

1. subagent_type='Plan' [ID: plan_agent]
   Task: Analyze the review results and create a prioritized remediation plan.

{plan_prompt}"""


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

    Never fails - errors are silently swallowed to avoid breaking hook.
    """
    try:
        metric = {
            "timestamp": datetime.now().isoformat(),
            "tier": tier,
            "diff_chars": diff_chars,
            "file_count": file_count,
            "agents": agents,
            "outcome": outcome,
            "fail_count": fail_count,
            "session_id": session_id[:8] if len(session_id) >= 8 else session_id,
        }

        with open(METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(metric) + "\n")
    except Exception as e:
        log(f"Failed to log metrics: {e}")


def validate_agent_result(agent_id: str, data: dict) -> bool:
    """Validate that an agent result has the expected schema.

    Expected: {"status": "pass"|"fail", "issues": [...]}
    """
    if not isinstance(data, dict):
        log(f"Agent {agent_id}: result is not a dict")
        return False
    status = data.get("status")
    if status not in (STATUS_PASS, STATUS_FAIL):
        log(f"Agent {agent_id}: invalid status '{status}' (expected 'pass' or 'fail')")
        return False
    issues = data.get("issues")
    if not isinstance(issues, list):
        log(f"Agent {agent_id}: issues is not a list")
        return False
    return True


# Markers for inline results in transcript (used in inline mode)
RESULTS_START_MARKER = "<!--REVIEW_RESULTS_START-->"
RESULTS_END_MARKER = "<!--REVIEW_RESULTS_END-->"


def _validate_results_data(data: dict) -> dict | None:
    """Validate and clean results data structure.

    Returns validated dict on success, None if invalid.
    """
    if not isinstance(data, dict):
        log(f"Invalid results: expected dict, got {type(data).__name__}")
        return None
    if "round_id" not in data:
        log("Invalid results: missing round_id")
        return None
    if "agents" in data:
        if not isinstance(data["agents"], dict):
            log(f"Invalid results: agents should be dict, got {type(data['agents']).__name__}")
            return None
        # Validate each agent result
        valid_agents = {}
        for agent_id, agent_data in data["agents"].items():
            if validate_agent_result(agent_id, agent_data):
                valid_agents[agent_id] = agent_data
            else:
                log(f"Skipping invalid agent result: {agent_id}")
        data["agents"] = valid_agents
    return data


def _repair_json(json_str: str) -> str:
    """Attempt to repair common JSON errors.

    Repairs:
    - Trailing commas before ] or }
    - Single quotes to double quotes (for keys/values)
    - Unescaped newlines in strings
    - Missing closing braces/brackets

    Args:
        json_str: Potentially malformed JSON string

    Returns:
        Repaired JSON string (may still be invalid)
    """
    import re

    s = json_str.strip()

    # 1. Remove trailing commas before ] or }
    s = re.sub(r',(\s*[}\]])', r'\1', s)

    # 2. Replace single quotes with double quotes for keys/values
    # Pattern: 'key': -> "key":
    s = re.sub(r"(?<=[{,\[])\s*'([^']+)'\s*(?=:)", r'"\1"', s)
    # Pattern: : 'value' -> : "value"
    s = re.sub(r"(?<=:)\s*'([^']*)'\s*(?=[,}\]])", r'"\1"', s)

    # 3. Collapse newlines (our JSON should be single-line)
    s = re.sub(r'\s*\n\s*', ' ', s)

    # 4. Try to balance braces if simple case (missing closers)
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    if open_braces > 0:
        s = s + '}' * open_braces
    if open_brackets > 0:
        s = s + ']' * open_brackets

    return s


def _extract_results_from_transcript(transcript_path: str) -> dict | None:
    """Extract review results from transcript using markers (inline mode).

    Parses JSONL line by line, extracts text content from assistant messages,
    and searches for markers in the decoded content. This handles JSON-escaped
    content properly.

    Args:
        transcript_path: Path to the JSONL transcript file.

    Returns validated dict on success, None if markers not found or invalid.
    """
    all_text_content = []

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Extract text content from message.content[]
                content_list = event.get("message", {}).get("content", [])
                if isinstance(content_list, list):
                    for item in content_list:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text = item.get("text", "")
                            if text:
                                all_text_content.append(text)
    except Exception as e:
        log(f"Error reading transcript: {e}")
        return None

    if not all_text_content:
        log("No text content found in transcript")
        return None

    # Join all text and search for markers
    full_text = "\n".join(all_text_content)

    # Find the LAST occurrence (most recent results)
    start_idx = full_text.rfind(RESULTS_START_MARKER)
    if start_idx == -1:
        log("Results markers not found in transcript text content")
        return None

    end_idx = full_text.find(RESULTS_END_MARKER, start_idx)
    if end_idx == -1:
        log("End marker not found after start marker")
        return None

    json_str = full_text[start_idx + len(RESULTS_START_MARKER):end_idx].strip()

    # Try standard parse first
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        log(f"JSON decode error in transcript results: {e}")
        log("Attempting JSON repair...")

        # Attempt repair
        repaired = _repair_json(json_str)
        try:
            data = json.loads(repaired)
            log("JSON repair successful")
        except json.JSONDecodeError as e2:
            log(f"JSON repair failed: {e2}")
            log(f"Original JSON (first 500 chars): {json_str[:500]}")
            return None

    return _validate_results_data(data)


def _read_results_from_file(session_hash: str) -> dict | None:
    """Read review results from JSON file (file mode).

    Args:
        session_hash: Hash of transcript_path for session-specific file.

    Returns validated dict on success, None on any failure.
    """
    results_file = get_results_file(session_hash)
    if not results_file.exists():
        return None
    try:
        content = results_file.read_text()
        if not content.strip():
            return None
        data = json.loads(content)
        return _validate_results_data(data)
    except json.JSONDecodeError as e:
        log(f"JSON decode error reading results file (possible race): {e}")
        return None
    except Exception as e:
        log(f"Error reading results file: {e}")
        return None


def read_review_results(transcript_path: str, session_hash: str) -> dict:
    """Read review results with retry logic (mode-aware).

    Uses RESULTS_MODE to determine whether to read from transcript (inline)
    or from file (file mode).

    Args:
        transcript_path: Path to the transcript file (for inline mode).
        session_hash: Hash of transcript_path (for file mode).

    Retries up to 3 times with exponential backoff (0.1s, 0.2s, 0.4s)
    to handle cases where Claude hasn't output/written results yet.
    Returns {} if all attempts fail.
    """
    for attempt in range(3):
        if RESULTS_MODE == "file":
            result = _read_results_from_file(session_hash)
        else:  # inline
            result = _extract_results_from_transcript(transcript_path)

        if result is not None:
            return result
        if attempt < 2:  # Don't sleep after last attempt
            sleep_time = RETRY_INITIAL_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
            log(f"Results read failed (mode={RESULTS_MODE}), retry {attempt + 1}/3 in {sleep_time}s")
            time.sleep(sleep_time)
    return {}


def get_required_agents(tier: str) -> list[str]:
    """Return list of agent IDs required for this tier."""
    return AGENT_IDS.get(tier, [])


# =============================================================================
# API Mode (Fallback)
# =============================================================================


def call_anthropic_review(transcript_path: str, use_sonnet: bool) -> dict:
    """Call Anthropic API to review the conversation for violations."""
    try:
        import anthropic
    except ImportError:
        log("anthropic package not installed")
        return {"violations": []}

    # Read transcript for context
    transcript_content = ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_content = f.read()
    except Exception as e:
        log(f"Error reading transcript for review: {e}")
        return {"violations": []}

    prompt = """You are a code reviewer for a software project.
Review the conversation transcript for design principle violations in any code that was written or modified.

## CRITICAL VIOLATIONS (must report):
1. Security Issues - SQL injection, XSS, hardcoded secrets, unsafe data handling
2. Silent Failures - return None/[] without exception, swallowed errors
3. Hardcoding - absolute paths, magic numbers without constants
4. Missing Error Handling - uncaught exceptions, missing validation
5. Code Smells - duplicated logic, overly complex functions, tight coupling

## Transcript (last 50000 chars):
{transcript}

## Response (JSON only, no other text):
If violations found: {{"violations": ["- [file:line] Description", ...]}}
If clean: {{"violations": []}}"""

    # Select model based on diff size
    model = "claude-sonnet-4-20250514" if use_sonnet else "claude-3-5-haiku-20241022"
    log(f"Calling {model} for review...")

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {"role": "user", "content": prompt.format(transcript=transcript_content[-MAX_TRANSCRIPT_CHARS_FOR_API:])}
            ],
        )
        # Check response has content before accessing
        if not response.content:
            log("API returned empty content")
            return {"violations": []}
        result_text = response.content[0].text
        log(f"LLM response: {result_text[:200]}...")
        return json.loads(result_text)
    except json.JSONDecodeError as e:
        log(f"Error parsing API response as JSON: {e}")
        return {"violations": []}
    except Exception as e:
        log(f"Error calling Anthropic API: {e}")
        return {"violations": []}


def main() -> None:
    """Main entry point for the stop hook.

    Flow:
    1. Parse input from stdin (transcript_path, cwd, etc.)
    2. Load state and detect new chat (reset counters if new)
    3. Parse ENTIRE transcript to get cumulative total diff
    4. Calculate incremental diff: current_total - last_total
    5. Classify review tier based on incremental changes
    6. Spawn review agents or allow stop based on tier
    """
    log("=" * 50)
    log("HOOK STARTED")

    # -------------------------------------------------------------------------
    # Step 1: Parse input from stdin
    # -------------------------------------------------------------------------
    try:
        raw_input = sys.stdin.read()
        log(f"Stdin: {len(raw_input)} chars")
        input_data = json.loads(raw_input, strict=False)
        log(f"Input keys: {list(input_data.keys())}")
    except Exception as e:
        log(f"Error reading stdin: {e}")
        sys.exit(0)  # Allow stop on error

    transcript_path = input_data.get("transcript_path", "")
    session_id_from_stdin = input_data.get("session_id", "")

    # Debug: Log both fields to understand what we're receiving
    log(f"transcript_path from stdin: {transcript_path}")
    log(f"session_id from stdin: {session_id_from_stdin}")

    if not transcript_path:
        log("No transcript_path in input")
        sys.exit(0)

    # Compute session hash for session-specific state files
    session_hash = get_session_hash(transcript_path)
    log(f"Session hash: {session_hash}")

    # -------------------------------------------------------------------------
    # Step 2: Load state and detect new session
    # -------------------------------------------------------------------------
    state = get_review_state(session_hash)
    old_session_id = state.get("session_id", "")

    # Use transcript_path for chat tracking (stays constant within a conversation)
    session_key = transcript_path
    is_new_session = (old_session_id != session_key)
    log(f"Comparing old='{old_session_id[-LOG_TRUNCATE_LENGTH:] if old_session_id else ''}' vs new='{session_key[-LOG_TRUNCATE_LENGTH:] if session_key else ''}'")
    is_stale = state.get("is_stale", False)

    if is_new_session:
        log("New session detected - resetting all counters")
        last_total_diff = 0
        last_files_seen: set[str] = set()
        auto_continue_count = 0
        fail_count = 0
        round_id = ""
        passed_agents: list[str] = []
        completed = False
        old_tier = ""
        violation_history: dict[str, dict] = {}
    elif is_stale:
        # Stale state: preserve total diff for incremental calculation, reset review state
        log("Stale state - preserving total diff, resetting review counters")
        last_total_diff = state.get("last_total_diff", 0)
        last_files_seen = set(state.get("last_files_seen", []))
        # Reset review-related state since it's stale
        auto_continue_count = 0
        fail_count = 0
        round_id = ""
        passed_agents = []
        completed = False
        old_tier = ""
        violation_history = {}
    else:
        last_total_diff = state.get("last_total_diff", 0)
        last_files_seen = set(state.get("last_files_seen", []))
        auto_continue_count = state.get("auto_continue_count", 0)
        fail_count = state.get("fail_count", 0)
        round_id = state.get("round_id", "")
        passed_agents = list(state.get("passed_agents", []))
        completed = state.get("completed", False)
        old_tier = state.get("tier", "")
        violation_history = state.get("violation_history", {})

    log(f"Session: {'NEW' if is_new_session else ('STALE' if is_stale else 'continuing')}, last_total_diff={last_total_diff}")

    # Load module boundaries for import checking
    module_rules = load_module_boundaries()

    # -------------------------------------------------------------------------
    # Step 3: Parse ENTIRE transcript to get cumulative total diff
    # -------------------------------------------------------------------------
    parse_result = parse_transcript_total(transcript_path)

    tools_used = parse_result["tools"]
    current_total_diff = parse_result["total_diff_chars"]
    all_modified_files = parse_result["files_modified"]
    edit_count = parse_result["edit_count"]
    write_count = parse_result["write_count"]

    # Calculate TRUE incremental diff by subtraction
    incremental_diff = current_total_diff - last_total_diff

    log(f"Total diff: {current_total_diff} chars, incremental: {incremental_diff} chars")
    log(f"Files modified: {len(all_modified_files)}, edits: {edit_count}, writes: {write_count}")
    log(f"Tools used: {sorted(tools_used)}")

    # -------------------------------------------------------------------------
    # Step 4: Calculate incremental files (NEW files not seen before)
    # -------------------------------------------------------------------------
    incremental_files = all_modified_files - last_files_seen
    all_files_seen = last_files_seen | all_modified_files

    log(f"Incremental files: {len(incremental_files)} new (total seen: {len(all_files_seen)})")

    # -------------------------------------------------------------------------
    # Early exit: No code modifications in this session (cumulative check)
    # Note: tools_used is the cumulative set from the entire transcript,
    # so this only catches sessions with zero edits ever.
    # -------------------------------------------------------------------------
    code_modified = "Edit" in tools_used or "Write" in tools_used
    if not code_modified:
        log("No code modified in this session - allowing stop")
        sys.exit(0)

    # -------------------------------------------------------------------------
    # Auto-approve after deep review cycle completed
    # -------------------------------------------------------------------------
    if completed and old_tier == "deep":
        log("Review cycle completed after deep review - auto-approving")
        # Clear completed flag for next round
        save_review_state(
            session_hash=session_hash,
            session_id=session_key,
            last_total_diff=current_total_diff,
            last_files_seen=list(all_files_seen),
            tier=old_tier,
            auto_continue_count=auto_continue_count,
            fail_count=0,
            round_id="",
            passed_agents=[],
            completed=False,
            violation_history=violation_history,
        )
        sys.exit(0)  # Allow stop for plan approval flow

    # -------------------------------------------------------------------------
    # Early exit: No incremental changes (position advanced but no diff)
    # -------------------------------------------------------------------------
    if incremental_diff == 0 and len(incremental_files) == 0:
        # Check if a review round just completed (results available with matching round_id)
        # This happens when Claude ran the agents and output/wrote the results
        if round_id:
            results = read_review_results(transcript_path, session_hash)
            if results.get("round_id") == round_id:
                log(f"Found results for pending round {round_id}")
                agents_results = results.get("agents", {})

                # Check if all required agents passed
                required_agents = get_required_agents(old_tier) if old_tier in ("quick", "standard", "deep") else []
                needs_integration, _, _ = should_run_integration_review(list(last_files_seen))
                if needs_integration:
                    required_agents = required_agents + ["integration_checker"]

                # Update passed_agents from results
                for agent_id, data in agents_results.items():
                    if isinstance(data, dict) and data.get("status") == STATUS_PASS and agent_id not in passed_agents:
                        passed_agents.append(agent_id)
                        log(f"  Agent {agent_id}: PASSED")

                all_agents_passed = all(a in passed_agents for a in required_agents)

                if all_agents_passed and required_agents:
                    auto_continue_count += 1
                    log(f"All agents passed! auto_continue_count now {auto_continue_count}")

                    # Save updated state
                    save_review_state(
                        session_hash=session_hash,
                        session_id=session_key,
                        last_total_diff=current_total_diff,
                        last_files_seen=list(all_files_seen),
                        tier=old_tier,
                        auto_continue_count=auto_continue_count,
                        fail_count=0,
                        round_id="",  # Clear round_id since it's complete
                        passed_agents=[],
                        completed=False,
                        violation_history=violation_history,
                    )

                    if auto_continue_count >= MAX_AUTO_CONTINUES:
                        log("Max auto-continues reached - allowing stop")
                        sys.exit(0)

                    continue_msg = "Continue with implementation. If tasks are finished, identify next logical steps."
                    log(f"Review passed - issuing auto-continue [Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}]")
                    print(json.dumps({
                        "decision": "block",
                        "reason": f"All reviews passed. [Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}"
                    }))
                    sys.exit(0)
                else:
                    # Some agents failed - check for deep tier plan requirement
                    failed_agents = [
                        agent_id for agent_id, data in agents_results.items()
                        if isinstance(data, dict) and data.get("status") == "fail"
                    ]
                    if failed_agents and old_tier == "deep":
                        total_issues = sum(
                            len(data.get("issues", []))
                            for data in agents_results.values()
                            if isinstance(data, dict)
                        )
                        log(f"  {len(failed_agents)} agent(s) failed: {failed_agents} - triggering plan agent")
                        # DON'T delete results file - plan agent needs it
                        save_review_state(
                            session_hash=session_hash,
                            session_id=session_key,
                            last_total_diff=current_total_diff,
                            last_files_seen=list(all_files_seen),
                            tier=old_tier,
                            auto_continue_count=auto_continue_count,
                            fail_count=len(failed_agents),
                            round_id=round_id,
                            passed_agents=passed_agents,
                            completed=False,
                            violation_history=violation_history,
                        )
                        # Generate plan agent instructions directly
                        results = read_review_results(transcript_path, session_hash)
                        code_hunks = extract_changed_hunks(
                            transcript_path=transcript_path,
                            start_position=0,
                            files=all_modified_files,
                            max_preview_chars=MAX_PREVIEW_CHARS
                        )
                        plan_instructions = get_plan_agent_instructions(
                            session_hash=session_hash,
                            review_results=results,
                            files=list(all_modified_files),
                            violation_history=violation_history,
                            code_hunks=code_hunks,
                        )
                        print(json.dumps({
                            "decision": "block",
                            "reason": f"⚠️ DEEP REVIEW FAILED: {len(failed_agents)} agent(s) found {total_issues} issue(s).\n\n{plan_instructions}"
                        }))
                        sys.exit(0)

        # Check if we already issued an auto-continue and should allow stop now
        # (no new changes after auto-continue = user is done)
        if auto_continue_count > 0:
            log(f"No new changes after auto-continue {auto_continue_count} - allowing stop")
            sys.exit(0)

        log("No incremental changes - allowing stop")
        # Save state so we track current total for next hook firing
        save_review_state(
            session_hash=session_hash,
            session_id=session_key,
            last_total_diff=current_total_diff,
            last_files_seen=list(all_files_seen),
            tier=old_tier or "skip",
            auto_continue_count=auto_continue_count,
            fail_count=fail_count,
            round_id=round_id,
            passed_agents=passed_agents,
            completed=completed,
            violation_history=violation_history,
        )
        sys.exit(0)

    # -------------------------------------------------------------------------
    # Step 5: Classify review tier based on INCREMENTAL changes
    # -------------------------------------------------------------------------
    tier = classify_review_tier(
        incremental_diff=incremental_diff,
        incremental_file_count=len(incremental_files),
    )
    log(f"Tier: {tier} (incremental_diff={incremental_diff}, incremental_files={len(incremental_files)})")

    # Build file list for review instructions (grouped by module)
    # Use all_modified_files (all modified) not incremental_files (only new to session)
    file_list = format_files_grouped(list(all_modified_files))

    # Detect file contexts for context-aware agent selection
    file_contexts = detect_file_contexts(list(all_modified_files))
    log(f"File contexts: {file_contexts}")

    # -------------------------------------------------------------------------
    # Skip tier: minimal changes - auto-continue up to MAX_AUTO_CONTINUES times
    # Save auto_continue_count but PRESERVE last_total_diff so changes accumulate.
    # -------------------------------------------------------------------------
    if tier == "skip":
        auto_continue_count += 1
        log(f"Skip tier - auto_continue_count now {auto_continue_count}, old_tier: {old_tier}")

        if auto_continue_count >= MAX_AUTO_CONTINUES:
            # After 3 auto-continues, just allow stop
            log("Max auto-continues reached on skip tier - allowing stop")
            sys.exit(0)
        elif old_tier == "skip":
            # Two skips in a row without a review - allow stop
            log("Consecutive skip without review - allowing stop")
            sys.exit(0)
        else:
            # Auto-continue with message
            continue_msg = "Continue with implementation. If tasks are finished, identify next logical steps."

            # Save state with updated counter but PRESERVE last_total_diff
            save_review_state(
                session_hash=session_hash,
                session_id=session_key,
                last_total_diff=last_total_diff,  # KEEP old value - let changes accumulate
                last_files_seen=list(last_files_seen),  # KEEP old value
                tier="skip",
                auto_continue_count=auto_continue_count,
                fail_count=fail_count,
                round_id=round_id,
                passed_agents=passed_agents,
                completed=False,
                violation_history=violation_history,
            )
            print(json.dumps({
                "decision": "block",
                "reason": f"Minimal changes ({abs(incremental_diff)} chars, {len(incremental_files)} files). [Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}"
            }))
            sys.exit(0)

    # -------------------------------------------------------------------------
    # Step 6: Review logic (agent mode or API mode)
    # -------------------------------------------------------------------------
    if REVIEW_MODE == "agent":
        # -----------------------------------------------------------------
        # Agent mode: Spawn Task subagents for review
        # -----------------------------------------------------------------

        # Check if review was already completed
        if completed:
            log("Review cycle already completed - allowing stop")
            # Save updated state
            save_review_state(
                session_hash=session_hash,
                session_id=session_key,
                last_total_diff=current_total_diff,
                last_files_seen=list(all_files_seen),
                tier=tier,
                auto_continue_count=auto_continue_count,
                fail_count=fail_count,
                round_id="",
                passed_agents=[],
                completed=True,
                violation_history=violation_history,
            )
            sys.exit(0)

        # Check if we've exceeded max auto continues
        if auto_continue_count >= MAX_AUTO_CONTINUES:
            log(f"Max auto continues reached ({MAX_AUTO_CONTINUES}) - allowing stop")
            get_state_file(session_hash).unlink(missing_ok=True)
            sys.exit(0)

        # Check if we've exceeded max fail retries
        if fail_count >= MAX_FAIL_RETRIES:
            log(f"Max fail retries reached ({MAX_FAIL_RETRIES}) - allowing stop for user intervention")
            get_state_file(session_hash).unlink(missing_ok=True)
            sys.exit(0)

        # Check if tier changed (different agents may be required)
        tier_changed = old_tier and old_tier != tier
        if tier_changed:
            log(f"Tier changed ({old_tier} -> {tier}) - starting new review round")
            passed_agents = []
            fail_count = 0
            round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]

        # Start new round if needed
        if not round_id:
            round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]
            log(f"Starting review round {round_id}")

        # Read previous review results (if any)
        results = read_review_results(transcript_path, session_hash)
        if results.get("round_id") == round_id:
            log(f"Found results for round {round_id}")
            agents_results = results.get("agents", {})

            # Update passed_agents from results
            for agent_id, data in agents_results.items():
                if isinstance(data, dict) and data.get("status") == STATUS_PASS and agent_id not in passed_agents:
                    passed_agents.append(agent_id)
                    log(f"  Agent {agent_id}: PASSED")

            # Count failed agents
            failed_agents = [
                agent_id for agent_id, data in agents_results.items()
                if isinstance(data, dict) and data.get("status") == "fail"
            ]

            # Get required agents for this tier (needed for both pass/fail metrics)
            required = get_required_agents(tier)
            needs_integration, _, _ = should_run_integration_review(list(incremental_files))
            if needs_integration:
                required = required + ["integration_checker"]

            if failed_agents:
                fail_count += len(failed_agents)
                log(f"  {len(failed_agents)} agent(s) failed: {failed_agents} - fail_count now {fail_count}")
                # Update violation history from failed results
                violation_history = update_violation_history(violation_history, results)
                # Log metrics for failed review
                log_review_metrics(
                    tier=tier,
                    diff_chars=abs(incremental_diff),
                    file_count=len(incremental_files),
                    agents=required,
                    outcome=STATUS_FAIL,
                    fail_count=fail_count,
                    session_id=session_key,
                )

                # Block and trigger plan agent for deep tier failures
                if tier == "deep":
                    total_issues = sum(
                        len(data.get("issues", []))
                        for data in agents_results.values()
                        if isinstance(data, dict)
                    )
                    log(f"Deep review failed with {total_issues} issues - triggering plan agent")
                    save_review_state(
                        session_hash=session_hash,
                        session_id=session_key,
                        last_total_diff=current_total_diff,
                        last_files_seen=list(all_files_seen),
                        tier=tier,
                        auto_continue_count=auto_continue_count,
                        fail_count=fail_count,
                        round_id=round_id,
                        passed_agents=passed_agents,
                        completed=False,
                        violation_history=violation_history,
                    )
                    # Generate plan agent instructions
                    code_hunks = extract_changed_hunks(
                        transcript_path=transcript_path,
                        start_position=0,
                        files=all_modified_files,
                        max_preview_chars=MAX_PREVIEW_CHARS
                    )
                    plan_instructions = get_plan_agent_instructions(
                        session_hash=session_hash,
                        review_results=results,
                        files=list(all_modified_files),
                        violation_history=violation_history,
                        code_hunks=code_hunks,
                    )
                    print(json.dumps({
                        "decision": "block",
                        "reason": f"⚠️ DEEP REVIEW FAILED: {len(failed_agents)} agent(s) found {total_issues} issue(s).\n\n{plan_instructions}"
                    }))
                    sys.exit(0)
            else:
                # Check if all required agents passed

                all_agents_passed = all(a in passed_agents for a in required)
                if all_agents_passed:
                    auto_continue_count += 1
                    log(f"All agents passed! auto_continue_count now {auto_continue_count}")
                    # Log metrics for passed review
                    log_review_metrics(
                        tier=tier,
                        diff_chars=abs(incremental_diff),
                        file_count=len(incremental_files),
                        agents=required,
                        outcome=STATUS_PASS,
                        fail_count=fail_count,
                        session_id=session_key,
                    )

                    if auto_continue_count >= MAX_AUTO_CONTINUES:
                        log(f"Max auto continues reached - marking complete")
                        save_review_state(
                            session_hash=session_hash,
                            session_id=session_key,
                            last_total_diff=current_total_diff,
                            last_files_seen=list(all_files_seen),
                            tier=tier,
                            auto_continue_count=auto_continue_count,
                            fail_count=0,
                            round_id="",
                            passed_agents=[],
                            completed=True,
                            violation_history=violation_history,
                        )
                        sys.exit(0)

                    # Continue with next round
                    new_round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]
                    save_review_state(
                        session_hash=session_hash,
                        session_id=session_key,
                        last_total_diff=current_total_diff,
                        last_files_seen=list(all_files_seen),
                        tier=tier,
                        auto_continue_count=auto_continue_count,
                        fail_count=0,
                        round_id=new_round_id,
                        passed_agents=[],
                        completed=False,
                        violation_history=violation_history,
                    )
                    # Determine continuation message based on progress
                    if auto_continue_count < MAX_AUTO_CONTINUES - 1:
                        continue_msg = "Continue with implementation. If tasks are finished, identify next logical steps."
                    else:
                        continue_msg = "If tasks remain, continue with implementation. Otherwise, identify next logical steps or improvements to consider."
                    print(json.dumps({
                        "decision": "block",
                        "reason": f"All reviews passed. [Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}"
                    }))
                    sys.exit(0)

        # Determine which agents still need to run and get integration context
        required_agents = get_required_agents(tier)
        needs_integration, top_dirs, crit_patterns = should_run_integration_review(list(incremental_files))
        integration_context = None
        if needs_integration:
            required_agents = required_agents + ["integration_checker"]
            integration_context = {"dirs": top_dirs, "patterns": crit_patterns}
        pending_agents = [a for a in required_agents if a not in passed_agents]

        if not pending_agents:
            # All agents already passed
            auto_continue_count += 1
            log(f"All agents already passed - auto_continue_count now {auto_continue_count}")

            if auto_continue_count >= MAX_AUTO_CONTINUES:
                log(f"Max auto continues reached - marking complete")
                save_review_state(
                    session_hash=session_hash,
                    session_id=session_key,
                    last_total_diff=current_total_diff,
                    last_files_seen=list(all_files_seen),
                    tier=tier,
                    auto_continue_count=auto_continue_count,
                    fail_count=0,
                    round_id="",
                    passed_agents=[],
                    completed=True,
                    violation_history=violation_history,
                )
                sys.exit(0)

            new_round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]
            save_review_state(
                session_hash=session_hash,
                session_id=session_key,
                last_total_diff=current_total_diff,
                last_files_seen=list(all_files_seen),
                tier=tier,
                auto_continue_count=auto_continue_count,
                fail_count=0,
                round_id=new_round_id,
                passed_agents=[],
                completed=False,
                violation_history=violation_history,
            )
            # Determine continuation message based on progress
            if auto_continue_count < MAX_AUTO_CONTINUES - 1:
                continue_msg = "Continue with implementation. If tasks are finished, identify next logical steps."
            else:
                continue_msg = "If tasks remain, continue with implementation. Otherwise, identify next logical steps or improvements to consider."
            print(json.dumps({
                "decision": "block",
                "reason": f"All reviews passed. [Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}"
            }))
            sys.exit(0)

        # Save state and trigger review
        log(f"Triggering {tier} review, round {round_id}, pending: {pending_agents}")
        save_review_state(
            session_hash=session_hash,
            session_id=session_key,
            last_total_diff=current_total_diff,
            last_files_seen=list(all_files_seen),
            tier=tier,
            auto_continue_count=auto_continue_count,
            fail_count=fail_count,
            round_id=round_id,
            passed_agents=passed_agents,
            completed=False,
            violation_history=violation_history,
        )

        # Extract code hunks for agent context (parse from start since we read full transcript)
        code_hunks = extract_changed_hunks(
            transcript_path=transcript_path,
            start_position=0,
            files=all_modified_files,
            max_preview_chars=MAX_PREVIEW_CHARS
        )
        log(f"Extracted code hunks for {len(code_hunks)} files")

        # Check for module boundary violations
        import_violations = check_import_violations(
            files=list(all_modified_files),
            hunks=code_hunks,
            module_rules=module_rules
        )
        if import_violations:
            log(f"Found {len(import_violations)} import violations")

        review_instructions = get_review_instructions(
            tier=tier,
            diff_size=abs(incremental_diff),
            total_file_count=len(all_modified_files),
            new_file_count=len(incremental_files),
            file_list=file_list,
            pending_agents=pending_agents,
            passed_agents=passed_agents,
            round_id=round_id,
            auto_continue_count=auto_continue_count,
            fail_count=fail_count,
            file_contexts=file_contexts,
            session_hash=session_hash,
            integration_context=integration_context,
            files=list(all_modified_files),
            code_hunks=code_hunks,
            violation_history=violation_history,
            import_violations=import_violations,
        )
        print(json.dumps({
            "decision": "block",
            "reason": review_instructions
        }))
        sys.exit(0)

    else:
        # -----------------------------------------------------------------
        # API mode (fallback): Call Anthropic API directly
        # -----------------------------------------------------------------
        use_sonnet = abs(incremental_diff) >= API_DIFF_THRESHOLD
        model_name = "Sonnet" if use_sonnet else "Haiku"
        log(f"API mode: Using {model_name} (diff {incremental_diff} chars)")

        review_result = call_anthropic_review(transcript_path, use_sonnet)
        violations = review_result.get("violations", [])

        if not violations:
            log(f"Clean code (reviewed by {model_name})")
            auto_continue_count += 1

            if auto_continue_count >= MAX_AUTO_CONTINUES:
                log(f"Max auto continues reached - allowing stop")
                get_state_file(session_hash).unlink(missing_ok=True)
                sys.exit(0)

            save_review_state(
                session_hash=session_hash,
                session_id=session_key,
                last_total_diff=current_total_diff,
                last_files_seen=list(all_files_seen),
                tier="api",
                auto_continue_count=auto_continue_count,
                fail_count=0,
                round_id="",
                passed_agents=[],
                completed=False,
                violation_history=violation_history,
            )
            # Determine continuation message based on progress
            if auto_continue_count < MAX_AUTO_CONTINUES - 1:
                continue_msg = "Continue with your implementation."
            else:
                continue_msg = "If tasks remain, continue with the implementation. Otherwise, identify the next logical steps or improvements to consider."
            print(json.dumps({
                "decision": "block",
                "reason": f"Code review passed ({model_name}). No violations found.\n\n[Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}"
            }))
            sys.exit(0)

        # Violations found
        violation_text = "\n".join(violations)
        log(f"Violations found (reviewed by {model_name})")
        print(json.dumps({
            "decision": "block",
            "reason": f"VIOLATIONS FOUND (reviewed by {model_name}):\n{violation_text}\n\nPlease fix these before proceeding."
        }))
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Always exit cleanly
        try:
            log(f"FATAL ERROR: {e}")
        except Exception:
            pass
        sys.exit(0)
