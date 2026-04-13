"""Agent definitions and tier-to-agent mapping."""

from __future__ import annotations

import json

from stop_design_audit.config import AGENT_IDS, STATE_DIR

# Agent definitions with their check instructions
AGENT_DEFINITIONS: dict[str, dict] = {
    "explore_haiku": {
        "subagent_type": "Explore",
        "model": "haiku",
        "checks": "code smells, obvious bugs, hardcoded values, missing error handling",
        "context_checks": {
            "proto": "Check field numbering, message compatibility, enum values",
            "database": "Check migration reversibility, index definitions",
        },
    },
    "general_haiku": {
        "subagent_type": "general-purpose",
        "model": "haiku",
        "checks": "silent failures (return None/[]), missing validation, security issues (SQL injection, XSS)",
        "context_checks": {
            "api_routes": "Check input validation, error responses, authentication",
            "grpc_service": "Check error handling, request validation, streaming edge cases",
        },
    },
    "bug_hunter": {
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "checks": "null/undefined access, off-by-one errors, race conditions, resource leaks, unhandled edge cases",
        "context_checks": {
            "frontend": "Check React hooks dependencies, state update batching, memory leaks",
            "database": "Check transaction boundaries, connection leaks, deadlocks",
        },
    },
    "general_opus": {
        "subagent_type": "general-purpose",
        "model": "opus",
        "checks": "service boundary violations, API contract breaks, architecture issues",
        "context_checks": {
            "grpc_service": "Check service contracts, backward compatibility",
        },
    },
    "integration_checker": {
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "effort": "max",
        "checks": "API contract consistency, cross-module imports, shared state mutations, event/callback mismatches",
        "context_checks": {
            "proto": "Check cross-service message dependencies, breaking changes",
        },
    },
    "plan_agent": {
        "subagent_type": "Plan",
        "description": "Creates structured remediation plans for review failures",
    },
}


def _load_extra_agent_definitions() -> None:
    """Load extra agent definitions from hook-overrides.json."""
    override_path = STATE_DIR / "hook-overrides.json"
    if not override_path.exists():
        return
    try:
        overrides = json.loads(override_path.read_text(encoding="utf-8"))
        for agent_id, defn in overrides.get("extra_agent_definitions", {}).items():
            AGENT_DEFINITIONS[agent_id] = defn
    except Exception as e:
        from stop_design_audit.exit_helpers import log

        log(f"WARNING: Failed to load extra agent definitions: {e}")


# Load at import time
_load_extra_agent_definitions()


def get_required_agents(tier: str) -> list[str]:
    """Return list of agent IDs required for this tier."""
    return AGENT_IDS.get(tier, [])
