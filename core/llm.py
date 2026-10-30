"""Small OpenAI Responses API client used by optional AI features."""

import json
import os
import random
import time
import urllib.error
import urllib.request

API_URL = "https://api.openai.com/v1/responses"
MODEL = os.environ.get("CAREERLOOP_MODEL", "gpt-6-luna")
RETRY_STATUS = (429,)
MAX_ATTEMPTS = 4


class LLMError(RuntimeError):
    pass


def _post(payload, timeout):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise LLMError("OPENAI_API_KEY not set")
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json",
               "authorization": f"Bearer {key}"}
    last = None
    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(API_URL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:400]
            except Exception:  # noqa: BLE001
                pass
            last = LLMError(f"HTTP {exc.code}: {detail or exc.reason}")
            if exc.code not in RETRY_STATUS:
                raise last from None
            wait = _retry_after(exc) or (2 ** attempt) + random.uniform(0, 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError("AI request outcome unknown; not retried to avoid duplicate spending") from exc
        if attempt == MAX_ATTEMPTS - 1:
            break
        print(f"  [llm] {last} - retrying in {wait:.0f}s ({attempt + 2}/{MAX_ATTEMPTS})")
        time.sleep(wait)
    raise last


def _retry_after(error):
    try:
        return min(60.0, float(error.headers.get("retry-after")))
    except (AttributeError, TypeError, ValueError):
        return None


def call_llm(system, prompt, max_tokens=2000, as_json=False, tools=None,
             cache_system=False):
    result, _ = call_llm_usage(system, prompt, max_tokens, as_json, tools,
                               cache_system)
    return result


def call_llm_usage(system, prompt, max_tokens=2000, as_json=False, tools=None,
                   cache_system=False, model=None):
    """Return response text (or parsed JSON) plus normalized token usage."""
    payload = {"model": model or MODEL, "instructions": system,
               "input": prompt, "max_output_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
    if as_json:
        payload["text"] = {"format": {"type": "json_object"}}
    data = _post(payload, timeout=180)
    text = "".join(
        part.get("text", "")
        for item in data.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text"
    )
    raw_usage = data.get("usage") or {}
    details = raw_usage.get("input_tokens_details") or {}
    usage = {"input_tokens": raw_usage.get("input_tokens", 0),
             "output_tokens": raw_usage.get("output_tokens", 0),
             "cache_read_input_tokens": details.get("cached_tokens", 0),
             "cache_creation_input_tokens": 0}
    from core.usage import record
    record(payload["model"], usage)
    if not as_json:
        return text, usage
    cleaned = text.replace("```json", "").replace("```", "").strip()
    if not cleaned:
        raise LLMError(f"model returned no text (status={data.get('status')!r})")
    try:
        return json.loads(cleaned), usage
    except json.JSONDecodeError:
        starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
        start = min(starts or [0])
        end = max(cleaned.rfind("}"), cleaned.rfind("]")) + 1
        try:
            return json.loads(cleaned[start:end]), usage
        except (json.JSONDecodeError, ValueError):
            raise LLMError(f"could not parse JSON from reply: {cleaned[:200]}") from None


WEB_SEARCH_TOOL = [{"type": "web_search"}]
