"""Review instructions and plan agent prompt generation."""

from __future__ import annotations

import os

from stop_design_audit.agents import AGENT_DEFINITIONS
from stop_design_audit.classify import severities_at_or_above
from stop_design_audit.config import (
    DEEP_AUTO_FIX,
    DEEP_AUTO_FIX_ENV,
    MAX_AUTO_CONTINUES,
    MAX_FAIL_RETRIES,
    MAX_FILES_IN_PROMPT,
    MAX_VIOLATION_FILES,
    RESULTS_MODE,
)
from stop_design_audit.transcript import normalize_path

# ---------------------------------------------------------------------------
# Git working tree protection constraints
# ---------------------------------------------------------------------------

GIT_READONLY_CONSTRAINT = (
    "\n\nCRITICAL CONSTRAINT — READ-ONLY REVIEW:\n"
    "Do NOT run any git commands that modify the working tree or index. "
    "Prohibited: git stash, git reset, git checkout, git clean, git restore, "
    "git rm, git mv, git apply, git merge, git rebase, git pull, git cherry-pick, "
    "git add, git commit. "
    "Review code ONLY from the provided hunks, file_list, and transcript data. "
    "Use the Read tool to inspect files — never git commands. "
    "Violating this will destroy uncommitted user work."
)

GIT_FIX_CONSTRAINT = (
    "\n\nCRITICAL CONSTRAINT — NO GIT WORKING TREE COMMANDS:\n"
    "Do NOT run any git commands that modify the working tree or index. "
    "Prohibited: git stash, git reset, git checkout, git clean, git restore, "
    "git rm, git mv, git apply, git merge, git rebase, git pull, git cherry-pick, "
    "git add, git commit. "
    "You MAY use Edit/Write tools to fix code and read-only git commands (git diff, git log). "
    "Violating this will destroy uncommitted user work."
)


def format_files_grouped(files: list[str], max_files: int = MAX_FILES_IN_PROMPT) -> str:
    """Format file list grouped by top-level directory."""
    if not files:
        return "(no files)"

    truncated = len(files) > max_files
    files_to_show = sorted(files)[:max_files]

    groups: dict[str, list[str]] = {}
    for f in files_to_show:
        normalized = normalize_path(f)

        if os.path.isabs(f):
            try:
                rel = normalize_path(os.path.relpath(f))
                if rel.startswith(".."):
                    normalized = os.path.basename(f)
                else:
                    normalized = rel
            except (ValueError, OSError):
                normalized = os.path.basename(f)

        parts = normalized.split("/", 1)

        if len(parts) == 1:
            top_dir = "."
            rel_path = parts[0]
        else:
            top_dir = parts[0]
            rel_path = parts[1]

        if top_dir not in groups:
            groups[top_dir] = []
        groups[top_dir].append(rel_path)

    lines = []
    for top_dir in sorted(groups.keys()):
        lines.append(f"  {top_dir}/")
        for file in sorted(groups[top_dir]):
            lines.append(f"    - {file}")

    if truncated:
        lines.append(f"  ...and {len(files) - max_files} more files")

    return "\n".join(lines)


def get_review_instructions(
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
    session_hash: str,
    integration_context: dict | None = None,
    files: list[str] = None,
    code_hunks: dict[str, str] = None,
    violation_history: dict[str, dict] = None,
    import_violations: list[str] = None,
) -> str:
    """Generate tier-specific review instructions for Claude."""
    tier_label = tier.upper()

    # Build header with file stats and module count
    if files:
        top_dirs = set()
        for f in files:
            parts = normalize_path(f).split("/", 1)
            if len(parts) > 1:
                top_dirs.add(parts[0])
        module_count = len(top_dirs)
    else:
        module_count = 0

    if new_file_count < total_file_count:
        file_display = f"{total_file_count} files ({new_file_count} new) across {module_count} module(s)"
    else:
        file_display = f"{total_file_count} file(s) across {module_count} module(s)"
    header_parts = [f"{tier_label}_REVIEW: +{diff_size} chars, {file_display}"]
    if round_id:
        header_parts.append(f"[Round: {round_id}]")
    if fail_count > 0:
        header_parts.append(f"[Retry {fail_count}/{MAX_FAIL_RETRIES}]")
    header = " ".join(header_parts)

    # Show passed agents
    passed_section = ""
    if passed_agents:
        passed_lines = [
            f"  \u2713 {agent_id}: PASSED (skip)" for agent_id in passed_agents
        ]
        passed_section = "\nPrevious results:\n" + "\n".join(passed_lines) + "\n"

    # Build agent instructions for pending agents only
    agent_instructions = []
    for i, agent_id in enumerate(pending_agents, 1):
        defn = AGENT_DEFINITIONS.get(agent_id, {})
        base_checks = defn.get("checks", "general code quality")

        context_checks = defn.get("context_checks", {})
        context_additions = [
            context_checks[ctx] for ctx in file_contexts if ctx in context_checks
        ]

        if agent_id == "integration_checker" and integration_context:
            dirs = integration_context.get("dirs", set())
            patterns = integration_context.get("patterns", set())
            if len(dirs) >= 2:
                context_additions.append(f"Cross-directory: {', '.join(sorted(dirs))}")
            if len(patterns) >= 2:
                context_additions.append(
                    f"Critical patterns: {', '.join(sorted(patterns))}"
                )

        if context_additions:
            full_checks = f"{base_checks}. ALSO: {', '.join(context_additions)}"
        else:
            full_checks = base_checks

        agent_instructions.append(
            f"{i}. subagent_type='{defn.get('subagent_type', 'general-purpose')}', "
            f"model='{defn.get('model', 'haiku')}' [ID: {agent_id}]\n"
            f"   Check: {full_checks}"
        )

    if len(pending_agents) > 1:
        spawn_note = f"Spawn {len(pending_agents)} Task agents IN PARALLEL (single message, multiple tool calls):"
    else:
        spawn_note = "Spawn 1 Task agent:"

    agents_section = spawn_note + "\n\n" + "\n\n".join(agent_instructions)

    # Violation history section
    history_section = ""
    if violation_history and files:
        problem_files = [f for f in files if f in violation_history]
        if problem_files:
            history_section = "\n\nFiles with previous violations:"
            for file in problem_files[:MAX_VIOLATION_FILES]:
                categories = violation_history[file]
                total = sum(categories.values())
                top_category = max(categories.items(), key=lambda x: x[1])[0]
                history_section += (
                    f"\n  - {file}: {total}x violations (most common: {top_category})"
                )

    status_note = f"\n\n[Auto-continue {auto_continue_count + 1} of {MAX_AUTO_CONTINUES}] If no issues found, continue with implementation."

    # Results instruction - mode-aware
    pending_agents_list = ", ".join(f'"{a}"' for a in pending_agents)
    results_schema = f"""Report: [{pending_agents_list}]
Format: {{"status": "pass"|"fail", "issues": [{{"file": "...", "line": N, "severity": "critical|high|medium|low", "description": "..."}}]}}
Fail if: critical issue OR 2+ high issues"""

    if RESULTS_MODE == "file":
        results_instruction = f'''

Write results to .claude/hooks/review-results-{session_hash}.json:
{{"round_id": "{round_id}", "agents": {{"<agent_id>": {{"status": "...", "issues": [...]}}}}}}

{results_schema}'''
    else:
        results_instruction = f'''

Output results (no files). Use 2-space indented JSON:
<!--REVIEW_RESULTS_START-->
{{
  "round_id": "{round_id}",
  "agents": {{
    "<agent_id>": {{
      "status": "pass",
      "issues": []
    }}
  }}
}}
<!--REVIEW_RESULTS_END-->

{results_schema}'''

    # Module boundary violations
    violations_section = ""
    if import_violations:
        violations_section = "\n\nModule Boundary Violations Detected:"
        for violation in import_violations:
            violations_section += f"\n  \u26a0\ufe0f  {violation}"

    # Code hunks
    hunks_section = ""
    if code_hunks:
        hunks_section = "\n\nCode Changes Preview:"
        for file, hunk in code_hunks.items():
            hunks_section += f"\n\n{file}:\n{hunk}"

    # Post-review instruction
    if tier == "deep":
        effective_auto_fix = (
            os.environ.get(DEEP_AUTO_FIX_ENV, "").lower() or DEEP_AUTO_FIX
        )
        if effective_auto_fix == "none":
            post_review_instruction = "After reviews complete, report findings. DO NOT FIX. Stop and wait for user."
        else:
            qualifying = severities_at_or_above(effective_auto_fix)
            severity_list = ", ".join(qualifying)
            post_review_instruction = (
                f"After reviews complete, report findings. Then spawn ONE general-purpose subagent (model='sonnet') "
                f"to fix all issues with severity [{severity_list}]. Pass it the full list of qualifying issues with "
                f"file paths and line numbers. Report remaining lower-severity issues without fixing. "
                f"Do NOT fix issues yourself in the main context. After the subagent completes, resume your prior task."
                f"{GIT_FIX_CONSTRAINT}"
            )
    else:
        post_review_instruction = (
            "After reviews complete, report findings. Then spawn ONE general-purpose subagent (model='sonnet') "
            "to fix all violations. Pass it the full list of issues with file paths and line numbers. "
            "Do NOT fix issues yourself in the main context. After the subagent completes, resume your prior task."
            f"{GIT_FIX_CONSTRAINT}"
        )

    return f"""{header}
{passed_section}
{agents_section}
{history_section}

Files:
{file_list}
{violations_section}
{hunks_section}

{post_review_instruction}{status_note}{results_instruction}{GIT_READONLY_CONSTRAINT}"""


def get_plan_agent_instructions(
    session_hash: str,
    review_results: dict,
    files: list[str],
    violation_history: dict[str, dict],
    code_hunks: dict[str, str],
) -> str:
    """Generate instructions for spawning the Plan subagent."""
    all_issues = []
    for agent_id, agent_data in review_results.get("agents", {}).items():
        if isinstance(agent_data, dict):
            issues = agent_data.get("issues", [])
            for issue in issues:
                if isinstance(issue, dict):
                    issue_copy = issue.copy()
                    issue_copy["found_by"] = agent_id
                    all_issues.append(issue_copy)

    critical_issues = [i for i in all_issues if i.get("severity") == "critical"]
    high_issues = [i for i in all_issues if i.get("severity") == "high"]
    medium_issues = [i for i in all_issues if i.get("severity") == "medium"]
    low_issues = [i for i in all_issues if i.get("severity") == "low"]

    issues_summary = f"""
Issues Found ({len(all_issues)} total):
- Critical: {len(critical_issues)}
- High: {len(high_issues)}
- Medium: {len(medium_issues)}
- Low: {len(low_issues)}

Detailed Issues:
"""
    for issue in all_issues:
        severity = issue.get("severity", "unknown").upper()
        file = issue.get("file", "unknown")
        desc = issue.get("description", "No description")
        found_by = issue.get("found_by", "")
        issues_summary += f"\n- [{severity}] {file}: {desc}"
        if found_by:
            issues_summary += f" (found by {found_by})"

    files_context = "\n".join([f"  - {f}" for f in files])

    plan_prompt = f"""You are a senior software architect creating a remediation plan.

## Context
A deep code review found {len(all_issues)} issues that need to be fixed.

## Files Under Review
{files_context}

{issues_summary}

## Your Task
Create a structured remediation plan that:
1. Prioritizes fixes by severity (critical first)
2. Groups related issues that can be fixed together
3. Identifies dependencies between fixes
4. Estimates complexity for each fix group
5. Suggests a safe implementation order

## Output Format
Return your plan as structured text (do NOT write to any file). Include:

1. SUMMARY: 1-2 sentence overview
2. FIX GROUPS: For each group:
   - Group name and priority (critical/high/medium/low)
   - Issues included
   - Files affected
   - Approach to fix
   - Complexity (simple/moderate/complex)
   - Dependencies on other groups
3. IMPLEMENTATION ORDER: Numbered list of groups in order
4. NOTES: Any important considerations

IMPORTANT:
- Do NOT write any files - return plan as text output only
- Do NOT implement any fixes - only create the plan
- Be specific about which files need changes
- Consider side effects and test requirements
"""

    return f"""PLAN REQUIRED: Deep review failed with {len(all_issues)} issue(s).

Before implementing fixes, a remediation plan must be created.

Spawn 1 Task agent to create the plan:

1. subagent_type='Plan' [ID: plan_agent]
   Task: Analyze the review results and create a prioritized remediation plan.

{plan_prompt}{GIT_READONLY_CONSTRAINT}"""
