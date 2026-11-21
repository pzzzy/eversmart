"""History and planner tests using temporary, synthetic collector output only."""

import csv
import json
import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from zoneinfo import ZoneInfo

import history


ACCOUNT = "test-meter-01"
STAMP = "20260312T120000Z"
INTERVAL = "2026-03-10T12:00:00Z/2026-03-10T12:15:00Z"


def utc(text):
    return datetime.fromisoformat(text + "+00:00").astimezone(UTC)


def reading(**changes):
    return {
        "utility_id": ACCOUNT, "service_point_uuid": "test-sp", "stream": "netUsage",
        "serviceQuantityIdentifier": "NET_USAGE", "unit": "KWH", "timeInterval": INTERVAL,
        "readType": "ACTUAL", "value": "1.00", "monetaryAmount": "", **changes,
    }


def write_csv(run, dataset, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with (run / f"{dataset}.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.db = self.root / "private" / "nested" / "history.sqlite"

    def run_fixture(self, stamp=STAMP, *, manifest=None, **datasets):
        run = self.archive / stamp
        run.mkdir()
        meta = {"account": ACCOUNT, "retrieved_at_utc": stamp, "resolution": "QUARTER_HOUR",
                "timezone": "America/New_York", **(manifest or {})}
        for dataset, rows in datasets.items():
            write_csv(run, dataset, rows)
        (run / "manifest.json").write_text(json.dumps(meta), encoding="utf-8")
        return run, meta

    def test_archive_only_imports_finalized_timestamp_runs_and_is_idempotent(self):
        first, first_meta = self.run_fixture(usage=[reading()])
        _, second_meta = self.run_fixture("20260313T120000Z", manifest={"account": "test-other"}, usage=[])
        self.run_fixture("20260313T120001Z.partial", usage=[reading()])
        self.run_fixture("20260230T120002Z", usage=[reading()])
        self.run_fixture("20260313T120003Z", manifest={"account": None}, usage=[reading()])
        unfinished = self.archive / "20260313T120004Z"
        unfinished.mkdir()
        (unfinished / "manifest.json").write_text('{"account":', encoding="utf-8")
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 2)
            self.assertFalse(h.ingest_run(first))
            self.assertIsNone(h.latest_manifest("unknown"))
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 0)
            self.assertEqual(h.manifests(), [first_meta, second_meta])
            self.assertEqual(h.latest_manifest(), second_meta)
            self.assertEqual(h.manifests(ACCOUNT), [first_meta])
            self.assertEqual(h.latest_manifest(ACCOUNT), first_meta)

    def test_file_and_new_parent_directories_are_private(self):
        with history.History(self.db):
            for path in (self.db.parent, self.db.parent.parent):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
            for sidecar in self.db.parent.glob("history.sqlite-*"):
                self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)

    def test_existing_database_is_restricted_before_opening_wal(self):
        self.db.parent.mkdir(parents=True)
        self.db.touch(mode=0o644)
        with history.History(self.db):
            self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
            for suffix in ("-wal", "-shm"):
                self.assertEqual(stat.S_IMODE(self.db.with_name(self.db.name + suffix).stat().st_mode), 0o600)

    def test_database_parent_database_file_and_sidecars_cannot_be_symlinks(self):
        self.db.parent.mkdir(parents=True)
        target = self.root / "untouched"
        target.write_text("unchanged", encoding="utf-8")
        target.chmod(0o644)
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                link = self.db.with_name(self.db.name + suffix)
                link.symlink_to(target)
                try:
                    with self.assertRaises(OSError), history.History(self.db):
                        self.fail("opened a linked database artifact")
                    self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                finally:
                    link.unlink(missing_ok=True)
        alias = self.root / "alias"
        alias.symlink_to(self.db.parent, target_is_directory=True)
        with self.assertRaises(OSError), history.History(alias / "nested" / "unexpected.sqlite"):
            self.fail("opened a database through a linked directory")
        self.assertFalse((self.db.parent / "nested").exists())

    def test_finalized_same_second_collector_runs_are_independently_recoverable(self):
        _, first = self.run_fixture(usage=[reading()])
        second_dir, second = self.run_fixture(
            STAMP + ".abcde_12", manifest={"retrieved_at_utc": STAMP, "target_count": 2},
            usage=[reading(value="2.0")],
        )
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 2)
            self.assertEqual(h.manifests(), [first, second])
            self.assertEqual(h.rows("usage", ACCOUNT), [reading(value="2.0")])
            self.assertFalse(h.ingest_run(second_dir))

    def test_same_second_revision_order_uses_manifest_microseconds_not_suffix(self):
        _, legacy = self.run_fixture(usage=[reading(value="1")])
        early = self.run_fixture(STAMP + ".zzzzzzzz", usage=[reading(value="2")],
                                 manifest={"retrieved_at_utc": "2026-03-12T12:00:00.123456Z"})[1]
        late = self.run_fixture(STAMP + ".aaaaaaaa", usage=[reading(value="3")],
                                manifest={"retrieved_at_utc": "2026-03-12T12:00:00.123457+00:00"})[1]
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 3)
            self.assertEqual(h.manifests(), [legacy, early, late])
            self.assertEqual(h.latest_manifest(), late)
            self.assertEqual(h.rows("usage", ACCOUNT), [reading(value="3")])

    def test_parallel_import_commits_run_once(self):
        run, _ = self.run_fixture(usage=[reading()])
        gate = Barrier(2)

        def load():
            gate.wait(timeout=10)
            with history.History(self.db) as h:
                return h.ingest_run(run)

        with ThreadPoolExecutor(2) as executor:
            self.assertCountEqual(executor.map(lambda _: load(), range(2)), [True, False])
        with history.History(self.db) as h:
            self.assertEqual(h.rows("usage", ACCOUNT), [reading()])

    def test_bad_csv_rolls_back_manifest_and_other_datasets_then_can_retry(self):
        run, _ = self.run_fixture(usage=[reading()])
        (run / "weather.csv").write_text('premise_uuid,timeInterval\n"unclosed', encoding="utf-8")
        with history.History(self.db) as h:
            with self.assertRaises(csv.Error):
                h.ingest_run(run)
            self.assertIsNone(h.latest_manifest())
            self.assertEqual(h.rows("usage", ACCOUNT), [])
            (run / "weather.csv").unlink()
            self.assertTrue(h.ingest_run(run))

    def test_archive_skips_bad_runs_counts_them_once_and_retries_after_repair(self):
        current = {"archive_format": 1, "poll_status": "collected",
                   "collection_windows": {ACCOUNT: [INTERVAL]}, "usage_rows": 1}
        malformed, _ = self.run_fixture(usage=[reading()], manifest=current)
        (malformed / "weather.csv").write_text('premise_uuid,timeInterval\n"unclosed', encoding="utf-8")
        missing, _ = self.run_fixture("20260312T120001Z", manifest=current)
        truncated, _ = self.run_fixture("20260312T120002Z", manifest=current, usage=[])
        invalid_header, _ = self.run_fixture("20260312T120003Z", manifest={**current, "usage_rows": 0}, usage=[])
        (invalid_header / "usage.csv").write_text("unexpected,header\n", encoding="utf-8")
        short_row, _ = self.run_fixture("20260312T120004Z", manifest=current, usage=[reading()])
        usage_path = short_row / "usage.csv"
        lines = usage_path.read_text(encoding="utf-8").splitlines()
        usage_path.write_text(lines[0] + "\n" + lines[1].rsplit(",", 1)[0] + "\n", encoding="utf-8")
        # The unversioned count is advisory, even if it differs from the CSV.
        _, legacy = self.run_fixture("20260313T120000Z", manifest={"usage_rows": 99}, usage=[reading(value="2")])
        _, prior_format = self.run_fixture("20260313T120001Z", usage=[reading(value="2")],
                                          manifest={"poll_status": "collected", "collection_windows": {}, "usage_rows": 99})
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 2)
            self.assertEqual(h.archive_failures, 5)
            self.assertEqual(h.manifests(), [legacy, prior_format])
            self.assertEqual(h.rows("usage", ACCOUNT), [reading(value="2")])
            (malformed / "weather.csv").unlink()
            for run in (missing, truncated, short_row):
                write_csv(run, "usage", [reading()])
            (invalid_header / "usage.csv").write_text("utility_id,timeInterval,stream,value\n", encoding="utf-8")
            self.assertEqual(h.ingest_archive(self.archive), 5)
            self.assertEqual(h.archive_failures, 0)
            self.assertEqual(h.ingest_archive(self.archive), 0)
            self.assertEqual(h.rows("usage", ACCOUNT), [reading(value="2")])

    def test_canonical_identity_uses_utc_service_point_stream_quantity_and_both_endpoints(self):
        equivalent = reading(timeInterval="2026-03-10T08:00:00-04:00/2026-03-10T08:15:00-04:00", value="2.00")
        changed = [reading(service_point_uuid="secondary-sp", utility_id="second"), reading(stream="demand"),
                   reading(serviceQuantityIdentifier="DELIVERED"),
                   reading(timeInterval=INTERVAL.replace("12:15", "12:30"))]
        first, _ = self.run_fixture(usage=[reading(), *changed])
        second, _ = self.run_fixture("20260313T120000Z", usage=[equivalent])
        with history.History(self.db) as h:
            self.assertTrue(h.ingest_run(second))
            self.assertTrue(h.ingest_run(first))
            self.assertCountEqual(h.rows("usage", ACCOUNT), [equivalent, *changed[1:]])
            self.assertEqual(h.rows("usage", "second"), [changed[0]])
            self.assertEqual(h.rows("usage", ACCOUNT, utc("2026-03-10T12:15"), utc("2026-03-10T12:31")), [changed[-1]])
            self.assertEqual(h.rows("usage", "other"), [])

    def test_actual_wins_and_latest_usable_fields_survive_out_of_order_ingestion(self):
        paths = []
        for day, kind, raw_value, dollars in [(12, "ACTUAL", "1.0", "0.42"), (13, "ACTUAL", "0", ""),
                                              (14, "ESTIMATED", "9", "7"), (15, "ACTUAL", "NaN", "")]:
            value = reading(readType=kind, value=raw_value, monetaryAmount=dollars)
            paths.append(self.run_fixture(f"202603{day}T120000Z", usage=[value], cost=[value])[0])
        for i, order in enumerate((paths, reversed(paths))):
            with history.History(self.db.with_name(f"test-{i}.sqlite")) as h:
                for path in order:
                    self.assertTrue(h.ingest_run(path))
                for dataset in ("usage", "cost"):
                    self.assertEqual(h.rows(dataset, ACCOUNT), [reading(value="0", monetaryAmount="0.42")])

    def test_latest_available_service_point_fields_win_regardless_of_import_order(self):
        paths = []
        for day, plan in [(12, "earliest"), (13, "revised"), (14, "")]:
            sp = {"account": ACCOUNT, "service_point_uuid": "test-sp", "rate_plan_code": plan}
            paths.append(self.run_fixture(f"202603{day}T120000Z", service_points=[sp])[0])
        with history.History(self.db) as h:
            for i in (2, 0, 1):
                h.ingest_run(paths[i])
            self.assertEqual(h.rows("service_points", ACCOUNT)[0]["rate_plan_code"], "revised")

    def test_unambiguous_legacy_local_readings_use_timezone_and_utility_fallback(self):
        legacy = reading(service_point_uuid="", timeInterval="20260310T08:00/20260310T08:15")
        revision = reading(service_point_uuid="", value="2")
        first, _ = self.run_fixture(usage=[legacy])
        second, _ = self.run_fixture("20260313T120000Z", usage=[revision])
        with history.History(self.db) as h:
            self.assertTrue(h.ingest_run(first))
            self.assertEqual(h.rows("usage", ACCOUNT), [legacy])
            self.assertTrue(h.ingest_run(second))
            self.assertEqual(h.rows("usage", ACCOUNT), [revision])

    def test_manifest_uuid_links_legacy_rows_without_changing_their_payload(self):
        legacy = reading(service_point_uuid="", value="2")
        first, _ = self.run_fixture(usage=[reading()])
        second, _ = self.run_fixture("20260313T120000Z", manifest={"service_point_uuid": "test-sp"}, usage=[legacy])
        with history.History(self.db) as h:
            h.ingest_run(second)
            self.assertEqual(h.rows("usage", ACCOUNT), [legacy])
            h.ingest_run(first)
            self.assertEqual([r["value"] for r in h.rows("usage", ACCOUNT)], ["2"])

    def test_malformed_ambiguous_and_nonexistent_read_times_do_not_become_canonical(self):
        valid = reading(timeInterval="2026-11-01T01:00-04:00/2026-11-01T01:15-04:00")
        invalid = [reading(timeInterval=interval) for interval in (
            "2026-02-30T00:00Z/2026-03-02T00:15Z", "2026-03-10/2026-03-11",
            "garbage", "2026-11-01T01:00/2026-11-01T01:15", "2026-03-08T02:00/2026-03-08T02:15",
            "2026-03-10T12:15Z/2026-03-10T12:00Z", "2026-03-10T12:00Z/2026-03-10T12:00Z",
            "2026-03-10T12:00+00:99/2026-03-10T12:15+00:99",
        )]
        run, _ = self.run_fixture(usage=[valid, *invalid])
        with history.History(self.db) as h:
            self.assertTrue(h.ingest_run(run))
            self.assertEqual(h.rows("usage", ACCOUNT), [valid])

    def test_optional_datasets_preserve_payload_and_dedup_by_dataset_identity(self):
        rate = reading(rate_plan_code="test-rate", rate_type="TOU", tier="1", tou_label="peak",
                       component_attributes="charge=supply", cost_per_unit="0.12")
        rates = [rate, dict(rate, tier="2"), dict(rate, rate_plan_code="other-rate"),
                 dict(rate, rate_type="FLAT"), dict(rate, tou_label="off-peak"),
                 dict(rate, component_attributes="charge=delivery")]
        weather = {"premise_uuid": "premise-a", "timeInterval": "2026-03-10/2026-03-11",
                   "date": "2026-03-10", "mean_temperature": "32"}
        bills = {"bill_urn": "bill-1", "segment_urn": "segment-1", "timeInterval": "2026-02-01/2026-03-01",
                 "usageInterval": "2026-02-01/2026-03-01", "estimated": "False", "currentAmount": "10.20"}
        sp = {"account": ACCOUNT, "service_point_uuid": "test-sp", "rate_plan_code": "test-rate"}
        demand = {"utility_id": ACCOUNT, "service_point_uuid": "test-sp", "timeInterval": INTERVAL,
                  "value": "2.512", "unit": "KW", "readType": "ACTUAL", "isMaximum": "True"}
        gb = {"dataset": "usage", "source_file": "meter-a.csv", "service_point": ACCOUNT,
              "type": "Electric usage", "date": "2026-03-10", "start_time": "08:00",
              "end_time": "08:14", "usage_kwh": "01.20", "cost": "", "notes": "test note"}
        optional = {"pricing": rates, "weather": [weather, dict(weather, premise_uuid="premise-b")],
                    "bills": [bills, dict(bills, segment_urn="segment-2"), dict(bills, bill_urn="bill-2")],
                    "service_points": [sp], "demand_maxima": [demand], "green_button_rows": [gb]}
        self.run_fixture(usage=[reading()], **optional)
        latest_rate = dict(rate, cost_per_unit="0.13")
        latest_weather = dict(weather, mean_temperature="35")
        latest_bill = dict(bills, timeInterval="2026-02-02/2026-03-01", currentAmount="12.55")
        latest_gb = dict(gb, source_file="export.csv", cost="0.24")
        self.run_fixture("20260313T120000Z", usage=[reading()], pricing=[latest_rate], weather=[latest_weather],
                         bills=[latest_bill], green_button_rows=[latest_gb], cost=[])
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 2)
            optional.update(pricing=[latest_rate, *rates[1:]], weather=[latest_weather, optional["weather"][1]],
                            bills=[latest_bill, *optional["bills"][1:]], green_button_rows=[latest_gb])
            for dataset, rows in optional.items():
                with self.subTest(dataset=dataset):
                    self.assertCountEqual(h.rows(dataset, ACCOUNT), rows)

    def test_coverage_only_uses_finite_net_usage_for_requested_account_and_cadence(self):
        def interval(a, b):
            return f"2026-03-10T{a}Z/2026-03-10T{b}Z"

        self.run_fixture(usage=[reading(), reading(timeInterval=interval("12:15", "12:30"), value="0"),
                                reading(timeInterval=interval("12:30", "12:45"), value="inf"),
                                reading(timeInterval=interval("12:45", "13:00"), stream="demand"),
                                reading(timeInterval=interval("13:00", "13:15"), serviceQuantityIdentifier="DELIVERED"),
                                reading(timeInterval=interval("13:15", "13:30"), utility_id="other", service_point_uuid="other"),
                                reading(timeInterval=interval("13:30", "14:30")),
                                reading(timeInterval=interval("14:30", "14:45"), value="-0.8", readType="ESTIMATED"),
                                reading(timeInterval=interval("14:46", "15:01"))])
        with history.History(self.db) as h:
            h.ingest_archive(self.archive)
            self.assertEqual(h.coverage(ACCOUNT, "QUARTER_HOUR"), [
                (utc("2026-03-10T12:00"), utc("2026-03-10T12:30")),
                (utc("2026-03-10T14:30"), utc("2026-03-10T14:45")),
            ])
            self.assertEqual(h.coverage(ACCOUNT, "HALF_HOUR"), [])

    def test_green_button_time_filter_uses_local_usage_times_not_the_full_day(self):
        row = {"dataset": "usage", "service_point": ACCOUNT, "type": "Electric usage",
               "date": "03/10/2026", "start_time": "23:45", "end_time": "00:00", "usage_kwh": "1.20"}
        self.run_fixture(green_button_rows=[row])
        with history.History(self.db) as h:
            h.ingest_archive(self.archive)
            self.assertEqual(h.rows("green_button_rows", ACCOUNT, end=utc("2026-03-11T03:45")), [])
            self.assertEqual(h.rows("green_button_rows", ACCOUNT, start=utc("2026-03-11T03:45")), [row])
            self.assertEqual(h.rows("green_button_rows", ACCOUNT, start=utc("2026-03-11T04:00")), [])

    def test_coverage_can_plan_secondary_meters_from_a_multi_point_collection(self):
        secondary = "test-meter-02"
        meters = [{"account": secondary, "service_point_uuid": "second-sp", "timezone": "America/Chicago"}]
        secondary_read = reading(utility_id="", service_point_uuid="second-sp",
                                 timeInterval="2026-03-10T07:00/2026-03-10T07:15")
        self.run_fixture(manifest={"service_points": meters}, usage=[reading(), secondary_read])
        with history.History(self.db) as h:
            h.ingest_archive(self.archive)
            self.assertEqual(h.coverage(secondary, "QUARTER_HOUR"),
                             [(utc("2026-03-10T12:00"), utc("2026-03-10T12:15"))])

    def test_secondary_history_is_scoped_to_the_actual_meter_not_inventory(self):
        secondary, sibling, planned = "meter-b", "meter-c", "meter-d"
        points = [{"account": secondary, "service_point_uuid": "secondary-sp", "timezone": "America/Chicago",
                   "available_interval": "2026-03-01/2026-04-01", "resolution": "HOUR", "premise_uuid": "premise-b"},
                  {"account": sibling, "service_point_uuid": "sibling-sp", "premise_uuid": "premise-c"},
                  {"account": planned, "service_point_uuid": "planned-sp"}]
        second_read = reading(utility_id="", service_point_uuid="secondary-sp", value="2")
        primary_bill = {"bill_urn": "bill-primary", "currentAmount": "10", "utility_id": secondary}
        weather = {"premise_uuid": "premise-b", "date": "2026-03-10", "mean_temperature": "32"}
        _, first = self.run_fixture(manifest={"service_points": points, "errors": {"pricing": "unavailable"},
                                              "collection_windows": {ACCOUNT: [INTERVAL], planned: [INTERVAL], sibling: []}},
                                    usage=[reading(), second_read], cost=[reading(), second_read], pricing=[second_read],
                                    demand_maxima=[second_read], service_points=points, bills=[primary_bill], weather=[weather])
        _, newer = self.run_fixture("20260313T120000Z", manifest={"service_points": points},
                                    usage=[reading(value="3")], service_points=points)
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 2)
            self.assertEqual(h.manifests(), [first, newer])
            self.assertEqual(h.latest_manifest(ACCOUNT), newer)
            self.assertEqual(h.rows("usage", ACCOUNT), [reading(value="3")])
            for dataset in ("usage", "cost", "pricing", "demand_maxima"):
                self.assertEqual(h.rows(dataset, secondary), [second_read])
            self.assertEqual(h.rows("weather", secondary), [weather])
            self.assertEqual(h.rows("weather", ACCOUNT), [])
            self.assertEqual(h.rows("bills", ACCOUNT), [primary_bill])
            self.assertEqual(h.rows("bills", secondary), [])
            self.assertEqual(h.rows("service_points", secondary), [points[0]])
            self.assertIsNone(h.latest_manifest(sibling))
            self.assertIsNotNone(h.latest_manifest(planned))
            meta, = h.manifests(secondary)
            self.assertEqual(h.latest_manifest(secondary), meta)
            self.assertEqual(meta["account"], secondary)
            self.assertEqual(meta["timezone"], "America/Chicago")
            self.assertEqual(meta["latest_available_end"], "2026-04-01")
            self.assertEqual(meta["errors"], first["errors"])
            self.assertEqual(meta["service_points"], points)

    def test_secondary_usage_is_successful_even_if_primary_did_not_poll_usage(self):
        secondary, other = "meter-b", "meter-c"
        member = reading(utility_id=secondary, service_point_uuid="point-b")
        _, run = self.run_fixture(usage=[member], manifest={
            "poll_status": "no_op", "success": False, "primary_usage_rows": 0, "usage_rows": 1,
            "collection_windows": {ACCOUNT: [], secondary: [INTERVAL], other: [INTERVAL]},
        })
        with history.History(self.db) as h:
            self.assertEqual(h.ingest_archive(self.archive), 1)
            self.assertEqual(h.latest_manifest(ACCOUNT), run)
            _, b, c = (h.latest_manifest(a) or {} for a in (ACCOUNT, secondary, other))
            self.assertEqual((b["poll_status"], b["success"], b["usage_rows"]), ("collected", True, 1))
            self.assertEqual((c["poll_status"], c["success"], c["usage_rows"]), ("no_op", False, 0))

    def test_existing_db_migrates_without_archive_and_can_reindex_archived_runs(self):
        secondary = "meter-b"
        points = [{"account": secondary, "service_point_uuid": "secondary-sp", "timezone": "America/Chicago"}]
        row = reading(utility_id="", service_point_uuid="secondary-sp",
                      timeInterval="2026-03-10T07:00/2026-03-10T07:15")
        _, manifest = self.run_fixture(service_points=points, usage=[reading(), row])
        self.db.parent.mkdir(parents=True)
        timestamp = "2026-03-12T12:00:00.000000Z"
        with sqlite3.connect(self.db) as db:
            # Pre-review schema and a real legacy row keyed by poll primary.
            db.executescript("""
                CREATE TABLE runs (account TEXT NOT NULL, retrieved TEXT NOT NULL, manifest TEXT NOT NULL,
                                   PRIMARY KEY (account, retrieved)) WITHOUT ROWID;
                CREATE TABLE readings (account TEXT NOT NULL, dataset TEXT NOT NULL, identity TEXT NOT NULL,
                    start_utc TEXT, end_utc TEXT, cadence TEXT, net_account TEXT, rank INTEGER NOT NULL,
                    retrieved TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, field_versions TEXT NOT NULL,
                    PRIMARY KEY (account, dataset, identity)) WITHOUT ROWID;
            """)
            db.execute("INSERT INTO runs VALUES (?, ?, ?)", (ACCOUNT, timestamp, json.dumps(manifest)))
            lo, hi = "2026-03-10T11:00:00.000000Z", "2026-03-10T11:15:00.000000Z"
            identity = json.dumps(["uuid", "secondary-sp", "netUsage", "NET_USAGE", lo, hi], separators=(",", ":"))
            versions = {field: [2 if field == "value" else 5, timestamp, 1] for field in row if row[field]}
            db.execute("INSERT INTO readings VALUES (?, 'usage', ?, ?, ?, 'QUARTER_HOUR', NULL, 5, ?, 1, ?, ?)",
                       (ACCOUNT, identity, lo, hi, timestamp, json.dumps(row), json.dumps(versions)))
            db.execute("INSERT INTO readings VALUES (?, 'service_points', ?, NULL, NULL, NULL, NULL, 4, ?, 0, ?, '{}')",
                       (ACCOUNT, '["uuid","secondary-sp"]', timestamp, json.dumps(points[0])))
        with history.History(self.db) as h:
            self.assertEqual(h.rows("usage", ACCOUNT), [])
            self.assertEqual(h.rows("usage", secondary), [row])
            self.assertEqual((h.latest_manifest(secondary) or {})["account"], secondary)
            self.assertEqual(h.coverage(secondary, "QUARTER_HOUR"), [(utc("2026-03-10T12:00"), utc("2026-03-10T12:15"))])
            self.assertEqual(h.ingest_archive(self.archive), 1)
            self.assertEqual(h.ingest_archive(self.archive), 0)
        with history.History(self.db) as h:
            self.assertEqual(h.rows("usage", ACCOUNT), [reading()])
            self.assertEqual(h.rows("usage", secondary), [row])
            self.assertEqual(h.manifests(), [manifest])


class PlanWindowsTests(unittest.TestCase):
    def test_default_without_coverage_only_requests_latest_720_hours(self):
        available = "2026-01-01T00:00Z/2026-03-05T00:00Z"
        self.assertEqual(history.plan_windows(available, "QUARTER_HOUR", []),
                         ["2026-02-03T00:00:00Z/2026-03-05T00:00:00Z"])

    def test_default_catches_up_from_observed_end_with_overlap(self):
        available = "2026-01-01T05:00+05:00/2026-03-05T05:00+05:00"
        coverage = [(utc("2026-01-27T00:00"), utc("2026-01-31T00:00"))]
        self.assertEqual(history.plan_windows(available, "HOUR", iter(coverage)), [
            "2026-01-28T00:00:00Z/2026-02-27T00:00:00Z",
            "2026-02-27T00:00:00Z/2026-03-05T00:00:00Z",
        ])

    def test_default_refreshes_overlap_even_when_up_to_date(self):
        coverage = [(utc("2026-01-02T00:00"), utc("2026-03-05T10:00"))]
        self.assertEqual(history.plan_windows("2026-01-01T00:00Z/2026-03-05T00:00Z", "HOUR", coverage),
                         ["2026-03-02T00:00:00Z/2026-03-05T00:00:00Z"])

    def test_default_catches_up_at_new_availability_start_if_coverage_has_aged_out(self):
        available = "2026-01-01T00:00Z/2026-02-05T00:00Z"
        previous = [(utc("2025-12-05T00:00"), utc("2025-12-06T00:00"))]
        self.assertEqual(history.plan_windows(available, "HOUR", previous), [
            "2026-01-01T00:00:00Z/2026-01-31T00:00:00Z",
            "2026-01-31T00:00:00Z/2026-02-05T00:00:00Z",
        ])

    def test_backfill_handles_partial_slots_unordered_overlaps_and_odd_boundary(self):
        available = "2026-03-10T00:02:45Z/2026-03-10T03:01:10Z"
        coverage = [(utc("2026-03-10T02:01"), utc("2026-03-10T02:18")),
                    (utc("2026-03-10T01:21"), utc("2026-03-10T01:45")),
                    (utc("2026-03-10T01:00"), utc("2026-03-10T01:30")),
                    (utc("2026-03-09T01:00"), utc("2026-03-10T00:20"))]
        self.assertEqual(history.plan_windows(available, "QUARTER_HOUR", coverage, backfill=True), [
            "2026-03-10T00:15:00Z/2026-03-10T01:00:00Z",
            "2026-03-10T01:45:00Z/2026-03-10T03:00:00Z",
        ])

    def test_backfill_chunks_a_long_gap_and_does_not_fetch_covered_slots(self):
        available = "2026-01-01T00:00Z/2026-03-05T00:00Z"
        coverage = [(utc("2026-03-03T00:00"), utc("2026-03-05T00:00"))]
        self.assertEqual(history.plan_windows(available, "HALF_HOUR", coverage, backfill=True), [
            "2026-01-01T00:00:00Z/2026-01-31T00:00:00Z", "2026-01-31T00:00:00Z/2026-03-02T00:00:00Z",
            "2026-03-02T00:00:00Z/2026-03-03T00:00:00Z",
        ])
        self.assertEqual(history.plan_windows(available, "DAY", [(utc("2026-01-01T00:00"), utc("2026-03-05T00:00"))],
                                              backfill=True), [])

    def test_planner_rejects_ambiguous_availability_and_bad_options(self):
        with self.assertRaises(ValueError):
            history.plan_windows("2026-03-10T00:00/2026-03-10T01:00", "HOUR", [])
        for options in [("nonsense", 72), ("HOUR", -1)]:
            with self.assertRaises(ValueError):
                history.plan_windows(INTERVAL, options[0], [], overlap_hours=options[1])

    def test_no_fetch_for_availability_containing_no_complete_slot(self):
        self.assertEqual(history.plan_windows("2026-03-10T12:01Z/2026-03-10T12:25Z", "HOUR", []), [])

    def test_daily_availability_retains_local_boundaries_across_dst(self):
        available = "2026-03-07T00:00:00-05:00/2026-03-10T00:00:00-04:00"
        last_day = (utc("2026-03-09T04:00"), utc("2026-03-10T04:00"))
        self.assertEqual(history.plan_windows(available, "DAY", [last_day], backfill=True),
                         ["2026-03-07T05:00:00Z/2026-03-09T04:00:00Z"])

    def test_daily_backfill_only_subtracts_complete_days(self):
        available = "2026-03-08T00:00:00-05:00/2026-03-11T00:00:00-04:00"
        partial = (utc("2026-03-09T05:00"), utc("2026-03-10T03:00"))
        self.assertEqual(history.plan_windows(available, "DAY", [partial], backfill=True),
                         ["2026-03-08T05:00:00Z/2026-03-11T04:00:00Z"])

    def test_daily_planner_uses_calendar_midnights_across_both_dst_transitions(self):
        zone = "America/New_York"
        for available, expected in [
            ("2026-02-22/2026-04-25", ["2026-02-22T05:00:00Z/2026-03-24T04:00:00Z",
                                       "2026-03-24T04:00:00Z/2026-04-23T04:00:00Z",
                                       "2026-04-23T04:00:00Z/2026-04-25T04:00:00Z"]),
            ("2026-10-20/2026-12-22", ["2026-10-20T04:00:00Z/2026-11-18T05:00:00Z",
                                       "2026-11-18T05:00:00Z/2026-12-18T05:00:00Z",
                                       "2026-12-18T05:00:00Z/2026-12-22T05:00:00Z"]),
        ]:
            with self.subTest(available=available):
                self.assertEqual(history.plan_windows(available, "DAY", [], backfill=True, timezone=zone), expected)
                self.assertEqual(history.plan_explicit_windows(available, "DAY", timezone=ZoneInfo(zone)), expected)

    def test_daily_refresh_chooses_the_latest_full_days_within_720_hours(self):
        self.assertEqual(history.plan_windows("2026-09-01/2026-11-16", "DAY", [], timezone="America/New_York"),
                         ["2026-10-18T04:00:00Z/2026-11-16T05:00:00Z"])

    def test_daily_backfill_subtracts_full_local_days_inside_a_partial_range(self):
        covered = [(utc("2026-10-31T05:00"), utc("2026-11-02T06:00"))]
        self.assertEqual(history.plan_windows("2026-10-30/2026-11-04", "DAY", covered, backfill=True,
                                              timezone="America/New_York"),
                         ["2026-10-30T04:00:00Z/2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z/2026-11-04T05:00:00Z"])

    def test_explicit_subdaily_range_preserves_requested_bounds(self):
        self.assertEqual(history.plan_explicit_windows("2026-01-01T01:25-05:00/2026-02-03T08:15+01:00", "HOUR"),
                         ["2026-01-01T06:25:00Z/2026-01-31T06:25:00Z", "2026-01-31T06:25:00Z/2026-02-03T07:15:00Z"])


if __name__ == "__main__":
    unittest.main()
