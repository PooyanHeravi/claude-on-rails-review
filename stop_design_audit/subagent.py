"""Subagent review mode — minimal context injection via file-based orchestration.

The background agent reads full instructions from a file, spawns review
sub-agents, and writes results to a file. The main session only sees
~5-line dispatch messages and ~1-line pass/fail summaries.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from stop_design_audit.classify import severities_at_or_above
from stop_design_audit.config import (
    DEEP_AUTO_FIX,
    DEEP_AUTO_FIX_ENV,
    MAX_AUTO_CONTINUES,
    ROUND_ID_LENGTH,
    STATUS_FAIL,
    STATUS_PASS,
    SUBAGENT_TIMEOUT,
)
from stop_design_audit.delegated import build_coordinator_payload
from stop_design_audit.exit_helpers import allow_stop, block_with_message, log
from stop_design_audit.flow import (
    ReviewContext,
    check_circuit_breaker,
    check_completion_guards,
    ensure_round_id,
    get_code_hunks_and_violations,
    get_pending_agents_and_context,
    handle_tier_change,
)
from stop_design_audit.instructions import (
    GIT_FIX_CONSTRAINT,
    GIT_READONLY_CONSTRAINT,
    get_review_instructions,
)
from stop_design_audit.metrics import log_review_metrics
from stop_design_audit.results import read_review_results
from stop_design_audit.state import ReviewState, update_violation_history


# =============================================================================
# Payload & Instructions
# =============================================================================


def _get_subagent_instructions_file(session_hash: str) -> Path:
    """Get session-specific subagent instructions file path."""
    from stop_design_audit.config import STATE_DIR

    return STATE_DIR / f"subagent-instructions-{session_hash}.json"


def _build_background_agent_prompt(payload: dict) -> str:
    """Build the full orchestration prompt stored in the payload file.

    This can be long because it lives in the file, not in the main session.
    """
    pending = payload.get("pending_agents", [])
    agent_defs = payload.get("agent_definitions", {})
    round_id = payload.get("round_id", "")
    tier = payload.get("tier", "")
    deep_auto_fix = payload.get("deep_auto_fix", "none")

    agent_lines = []
    for i, agent_id in enumerate(pending, 1):
        defn = agent_defs.get(agent_id, {})
        stype = defn.get("subagent_type", "general-purpose")
        model = defn.get("model", "haiku")
        effort = defn.get("effort", "")
        checks = defn.get("checks", "general code review")
        extra = defn.get("resolved_extra_checks", [])
        extra_str = f" ALSO: {'; '.join(extra)}" if extra else ""
        effort_str = ""
        if effort == "max":
            effort_str = (
                "\n   EFFORT=MAX: Be exhaustive. Trace every cross-module dependency. "
                "Read every changed file fully. Verify all contracts and signatures. "
                "No shortcuts — check edge cases, ordering, and side effects."
            )
        elif effort:
            effort_str = f"\n   EFFORT={effort.upper()}: Be thorough in your analysis."
        agent_lines.append(
            f"{i}. Agent ID: {agent_id}\n"
            f"   subagent_type='{stype}', model='{model}'\n"
            f"   Check: {checks}{extra_str}{effort_str}"
        )
    agents_block = "\n".join(agent_lines)

    fix_section = ""
    if deep_auto_fix != "none":
        qualifying = ", ".join(severities_at_or_above(deep_auto_fix))
        fix_section = (
            f"\n## Issue Handling\n"
            f"If any agent fails, spawn ONE general-purpose subagent (model='sonnet') "
            f"to fix issues at severity [{qualifying}]. "
            f"After fixes, set outcome to 'auto_fixed'.\n"
            f"{GIT_FIX_CONSTRAINT}\n"
        )
    elif tier == "deep":
        # Plan agent only for deep tier with auto-fix disabled
        fix_section = (
            "\n## Issue Handling\n"
            "If any agent fails, spawn ONE Plan subagent (subagent_type='Plan') "
            "to create a prioritized remediation plan from the issues found. "
            "Include the plan text in the results under 'plan'. "
            "Set outcome to 'plan_created'.\n"
        )

    return (
        f"You are a code review orchestrator.\n\n"
        f"## Instructions\n"
        f"1. The file_list, code_hunks, and file_contexts are in this JSON payload.\n"
        f"2. Spawn {len(pending)} review agent(s) IN PARALLEL using the Agent tool:\n\n"
        f"{agents_block}\n\n"
        f"3. Pass each agent: its checks, the file_list, code_hunks from this payload.\n"
        f"4. Fail criteria: any critical issue OR 2+ high issues means status='fail'.\n"
        f"5. Collect all results.\n"
        f"{fix_section}\n"
        f"## Output\n"
        f"Return the aggregated results JSON as your final output. Use this exact format:\n"
        f'{{"round_id": "{round_id}", "agents": {{"<agent_id>": '
        f'{{"status": "pass"|"fail", "issues": [...]}}}}, '
        f'"outcome": "pass"|"fail"|"auto_fixed"|"plan_created"}}\n\n'
        f"Return ONLY the JSON object — no markdown fences, no commentary."
        f"{GIT_READONLY_CONSTRAINT}"
    )


def _build_subagent_payload(
    session_hash: str,
    **coordinator_kwargs,
) -> dict:
    """Build payload for background agent. Extends coordinator payload."""
    payload = build_coordinator_payload(**coordinator_kwargs)

    effective_auto_fix = os.environ.get(DEEP_AUTO_FIX_ENV, "").lower() or DEEP_AUTO_FIX

    payload["session_hash"] = session_hash
    payload["deep_auto_fix"] = effective_auto_fix
    payload["autonomous_deep_failure"] = True
    payload["background_agent_instructions"] = _build_background_agent_prompt(payload)

    return payload


def _write_subagent_instructions(session_hash: str, payload: dict) -> Path:
    """Write subagent payload to JSON file. Returns the absolute file path."""
    instructions_file = _get_subagent_instructions_file(session_hash)
    instructions_file.write_text(json.dumps(payload, indent=2))
    log(f"Wrote subagent instructions to {instructions_file.name}")
    return instructions_file


# =============================================================================
# Block Messages (minimal)
# =============================================================================


def _get_dispatch_message(
    tier: str,
    round_id: str,
    instructions_file: Path,
    pending_agent_count: int,
    diff_size: int,
    file_count: int,
    auto_continue_count: int,
) -> str:
    """Build the minimal block message for subagent dispatch (~4 lines)."""
    abs_path = str(instructions_file.resolve()).replace("\\", "/")
    return (
        f"REVIEW: +{diff_size} chars, {file_count} files [{tier.upper()}] "
        f"[Round: {round_id}]\n"
        f"Spawn 1 background agent (run_in_background=true): "
        f'"Read instructions from {abs_path}. '
        f"Execute all {pending_agent_count} review agent(s). "
        f'Handle failures per instructions. Return the results JSON."\n'
        f"[Auto-continue {auto_continue_count + 1} of {MAX_AUTO_CONTINUES}] "
        f"Continue with implementation."
        f"{GIT_READONLY_CONSTRAINT}"
    )


def _get_pending_message(round_id: str) -> str:
    return f"Background review in progress (round {round_id}). Continue."


def _get_passed_message(auto_continue_count: int) -> str:
    if auto_continue_count < MAX_AUTO_CONTINUES - 1:
        return (
            f"All reviews passed. "
            f"[Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] Continue."
        )
    return (
        f"All reviews passed. "
        f"[Auto-continue {auto_continue_count} of {MAX_AUTO_CONTINUES}] "
        f"Continue or identify next steps."
    )


def _get_failure_message(
    failed_count: int, issue_count: int, plan_included: bool
) -> str:
    if plan_included:
        return (
            f"Review: {failed_count} agent(s) found {issue_count} issue(s). "
            f"Remediation plan in results file. Review and proceed with fixes."
        )
    return (
        f"Review: {failed_count} agent(s) found {issue_count} issue(s). "
        f"Auto-fix agent handled qualifying issues. Resume prior task."
    )


# =============================================================================
# Result Processing (minimal output)
# =============================================================================


def _process_subagent_results(
    state: ReviewState,
    ctx: ReviewContext,
    results: dict,
) -> bool:
    """Process results from subagent. Emits 1-2 line messages.

    Returns True if results were processed, False if no matching results.
    """
    if results.get("round_id") != state.round_id:
        return False

    log(f"Found subagent results for round {state.round_id}")
    agents_results = results.get("agents", {})
    outcome = results.get("outcome", "")

    # Update passed_agents
    for agent_id, data in agents_results.items():
        if isinstance(data, dict) and data.get("status") == STATUS_PASS:
            if agent_id not in state.passed_agents:
                state.passed_agents.append(agent_id)
                log(f"  Agent {agent_id}: PASSED")

    failed_agents = [
        aid
        for aid, data in agents_results.items()
        if isinstance(data, dict) and data.get("status") == STATUS_FAIL
    ]

    required, _, _ = get_pending_agents_and_context(state, ctx)

    if failed_agents:
        state.fail_count += len(failed_agents)
        log(
            f"  {len(failed_agents)} agent(s) failed, fail_count now {state.fail_count}"
        )
        state.violation_history = update_violation_history(
            state.violation_history, results
        )

        log_review_metrics(
            tier=ctx.tier,
            diff_chars=abs(ctx.incremental_diff),
            file_count=len(ctx.incremental_files),
            agents=required,
            outcome=STATUS_FAIL,
            fail_count=state.fail_count,
            session_id=state.session_id,
        )

        if outcome in (
            "auto_fixed",
            "plan_created",
            "deep_failure_with_plan",
            "deep_failure_auto_fixed",
        ):
            total_issues = sum(
                len(d.get("issues", []))
                for d in agents_results.values()
                if isinstance(d, dict)
            )
            log(f"Failure handled by background agent: {outcome}")
            state.last_total_diff = ctx.current_total_diff
            state.last_files_seen = ctx.all_files_seen
            state.tier = ctx.tier
            state.completed = True
            state.save()
            plan_included = outcome in ("plan_created", "deep_failure_with_plan")
            block_with_message(
                _get_failure_message(
                    len(failed_agents),
                    total_issues,
                    plan_included=plan_included,
                )
            )
        # Non-deep failure: fall through to re-dispatch
        return True

    # No failures — check if all passed
    all_passed = all(a in state.passed_agents for a in required)
    if all_passed:
        log_review_metrics(
            tier=ctx.tier,
            diff_chars=abs(ctx.incremental_diff),
            file_count=len(ctx.incremental_files),
            agents=required,
            outcome=STATUS_PASS,
            fail_count=state.fail_count,
            session_id=state.session_id,
        )
        state.auto_continue_count += 1
        log(f"All agents passed! auto_continue_count now {state.auto_continue_count}")

        if state.auto_continue_count >= MAX_AUTO_CONTINUES:
            log("Max auto continues reached (subagent)")
            state.last_total_diff = ctx.current_total_diff
            state.last_files_seen = ctx.all_files_seen
            state.tier = ctx.tier
            state.fail_count = 0
            state.round_id = ""
            state.passed_agents = []
            state.completed = True
            state.save()
            allow_stop("All passed, max auto-continues (subagent)")

        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.fail_count = 0
        state.round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]
        state.passed_agents = []
        state.completed = False
        state.save()
        block_with_message(_get_passed_message(state.auto_continue_count))

    return True


# =============================================================================
# Mode Dispatch
# =============================================================================


def handle_subagent_pending(
    state: ReviewState, ctx: ReviewContext, old_tier: str, old_round_id: str
) -> None:
    """Handle the case when a subagent is pending."""
    log(f"Subagent pending (blocked_once={state.subagent_blocked_once})")

    # Check if tier changed — abandon pending
    tier_changed = old_tier and old_tier != ctx.tier
    if tier_changed:
        log(
            f"Tier changed ({old_tier} -> {ctx.tier}) while subagent pending - resetting"
        )
        state.subagent_pending = False
        state.subagent_blocked_once = False
        return

    # Background agent returns results JSON in transcript (inline mode)
    results = read_review_results(
        ctx.transcript_path, state.session_hash, mode="inline"
    )

    if results.get("round_id") == state.round_id:
        log(f"Found subagent results for round {state.round_id}")
        state.subagent_pending = False
        state.subagent_blocked_once = False
        _process_subagent_results(state, ctx, results)
        return

    # No results yet — check timeout
    timed_out = False
    if state.subagent_dispatch_time:
        try:
            dispatch_dt = datetime.fromisoformat(state.subagent_dispatch_time)
            elapsed = (datetime.now() - dispatch_dt).total_seconds()
            if elapsed > SUBAGENT_TIMEOUT:
                log(f"Subagent timed out ({elapsed:.0f}s > {SUBAGENT_TIMEOUT}s)")
                timed_out = True
        except (ValueError, TypeError):
            log("Invalid subagent_dispatch_time, treating as timed out")
            timed_out = True

    if timed_out:
        log("Falling back to inline agent mode due to subagent timeout")
        state.subagent_pending = False
        state.subagent_blocked_once = False
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.save()

        # Fall back to full inline agent instructions
        code_hunks, import_violations = get_code_hunks_and_violations(ctx)
        _, pending_agents, integration_context = get_pending_agents_and_context(
            state, ctx
        )
        review_instructions = get_review_instructions(
            tier=ctx.tier,
            diff_size=abs(ctx.incremental_diff),
            total_file_count=len(ctx.all_modified_files),
            new_file_count=len(ctx.incremental_files),
            file_list=ctx.file_list,
            pending_agents=pending_agents,
            passed_agents=state.passed_agents,
            round_id=state.round_id,
            auto_continue_count=state.auto_continue_count,
            fail_count=state.fail_count,
            file_contexts=ctx.file_contexts,
            session_hash=state.session_hash,
            integration_context=integration_context,
            files=list(ctx.all_modified_files),
            code_hunks=code_hunks,
            violation_history=state.violation_history,
            import_violations=import_violations,
        )
        block_with_message(review_instructions)

    elif state.subagent_blocked_once:
        log("Already blocked once while waiting for subagent — allowing stop")
        allow_stop("Subagent pending, blocked once already")
    else:
        log("Blocking once while waiting for subagent")
        state.subagent_blocked_once = True
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.save()
        block_with_message(_get_pending_message(state.round_id))


def run_subagent_mode(
    state: ReviewState, ctx: ReviewContext, old_tier: str, old_round_id: str
) -> None:
    """Subagent mode: spawn ONE background agent that handles everything."""
    check_completion_guards(state, ctx)

    # Handle pending subagent
    if state.subagent_pending:
        handle_subagent_pending(state, ctx, old_tier, old_round_id)

    handle_tier_change(state, ctx.tier)
    ensure_round_id(state)

    # Check pending agents
    _, pending_agents, integration_context = get_pending_agents_and_context(state, ctx)
    if not pending_agents:
        # All passed already — use minimal message
        state.auto_continue_count += 1
        if state.auto_continue_count >= MAX_AUTO_CONTINUES:
            state.last_total_diff = ctx.current_total_diff
            state.last_files_seen = ctx.all_files_seen
            state.tier = ctx.tier
            state.completed = True
            state.save()
            allow_stop("All passed, max auto-continues (subagent)")
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.save()
        block_with_message(_get_passed_message(state.auto_continue_count))

    # Circuit breaker
    check_circuit_breaker(state, ctx, old_round_id)

    # Track review attempts
    if state.round_id == old_round_id:
        state.review_attempts += 1
    else:
        state.review_attempts = 1

    # Build payload and dispatch
    log(
        f"Dispatching subagent {ctx.tier} review, round {state.round_id}, pending: {pending_agents} (attempt {state.review_attempts})"
    )

    code_hunks, import_violations = get_code_hunks_and_violations(ctx)

    payload = _build_subagent_payload(
        session_hash=state.session_hash,
        tier=ctx.tier,
        diff_size=abs(ctx.incremental_diff),
        total_file_count=len(ctx.all_modified_files),
        new_file_count=len(ctx.incremental_files),
        file_list=ctx.file_list,
        pending_agents=pending_agents,
        passed_agents=state.passed_agents,
        round_id=state.round_id,
        auto_continue_count=state.auto_continue_count,
        fail_count=state.fail_count,
        file_contexts=ctx.file_contexts,
        integration_context=integration_context,
        files=list(ctx.all_modified_files),
        code_hunks=code_hunks,
        violation_history=state.violation_history,
        import_violations=import_violations,
    )
    instructions_file = _write_subagent_instructions(state.session_hash, payload)

    state.last_total_diff = ctx.current_total_diff
    state.last_files_seen = ctx.all_files_seen
    state.tier = ctx.tier
    state.subagent_pending = True
    state.subagent_dispatch_time = datetime.now().isoformat()
    state.subagent_blocked_once = False
    state.save()

    block_with_message(
        _get_dispatch_message(
            tier=ctx.tier,
            round_id=state.round_id,
            instructions_file=instructions_file,
            pending_agent_count=len(pending_agents),
            diff_size=abs(ctx.incremental_diff),
            file_count=len(ctx.all_modified_files),
            auto_continue_count=state.auto_continue_count,
        )
    )
