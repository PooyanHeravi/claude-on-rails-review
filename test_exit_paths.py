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

# These constants must match the hook — import them at test time
MAX_AUTO_CONTINUES = 3
MAX_FAIL_RETRIES = 3


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


def run_hook(transcript_path: Path) -> tuple[int, str, str]:
    """Run the hook and return (exit_code, stdout, stderr)."""
    input_json = json.dumps({"transcript_path": str(transcript_path)})
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=input_json,
        capture_output=True,
        text=True,
        cwd=str(HOOK_DIR),
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
    """Verify no raw sys.exit(0) outside helper functions and __main__ guard."""
    source = HOOK_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    violations = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match sys.exit(0)
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "exit"
                and isinstance(func.value, ast.Name) and func.value.id == "sys"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0):
            continue

        line = node.lineno

        # Check if inside allow_stop or block_with_message (allowed)
        # We detect this by checking if the line is within a known helper function
        source_lines = source.splitlines()
        # Walk up to find containing function
        in_helper = False
        in_main_guard = False
        for parent_node in ast.walk(tree):
            if isinstance(parent_node, ast.FunctionDef) and parent_node.name in ("allow_stop", "block_with_message"):
                if parent_node.lineno <= line <= parent_node.end_lineno:
                    in_helper = True
                    break

        # Check if in __main__ guard's except block (the one raw exception allowed)
        for parent_node in ast.walk(tree):
            if isinstance(parent_node, ast.If):
                # Look for if __name__ == "__main__"
                test = parent_node.test
                if (isinstance(test, ast.Compare)
                        and isinstance(test.left, ast.Name) and test.left.id == "__name__"
                        and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value == "__main__"):
                    if parent_node.lineno <= line <= parent_node.end_lineno:
                        in_main_guard = True
                        break

        if not in_helper and not in_main_guard:
            violations.append(line)

    if violations:
        print(f"  FAIL: Found raw sys.exit(0) at lines: {violations}")
        print("        All exits must use allow_stop() or block_with_message()")
        return False

    print("  PASS: No raw sys.exit(0) outside helpers")
    return True


def test_no_print_before_allow_stop():
    """Verify no print(json.dumps(...)) immediately before allow_stop() — the exact bug pattern."""
    source = HOOK_SCRIPT.read_text(encoding="utf-8")
    lines = source.splitlines()

    violations = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("allow_stop("):
            # Check previous non-empty lines for print(json.dumps
            for j in range(i - 1, max(i - 6, -1), -1):
                prev = lines[j].strip()
                if not prev or prev.startswith("#"):
                    continue
                if "print(json.dumps(" in prev or '"decision"' in prev:
                    violations.append((i + 1, j + 1))
                break  # Only check the immediately preceding statement

    if violations:
        for allow_line, print_line in violations:
            print(f"  FAIL: print+allow_stop combo at lines {print_line}-{allow_line} (will loop!)")
        return False

    print("  PASS: No print(json.dumps) before allow_stop()")
    return True


def test_block_with_message_count():
    """Verify the number of block_with_message() calls matches expected count from registry."""
    source = HOOK_SCRIPT.read_text(encoding="utf-8")

    # Count block_with_message calls (excluding definitions, comments, docstrings)
    block_calls = []
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
            continue
        if "block_with_message(" in stripped and "def block_with_message" not in stripped:
            block_calls.append(i)

    # Count expected blocks from registry
    # Import registry from source
    registry_match = re.search(r"EXIT_PATH_REGISTRY\s*=\s*\{([^}]+)\}", source, re.DOTALL)
    if not registry_match:
        print("  FAIL: Could not find EXIT_PATH_REGISTRY in source")
        return False

    expected_blocks = source.count('"type": "block"')

    if len(block_calls) != expected_blocks:
        print(f"  FAIL: Found {len(block_calls)} block_with_message() calls but registry has {expected_blocks} 'block' entries")
        print(f"        block_with_message at lines: {block_calls}")
        return False

    print(f"  PASS: {len(block_calls)} block_with_message() calls match {expected_blocks} registry 'block' entries")
    return True


def test_allow_stop_count():
    """Verify the number of allow_stop() calls matches expected count from registry."""
    source = HOOK_SCRIPT.read_text(encoding="utf-8")

    allow_calls = []
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
            continue
        if "allow_stop(" in stripped and "def allow_stop" not in stripped:
            allow_calls.append(i)

    expected_allows = source.count('"type": "allow"')

    # Note: fatal_error uses raw sys.exit(0) so we expect one fewer allow_stop than registry allows
    expected_allow_stop_calls = expected_allows - 1  # minus fatal_error

    if len(allow_calls) != expected_allow_stop_calls:
        print(f"  FAIL: Found {len(allow_calls)} allow_stop() calls but expected {expected_allow_stop_calls} (registry has {expected_allows} 'allow' entries, minus 1 for fatal_error)")
        print(f"        allow_stop at lines: {allow_calls}")
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
