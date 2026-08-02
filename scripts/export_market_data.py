"""Export the retained market database into a browser-safe JSON snapshot."""

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


RETIRED = {
    "S&P 500", "Nasdaq 50", "Gold", "Silver",
    "NIFTY TRI", "NIFTY TOTAL MARKET", "NIFTY MIDCAP SELECT", "NIFTY MICROCAP 250",
    "MIDCAP SELECT", "MICROCAP 250",
}


def identify_patterns(
    history: list[dict[str, float | str | None]],
    depth_pct: float,
    min_days: int = 31,
) -> list[dict]:
    if len(history) < 3:
        return []
    prices = [float(point["close"]) for point in history]
    dates = [datetime.fromisoformat(str(point["date"])) for point in history]
    found: list[dict] = []

    for is_mountain in (True, False):
        for b_index in range(1, len(prices) - 1):
            if is_mountain:
                if not (prices[b_index] >= prices[b_index - 1] and prices[b_index] >= prices[b_index + 1]):
                    continue
                if prices[b_index] == prices[b_index - 1] == prices[b_index + 1]:
                    continue
            else:
                if not (prices[b_index] <= prices[b_index - 1] and prices[b_index] <= prices[b_index + 1]):
                    continue
                if prices[b_index] == prices[b_index - 1] == prices[b_index + 1]:
                    continue

            valid_pairs = []
            for a_index in range(b_index - 1, -1, -1):
                if is_mountain and prices[a_index] > prices[b_index]:
                    break
                if not is_mountain and prices[a_index] < prices[b_index]:
                    break
                for c_index in range(b_index + 1, len(prices)):
                    if is_mountain and prices[c_index] > prices[b_index]:
                        break
                    if not is_mountain and prices[c_index] < prices[b_index]:
                        break
                    intersection = (
                        max(prices[a_index], prices[c_index])
                        if is_mountain
                        else min(prices[a_index], prices[c_index])
                    )
                    span = prices[a_index + 1 : c_index]
                    if is_mountain and span and min(span) < intersection:
                        continue
                    if not is_mountain and span and max(span) > intersection:
                        continue
                    depth_valid = (
                        prices[b_index] >= intersection * (1 + depth_pct)
                        if is_mountain
                        else prices[b_index] <= intersection / (1 + depth_pct)
                    )
                    duration = (dates[c_index] - dates[a_index]).days
                    if depth_valid and duration >= min_days:
                        valid_pairs.append((a_index, c_index, intersection))
            if valid_pairs:
                best = (
                    max(valid_pairs, key=lambda pair: pair[2])
                    if is_mountain
                    else min(valid_pairs, key=lambda pair: pair[2])
                )
                a_index, c_index, intersection = best
                found.append(
                    {
                        "type": "Top" if is_mountain else "Bottom",
                        "a": {"date": history[a_index]["date"], "price": round(intersection, 2), "index": a_index},
                        "b": {"date": history[b_index]["date"], "price": round(prices[b_index], 2), "index": b_index},
                        "c": {"date": history[c_index]["date"], "price": round(intersection, 2), "index": c_index},
                    }
                )

    unique = []
    for pattern in found:
        enveloped = False
        for other in found:
            if pattern is other or pattern["type"] != other["type"]:
                continue
            if other["a"]["index"] <= pattern["b"]["index"] <= other["c"]["index"]:
                if pattern["type"] == "Top" and pattern["b"]["price"] < other["b"]["price"]:
                    enveloped = True
                if pattern["type"] == "Bottom" and pattern["b"]["price"] > other["b"]["price"]:
                    enveloped = True
        if not enveloped:
            unique.append(pattern)
    by_peak = {}
    for pattern in unique:
        by_peak[pattern["b"]["index"]] = pattern
    return sorted(by_peak.values(), key=lambda pattern: pattern["b"]["index"])


def dow_signals(
    current_price: float,
    current_pe: float | None,
    patterns: list[dict],
    bond_yield: float,
) -> dict:
    last_top = next((item for item in reversed(patterns) if item["type"] == "Top"), None)
    last_bottom = next((item for item in reversed(patterns) if item["type"] == "Bottom"), None)
    base_pe = 100 / bond_yield
    mid_bottom = base_pe + 5
    high_bottom = base_pe + 10
    pe_category = (
        "N/A"
        if current_pe is None
        else "HIGH"
        if current_pe >= high_bottom
        else "MID"
        if current_pe >= mid_bottom
        else "LOW"
    )

    in_signal = "HOLD / NO ACTION"
    out_signal = "WAIT / NO ACTION"
    if last_bottom and current_price < last_bottom["b"]["price"]:
        in_signal = {"HIGH": "SELL 100%", "MID": "SELL 50%", "LOW": "SELL 0%"}.get(pe_category, "SELL")
    if last_top and current_price > last_top["b"]["price"]:
        out_signal = {"HIGH": "BUY 0%", "MID": "BUY 50%", "LOW": "BUY 100%"}.get(pe_category, "BUY")

    if pe_category == "MID":
        sip_signal = "Continue normal SIP"
    elif pe_category == "HIGH":
        sip_signal = "Stop SIP / wait"
    elif current_pe is not None and current_pe < base_pe + 2.5:
        sip_signal = "Increase SIP to 3×"
    elif pe_category == "LOW":
        sip_signal = "Increase SIP to 2×"
    else:
        sip_signal = "PE data unavailable"

    return {
        "lastTop": last_top["b"] if last_top else None,
        "lastBottom": last_bottom["b"] if last_bottom else None,
        "peCategory": pe_category,
        "peRanges": {
            "base": round(base_pe, 2),
            "lowBelow": round(mid_bottom, 2),
            "midTop": round(high_bottom, 2),
            "highAbove": round(high_bottom, 2),
        },
        "inMarket": in_signal,
        "outMarket": out_signal,
        "sip": sip_signal,
    }


def yearly_analysis(history: list[dict[str, float | str | None]]) -> list[dict]:
    years: dict[str, dict] = {}
    for point in history:
        if point["pe"]:
            years[str(point["date"])[:4]] = point
    descending = sorted(years.values(), key=lambda point: str(point["date"]), reverse=True)
    result = []
    for index, point in enumerate(descending):
        earnings = float(point["close"]) / float(point["pe"])
        previous_earnings = None
        if index + 1 < len(descending):
            previous = descending[index + 1]
            previous_earnings = float(previous["close"]) / float(previous["pe"])
        result.append(
            {
                "date": point["date"],
                "price": point["close"],
                "pe": point["pe"],
                "earnings": round(earnings, 2),
                "egr": (
                    round((earnings / previous_earnings - 1) * 100, 2)
                    if previous_earnings
                    else None
                ),
            }
        )
    return result


def average(values: list[float], window: int) -> float | None:
    clean = values[-window:]
    return sum(clean) / len(clean) if clean else None


def classify(prices: list[float]) -> tuple[str, str, int]:
    latest = prices[-1]
    sma_20 = average(prices, 20) or latest
    sma_50 = average(prices, 50) or latest
    sma_200 = average(prices, 200) or latest
    momentum = ((latest / prices[-21]) - 1) if len(prices) > 20 else 0

    if latest > sma_50 > sma_200:
        trend, phase, base = "Primary uptrend", "Leading", 78
    elif latest > sma_50:
        trend, phase, base = "Recovery", "Improving", 65
    elif latest < sma_50 < sma_200:
        trend, phase, base = "Primary downtrend", "Lagging", 32
    else:
        trend, phase, base = "Secondary reaction", "Weakening", 48

    score = round(max(5, min(95, base + momentum * 100 + (5 if latest > sma_20 else -5))))
    return trend, phase, score


def export(database: Path, output: Path, config_path: Path | None, macro_path: Path | None) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path else {}
    macro = json.loads(macro_path.read_text(encoding="utf-8")) if macro_path else {}
    bond_yield = float(macro.get("bond_yield", {}).get("value", 6.8))
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT IndexName, Date, ClosingPrice, PE_Ratio
            FROM daily_records
            WHERE ClosingPrice IS NOT NULL AND ClosingPrice > 0
            ORDER BY IndexName, Date
            """
        ).fetchall()
        weekly_rows = connection.execute(
            """
            SELECT IndexName, Date, ClosingPrice, PE_Ratio
            FROM weekly_records
            WHERE ClosingPrice IS NOT NULL AND ClosingPrice > 0
            ORDER BY IndexName, Date
            """
        ).fetchall()

    grouped: dict[str, list[dict[str, float | str | None]]] = {}
    for name, date, close, pe_ratio in rows:
        if name in RETIRED:
            continue
        grouped.setdefault(name, []).append(
            {
                "date": date,
                "close": round(float(close), 2),
                "pe": round(float(pe_ratio), 2) if pe_ratio and pe_ratio > 0 else None,
            }
        )
    weekly_grouped: dict[str, list[dict[str, float | str | None]]] = defaultdict(list)
    for name, date, close, pe_ratio in weekly_rows:
        if name not in RETIRED:
            weekly_grouped[name].append(
                {
                    "date": date,
                    "close": round(float(close), 2),
                    "pe": round(float(pe_ratio), 2) if pe_ratio and pe_ratio > 0 else None,
                }
            )

    instruments = []
    for name, history in sorted(grouped.items()):
        if len(history) < 2:
            continue
        prices = [float(point["close"]) for point in history]
        latest, previous = history[-1], history[-2]
        change = (float(latest["close"]) / float(previous["close"]) - 1) * 100
        trend, phase, score = classify(prices)
        weekly = weekly_grouped.get(name, [])
        depth_pct = float(config.get(name, {}).get("depth_pct", 0.04))
        patterns = identify_patterns(weekly[-160:], depth_pct)
        instruments.append(
            {
                "name": name,
                "asOf": latest["date"],
                "close": latest["close"],
                "pe": latest["pe"],
                "change": round(change, 2),
                "trend": trend,
                "phase": phase,
                "score": score,
                "depthPct": depth_pct,
                "history": history,
                "analysis": yearly_analysis(history),
                "weekly": weekly,
                "patterns": patterns,
                "dow": dow_signals(
                    float(latest["close"]),
                    float(latest["pe"]) if latest["pe"] else None,
                    patterns,
                    bond_yield,
                ),
            }
        )

    mcap_crore = float(macro.get("mcap", {}).get("value", 0))
    usd_inr = float(macro.get("usd_inr", {}).get("value", 0))
    gdp_usd_trillion = float(macro.get("gdp_usd", {}).get("value", 0))
    gdp_lakh_crore = gdp_usd_trillion * usd_inr
    mcap_lakh_crore = mcap_crore / 100_000
    payload = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": "Market Cycle historical database",
        "bondYield": bond_yield,
        "groups": config.get("overview_groups", {}),
        "macro": {
            "marketCapCrore": round(mcap_crore, 2),
            "marketCapLakhCrore": round(mcap_lakh_crore, 2),
            "marketCapDate": macro.get("mcap", {}).get("date"),
            "gdpLakhCrore": round(gdp_lakh_crore, 2),
            "gdpDate": macro.get("gdp_usd", {}).get("date"),
            "buffettRatio": round((mcap_lakh_crore / gdp_lakh_crore) * 100, 2) if gdp_lakh_crore else None,
            "bondYieldDate": macro.get("bond_yield", {}).get("date"),
        },
        "instruments": instruments,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Exported {len(instruments)} instruments to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--macro", type=Path)
    arguments = parser.parse_args()
    export(arguments.database, arguments.output, arguments.config, arguments.macro)
