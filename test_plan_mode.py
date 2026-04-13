#!/usr/bin/env python3
"""
Test script for Plan Subagent feature.

Verifies that when a deep review fails with DEEP_AUTO_FIX=none,
the hook returns plan agent instructions.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_DIR = Path(__file__).parent
HOOK_SCRIPT = HOOK_DIR / "stop-design-audit.py"


def get_session_hash(transcript_path: str) -> str:
    return hashlib.md5(transcript_path.encode()).hexdigest()[:12]


def run_hook(transcript_path: Path, env_overrides: dict | None = None) -> tuple[int, str, str]:
    """Run the hook and return (exit_code, stdout, stderr)."""
    import os
    env = os.environ.copy()
    # Force agent mode + DEEP_AUTO_FIX=none so inline plan agent path is triggered
    env["CLAUDE_HOOK_REVIEW_MODE"] = "agent"
    env["CLAUDE_HOOK_DEEP_AUTO_FIX"] = "none"
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


def test_deep_failure_returns_plan_instructions():
    """Test that deep review failure with DEEP_AUTO_FIX=none returns Plan subagent instructions."""
    print("=" * 60)
    print("TEST: Deep Failure Returns Plan Subagent Instructions")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        transcript_path = tmpdir / "mock_transcript.jsonl"
        round_id = "deepfail1"

        # Create transcript with large diff (to reach deep tier) and failed results
        big_content = "x" * 25000
        results_data = {
            "round_id": round_id,
            "agents": {
                "bug_hunter": {
                    "status": "fail",
                    "issues": [
                        {
                            "severity": "critical",
                            "file": "/test/file.py",
                            "line": 42,
                            "description": "SQL injection vulnerability",
                            "category": "security",
                        }
                    ],
                }
            },
        }

        with open(transcript_path, "w") as f:
            # Edit with large diff
            f.write(json.dumps({
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/test/file.py", "old_string": "old", "new_string": big_content},
            }) + "\n")
            # Failed review results in transcript
            f.write(json.dumps({
                "type": "assistant",
                "content": f"<!--REVIEW_RESULTS_START-->\n{json.dumps(results_data, indent=2)}\n<!--REVIEW_RESULTS_END-->",
            }) + "\n")

        session_hash = get_session_hash(str(transcript_path))
        state_path = HOOK_DIR / f"stop-hook-state-{session_hash}.json"

        # State: deep review in progress with matching round_id
        state = {
            "session_id": str(transcript_path),
            "last_total_diff": 0,
            "last_files_seen": [],
            "tier": "deep",
            "auto_continue_count": 0,
            "fail_count": 0,
            "round_id": round_id,
            "passed_agents": [],
            "completed": False,
            "violation_history": {},
            "review_attempts": 1,
        }
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

        try:
            print("\n1. Running hook with deep tier + failed results + DEEP_AUTO_FIX=none...")
            exit_code, stdout, stderr = run_hook(transcript_path)

            print(f"\n   Exit code: {exit_code}")
            print(f"   Stdout (first 500 chars): {stdout[:500] if stdout else '(empty)'}")

            print("\n2. Checking results...")

            if exit_code != 0:
                print("   FAIL: Exit code should be 0")
                return False

            if not stdout:
                print("   FAIL: Expected JSON output with plan agent instructions")
                return False

            try:
                output = json.loads(stdout)
                reason = output.get("reason", "")

                checks_passed = True

                if output.get("decision") != "block":
                    print("   FAIL: Decision should be 'block'")
                    checks_passed = False

                if "DEEP REVIEW FAILED" not in reason:
                    print(f"   FAIL: Missing 'DEEP REVIEW FAILED' in reason")
                    checks_passed = False
                else:
                    print("   PASS: Found 'DEEP REVIEW FAILED'")

                if "Plan" in reason or "plan" in reason:
                    print("   PASS: Contains plan-related instructions")
                else:
                    print("   FAIL: Missing plan-related instructions")
                    checks_passed = False

                if "remediation-plan-" in reason:
                    print("   FAIL: Still references remediation-plan file (should not)")
                    checks_passed = False
                else:
                    print("   PASS: No remediation-plan file reference")

                if checks_passed:
                    print("   PASS: All checks passed!")
                    return True
                else:
                    print(f"   FAIL: Some checks failed. Full output:\n{reason[:1500]}")
                    return False

            except json.JSONDecodeError:
                print(f"   FAIL: Could not parse JSON output: {stdout[:300]}")
                return False

        finally:
            state_path.unlink(missing_ok=True)


if __name__ == "__main__":
    print("\nTesting Plan Subagent Feature")
    print("=" * 60)

    results = []
    results.append(("Deep Failure Returns Plan Instructions", test_deep_failure_returns_plan_instructions()))

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
