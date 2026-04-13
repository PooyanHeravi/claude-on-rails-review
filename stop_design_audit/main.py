"""Main entry point — clean top-level flow with mode dispatch."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from stop_design_audit.agents import get_required_agents
from stop_design_audit.api_mode import call_anthropic_review
from stop_design_audit.classify import (
    classify_review_tier,
    detect_file_contexts,
    load_module_boundaries,
    should_run_integration_review,
)
from stop_design_audit.cleanup import cleanup_stale_files, scavenge_abandoned_reviews
from stop_design_audit.config import (
    API_DIFF_THRESHOLD,
    MAX_AUTO_CONTINUES,
    MAX_FAIL_RETRIES,
    REVIEW_MODE,
    REVIEW_MODE_ENV,
    SKIP_HOOK_ENV,
    STATUS_PASS,
    get_session_hash,
)
from stop_design_audit.delegated import (
    build_coordinator_payload,
    get_delegated_review_message,
    write_coordinator_instructions,
)
from stop_design_audit.subagent import run_subagent_mode
from stop_design_audit.exit_helpers import allow_stop, block_with_message, log
from stop_design_audit.git_guard import check_and_recover_stash, get_stash_count
from stop_design_audit.flow import (
    ReviewContext,
    check_circuit_breaker,
    check_completion_guards,
    ensure_round_id,
    get_code_hunks_and_violations,
    get_continue_message,
    get_pending_agents_and_context,
    handle_all_passed,
    handle_deep_failure,
    handle_tier_change,
    process_results,
)
from stop_design_audit.instructions import (
    format_files_grouped,
    get_review_instructions,
)
from stop_design_audit.results import read_review_results
from stop_design_audit.state import ReviewState
from stop_design_audit.transcript import (
    extract_pre_review_context,
    parse_transcript_total,
)

from stop_design_audit.config import (
    DELEGATED_TIMEOUT,
    STATUS_FAIL,
)


def main() -> None:
    """Main entry point for the stop hook."""
    log("=" * 50)
    log("HOOK STARTED")

    # --- Early exits ---
    if os.environ.get(SKIP_HOOK_ENV, "").strip() == "1":
        allow_stop("Hook skipped via CLAUDE_HOOK_SKIP=1")

    cleanup_stale_files()

    effective_mode = os.environ.get(REVIEW_MODE_ENV, "").lower().strip() or REVIEW_MODE
    # Scavenge runs after session hash is computed (see below)
    if effective_mode not in ("agent", "delegated", "api", "subagent"):
        log(f"Invalid REVIEW_MODE '{effective_mode}', falling back to 'agent'")
        effective_mode = "agent"

    # --- Parse input ---
    try:
        raw_input = sys.stdin.read()
        log(f"Stdin: {len(raw_input)} chars")
        input_data = json.loads(raw_input, strict=False)
        log(f"Input keys: {list(input_data.keys())}")
    except Exception as e:
        log(f"Error reading stdin: {e}")
        allow_stop("Error reading stdin")

    transcript_path = input_data.get("transcript_path", "")
    log(f"transcript_path from stdin: {transcript_path}")
    if not transcript_path:
        allow_stop("No transcript_path in input")

    # --- Load state ---
    session_hash = get_session_hash(transcript_path)
    log(f"Session hash: {session_hash}")
    scavenge_abandoned_reviews(session_hash)
    state = ReviewState.from_file(session_hash)
    old_tier = state.tier
    old_round_id = state.round_id
    state.detect_session(transcript_path)
    check_and_recover_stash(state)

    # --- Parse transcript ---
    parse_result = parse_transcript_total(transcript_path)
    current_total_diff = parse_result["total_diff_chars"]
    all_modified_files = parse_result["files_modified"]

    incremental_diff = current_total_diff - state.last_total_diff
    incremental_files = all_modified_files - state.last_files_seen
    all_files_seen = state.last_files_seen | all_modified_files

    log(
        f"Total diff: {current_total_diff} chars, incremental: {incremental_diff} chars"
    )
    log(
        f"Files modified: {len(all_modified_files)}, edits: {parse_result['edit_count']}, writes: {parse_result['write_count']}"
    )

    # --- Early exit: no code modified ---
    code_modified = "Edit" in parse_result["tools"] or "Write" in parse_result["tools"]
    if not code_modified:
        allow_stop("No code modified in this session")

    # --- Early exit: deep review completed ---
    if state.completed and old_tier == "deep":
        log("Review cycle completed after deep review - auto-approving")
        state.last_total_diff = current_total_diff
        state.last_files_seen = all_files_seen
        state.fail_count = 0
        state.round_id = ""
        state.passed_agents = []
        state.completed = False
        state.save()
        allow_stop("Deep review cycle completed")

    # --- Zero-diff path ---
    if incremental_diff == 0 and len(incremental_files) == 0:
        _handle_zero_diff(
            state=state,
            transcript_path=transcript_path,
            session_hash=session_hash,
            current_total_diff=current_total_diff,
            all_modified_files=all_modified_files,
            all_files_seen=all_files_seen,
            old_tier=old_tier,
        )

    # --- Classify tier ---
    tier = classify_review_tier(incremental_diff, len(incremental_files))
    log(
        f"Tier: {tier} (incremental_diff={incremental_diff}, incremental_files={len(incremental_files)})"
    )

    file_list = format_files_grouped(list(all_modified_files))
    file_contexts = detect_file_contexts(list(all_modified_files))
    log(f"File contexts: {file_contexts}")
    module_rules = load_module_boundaries()

    # --- Skip tier ---
    if tier == "skip":
        _handle_skip_tier(
            state=state,
            transcript_path=transcript_path,
            current_total_diff=current_total_diff,
            all_files_seen=all_files_seen,
            incremental_diff=incremental_diff,
            incremental_files=incremental_files,
            old_tier=old_tier,
        )

    # --- Build review context ---
    ctx = ReviewContext(
        transcript_path=transcript_path,
        incremental_diff=incremental_diff,
        incremental_files=incremental_files,
        all_modified_files=all_modified_files,
        all_files_seen=all_files_seen,
        current_total_diff=current_total_diff,
        tier=tier,
        file_list=file_list,
        file_contexts=file_contexts,
        module_rules=module_rules,
    )

    # --- Dispatch by mode ---
    if effective_mode == "agent":
        _run_agent_mode(state, ctx, old_round_id)
    elif effective_mode == "delegated":
        _run_delegated_mode(state, ctx, old_tier, old_round_id)
    elif effective_mode == "subagent":
        run_subagent_mode(state, ctx, old_tier, old_round_id)
    else:
        _run_api_mode(state, ctx)


# =============================================================================
# Pre-review handlers
# =============================================================================


def _handle_zero_diff(
    *,
    state: ReviewState,
    transcript_path: str,
    session_hash: str,
    current_total_diff: int,
    all_modified_files: set[str],
    all_files_seen: set[str],
    old_tier: str,
) -> None:
    """Handle the zero incremental diff case."""
    if state.round_id:
        results = read_review_results(transcript_path, session_hash)
        if results.get("round_id") == state.round_id:
            log(f"Found results for pending round {state.round_id}")
            agents_results = results.get("agents", {})

            required_agents = (
                get_required_agents(old_tier)
                if old_tier in ("quick", "standard", "deep")
                else []
            )
            needs_integration, _, _ = should_run_integration_review(
                list(state.last_files_seen)
            )
            if needs_integration:
                required_agents = required_agents + ["integration_checker"]

            for agent_id, data in agents_results.items():
                if (
                    isinstance(data, dict)
                    and data.get("status") == STATUS_PASS
                    and agent_id not in state.passed_agents
                ):
                    state.passed_agents.append(agent_id)
                    log(f"  Agent {agent_id}: PASSED")

            all_agents_passed = all(a in state.passed_agents for a in required_agents)

            if all_agents_passed and required_agents:
                # Build a minimal ctx for handle_all_passed
                ctx = ReviewContext(
                    transcript_path=transcript_path,
                    incremental_diff=0,
                    incremental_files=set(),
                    all_modified_files=all_modified_files,
                    all_files_seen=all_files_seen,
                    current_total_diff=current_total_diff,
                    tier=old_tier,
                    file_list="",
                    file_contexts=set(),
                )
                handle_all_passed(state, ctx)
            else:
                failed_agents = [
                    agent_id
                    for agent_id, data in agents_results.items()
                    if isinstance(data, dict) and data.get("status") == STATUS_FAIL
                ]
                if failed_agents and old_tier == "deep":
                    accumulated_fail_count = state.fail_count + len(failed_agents)
                    if accumulated_fail_count >= MAX_FAIL_RETRIES:
                        log(
                            f"Max fail retries in zero-diff path ({accumulated_fail_count} >= {MAX_FAIL_RETRIES})"
                        )
                        state.last_total_diff = current_total_diff
                        state.last_files_seen = all_files_seen
                        state.fail_count = accumulated_fail_count
                        state.completed = True
                        state.save()
                        context_str = extract_pre_review_context(transcript_path)
                        block_with_message(
                            f"\u26a0\ufe0f Review retries exhausted ({accumulated_fail_count} failures, max {MAX_FAIL_RETRIES}). "
                            f"Automated reviews found persistent issues that could not be resolved automatically."
                            f"{context_str}\n\nReport this to the user, then resume your prior task."
                        )
                    log(
                        f"  {len(failed_agents)} agent(s) failed: {failed_agents} - triggering plan agent"
                    )
                    ctx = ReviewContext(
                        transcript_path=transcript_path,
                        incremental_diff=0,
                        incremental_files=set(),
                        all_modified_files=all_modified_files,
                        all_files_seen=all_files_seen,
                        current_total_diff=current_total_diff,
                        tier=old_tier,
                        file_list="",
                        file_contexts=set(),
                    )
                    state.fail_count = accumulated_fail_count
                    handle_deep_failure(state, ctx, failed_agents, agents_results)

    if state.auto_continue_count > 0:
        allow_stop(f"No new changes after auto-continue {state.auto_continue_count}")

    log("No incremental changes - allowing stop")
    state.last_total_diff = current_total_diff
    state.last_files_seen = all_files_seen
    state.tier = old_tier or "skip"
    state.save()
    allow_stop("No incremental changes")


def _handle_skip_tier(
    *,
    state: ReviewState,
    transcript_path: str,
    current_total_diff: int,
    all_files_seen: set[str],
    incremental_diff: int,
    incremental_files: set[str],
    old_tier: str,
) -> None:
    """Handle the skip tier case."""
    state.auto_continue_count += 1
    log(
        f"Skip tier - auto_continue_count now {state.auto_continue_count}, old_tier: {old_tier}"
    )

    if state.auto_continue_count >= MAX_AUTO_CONTINUES:
        log("Max auto-continues reached on skip tier - allowing stop")
        state.last_total_diff = current_total_diff
        state.last_files_seen = all_files_seen
        state.tier = "skip"
        state.fail_count = 0
        state.round_id = ""
        state.passed_agents = []
        state.completed = True
        state.save()
        allow_stop("Skip tier max auto-continues reached")
    elif old_tier == "skip":
        allow_stop("Consecutive skip without review")
    else:
        continue_msg = "Continue with implementation. If tasks are finished, identify next logical steps."
        # PRESERVE last_total_diff — let changes accumulate
        state.tier = "skip"
        state.save()
        context_str = extract_pre_review_context(transcript_path)
        block_with_message(
            f"Minimal changes ({abs(incremental_diff)} chars, {len(incremental_files)} files). "
            f"[Auto-continue {state.auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}{context_str}"
        )


# =============================================================================
# Mode dispatch
# =============================================================================


def _run_agent_mode(state: ReviewState, ctx: ReviewContext, old_round_id: str) -> None:
    """Agent mode: Spawn Task subagents for review."""
    check_completion_guards(state, ctx)
    handle_tier_change(state, ctx.tier)
    ensure_round_id(state)

    # Check for existing results
    results = read_review_results(ctx.transcript_path, state.session_hash)
    process_results(state, ctx, results)

    # Check pending agents
    required_agents, pending_agents, integration_context = (
        get_pending_agents_and_context(state, ctx)
    )
    if not pending_agents:
        handle_all_passed(state, ctx)

    # Circuit breaker
    check_circuit_breaker(state, ctx, old_round_id)

    # Track review attempts
    if state.round_id == old_round_id:
        state.review_attempts += 1
    else:
        state.review_attempts = 1

    # Save state and trigger review
    log(
        f"Triggering {ctx.tier} review, round {state.round_id}, pending: {pending_agents} (attempt {state.review_attempts})"
    )
    state.last_total_diff = ctx.current_total_diff
    state.last_files_seen = ctx.all_files_seen
    state.tier = ctx.tier
    state.stash_count_at_dispatch = get_stash_count()
    state.save()

    code_hunks, import_violations = get_code_hunks_and_violations(ctx)

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


def _run_delegated_mode(
    state: ReviewState, ctx: ReviewContext, old_tier: str, old_round_id: str
) -> None:
    """Delegated mode: Spawn ONE background coordinator agent."""
    check_completion_guards(state, ctx)

    # Handle pending coordinator
    if state.delegated_pending:
        _handle_delegated_pending(state, ctx, old_tier, old_round_id)

    handle_tier_change(state, ctx.tier)
    ensure_round_id(state)

    # Check pending agents
    required_agents, pending_agents, integration_context = (
        get_pending_agents_and_context(state, ctx)
    )
    if not pending_agents:
        handle_all_passed(state, ctx)

    # Circuit breaker
    check_circuit_breaker(state, ctx, old_round_id)

    # Track review attempts
    if state.round_id == old_round_id:
        state.review_attempts += 1
    else:
        state.review_attempts = 1

    # Build coordinator payload and dispatch
    log(
        f"Dispatching delegated {ctx.tier} review, round {state.round_id}, pending: {pending_agents} (attempt {state.review_attempts})"
    )

    code_hunks, import_violations = get_code_hunks_and_violations(ctx)

    payload = build_coordinator_payload(
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
    instructions_file = write_coordinator_instructions(state.session_hash, payload)

    state.last_total_diff = ctx.current_total_diff
    state.last_files_seen = ctx.all_files_seen
    state.tier = ctx.tier
    state.delegated_pending = True
    state.delegated_dispatch_time = datetime.now().isoformat()
    state.delegated_blocked_once = False
    state.stash_count_at_dispatch = get_stash_count()
    state.save()

    delegated_message = get_delegated_review_message(
        tier=ctx.tier,
        round_id=state.round_id,
        instructions_file=instructions_file,
        pending_agent_count=len(pending_agents),
        diff_size=abs(ctx.incremental_diff),
        file_count=len(ctx.all_modified_files),
        auto_continue_count=state.auto_continue_count,
    )
    block_with_message(delegated_message)


def _handle_delegated_pending(
    state: ReviewState, ctx: ReviewContext, old_tier: str, old_round_id: str
) -> None:
    """Handle the case when a delegated coordinator is pending."""
    log(f"Delegated coordinator pending (blocked_once={state.delegated_blocked_once})")

    # Check if tier changed — abandon pending coordinator
    tier_changed = old_tier and old_tier != ctx.tier
    if tier_changed:
        log(
            f"Tier changed ({old_tier} -> {ctx.tier}) while delegated pending - resetting"
        )
        state.delegated_pending = False
        state.delegated_blocked_once = False
        return  # Fall through to dispatch

    # Check for results from coordinator
    results = read_review_results(ctx.transcript_path, state.session_hash)
    if results.get("round_id") == state.round_id:
        log(f"Found delegated results for round {state.round_id}")
        state.delegated_pending = False
        state.delegated_blocked_once = False
        process_results(state, ctx, results)
        # If process_results didn't exit, falls through to re-dispatch
        return

    # No results yet — check timeout
    timed_out = False
    if state.delegated_dispatch_time:
        try:
            dispatch_dt = datetime.fromisoformat(state.delegated_dispatch_time)
            elapsed = (datetime.now() - dispatch_dt).total_seconds()
            if elapsed > DELEGATED_TIMEOUT:
                log(
                    f"Delegated coordinator timed out ({elapsed:.0f}s > {DELEGATED_TIMEOUT}s)"
                )
                timed_out = True
        except (ValueError, TypeError):
            log("Invalid delegated_dispatch_time, treating as timed out")
            timed_out = True

    if timed_out:
        # Timeout: fall back to inline agent mode
        log("Falling back to inline agent mode due to timeout")
        state.delegated_pending = False
        state.delegated_blocked_once = False
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.save()

        # Build full inline review instructions
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

    elif state.delegated_blocked_once:
        log("Already blocked once while waiting for coordinator — allowing stop")
        allow_stop("Delegated coordinator pending, blocked once already")
    else:
        # First time waiting — block once
        log("Blocking once while waiting for coordinator results")
        state.delegated_blocked_once = True
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.save()
        context_str = extract_pre_review_context(ctx.transcript_path)
        block_with_message(
            f"Background review still running (round {state.round_id}). "
            f"Continue with your current task. When the coordinator agent completes, "
            f"output its results between the review markers.{context_str}"
        )


def _run_api_mode(state: ReviewState, ctx: ReviewContext) -> None:
    """API mode (fallback): Call Anthropic API directly."""
    use_sonnet = abs(ctx.incremental_diff) >= API_DIFF_THRESHOLD
    model_name = "Sonnet" if use_sonnet else "Haiku"
    log(f"API mode: Using {model_name} (diff {ctx.incremental_diff} chars)")

    review_result = call_anthropic_review(ctx.transcript_path, use_sonnet)
    violations = review_result.get("violations", [])

    if not violations:
        state.auto_continue_count += 1
        if state.auto_continue_count >= MAX_AUTO_CONTINUES:
            log("Max auto continues reached - allowing stop")
            state.last_total_diff = ctx.current_total_diff
            state.last_files_seen = ctx.all_files_seen
            state.tier = "api"
            state.completed = True
            state.save()
            allow_stop("API mode max auto-continues reached")

        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = "api"
        state.save()
        continue_msg = get_continue_message(state.auto_continue_count)
        context_str = extract_pre_review_context(ctx.transcript_path)
        block_with_message(
            f"Code review passed ({model_name}). No violations found.\n\n"
            f"[Auto-continue {state.auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}{context_str}"
        )

    violation_text = "\n".join(violations)
    log(f"Violations found (reviewed by {model_name})")
    block_with_message(
        f"VIOLATIONS FOUND (reviewed by {model_name}):\n{violation_text}\n\nPlease fix these before proceeding."
    )
