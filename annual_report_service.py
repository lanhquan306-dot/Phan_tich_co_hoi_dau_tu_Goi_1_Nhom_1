"""Optional bridge to Tumiqa/vn-annual-report-miner.

The main dashboard does not fabricate annual-report content. If `arminer` is
installed, this adapter can query its catalogue and expose the result as a
supplementary source. If it is unavailable, the UI explicitly says so.

Install separately:
    git clone https://github.com/Tumiqa/vn-annual-report-miner.git
    cd vn-annual-report-miner && pip install -e .
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

ARMINER_PROJECT_URL = "https://github.com/Tumiqa/vn-annual-report-miner"


def status() -> dict[str, Any]:
    exe = shutil.which("arminer")
    enabled = os.getenv("ARMINER_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "available": bool(exe),
        "enabled": enabled and bool(exe),
        "executable": exe or "",
        "project_url": ARMINER_PROJECT_URL,
        "source_name": "vn-annual-report-miner (Tumiqa)",
    }


def catalog_lookup(symbol: str, timeout: int = 20) -> dict[str, Any]:
    st = status()
    base = {**st, "symbol": symbol.upper(), "used": False, "catalog_excerpt": ""}
    if not st["enabled"]:
        base["note"] = (
            "Nguồn bổ sung đã được tích hợp ở chế độ tùy chọn nhưng chưa kích hoạt. "
            "Đặt ARMINER_ENABLED=1 sau khi cài arminer để tra cứu danh mục BCTN."
        )
        return base
    try:
        cp = subprocess.run(
            [st["executable"], "catalog", "search", "--ticker", symbol.upper()],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        text = (cp.stdout or cp.stderr or "").strip()
        base["used"] = cp.returncode == 0 and bool(text)
        base["catalog_excerpt"] = text[:1600]
        base["note"] = "Đã tra cứu danh mục BCTN bằng arminer." if base["used"] else "arminer không trả về kết quả danh mục có thể đọc."
    except Exception as exc:
        base["note"] = f"Không truy vấn được arminer: {type(exc).__name__}."
    return base
