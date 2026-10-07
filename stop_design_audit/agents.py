"""Agent definitions, the review checklist, and tier-to-agent mapping."""

from __future__ import annotations

import re

from stop_design_audit import config
from stop_design_audit.config import ConfigError

# Generic checklist used when no project checklist file is configured.
# A project checklist (review_checklist_file) REPLACES this text — put the
# generic items in it too if you still want them.
DEFAULT_CHECKLIST = """\
1. Silent failures — swallowed exceptions, defaults returned on failure,
   execution continuing after a validation error.
2. Correctness — null/undefined access, off-by-one, race conditions,
   resource leaks, unhandled edge cases, wrong error handling.
3. Contracts & integration — changed signatures/schemas with callers not
   updated, cross-module import violations, wire/proto compatibility.
4. Security — injection, missing authn/authz checks, secrets in code or logs.
5. Hardcoding — values that belong in config, a registry, or a schema.
6. Tests — changed behaviour without coverage; bug fixes without a
   regression test that would have caught the bug.
"""

# File-context hints added to the reviewer's focus when matching files change.
CONTEXT_CHECKS: dict[str, str] = {
    "proto": "Proto: field numbering, message/enum compatibility, cross-service dependencies",
    "database": "Database: migration reversibility, indexes, transaction boundaries, connection leaks",
    "api_routes": "API routes: input validation, error responses, authentication",
    "grpc_service": "gRPC services: request validation, error mapping, streaming edge cases, contract compatibility",
    "frontend": "Frontend: React hook dependencies, state update batching, memory leaks",
}

# How hard the reviewer digs, by tier. The tier scales effort, not agent count.
TIER_EFFORT: dict[str, str] = {
    "quick": "Focused pass: the changed lines and their direct callers/callees.",
    "standard": "Read every changed file in full; check direct callers/callees and tests.",
    "deep": (
        "Exhaustive: read every changed file in full, trace cross-module dependencies, "
        "verify contracts and signatures at every call site, check edge cases, "
        "ordering and side effects."
    ),
}

_REQUIRED_AGENT_FIELDS = ("subagent_type", "model", "checks")
AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def _builtin_definitions() -> dict[str, dict]:
    return {
        "reviewer": {
            "subagent_type": "general-purpose",
            "model": config.REVIEWER_MODEL,
            "checks": "the review checklist",
            "context_checks": dict(CONTEXT_CHECKS),
        },
    }


AGENT_DEFINITIONS: dict[str, dict] = {}


def load_agent_definitions() -> None:
    """Build AGENT_DEFINITIONS from built-ins + extra_agent_definitions.

    Must run after config.load_overrides(). Raises ConfigError if an extra
    definition is malformed or agent_ids references an undefined agent.
    """
    AGENT_DEFINITIONS.clear()
    AGENT_DEFINITIONS.update(_builtin_definitions())

    errors: list[str] = []
    extra = config.read_overrides_file().get("extra_agent_definitions", {})
    for agent_id, defn in extra.items():
        if not isinstance(defn, dict):
            errors.append(f"extra_agent_definitions.{agent_id} must be an object")
            continue
        if not AGENT_ID_PATTERN.fullmatch(agent_id):
            # ids become file names and prompt text
            errors.append(
                f"extra_agent_definitions id {agent_id!r} must match {AGENT_ID_PATTERN.pattern}"
            )
            continue
        missing = [
            k
            for k in _REQUIRED_AGENT_FIELDS
            if not isinstance(defn.get(k), str) or not defn[k].strip()
        ]
        if missing:
            errors.append(
                f"extra_agent_definitions.{agent_id}: {missing} must be non-empty strings"
            )
            continue
        ctx_checks = defn.get("context_checks", {})
        if not isinstance(ctx_checks, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in ctx_checks.items()
        ):
            errors.append(
                f"extra_agent_definitions.{agent_id}.context_checks must map strings to strings"
            )
            continue
        AGENT_DEFINITIONS[agent_id] = defn

    for tier, agent_ids in config.AGENT_IDS.items():
        for agent_id in agent_ids:
            if agent_id not in AGENT_DEFINITIONS:
                errors.append(
                    f"agent_ids.{tier} references undefined agent {agent_id!r}"
                )

    if errors:
        raise ConfigError(errors)


def load_checklist() -> str:
    """Return the review checklist text (project file, else the default)."""
    if not config.REVIEW_CHECKLIST_FILE:
        return DEFAULT_CHECKLIST
    return config.resolve_checklist_path().read_text(encoding="utf-8")


def get_required_agents(tier: str) -> list[str]:
    """Return list of agent IDs required for this tier."""
    return config.AGENT_IDS.get(tier, [])
