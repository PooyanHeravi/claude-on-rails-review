"""Tier classification, module boundaries, and file context detection."""

from __future__ import annotations

import json
import os

from stop_design_audit.config import (
    CRITICAL_PATTERNS,
    FORCE_TIER_ENV,
    SEVERITY_ORDER,
    STATE_DIR,
    TIER_FILE_LIMITS,
    TIER_THRESHOLDS,
)
from stop_design_audit.exit_helpers import log
from stop_design_audit.transcript import normalize_path


def severities_at_or_above(threshold: str) -> list[str]:
    """Return list of severities at or above the given threshold.

    "all" returns all severities. Invalid thresholds return empty list.
    """
    if threshold == "all":
        return list(SEVERITY_ORDER)
    if threshold in SEVERITY_ORDER:
        idx = SEVERITY_ORDER.index(threshold)
        return SEVERITY_ORDER[: idx + 1]
    return []


def should_run_integration_review(files: list[str]) -> tuple[bool, set[str], set[str]]:
    """Check if changes span multiple top-level directories OR multiple critical patterns.

    Returns: (needs_review, top_level_dirs, matched_critical_patterns)
    """
    top_level_dirs: set[str] = set()
    for f in files:
        normalized = normalize_path(f)
        parts = normalized.split("/")
        if len(parts) > 1:
            top_level_dirs.add(parts[0])

    matched_patterns: set[str] = set()
    for f in files:
        f_normalized = normalize_path(f.lower())
        for pattern in CRITICAL_PATTERNS:
            if pattern in f_normalized:
                matched_patterns.add(pattern)
                break

    needs_review = len(top_level_dirs) >= 2 or len(matched_patterns) >= 2
    return needs_review, top_level_dirs, matched_patterns


def load_review_config() -> dict:
    """Load review config from .claude/review-config.json."""
    config_path = STATE_DIR.parent / "review-config.json"
    if not config_path.exists():
        log("No review-config.json found")
        return {}
    try:
        return json.loads(config_path.read_text())
    except Exception as e:
        log(f"Error loading review config: {e}")
        return {}


def load_module_boundaries() -> dict:
    """Load module boundary rules from config file."""
    config = load_review_config()
    return config.get("module_boundaries", {})


def check_import_violations(
    files: list[str],
    hunks: dict[str, str],
    module_rules: dict,
) -> list[str]:
    """Check for module boundary import violations.

    Returns: List of violation messages
    """
    violations = []
    if not module_rules:
        return violations

    for file_path in files:
        normalized = normalize_path(file_path)
        parts = normalized.split("/", 1)
        if len(parts) < 2:
            continue

        module = parts[0]
        rules = module_rules.get(module)
        if not rules:
            continue

        code = hunks.get(file_path, "")
        if not code:
            continue

        forbidden = rules.get("forbidden_imports", [])
        for forbidden_module in forbidden:
            patterns = [f"from {forbidden_module}", f"import {forbidden_module}"]
            for pattern in patterns:
                if pattern in code:
                    communication = rules.get("communication", "See architecture docs")
                    violations.append(
                        f"{file_path}: Imports from '{forbidden_module}' (forbidden). {communication}"
                    )
                    break

    return violations


def classify_review_tier(
    incremental_diff: int,
    incremental_file_count: int,
) -> str:
    """Classify review tier based on INCREMENTAL changes since last hook.

    Returns: "skip", "quick", "standard", or "deep"
    """
    # Check for config file override first
    config = load_review_config()
    config_tier = config.get("force_tier", "").lower()
    if config_tier in ("deep", "standard", "quick"):
        log(f"Tier forced via review-config.json: {config_tier}")
        return config_tier

    # Check for environment variable override
    forced_tier = os.environ.get(FORCE_TIER_ENV, "").lower()
    if forced_tier in ("deep", "standard", "quick"):
        log(f"Tier forced via {FORCE_TIER_ENV}: {forced_tier}")
        return forced_tier

    abs_diff = abs(incremental_diff)

    for tier in ["skip", "quick", "standard"]:
        if (
            abs_diff < TIER_THRESHOLDS[tier]
            and incremental_file_count <= TIER_FILE_LIMITS[tier]
        ):
            return tier

    return "deep"


def detect_file_contexts(files: list[str]) -> set[str]:
    """Detect special file contexts that need targeted review.

    Returns set of context tags: 'proto', 'grpc_service', 'database', 'api_routes', 'frontend'
    """
    contexts = set()

    for f in files:
        f_lower = normalize_path(f.lower())

        if f_lower.endswith(".proto"):
            contexts.add("proto")
        if "_service.py" in f_lower or "grpc_service.py" in f_lower:
            contexts.add("grpc_service")
        if "/migrations/" in f_lower or "/models/" in f_lower:
            contexts.add("database")
        if "/routes/" in f_lower or "/api/" in f_lower:
            contexts.add("api_routes")
        if f_lower.endswith((".tsx", ".jsx", ".ts", ".js")) and "/frontend/" in f_lower:
            contexts.add("frontend")

    return contexts
