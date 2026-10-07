# Setup Guide

This guide provides structured steps for installing Claude on Rails Review 3.0.0 into a target project. It works for both Claude (automated) and human installers.

## Prerequisites

- Python 3.10+
- Claude Code CLI
- A project with a `.git` or `.claude` directory

## Step 1: Analyze the Target Project

Before installing, understand the project:

1. **Language and framework** - Read `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, etc.
2. **Directory structure** - List top-level directories to identify modules, services, layers
3. **Existing Claude config** - Check if `.claude/settings.json` exists (the hook entry will need merging)
4. **Critical paths** - Identify where core business logic, API routes, auth and data models live
5. **Project invariants** - Note the rules a reviewer should enforce (units, provenance, error handling, service boundaries); they go into a review checklist (Step 4)

## Step 2: Choose a Preset

| Preset | When to use | Skip threshold | Auto-continues | Deep auto-fix | Reviewer model |
|--------|-------------|---------------|----------------|---------------|----------------|
| `strict` | Production, security-critical, compliance | 0 chars (review everything) | 1 | none | opus |
| `balanced` | Most projects (default) | 500 chars | 3 | high | opus |
| `relaxed` | Active development, frontend, small teams | 1000 chars | 5 | high | opus |
| `minimal` | Prototyping, solo dev, experiments | 3000 chars | 10 | all | sonnet |

**Decision tree:**
- Is this production or security-critical? Use `strict`
- Is this rapid prototyping or a personal project? Use `minimal`
- Is this active development with frequent iteration? Use `relaxed`
- Otherwise use `balanced`

All presets use ONE reviewer at every tier; the tier scales the reviewer's effort, not the number of agents.

## Step 3: Run Installation

From the target project's root directory:

```bash
bash /path/to/claude-on-rails-review/install.sh --non-interactive --preset=balanced
```

This will:
- Copy the hook shim and package to `.claude/hooks/`
- Create or merge `.claude/settings.json` with the hook entry
- Write `.claude/hooks/hook-overrides.json` with the chosen preset
- Update `.gitignore`

Then make sure the runtime directory is git-ignored. All runtime files (state, prompts, results, debug log, metrics) live in `.claude/hooks/state/`:

```
.claude/hooks/state/
```

If the installer added the older per-file patterns (`.claude/hooks/stop-hook-state-*.json` and similar), they are harmless but no longer match anything; the line above is the one that matters.

Optionally allow the reviewer to write its results file without a permission prompt, in `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/state/review-results-*.json)"]
  }
}
```

## Step 4: Customize hook-overrides.json

After installation, edit `.claude/hooks/hook-overrides.json` to add project-specific configuration. The preset provides sensible defaults; add only what is specific to this project. The file is validated on every run: unknown keys, wrong types and bad values are fatal and visible, not ignored.

### Review Checklist

The reviewer uses a built-in generic checklist unless you provide your own. A project checklist file **replaces** the default, so include any generic items you still want:

```json
{
    "preset": "balanced",
    "review_checklist_file": "review-checklist.md"
}
```

The path is relative to `.claude/hooks/` (or absolute). Put project rules in it, for example:

```markdown
1. Silent failures - no swallowed exceptions, no default returned on failure.
2. All scalar values carry units; no implicit unit conversion.
3. Every output carries a config fingerprint and version.
4. No cross-service imports; services talk through their gRPC interfaces.
5. Every bug fix includes a regression test.
```

### Models

```json
{
    "reviewer_model": "opus",
    "fixer_model": "sonnet"
}
```

### Critical Patterns

Paths whose combined changes should make the reviewer check cross-module callers and contracts. Use `+critical_patterns` to append:

```json
{
    "+critical_patterns": ["/api/", "/auth/", "/models/", "/migrations/"]
}
```

Think about API routes, authentication and authorization code, database models and migrations, core business logic, and the public API surface (exports, `__init__.py`, `index.ts`).

### Excluded Paths

Paths to skip during review. Use `+excluded_paths` to append (patterns are matched lower-case):

```json
{
    "+excluded_paths": ["/docs/", "/scripts/", "/examples/"]
}
```

### Custom Agents (optional)

The default is the single `reviewer` agent. For a domain-specific second opinion, define an agent and add it next to `reviewer`:

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
        "deep": ["reviewer", "security_checker"]
    }
}
```

Every agent in `agent_ids` must be `reviewer` or defined in `extra_agent_definitions`, and every definition needs `subagent_type`, `model` and `checks`. Prefer improving the checklist before adding agents.

### Module Boundaries (optional)

If the project has clear architectural layers, create `.claude/review-config.json`:

```json
{
    "module_boundaries": {
        "api": {
            "forbidden_imports": ["services", "internal"],
            "communication": "Must use gRPC to communicate with services"
        },
        "services": {
            "forbidden_imports": ["api", "frontend"],
            "communication": "Use message queue for async, gRPC for sync"
        }
    }
}
```

## Step 5: Verify

Confirm the hook loads and the config is valid:

```bash
echo '{}' | python .claude/hooks/stop-design-audit.py
```

Expected output is a JSON line with a `systemMessage` saying `hook error — code review did NOT run: ValueError: hook input has no transcript_path`. That is the hook failing loud on empty input, and it proves the package imports and `hook-overrides.json` is valid.

If the message instead starts with `invalid config:`, it lists every problem in `hook-overrides.json` or the `CLAUDE_HOOK_*` variables; fix them and run again. An `ImportError` or `SyntaxError` means the package did not copy correctly.

## All Available Override Keys

Presets set sensible defaults; override only what you need. See [CONFIGURATION.md](CONFIGURATION.md) for details and validation rules.

| Key | Type | Default (balanced) | Description |
|-----|------|--------------------|-------------|
| `preset` | string | none | Base preset: `strict`, `balanced`, `relaxed`, `minimal` |
| `review_mode` | string | `"subagent"` | `subagent`, `agent`, `delegated`, `api` |
| `reviewer_model` | string | `"opus"` | Model of the `reviewer` agent |
| `fixer_model` | string | `"sonnet"` | Model of the fix agent |
| `review_checklist_file` | string | `""` | Project checklist (relative to hooks dir, or absolute); replaces the built-in one |
| `deep_auto_fix` | string | `"high"` | `none`, `critical`, `high`, `medium`, `all` |
| `tier_thresholds` | object | `{"skip":500,"quick":5000,"standard":20000}` | Characters per tier; must define all three |
| `tier_file_limits` | object | `{"skip":1,"quick":3,"standard":6}` | Files per tier; must define all three |
| `max_auto_continues` | int | `3` | Passing reviews before Claude may stop |
| `max_fail_retries` | int | `3` | Failed reviews before handing off to the user |
| `max_review_attempts` | int | `2` | Attempts per round to get valid results |
| `state_expiry` | int | `3600` | Session state TTL in seconds |
| `critical_patterns` / `+critical_patterns` | list | `[]` | Replace / append |
| `excluded_paths` / `+excluded_paths` | list | see config.py | Replace / append |
| `excluded_extensions` / `+excluded_extensions` | list | see config.py | Replace / append |
| `excluded_filenames` / `+excluded_filenames` | list | see config.py | Replace / append |
| `agent_ids` | object | `reviewer` for each tier | Agent IDs per tier (merged per tier) |
| `extra_agent_definitions` | object | `{}` | Custom agent definitions |
| `_doc`, `_comment` | any | none | Free-form notes, ignored |

Mode-specific keys (`results_mode`, `delegated_timeout`, `api_diff_threshold`) apply to the `agent`, `delegated` and `api` modes only.

**Removed in 3.0.0:** `subagent_timeout`. The old agent ids (`explore_haiku`, `general_haiku`, `bug_hunter`, `integration_checker`, `general_opus`) no longer exist. Both are errors if left in `hook-overrides.json`.

**Merge order:** defaults, preset, explicit overrides, environment variables.

## Example: Python Web API

```json
{
    "preset": "balanced",
    "review_checklist_file": "review-checklist.md",
    "+critical_patterns": ["/api/routes/", "/api/middleware/", "/models/", "/migrations/"],
    "+excluded_paths": ["/tests/fixtures/", "/docs/", "/scripts/"],
    "extra_agent_definitions": {
        "api_validator": {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "checks": "missing input validation, unhandled error responses, auth middleware gaps",
            "context_checks": {
                "api_routes": "Check all endpoints have auth and rate limiting"
            }
        }
    },
    "agent_ids": {
        "deep": ["reviewer", "api_validator"]
    }
}
```

## Example: TypeScript Monorepo

```json
{
    "preset": "relaxed",
    "reviewer_model": "sonnet",
    "+critical_patterns": ["/packages/core/", "/packages/api/"],
    "+excluded_paths": ["/packages/docs/", "/packages/storybook/"],
    "+excluded_extensions": [".css", ".scss"]
}
```
