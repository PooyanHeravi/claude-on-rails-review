# Configuration Guide

This guide covers all configuration options for Claude on Rails Review.

## Table of Contents

- [Basic Setup](#basic-setup)
- [Results Mode](#results-mode)
- [Tier Configuration](#tier-configuration)
- [Agent Configuration](#agent-configuration)
- [File Filtering](#file-filtering)
- [Module Boundaries](#module-boundaries)
- [Advanced Options](#advanced-options)

## Basic Setup

### Installation

1. Copy `stop-design-audit.py` to `.claude/hooks/` in your project
2. Add hook configuration to `.claude/settings.json`:

```json
{
  "hooks": {
    "stop": [{
      "command": "python .claude/hooks/stop-design-audit.py",
      "timeout": 30000
    }]
  }
}
```

### Review Mode

Choose between agent mode (recommended) and API mode:

```python
# Agent mode - Uses Claude Code Task subagents (no API key needed)
REVIEW_MODE = "agent"

# API mode - Calls Anthropic API directly (requires ANTHROPIC_API_KEY)
REVIEW_MODE = "api"
```

## Results Mode

Choose how review results are delivered from Claude to the hook.

### Inline Mode (Default)

```python
RESULTS_MODE = "inline"
```

Claude outputs results with markers embedded in its response (2-space indented JSON):

```
<!--REVIEW_RESULTS_START-->
{
  "round_id": "a3f8d921",
  "agents": {
    "explore_haiku": {
      "status": "pass",
      "issues": []
    }
  }
}
<!--REVIEW_RESULTS_END-->
```

**Pros:**
- No file write permissions needed
- Results are in conversation history
- Works out of the box

**Cons:**
- Slightly more complex parsing (JSONL transcript extraction)

### File Mode

```python
RESULTS_MODE = "file"
```

Claude writes results directly to a JSON file:
```
.claude/hooks/review-results-{session_hash}.json
```

**Requires permission in `.claude/settings.local.json`:**

```json
{
  "permissions": {
    "allow": [
      "Write(.claude/hooks/review-results-*.json)"
    ]
  }
}
```

**Permission options (from most to least restrictive):**

| Pattern | What it allows |
|---------|----------------|
| `Write(.claude/hooks/review-results-*.json)` | Only review results files (recommended) |
| `Write(.claude/hooks/*.json)` | Any JSON file in hooks dir |
| `Write(.claude/hooks/**)` | Any file in hooks dir |

**Pros:**
- Clean file-based results for external tooling
- Simpler parsing

**Cons:**
- Requires adding write permission to settings.local.json

## Tier Configuration

### Tier Thresholds

Control when each review tier triggers based on character changes:

```python
TIER_THRESHOLDS = {
    "skip": 500,       # <500 chars: no review
    "quick": 5000,     # 500-5000 chars: lightweight review
    "standard": 20000, # 5000-20000 chars: standard review
    # ≥20000 chars: deep review (implicit)
}
```

### File Count Limits

Tier must meet BOTH character threshold AND file count:

```python
TIER_FILE_LIMITS = {
    "skip": 1,      # 1 file max for skip tier
    "quick": 3,     # ≤3 files for quick tier
    "standard": 6,  # ≤6 files for standard tier
    # >6 files: deep review (implicit)
}
```

**Example:** A change with 400 characters but 2 files will use **quick** tier (exceeds skip file limit).

### Auto-Continue Settings

Control how many successful review passes before manual approval:

```python
MAX_AUTO_CONTINUES = 3  # How many passes before requiring stop
MAX_FAIL_RETRIES = 3    # How many retry rounds when agents find issues
STATE_EXPIRY = 3600     # Seconds - reset state if older than this (1 hour)
```

## Agent Configuration

### Agent Definitions

Customize agents spawned for each tier:

```python
AGENT_IDS = {
    "quick": ["explore_haiku"],
    "standard": ["explore_haiku", "general_haiku", "bug_hunter"],
    "deep": ["explore_haiku", "general_haiku", "bug_hunter", "general_sonnet"],
}
```

### Customizing Agent Checks

Modify what each agent looks for:

```python
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
        "checks": "silent failures (return None/[]), missing validation, security issues",
        "context_checks": {
            "api_routes": "Check input validation, error responses, authentication",
            "grpc_service": "Check error handling, request validation",
        }
    },
    # ... add your own agents ...
}
```

### Adding Custom Agents

1. Add agent ID to `AGENT_IDS` for desired tier
2. Add agent definition to `AGENT_DEFINITIONS`:

```python
AGENT_DEFINITIONS["security_checker"] = {
    "subagent_type": "general-purpose",
    "model": "sonnet",
    "checks": "SQL injection, XSS, CSRF, authentication bypass",
    "context_checks": {
        "api_routes": "Check auth middleware, rate limiting",
    }
}
```

## File Filtering

### Excluded Extensions

Skip review for these file types:

```python
EXCLUDED_EXTENSIONS = {
    ".json", ".md", ".txt",
    ".yml", ".yaml",
    ".toml", ".ini", ".cfg",
    ".lock", ".sum",
}
```

### Excluded Paths

Skip review for files matching these patterns (case-insensitive):

```python
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
    "/docs/examples/",
    "/scripts/temp/",
    "/scratch/",
]
```

**Pattern Matching:** Uses substring matching with forward slashes. Example: `/tests/fixtures/` matches `src/tests/fixtures/data.json`.

### Critical Patterns

Changes to these paths trigger deeper review regardless of size:

```python
CRITICAL_PATTERNS = [
    "/api/",
    "/routes/",
    "/models/",
    "_service.py",
    "/proto/",
    "/migrations/",
]
```

## Module Boundaries

### Setup

1. Create `.claude/review-config.json` in your project root
2. Define module rules (see example below)

### Configuration Format

```json
{
  "module_boundaries": {
    "module_name": {
      "allowed_imports": ["list", "of", "allowed", "modules"],
      "forbidden_imports": ["list", "of", "forbidden", "modules"],
      "communication": "Human-readable message when violation detected"
    }
  }
}
```

### Example: Microservices Architecture

```json
{
  "module_boundaries": {
    "api": {
      "allowed_imports": ["core", "shared"],
      "forbidden_imports": ["services", "internal"],
      "communication": "Must use gRPC to communicate with services"
    },
    "services": {
      "allowed_imports": ["core", "shared"],
      "forbidden_imports": ["api", "frontend"],
      "communication": "Use gRPC for inter-service communication"
    },
    "frontend": {
      "allowed_imports": ["shared"],
      "forbidden_imports": ["api", "services", "core"],
      "communication": "Must use REST API or WebSocket only"
    },
    "core": {
      "allowed_imports": [],
      "forbidden_imports": ["api", "services", "frontend"],
      "communication": "Core is a shared library - no dependencies on other modules"
    }
  }
}
```

### How It Works

The hook checks code changes for import statements:

```python
# ❌ VIOLATION: api/routes/users.py
from services.auth import AuthService  # Forbidden!

# ✅ OK: api/routes/users.py
from core.models import User  # Allowed
```

Violations are reported in review instructions with the `communication` message.

## Deep Review Auto-Fix

Control whether deep review failures automatically fix issues or stop and wait.

### Configuration

```python
# Deep review auto-fix threshold.
# "none"     = stop and wait for user (current behavior)
# "critical" = auto-fix critical only, report rest
# "high"     = auto-fix critical + high, report rest
# "medium"   = auto-fix critical + high + medium, report rest
# "all"      = auto-fix everything
DEEP_AUTO_FIX = "none"
```

### Environment Variable Override

```bash
export CLAUDE_HOOK_DEEP_AUTO_FIX="high"
```

### How It Works

When `DEEP_AUTO_FIX` is set to anything other than `"none"`:

1. Deep review failures filter issues by severity threshold
2. Qualifying issues (at or above the threshold) are passed to a subagent for fixing
3. Lower-severity issues are reported but not fixed
4. Claude resumes its prior task after the subagent completes

When `DEEP_AUTO_FIX = "none"` (default), deep review failures trigger the plan agent and stop for user review — the original behavior.

**Severity hierarchy:** critical > high > medium > low

**Example:** With `DEEP_AUTO_FIX = "high"`, a deep review finding 2 critical, 3 high, and 5 medium issues will auto-fix the 5 critical+high issues and report the 5 medium issues without fixing.

## Fix Strategy

### Non-Deep Tiers (Quick, Standard)

When review agents find violations, Claude spawns a single general-purpose subagent (model=sonnet) to fix all issues. This keeps fixes out of the main context, preserving Claude's train of thought.

### Deep Tier

Controlled by `DEEP_AUTO_FIX` (see above). Default behavior stops and waits for user review.

## Context Restoration

After a review completes and Claude is instructed to continue, the hook automatically extracts context from the transcript:

- **Last user request** (truncated to 200 chars)
- **Last 5 tool actions** (tool name + file path)

This context is appended to all continue/resume messages, helping Claude pick up where it left off instead of losing its train of thought.

## Advanced Options

### API Mode Settings

Only used when `REVIEW_MODE = "api"`:

```python
API_DIFF_THRESHOLD = 500  # chars - below uses Haiku, above uses Sonnet
```

### File Paths

State files are stored in `.claude/hooks/`:

```python
DEBUG_FILE = Path(__file__).parent / "stop-hook-debug.log"
METRICS_FILE = Path(__file__).parent / "stop-hook-metrics.jsonl"
# Session-specific files use hash suffix:
# stop-hook-state-{session_hash}.json
# review-results-{session_hash}.json  (file mode only)
```

**Note:** The `review-results-*.json` file is only created in file mode. In inline mode (default), results are embedded in Claude's response and extracted from the transcript.

### Integration Checker

The `integration_checker` agent is added automatically when:

1. Changes span 2+ top-level directories (e.g., `api/` and `services/`)
2. Changes touch 2+ critical patterns (e.g., `/proto/` and `/api/`)

This ensures cross-module consistency is checked.

### Context Detection

The hook automatically detects file contexts for specialized checks:

- `proto` - Protocol buffer files (`.proto`)
- `grpc_service` - gRPC service implementations (`_service.py`)
- `database` - Database models/migrations (`/models/`, `/migrations/`)
- `api_routes` - API route handlers (`/routes/`, `/api/`)
- `frontend` - Frontend files (`.tsx`, `.jsx` in `/frontend/`)

Agents use these contexts to apply specialized checks from `context_checks`.

## Configuration Best Practices

### Start Conservative

Begin with stricter settings and relax as needed:

```python
# Stricter thresholds
TIER_THRESHOLDS = {"skip": 200, "quick": 1000, "standard": 5000}
MAX_AUTO_CONTINUES = 1  # Require approval more often
```

### Project Size Matters

Adjust thresholds based on project size:

**Small projects (<10K LOC):**
```python
TIER_THRESHOLDS = {"skip": 1000, "quick": 5000, "standard": 20000}
MAX_AUTO_CONTINUES = 5
```

**Large projects (>100K LOC):**
```python
TIER_THRESHOLDS = {"skip": 300, "quick": 2000, "standard": 10000}
MAX_AUTO_CONTINUES = 2
```

### Critical Projects

For production systems requiring strict review:

```python
# No skip tier - always review
TIER_THRESHOLDS = {"skip": 0, "quick": 1000, "standard": 5000}
MAX_AUTO_CONTINUES = 1
MAX_FAIL_RETRIES = 1

# Add all agents to quick tier
AGENT_IDS["quick"] = ["explore_haiku", "general_haiku", "bug_hunter"]
```

### Fast Iteration

For rapid prototyping/experimentation:

```python
# Lenient thresholds
TIER_THRESHOLDS = {"skip": 2000, "quick": 10000, "standard": 50000}
MAX_AUTO_CONTINUES = 10

# Only use fast agents
AGENT_IDS["standard"] = ["explore_haiku", "general_haiku"]
AGENT_IDS["deep"] = ["explore_haiku", "general_haiku"]
```

## Debugging Configuration

### Enable Verbose Logging

Check `.claude/hooks/stop-hook-debug.log` for detailed execution logs:

```bash
tail -f .claude/hooks/stop-hook-debug.log
```

### Test Configuration

Manually trigger the hook with a test transcript:

```bash
echo '{"transcript_path": "/path/to/transcript.jsonl"}' | python .claude/hooks/stop-design-audit.py
```

### View Current State

Check session state:

```bash
cat .claude/hooks/stop-hook-state.json | jq
```

### Reset State

Start fresh by deleting state files:

```bash
# Delete session state files
rm .claude/hooks/stop-hook-state-*.json

# Delete results files (file mode only)
rm .claude/hooks/review-results-*.json
```

**Note:** In inline mode (default), review results are embedded in the transcript, not stored in separate files.

## Environment Variables

| Variable | Values | Description |
|----------|--------|-------------|
| `CLAUDE_HOOK_FORCE_TIER` | `deep`, `standard`, `quick` | Force a specific review tier (bypass threshold checks) |
| `CLAUDE_HOOK_SKIP` | `1` | Temporarily disable the hook (snooze) |
| `CLAUDE_HOOK_DEEP_AUTO_FIX` | `none`, `critical`, `high`, `medium`, `all` | Override deep review auto-fix threshold |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | API key for API mode |

### API Mode

Set your Anthropic API key for API mode:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Next Steps

- See [README.md](README.md) for feature overview
- Check [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common issues
- Review [EXAMPLES.md](EXAMPLES.md) for configuration examples
