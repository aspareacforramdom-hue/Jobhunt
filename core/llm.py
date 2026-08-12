"""Shared LLM caller. One place to swap providers."""

import json
import os
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CAREERLOOP_MODEL", "claude-sonnet-4-6")


def call_llm(system, prompt, max_tokens=2000, as_json=False, tools=None,
             cache_system=False):
    """Returns the parsed response. Use call_llm_usage when you need the
    token accounting as well."""
    result, _ = call_llm_usage(system, prompt, max_tokens, as_json, tools,
                               cache_system)
    return result


def call_llm_usage(system, prompt, max_tokens=2000, as_json=False, tools=None,
                   cache_system=False):
    """
    Returns (parsed_response, usage).

    cache_system marks the system prompt as cacheable. Scoring sends the
    same system block — instructions plus this user's dimensions — ahead
    of every batch in a scan, so after the first call the rest read it
    back at roughly a tenth of the input price.

    Caching is a PREFIX match, so the volatile part (the listings) must
    stay in the user message, after the breakpoint. It also only engages
    above a model-dependent minimum prefix length; below it the API
    silently declines to cache rather than erroring. That is why usage
    is returned — `cache_read_input_tokens` staying at zero across a run
    is the only reliable proof it is not working.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": ([{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}]
                   if cache_system else system),
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        payload["tools"] = tools

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode())

    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
    usage = data.get("usage") or {}

    if not as_json:
        return text, usage

    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned), usage
    except json.JSONDecodeError:
        start = min([i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
                    or [0])
        end = max(cleaned.rfind("}"), cleaned.rfind("]")) + 1
        return json.loads(cleaned[start:end]), usage


# Dynamic filtering: the model writes code to filter results before they
# reach the context window. Supported on the models this tool runs on.
WEB_SEARCH_TOOL = [{"type": "web_search_20260209", "name": "web_search"}]
