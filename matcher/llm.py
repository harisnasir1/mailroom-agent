import json
import os
import re
import sqlite3
import time

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv()

API_URL = "https://api.groq.com/openai/v1/chat/completions"
API_KEY = os.environ.get("GroqApiKey")
DEFAULT_MODEL = os.environ.get("LLM_MODEL_CHEAP") or "openai/gpt-oss-20b"
TIMEOUT_SECONDS = 20

# USD per 1M input / output tokens.
PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-20b": (0.075, 0.30),
}

FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class _TransientError(Exception):
    pass


def _strip_fences(text: str) -> str:
    return FENCE_RE.sub("", text.strip()).strip()


def _json_instruction(schema: type[BaseModel]) -> str:
    return (
        "\n\nRespond with JSON only - no prose, no markdown code fences, no "
        "explanation before or after. The JSON must match this schema exactly:\n"
        f"{json.dumps(schema.model_json_schema())}"
    )


def _post(model: str, prompt: str) -> requests.Response:
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code >= 500:
        raise _TransientError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError, _TransientError)),
    reraise=True,
)
def _post_with_retry(model: str, prompt: str) -> requests.Response:
    return _post(model, prompt)


class _Attempt:
    def __init__(self, raw_text: str | None, prompt_tokens: int, completion_tokens: int,
                 retries: int, latency_ms: int, error: str | None):
        self.raw_text = raw_text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.retries = retries
        self.latency_ms = latency_ms
        self.error = error


def _do_attempt(model: str, prompt: str) -> _Attempt:
    started = time.monotonic()
    try:
        resp = _post_with_retry(model, prompt)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
             _TransientError, requests.exceptions.RequestException) as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        retries = _post_with_retry.statistics.get("attempt_number", 1) - 1
        return _Attempt(None, 0, 0, retries, latency_ms, str(exc))

    latency_ms = int((time.monotonic() - started) * 1000)
    retries = _post_with_retry.statistics.get("attempt_number", 1) - 1
    body = resp.json()
    text = body["choices"][0]["message"]["content"] or ""
    usage = body.get("usage", {})
    return _Attempt(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                     retries, latency_ms, None)


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (prompt_tokens / 1_000_000) * price_in + (completion_tokens / 1_000_000) * price_out


def _parse(raw_text: str | None, schema: type[BaseModel]) -> tuple[BaseModel | None, str | None]:
    if raw_text is None:
        return None, "empty response"
    cleaned = _strip_fences(raw_text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    try:
        return schema.model_validate(data), None
    except ValidationError as exc:
        return None, str(exc)


def _write_trace(conn: sqlite3.Connection, run_id: str, email_id: int, stage: str, model: str,
                  attempt: _Attempt, cost_usd: float) -> None:
    conn.execute(
        """
        INSERT INTO traces (run_id, email_id, stage, model, input_tokens, output_tokens,
                             cost_usd, latency_ms, retries, error, output_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, email_id, stage, model, attempt.prompt_tokens, attempt.completion_tokens,
         cost_usd, attempt.latency_ms, attempt.retries, attempt.error,
         (attempt.raw_text or "")[:2000]),
    )
    conn.commit()


def _attempt_and_trace(conn: sqlite3.Connection, run_id: str, email_id: int, stage: str,
                        model: str, prompt: str) -> _Attempt:
    attempt = _do_attempt(model, prompt)
    cost = _cost_usd(model, attempt.prompt_tokens, attempt.completion_tokens)
    _write_trace(conn, run_id, email_id, stage, model, attempt, cost)
    return attempt


def call_structured(
    prompt: str,
    schema: type[BaseModel],
    run_id: str,
    email_id: int,
    conn: sqlite3.Connection,
    stage: str,
    model: str | None = None,
) -> tuple[BaseModel | None, bool]:
    try:
        model = model or DEFAULT_MODEL
        full_prompt = prompt + _json_instruction(schema)

        attempt = _attempt_and_trace(conn, run_id, email_id, stage, model, full_prompt)
        if attempt.error is not None:
            return None, False

        parsed, parse_error = _parse(attempt.raw_text, schema)
        if parsed is not None:
            return parsed, True

        repair_prompt = (
            full_prompt
            + f"\n\nYour previous reply failed validation with this error:\n{parse_error}\n"
            + "Reply with corrected JSON only."
        )
        repair = _attempt_and_trace(conn, run_id, email_id, stage, model, repair_prompt)
        if repair.error is not None:
            return None, False

        parsed, _ = _parse(repair.raw_text, schema)
        return (parsed, True) if parsed is not None else (None, False)
    except Exception as exc:
        try:
            conn.execute(
                """
                INSERT INTO traces (run_id, email_id, stage, model, error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, email_id, stage, model or DEFAULT_MODEL, f"unexpected error: {exc}"),
            )
            conn.commit()
        except Exception:
            pass
        return None, False
