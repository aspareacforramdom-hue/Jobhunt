"""
Shared LLM caller. One place to swap providers.

Deliberately stdlib-only. The official SDK is the better default in a
project that already has dependencies, but this one has to run on three
laptops from a `pip install` a person types by hand, and every package
added here is one more thing that can be missing at 07:00.
"""

import json
import os
import random
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CAREERLOOP_MODEL", "claude-sonnet-4-6")

# Rate limits, overloads and transient 5xx. Anything else is a bug in the
# request and retrying it just spends the same money twice.
RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504, 529)
MAX_ATTEMPTS = 4


class LLMError(RuntimeError):
    """Carries the API's own explanation, which the plain HTTPError does
    not: `HTTP Error 400: Bad Request` is indistinguishable from a dozen
    different mistakes, and the body says which one."""


def _post(payload, timeout):
    """
    One request, retried on the statuses that are worth retrying.

    A scan scores ~25 batches back to back, so a single 429 in the middle
    used to lose eight listings silently — score_jobs caught the error,
    printed one line, and moved on. Sleeping a few seconds is cheaper
    than a user never seeing those roles.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("ANTHROPIC_API_KEY not set")

    body = json.dumps(payload).encode()
    headers = {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }

    last = None
    for attempt in range(MAX_ATTEMPTS):
        # A fresh Request per attempt: urlopen mutates the one it is
        # given, and a retry should not carry state from the failure.
        req = urllib.request.Request(API_URL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:400]
            except Exception:                        # noqa: BLE001
                pass
            last = LLMError(f"HTTP {e.code}: {detail or e.reason}")
            if e.code not in RETRY_STATUS:
                raise last from None
            # Honour Retry-After when the API sends one; it knows better
            # than any backoff curve guessed here.
            wait = _retry_after(e) or (2 ** attempt) + random.uniform(0, 1)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = LLMError(f"network error: {e}")
            wait = (2 ** attempt) + random.uniform(0, 1)

        if attempt == MAX_ATTEMPTS - 1:
            break
        print(f"  [llm] {last} — retrying in {wait:.0f}s "
              f"({attempt + 2}/{MAX_ATTEMPTS})")
        time.sleep(wait)

    raise last


def _retry_after(err):
    try:
        return min(60.0, float(err.headers.get("retry-after")))
    except (AttributeError, TypeError, ValueError):
        return None


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

    data = _post(payload, timeout=180)

    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
    usage = data.get("usage") or {}

    if not as_json:
        return text, usage

    cleaned = text.replace("```json", "").replace("```", "").strip()
    if not cleaned:
        # An empty body is not a parse failure, and reporting it as one
        # sends whoever is debugging to the wrong place. It means the
        # model declined, or the answer was cut off before any text.
        raise LLMError(
            f"model returned no text (stop_reason="
            f"{data.get('stop_reason')!r}) — nothing to parse")
    try:
        return json.loads(cleaned), usage
    except json.JSONDecodeError:
        start = min([i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
                    or [0])
        end = max(cleaned.rfind("}"), cleaned.rfind("]")) + 1
        try:
            return json.loads(cleaned[start:end]), usage
        except (json.JSONDecodeError, ValueError):
            hint = (" — the reply hit max_tokens and is truncated"
                    if data.get("stop_reason") == "max_tokens" else "")
            raise LLMError(
                f"could not parse JSON from the reply{hint}: "
                f"{cleaned[:200]}") from None


# Dynamic filtering: the model writes code to filter results before they
# reach the context window. Supported on the models this tool runs on.
WEB_SEARCH_TOOL = [{"type": "web_search_20260209", "name": "web_search"}]
