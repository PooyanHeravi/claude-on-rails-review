# Quick Start Guide

Get up and running with Claude on Rails Review 3.0.0 in 5 minutes.

## Prerequisites

- **Python 3.10+** - check with `python --version`
- **Claude Code CLI** - install with `npm install -g @anthropic-ai/claude-code`
- **Git** - recommended

## Installation

### Option 1: Automated (Recommended)

```bash
# Clone or download the repository
git clone https://github.com/PooyanHeravi/claude-on-rails-review.git

# Run the installer from your project root
cd /path/to/your/project
bash /path/to/claude-on-rails-review/install.sh
```

### Option 2: Manual

```bash
# In your project root
mkdir -p .claude/hooks

# Copy the shim AND the package - they must live together
cp -r /path/to/claude-on-rails-review/stop-design-audit.py \
      /path/to/claude-on-rails-review/stop_design_audit \
      .claude/hooks/
chmod +x .claude/hooks/stop-design-audit.py

# Create settings
cat > .claude/settings.json << 'EOF'
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
EOF
```

### Git-Ignore the Runtime Directory

All runtime files live in `.claude/hooks/state/`:

```bash
echo ".claude/hooks/state/" >> .gitignore
```

### Optional: Allow the Results File

The reviewer writes its results file with the Write tool. To avoid a permission prompt, add to `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/state/review-results-*.json)"]
  }
}
```

## Verify Installation

```bash
echo '{}' | python .claude/hooks/stop-design-audit.py
```

Expected: a JSON line containing `hook error — code review did NOT run: ValueError: hook input has no transcript_path`. The hook fails loud on empty input; this proves the package imports and your config is valid. A message starting with `invalid config:` lists what to fix in `.claude/hooks/hook-overrides.json`.

## First Use

1. **Start Claude Code in your project:**

```bash
claude
```

2. **Make a small change.** For example "Add a comment to one file". The change is under 500 characters in one file, so the hook skips review and Claude stops normally.

3. **Make a bigger change.** For example "Refactor the authentication module to use JWT tokens". The hook now blocks the stop and asks Claude to run the reviewer:

```
DESIGN AUDIT [STANDARD] round abc12345: +8547 chars across 5 file(s).
Run the review now, before anything else.
In ONE message, make 1 Agent call(s) with run_in_background=false:
  - subagent_type='general-purpose', model='opus', description='design audit abc12345 (reviewer)', prompt='Read .../state/review-prompt-<session>-abc12345-reviewer.md and follow it exactly.'
When the review returns, end your turn. The stop hook reads the results file(s) and reports the outcome — do not act on the review yourself.
```

4. **The reviewer reviews and writes its results file.** When Claude ends its turn, the hook reads the file:

- **Pass:** `Design audit passed. [Auto-continue 1 of 3] Continue.`
- **Fail:** the issues are listed and a fix agent (`fixer_model`, default `sonnet`) is asked to fix them.
- **No valid results:** the hook asks once more, then warns that the changes are UNREVIEWED.

## Understanding the Output

### Tiers

| Tier | Incremental change | What happens |
|------|--------------------|--------------|
| skip | <500 chars, 1 file | No review |
| quick | <5000 chars, <=3 files | One reviewer, focused pass |
| standard | <20000 chars, <=6 files | One reviewer, reads every changed file |
| deep | larger | One reviewer, exhaustive pass |

The tier scales the reviewer's effort, never the number of agents.

### Auto-Continue Counter

```
[Auto-continue 1 of 3]
```

Shows how many passing reviews have occurred out of the maximum before Claude is allowed to stop.

## Configuration Basics

Create `.claude/hooks/hook-overrides.json` (see [`hook-overrides.example.json`](hook-overrides.example.json)). The file is validated on every run, so typos are reported instead of ignored.

### Pick a Preset

```json
{ "preset": "balanced" }
```

`strict`, `balanced`, `relaxed` or `minimal`. See [SETUP.md](SETUP.md#step-2-choose-a-preset).

### Choose Models

```json
{
  "reviewer_model": "sonnet",
  "fixer_model": "sonnet"
}
```

`reviewer_model` defaults to `opus`, `fixer_model` to `sonnet`.

### Use Your Own Checklist

```json
{ "review_checklist_file": "review-checklist.md" }
```

The file lives in `.claude/hooks/` (or use an absolute path) and replaces the built-in checklist.

### Adjust Thresholds

```json
{
  "tier_thresholds": {"skip": 1000, "quick": 5000, "standard": 20000},
  "tier_file_limits": {"skip": 1, "quick": 3, "standard": 6}
}
```

Both objects must define all three tiers.

### Change the Auto-Continue Limit

```json
{ "max_auto_continues": 5 }
```

### Exclude Files and Paths

```json
{
  "+excluded_extensions": [".css"],
  "+excluded_paths": ["/docs/", "/scripts/temp/"]
}
```

## Common Use Cases

**Less interruption**

```json
{ "preset": "relaxed" }
```

**Stricter review**

```json
{ "preset": "strict" }
```

**Faster, cheaper reviews**

```json
{ "reviewer_model": "sonnet" }
```

## Troubleshooting

### Hook not running?

```bash
cat .claude/settings.json
ls -la .claude/hooks/stop-design-audit.py
echo '{}' | python .claude/hooks/stop-design-audit.py
```

### "hook error — code review did NOT run"?

The hook failed loudly. The text after the colon says why (invalid config, missing transcript, corrupt state file). See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

### Changes marked UNREVIEWED?

The reviewer did not write a valid results file after a re-request. Check the permission above, then `.claude/hooks/state/review-results-*.json` and the debug log.

### Reset state

```bash
rm .claude/hooks/state/stop-hook-state-*.json
```

## Debug Output

```bash
tail -f .claude/hooks/state/stop-hook-debug.log
jq . .claude/hooks/state/stop-hook-metrics.jsonl
```

## Next Steps

1. **Write a project checklist** - see [CONFIGURATION.md](CONFIGURATION.md#review-checklist)
2. **Set up module boundaries** - see [CONFIGURATION.md](CONFIGURATION.md#module-boundaries)
3. **Review metrics** - `.claude/hooks/state/stop-hook-metrics.jsonl`
4. **Read examples** - [EXAMPLES.md](EXAMPLES.md)

## Getting Help

- **Documentation**: README.md, CONFIGURATION.md, TROUBLESHOOTING.md
- **Issues**: open an issue on GitHub
- **Debug**: `.claude/hooks/state/stop-hook-debug.log`
