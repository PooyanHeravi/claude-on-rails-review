"""Subagent review mode — foreground reviewer(s), results through files.

Flow per review round:
  dispatch  → hook writes one prompt file per reviewer and blocks, asking the
              main session to run the reviewer(s) in the FOREGROUND.
  reviewer  → reads its prompt file, reviews, writes a results file keyed by
              (session, round, agent).
  next stop → hook reads the results files (before any diff checks) and
              reports pass/fail. Missing or invalid results are re-requested
              once, then surfaced to the user as UNREVIEWED — never dropped.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import NoReturn

from stop_design_audit import config
from stop_design_audit.agents import (
    AGENT_DEFINITIONS,
    TIER_EFFORT,
    load_checklist,
)
from stop_design_audit.classify import severities_at_or_above
from stop_design_audit.config import (
    MAX_AUTO_CONTINUES,
    MAX_FILES_IN_PROMPT,
    MAX_REVIEW_ATTEMPTS,
    MAX_VIOLATION_FILES,
    STATUS_FAIL,
    STATUS_PASS,
    effective_deep_auto_fix,
    get_results_file,
    get_reviewer_prompt_file,
)
from stop_design_audit.exit_helpers import (
    allow_stop,
    block_with_message,
    log,
    warn_and_allow,
)
from stop_design_audit.flow import (
    ReviewContext,
    check_completion_guards,
    ensure_round_id,
    get_code_hunks_and_violations,
    get_pending_agents_and_context,
    handle_tier_change,
)
from stop_design_audit.instructions import GIT_FIX_CONSTRAINT, GIT_READONLY_CONSTRAINT
from stop_design_audit.metrics import log_review_metrics
from stop_design_audit.results import ResultsError, read_reviewer_result
from stop_design_audit.state import ReviewState, update_violation_history


def _posix(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


# =============================================================================
# Reviewer prompt
# =============================================================================


def _results_schema_example(round_id: str, agent_id: str) -> str:
    return (
        "{\n"
        f'  "round_id": "{round_id}",\n'
        f'  "agent_id": "{agent_id}",\n'
        '  "status": "pass" | "fail",\n'
        '  "issues": [\n'
        "    {\n"
        '      "file": "path/to/file.py",\n'
        '      "line": 42,\n'
        '      "severity": "critical" | "high" | "medium" | "low",\n'
        '      "category": "short-kebab-slug",\n'
        '      "description": "what is wrong and the concrete failure it causes"\n'
        "    }\n"
        "  ]\n"
        "}"
    )


def build_reviewer_prompt(
    *,
    agent_id: str,
    state: ReviewState,
    ctx: ReviewContext,
    integration_context: dict | None,
    code_hunks: dict[str, str],
    import_violations: list[str],
    results_path: Path,
) -> str:
    """Build the markdown prompt one reviewer reads from its prompt file."""
    defn = AGENT_DEFINITIONS[agent_id]
    round_id = state.round_id

    if agent_id == "reviewer":
        checklist = load_checklist()
    else:
        checklist = defn["checks"]

    focus: list[str] = []
    context_checks = defn.get("context_checks", {})
    focus.extend(
        context_checks[c] for c in sorted(ctx.file_contexts) if c in context_checks
    )
    if integration_context:
        if len(integration_context["dirs"]) >= 2:
            focus.append(
                "Cross-module change spanning: "
                + ", ".join(integration_context["dirs"])
                + " — check callers and contracts across these boundaries."
            )
        if len(integration_context["patterns"]) >= 2:
            focus.append(
                "Critical paths touched: " + ", ".join(integration_context["patterns"])
            )
    focus.extend(f"Module-boundary violation detected: {v}" for v in import_violations)
    problem_files = [
        f for f in sorted(ctx.all_modified_files) if f in state.violation_history
    ]
    for f in problem_files[:MAX_VIOLATION_FILES]:
        cats = state.violation_history[f]
        focus.append(
            f"{f} had {sum(cats.values())} earlier finding(s) this session "
            f"(most common: {max(cats.items(), key=lambda x: x[1])[0]})"
        )
    focus_section = "\n".join(f"- {line}" for line in focus) or "- (none)"

    files = sorted(ctx.all_modified_files)
    file_lines = "\n".join(f"- {f}" for f in files[:MAX_FILES_IN_PROMPT])
    if len(files) > MAX_FILES_IN_PROMPT:
        file_lines += f"\n- ...and {len(files) - MAX_FILES_IN_PROMPT} more"

    hunk_section = (
        "\n\n".join(
            f"### {path}\n```\n{hunk}\n```" for path, hunk in sorted(code_hunks.items())
        )
        or "(no previews available — read the files)"
    )

    return f"""# Design audit — round {round_id} ({ctx.tier} tier)

You are a senior code reviewer. Review the changes below against the checklist.
The previews are pointers only — read each changed file with Read/Grep/Glob.

## Effort
{TIER_EFFORT[ctx.tier]}

## Checklist
{checklist.strip()}

## Extra focus for this change
{focus_section}

## Changed files ({len(files)})
{file_lines}

## Change previews (from the session transcript; may be truncated)
{hunk_section}

## Rules
- Report real defects in the changed code, or in code the change breaks. No style nits.
- Each issue needs a concrete failure: what input/state leads to what wrong outcome.
- status = "fail" if there is any critical issue or 2+ high issues; otherwise "pass"
  (still list the lower-severity issues).
- Do not edit any file other than the results file below.{GIT_READONLY_CONSTRAINT}

## Output — REQUIRED
Write exactly this JSON shape to `{_posix(results_path)}` with the Write tool:

{_results_schema_example(round_id, agent_id)}

Then reply with one line: `round {round_id}: <status>, <N> issue(s)`.
"""


# =============================================================================
# Block messages
# =============================================================================


def _spawn_instructions(state: ReviewState, agent_ids: list[str]) -> str:
    lines = []
    for agent_id in agent_ids:
        defn = AGENT_DEFINITIONS[agent_id]
        prompt_path = get_reviewer_prompt_file(
            state.session_hash, state.round_id, agent_id
        )
        lines.append(
            f"  - subagent_type='{defn['subagent_type']}', model='{defn['model']}', "
            f"description='design audit {state.round_id} ({agent_id})', "
            f"prompt='Read {_posix(prompt_path)} and follow it exactly.'"
        )
    return (
        f"In ONE message, make {len(agent_ids)} Agent call(s) with run_in_background=false:\n"
        + "\n".join(lines)
        + "\nWhen the review returns, end your turn. The stop hook reads the results "
        "file(s) and reports the outcome — do not act on the review yourself."
    )


def _get_dispatch_message(
    state: ReviewState, ctx: ReviewContext, agent_ids: list[str]
) -> str:
    return (
        f"DESIGN AUDIT [{ctx.tier.upper()}] round {state.round_id}: "
        f"+{abs(ctx.incremental_diff)} chars across {len(ctx.all_modified_files)} file(s).\n"
        f"Run the review now, before anything else.\n"
        f"{_spawn_instructions(state, agent_ids)}"
    )


def _get_passed_message(auto_continue_count: int, notes: list[dict]) -> str:
    msg = (
        f"Design audit passed. [Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] "
        + (
            "Continue."
            if auto_continue_count < MAX_AUTO_CONTINUES - 1
            else "Continue or identify next steps."
        )
    )
    if notes:
        msg += (
            f"\nNon-blocking findings ({len(notes)}) — mention them to the user:\n"
            + _format_issues(notes)
        )
    return msg


def _format_issues(issues: list[dict]) -> str:
    return "\n".join(
        f"- [{i['severity'].upper()}] {i['file']}:{i['line'] if i['line'] is not None else '?'} "
        f"({i['found_by']}) {i['description']}"
        for i in issues
    )


# =============================================================================
# Result processing
# =============================================================================


def _collect_issues(results: dict[str, dict]) -> list[dict]:
    issues = []
    for agent_id, data in results.items():
        for issue in data["issues"]:
            issues.append({**issue, "found_by": agent_id})
    order = {s: n for n, s in enumerate(config.SEVERITY_ORDER)}
    return sorted(issues, key=lambda i: order[i["severity"]])


def _log_round_metrics(state: ReviewState, outcome: str) -> None:
    log_review_metrics(
        tier=state.tier,
        diff_chars=state.review_diff_chars,
        file_count=state.review_file_count,
        agents=state.review_agents,
        outcome=outcome,
        fail_count=state.fail_count,
        session_id=state.session_hash,
    )


def _finish_round(state: ReviewState) -> None:
    """Clear per-round fields once a round's results are consumed."""
    state.subagent_pending = False
    state.round_id = ""
    state.passed_agents = []
    state.review_agents = []
    state.review_attempts = 0


def _handle_pass(state: ReviewState, results: dict[str, dict]) -> NoReturn:
    notes = _collect_issues(results)
    _log_round_metrics(state, STATUS_PASS)
    _finish_round(state)
    state.fail_count = 0
    state.auto_continue_count += 1
    log(f"Design audit passed; auto_continue_count now {state.auto_continue_count}")

    if state.auto_continue_count >= MAX_AUTO_CONTINUES:
        state.completed = True
        state.save()
        if notes:
            warn_and_allow(
                f"design audit passed with {len(notes)} non-blocking finding(s):\n"
                + _format_issues(notes)
            )
        allow_stop("All passed, max auto-continues (subagent)")

    state.save()
    block_with_message(_get_passed_message(state.auto_continue_count, notes))


def _handle_fail(state: ReviewState, results: dict[str, dict]) -> NoReturn:
    failed = [a for a, d in results.items() if d["status"] == STATUS_FAIL]
    issues = _collect_issues(results)
    state.fail_count += len(failed)
    state.violation_history = update_violation_history(
        state.violation_history, {"agents": results}
    )
    _log_round_metrics(state, STATUS_FAIL)
    round_id = state.round_id
    tier = state.tier
    _finish_round(state)

    threshold = effective_deep_auto_fix() if tier == "deep" else "all"
    qualifying = set(severities_at_or_above(threshold))
    fixable = [i for i in issues if i["severity"] in qualifying]
    report_only = [i for i in issues if i["severity"] not in qualifying]
    log(f"Design audit failed: {len(fixable)} fixable, {len(report_only)} report-only")

    header = (
        f"DESIGN AUDIT FAILED (round {round_id}, {tier}): "
        f"{len(issues)} issue(s) from {len(failed)} reviewer(s)."
    )
    if not fixable:
        # Nothing qualifies for auto-fix — report and stop.
        state.completed = True
        state.save()
        block_with_message(
            f"{header}\n{_format_issues(issues)}\n\n"
            "Report these findings to the user and end your turn. "
            "Do not fix them unless the user asks."
        )

    message = (
        f"{header}\nFix these {len(fixable)} issue(s) by spawning ONE Agent "
        f"(subagent_type='general-purpose', model='{config.FIXER_MODEL}', "
        f"run_in_background=false) and passing it this list:\n{_format_issues(fixable)}\n"
        f"{GIT_FIX_CONSTRAINT}\n"
    )
    if report_only:
        message += (
            f"\nReport to the user without fixing ({len(report_only)}):\n"
            f"{_format_issues(report_only)}\n"
        )
    # Edits made by a subagent are not in the main transcript, so this hook
    # cannot re-review the fix; say so rather than imply it will.
    message += (
        "\nAfter the fix agent returns, tell the user what was fixed and end your turn. "
        "Note for the user: subagent fixes are not re-reviewed by this hook."
    )
    state.save()
    block_with_message(message)


def handle_subagent_pending(state: ReviewState) -> NoReturn:
    """Consume the results of the pending round. Always exits."""
    round_id = state.round_id
    log(f"Subagent round {round_id} pending (attempt {state.review_attempts})")
    if not state.review_agents:
        _finish_round(state)
        state.save()  # discard the broken round so the next stop is clean
        raise ValueError(
            f"pending round {round_id!r} has no reviewers in state; discarded, "
            "its changes were NOT reviewed"
        )

    results: dict[str, dict] = {}
    problems: list[str] = []
    retry_agents: list[str] = []
    for agent_id in state.review_agents:
        try:
            result = read_reviewer_result(state.session_hash, round_id, agent_id)
        except ResultsError as e:
            problems.append(str(e))
            retry_agents.append(agent_id)
            continue
        if result is None:
            problems.append(f"{agent_id}: no results file")
            retry_agents.append(agent_id)
        else:
            results[agent_id] = result

    if not retry_agents:
        if any(d["status"] == STATUS_FAIL for d in results.values()):
            _handle_fail(state, results)
        _handle_pass(state, results)

    detail = "; ".join(problems)
    if state.review_attempts < MAX_REVIEW_ATTEMPTS:
        state.review_attempts += 1
        state.save()
        log(f"Round {round_id} incomplete ({detail}) — re-requesting")
        block_with_message(
            f"DESIGN AUDIT round {round_id} has no valid results yet: {detail}.\n"
            f"{_spawn_instructions(state, retry_agents)}"
        )

    outcome = (
        "invalid_results"
        if any("no results file" not in p for p in problems)
        else "abandoned"
    )
    _log_round_metrics(state, outcome)
    _finish_round(state)
    state.save()
    warn_and_allow(
        f"design audit round {round_id} produced no valid results after "
        f"{MAX_REVIEW_ATTEMPTS} attempt(s) ({detail}). These changes are UNREVIEWED."
    )


# =============================================================================
# Dispatch
# =============================================================================


def run_subagent_mode(state: ReviewState, ctx: ReviewContext) -> NoReturn:
    """Dispatch a new review round. Always exits."""
    check_completion_guards(state, ctx)
    handle_tier_change(state, ctx.tier)
    ensure_round_id(state)

    # Every round reviews with all required agents: passes don't carry over.
    state.passed_agents = []
    _, pending_agents, integration_context = get_pending_agents_and_context(state, ctx)
    if not pending_agents:
        raise ValueError(f"no reviewers configured for tier {ctx.tier!r}")
    code_hunks, import_violations = get_code_hunks_and_violations(ctx)

    for agent_id in pending_agents:
        results_path = get_results_file(state.session_hash, state.round_id, agent_id)
        prompt = build_reviewer_prompt(
            agent_id=agent_id,
            state=state,
            ctx=ctx,
            integration_context=integration_context,
            code_hunks=code_hunks,
            import_violations=import_violations,
            results_path=results_path,
        )
        prompt_path = get_reviewer_prompt_file(
            state.session_hash, state.round_id, agent_id
        )
        prompt_path.write_text(prompt, encoding="utf-8")
        log(f"Wrote reviewer prompt {prompt_path.name}")

    log(
        f"Dispatching {ctx.tier} review, round {state.round_id}, agents: {pending_agents}"
    )
    state.last_total_diff = ctx.current_total_diff
    state.last_files_seen = ctx.all_files_seen
    state.tier = ctx.tier
    state.subagent_pending = True
    state.subagent_dispatch_time = datetime.now().isoformat()
    state.review_agents = list(pending_agents)
    state.review_attempts = 1
    state.review_diff_chars = abs(ctx.incremental_diff)
    state.review_file_count = len(ctx.incremental_files)
    state.save()

    block_with_message(_get_dispatch_message(state, ctx, pending_agents))
