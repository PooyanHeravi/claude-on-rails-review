# Changelog

All notable changes to Claude on Rails Review will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## 3.0.0 — 2026-10-07

### Breaking
- **One reviewer for every tier.** The default review is a single agent, `reviewer` (model from `reviewer_model`, default `opus`), at quick, standard and deep. The tier scales the reviewer's effort, not the agent count. The five built-in agents (`explore_haiku`, `general_haiku`, `bug_hunter`, `integration_checker`, `general_opus`) are removed, and so is the automatic `integration_checker`: cross-module context (changes spanning 2+ top-level directories or 2+ `critical_patterns`) is now passed to the reviewer as extra focus. Custom agents are still possible through `extra_agent_definitions` plus `agent_ids`; each definition needs `subagent_type`, `model` and `checks`, and `agent_ids` may only reference defined agents (`reviewer` or your own).
- **`subagent_timeout` is removed.** The reviewer runs in the foreground, so there is nothing to time out. The key is now an unknown-key error.
- **Subagent mode no longer uses a background orchestrator or transcript markers.** The reviewer runs in the FOREGROUND (`run_in_background=false`) and reports through a results file per (session, round, agent) with a strict contract. `results_mode` applies to `agent` and `delegated` modes only.
- **Runtime files moved to `<hooks_dir>/state/`:** session state, reviewer prompts, results, the debug log and the metrics file. Configuration (`hook-overrides.json`, checklist files) stays in the hooks directory. Projects should git-ignore `.claude/hooks/state/`. Old files in the hooks directory are no longer used.
- **Configuration is validated and errors are fatal.** Unknown keys, bad types or enum values, invalid JSON, an unknown preset, a missing checklist file, undefined agents and invalid `CLAUDE_HOOK_*` values stop the hook with a visible error instead of being ignored. A 2.x `hook-overrides.json` that lists the removed agents or `subagent_timeout` must be updated.
- **A missing transcript or a corrupt state file is now an error**, not "no changes".

### Fixed
- Deep-tier set-serialization crash that silently skipped every deep review.
- Pending review results were never read after a foreground review (the zero-diff exit ran first); results are now consumed before any diff check.
- Silent allow on errors: any hook or config error now fails loud (a `systemMessage` to the user plus one block so Claude relays it; on a hook-forced continuation, `stop_hook_active`, it only warns, so it cannot loop).
- Metrics `session_id` is now the session hash (first 8 characters), so records can be matched to state files.
- Review no longer switches itself off for the rest of a session: `auto_continue_count`, `fail_count` and `completed` bound consecutive hook-forced continuations and now reset at the first stop of each user turn (`stop_hook_active` false). Previously, three passes disabled review for the rest of the session and marked later edits as seen.
- The abandoned-review scavenger is read-only on other sessions' state. Previously it cancelled live sessions' pending rounds after 600 s. A round is scored once (marker file) after `state_expiry`.
- A stale session keeps its pending round, which is then consumed or surfaced as UNREVIEWED instead of dropped.
- A pending round with no reviewers, or a tier with none configured, is an error, not a vacuous pass. State writes are atomic.
- Results contract: a `fail` must list issues, and every issue field must be present.
- Config: `max_*` bounds must be ≥ 1. Agent ids must match `[A-Za-z0-9_-]+` because they become file names. Custom agent field types are validated. The config fingerprint covers custom agents and `review-config.json`.
- Install and docs: hooks register under `"Stop"` with nested `hooks: [{type: "command", ...}]`, and `timeout` is in seconds (`30`, not `30000`).

### Changed
- **Checklist:** the reviewer uses a built-in generic checklist (silent failures, correctness, contracts and integration, security, hardcoding, tests), or a project file set with `review_checklist_file` (relative to the hooks directory, or absolute), which REPLACES the built-in one. A missing file is a config error.
- **New keys:** `reviewer_model`, `fixer_model` (default `sonnet`), `review_checklist_file`. **Removed key:** `subagent_timeout`.
- **Presets updated:** `minimal` now sets `reviewer_model` to `sonnet` instead of reducing `agent_ids`.
- **Results contract:** the reviewer writes `state/review-results-<session>-<round>-<agent>.json` with `round_id`, `agent_id`, `status` (`pass`/`fail`) and `issues` (each with `file`, `line`, `severity`, `category`, `description`). The hook rejects anything else without repair; `status` must be `fail` when there is a `critical` issue or two or more `high` issues. Missing or invalid results are re-requested once, then the user sees an "UNREVIEWED" warning.
- **On fail:** issues are listed and a fix agent (model from `fixer_model`) fixes them. The deep tier honours `deep_auto_fix` (`none` means report only). Fixes made by a subagent are not re-reviewed, because the hook only sees main-session edits.
- **Provenance:** every metrics record includes the hook `version` and a `config_fingerprint` (SHA-256 over the effective config, the version and the checklist file contents).
- Loop bounds (`max_auto_continues`, `max_fail_retries`, `max_review_attempts`) and the "continue" text on a pass are unchanged.
- `agent`, `delegated` and `api` review modes remain available; the documentation focuses on the default `subagent` mode.
- Documentation rewritten for the single-reviewer flow, state directory, checklist and fail-loud behavior.

## [2.0.0] - 2026-04-21

### Added
- **Context Restoration After Review**: When the hook issues a "continue" or "fix" instruction, it now extracts what Claude was working on before the review interrupted and appends it to the resume message. Includes the last user request (truncated to 200 chars) and last 5 tool actions. This prevents Claude from losing its train of thought after reviews.
- **Deep Review Auto-Fix Configuration** (`DEEP_AUTO_FIX`): Configurable severity threshold for deep review auto-fixing. Options: `"none"` (default, stop and wait), `"critical"`, `"high"`, `"medium"`, `"all"`. When enabled, deep review failures spawn a subagent to fix qualifying issues by severity instead of stopping.
  - Env var override: `CLAUDE_HOOK_DEEP_AUTO_FIX`
- `extract_pre_review_context()` function for transcript context extraction
- `_severities_at_or_above()` helper for severity threshold filtering
- **Dual Results Mode**: Choose between inline and file-based results delivery
  - **Inline mode (default)**: Results embedded in Claude's response with `<!--REVIEW_RESULTS_START-->` and `<!--REVIEW_RESULTS_END-->` markers. No extra permissions needed.
  - **File mode**: Results written to `.claude/hooks/review-results-{hash}.json`. Requires `Write(.claude/hooks/review-results-*.json)` permission in `settings.local.json`.
- `RESULTS_MODE` configuration constant (`"inline"` or `"file"`)
- JSONL-aware transcript parsing for inline mode
- Mode-specific documentation in all guides
- **Exit path helpers** (`allow_stop()`, `block_with_message()`): All 25 exit points now go through two dedicated functions. `allow_stop()` = silent exit (Claude stops). `block_with_message()` = inject message (Claude continues). Raw `sys.exit(0)` is forbidden outside these helpers.
- **`EXIT_PATH_REGISTRY`**: Declarative registry documenting all 23 exit paths and their expected behavior (`"allow"` or `"block"`). Used by the static audit test to detect mismatches.
- **`test_exit_paths.py`**: Two-layer test suite. Layer 1 (static audit): scans source for raw `sys.exit(0)`, print-before-allow_stop combos, and registry/call-count mismatches. Layer 2 (behavioral): runs hook with mock state/transcript to verify terminal paths produce no stdout and state flags are set correctly.

### Fixed
- **Deep review auto-fix infinite loop**: `_handle_deep_failure()` now sets `completed=True` so the next hook firing exits via the auto-approve gate instead of triggering another full deep review cycle.
- **`fail_count` circuit breaker unreachable in zero-diff path**: Added `fail_count >= MAX_FAIL_RETRIES` check before triggering plan agent in the zero-diff path. Previously, the circuit breaker at the main path was never reached from zero-diff, allowing infinite retry loops.
- **`fail_count` not accumulating across retries**: Changed from `fail_count=len(failed_agents)` (replace) to `fail_count=fail_count + len(failed_agents)` (accumulate) in the zero-diff failure path. Without this, the circuit breaker was effectively dead since the count reset each round.
- **5 `block`-on-terminal-path infinite loops**: Terminal exit paths (success/completion) were using `"decision": "block"` which injects a message and keeps Claude going — creating infinite loops. Fixed by replacing with `allow_stop()`:
  - `_handle_all_passed()` max auto-continues: was block → now silent allow
  - Deep review completed: was block → now silent allow
  - Skip tier max auto-continues: was block with no state save → now saves state with `completed=True` then silent allow
  - Already completed (agent mode): was block with `completed=True` re-save → now saves `completed=False` (reset) then silent allow
  - API mode max auto-continues: was block → now silent allow

### Changed
- **Quick-tier agent model upgrade**: `explore_haiku` and `general_haiku` now run on `sonnet` instead of `haiku` for better reasoning on code smells, validation gaps, and security issues. Identifiers kept for state compatibility.
- `MAX_PREVIEW_CHARS` bumped from 300 to 500 for more code context in review agents
- **Subagent-Based Fixes**: Non-deep tier reviews now instruct Claude to spawn a single general-purpose subagent (model=sonnet) to fix violations, instead of fixing inline. This preserves Claude's main context and train of thought.
- **Pretty-Printed JSON Results**: Inline results template now shows 2-space indented JSON between markers instead of a single-line blob. Existing parsers handle this transparently via `json.loads()`.
- Deep review `_handle_deep_failure` now branches on `DEEP_AUTO_FIX` setting — auto-fix mode filters issues by severity and spawns a subagent, while `"none"` mode preserves the existing plan-agent behavior.
- Results reading now supports both inline transcript extraction and file-based reading
- State files now use session hash suffix (`stop-hook-state-{hash}.json`)
- Updated all documentation files with results mode information

## [1.0.0] - 2026-01-10

### Added
- **Tiered Review System**: Automatic scaling from skip → quick → standard → deep
- **Incremental Diff Tracking**: Only reviews changes since last hook firing
- **Multi-Agent Coordination**: Parallel agent spawning with pass/fail tracking
- **Session State Management**: Per-session tracking with staleness detection
- **Context-Aware Reviews**: Specialized checks for proto/API/database files
- **Integration Checker**: Extra agent when changes span 2+ directories
- **Module Boundary Enforcement**: Optional architectural constraint checking
- **Violation History**: Tracks problem files across sessions
- **Metrics Logging**: JSONL format for analysis
- **Auto-Continue Logic**: Allows up to 3 successful passes before stopping
- **API Mode**: Fallback to direct Anthropic API calls
- **Code Hunk Preview**: Shows actual code changes to review agents

### Agent Types
- `explore_haiku` - Fast scan for obvious issues
- `general_haiku` - Validation and security checks
- `bug_hunter` - Deep analysis for edge cases (Sonnet)
- `general_sonnet` - Architecture and boundaries (Sonnet)
- `integration_checker` - Cross-module consistency (dynamic)

### Configuration
- Customizable tier thresholds (character and file count)
- Configurable agent definitions per tier
- File filtering (extensions and paths)
- Critical patterns for forced deep review
- Module boundary rules (optional)
- Auto-continue and retry limits
- State expiry timeout

### Documentation
- **README.md**: Feature overview and quick start
- **CONFIGURATION.md**: Complete configuration guide
- **TROUBLESHOOTING.md**: Common issues and solutions
- **CONTRIBUTING.md**: Contribution guidelines
- **LICENSE**: MIT license
- **review-config.example.json**: Module boundary example

### State Files
- `stop-hook-state-{hash}.json` - Session state (per-session)
- `review-results-{hash}.json` - Agent coordination results (file mode only)
- `stop-hook-debug.log` - Debug logging
- `stop-hook-metrics.jsonl` - Review metrics

**Note:** In inline mode (default), review results are embedded in the transcript, not stored in separate files.

### Tier Details

#### Skip Tier
- **Threshold**: <500 chars, 1 file
- **Agents**: None
- **Action**: Allow stop without review

#### Quick Tier
- **Threshold**: 500-5000 chars, ≤3 files
- **Agents**: 1 (explore_haiku)
- **Duration**: ~2-5 seconds

#### Standard Tier
- **Threshold**: 5000-20000 chars, ≤6 files
- **Agents**: 3 (explore_haiku, general_haiku, bug_hunter)
- **Duration**: ~5-15 seconds

#### Deep Tier
- **Threshold**: ≥20000 chars or >6 files
- **Agents**: 4 (explore_haiku, general_haiku, bug_hunter, general_sonnet)
- **Duration**: ~10-30 seconds

### Features by Mode

#### Agent Mode (Default)
- Uses Claude Code Task subagents
- No API key required
- Recommended for most users
- Full integration with Claude Code

#### API Mode (Fallback)
- Direct Anthropic API calls
- Requires `ANTHROPIC_API_KEY`
- Faster startup
- Less feature-rich

#### Results Delivery Modes
- **Inline (Default)**: Results embedded in Claude's response with HTML comment markers
- **File**: Results written to JSON file (requires write permission)

### Excluded Files
- Documentation: `.md`, `.txt`
- Config: `.json`, `.yaml`, `.toml`
- Dependencies: `.lock`, `.sum`
- Build artifacts: `/build/`, `/dist/`
- Testing: `/__pycache__/`, `/.pytest_cache/`
- Version control: `/.git/`

### Critical Patterns
Changes to these paths always trigger deep review:
- `/api/`
- `/routes/`
- `/models/`
- `_service.py`
- `/proto/`
- `/migrations/`

[Unreleased]: https://github.com/PooyanHeravi/claude-on-rails-review/compare/v3.0.0...HEAD
[2.0.0]: https://github.com/PooyanHeravi/claude-on-rails-review/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/PooyanHeravi/claude-on-rails-review/releases/tag/v1.0.0
