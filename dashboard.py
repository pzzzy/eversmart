#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import urllib.parse
import urllib.request
from functools import lru_cache
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

DASHBOARD_PATH = "dashboard.html"


def _float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
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


def _start_time(interval: str) -> str:
    return (interval or "").split("/", 1)[0]


def _date_key(interval: str) -> str:
    start = _start_time(interval)
    return start[:10] if len(start) >= 10 else "unknown"


def _hour_key(interval: str) -> str:
    start = _start_time(interval)
    return start[:13] + ":00" if len(start) >= 13 else "unknown"


def _iso_to_label(ts: str) -> str:
    if not ts:
        return "unknown"
    try:
        dt = datetime.strptime(ts, "%Y%m%dT%H%M%SZ")
        return dt.strftime("%b %d, %Y %H:%M UTC")
    except ValueError:
        return ts


def _safe_rate(cost: float, kwh: float) -> float | None:
    return cost / kwh if kwh > 0 else None


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
    return sorted(out, key=lambda r: (r.get("period") or "", r.get("scope") or ""))


def _parse_interval_datetime(value: str) -> datetime | None:
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _period_label(interval: str) -> str:
    start_s, _, end_s = (interval or "").partition("/")
    start = _parse_interval_datetime(start_s)
    end = _parse_interval_datetime(end_s)
    if not start or not end:
        return interval or "unknown"
    if start.year == end.year:
        return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"


def _period_days(interval: str) -> int | None:
    start_s, _, end_s = (interval or "").partition("/")
    start = _parse_interval_datetime(start_s)
    end = _parse_interval_datetime(end_s)
    if not start or not end:
        return None
    return max(1, round((end - start).total_seconds() / 86400))


def _iso_week_key(interval: str) -> str:
    dt = _parse_interval_datetime(_start_time(interval))
    if not dt:
        return "unknown"
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _round_money(value: float) -> float:
    return round(value + 0.0000000001, 6)


OPTIONAL_ERROR_KEYS = {"bill_forecast"}


def _split_run_issues(manifest: dict[str, Any]) -> tuple[Any | None, dict[str, Any]]:
    warnings = dict(manifest.get("warnings") or {})
    raw_errors = dict(manifest.get("errors") or {})
    for key in list(raw_errors):
        if key in OPTIONAL_ERROR_KEYS:
            warnings[key] = raw_errors.pop(key)
    hard_error = manifest.get("cost_error") or manifest.get("_error") or (raw_errors or None)
    return hard_error, warnings


def summarize_run(run_dir: Path) -> dict[str, Any]:
    manifest = _read_json(run_dir / "manifest.json")
    usage_rows = _read_csv(run_dir / "usage.csv")
    cost_rows = _read_csv(run_dir / "cost.csv")
    pricing_rows = _read_csv(run_dir / "pricing.csv")
    weather_rows = _read_csv(run_dir / "weather.csv")
    bill_rows = _read_csv(run_dir / "bills.csv")
    demand_maxima_rows = _read_csv(run_dir / "demand_maxima.csv")
    green_button_rows = _read_csv(run_dir / "green_button_rows.csv")
    service_point_rows = _read_csv(run_dir / "service_points.csv")
    if not service_point_rows:
        meta = _read_json(run_dir / "metadata.json")
        acct = ((meta.get("data") or {}).get("billingAccountByAuthContext") or {})
        for edge in ((acct.get("serviceAgreementsConnection") or {}).get("edges") or []):
            sa = edge.get("node") or {}
            rate_plan = sa.get("ratePlan") or {}
            for sp_edge in ((sa.get("servicePointsConnection") or {}).get("edges") or []):
                sp = sp_edge.get("node") or {}
                regs = sp.get("registers") or []
                net_reg = next((r for r in regs if r.get("serviceQuantityIdentifier") == "NET_USAGE"), regs[0] if regs else {})
                service_point_rows.append({
                    "account": sp.get("utilityId"),
                    "utility_id": sp.get("utilityId"),
                    "service_point_uuid": sp.get("uuid"),
                    "uuid": sp.get("uuid"),
                    "rate_plan_code": rate_plan.get("code"),
                    "available_interval": net_reg.get("availableReadsTimeInterval"),
                })

    target_account = manifest.get("account") or manifest.get("utility_id")
    target_usage_rows = [r for r in usage_rows if not target_account or r.get("utility_id") == target_account]
    target_cost_rows = [r for r in cost_rows if not target_account or r.get("utility_id") == target_account]
    target_pricing_rows = [r for r in pricing_rows if not target_account or r.get("utility_id") == target_account]
    target_demand_maxima_rows = [r for r in demand_maxima_rows if not target_account or r.get("utility_id") == target_account]

    pricing_by_sp_interval = {}
    pricing_by_interval = {}
    for r in target_pricing_rows:
        interval = r.get("timeInterval")
        utility_id = r.get("utility_id") or target_account or "unknown"
        price = _float(r.get("cost_per_unit"))
        if interval and price is not None:
            info = {
                "cost_per_unit": price,
                "rate_type": r.get("rate_type"),
                "tier": r.get("tier"),
                "tou_label": r.get("tou_label"),
                "component_attributes": r.get("component_attributes"),
            }
            pricing_by_sp_interval[(utility_id, interval)] = info
            pricing_by_interval[interval] = info
    raw_cost_by_sp_interval = {
        (r.get("utility_id") or "unknown", r.get("timeInterval")): _float(r.get("monetaryAmount"))
        for r in cost_rows
        if r.get("stream") == "netUsage" and r.get("serviceQuantityIdentifier") == "NET_USAGE"
    }

    def interval_cost(utility_id: str, interval: str | None, kwh: float) -> tuple[float, str]:
        raw = raw_cost_by_sp_interval.get((utility_id, interval))
        price_info = pricing_by_sp_interval.get((utility_id, interval)) or pricing_by_interval.get(interval or "") or {}
        rate = price_info.get("cost_per_unit")
        if raw is not None and not (raw == 0.0 and kwh > 0 and rate is not None):
            return raw, "cost_stream"
        if kwh > 0 and rate is not None:
            return kwh * rate, "estimated_from_rate"
        return raw or 0.0, "missing" if raw is None else "cost_stream"

    service_point_summaries_map: dict[str, dict[str, Any]] = {}
    for sp in service_point_rows or manifest.get("service_points", []):
        utility_id = sp.get("utility_id") or sp.get("account") or "unknown"
        service_point_summaries_map.setdefault(utility_id, {
            "utility_id": utility_id,
            "service_point_uuid": sp.get("service_point_uuid") or sp.get("uuid"),
            "rate_plan_code": sp.get("rate_plan_code"),
            "available_interval": sp.get("available_interval"),
            "total_kwh": 0.0,
            "total_cost": 0.0,
            "days": {},
            "weeks": {},
        })
    for r in usage_rows:
        if r.get("stream") != "netUsage" or r.get("serviceQuantityIdentifier") != "NET_USAGE":
            continue
        utility_id = r.get("utility_id") or "unknown"
        item = service_point_summaries_map.setdefault(utility_id, {
            "utility_id": utility_id, "service_point_uuid": r.get("service_point_uuid"), "rate_plan_code": None,
            "available_interval": None, "total_kwh": 0.0, "total_cost": 0.0, "days": {}, "weeks": {},
        })
        interval = r.get("timeInterval")
        kwh = _float(r.get("value")) or 0.0
        cost, _cost_source = interval_cost(utility_id, interval, kwh)
        item["total_kwh"] = _round_money(item["total_kwh"] + kwh)
        item["total_cost"] = _round_money(item["total_cost"] + cost)
        day = _date_key(interval or "")
        week = _iso_week_key(interval or "")
        for bucket_name, bucket_key in (("days", day), ("weeks", week)):
            bucket = item[bucket_name].setdefault(bucket_key, {"kwh": 0.0, "cost": 0.0})
            bucket["kwh"] = _round_money(bucket["kwh"] + kwh)
            bucket["cost"] = _round_money(bucket["cost"] + cost)
    service_point_summaries = sorted(service_point_summaries_map.values(), key=lambda x: x.get("utility_id") or "")

    net_usage = [r for r in target_usage_rows if r.get("stream") == "netUsage" and r.get("serviceQuantityIdentifier") == "NET_USAGE"]
    delivered = [r for r in target_usage_rows if r.get("stream") == "energyDelivered"]
    demand = [r for r in target_usage_rows if r.get("stream") == "demand"]
    cost_net = [r for r in target_cost_rows if r.get("stream") == "netUsage" and r.get("serviceQuantityIdentifier") == "NET_USAGE"]

    total_net_kwh = sum((_float(r.get("value")) or 0.0) for r in net_usage)
    total_delivered_kwh = sum((_float(r.get("value")) or 0.0) for r in delivered)
    peak_kw = max([_float(r.get("value")) or 0.0 for r in demand] or [0.0])
    computed_cost_by_interval = {}
    cost_sources = defaultdict(int)
    for r in net_usage:
        interval = r.get("timeInterval")
        utility_id = r.get("utility_id") or target_account or "unknown"
        kwh = _float(r.get("value")) or 0.0
        c, src = interval_cost(utility_id, interval, kwh)
        computed_cost_by_interval[interval] = c
        cost_sources[src] += 1
    total_cost = sum(computed_cost_by_interval.values())

    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"net_kwh": 0.0, "delivered_kwh": 0.0, "cost": 0.0, "peak_kw": 0.0})
    for r in net_usage:
        interval = r.get("timeInterval", "")
        by_day[_date_key(interval)]["net_kwh"] += _float(r.get("value")) or 0.0
        by_day[_date_key(interval)]["cost"] += computed_cost_by_interval.get(interval, 0.0)
    for r in delivered:
        by_day[_date_key(r.get("timeInterval", ""))]["delivered_kwh"] += _float(r.get("value")) or 0.0
    for r in demand:
        k = _date_key(r.get("timeInterval", ""))
        by_day[k]["peak_kw"] = max(by_day[k]["peak_kw"], _float(r.get("value")) or 0.0)

    daily = []
    for k, v in sorted(by_day.items()):
        daily.append({"date": k, **v, "price_per_kwh": _safe_rate(v["cost"], v["net_kwh"])})

    hourly: dict[str, dict[str, float]] = defaultdict(lambda: {"net_kwh": 0.0, "cost": 0.0, "peak_kw": 0.0})
    for r in net_usage:
        interval = r.get("timeInterval", "")
        hourly[_hour_key(interval)]["net_kwh"] += _float(r.get("value")) or 0.0
        hourly[_hour_key(interval)]["cost"] += computed_cost_by_interval.get(interval, 0.0)
    for r in demand:
        k = _hour_key(r.get("timeInterval", ""))
        hourly[k]["peak_kw"] = max(hourly[k]["peak_kw"], _float(r.get("value")) or 0.0)
    hourly_rows = []
    for k, v in sorted(hourly.items()):
        hourly_rows.append({"hour": k, **v, "price_per_kwh": _safe_rate(v["cost"], v["net_kwh"])})

    cost_by_interval = computed_cost_by_interval
    demand_by_interval = {r.get("timeInterval"): _float(r.get("value")) or 0.0 for r in demand}
    pricing_by_interval = {}
    for r in target_pricing_rows:
        interval = r.get("timeInterval")
        price = _float(r.get("cost_per_unit"))
        if interval and price is not None:
            pricing_by_interval[interval] = {
                "cost_per_unit": price,
                "rate_type": r.get("rate_type"),
                "tier": r.get("tier"),
                "tou_label": r.get("tou_label"),
                "component_attributes": r.get("component_attributes"),
            }
    weather_by_date = {}
    weather = []
    for r in weather_rows:
        item = {
            "date": r.get("date") or _date_key(r.get("timeInterval", "")),
            "timeInterval": r.get("timeInterval"),
            "min_temperature": _float(r.get("min_temperature")),
            "mean_temperature": _float(r.get("mean_temperature")),
            "max_temperature": _float(r.get("max_temperature")),
            "premise_uuid": r.get("premise_uuid"),
        }
        weather.append(item)
        weather_by_date[item["date"]] = item
    for d in daily:
        if d["date"] in weather_by_date:
            d.update({k: weather_by_date[d["date"]].get(k) for k in ("min_temperature", "mean_temperature", "max_temperature")})
    series = []
    for r in net_usage:
        interval = r.get("timeInterval", "")
        kwh = _float(r.get("value")) or 0.0
        cost = cost_by_interval.get(interval, 0.0)
        _, cost_source = interval_cost(r.get("utility_id") or target_account or "unknown", interval, kwh)
        price_info = pricing_by_interval.get(interval, {})
        canonical_price = price_info.get("cost_per_unit")
        day_weather = weather_by_date.get(_date_key(interval), {})
        series.append({
            "t": _start_time(interval),
            "interval": interval,
            "kwh": kwh,
            "cost": cost,
            "cost_source": cost_source,
            "kw": demand_by_interval.get(interval, 0.0),
            "price_per_kwh": canonical_price if canonical_price is not None else _safe_rate(cost, kwh),
            "price_source": "rated_components" if canonical_price is not None else "cost_div_kwh",
            "mean_temperature": day_weather.get("mean_temperature"),
            "min_temperature": day_weather.get("min_temperature"),
            "max_temperature": day_weather.get("max_temperature"),
            "rate_type": price_info.get("rate_type"),
            "tou_label": price_info.get("tou_label"),
            "readType": r.get("readType"),
        })

    day_count = len([d for d in daily if d["date"] != "unknown"]) or len(daily) or 1
    avg_daily_use = total_net_kwh / day_count
    avg_daily_cost = total_cost / day_count
    effective_price = _safe_rate(total_cost, total_net_kwh)

    read_types = defaultdict(int)
    for r in target_usage_rows:
        read_types[r.get("readType") or "unknown"] += 1

    bill_history = []
    for r in bill_rows:
        kwh = _float(r.get("kwh"))
        usage_charges = _float(r.get("usageCharges"))
        current_amount = _float(r.get("currentAmount"))
        interval = r.get("timeInterval")
        usage_interval = r.get("usageInterval")
        bill_history.append({
            "bill_urn": r.get("bill_urn"),
            "segment_urn": r.get("segment_urn"),
            "timeInterval": interval,
            "usageInterval": usage_interval,
            "period_label": _period_label(usage_interval or interval),
            "billing_days": _period_days(usage_interval or interval),
            "serviceType": r.get("serviceType"),
            "estimated": str(r.get("estimated")).lower() == "true",
            "kwh": kwh,
            "usageCharges": usage_charges,
            "currentAmount": current_amount,
            "effective_price_per_kwh": _safe_rate(current_amount or 0.0, kwh or 0.0),
        })
    bill_history.sort(key=lambda x: x.get("timeInterval") or "")

    peak_demand_events = []
    source_peak_rows = target_demand_maxima_rows or demand
    for r in source_peak_rows:
        kw = _float(r.get("value"))
        if kw is not None:
            peak_demand_events.append({
                "timeInterval": r.get("timeInterval"),
                "t": _start_time(r.get("timeInterval", "")),
                "kw": kw,
                "unit": r.get("unit"),
                "readType": r.get("readType"),
                "service_point_uuid": r.get("service_point_uuid"),
                "utility_id": r.get("utility_id"),
                "isMaximum": str(r.get("isMaximum")).lower() == "true",
            })
    peak_demand_events.sort(key=lambda x: x["kw"], reverse=True)

    price_source = "rated_components" if pricing_by_interval else "cost_div_kwh"
    green_button = manifest.get("green_button") or {"row_count": len(green_button_rows)}
    if "row_count" not in green_button:
        green_button["row_count"] = len(green_button_rows)
    dataset_counts = {
        "usage": len(usage_rows), "cost": len(cost_rows), "pricing": len(pricing_rows),
        "weather": len(weather_rows), "bills": len(bill_rows), "demand_maxima": len(demand_maxima_rows),
        "green_button": len(green_button_rows),
    }

    error, warnings = _split_run_issues(manifest)
    return {
        "run_dir": str(run_dir.resolve()),
        "manifest": manifest,
        "retrieved_at_utc": manifest.get("retrieved_at_utc", run_dir.name),
        "retrieved_label": _iso_to_label(manifest.get("retrieved_at_utc", run_dir.name)),
        "account": manifest.get("account"),
        "available_interval": manifest.get("available_interval"),
        "requested_interval": manifest.get("requested_interval"),
        "resolution": manifest.get("resolution"),
        "timezone": manifest.get("timezone"),
        "usage_rows": len(usage_rows),
        "cost_rows": len(cost_rows),
        "total_net_kwh": total_net_kwh,
        "total_delivered_kwh": total_delivered_kwh,
        "total_cost": total_cost,
        "peak_kw": peak_kw,
        "avg_daily_use": avg_daily_use,
        "avg_daily_cost": avg_daily_cost,
        "effective_price_per_kwh": effective_price,
        "read_types": dict(sorted(read_types.items())),
        "dataset_counts": dataset_counts,
        "price_source": price_source,
        "cost_sources": dict(sorted(cost_sources.items())),
        "weather": weather,
        "bill_history": bill_history,
        "peak_demand_events": peak_demand_events[:20],
        "green_button": green_button,
        "service_points": service_point_rows or manifest.get("service_points", []),
        "service_point_summaries": service_point_summaries,
        "quarter_hour_defs": [
            {"key": "kwh", "label": "Net kWh", "axis": "left", "default": True},
            {"key": "cost", "label": "Cost", "axis": "left", "default": True},
            {"key": "kw", "label": "Demand", "axis": "left", "default": True},
            {"key": "mean_temperature", "label": "Mean temperature", "axis": "right", "default": False},
        ],
        "latest_available_end": manifest.get("latest_available_end") or ((manifest.get("available_interval") or "/").split("/")[-1] or None),
        "daily": daily,
        "hourly": hourly_rows,
        "series": series,
        "error": error,
        "warnings": warnings,
    }


def _aggregate_dashboard_series(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    price_by_interval: dict[str, dict[str, Any]] = {}
    bill_by_key: dict[str, dict[str, Any]] = {}
    demand_by_interval: dict[str, dict[str, Any]] = {}
    for run in runs:
        retrieved_at = run.get("retrieved_at_utc")
        for row in run.get("series") or []:
            interval = row.get("interval")
            price = row.get("price_per_kwh")
            if interval and price is not None:
                price_by_interval[interval] = {
                    "t": row.get("t"),
                    "interval": interval,
                    "price_per_kwh": price,
                    "price_source": row.get("price_source"),
                    "kwh": row.get("kwh"),
                    "cost": row.get("cost"),
                    "retrieved_at_utc": retrieved_at,
                }
            kw = row.get("kw")
            if interval and kw is not None and kw > 0:
                prior = demand_by_interval.get(interval)
                if prior is None or kw >= prior.get("kw", 0):
                    demand_by_interval[interval] = {
                        "t": row.get("t"),
                        "interval": interval,
                        "kw": kw,
                        "kwh": row.get("kwh"),
                        "cost": row.get("cost"),
                        "mean_temperature": row.get("mean_temperature"),
                        "retrieved_at_utc": retrieved_at,
                    }
        for bill in run.get("bill_history") or []:
            key = bill.get("segment_urn") or bill.get("timeInterval") or bill.get("usageInterval")
            if key:
                interval = bill.get("usageInterval") or bill.get("timeInterval")
                _, _, end_s = (interval or "").partition("/")
                bill_by_key[key] = {**bill, "interval": interval, "t": end_s or _start_time(interval), "retrieved_at_utc": retrieved_at}
        for event in run.get("peak_demand_events") or []:
            interval = event.get("timeInterval")
            kw = event.get("kw")
            if interval and kw is not None:
                prior = demand_by_interval.get(interval)
                if prior is None or kw >= prior.get("kw", 0):
                    merged = {**event, "retrieved_at_utc": retrieved_at}
                    if prior:
                        merged.setdefault("kwh", prior.get("kwh"))
                        merged.setdefault("cost", prior.get("cost"))
                        merged.setdefault("mean_temperature", prior.get("mean_temperature"))
                        merged.setdefault("t", prior.get("t"))
                    demand_by_interval[interval] = merged

    return {
        "historical_price_series": sorted(price_by_interval.values(), key=lambda x: x.get("t") or x.get("interval") or ""),
        "bill_trend": sorted(bill_by_key.values(), key=lambda x: x.get("timeInterval") or x.get("usageInterval") or ""),
        "demand_event_series": sorted(demand_by_interval.values(), key=lambda x: x.get("kw") or 0, reverse=True),
    }


def build_dashboard_data(data_dir: Path = Path("data")) -> dict[str, Any]:
    runs = []
    if data_dir.exists():
        for path in sorted(data_dir.iterdir()):
            if path.is_dir() and (path / "manifest.json").exists():
                runs.append(summarize_run(path))
    runs.sort(key=lambda r: r.get("retrieved_at_utc") or "")
    latest = runs[-1] if runs else None
    errors = [r for r in runs if r.get("error")]
    warnings = [r for r in runs if r.get("warnings")]
    aggregates = _aggregate_dashboard_series(runs)
    benchmarks = _fetch_eia_benchmark_prices(os.environ.get("EVERSMART_EIA_STATE", "MA"))
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_dir": str(data_dir.resolve()),
        "run_count": len(runs),
        "latest": latest,
        "runs": runs,
        "errors": errors,
        "warnings": warnings,
        "price_benchmarks": benchmarks,
        **aggregates,
    }


def generate_dashboard(data_dir: Path = Path("data"), output: Path = Path(DASHBOARD_PATH)) -> Path:
    dashboard_data = build_dashboard_data(data_dir)
    payload = json.dumps(dashboard_data, separators=(",", ":"))
    html = HTML_TEMPLATE.replace("__DASHBOARD_JSON__", payload)
    output.write_text(html)
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
<body><div class="wrap"><header><div><div class="eyebrow">Eversmart local dashboard</div><h1>Energy Observatory</h1><div class="sub" id="subtitle">Static dark-mode dashboard for Eversource smart-meter pulls.</div></div><div class="status" id="statusPills"></div></header><main id="app"></main><footer id="footer"></footer></div><div id="tooltip"></div>
<script>const DASHBOARD_DATA=__DASHBOARD_JSON__;</script>
<script>
const $=id=>document.getElementById(id), tip=$('tooltip'), latest=DASHBOARD_DATA.latest;
const fmt=(n,d=2)=>Number.isFinite(+n)?(+n).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d}):'—';
const money=n=>'$'+fmt(n,2), rate=n=>Number.isFinite(+n)?'$'+fmt(n,3)+'/kWh':'—';
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function metric(label,value,hint,compact=false){return `<section class="card pad metric ${compact?'compact':''}" title="${esc(label)}: ${esc(value)} — ${esc(hint)}"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></section>`}
function showTip(html,x,y){tip.innerHTML=html;tip.style.display='block';const pad=16,w=tip.offsetWidth,h=tip.offsetHeight;tip.style.left=Math.min(x+14,innerWidth-w-pad)+'px';tip.style.top=Math.min(y+14,innerHeight-h-pad)+'px'}
function hideTip(){tip.style.display='none'}
function showRunTip(run,evt){let lines=[`<span class="tip-title">${esc(run.retrieved_label)}</span>`,`${run.usage_rows} usage rows · ${run.cost_rows} cost rows`,`${fmt(run.total_net_kwh,3)} kWh · ${money(run.total_cost)}`,`$/kWh ${rate(run.effective_price_per_kwh)} · peak ${fmt(run.peak_kw,3)} kW`,run.error?'ERROR: '+JSON.stringify(run.error):(run.warnings?'optional: '+Object.keys(run.warnings).join(', '):'ok')];showTip(lines.join('\n'),evt.clientX,evt.clientY)}
function timeLabel(s){if(!s)return 'unknown'; try{return new Date(s).toLocaleString()}catch{return s}}
function shortDateLabel(s){if(!s)return '';let d=new Date(s);if(Number.isNaN(d.getTime()))return String(s).slice(0,10);return d.toLocaleDateString(undefined,{month:'short',day:'numeric'})}
function xLabelFor(row,opt={}){if(row?.hour)return opt.timeLabels==='date'?String(row.hour).slice(5,10):String(row.hour).slice(5,16).replace('T',' ');if(opt.timeLabels&&row?.t){let d=new Date(row.t);if(!Number.isNaN(d.getTime()))return opt.timeLabels==='hour'?d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'}):d.toLocaleDateString(undefined,{month:'short',day:'numeric'})}return row?.date||shortDateLabel(row?.t)||row?.period_label||(row?.interval||row?.timeInterval||'').split('/')[0].slice(0,10)}
function finiteOrNull(raw){if(raw==null||raw==='')return null;let v=+raw;return Number.isFinite(v)?v:null}
function drawXAxisLabels(ctx,items,xFor,y,opt={}){let n=items.length;if(!n)return;let maxTicks=opt.maxTicks||6,step=Math.max(1,Math.ceil(n/maxTicks));ctx.save();ctx.fillStyle='rgba(208,214,224,.72)';ctx.strokeStyle='rgba(255,255,255,.10)';ctx.font='11px ui-monospace';ctx.textAlign='center';ctx.textBaseline='top';for(let i=0;i<n;i+=step){let label=xLabelFor(items[i],opt);if(!label)continue;let x=xFor(i);ctx.beginPath();ctx.moveTo(x,y-8);ctx.lineTo(x,y-3);ctx.stroke();ctx.fillText(label,x,y)}if(n>1&&(n-1)%step!==0){let label=xLabelFor(items[n-1],opt);let x=xFor(n-1);ctx.beginPath();ctx.moveTo(x,y-8);ctx.lineTo(x,y-3);ctx.stroke();ctx.fillText(label,x,y)}ctx.restore()}
function lineChart(canvas,series,defs,opt={}){const ctx=canvas.getContext('2d'),dpr=devicePixelRatio||1,rect=canvas.getBoundingClientRect();canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;ctx.scale(dpr,dpr);const W=rect.width,H=rect.height,pt=34,pb=56,pl=42,rp=opt.rightAxis?46:42,plotH=H-pt-pb;ctx.clearRect(0,0,W,H);const xFor=i=>pl+(W-pl-rp)*(series.length<=1?0:i/(series.length-1));ctx.strokeStyle='rgba(255,255,255,.07)';ctx.lineWidth=1;for(let i=0;i<5;i++){let y=pt+plotH*i/4;ctx.beginPath();ctx.moveTo(pl,y);ctx.lineTo(W-rp,y);ctx.stroke()}const domain=axis=>{const vals=[];defs.filter(d=>(d.axis||'left')===axis).forEach(def=>series.forEach(x=>{let v=finiteOrNull(x[def.key]);if(v!=null)vals.push(v)}));if(!vals.length)return {min:0,max:1};let min=Math.min(...vals),max=Math.max(...vals);if(opt.zero!==false&&min>0)min=0;if(min===max){max+=1;min-=1}return {min,max}};const left=domain('left'),right=domain('right');const yFor=(v,axis)=>{const d=(axis||'left')==='right'?right:left;return pt+plotH-plotH*(v-d.min)/(d.max-d.min||1)};defs.forEach(def=>{ctx.beginPath();let started=false;series.forEach((x,i)=>{let raw=x[def.key],v=finiteOrNull(raw);if(v==null){started=false;return;}let xx=xFor(i),yy=yFor(v,def.axis);if(started)ctx.lineTo(xx,yy);else{ctx.moveTo(xx,yy);started=true}});ctx.strokeStyle=def.color;ctx.lineWidth=def.axis==='right'?1.7:2.2;if(def.dash)ctx.setLineDash(def.dash);ctx.stroke();ctx.setLineDash([])});ctx.fillStyle='rgba(208,214,224,.8)';ctx.font='11px ui-monospace';ctx.textAlign='left';ctx.fillText(fmt(left.max,opt.yDigits??2),8,pt+4);ctx.fillText(fmt(left.min,opt.yDigits??2),8,pt+plotH+4);if(opt.rightAxis){ctx.fillStyle='rgba(244,114,182,.85)';ctx.fillText(fmt(right.max,opt.rightDigits??1),W-rp+8,pt+4);ctx.fillText(fmt(right.min,opt.rightDigits??1),W-rp+8,pt+plotH+4)}drawXAxisLabels(ctx,series,xFor,H-pb+18,opt);canvas.onmousemove=e=>{if(!series.length)return;let r=canvas.getBoundingClientRect(),x=e.clientX-r.left,idx=Math.round((x-pl)/(W-pl-rp)*(series.length-1));idx=Math.max(0,Math.min(series.length-1,idx));let row=series[idx];let lines=[`<span class="tip-title">${esc(opt.title||'Reading')}</span>`,`<span class="tip-accent">${esc(timeLabel(row.t))}</span>`,esc(row.interval||row.period_label||'')];defs.forEach(def=>{let v=row[def.key];if(v!=null)lines.push(`${def.label}: ${def.format?def.format(v):fmt(v,3)}`)});if(row.price_per_kwh!=null)lines.push(`Effective price: ${rate(row.price_per_kwh)}`);if(row.mean_temperature!=null)lines.push(`Temp: ${fmt(row.mean_temperature,1)}°F mean (${fmt(row.min_temperature,1)}–${fmt(row.max_temperature,1)}°F)`);if(row.readType)lines.push(`Read type: ${esc(row.readType)}`);showTip(lines.join('\n'),e.clientX,e.clientY)};canvas.onmouseleave=hideTip}
function renderDemandHeatmap(el,rows){rows=rows||[];if(!rows.length){el.innerHTML='<div class="empty">No demand intervals returned.</div>';return}let vals=rows.map(r=>+r.kw||0),max=Math.max(...vals,.001),sorted=rows.slice().sort((a,b)=>String(a.t||a.interval).localeCompare(String(b.t||b.interval)));el.innerHTML='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(54px,1fr));gap:6px">'+sorted.map(r=>{let pct=(+r.kw||0)/max,dt=new Date(r.t||r.interval),label=Number.isNaN(dt.getTime())?String(r.interval||'').slice(0,16):dt.toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric'});return `<div class="check" style="border-color:rgba(34,211,238,${.2+.6*pct});background:rgba(34,211,238,${.05+.28*pct})" title="${esc(label)}: ${fmt(r.kw,3)} kW">${esc(label)}<br><b>${fmt(r.kw,2)}</b> kW</div>`}).join('')+'</div>'}
function barChart(canvas,rows,key,color,opt={}){const ctx=canvas.getContext('2d'),dpr=devicePixelRatio||1,rect=canvas.getBoundingClientRect();canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;ctx.scale(dpr,dpr);const W=rect.width,H=rect.height,pt=24,pb=48,pl=34,plotH=H-pt-pb,max=Math.max(...rows.map(r=>+r[key]||0),.001);ctx.clearRect(0,0,W,H);const bw=(W-pl*2)/Math.max(rows.length,1)*.68;rows.forEach((r,i)=>{let x=pl+(W-pl*2)*i/rows.length+bw*.25,h=plotH*(+r[key]||0)/max,g=ctx.createLinearGradient(0,pt+plotH-h,0,pt+plotH);g.addColorStop(0,color);g.addColorStop(1,'rgba(113,112,255,.18)');ctx.fillStyle=g;ctx.beginPath();ctx.roundRect(x,pt+plotH-h,bw,h,6);ctx.fill()});drawXAxisLabels(ctx,rows,i=>pl+(W-pl*2)*(i+.5)/Math.max(rows.length,1),H-pb+14,{maxTicks:opt.maxTicks||7});canvas.onmousemove=e=>{if(!rows.length)return;let rct=canvas.getBoundingClientRect(),x=e.clientX-rct.left,idx=Math.floor((x-pl)/(W-pl*2)*rows.length);idx=Math.max(0,Math.min(rows.length-1,idx));let r=rows[idx],lines=[`<span class="tip-title">${esc(opt.title||'Bar')}</span>`,`Date/hour: ${esc(r.date||r.hour)}`,`Net usage: ${fmt(r.net_kwh,3)} kWh`,`Cost: ${money(r.cost)}`,`Effective price: ${rate(r.price_per_kwh)}`,`Peak demand: ${fmt(r.peak_kw,3)} kW`];if(r.mean_temperature!=null)lines.push(`Weather: ${fmt(r.mean_temperature,1)}°F mean (${fmt(r.min_temperature,1)}–${fmt(r.max_temperature,1)}°F)`);showTip(lines.join('\n'),e.clientX,e.clientY)};canvas.onmouseleave=hideTip}
function render(){if(!latest){$('app').innerHTML='<div class="card empty">No data runs found. Run <span class="mono">python3 eversmart.py --out data</span>.</div>';return}const hasErr=DASHBOARD_DATA.errors.length>0,warnCount=(DASHBOARD_DATA.warnings||[]).length;const counts=latest.dataset_counts||{};$('statusPills').innerHTML=`<div class="pill"><span class="dot ${hasErr?'bad':(warnCount?'warn':'')}"></span>${hasErr?'Errors seen':(warnCount?'Optional warnings':'All green')}</div><div class="pill mono">Last poll ${esc(latest.retrieved_label)}</div><div class="pill mono">AMI through ${esc(latest.latest_available_end||'unknown')}</div><div class="pill mono">${DASHBOARD_DATA.run_count} runs</div>`;$('subtitle').innerHTML=`Account <span class="mono">${esc(latest.account)}</span> · ${esc(latest.resolution)} · ${esc(latest.timezone)} · price source: <span class="mono">${esc(latest.price_source)}</span> · generated ${esc(DASHBOARD_DATA.generated_at)}`;
$('app').innerHTML=`<div class="grid">
${metric('Net usage',fmt(latest.total_net_kwh,3)+' kWh','Current available interval',true)}${metric('Estimated cost',money(latest.total_cost),'Cost stream plus rated-component estimates when cost rows lag',true)}${metric('Peak demand',fmt(latest.peak_kw,3)+' kW','Highest quarter-hour demand',true)}${metric('Avg daily use',fmt(latest.avg_daily_use,3)+' kWh/day','Total divided by returned days',true)}${metric('Avg daily cost',money(latest.avg_daily_cost)+'/day','Cost divided by returned days',true)}${metric('Avg kWh price',rate(latest.effective_price_per_kwh),'Total cost ÷ net kWh',true)}
<section class="card pad wide"><div class="section-title"><h2>Quarter-hour profile</h2><span>independent toggles; temperature uses right axis</span></div><div class="filters" id="qhFilters"></div><canvas id="usageChart"></canvas><div class="legend" id="qhLegend"></div></section>
<section class="card pad side"><div class="section-title"><h2>Coverage and run health</h2><span>poll history</span></div><div class="timeline">${DASHBOARD_DATA.runs.map(r=>`<div class="bar ${r.error?'err':''}" style="height:${Math.max(8,Math.min(120,(r.usage_rows||1)/(latest.usage_rows||1)*120))}px" data-tip-id="run-${DASHBOARD_DATA.runs.indexOf(r)}"></div>`).join('')}</div><div class="kv" style="margin-top:18px"><div>Last successful poll</div><div>${esc(latest.retrieved_label)}</div><div>AMI available through</div><div class="mono">${esc(latest.latest_available_end||'unknown')}</div><div>Data directory</div><div class="mono">${esc(latest.run_dir)}</div><div>Available interval</div><div class="mono">${esc(latest.available_interval)}</div><div>Requested interval</div><div class="mono">${esc(latest.requested_interval)}</div></div></section>
<section class="card pad half mini"><div class="section-title"><h2>Daily usage + weather</h2><span>tooltip includes temp</span></div><canvas id="dailyChart"></canvas></section><section class="card pad half mini"><div class="section-title"><h2>Daily cost</h2><span>hover bars</span></div><canvas id="costChart"></canvas></section>
<section class="card pad full"><div class="section-title"><h2>Canonical kWh price over time</h2><span>${DASHBOARD_DATA.historical_price_series.length.toLocaleString()} intervals · benchmark overlays are optional</span></div><div class="filters" id="priceFilters"></div><canvas id="priceChart"></canvas><div class="legend" id="priceLegend"></div><div class="sub" style="font-size:13px;margin:6px 0 0">Eversource canonical price is the rated component returned for each interval. It should be flat unless your actual tariff changes. EIA regional/national values are monthly residential averages repeated as optional comparison lines, not additional Eversource interval observations.</div></section>
<section class="card pad half"><div class="section-title"><h2>Long-term bill trend</h2><span>bills are shown by billing-period end date; table lists covered period</span></div><canvas id="billTrendChart"></canvas><div class="sub" style="font-size:13px;margin:4px 0 10px">Lines compare billed kWh, usage charges, and actual $/kWh for each monthly billing period. Actual $/kWh is total bill amount divided by kWh, not just usage charges. The label is the bill's covered usage interval, not the scraper run time.</div><table class="table"><thead><tr><th>Billing period</th><th>Days</th><th>Usage</th><th>Usage charges</th><th>$/kWh</th><th>Bill amount</th></tr></thead><tbody>${(DASHBOARD_DATA.bill_trend||latest.bill_history||[]).slice().reverse().map(b=>`<tr title="raw interval: ${esc(b.timeInterval||b.usageInterval)}"><td>${esc(b.period_label||b.timeInterval||b.usageInterval)}</td><td>${b.billing_days||'—'}</td><td>${fmt(b.kwh,2)} kWh</td><td>${money(b.usageCharges)}</td><td>${rate(b.effective_price_per_kwh)}</td><td>${money(b.currentAmount)} ${b.estimated?'· estimated':''}</td></tr>`).join('')||'<tr><td colspan="6" class="warn">No bill history rows returned.</td></tr>'}</tbody></table></section>
<section class="card pad half"><div class="section-title"><h2>Peak demand events</h2><span>top intervals plus time-of-day heatmap</span></div><div id="demandHeatmap"></div><canvas id="demandEventChart"></canvas><table class="table"><thead><tr><th>Interval</th><th>Demand</th><th>kWh</th><th>Temp</th></tr></thead><tbody>${(DASHBOARD_DATA.demand_event_series||latest.peak_demand_events||[]).slice(0,10).map(p=>`<tr><td class="mono">${esc(p.timeInterval||p.interval)}</td><td>${fmt(p.kw,3)} kW</td><td>${fmt(p.kwh,3)}</td><td>${p.mean_temperature!=null?fmt(p.mean_temperature,1)+'°F':'—'}</td></tr>`).join('')||'<tr><td colspan="4" class="warn">No demand intervals returned.</td></tr>'}</tbody></table></section>
<section class="card pad half"><div class="section-title"><h2>Dataset coverage</h2><span>normalized tables</span></div><table class="table"><thead><tr><th>Dataset</th><th>Rows</th></tr></thead><tbody>${Object.entries(counts).map(([k,v])=>`<tr><td class="mono">${esc(k)}</td><td>${Number(v||0).toLocaleString()}</td></tr>`).join('')}</tbody></table><div class="kv" style="margin-top:14px"><div>Read types</div><div class="mono">${esc(JSON.stringify(latest.read_types||{}))}</div><div>Green Button audit</div><div>${(latest.green_button?.row_count||0).toLocaleString()} parsed rows ${latest.green_button?.bytes?'· '+latest.green_button.bytes.toLocaleString()+' bytes':''}</div></div></section>
<section class="card pad half"><div class="section-title"><h2>Service point usage/cost</h2><span>per device totals from the latest run</span></div><table class="table"><thead><tr><th>Utility id</th><th>Total</th><th>Cost</th><th>Daily kWh</th><th>Weekly kWh</th></tr></thead><tbody>${(latest.service_point_summaries||[]).map(sp=>{let days=Object.entries(sp.days||{}).sort().slice(-3).map(([d,v])=>`${d}: ${fmt(v.kwh,2)} (${money(v.cost)})`).join('<br>');let weeks=Object.entries(sp.weeks||{}).sort().slice(-4).map(([w,v])=>`${w}: ${fmt(v.kwh,2)} (${money(v.cost)})`).join('<br>');return `<tr class="${sp.utility_id===latest.account?'ok':''}"><td class="mono">${esc(sp.utility_id)}<br><span style="color:var(--dim)">${esc(sp.service_point_uuid||'')}</span></td><td>${fmt(sp.total_kwh,3)} kWh</td><td>${money(sp.total_cost)}</td><td>${days||'—'}</td><td>${weeks||'—'}</td></tr>`}).join('')||'<tr><td colspan="5" class="warn">No per-service-point rows available. Run with --all-service-points to populate sibling devices.</td></tr>'}</tbody></table></section>
<section class="card pad full"><div class="section-title"><h2>History and diagnostics</h2><span>${DASHBOARD_DATA.errors.length} error runs · ${warnCount} optional warning runs</span></div><table class="table"><thead><tr><th>Poll</th><th>Rows</th><th>Usage</th><th>Cost</th><th>Avg/day</th><th>$/kWh</th><th>Peak</th><th>Status</th></tr></thead><tbody>${DASHBOARD_DATA.runs.slice().reverse().map(r=>{let warn=r.warnings&&Object.keys(r.warnings).length;return `<tr title="${esc(r.run_dir)}"><td class="mono">${esc(r.retrieved_label)}</td><td>${r.usage_rows.toLocaleString()}</td><td>${fmt(r.total_net_kwh,3)} kWh</td><td>${money(r.total_cost)}</td><td>${fmt(r.avg_daily_use,2)} kWh · ${money(r.avg_daily_cost)}</td><td>${rate(r.effective_price_per_kwh)}</td><td>${fmt(r.peak_kw,3)} kW</td><td class="${r.error?'bad':(warn?'warn':'ok')}">${r.error?esc(JSON.stringify(r.error)).slice(0,140):(warn?'optional: '+esc(Object.keys(r.warnings).join(', ')):'ok')}</td></tr>`}).join('')}</tbody></table></section></div>`;
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
function drawAll(){drawQuarterHour();barChart($('dailyChart'),latest.daily,'net_kwh','#7170ff',{title:'Daily usage',timeLabels:'date'});barChart($('costChart'),latest.daily,'cost','#34d399',{title:'Daily cost',timeLabels:'date'});lineChart($('priceChart'),buildPriceSeries(),selectedPriceDefs(),{title:'Canonical price with optional benchmark overlays',yDigits:3,zero:false,timeLabels:'date'});lineChart($('billTrendChart'),DASHBOARD_DATA.bill_trend||[],[{key:'kwh',label:'Bill kWh',color:'#7170ff',format:v=>fmt(v,1)+' kWh'},{key:'usageCharges',label:'Usage charges',color:'#34d399',format:money},{key:'effective_price_per_kwh',label:'Actual $/kWh',color:'#f472b6',format:rate,axis:'right',dash:[5,5]}],{title:'Bill trend',rightAxis:true,yDigits:1,rightDigits:3,zero:false});renderDemandHeatmap($('demandHeatmap'),(DASHBOARD_DATA.demand_event_series||[]).slice(0,48));lineChart($('demandEventChart'),(DASHBOARD_DATA.demand_event_series||[]).slice(0,30).slice().reverse(),[{key:'kw',label:'Demand',color:'#22d3ee',format:v=>fmt(v,3)+' kW'},{key:'mean_temperature',label:'Mean temp',color:'#f472b6',format:v=>fmt(v,1)+'°F',axis:'right',dash:[5,5]}],{title:'Peak demand events',rightAxis:true,yDigits:3,rightDigits:1})}
renderQhControls();renderPriceControls();document.querySelectorAll('[data-tip-id]').forEach(el=>{el.onmousemove=e=>showRunTip(DASHBOARD_DATA.runs[+el.dataset.tipId.split('-')[1]],e);el.onmouseleave=hideTip});drawAll();window.onresize=drawAll;$('footer').textContent=`Static file: refresh after each scraper run · Source data: ${DASHBOARD_DATA.data_dir}`}
render();
</script></body></html>'''


if __name__ == "__main__":
    print(generate_dashboard())
