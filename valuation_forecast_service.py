
"""Groq-assisted three-year scenario valuation.

Luồng:
1. Kiểm tra tín hiệu vòng 1/L2: không phải BUY/MUA thì trả về SKIPPED.
2. Kiểm tra dữ liệu đầu vào.
3. Groq sinh giả định cho ba kịch bản.
4. Kiểm tra giả định.
5. Dự phóng ba năm và tính giá mục tiêu.
6. Tính upside, CAGR và tín hiệu định giá (không ghi đè tín hiệu L2).

Quy ước đơn vị:
- Giá cổ phiếu: VND/cổ phiếu.
- Doanh thu, LNST, vốn chủ sở hữu: VND.
- Số cổ phiếu lưu hành: cổ phiếu thực tế.
- EPS, BVPS: VND/cổ phiếu.

Phải chuẩn hóa dữ liệu nguồn theo các đơn vị trên trước khi gọi service.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("stockinsight.valuation")

SCENARIOS = {
    "conservative": "Thận trọng",
    "base": "Cơ sở",
    "optimistic": "Lạc quan",
}

YEAR_KEYS = ("year_1", "year_2", "year_3")

SCENARIO_KEYS = ("conservative", "base", "optimistic")


class ValuationForecastService:
    """Tạo giả định bằng Groq và tính định giá bằng công thức trong ứng dụng."""

    MODEL_NAME = "openai/gpt-oss-120b"
    FALLBACK_MODEL_NAME = "openai/gpt-oss-20b"

    GENERATION_RETRY_DELAYS = (1.0, 2.0, 4.0)

    # Cache chỉ phần giả định AI. Key bao gồm toàn bộ dữ liệu prompt nên không
    # làm cũ giá/đầu vào: chỉ reuse khi input thực sự giống hệt lần trước.
    _SCENARIO_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
    _SCENARIO_CACHE_LOCK = threading.Lock()
    _SCENARIO_CACHE_TTL = 1_800.0

    # Chỉ các mã tín hiệu này mới được chạy định giá chuyên sâu.
    BUY_SIGNAL_CODES = frozenset({"BUY", "MUA"})

    # Cảnh báo khi P/E mục tiêu kịch bản cơ sở lệch quá xa P/E hiện tại.
    BASE_MULTIPLE_WARNING_GAP = 0.30

    INDUSTRY_MINIMUM_UPSIDE = {
        "REAL_ESTATE": 0.25,
        "CYCLICAL": 0.20,
        "NORMAL": 0.15,
        "TECH": 0.12,
        "BANK": 0.10,
    }

    @classmethod
    def execute(
        cls,
        symbol: str,
        fundamental_data: dict[str, Any],
        technical_data: dict[str, Any],
        company_info: dict[str, Any],
        initial_signal: str = "",
    ) -> dict[str, Any]:
        """Định giá chuyên sâu 3 kịch bản, CHỈ chạy khi tín hiệu vòng 1/L2 là BUY.

        Theo đặc tả Gói 1: tham số initial_signal quyết định có chạy định giá
        chuyên sâu hay không. Nếu không phải BUY hoặc MUA, trả về SKIPPED và
        không gọi AI. Khi đã chạy, tín hiệu định giá chỉ là thông tin bổ sung,
        không ghi đè tín hiệu L2.
        """

        symbol = str(symbol or "").strip().upper()
        signal_code = str(initial_signal or "").strip().upper() or "UNKNOWN"

        # Bước 1: Kiểm tra tín hiệu đầu vào.
        if signal_code not in cls.BUY_SIGNAL_CODES:
            return {
                "status": "SKIPPED",
                "symbol": symbol,
                "initial_signal": signal_code,
                "message": (
                    "Định giá chuyên sâu chỉ chạy khi tín hiệu vòng 1/L2 là "
                    f"MUA (BUY). Tín hiệu hiện tại: {signal_code}."
                ),
            }

        # Bước 2: Phân loại ngành.
        company_type = str(
            fundamental_data.get("company_type") or "regular"
        )
        industry = str(company_info.get("industry") or "")

        industry_type = cls._industry_type(company_type, industry)

        if industry_type == "REAL_ESTATE":
            return {
                "status": "INSUFFICIENT_DATA",
                "symbol": symbol,
                "industry_type": industry_type,
                "initial_signal": signal_code,
                "message": (
                    "Chưa thể định giá RNAV vì dữ liệu hiện có chưa bao gồm "
                    "danh mục dự án, giá trị phát triển và nghĩa vụ nợ theo "
                    "từng dự án. Hệ thống không tự tạo số RNAV."
                ),
            }

        if industry_type == "UNSUPPORTED_FINANCIAL":
            return {
                "status": "UNSUPPORTED_INDUSTRY",
                "symbol": symbol,
                "industry_type": industry_type,
                "initial_signal": signal_code,
                "message": (
                    "Chưa có mô hình định giá chuyên biệt cho công ty chứng "
                    "khoán và bảo hiểm. Hệ thống không áp dụng trực tiếp "
                    "mô hình P/B ngân hàng."
                ),
            }

        # Bước 2: Kiểm tra giá hiện tại.
        current_price = cls._positive_value(
            technical_data.get("close")
        )

        if current_price is None:
            return cls._insufficient(
                symbol,
                industry_type,
                "Không có giá thị trường hợp lệ để tính giá mục tiêu.",
            )

        # Bước 3: Lấy lịch sử tài chính.
        raw_history = fundamental_data.get("history") or []

        if not isinstance(raw_history, list) or not raw_history:
            return cls._insufficient(
                symbol,
                industry_type,
                "Thiếu lịch sử tài chính để tạo kịch bản dự phóng.",
            )

        history = sorted(
            [
                row for row in raw_history
                if isinstance(row, dict)
            ],
            key=lambda row: cls._safe_year(row.get("year")),
        )

        if not history:
            return cls._insufficient(
                symbol,
                industry_type,
                "Không có bản ghi lịch sử tài chính hợp lệ.",
            )

        latest = history[-1]

        # Số cổ phiếu phải là số cổ phiếu thực tế.
        shares = cls._positive_value(
            company_info.get("issued_share")
        )

        if shares is None:
            market_cap = cls._positive_value(
                company_info.get("market_cap")
            )
            if market_cap is not None:
                shares = market_cap / current_price

        base_revenue = cls._positive_value(
            latest.get("revenue")
        )
        base_equity = cls._positive_value(
            latest.get("equity")
        )

        # Một số nguồn có thể không cung cấp vốn chủ sở hữu tổng.
        if industry_type == "BANK":
            if base_equity is None and shares is not None:
                bvps = cls._positive_value(
                    fundamental_data.get("bvps")
                )
                if bvps is not None:
                    base_equity = bvps * shares

            error = cls._validate_valuation_inputs(
                current_price=current_price,
                shares=shares,
                base_revenue=base_revenue,
                base_equity=base_equity,
                industry_type=industry_type,
            )
        else:
            error = cls._validate_valuation_inputs(
                current_price=current_price,
                shares=shares,
                base_revenue=base_revenue,
                base_equity=base_equity,
                industry_type=industry_type,
            )

        if error:
            return cls._insufficient(
                symbol,
                industry_type,
                error,
            )

        # Bước 4: Yêu cầu Groq tạo giả định.
        current_roe = cls._number_value(
            (
                fundamental_data.get("criteria", {})
                .get("roe", {})
                .get("value")
            )
        )

        criteria = fundamental_data.get("criteria") or {}
        current_pe = cls._positive_value((criteria.get("pe") or {}).get("value"))
        current_pb = cls._positive_value((criteria.get("pb") or {}).get("value"))

        ai_result = cls._generate_scenarios(
            symbol=symbol,
            industry_type=industry_type,
            industry=industry,
            company_name=str(
                company_info.get("company_name")
                or company_info.get("name")
                or symbol
            ),
            history=history[-5:],
            current_price=current_price,
            shares=shares,
            base_revenue=base_revenue,
            base_equity=base_equity,
            current_roe=current_roe,
            current_pe=current_pe,
            current_pb=current_pb,
        )

        if ai_result.get("error"):
            return {
                "status": ai_result["error"],
                "symbol": symbol,
                "industry_type": industry_type,
                "initial_signal": signal_code,
                "message": ai_result["message"],
            }

        # Bước 5: Tính toán ba kịch bản bằng công thức.
        scenario_results: dict[str, dict[str, Any]] = {}
        summary_results: list[dict[str, Any]] = []

        base_year = cls._safe_year(latest.get("year"))

        for scenario_key, scenario_name in SCENARIOS.items():
            if industry_type == "BANK":
                forecast, valuation = cls._project_bank_scenario(
                    key=scenario_key,
                    assumptions=ai_result["assumptions"],
                    base_equity=base_equity,
                    shares=shares,
                    base_year=base_year,
                )
            else:
                forecast, valuation = cls._project_company_scenario(
                    key=scenario_key,
                    assumptions=ai_result["assumptions"],
                    base_revenue=base_revenue,
                    shares=shares,
                    base_year=base_year,
                )

            metrics = cls._calculate_metrics(
                target_price=valuation["target_price_y3"],
                current_price=current_price,
            )

            scenario_results[scenario_key] = {
                "name": scenario_name,
                "forecast_projections": forecast,
                "valuation": valuation,
                "investment_metrics": metrics,
                "note": ai_result["scenario_notes"][scenario_name],
            }

            summary_results.append({
                "name": scenario_name,
                "method": valuation["valuation_method"],
                "target_price_y3": valuation["target_price_y3"],
                "cumulative_return_pct": metrics["upside_pct"],
                "cagr_pct": metrics["cagr_pct"],
            })

        # Bước 6: Đánh giá theo upside kịch bản cơ sở.
        base_result = scenario_results["base"]

        base_upside = (
            base_result["investment_metrics"]["upside_pct"] / 100
        )

        minimum_upside = cls.INDUSTRY_MINIMUM_UPSIDE[industry_type]

        final_signal, action_note = cls._independent_valuation_signal(
            base_upside=base_upside,
            minimum_upside=minimum_upside,
        )

        warnings: list[str] = []
        if industry_type == "BANK":
            base_multiple = ai_result["assumptions"]["pb_scenarios"]["base"]
            current_multiple, label = current_pb, "P/B"
        else:
            base_multiple = ai_result["assumptions"]["pe_scenarios"]["base"]
            current_multiple, label = current_pe, "P/E"
        if current_multiple:
            gap = base_multiple / current_multiple - 1
            if abs(gap) > cls.BASE_MULTIPLE_WARNING_GAP:
                warnings.append(
                    f"{label} mục tiêu kịch bản cơ sở {base_multiple:.2f}x lệch "
                    f"{gap:+.0%} so với {label} hiện tại {current_multiple:.2f}x; "
                    "upside phụ thuộc mạnh vào giả định tái định giá."
                )
        warnings.append(
            f"Upside là lũy kế đến cuối năm {base_year + 3} so với giá hiện tại, "
            "chưa chiết khấu về hiện tại và chưa tính cổ tức."
        )

        # Bước 7: Trả kết quả có cấu trúc để module khác sử dụng.
        return {
            "status": "COMPLETED",
            "symbol": symbol,
            "industry_type": industry_type,
            "initial_signal": signal_code,
            "current_price": current_price,
            "industry_minimum_upside_pct": minimum_upside * 100,
            "final_signal": final_signal,
            "base_upside_pct": round(base_upside * 100, 1),
            "action_note": action_note,
            "summary_table": summary_results,
            "scenarios": scenario_results,
            "forecast_projections": base_result["forecast_projections"],
            "valuation": base_result["valuation"],
            "investment_metrics": base_result["investment_metrics"],
            "scenario_notes": ai_result["scenario_notes"],
            "assumptions": ai_result["assumptions_text"],
            "limitations": ai_result["limitations"],
            "current_multiple": current_multiple,
            "warnings": warnings,
            "model_note": (
                "Định giá chuyên sâu chỉ chạy khi tín hiệu vòng 1/L2 là MUA. "
                "Groq chỉ đề xuất giả định cho các kịch bản; giá mục tiêu, "
                "upside và CAGR được tính bằng công thức trong ứng dụng. "
                "Tín hiệu định giá không kết hợp với và không ghi đè tín hiệu L2."
            ),
        }

    @staticmethod
    def _insufficient(
        symbol: str,
        industry_type: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "status": "INSUFFICIENT_DATA",
            "symbol": symbol,
            "industry_type": industry_type,
            "message": message,
        }

    @staticmethod
    def _safe_year(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _industry_type(
        company_type: str,
        industry: str,
    ) -> str:
        normalized = industry.casefold()
        company = company_type.casefold().strip()

        # Bất động sản: chưa định giá nếu thiếu dữ liệu RNAV dự án.
        if any(term in normalized for term in (
            "bất động sản",
            "bat dong san",
            "real estate",
            "property",
        )):
            return "REAL_ESTATE"

        # Ngân hàng: chỉ dùng mô hình P/B khi nhận diện được ngân hàng.
        if company == "bank" or any(term in normalized for term in (
            "ngân hàng",
            "ngan hang",
            "commercial bank",
            "banking",
        )):
            return "BANK"

        # Không áp dụng mô hình P/B ngân hàng cho chứng khoán/bảo hiểm.
        if company in {
            "securities",
            "insurance",
            "financial",
        } or any(term in normalized for term in (
            "chứng khoán",
            "chung khoan",
            "bảo hiểm",
            "bao hiem",
            "securities",
            "insurance",
        )):
            return "UNSUPPORTED_FINANCIAL"

        if any(term in normalized for term in (
            "công nghệ",
            "cong nghe",
            "technology",
            "software",
            "information technology",
            "it ",
        )):
            return "TECH"

        if any(term in normalized for term in (
            "thép",
            "thep",
            "dầu khí",
            "dau khi",
            "phân bón",
            "phan bon",
            "hóa chất",
            "hoa chat",
            "commodity",
            "cyclical",
            "steel",
            "oil",
            "gas",
            "fertilizer",
            "chemical",
        )):
            return "CYCLICAL"

        return "NORMAL"

    @staticmethod
    def _validate_valuation_inputs(
        current_price: Optional[float],
        shares: Optional[float],
        base_revenue: Optional[float],
        base_equity: Optional[float],
        industry_type: str,
    ) -> Optional[str]:
        if current_price is None or current_price <= 0:
            return "Giá thị trường không hợp lệ."

        if shares is None or shares <= 0:
            return "Thiếu số cổ phiếu lưu hành hợp lệ."

        if industry_type == "BANK":
            if base_equity is None or base_equity <= 0:
                return "Thiếu vốn chủ sở hữu/BVPS hợp lệ để định giá P/B."
        else:
            if base_revenue is None or base_revenue <= 0:
                return "Thiếu doanh thu hợp lệ để định giá P/E."

        return None

    @classmethod
    def _generate_scenarios(
        cls,
        symbol: str,
        industry_type: str,
        industry: str,
        company_name: str,
        history: list[dict[str, Any]],
        current_price: float,
        shares: Optional[float],
        base_revenue: Optional[float],
        base_equity: Optional[float],
        current_roe: Optional[float],
        current_pe: Optional[float] = None,
        current_pb: Optional[float] = None,
    ) -> dict[str, Any]:
        try:
            from groq_service import available as groq_available
            if not groq_available():
                return {
                    "error": "AI_UNAVAILABLE",
                    "message": "Chưa cấu hình GROQ_API_KEY trong file .env.",
                }
        except Exception:
            return {
                "error": "AI_UNAVAILABLE",
                "message": "Không khởi tạo được Groq service.",
            }

        if industry_type == "BANK":
            instructions = (
                "Tạo roe_scenarios: mỗi kịch bản có đúng 3 số ROE dự phóng "
                "theo năm; retention_rate là tỷ lệ giữ lại lợi nhuận; "
                "pb_scenarios là P/B mục tiêu. Dựa trên dữ liệu lịch sử "
                "được cung cấp. Không tự tạo số liệu lịch sử. Lấy current_pb "
                "làm mốc cho pb_scenarios.base; nếu chọn khác, giải thích lý do."
            )
        else:
            instructions = (
                "Tạo growth_scenarios: mỗi kịch bản có đúng 3 số tăng trưởng "
                "doanh thu theo năm; margin_scenarios là biên lợi nhuận ròng; "
                "pe_scenarios là P/E mục tiêu. Dựa trên lịch sử tài chính và "
                "đặc điểm ngành. Không tự tạo số liệu lịch sử. Lấy current_pe "
                "làm mốc cho pe_scenarios.base; nếu chọn khác, giải thích lý do."
            )

        prompt_data = {
            "symbol": symbol,
            "company_name": company_name,
            "industry": industry,
            "industry_type": industry_type,
            "current_price_vnd": current_price,
            "shares_outstanding": shares,
            "base_revenue_vnd": base_revenue,
            "base_equity_vnd": base_equity,
            "current_roe": current_roe,
            "current_pe": current_pe,
            "current_pb": current_pb,
            "financial_history": history,
            "profit_basis": "npat = LNST thuộc cổ đông công ty mẹ khi nguồn có dữ liệu",
        }

        use_cache = bool(history)
        cache_payload = json.dumps(
            prompt_data,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()
        now = time.time()
        if use_cache:
            with cls._SCENARIO_CACHE_LOCK:
                cached = cls._SCENARIO_CACHE.get(cache_key)
                if cached and now - cached[0] < cls._SCENARIO_CACHE_TTL:
                    # JSON round-trip tạo bản sao sâu, tránh caller sửa cache.
                    return json.loads(json.dumps(cached[1], ensure_ascii=False, default=str))

        prompt = (
            "Bạn là trợ lý phân tích định giá cổ phiếu cho mục đích học tập. "
            "Chỉ trả về một JSON object hợp lệ, không kèm markdown.\n"
            "Tạo ba kịch bản: conservative (thận trọng), base (cơ sở), "
            "optimistic (lạc quan). Giả định phải có cơ sở, tránh khẳng định "
            "chắc chắn về tương lai.\n"
            f"{instructions}\n"
            "Tất cả tỷ lệ là số thập phân thuần túy, ví dụ 0.15 tương ứng 15%. "
            "Không trả tỷ lệ dưới dạng chuỗi, không kèm ký hiệu %. "
            "Các trường P/E và P/B là bội số số học dương.\n"
            "Schema bắt buộc cho mọi ngành:\n"
            "{"
            '"industry_type":"...",'
            '"scenario_notes":{'
            '"Thận trọng":"...",'
            '"Cơ sở":"...",'
            '"Lạc quan":"..."},'
            '"assumptions":"...",'
            '"limitations":"...",'
            '"growth_scenarios":{"conservative":[0.0,0.0,0.0],'
            '"base":[0.0,0.0,0.0],'
            '"optimistic":[0.0,0.0,0.0]},'
            '"margin_scenarios":{"conservative":0.0,'
            '"base":0.0,"optimistic":0.0},'
            '"pe_scenarios":{"conservative":1.0,'
            '"base":1.0,"optimistic":1.0},'
            '"roe_scenarios":{"conservative":[0.0,0.0,0.0],'
            '"base":[0.0,0.0,0.0],'
            '"optimistic":[0.0,0.0,0.0]},'
            '"retention_rate":0.0,'
            '"pb_scenarios":{"conservative":1.0,'
            '"base":1.0,"optimistic":1.0}'
            "}\n"
            "Chỉ điền các trường phù hợp với industry_type. "
            "Với ngân hàng, bắt buộc có roe_scenarios, retention_rate, "
            "pb_scenarios. Với ngành khác, bắt buộc có growth_scenarios, "
            "margin_scenarios, pe_scenarios. Mỗi trường kịch bản phải có "
            "đúng ba khóa conservative, base, optimistic.\n"
            "Giải thích ngắn gọn giả định và hạn chế. Không tự tạo dữ liệu "
            "thực tế không có trong đầu vào.\n"
            "Dữ liệu đầu vào:\n"
            + json.dumps(prompt_data, ensure_ascii=False, default=str)
        )

        try:
            from groq_service import json_completion

            generated = json_completion(
                prompt,
                primary_model=cls.MODEL_NAME,
                fallback_model=cls.FALLBACK_MODEL_NAME,
                max_tokens=2600,
                temperature=0.05,
            )
            if not isinstance(generated, dict):
                raise ValueError("Kết quả Groq phải là JSON object.")

            validated = cls._validate_scenario_data(generated, industry_type)
            result = {
                "assumptions": validated,
                "scenario_notes": generated["scenario_notes"],
                "assumptions_text": generated["assumptions"],
                "limitations": generated["limitations"],
            }
            if use_cache:
                with cls._SCENARIO_CACHE_LOCK:
                    cls._SCENARIO_CACHE[cache_key] = (now, result)
            return result

        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            log.exception("Invalid Groq valuation response for %s", symbol)
            return {
                "error": "AI_GENERATION_FAILED",
                "message": f"Groq trả về giả định định giá không hợp lệ: {exc}",
            }
        except Exception as exc:
            log.exception("Groq valuation generation failed for %s", symbol)
            try:
                from groq_service import describe_error
                message = describe_error(exc)
            except Exception:
                message = f"Groq API lỗi: {type(exc).__name__}: {str(exc)[:250]}"
            return {"error": "AI_GENERATION_FAILED", "message": message}

    @classmethod
    def _validate_scenario_data(
        cls,
        generated: dict[str, Any],
        industry_type: str,
    ) -> dict[str, Any]:
        """Kiểm tra cấu trúc, kiểu dữ liệu và giới hạn giả định Groq."""

        validated: dict[str, Any] = {}

        def require_scenario_keys(
            data: Any,
            field: str,
        ) -> dict[str, Any]:
            if not isinstance(data, dict):
                raise ValueError(
                    f"{field} phải là JSON object."
                )

            missing = [
                key for key in SCENARIO_KEYS
                if key not in data
            ]

            if missing:
                raise ValueError(
                    f"{field} thiếu kịch bản: {', '.join(missing)}."
                )

            return data

        def number(
            value: Any,
            field: str,
            minimum: float,
            maximum: float,
        ) -> float:
            # Không chấp nhận bool, chuỗi hoặc giá trị ngoài giới hạn.
            if isinstance(value, bool) or not isinstance(
                value, (int, float)
            ):
                raise ValueError(
                    f"{field} phải là số thập phân thuần túy; "
                    f"nhận được {value!r}."
                )

            result = float(value)

            if not (
                minimum <= result <= maximum
            ):
                raise ValueError(
                    f"{field}={result} nằm ngoài khoảng "
                    f"[{minimum}, {maximum}]."
                )

            return result

        if industry_type == "BANK":
            roe_data = require_scenario_keys(
                generated.get("roe_scenarios"),
                "roe_scenarios",
            )

            validated["roe_scenarios"] = {}

            for scenario_key in SCENARIO_KEYS:
                values = roe_data[scenario_key]

                if not isinstance(values, list) or len(values) != 3:
                    raise ValueError(
                        f"roe_scenarios.{scenario_key} "
                        "phải có đúng 3 năm."
                    )

                validated["roe_scenarios"][scenario_key] = [
                    number(
                        value,
                        f"roe_scenarios.{scenario_key}[{index}]",
                        -0.20,
                        0.60,
                    )
                    for index, value in enumerate(values)
                ]

            validated["retention_rate"] = number(
                generated.get("retention_rate"),
                "retention_rate",
                0.0,
                1.0,
            )

            pb_data = require_scenario_keys(
                generated.get("pb_scenarios"),
                "pb_scenarios",
            )

            validated["pb_scenarios"] = {
                scenario_key: number(
                    pb_data[scenario_key],
                    f"pb_scenarios.{scenario_key}",
                    0.1,
                    5.0,
                )
                for scenario_key in SCENARIO_KEYS
            }

        else:
            growth_data = require_scenario_keys(
                generated.get("growth_scenarios"),
                "growth_scenarios",
            )

            margin_data = require_scenario_keys(
                generated.get("margin_scenarios"),
                "margin_scenarios",
            )

            pe_data = require_scenario_keys(
                generated.get("pe_scenarios"),
                "pe_scenarios",
            )

            validated["growth_scenarios"] = {}
            validated["margin_scenarios"] = {}
            validated["pe_scenarios"] = {}

            for scenario_key in SCENARIO_KEYS:
                rates = growth_data[scenario_key]

                if not isinstance(rates, list) or len(rates) != 3:
                    raise ValueError(
                        f"growth_scenarios.{scenario_key} "
                        "phải có đúng 3 năm."
                    )

                validated["growth_scenarios"][scenario_key] = [
                    number(
                        value,
                        f"growth_scenarios.{scenario_key}[{index}]",
                        -0.50,
                        1.00,
                    )
                    for index, value in enumerate(rates)
                ]

                validated["margin_scenarios"][scenario_key] = number(
                    margin_data[scenario_key],
                    f"margin_scenarios.{scenario_key}",
                    0.0001,
                    1.0,
                )

                validated["pe_scenarios"][scenario_key] = number(
                    pe_data[scenario_key],
                    f"pe_scenarios.{scenario_key}",
                    1.0,
                    60.0,
                )

        # Kiểm tra thứ tự kịch bản giữa thận trọng, cơ sở, lạc quan.
        # Không bắt buộc tăng trưởng phải tăng đều qua từng năm.
        for field in (
            "margin_scenarios",
            "pe_scenarios",
            "pb_scenarios",
        ):
            if field not in validated:
                continue

            values = validated[field]

            if not (
                values["conservative"]
                <= values["base"]
                <= values["optimistic"]
            ):
                raise ValueError(
                    f"{field} phải theo thứ tự "
                    "thận trọng <= cơ sở <= lạc quan."
                )

        for field in (
            "growth_scenarios",
            "roe_scenarios",
        ):
            if field not in validated:
                continue

            values = validated[field]

            for index in range(3):
                conservative = values["conservative"][index]
                base = values["base"][index]
                optimistic = values["optimistic"][index]

                if not (
                    conservative <= base <= optimistic
                ):
                    raise ValueError(
                        f"{field}: giả định năm {index + 1} phải theo "
                        "thứ tự thận trọng <= cơ sở <= lạc quan."
                    )

        notes = generated.get("scenario_notes")

        if not isinstance(notes, dict):
            raise ValueError(
                "scenario_notes phải là JSON object."
            )

        validated_notes: dict[str, str] = {}

        for label in SCENARIOS.values():
            note = notes.get(label)

            if not isinstance(note, str) or not note.strip():
                raise ValueError(
                    f"Thiếu ghi chú cho kịch bản {label}."
                )

            validated_notes[label] = note.strip()[:600]

        generated["scenario_notes"] = validated_notes

        for field in ("assumptions", "limitations"):
            value = generated.get(field)

            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Thiếu trường {field}."
                )

            generated[field] = value.strip()[:1200]

        return validated

    @classmethod
    def _project_company_scenario(
        cls,
        key: str,
        assumptions: dict[str, Any],
        base_revenue: Optional[float],
        shares: Optional[float],
        base_year: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Dự phóng doanh thu, LNST, EPS và giá mục tiêu theo P/E."""

        if base_revenue is None or base_revenue <= 0:
            raise ValueError(
                "Thiếu doanh thu cơ sở để dự phóng."
            )

        if shares is None or shares <= 0:
            raise ValueError(
                "Thiếu số cổ phiếu để tính EPS."
            )

        growth_rates = assumptions["growth_scenarios"][key]
        margin = assumptions["margin_scenarios"][key]
        target_pe = assumptions["pe_scenarios"][key]

        projections: dict[str, Any] = {
            "metric_type": "eps",
        }

        previous_revenue = base_revenue

        for index, year_key in enumerate(YEAR_KEYS):
            growth = growth_rates[index]
            revenue = previous_revenue * (1 + growth)
            npat = revenue * margin

            if npat <= 0:
                raise ValueError(
                    "LNST dự phóng không dương, "
                    "không thể áp dụng mô hình P/E."
                )

            eps = npat / shares

            projections[year_key] = {
                "year": base_year + index + 1,
                "revenue": revenue,
                "npat": npat,
                "rev_growth_pct": growth * 100,
                "net_margin_pct": margin * 100,
                "eps": eps,
            }

            previous_revenue = revenue

        eps_y3 = projections["year_3"]["eps"]
        target_price = eps_y3 * target_pe

        return projections, {
            "valuation_method": (
                "P/E mục tiêu trên EPS dự phóng năm thứ 3"
            ),
            "target_multiplier": round(target_pe, 2),
            "multiplier_unit": "x P/E",
            "y3_metric_value": eps_y3,
            "target_price_y3": round(target_price, 0),
        }

    @classmethod
    def _project_bank_scenario(
        cls,
        key: str,
        assumptions: dict[str, Any],
        base_equity: Optional[float],
        shares: Optional[float],
        base_year: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Dự phóng vốn chủ, BVPS và giá mục tiêu theo P/B."""

        if base_equity is None or base_equity <= 0:
            raise ValueError(
                "Thiếu vốn chủ sở hữu để dự phóng."
            )

        if shares is None or shares <= 0:
            raise ValueError(
                "Thiếu số cổ phiếu để tính BVPS."
            )

        retention = assumptions["retention_rate"]
        roe_rates = assumptions["roe_scenarios"][key]

        equity = base_equity

        projections: dict[str, Any] = {
            "metric_type": "bvps",
        }

        for index, year_key in enumerate(YEAR_KEYS):
            roe = roe_rates[index]
            npat = equity * roe
            equity += npat * retention

            if equity <= 0:
                raise ValueError(
                    "Vốn chủ sở hữu dự phóng không dương, "
                    "không thể áp dụng mô hình P/B."
                )

            bvps = equity / shares

            projections[year_key] = {
                "year": base_year + index + 1,
                "revenue": None,
                "npat": npat,
                "roe_pct": roe * 100,
                "retention_rate_pct": retention * 100,
                "equity": equity,
                "bvps": bvps,
            }

        target_pb = assumptions["pb_scenarios"][key]
        bvps_y3 = projections["year_3"]["bvps"]
        target_price = bvps_y3 * target_pb

        return projections, {
            "valuation_method": (
                "P/B mục tiêu trên BVPS dự phóng năm thứ 3"
            ),
            "target_multiplier": round(target_pb, 2),
            "multiplier_unit": "x P/B",
            "y3_metric_value": bvps_y3,
            "target_price_y3": round(target_price, 0),
        }

    @staticmethod
    def _calculate_metrics(
        target_price: float,
        current_price: float,
    ) -> dict[str, float]:
        if current_price <= 0:
            raise ValueError(
                "Giá thị trường phải lớn hơn 0."
            )

        cumulative_return = target_price / current_price - 1

        cagr = (
            (1 + cumulative_return) ** (1 / 3) - 1
            if 1 + cumulative_return > 0
            else -1.0
        )

        return {
            "target_price_y3": round(target_price, 0),
            "upside_pct": round(cumulative_return * 100, 1),
            "cagr_pct": round(cagr * 100, 1),
        }

    @staticmethod
    def _independent_valuation_signal(
        base_upside: float,
        minimum_upside: float,
    ) -> tuple[str, str]:
        upside_pct = round(base_upside * 100, 1)

        if base_upside >= 0.25:
            return (
                "STRONG BUY",
                f"Upside kịch bản cơ sở đạt {upside_pct}%; "
                "vượt ngưỡng 25%.",
            )

        if base_upside >= minimum_upside:
            return (
                "ACCUMULATE / BUY",
                f"Upside kịch bản cơ sở đạt {upside_pct}%; "
                f"đạt ngưỡng ngành {minimum_upside:.0%}.",
            )

        if base_upside >= 0:
            return (
                "HOLD",
                f"Upside kịch bản cơ sở đạt {upside_pct}%; "
                f"thấp hơn ngưỡng ngành {minimum_upside:.0%}.",
            )

        return (
            "SELL / TAKE PROFIT",
            f"Upside âm ({upside_pct}%); giá thị trường cao hơn "
            "giá mục tiêu theo mô hình.",
        )

    @staticmethod
    def _positive_value(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None

        try:
            result = float(value)
        except (TypeError, ValueError):
            return None

        if result <= 0:
            return None

        return result

    @staticmethod
    def _number_value(value: Any) -> Optional[float]:
        """Đọc số có thể âm, dùng cho các chỉ tiêu như ROE."""
        if isinstance(value, bool):
            return None

        try:
            result = float(value)
        except (TypeError, ValueError):
            return None

        return result
