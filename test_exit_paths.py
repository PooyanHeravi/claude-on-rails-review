#!/usr/bin/env python3
"""
Test suite for exit path correctness in the stop-design-audit hook.

Layers:
  Layer 1 — Static audit: scans package source for structural violations and
            checks every exit-helper call against EXIT_PATH_REGISTRY.
  Layer 2 — Behavioral tests: runs the hook with mock state/transcript and
            verifies stdout/state.
  Layer 3 — Delegated mode.
  Layer 4 — Subagent mode dispatch (the full v3 review flow lives in
            test_review_flow.py, which reuses the helpers below).

Runtime files live in <hooks_dir>/state/; hooks_dir is this repo root.

Run:  python test_exit_paths.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

HOOK_DIR = Path(__file__).parent
HOOK_SCRIPT = HOOK_DIR / "stop-design-audit.py"
PACKAGE_DIR = HOOK_DIR / "stop_design_audit"
STATE_DIR = HOOK_DIR / "state"
OVERRIDES_PATH = HOOK_DIR / "hook-overrides.json"

# These constants must match the hook defaults (config.py)
MAX_AUTO_CONTINUES = 3
SESSION_HASH_LENGTH = 12

# An edit big enough to land in the quick tier (skip threshold is 500 chars)
QUICK_EDIT_CHARS = 600


def _read_all_package_sources() -> list[tuple[Path, str]]:
    """Read all .py files in the package directory. Returns list of (path, source)."""
    return [(p, p.read_text(encoding="utf-8")) for p in sorted(PACKAGE_DIR.glob("*.py"))]


# =============================================================================
# Shared helpers (also imported by test_review_flow.py)
# =============================================================================

def fail(msg: str) -> bool:
    print(f"  FAIL: {msg}")
    return False


def get_session_hash(transcript_path: str) -> str:
    return hashlib.md5(transcript_path.encode()).hexdigest()[:SESSION_HASH_LENGTH]


def state_path_for(session_hash: str) -> Path:
    return STATE_DIR / f"stop-hook-state-{session_hash}.json"


def prompt_path_for(session_hash: str, round_id: str, agent_id: str = "reviewer") -> Path:
    return STATE_DIR / f"review-prompt-{session_hash}-{round_id}-{agent_id}.md"


def results_path_for(session_hash: str, round_id: str, agent_id: str = "reviewer") -> Path:
    return STATE_DIR / f"review-results-{session_hash}-{round_id}-{agent_id}.json"


def coordinator_path_for(session_hash: str) -> Path:
    return STATE_DIR / f"coordinator-instructions-{session_hash}.json"


def cleanup_session(session_hash: str) -> None:
    """Remove every runtime file the hook wrote for this session."""
    for f in STATE_DIR.glob(f"*{session_hash}*"):
        f.unlink(missing_ok=True)


@contextmanager
def hook_session() -> Iterator[tuple[Path, str]]:
    """Temp transcript path + its session hash; session files removed afterwards."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript = Path(tmpdir) / "transcript.jsonl"
        session_hash = get_session_hash(str(transcript))
        try:
            yield transcript, session_hash
        finally:
            cleanup_session(session_hash)


@contextmanager
def overrides_file(content: str) -> Iterator[Path]:
    """Write hook-overrides.json in the hooks dir; always removed afterwards."""
    if OVERRIDES_PATH.exists():
        raise RuntimeError(f"{OVERRIDES_PATH} already exists — refusing to overwrite it")
    try:
        OVERRIDES_PATH.write_text(content, encoding="utf-8")
        yield OVERRIDES_PATH
    finally:
        OVERRIDES_PATH.unlink(missing_ok=True)


def edit_event(file_path: str, new_string: str, old_string: str = "old") -> dict:
    return {
        "type": "tool_use",
        "name": "Edit",
        "input": {"file_path": file_path, "old_string": old_string, "new_string": new_string},
    }


def write_transcript(path: Path, events: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def write_edit_transcript(
    path: Path, files: tuple[str, ...] = ("/test/file.py",), chars: int = QUICK_EDIT_CHARS
) -> None:
    """Transcript with one Edit of `chars` characters per file."""
    write_transcript(path, [edit_event(f, "x" * chars) for f in files])


def create_mock_state(state_path: Path, transcript_path: str, **overrides) -> None:
    """Create a mock state file with sensible defaults."""
    state = {
        "session_id": transcript_path,
        "last_total_diff": 100,
        "last_files_seen": ["/test/file.py"],
        "tier": "quick",
        "auto_continue_count": 0,
        "fail_count": 0,
        "round_id": "",
        "passed_agents": [],
        "completed": False,
        "violation_history": {},
    }
    state.update(overrides)
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_hook(
    transcript_path: Path | None,
    env_overrides: dict | None = None,
    *,
    stop_hook_active: bool | None = None,
    raw_input: str | None = None,
) -> tuple[int, str, str]:
    """Run the hook and return (exit_code, stdout, stderr).

    The caller's CLAUDE_HOOK_* environment is stripped so tests see defaults.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_HOOK_")}
    if env_overrides:
        env.update(env_overrides)
    if raw_input is None:
        payload: dict = {}
        if transcript_path is not None:
            payload["transcript_path"] = str(transcript_path)
        if stop_hook_active is not None:
            payload["stop_hook_active"] = stop_hook_active
        raw_input = json.dumps(payload)
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=raw_input,
        capture_output=True,
        text=True,
        cwd=str(HOOK_DIR),
        env=env,
        timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def read_state_file(state_path: Path) -> dict | None:
    """Read and return state file contents, or None if missing."""
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def expect_block(exit_code: int, stdout: str, stderr: str = "") -> dict | None:
    """Return the parsed block output, or None (after printing why) if not a block."""
    if exit_code != 0:
        fail(f"exit code {exit_code}, expected 0 (stderr: {stderr[:300]})")
        return None
    if not stdout:
        fail("expected a block decision, got empty stdout")
        return None
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        fail(f"stdout is not JSON: {stdout[:300]}")
        return None
    if output.get("decision") != "block" or "systemMessage" in output:
        fail(f"expected a plain block, got: {stdout[:400]}")
        return None
    return output


def expect_silent_allow(exit_code: int, stdout: str, stderr: str = "") -> bool:
    if exit_code != 0:
        return fail(f"exit code {exit_code}, expected 0 (stderr: {stderr[:300]})")
    if stdout:
        return fail(f"expected silent allow, got: {stdout[:400]}")
    return True


# =============================================================================
# Layer 1: Static Audit
# =============================================================================

EXIT_HELPERS = ("allow_stop", "warn_and_allow", "block_with_message", "fail_loud")


def test_no_raw_sys_exit():
    """Verify no raw sys.exit() outside the exit helper functions."""
    violations = []

    for py_file, source in _read_all_package_sources():
        tree = ast.parse(source, filename=str(py_file))
        helper_spans = [
            (n.lineno, n.end_lineno)
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name in EXIT_HELPERS
            and py_file.name == "exit_helpers.py"
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "exit"
                    and isinstance(func.value, ast.Name) and func.value.id == "sys"):
                continue
            if not any(start <= node.lineno <= end for start, end in helper_spans):
                violations.append(f"{py_file.name}:{node.lineno}")

    if violations:
        print(f"  FAIL: Found raw sys.exit() at: {violations}")
        print(f"        All exits must use one of {EXIT_HELPERS}")
        return False

    print("  PASS: No raw sys.exit() outside exit helpers")
    return True


def test_no_print_before_allow_stop():
    """Verify no print(json.dumps(...)) immediately before allow_stop() — the exact bug pattern."""
    violations = []

    for py_file, source in _read_all_package_sources():
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("allow_stop("):
                for j in range(i - 1, max(i - 6, -1), -1):
                    prev = lines[j].strip()
                    if not prev or prev.startswith("#"):
                        continue
                    if "print(json.dumps(" in prev or '"decision"' in prev:
                        violations.append((py_file.name, i + 1, j + 1))
                    break

    if violations:
        for fname, allow_line, print_line in violations:
            print(f"  FAIL: print+allow_stop combo at {fname}:{print_line}-{allow_line} (will loop!)")
        return False

    print("  PASS: No print(json.dumps) before allow_stop()")
    return True


def _helper_call_sites(helper: str) -> list[str]:
    """Call sites of an exit helper (definitions, comments, docstring lines excluded)."""
    pattern = re.compile(rf"\b{helper}\(")
    sites = []
    for py_file, source in _read_all_package_sources():
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", '"', "'")) or f"def {helper}" in stripped:
                continue
            if pattern.search(stripped):
                sites.append(f"{py_file.name}:{i}")
    return sites


def _registry_count(exit_type: str) -> int:
    from stop_design_audit.exit_helpers import EXIT_PATH_REGISTRY

    return sum(1 for e in EXIT_PATH_REGISTRY.values() if e["type"] == exit_type)


def _check_helper_count(helper: str, exit_type: str) -> bool:
    sites = _helper_call_sites(helper)
    expected = _registry_count(exit_type)
    if len(sites) != expected:
        print(f"  FAIL: Found {len(sites)} {helper}() calls but registry has {expected} '{exit_type}' entries")
        print(f"        {helper} at: {sites}")
        return False
    print(f"  PASS: {len(sites)} {helper}() calls match {expected} registry '{exit_type}' entries")
    return True


def test_registry_types_known():
    """Verify every EXIT_PATH_REGISTRY entry has a known exit type."""
    from stop_design_audit.exit_helpers import EXIT_PATH_REGISTRY

    known = {"allow", "block", "warn", "fail_loud"}
    unknown = {k: e["type"] for k, e in EXIT_PATH_REGISTRY.items() if e["type"] not in known}
    if unknown:
        return fail(f"registry entries with unknown types: {unknown}")
    print(f"  PASS: all {len(EXIT_PATH_REGISTRY)} registry entries use known types")
    return True


def test_block_with_message_count():
    """Verify block_with_message() call count matches registry 'block' entries."""
    return _check_helper_count("block_with_message", "block")


def test_allow_stop_count():
    """Verify allow_stop() call count matches registry 'allow' entries."""
    return _check_helper_count("allow_stop", "allow")


def test_warn_and_allow_count():
    """Verify warn_and_allow() call count matches registry 'warn' entries."""
    return _check_helper_count("warn_and_allow", "warn")


def test_fail_loud_count():
    """Verify fail_loud() call count matches registry 'fail_loud' entries."""
    return _check_helper_count("fail_loud", "fail_loud")


# =============================================================================
# Layer 2: Behavioral Tests
# =============================================================================

def test_no_code_modified_silent_exit():
    """Hook should silently allow stop when no Edit/Write tools were used."""
    with hook_session() as (transcript, _):
        write_transcript(transcript, [{"type": "assistant", "content": "just talking"}])
        if not expect_silent_allow(*run_hook(transcript)):
            return False
        print("  PASS: Silent exit when no code modified")
        return True


def test_completed_flag_resets_on_allow():
    """When completed=True, hook should allow stop and reset completed to False."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state_path = state_path_for(session_hash)
        create_mock_state(
            state_path, str(transcript),
            completed=True, tier="quick", auto_continue_count=1,
            last_total_diff=50, last_files_seen=["/test/file.py"],
        )
        if not expect_silent_allow(*run_hook(transcript, stop_hook_active=True)):
            return False
        state = read_state_file(state_path)
        if state is None:
            return fail("state file was deleted instead of updated")
        if state.get("completed") is not False:
            return fail(f"completed should be False, got: {state.get('completed')}")
        print("  PASS: completed flag reset to False on allow stop")
        return True


def test_skip_tier_max_continues_saves_state():
    """Skip tier at max auto-continues should save state with completed=True and exit silently."""
    with hook_session() as (transcript, session_hash):
        write_transcript(transcript, [edit_event("/test/file.py", "b", old_string="a")])
        state_path = state_path_for(session_hash)
        create_mock_state(
            state_path, str(transcript),
            tier="skip", auto_continue_count=MAX_AUTO_CONTINUES - 1,
            last_total_diff=0, last_files_seen=[],
        )
        if not expect_silent_allow(*run_hook(transcript, stop_hook_active=True)):
            return False
        state = read_state_file(state_path)
        if state is None:
            return fail("state file missing — should have been saved")
        if state.get("completed") is not True:
            return fail(f"completed should be True, got: {state.get('completed')}")
        print("  PASS: Skip tier max continues saves state and exits silently")
        return True


def test_deep_completed_silent_exit():
    """Deep review completed flag should trigger silent exit, not block."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript, chars=21)
        create_mock_state(
            state_path_for(session_hash), str(transcript),
            completed=True, tier="deep", auto_continue_count=1,
            last_total_diff=50, last_files_seen=["/test/file.py"],
        )
        if not expect_silent_allow(*run_hook(transcript, stop_hook_active=True)):
            return False
        print("  PASS: Deep completed triggers silent exit")
        return True


# =============================================================================
# Layer 3: Delegated Mode Tests
# =============================================================================

DELEGATED_ENV = {"CLAUDE_HOOK_REVIEW_MODE": "delegated"}


def _pending_delegated_state(transcript: Path, session_hash: str, **overrides) -> Path:
    state_path = state_path_for(session_hash)
    fields = {
        "tier": "quick",
        "round_id": "abc12345",
        "delegated_pending": True,
        "delegated_dispatch_time": datetime.now().isoformat(),
        "delegated_blocked_once": False,
        "last_total_diff": 0,
        "last_files_seen": [],
    }
    fields.update(overrides)
    create_mock_state(state_path, str(transcript), **fields)
    return state_path


def test_delegated_dispatches_coordinator():
    """Delegated mode should dispatch coordinator and set delegated_pending=True."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        output = expect_block(*run_hook(transcript, env_overrides=DELEGATED_ENV))
        if output is None:
            return False
        if "background coordinator" not in output["reason"].lower():
            return fail("block message should mention 'background coordinator'")

        state = read_state_file(state_path_for(session_hash))
        if state is None:
            return fail("state file missing")
        if not state.get("delegated_pending"):
            return fail(f"delegated_pending should be True, got: {state.get('delegated_pending')}")
        if not state.get("delegated_dispatch_time"):
            return fail("delegated_dispatch_time should be set")
        if state.get("delegated_blocked_once"):
            return fail("delegated_blocked_once should be False on dispatch")

        instructions_path = coordinator_path_for(session_hash)
        if not instructions_path.exists():
            return fail(f"coordinator instructions not created at {instructions_path}")
        payload = json.loads(instructions_path.read_text(encoding="utf-8"))
        for key in ("tier", "round_id", "pending_agents", "agent_definitions", "code_hunks"):
            if key not in payload:
                return fail(f"payload missing key: {key}")
        print("  PASS: Delegated mode dispatches coordinator correctly")
        return True


def test_delegated_pending_blocks_once():
    """When coordinator is pending and not blocked yet, should block once with continue message."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        state_path = _pending_delegated_state(transcript, session_hash)
        output = expect_block(*run_hook(transcript, env_overrides=DELEGATED_ENV))
        if output is None:
            return False
        if "still running" not in output["reason"].lower():
            return fail(f"message should mention 'still running', got: {output['reason'][:100]}")
        state = read_state_file(state_path)
        if not state.get("delegated_blocked_once"):
            return fail("delegated_blocked_once should be True after first block")
        print("  PASS: Blocks once while waiting for coordinator")
        return True


def test_delegated_pending_allows_second_stop():
    """When coordinator is pending and already blocked once, should allow stop (no loop)."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        _pending_delegated_state(transcript, session_hash, delegated_blocked_once=True)
        if not expect_silent_allow(*run_hook(transcript, env_overrides=DELEGATED_ENV)):
            return False
        print("  PASS: Allows stop after blocking once (no loop)")
        return True


def test_delegated_pending_with_results():
    """When coordinator returns passing results, should process them and reset delegated state."""
    with hook_session() as (transcript, session_hash):
        round_id = "abc12345"
        results_data = {"round_id": round_id, "agents": {"reviewer": {"status": "pass", "issues": []}}}
        write_transcript(transcript, [
            edit_event("/test/file.py", "x" * QUICK_EDIT_CHARS),
            {
                "type": "assistant",
                "content": "<!--REVIEW_RESULTS_START-->\n"
                + json.dumps(results_data, indent=2)
                + "\n<!--REVIEW_RESULTS_END-->",
            },
        ])
        state_path = _pending_delegated_state(transcript, session_hash, round_id=round_id)
        output = expect_block(*run_hook(transcript, env_overrides=DELEGATED_ENV))
        if output is None:
            return False
        if "All reviews passed" not in output["reason"]:
            return fail(f"expected 'All reviews passed', got: {output['reason'][:200]}")
        state = read_state_file(state_path)
        if state is None:
            return fail("state file missing")
        if state.get("delegated_pending"):
            return fail("delegated_pending should be False after results processed")
        if state.get("auto_continue_count") != 1:
            return fail(f"auto_continue_count should be 1, got {state.get('auto_continue_count')}")
        print("  PASS: Results processed and delegated state reset")
        return True


def test_delegated_timeout_fallback():
    """When coordinator times out, should fall back to inline agent mode."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        old_time = (datetime.now() - timedelta(seconds=600)).isoformat()
        state_path = _pending_delegated_state(
            transcript, session_hash, delegated_dispatch_time=old_time
        )
        output = expect_block(*run_hook(transcript, env_overrides=DELEGATED_ENV))
        if output is None:
            return False
        if "background coordinator" in output["reason"].lower():
            return fail("timeout fallback should use inline agent mode, not delegated")
        state = read_state_file(state_path)
        if state and state.get("delegated_pending"):
            return fail("delegated_pending should be False after timeout")
        print("  PASS: Timeout triggers fallback to inline agent mode")
        return True


def test_delegated_message_compact():
    """Delegated block message should be compact (under 25 lines)."""
    with hook_session() as (transcript, _):
        write_edit_transcript(transcript)
        output = expect_block(*run_hook(transcript, env_overrides=DELEGATED_ENV))
        if output is None:
            return False
        line_count = len(output["reason"].splitlines())
        if line_count > 25:
            return fail(f"delegated message is {line_count} lines, expected <= 25")
        print(f"  PASS: Delegated message is compact ({line_count} lines)")
        return True


def test_delegated_payload_completeness():
    """Coordinator payload should contain all required fields."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        if expect_block(*run_hook(transcript, env_overrides=DELEGATED_ENV)) is None:
            return False
        instructions_path = coordinator_path_for(session_hash)
        if not instructions_path.exists():
            return fail("instructions file not created")
        payload = json.loads(instructions_path.read_text(encoding="utf-8"))
        required_keys = [
            "tier", "round_id", "diff_size", "total_file_count",
            "pending_agents", "agent_definitions", "file_list",
            "code_hunks", "violation_history", "results_schema",
        ]
        missing = [k for k in required_keys if k not in payload]
        if missing:
            return fail(f"payload missing keys: {missing}")
        if "fail_criteria" not in payload.get("results_schema", {}):
            return fail("results_schema missing fail_criteria")
        print("  PASS: Payload has all required fields")
        return True


# =============================================================================
# Layer 4: Subagent Mode Dispatch (default mode)
# =============================================================================

def test_subagent_dispatches_reviewer():
    """Default (subagent) mode writes the reviewer prompt, marks the round pending, blocks for a foreground run."""
    with hook_session() as (transcript, session_hash):
        write_edit_transcript(transcript)
        output = expect_block(*run_hook(transcript))
        if output is None:
            return False
        reason = output["reason"]
        if "run_in_background=false" not in reason:
            return fail(f"dispatch must ask for a foreground run, got: {reason[:300]}")
        if "run_in_background=true" in reason:
            return fail("dispatch must not ask for a background run")

        state = read_state_file(state_path_for(session_hash))
        if state is None:
            return fail("state file missing")
        expected = {"subagent_pending": True, "review_agents": ["reviewer"], "review_attempts": 1, "tier": "quick"}
        for key, value in expected.items():
            if state.get(key) != value:
                return fail(f"state[{key!r}] = {state.get(key)!r}, expected {value!r}")
        if not state.get("round_id") or not state.get("subagent_dispatch_time"):
            return fail("round_id and subagent_dispatch_time must be set on dispatch")

        prompt_path = prompt_path_for(session_hash, state["round_id"])
        if not prompt_path.exists():
            return fail(f"reviewer prompt not written at {prompt_path}")
        if prompt_path.name not in reason:
            return fail("block message must point the reviewer at its prompt file")
        prompt = prompt_path.read_text(encoding="utf-8")
        if results_path_for(session_hash, state["round_id"]).name not in prompt:
            return fail("prompt must name the results file the reviewer writes")
        print("  PASS: Subagent mode dispatches one foreground reviewer")
        return True


def test_subagent_message_compact():
    """Subagent dispatch message should be very compact (under 8 lines)."""
    with hook_session() as (transcript, _):
        write_edit_transcript(transcript)
        output = expect_block(*run_hook(transcript))
        if output is None:
            return False
        line_count = len(output["reason"].splitlines())
        if line_count > 8:
            return fail(f"subagent message is {line_count} lines, expected <= 8")
        print(f"  PASS: Subagent message is compact ({line_count} lines)")
        return True


# =============================================================================
# Runner
# =============================================================================

LAYERS = [
    ("Layer 1: Static Audit", [
        test_no_raw_sys_exit,
        test_no_print_before_allow_stop,
        test_registry_types_known,
        test_block_with_message_count,
        test_allow_stop_count,
        test_warn_and_allow_count,
        test_fail_loud_count,
    ]),
    ("Layer 2: Behavioral Tests", [
        test_no_code_modified_silent_exit,
        test_completed_flag_resets_on_allow,
        test_skip_tier_max_continues_saves_state,
        test_deep_completed_silent_exit,
    ]),
    ("Layer 3: Delegated Mode Tests", [
        test_delegated_dispatches_coordinator,
        test_delegated_pending_blocks_once,
        test_delegated_pending_allows_second_stop,
        test_delegated_pending_with_results,
        test_delegated_timeout_fallback,
        test_delegated_message_compact,
        test_delegated_payload_completeness,
    ]),
    ("Layer 4: Subagent Mode Dispatch", [
        test_subagent_dispatches_reviewer,
        test_subagent_message_compact,
    ]),
]


def run_layers(title: str, layers: list) -> None:
    """Run test layers, print a summary, exit non-zero on any failure."""
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)

    results = []
    for layer_name, tests in layers:
        print(f"\n--- {layer_name} ---")
        for test_fn in tests:
            print(f"\n{test_fn.__doc__}")
            try:
                ok = bool(test_fn())
            except Exception as e:  # a crashing test is a failing test, not a silent skip
                ok = fail(f"{test_fn.__name__} raised {type(e).__name__}: {e}")
            results.append((test_fn.__name__, ok))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, r in results if r)
    for name, result in results:
        print(f"  {'PASS' if result else 'FAIL'}: {name}")
    print(f"\nTotal: {passed}/{len(results)} tests passed")
    sys.exit(0 if passed == len(results) else 1)


def main():
    run_layers("Exit Path Audit & Tests", LAYERS)


if __name__ == "__main__":
    main()
