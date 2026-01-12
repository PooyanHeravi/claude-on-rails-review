# Claude on Rails Review

**Smart tiered code review hook for Claude Code**

Keep Claude on the rails with automated, incremental code review that scales with the size of your changes. Designed to catch issues early without interrupting your flow for small changes.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](CHANGELOG.md)

## ✨ Features

- **🎯 Tiered Reviews** - Review depth scales automatically with change size
- **📊 Incremental Tracking** - Only reviews changes since last hook firing
- **🤖 Multi-Agent Coordination** - Spawns specialized agents in parallel
- **🔄 Session Persistence** - Per-session state tracking across stops
- **🎨 Context-Aware** - Specialized checks for API routes, proto files, databases
- **📈 Metrics Logging** - Track review outcomes over time
- **⚡ Integration Detection** - Extra scrutiny when changes span modules
- **🛡️ Module Boundaries** - Enforces architectural constraints (optional)

## 🚀 Quick Start

### 1. Install the Hook

```bash
# Copy to your project's .claude/hooks/ directory
mkdir -p .claude/hooks
cp stop-design-audit.py .claude/hooks/
```

### 2. Configure Claude Code

Add to `.claude/settings.json`:

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

### 3. Start Coding

The hook runs automatically when Claude tries to stop. It will:
- ✅ Skip review for tiny changes (<500 chars, 1 file)
- 🔍 Quick review for small changes (500-5000 chars, ≤3 files)
- 🔬 Standard review for medium changes (5000-20000 chars, ≤6 files)
- 🏗️ Deep review for large changes (≥20000 chars or >6 files)

## 📋 How It Works

### Tier System

| Tier | Threshold | Files | Agents | Description |
|------|-----------|-------|--------|-------------|
| **skip** | <500 chars | 1 file | 0 | No review needed |
| **quick** | 500-5K chars | ≤3 files | 1 | Lightweight check |
| **standard** | 5K-20K chars | ≤6 files | 3 | Standard review |
| **deep** | ≥20K chars | >6 files | 4 | Comprehensive audit |

### Agent Roles

- **explore_haiku** - Fast scan for obvious issues, hardcoded values
- **general_haiku** - Silent failures, missing validation, security
- **bug_hunter** - Race conditions, resource leaks, edge cases (Sonnet)
- **general_sonnet** - Architecture, service boundaries (Sonnet)
- **integration_checker** - Cross-module consistency (added dynamically)

### Auto-Continue Logic

The hook allows Claude to continue automatically up to **3 successful review passes** per session before requiring manual approval. This keeps you in flow while maintaining code quality.

## ⚙️ Configuration

Edit the `Configuration` section in `stop-design-audit.py`:

### Tier Thresholds

```python
TIER_THRESHOLDS = {
    "skip": 500,       # Characters
    "quick": 5000,
    "standard": 20000,
}

TIER_FILE_LIMITS = {
    "skip": 1,         # File count
    "quick": 3,
    "standard": 6,
}
```

### Auto-Continue Settings

```python
MAX_AUTO_CONTINUES = 3    # How many passes before stopping
MAX_FAIL_RETRIES = 3      # Retries before giving up
STATE_EXPIRY = 3600       # Session timeout (seconds)
```

### Critical Patterns

Changes matching these patterns trigger deeper review. **Customize for your project!**

```python
# IMPORTANT: Replace with patterns for YOUR codebase!
CRITICAL_PATTERNS = [
    # Examples - uncomment/modify for your project:
    # "/api/", "/routes/", "/models/", "/migrations/"
]
```

### Excluded Paths

Skip review for these paths:

```python
EXCLUDED_EXTENSIONS = {".json", ".md", ".txt", ".yml", ".lock"}
EXCLUDED_PATHS = ["/tests/fixtures/", "/node_modules/", "/.venv/"]
```

## 🎨 Module Boundaries (Optional)

Create `.claude/review-config.json` to enforce architectural boundaries:

```json
{
  "module_boundaries": {
    "api": {
      "allowed_imports": ["core"],
      "forbidden_imports": ["services", "internal"],
      "communication": "Must use gRPC to communicate with services"
    },
    "services": {
      "allowed_imports": ["core"],
      "forbidden_imports": ["api", "frontend"],
      "communication": "Use gRPC for inter-service communication"
    },
    "frontend": {
      "allowed_imports": [],
      "forbidden_imports": ["api", "services"],
      "communication": "Must use REST API only"
    }
  }
}
```

The hook will detect cross-boundary imports in code changes and flag violations.

## 📊 Metrics & Debugging

The hook generates state files in `.claude/hooks/`:

- **`stop-hook-state-{hash}.json`** - Session state (counters, positions)
- **`stop-hook-debug.log`** - Debug output
- **`stop-hook-metrics.jsonl`** - Review metrics (JSONL format)
- **`review-results-{hash}.json`** - Agent results (file mode only, see Results Mode below)

### Analyzing Metrics

```bash
# Count reviews by tier
jq -r .tier .claude/hooks/stop-hook-metrics.jsonl | sort | uniq -c

# Average diff size by outcome
jq -s 'group_by(.outcome) | map({outcome: .[0].outcome, avg_diff: (map(.diff_chars) | add / length)})' .claude/hooks/stop-hook-metrics.jsonl

# Pass rate
jq -s '[.[] | .outcome] | group_by(.) | map({outcome: .[0], count: length})' .claude/hooks/stop-hook-metrics.jsonl
```

## 🛠️ Advanced Usage

### Results Mode

Choose how review results are delivered:

```python
RESULTS_MODE = "inline"  # Default - no extra permissions needed
# or
RESULTS_MODE = "file"    # Write to JSON file - requires permission
```

**Inline mode (default):** Claude outputs results with markers in its response. The hook parses the transcript to extract results. No file write permissions needed.

**File mode:** Claude writes results to `.claude/hooks/review-results-{hash}.json`. Requires adding to `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/review-results-*.json)"]
  }
}
```

### API Mode (Fallback)

If you prefer direct API calls over subagents:

```python
REVIEW_MODE = "api"  # Change from "agent"
API_DIFF_THRESHOLD = 500  # Haiku below, Sonnet above
```

Requires `ANTHROPIC_API_KEY` environment variable.

### Custom Agent Definitions

Modify `AGENT_DEFINITIONS` to customize checks:

```python
AGENT_DEFINITIONS = {
    "explore_haiku": {
        "subagent_type": "Explore",
        "model": "haiku",
        "checks": "your custom checks here",
        "context_checks": {
            "proto": "proto-specific checks",
        }
    },
}
```

### Integration with CI/CD

The hook can be used in CI/CD pipelines:

```bash
# Pass transcript path as stdin
echo '{"transcript_path": "/path/to/transcript.jsonl"}' | python stop-design-audit.py
```

## 🤔 FAQ

**Q: Will this slow down my workflow?**
A: No! Small changes (<500 chars) skip review entirely. Quick reviews use fast Haiku agents.

**Q: What happens after 3 auto-continues?**
A: Claude stops and waits for your manual approval before continuing.

**Q: Can I disable the hook temporarily?**
A: Yes, remove the hook from `.claude/settings.json` or set `MAX_AUTO_CONTINUES = 999`.

**Q: Does this work with custom skills?**
A: Yes! The hook is skill-agnostic and works with any Claude Code workflow.

**Q: How do I reset session state?**
A: Delete `.claude/hooks/stop-hook-state-*.json` to start fresh.

**Q: What's the difference between inline and file mode?**
A: Inline mode (default) requires no extra permissions - results are embedded in Claude's response with markers. File mode writes results to a JSON file but requires adding write permissions to `settings.local.json`.

## 📚 Documentation

- **[QUICKSTART.md](QUICKSTART.md)** - Get started in 5 minutes
- **[CONFIGURATION.md](CONFIGURATION.md)** - Complete configuration reference
- **[EXAMPLES.md](EXAMPLES.md)** - Real-world configuration examples
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** - Common issues and solutions
- **[CONTRIBUTING.md](CONTRIBUTING.md)** - Contribution guidelines

## 📝 Example Output

```
QUICK_REVIEW: +1847 chars, 2 files across 1 module(s) [Round: a3f8d921]

Spawn 1 Task agent:

1. subagent_type='Explore', model='haiku' [ID: explore_haiku]
   Check: code smells, obvious bugs, hardcoded values, missing error handling

Files:
  api/
    - routes/users.py
    - utils/validation.py

[Auto-continue 1 of 3] If no issues found, continue with implementation.
```

**Results output (inline mode - default):**
```
<!--REVIEW_RESULTS_START-->
{"round_id": "a3f8d921", "agents": {"explore_haiku": {"status": "pass", "issues": []}}}
<!--REVIEW_RESULTS_END-->
```

**Results output (file mode):**
```
IMPORTANT: After ALL agents complete, write results to .claude/hooks/review-results-a3f8d921.json
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

## 📄 License

MIT License - see LICENSE file for details.

## 🙏 Credits

Created for the Claude Code community. Inspired by Ruby on Rails conventions and the need for intelligent, non-intrusive code review.

## 🔗 Links

- [Claude Code Documentation](https://code.claude.com/docs)
- [Claude Code Hooks Guide](https://code.claude.com/docs/en/hooks-guide)
- [Issue Tracker](https://github.com/PooyanHeravi/claude-on-rails-review/issues)

---

**Keep Claude on the rails! 🚂**
