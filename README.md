# Claude on Rails Review

**Smart tiered code review hook for Claude Code**

A Claude Code `Stop` hook that reviews what Claude changed before it is allowed to stop. Review effort scales with the size of the change, only changes since the last review are examined, and every failure of the hook itself is reported loudly instead of being swallowed.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-3.0.0-blue.svg)](CHANGELOG.md)

## Features

- **Tiered reviews** - the tier (skip, quick, standard, deep) is chosen from the size of the change and sets how hard the reviewer digs
- **One reviewer** - a single foreground reviewer agent per round; the tier scales effort, not agent count
- **Incremental tracking** - only changes since the last hook firing are reviewed
- **Your checklist** - built-in generic checklist, or replace it with a project file (`review_checklist_file`)
- **Context-aware focus** - extra focus for proto, database, API route, gRPC service and frontend files, for cross-module changes, and for files with earlier findings in the session
- **Strict results contract** - the reviewer writes a JSON file the hook validates; missing or invalid results are re-requested once, then reported as UNREVIEWED
- **Fail loud** - invalid config, a missing transcript or corrupt state produce a visible error, never a silent allow
- **Provenance** - every metrics record carries the hook version and a config fingerprint
- **Module boundaries** - optional enforcement of forbidden imports between top-level directories
- **Fixes by a subagent** - fixes run in a separate agent (`fixer_model`), preserving the main context

## Quick Start

### 1. Install the Hook

The hook is a `stop_design_audit/` Python package plus a `stop-design-audit.py` shim. Run the bundled installer from this repo's root:

```bash
bash install.sh
```

Or install manually. Both the shim and the package must live in the same directory:

```bash
mkdir -p .claude/hooks
cp -r stop-design-audit.py stop_design_audit .claude/hooks/
```

### 2. Configure Claude Code

Add to `.claude/settings.json`:

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

### 3. Ignore the Runtime Directory

All runtime files live in `.claude/hooks/state/`. Add it to your project's `.gitignore`:

```
.claude/hooks/state/
```

### 4. Start Coding

The hook runs when Claude tries to stop and picks a tier from the incremental change:

| Tier | Incremental change | Action |
|------|--------------------|--------|
| **skip** | <500 chars and 1 file | No review |
| **quick** | <5000 chars and <=3 files | One reviewer, focused pass |
| **standard** | <20000 chars and <=6 files | One reviewer, reads every changed file |
| **deep** | anything larger | One reviewer, exhaustive pass |

A tier needs to fit BOTH its character threshold and its file limit. Thresholds are configurable ([CONFIGURATION.md](CONFIGURATION.md)).

## How It Works

```
Claude tries to stop
        |
        v
  Hook reads transcript, computes incremental diff, picks tier
        |
        +-- skip tier ------------------------> allow / auto-continue
        |
        v
  Hook writes  state/review-prompt-<session>-<round>-reviewer.md
  Hook BLOCKS: "run the reviewer (foreground Agent call)"
        |
        v
  Reviewer (agent "reviewer", model = reviewer_model) reads the prompt,
  reviews the changed files, writes
        state/review-results-<session>-<round>-reviewer.json
        |
        v
  Claude tries to stop again -> hook reads the results FIRST
        |
        +-- pass ----> "Design audit passed ... Continue."  (auto-continue)
        +-- fail ----> issues listed; a fix agent (fixer_model) fixes them
        +-- missing / invalid results -> re-requested once,
                                         then an UNREVIEWED warning
```

The reviewer runs in the **foreground** (`run_in_background=false`) in the main session. There is no background orchestrator and no transcript marker parsing in subagent mode.

### The Reviewer

The default agent id is `reviewer`. Its model comes from `reviewer_model` (default `opus`). Its prompt contains the effort instruction for the tier, the checklist, extra focus for this change, the changed files and code previews from the transcript.

Extra focus is added when:

- changed files match a context: `proto`, `database`, `api_routes`, `grpc_service`, `frontend`
- the change spans 2+ top-level directories, or touches 2+ `critical_patterns` (cross-module context is passed to the reviewer as focus; no extra agent is spawned)
- a module-boundary violation is detected
- a file already had findings earlier in the session

### Checklist

By default the reviewer uses the built-in checklist (silent failures, correctness, contracts and integration, security, hardcoding, tests). Set `review_checklist_file` to a project file to **replace** it. See [CONFIGURATION.md](CONFIGURATION.md#review-checklist).

### Results Contract

The reviewer must write exactly this shape to its results file:

```json
{
  "round_id": "a3f8d921",
  "agent_id": "reviewer",
  "status": "pass",
  "issues": [
    {
      "file": "api/routes/users.py",
      "line": 42,
      "severity": "high",
      "category": "silent-failure",
      "description": "what is wrong and the concrete failure it causes"
    }
  ]
}
```

The hook rejects the file (no repair, no partial acceptance) unless it is valid JSON, `round_id` and `agent_id` match the round, `status` is `pass` or `fail`, every issue has `file` (string), `line` (integer or `null`), `severity` (`critical`, `high`, `medium`, `low`), `category` and `description` (strings), and `status` is `fail` whenever there is a critical issue or two or more high issues.

### After a Review

- **Pass** - Claude is told to continue (`[Auto-continue N of 3]`). Non-blocking findings are passed on to the user. After `max_auto_continues` passes Claude may stop.
- **Fail** - the issues are listed. For quick and standard tiers every issue is sent to ONE fix agent (`fixer_model`, default `sonnet`). For the deep tier `deep_auto_fix` sets the minimum severity to fix; `none` means report only. Issues below the threshold are reported, not fixed.
- **Fixes are not re-reviewed.** Edits made by a subagent do not appear in the main session transcript, so the hook cannot see them.
- **Unreviewed** - if the results file is missing or invalid, the hook asks again once (`max_review_attempts` = 2 attempts in total). If that also fails, the user sees a warning that the changes are UNREVIEWED.

### Fail Loud

Configuration and hook errors are never swallowed. On an invalid config (unknown key, wrong type or enum value, bad JSON, unknown preset, missing checklist file, undefined agent, invalid `CLAUDE_HOOK_*` value), a missing transcript, a corrupt state file, or any unexpected exception, the hook shows the user a `systemMessage` ("hook error — code review did NOT run: ...") and blocks once so Claude relays it. If that stop is already a hook-forced continuation (`stop_hook_active`), it only warns, so it can never loop. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Configuration

Defaults live in [`stop_design_audit/config.py`](stop_design_audit/config.py). Project overrides go in `.claude/hooks/hook-overrides.json` (see [`hook-overrides.example.json`](hook-overrides.example.json)). The file is validated on every run:

```json
{
  "preset": "balanced",
  "reviewer_model": "opus",
  "fixer_model": "sonnet",
  "review_checklist_file": "review-checklist.md",
  "+critical_patterns": ["/api/", "/models/", "/migrations/"],
  "+excluded_paths": ["/docs/", "/examples/"]
}
```

Presets: `strict`, `balanced` (defaults), `relaxed`, `minimal`. Merge order: defaults, preset, explicit keys, environment variables. See [CONFIGURATION.md](CONFIGURATION.md) for every key.

### Environment Variables

| Variable | Values | Effect |
|----------|--------|--------|
| `CLAUDE_HOOK_SKIP` | `0`, `1` | `1` disables the hook for this run |
| `CLAUDE_HOOK_FORCE_TIER` | `quick`, `standard`, `deep` | Force a tier |
| `CLAUDE_HOOK_DEEP_AUTO_FIX` | `none`, `critical`, `high`, `medium`, `all` | Override `deep_auto_fix` |
| `CLAUDE_HOOK_REVIEW_MODE` | `subagent`, `agent`, `delegated`, `api` | Override `review_mode` |

Any other value for these is a config error and fails loud, even when `CLAUDE_HOOK_SKIP=1`.

### Module Boundaries (Optional)

Create `.claude/review-config.json` (one level above the hooks directory):

```json
{
  "module_boundaries": {
    "api": {
      "forbidden_imports": ["services", "internal"],
      "communication": "Must use gRPC to communicate with services"
    },
    "frontend": {
      "forbidden_imports": ["api", "services"],
      "communication": "Must use REST API only"
    }
  }
}
```

The module is the first path segment of a changed file. Violations (`from <module>` / `import <module>` of a forbidden module in the change preview) are given to the reviewer as extra focus. The optional `force_tier` key (`quick`, `standard`, `deep`) forces a tier.

## Runtime Files and Metrics

Everything the hook writes goes to `.claude/hooks/state/`. Configuration stays in `.claude/hooks/`.

| File | Contents |
|------|----------|
| `stop-hook-state-<session>.json` | Session state (counters, round, pending review) |
| `review-prompt-<session>-<round>-<agent>.md` | Prompt the reviewer reads |
| `review-results-<session>-<round>-<agent>.json` | Results the reviewer writes |
| `stop-hook-debug.log` | Debug log (truncated above 1 MB) |
| `stop-hook-metrics.jsonl` | One JSON record per review round (last 5000 kept) |

State, prompt and results files older than 24 hours are deleted automatically. `<session>` is a 12-character hash of the transcript path.

A metrics record:

```json
{"timestamp": "2026-10-07T15:20:11", "version": "3.0.0", "config_fingerprint": "9f2c...", "tier": "standard", "diff_chars": 8547, "file_count": 5, "agents": ["reviewer"], "outcome": "pass", "fail_count": 0, "session_id": "a6277e24"}
```

`session_id` is the first 8 characters of the session hash. Outcomes: `pass`, `fail`, `invalid_results`, `abandoned`, `scavenged_pass`, `scavenged_fail`.

```bash
# Reviews by tier
jq -r .tier .claude/hooks/state/stop-hook-metrics.jsonl | sort | uniq -c

# Outcomes
jq -s '[.[] | .outcome] | group_by(.) | map({outcome: .[0], count: length})' .claude/hooks/state/stop-hook-metrics.jsonl
```

### Permissions

The reviewer writes its results file with the Write tool. To avoid a permission prompt, allow it in `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/state/review-results-*.json)"]
  }
}
```

## Other Review Modes

`review_mode` also accepts `agent`, `delegated` and `api` (the last needs `ANTHROPIC_API_KEY`). They are kept for compatibility and are not covered in the main guides; the default is `subagent`. The `results_mode` key applies to `agent` and `delegated` only.

## FAQ

**Will this slow down my workflow?**
Changes under 500 characters in one file skip review. Every other tier runs one reviewer agent, so the time cost is one foreground agent call.

**What happens after 3 auto-continues?**
Claude is allowed to stop. Set `max_auto_continues` to change it.

**Can I disable the hook temporarily?**
Set `CLAUDE_HOOK_SKIP=1`, or remove the hook from `.claude/settings.json`.

**Can I use a different model for the review?**
Yes: `reviewer_model` (default `opus`) and `fixer_model` (default `sonnet`). The `minimal` preset sets `reviewer_model` to `sonnet`.

**Can I add my own reviewers?**
Yes, with `extra_agent_definitions` plus `agent_ids`; see [CONFIGURATION.md](CONFIGURATION.md#custom-agents). The default is a single reviewer.

**How do I reset session state?**
Delete `.claude/hooks/state/stop-hook-state-*.json`.

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** - Get started in 5 minutes
- **[SETUP.md](SETUP.md)** - Structured install guide and override key reference
- **[CONFIGURATION.md](CONFIGURATION.md)** - Complete configuration reference
- **[EXAMPLES.md](EXAMPLES.md)** - Real-world `hook-overrides.json` examples
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** - Common issues and solutions
- **[CHANGELOG.md](CHANGELOG.md)** - Release notes
- **[CONTRIBUTING.md](CONTRIBUTING.md)** - Contribution guidelines

## Contributing

Contributions are welcome. For major changes, please open an issue first.

## License

MIT License - see LICENSE file for details.

## Links

- [Claude Code Documentation](https://code.claude.com/docs)
- [Claude Code Hooks Guide](https://code.claude.com/docs/en/hooks-guide)
- [Issue Tracker](https://github.com/PooyanHeravi/claude-on-rails-review/issues)
