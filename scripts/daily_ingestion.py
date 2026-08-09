"""Update the browser snapshot from the NSE Indices daily snapshot and notify Telegram."""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from curl_cffi import requests

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "market-data.json"
sys.path.insert(0, str(ROOT))

from scripts.export_market_data import (  # noqa: E402
    classify,
    dow_signals,
    identify_patterns,
    yearly_analysis,
)


ALIASES = {
    "NIFTY TOTAL MARKET": ["NIFTY TOTAL MKT"],
    "NIFTY MIDCAP SELECT": ["NIFTY MID SELECT"],
    "NIFTY MICROCAP 250": ["NIFTY MICROCAP250"],
    "NIFTY SMALLCAP 250": ["NIFTY SMLCAP 250"],
    "NIFTY SMALLCAP 50": ["NIFTY SMLCAP 50"],
    "NIFTY PRIVATE BANK": ["NIFTY PVT BANK"],
    "NIFTY FINANCIAL SERVICES": ["NIFTY FIN SERVICE"],
    "NIFTY FINANCIAL SERVICES EX-BANK": ["NIFTY FINSEREXBNK"],
    "NIFTY CONSUMER DURABLES": ["NIFTY CONSR DURBL"],
    "NIFTY INDIA MANUFACTURING": ["NIFTY INDIA MFG"],
    "NIFTY INFRASTRUCTURE": ["NIFTY INFRA"],
    "NIFTY OIL & GAS": ["NIFTY OIL AND GAS"],
}

MARKET_CAP_BASE_DATE = "2026-03-31"
MARKET_CAP_BASE_LAKH_CRORE = 412.43
GDP_BASE_DATE = "2025-03-31"
GDP_BASE_USD_TRILLION = 3.96
GDP_BASE_USD_INR = 85.47
GDP_REAL_GROWTH_RATE = 0.066
GDP_INFLATION_RATE = 0.049
GDP_FORECAST_PERIOD = "FY2026-27"
GDP_ESTIMATE_SOURCE = "World Bank India Development Update, April 2026"
GDP_ESTIMATE_SOURCE_URL = "https://thedocs.worldbank.org/en/doc/4262e1e15b463ecb360cec4ad78cf062-0310012026/original/April-2026-India-Development-Update.pdf"
BSE_URL = "https://m.bseindia.com/"
CCIL_URL = "https://www.ccilindia.com/web/ccil/tenorwise-indicative-yields"


def fetch_snapshot() -> dict[str, dict]:
    session = requests.Session(impersonate="chrome131")
    page_url = "https://www.niftyindices.com/reports/historical-data"
    page = session.get(page_url, timeout=30)
    page.raise_for_status()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": page_url,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    result = None
    for days_back in range(7):
        candidate = datetime.now() - timedelta(days=days_back)
        response = session.post(
            f"{page_url}/Index/",
            json={
                "SelectedReportType": "1",
                "SelectedDate": candidate.strftime("%m/%d/%Y"),
                "MonthYear": candidate.strftime("%b %Y"),
            },
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("success") and result.get("data"):
            break
    if not result or not result.get("success") or not result.get("data"):
        raise RuntimeError(
            (result or {}).get("message") or "NSE snapshot returned no recent download link"
        )
    csv_url = urljoin("https://www.niftyindices.com", result["data"][0]["DownloadLink"])
    download = session.get(csv_url, timeout=30)
    download.raise_for_status()

    records: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(download.text)):
        try:
            date = datetime.strptime(row["Index Date"].strip(), "%d-%m-%Y")
            close = float(row["Closing Index Value"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_pe = (row.get("P/E") or "").strip()
        try:
            pe = float(raw_pe) if raw_pe not in {"", "-", "nan"} else None
        except ValueError:
            pe = None
        records[row["Index Name"].strip().upper()] = {
            "date": date.strftime("%Y-%m-%d"),
            "close": round(close, 2),
            "pe": round(pe, 2) if pe and pe > 0 else None,
        }
    if not records:
        raise RuntimeError("NSE snapshot CSV contained no usable index records")
    return records


def candidates(name: str) -> list[str]:
    names = [name, *ALIASES.get(name, [])]
    expanded: list[str] = []
    for candidate in names:
        expanded.extend([candidate, f"{candidate} INDEX"])
        if " & " in candidate:
            expanded.append(candidate.replace(" & ", " AND "))
        if " AND " in candidate:
            expanded.append(candidate.replace(" AND ", " & "))
    return list(dict.fromkeys(value.upper() for value in expanded))


def fetch_macro_snapshot() -> dict[str, float | str]:
    session = requests.Session(impersonate="chrome131")
    headers = {"User-Agent": "Mozilla/5.0"}
    bse_response = session.get(BSE_URL, headers=headers, timeout=30)
    bse_response.raise_for_status()
    market_cap_match = re.search(r"Market Capitalization of BSE Listed Companies.*?TTRow_right'>([\d,]+)<", bse_response.text, re.DOTALL)
    market_date_match = re.search(r'id="msdate"[^>]*>As on\s+([^|<]+)', bse_response.text)
    if not market_cap_match or not market_date_match:
        raise RuntimeError("BSE market capitalization was not found")
    market_date = datetime.strptime(market_date_match.group(1).strip(), "%d %b %y").strftime("%Y-%m-%d")
    ccil_response = session.get(CCIL_URL, headers=headers, timeout=30)
    ccil_response.raise_for_status()
    yield_match = re.search(r">(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})(?: 00:00:00\.0)?</td>\s*<td[^>]*>9Y-10Y</td>\s*<td[^>]*>[^<]+</td>\s*<td[^>]*>([\d.]+)</td>", ccil_response.text)
    if not yield_match:
        raise RuntimeError("CCIL 9Y-10Y government-security yield was not found")
    yield_date = yield_match.group(1)
    if yield_date[2] == "-":
        yield_date = datetime.strptime(yield_date, "%d-%m-%Y").strftime("%Y-%m-%d")
    return {"marketCapCrore": float(market_cap_match.group(1).replace(",", "")), "marketCapDate": market_date, "bondYield": float(yield_match.group(2)), "bondYieldDate": yield_date}


def update_macro(payload: dict, snapshot: dict[str, float | str]) -> None:
    """Apply daily BSE/CCIL data and project GDP from the World Bank USD base."""
    macro = payload.get("macro")
    if not isinstance(macro, dict):
        return
    market_cap_crore = float(snapshot["marketCapCrore"])
    market_cap_lakh_crore = market_cap_crore / 100_000
    market_date = str(snapshot["marketCapDate"])
    elapsed_days = max(0, (datetime.fromisoformat(market_date) - datetime.fromisoformat(GDP_BASE_DATE)).days)
    gdp_base_lakh_crore = GDP_BASE_USD_TRILLION * GDP_BASE_USD_INR
    nominal_growth_rate = (1 + GDP_REAL_GROWTH_RATE) * (1 + GDP_INFLATION_RATE) - 1
    gdp_lakh_crore = gdp_base_lakh_crore * (1 + nominal_growth_rate) ** (elapsed_days / 365.2425)
    macro.update(
        {
            "marketCapCrore": round(market_cap_crore, 2),
            "marketCapLakhCrore": round(market_cap_lakh_crore, 2),
            "marketCapDate": market_date,
            "marketCapBaseLakhCrore": MARKET_CAP_BASE_LAKH_CRORE,
            "marketCapBaseDate": MARKET_CAP_BASE_DATE,
            "marketCapMethod": "BSE listed-company market capitalization",
            "gdpLakhCrore": round(gdp_lakh_crore, 2),
            "gdpDate": market_date,
            "gdpBaseUsdTrillion": GDP_BASE_USD_TRILLION,
            "gdpBaseUsdInr": GDP_BASE_USD_INR,
            "gdpBaseLakhCrore": round(gdp_base_lakh_crore, 2),
            "gdpBaseDate": GDP_BASE_DATE,
            "gdpRealGrowthRate": round(GDP_REAL_GROWTH_RATE * 100, 1),
            "gdpInflationRate": round(GDP_INFLATION_RATE * 100, 1),
            "gdpNominalGrowthRate": round(nominal_growth_rate * 100, 2),
            "gdpForecastPeriod": GDP_FORECAST_PERIOD,
            "gdpEstimateSource": GDP_ESTIMATE_SOURCE,
            "gdpEstimateSourceUrl": GDP_ESTIMATE_SOURCE_URL,
            "gdpMethod": "World Bank 2025 USD GDP converted at base-date FX and compounded using the World Bank FY2026-27 real-growth and inflation forecasts",
            "buffettRatio": round(market_cap_lakh_crore / gdp_lakh_crore * 100, 2),
            "bondYieldDate": str(snapshot["bondYieldDate"]),
        }
    )
    payload["bondYield"] = round(float(snapshot["bondYield"]), 4)


def update_payload(payload: dict, nse: dict[str, dict], macro_snapshot: dict[str, float | str]) -> tuple[list[str], list[str], list[str]]:
    updated: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []
    update_macro(payload, macro_snapshot)
    bond_yield = float(payload.get("bondYield") or 6.8)

    for item in payload["instruments"]:
        record = next((nse[key] for key in candidates(item["name"]) if key in nse), None)
        if not record:
            missing.append(item["name"])
            continue
        history = item["history"]
        existing = next((point for point in reversed(history) if point["date"] == record["date"]), None)
        if existing:
            if existing.get("close") == record["close"] and existing.get("pe") == record["pe"]:
                unchanged.append(item["name"])
            else:
                existing.update(record)
                updated.append(item["name"])
        else:
            history.append(record)
            history.sort(key=lambda point: point["date"])
            updated.append(item["name"])

        weekly = item["weekly"]
        record_date = datetime.fromisoformat(record["date"])
        if record_date.weekday() == 4:
            weekly_existing = next(
                (point for point in reversed(weekly) if point["date"] == record["date"]),
                None,
            )
            if weekly_existing:
                weekly_existing.update(record)
            else:
                weekly.append(record.copy())
                weekly.sort(key=lambda point: point["date"])

        prices = [float(point["close"]) for point in history]
        latest = history[-1]
        previous = history[-2] if len(history) > 1 else latest
        trend, phase, score = classify(prices)
        patterns = identify_patterns(weekly[-160:], float(item.get("depthPct", 0.04)))
        item.update(
            {
                "asOf": latest["date"],
                "close": latest["close"],
                "pe": latest["pe"],
                "change": round((latest["close"] / previous["close"] - 1) * 100, 2),
                "trend": trend,
                "phase": phase,
                "score": score,
                "analysis": yearly_analysis(history),
                "patterns": patterns,
                "dow": dow_signals(latest["close"], latest["pe"], patterns, bond_yield),
            }
        )

    payload["generatedAt"] = datetime.now(UTC).isoformat()
    payload["source"] = "NSE Indices Daily Snapshot"
    return updated, unchanged, missing


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets are not configured; notification skipped.")
        return
    body = json.dumps(
        {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    ).encode()
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        if response.status >= 300:
            raise RuntimeError(f"Telegram returned HTTP {response.status}")


def main() -> None:
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        nse = fetch_snapshot()
        macro_snapshot = fetch_macro_snapshot()
        updated, unchanged, missing = update_payload(payload, nse, macro_snapshot)
        SNAPSHOT_PATH.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        latest_date = max(record["date"] for record in nse.values())
        message = (
            "✅ <b>Daily NSE index update complete</b>\n"
            f"Date: <b>{html.escape(latest_date)}</b>\n"
            f"Updated: <b>{len(updated)}</b>\n"
            f"Already current: <b>{len(unchanged)}</b>\n"
            f"Missing from snapshot: <b>{len(missing)}</b>"
        )
        if missing:
            message += "\nMissing: " + html.escape(", ".join(missing[:12]))
        print(
            f"Daily NSE index update complete: date={latest_date}, "
            f"updated={len(updated)}, current={len(unchanged)}, missing={len(missing)}"
        )
        send_telegram(message)
    except Exception as exc:
        message = (
            "❌ <b>Daily NSE index update failed</b>\n"
            f"<code>{html.escape(str(exc))}</code>"
        )
        try:
            send_telegram(message)
        finally:
            raise


if __name__ == "__main__":
    main()
