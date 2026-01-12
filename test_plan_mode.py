#!/usr/bin/env python3
"""
Test script for Plan Subagent feature.

This creates mock state and transcript files to verify the hook correctly
returns plan agent instructions when planning_state="plan_needed".
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Get the directory containing the hook
HOOK_DIR = Path(__file__).parent
HOOK_SCRIPT = HOOK_DIR / "stop-design-audit.py"


def create_mock_transcript(transcript_path: Path, tools: list[str], include_edits: bool = True):
    """Create a mock JSONL transcript with specified tool calls."""
    entries = []

    # Add some Edit operations if requested (to trigger code_modified check)
    if include_edits:
        entries.append({
            "type": "tool_use",
            "name": "Edit",
            "input": {
                "file_path": "/test/file.py",
                "old_string": "old code",
                "new_string": "new code with changes"
            }
        })

    # Add the specified tools
    for tool in tools:
        entries.append({
            "type": "tool_use",
            "name": tool,
            "input": {}
        })

    # Write as JSONL
    with open(transcript_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def create_mock_state(
    state_path: Path,
    transcript_path: Path,
    planning_state: str = "none",
    plan_round_id: str = "",
):
    """Create a mock state file with planning_state.

    Args:
        state_path: Path to write the state file.
        transcript_path: Path to the transcript (used as session_id).
        planning_state: "none" or "plan_needed".
        plan_round_id: UUID for the planning round.
    """
    state = {
        "session_id": str(transcript_path),
        "last_total_diff": 100,
        "last_files_seen": ["/test/file.py"],
        "tier": "deep",
        "auto_continue_count": 0,
        "fail_count": 1,
        "round_id": "test1234",
        "passed_agents": [],
        "completed": False,
        "violation_history": {},
        "planning_state": planning_state,
        "plan_round_id": plan_round_id,
    }

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def create_mock_review_results(results_path: Path, round_id: str):
    """Create mock review results with failures.

    Args:
        results_path: Path to write the results file.
        round_id: Round ID for the results.
    """
    results = {
        "round_id": round_id,
        "agents": {
            "bug_hunter": {
                "status": "fail",
                "issues": [
                    {
                        "severity": "critical",
                        "file": "/test/file.py",
                        "description": "SQL injection vulnerability",
                        "category": "security"
                    },
                    {
                        "severity": "high",
                        "file": "/test/file.py",
                        "description": "Missing input validation",
                        "category": "validation"
                    }
                ]
            }
        }
    }

    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)


def run_hook(transcript_path: Path) -> tuple[int, str, str]:
    """Run the hook with the given transcript path and return (exit_code, stdout, stderr)."""
    input_json = json.dumps({"transcript_path": str(transcript_path)})

    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=input_json,
        capture_output=True,
        text=True,
        cwd=str(HOOK_DIR)
    )

    return result.returncode, result.stdout, result.stderr


def test_plan_needed_returns_instructions():
    """Test that planning_state='plan_needed' returns Plan subagent instructions."""
    print("=" * 60)
    print("TEST: Plan Needed Returns Plan Subagent Instructions")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript_path = tmpdir / "mock_transcript.jsonl"

        import hashlib
        session_hash = hashlib.md5(str(transcript_path).encode()).hexdigest()[:12]
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"
        results_path = HOOK_DIR / f"review-results-{session_hash}.json"

        try:
            print("\n1. Creating mock transcript with edits...")
            create_mock_transcript(transcript_path, [])

            print("2. Creating mock state with planning_state='plan_needed'...")
            create_mock_state(
                state_path,
                transcript_path,
                planning_state="plan_needed",
                plan_round_id="planround1"
            )

            print("3. Creating mock review results (for plan agent context)...")
            create_mock_review_results(results_path, "test1234")

            print("4. Running hook...")
            exit_code, stdout, stderr = run_hook(transcript_path)

            print(f"\n   Exit code: {exit_code}")
            print(f"   Stdout (first 500 chars): {stdout[:500] if stdout else '(empty)'}")
            if stderr:
                print(f"   Stderr: {stderr[:200]}")

            print("\n5. Checking results...")

            if exit_code != 0:
                print("   FAIL: Exit code should be 0")
                return False

            if not stdout:
                print("   FAIL: Expected JSON output with plan agent instructions")
                return False

            try:
                output = json.loads(stdout)
                reason = output.get("reason", "")

                # Check for key elements
                checks_passed = True

                if output.get("decision") != "block":
                    print("   FAIL: Decision should be 'block'")
                    checks_passed = False

                if "PLAN REQUIRED" not in reason:
                    print("   FAIL: Missing 'PLAN REQUIRED' in reason")
                    checks_passed = False

                if "subagent_type='Plan'" not in reason:
                    print("   FAIL: Missing subagent_type='Plan' in instructions")
                    checks_passed = False
                else:
                    print("   PASS: Found subagent_type='Plan'")

                # Verify NO file write instructions
                if "Write tool" in reason or "write results to" in reason.lower()[:200]:
                    # The results instruction is OK, but plan file write is not
                    pass
                if "remediation-plan-" in reason:
                    print("   FAIL: Still references remediation-plan file (should not)")
                    checks_passed = False
                else:
                    print("   PASS: No remediation-plan file reference")

                if "Do NOT write any files" in reason:
                    print("   PASS: Includes 'Do NOT write any files' instruction")
                else:
                    print("   WARN: Missing explicit 'Do NOT write any files' instruction")

                if checks_passed:
                    print("   PASS: All checks passed!")
                    return True
                else:
                    print(f"   FAIL: Some checks failed. Full output:\n{reason[:1000]}")
                    return False

            except json.JSONDecodeError:
                print(f"   FAIL: Could not parse JSON output: {stdout}")
                return False

        finally:
            if state_path.exists():
                state_path.unlink()
            if results_path.exists():
                results_path.unlink()


if __name__ == "__main__":
    print("\nTesting Plan Subagent Feature")
    print("=" * 60)

    results = []

    # Run tests
    results.append(("Plan Needed Returns Plan Subagent Instructions", test_plan_needed_returns_instructions()))

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
