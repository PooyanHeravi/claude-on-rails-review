#!/usr/bin/env python3
"""
Test suite for exit path correctness in stop-design-audit.py.

Two layers of protection:
  Layer 1 — Static audit: scans source code for structural violations
  Layer 2 — Behavioral tests: runs hook with mock state/transcript, verifies stdout/state

Run:  python test_exit_paths.py
"""
import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_DIR = Path(__file__).parent
HOOK_SCRIPT = HOOK_DIR / "stop-design-audit.py"
PACKAGE_DIR = HOOK_DIR / "stop_design_audit"

# These constants must match the hook — import them at test time
MAX_AUTO_CONTINUES = 3
MAX_FAIL_RETRIES = 3


def _read_all_package_sources() -> list[tuple[Path, str]]:
    """Read all .py files in the package directory. Returns list of (path, source)."""
    sources = []
    for py_file in sorted(PACKAGE_DIR.glob("*.py")):
        sources.append((py_file, py_file.read_text(encoding="utf-8")))
    return sources


def _combined_package_source() -> str:
    """Concatenate all package sources for simple text searches."""
    return "\n".join(src for _, src in _read_all_package_sources())


# =============================================================================
# Helpers
# =============================================================================

def get_session_hash(transcript_path: str) -> str:
    return hashlib.md5(transcript_path.encode()).hexdigest()[:12]


def create_mock_transcript(path: Path, include_edits: bool = True, include_results: bool = False,
                           round_id: str = "", agents_results: dict | None = None):
    """Create a mock JSONL transcript."""
    entries = []
    if include_edits:
        entries.append({
            "type": "tool_use",
            "name": "Edit",
            "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": "new code changes here"}
        })
    if include_results and round_id:
        results_data = {
            "round_id": round_id,
            "agents": agents_results or {},
        }
        # Simulate inline results marker in assistant message
        entries.append({
            "type": "assistant",
            "content": f"<!--REVIEW_RESULTS_START-->\n{json.dumps(results_data, indent=2)}\n<!--REVIEW_RESULTS_END-->"
        })
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def create_mock_state(state_path: Path, transcript_path: str, **overrides):
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
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def run_hook(transcript_path: Path, env_overrides: dict | None = None) -> tuple[int, str, str]:
    """Run the hook and return (exit_code, stdout, stderr)."""
    import os
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    input_json = json.dumps({"transcript_path": str(transcript_path)})
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=input_json,
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
    with open(state_path) as f:
        return json.load(f)


# =============================================================================
# Layer 1: Static Audit
# =============================================================================

def test_no_raw_sys_exit():
    """Verify no raw sys.exit(0) outside helper functions and __main__ entry."""
    violations = []

    for py_file, source in _read_all_package_sources():
        tree = ast.parse(source, filename=str(py_file))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "exit"
                    and isinstance(func.value, ast.Name) and func.value.id == "sys"):
                continue
            if not (node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0):
                continue

            line = node.lineno

            # Allowed in: allow_stop, block_with_message (exit_helpers.py)
            in_helper = False
            for parent_node in ast.walk(tree):
                if isinstance(parent_node, ast.FunctionDef) and parent_node.name in ("allow_stop", "block_with_message"):
                    if parent_node.lineno <= line <= parent_node.end_lineno:
                        in_helper = True
                        break

            # Allowed in: __main__.py run() function (fatal error handler)
            in_entry = py_file.name == "__main__.py"

            if not in_helper and not in_entry:
                violations.append(f"{py_file.name}:{line}")

    if violations:
        print(f"  FAIL: Found raw sys.exit(0) at: {violations}")
        print("        All exits must use allow_stop() or block_with_message()")
        return False

    print("  PASS: No raw sys.exit(0) outside helpers")
    return True


def test_no_print_before_allow_stop():
    """Verify no print(json.dumps(...)) immediately before allow_stop() — the exact bug pattern."""
    violations = []

    for py_file, source in _read_all_package_sources():
        lines = source.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("allow_stop("):
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


def test_block_with_message_count():
    """Verify the number of block_with_message() calls matches expected count from registry."""
    combined = _combined_package_source()

    # Count block_with_message calls across all package files (excluding definitions, comments, docstrings)
    block_calls = []
    for py_file, source in _read_all_package_sources():
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            if "block_with_message(" in stripped and "def block_with_message" not in stripped:
                block_calls.append(f"{py_file.name}:{i}")

    # Count expected blocks from registry (in exit_helpers.py)
    registry_source = (PACKAGE_DIR / "exit_helpers.py").read_text(encoding="utf-8")
    registry_match = re.search(r"EXIT_PATH_REGISTRY\s*=\s*\{", registry_source)
    if not registry_match:
        print("  FAIL: Could not find EXIT_PATH_REGISTRY in exit_helpers.py")
        return False

    expected_blocks = registry_source.count('"type": "block"')

    if len(block_calls) != expected_blocks:
        print(f"  FAIL: Found {len(block_calls)} block_with_message() calls but registry has {expected_blocks} 'block' entries")
        print(f"        block_with_message at: {block_calls}")
        return False

    print(f"  PASS: {len(block_calls)} block_with_message() calls match {expected_blocks} registry 'block' entries")
    return True


def test_allow_stop_count():
    """Verify the number of allow_stop() calls matches expected count from registry."""
    allow_calls = []
    for py_file, source in _read_all_package_sources():
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            if "allow_stop(" in stripped and "def allow_stop" not in stripped:
                allow_calls.append(f"{py_file.name}:{i}")

    registry_source = (PACKAGE_DIR / "exit_helpers.py").read_text(encoding="utf-8")
    expected_allows = registry_source.count('"type": "allow"')

    # fatal_error uses allow_stop() inside __main__.py, so all allow entries map to allow_stop() calls
    # But fatal_error's allow_stop is in __main__.py, not exit_helpers.py
    expected_allow_stop_calls = expected_allows

    if len(allow_calls) != expected_allow_stop_calls:
        print(f"  FAIL: Found {len(allow_calls)} allow_stop() calls but expected {expected_allow_stop_calls} (registry has {expected_allows} 'allow' entries)")
        print(f"        allow_stop at: {allow_calls}")
        return False

    print(f"  PASS: {len(allow_calls)} allow_stop() calls match expected {expected_allow_stop_calls}")
    return True


# =============================================================================
# Layer 2: Behavioral Tests
# =============================================================================

def test_no_code_modified_silent_exit():
    """Hook should silently allow stop when no Edit/Write tools were used."""
    print("  Setting up: transcript with no edits...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        create_mock_transcript(transcript, include_edits=False)

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        try:
            exit_code, stdout, stderr = run_hook(transcript)
            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if stdout:
                print(f"  FAIL: Expected empty stdout for silent allow, got: {stdout[:200]}")
                return False
            print("  PASS: Silent exit when no code modified")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_completed_flag_resets_on_allow():
    """When completed=True and agent mode, hook should allow stop and reset completed to False."""
    print("  Setting up: completed=True state, agent mode...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        # Need enough chars to exceed skip tier (500 chars) so we reach agent mode completed check
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        create_mock_state(
            state_path, str(transcript),
            completed=True,
            tier="quick",
            auto_continue_count=1,
            last_total_diff=50,
            last_files_seen=["/test/file.py"],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if stdout:
                print(f"  FAIL: Expected silent exit, got: {stdout[:200]}")
                return False

            # Check state was updated with completed=False
            state = read_state_file(state_path)
            if state is None:
                print("  FAIL: State file was deleted instead of updated")
                return False
            if state.get("completed") is not False:
                print(f"  FAIL: completed should be False, got: {state.get('completed')}")
                return False

            print("  PASS: completed flag reset to False on allow stop")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_skip_tier_max_continues_saves_state():
    """Skip tier at max auto-continues should save state with completed=True and exit silently."""
    print("  Setting up: skip tier at max-1 auto-continues...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        # Create transcript with a small edit (to trigger skip tier)
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "a", "new_string": "b"}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        create_mock_state(
            state_path, str(transcript),
            tier="skip",
            auto_continue_count=MAX_AUTO_CONTINUES - 1,
            last_total_diff=10,
            last_files_seen=["/test/file.py"],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if stdout:
                print(f"  FAIL: Expected silent exit, got: {stdout[:200]}")
                return False

            # Check state was saved with completed=True
            state = read_state_file(state_path)
            if state is None:
                print("  FAIL: State file missing — should have been saved")
                return False
            if state.get("completed") is not True:
                print(f"  FAIL: completed should be True, got: {state.get('completed')}")
                return False

            print("  PASS: Skip tier max continues saves state and exits silently")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_deep_completed_silent_exit():
    """Deep review completed flag should trigger silent exit, not block."""
    print("  Setting up: completed=True, old_tier=deep...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        create_mock_transcript(transcript, include_edits=True)

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        create_mock_state(
            state_path, str(transcript),
            completed=True,
            tier="deep",
            auto_continue_count=1,
            last_total_diff=50,
            last_files_seen=["/test/file.py"],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if stdout:
                print(f"  FAIL: Expected silent exit, got: {stdout[:200]}")
                return False

            print("  PASS: Deep completed triggers silent exit")
            return True
        finally:
            state_path.unlink(missing_ok=True)


# =============================================================================
# Layer 3: Delegated Mode Tests
# =============================================================================

DELEGATED_ENV = {"CLAUDE_HOOK_REVIEW_MODE": "delegated"}


def test_delegated_dispatches_coordinator():
    """Delegated mode should dispatch coordinator and set delegated_pending=True."""
    print("  Setting up: delegated mode with enough diff for quick tier...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"
        instructions_path = HOOK_DIR / f"coordinator-instructions-{session_hash}.json"

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=DELEGATED_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message, got empty stdout")
                return False

            output = json.loads(stdout)
            if output.get("decision") != "block":
                print(f"  FAIL: Expected 'block' decision, got: {output.get('decision')}")
                return False
            if "background coordinator" not in output.get("reason", "").lower():
                print(f"  FAIL: Block message should mention 'background coordinator'")
                return False

            # Check state
            state = read_state_file(state_path)
            if state is None:
                print("  FAIL: State file missing")
                return False
            if not state.get("delegated_pending"):
                print(f"  FAIL: delegated_pending should be True, got: {state.get('delegated_pending')}")
                return False
            if not state.get("delegated_dispatch_time"):
                print(f"  FAIL: delegated_dispatch_time should be set")
                return False
            if state.get("delegated_blocked_once"):
                print(f"  FAIL: delegated_blocked_once should be False on dispatch")
                return False

            # Check instructions file was created
            if not instructions_path.exists():
                print("  FAIL: Coordinator instructions file was not created")
                return False
            payload = json.loads(instructions_path.read_text())
            for key in ("tier", "round_id", "pending_agents", "agent_definitions", "code_hunks"):
                if key not in payload:
                    print(f"  FAIL: Payload missing key: {key}")
                    return False

            print("  PASS: Delegated mode dispatches coordinator correctly")
            return True
        finally:
            state_path.unlink(missing_ok=True)
            instructions_path.unlink(missing_ok=True)


def test_delegated_pending_blocks_once():
    """When coordinator is pending and not blocked yet, should block once with continue message."""
    print("  Setting up: delegated_pending=True, blocked_once=False...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        from datetime import datetime
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id="abc12345",
            delegated_pending=True,
            delegated_dispatch_time=datetime.now().isoformat(),
            delegated_blocked_once=False,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=DELEGATED_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message, got empty stdout")
                return False

            output = json.loads(stdout)
            if output.get("decision") != "block":
                print(f"  FAIL: Expected 'block' decision, got: {output.get('decision')}")
                return False
            if "still running" not in output.get("reason", "").lower():
                print(f"  FAIL: Message should mention 'still running', got: {output.get('reason', '')[:100]}")
                return False

            # Check state updated with blocked_once=True
            state = read_state_file(state_path)
            if not state.get("delegated_blocked_once"):
                print(f"  FAIL: delegated_blocked_once should be True after first block")
                return False

            print("  PASS: Blocks once while waiting for coordinator")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_delegated_pending_allows_second_stop():
    """When coordinator is pending and already blocked once, should allow stop (no loop)."""
    print("  Setting up: delegated_pending=True, blocked_once=True...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        from datetime import datetime
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id="abc12345",
            delegated_pending=True,
            delegated_dispatch_time=datetime.now().isoformat(),
            delegated_blocked_once=True,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=DELEGATED_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if stdout:
                print(f"  FAIL: Expected silent exit (allow stop), got: {stdout[:200]}")
                return False

            print("  PASS: Allows stop after blocking once (no loop)")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_delegated_pending_with_results():
    """When coordinator returns results, should process them and reset delegated state."""
    print("  Setting up: delegated_pending=True with matching results in transcript...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        round_id = "abc12345"
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")
            # Add results from coordinator
            results_data = {
                "round_id": round_id,
                "agents": {
                    "explore_haiku": {"status": "pass", "issues": []},
                },
            }
            f.write(json.dumps({
                "type": "assistant",
                "content": f"<!--REVIEW_RESULTS_START-->\n{json.dumps(results_data, indent=2)}\n<!--REVIEW_RESULTS_END-->"
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        from datetime import datetime
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id=round_id,
            delegated_pending=True,
            delegated_dispatch_time=datetime.now().isoformat(),
            delegated_blocked_once=False,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=DELEGATED_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False

            # Should either block with auto-continue or allow stop
            # (depends on auto_continue_count)
            state = read_state_file(state_path)
            if state is None:
                print("  FAIL: State file missing")
                return False
            if state.get("delegated_pending"):
                print(f"  FAIL: delegated_pending should be False after results processed")
                return False

            print("  PASS: Results processed and delegated state reset")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_delegated_timeout_fallback():
    """When coordinator times out, should fall back to inline agent mode."""
    print("  Setting up: delegated_pending=True with expired dispatch time...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        # Set dispatch time far in the past (600 seconds ago)
        from datetime import datetime, timedelta
        old_time = (datetime.now() - timedelta(seconds=600)).isoformat()
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id="abc12345",
            delegated_pending=True,
            delegated_dispatch_time=old_time,
            delegated_blocked_once=False,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=DELEGATED_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message (inline fallback), got empty stdout")
                return False

            output = json.loads(stdout)
            if output.get("decision") != "block":
                print(f"  FAIL: Expected 'block' decision for inline fallback, got: {output.get('decision')}")
                return False

            # The fallback should use inline agent instructions (not delegated)
            reason = output.get("reason", "")
            if "background coordinator" in reason.lower():
                print("  FAIL: Timeout fallback should use inline agent mode, not delegated")
                return False

            # State should have delegated_pending=False
            state = read_state_file(state_path)
            if state and state.get("delegated_pending"):
                print(f"  FAIL: delegated_pending should be False after timeout")
                return False

            print("  PASS: Timeout triggers fallback to inline agent mode")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_delegated_message_compact():
    """Delegated block message should be compact (under 25 lines)."""
    print("  Setting up: delegated mode, checking message size...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"
        instructions_path = HOOK_DIR / f"coordinator-instructions-{session_hash}.json"

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=DELEGATED_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message, got empty stdout")
                return False

            output = json.loads(stdout)
            reason = output.get("reason", "")
            line_count = len(reason.splitlines())

            if line_count > 25:
                print(f"  FAIL: Delegated message is {line_count} lines, expected <= 25")
                return False

            print(f"  PASS: Delegated message is compact ({line_count} lines)")
            return True
        finally:
            state_path.unlink(missing_ok=True)
            instructions_path.unlink(missing_ok=True)


def test_delegated_payload_completeness():
    """Coordinator payload should contain all required fields."""
    print("  Setting up: checking payload fields...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"
        instructions_path = HOOK_DIR / f"coordinator-instructions-{session_hash}.json"

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=DELEGATED_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not instructions_path.exists():
                print("  FAIL: Instructions file not created")
                return False

            payload = json.loads(instructions_path.read_text())
            required_keys = [
                "tier", "round_id", "diff_size", "total_file_count",
                "pending_agents", "agent_definitions", "file_list",
                "code_hunks", "violation_history", "results_schema",
            ]
            missing = [k for k in required_keys if k not in payload]
            if missing:
                print(f"  FAIL: Payload missing keys: {missing}")
                return False

            # Verify results_schema has fail_criteria
            if "fail_criteria" not in payload.get("results_schema", {}):
                print("  FAIL: results_schema missing fail_criteria")
                return False

            print("  PASS: Payload has all required fields")
            return True
        finally:
            state_path.unlink(missing_ok=True)
            instructions_path.unlink(missing_ok=True)


# =============================================================================
# Layer 4: Subagent Mode Tests
# =============================================================================

SUBAGENT_ENV = {"CLAUDE_HOOK_REVIEW_MODE": "subagent"}


def test_subagent_dispatches_agent():
    """Subagent mode should write payload file, set subagent_pending=True, block with spawn message."""
    print("  Setting up: subagent mode with enough diff for quick tier...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"
        instructions_path = HOOK_DIR / f"subagent-instructions-{session_hash}.json"

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=SUBAGENT_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message, got empty stdout")
                return False

            output = json.loads(stdout)
            if output.get("decision") != "block":
                print(f"  FAIL: Expected 'block' decision, got: {output.get('decision')}")
                return False
            if "background agent" not in output.get("reason", "").lower():
                print(f"  FAIL: Block message should mention 'background agent'")
                return False

            # Check state
            state = read_state_file(state_path)
            if state is None:
                print("  FAIL: State file missing")
                return False
            if not state.get("subagent_pending"):
                print(f"  FAIL: subagent_pending should be True, got: {state.get('subagent_pending')}")
                return False
            if not state.get("subagent_dispatch_time"):
                print("  FAIL: subagent_dispatch_time should be set")
                return False
            if state.get("subagent_blocked_once"):
                print("  FAIL: subagent_blocked_once should be False on dispatch")
                return False

            # Check instructions file
            if not instructions_path.exists():
                print("  FAIL: Subagent instructions file was not created")
                return False
            payload = json.loads(instructions_path.read_text())
            for key in ("tier", "round_id", "pending_agents", "agent_definitions", "code_hunks"):
                if key not in payload:
                    print(f"  FAIL: Payload missing key: {key}")
                    return False

            print("  PASS: Subagent mode dispatches correctly")
            return True
        finally:
            state_path.unlink(missing_ok=True)
            instructions_path.unlink(missing_ok=True)


def test_subagent_pending_blocks_once():
    """When subagent is pending and not blocked yet, should block once with in-progress message."""
    print("  Setting up: subagent_pending=True, blocked_once=False...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        from datetime import datetime
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id="abc12345",
            subagent_pending=True,
            subagent_dispatch_time=datetime.now().isoformat(),
            subagent_blocked_once=False,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=SUBAGENT_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message, got empty stdout")
                return False

            output = json.loads(stdout)
            if output.get("decision") != "block":
                print(f"  FAIL: Expected 'block' decision, got: {output.get('decision')}")
                return False
            reason = output.get("reason", "").lower()
            if "in progress" not in reason and "background review" not in reason:
                print(f"  FAIL: Message should mention 'in progress', got: {output.get('reason', '')[:100]}")
                return False

            state = read_state_file(state_path)
            if not state.get("subagent_blocked_once"):
                print("  FAIL: subagent_blocked_once should be True after first block")
                return False

            print("  PASS: Blocks once while waiting for subagent")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_subagent_pending_allows_second_stop():
    """When subagent is pending and already blocked once, should allow stop (no loop)."""
    print("  Setting up: subagent_pending=True, blocked_once=True...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        from datetime import datetime
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id="abc12345",
            subagent_pending=True,
            subagent_dispatch_time=datetime.now().isoformat(),
            subagent_blocked_once=True,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=SUBAGENT_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if stdout:
                print(f"  FAIL: Expected silent exit (allow stop), got: {stdout[:200]}")
                return False

            print("  PASS: Allows stop after blocking once (no loop)")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_subagent_results_pass():
    """When background agent returns results in transcript, should process and reset pending."""
    print("  Setting up: subagent_pending=True with passing results in transcript...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        round_id = "abc12345"
        results_data = {
            "round_id": round_id,
            "agents": {
                "explore_haiku": {"status": "pass", "issues": []},
            },
            "outcome": "pass",
        }
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")
            # Background agent's return value in transcript (inline markers)
            f.write(json.dumps({
                "type": "assistant",
                "content": f"<!--REVIEW_RESULTS_START-->\n{json.dumps(results_data, indent=2)}\n<!--REVIEW_RESULTS_END-->"
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        from datetime import datetime
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id=round_id,
            subagent_pending=True,
            subagent_dispatch_time=datetime.now().isoformat(),
            subagent_blocked_once=False,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=SUBAGENT_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False

            # Should have processed results
            state = read_state_file(state_path)
            if state is None:
                print("  FAIL: State file missing")
                return False
            if state.get("subagent_pending"):
                print("  FAIL: subagent_pending should be False after results processed")
                return False

            print("  PASS: Subagent results processed from transcript")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_subagent_timeout_fallback():
    """When subagent times out, should fall back to inline agent mode."""
    print("  Setting up: subagent_pending=True with expired dispatch time...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        from datetime import datetime, timedelta
        old_time = (datetime.now() - timedelta(seconds=600)).isoformat()
        create_mock_state(
            state_path, str(transcript),
            tier="quick",
            round_id="abc12345",
            subagent_pending=True,
            subagent_dispatch_time=old_time,
            subagent_blocked_once=False,
            last_total_diff=0,
            last_files_seen=[],
        )

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=SUBAGENT_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message (inline fallback), got empty stdout")
                return False

            output = json.loads(stdout)
            if output.get("decision") != "block":
                print(f"  FAIL: Expected 'block' decision for inline fallback, got: {output.get('decision')}")
                return False

            # The fallback should use inline agent instructions (not subagent dispatch)
            reason = output.get("reason", "")
            if "background agent" in reason.lower() and "spawn" in reason.lower():
                print("  FAIL: Timeout fallback should use inline agent mode, not subagent dispatch")
                return False

            state = read_state_file(state_path)
            if state and state.get("subagent_pending"):
                print("  FAIL: subagent_pending should be False after timeout")
                return False

            print("  PASS: Timeout triggers fallback to inline agent mode")
            return True
        finally:
            state_path.unlink(missing_ok=True)


def test_subagent_message_compact():
    """Subagent dispatch message should be very compact (under 8 lines)."""
    print("  Setting up: subagent mode, checking message size...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"
        instructions_path = HOOK_DIR / f"subagent-instructions-{session_hash}.json"

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=SUBAGENT_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not stdout:
                print("  FAIL: Expected block message, got empty stdout")
                return False

            output = json.loads(stdout)
            reason = output.get("reason", "")
            line_count = len(reason.splitlines())

            if line_count > 8:
                print(f"  FAIL: Subagent message is {line_count} lines, expected <= 8")
                return False

            print(f"  PASS: Subagent message is compact ({line_count} lines)")
            return True
        finally:
            state_path.unlink(missing_ok=True)
            instructions_path.unlink(missing_ok=True)


def test_subagent_payload_has_autonomous_fields():
    """Subagent payload should have deep_auto_fix, background_agent_instructions, session_hash."""
    print("  Setting up: checking subagent-specific payload fields...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript = tmpdir / "transcript.jsonl"
        big_new = "x" * 600
        with open(transcript, "w") as f:
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_new}
            }) + "\n")

        session_hash = get_session_hash(str(transcript))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"
        instructions_path = HOOK_DIR / f"subagent-instructions-{session_hash}.json"

        try:
            exit_code, stdout, stderr = run_hook(transcript, env_overrides=SUBAGENT_ENV)

            if exit_code != 0:
                print(f"  FAIL: Exit code {exit_code}, expected 0")
                return False
            if not instructions_path.exists():
                print("  FAIL: Instructions file not created")
                return False

            payload = json.loads(instructions_path.read_text())

            # Subagent-specific fields
            subagent_keys = [
                "session_hash", "deep_auto_fix",
                "autonomous_deep_failure", "background_agent_instructions",
            ]
            missing = [k for k in subagent_keys if k not in payload]
            if missing:
                print(f"  FAIL: Payload missing subagent-specific keys: {missing}")
                return False

            # Verify background_agent_instructions is substantial
            instructions = payload.get("background_agent_instructions", "")
            if len(instructions) < 100:
                print(f"  FAIL: background_agent_instructions too short ({len(instructions)} chars)")
                return False

            # Also verify coordinator fields are present
            coordinator_keys = ["tier", "round_id", "pending_agents", "agent_definitions", "code_hunks"]
            missing = [k for k in coordinator_keys if k not in payload]
            if missing:
                print(f"  FAIL: Payload missing coordinator keys: {missing}")
                return False

            print("  PASS: Payload has all subagent-specific and coordinator fields")
            return True
        finally:
            state_path.unlink(missing_ok=True)
            instructions_path.unlink(missing_ok=True)


# =============================================================================
# Runner
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("Exit Path Audit & Tests")
    print("=" * 60)

    results = []

    # Layer 1: Static audit
    print("\n--- Layer 1: Static Audit ---")
    for test_fn in [
        test_no_raw_sys_exit,
        test_no_print_before_allow_stop,
        test_block_with_message_count,
        test_allow_stop_count,
    ]:
        print(f"\n{test_fn.__doc__}")
        results.append((test_fn.__name__, test_fn()))

    # Layer 2: Behavioral tests
    print("\n--- Layer 2: Behavioral Tests ---")
    for test_fn in [
        test_no_code_modified_silent_exit,
        test_completed_flag_resets_on_allow,
        test_skip_tier_max_continues_saves_state,
        test_deep_completed_silent_exit,
    ]:
        print(f"\n{test_fn.__doc__}")
        results.append((test_fn.__name__, test_fn()))

    # Layer 3: Delegated mode tests
    print("\n--- Layer 3: Delegated Mode Tests ---")
    for test_fn in [
        test_delegated_dispatches_coordinator,
        test_delegated_pending_blocks_once,
        test_delegated_pending_allows_second_stop,
        test_delegated_pending_with_results,
        test_delegated_timeout_fallback,
        test_delegated_message_compact,
        test_delegated_payload_completeness,
    ]:
        print(f"\n{test_fn.__doc__}")
        results.append((test_fn.__name__, test_fn()))

    # Layer 4: Subagent mode tests
    print("\n--- Layer 4: Subagent Mode Tests ---")
    for test_fn in [
        test_subagent_dispatches_agent,
        test_subagent_pending_blocks_once,
        test_subagent_pending_allows_second_stop,
        test_subagent_results_pass,
        test_subagent_timeout_fallback,
        test_subagent_message_compact,
        test_subagent_payload_has_autonomous_fields,
    ]:
        print(f"\n{test_fn.__doc__}")
        results.append((test_fn.__name__, test_fn()))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, r in results if r)
    total = len(results)
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"  {status}: {name}")
    print(f"\nTotal: {passed}/{total} tests passed")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
