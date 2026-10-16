"""Kiểm tra nhanh cấu hình Groq trước khi chạy bot: python check_groq.py

In ra: key hợp lệ không, model trong .env có tồn tại không, gọi thử một câu. Không bao giờ in API key.
"""
from __future__ import annotations

import llm
from config import GROQ_MODEL, GROQ_MODEL_FAST, GROQ_MODEL_VALUATION


def main() -> None:
    client = llm.get_client()
    if client is None:
        raise SystemExit("ERROR: GROQ_API_KEY chưa có trong .env")
    try:
        available = {m.id for m in client.models.list().data}
        print("API key: OK")
        for label, model in (("Chat", GROQ_MODEL), ("Fast/news", GROQ_MODEL_FAST), ("Valuation", GROQ_MODEL_VALUATION)):
            print(f"{label}: {model} -> {'CÓ' if model in available else 'KHÔNG CÓ'}")
        if not all(m in available for m in (GROQ_MODEL, GROQ_MODEL_FAST, GROQ_MODEL_VALUATION)):
            print("Một số model đang khả dụng:")
            for model_id in sorted(available)[:20]:
                print(" -", model_id)
            return
        response = llm.chat_completion([{"role": "user", "content": "Trả lời đúng một từ: OK"}], max_tokens=128)
        print("Gọi thử: OK ->", (response.choices[0].message.content or "").strip())
    except Exception as exc:
        raise SystemExit(f"ERROR: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()
