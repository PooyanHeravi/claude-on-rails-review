# Setup Guide

This guide provides structured steps for installing Claude on Rails Review into a target project. It works for both Claude (automated) and human installers.

## Prerequisites

- Python 3.10+
- Claude Code CLI
- A project with a `.git` or `.claude` directory

## Step 1: Analyze the Target Project

Before installing, understand the project:

1. **Language & framework** — Read `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, etc.
2. **Directory structure** — List top-level directories to identify modules, services, layers
3. **Existing Claude config** — Check if `.claude/settings.json` exists (hook entry will need merging)
4. **Critical paths** — Identify where core business logic, API routes, auth, and data models live

## Step 2: Choose a Preset

Pick the preset that matches the project's needs:

| Preset | When to use | Skip threshold | Auto-continues | Auto-fix |
|--------|-------------|---------------|----------------|----------|
| `strict` | Production, security-critical, compliance | 0 chars (review everything) | 1 | none |
| `balanced` | Most projects (default) | 500 chars | 3 | high |
| `relaxed` | Active development, frontend, small teams | 1000 chars | 5 | high |
| `minimal` | Prototyping, solo dev, experiments | 3000 chars | 10 | all |

**Decision tree:**
- Is this production or security-critical? → `strict`
- Is this rapid prototyping or a personal project? → `minimal`
- Is this active development with frequent iteration? → `relaxed`
- Otherwise → `balanced`

## Step 3: Run Installation

From the target project's root directory:

```bash
bash /path/to/claude-on-rails-review/install.sh --non-interactive --preset=balanced
```

This will:
- Copy the hook shim and package to `.claude/hooks/`
- Create or merge `.claude/settings.json` with the hook entry
- Write `.claude/hooks/hook-overrides.json` with the chosen preset
- Update `.gitignore` with state file patterns

## Step 4: Customize hook-overrides.json

After installation, edit `.claude/hooks/hook-overrides.json` to add project-specific configuration. The preset provides sensible defaults; add only what's specific to this project.

### Critical Patterns

Paths that should always trigger deeper review. Use `+critical_patterns` to append to preset defaults:

```json
{
    "preset": "balanced",
    "+critical_patterns": ["/api/", "/auth/", "/models/", "/migrations/"]
}
```

Think about:
- API routes and handlers
- Authentication and authorization code
- Database models and migrations
- Core business logic
- Public API surface (exports, `__init__.py`, `index.ts`)

### Excluded Paths

Paths to skip during review. Use `+excluded_paths` to append:

```json
{
    "+excluded_paths": ["/docs/", "/scripts/", "/examples/"]
}
```

### Custom Agents (optional)

For domain-specific review concerns, add custom agents:

```json
{
    "extra_agent_definitions": {
        "security_checker": {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "checks": "OWASP top 10, input sanitization, auth bypass vectors",
            "context_checks": {
                "api_routes": "Check for missing auth middleware, rate limiting"
            }
        }
    },
    "agent_ids": {
        "deep": ["explore_haiku", "general_haiku", "bug_hunter", "security_checker", "general_sonnet"]
    }
}
```

Custom agent examples by domain:
- **Scientific computing** — unit conversion errors, numerical stability, provenance tracking
- **Financial** — rounding errors, currency handling, audit trail completeness
- **Protocol buffers** — field numbering, backward compatibility, service contract changes
- **Frontend** — accessibility, SSR hydration mismatches, bundle size impact

### Module Boundaries (optional)

If the project has clear architectural layers, create `.claude/review-config.json`:

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
            "communication": "Use message queue for async, gRPC for sync"
        }
    }
}
```

## Step 5: Verify

Confirm the hook loads without Python errors:

```bash
echo '{}' | python .claude/hooks/stop-design-audit.py
```

Expected: exits cleanly (may print a debug message about missing transcript_path). Any `ImportError` or `SyntaxError` means the package didn't copy correctly.

## All Available Override Keys

These can all be set in `hook-overrides.json`. Presets set sensible defaults; override only what you need.

| Key | Type | Default (balanced) | Description |
|-----|------|--------------------|-------------|
| `preset` | string | — | Base preset: `strict`, `balanced`, `relaxed`, `minimal` |
| `review_mode` | string | `"subagent"` | Review engine: `subagent`, `agent`, `delegated`, `api` |
| `results_mode` | string | `"inline"` | Where results go: `inline` or `file` |
| `deep_auto_fix` | string | `"high"` | Auto-fix threshold: `none`, `critical`, `high`, `medium`, `all` |
| `tier_thresholds` | object | `{"skip":500,"quick":5000,"standard":20000}` | Char count thresholds per tier |
| `tier_file_limits` | object | `{"skip":1,"quick":3,"standard":6}` | File count limits per tier |
| `max_auto_continues` | int | `3` | Successful reviews before requiring manual approval |
| `max_fail_retries` | int | `3` | Retry rounds when agents find issues |
| `max_review_attempts` | int | `2` | Max review attempts per round |
| `state_expiry` | int | `3600` | Session state TTL in seconds |
| `subagent_timeout` | int | `300` | Subagent timeout in seconds |
| `critical_patterns` | list | `[]` | Paths that trigger deeper review (replace) |
| `+critical_patterns` | list | — | Append to current critical patterns |
| `excluded_paths` | list | (see config.py) | Paths to skip (replace) |
| `+excluded_paths` | list | — | Append to current excluded paths |
| `excluded_extensions` | list | (see config.py) | File extensions to skip (replace) |
| `+excluded_extensions` | list | — | Append to current excluded extensions |
| `agent_ids` | object | (see config.py) | Agent IDs per tier (merged per-tier) |
| `extra_agent_definitions` | object | `{}` | Custom agent definitions |

**Merge order:** hardcoded defaults → preset → explicit overrides → environment variables

## Example: Python Web API

```json
{
    "preset": "balanced",
    "+critical_patterns": ["/api/routes/", "/api/middleware/", "/models/", "/migrations/"],
    "+excluded_paths": ["/tests/fixtures/", "/docs/", "/scripts/"],
    "extra_agent_definitions": {
        "api_validator": {
            "subagent_type": "general-purpose",
            "model": "haiku",
            "checks": "missing input validation, unhandled error responses, auth middleware gaps",
            "context_checks": {
                "api_routes": "Check all endpoints have auth and rate limiting"
            }
        }
    },
    "agent_ids": {
        "standard": ["explore_haiku", "general_haiku", "api_validator"],
        "deep": ["explore_haiku", "general_haiku", "bug_hunter", "api_validator", "general_sonnet"]
    }
}
```

## Example: TypeScript Monorepo

```json
{
    "preset": "relaxed",
    "+critical_patterns": ["/packages/core/", "/packages/api/"],
    "+excluded_paths": ["/packages/docs/", "/packages/storybook/"],
    "+excluded_extensions": [".css", ".scss"],
    "extra_agent_definitions": {
        "type_checker": {
            "subagent_type": "general-purpose",
            "model": "haiku",
            "checks": "unsafe type assertions (as any), missing null checks, incorrect generic constraints"
        }
    },
    "agent_ids": {
        "standard": ["explore_haiku", "general_haiku", "type_checker"],
        "deep": ["explore_haiku", "general_haiku", "bug_hunter", "type_checker", "general_sonnet"]
    }
}
```
