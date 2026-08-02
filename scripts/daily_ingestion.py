"""Update the browser snapshot from the NSE Indices daily snapshot and notify Telegram."""

from __future__ import annotations

import csv
import html
import io
import json
import os
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


def update_payload(payload: dict, nse: dict[str, dict]) -> tuple[list[str], list[str], list[str]]:
    updated: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []
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
        updated, unchanged, missing = update_payload(payload, nse)
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
