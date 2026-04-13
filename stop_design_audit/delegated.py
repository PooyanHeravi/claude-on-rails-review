"""Delegated review mode — background coordinator payload and dispatch."""

from __future__ import annotations

import json
from pathlib import Path

from stop_design_audit.agents import AGENT_DEFINITIONS
from stop_design_audit.config import (
    MAX_AUTO_CONTINUES,
    MAX_FAIL_RETRIES,
    get_coordinator_instructions_file,
)
from stop_design_audit.exit_helpers import log
from stop_design_audit.instructions import GIT_READONLY_CONSTRAINT


def build_coordinator_payload(
    tier: str,
    diff_size: int,
    total_file_count: int,
    new_file_count: int,
    file_list: str,
    pending_agents: list[str],
    passed_agents: list[str],
    round_id: str,
    auto_continue_count: int,
    fail_count: int,
    file_contexts: set[str],
    integration_context: dict | None = None,
    files: list[str] = None,
    code_hunks: dict[str, str] = None,
    violation_history: dict[str, dict] = None,
    import_violations: list[str] = None,
) -> dict:
    """Build the full review payload for the background coordinator agent."""
    agent_defs = {}
    for agent_id in pending_agents:
        if agent_id in AGENT_DEFINITIONS:
            defn = dict(AGENT_DEFINITIONS[agent_id])
            context_checks = defn.get("context_checks", {})
            extra_checks = [
                context_checks[ctx] for ctx in file_contexts if ctx in context_checks
            ]
            if extra_checks:
                defn["resolved_extra_checks"] = extra_checks
            agent_defs[agent_id] = defn

    return {
        "tier": tier,
        "round_id": round_id,
        "diff_size": diff_size,
        "total_file_count": total_file_count,
        "new_file_count": new_file_count,
        "file_list": file_list,
        "pending_agents": pending_agents,
        "passed_agents": passed_agents,
        "agent_definitions": agent_defs,
        "file_contexts": list(file_contexts),
        "integration_context": integration_context,
        "files": files or [],
        "code_hunks": code_hunks or {},
        "violation_history": violation_history or {},
        "import_violations": import_violations or [],
        "auto_continue_count": auto_continue_count,
        "fail_count": fail_count,
        "max_auto_continues": MAX_AUTO_CONTINUES,
        "max_fail_retries": MAX_FAIL_RETRIES,
        "results_schema": {
            "round_id": round_id,
            "format": {
                "status": "pass|fail",
                "issues": [
                    {
                        "file": "...",
                        "line": "N",
                        "severity": "critical|high|medium|low",
                        "description": "...",
                    }
                ],
            },
            "fail_criteria": "critical issue OR 2+ high issues",
        },
    }


def write_coordinator_instructions(session_hash: str, payload: dict) -> Path:
    """Write coordinator payload to JSON file. Returns the absolute file path."""
    instructions_file = get_coordinator_instructions_file(session_hash)
    instructions_file.write_text(json.dumps(payload, indent=2))
    log(f"Wrote coordinator instructions to {instructions_file.name}")
    return instructions_file


def get_delegated_review_message(
    tier: str,
    round_id: str,
    instructions_file: Path,
    pending_agent_count: int,
    diff_size: int,
    file_count: int,
    auto_continue_count: int,
) -> str:
    """Build the minimal block message for delegated review mode."""
    abs_path = str(instructions_file.resolve()).replace("\\", "/")
    return (
        f"DELEGATED_REVIEW: +{diff_size} chars, {file_count} files [{tier.upper()}] "
        f"[Round: {round_id}]\n\n"
        f"Spawn 1 background coordinator agent (general-purpose, run_in_background=true) with this prompt:\n\n"
        f'"You are a code review coordinator. Read your instructions from {abs_path} using the Read tool. '
        f"Parse the JSON payload. For each agent in pending_agents ({pending_agent_count} agents), "
        f"spawn a sub-agent (Agent tool) IN PARALLEL with the specified subagent_type and model. "
        f"Pass each agent: its checks, resolved_extra_checks (if any), the file_list, code_hunks, "
        f"and file_contexts from the instructions. "
        f"Fail criteria: critical issue OR 2+ high issues. "
        f'Aggregate all results into: {{"round_id": "{round_id}", "agents": {{"<agent_id>": {{"status": "pass"|"fail", "issues": [...]}}}}}} '
        f'Return ONLY the aggregated JSON result, nothing else."\n\n'
        f"When the background coordinator completes:\n"
        f"1. Read its returned output (the JSON result)\n"
        f"2. Output the result between these exact markers:\n"
        f"<!--REVIEW_RESULTS_START-->\n"
        f"<paste coordinator JSON here>\n"
        f"<!--REVIEW_RESULTS_END-->\n\n"
        f"[Auto-continue {auto_continue_count + 1} of {MAX_AUTO_CONTINUES}] "
        f"Continue with your current task while the review runs in the background."
        f"{GIT_READONLY_CONSTRAINT}"
    )
