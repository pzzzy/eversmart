import csv
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import dashboard


ACCOUNT = "synthetic-account"
ZONE = ZoneInfo("America/New_York")
UTC = timezone.utc


def span(start, end=None):
    return f"{start.isoformat()}/{(end or start + timedelta(hours=1)).isoformat()}"


def read(start, value: float | None = 1.0, kind="ACTUAL", *, stream="netUsage", end=None):
    return {"utility_id": ACCOUNT, "service_point_uuid": "synthetic-meter",
            "stream": stream, "serviceQuantityIdentifier": "NET_USAGE" if stream == "netUsage" else "DELIVERED",
            "timeInterval": span(start, end), "readType": kind, "value": value}


def day_reads(day):
    start = datetime.combine(day, time.min, ZONE).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, ZONE).astimezone(UTC)
    return [read(start + timedelta(hours=i)) for i in range(int((end - start).total_seconds() / 3600))]


def bill(start, end, service="ELECTRICITY"):
    return {"segment_urn": f"synthetic-{service}-{end.date()}", "serviceType": service,
            "timeInterval": span(start, end), "usageInterval": span(start, end), "kwh": 456}


class DashboardReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.manual = self.root / "statements"
        self.manual.mkdir()
        # Tests never perform egress, even when run on a configured machine.
        egress = patch.object(dashboard, "_fetch_eia_benchmark_prices", side_effect=AssertionError("dashboard made a network request"))
        self.fetch = egress.start()
        self.addCleanup(egress.stop)

    def poll(self, stamp, **datasets):
        path = self.data / stamp
        path.mkdir(parents=True)
        manifest = {"account": ACCOUNT, "retrieved_at_utc": stamp, "timezone": ZONE.key, "resolution": "HOUR", **datasets.pop("manifest", {})}
        (path / "manifest.json").write_text(json.dumps(manifest))
        for name, rows in datasets.items():
            if rows:
                with (path / f"{name}.csv").open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
                    writer.writeheader()
                    writer.writerows(rows)
        return manifest

    def statement(self, **fields):
        statement = {"statement_date": "2026-09-10", "billing_period_start": "2026-08-10",
                     "billing_period_end": "2026-09-09", "total_due": 280,
                     "total_current_charges": 110, "total_billed_kwh": 400, "customer_charge": 10, **fields}
        (self.manual / "statement.json").write_text(json.dumps(statement))

    def recent_bill(self):
        # UTC boundary is Oct 10 locally at midnight; gas ends even later and
        # must not anchor an electricity forecast. Physical statement is older.
        return [bill(datetime(2026, 9, 10, 4, tzinfo=UTC), datetime(2026, 10, 10, 4, tzinfo=UTC)),
                bill(datetime(2026, 10, 12, 4, tzinfo=UTC), datetime(2026, 11, 12, 5, tzinfo=UTC), "GAS")]

    def build(self, **kwargs):
        return dashboard.build_dashboard_data(self.data, manual_bill_dir=self.manual, **kwargs)

    def test_forecast_uses_latest_electricity_boundary_but_physical_rate(self):
        self.statement()
        as_of = datetime(2026, 10, 12, 3, tzinfo=UTC)  # still Oct 11 in meter zone
        start = datetime(2026, 10, 10).date()
        self.poll("20261012T030000Z", bills=self.recent_bill(), usage=day_reads(start) + day_reads(start + timedelta(days=1))[:4])
        cycle = self.build(as_of=as_of)["latest"]["current_billing_cycle"]
        self.assertEqual(cycle["status"], "available")
        self.assertEqual(cycle["cycle_start_date"], "2026-10-10")
        self.assertEqual(cycle["latest_electricity_segment_end_utc"], "2026-10-10T04:00:00Z")
        self.assertEqual(cycle["cycle_length_source"], "latest_electricity_segment")
        self.assertEqual(cycle["billing_days"], 30)
        self.assertEqual(cycle["observed_days"], 1)
        self.assertEqual(cycle["kwh_so_far"], 24)
        self.assertEqual(cycle["variable_rate_per_kwh"], 0.25)
        self.assertEqual(cycle["projected_cost"], 190)

    def test_stale_cycle_is_unavailable_even_with_old_reads(self):
        self.statement()
        self.poll("20261120T120000Z", bills=self.recent_bill(), usage=day_reads(datetime(2026, 10, 10).date()))
        cycle = self.build(as_of=datetime(2026, 11, 20, 12, tzinfo=UTC))["latest"]["current_billing_cycle"]
        self.assertEqual(cycle["reason"], "stale_billing_cycle")
        self.assertIsNone(cycle["projected_cost"])

    def test_november_segment_advances_cycle_without_new_physical_bill(self):
        self.statement()
        start = datetime(2026, 11, 10).date()
        october, gas = self.recent_bill()
        november = bill(datetime(2026, 10, 10, 4, tzinfo=UTC), datetime(2026, 11, 10, 5, tzinfo=UTC))
        self.poll("20261112T040000Z", bills=[october, november, gas], usage=day_reads(start))
        cycle = self.build(as_of=datetime(2026, 11, 12, 4, tzinfo=UTC))["latest"]["current_billing_cycle"]
        self.assertEqual((cycle["cycle_start_date"], cycle["rate_statement_date"], cycle["projected_cost"]),
                         ("2026-11-10", "2026-09-10", 196))

    def test_forecast_withholds_for_incomplete_or_missing_days(self):
        self.statement()
        self.poll("20261012T060000Z", bills=self.recent_bill(), usage=day_reads(datetime(2026, 10, 10).date())[:-1])
        cycle = self.build(as_of=datetime(2026, 10, 12, 6, tzinfo=UTC))["latest"]["current_billing_cycle"]
        self.assertEqual(cycle["reason"], "incomplete_usage_coverage")
        self.assertIsNone(cycle["projected_cost"])

    def test_forecast_has_dedicated_no_reads_or_unknown_cycle_status(self):
        self.statement()
        self.poll("20261011T060000Z", bills=self.recent_bill())
        as_of = datetime(2026, 10, 11, 6, tzinfo=UTC)
        self.assertEqual(self.build(as_of=as_of)["latest"]["current_billing_cycle"]["reason"], "no_actual_reads")
        self.statement(billing_period_start=None, billing_period_end=None)
        self.data = self.root / "other-data"
        self.poll("20261012T060000Z", bills=[{"serviceType": "ELECTRICITY", "usageInterval": "bad"}])
        self.assertEqual(self.build(as_of=as_of)["latest"]["current_billing_cycle"]["reason"], "unknown_cycle")

    def test_bill_header_dates_alone_are_not_electricity_usage_evidence(self):
        self.statement(billing_period_start=None, billing_period_end=None)
        electric, _ = self.recent_bill()
        electric.pop("usageInterval")
        self.poll("20261011T060000Z", bills=[electric], usage=day_reads(datetime(2026, 10, 10).date()))
        cycle = self.build(as_of=datetime(2026, 10, 11, 6, tzinfo=UTC))["latest"]["current_billing_cycle"]
        self.assertEqual(cycle["reason"], "unknown_cycle")

    def test_balance_is_not_a_rate(self):
        self.statement(total_current_charges=None)
        self.poll("20261011T060000Z", bills=self.recent_bill(), usage=day_reads(datetime(2026, 10, 10).date()))
        latest = self.build(as_of=datetime(2026, 10, 11, 6, tzinfo=UTC))["latest"]
        self.assertIsNone(latest["bill_cost_modes"]["bill_calibrated_variable_rate"])
        self.assertEqual(latest["current_billing_cycle"]["reason"], "unknown_rate")

    def test_estimated_day_does_not_make_a_forecast_available(self):
        self.statement()
        day = datetime(2026, 10, 10).date()
        rows = day_reads(day)
        rows[-1]["readType"] = "ESTIMATED"
        self.poll("20261011T060000Z", bills=self.recent_bill(), usage=rows)
        latest = self.build(as_of=datetime(2026, 10, 11, 6, tzinfo=UTC))["latest"]
        self.assertEqual(latest["total_net_kwh"], 24)
        self.assertEqual(latest["current_billing_cycle"]["reason"], "incomplete_usage_coverage")

    def test_statement_with_no_fixed_charge_evidence_does_not_assume_zero(self):
        text = "Statement Date: 09/10/26\nTotal Current Charges\n$110.00\n= 400 Total Billed Usage"
        with patch.object(dashboard, "_extract_pdf_text", return_value=text):
            parsed = dashboard.parse_eversource_bill_pdf(Path("synthetic-statement.pdf"))
        self.assertIsNone(parsed["customer_charge"])
        self.assertIsNone(parsed["variable_rate_per_kwh_excluding_customer_charge"])

    def test_equal_partial_aggregate_is_not_matched(self):
        day = datetime(2026, 11, 1).date()
        self.statement(service_periods=[{"kind": "smart_meter", "start_date": str(day), "end_date": str(day), "usage_kwh": 24}])
        rows = day_reads(day)
        self.poll("20261103T060000Z", usage=rows[1:])
        rec = self.build()["latest"]["bill_reconciliation"]
        self.assertEqual(rec["expected_intervals"], 25)
        self.assertEqual(rec["difference_kwh"], 0)
        self.assertEqual(rec["status"], "partial_overlap")

    def test_reconciliation_does_not_attribute_cross_boundary_usage_to_one_period(self):
        day = datetime(2026, 11, 1).date()
        self.statement(service_periods=[{"kind": "smart_meter", "start_date": str(day), "end_date": str(day), "usage_kwh": 40}])
        start = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
        self.poll("20261103T060000Z", usage=[read(start, 40)])
        rec = self.build()["latest"]["bill_reconciliation"]
        self.assertEqual(rec["status"], "partial_overlap")
        self.assertIsNone(rec["smart_meter_overlap_kwh"])

    def test_dst_repeated_hour_is_distinct_and_sorted_by_instant(self):
        day = datetime(2026, 11, 1).date()
        rows = day_reads(day)
        self.statement(service_periods=[{"kind": "smart_meter", "start_date": str(day), "end_date": str(day), "usage_kwh": 25}])
        self.poll("20261103T060000Z", usage=rows[::-1])
        latest = self.build()["latest"]
        self.assertEqual(latest["timezone"], ZONE.key)
        self.assertEqual(latest["daily"][0]["date"], str(day))
        self.assertEqual([r["hour"] for r in latest["hourly"] if "T01:" in r["hour"]],
                         ["2026-11-01T01:00:00-04:00", "2026-11-01T01:00:00-05:00"])
        self.assertEqual(latest["series"][0]["t"], "2026-11-01T04:00:00Z")
        self.assertEqual(latest["bill_reconciliation"]["status"], "matched")

    def test_spring_forward_expects_23_intervals(self):
        day = datetime(2026, 3, 8).date()
        self.statement(service_periods=[{"kind": "smart_meter", "start_date": str(day), "end_date": str(day), "usage_kwh": 23}])
        self.poll("20260310T060000Z", usage=day_reads(day))
        rec = self.build()["latest"]["bill_reconciliation"]
        self.assertEqual((rec["expected_intervals"], rec["status"]), (23, "matched"))

    def test_missing_cadence_does_not_prove_full_reconciliation(self):
        day = datetime(2026, 11, 1).date()
        self.statement(service_periods=[{"kind": "smart_meter", "start_date": str(day), "end_date": str(day), "usage_kwh": 25}])
        self.poll("20261103T060000Z", usage=day_reads(day), manifest={"resolution": None})
        rec = self.build()["latest"]["bill_reconciliation"]
        self.assertEqual(rec["difference_kwh"], 0)
        self.assertIsNone(rec["expected_intervals"])
        self.assertEqual(rec["status"], "partial_overlap")

    def test_poll_read_and_availability_are_independent(self):
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        self.poll("20261102T060000Z", usage=[read(start, 0), read(start + timedelta(hours=1), 9, "ESTIMATED"), read(start + timedelta(hours=2), None)])
        self.poll("20261103T060000Z", manifest={"errors": {"usage": "synthetic failure"}, "latest_available_end": "2026-11-02T05:00:00Z"})
        status = self.build()["latest"]["status"]
        self.assertEqual(status["last_successful_poll_at_utc"], "20261102T060000Z")
        self.assertEqual(status["latest_actual_net_read_end_utc"], "2026-11-01T05:00:00Z")
        self.assertEqual(status["metadata_available_end"], "2026-11-02T05:00:00Z")
        self.assertEqual(status["usage_intervals"], {"actual": 1, "estimated": 1, "unknown": 0, "missing": 1})

    def test_optional_error_does_not_hide_a_successful_usage_poll(self):
        self.poll("20261102T060000Z", manifest={"poll_status": "collected", "errors": {"weather": "unavailable"}})
        status = self.build()["latest"]["status"]
        self.assertEqual(status["last_successful_poll_at_utc"], "20261102T060000Z")

    def test_missing_values_are_null_and_true_zero_cost_is_preserved(self):
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        first = read(start, 0)
        second = read(start + timedelta(hours=1), None)
        third = read(start + timedelta(hours=2), 2)
        self.poll("20261102T060000Z", usage=[first, second, third],
                  cost=[{**first, "monetaryAmount": 0}], pricing=[{**third, "cost_per_unit": 0.25}])
        series = self.build()["latest"]["series"]
        self.assertEqual((series[0]["kwh"], series[0]["cost"], series[0]["kw"]), (0, 0, None))
        self.assertEqual((series[1]["kwh"], series[1]["cost"], series[1]["cost_source"]), (None, None, "missing"))
        self.assertEqual((series[2]["cost"], series[2]["cost_source"]), (0.5, "estimated_from_rate"))

    def test_db_path_never_reads_archive_and_consumes_canonical_rows(self):
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        manifest = self.poll("20261102T060000Z", usage=[read(start, 999)])
        datasets = {"usage": [read(start, 0), read(start, 2, stream="demand")],
                    "pricing": [{**read(start), "cost_per_unit": 0.29}],
                    "demand_maxima": [{**read(start, 12), "isMaximum": "true"}]}
        self_test = self

        class HistoryContract:
            def __init__(self, path):
                self_test.assertEqual(path, self_test.root / "history.sqlite")
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                pass
            def latest_manifest(self, account=None):
                self_test.assertEqual(account, ACCOUNT)
                return manifest
            def manifests(self, account=None):
                self_test.assertEqual(account, ACCOUNT)
                return [manifest]
            def rows(self, dataset, account, start=None, end=None):
                self_test.assertEqual(account, ACCOUNT)
                return datasets.get(dataset, [])
            def ingest_archive(self, *args):
                self_test.fail("render tried to ingest the archive")

        with patch.dict(sys.modules, {"history": SimpleNamespace(History=HistoryContract)}), patch.object(dashboard, "_read_csv", side_effect=AssertionError("archive scan")):
            data = self.build(db_path=self.root / "history.sqlite", account=ACCOUNT)
        self.assertEqual(data["latest"]["total_net_kwh"], 0)
        self.assertEqual(data["historical_price_series"][0]["price_per_kwh"], 0.29)
        self.assertEqual([r["kw"] for r in data["demand_event_series"]], [2])

    def test_missing_hourly_measures_are_not_zero_averages(self):
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        self.poll("20261102T060000Z", usage=[read(start, None, stream="demand"), read(start, None)])
        data = self.build()
        self.assertIsNone(data["hourly_profile"][0]["avg_kwh"])
        self.assertIsNone(data["hourly_profile"][0]["avg_cost"])
        self.assertIsNone(data["latest"]["baseload"]["estimated_kw"])

    def test_legacy_revisions_replace_higher_demand_and_keep_price_only_rows(self):
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        self.poll("20261102T060000Z", usage=[read(start), read(start, 10, stream="demand")])
        self.poll("20261103T060000Z", usage=[read(start, 1, stream="demand")],
                  pricing=[{**read(start), "cost_per_unit": 0.2}])
        data = self.build()
        self.assertEqual(data["historical_price_series"][0]["price_per_kwh"], 0.2)
        self.assertEqual([r["kw"] for r in data["demand_event_series"]], [1])

    def test_mixed_resolution_history_does_not_double_count_usage(self):
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        self.poll("20261102T060000Z", usage=[read(start, 15, end=start + timedelta(minutes=15))], manifest={"resolution": "QUARTER_HOUR"})
        self.poll("20261103T060000Z", usage=[read(start, 30)])
        latest = self.build()["latest"]
        self.assertEqual(latest["total_net_kwh"], 30)
        self.assertEqual(latest["status"]["usage_intervals"]["actual"], 1)

    def test_real_sqlite_history_uses_revisions_after_archive_moves(self):
        from history import History

        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        self.poll("20261102T060000Z", usage=[read(start, 10, stream="demand"), read(start, 1, "ESTIMATED")])
        self.poll("20261103T060000Z", usage=[read(start, 2, stream="demand"), read(start, 0)])
        db = self.root / "history.sqlite"
        with History(db) as history:
            self.assertEqual(history.ingest_archive(self.data), 2)
        self.data.rename(self.root / "moved-archive")
        latest = self.build(db_path=db)["latest"]
        self.assertEqual(latest["account"], ACCOUNT)
        self.assertEqual(latest["total_net_kwh"], 0)
        self.assertEqual(latest["peak_kw"], 2)

    def test_latest_actual_read_is_not_replaced_by_estimate_at_new_cadence(self):
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        self.poll("20261102T060000Z", usage=[read(start, 15, end=start + timedelta(minutes=15))], manifest={"resolution": "QUARTER_HOUR"})
        self.poll("20261103T060000Z", usage=[read(start, 30, "ESTIMATED")])
        status = self.build()["latest"]["status"]
        self.assertEqual(status["latest_actual_net_read_end_utc"], "2026-11-01T04:15:00Z")

    def test_private_html_labels_status_and_never_claims_exports_are_safe(self):
        self.poll("20261102T060000Z")
        html = dashboard.generate_dashboard(self.data, self.root / "report.html").read_text()
        for label in ("Last successful poll", "Latest ACTUAL net read", "Metadata available through", "Estimated usage", "Estimated cost intervals", "Private dashboard JSON"):
            self.assertIn(label, html)
        self.assertNotIn("PII-safe", html)
        self.assertNotIn("privacySafeExport", html)

    @unittest.skipUnless(shutil.which("node"), "Node is optional")
    def test_javascript_renders_both_publication_modes_in_local_timezone(self):
        start = datetime(2026, 11, 1, 5, tzinfo=UTC)
        self.poll("20261102T060000Z", usage=[read(start), read(start + timedelta(hours=1), 0)])
        harness = """
        const assert=require('node:assert/strict');
        global.window=globalThis;
        global.devicePixelRatio=1;
        const context=new Proxy({}, {get:(_,key)=>key==='createLinearGradient'?()=>({addColorStop(){}}):()=>{}});
        const elements=new Map();
        global.document={
          getElementById(id){
            if(!elements.has(id)) elements.set(id,{getContext:()=>context,getBoundingClientRect:()=>({width:800,height:350})});
            return elements.get(id);
          },
          querySelectorAll(){return []}
        };
        """
        for split in (False, True):
            output = dashboard.generate_dashboard(self.data, self.root / "report.html", split_data=split)
            scripts = [((output.parent / source).read_text() if source else inline)
                       for source, inline in re.findall(r'<script(?: src="([^"]+)")?>(.*?)</script>', output.read_text(), re.S)]
            checks = """
            assert.equal(fmt(null),'—');
            assert.match(money(0), /^\\$0[.,]00$/);
            assert.match(timeLabel('2026-11-01T05:00:00Z'), /EDT|GMT-4/);
            assert.match(timeLabel('2026-11-01T06:00:00Z'), /EST|GMT-5/);
            assert.match(document.getElementById('app').innerHTML, /2026-11-01T07:00:00Z/);
            """
            run = subprocess.run(["node", "-"], input=harness + "\n".join(scripts) + checks,
                                 text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_offline_deterministic_and_manual_discovery_is_opt_in(self):
        self.poll("20261102T060000Z")
        self.statement()
        (self.root / "manual_bills").symlink_to(self.manual)
        a = dashboard.build_dashboard_data(self.data)
        b = dashboard.build_dashboard_data(self.data)
        self.assertEqual(a, b)
        self.assertEqual(a["manual_bills"], [])
        self.assertEqual(self.build()["manual_bills"][0]["total_current_charges"], 110)
        self.fetch.assert_not_called()
        self.assertEqual(dashboard.build_dashboard_data(self.data, benchmark_prices=[])["price_benchmarks"], [])

    def test_presentation_limit_does_not_remove_reads_from_reconciliation(self):
        day = datetime(2026, 11, 1).date()
        self.statement(service_periods=[{"kind": "smart_meter", "start_date": str(day), "end_date": str(day), "usage_kwh": 25}])
        self.poll("20261103T060000Z", usage=day_reads(day))
        with patch.object(dashboard, "INTERVAL_POINTS", 2), patch.object(dashboard, "HOURLY_POINTS", 1):
            data = self.build()
        latest = data["latest"]
        self.assertEqual((len(latest["series"]), len(latest["hourly"])), (2, 1))
        self.assertEqual(latest["bill_reconciliation"]["status"], "matched")

    def test_default_html_is_one_private_file_with_safe_json(self):
        sentinel = "</script><img src=x>&\u2028"
        self.poll("20261102T060000Z", manifest={"warnings": {"synthetic": sentinel}})
        result = dashboard.generate_dashboard(self.data, self.root / "report.html")
        html = result.read_text()
        self.assertNotIn(sentinel, html)
        self.assertIn(r"\u003c/script\u003e\u003cimg", html)
        self.assertNotIn("<script src=", html)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob("*.js")), [])

    def test_atomic_split_fails_without_breaking_previous_html_data_pair(self):
        output = self.root / "report.html"
        self.poll("20261102T060000Z")
        dashboard.generate_dashboard(self.data, output, True)
        old_html = output.read_text()
        old_path = self.root / re.search(r'<script src="([^"]+)"', old_html)[1]
        old_bytes = old_path.read_bytes()
        self.poll("20261103T060000Z")
        replace = dashboard.os.replace

        def fail_html(src, dest):
            if Path(dest) == output:
                raise OSError("synthetic publication failure")
            replace(src, dest)

        with patch.object(dashboard.os, "replace", side_effect=fail_html):
            with self.assertRaisesRegex(OSError, "synthetic publication failure"):
                dashboard.generate_dashboard(self.data, output, True)
        self.assertEqual(output.read_text(), old_html)
        self.assertEqual(old_path.read_bytes(), old_bytes)
        dashboard.generate_dashboard(self.data, output, True)
        self.assertNotEqual(output.read_text(), old_html)

    def test_shareable_artifact_contains_only_allowlisted_aggregate_metrics(self):
        self.poll("20261102T060000Z", usage=day_reads(datetime(2026, 11, 1).date()),
                  manifest={"warnings": {"synthetic-diagnostic-key": "synthetic-warning-value"}, "entity_id": "synthetic-id"})
        output = dashboard.generate_dashboard(self.data, self.root / "share.html", shareable=True, split_data=True)
        html = output.read_text()
        for banned in (ACCOUNT, "synthetic-meter", "synthetic-diagnostic-key", "synthetic-warning-value", "synthetic-id", str(self.data), "2026-11-01", "bill_urn", "service_points", "privacySafeExport", "<script src="):
            self.assertNotIn(banned, html)
        public = json.loads(re.search(r'<script type="application/json" id="dashboard-summary">(.*?)</script>', html)[1])
        self.assertEqual(set(public), {"total_net_kwh", "estimated_interval_cost_usd", "peak_demand_kw"})
        self.assertEqual(public["total_net_kwh"], 25)
        self.assertIn("Aggregates shared", html)
        self.assertEqual(list(self.root.glob("*.js")), [])
