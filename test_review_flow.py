#!/usr/bin/env python3
"""
v3 review-flow tests for the stop-design-audit hook.

Covers the foreground-reviewer round trip (dispatch -> results file -> verdict),
the strict results contract, fail-loud error handling, config validation,
metrics provenance, and regression tests for the bugs fixed in 3.0.0:

  BUG-1  deep tier + files in >=2 top-level dirs crashed on a set in the
         integration context ("Object of type set is not JSON serializable")
         and the stop was silently allowed.
  BUG-2  pending subagent results were never read when the main transcript
         had no new edits (the zero-diff exit ran first).
  BUG-3  any exception / bad config silently allowed the stop.
  BUG-4  a missing transcript read as "no code modified".
  BUG-5  metrics session_id was the transcript-path prefix, not the session hash.

Run:  python test_review_flow.py
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from test_exit_paths import (
    STATE_DIR,
    coordinator_path_for,
    expect_block,
    expect_silent_allow,
    fail,
    hook_session,
    overrides_file,
    prompt_path_for,
    read_state_file,
    results_path_for,
    run_hook,
    run_layers,
    state_path_for,
    edit_event,
    write_edit_transcript,
    write_transcript,
)

from stop_design_audit import __version__

METRICS_FILE = STATE_DIR / "stop-hook-metrics.jsonl"
CROSS_MODULE_FILES = ("api/a.py", "services/b.py")  # relative: two top-level dirs
DEEP_ENV = {"CLAUDE_HOOK_FORCE_TIER": "deep"}


# =============================================================================
# Helpers
# =============================================================================

def issue(severity: str, file: str = "api/a.py", line: int | None = 1, description: str = "") -> dict:
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "category": "test-category",
        "description": description or f"{severity} defect",
    }


def write_results(
    session_hash: str,
    round_id: str,
    status: str,
    issues: list[dict],
    *,
    content_overrides: dict | None = None,
    raw: str | None = None,
) -> None:
    """Write the reviewer's results file for a round.

    `content_overrides` replaces top-level fields (e.g. a wrong round_id);
    `raw` writes the text verbatim instead of JSON.
    """
    if raw is None:
        content = {"round_id": round_id, "agent_id": "reviewer", "status": status, "issues": issues}
        content.update(content_overrides or {})
        raw = json.dumps(content)
    results_path_for(session_hash, round_id).write_text(raw, encoding="utf-8")


def dispatch(transcript: Path, session_hash: str, env: dict | None = None) -> dict | None:
    """Run the hook expecting a dispatch block; return the saved state."""
    output = expect_block(*run_hook(transcript, env_overrides=env))
    if output is None:
        return None
    if "run_in_background=false" not in output["reason"]:
        fail(f"expected a dispatch block, got: {output['reason'][:300]}")
        return None
    state = read_state_file(state_path_for(session_hash))
    if not state or not state.get("subagent_pending") or not state.get("round_id"):
        fail(f"dispatch did not leave a pending round: {state}")
        return None
    return state


def check_fail_loud(stdout: str, stop_hook_active: bool, expect: str) -> bool:
    """Assert the fail_loud output shape for the given stop_hook_active value."""
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        return fail(f"[stop_hook_active={stop_hook_active}] expected fail_loud JSON, got: {stdout[:300]!r}")
    keys = set(output)
    expected_keys = {"systemMessage"} if stop_hook_active else {"systemMessage", "decision", "reason"}
    if keys != expected_keys:
        return fail(f"[stop_hook_active={stop_hook_active}] keys {sorted(keys)}, expected {sorted(expected_keys)}")
    message = output["systemMessage"]
    if "code review did NOT run" not in message:
        return fail(f"systemMessage does not say the review did not run: {message[:300]}")
    if expect not in message:
        return fail(f"systemMessage missing {expect!r}: {message[:400]}")
    if not stop_hook_active:
        if output["decision"] != "block":
            return fail(f"decision is {output['decision']!r}, expected 'block'")
        if expect not in output["reason"]:
            return fail(f"reason missing {expect!r}: {output['reason'][:400]}")
    return True


def check_fail_loud_both(
    transcript: Path | None, expect: str, env: dict | None = None, label: str = ""
) -> bool:
    """Run the hook with stop_hook_active False then True; both must fail loud."""
    for active in (False, True):
        code, stdout, stderr = run_hook(transcript, env_overrides=env, stop_hook_active=active)
        if code != 0:
            return fail(f"{label}: exit code {code} (stderr: {stderr[:300]})")
        if not check_fail_loud(stdout, active, expect):
            return fail(f"{label}: wrong fail_loud output")
    return True


# =============================================================================
# Regression tests
# =============================================================================

def test_bug1_deep_cross_module_subagent_dispatch():
    """[BUG-1 regression] Deep tier across api/ + services/ dispatches a reviewer whose prompt names both modules."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript, files=CROSS_MODULE_FILES)
        state = dispatch(transcript, session_hash, env=DEEP_ENV)
        if state is None:
            return False
        if state.get("tier") != "deep":
            return fail(f"tier should be deep, got {state.get('tier')}")
        prompt_path = prompt_path_for(session_hash, state["round_id"])
        if not prompt_path.exists():
            return fail(f"prompt file not written: {prompt_path}")
        prompt = prompt_path.read_text(encoding="utf-8")
        if "Cross-module change spanning: api, services" not in prompt:
            return fail("prompt is missing 'Cross-module change spanning: api, services'")
        print("  PASS: deep cross-module dispatch writes the integration focus into the prompt")
        return True


def test_bug1_deep_cross_module_delegated_payload():
    """[BUG-1 regression] Delegated mode writes a JSON coordinator payload for a deep cross-module change."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript, files=CROSS_MODULE_FILES)
        env = {**DEEP_ENV, "CLAUDE_HOOK_REVIEW_MODE": "delegated"}
        output = expect_block(*run_hook(transcript, env_overrides=env))
        if output is None:
            return False
        if "DELEGATED_REVIEW" not in output["reason"]:
            return fail(f"expected a delegated dispatch, got: {output['reason'][:300]}")
        path = coordinator_path_for(session_hash)
        if not path.exists():
            return fail(f"coordinator payload not written: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("tier") != "deep":
            return fail(f"payload tier {payload.get('tier')!r}, expected 'deep'")
        if payload.get("integration_context") != {"dirs": ["api", "services"], "patterns": []}:
            return fail(f"integration_context = {payload.get('integration_context')!r}")
        print("  PASS: delegated deep cross-module payload serializes")
        return True


def test_bug2_pending_results_read_without_new_edits():
    """[BUG-2 regression] A pass results file is consumed on the next stop even with no new edits."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        write_results(session_hash, state["round_id"], "pass", [])
        output = expect_block(*run_hook(transcript))  # same transcript: zero new diff
        if output is None:
            return False
        if "Design audit passed" not in output["reason"]:
            return fail(f"expected 'Design audit passed', got: {output['reason'][:300]}")
        state = read_state_file(state_path_for(session_hash))
        cleared = {"subagent_pending": False, "round_id": "", "review_agents": [], "review_attempts": 0}
        for key, value in cleared.items():
            if state.get(key) != value:
                return fail(f"state[{key!r}] = {state.get(key)!r}, expected {value!r}")
        if state.get("auto_continue_count") != 1:
            return fail(f"auto_continue_count = {state.get('auto_continue_count')}, expected 1")
        print("  PASS: pending results consumed before the zero-diff exit; round cleared")
        return True


def test_bug3_exception_fails_loud():
    """[BUG-3 regression] An internal error (corrupt state file) fails loud for both stop_hook_active values."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state_path = state_path_for(session_hash)
        for active in (False, True):
            state_path.write_text("{corrupt", encoding="utf-8")
            code, stdout, stderr = run_hook(transcript, stop_hook_active=active)
            if code != 0 or not check_fail_loud(stdout, active, "corrupt state file moved to"):
                return fail(f"corrupt state (active={active}): wrong fail_loud output")
            # Self-heal: moved aside so the error is reported once, not every stop
            if state_path.exists():
                return fail("corrupt state file should be moved aside")
            if not list(STATE_DIR.glob(f"stop-hook-state-{session_hash}.corrupt-*")):
                return fail("corrupt state file should be kept as .corrupt-<ts>")
        print("  PASS: corrupt state file -> fail_loud once, moved aside")
        return True


CONFIG_CASES = [
    ("unknown key", json.dumps({"bogus_key": 1}), "unknown key 'bogus_key'"),
    ("bad enum", json.dumps({"review_mode": "telepathy"}), "'review_mode' must be one of"),
    ("bad results_mode", json.dumps({"results_mode": "carrier-pigeon"}), "'results_mode' must be one of"),
    ("bad deep_auto_fix", json.dumps({"deep_auto_fix": "everything"}), "'deep_auto_fix' must be one of"),
    ("wrong type", json.dumps({"max_auto_continues": "3"}), "'max_auto_continues' must be an integer >= 1"),
    ("zero loop bound", json.dumps({"max_review_attempts": 0}), "'max_review_attempts' must be an integer >= 1"),
    ("redefine reviewer", json.dumps({"extra_agent_definitions": {"reviewer": {
        "subagent_type": "general-purpose", "model": "haiku", "checks": "x"}}}),
     "redefines a built-in agent"),
    ("bad agent id", json.dumps({"extra_agent_definitions": {"../evil": {
        "subagent_type": "general-purpose", "model": "opus", "checks": "x"}}}), "must match"),
    ("bad JSON", "{not json", "cannot parse"),
    ("unknown preset", json.dumps({"preset": "yolo"}), "unknown preset 'yolo'"),
    ("undefined agent", json.dumps({"agent_ids": {"quick": ["ghost"]}}), "references undefined agent 'ghost'"),
]


def test_bug3_bad_config_fails_loud():
    """[BUG-3 regression] Each invalid hook-overrides.json fails loud instead of allowing the stop."""
    with hook_session() as (transcript, _):
        write_edit_transcript(transcript)
        for label, content, expect in CONFIG_CASES:
            with overrides_file(content):
                if not check_fail_loud_both(transcript, expect, label=label):
                    return False
        with tempfile.TemporaryDirectory() as tmp:
            missing = (Path(tmp) / "nope.md").as_posix()
            with overrides_file(json.dumps({"review_checklist_file": missing})):
                if not check_fail_loud_both(transcript, "review_checklist_file not found", label="missing checklist"):
                    return False
        print(f"  PASS: {len(CONFIG_CASES) + 1} invalid config files all fail loud")
        return True


ENV_CASES = [
    ({"CLAUDE_HOOK_REVIEW_MODE": "telepathy"}, "CLAUDE_HOOK_REVIEW_MODE='telepathy'"),
    ({"CLAUDE_HOOK_DEEP_AUTO_FIX": "everything"}, "CLAUDE_HOOK_DEEP_AUTO_FIX='everything'"),
    ({"CLAUDE_HOOK_FORCE_TIER": "skip"}, "CLAUDE_HOOK_FORCE_TIER='skip'"),
    ({"CLAUDE_HOOK_SKIP": "yes"}, "CLAUDE_HOOK_SKIP='yes'"),
]


def test_bug3_invalid_env_fails_loud():
    """[BUG-3 regression] Invalid CLAUDE_HOOK_* env values fail loud."""
    with hook_session() as (transcript, _):
        write_edit_transcript(transcript)
        for env, expect in ENV_CASES:
            if not check_fail_loud_both(transcript, expect, env=env, label=str(env)):
                return False
        print(f"  PASS: {len(ENV_CASES)} invalid env values all fail loud")
        return True


def test_bad_hook_input_fails_loud():
    """Hook input without transcript_path, or not JSON at all, fails loud."""
    if not check_fail_loud_both(None, "no transcript_path", label="no transcript_path"):
        return False
    # stop_hook_active unknown -> fail loud WITHOUT blocking (no unbounded loop)
    code, stdout, _ = run_hook(None, raw_input="this is not json")
    if code != 0 or not check_fail_loud(stdout, True, "JSONDecodeError"):
        return fail("non-JSON hook input must fail loud, warn-only")
    for label, flag in (("missing flag", None), ("non-bool flag", "yes")):
        payload = {"transcript_path": "t.jsonl"}
        if flag is not None:
            payload["stop_hook_active"] = flag
        code, stdout, _ = run_hook(None, raw_input=json.dumps(payload))
        if code != 0 or not check_fail_loud(stdout, True, "stop_hook_active must be a bool"):
            return fail(f"{label}: must fail loud, warn-only")
    print("  PASS: malformed hook input fails loud; unknown loop flag never blocks")
    return True


def test_bug4_missing_transcript_fails_loud():
    """[BUG-4 regression] A missing transcript file fails loud, not 'no code modified'."""
    with hook_session() as (transcript, _):
        # transcript path is never created
        if not check_fail_loud_both(transcript, "FileNotFoundError", label="missing transcript"):
            return False
        print("  PASS: missing transcript -> fail_loud")
        return True


def _metrics_for(session_prefix: str) -> list[dict]:
    if not METRICS_FILE.exists():
        return []
    records = []
    for line in METRICS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # other runs' lines are not under test here
        if record.get("session_id") == session_prefix:
            records.append(record)
    return records


def test_bug5_metrics_session_id_and_provenance():
    """[BUG-5 regression] Round metrics carry session_id = session-hash prefix, version and config_fingerprint."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        write_results(session_hash, state["round_id"], "pass", [])
        if expect_block(*run_hook(transcript)) is None:
            return False
        records = [r for r in _metrics_for(session_hash[:8]) if r.get("outcome") == "pass"]
        if len(records) != 1:
            return fail(f"expected 1 pass metric with session_id={session_hash[:8]!r}, found {len(records)}")
        record = records[0]
        if record.get("version") != __version__:
            return fail(f"metric version {record.get('version')!r}, expected {__version__!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("config_fingerprint", ""))):
            return fail(f"config_fingerprint is not a sha256 hex: {record.get('config_fingerprint')!r}")
        if record.get("tier") != "quick" or record.get("agents") != ["reviewer"]:
            return fail(f"unexpected metric record: {record}")
        print("  PASS: metric session_id is the session hash prefix, with provenance")
        return True


# =============================================================================
# Review-flow behavior
# =============================================================================

def test_missing_results_rerequest_then_unreviewed():
    """No results: re-request once (block), then warn_and_allow with UNREVIEWED."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        round_id = state["round_id"]

        output = expect_block(*run_hook(transcript))
        if output is None:
            return False
        reason = output["reason"]
        if "no results file" not in reason or "run_in_background=false" not in reason:
            return fail(f"re-request message wrong: {reason[:300]}")
        state = read_state_file(state_path_for(session_hash))
        if state.get("review_attempts") != 2 or not state.get("subagent_pending"):
            return fail(f"after re-request expected attempts=2 and pending, got {state}")
        if state.get("round_id") != round_id:
            return fail("re-request must keep the same round_id")

        code, stdout, stderr = run_hook(transcript)
        if code != 0:
            return fail(f"exit code {code} (stderr: {stderr[:300]})")
        try:
            output = json.loads(stdout)
        except json.JSONDecodeError:
            return fail(f"expected warn_and_allow JSON, got: {stdout[:300]!r}")
        if set(output) != {"systemMessage"}:
            return fail(f"warn_and_allow must emit only systemMessage, got keys {sorted(output)}")
        if "UNREVIEWED" not in output["systemMessage"] or round_id not in output["systemMessage"]:
            return fail(f"warning must say UNREVIEWED and name the round: {output['systemMessage']}")
        state = read_state_file(state_path_for(session_hash))
        if state.get("subagent_pending") or state.get("round_id"):
            return fail(f"round should be cleared after giving up, got {state}")
        print("  PASS: re-request once, then a visible UNREVIEWED warning")
        return True


FAIL_CRITERIA = "status is 'pass' but issues meet the fail criteria"

# (label, write_results kwargs, expected reason substring)
INVALID_RESULT_CASES = [
    ("bad JSON", {"status": "pass", "issues": [], "raw": "{oops"}, "is not valid UTF-8 JSON"),
    ("fail without issues", {"status": "fail", "issues": []}, "status is 'fail' but lists no issues"),
    ("missing line key", {"status": "fail", "issues": [
        {k: v for k, v in issue("critical").items() if k != "line"}]},
     "issues[0].line is missing or mistyped"),
    ("round mismatch", {"status": "pass", "issues": [], "content_overrides": {"round_id": "deadbeef"}},
     "round_id is 'deadbeef'"),
    ("agent mismatch", {"status": "pass", "issues": [], "content_overrides": {"agent_id": "someone"}},
     "agent_id is 'someone'"),
    ("pass with critical", {"status": "pass", "issues": [issue("critical")]}, FAIL_CRITERIA),
    ("pass with 2 high", {"status": "pass", "issues": [issue("high"), issue("high")]}, FAIL_CRITERIA),
    ("bad status", {"status": "maybe", "issues": []}, "status must be 'pass' or 'fail'"),
    ("mistyped line", {"status": "fail", "issues": [{**issue("critical"), "line": "42"}]},
     "issues[0].line is missing or mistyped"),
    ("unknown severity", {"status": "fail", "issues": [issue("catastrophic")]},
     "issues[0].severity must be one of"),
]


def test_invalid_results_are_rerequested_with_reason():
    """Each contract violation in a results file is re-requested with the specific reason."""
    for label, kwargs, expect in INVALID_RESULT_CASES:
        with hook_session() as (transcript, session_hash):
            write_edit_transcript(transcript)
            state = dispatch(transcript, session_hash)
            if state is None:
                return fail(f"{label}: dispatch failed")
            write_results(session_hash, state["round_id"], **kwargs)
            output = expect_block(*run_hook(transcript))
            if output is None:
                return fail(f"{label}: expected a re-request block")
            reason = output["reason"]
            if "has no valid results yet" not in reason or expect not in reason:
                return fail(f"{label}: re-request should cite {expect!r}, got: {reason[:400]}")
    print(f"  PASS: {len(INVALID_RESULT_CASES)} invalid results files re-requested with their reason")
    return True


def test_fail_lists_issues_critical_first():
    """A fail verdict blocks with fix instructions listing issues sorted critical-first."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        write_results(session_hash, state["round_id"], "fail", [
            issue("low", description="LOW-ONE"),
            issue("high", description="HIGH-ONE"),
            issue("critical", description="CRIT-ONE"),
            issue("medium", line=None, description="MED-ONE"),
        ])
        output = expect_block(*run_hook(transcript))
        if output is None:
            return False
        reason = output["reason"]
        if not reason.startswith("DESIGN AUDIT FAILED") or "Fix these 4 issue(s)" not in reason:
            return fail(f"unexpected fail message: {reason[:300]}")
        positions = [reason.find(tag) for tag in ("CRIT-ONE", "HIGH-ONE", "MED-ONE", "LOW-ONE")]
        if -1 in positions or positions != sorted(positions):
            return fail(f"issues not sorted critical-first (positions {positions})")
        if "api/a.py:?" not in reason:
            return fail("an issue with line=null should render as file:?")
        state = read_state_file(state_path_for(session_hash))
        if state.get("fail_count") != 1 or state.get("subagent_pending"):
            return fail(f"expected fail_count=1 and no pending round, got {state}")
        if state.get("violation_history", {}).get("api/a.py", {}).get("test-category") != 4:
            return fail(f"violation_history not updated: {state.get('violation_history')}")
        if state.get("completed"):
            return fail("a fixable failure must not mark the cycle completed")
        print("  PASS: fail verdict lists issues critical-first with fix instructions")
        return True


def test_deep_auto_fix_none_is_report_only():
    """Deep tier + CLAUDE_HOOK_DEEP_AUTO_FIX=none reports without fixing and completes the cycle."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash, env=DEEP_ENV)
        if state is None:
            return False
        write_results(session_hash, state["round_id"], "fail", [issue("critical"), issue("low")])
        env = {**DEEP_ENV, "CLAUDE_HOOK_DEEP_AUTO_FIX": "none"}
        output = expect_block(*run_hook(transcript, env_overrides=env))
        if output is None:
            return False
        reason = output["reason"]
        if "Report these findings to the user" not in reason or "Fix these" in reason:
            return fail(f"expected a report-only message, got: {reason[:400]}")
        state = read_state_file(state_path_for(session_hash))
        if state.get("completed") is not True or state.get("tier") != "deep":
            return fail(f"report-only deep failure must complete the cycle, got {state}")
        # Next stop with no new edits: the completed deep cycle allows silently.
        if not expect_silent_allow(*run_hook(transcript, env_overrides=env, stop_hook_active=True)):
            return False
        if read_state_file(state_path_for(session_hash)).get("completed") is not False:
            return fail("completed flag should reset after the deep-completed allow")
        print("  PASS: deep + DEEP_AUTO_FIX=none is report-only and completes")
        return True


def test_deep_no_qualifying_issue_is_report_only():
    """Deep tier with DEEP_AUTO_FIX=critical and only high/medium issues is report-only."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash, env=DEEP_ENV)
        if state is None:
            return False
        write_results(session_hash, state["round_id"], "fail", [issue("high"), issue("high"), issue("medium")])
        env = {"CLAUDE_HOOK_DEEP_AUTO_FIX": "critical"}
        output = expect_block(*run_hook(transcript, env_overrides=env))
        if output is None:
            return False
        if "Report these findings to the user" not in output["reason"]:
            return fail(f"expected report-only, got: {output['reason'][:400]}")
        if read_state_file(state_path_for(session_hash)).get("completed") is not True:
            return fail("report-only failure must mark the cycle completed")
        print("  PASS: no issue at/above the auto-fix threshold -> report-only")
        return True


def test_deep_default_splits_fix_and_report():
    """Deep tier with the default threshold (high) fixes critical/high and reports the rest."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash, env=DEEP_ENV)
        if state is None:
            return False
        write_results(session_hash, state["round_id"], "fail", [
            issue("low", description="LOW-ONE"), issue("critical", description="CRIT-ONE"),
        ])
        output = expect_block(*run_hook(transcript))
        if output is None:
            return False
        reason = output["reason"]
        fix_part, _, report_part = reason.partition("Report to the user without fixing (1)")
        if "Fix these 1 issue(s)" not in fix_part or "CRIT-ONE" not in fix_part:
            return fail(f"critical issue should be in the fix list: {reason[:500]}")
        if "LOW-ONE" not in report_part or "LOW-ONE" in fix_part:
            return fail(f"low issue should be report-only: {reason[:500]}")
        print("  PASS: deep default threshold splits fixable and report-only issues")
        return True


def test_pass_at_max_auto_continues_allows():
    """A pass at the last auto-continue allows the stop: silently, or with a warning when there are notes."""
    for notes, expect_warning in (([], False), ([issue("low", description="NOTE-ONE")], True)):
        with hook_session() as (transcript, session_hash):
            write_edit_transcript(transcript)
            state = dispatch(transcript, session_hash)
            if state is None:
                return False
            state_path = state_path_for(session_hash)
            state["auto_continue_count"] = 2
            state_path.write_text(json.dumps(state), encoding="utf-8")
            write_results(session_hash, state["round_id"], "pass", notes)
            # A pass that reaches the bound happens inside a hook-forced chain
            code, stdout, stderr = run_hook(transcript, stop_hook_active=True)
            if expect_warning:
                if code != 0 or not stdout:
                    return fail(f"expected a warning, got code={code} stdout={stdout!r}")
                output = json.loads(stdout)
                if set(output) != {"systemMessage"} or "NOTE-ONE" not in output["systemMessage"]:
                    return fail(f"warning should carry the non-blocking finding: {output}")
            elif not expect_silent_allow(code, stdout, stderr):
                return False
            if read_state_file(state_path).get("completed") is not True:
                return fail("pass at max auto-continues must mark the cycle completed")
    print("  PASS: pass at max auto-continues allows (warns when there are notes)")
    return True


def test_project_checklist_replaces_default():
    """review_checklist_file in hook-overrides.json replaces the default checklist in the prompt."""
    sentinel = "PROJECT-CHECKLIST-SENTINEL: every widget has an owner"
    with tempfile.TemporaryDirectory() as tmp:
        checklist = Path(tmp) / "checklist.md"
        checklist.write_text(f"1. {sentinel}\n", encoding="utf-8")
        with overrides_file(json.dumps({"review_checklist_file": checklist.as_posix()})):
            with hook_session() as (transcript, session_hash):
                write_edit_transcript(transcript)
                state = dispatch(transcript, session_hash)
                if state is None:
                    return False
                prompt = prompt_path_for(session_hash, state["round_id"]).read_text(encoding="utf-8")
    if sentinel not in prompt:
        return fail("project checklist text missing from the reviewer prompt")
    if "Silent failures — swallowed exceptions" in prompt:
        return fail("default checklist should be replaced, not appended")
    print("  PASS: project checklist replaces the default checklist")
    return True


# =============================================================================
# Runner
# =============================================================================

def test_review_not_disabled_after_loop_bound():
    """[Review-round regression] Exhausted per-turn bounds must not disable review for the session.

    Old behaviour: auto_continue_count/fail_count/completed carried across
    user turns, so after 3 passes every later stop was allowed silently.
    """
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        state.update(
            subagent_pending=False, round_id="", review_agents=[],
            auto_continue_count=3, fail_count=3, completed=True,
        )
        state_path_for(session_hash).write_text(json.dumps(state), encoding="utf-8")
        # New user turn with new edits
        write_transcript(transcript, [
            edit_event("/test/file.py", "x" * 600),
            edit_event("/test/other.py", "y" * 900),
        ])
        if dispatch(transcript, session_hash) is None:
            return fail("a fresh turn with new edits must start a new review round")
    print("  PASS: loop bounds reset per user turn; review keeps running")
    return True


def test_stale_state_keeps_pending_round():
    """[Review-round regression] A stale session still consumes its pending round instead of dropping it."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        state["timestamp"] = "2000-01-01T00:00:00"
        state_path_for(session_hash).write_text(json.dumps(state), encoding="utf-8")
        write_results(session_hash, state["round_id"], "pass", [])
        output = expect_block(*run_hook(transcript, stop_hook_active=True))
        if output is None or "Design audit passed" not in output["reason"]:
            return fail(f"stale pending round should be consumed, got {output}")
    print("  PASS: stale state keeps and consumes its pending round")
    return True


def test_scavenger_never_mutates_other_sessions():
    """[Review-round regression] The scavenger is read-only on other sessions' state."""
    with hook_session() as (other_transcript, other_hash):
        write_edit_transcript(other_transcript)
        other = dispatch(other_transcript, other_hash)
        if other is None:
            return False
        other_path = state_path_for(other_hash)
        marker = STATE_DIR / f"scavenged-{other_hash}-{other['round_id']}.marker"
        try:
            with hook_session() as (transcript, _):
                write_edit_transcript(transcript)
                # Young pending round: untouched, not scored
                before = other_path.read_text(encoding="utf-8")
                run_hook(transcript)
                if other_path.read_text(encoding="utf-8") != before or marker.exists():
                    return fail("scavenger touched a live (young) pending round")
                # Old pending round: scored once via a marker, state untouched
                aged = json.loads(before)
                aged["timestamp"] = "2000-01-01T00:00:00"
                aged_text = json.dumps(aged)
                other_path.write_text(aged_text, encoding="utf-8")
                run_hook(transcript, stop_hook_active=True)
                if other_path.read_text(encoding="utf-8") != aged_text:
                    return fail("scavenger modified another session's state file")
                if not marker.exists():
                    return fail("scavenger should mark an abandoned round as scored")
                if not _metrics_for(other_hash[:8]):
                    return fail("scavenger should log a metric for the abandoned round")
        finally:
            marker.unlink(missing_ok=True)
    print("  PASS: scavenger is read-only on other sessions")
    return True


def test_pending_round_without_reviewers_fails_loud():
    """A pending round with no reviewers recorded is an error, never a vacuous pass."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        state["review_agents"] = []
        state_path_for(session_hash).write_text(json.dumps(state), encoding="utf-8")
        code, stdout, _ = run_hook(transcript)
        if code != 0 or not check_fail_loud(stdout, False, "has no reviewers"):
            return fail("empty reviewer list must fail loud")
        # Self-heal: the broken round is discarded, so the next stop is clean
        healed = read_state_file(state_path_for(session_hash))
        if healed.get("subagent_pending") or healed.get("round_id"):
            return fail(f"broken round should be discarded, got {healed}")
    print("  PASS: empty reviewer list fails loud once, then self-heals")
    return True


def test_completion_guard_warns_on_unreviewed_edits():
    """[Review-round regression] Guards that mark new edits as seen must warn, not allow silently."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        state.update(subagent_pending=False, round_id="", review_agents=[], completed=True)
        state_path_for(session_hash).write_text(json.dumps(state), encoding="utf-8")
        write_transcript(transcript, [
            edit_event("/test/file.py", "x" * 600),
            edit_event("/test/other.py", "y" * 900),
        ])
        code, stdout, _ = run_hook(transcript, stop_hook_active=True)
        if code != 0 or not stdout:
            return fail(f"expected a visible warning, got code={code} stdout={stdout!r}")
        output = json.loads(stdout)
        if set(output) != {"systemMessage"} or "NOT reviewed" not in output["systemMessage"]:
            return fail(f"guard must warn about unreviewed edits: {output}")
    print("  PASS: completion guard warns when it skips new edits")
    return True


FIELD_CORRUPTION_CASES = [
    ("bad timestamp", {"timestamp": "not-a-date"}),
    ("string counter", {"auto_continue_count": "3"}),
    ("non-list files", {"last_files_seen": 7}),
    ("bool as int", {"fail_count": True}),
    ("tz-aware timestamp", {"timestamp": "2026-10-07T10:00:00+00:00"}),
    ("empty violation categories", {"violation_history": {"a.py": {}}}),
    ("non-dict violation entry", {"violation_history": {"a.py": 3}}),
]


def test_field_level_corruption_self_heals():
    """[Live-round aeceec01 regression] Valid JSON with mistyped fields is quarantined once, not every stop."""
    for label, bad in FIELD_CORRUPTION_CASES:
        with hook_session() as (transcript, session_hash):
            write_edit_transcript(transcript)
            state = dispatch(transcript, session_hash)
            if state is None:
                return False
            state_path = state_path_for(session_hash)
            state.update(bad)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            code, stdout, _ = run_hook(transcript)
            if code != 0 or not check_fail_loud(stdout, False, "corrupt state file moved to"):
                return fail(f"{label}: must fail loud and quarantine")
            if state_path.exists():
                return fail(f"{label}: corrupt state should be moved aside")
            # Next stop is clean (fresh state), not another failure
            code, stdout, _ = run_hook(transcript)
            if stdout and "hook error" in stdout:
                return fail(f"{label}: second stop still fails: {stdout[:200]}")
    print("  PASS: field-level corruption fails loud once, then self-heals")
    return True


def test_auto_continue_limit_warns_on_unreviewed_edits():
    """[Live-round aeceec01 regression] The auto-continue-limit guard warns about new unreviewed edits."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        state.update(subagent_pending=False, round_id="", review_agents=[], auto_continue_count=3)
        state_path_for(session_hash).write_text(json.dumps(state), encoding="utf-8")
        write_transcript(transcript, [
            edit_event("/test/file.py", "x" * 600),
            edit_event("/test/other.py", "y" * 900),
        ])
        code, stdout, _ = run_hook(transcript, stop_hook_active=True)
        if code != 0 or not stdout:
            return fail(f"expected a visible warning, got code={code} stdout={stdout!r}")
        output = json.loads(stdout)
        if set(output) != {"systemMessage"} or "auto-continue limit reached" not in output["systemMessage"]:
            return fail(f"limit guard must warn about unreviewed edits: {output}")
    print("  PASS: auto-continue limit guard warns when it skips new edits")
    return True


def test_deep_completed_warns_on_unreviewed_edits():
    """[Live-round aeceec01 regression] The deep-cycle-completed exit warns about new unreviewed edits."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        state.update(subagent_pending=False, round_id="", review_agents=[], completed=True, tier="deep")
        state_path_for(session_hash).write_text(json.dumps(state), encoding="utf-8")
        write_transcript(transcript, [
            edit_event("/test/file.py", "x" * 600),
            edit_event("/test/other.py", "y" * 900),
        ])
        code, stdout, _ = run_hook(transcript, stop_hook_active=True)
        if code != 0 or not stdout:
            return fail(f"expected a visible warning, got code={code} stdout={stdout!r}")
        output = json.loads(stdout)
        if set(output) != {"systemMessage"} or "deep review cycle completed" not in output["systemMessage"]:
            return fail(f"deep-completed exit must warn about unreviewed edits: {output}")
    print("  PASS: deep-completed exit warns when it skips new edits")
    return True


def test_input_error_log_is_written():
    """[Live-round aeceec01 regression] An input error is logged to the log file the message cites."""
    code, stdout, _ = run_hook(None, raw_input="this is not json")
    output = json.loads(stdout)
    log_path = output["systemMessage"].rsplit("(log: ", 1)[-1].rstrip(")")
    if not Path(log_path).is_file() or "JSONDecodeError" not in Path(log_path).read_text(encoding="utf-8"):
        return fail(f"cited log {log_path} must exist and contain the error")
    print("  PASS: input errors land in the cited log")
    return True


def test_tier_change_keeps_fail_count_in_chain():
    """[Live-round da48f4e8 regression] A tier change inside a hook-forced chain keeps fail_count.

    Resetting it on tier change would let a fail/fix loop that oscillates
    between tiers run past max_fail_retries without bound.
    """
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state = dispatch(transcript, session_hash)
        if state is None:
            return False
        state_path = state_path_for(session_hash)
        # One failed round so far; now a fix of a different size changes the tier
        state.update(subagent_pending=False, round_id="", review_agents=[], fail_count=2, tier="quick")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        write_transcript(transcript, [
            edit_event("/test/file.py", "x" * 600),
            edit_event("/test/b.py", "y" * 9000),
            edit_event("/test/c.py", "z" * 9000),
            edit_event("/test/d.py", "w" * 9000),
        ])
        code, stdout, _ = run_hook(transcript, stop_hook_active=True)
        after = read_state_file(state_path)
        if after.get("tier") == "quick":
            return fail(f"setup did not change the tier: {after.get('tier')}")
        if after.get("fail_count") != 2:
            return fail(f"tier change within a chain must keep fail_count=2, got {after.get('fail_count')}")
    print("  PASS: tier change keeps fail_count within a chain")
    return True


LAYERS = [
    ("Regression tests (bugs fixed in 3.0.0)", [
        test_bug1_deep_cross_module_subagent_dispatch,
        test_bug1_deep_cross_module_delegated_payload,
        test_bug2_pending_results_read_without_new_edits,
        test_bug3_exception_fails_loud,
        test_bug3_bad_config_fails_loud,
        test_bug3_invalid_env_fails_loud,
        test_bug4_missing_transcript_fails_loud,
        test_bug5_metrics_session_id_and_provenance,
    ]),
    ("Fail-loud input handling", [
        test_bad_hook_input_fails_loud,
    ]),
    ("Subagent review flow", [
        test_missing_results_rerequest_then_unreviewed,
        test_invalid_results_are_rerequested_with_reason,
        test_fail_lists_issues_critical_first,
        test_deep_auto_fix_none_is_report_only,
        test_deep_no_qualifying_issue_is_report_only,
        test_deep_default_splits_fix_and_report,
        test_pass_at_max_auto_continues_allows,
        test_project_checklist_replaces_default,
    ]),
    ("Design-audit review findings (round 656dfe68)", [
        test_review_not_disabled_after_loop_bound,
        test_stale_state_keeps_pending_round,
        test_scavenger_never_mutates_other_sessions,
        test_pending_round_without_reviewers_fails_loud,
    ]),
    ("Design-audit review findings (round 13479109)", [
        test_completion_guard_warns_on_unreviewed_edits,
    ]),
    ("Live review findings (round aeceec01)", [
        test_field_level_corruption_self_heals,
        test_auto_continue_limit_warns_on_unreviewed_edits,
        test_deep_completed_warns_on_unreviewed_edits,
        test_input_error_log_is_written,
    ]),
    ("Live review findings (round da48f4e8)", [
        test_tier_change_keeps_fail_count_in_chain,
    ]),
]


def main():
    run_layers("Review Flow Tests (v3)", LAYERS)


if __name__ == "__main__":
    main()
