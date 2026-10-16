"""Shared low-latency Groq JSON/text client for analysis services.

All AI-assisted modules use this file so model selection, timeout, retries and
error handling stay consistent. Deterministic finance calculations remain in
application code; the model only produces assumptions/summaries/metadata.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("stockinsight.groq")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
FAST_MODEL = os.getenv("GROQ_MODEL_FAST", os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")).strip()
VALUATION_MODEL = os.getenv("GROQ_MODEL_VALUATION", "openai/gpt-oss-120b").strip()
FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b").strip()
TIMEOUT_S = max(4.0, float(os.getenv("GROQ_TIMEOUT_MS", "15000")) / 1000.0)

_client = None
_client_lock = threading.Lock()
_blocked_until = 0.0
_block_reason = ""


def api_key() -> str:
    return os.getenv("GROQ_API_KEY", "").strip()


def get_client():
    global _client
    if not api_key():
        return None
    if _client is None:
        with _client_lock:
            if _client is None:
                from openai import OpenAI
                _client = OpenAI(
                    api_key=api_key(), base_url=GROQ_BASE_URL,
                    timeout=TIMEOUT_S, max_retries=0,
                )
    return _client


def available() -> bool:
    return bool(api_key()) and time.time() >= _blocked_until


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        return int(value)
    except Exception:
        m = re.search(r"\b(400|401|403|404|408|409|429|500|502|503|504)\b", str(exc))
        return int(m.group(1)) if m else None


def _record_error(exc: Exception) -> None:
    global _blocked_until, _block_reason
    code = _status_code(exc)
    text = f"{type(exc).__name__}: {str(exc)[:300]}"
    _block_reason = text
    if code in (401, 403):
        _blocked_until = max(_blocked_until, time.time() + 600)
    elif code == 429:
        _blocked_until = max(_blocked_until, time.time() + 20)


def describe_error(exc: Exception) -> str:
    code = _status_code(exc)
    detail = str(exc).replace(api_key(), "[đã ẩn]") if api_key() else str(exc)
    detail = detail[:350]
    if code in (401, 403):
        hint = "Kiểm tra GROQ_API_KEY và quyền truy cập model."
    elif code == 404:
        hint = "Model Groq được cấu hình không còn khả dụng."
    elif code == 429:
        hint = "Groq đang chạm rate limit; ứng dụng sẽ dùng fallback/cache khi có thể."
    elif code in (500, 502, 503, 504):
        hint = "Groq đang tạm quá tải; ứng dụng đã thử model dự phòng."
    else:
        hint = "Kiểm tra kết nối mạng và cấu hình Groq."
    return f"Groq API{f' HTTP {code}' if code else ''}: {detail}. {hint}"


def _extra_body(model: str) -> dict[str, Any]:
    if model.lower().startswith("openai/gpt-oss-"):
        return {"reasoning_effort": "low", "include_reasoning": False}
    return {}


def _models(primary: str, fallback: str | None) -> list[str]:
    out=[]
    for m in (primary, fallback):
        m=(m or "").strip()
        if m and m not in out:
            out.append(m)
    return out


def text_completion(prompt: str, *, primary_model: str = FAST_MODEL,
                    fallback_model: str | None = FALLBACK_MODEL,
                    max_tokens: int = 1200, temperature: float = 0.1) -> str:
    if not available():
        raise RuntimeError("GROQ_API_KEY chưa được cấu hình hoặc Groq đang tạm bị circuit-breaker.")
    client=get_client()
    last=None
    for model in _models(primary_model, fallback_model):
        for attempt in range(2):
            try:
                resp=client.chat.completions.create(
                    model=model,
                    messages=[{"role":"user","content":prompt}],
                    temperature=temperature,
                    max_completion_tokens=max_tokens,
                    extra_body=_extra_body(model),
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as exc:
                last=exc
                code=_status_code(exc)
                _record_error(exc)
                # 401/403: key problem; retrying other model is useless.
                if code in (401,403):
                    raise
                # 404: model unavailable -> immediately try fallback.
                if code == 404:
                    break
                # One short retry for transient failures; never 1+2+4s x models.
                if code in (408,429,500,502,503,504) and attempt == 0:
                    time.sleep(0.45)
                    continue
                break
    if last:
        raise last
    raise RuntimeError("Không có model Groq hợp lệ được cấu hình.")


def json_completion(prompt: str, *, primary_model: str = FAST_MODEL,
                    fallback_model: str | None = FALLBACK_MODEL,
                    max_tokens: int = 1600, temperature: float = 0.05) -> dict[str, Any]:
    if not available():
        raise RuntimeError("GROQ_API_KEY chưa được cấu hình hoặc Groq đang tạm bị circuit-breaker.")
    client=get_client()
    last=None
    for model in _models(primary_model, fallback_model):
        for attempt in range(2):
            try:
                resp=client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role":"system","content":"Return only a valid JSON object matching the user's requested schema."},
                        {"role":"user","content":prompt},
                    ],
                    response_format={"type":"json_object"},
                    temperature=temperature,
                    max_completion_tokens=max_tokens,
                    extra_body=_extra_body(model),
                )
                text=(resp.choices[0].message.content or "").strip()
                text=re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
                value=json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError("Groq response is not a JSON object")
                return value
            except (json.JSONDecodeError, ValueError) as exc:
                last=exc
                # malformed JSON: one retry on same model, then fallback
                if attempt == 0:
                    continue
                break
            except Exception as exc:
                last=exc
                code=_status_code(exc)
                _record_error(exc)
                if code in (401,403):
                    raise
                if code == 404:
                    break
                if code in (408,429,500,502,503,504) and attempt == 0:
                    time.sleep(0.45)
                    continue
                break
    if last:
        raise last
    raise RuntimeError("Không có model Groq hợp lệ được cấu hình.")
