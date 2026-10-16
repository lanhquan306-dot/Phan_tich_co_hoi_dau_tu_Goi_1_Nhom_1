# Groq migration v4

1. Open `.env` and set `GROQ_API_KEY=<your Groq key>`.
2. Defaults: fast/news/company = `openai/gpt-oss-20b`; valuation = `openai/gpt-oss-120b`.
3. Run `python check_groq.py`, then `python web_app.py`.
4. Gemini is no longer used by runtime analysis services.
5. Legacy `Vnstock().stock(...)` fallbacks were removed. Quote adapter prefers `vnstock.api.quote.Quote`.
6. PDF export reuses the dashboard snapshot for up to 15 minutes, caches PDF bytes for 5 minutes and chart images for 5 minutes.
