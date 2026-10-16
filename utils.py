"""Hàm tiện ích dùng chung: ép kiểu số, định dạng số liệu và chuẩn hóa chữ tiếng Việt.

Trước đây mỗi module tự viết một bản riêng (fnum, _num, _f, _fmt...). Gom về
một chỗ để mọi nơi hiển thị số theo cùng một quy ước.
"""
from __future__ import annotations

import math
import re
import unicodedata


# ---------------------------------------------------------------- số
def to_float(value, default=None):
    """Chuyển về float hữu hạn; None/NaN/inf/chuỗi lỗi trả về `default`."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def fmt_price(value) -> str:
    """Giá VND có dấu phân tách nghìn: 62770 -> '62,770'."""
    x = to_float(value)
    return "N/A" if x is None else f"{x:,.0f}"


def fmt_num(value, digits: int = 2) -> str:
    x = to_float(value)
    return "N/A" if x is None else f"{x:,.{digits}f}"


def fmt_pct(value, signed: bool = False, digits: int = 1) -> str:
    """Phần trăm: fmt_pct(3.14159, signed=True) -> '+3.1%'."""
    x = to_float(value)
    if x is None:
        return "N/A"
    return f"{x:+.{digits}f}%" if signed else f"{x:.{digits}f}%"


def fmt_money(value) -> str:
    """Giá trị tiền VND rút gọn: 1.25e12 -> '1.2 nghìn tỷ'."""
    x = to_float(value)
    if x is None:
        return "N/A"
    a = abs(x)
    if a >= 1e12:
        return f"{x / 1e12:,.1f} nghìn tỷ"
    if a >= 1e9:
        return f"{x / 1e9:,.1f} tỷ"
    if a >= 1e6:
        return f"{x / 1e6:,.1f} triệu"
    return f"{x:,.0f}"


def fmt_volume(value) -> str:
    """Khối lượng cổ phiếu rút gọn: 3_200_000 -> '3.20 triệu'."""
    x = to_float(value)
    if x is None:
        return "N/A"
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.2f} tỷ"
    if a >= 1e6:
        return f"{x / 1e6:.2f} triệu"
    if a >= 1e3:
        return f"{x / 1e3:.1f} nghìn"
    return f"{x:,.0f}"


# ---------------------------------------------------------------- chữ
def normalize_text(text) -> str:
    """Bỏ dấu, chữ thường, chỉ giữ a-z0-9 và khoảng trắng.

    'Cổ phiếu FPT thế nào?' -> 'co phieu fpt the nao'.
    Chữ 'đ' không tách dấu được bằng NFD nên phải thay tay.
    """
    s = str(text or "").lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def padded(text) -> str:
    """normalize_text có thêm khoảng trắng hai đầu để so khớp nguyên cụm từ.

    Ví dụ khóa ' no ' khớp 'gia no bao nhieu' nhưng không khớp 'nong'.
    """
    return f" {normalize_text(text)} "


def contains_any(haystack: str, keys) -> bool:
    return any(k in haystack for k in keys)
