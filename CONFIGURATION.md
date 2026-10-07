# Configuration Guide

Complete configuration reference for Claude on Rails Review 3.0.0.

## Table of Contents

- [Where Configuration Lives](#where-configuration-lives)
- [Validation (Fail Loud)](#validation-fail-loud)
- [Key Reference](#key-reference)
- [Presets](#presets)
- [Tier Configuration](#tier-configuration)
- [Reviewer and Models](#reviewer-and-models)
- [Review Checklist](#review-checklist)
- [Custom Agents](#custom-agents)
- [Fix Strategy and Deep Auto-Fix](#fix-strategy-and-deep-auto-fix)
- [Loop Bounds](#loop-bounds)
- [File Filtering](#file-filtering)
- [Critical Patterns](#critical-patterns)
- [Module Boundaries](#module-boundaries)
- [Environment Variables](#environment-variables)
- [Runtime Files](#runtime-files)
- [Results Contract](#results-contract)
- [Other Review Modes](#other-review-modes)
- [Debugging](#debugging)
- [Best Practices](#best-practices)

## Where Configuration Lives

| File | Location | Purpose |
|------|----------|---------|
| `settings.json` | `.claude/` | Registers the hook with Claude Code |
| `hook-overrides.json` | `.claude/hooks/` | Project overrides (see [`hook-overrides.example.json`](hook-overrides.example.json)) |
| checklist file | `.claude/hooks/` (or absolute path) | Optional project review checklist |
| `review-config.json` | `.claude/` | Module boundaries and optional `force_tier` |
| `state/` | `.claude/hooks/state/` | Everything the hook writes (git-ignore it) |

Defaults are in [`stop_design_audit/config.py`](stop_design_audit/config.py). Prefer `hook-overrides.json` over editing the package, so upgrades are a plain copy.

Register the hook in `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python .claude/hooks/stop-design-audit.py",
        "timeout": 30
      }]
    }]
  }
}
```

**Merge order:** defaults, then `preset`, then explicit keys in `hook-overrides.json`, then environment variables.

## Validation (Fail Loud)

`hook-overrides.json` and the `CLAUDE_HOOK_*` environment variables are validated on every run. Every problem found is listed, and nothing from the file is applied if any key is invalid. The hook then fails loud: the user sees a `systemMessage` and Claude is blocked once to relay it (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#hook-error---code-review-did-not-run)).

These are errors:

- invalid JSON, or a top level that is not an object
- an unknown key (including keys removed in 3.0.0 such as `subagent_timeout`)
- a wrong type, a negative number, or a boolean where an integer is expected
- an invalid enum value for `review_mode`, `results_mode` or `deep_auto_fix`
- an empty `reviewer_model` or `fixer_model`
- `tier_thresholds` / `tier_file_limits` that do not define exactly `skip`, `quick` and `standard`
- `agent_ids` with an unknown tier (valid: `quick`, `standard`, `deep`) or an empty list
- an unknown `preset`
- `+key` append syntax on a key that is not a list
- a `review_checklist_file` that does not exist
- an agent in `agent_ids` that is not defined, or an `extra_agent_definitions` entry missing `subagent_type`, `model` or `checks`
- an invalid `CLAUDE_HOOK_*` value, an invalid `.claude/review-config.json`, or an invalid `force_tier`

The only keys that are not configuration values are `preset`, `extra_agent_definitions`, `_doc` and `_comment`. Use `_doc` or `_comment` for notes.

## Key Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `preset` | string | none | `strict`, `balanced`, `relaxed`, `minimal` |
| `review_mode` | string | `"subagent"` | `subagent`, `agent`, `delegated`, `api` |
| `reviewer_model` | string | `"opus"` | Model of the built-in `reviewer` agent |
| `fixer_model` | string | `"sonnet"` | Model of the fix agent |
| `review_checklist_file` | string | `""` | Project checklist replacing the built-in one |
| `deep_auto_fix` | string | `"high"` | `none`, `critical`, `high`, `medium`, `all` |
| `tier_thresholds` | object | `{"skip":500,"quick":5000,"standard":20000}` | Characters per tier (replaced as a whole) |
| `tier_file_limits` | object | `{"skip":1,"quick":3,"standard":6}` | Files per tier (replaced as a whole) |
| `max_auto_continues` | int | `3` | Passing reviews before Claude may stop |
| `max_fail_retries` | int | `3` | Failed reviews before the retry circuit breaker |
| `max_review_attempts` | int | `2` | Attempts per round to obtain valid results (first request plus re-requests) |
| `state_expiry` | int | `3600` | Seconds before session state counts as stale |
| `critical_patterns` | list | `[]` | Path substrings; see [Critical Patterns](#critical-patterns) |
| `excluded_extensions` | list | see [File Filtering](#file-filtering) | File extensions to skip |
| `excluded_filenames` | list | see [File Filtering](#file-filtering) | Exact file names to skip |
| `excluded_paths` | list | see [File Filtering](#file-filtering) | Path substrings to skip |
| `+critical_patterns`, `+excluded_extensions`, `+excluded_filenames`, `+excluded_paths` | list | none | Append to the current value instead of replacing it |
| `agent_ids` | object | `{"quick":["reviewer"],"standard":["reviewer"],"deep":["reviewer"]}` | Agents per tier (merged per tier) |
| `extra_agent_definitions` | object | `{}` | Custom agent definitions |
| `results_mode` | string | `"inline"` | `agent`/`delegated` modes only |
| `delegated_timeout` | int | `300` | `delegated` mode only |
| `api_diff_threshold` | int | `500` | `api` mode only |

All integers must be non-negative. All lists must contain strings only.

## Presets

A preset is a bundle of keys applied before your explicit keys.

| Preset | skip / quick / standard (chars) | File limits | `max_auto_continues` | `deep_auto_fix` | Other |
|--------|-------------------------------|-------------|----------------------|-----------------|-------|
| `strict` | 0 / 500 / 3000 | 0 / 1 / 3 | 1 | `none` | |
| `balanced` | 500 / 5000 / 20000 | 1 / 3 / 6 | 3 | `high` | the defaults |
| `relaxed` | 1000 / 5000 / 20000 | 3 / 5 / 10 | 5 | `high` | |
| `minimal` | 3000 / 10000 / 50000 | 5 / 10 / 20 | 10 | `all` | `reviewer_model` = `sonnet` |

With `strict`, `skip` is `0`, so no change is small enough to skip.

## Tier Configuration

The hook classifies the incremental change (since the last hook firing) by size. A tier applies when the change is under its character threshold AND within its file limit; the checks run in the order skip, quick, standard; anything larger is deep.

```json
{
  "tier_thresholds": {"skip": 500, "quick": 5000, "standard": 20000},
  "tier_file_limits": {"skip": 1, "quick": 3, "standard": 6}
}
```

**Example:** 400 characters across 2 files exceeds the skip file limit, so it is a quick review.

### What a Tier Changes

Every tier runs the same single reviewer. The tier sets the effort the reviewer is told to apply:

| Tier | Effort given to the reviewer |
|------|------------------------------|
| quick | Focused pass: the changed lines and their direct callers/callees |
| standard | Read every changed file in full; check direct callers/callees and tests |
| deep | Exhaustive: read every changed file in full, trace cross-module dependencies, verify contracts and signatures at every call site, check edge cases, ordering and side effects |

Tiers also differ in how failures are handled; see [Fix Strategy](#fix-strategy-and-deep-auto-fix).

## Reviewer and Models

The built-in agent id is `reviewer` (`subagent_type` `general-purpose`). Its model comes from `reviewer_model`; the fix agent's model comes from `fixer_model`:

```json
{
  "reviewer_model": "opus",
  "fixer_model": "sonnet"
}
```

Use a cheaper `reviewer_model` (for example `sonnet`) for speed or cost; the `minimal` preset does this. Any non-empty string is accepted and passed to the Agent tool as `model`; the hook does not check that the model exists.

The five agents of earlier versions (`explore_haiku`, `general_haiku`, `bug_hunter`, `integration_checker`, `general_opus`) no longer exist. Remove them from `agent_ids`; they are now an error.

## Review Checklist

The reviewer is given a checklist. The built-in default covers:

1. Silent failures - swallowed exceptions, defaults returned on failure, execution continuing after a validation error
2. Correctness - null access, off-by-one, race conditions, resource leaks, unhandled edge cases
3. Contracts and integration - changed signatures/schemas with callers not updated, cross-module import violations
4. Security - injection, missing authn/authz checks, secrets in code or logs
5. Hardcoding - values that belong in config, a registry or a schema
6. Tests - changed behaviour without coverage; bug fixes without a regression test

To use your own checklist, point `review_checklist_file` at a text or markdown file. The path is relative to the hooks directory (`.claude/hooks/`) or absolute:

```json
{ "review_checklist_file": "review-checklist.md" }
```

The file **replaces** the default checklist entirely. Include the generic items in your file if you still want them. A missing file is a config error. The file's SHA-256 is part of the config fingerprint, so editing it changes the fingerprint in metrics.

The checklist applies to the `reviewer` agent only. Custom agents use their own `checks` text.

## Custom Agents

The default is one reviewer. To run additional or different reviewers, define them and list them per tier:

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

Rules:

- Each definition needs `subagent_type`, `model` and `checks` (non-empty). `context_checks` is optional.
- `agent_ids` may only reference defined agents: `reviewer` or an id from `extra_agent_definitions`. Anything else is a config error.
- `agent_ids` is merged per tier. Setting `deep` replaces the deep list and leaves `quick` and `standard` alone. Include `"reviewer"` if you still want it.
- A custom agent is reviewed against its own `checks` text, not the checklist file. It only gets the context hints you define in its own `context_checks`; the built-in `reviewer` has all of them.
- All agents of a round are started in one message, in the foreground. Each gets its own prompt and results file. The round fails if any agent reports `fail`.

Context names available to `context_checks`: `proto` (`.proto` files), `grpc_service` (`_service.py`), `database` (`/models/`, `/migrations/`), `api_routes` (`/routes/`, `/api/`), `frontend` (`.ts`/`.tsx`/`.js`/`.jsx` under `/frontend/`).

## Fix Strategy and Deep Auto-Fix

When a reviewer reports `fail`:

| Tier | Behavior |
|------|----------|
| quick, standard | ALL issues go to ONE fix agent (`general-purpose`, model `fixer_model`, foreground) |
| deep | `deep_auto_fix` decides which severities are fixed: `critical`, `high`, `medium` (each includes the more severe levels), `all`, or `none` |

With `deep_auto_fix` set to `none`, deep failures are reported to the user and Claude does not fix them unless asked. Lower-severity issues below the threshold are reported without fixing. The environment variable `CLAUDE_HOOK_DEEP_AUTO_FIX` overrides the setting.

**Example:** with `deep_auto_fix` = `high`, a deep review finding 2 critical, 3 high and 5 medium issues sends the 5 critical and high issues to the fix agent and reports the 5 medium ones.

**Fixes are not re-reviewed.** The hook only sees edits made in the main session transcript; edits by a subagent are invisible to it. The failure message says so.

## Loop Bounds

A blocking Stop hook must always terminate. These bounds are unchanged in 3.0.0:

| Key | Default | Meaning |
|-----|---------|---------|
| `max_auto_continues` | 3 | After this many passes the hook allows the stop. Message on a pass: `Design audit passed. [Auto-continue N of 3] Continue.` |
| `max_fail_retries` | 3 | Failed reviews before the hook reports "retries exhausted" and hands off to the user |
| `max_review_attempts` | 2 | Attempts to get valid results for one round. With 2: the first request plus one re-request, then an UNREVIEWED warning |

## File Filtering

A file is ignored when its name, extension (case-insensitive) or path (lower-cased, forward slashes, substring match) matches. Defaults:

- `excluded_extensions`: `.json .md .txt .yml .yaml .toml .ini .cfg .lock .sum`
- `excluded_filenames`: `LICENSE LICENCE Makefile Dockerfile Procfile Gemfile Rakefile Vagrantfile Brewfile .gitignore .gitattributes .dockerignore .editorconfig`
- `excluded_paths`: `/tests/fixtures/ /test/fixtures/ /__pycache__/ /.pytest_cache/ /node_modules/ /.venv/ /venv/ /build/ /dist/ /.git/ /coverage/ /.coverage /htmlcov/`

Use the plain key to replace a list and the `+` form to extend it:

```json
{
  "+excluded_paths": ["/docs/", "/scripts/"],
  "+excluded_extensions": [".css", ".scss"]
}
```

Write path patterns in lower case; they are matched against the lower-cased path.

## Critical Patterns

`critical_patterns` is a list of path substrings, empty by default. A change that touches files matching **two or more different patterns** is treated as cross-module: the reviewer is told which critical paths were touched and to check callers and contracts across those boundaries. The same hint is added when the change spans two or more top-level directories. Critical patterns do not force a tier and do not add agents.

```json
{ "+critical_patterns": ["/api/", "/models/", "/migrations/"] }
```

## Module Boundaries

Create `.claude/review-config.json` (one level above the hooks directory):

```json
{
  "force_tier": "deep",
  "module_boundaries": {
    "api": {
      "forbidden_imports": ["services", "internal"],
      "communication": "Must use gRPC to communicate with services"
    },
    "core": {
      "forbidden_imports": ["api", "services", "frontend"],
      "communication": "Core is a shared library - no dependencies on other modules"
    }
  }
}
```

- The module of a file is its first path segment (`api/routes/users.py` is module `api`; a file at the project root has no module).
- The hook looks for `from <forbidden>` and `import <forbidden>` in the code preview of the change. Matches are given to the reviewer as extra focus together with the `communication` text.
- Only `forbidden_imports` and `communication` are enforced. `allowed_imports` is accepted but not used.
- `force_tier` (optional) must be `quick`, `standard` or `deep`; anything else is a config error. It beats `CLAUDE_HOOK_FORCE_TIER`.

See [`review-config.example.json`](review-config.example.json).

## Environment Variables

| Variable | Values | Description |
|----------|--------|-------------|
| `CLAUDE_HOOK_SKIP` | `0`, `1` | `1` disables the hook for this run |
| `CLAUDE_HOOK_FORCE_TIER` | `quick`, `standard`, `deep` | Force a tier, bypassing thresholds |
| `CLAUDE_HOOK_DEEP_AUTO_FIX` | `none`, `critical`, `high`, `medium`, `all` | Override `deep_auto_fix` |
| `CLAUDE_HOOK_REVIEW_MODE` | `subagent`, `agent`, `delegated`, `api` | Override `review_mode` |
| `ANTHROPIC_API_KEY` | key | Only for `api` mode |

Values are validated before anything else runs, so a typo is a visible error even with `CLAUDE_HOOK_SKIP=1`.

## Runtime Files

Everything the hook writes lives in `<hooks_dir>/state/` (normally `.claude/hooks/state/`). Add it to `.gitignore`:

```
.claude/hooks/state/
```

| File | Contents |
|------|----------|
| `stop-hook-state-<session>.json` | Counters, current round, pending review |
| `review-prompt-<session>-<round>-<agent>.md` | Prompt the reviewer reads |
| `review-results-<session>-<round>-<agent>.json` | Results the reviewer writes |
| `stop-hook-debug.log` | Debug log, truncated when above 1 MB |
| `stop-hook-metrics.jsonl` | One record per review round; the last 5000 lines are kept |

`<session>` is the first 12 hex characters of an MD5 of the transcript path; `<round>` is an 8-character id. Files older than 24 hours are removed. `agent` and `delegated` modes also write `review-results-<session>.json` and `coordinator-instructions-<session>.json` here.

Every metrics record contains `version`, `config_fingerprint` (SHA-256 over the effective config, the hook version and the checklist file contents), `tier`, `diff_chars`, `file_count`, `agents`, `outcome`, `fail_count`, `timestamp` and `session_id` (the first 8 characters of the session hash).

The reviewer needs to write its results file. To avoid a permission prompt add to `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/state/review-results-*.json)"]
  }
}
```

## Results Contract

The reviewer writes one JSON file per (session, round, agent):

```json
{
  "round_id": "a3f8d921",
  "agent_id": "reviewer",
  "status": "fail",
  "issues": [
    {
      "file": "api/routes/users.py",
      "line": 42,
      "severity": "high",
      "category": "silent-failure",
      "description": "get_user returns None on DB error, so the caller renders an empty profile"
    }
  ]
}
```

The hook accepts a file only if all of these hold, with no repair and no partial acceptance:

- valid JSON object
- `round_id` and `agent_id` equal the expected values
- `status` is `"pass"` or `"fail"`
- `issues` is a list; each issue has `file` (string), `line` (integer or `null`), `severity` (`critical`, `high`, `medium` or `low`), `category` (string), `description` (string)
- `status` is `"fail"` if there is any `critical` issue or two or more `high` issues

A missing file, or one that breaks the contract, is re-requested once; if it still fails the user sees an UNREVIEWED warning. The results are read at the next stop before any diff check, because a foreground review adds no edits to the main transcript.

## Other Review Modes

`review_mode` also accepts `agent` (inline instructions), `delegated` (background coordinator) and `api` (direct Anthropic API, needs `ANTHROPIC_API_KEY`). These remain available and use `results_mode`, `delegated_timeout` and `api_diff_threshold`. They are not the recommended path and are not covered further here; the 3.0.0 reviewer flow, results files and `reviewer_model`/`fixer_model` settings apply to `subagent` mode.

## Debugging

```bash
# Watch the debug log
tail -f .claude/hooks/state/stop-hook-debug.log

# Inspect session state
jq . .claude/hooks/state/stop-hook-state-*.json

# Reset state
rm .claude/hooks/state/stop-hook-state-*.json
```

Run the hook by hand with a transcript:

```bash
echo '{"transcript_path": "/path/to/transcript.jsonl"}' | python .claude/hooks/stop-design-audit.py
```

Run from the project root; an empty `{}` input fails loud ("hook input has no transcript_path"), which also confirms the package imports.

## Best Practices

**Start conservative, relax with evidence.** Begin with `strict` or tighter thresholds and use the metrics file to loosen them.

```json
{ "preset": "strict" }
```

**Production systems:** no skip tier, one auto-continue, fail fast.

```json
{
  "tier_thresholds": {"skip": 0, "quick": 1000, "standard": 5000},
  "tier_file_limits": {"skip": 0, "quick": 2, "standard": 4},
  "max_auto_continues": 1,
  "max_fail_retries": 1,
  "deep_auto_fix": "none"
}
```

**Rapid iteration:** `{ "preset": "minimal" }`.

**Review quality comes from the checklist.** Put your project's real invariants in `review_checklist_file` instead of adding agents.

## Next Steps

- [README.md](README.md) - feature overview
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - common issues
- [EXAMPLES.md](EXAMPLES.md) - configuration examples
