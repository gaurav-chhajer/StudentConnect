"""Optional explanation-only provider gateway for deterministic scores."""

import json
import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from backend import deterministic_scoring

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 2
MAX_OUTPUT_CHARACTERS = 2000
MINUTE_SECONDS = 60
MAX_USER_EXPLANATIONS_PER_DAY = 3


class ProviderUnavailable(RuntimeError):
    """Provider failed, was limited, or returned invalid output."""


@dataclass(frozen=True)
class Provider:
    name: str
    small_model: str
    large_model: str
    requests_per_minute: int
    requests_per_day: int
    tokens_per_day: int
    invoke: object

    def __post_init__(self):
        if (not self.name or not self.small_model or not self.large_model or
                any(type(value) is not int or value < 1 for value in
                    (self.requests_per_minute, self.requests_per_day, self.tokens_per_day)) or
                not callable(self.invoke)):
            raise ValueError("Provider configuration is incomplete")


@dataclass
class _Circuit:
    failures: int = 0
    open_until: float = 0.0
    recent_calls: deque = field(default_factory=deque)


def _positive_environment(name):
    raw = os.environ.get(name, "")
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 9 or int(raw) < 1:
        return None
    return int(raw)


def _openai_compatible(url, key, model, prompt):
    response = requests.post(url, headers={"Authorization": "Bearer " + key,
                              "Content-Type": "application/json"}, json={
        "model": model, "temperature": 0, "max_tokens": 350,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
    }, timeout=20)
    try:
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailable("Provider is temporarily unavailable")
        response.raise_for_status()
        payload = response.json()
        message = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        return message, usage.get("prompt_tokens"), usage.get("completion_tokens")
    finally:
        response.close()


def _groq(model, prompt):
    return _openai_compatible("https://api.groq.com/openai/v1/chat/completions",
                              os.environ["GROQ_API_KEY"], model, prompt)


def _cerebras(model, prompt):
    return _openai_compatible("https://api.cerebras.ai/v1/chat/completions",
                              os.environ["CEREBRAS_API_KEY"], model, prompt)


def _gemini(model, prompt):
    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": _SYSTEM_PROMPT + "\n" + prompt}]}],
              "generationConfig": {"temperature": 0, "maxOutputTokens": 350,
                                   "responseMimeType": "application/json"}}, timeout=20)
    try:
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailable("Provider is temporarily unavailable")
        response.raise_for_status()
        payload = response.json()
        message = payload["candidates"][0]["content"]["parts"][0]["text"]
        usage = payload.get("usageMetadata", {})
        return message, usage.get("promptTokenCount"), usage.get("candidatesTokenCount")
    finally:
        response.close()


def _ollama(model, prompt):
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    response = requests.post(base_url + "/api/chat", json={
        "model": model, "stream": False, "format": "json", "options": {"temperature": 0},
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
    }, timeout=30)
    try:
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailable("Provider is temporarily unavailable")
        response.raise_for_status()
        payload = response.json()
        message = payload["message"]["content"]
        return message, payload.get("prompt_eval_count"), payload.get("eval_count")
    finally:
        response.close()


def configured_providers():
    """Build only providers with keys, models, and explicit rate budgets configured."""
    settings = (
        ("groq", "GROQ_API_KEY", "GROQ_MODEL", "GROQ_LARGE_MODEL", _groq),
        ("gemini", "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_LARGE_MODEL", _gemini),
        ("cerebras", "CEREBRAS_API_KEY", "CEREBRAS_MODEL", "CEREBRAS_LARGE_MODEL", _cerebras),
        ("ollama", "OLLAMA_MODEL", "OLLAMA_MODEL", "OLLAMA_LARGE_MODEL", _ollama),
    )
    result = []
    for name, key_name, small_name, large_name, invoke in settings:
        key_or_model, small, large = (os.environ.get(key_name, "").strip(),
                                      os.environ.get(small_name, "").strip(),
                                      os.environ.get(large_name, "").strip())
        limits = tuple(_positive_environment("LLM_" + name.upper() + "_" + suffix)
                       for suffix in ("RPM", "DAILY_REQUESTS", "DAILY_TOKENS"))
        if not key_or_model or not small or not large or any(value is None for value in limits):
            continue
        result.append(Provider(name, small, large, *limits, invoke))
    return result


_default_gateway = None
_default_gateway_lock = threading.Lock()


def default_gateway():
    """Return one process-owned limiter and circuit-breaker state for all requests."""
    global _default_gateway
    with _default_gateway_lock:
        if _default_gateway is None:
            _default_gateway = Gateway(configured_providers())
        return _default_gateway


_SYSTEM_PROMPT = (
    "Explain a deterministic resume-to-job match. The numeric score is fixed and must never be changed. "
    "Use only the supplied score components, matched skills, and missing requirements. "
    "Do not infer personal attributes or qualifications. Return exactly JSON with string fields "
    "explanation and gap_summary."
)


def template_explanation(result):
    if not isinstance(result, dict) or type(result.get("score")) is not int:
        raise ValueError("A valid deterministic result is required")
    gaps = result.get("missing_must_have_requirements", [])
    matches = result.get("matched", [])
    gap_summary = ("Review the listed required gaps." if gaps else "No extracted must-have gaps were found.")
    return {"explanation": f"Deterministic fit score: {result['score']}/100, with {len(matches)} evidence-backed match(es).",
            "gap_summary": gap_summary, "explanation_source": "template"}


def _validated_output(raw):
    if not isinstance(raw, str) or len(raw) > MAX_OUTPUT_CHARACTERS:
        raise ValueError("Explanation response exceeds its output limit")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"explanation", "gap_summary"} or
            any(not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 800
                for key in value)):
        raise ValueError("Explanation response does not match the required JSON schema")
    return {**value, "explanation_source": "llm"}


class Gateway:
    """Rate limited, bounded provider chain. Provider output never changes score."""

    def __init__(self, providers, *, clock=time.monotonic, wall_clock=lambda: datetime.now(timezone.utc),
                 sleep=time.sleep, jitter=random.random):
        if (not isinstance(providers, (list, tuple)) or any(not isinstance(item, Provider) for item in providers) or
                len({item.name for item in providers}) != len(providers)):
            raise ValueError("Providers must be an ordered list of valid provider configurations")
        self.providers = tuple(providers)
        self.clock, self.wall_clock, self.sleep, self.jitter = clock, wall_clock, sleep, jitter
        self._circuits = {provider.name: _Circuit() for provider in providers}
        self._lock = threading.Lock()

    def explain(self, conn, result, *, user_id):
        fallback = template_explanation(result)
        if not self.providers:
            return fallback
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("A positive user ID is required for an LLM explanation")
        if not self._reserve_user_request(conn, user_id):
            return fallback
        if (not isinstance(result.get("components"), dict) or not isinstance(result.get("matched"), list) or
                not isinstance(result.get("missing_must_have_requirements"), list)):
            raise ValueError("Deterministic score details are incomplete")
        # Resume text and direct identifiers are deliberately absent from this prompt.
        safe_result = {key: result[key] for key in
                       ("score", "components", "matched", "missing_must_have_requirements")}
        prompt = json.dumps(safe_result, ensure_ascii=False, separators=(",", ":"))
        borderline = 55 <= result["score"] <= 70
        for provider in self.providers:
            circuit = self._circuits[provider.name]
            model = provider.large_model if borderline else provider.small_model
            for attempt in range(2):
                if self._skip(provider, circuit, conn, prompt):
                    break
                started = self.clock()
                status, input_tokens, output_tokens = "failed", None, None
                try:
                    raw, input_tokens, output_tokens = provider.invoke(model, prompt)
                    output = _validated_output(raw)
                    estimated_input = max(1, (len((_SYSTEM_PROMPT + prompt).encode("utf-8")) + 3) // 4)
                    input_tokens = (input_tokens if type(input_tokens) is int and input_tokens >= 0
                                    else estimated_input)
                    output_tokens = (output_tokens if type(output_tokens) is int and output_tokens >= 0
                                     else max(1, (len(raw.encode("utf-8")) + 3) // 4))
                    status = "succeeded"
                    self._record(conn, provider, model, status, input_tokens, output_tokens,
                                 round((self.clock() - started) * 1000))
                    self._success(circuit)
                    logger.info("LLM explanation provider=%s model=%s latency_ms=%s input_tokens=%s output_tokens=%s status=%s",
                                provider.name, model, round((self.clock() - started) * 1000),
                                input_tokens, output_tokens, status)
                    return output
                except (requests.RequestException, ProviderUnavailable, ValueError, KeyError, TypeError) as error:
                    retryable = isinstance(error, (ProviderUnavailable, ValueError)) or (
                        isinstance(error, requests.HTTPError) and error.response is not None and
                        (error.response.status_code == 429 or error.response.status_code >= 500))
                    self._record(conn, provider, model, status, input_tokens, output_tokens,
                                 round((self.clock() - started) * 1000))
                    self._failure(circuit)
                    logger.warning("LLM explanation provider=%s model=%s error=%s attempt=%s",
                                   provider.name, model, type(error).__name__, attempt + 1)
                    if retryable and attempt == 0:
                        self.sleep(0.5 + self.jitter() * 0.5)
                        continue
                    break
        return fallback

    def _reserve_user_request(self, conn, user_id):
        now = self.wall_clock().astimezone(timezone.utc)
        day_start = now.date().isoformat()
        with self._lock, conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("""SELECT COUNT(*) FROM deterministic_user_llm_requests
                WHERE user_id=? AND created_at>=?""", (user_id, day_start)).fetchone()[0]
            if count >= MAX_USER_EXPLANATIONS_PER_DAY:
                return False
            conn.execute("INSERT INTO deterministic_user_llm_requests(user_id,created_at) VALUES(?,?)",
                         (user_id, now.isoformat()))
        return True

    def _skip(self, provider, circuit, conn, prompt):
        now = self.clock()
        with self._lock:
            while circuit.recent_calls and circuit.recent_calls[0] <= now - MINUTE_SECONDS:
                circuit.recent_calls.popleft()
            if circuit.open_until > now or len(circuit.recent_calls) >= provider.requests_per_minute:
                return True
            day_start = self.wall_clock().astimezone(timezone.utc).date().isoformat()
            row = conn.execute("""SELECT COUNT(*),COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0)
                FROM deterministic_llm_calls WHERE provider=? AND created_at>=?""",
                (provider.name, day_start)).fetchone()
            estimate = max(1, (len((_SYSTEM_PROMPT + prompt).encode("utf-8")) + 3) // 4) + 350
            if row[0] >= provider.requests_per_day or row[1] + estimate > provider.tokens_per_day:
                return True
            circuit.recent_calls.append(now)
            return False

    def _success(self, circuit):
        with self._lock:
            circuit.failures = 0
            circuit.open_until = 0

    def _failure(self, circuit):
        with self._lock:
            circuit.failures += 1
            if circuit.failures >= 3:
                circuit.open_until = self.clock() + 300
                circuit.failures = 0

    def _record(self, conn, provider, model, status, input_tokens, output_tokens, latency_ms):
        with conn:
            conn.execute("""INSERT INTO deterministic_llm_calls
                (provider,model,latency_ms,input_tokens,output_tokens,status,created_at)
                VALUES(?,?,?,?,?,?,?)""", (provider.name, model, latency_ms, input_tokens, output_tokens,
                                              status, self.wall_clock().astimezone(timezone.utc).isoformat()))
