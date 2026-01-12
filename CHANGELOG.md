# Changelog

All notable changes to Claude on Rails Review will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Dual Results Mode**: Choose between inline and file-based results delivery
  - **Inline mode (default)**: Results embedded in Claude's response with `<!--REVIEW_RESULTS_START-->` and `<!--REVIEW_RESULTS_END-->` markers. No extra permissions needed.
  - **File mode**: Results written to `.claude/hooks/review-results-{hash}.json`. Requires `Write(.claude/hooks/review-results-*.json)` permission in `settings.local.json`.
- `RESULTS_MODE` configuration constant (`"inline"` or `"file"`)
- JSONL-aware transcript parsing for inline mode
- Mode-specific documentation in all guides

### Changed
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
