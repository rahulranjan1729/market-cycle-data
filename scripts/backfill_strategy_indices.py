"""Backfill the approved strategy and diversified indices from official NSE history."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from curl_cffi import requests

from export_market_data import classify, dow_signals, identify_patterns, yearly_analysis


HISTORY_PAGE = "https://www.niftyindices.com/reports/historical-data"
PRICE_ENDPOINT = "https://www.niftyindices.com/BackPage/getHistoricaldatatabletoString"
PE_ENDPOINT = "https://www.niftyindices.com/BackPage/getpepbHistoricaldataDBtoString"
GROUP_NAME = "Strategy & Diversified Indices"
INDICES = {
    "NIFTY MNC": ("Nifty MNC", 1996),
    "NIFTY SERVICES SECTOR": ("Nifty Services Sector", 2001),
    "NIFTY DIVIDEND OPPORTUNITIES 50": ("Nifty Dividend Opportunities 50", 2011),
    "NIFTY ALPHA 50": ("Nifty Alpha 50", 2012),
    "NIFTY LOW VOLATILITY 50": ("Nifty Low Volatility 50", 2012),
    "NIFTY50 VALUE 20": ("Nifty50 Value 20", 2014),
    "NIFTY100 QUALITY 30": ("Nifty100 Quality 30", 2015),
    "NIFTY50 EQUAL WEIGHT": ("NIFTY50 Equal Weight", 2017),
    "NIFTY GROWTH SECTORS 15": ("Nifty Growth Sectors 15", 2014),
}


def fetch_year(index_name: str, year: int, endpoint: str) -> list[dict]:
    session = requests.Session(impersonate="chrome131")
    session.get(HISTORY_PAGE, timeout=30).raise_for_status()
    cinfo = (
        "{'name':'" + index_name.upper() + "','startDate':'01-Jan-" + str(year)
        + "','endDate':'31-Dec-" + str(year) + "','indexName':'" + index_name + "'}"
    )
    response = session.post(
        endpoint,
        json={"cinfo": cinfo},
        headers={"Referer": HISTORY_PAGE, "X-Requested-With": "XMLHttpRequest"},
        timeout=45,
    )
    response.raise_for_status()
    result = response.json() if response.text.strip() else []
    if isinstance(result, dict) and "d" in result:
        result = json.loads(result["d"]) if isinstance(result["d"], str) else result["d"]
    return result or []


def parse_date(value: str) -> str:
    return datetime.strptime(value.strip(), "%d %b %Y").strftime("%Y-%m-%d")


def load_index(index_name: str, first_year: int, final_year: int) -> list[dict]:
    tasks = [(year, endpoint) for year in range(first_year, final_year + 1) for endpoint in (PRICE_ENDPOINT, PE_ENDPOINT)]
    prices: dict[str, float] = {}
    pe_values: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_year, index_name, year, endpoint): endpoint for year, endpoint in tasks}
        for future in as_completed(futures):
            endpoint = futures[future]
            for row in future.result():
                try:
                    date = parse_date(row.get("HistoricalDate") or row.get("DATE"))
                    if endpoint == PRICE_ENDPOINT:
                        close = float(row["CLOSE"])
                        if close > 0:
                            prices[date] = close
                    else:
                        pe = float(row["pe"])
                        if pe > 0:
                            pe_values[date] = pe
                except (KeyError, TypeError, ValueError):
                    continue
    return [
        {"date": date, "close": round(close, 2), "pe": round(pe_values[date], 2) if date in pe_values else None}
        for date, close in sorted(prices.items())
    ]


def weekly_points(history: list[dict]) -> list[dict]:
    weeks: dict[tuple[int, int], dict] = {}
    for point in history:
        date = datetime.strptime(point["date"], "%Y-%m-%d")
        iso = date.isocalendar()
        weeks[(iso.year, iso.week)] = point
    return list(weeks.values())


def instrument(name: str, history: list[dict], bond_yield: float) -> dict:
    if len(history) < 2:
        raise RuntimeError(f"NSE returned insufficient history for {name}")
    prices = [float(point["close"]) for point in history]
    latest, previous = history[-1], history[-2]
    weekly = weekly_points(history)
    patterns = identify_patterns(weekly[-160:], 0.04)
    trend, phase, score = classify(prices)
    return {
        "name": name,
        "asOf": latest["date"],
        "close": latest["close"],
        "pe": latest["pe"],
        "change": round((latest["close"] / previous["close"] - 1) * 100, 2),
        "trend": trend,
        "phase": phase,
        "score": score,
        "depthPct": 0.04,
        "history": history,
        "analysis": yearly_analysis(history),
        "weekly": weekly,
        "patterns": patterns,
        "dow": dow_signals(latest["close"], latest["pe"], patterns, bond_yield),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=Path("market-data.json"))
    parser.add_argument("--through-year", type=int, default=datetime.now().year)
    arguments = parser.parse_args()
    payload = json.loads(arguments.file.read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in payload["instruments"]}
    for name, (official_name, first_year) in INDICES.items():
        history = load_index(official_name, first_year, arguments.through_year)
        by_name[name] = instrument(name, history, float(payload.get("bondYield") or 6.8))
        print(f"Backfilled {name}: {history[0]['date']} to {history[-1]['date']} ({len(history)} prices)")
    payload["instruments"] = sorted(by_name.values(), key=lambda item: item["name"])
    payload.setdefault("groups", {})[GROUP_NAME] = list(INDICES)
    arguments.file.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
