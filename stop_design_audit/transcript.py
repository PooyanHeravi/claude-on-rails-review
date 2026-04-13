"""Transcript parsing — extract diffs, tool uses, hunks, and context."""

from __future__ import annotations

import json
from pathlib import Path

from stop_design_audit.config import (
    EXCLUDED_EXTENSIONS,
    EXCLUDED_FILENAMES,
    EXCLUDED_PATHS,
)
from stop_design_audit.exit_helpers import log


def normalize_path(path: str) -> str:
    """Normalize path separators to forward slashes for cross-platform consistency."""
    return path.replace("\\", "/")


def _should_exclude_file(file_path: str) -> bool:
    """Check if file should be excluded from review."""
    path = Path(file_path)

    if path.name in EXCLUDED_FILENAMES:
        return True

    suffix = path.suffix.lower()
    if suffix in EXCLUDED_EXTENSIONS:
        return True

    normalized = normalize_path(file_path.lower())
    for pattern in EXCLUDED_PATHS:
        if pattern in normalized:
            return True

    return False


def _calculate_edit_diff(tool_input: dict) -> tuple[int, str | None]:
    """Calculate absolute character change for an Edit tool call.

    Uses max(len(old), len(new)) so that replacing 500 chars with 500 different
    chars registers as a 500-char change, not 0.
    """
    file_path = tool_input.get("file_path", "")
    if not file_path or _should_exclude_file(file_path):
        return 0, None
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    return max(len(old_string), len(new_string)), file_path


def _calculate_write_diff(tool_input: dict) -> tuple[int, str | None]:
    """Calculate character count for a Write tool call."""
    file_path = tool_input.get("file_path", "")
    if not file_path or _should_exclude_file(file_path):
        return 0, None
    content = tool_input.get("content", "")
    return len(content), file_path


def _extract_tool_uses(event: dict) -> list[dict]:
    """Extract tool_use objects from a transcript event.

    Handles two formats:
    1. Direct: {"type": "tool_use", "name": "Edit", "input": {...}}
    2. Nested: {"message": {"content": [{"type": "tool_use", ...}]}}
    """
    tool_uses = []

    if event.get("type") == "tool_use":
        tool_uses.append(event)

    content_list = event.get("message", {}).get("content", [])
    if isinstance(content_list, list):
        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_uses.append(item)

    return tool_uses


def parse_transcript_total(transcript_path: str) -> dict:
    """Parse entire transcript to calculate TOTAL cumulative diff.

    Returns:
        {"tools": set, "total_diff_chars": int, "files_modified": set,
         "edit_count": int, "write_count": int}
    """
    result: dict = {
        "tools": set(),
        "total_diff_chars": 0,
        "files_modified": set(),
        "edit_count": 0,
        "write_count": 0,
    }

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                tool_uses = _extract_tool_uses(event)
                for tool_data in tool_uses:
                    tool_name = tool_data.get("name") or tool_data.get("tool_name")
                    if tool_name:
                        result["tools"].add(tool_name)

                    tool_input = tool_data.get("input", {})
                    if not isinstance(tool_input, dict):
                        continue

                    if tool_name == "Edit":
                        chars, file_path = _calculate_edit_diff(tool_input)
                        result["total_diff_chars"] += chars
                        if file_path:
                            result["files_modified"].add(file_path)
                            result["edit_count"] += 1

                    elif tool_name == "Write":
                        chars, file_path = _calculate_write_diff(tool_input)
                        result["total_diff_chars"] += chars
                        if file_path:
                            result["files_modified"].add(file_path)
                            result["write_count"] += 1

    except FileNotFoundError:
        log(f"Transcript file not found: {transcript_path}")
    except PermissionError:
        log(f"Permission denied reading transcript: {transcript_path}")
    except Exception as e:
        log(f"Error parsing transcript: {e}")

    return result


def get_last_tool_used(transcript_path: str) -> str | None:
    """Get the name of the last tool used in the transcript."""
    last_tool = None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tool_uses = _extract_tool_uses(event)
                for tool_data in tool_uses:
                    tool_name = tool_data.get("name") or tool_data.get("tool_name")
                    if tool_name:
                        last_tool = tool_name
    except Exception as e:
        log(f"Error getting last tool: {e}")
    return last_tool


def extract_pre_review_context(transcript_path: str) -> str:
    """Extract what Claude was working on before this review fired.

    Returns a formatted context string or empty string.
    """
    last_user_message = ""
    recent_actions: list[str] = []

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                role = event.get("role") or event.get("message", {}).get("role")
                if role == "user":
                    content = event.get("content") or event.get("message", {}).get(
                        "content", []
                    )
                    if isinstance(content, str):
                        last_user_message = content
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item.get("text", "")
                                if text:
                                    last_user_message = text

                if (
                    role == "assistant"
                    or event.get("message", {}).get("role") == "assistant"
                ):
                    tool_uses = _extract_tool_uses(event)
                    for tool_data in tool_uses:
                        tool_name = tool_data.get("name") or tool_data.get("tool_name")
                        if not tool_name:
                            continue
                        tool_input = tool_data.get("input", {})
                        file_path = ""
                        if isinstance(tool_input, dict):
                            file_path = tool_input.get("file_path", "")
                        if file_path:
                            recent_actions.append(f"{tool_name}({file_path})")
                        else:
                            recent_actions.append(tool_name)

    except Exception as e:
        log(f"Error extracting pre-review context: {e}")
        return ""

    if not last_user_message and not recent_actions:
        return ""

    parts = []
    if last_user_message:
        if len(last_user_message) > 200:
            last_user_message = last_user_message[:200] + "..."
        parts.append(f"- User request: {last_user_message}")

    if recent_actions:
        for action in recent_actions[-5:]:
            parts.append(f"- {action}")

    if not parts:
        return ""

    return (
        "\n\nBefore this review, you were working on:\n"
        + "\n".join(parts)
        + "\n\nResume that task."
    )


def extract_changed_hunks(
    transcript_path: str,
    start_position: int,
    files: set[str],
    max_preview_chars: int = 500,
) -> dict[str, str]:
    """Extract actual code changes from Edit/Write tool calls.

    Returns: {file_path: "preview of changes..."}
    """
    hunks: dict[str, str] = {}

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            f.seek(start_position)

            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                tool_uses = _extract_tool_uses(event)
                for tool_data in tool_uses:
                    tool_name = tool_data.get("name") or tool_data.get("tool_name")
                    tool_input = tool_data.get("input", {})
                    if not isinstance(tool_input, dict):
                        continue

                    file_path = tool_input.get("file_path", "")
                    if file_path not in files:
                        continue

                    if tool_name == "Edit":
                        old_str = tool_input.get("old_string", "")
                        new_str = tool_input.get("new_string", "")
                        old_preview = (
                            old_str[:100] + "..." if len(old_str) > 100 else old_str
                        )
                        new_preview = (
                            new_str[:100] + "..." if len(new_str) > 100 else new_str
                        )
                        hunk = f"- Old: {old_preview}\n+ New: {new_preview}"
                        if file_path in hunks:
                            hunks[file_path] += f"\n\n{hunk}"
                        else:
                            hunks[file_path] = hunk

                    elif tool_name == "Write":
                        content = tool_input.get("content", "")
                        preview = content[:max_preview_chars]
                        if len(content) > max_preview_chars:
                            preview += "..."
                        hunks[file_path] = f"+ New file/overwrite:\n{preview}"

    except Exception as e:
        log(f"Error extracting hunks: {e}")

    for file_path in hunks:
        if len(hunks[file_path]) > max_preview_chars:
            hunks[file_path] = (
                hunks[file_path][:max_preview_chars] + "\n... (truncated)"
            )

    return hunks
