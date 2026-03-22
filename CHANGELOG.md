# Changelog

All notable changes to Claude on Rails Review will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/PooyanHeravi/claude-on-rails-review/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/PooyanHeravi/claude-on-rails-review/releases/tag/v1.0.0
