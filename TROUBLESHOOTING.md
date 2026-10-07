# Troubleshooting Guide

Common issues and solutions for Claude on Rails Review 3.0.0.

## Table of Contents

- [Hook Not Running](#hook-not-running)
- [Hook Error: code review did NOT run](#hook-error---code-review-did-not-run)
- [Review Never Completes or Is UNREVIEWED](#review-never-completes-or-is-unreviewed)
- [Reviewer Not Starting](#reviewer-not-starting)
- [Results File Problems](#results-file-problems)
- [False Positives](#false-positives)
- [Performance Issues](#performance-issues)
- [State File Issues](#state-file-issues)
- [Module Boundary Errors](#module-boundary-errors)
- [Upgrading from 2.x](#upgrading-from-2x)
- [Debug Mode](#debug-mode)
- [Getting Help](#getting-help)
- [Known Limitations](#known-limitations)

## Hook Not Running

**Symptoms:** no review instructions appear, no `state/` directory activity, empty debug log.

1. **Check the hook registration in `.claude/settings.json`:**

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

2. **Verify the files exist** (the shim and the package must be side by side):

```bash
ls -la .claude/hooks/stop-design-audit.py .claude/hooks/stop_design_audit/
chmod +x .claude/hooks/stop-design-audit.py   # Unix/macOS
```

3. **Run it by hand:**

```bash
echo '{}' | python .claude/hooks/stop-design-audit.py
```

A JSON line with `hook error — code review did NOT run: ValueError: hook input has no transcript_path` is the expected result and means the hook itself works. A Python traceback (`ImportError`, `SyntaxError`) means the package did not copy correctly or Python is older than 3.10.

4. **Is it being skipped on purpose?** `CLAUDE_HOOK_SKIP=1` disables the hook. Changes that only touch excluded files (`.md`, `.json`, `.yml`, lock files, `/node_modules/`, ...) and changes under the skip threshold (500 characters in one file by default) also produce no review.

## Hook Error: code review did NOT run

```
[stop-design-audit] hook error — code review did NOT run: <reason> (log: .../state/stop-hook-debug.log)
```

This is the hook failing loud. It never allows the stop silently: the user gets this message and Claude is blocked once so it relays the error. If that stop is already a hook-forced continuation (`stop_hook_active`), the message is shown as a warning only, so it cannot loop. Fix the reason; nothing was reviewed in the meantime.

Common reasons:

| Reason | Cause | Fix |
|--------|-------|-----|
| `invalid config: hook-overrides.json: unknown key 'subagent_timeout'` | Key removed or misspelled | Remove or correct it; see [Upgrading from 2.x](#upgrading-from-2x) |
| `invalid config: hook-overrides.json: 'deep_auto_fix' must be one of [...]` | Bad enum or type | Use a listed value |
| `invalid config: hook-overrides.json: unknown preset 'x'` | Preset name | Use `strict`, `balanced`, `relaxed`, `minimal` |
| `invalid config: hook-overrides.json: cannot parse: ...` | Invalid JSON | Validate with `jq empty .claude/hooks/hook-overrides.json` |
| `invalid config: review_checklist_file not found: <path>` | Checklist path wrong | The path is relative to `.claude/hooks/` (or absolute) |
| `invalid config: agent_ids.deep references undefined agent 'explore_haiku'` | Old agent id, or an agent without a definition | Use `reviewer` or define the agent in `extra_agent_definitions` |
| `invalid config: extra_agent_definitions.x missing ['checks']` | Incomplete definition | Provide `subagent_type`, `model` and `checks` |
| `invalid config: CLAUDE_HOOK_FORCE_TIER='x' must be one of [...]` | Bad environment variable | Fix or unset it (checked even with `CLAUDE_HOOK_SKIP=1`) |
| `invalid config: .../review-config.json: ...` | Bad module-boundaries file or `force_tier` | Fix the JSON; `force_tier` must be `quick`, `standard` or `deep` |
| `ValueError: hook input has no transcript_path` | Hook run without Claude Code input | Normal when run by hand with `{}` |
| `FileNotFoundError: [Errno 2] No such file or directory: '<transcript>'` | Transcript path unreadable | A missing transcript is not treated as "no changes"; check the path Claude Code passes |
| `ValueError: corrupt state file ...` | State file truncated or edited | Delete that `state/stop-hook-state-*.json` (a corrupt file is never reset silently because it could hold a pending review) |
| `JSONDecodeError: ...` | Hook input was not valid JSON | Only seen when run by hand |

The config validator reports every problem at once, so fix the whole list before retrying. Details are in `state/stop-hook-debug.log`.

## Review Never Completes or Is UNREVIEWED

The review runs as a foreground agent in the main session, so there is no background process to wait for. What can go wrong is the results file.

Messages and what they mean:

- `DESIGN AUDIT round <id> has no valid results yet: <detail>` - the hook found no results file, or one that breaks the contract, and asks again (once with the default `max_review_attempts` of 2). Claude should re-run the reviewer.
- `design audit round <id> produced no valid results after 2 attempt(s) (...). These changes are UNREVIEWED.` - the re-request also failed. Nothing was reviewed. Re-run the review by making another change, or run a review manually; check the points below to prevent a repeat.
- `Review retries exhausted (N failures, max 3)` - the reviewer failed `max_fail_retries` times. The hook hands over to the user; read the findings and decide.

Checklist:

1. **Is a results file being written?**

```bash
ls -la .claude/hooks/state/review-results-*.json
```

2. **Does it match the current round?**

```bash
jq .round_id .claude/hooks/state/stop-hook-state-*.json
jq '{round_id, agent_id, status}' .claude/hooks/state/review-results-*.json
```

3. **Can the reviewer write there?** If Claude Code asks for permission or denies the write, allow it in `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/state/review-results-*.json)"]
  }
}
```

4. **Read the debug log:** `tail -50 .claude/hooks/state/stop-hook-debug.log`

## Reviewer Not Starting

**Symptoms:** the `DESIGN AUDIT [TIER] round ...` message appears but Claude does not launch the agent.

1. The message names the exact Agent call (subagent type, model, prompt file). Ask Claude: "Please run the design audit review as instructed."
2. Check the prompt file exists: `ls .claude/hooks/state/review-prompt-*-reviewer.md`
3. Check the configured model is valid for your account (`reviewer_model`); an unavailable model can make the Agent call fail before a results file is written.
4. If you defined custom agents, check their `subagent_type` and `model`.

## Results File Problems

The hook validates the results file strictly (no repair, no partial acceptance). The detail in the message names the problem:

| Message fragment | Meaning |
|------------------|---------|
| `no results file` | The reviewer did not write it (or wrote it elsewhere) |
| `is not valid JSON` | Malformed JSON, for example a Markdown code fence written into the file |
| `round_id is 'x', expected 'y'` | The reviewer used a stale or wrong round id |
| `agent_id is 'x', expected 'y'` | Wrong agent id |
| `status must be 'pass' or 'fail'` | Missing or other status value |
| `issues[N].line is missing or mistyped` | `line` must be an integer or `null`; every issue needs `file`, `line`, `severity`, `category`, `description` |
| `issues[N].severity must be one of [...]` | Use `critical`, `high`, `medium` or `low` |
| `status is 'pass' but issues meet the fail criteria` | A `critical` issue or two or more `high` issues require `status: "fail"` |

The expected shape is in [CONFIGURATION.md](CONFIGURATION.md#results-contract) and is repeated inside each prompt file.

Results are read at the next stop before any diff check. Stale results files are deleted after 24 hours.

## False Positives

**Symptoms:** the reviewer reports issues that are not real, reviews fail on valid code, excessive retries.

1. **Tune the checklist.** Put your project's real rules in `review_checklist_file`, and tell the reviewer what not to flag. The file replaces the default, so it is the main control over review quality.
2. **Raise tier thresholds** so small changes are skipped:

```json
{ "tier_thresholds": {"skip": 1000, "quick": 8000, "standard": 25000} }
```

3. **Exclude paths:**

```json
{ "+excluded_paths": ["/tests/", "/scripts/"] }
```

4. **Reduce retry limits:**

```json
{ "max_fail_retries": 1 }
```

5. **Use report-only for deep reviews:** `"deep_auto_fix": "none"`.

Note that fixes made by the fix agent are not re-reviewed by the hook, so a wrong fix is not caught automatically.

## Performance Issues

Every review round is one foreground reviewer agent, so the cost is that agent's run time and tokens.

1. **Use a faster model:** `{ "reviewer_model": "sonnet" }` (the `minimal` preset does this).
2. **Skip more small changes:** raise `tier_thresholds`.
3. **Fewer interruptions:** raise `max_auto_continues`, or use the `relaxed` preset.
4. **Check what you added.** Every extra agent in `agent_ids` runs for each round of its tier. The default is just `reviewer`.
5. **Profile with metrics:**

```bash
jq 'select(.tier == "deep")' .claude/hooks/state/stop-hook-metrics.jsonl | head -5
```

## State File Issues

All state lives in `.claude/hooks/state/`.

**Reset state:**

```bash
rm .claude/hooks/state/stop-hook-state-*.json
```

- A corrupt state file is a hard error (see [Hook Error](#hook-error---code-review-did-not-run)); delete it as above.
- State older than `state_expiry` (3600 s) is treated as stale: the diff baseline is kept and review counters are reset.
- Starting a new Claude session (new transcript path) starts a fresh state file.
- State, prompt and results files older than 24 hours are removed automatically.

## Module Boundary Errors

1. **Config file location.** It must be `.claude/review-config.json`, one level above the hooks directory, not inside `.claude/hooks/`.
2. **JSON validity.** Invalid JSON is a fail-loud error: `jq empty .claude/review-config.json`.
3. **Module detection.** The module is the first path segment: `api/routes/users.py` is module `api`; `main.py` has no module and is skipped.
4. **What is checked.** `from <forbidden>` and `import <forbidden>` in the code preview of the change; only `forbidden_imports` and `communication` are used. `allowed_imports` is not enforced.
5. **How violations appear.** They are given to the reviewer as extra focus; they are not blocked by the hook itself.

## Upgrading from 2.x

3.0.0 is a breaking release. A 2.x `hook-overrides.json` fails loud until it is updated:

| 2.x | 3.0.0 |
|-----|-------|
| `agent_ids` with `explore_haiku`, `general_haiku`, `bug_hunter`, `integration_checker`, `general_opus` | Remove them. One `reviewer` per tier; custom agents via `extra_agent_definitions` |
| `subagent_timeout` | Removed (reviews run in the foreground) |
| Review model fixed per agent | `reviewer_model`, `fixer_model` |
| Generic checks inside the package | `review_checklist_file` |
| Files in `.claude/hooks/` | Files in `.claude/hooks/state/`; add `.claude/hooks/state/` to `.gitignore` |

Old `stop-hook-*.json`, `review-*` files and logs in `.claude/hooks/` are no longer used and can be deleted. See [CHANGELOG.md](CHANGELOG.md).

## Debug Mode

```bash
tail -f .claude/hooks/state/stop-hook-debug.log
```

Test with a real transcript:

```bash
echo '{"transcript_path": "/path/to/real/transcript.jsonl"}' | python .claude/hooks/stop-design-audit.py
```

Force a tier while testing: `CLAUDE_HOOK_FORCE_TIER=deep` (values `quick`, `standard`, `deep`).

## Getting Help

1. **Gather debug info:**

```bash
mkdir debug-info
cp .claude/hooks/state/stop-hook-debug.log .claude/hooks/state/stop-hook-metrics.jsonl debug-info/
cp .claude/hooks/state/stop-hook-state-*.json debug-info/
cp .claude/hooks/hook-overrides.json .claude/settings.json debug-info/
cp .claude/review-config.json debug-info/ 2>/dev/null || true
```

2. **Hook version:** every metrics record contains `version` and `config_fingerprint`.
3. **Open an issue** with the debug log, configuration files, a description of the problem and steps to reproduce.

## Known Limitations

1. **Context window:** very large diffs may exceed what the reviewer can read in one pass.
2. **Subagent fixes are not re-reviewed:** the hook only sees edits made in the main session transcript.
3. **Binary and excluded files:** not reviewed.
4. **Git conflicts:** conflict markers are not understood.
5. **Runtime behavior:** only static code is reviewed.
