"""Offline checks for the final assignment-facing additions."""
from pathlib import Path

import investment_analysis as ia
import annual_report_service
import report_pdf_new as report_pdf
from investment_selftests import full_fund, full_tech


def test_industry_classification_metadata():
    assert ia.company_type("ZZZ", "Banking", "") == "bank"
    assert ia.company_type("ZZZ", "Chứng khoán", "") == "securities"
    assert ia.company_type("ZZZ", "Insurance", "") == "insurance"
    assert ia.company_type("ZZZ", "Financials", "") == "financial"
    assert ia.company_type("ZZZ", "Technology", "") == "regular"
    assert "de" not in ia._weights("financial")


def test_dashboard_sections_exist():
    text = Path("templates/investment_dashboard.html").read_text(encoding="utf-8")
    for phrase in (
        "Thông tin doanh nghiệp", "Tin tức doanh nghiệp", "Kiểm thử & So sánh",
        "Bảng nguồn dữ liệu", "/api/validate", "Backtest 1 năm", "buy_checks",
        "VN-Index", "SKIPPED", "Giả định Groq",
    ):
        assert phrase in text, phrase


def test_arminer_provenance_is_conservative():
    st = annual_report_service.status()
    assert "available" in st and "enabled" in st and st["source_name"].startswith("vn-annual-report-miner")
    if not st["enabled"]:
        lookup = annual_report_service.catalog_lookup("FPT")
        assert lookup["used"] is False


def test_pdf_source_table():
    fund = full_fund(); fund.update({"latest_period":"Q2/2026","source":"TEST","comments":{"growth":[],"profitability_leverage":[],"valuation":[]},"history":[]})
    tech = full_tech(); tech.update({"close":100000,"ma20":99000,"ma50":95000,"date":"2026-10-08","vol_ratio":1.2})
    sc=ia._score_all(fund,tech); sig=ia._signal_from_score(sc,fund,tech)
    r={"symbol":"AAA","company_name":"Demo","industry":"Technology","exchange":"HOSE","updated_at":"10:00 09/10/2026","fundamental":fund,"technical":tech,"score":sc,"signal":sig,"risk_level":"Trung bình","assessment":"Demo","strengths":[],"weaknesses":[],"risks":[],"missing_data":[],"methodology":ia._methodology("regular"),"disclaimer":"Demo","company_info":{},"news":[],"annual_report_source":{"note":"not used"},"data_sources":[{"data":"Giá","source":"TEST","period":"08/10/2026","retrieved_at":"09/10/2026","note":"VND"}]}
    pdf=report_pdf.build_report_pdf(r,include_chart=False)
    assert pdf.startswith(b"%PDF") and len(pdf)>10000


if __name__ == "__main__":
    tests=[test_industry_classification_metadata,test_dashboard_sections_exist,test_arminer_provenance_is_conservative,test_pdf_source_table]
    for fn in tests:
        fn(); print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)} nhóm final)")
