"""Review results parsing, validation, and JSON repair."""

from __future__ import annotations

import json
import re
import time

from stop_design_audit.config import (
    RESULTS_MODE,
    RETRY_BACKOFF_FACTOR,
    RETRY_INITIAL_DELAY,
    STATUS_FAIL,
    STATUS_PASS,
    get_results_file,
)
from stop_design_audit.exit_helpers import log

# Markers for inline results in transcript
RESULTS_START_MARKER = "<!--REVIEW_RESULTS_START-->"
RESULTS_END_MARKER = "<!--REVIEW_RESULTS_END-->"


def validate_agent_result(agent_id: str, data: dict) -> bool:
    """Validate that an agent result has the expected schema."""
    if not isinstance(data, dict):
        log(f"Agent {agent_id}: result is not a dict")
        return False
    status = data.get("status")
    if status not in (STATUS_PASS, STATUS_FAIL):
        log(f"Agent {agent_id}: invalid status '{status}' (expected 'pass' or 'fail')")
        return False
    issues = data.get("issues")
    if not isinstance(issues, list):
        log(f"Agent {agent_id}: issues is not a list")
        return False
    return True


def _validate_results_data(data: dict) -> dict | None:
    """Validate and clean results data structure."""
    if not isinstance(data, dict):
        log(f"Invalid results: expected dict, got {type(data).__name__}")
        return None
    if "round_id" not in data:
        log("Invalid results: missing round_id")
        return None
    if "agents" in data:
        if not isinstance(data["agents"], dict):
            log(
                f"Invalid results: agents should be dict, got {type(data['agents']).__name__}"
            )
            return None
        valid_agents = {}
        for agent_id, agent_data in data["agents"].items():
            if validate_agent_result(agent_id, agent_data):
                valid_agents[agent_id] = agent_data
            else:
                log(f"Skipping invalid agent result: {agent_id}")
        data["agents"] = valid_agents
    return data


def _repair_json(json_str: str) -> str:
    """Attempt to repair common JSON errors."""
    s = json_str.strip()

    # Strip markdown code fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = s.strip()

    # Remove trailing commas
    s = re.sub(r",(\s*[}\]])", r"\1", s)

    # Replace single quotes with double quotes for keys/values
    s = re.sub(r"(?<=[{,\[])\s*'([^']+)'\s*(?=:)", r'"\1"', s)
    s = re.sub(r"(?<=:)\s*'([^']*)'\s*(?=[,}\]])", r'"\1"', s)

    # Collapse newlines
    s = re.sub(r"\s*\n\s*", " ", s)

    # Balance braces
    open_braces = s.count("{") - s.count("}")
    open_brackets = s.count("[") - s.count("]")
    if open_braces > 0:
        s = s + "}" * open_braces
    if open_brackets > 0:
        s = s + "]" * open_brackets

    return s


def _try_parse_json(json_str: str) -> dict | None:
    """Try to parse a JSON string, with repair fallback."""
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        log("Attempting JSON repair...")
        repaired = _repair_json(json_str)
        try:
            data = json.loads(repaired)
            log("JSON repair successful")
            return data
        except json.JSONDecodeError as e:
            log(f"JSON repair failed: {e}")
            log(f"Original JSON (first 500 chars): {json_str[:500]}")
            return None


def _fallback_extract_results(full_text: str) -> dict | None:
    """Fallback: scan transcript text for a JSON blob with round_id and agents."""
    candidates = []
    for match in re.finditer(r'\{[^{}]*"round_id"[^{}]*"agents"\s*:\s*\{', full_text):
        start = match.start()
        depth = 0
        end = start
        for i in range(start, min(start + 5000, len(full_text))):
            if full_text[i] == "{":
                depth += 1
            elif full_text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            candidates.append(full_text[start:end])

    for candidate in reversed(candidates):
        data = _try_parse_json(candidate)
        if data is not None and isinstance(data.get("agents"), dict):
            log("Fallback extraction found results without markers")
            return _validate_results_data(data)

    return None


def _extract_results_from_transcript(transcript_path: str) -> dict | None:
    """Extract review results from transcript using markers (inline mode)."""
    all_text_content = []

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Handle both transcript formats:
                # 1. Claude Code format: {"message": {"content": [{"type": "text", "text": "..."}]}}
                # 2. Simplified format: {"type": "assistant", "content": "..."}
                content_list = event.get("message", {}).get("content", [])
                if isinstance(content_list, list):
                    for item in content_list:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text = item.get("text", "")
                            if text:
                                all_text_content.append(text)
                # Direct content string (simplified format)
                direct_content = event.get("content")
                if isinstance(direct_content, str) and direct_content:
                    all_text_content.append(direct_content)
    except Exception as e:
        log(f"Error reading transcript: {e}")
        return None

    if not all_text_content:
        log("No text content found in transcript")
        return None

    full_text = "\n".join(all_text_content)

    start_idx = full_text.rfind(RESULTS_START_MARKER)
    if start_idx != -1:
        end_idx = full_text.find(RESULTS_END_MARKER, start_idx)
        if end_idx != -1:
            json_str = full_text[
                start_idx + len(RESULTS_START_MARKER) : end_idx
            ].strip()
            data = _try_parse_json(json_str)
            if data is not None:
                return _validate_results_data(data)
            log("Failed to parse JSON between markers")
        else:
            log("End marker not found after start marker")
    else:
        log("Results markers not found in transcript text content")

    return _fallback_extract_results(full_text)


def _read_results_from_file(session_hash: str) -> dict | None:
    """Read review results from JSON file (file mode)."""
    results_file = get_results_file(session_hash)
    if not results_file.exists():
        return None
    try:
        content = results_file.read_text()
        if not content.strip():
            return None
        data = json.loads(content)
        return _validate_results_data(data)
    except json.JSONDecodeError as e:
        log(f"JSON decode error reading results file (possible race): {e}")
        return None
    except Exception as e:
        log(f"Error reading results file: {e}")
        return None


def read_review_results(
    transcript_path: str, session_hash: str, *, mode: str | None = None
) -> dict:
    """Read review results with retry logic (mode-aware).

    Retries up to 3 times with exponential backoff.
    Returns {} if all attempts fail.
    """
    effective_mode = mode or RESULTS_MODE
    for attempt in range(3):
        if effective_mode == "file":
            result = _read_results_from_file(session_hash)
        else:
            result = _extract_results_from_transcript(transcript_path)

        if result is not None:
            return result
        if attempt < 2:
            sleep_time = RETRY_INITIAL_DELAY * (RETRY_BACKOFF_FACTOR**attempt)
            log(
                f"Results read failed (mode={effective_mode}), retry {attempt + 1}/3 in {sleep_time}s"
            )
            time.sleep(sleep_time)
    return {}
