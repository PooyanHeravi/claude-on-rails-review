"""API mode — direct Anthropic API review (fallback)."""

from __future__ import annotations

import json

from stop_design_audit.config import MAX_TRANSCRIPT_CHARS_FOR_API
from stop_design_audit.exit_helpers import log


def call_anthropic_review(transcript_path: str, use_sonnet: bool) -> dict:
    """Call Anthropic API to review the conversation for violations."""
    try:
        import anthropic
    except ImportError:
        log("anthropic package not installed")
        return {"violations": []}

    transcript_content = ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_content = f.read()
    except Exception as e:
        log(f"Error reading transcript for review: {e}")
        return {"violations": []}

    prompt = """You are a code reviewer for a software project.
Review the conversation transcript for design principle violations in any code that was written or modified.

## CRITICAL VIOLATIONS (must report):
1. Security Issues - SQL injection, XSS, hardcoded secrets, unsafe data handling
2. Silent Failures - return None/[] without exception, swallowed errors
3. Hardcoding - absolute paths, magic numbers without constants
4. Missing Error Handling - uncaught exceptions, missing validation
5. Code Smells - duplicated logic, overly complex functions, tight coupling

## Transcript (last 50000 chars):
{transcript}

## Response (JSON only, no other text):
If violations found: {{"violations": ["- [file:line] Description", ...]}}
If clean: {{"violations": []}}"""

    model = "claude-sonnet-4-20250514" if use_sonnet else "claude-3-5-haiku-20241022"
    log(f"Calling {model} for review...")

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": prompt.format(
                        transcript=transcript_content[-MAX_TRANSCRIPT_CHARS_FOR_API:]
                    ),
                }
            ],
        )
        if not response.content:
            log("API returned empty content")
            return {"violations": []}
        result_text = response.content[0].text
        log(f"LLM response: {result_text[:200]}...")
        return json.loads(result_text)
    except json.JSONDecodeError as e:
        log(f"Error parsing API response as JSON: {e}")
        return {"violations": []}
    except Exception as e:
        log(f"Error calling Anthropic API: {e}")
        return {"violations": []}
