"""Shared review flow logic — eliminates duplication between agent/delegated modes."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from stop_design_audit.agents import get_required_agents
from stop_design_audit.classify import (
    check_import_violations,
    severities_at_or_above,
    should_run_integration_review,
)
from stop_design_audit.config import (
    DEEP_AUTO_FIX,
    DEEP_AUTO_FIX_ENV,
    MAX_AUTO_CONTINUES,
    MAX_FAIL_RETRIES,
    MAX_PREVIEW_CHARS,
    MAX_REVIEW_ATTEMPTS,
    ROUND_ID_LENGTH,
    STATUS_FAIL,
    STATUS_PASS,
)
from stop_design_audit.exit_helpers import allow_stop, block_with_message, log
from stop_design_audit.instructions import (
    GIT_FIX_CONSTRAINT,
    GIT_READONLY_CONSTRAINT,
    get_plan_agent_instructions,
)
from stop_design_audit.metrics import log_review_metrics
from stop_design_audit.results import read_review_results
from stop_design_audit.state import ReviewState, update_violation_history
from stop_design_audit.transcript import (
    extract_changed_hunks,
    extract_pre_review_context,
)


@dataclass
class ReviewContext:
    """Computed parameters for the current review cycle."""

    transcript_path: str
    incremental_diff: int
    incremental_files: set[str]
    all_modified_files: set[str]
    all_files_seen: set[str]
    current_total_diff: int
    tier: str
    file_list: str
    file_contexts: set[str]
    module_rules: dict = field(default_factory=dict)


def get_continue_message(auto_continue_count: int) -> str:
    """Return the continuation message based on auto-continue progress."""
    if auto_continue_count < MAX_AUTO_CONTINUES - 1:
        return "Continue with implementation. If tasks are finished, identify next logical steps."
    return "If tasks remain, continue with implementation. Otherwise, identify next logical steps or improvements to consider."


def check_completion_guards(state: ReviewState, ctx: ReviewContext) -> None:
    """Check completed, max_continues, max_fails. Exits if triggered.

    This is shared between agent and delegated modes.
    """
    old_tier = state.tier

    if state.completed:
        log("Review cycle already completed - allowing stop")
        state.completed = False
        state.round_id = ""
        state.passed_agents = []
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.save()
        allow_stop("Review cycle already completed")

    if state.auto_continue_count >= MAX_AUTO_CONTINUES:
        log(f"Max auto continues reached ({MAX_AUTO_CONTINUES}) - allowing stop")
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = old_tier or ctx.tier
        state.completed = True
        state.save()
        allow_stop("Max auto-continues reached")

    if state.fail_count >= MAX_FAIL_RETRIES:
        log(
            f"Max fail retries reached ({MAX_FAIL_RETRIES}) - allowing stop for user intervention"
        )
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = old_tier or ctx.tier
        state.completed = True
        state.save()
        context_str = extract_pre_review_context(ctx.transcript_path)
        block_with_message(
            f"\u26a0\ufe0f Review retries exhausted ({state.fail_count} failures, max {MAX_FAIL_RETRIES}). "
            f"Automated reviews found persistent issues that could not be resolved automatically."
            f"{context_str}\n\nReport this to the user, then resume your prior task."
        )


def handle_tier_change(state: ReviewState, new_tier: str) -> None:
    """Reset state if tier changed."""
    if state.tier and state.tier != new_tier:
        log(f"Tier changed ({state.tier} -> {new_tier}) - starting new review round")
        state.passed_agents = []
        state.fail_count = 0
        state.round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]


def ensure_round_id(state: ReviewState) -> None:
    """Generate a new round_id if none exists."""
    if not state.round_id:
        state.round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]
        log(f"Starting review round {state.round_id}")


def get_pending_agents_and_context(
    state: ReviewState, ctx: ReviewContext
) -> tuple[list[str], list[str], dict | None]:
    """Determine required agents, pending agents, and integration context.

    Returns: (required_agents, pending_agents, integration_context)
    """
    required_agents = get_required_agents(ctx.tier)
    needs_integration, top_dirs, crit_patterns = should_run_integration_review(
        list(ctx.incremental_files)
    )
    integration_context = None
    if needs_integration:
        required_agents = required_agents + ["integration_checker"]
        integration_context = {"dirs": top_dirs, "patterns": crit_patterns}

    pending_agents = [a for a in required_agents if a not in state.passed_agents]
    return required_agents, pending_agents, integration_context


def get_code_hunks_and_violations(
    ctx: ReviewContext,
) -> tuple[dict[str, str], list[str]]:
    """Extract code hunks and check import violations.

    Returns: (code_hunks, import_violations)
    """
    code_hunks = extract_changed_hunks(
        transcript_path=ctx.transcript_path,
        start_position=0,
        files=ctx.all_modified_files,
        max_preview_chars=MAX_PREVIEW_CHARS,
    )
    log(f"Extracted code hunks for {len(code_hunks)} files")

    import_violations = check_import_violations(
        files=list(ctx.all_modified_files),
        hunks=code_hunks,
        module_rules=ctx.module_rules,
    )
    if import_violations:
        log(f"Found {len(import_violations)} import violations")

    return code_hunks, import_violations


def check_circuit_breaker(
    state: ReviewState, ctx: ReviewContext, old_round_id: str
) -> None:
    """Treat as pass if we've hit MAX_REVIEW_ATTEMPTS for the same round."""
    if state.round_id == old_round_id and state.review_attempts >= MAX_REVIEW_ATTEMPTS:
        log(
            f"Circuit breaker: {state.review_attempts} attempts for round {state.round_id} - treating as pass"
        )
        handle_all_passed(state, ctx)


def process_results(
    state: ReviewState,
    ctx: ReviewContext,
    results: dict,
) -> bool:
    """Process review results from transcript/file.

    Updates state with passed/failed agents. Calls handle_all_passed or
    handle_deep_failure if applicable (which exit).

    Returns True if results were found and processed, False if no matching results.
    """
    if results.get("round_id") != state.round_id:
        return False

    log(f"Found results for round {state.round_id}")
    agents_results = results.get("agents", {})

    # Update passed_agents from results
    for agent_id, data in agents_results.items():
        if (
            isinstance(data, dict)
            and data.get("status") == STATUS_PASS
            and agent_id not in state.passed_agents
        ):
            state.passed_agents.append(agent_id)
            log(f"  Agent {agent_id}: PASSED")

    # Count failed agents
    failed_agents = [
        agent_id
        for agent_id, data in agents_results.items()
        if isinstance(data, dict) and data.get("status") == STATUS_FAIL
    ]

    # Get required agents for metrics
    required, _, _ = get_pending_agents_and_context(state, ctx)

    if failed_agents:
        state.fail_count += len(failed_agents)
        log(
            f"  {len(failed_agents)} agent(s) failed: {failed_agents} - fail_count now {state.fail_count}"
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

        if ctx.tier == "deep":
            handle_deep_failure(state, ctx, failed_agents, agents_results)
        # Non-deep failure: save and re-dispatch (falls through)
    else:
        all_agents_passed = all(a in state.passed_agents for a in required)
        if all_agents_passed:
            log_review_metrics(
                tier=ctx.tier,
                diff_chars=abs(ctx.incremental_diff),
                file_count=len(ctx.incremental_files),
                agents=required,
                outcome=STATUS_PASS,
                fail_count=state.fail_count,
                session_id=state.session_id,
            )
            handle_all_passed(state, ctx)

    return True


def handle_all_passed(state: ReviewState, ctx: ReviewContext) -> None:
    """Handle the case when all review agents have passed. Exits."""
    state.auto_continue_count += 1
    log(f"All agents passed! auto_continue_count now {state.auto_continue_count}")

    if state.auto_continue_count >= MAX_AUTO_CONTINUES:
        log("Max auto continues reached - marking complete")
        state.last_total_diff = ctx.current_total_diff
        state.last_files_seen = ctx.all_files_seen
        state.tier = ctx.tier
        state.fail_count = 0
        state.round_id = ""
        state.passed_agents = []
        state.completed = True
        state.save()
        allow_stop("All passed, max auto-continues reached")

    state.last_total_diff = ctx.current_total_diff
    state.last_files_seen = ctx.all_files_seen
    state.tier = ctx.tier
    state.fail_count = 0
    state.round_id = uuid.uuid4().hex[:ROUND_ID_LENGTH]
    state.passed_agents = []
    state.completed = False
    state.save()

    continue_msg = get_continue_message(state.auto_continue_count)
    context_str = extract_pre_review_context(ctx.transcript_path)
    log(
        f"Review passed - issuing auto-continue [Auto-continue {state.auto_continue_count} of {MAX_AUTO_CONTINUES}]"
    )
    block_with_message(
        f"All reviews passed. [Auto-continue {state.auto_continue_count} of {MAX_AUTO_CONTINUES}] {continue_msg}{context_str}"
    )


def handle_deep_failure(
    state: ReviewState,
    ctx: ReviewContext,
    failed_agents: list[str],
    agents_results: dict,
) -> None:
    """Handle deep review failure. Exits."""
    total_issues = sum(
        len(data.get("issues", []))
        for data in agents_results.values()
        if isinstance(data, dict)
    )
    log(f"Deep review failed with {total_issues} issues")

    state.last_total_diff = ctx.current_total_diff
    state.last_files_seen = ctx.all_files_seen
    state.tier = ctx.tier
    state.completed = True
    state.save()

    effective_auto_fix = os.environ.get(DEEP_AUTO_FIX_ENV, "").lower() or DEEP_AUTO_FIX

    if effective_auto_fix != "none":
        # Auto-fix mode
        qualifying_severities = severities_at_or_above(effective_auto_fix)
        severity_list = ", ".join(qualifying_severities)

        all_issues = []
        for agent_id, data in agents_results.items():
            if not isinstance(data, dict):
                continue
            for issue in data.get("issues", []):
                if isinstance(issue, dict):
                    issue_copy = issue.copy()
                    issue_copy["found_by"] = agent_id
                    all_issues.append(issue_copy)

        fixable = [
            i
            for i in all_issues
            if i.get("severity", "").lower() in qualifying_severities
        ]
        reportable = [
            i
            for i in all_issues
            if i.get("severity", "").lower() not in qualifying_severities
        ]

        fix_list = "\n".join(
            f"- [{i.get('severity', '?').upper()}] {i.get('file', '?')}:{i.get('line', '?')} - {i.get('description', '?')}"
            for i in fixable
        )
        report_list = "\n".join(
            f"- [{i.get('severity', '?').upper()}] {i.get('file', '?')}:{i.get('line', '?')} - {i.get('description', '?')}"
            for i in reportable
        )

        context_str = extract_pre_review_context(ctx.transcript_path)
        instructions = f"\u26a0\ufe0f DEEP REVIEW FAILED: {len(failed_agents)} agent(s) found {total_issues} issue(s).\n\n"

        if fixable:
            instructions += (
                f"Spawn ONE general-purpose subagent (model='sonnet') to fix these {len(fixable)} issue(s) "
                f"at severity [{severity_list}]:\n{fix_list}\n\n"
                f"Do NOT fix issues yourself in the main context.\n"
                f"{GIT_FIX_CONSTRAINT}\n"
            )
        if reportable:
            instructions += f"\nReport only (do not fix) \u2014 {len(reportable)} lower-severity issue(s):\n{report_list}\n"
        if not fixable:
            instructions += "No issues match the auto-fix threshold. Review the reported issues above.\n"

        instructions += (
            f"\nAfter the subagent completes, resume your prior task.{context_str}"
            f"{GIT_READONLY_CONSTRAINT}"
        )

        log(
            f"Deep auto-fix mode ({effective_auto_fix}): {len(fixable)} fixable, {len(reportable)} report-only"
        )
        block_with_message(instructions)

    # Default: "none" — stop and wait, trigger plan agent
    log("Deep review - triggering plan agent (DEEP_AUTO_FIX=none)")
    results = read_review_results(ctx.transcript_path, state.session_hash)
    code_hunks = extract_changed_hunks(
        transcript_path=ctx.transcript_path,
        start_position=0,
        files=ctx.all_modified_files,
        max_preview_chars=MAX_PREVIEW_CHARS,
    )
    plan_instructions = get_plan_agent_instructions(
        session_hash=state.session_hash,
        review_results=results,
        files=list(ctx.all_modified_files),
        violation_history=state.violation_history,
        code_hunks=code_hunks,
    )
    block_with_message(
        f"\u26a0\ufe0f DEEP REVIEW FAILED: {len(failed_agents)} agent(s) found {total_issues} issue(s).\n\n{plan_instructions}"
        f"{GIT_READONLY_CONSTRAINT}"
    )
