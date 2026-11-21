#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
from functools import lru_cache
from datetime import date, datetime, time, timezone, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DASHBOARD_PATH = "dashboard.html"
DEFAULT_METER_ZONE = ZoneInfo("America/New_York")
MIN_CYCLE_DAYS, MAX_CYCLE_DAYS = 20, 45
READ_MINUTES = {"QUARTER_HOUR": 15, "HALF_HOUR": 30, "HOUR": 60}
DATASETS = ("usage", "cost", "pricing", "weather", "bills", "demand_maxima", "service_points", "green_button_rows")
INTERVAL_POINTS = 3000
HOURLY_POINTS = 31 * 25
DAILY_POINTS = 366
POLL_POINTS = 100
BILL_POINTS = 120
DEMAND_POINTS = 100
EIA_BENCHMARK_FALLBACKS = [
    {"period": "2026-03", "scope": "regional", "stateid": "MA", "label": "Massachusetts", "price_per_kwh": 0.3021, "unit": "USD/kWh", "source": "EIA retail-sales monthly residential average fallback"},
    {"period": "2026-03", "scope": "national", "stateid": "US", "label": "U.S. Total", "price_per_kwh": 0.1883, "unit": "USD/kWh", "source": "EIA retail-sales monthly residential average fallback"},
]


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - surfaced in dashboard diagnostics
        return {"_error": str(exc)}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))





def _iso_to_label(ts: str | None) -> str:
    dt = _instant(ts, timezone.utc)
    return dt.strftime("%b %d, %Y %H:%M UTC") if dt else ts or "unknown"


def _safe_rate(cost: float | None, kwh: float | None) -> float | None:
    return cost / kwh if cost is not None and kwh is not None and kwh > 0 else None


def _sum_known(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return math.fsum(known) if known else None


def _parse_eia_price_rows(payload: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    rows = []
    for row in ((payload or {}).get("response") or {}).get("data") or []:
        price_cents = _float(row.get("price"))
        if price_cents is None:
            continue
        rows.append({
            "period": row.get("period"),
            "scope": scope,
            "stateid": row.get("stateid"),
            "label": row.get("stateDescription") or row.get("stateid") or scope,
            "price_per_kwh": price_cents / 100.0,
            "unit": "USD/kWh",
            "source": "EIA retail-sales monthly residential average",
        })
    return sorted(rows, key=lambda r: r.get("period") or "")


@lru_cache(maxsize=8)
def _fetch_eia_benchmark_prices(state: str = "MA", months: int = 24) -> list[dict[str, Any]]:
    api_key = os.environ.get("EIA_API_KEY") or "DEMO_KEY"
    state = state or os.environ.get("EVERSMART_EIA_STATE", "MA")
    out: list[dict[str, Any]] = []
    base = "https://api.eia.gov/v2/electricity/retail-sales/data/"
    for scope, stateid in (("regional", state), ("national", "US")):
        params = {
            "api_key": api_key,
            "frequency": "monthly",
            "data[0]": "price",
            "facets[stateid][]": stateid,
            "facets[sectorid][]": "RES",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "offset": "0",
            "length": str(months),
        }
        try:
            with urllib.request.urlopen(base + "?" + urllib.parse.urlencode(params), timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            out.extend(_parse_eia_price_rows(payload, scope))
        except Exception:
            continue
    return sorted(out or EIA_BENCHMARK_FALLBACKS, key=lambda r: (r.get("period") or "", r.get("scope") or ""))


def _parse_interval_datetime(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _meter_zone(name: str | None) -> tuple[ZoneInfo, bool]:
    try:
        return (ZoneInfo(name), True) if name else (DEFAULT_METER_ZONE, False)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return DEFAULT_METER_ZONE, False


def _instant(value: str | None, zone: tzinfo = DEFAULT_METER_ZONE) -> datetime | None:
    dt = _parse_interval_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    # An ambiguous or nonexistent naive wall time cannot prove coverage.
    options = set()
    for fold in (0, 1):
        utc = dt.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == dt:
            options.add(utc)
    return options.pop() if len(options) == 1 else None


def _bounds(interval: str | None, zone=DEFAULT_METER_ZONE) -> tuple[datetime, datetime] | None:
    start_s, _, end_s = (interval or "").partition("/")
    start, end = _instant(start_s, zone), _instant(end_s, zone)
    return (start, end) if start is not None and end is not None and start < end else None


def _utc_label(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, zone).astimezone(timezone.utc)


def _period_label(interval: str | None, zone=DEFAULT_METER_ZONE) -> str:
    start_s, _, end_s = (interval or "").partition("/")
    start, end = _instant(start_s, zone), _instant(end_s, zone)
    if start is None or end is None:
        return interval or "unknown"
    start, end = start.astimezone(zone), end.astimezone(zone)
    if start.year == end.year:
        return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"





def _duration_label(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    if minutes < 60:
        return f"{minutes} min"
    whole, rem = divmod(minutes, 60)
    return f"{whole}h {rem}m" if rem else f"{whole}h"





def _round_money(value: float) -> float:
    return round(value + 0.0000000001, 6)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else None





def _weather_sensitivity(daily: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [d for d in daily if d.get("mean_temperature") is not None and d.get("net_kwh") is not None]
    temps = [float(d["mean_temperature"]) for d in rows]
    kwhs = [float(d["net_kwh"]) for d in rows]
    corr = _pearson(temps, kwhs)
    mild = [d for d in rows if 55 <= float(d["mean_temperature"]) <= 70]
    baseline = (sum(float(d["net_kwh"]) for d in mild) / len(mild)) if mild else ((sum(kwhs) / len(kwhs)) if kwhs else None)
    points = [{"date": d.get("date"), "mean_temperature": d.get("mean_temperature"), "net_kwh": d.get("net_kwh"), "cost": d.get("cost")} for d in rows]
    return {"correlation": corr, "baseline_kwh": baseline, "points": points}


def _hourly_profile(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = defaultdict(list)
    for row in hourly:
        buckets[int(row["hour"][11:13])].append(row)

    def mean_known(rows, key):
        values = [r[key] for r in rows if r[key] is not None]
        return math.fsum(values) / len(values) if values else None

    return [{"hour_of_day": hour, "count": len(rows), "avg_kwh": mean_known(rows, "net_kwh"),
             "avg_cost": mean_known(rows, "cost"),
             "peak_kw": max((r["peak_kw"] for r in rows if r["peak_kw"] is not None), default=None)}
            for hour, rows in sorted(buckets.items())]


def _anomalies(series: list[dict[str, Any]], daily: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [float(r.get("kwh") or 0.0) for r in series if r.get("kwh") is not None]
    kws = [float(r.get("kw") or 0.0) for r in series if r.get("kw") is not None]
    def threshold(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        med = ordered[len(ordered)//2]
        mean = sum(values) / len(values)
        std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        return max(med * 3, mean + std)
    kwh_thr = threshold(vals)
    kw_thr = threshold(kws)
    interval_hits = []
    for r in series:
        kwh, kw = r.get("kwh"), r.get("kw")
        if (kwh_thr and kwh is not None and kwh >= kwh_thr and kwh > 0) or (kw_thr and kw is not None and kw >= kw_thr and kw > 0):
            interval_hits.append({"t": r.get("t"), "interval": r.get("interval"), "kwh": kwh, "kw": kw, "cost": r.get("cost"), "reason": "high usage/demand"})
    day_vals = [float(d.get("net_kwh") or 0.0) for d in daily]
    day_thr = threshold(day_vals)
    day_hits = [{"date": d.get("date"), "net_kwh": d.get("net_kwh"), "cost": d.get("cost"), "reason": "high daily use"} for d in daily if day_thr and float(d.get("net_kwh") or 0.0) >= day_thr]
    return {"interval_threshold_kwh": kwh_thr, "interval_threshold_kw": kw_thr, "intervals": interval_hits[:20], "days": day_hits[:10]}


def _baseload(hourly: list[dict[str, Any]], effective_price: float | None) -> dict[str, Any]:
    overnight = []
    for row in hourly:
        if 0 <= int(row["hour"][11:13]) <= 5:
            measure = row["peak_kw"] if row["peak_kw"] is not None else row["net_kwh"]
            if measure is not None and measure >= 0:
                overnight.append(measure)
    estimated_kw = min(overnight, default=None)
    daily_kwh = estimated_kw * 24 if estimated_kw is not None else None
    monthly_kwh = daily_kwh * 30 if daily_kwh is not None else None
    monthly_cost = monthly_kwh * effective_price if monthly_kwh is not None and effective_price is not None else None
    return {"estimated_kw": estimated_kw, "daily_kwh": daily_kwh, "monthly_kwh": monthly_kwh,
            "monthly_cost": monthly_cost, "sample_count": len(overnight)}


def _savings_simulator(avg_daily_kwh: float | None, baseload: dict[str, Any], effective_price: float | None) -> dict[str, Any]:
    def scenario(kwh):
        return {"monthly_kwh_saved": kwh,
                "monthly_savings": kwh * effective_price if kwh is not None and effective_price is not None else None}

    baseload_kwh = baseload.get("monthly_kwh")
    return {
        "reduce_daily_10pct": scenario(avg_daily_kwh * 3 if avg_daily_kwh is not None else None),
        "cut_baseload_100w": scenario(72.0),
        "cut_baseload_25pct": scenario(baseload_kwh * 0.25 if baseload_kwh is not None else None),
        "reduce_peak_events_15min": scenario(1.0),
    }


def _run_diff(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(runs) < 2:
        return None
    prev, cur = runs[-2], runs[-1]
    def delta(key):
        before, after = prev.get(key), cur.get(key)
        return after - before if before is not None and after is not None else None
    return {
        "previous_retrieved_at_utc": prev.get("retrieved_at_utc"),
        "latest_retrieved_at_utc": cur.get("retrieved_at_utc"),
        "new_usage_rows": delta("usage_rows"),
        "new_cost_rows": delta("cost_rows"),
        "kwh_delta": delta("total_net_kwh"),
        "cost_delta": delta("total_cost"),
    }



def _date_from_mmddyy(value: str) -> str | None:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%y").date().isoformat()
    except Exception:
        return None


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF using the best available local backend."""
    path = Path(path)
    errors: list[str] = []
    try:
        import fitz  # type: ignore
        with fitz.open(str(path)) as doc:
            text = "\n".join(page.get_text("text") for page in doc)
        if text.strip():
            return text
        errors.append("PyMuPDF returned no text")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"PyMuPDF/fitz: {exc}")
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            return text
        errors.append("pypdf returned no text")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pypdf: {exc}")
    try:
        swift = "import Foundation; import PDFKit; let u=URL(fileURLWithPath:CommandLine.arguments[1]); guard let d=PDFDocument(url:u) else { exit(2) }; for i in 0..<d.pageCount { if let s=d.page(at:i)?.string { print(s) } }"
        result = subprocess.run(
            ["/usr/bin/swift", "-e", swift, str(path.resolve())],
            check=True, capture_output=True, text=True, timeout=60,
        )
        if result.stdout.strip():
            return result.stdout
        errors.append("macOS PDFKit returned no text")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"macOS PDFKit: {exc}")
    raise RuntimeError("Unable to extract bill PDF text; tried PyMuPDF, pypdf, and macOS PDFKit (" + "; ".join(errors) + ")")


def parse_eversource_bill_pdf(path: Path) -> dict[str, Any]:
    """Parse bill/reconciliation facts from an Eversource PDF without preserving name/address PII."""
    text = _extract_pdf_text(Path(path))
    stmt_m = re.search(r"Statement Date:\s*(\d{2}/\d{2}/\d{2})", text)
    statement_date = _date_from_mmddyy(stmt_m.group(1)) if stmt_m else None
    due_m = re.search(r"by\s+(\d{2}/\d{2}/\d{2})", text)
    due_date = _date_from_mmddyy(due_m.group(1)) if due_m else None
    amounts = [float(x.replace(',', '')) for x in re.findall(r"Total Amount Due(?:\s*\n|\s+)\$([0-9,]+\.\d{2})", text)]
    total_due = amounts[-1] if amounts else None
    cur_vals = [float(x.replace(',', '')) for x in re.findall(r"Total Current Charges\s*\n\$([0-9,]+\.\d{2})", text)]
    current_charges = cur_vals[-1] if cur_vals else None
    service_periods: list[dict[str, Any]] = []
    for m in re.finditer(r"Service from (\d{2}/\d{2}/\d{2}) - (\d{2}/\d{2}/\d{2})(?P<body>.*?)(?=Service Reference:|\nService from |Total Charges for Electricity|\Z)", text, re.S):
        body = m.group('body')
        days_m = re.search(r"(\d+) Days", body)
        rows = re.findall(r"(?:\n|^)(\d{7,8})(?:\n|\s+)(\d+)(?:\n|\s+)(\d+)(?:\n|\s+)(\d+)(?:\n|\s+)Actual", body)
        period_usage = sum(float(row[3]) for row in rows)
        service_periods.append({
            "start_date": _date_from_mmddyy(m.group(1)), "end_date": _date_from_mmddyy(m.group(2)), "days": int(days_m.group(1)) if days_m else None,
            "meter_rows": [{"meter_number": r[0], "current_read": float(r[1]), "previous_read": float(r[2]), "usage_kwh": float(r[3]), "reading_type": "Actual"} for r in rows],
            "usage_kwh": period_usage,
        })
    total_m = re.search(r"=\s*([0-9,]+)\s+Total Billed Usage", text)
    total_billed_kwh = float(total_m.group(1).replace(',', '')) if total_m else None
    line_items: list[dict[str, Any]] = []
    for kwh, rate_s, sign, amount_s, desc in re.findall(r"\n([0-9,]+) kWh\s*X\s*([\-.0-9]+)\n(-?)\$?([0-9,]+\.\d{2})\n([^\n]+)", text):
        description = desc.strip()
        category = "supply" if "Generation Service" in description else "delivery"
        item = {"category": category, "description": description, "kwh": float(kwh.replace(',', '')), "rate": float(rate_s), "amount": (-1 if sign else 1) * float(amount_s.replace(',', ''))}
        if item not in line_items:
            line_items.append(item)
    # PDFKit lists labels separately from one-line formulas. Parse those ordered
    # sections only when the layout-oriented extractor found no line items.
    if not line_items:
        supply_m = re.search(r"Generation Service Charge.*?([0-9,]+) kWh\s*X\s*([\-.0-9]+)\s+(-?)\$?([0-9,]+\.\d{2})", text, re.S)
        if supply_m:
            line_items.append({"category": "supply", "description": "Generation Service Charge", "kwh": float(supply_m.group(1).replace(',', '')), "rate": float(supply_m.group(2)), "amount": (-1 if supply_m.group(3) else 1) * float(supply_m.group(4).replace(',', ''))})
        delivery_m = re.search(r"Customer Charge(?P<body>.*?)Subtotal Delivery Services", text, re.S)
        if delivery_m:
            body = delivery_m.group('body')
            known_labels = {
                "Distribution Charge", "Transition Charge", "Transmission Charge",
                "Net Meter Recovery Surcharge", "Revenue Decoupling Charge",
                "Distributed Solar Charge", "Renewable Energy Charge",
                "Energy Efficiency Charge", "Electric Vehicle Program",
                "Provisional Syst Plan Factor", "Provisional System Planning Factor",
            }
            descriptions = [line.strip() for line in body.splitlines() if line.strip() in known_labels]
            formulas = re.findall(r"([0-9,]+) kWh\s*X\s*([\-.0-9]+)\s+(-?)\$?([0-9,]+\.\d{2})", body)
            for description, (kwh, rate_s, sign, amount_s) in zip(descriptions, formulas):
                line_items.append({"category": "delivery", "description": description, "kwh": float(kwh.replace(',', '')), "rate": float(rate_s), "amount": (-1 if sign else 1) * float(amount_s.replace(',', ''))})
    cust_m = re.search(r"\$([0-9,]+\.\d{2})\nCustomer Charge", text)
    customer_charge = float(cust_m.group(1).replace(',', '')) if cust_m else None
    if customer_charge is None and current_charges is not None and line_items and "Customer Charge" in text:
        inferred_fixed = round(current_charges - sum(item["amount"] for item in line_items), 2)
        if 0 < inferred_fixed <= current_charges:
            customer_charge = inferred_fixed
    if customer_charge:
        line_items.append({"description": "Customer Charge", "kwh": None, "rate": None, "amount": customer_charge, "category": "fixed"})
    supply_rate = next((li["rate"] for li in line_items if "Generation Service" in li.get("description", "")), None)
    if supply_rate is None:
        supply_m = re.search(r"Generation Service Charge.*?([0-9,]+) kWh\s*X\s*([\-.0-9]+)", text, re.S)
        supply_rate = float(supply_m.group(2)) if supply_m else None
    change_m = re.search(r"meter was replaced on ([A-Za-z]+ \d{1,2}, \d{4})", text)
    meter_change_date = None
    if change_m:
        try:
            meter_change_date = datetime.strptime(change_m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            pass
    if meter_change_date and service_periods:
        for index, period in enumerate(service_periods):
            period["kind"] = "old_meter" if index == 0 else "new_meter"
        old_meter_kwh = service_periods[0]["usage_kwh"]
        new_meter_kwh = sum(sp["usage_kwh"] for sp in service_periods[1:]) if len(service_periods) > 1 else None
    else:
        for period in service_periods:
            period["kind"] = "smart_meter"
        old_meter_kwh = None
        new_meter_kwh = sum(sp["usage_kwh"] for sp in service_periods) if service_periods else total_billed_kwh
    billing_period_start = service_periods[0].get("start_date") if service_periods else None
    billing_period_end = service_periods[-1].get("end_date") if service_periods else None
    variable_total = current_charges - customer_charge if current_charges is not None and customer_charge is not None else None
    return {
        "source": str(Path(path).name), "statement_date": statement_date, "due_date": due_date,
        "total_due": total_due, "total_current_charges": current_charges, "total_billed_kwh": total_billed_kwh,
        "billing_period_start": billing_period_start, "billing_period_end": billing_period_end,
        "old_meter_kwh": old_meter_kwh, "new_meter_kwh": new_meter_kwh, "customer_charge": customer_charge,
        "supply_rate_per_kwh": supply_rate, "variable_rate_per_kwh_excluding_customer_charge": _safe_rate(variable_total, total_billed_kwh or 0.0),
        "all_in_rate_per_kwh": _safe_rate(current_charges, total_billed_kwh),
        "meter_change_date": meter_change_date, "service_periods": service_periods, "line_items": line_items, "pii_redacted": True,
    }


def _sanitize_bill_for_dashboard(bill: dict[str, Any]) -> dict[str, Any]:
    """Keep bill facts useful for reconciliation while dropping meter/account PII."""
    if bill.get("error"):
        return dict(bill)
    safe = {k: v for k, v in bill.items() if k not in {"meter_number", "meter_rows", "raw_text", "account", "account_number", "customer_name", "service_address", "address"}}
    periods = []
    for sp in bill.get("service_periods") or []:
        periods.append({
            "start_date": sp.get("start_date"),
            "end_date": sp.get("end_date"),
            "days": sp.get("days"),
            "usage_kwh": sp.get("usage_kwh"),
            "kind": sp.get("kind"),
            "meter_count": len(sp.get("meter_rows") or []),
        })
    if periods:
        safe["service_periods"] = periods
    safe["pii_redacted"] = True
    return safe


def _load_manual_bills(directory: Path) -> list[dict[str, Any]]:
    """Read statements only from the explicitly supplied directory."""
    bills: list[dict[str, Any]] = []
    files = sorted(directory.iterdir()) if directory.is_dir() else []
    for path in files:
        if path.is_file() and path.suffix.lower() == ".json":
            try:
                bills.append(_sanitize_bill_for_dashboard(json.loads(path.read_text())))
            except Exception as exc:  # surfaced only in the private dashboard
                bills.append({"source": path.name, "error": str(exc)})
    sources = {b.get("source") for b in bills if not b.get("error")}
    for path in files:
        if not path.is_file() or path.suffix.lower() != ".pdf" or {path.name, str(path)} & sources:
            continue
        try:
            bills.append(_sanitize_bill_for_dashboard(parse_eversource_bill_pdf(path)))
        except Exception as exc:
            bills.append({"source": path.name, "error": str(exc)})
    by_statement: dict[tuple[Any, ...], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for bill in bills:
        if bill.get("error"):
            errors.append(bill)
            continue
        key = (bill.get("statement_date"), bill.get("total_due"), bill.get("total_billed_kwh"))
        current = by_statement.get(key)
        # Prefer the richest parse when a legacy JSON and PDF describe one statement.
        richness = sum(bill.get(field) is not None for field in ("billing_period_start", "billing_period_end", "meter_change_date")) + len(bill.get("line_items") or [])
        current_richness = sum(current.get(field) is not None for field in ("billing_period_start", "billing_period_end", "meter_change_date")) + len(current.get("line_items") or []) if current else -1
        if current is None or richness > current_richness:
            by_statement[key] = bill
    return sorted(by_statement.values(), key=lambda b: (b.get("statement_date") or "", b.get("source") or "")) + errors


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except (TypeError, ValueError):
        return None


def _latest_physical_bill(bills):
    return max((b for b in bills if not b.get("error")), key=lambda b: b.get("statement_date") or "", default=None)


def _bill_rates(bill):
    charges = _float(bill.get("total_current_charges"))
    fixed = _float(bill.get("customer_charge"))
    kwh = _float(bill.get("total_billed_kwh"))
    # Amount due may include prior balances, payments or credits. Never use it.
    variable = _safe_rate(charges - fixed, kwh) if charges is not None and fixed is not None and charges >= fixed >= 0 else None
    return variable, _safe_rate(charges, kwh), fixed


def _expected_spans(day, zone, resolution):
    """Expected UTC slots for one local meter day; 23/25h on DST transitions."""
    start, end = _midnight(day, zone), _midnight(day + timedelta(days=1), zone)
    if resolution == "DAY":
        return {(start, end)}
    minutes = READ_MINUTES.get(resolution)
    if minutes is None:
        return None
    step = timedelta(minutes=minutes)
    slots, remainder = divmod(end - start, step)
    if remainder:
        return None
    return {(start + n * step, start + (n + 1) * step) for n in range(slots)}


def _days(start, end):
    return (start + timedelta(days=i) for i in range((end - start).days))


def _day_coverage(series, zone, zone_known, resolution):
    actual = defaultdict(list)
    for row in series:
        if row["usage_source"] == "actual":
            bounds = _bounds(row["interval"], zone)
            if bounds:
                actual[bounds[0].astimezone(zone).date()].append(bounds)

    def coverage(day):
        observed = actual[day]
        expected = _expected_spans(day, zone, resolution) if zone_known else None
        return (len(observed), len(expected) if expected is not None else None,
                expected is not None and len(observed) == len(expected) and set(observed) == expected)

    return coverage


def _period_usage(series, zone, start, end, *, actual_only=False, contained=True):
    start_utc, end_utc = _midnight(start, zone), _midnight(end, zone)
    rows = []
    for row in series:
        if row["kwh"] is None or (actual_only and row["usage_source"] != "actual"):
            continue
        lo, hi = _instant(row["t"]), _instant(row["end_utc"])
        if lo is not None and hi is not None and lo < end_utc and hi > start_utc:
            if not contained or (start_utc <= lo and hi <= end_utc):
                rows.append(row)
    return rows


def _bill_reconciliation(latest: dict[str, Any], manual_bills: list[dict[str, Any]], coverage) -> dict[str, Any] | None:
    bill = _latest_physical_bill(manual_bills)
    if bill is None:
        return None
    periods = [sp for sp in bill.get("service_periods", []) if sp.get("kind") != "old_meter"]
    period = next((sp for sp in reversed(periods) if sp.get("kind") in {"new_meter", "smart_meter"}), periods[-1] if periods else {})
    start = _parse_date(period.get("start_date") or (None if bill.get("meter_change_date") else bill.get("billing_period_start")))
    last = _parse_date(period.get("end_date") or (None if bill.get("meter_change_date") else bill.get("billing_period_end")))
    bill_kwh = _float(period.get("usage_kwh"))
    if bill_kwh is None:
        bill_kwh = _float(bill.get("new_meter_kwh"))
    if bill_kwh is None and not bill.get("meter_change_date"):
        bill_kwh = _float(bill.get("total_billed_kwh"))
    zone, _ = _meter_zone(latest["timezone"])
    # Physical statement dates are printed as inclusive service dates.
    end = last + timedelta(days=1) if last else None
    days = list(_days(start, end)) if start and end and start < end and (end - start).days <= 366 else []
    overlapping = _period_usage(latest["series"], zone, days[0], end, contained=False) if days and end else []
    rows = _period_usage(overlapping, zone, days[0], end) if days and end else []
    covered = [coverage(day) for day in days]
    expected = sum(n for _, n, _ in covered if n is not None) if covered and all(n is not None for _, n, _ in covered) else None
    actual = sum(n for n, _, _ in covered)
    smart_kwh = _sum_known(r["kwh"] for r in rows)
    smart_cost = _sum_known(r["cost"] for r in rows)
    coverage_status = ("full_overlap" if covered and all(ok for _, _, ok in covered) else
                       "partial_overlap" if overlapping or not days else "no_overlap")
    diff = smart_kwh - bill_kwh if smart_kwh is not None and bill_kwh is not None else None
    tolerance = max(0.5, abs(bill_kwh) * 0.01) if bill_kwh is not None else None
    status = coverage_status
    if coverage_status == "full_overlap":
        if diff is None or tolerance is None:
            status = "unavailable"
        else:
            status = "matched" if abs(diff) <= tolerance else "mismatch"
    return {
        "status": status, "coverage_status": coverage_status, "source": bill.get("source"),
        "statement_date": bill.get("statement_date"), "due_date": bill.get("due_date"),
        "total_billed_kwh": bill.get("total_billed_kwh"), "total_due": bill.get("total_due"),
        "old_meter_bill_kwh": bill.get("old_meter_kwh"), "new_meter_bill_kwh": bill_kwh,
        "smart_meter_overlap_kwh": smart_kwh, "smart_meter_overlap_cost": smart_cost,
        "difference_kwh": _round_money(diff) if diff is not None else None, "tolerance_kwh": tolerance,
        "overlap_start_date": start.isoformat() if start else None, "overlap_end_date": last.isoformat() if last else None,
        "overlap_days": len({r["local_date"] for r in rows}), "expected_days": len(days) if days else None,
        "expected_intervals": expected, "observed_actual_intervals": actual, "observed_intervals": len(rows),
        "meter_change_date": bill.get("meter_change_date"), "physical_bill": bill,
        "explanation": "Compares canonical meter readings with the physical statement's smart-meter period. A match requires full actual interval coverage; old-meter usage is excluded.",
    }


def _apply_bill_cost_modes(latest: dict[str, Any], manual_bills: list[dict[str, Any]]) -> None:
    bill = _latest_physical_bill(manual_bills)
    if bill is None:
        return
    variable, all_in, fixed = _bill_rates(bill)
    for bucket in ("daily", "hourly"):
        for row in latest.get(bucket, []):
            kwh = row.get("net_kwh")
            row["bill_calibrated_variable_cost"] = kwh * variable if kwh is not None and variable is not None else None
            row["all_in_allocated_cost"] = kwh * all_in if kwh is not None and all_in is not None else None
    latest["bill_cost_modes"] = {"opower_interval_cost_label": "Opower interval estimate", "bill_calibrated_variable_rate": variable, "all_in_allocated_rate": all_in, "customer_charge": fixed}
    latest["line_item_breakdown"] = bill.get("line_items") or []
    latest["meter_change"] = {"date": bill.get("meter_change_date"), "old_meter_kwh": bill.get("old_meter_kwh"), "new_meter_kwh": bill.get("new_meter_kwh")}


def _latest_electricity_segment(bills, zone):
    # usageInterval is the segment's service period; timeInterval can be the
    # parent bill's issue period, which is not evidence of an electricity cutoff.
    segments = [bounds for b in bills if str(b.get("serviceType") or "").strip().upper() in {"ELECTRIC", "ELECTRICITY"}
                and (bounds := _bounds(b.get("usageInterval"), zone))]
    return max(segments, key=lambda v: (v[1], v[0]), default=None)


def _cycle_basis(segment, physical, zone):
    physical_start = _parse_date(physical.get("billing_period_start"))
    physical_last = _parse_date(physical.get("billing_period_end"))
    physical_end = physical_last + timedelta(days=1) if physical_last else None
    physical_days = (physical_end - physical_start).days if physical_end and physical_start else None
    if segment:
        segment_start, segment_end = (v.astimezone(zone) for v in segment)
        # A half-open midnight end is already the first unbilled day. Skip
        # the transition day itself when the boundary falls during that day.
        cycle_start = segment_end.date() + timedelta(days=int(segment_end.time() != time.min))
        cutoff_source = "latest_electricity_segment"
        billing_days = (segment_end.date() - segment_start.date()).days
        if physical_end and physical_end > cycle_start:
            return physical_end, physical_days, "physical_statement", "physical_statement"
        if not MIN_CYCLE_DAYS <= billing_days <= MAX_CYCLE_DAYS:
            return cycle_start, physical_days, cutoff_source, "physical_statement"
        return cycle_start, billing_days, cutoff_source, cutoff_source
    return physical_end, physical_days, "physical_statement", "physical_statement"


def _current_billing_cycle(latest, physical, coverage, as_of):
    """Forecast using contiguous, complete actual local days and statement charges."""
    zone, _ = _meter_zone(latest["timezone"])
    today = as_of.astimezone(zone).date()
    physical = physical or {}
    segment = _latest_electricity_segment(latest["bill_history"], zone)
    start, length, start_source, length_source = _cycle_basis(segment, physical, zone)
    if length is not None and not MIN_CYCLE_DAYS <= length <= MAX_CYCLE_DAYS:
        length = None
    cycle = {"status": "unavailable", "reason": None, "projected_cost": None, "projected_kwh": None,
             "cycle_start_date": start.isoformat() if start else None,
             "cycle_start_source": start_source if start else None,
             "billing_days": length,
             "cycle_length_source": length_source if length else None,
             "latest_electricity_segment_end_utc": _utc_label(segment[1]) if segment else None,
             "rate_statement_date": physical.get("statement_date")}

    def unavailable(reason):
        return {**cycle, "reason": reason}

    if start is None or length is None or not latest["meter_zone_known"]:
        return unavailable("unknown_cycle")
    expected_end = start + timedelta(days=length)
    cycle.update(expected_end_date=expected_end.isoformat(), completed_days_through=(today - timedelta(days=1)).isoformat())
    if today >= expected_end:
        return unavailable("stale_billing_cycle")
    if today <= start:
        return unavailable("no_complete_days")
    observed = (today - start).days
    overlapping = _period_usage(latest["series"], zone, start, today, actual_only=True, contained=False)
    if not overlapping:
        return unavailable("no_actual_reads")
    if not all(coverage(day)[2] for day in _days(start, today)):
        return unavailable("incomplete_usage_coverage")
    variable, _, fixed = _bill_rates(physical)
    if variable is None or fixed is None:
        return unavailable("unknown_rate")
    rows = _period_usage(overlapping, zone, start, today)
    kwh = math.fsum(row["kwh"] for row in rows)
    projected_kwh = kwh / observed * length
    observed_cost = kwh * variable
    projected_cost = projected_kwh * variable + fixed
    return {**cycle, "status": "available", "observed_days": observed, "kwh_so_far": kwh,
            "opower_interval_cost_so_far": _sum_known(row["cost"] for row in rows),
            "bill_calibrated_variable_cost_so_far": observed_cost,
            "projected_kwh": projected_kwh, "projected_variable_cost": projected_kwh * variable,
            "projected_cost": projected_cost, "remaining_cost": max(0.0, projected_cost - observed_cost),
            "variable_rate_per_kwh": variable, "fixed_charge": fixed,
            "cost_basis": "Complete ACTUAL meter days so far × expected cycle length, valued at the latest physical statement's variable rate plus its customer charge."}


OPTIONAL_ERROR_KEYS = {"bill_forecast"}


def _split_run_issues(manifest: dict[str, Any]) -> tuple[Any | None, dict[str, Any]]:
    warnings = dict(manifest.get("warnings") or {})
    raw_errors = dict(manifest.get("errors") or {})
    for key in list(raw_errors):
        if key in OPTIONAL_ERROR_KEYS:
            warnings[key] = raw_errors.pop(key)
    hard_error = manifest.get("cost_error") or manifest.get("_error") or (raw_errors or None)
    return hard_error, warnings


def _legacy_poll(run_dir: Path):
    manifest = _read_json(run_dir / "manifest.json")
    manifest.setdefault("retrieved_at_utc", run_dir.name)
    rows = {name: _read_csv(run_dir / f"{name}.csv") for name in DATASETS}
    if not rows["service_points"]:
        rows["service_points"] = manifest.get("service_points") or []
    return manifest, rows


def _row_account(row):
    return row.get("utility_id") or row.get("account")


def _kind(row):
    return str(row.get("readType") or "").strip().upper()


def _net(row):
    return str(row.get("stream") or "").lower() == "netusage" and str(row.get("serviceQuantityIdentifier") or "NET_USAGE").upper() == "NET_USAGE"


def _channel(row):
    return (_row_account(row), row.get("service_point_uuid") or row.get("uuid") or "")


def _revision_key(dataset, row, zone):
    span = _bounds(row.get("timeInterval"), zone) or row.get("timeInterval")
    if dataset == "service_points":
        return (_channel(row),)
    if dataset == "bills":
        return (row.get("segment_urn") or (row.get("service_agreement_uuid"), row.get("serviceType"), row.get("usageInterval") or span),)
    if dataset == "weather":
        return (row.get("premise_uuid"), span or row.get("date"))
    if dataset == "green_button_rows":
        return tuple(row.get(k) for k in ("dataset", "service_point", "date", "start_time", "end_time"))
    key = (_channel(row), row.get("stream"), row.get("serviceQuantityIdentifier"), span)
    if dataset == "pricing":
        key += tuple(row.get(k) for k in ("rate_plan_code", "rate_type", "tier", "tou_label", "component_attributes"))
    return key


def _merge_legacy(polls, zone):
    merged = {dataset: {} for dataset in DATASETS}
    for _, datasets in polls:
        for dataset in DATASETS:
            for row in datasets[dataset]:
                key = _revision_key(dataset, row, zone)
                prior = merged[dataset].get(key)
                if dataset in {"usage", "cost", "pricing", "demand_maxima"} and prior and _kind(prior) == "ACTUAL" and _kind(row) != "ACTUAL":
                    continue
                merged[dataset][key] = row
    return {dataset: list(rows.values()) for dataset, rows in merged.items()}


def _poll_summary(manifest, rows=None, run_dir=None):
    error, warnings = _split_run_issues(manifest)
    stamp = manifest.get("retrieved_at_utc")
    available = manifest.get("available_interval")
    end = manifest.get("latest_available_end") or (available or "/").partition("/")[2] or None
    counts = {name: manifest.get(f"{name}_rows") for name in DATASETS}
    if rows is not None:
        counts = {name: len(rows[name]) for name in DATASETS}
    return {"run_dir": str(run_dir) if run_dir else manifest.get("output_dir"),
            "retrieved_at_utc": stamp, "retrieved_label": _iso_to_label(stamp),
            "account": manifest.get("account") or manifest.get("utility_id"),
            "available_interval": available, "latest_available_end": end,
            "requested_interval": manifest.get("requested_interval"), "resolution": manifest.get("resolution"),
            "timezone": manifest.get("timezone"), "usage_rows": counts["usage"], "cost_rows": counts["cost"],
            "dataset_counts": counts, "error": error, "warnings": warnings, "poll_succeeded": _poll_succeeded(manifest)}


def _poll_succeeded(manifest):
    status = str(manifest.get("poll_status") or manifest.get("status") or "").lower()
    if status in {"failed", "error", "failure", "partial"} or manifest.get("success") is False:
        return False
    if status == "collected":
        return True  # New collectors commit only after required usage succeeds.
    error, _ = _split_run_issues(manifest)
    return not error


def _usage_source(row):
    if _float(row.get("value")) is None:
        return "missing"
    kind = _kind(row)
    return "actual" if kind == "ACTUAL" else "estimated" if kind in {"ESTIMATE", "ESTIMATED"} else "unknown"


def _nonoverlapping_usage(usage, resolution, zone):
    """Prefer the current cadence where it overlaps history at another cadence."""
    channels = defaultdict(list)
    for row, bounds in usage:
        channels[_channel(row)].append((row, bounds))

    def preferred(bounds):
        start, end = bounds
        if resolution == "DAY":
            day = start.astimezone(zone).date()
            return start == _midnight(day, zone) and end == _midnight(day + timedelta(days=1), zone)
        minutes = READ_MINUTES.get(resolution)
        return minutes is not None and end - start == timedelta(minutes=minutes)

    for rows in channels.values():
        rows.sort(key=lambda item: (item[1][0], -item[1][1].timestamp()))
        primary = []
        for row, bounds in rows:
            if _float(row.get("value")) is not None and preferred(bounds) and (not primary or primary[-1][1][1] <= bounds[0]):
                primary.append((row, bounds))
        starts = [bounds[0] for _, bounds in primary]
        previous_end = None
        for row, (start, end) in rows:
            i = bisect_left(starts, end)
            if i and primary[i - 1][1][1] > start:
                continue
            if previous_end is None or previous_end <= start:
                previous_end = end
                yield row, (start, end)
        yield from primary


def _build_meter_summary(manifest, datasets, account):
    service_points = datasets["service_points"] or manifest.get("service_points") or []
    zone_name = manifest.get("timezone")
    if not zone_name:
        zone_name = next((r["timezone"] for r in service_points if (not account or _row_account(r) == account) and r.get("timezone")), None)
    zone, zone_known = _meter_zone(zone_name)

    def belongs(row):
        return not account or not _row_account(row) or _row_account(row) == account

    def measurements(rows):
        for row in rows:
            if bounds := _bounds(row.get("timeInterval"), zone):
                yield row, bounds

    costs = {}
    pricing = {}
    usage = []
    delivered = []
    demand = {}
    for row, bounds in measurements(datasets["cost"]):
        if _net(row):
            costs[(_channel(row), bounds)] = row
    for row, bounds in measurements(datasets["pricing"]):
        if (_net(row) or not row.get("stream")) and _float(row.get("cost_per_unit")) is not None:
            pricing[(_channel(row), bounds)] = row
    for row, bounds in measurements(datasets["usage"]):
        if _net(row):
            usage.append((row, bounds))
        elif belongs(row) and row.get("stream") == "energyDelivered":
            delivered.append((bounds, _float(row.get("value"))))
        elif belongs(row) and row.get("stream") == "demand":
            demand[(_channel(row), bounds)] = row
    # Maxima is a separate dataset and can contain an older revision of an
    # interval also returned by the canonical demand stream. Never take max(old,new).
    for row, bounds in measurements(datasets["demand_maxima"]):
        if belongs(row):
            demand.setdefault((_channel(row), bounds), row)

    weather = {}
    for row in datasets["weather"]:
        bounds = _bounds(row.get("timeInterval"), zone)
        day = bounds[0].astimezone(zone).date().isoformat() if bounds else row.get("date")
        if day:
            weather[day] = {"date": day, "timeInterval": row.get("timeInterval"),
                            **{k: _float(row.get(k)) for k in ("min_temperature", "mean_temperature", "max_temperature")}}

    def interval_details(row, bounds):
        start, end = bounds
        local = start.astimezone(zone)
        minutes = int((end - start).total_seconds() / 60)
        return {"t": _utc_label(start), "end_utc": _utc_label(end), "interval": row.get("timeInterval"),
                "local_date": local.date().isoformat(), "label": local.strftime("%b %d, %I:%M %p %Z"),
                "duration_minutes": minutes, "duration_label": _duration_label(minutes)}

    price_series = {}
    for (channel, bounds), r in pricing.items():
        if belongs(r):
            price_series[(channel, bounds)] = {**interval_details(r, bounds), "price_per_kwh": _float(r.get("cost_per_unit")),
                                                "price_source": "rated_components"}
    demand_events = {}
    for key, r in demand.items():
        _, bounds = key
        kw = _float(r.get("value"))
        if kw is None:
            continue
        event = interval_details(r, bounds)
        price = _float(pricing.get(key, {}).get("cost_per_unit"))
        at_peak = kw * event["duration_minutes"] / 60
        demand_events[key] = {**event, "timeInterval": event["interval"], "kw": kw, "price_per_kwh": price,
                              "estimated_kwh_at_peak": at_peak, "estimated_cost_at_peak": at_peak * price if price is not None else None,
                              "mean_temperature": weather.get(event["local_date"], {}).get("mean_temperature"),
                              "readType": r.get("readType"), "unit": r.get("unit"),
                              "isMaximum": str(r.get("isMaximum")).lower() == "true"}

    point_totals = defaultdict(list)
    series = []
    for r, bounds in sorted(_nonoverlapping_usage(usage, manifest.get("resolution"), zone), key=lambda item: item[1]):
        channel = _channel(r)
        key = channel, bounds
        kwh = _float(r.get("value"))
        usage_source = _usage_source(r)
        cost_row = costs.get(key, {})
        cost = _float(cost_row.get("monetaryAmount"))
        price_row = pricing.get(key, {})
        price = _float(price_row.get("cost_per_unit"))
        if cost is not None:
            cost_source = "estimated_cost_stream" if _kind(cost_row) in {"ESTIMATE", "ESTIMATED"} else "cost_stream"
        elif kwh is not None and price is not None:
            cost, cost_source = kwh * price, "estimated_from_rate"
        else:
            cost_source = "missing"
        detail = interval_details(r, bounds)
        point_totals[channel].append((bounds[0].astimezone(zone), kwh, cost))
        if not belongs(r):
            continue
        effective = _safe_rate(cost, kwh)
        if price is None and effective is not None and key not in price_series:
            price_series[key] = {**detail, "price_per_kwh": effective, "price_source": "cost_div_kwh"}
        event = demand_events.get(key, {})
        series.append({**detail, "kwh": kwh, "kw": event.get("kw"), "cost": cost, "cost_source": cost_source,
                       "usage_source": usage_source, "readType": r.get("readType"),
                       "price_per_kwh": price if price is not None else effective,
                       "price_source": "rated_components" if price is not None else "cost_div_kwh" if effective is not None else None,
                       "rate_type": price_row.get("rate_type"), "tou_label": price_row.get("tou_label"),
                       "estimated_kwh_at_peak": event.get("estimated_kwh_at_peak"), "estimated_cost_at_peak": event.get("estimated_cost_at_peak"),
                       **{k: weather.get(detail["local_date"], {}).get(k) for k in ("min_temperature", "mean_temperature", "max_temperature")}})

    service_point_summaries = []
    for channel, points in point_totals.items():
        days, weeks = defaultdict(list), defaultdict(list)
        for stamp, kwh, cost in points:
            year, week, _ = stamp.isocalendar()
            days[stamp.date().isoformat()].append((kwh, cost))
            weeks[f"{year}-W{week:02d}"].append((kwh, cost))

        def point_history(buckets, cap):
            return {key: {"kwh": _sum_known(p[0] for p in rows), "cost": _sum_known(p[1] for p in rows)}
                    for key, rows in sorted(buckets.items())[-cap:]}

        service_point_summaries.append({"utility_id": channel[0], "service_point_uuid": channel[1],
                                        "total_kwh": _sum_known(p[1] for p in points), "total_cost": _sum_known(p[2] for p in points),
                                        "days": point_history(days, 3), "weeks": point_history(weeks, 4)})

    def aggregate(bucket):
        groups = defaultdict(lambda: defaultdict(list))

        def add(utc, key, value):
            local = utc.astimezone(zone)
            # Aware local datetimes with the same tzinfo compare equal across
            # folds. Key hours by UTC instants and label them by offset later.
            stamp = local.date() if bucket == "date" else local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            groups[stamp][key].append(value)

        for r in series:
            ts = _instant(r["t"], zone)
            add(ts, "net_kwh", r["kwh"])
            add(ts, "cost", r["cost"])
        for r in demand_events.values():
            add(_instant(r["t"], zone), "peak_kw", r["kw"])
        if bucket == "date":
            for bounds, kwh in delivered:
                add(bounds[0], "delivered_kwh", kwh)
        items = []
        for stamp, values in sorted(groups.items()):
            usage_kwh, cost = _sum_known(values["net_kwh"]), _sum_known(values["cost"])
            key = stamp.astimezone(zone).isoformat() if bucket == "hour" else stamp.isoformat()
            row = {bucket: key, "net_kwh": usage_kwh, "cost": cost, "peak_kw": max(values["peak_kw"], default=None),
                   "price_per_kwh": _safe_rate(cost, usage_kwh) if all(v is not None for v in values["cost"] + values["net_kwh"]) else None}
            if bucket == "date":
                row["delivered_kwh"] = _sum_known(values["delivered_kwh"])
                row.update({k: weather.get(key, {}).get(k) for k in ("min_temperature", "mean_temperature", "max_temperature")})
            items.append(row)
        return items

    daily, hourly = aggregate("date"), aggregate("hour")
    bill_history = []
    for r in datasets["bills"]:
        if not belongs(r):
            continue
        interval = r.get("usageInterval") or r.get("timeInterval")
        bounds = _bounds(interval, zone)
        kwh, amount = _float(r.get("kwh")), _float(r.get("currentAmount"))
        days = (bounds[1].astimezone(zone).date() - bounds[0].astimezone(zone).date()).days if bounds else None
        bill_history.append({"bill_urn": r.get("bill_urn"), "segment_urn": r.get("segment_urn"), "timeInterval": r.get("timeInterval"),
                             "usageInterval": r.get("usageInterval"), "interval": interval,
                             "t": _utc_label(bounds[1]) if bounds else None, "period_label": _period_label(interval, zone),
                             "billing_days": days, "serviceType": r.get("serviceType"), "estimated": str(r.get("estimated")).lower() == "true",
                             "kwh": kwh, "usageCharges": _float(r.get("usageCharges")), "currentAmount": amount,
                             "effective_price_per_kwh": _safe_rate(amount, kwh)})
    bill_history.sort(key=lambda b: b["t"] or "")
    demand_ranked = [demand_events[key] for key in sorted(demand_events, key=lambda key: (-demand_events[key]["kw"], key[1]))]
    price_ranked = [price_series[key] for key in sorted(price_series, key=lambda key: key[1])]
    total_net = _sum_known(r["kwh"] for r in series)
    total_cost = _sum_known(r["cost"] for r in series)
    usage_days = sum(d["net_kwh"] is not None for d in daily)
    cost_days = sum(d["cost"] is not None for d in daily)
    usage_counts = {key: sum(r["usage_source"] == key for r in series) for key in ("actual", "estimated", "unknown", "missing")}
    cost_counts = {key: sum(r["cost_source"] == key for r in series) for key in ("cost_stream", "estimated_cost_stream", "estimated_from_rate", "missing")}
    effective = _safe_rate(total_cost, total_net) if not usage_counts["missing"] and not cost_counts["missing"] else None
    avg_use = total_net / usage_days if total_net is not None and usage_days else None
    avg_cost = total_cost / cost_days if total_cost is not None and cost_days else None
    read_types = defaultdict(int)
    for r in datasets["usage"]:
        if belongs(r):
            read_types[_kind(r) or "unknown"] += 1
    actual_end = max((bounds[1] for r, bounds in usage if belongs(r) and _usage_source(r) == "actual"), default=None)
    baseload = _baseload(hourly[-HOURLY_POINTS:], effective)
    summary = {**_poll_summary(manifest, datasets), "manifest": manifest, "account": account,
               "timezone": zone.key, "meter_zone_known": zone_known,
               "total_net_kwh": total_net, "total_delivered_kwh": _sum_known(kwh for _, kwh in delivered),
               "total_cost": total_cost, "peak_kw": demand_ranked[0]["kw"] if demand_ranked else None,
               "avg_daily_use": avg_use, "avg_daily_cost": avg_cost, "effective_price_per_kwh": effective,
               "read_types": dict(read_types), "cost_sources": cost_counts,
               "price_source": "rated_components" if pricing else "cost_div_kwh" if price_series else None,
               "status": {"latest_actual_net_read_end_utc": _utc_label(actual_end) if actual_end else None,
                          "usage_intervals": usage_counts, "cost_intervals": cost_counts},
               "weather": [weather[day] for day in sorted(weather)[-DAILY_POINTS:]], "bill_history": bill_history,
               "weather_sensitivity": _weather_sensitivity(daily[-DAILY_POINTS:]),
               "baseload": baseload, "anomalies": _anomalies(series[-INTERVAL_POINTS:], daily[-DAILY_POINTS:]),
               "savings_simulator": _savings_simulator(avg_use, baseload, effective),
               "peak_demand_events": demand_ranked[:20],
               "green_button": {**(manifest.get("green_button") or {}), "row_count": len(datasets["green_button_rows"])},
               "service_points": service_points, "service_point_summaries": service_point_summaries,
               "quarter_hour_defs": [
                   {"key": "kwh", "label": "Net kWh", "axis": "left", "default": True},
                   {"key": "cost", "label": "Cost", "axis": "left", "default": True},
                   {"key": "kw", "label": "Demand", "axis": "left", "default": True},
                   {"key": "mean_temperature", "label": "Mean temperature", "axis": "right", "default": False},
               ], "daily": daily, "hourly": hourly, "series": series}
    return summary, price_ranked, demand_ranked


def summarize_run(run_dir: Path) -> dict[str, Any]:
    """Read one legacy CSV snapshot; DB rendering never calls this path."""
    manifest, datasets = _legacy_poll(run_dir)
    zone, _ = _meter_zone(manifest.get("timezone"))
    datasets = _merge_legacy([(manifest, datasets)], zone)
    summary, _, _ = _build_meter_summary(manifest, datasets, manifest.get("account"))
    return {**summary, "run_dir": str(run_dir.resolve())}


def _get_dashboard_inputs(data_dir, db_path, account):
    if db_path is not None:
        from history import History

        with History(Path(db_path)) as history:
            manifest = history.latest_manifest(account)
            if manifest is None:
                return None, [], {}, account
            account = account or manifest.get("account") or manifest.get("utility_id")
            manifests = history.manifests(account)
            if not account:
                # Never ask SQLite to combine readings for unrelated accounts.
                return manifest, [_poll_summary(m) for m in manifests], {name: [] for name in DATASETS}, account
            datasets = {name: history.rows(name, account) for name in DATASETS}
        return manifest, [_poll_summary(m) for m in manifests], datasets, account

    # Legacy preview only. Merge intervals, not daily snapshots, so an
    # overlapping capped poll cannot turn a full day into a partial day.
    paths = sorted(p for p in data_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file()) if data_dir.is_dir() else []
    polls = [(path, *_legacy_poll(path)) for path in paths]
    polls.sort(key=lambda p: _instant(p[1].get("retrieved_at_utc"), timezone.utc) or datetime.min.replace(tzinfo=timezone.utc))
    if not polls:
        return None, [], {}, account
    _, last, _ = polls[-1]
    account = account or last.get("account") or last.get("utility_id")
    if account:
        polls = [p for p in polls if (p[1].get("account") or p[1].get("utility_id")) == account]
    if not polls:
        return None, [], {}, account
    _, manifest, _ = polls[-1]
    zone, _ = _meter_zone(manifest.get("timezone"))
    summaries = []
    for path, m, rows in polls:
        summary = _poll_summary(m, _merge_legacy([(m, rows)], zone), path.resolve())
        net_rows = [r for r in rows["usage"] if _net(r) and (not account or _row_account(r) == account)]
        summary["total_net_kwh"] = _sum_known(_float(r.get("value")) for r in net_rows)
        summaries.append(summary)
    return manifest, summaries, _merge_legacy([(m, rows) for _, m, rows in polls], zone), account


def build_dashboard_data(data_dir: Path = Path("data"), *, db_path: Path | None = None,
                         account: str | None = None, manual_bill_dir: Path | None = None,
                         benchmark_prices: list[dict[str, Any]] | None = None,
                         fetch_eia: bool = False, as_of: datetime | None = None) -> dict[str, Any]:
    """Read canonical history if selected; otherwise preview legacy CSVs.

    Manual statements are opt-in and must belong to the selected account.
    EIA is offline by default. Pass an aware as_of for reproducible forecasts;
    otherwise staleness is checked against the current meter-local calendar day.
    """
    data_dir = Path(data_dir)
    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    manifest, runs, datasets, account = _get_dashboard_inputs(data_dir, db_path, account)
    latest = None
    prices, events, bill_history, hourly_profile = [], [], [], []
    manual_bills = _load_manual_bills(Path(manual_bill_dir)) if manual_bill_dir is not None else []
    if manifest is not None:
        latest, prices, events = _build_meter_summary(manifest, datasets, account)
        zone, _ = _meter_zone(latest["timezone"])
        coverage = _day_coverage(latest["series"], zone, latest["meter_zone_known"], latest["resolution"])
        latest["bill_reconciliation"] = _bill_reconciliation(latest, manual_bills, coverage)
        latest["current_billing_cycle"] = _current_billing_cycle(latest, _latest_physical_bill(manual_bills), coverage,
                                                                as_of or datetime.now(timezone.utc))
        _apply_bill_cost_modes(latest, manual_bills)
        # Legacy alias; do not publish the former unconstrained 30-day extrapolation.
        latest["bill_projection"] = latest["current_billing_cycle"]
        latest["status"].update({"last_poll_at_utc": latest["retrieved_at_utc"],
                                 "last_successful_poll_at_utc": next((r["retrieved_at_utc"] for r in reversed(runs) if r["poll_succeeded"]), None),
                                 "metadata_available_end": next((r["latest_available_end"] for r in reversed(runs) if r["latest_available_end"]), None),
                                 "meter_timezone": latest["timezone"], "meter_zone_known": latest["meter_zone_known"]})
        bill_history = latest["bill_history"][-BILL_POINTS:]
        latest["bill_history"] = bill_history
        latest["series"] = latest["series"][-INTERVAL_POINTS:]
        latest["hourly"] = latest["hourly"][-HOURLY_POINTS:]
        latest["daily"] = latest["daily"][-DAILY_POINTS:]
        hourly_profile = _hourly_profile(latest["hourly"])

    benchmarks = benchmark_prices
    if benchmarks is None:
        benchmarks = _fetch_eia_benchmark_prices(os.environ.get("EVERSMART_EIA_STATE", "MA")) if fetch_eia else EIA_BENCHMARK_FALLBACKS
    return {
        # The nominal generation stamp is source-derived unless a clock was
        # explicitly provided, so repeated same-day offline builds are stable.
        "generated_at": _utc_label(as_of) if as_of else (latest or {}).get("retrieved_at_utc"),
        "data_dir": str(Path(db_path).resolve() if db_path is not None else data_dir.resolve()),
        "run_count": len(runs), "latest": latest, "runs": runs[-POLL_POINTS:],
        "errors": [r for r in runs if r["error"]][-POLL_POINTS:],
        "warnings": [r for r in runs if r["warnings"]][-POLL_POINTS:],
        "manual_bills": manual_bills[-BILL_POINTS:], "price_benchmarks": [dict(row) for row in benchmarks],
        "run_diff": _run_diff(runs), "hourly_profile": hourly_profile,
        "historical_price_series": prices[-INTERVAL_POINTS:], "bill_trend": bill_history,
        "demand_event_series": events[:DEMAND_POINTS],
    }


def _safe_json(value):
    return (json.dumps(value, separators=(",", ":"), allow_nan=False)
            .replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026"))


def _atomic_write(path: Path, text: str) -> None:
    # Same directory guarantees same filesystem; mkstemp starts private.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _shareable_html(data):
    """Share only three numeric aggregates across the retained meter history.

    Net kWh can include estimated reads, cost is a non-statement estimate,
    and peak is the highest recorded demand. Never embed private data or
    arbitrary strings, even as hidden metadata or script variables.
    """
    latest = data.get("latest") or {}
    summary = {"total_net_kwh": _float(latest.get("total_net_kwh")),
               "estimated_interval_cost_usd": _float(latest.get("total_cost")),
               "peak_demand_kw": _float(latest.get("peak_kw"))}

    def display(key, unit):
        value = summary[key]
        return "Unavailable" if value is None else f"{value:,.2f} {unit}"

    return f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eversmart aggregate summary</title><body><main>
<h1>Energy summary</h1><h2>Aggregates shared</h2>
<p>Net energy and estimated interval cost are sums of available readings (which may include estimates); peak demand is the highest recorded value. Cost is not a bill amount.</p>
<dl><dt>Net energy</dt><dd>{display("total_net_kwh", "kWh")}</dd>
<dt>Estimated interval cost</dt><dd>{display("estimated_interval_cost_usd", "USD")}</dd>
<dt>Peak demand</dt><dd>{display("peak_demand_kw", "kW")}</dd></dl>
<script type="application/json" id="dashboard-summary">{_safe_json(summary)}</script>
</main></body></html>'''


def generate_dashboard(data_dir: Path = Path("data"), output: Path = Path(DASHBOARD_PATH), split_data: bool = False,
                       *, db_path: Path | None = None, account: str | None = None, shareable: bool = False,
                       manual_bill_dir: Path | None = None, benchmark_prices: list[dict[str, Any]] | None = None,
                       fetch_eia: bool = False, as_of: datetime | None = None) -> Path:
    """Publish offline HTML atomically. Split mode retains immutable JS companions.

    Shareable mode always writes a single aggregate-only HTML file, regardless
    of split_data. Private companions from older renders are never shared or
    removed automatically, since another reader may still have that HTML open.
    """
    output = Path(output)
    data = build_dashboard_data(data_dir, db_path=db_path, account=account, manual_bill_dir=manual_bill_dir,
                                benchmark_prices=benchmark_prices, fetch_eia=fetch_eia, as_of=as_of)
    if shareable:
        html = _shareable_html(data)
    else:
        payload = _safe_json(data)
        if split_data:
            js = f"window.DASHBOARD_DATA={payload};\n"
            name = f"dashboard-data-{hashlib.sha256(js.encode('utf-8')).hexdigest()}.js"
            _atomic_write(output.with_name(name), js)
            html = HTML_TEMPLATE.replace("<script>const DASHBOARD_DATA=__DASHBOARD_JSON__;</script>", f'<script src="{name}"></script>')
        else:
            html = HTML_TEMPLATE.replace("__DASHBOARD_JSON__", payload)
    _atomic_write(output, html)
    return output.resolve()


HTML_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Eversmart Energy Observatory</title>
  <style>
    :root{--bg:#08090a;--panel:#0f1011;--surface:#17181b;--line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.05);--text:#f7f8f8;--muted:#8a8f98;--soft:#d0d6e0;--dim:#62666d;--accent:#7170ff;--accent2:#828fff;--good:#10b981;--warn:#f59e0b;--bad:#ef4444;--cyan:#22d3ee;--green:#34d399;--pink:#f472b6;--shadow:0 24px 80px rgba(0,0,0,.45)}
    *{box-sizing:border-box} html,body{margin:0;min-height:100%;background:radial-gradient(circle at 20% -10%,rgba(113,112,255,.22),transparent 34%),radial-gradient(circle at 90% 0%,rgba(34,211,238,.12),transparent 30%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-feature-settings:"cv01","ss03"} body{padding:28px}.wrap{max-width:1440px;margin:0 auto}
    header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:22px}.eyebrow{font:510 12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent2);letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px}h1{font-size:clamp(34px,5vw,68px);line-height:.95;letter-spacing:-1.3px;margin:0;font-weight:510}.sub{color:var(--muted);font-size:16px;line-height:1.55;max-width:760px;margin-top:14px}.status{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.pill{border:1px solid var(--line);background:rgba(255,255,255,.03);border-radius:999px;padding:9px 12px;font-size:12px;color:var(--soft);display:flex;gap:8px;align-items:center;white-space:nowrap}.dot{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 20px var(--good)}.dot.warn{background:var(--warn);box-shadow:0 0 20px var(--warn)}.dot.bad{background:var(--bad);box-shadow:0 0 20px var(--bad)}
    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}.card{background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.018));border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}.card.pad{padding:18px}.metric{grid-column:span 3;min-height:150px;position:relative}.metric.compact{grid-column:span 2}.metric .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.metric .value{font-size:clamp(25px,2.3vw,34px);letter-spacing:-.8px;margin-top:12px;font-weight:590}.metric .hint{color:var(--dim);font-size:13px;margin-top:8px}.metric:after{content:"";position:absolute;inset:auto 18px 0 18px;height:3px;border-radius:9px;background:linear-gradient(90deg,var(--accent),transparent)}
    .wide{grid-column:span 8}.side{grid-column:span 4}.half{grid-column:span 6}.full{grid-column:1/-1}.section-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.section-title h2{font-size:18px;letter-spacing:-.2px;margin:0;font-weight:590}.section-title span{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim)}canvas{width:100%;height:320px;display:block}.mini canvas{height:190px}.table{width:100%;border-collapse:collapse;font-size:13px}.table th{text-align:left;color:var(--dim);font:510 11px ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:.07em;padding:10px;border-bottom:1px solid var(--line2)}.table td{padding:10px;border-bottom:1px solid var(--line2);color:var(--soft)}.table tr:last-child td{border-bottom:0}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.ok{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}
    .timeline{display:flex;gap:8px;align-items:end;height:130px;padding-top:12px}.bar{flex:1;min-width:8px;border-radius:6px 6px 2px 2px;background:linear-gradient(180deg,var(--accent2),rgba(113,112,255,.28));position:relative}.bar.err{background:linear-gradient(180deg,var(--bad),rgba(239,68,68,.25))}.kv{display:grid;grid-template-columns:150px 1fr;gap:8px;font-size:13px}.kv div:nth-child(odd){color:var(--dim)}.kv div:nth-child(even){color:var(--soft);overflow-wrap:anywhere}.empty{padding:60px;text-align:center;color:var(--muted)}footer{color:var(--dim);font-size:12px;margin:18px 0 4px;text-align:center}.tabs{display:flex;gap:8px;margin-bottom:12px}.tab{cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--muted);padding:7px 10px;border-radius:9px;font-size:12px}.tab.active{color:var(--text);background:rgba(113,112,255,.18);border-color:rgba(113,112,255,.45)}
    .filters{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 12px}.check{cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--soft);padding:8px 10px;border-radius:9px;font-size:12px;user-select:none}.check input{accent-color:var(--accent);vertical-align:-2px;margin-right:6px}.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font:12px ui-monospace,SFMono-Regular,Menlo,monospace;margin-top:10px}.legend span:before{content:"";display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--c);margin-right:6px;vertical-align:-1px}
    #tooltip{position:fixed;pointer-events:none;z-index:1000;display:none;max-width:360px;background:rgba(15,16,17,.96);border:1px solid var(--line);border-radius:12px;padding:11px 12px;box-shadow:0 20px 60px rgba(0,0,0,.55);font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--soft);white-space:pre}.tip-title{color:var(--text);font-weight:700}.tip-accent{color:var(--accent2)}
    @media(max-width:1120px){.metric.compact{grid-column:span 4}.metric{grid-column:span 6}.wide,.side,.half{grid-column:1/-1}header{display:block}.status{justify-content:flex-start;margin-top:16px}}@media(max-width:640px){body{padding:14px}.metric,.metric.compact{grid-column:1/-1}.kv{grid-template-columns:1fr}.status{display:block}.pill{margin-bottom:8px}}
  </style>
</head>
<body><div class="wrap"><header><div><div class="eyebrow">Eversmart local dashboard</div><h1>Energy Observatory</h1><div class="sub" id="subtitle">Static dark-mode dashboard for Eversource smart-meter pulls.</div></div><div class="status" id="statusPills"></div></header><main id="app"></main><footer id="footer"></footer></div><div id="tooltip" role="status" aria-live="polite"></div>
<script>const DASHBOARD_DATA=__DASHBOARD_JSON__;</script>
<script>
const $=id=>document.getElementById(id), tip=$('tooltip'), latest=DASHBOARD_DATA.latest;
const fmt=(n,d=2)=>finiteOrNull(n)!=null?(+n).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d}):'—';
const money=n=>finiteOrNull(n)==null?'—':'$'+fmt(n,2), rate=n=>finiteOrNull(n)==null?'—':'$'+fmt(n,3)+'/kWh';
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function metric(label,value,hint,compact=false){return `<section class="card pad metric ${compact?'compact':''}" title="${esc(label)}: ${esc(value)} — ${esc(hint)}"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></section>`}
function showTip(html,x,y){tip.innerHTML=html;tip.style.display='block';const pad=16,w=tip.offsetWidth,h=tip.offsetHeight;tip.style.left=Math.min(x+14,innerWidth-w-pad)+'px';tip.style.top=Math.min(y+14,innerHeight-h-pad)+'px'}
function hideTip(){tip.style.display='none'}
function showRunTip(run,evt){let lines=[`<span class="tip-title">${esc(run.retrieved_label)}</span>`,`${run.usage_rows} usage rows · ${run.cost_rows} cost rows`,`${fmt(run.total_net_kwh,3)} kWh · ${money(run.total_cost)}`,`$/kWh ${rate(run.effective_price_per_kwh)} · peak ${fmt(run.peak_kw,3)} kW`,run.error?'ERROR: '+esc(JSON.stringify(run.error)):(Object.keys(run.warnings||{}).length?'optional: '+esc(Object.keys(run.warnings).join(', ')):(run.poll_succeeded?'ok':'failed'))];showTip(lines.join('\n'),evt.clientX,evt.clientY)}
function timeLabel(s){if(!s)return 'unknown'; try{return new Date(s).toLocaleString(undefined,{timeZone:latest.timezone,timeZoneName:'short'})}catch{return s}}
function shortDateLabel(s){if(!s)return '';let d=new Date(s);if(Number.isNaN(d.getTime()))return String(s).slice(0,10);return d.toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:latest.timezone})}
function xLabelFor(row,opt={}){if(row?.hour_of_day!=null)return String(row.hour_of_day).padStart(2,'0')+':00';if(row?.hour)return opt.timeLabels==='date'?String(row.hour).slice(5,10):String(row.hour).slice(5,16).replace('T',' ')+' '+String(row.hour).slice(19);if(opt.timeLabels&&row?.t){let d=new Date(row.t);if(!Number.isNaN(d.getTime()))return opt.timeLabels==='hour'?d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit',timeZone:latest.timezone,timeZoneName:'short'}):d.toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:latest.timezone})}return row?.date||shortDateLabel(row?.t)||row?.period_label||(row?.interval||row?.timeInterval||'').split('/')[0].slice(0,10)}
function finiteOrNull(raw){if(raw==null||raw==='')return null;let v=+raw;return Number.isFinite(v)?v:null}
function drawXAxisLabels(ctx,items,xFor,y,opt={}){let n=items.length;if(!n)return;let maxTicks=opt.maxTicks||6,step=Math.max(1,Math.ceil(n/maxTicks));ctx.save();ctx.fillStyle='rgba(208,214,224,.72)';ctx.strokeStyle='rgba(255,255,255,.10)';ctx.font='11px ui-monospace';ctx.textAlign='center';ctx.textBaseline='top';for(let i=0;i<n;i+=step){let label=xLabelFor(items[i],opt);if(!label)continue;let x=xFor(i);ctx.beginPath();ctx.moveTo(x,y-8);ctx.lineTo(x,y-3);ctx.stroke();ctx.fillText(label,x,y)}if(n>1&&(n-1)%step!==0){let label=xLabelFor(items[n-1],opt);let x=xFor(n-1);ctx.beginPath();ctx.moveTo(x,y-8);ctx.lineTo(x,y-3);ctx.stroke();ctx.fillText(label,x,y)}ctx.restore()}
function lineChart(canvas,series,defs,opt={}){const ctx=canvas.getContext('2d'),dpr=devicePixelRatio||1,rect=canvas.getBoundingClientRect();canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;ctx.scale(dpr,dpr);const W=rect.width,H=rect.height,pt=34,pb=56,pl=42,rp=opt.rightAxis?46:42,plotH=H-pt-pb;ctx.clearRect(0,0,W,H);const xFor=i=>pl+(W-pl-rp)*(series.length<=1?0:i/(series.length-1));ctx.strokeStyle='rgba(255,255,255,.07)';ctx.lineWidth=1;for(let i=0;i<5;i++){let y=pt+plotH*i/4;ctx.beginPath();ctx.moveTo(pl,y);ctx.lineTo(W-rp,y);ctx.stroke()}const domain=axis=>{const vals=[];defs.filter(d=>(d.axis||'left')===axis).forEach(def=>series.forEach(x=>{let v=finiteOrNull(x[def.key]);if(v!=null)vals.push(v)}));if(!vals.length)return {min:0,max:1};let min=Math.min(...vals),max=Math.max(...vals);if(opt.zero!==false&&min>0)min=0;if(min===max){max+=1;min-=1}return {min,max}};const left=domain('left'),right=domain('right');const yFor=(v,axis)=>{const d=(axis||'left')==='right'?right:left;return pt+plotH-plotH*(v-d.min)/(d.max-d.min||1)};defs.forEach(def=>{ctx.beginPath();let started=false;series.forEach((x,i)=>{let raw=x[def.key],v=finiteOrNull(raw);if(v==null){started=false;return;}let xx=xFor(i),yy=yFor(v,def.axis);if(started)ctx.lineTo(xx,yy);else{ctx.moveTo(xx,yy);started=true}});ctx.strokeStyle=def.color;ctx.lineWidth=def.axis==='right'?1.7:2.2;if(def.dash)ctx.setLineDash(def.dash);ctx.stroke();ctx.setLineDash([])});ctx.fillStyle='rgba(208,214,224,.8)';ctx.font='11px ui-monospace';ctx.textAlign='left';ctx.fillText(fmt(left.max,opt.yDigits??2),8,pt+4);ctx.fillText(fmt(left.min,opt.yDigits??2),8,pt+plotH+4);if(opt.rightAxis){ctx.fillStyle='rgba(244,114,182,.85)';ctx.fillText(fmt(right.max,opt.rightDigits??1),W-rp+8,pt+4);ctx.fillText(fmt(right.min,opt.rightDigits??1),W-rp+8,pt+plotH+4)}drawXAxisLabels(ctx,series,xFor,H-pb+18,opt);canvas.onmousemove=e=>{if(!series.length)return;let r=canvas.getBoundingClientRect(),x=e.clientX-r.left,idx=Math.round((x-pl)/(W-pl-rp)*(series.length-1));idx=Math.max(0,Math.min(series.length-1,idx));let row=series[idx];let lines=[`<span class="tip-title">${esc(opt.title||'Reading')}</span>`,`<span class="tip-accent">${esc(timeLabel(row.t))}</span>`,esc(row.interval||row.period_label||'')];defs.forEach(def=>{let v=row[def.key];if(v!=null)lines.push(`${def.label}: ${def.format?def.format(v):fmt(v,3)}`)});if(row.price_per_kwh!=null)lines.push(`Effective price: ${rate(row.price_per_kwh)}`);if(row.mean_temperature!=null)lines.push(`Temp: ${fmt(row.mean_temperature,1)}°F mean (${fmt(row.min_temperature,1)}–${fmt(row.max_temperature,1)}°F)`);if(row.usage_source)lines.push(`Usage: ${esc(row.usage_source)}`);if(row.cost_source)lines.push(`Cost source: ${esc(row.cost_source)}`);showTip(lines.join('\n'),e.clientX,e.clientY)};canvas.onmouseleave=hideTip}
function renderDemandHeatmap(el,rows){rows=rows||[];if(!rows.length){el.innerHTML='<div class="empty">No demand intervals returned.</div>';return}let vals=rows.map(r=>+r.kw||0),max=Math.max(...vals,.001),sorted=rows.slice().sort((a,b)=>String(a.t||a.interval).localeCompare(String(b.t||b.interval)));el.innerHTML='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(54px,1fr));gap:6px">'+sorted.map(r=>{let pct=(+r.kw||0)/max,dt=new Date(r.t||r.interval),label=Number.isNaN(dt.getTime())?String(r.interval||'').slice(0,16):dt.toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',timeZone:latest.timezone,timeZoneName:'short'});return `<div class="check" style="border-color:rgba(34,211,238,${.2+.6*pct});background:rgba(34,211,238,${.05+.28*pct})" title="${esc(label)} · ${esc(r.duration_label||'')} · est ${money(r.estimated_cost_at_peak)}">${esc(label)}<br><b>${fmt(r.kw,2)}</b> kW<br><span style="font-size:11px;color:var(--muted)">${esc(r.duration_label||'')}</span></div>`}).join('')+'</div>'}
function barChart(canvas,rows,key,color,opt={}){const ctx=canvas.getContext('2d'),dpr=devicePixelRatio||1,rect=canvas.getBoundingClientRect();canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;ctx.scale(dpr,dpr);const W=rect.width,H=rect.height,pt=24,pb=48,pl=34,plotH=H-pt-pb,max=Math.max(...rows.map(r=>+r[key]||0),.001);ctx.clearRect(0,0,W,H);const bw=(W-pl*2)/Math.max(rows.length,1)*.68;rows.forEach((r,i)=>{if(finiteOrNull(r[key])==null)return;let x=pl+(W-pl*2)*i/rows.length+bw*.25,h=plotH*(+r[key]||0)/max,g=ctx.createLinearGradient(0,pt+plotH-h,0,pt+plotH);g.addColorStop(0,color);g.addColorStop(1,'rgba(113,112,255,.18)');ctx.fillStyle=g;ctx.beginPath();ctx.roundRect(x,pt+plotH-h,bw,h,6);ctx.fill()});drawXAxisLabels(ctx,rows,i=>pl+(W-pl*2)*(i+.5)/Math.max(rows.length,1),H-pb+14,{maxTicks:opt.maxTicks||7,timeLabels:opt.timeLabels});canvas.onmousemove=e=>{if(!rows.length)return;let rct=canvas.getBoundingClientRect(),x=e.clientX-rct.left,idx=Math.floor((x-pl)/(W-pl*2)*rows.length);idx=Math.max(0,Math.min(rows.length-1,idx));let r=rows[idx],lines=[`<span class="tip-title">${esc(opt.title||'Bar')}</span>`,`Date/hour: ${esc(r.date||r.hour)}`,`Net usage: ${fmt(r.net_kwh,3)} kWh`,`Cost: ${money(r.cost)}`,`Effective price: ${rate(r.price_per_kwh)}`,`Peak demand: ${fmt(r.peak_kw,3)} kW`];if(r.mean_temperature!=null)lines.push(`Weather: ${fmt(r.mean_temperature,1)}°F mean (${fmt(r.min_temperature,1)}–${fmt(r.max_temperature,1)}°F)`);showTip(lines.join('\n'),e.clientX,e.clientY)};canvas.onmouseleave=hideTip}
function rowsToCsv(rows){if(!rows||!rows.length)return '';let keys=Array.from(rows.reduce((s,r)=>{Object.keys(r||{}).forEach(k=>s.add(k));return s},new Set()));let cell=v=>{let s=String(v??'');if(typeof v==='string'&&/^[\s]*[=+@-]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"'};return keys.join(',')+'\n'+rows.map(r=>keys.map(k=>cell(r[k])).join(',')).join('\n')}
function downloadCsv(name,csv){let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download=name;a.click();URL.revokeObjectURL(a.href)}
function downloadJson(){let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(DASHBOARD_DATA,null,2)],{type:'application/json'}));a.download='dashboard-data-private.json';a.click();URL.revokeObjectURL(a.href)}
const cycleReasons={unknown_cycle:'Unknown cycle or meter zone',stale_billing_cycle:'Newer electricity bill required',no_complete_days:'No completed days in this cycle',no_actual_reads:'No actual reads for completed days',incomplete_usage_coverage:'Missing, partial or estimated meter days',unknown_rate:'Physical statement rate unavailable'};
const cycleSources={latest_electricity_segment:'latest electricity segment',physical_statement:'physical statement'};
function billReconciliationCsv(){let r=latest.bill_reconciliation||{};return rowsToCsv([r])}
function render(){if(!latest){$('app').innerHTML='<div class="card empty">No data runs found. Run <span class="mono">python3 eversmart.py --out data</span>.</div>';return}const hasErr=DASHBOARD_DATA.errors.length>0,warnCount=(DASHBOARD_DATA.warnings||[]).length;const counts=latest.dataset_counts||{},rec=latest.bill_reconciliation||null,cycle=latest.current_billing_cycle||{},costModes=latest.bill_cost_modes||{},status=latest.status||{},usageCounts=status.usage_intervals||{},costCounts=status.cost_intervals||{};$('statusPills').innerHTML=`<div class="pill"><span class="dot ${hasErr?'bad':(warnCount?'warn':'')}"></span>${hasErr?'Errors seen':(warnCount?'Optional warnings':'All green')}</div><div class="pill mono">Last poll ${esc(latest.retrieved_label)}</div><div class="pill mono">Actual read through ${esc(status.latest_actual_net_read_end_utc||'unknown')}</div><div class="pill mono">${DASHBOARD_DATA.run_count} runs</div><div class="pill">Private dashboard</div>`;$('subtitle').innerHTML=`Account <span class="mono" id="accountLabel">${esc(latest.account)}</span> · ${esc(latest.resolution)} · Meter zone: ${esc(latest.timezone)}${latest.meter_zone_known?'':' (display fallback)'} · price source: <span class="mono">${esc(latest.price_source)}</span> · source poll ${esc(latest.retrieved_label)}`;
$('app').innerHTML=`<div class="grid">
${metric('Net usage',fmt(latest.total_net_kwh,3)+' kWh','Available canonical readings; may include estimates',true)}${metric('Estimated cost',money(latest.total_cost),'Cost stream plus rated-component estimates when cost rows lag',true)}${metric('Peak demand',fmt(latest.peak_kw,3)+' kW','Highest recorded canonical demand',true)}${metric('Avg daily use',fmt(latest.avg_daily_use,3)+' kWh/day','Total divided by returned days',true)}${metric('Avg daily cost',money(latest.avg_daily_cost)+'/day','Cost divided by returned days',true)}${metric('Avg kWh price',rate(latest.effective_price_per_kwh),'Total cost ÷ net kWh',true)}
<section class="card pad half"><div class="section-title"><h2>Bill reconciliation</h2><span>Physical bill vs smart-meter overlap</span></div><div class="kv"><div>Status</div><div class="${rec?.status==='matched'?'ok':(rec?'warn':'warn')}">${rec?esc(rec.status):'No physical bill loaded'}</div><div>Physical bill</div><div>${rec?fmt(rec.total_billed_kwh,1)+' kWh · '+money(rec.total_due):'—'}</div><div>New meter overlap</div><div>${rec?fmt(rec.new_meter_bill_kwh,3)+' billed kWh vs '+fmt(rec.smart_meter_overlap_kwh,3)+' smart kWh':'—'}</div><div>Difference</div><div>${rec?fmt(rec.difference_kwh,3)+' kWh (tolerance '+fmt(rec.tolerance_kwh,3)+')':'—'}</div><div>Actual intervals covered</div><div>${rec?fmt(rec.observed_actual_intervals,0)+' of '+fmt(rec.expected_intervals,0):'—'}</div><div>Meter-change awareness</div><div>${rec?esc(rec.explanation):'No meter split detected yet'}</div></div></section>
<section class="card pad half"><div class="section-title"><h2>Physical bill</h2><span>Locally parsed statement</span></div><div class="kv"><div>Statement / due</div><div>${rec?esc(rec.statement_date)+' / '+esc(rec.due_date):'—'}</div><div>Billing period</div><div>${rec?esc(rec.physical_bill?.billing_period_start)+' – '+esc(rec.physical_bill?.billing_period_end):'—'}</div><div>${rec?.meter_change_date?'Old meter portion':'Smart-meter usage'}</div><div>${rec?fmt(rec.meter_change_date?rec.old_meter_bill_kwh:rec.new_meter_bill_kwh,1)+' kWh':'—'}</div><div>Variable bill rate</div><div>${rate(costModes.bill_calibrated_variable_rate)} · all-in ${rate(costModes.all_in_allocated_rate)}</div></div><div class="sub" style="font-size:13px">The private dashboard and its exports contain account and usage data. Use the separate shareable render for aggregate-only output.</div></section>
<section class="card pad half"><div class="section-title"><h2>Current billing cycle</h2><span>post-bill, statement-calibrated forecast</span></div><div class="kv"><div>Forecast status</div><div>${cycle.status==='available'?'Available':esc(cycleReasons[cycle.reason]||'Unavailable')}</div><div>Cycle start / expected end</div><div>${esc(cycle.cycle_start_date||'—')} / ${esc(cycle.expected_end_date||'—')}</div><div>Likely cycle length</div><div>${cycle.billing_days?fmt(cycle.billing_days,0)+' days · '+esc(cycleSources[cycle.cycle_length_source]):'—'}</div><div>Latest electricity segment end</div><div>${esc(cycle.latest_electricity_segment_end_utc||'—')}</div><div>Calibration statement</div><div>${esc(cycle.rate_statement_date||'—')}</div><div>Observed so far</div><div>${cycle.status==='available'?cycle.observed_days+' days · '+fmt(cycle.kwh_so_far,1)+' kWh':'—'}</div><div>Meter-data estimate</div><div>${cycle.opower_interval_cost_so_far!=null?money(cycle.opower_interval_cost_so_far)+' interval only':'—'}</div><div>Bill-calibrated variable</div><div>${cycle.status==='available'?money(cycle.bill_calibrated_variable_cost_so_far)+' at '+rate(cycle.variable_rate_per_kwh):'—'}</div><div>Projected usage</div><div>${cycle.status==='available'?fmt(cycle.projected_kwh,1)+' kWh':'—'}</div><div>Projected total bill</div><div>${cycle.status==='available'?money(cycle.projected_cost)+' incl. '+money(cycle.fixed_charge)+' customer charge':'—'}</div></div><div class="sub" style="font-size:13px">${esc(cycle.cost_basis||'Forecast requires complete actual reads for every elapsed local calendar day and known physical statement rates.') }</div></section>
<section class="card pad half"><div class="section-title"><h2>Exports</h2><span>private downloads · do not share</span></div><div class="filters"><button class="check" onclick="downloadJson()">Private dashboard JSON</button><button class="check" onclick="downloadCsv('bill-reconciliation.csv',billReconciliationCsv())">Export bill reconciliation CSV</button><button class="check" onclick="downloadCsv('daily-usage.csv',rowsToCsv(latest.daily||[]))">Export daily/hourly CSV</button></div><div id="costSourceLegend" class="legend"><span style="--c:#34d399">Opower interval cost</span><span style="--c:#f472b6">billCalibratedVariableCost</span><span style="--c:#22d3ee">allInAllocatedCost</span></div></section>
<section class="card pad full"><div class="section-title"><h2>Physical bill history</h2><span>deduplicated PDF statements · newest bill drives cost calibration</span></div><table class="table"><thead><tr><th>Statement</th><th>Billing period</th><th>Usage</th><th>Current charges</th><th>All-in rate</th><th>Source</th></tr></thead><tbody>${(DASHBOARD_DATA.manual_bills||[]).filter(b=>!b.error).slice().reverse().map(b=>`<tr><td>${esc(b.statement_date)}<br><span style="color:var(--dim)">due ${esc(b.due_date)}</span></td><td>${esc(b.billing_period_start||'—')} – ${esc(b.billing_period_end||'—')}</td><td>${fmt(b.total_billed_kwh,1)} kWh</td><td>${money(b.total_current_charges)}</td><td>${rate(b.all_in_rate_per_kwh)}</td><td class="mono">${esc(b.source)}</td></tr>`).join('')||'<tr><td colspan="6" class="warn">No physical bill PDFs loaded.</td></tr>'}</tbody></table></section>
<section class="card pad full"><div class="section-title"><h2>Line-item breakdown</h2><span id="lineItemBreakdown">latest physical bill · supply, delivery, credits, fixed charges</span></div><table class="table"><thead><tr><th>Category</th><th>Description</th><th>kWh</th><th>Rate</th><th>Amount</th></tr></thead><tbody>${(latest.line_item_breakdown||[]).map(li=>`<tr><td>${esc(li.category||'other')}</td><td>${esc(li.description)}</td><td>${li.kwh==null?'—':fmt(li.kwh,1)}</td><td>${li.rate==null?'—':rate(li.rate)}</td><td class="${+li.amount<0?'ok':''}">${money(li.amount)}</td></tr>`).join('')||'<tr><td colspan="5" class="warn">No physical bill line items loaded.</td></tr>'}</tbody></table></section>
<section class="card pad wide"><div class="section-title"><h2>Quarter-hour profile</h2><span>independent toggles; temperature uses right axis</span></div><div class="filters" id="qhFilters"></div><canvas id="usageChart"></canvas><div class="legend" id="qhLegend"></div></section>
<section class="card pad side"><div class="section-title"><h2>Coverage and run health</h2><span>poll history</span></div><div class="timeline">${DASHBOARD_DATA.runs.map(r=>`<div class="bar ${!r.poll_succeeded?'err':''}" style="height:${Math.max(8,Math.min(120,(r.usage_rows||1)/(latest.usage_rows||1)*120))}px" data-tip-id="run-${DASHBOARD_DATA.runs.indexOf(r)}"></div>`).join('')}</div><div class="kv" style="margin-top:18px"><div>Last successful poll</div><div class="mono">${esc(status.last_successful_poll_at_utc||'unknown')}</div><div>Latest ACTUAL net read</div><div class="mono">${esc(status.latest_actual_net_read_end_utc||'unknown')}</div><div>Metadata available through</div><div class="mono">${esc(status.metadata_available_end||'unknown')}</div><div>Data source</div><div class="mono">${esc(DASHBOARD_DATA.data_dir)}</div><div>Available interval</div><div class="mono">${esc(latest.available_interval)}</div><div>Requested interval</div><div class="mono">${esc(latest.requested_interval)}</div></div></section>
<section class="card pad half mini"><div class="section-title"><h2>Usage by period</h2><span id="usageGranularityLabel">daily usage + weather</span></div><div class="filters" id="usageGranularity"><label class="check"><input type="radio" name="usageGranularity" data-usage-granularity="daily" checked>Daily</label><label class="check"><input type="radio" name="usageGranularity" data-usage-granularity="hourly">Hourly</label></div><canvas id="dailyChart"></canvas></section><section class="card pad half mini"><div class="section-title"><h2>Cost by period</h2><span id="costGranularityLabel">daily cost</span></div><div class="filters" id="costGranularity"><label class="check"><input type="radio" name="costGranularity" data-cost-granularity="daily" checked>Daily</label><label class="check"><input type="radio" name="costGranularity" data-cost-granularity="hourly">Hourly</label></div><canvas id="costChart"></canvas></section>
<section class="card pad half"><div class="section-title"><h2>Bill-cost calibration</h2><span id="billProjection">what the meter-data cost excludes</span></div><div class="kv"><div>Latest bill variable rate</div><div>${rate(costModes.bill_calibrated_variable_rate)}</div><div>Latest bill all-in rate</div><div>${rate(costModes.all_in_allocated_rate)}</div><div>Monthly customer charge</div><div>${money(costModes.customer_charge)}</div><div>Latest meter-window rate</div><div>${rate(latest.effective_price_per_kwh)} interval estimate</div></div><div class="sub" style="font-size:13px">Use the statement-calibrated current-cycle forecast for a bill estimate. The meter-window rate is useful for interval comparisons, but it omits some bill-level charges.</div></section>
<section class="card pad half"><div class="section-title"><h2>What changed since last run?</h2><span id="runDiff">run-to-run diff</span></div><div class="kv"><div>New usage rows</div><div>${fmt(DASHBOARD_DATA.run_diff?.new_usage_rows,0)}</div><div>New cost rows</div><div>${fmt(DASHBOARD_DATA.run_diff?.new_cost_rows,0)}</div><div>Usage delta</div><div>${DASHBOARD_DATA.run_diff?fmt(DASHBOARD_DATA.run_diff.kwh_delta,3)+' kWh':'—'}</div><div>Cost delta</div><div>${DASHBOARD_DATA.run_diff?money(DASHBOARD_DATA.run_diff.cost_delta):'—'}</div></div></section>
<section class="card pad half mini"><div class="section-title"><h2>Average usage by hour</h2><span>typical daily shape</span></div><canvas id="hourlyProfileChart"></canvas></section>
<section class="card pad half mini"><div class="section-title"><h2>Weather sensitivity</h2><span>kWh vs temperature</span></div><canvas id="weatherSensitivityChart"></canvas><div class="sub" style="font-size:13px">Correlation: ${latest.weather_sensitivity.correlation==null?'—':fmt(latest.weather_sensitivity.correlation,2)} · mild-day baseline ${latest.weather_sensitivity.baseline_kwh==null?'—':fmt(latest.weather_sensitivity.baseline_kwh,2)+' kWh/day'}</div></section>
<section class="card pad half"><div class="section-title"><h2>Anomaly watch</h2><span id="anomalyTable">high usage/demand outliers</span></div><table class="table"><thead><tr><th>When</th><th>kWh</th><th>Demand</th><th>Cost</th><th>Reason</th></tr></thead><tbody>${(latest.anomalies.intervals||[]).slice(0,8).map(a=>`<tr><td>${esc(timeLabel(a.t))}</td><td>${fmt(a.kwh,3)}</td><td>${fmt(a.kw,3)} kW</td><td>${money(a.cost)}</td><td>${esc(a.reason)}</td></tr>`).join('')||'<tr><td colspan="5" class="ok">No interval anomalies detected.</td></tr>'}</tbody></table></section>
<section class="card pad half"><div class="section-title"><h2>Baseload + savings simulator</h2><span id="baseloadCard">overnight always-on estimate</span></div><div id="savingsSimulator" class="kv"><div>Estimated baseload</div><div>${fmt(latest.baseload.estimated_kw,3)} kW · ${fmt(latest.baseload.monthly_kwh,1)} kWh/month · ${money(latest.baseload.monthly_cost)}/month</div><div>Reduce daily use 10%</div><div>${money(latest.savings_simulator.reduce_daily_10pct.monthly_savings)}/month</div><div>Cut 100W always-on</div><div>${money(latest.savings_simulator.cut_baseload_100w.monthly_savings)}/month</div><div>Cut baseload 25%</div><div>${money(latest.savings_simulator.cut_baseload_25pct.monthly_savings)}/month</div></div></section>
<section class="card pad full"><div class="section-title"><h2>Canonical kWh price over time</h2><span>${DASHBOARD_DATA.historical_price_series.length.toLocaleString()} intervals · benchmark overlays are optional</span></div><div class="filters" id="priceFilters"></div><canvas id="priceChart"></canvas><div class="legend" id="priceLegend"></div><div class="sub" style="font-size:13px;margin:6px 0 0">Eversource canonical price is the rated component returned for each interval. It should be flat unless your actual tariff changes. EIA regional/national values are monthly residential averages repeated as optional comparison lines, not additional Eversource interval observations.</div></section>
<section class="card pad half"><div class="section-title"><h2>Long-term bill trend</h2><span>bills are shown by billing-period end date; table lists covered period</span></div><canvas id="billTrendChart"></canvas><div class="sub" style="font-size:13px;margin:4px 0 10px">Lines compare billed kWh, usage charges, and actual $/kWh for each monthly billing period. Actual $/kWh is total bill amount divided by kWh, not just usage charges. The label is the bill's covered usage interval, not the scraper run time.</div><table class="table"><thead><tr><th>Billing period</th><th>Days</th><th>Usage</th><th>Usage charges</th><th>$/kWh</th><th>Bill amount</th></tr></thead><tbody>${(DASHBOARD_DATA.bill_trend||latest.bill_history||[]).slice().reverse().map(b=>`<tr title="raw interval: ${esc(b.timeInterval||b.usageInterval)}"><td>${esc(b.period_label||b.timeInterval||b.usageInterval)}</td><td>${b.billing_days||'—'}</td><td>${fmt(b.kwh,2)} kWh</td><td>${money(b.usageCharges)}</td><td>${rate(b.effective_price_per_kwh)}</td><td>${money(b.currentAmount)} ${b.estimated?'· estimated':''}</td></tr>`).join('')||'<tr><td colspan="6" class="warn">No bill history rows returned.</td></tr>'}</tbody></table></section>
<section class="card pad half"><div class="section-title"><h2>Peak demand events</h2><span>top intervals plus time-of-day heatmap</span></div><div id="demandHeatmap"></div><canvas id="demandEventChart"></canvas><table class="table"><thead><tr><th>When</th><th>Duration</th><th>Demand</th><th>Est. spike cost</th><th>Temp</th></tr></thead><tbody>${(DASHBOARD_DATA.demand_event_series||latest.peak_demand_events||[]).slice(0,10).map(p=>`<tr><td>${esc(p.label||timeLabel(p.t))}</td><td>${esc(p.duration_label||'—')}</td><td>${fmt(p.kw,3)} kW</td><td>${money(p.estimated_cost_at_peak)} <span style="color:var(--dim)">(${fmt(p.estimated_kwh_at_peak,3)} kWh @ peak)</span></td><td>${p.mean_temperature!=null?fmt(p.mean_temperature,1)+'°F':'—'}</td></tr>`).join('')||'<tr><td colspan="5" class="warn">No demand intervals returned.</td></tr>'}</tbody></table></section>
<section class="card pad half"><div class="section-title"><h2>Dataset coverage</h2><span>normalized tables</span></div><table class="table"><thead><tr><th>Dataset</th><th>Rows</th></tr></thead><tbody>${Object.entries(counts).map(([k,v])=>`<tr><td class="mono">${esc(k)}</td><td>${Number(v||0).toLocaleString()}</td></tr>`).join('')}</tbody></table><div class="kv" style="margin-top:14px"><div>Actual net usage</div><div>${fmt(usageCounts.actual,0)} intervals</div><div>Estimated usage</div><div>${fmt(usageCounts.estimated,0)} intervals</div><div>Unknown / missing usage</div><div>${fmt(usageCounts.unknown,0)} / ${fmt(usageCounts.missing,0)}</div><div>Estimated cost intervals</div><div>${fmt(costCounts.estimated_cost_stream,0)} from cost stream · ${fmt(costCounts.estimated_from_rate,0)} from rate</div><div>Missing cost intervals</div><div>${fmt(costCounts.missing,0)}</div><div>Green Button audit</div><div>${(latest.green_button?.row_count||0).toLocaleString()} parsed rows ${latest.green_button?.bytes?'· '+latest.green_button.bytes.toLocaleString()+' bytes':''}</div></div></section>
<section class="card pad half"><div class="section-title"><h2>Service point usage/cost</h2><span>canonical per-device totals</span></div><table class="table"><thead><tr><th>Utility id</th><th>Total</th><th>Cost</th><th>Daily kWh</th><th>Weekly kWh</th></tr></thead><tbody>${(latest.service_point_summaries||[]).map(sp=>{let days=Object.entries(sp.days||{}).sort().slice(-3).map(([d,v])=>`${d}: ${fmt(v.kwh,2)} (${money(v.cost)})`).join('<br>');let weeks=Object.entries(sp.weeks||{}).sort().slice(-4).map(([w,v])=>`${w}: ${fmt(v.kwh,2)} (${money(v.cost)})`).join('<br>');return `<tr class="${sp.utility_id===latest.account?'ok':''}"><td class="mono">${esc(sp.utility_id)}<br><span style="color:var(--dim)">${esc(sp.service_point_uuid||'')}</span></td><td>${fmt(sp.total_kwh,3)} kWh</td><td>${money(sp.total_cost)}</td><td>${days||'—'}</td><td>${weeks||'—'}</td></tr>`}).join('')||'<tr><td colspan="5" class="warn">No per-service-point rows available. Run with --all-service-points to populate sibling devices.</td></tr>'}</tbody></table></section>
<section class="card pad full"><div class="section-title"><h2>History and diagnostics</h2><span>${DASHBOARD_DATA.errors.length} error runs · ${warnCount} optional warning runs</span></div><table class="table"><thead><tr><th>Poll</th><th>Rows</th><th>Usage</th><th>Cost</th><th>Avg/day</th><th>$/kWh</th><th>Peak</th><th>Status</th></tr></thead><tbody>${DASHBOARD_DATA.runs.slice().reverse().map(r=>{let warn=r.warnings&&Object.keys(r.warnings).length;return `<tr title="${esc(r.run_dir)}"><td class="mono">${esc(r.retrieved_label)}</td><td>${fmt(r.usage_rows,0)}</td><td>${fmt(r.total_net_kwh,3)} kWh</td><td>${money(r.total_cost)}</td><td>${fmt(r.avg_daily_use,2)} kWh · ${money(r.avg_daily_cost)}</td><td>${rate(r.effective_price_per_kwh)}</td><td>${fmt(r.peak_kw,3)} kW</td><td class="${!r.poll_succeeded?'bad':(warn?'warn':'ok')}">${r.error?esc(JSON.stringify(r.error)).slice(0,140):(!r.poll_succeeded?'failed':warn?'optional: '+esc(Object.keys(r.warnings).join(', ')):'ok')}</td></tr>`}).join('')}</tbody></table></section></div>`;
const colors={kwh:'#7170ff',cost:'#34d399',kw:'#22d3ee',mean_temperature:'#f472b6'};
const labels={kwh:'Net kWh',cost:'Cost',kw:'Demand',mean_temperature:'Mean temp'};
const formats={kwh:v=>fmt(v,3)+' kWh',cost:money,kw:v=>fmt(v,3)+' kW',mean_temperature:v=>fmt(v,1)+'°F'};
const qhDefs=(latest.quarter_hour_defs||[{key:'kwh',label:'Net kWh',axis:'left',default:true},{key:'cost',label:'Cost',axis:'left',default:true},{key:'kw',label:'Demand',axis:'left',default:true},{key:'mean_temperature',label:'Mean temperature',axis:'right',default:false}]).map(d=>({...d,color:colors[d.key]||'#d0d6e0',format:formats[d.key],dash:d.axis==='right'?[5,5]:null}));
const active=new Set(qhDefs.filter(d=>d.default).map(d=>d.key));
function selectedDefs(){let defs=qhDefs.filter(d=>active.has(d.key));return defs.length?defs:[qhDefs[0]]}
function updateQhLegend(){$('qhLegend').innerHTML=qhDefs.filter(d=>active.has(d.key)).map(d=>`<span style="--c:${d.color}">${esc(d.label||labels[d.key]||d.key)}${d.axis==='right'?' · right axis':''}</span>`).join('')}
function renderQhControls(){ $('qhFilters').innerHTML=qhDefs.map(d=>`<label class="check"><input type="checkbox" data-qh="${esc(d.key)}" ${active.has(d.key)?'checked':''}>${esc(d.label||labels[d.key]||d.key)}</label>`).join('');updateQhLegend();document.querySelectorAll('[data-qh]').forEach(ch=>ch.onchange=()=>{ch.checked?active.add(ch.dataset.qh):active.delete(ch.dataset.qh);updateQhLegend();drawAll()})}
function drawQuarterHour(){lineChart($('usageChart'),latest.series,selectedDefs(),{title:'Quarter-hour reading',rightAxis:selectedDefs().some(d=>d.axis==='right'),timeLabels:'hour'})}
const priceOverlayDefs=[{key:'price_per_kwh',label:'Eversource canonical',color:'#f472b6',format:rate,default:true},{key:'regional_price',label:'EIA MA avg',color:'#22d3ee',format:rate,dash:[5,5],default:false},{key:'national_price',label:'EIA US avg',color:'#34d399',format:rate,dash:[2,4],default:false}];
const activePrice=new Set(priceOverlayDefs.filter(d=>d.default).map(d=>d.key));
function latestBenchmark(scope){let rows=(DASHBOARD_DATA.price_benchmarks||[]).filter(x=>x.scope===scope&&x.price_per_kwh!=null).sort((a,b)=>String(a.period).localeCompare(String(b.period)));return rows.length?rows[rows.length-1]:null}
function buildPriceSeries(){let regional=latestBenchmark('regional'),national=latestBenchmark('national');return DASHBOARD_DATA.historical_price_series.filter(x=>x.price_per_kwh!=null).map(x=>({...x,regional_price:regional?.price_per_kwh??null,regional_label:regional?.period,national_price:national?.price_per_kwh??null,national_label:national?.period}))}
function selectedPriceDefs(){return priceOverlayDefs.filter(d=>activePrice.has(d.key))}
function updatePriceLegend(){$('priceLegend').innerHTML=selectedPriceDefs().map(d=>`<span style="--c:${d.color}">${esc(d.label)}${d.default?'':' · optional'}</span>`).join('')}
function renderPriceControls(){$('priceFilters').innerHTML=priceOverlayDefs.map(d=>`<label class="check"><input type="checkbox" data-price="${esc(d.key)}" ${activePrice.has(d.key)?'checked':''}>${esc(d.label)}</label>`).join('');updatePriceLegend();document.querySelectorAll('[data-price]').forEach(ch=>ch.onchange=()=>{ch.checked?activePrice.add(ch.dataset.price):activePrice.delete(ch.dataset.price);updatePriceLegend();drawAll()})}
let usageGranularity='daily';
let costGranularity='daily';
function dailyUsageRows(){return latest.daily||[]}
function hourlyUsageRows(){return latest.hourly||[]}
function activeUsageRows(){return usageGranularity==='hourly'?hourlyUsageRows():dailyUsageRows()}
function dailyCostRows(){return latest.daily||[]}
function hourlyCostRows(){return latest.hourly||[]}
function activeCostRows(){return costGranularity==='hourly'?hourlyCostRows():dailyCostRows()}
function renderUsageGranularityControls(){document.querySelectorAll('[data-usage-granularity]').forEach(ch=>ch.onchange=()=>{if(ch.checked){usageGranularity=ch.dataset.usageGranularity;$('usageGranularityLabel').textContent=usageGranularity==='hourly'?'hourly usage':'daily usage + weather';drawAll()}})}
function renderCostGranularityControls(){document.querySelectorAll('[data-cost-granularity]').forEach(ch=>ch.onchange=()=>{if(ch.checked){costGranularity=ch.dataset.costGranularity;$('costGranularityLabel').textContent=costGranularity==='hourly'?'hourly cost':'daily cost';drawAll()}})}
function drawAll(){drawQuarterHour();barChart($('dailyChart'),activeUsageRows(),'net_kwh','#7170ff',{title:usageGranularity==='hourly'?'Hourly usage':'Daily usage',timeLabels:usageGranularity==='hourly'?'hour':'date',maxTicks:usageGranularity==='hourly'?8:7});barChart($('costChart'),activeCostRows(),'cost','#34d399',{title:costGranularity==='hourly'?'Hourly cost':'Daily cost',timeLabels:costGranularity==='hourly'?'hour':'date',maxTicks:costGranularity==='hourly'?8:7});barChart($('hourlyProfileChart'),DASHBOARD_DATA.hourly_profile||[],'avg_kwh','#22d3ee',{title:'Average usage by hour',maxTicks:8});lineChart($('weatherSensitivityChart'),latest.weather_sensitivity.points||[],[{key:'net_kwh',label:'Daily kWh',color:'#7170ff',format:v=>fmt(v,2)+' kWh'},{key:'mean_temperature',label:'Mean temp',color:'#f472b6',format:v=>fmt(v,1)+'°F',axis:'right',dash:[5,5]}],{title:'Weather sensitivity',rightAxis:true,yDigits:2,rightDigits:1});lineChart($('priceChart'),buildPriceSeries(),selectedPriceDefs(),{title:'Canonical price with optional benchmark overlays',yDigits:3,zero:false,timeLabels:'date'});lineChart($('billTrendChart'),DASHBOARD_DATA.bill_trend||[],[{key:'kwh',label:'Bill kWh',color:'#7170ff',format:v=>fmt(v,1)+' kWh'},{key:'usageCharges',label:'Usage charges',color:'#34d399',format:money},{key:'effective_price_per_kwh',label:'Actual $/kWh',color:'#f472b6',format:rate,axis:'right',dash:[5,5]}],{title:'Bill trend',rightAxis:true,yDigits:1,rightDigits:3,zero:false});renderDemandHeatmap($('demandHeatmap'),(DASHBOARD_DATA.demand_event_series||[]).slice(0,48));lineChart($('demandEventChart'),(DASHBOARD_DATA.demand_event_series||[]).slice(0,30).sort((a,b)=>Date.parse(a.t)-Date.parse(b.t)),[{key:'kw',label:'Demand',color:'#22d3ee',format:v=>fmt(v,3)+' kW'},{key:'mean_temperature',label:'Mean temp',color:'#f472b6',format:v=>fmt(v,1)+'°F',axis:'right',dash:[5,5]}],{title:'Peak demand events',rightAxis:true,yDigits:3,rightDigits:1})}
renderQhControls();renderPriceControls();renderUsageGranularityControls();renderCostGranularityControls();document.querySelectorAll('[data-tip-id]').forEach(el=>{el.onmousemove=e=>showRunTip(DASHBOARD_DATA.runs[+el.dataset.tipId.split('-')[1]],e);el.onmouseleave=hideTip});drawAll();window.onresize=drawAll;$('footer').textContent=`Static file: refresh after each scraper run · Source data: ${DASHBOARD_DATA.data_dir}`}
render();
</script></body></html>'''


if __name__ == "__main__":
    print(generate_dashboard())
