# Quick Start Guide

Get up and running with Claude on Rails Review in 5 minutes.

## Prerequisites

- **Python 3.10+** - Check with `python --version`
- **Claude Code CLI** - Install with `npm install -g @anthropic-ai/claude-code`
- **Git** - For version control (recommended)

## Installation

### Option 1: Automated (Recommended)

```bash
# Clone or download the repository
git clone https://github.com/PooyanHeravi/claude-on-rails-review.git
cd claude-on-rails-review

# Copy to your project
cd /path/to/your/project
bash /path/to/claude-on-rails-review/install.sh
```

### Option 2: Manual

```bash
# In your project root
mkdir -p .claude/hooks

# Copy the shim AND the package — they must live together
cp -r /path/to/claude-on-rails-review/stop-design-audit.py \
      /path/to/claude-on-rails-review/stop_design_audit \
      .claude/hooks/
chmod +x .claude/hooks/stop-design-audit.py

# Create settings
cat > .claude/settings.json << 'EOF'
{
  "hooks": {
    "stop": [{
      "command": "python .claude/hooks/stop-design-audit.py",
      "timeout": 30000
    }]
  }
}
EOF

# Add to .gitignore
cat >> .gitignore << 'EOF'

# Claude on Rails Review
.claude/hooks/stop-hook-*.json
.claude/hooks/stop-hook-*.log
.claude/hooks/review-results-*.json
EOF
```

## Verify Installation

```bash
# Test the hook
echo '{"transcript_path":"/tmp/test.jsonl"}' | python .claude/hooks/stop-design-audit.py

# Should output: "No transcript_path in input" or similar
# If it runs without Python errors, you're good!
```

## First Use

1. **Start Claude Code in your project:**

```bash
claude code
```

2. **Make a small change:**

```
You: "Add a comment to the README"
```

Claude will make the change and try to stop. The hook will run automatically!

3. **See the review:**

Since the change is small (<500 chars), it will skip review and Claude will stop normally.

4. **Make a bigger change:**

```
You: "Refactor the authentication module to use JWT tokens"
```

Now the hook will trigger a review! You'll see:

```
STANDARD_REVIEW: +8547 chars, 5 files across 2 module(s) [Round: abc12345]

Spawn 3 Task agents IN PARALLEL (single message, multiple tool calls):

1. subagent_type='Explore', model='haiku' [ID: explore_haiku]
   Check: code smells, obvious bugs, hardcoded values

2. subagent_type='general-purpose', model='haiku' [ID: general_haiku]
   Check: silent failures, missing validation, security issues

3. subagent_type='general-purpose', model='sonnet' [ID: bug_hunter]
   Check: null access, race conditions, resource leaks

Files:
  api/
    - routes/auth.py
    - middleware/jwt.py
  core/
    - models/user.py
    - utils/tokens.py
  tests/
    - test_auth.py

[Auto-continue 1 of 3] If no issues found, continue with implementation.
```

Claude will spawn the review agents, they'll check your code, and report back!

## Understanding the Output

### Tier Indicators

- **`SKIP`** - Very small change, no review needed
- **`QUICK_REVIEW`** - Small change, lightweight check
- **`STANDARD_REVIEW`** - Medium change, thorough review
- **`DEEP_REVIEW`** - Large change, comprehensive audit

### Agent Progress

Watch as agents complete:

```
✓ explore_haiku: PASSED (skip)
✓ general_haiku: PASSED (skip)
✗ bug_hunter: FAILED - 2 issues found
```

### Auto-Continue Counter

```
[Auto-continue 1 of 3]
```

Shows how many successful reviews before manual approval required.

## Configuration Basics

### Results Mode

By default, the hook uses **inline mode** - no extra permissions needed. Claude embeds results in its response with markers.

To use **file mode** (results written to JSON file), set `RESULTS_MODE` in [`stop_design_audit/config.py`](stop_design_audit/config.py) or override it in `.claude/hooks/hook-overrides.json`:

```json
{ "RESULTS_MODE": "file" }
```

Then add permission to `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/review-results-*.json)"]
  }
}
```

### Adjust Review Thresholds

Edit [`stop_design_audit/config.py`](stop_design_audit/config.py) (or override in `hook-overrides.json`):

```python
# Make it more/less strict
TIER_THRESHOLDS = {
    "skip": 500,       # <500 chars: no review (change to 1000 for more lenient)
    "quick": 5000,     # 500-5000 chars: quick review
    "standard": 20000, # 5000-20000 chars: standard review
}
```

### Change Auto-Continue Limit

```python
MAX_AUTO_CONTINUES = 3  # Change to 5 or 10 for fewer interruptions
```

### Exclude File Types

```python
EXCLUDED_EXTENSIONS = {
    ".json", ".md", ".txt",  # Add more: ".yaml", ".toml", etc.
}
```

### Exclude Paths

```python
EXCLUDED_PATHS = [
    "/tests/",          # Skip all tests
    "/docs/",           # Skip documentation
    "/scripts/temp/",   # Skip temporary scripts
]
```

## Common Use Cases

### "I want less interruption"

```python
MAX_AUTO_CONTINUES = 10  # More passes before stopping
TIER_THRESHOLDS["skip"] = 2000  # Skip more changes
```

### "I want stricter review"

```python
MAX_AUTO_CONTINUES = 1   # Stop after each review
TIER_THRESHOLDS["skip"] = 200  # Review even small changes
```

### "I only want to review API changes"

```python
CRITICAL_PATTERNS = ["/api/", "/routes/"]  # Only these paths
TIER_THRESHOLDS["skip"] = 10000  # Skip everything else
```

### "Speed up reviews"

```python
# Use only fast Haiku agents
AGENT_IDS = {
    "quick": ["explore_haiku"],
    "standard": ["explore_haiku"],
    "deep": ["explore_haiku", "general_haiku"],
}
```

## Troubleshooting

### Hook not running?

```bash
# Check settings
cat .claude/settings.json

# Check hook exists
ls -la .claude/hooks/stop-design-audit.py

# Test manually
echo '{"transcript_path":"test"}' | python .claude/hooks/stop-design-audit.py
```

### Reviews taking too long?

- Reduce agent count (edit `AGENT_IDS`)
- Use only Haiku agents (faster)
- Increase tier thresholds (fewer reviews)

### Too many false positives?

- Increase `MAX_AUTO_CONTINUES` (ignore more)
- Adjust agent checks (edit `AGENT_DEFINITIONS`)
- Exclude problematic paths (edit `EXCLUDED_PATHS`)

### Want to reset state?

```bash
rm .claude/hooks/stop-hook-state-*.json
rm .claude/hooks/review-results-*.json  # File mode only
```

### Results not appearing (file mode)?

1. Check `RESULTS_MODE = "file"` is set
2. Verify permission in `.claude/settings.local.json`
3. Check for write errors in debug log

### Can't find review results (inline mode)?

Results are embedded in Claude's response with markers:
```
<!--REVIEW_RESULTS_START-->
{"round_id": "...", "agents": {...}}
<!--REVIEW_RESULTS_END-->
```

## Debug Output

Check the logs:

```bash
# Watch in real-time
tail -f .claude/hooks/stop-hook-debug.log

# View metrics
cat .claude/hooks/stop-hook-metrics.jsonl | jq
```

## Next Steps

Now that you're running:

1. **Customize thresholds** - Adjust to your workflow
2. **Set up module boundaries** - See [CONFIGURATION.md](CONFIGURATION.md)
3. **Review metrics** - Use `.claude/hooks/stop-hook-metrics.jsonl`
4. **Read examples** - See [EXAMPLES.md](EXAMPLES.md) for your project type

## Getting Help

- **Documentation**: See README.md, CONFIGURATION.md, TROUBLESHOOTING.md
- **Examples**: Check EXAMPLES.md for your project type
- **Issues**: Open an issue on GitHub
- **Debug**: Check `.claude/hooks/stop-hook-debug.log`

## Tips

✅ **Do:**
- Start with default settings
- Adjust based on metrics
- Exclude non-critical paths
- Use faster agents for quick iteration

❌ **Don't:**
- Make thresholds too strict initially
- Review documentation files
- Review test fixtures
- Forget to add state files to .gitignore

---

**You're all set! Happy coding with automated review! 🚂**

For more details, see:
- [README.md](README.md) - Full feature overview
- [CONFIGURATION.md](CONFIGURATION.md) - Complete configuration guide
- [EXAMPLES.md](EXAMPLES.md) - Real-world configurations
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Common issues
