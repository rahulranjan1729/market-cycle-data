"""Run one independent market-data feed and publish an observable status record."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from curl_cffi import requests

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "market-data.json"
STATUS_PATH = ROOT / "data-status.json"
sys.path.insert(0, str(ROOT))

from scripts.daily_ingestion import (  # noqa: E402
    BSE_URL,
    CCIL_URL,
    GDP_BASE_DATE,
    GDP_BASE_USD_INR,
    GDP_BASE_USD_TRILLION,
    GDP_ESTIMATE_SOURCE,
    GDP_ESTIMATE_SOURCE_URL,
    GDP_FORECAST_PERIOD,
    GDP_INFLATION_RATE,
    GDP_REAL_GROWTH_RATE,
    MARKET_CAP_BASE_DATE,
    MARKET_CAP_BASE_LAKH_CRORE,
    RBI_URL,
    candidates,
    fetch_snapshot,
)
from scripts.export_market_data import classify, dow_signals, identify_patterns, yearly_analysis  # noqa: E402


def load_status() -> dict:
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    return {"feeds": {}}


def save_status(feed: str, state: str, data_date: str | None, message: str) -> None:
    status = load_status()
    previous = status.setdefault("feeds", {}).get(feed, {})
    attempt = datetime.now(UTC).isoformat()
    entry = {**previous, "status": state, "lastAttemptAt": attempt, "message": message}
    if state == "success":
        entry.update({"lastSuccessAt": attempt, "dataDate": data_date})
    status["feeds"][feed] = entry
    status["updatedAt"] = attempt
    STATUS_PATH.write_text(json.dumps(status, separators=(",", ":")), encoding="utf-8")


def update_indices(payload: dict) -> tuple[bool, str, str]:
    nse = fetch_snapshot()
    updated = unchanged = missing = 0
    bond_yield = float(payload.get("bondYield") or 6.8)
    for item in payload["instruments"]:
        record = next((nse[key] for key in candidates(item["name"]) if key in nse), None)
        if not record:
            missing += 1
            continue
        history = item["history"]
        existing = next((point for point in reversed(history) if point["date"] == record["date"]), None)
        if existing and existing.get("close") == record["close"] and existing.get("pe") == record["pe"]:
            unchanged += 1
        elif existing:
            existing.update(record)
            updated += 1
        else:
            history.append(record)
            history.sort(key=lambda point: point["date"])
            updated += 1
        weekly = item["weekly"]
        if datetime.fromisoformat(record["date"]).weekday() == 4:
            weekly_existing = next((point for point in reversed(weekly) if point["date"] == record["date"]), None)
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
        item.update({"asOf": latest["date"], "close": latest["close"], "pe": latest["pe"], "change": round((latest["close"] / previous["close"] - 1) * 100, 2), "trend": trend, "phase": phase, "score": score, "analysis": yearly_analysis(history), "patterns": patterns, "dow": dow_signals(latest["close"], latest["pe"], patterns, bond_yield)})
    data_date = max(record["date"] for record in nse.values())
    return updated > 0, data_date, f"updated={updated}, duplicate={unchanged}, missing={missing}"


def fetch_bond() -> dict:
    response = requests.Session(impersonate="chrome131").get(CCIL_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    match = re.search(r">(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})(?: 00:00:00\.0)?</td>\s*<td[^>]*>9Y-10Y</td>\s*<td[^>]*>[^<]+</td>\s*<td[^>]*>([\d.]+)</td>", response.text)
    if not match:
        raise RuntimeError("CCIL 9Y-10Y government-security yield was not found")
    date = match.group(1)
    if date[2] == "-":
        date = datetime.strptime(date, "%d-%m-%Y").strftime("%Y-%m-%d")
    return {"date": date, "value": round(float(match.group(2)), 4)}


def update_bond(payload: dict) -> tuple[bool, str, str]:
    result = fetch_bond()
    macro = payload.get("macro", {})
    changed = payload.get("bondYield") != result["value"] or macro.get("bondYieldDate") != result["date"]
    payload["bondYield"] = result["value"]
    macro["bondYieldDate"] = result["date"]
    if changed:
        for item in payload.get("instruments", []):
            item["dow"] = dow_signals(item["close"], item.get("pe"), item.get("patterns", []), result["value"])
    return changed, result["date"], f"yield={result['value']}%"


def update_market_cap(payload: dict) -> tuple[bool, str, str]:
    session = requests.Session(impersonate="chrome131")
    headers = {"User-Agent": "Mozilla/5.0"}
    bse = session.get(BSE_URL, headers=headers, timeout=30)
    bse.raise_for_status()
    cap_match = re.search(r"Market Capitalization of BSE Listed Companies.*?TTRow_right'>([\d,]+)<", bse.text, re.DOTALL)
    date_match = re.search(r'id="msdate"[^>]*>As on\s+([^|<]+)', bse.text)
    if not cap_match or not date_match:
        raise RuntimeError("BSE market capitalization was not found")
    data_date = datetime.strptime(date_match.group(1).strip(), "%d %b %y").strftime("%Y-%m-%d")
    rbi = session.get(RBI_URL, headers=headers, timeout=30)
    rbi.raise_for_status()
    fx_match = re.search(r"INR\s*/\s*1 USD.*?<td[^>]*>\s*:\s*([\d.]+)", rbi.text, re.DOTALL)
    fx_date_match = re.search(r"As at .*? of ([A-Za-z]+ \d{2}, \d{4})", rbi.text)
    if not fx_match or not fx_date_match:
        raise RuntimeError("RBI USD/INR reference rate was not found")
    cap = float(cap_match.group(1).replace(",", ""))
    fx = float(fx_match.group(1))
    fx_date = datetime.strptime(fx_date_match.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    macro = payload["macro"]
    before = json.dumps(macro, sort_keys=True)
    nominal = (1 + GDP_REAL_GROWTH_RATE) * (1 + GDP_INFLATION_RATE) - 1
    elapsed = max(0, (datetime.fromisoformat(data_date) - datetime.fromisoformat(GDP_BASE_DATE)).days)
    gdp_usd = GDP_BASE_USD_TRILLION * (1 + nominal) ** (elapsed / 365.2425)
    gdp_inr = gdp_usd * fx
    macro.update({"marketCapCrore": round(cap, 2), "marketCapLakhCrore": round(cap / 100_000, 2), "marketCapDate": data_date, "marketCapBaseLakhCrore": MARKET_CAP_BASE_LAKH_CRORE, "marketCapBaseDate": MARKET_CAP_BASE_DATE, "marketCapMethod": "BSE listed-company market capitalization", "gdpLakhCrore": round(gdp_inr, 2), "gdpDate": data_date, "gdpBaseUsdTrillion": GDP_BASE_USD_TRILLION, "gdpBaseUsdInr": GDP_BASE_USD_INR, "gdpBaseDate": GDP_BASE_DATE, "gdpUsdTrillion": round(gdp_usd, 3), "usdInrRate": round(fx, 4), "usdInrDate": fx_date, "gdpRealGrowthRate": round(GDP_REAL_GROWTH_RATE * 100, 1), "gdpInflationRate": round(GDP_INFLATION_RATE * 100, 1), "gdpNominalGrowthRate": round(nominal * 100, 2), "gdpForecastPeriod": GDP_FORECAST_PERIOD, "gdpEstimateSource": GDP_ESTIMATE_SOURCE, "gdpEstimateSourceUrl": GDP_ESTIMATE_SOURCE_URL, "gdpMethod": "World Bank 2025 USD GDP compounded using FY2026-27 real-growth and inflation forecasts, then converted at the latest RBI USD/INR reference rate", "buffettRatio": round((cap / 100_000) / gdp_inr * 100, 2)})
    return before != json.dumps(macro, sort_keys=True), data_date, f"marketCapCrore={cap:.2f}, USD/INR={fx:.4f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed", required=True, choices=("indices", "bond", "market-cap"))
    feed = parser.parse_args().feed
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        changed, data_date, message = {"indices": update_indices, "bond": update_bond, "market-cap": update_market_cap}[feed](payload)
        if changed:
            payload["generatedAt"] = datetime.now(UTC).isoformat()
            SNAPSHOT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        save_status(feed, "success", data_date, ("updated; " if changed else "duplicate ignored; ") + message)
        print(f"{feed}: {message}")
    except Exception as exc:
        save_status(feed, "failure", None, str(exc))
        raise


if __name__ == "__main__":
    main()
