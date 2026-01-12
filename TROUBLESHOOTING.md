# Troubleshooting Guide

Common issues and solutions for Claude on Rails Review.

## Table of Contents

- [Hook Not Running](#hook-not-running)
- [Results Mode Issues](#results-mode-issues)
- [Review Never Completes](#review-never-completes)
- [Agents Not Spawning](#agents-not-spawning)
- [False Positives](#false-positives)
- [Performance Issues](#performance-issues)
- [State File Issues](#state-file-issues)
- [Module Boundary Errors](#module-boundary-errors)

## Hook Not Running

### Problem: Hook doesn't execute when Claude stops

**Symptoms:**
- No review instructions appear
- State files aren't created
- Debug log is empty

**Solutions:**

1. **Check hook configuration in `.claude/settings.json`:**

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

2. **Verify file path:**

```bash
# Should exist
ls -la .claude/hooks/stop-design-audit.py

# Check permissions (Unix/Linux/Mac)
chmod +x .claude/hooks/stop-design-audit.py
```

3. **Test Python execution:**

```bash
# Should print help/error
python .claude/hooks/stop-design-audit.py

# Check Python version (requires 3.10+)
python --version
```

4. **Check Claude Code version:**

```bash
claude --version
# Update if needed: npm install -g @anthropic-ai/claude-code
```

## Results Mode Issues

### Understanding Results Modes

Claude on Rails Review supports two results delivery modes:

| Mode | Default | Permissions | How Results Arrive |
|------|---------|-------------|-------------------|
| **inline** | Yes | None needed | Embedded in Claude's response with markers |
| **file** | No | `Write(.claude/hooks/review-results-*.json)` | Written to JSON file |

Check your current mode in `stop-design-audit.py`:

```python
RESULTS_MODE = "inline"  # or "file"
```

### Problem: Results not appearing (inline mode)

**Symptoms:**
- Review instructions sent but results not detected
- Hook times out waiting for results
- No errors in debug log

**Solutions:**

1. **Verify inline markers in Claude's response:**

Results should appear between these markers:
```
<!--REVIEW_RESULTS_START-->
{"round_id": "abc12345", "agents": {...}}
<!--REVIEW_RESULTS_END-->
```

2. **Check transcript parsing:**

The hook parses the JSONL transcript to find markers. Verify:
```bash
# Look for markers in transcript
grep -a "REVIEW_RESULTS" <transcript_path>
```

3. **Manual extraction test:**
```python
# Test the extraction function
python -c "
from stop_design_audit import _extract_results_from_transcript
result = _extract_results_from_transcript('/path/to/transcript.jsonl')
print(result)
"
```

### Problem: Results not appearing (file mode)

**Symptoms:**
- File `review-results-{hash}.json` not created
- Permission denied errors
- Claude asks for file write permission

**Solutions:**

1. **Add write permission to `.claude/settings.local.json`:**

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/review-results-*.json)"]
  }
}
```

2. **Verify mode is set correctly:**
```python
RESULTS_MODE = "file"  # Must be "file", not "inline"
```

3. **Check file path:**
```bash
# Results file location
ls -la .claude/hooks/review-results-*.json
```

### Problem: Switching between modes

When switching modes, clear old state:

```bash
# Clear all state files
rm .claude/hooks/stop-hook-state-*.json
rm .claude/hooks/review-results-*.json  # File mode only
```

## Review Never Completes

### Problem: Hook blocks indefinitely, agents never finish

**Symptoms:**
- Claude stuck in "reviewing" state
- No progress after spawning agents
- Timeout after 30 seconds

**Solutions:**

1. **Check results format (both modes):**

Agents must output results in this exact format:

**Inline mode:**
```
<!--REVIEW_RESULTS_START-->
{"round_id": "abc12345", "agents": {"explore_haiku": {"status": "pass", "issues": []}}}
<!--REVIEW_RESULTS_END-->
```

**File mode:**
```json
{
  "round_id": "abc12345",
  "agents": {
    "explore_haiku": {
      "status": "pass",
      "issues": []
    }
  }
}
```

2. **Verify round_id matches:**

```bash
# Check state file
jq .round_id .claude/hooks/stop-hook-state-*.json

# For file mode, check results file
jq .round_id .claude/hooks/review-results-*.json
```

3. **Check for hung agents:**

If agents don't complete, review will timeout. Check debug log:

```bash
tail -50 .claude/hooks/stop-hook-debug.log
```

4. **Reset and retry:**

```bash
# Clear state and start fresh
rm .claude/hooks/stop-hook-state-*.json
rm .claude/hooks/review-results-*.json  # File mode only
```

## Agents Not Spawning

### Problem: Review instructions appear but no agents spawn

**Symptoms:**
- Instructions printed but Claude doesn't launch agents
- "Spawn N Task agents" message but no activity

**Solutions:**

1. **Claude might not understand instructions:**

The hook returns instructions telling Claude to spawn agents. If Claude doesn't comply, try:

- Making changes more significant (trigger higher tier)
- Manually asking: "Please run the review agents as instructed"

2. **Check agent definitions:**

Verify agents are defined in `AGENT_DEFINITIONS`:

```python
# All agents in AGENT_IDS must exist in AGENT_DEFINITIONS
AGENT_IDS = {
    "quick": ["explore_haiku"],  # Must have AGENT_DEFINITIONS["explore_haiku"]
}
```

3. **Increase timeout:**

In `.claude/settings.json`:

```json
{
  "hooks": {
    "stop": [{
      "command": "python .claude/hooks/stop-design-audit.py",
      "timeout": 60000
    }]
  }
}
```

## False Positives

### Problem: Agents flag non-issues as violations

**Symptoms:**
- Agents report issues that aren't real problems
- Review fails on valid code
- Excessive retries

**Solutions:**

1. **Adjust agent checks:**

Make checks more specific in `AGENT_DEFINITIONS`:

```python
AGENT_DEFINITIONS["explore_haiku"]["checks"] = "critical bugs only, ignore style issues"
```

2. **Increase tier thresholds:**

Skip review for more changes:

```python
TIER_THRESHOLDS = {
    "skip": 2000,    # Was 500
    "quick": 5000,   # Was 3000
    "standard": 20000,  # Was 15000
}
```

3. **Exclude problematic paths:**

```python
EXCLUDED_PATHS = [
    "/tests/",  # Skip test code
    "/scripts/",  # Skip utility scripts
]
```

4. **Reduce retry limit:**

Don't get stuck in retry loops:

```python
MAX_FAIL_RETRIES = 1  # Was 3
```

## Performance Issues

### Problem: Reviews take too long or slow down workflow

**Symptoms:**
- Hook takes >30 seconds
- Frequent review interruptions
- Claude feels sluggish

**Solutions:**

1. **Optimize tier thresholds:**

Skip more small changes:

```python
TIER_THRESHOLDS = {
    "skip": 1000,   # Skip more
    "quick": 8000,
    "standard": 25000,
}
```

2. **Use faster agents:**

Replace Sonnet with Haiku:

```python
AGENT_IDS = {
    "standard": ["explore_haiku", "general_haiku"],  # No bug_hunter (Sonnet)
    "deep": ["explore_haiku", "general_haiku", "general_haiku"],  # No general_sonnet
}
```

3. **Reduce agent count:**

```python
AGENT_IDS = {
    "quick": ["explore_haiku"],
    "standard": ["explore_haiku"],  # Was 3 agents
    "deep": ["explore_haiku", "general_haiku"],  # Was 4 agents
}
```

4. **Increase auto-continue limit:**

```python
MAX_AUTO_CONTINUES = 10  # Was 3 - less frequent stops
```

5. **Profile with metrics:**

```bash
# Find slowest reviews
jq 'select(.tier == "deep")' .claude/hooks/stop-hook-metrics.jsonl | head -5
```

## State File Issues

### Problem: State file corruption or accumulation

**Symptoms:**
- `stop-hook-state.json` has wrong format
- State not resetting between sessions
- Old state interfering with new work

**Solutions:**

1. **Validate state file:**

```bash
# Check if valid JSON
jq empty .claude/hooks/stop-hook-state.json && echo "Valid" || echo "Invalid"
```

2. **Reset state manually:**

```bash
rm .claude/hooks/stop-hook-state-*.json
```

3. **Check staleness timeout:**

State expires after 1 hour by default. Adjust if needed:

```python
STATE_EXPIRY = 7200  # 2 hours instead of 1
```

4. **Clean up old files:**

```bash
# Remove all state files
rm .claude/hooks/stop-hook-state-*.json

# Remove results files (file mode only)
rm .claude/hooks/review-results-*.json
```

**Note:** In inline mode (default), review results are embedded in the transcript, not stored in separate files. Only state files need cleanup.

## Module Boundary Errors

### Problem: Module boundary violations not detected or wrong

**Symptoms:**
- Forbidden imports not caught
- Violations reported for valid imports
- Configuration ignored

**Solutions:**

1. **Verify config file location:**

Must be at `.claude/review-config.json` (one level up from hooks):

```bash
# Should exist
ls -la .claude/review-config.json

# NOT here
ls -la .claude/hooks/review-config.json  # Wrong location
```

2. **Check JSON format:**

```bash
# Validate JSON
jq empty .claude/review-config.json && echo "Valid" || echo "Invalid"
```

3. **Review module path detection:**

The hook determines module from the first path segment:

```
api/routes/users.py  → module = "api"
services/auth/service.py  → module = "services"
main.py  → no module (skipped)
```

Ensure your project structure matches config:

```json
{
  "module_boundaries": {
    "api": { ... },       // Matches api/
    "services": { ... }   // Matches services/
  }
}
```

4. **Check import pattern detection:**

The hook looks for these patterns in code hunks:

```python
from services import something  # Detected
import services.auth           # Detected
import services                 # Detected
```

But not:

```python
# from services import x  # Commented out - not detected
```

## Debug Mode

### Enable Maximum Verbosity

1. **Check debug log:**

```bash
tail -f .claude/hooks/stop-hook-debug.log
```

2. **Add custom logging:**

Edit `stop-design-audit.py` and add logs anywhere:

```python
log(f"Custom debug: {variable_name}")
```

3. **Test with sample input:**

```bash
cat > test-input.json << 'EOF'
{
  "transcript_path": "/path/to/real/transcript.jsonl",
  "cwd": "/path/to/project"
}
EOF

cat test-input.json | python .claude/hooks/stop-design-audit.py
```

## Common Error Messages

### "Transcript file not found"

**Cause:** Invalid transcript path in input
**Fix:** Check that Claude Code is passing correct path (usually not user error)

### "JSON decode error reading results file"

**Cause:** Race condition - Claude is still writing results
**Fix:** Hook retries automatically, no action needed

### "No transcript_path in input"

**Cause:** Hook called without proper input
**Fix:** Hook must be triggered by Claude Code, not manually (unless testing)

### "State file is malformed JSON"

**Cause:** Corruption or interrupted write
**Fix:** Delete state file and restart

### "No results markers found in transcript" (inline mode)

**Cause:** Claude didn't output results with the expected markers
**Fix:**
- Verify agents were spawned and completed
- Check that Claude is following instructions to output markers
- Review transcript for partial or malformed output

### "Permission denied writing review-results" (file mode)

**Cause:** Missing file write permission
**Fix:** Add to `.claude/settings.local.json`:
```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/review-results-*.json)"]
  }
}
```

## Getting Help

If you're still stuck:

1. **Gather debug info:**

```bash
# Collect all relevant files
mkdir debug-info
cp .claude/hooks/stop-hook-*.log debug-info/
cp .claude/hooks/stop-hook-*.json debug-info/
cp .claude/settings.json debug-info/
cp .claude/review-config.json debug-info/ 2>/dev/null || true
```

2. **Check hook version:**

```bash
head -20 .claude/hooks/stop-design-audit.py | grep "Claude Code Stop Hook"
```

3. **Open an issue:**

Include:
- Debug logs
- Configuration files
- Description of problem
- Steps to reproduce

## Performance Benchmarks

Expected performance (typical laptop):

- **Skip tier:** <100ms (no agents)
- **Quick tier:** 2-5 seconds (1 Haiku agent)
- **Standard tier:** 5-15 seconds (3 agents, 1 Sonnet)
- **Deep tier:** 10-30 seconds (4 agents, 2 Sonnet)

If your times are significantly higher:
- Check agent definitions (too complex instructions?)
- Reduce parallel agents
- Increase tier thresholds
- Consider API mode for faster startup

## Known Limitations

1. **Context Window:** Very large diffs (>100K chars) may exceed agent context
2. **Binary Files:** Can't review binary file changes (but these are excluded)
3. **Git Conflicts:** Hook doesn't understand merge conflict markers
4. **External Deps:** Can't verify correctness of external library usage
5. **Runtime Behavior:** Only reviews static code, not runtime behavior
