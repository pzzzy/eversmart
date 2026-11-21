import base64
import csv
import gzip
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from cookie_convert import parse_cookieinfo
from dashboard import build_dashboard_data
from eversmart import (
    EversourceClient,
    collect_once,
    cap_interval_hours,
    iter_bill_segments,
    iter_pricing_components,
    iter_stream_reads,
    iter_weather_rows,
    parse_green_button_zip,
    write_optional_warning,
    write_csv,
    write_json,
)


FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
ENTITY_ID = "fixture-entity"
SERVICE_POINT = "fixture-point"
METER_ZONE = ZoneInfo("America/New_York")
STREAM_FIELDS = (
    "utility_id", "stream", "serviceQuantityIdentifier", "unit", "timeInterval",
    "readType", "value", "monetaryAmount", "isPeakPeriod", "service_agreement_uuid",
    "service_point_uuid",
)


def fake_token(expiry):
    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': expiry})}."


def write_usage_run(data_dir, stamp, days):
    """Write an explicitly daily legacy snapshot with complete local-day intervals."""
    run = data_dir / stamp
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({
        "retrieved_at_utc": stamp, "account": SERVICE_POINT,
        "resolution": "DAY", "timezone": METER_ZONE.key,
    }))
    with (run / "usage.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(STREAM_FIELDS)
        for day, kwh in days:
            start = datetime.combine(day, time.min, METER_ZONE)
            end = datetime.combine(day + timedelta(days=1), time.min, METER_ZONE)
            writer.writerow([SERVICE_POINT, "netUsage", "NET_USAGE", "KWH",
                             f"{start.isoformat()}/{end.isoformat()}", "ACTUAL", kwh, "", "false", "sa", "sp"])
    return run


def write_manual_bill_pdf(directory, name):
    """A scan candidate only; PDF text is supplied by a mock, never by the OS."""
    path = directory / name
    path.write_bytes(b"%PDF-1.4\n% synthetic placeholder\n%%EOF\n")
    return path


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name).resolve()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)

        # Catch accidental network use even when production catches the exception.
        for name in ("create_connection", "getaddrinfo", "socket.connect", "socket.connect_ex",
                     "socket.sendto", "gethostbyname", "gethostbyname_ex"):
            deny = self.start_patch(patch(f"socket.{name}", side_effect=AssertionError("Network denied in tests")))
            self.addCleanup(deny.assert_not_called)

        self.start_patch(patch.dict(os.environ))
        for key in list(os.environ):
            if key == "EIA_API_KEY" or key.startswith(("EVERSMART_", "EVERSOURCE_")):
                del os.environ[key]
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result


class CookieConvertTests(OfflineTestCase):
    def test_parse_cookieinfo_creates_private_cookie_file(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "cookieinfo.txt"
            out = Path(td) / "cookies.txt"
            src.write_text("ASP.NET_SessionId\tfixture-cookie\tutility.example\t/\tSession\t41 B\t✓\t✓\tLax\n", encoding="utf-8")

            parse_cookieinfo(src, out)

            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_parse_cookieinfo_writes_netscape_cookie(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "cookieinfo.txt"
            out = Path(td) / "cookies.txt"
            src.write_text("ASP.NET_SessionId\tfixture-cookie\tutility.example\t/\tSession\t41 B\t✓\t✓\tLax\n", encoding="utf-8")
            parse_cookieinfo(src, out)
            text = out.read_text()
            self.assertIn("# Netscape HTTP Cookie File", text)
            self.assertIn("utility.example\tFALSE\t/\tTRUE\t0\tASP.NET_SessionId\tfixture-cookie", text)

    def test_parse_cookieinfo_handles_subdomain_and_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "cookieinfo.txt"
            out = Path(td) / "cookies.txt"
            src.write_text("SEGMENT\tfixture-segment\t.utility.example\t/\t6/6/2027, 8:48:58 PM\t11 B\t✓\t\tLax\n", encoding="utf-8")
            parse_cookieinfo(src, out)
            text = out.read_text()
            expiry = int(datetime(2027, 6, 6, 20, 48, 58).timestamp())
            self.assertIn(f".utility.example\tTRUE\t/\tTRUE\t{expiry}\tSEGMENT\tfixture-segment", text)


class EversmartParserTests(OfflineTestCase):
    def make_client(self):
        return EversourceClient(state_dir=self.root)

    def test_private_collection_writers_create_private_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "usage.csv"
            json_path = root / "manifest.json"

            write_csv(csv_path, [{"value": 1}])
            write_json(json_path, {"ok": True})

            self.assertEqual(stat.S_IMODE(csv_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(json_path.stat().st_mode), 0o600)

    def test_private_binary_writer_creates_private_file(self):
        from eversmart import write_bytes
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "green_button.zip"

            write_bytes(path, b"private export")

            self.assertEqual(path.read_bytes(), b"private export")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_optional_warning_file_is_private(self):
        with tempfile.TemporaryDirectory() as td:
            warnings = {}

            write_optional_warning(Path(td), "forecast", RuntimeError("unavailable"), warnings)

            path = Path(td) / "forecast_warning.txt"
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_request_persists_cookie_jar_privately(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self): return b"ok"

        with tempfile.TemporaryDirectory() as td:
            cookie_file = Path(td) / "cookies.txt"
            client = EversourceClient(cookie_file=cookie_file, cookieinfo=Path(td) / "missing")
            client.opener.open = lambda *_args, **_kwargs: Response()

            self.assertEqual(client.request("https://example.invalid"), b"ok")

            self.assertEqual(stat.S_IMODE(cookie_file.stat().st_mode), 0o600)

    def test_load_databrowser_extracts_token_and_entity(self):
        client = self.make_client()
        future_token = fake_token(2_000_003_600)
        html = f"""
        <script>
        window.addEventListener('opower:unauthorized', function (event) {{
          var authorization = {{ accessToken: '{future_token}' }};
        }});
        window.opowerApi.setEntityIds(['{ENTITY_ID}']);
        </script>
        """
        with patch.object(client, "request", return_value=html.encode()), patch("eversmart.time.time", return_value=2_000_000_000):
            client.load_databrowser()
        self.assertEqual(client.access_token, future_token)
        self.assertEqual(client.entity_id, ENTITY_ID)

    def test_load_databrowser_replaces_expired_embedded_token(self):
        client = self.make_client()
        expired_token = fake_token(1)
        html = f"""
        <script>
          var authorization = {{ accessToken: '{expired_token}' }};
          window.opowerApi.setEntityIds(['{ENTITY_ID}']);
        </script>
        """
        client.obtain_fresh_opower_token = lambda force_login=False: setattr(client, "access_token", "fresh-token") or "fresh-token"
        with patch.object(client, "request", return_value=html.encode()):
            client.load_databrowser()
        self.assertEqual(client.access_token, "fresh-token")
        self.assertEqual(client.entity_id, ENTITY_ID)

    def test_load_databrowser_mints_token_when_authenticated_shell_has_no_embedded_token(self):
        client = self.make_client()
        client.obtain_fresh_opower_token = lambda force_login=True: setattr(client, "access_token", "fresh-token") or "fresh-token"
        with patch.object(client, "request", return_value=b"<html><body>authenticated app shell</body></html>"):
            html = client.load_databrowser()
        self.assertIn("authenticated app shell", html)
        self.assertEqual(client.access_token, "fresh-token")
        self.assertIsNone(client.entity_id)

    def test_obtain_fresh_opower_token_uses_okta_session_redirect(self):
        client = self.make_client()
        client.login = lambda force_refresh=False: {"SessionToken": "session-token"}

        class FakeHeaders(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        class FakeOpener:
            def open(self, req, timeout=60):
                self.url = req.full_url
                raise urllib.error.HTTPError(
                    req.full_url, 302, "Found", FakeHeaders({"Location": "https://www.eversource.com/cg/customer/accountoverview#access_token=fresh-token&token_type=Bearer"}), None
                )

        fake = FakeOpener()
        client.okta_opener = fake
        with patch("eversmart.OKTA_OPOWER_CLIENT_ID", "fixture-client"):
            token = client.obtain_fresh_opower_token(force_login=True)
        self.assertEqual(token, "fresh-token")
        self.assertEqual(client.access_token, "fresh-token")
        self.assertFalse((self.root / ".opower_access_token").exists())
        query = parse_qs(urlsplit(fake.url).query)
        self.assertEqual(query["client_id"], ["fixture-client"])
        self.assertEqual(query["sessionToken"], ["session-token"])
        self.assertEqual(query["redirect_uri"], ["https://www.eversource.com/cg/customer/accountoverview"])

    def test_login_retries_dotenv_credentials_when_environment_credentials_are_rejected(self):
        client = self.make_client()
        attempts = []

        (self.root / ".env").write_text("EVERSOURCE_LOGIN=good-user\nEVERSOURCE_PASS=good-pass\n")

        def fake_login_with_credentials(login_url, return_url, web_id, password):
            attempts.append((web_id, password))
            if web_id == "bad-user":
                return {"IsSuccess": False, "Errors": {"InvalidWebIdPassword": "try a password reset"}}
            return {"IsSuccess": True, "SessionToken": "session-token"}

        client._login_with_credentials = fake_login_with_credentials
        with patch.dict(os.environ, {"EVERSOURCE_LOGIN": "bad-user", "EVERSOURCE_PASS": "bad-pass"}):
            result = client.login()
            self.assertEqual(os.environ["EVERSOURCE_LOGIN"], "good-user")
            self.assertEqual(os.environ["EVERSOURCE_PASS"], "good-pass")

        self.assertEqual(result["SessionToken"], "session-token")
        self.assertEqual(attempts, [("bad-user", "bad-pass"), ("good-user", "good-pass")])

    def test_login_does_not_retry_dotenv_for_non_credential_login_failures(self):
        client = self.make_client()
        attempts = []
        with patch.dict("os.environ", {"EVERSOURCE_LOGIN": "env-user", "EVERSOURCE_PASS": "env-pass"}, clear=False):
            with patch("eversmart.read_dotenv", return_value={"EVERSOURCE_LOGIN": "dotenv-user", "EVERSOURCE_PASS": "dotenv-pass"}):
                def fake_login_with_credentials(login_url, return_url, web_id, password):
                    attempts.append((web_id, password))
                    return {"IsSuccess": False, "Errors": {"MfaRequired": "Additional verification required"}}

                client._login_with_credentials = fake_login_with_credentials
                with self.assertRaises(RuntimeError) as ctx:
                    client.login()

        self.assertEqual(attempts, [("env-user", "env-pass")])
        self.assertIn("MfaRequired", str(ctx.exception))

    def test_find_target_picks_requested_service_point_and_register(self):
        client = self.make_client()
        metadata = {
            "data": {"billingAccountByAuthContext": {
                "utilityId": ENTITY_ID,
                "urn": "acct-urn",
                "serviceAgreementsConnection": {"edges": [{"node": {
                    "uuid": "sa-1",
                    "availableBillSegmentsInterval": "bill-start/bill-end",
                    "servicePointsConnection": {"edges": [{"node": {
                        "uuid": "sp-1", "utilityId": SERVICE_POINT,
                        "premise": {"timeZone": "America/New_York", "uuid": "prem-1", "urn": "prem-urn"},
                        "registers": [
                            {"serviceQuantityIdentifier": "DELIVERED", "availableReadsTimeInterval": "x/y", "readResolution": "QUARTER_HOUR"},
                            {"serviceQuantityIdentifier": "NET_USAGE", "availableReadsTimeInterval": "a/b", "readResolution": "QUARTER_HOUR"},
                        ],
                    }}]},
                }}]},
            }}
        }
        target = client.find_target(metadata, SERVICE_POINT)
        self.assertEqual(target.service_agreement_uuid, "sa-1")
        self.assertEqual(target.service_point_uuid, "sp-1")
        self.assertEqual(target.available_interval, "a/b")
        self.assertEqual(target.entity_id, ENTITY_ID)
        self.assertEqual(target.billing_account_urn, "acct-urn")
        self.assertEqual(target.premise_uuid, "prem-1")
        self.assertEqual(target.available_bill_interval, "bill-start/bill-end")

    def test_graphql_retries_transient_502_before_failing(self):
        client = self.make_client()
        client.access_token = "token"
        client.entity_id = "entity"
        attempts = []

        def fake_request(url, body, headers):
            attempts.append(headers.get("Opower-Selected-Entities"))
            if len(attempts) == 1:
                raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b"Bad Gateway"))
            return b'{"data":{"ok":true}}'

        client.request = fake_request
        client._sleep = lambda seconds: None
        self.assertEqual(client.graphql("query { ok }", {}), {"data": {"ok": True}})
        self.assertEqual(len(attempts), 2)

    def test_graphql_refreshes_token_on_http_401_or_403_auth_failures(self):
        client = self.make_client()
        client.access_token = "old-token"
        client.entity_id = "entity"
        calls = []

        def fake_request(url, body, headers):
            calls.append(headers.get("Authorization"))
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"session invalid"))
            return b'{"data":{"ok":true}}'

        def fake_refresh():
            client.access_token = "new-token"

        client.request = fake_request
        client._refresh_opower_session = fake_refresh
        self.assertEqual(client.graphql("query { ok }", {}), {"data": {"ok": True}})
        self.assertEqual(calls, ["Bearer old-token", "Bearer new-token"])

    def test_graphql_refreshes_token_on_graphql_auth_errors(self):
        client = self.make_client()
        client.access_token = "old-token"
        client.entity_id = "entity"
        calls = []

        def fake_request(url, body, headers):
            calls.append(headers.get("Authorization"))
            if len(calls) == 1:
                return b'{"errors":[{"message":"Unauthorized: access token expired"}]}'
            return b'{"data":{"ok":true}}'

        def fake_refresh():
            client.access_token = "new-token"

        client.request = fake_request
        client._refresh_opower_session = fake_refresh
        self.assertEqual(client.graphql("query { ok }", {}), {"data": {"ok": True}})
        self.assertEqual(calls, ["Bearer old-token", "Bearer new-token"])

    def test_graphql_does_not_refresh_token_for_data_fetching_errors(self):
        client = self.make_client()
        client.access_token = "token"
        client.entity_id = "entity"
        refreshed = []
        client.request = lambda url, body, headers: b'{"errors":[{"message":"Provided range must be no more than 720 hours."}]}'
        client._refresh_opower_session = lambda: refreshed.append(True)
        with self.assertRaises(RuntimeError):
            client.graphql("query { ok }", {})
        self.assertEqual(refreshed, [])

    def test_collect_once_does_not_mark_noncritical_rate_plan_502_as_error(self):
        from eversmart import Target
        target = Target(
            account="acct", entity_id="entity", service_agreement_uuid="sa", service_point_uuid="sp",
            available_interval="2026-01-01T00:00:00-05:00/2026-01-02T00:00:00-05:00", resolution="DAY",
            timezone="America/New_York", billing_account_urn="urn", billing_account_uuid="ba",
            service_agreement_urn="saurn", premise_uuid="premise", premise_urn="purn",
            available_bill_interval="2026-01-01T00:00:00-05:00/2026-01-02T00:00:00-05:00",
            rate_plan_code="R1",
        )

        class FakeClient:
            def metadata(self): return {"data": {"billingAccountByAuthContext": {}}}
            def service_points(self, metadata): return [target]
            def usage(self, *args): return {"data": {"billingAccountByAuthContext": {"serviceAgreementsConnection": {"edges": []}}}}
            cost = usage
            pricing = usage
            demand_maxima = usage
            weather = usage
            bills = usage
            def forecast(self, *args): raise RuntimeError("optional forecast failure")
            def rate_static_content(self, *args):
                raise RuntimeError("Opower GraphQL HTTP 502: Bad Gateway")

        with tempfile.TemporaryDirectory() as td:
            manifest = collect_once(FakeClient(), Path(td), "acct", None, None)
        self.assertEqual(manifest["errors"], {})
        self.assertIn("rate_plan", manifest["warnings"])

    def test_collect_once_plans_one_safe_window_for_regular_poll(self):
        from eversmart import Target
        target = Target(
            account="acct", entity_id="entity", service_agreement_uuid="sa", service_point_uuid="sp",
            available_interval="2026-06-03T00:00:00-04:00/2026-07-04T00:00:00-04:00", resolution="QUARTER_HOUR",
            timezone="America/New_York", billing_account_urn="urn", billing_account_uuid="ba",
            service_agreement_urn="saurn", premise_uuid="premise", premise_urn="purn",
            available_bill_interval="2026-06-01T00:00:00-04:00/2026-07-01T00:00:00-04:00",
            rate_plan_code="R1",
        )
        calls = []

        class FakeClient:
            def metadata(self): return {"data": {"billingAccountByAuthContext": {}}}
            def service_points(self, metadata): return [target]
            def usage(self, target, interval, resolution):
                calls.append(interval)
                return {"data": {"billingAccountByAuthContext": {"serviceAgreementsConnection": {"edges": []}}}}
            cost = usage
            pricing = usage
            def demand_maxima(self, *args): return {"data": {"billingAccountByAuthContext": {"serviceAgreementsConnection": {"edges": []}}}}
            weather = demand_maxima
            bills = demand_maxima
            forecast = demand_maxima
            rate_static_content = demand_maxima

        with tempfile.TemporaryDirectory() as td:
            manifest = collect_once(
                FakeClient(), Path(td), "acct", None, None, output=self.root / "dashboard.html",
                renderer=lambda _, path, **options: path.write_text("synthetic dashboard"),
            )
            run_dir = Path(manifest["output_dir"])
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
            (raw_usage,) = run_dir.glob("usage_*.json.gz")
            self.assertEqual(stat.S_IMODE(raw_usage.stat().st_mode), 0o600)
            with gzip.open(raw_usage, "rt", encoding="utf-8") as stream:
                self.assertIn("billingAccountByAuthContext", json.load(stream)["data"])
        (window,) = manifest["collection_windows"]["acct"]
        start, end = map(datetime.fromisoformat, window.split("/"))
        self.assertEqual(end, datetime.fromisoformat("2026-07-04T00:00:00-04:00"))
        self.assertGreater(end, start)
        self.assertLessEqual(end - start, timedelta(hours=720))
        self.assertEqual(calls, [window] * 3)
        self.assertEqual(manifest["collection_interval"], window)

    def test_cap_interval_hours_preserves_safe_or_invalid_intervals(self):
        self.assertEqual(cap_interval_hours("2026-01-01T00:00:00-05:00/2026-01-31T00:00:00-05:00")[1], None)
        self.assertEqual(cap_interval_hours("not-an-interval"), ("not-an-interval", None))

    def test_iter_stream_reads_flattens_stream_values(self):
        doc = {"data": {"billingAccountByAuthContext": {"serviceAgreementsConnection": {"edges": [{"node": {
            "uuid": "sa", "servicePointsConnection": {"edges": [{"node": {
                "uuid": "sp", "utilityId": "acct", "readStreams": {"netUsage": [{
                    "serviceQuantityIdentifier": "NET_USAGE", "unit": "KWH", "reads": [{
                        "readType": "ESTIMATED", "timeInterval": "t0/t1", "measuredAmount": {"value": 1.25}, "isPeakPeriod": None
                    }]
                }]}
            }}]}
        }}]}}}}
        rows = list(iter_stream_reads(doc))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["utility_id"], "acct")
        self.assertEqual(rows[0]["value"], 1.25)
        self.assertEqual(rows[0]["stream"], "netUsage")

    def test_iter_pricing_components_extracts_rates_and_components(self):
        doc = {"data": {"billingAccountByAuthContext": {"serviceAgreementsConnection": {"edges": [{"node": {
            "uuid": "sa", "ratePlan": {"code": "R1"}, "servicePointsConnection": {"edges": [{"node": {
                "uuid": "sp", "utilityId": "acct", "readStreams": {
                    "rates": [{"rateType": "FLAT", "costPerUnit": {"value": 0.33626}, "activationDate": "2026-02-01"}],
                    "netUsage": [{"serviceQuantityIdentifier": "NET_USAGE", "unit": "KWH", "reads": [{
                        "readType": "ACTUAL", "timeInterval": "t0/t1", "measuredAmount": {"value": 2.0}, "monetaryAmount": {"value": 0.67},
                        "ratedReadComponents": [{"costPerUnit": {"value": 0.33626}, "monetaryAmount": {"value": 0.67}, "measuredAmount": {"value": 2.0}, "attributes": [{"key": "source", "value": "unit"}]}]
                    }]}]
                }
            }}]}
        }}]}}}}
        rows = list(iter_pricing_components(doc))
        self.assertEqual(rows[0]["service_point_uuid"], "sp")
        self.assertEqual(rows[0]["rate_type"], "FLAT")
        self.assertEqual(rows[0]["cost_per_unit"], 0.33626)
        self.assertEqual(rows[0]["component_attributes"], "source=unit")

    def test_iter_weather_rows_flattens_daily_weather(self):
        doc = {"data": {"billingAccountByAuthContext": {"premisesConnection": {"edges": [{"node": {
            "uuid": "prem", "weather": [{"timeInterval": "2026-06-03/2026-06-04", "minTemperature": {"value": 42.9}, "meanTemperature": {"value": 65.1}, "maxTemperature": {"value": 82.0}}]
        }}]}}}}
        rows = list(iter_weather_rows(doc))
        self.assertEqual(rows[0]["premise_uuid"], "prem")
        self.assertEqual(rows[0]["mean_temperature"], 65.1)

    def test_iter_bill_segments_flattens_billing_history(self):
        doc = {"data": {"billingAccountByAuthContext": {"bills": [{"urn": "bill-1", "timeInterval": "b0/b1", "segments": [{
            "urn": "seg-1", "estimated": False, "usageInterval": "u0/u1", "usageCharges": {"value": 10.0}, "currentAmount": {"value": 12.0},
            "serviceAgreement": {"uuid": "sa", "serviceType": "ELECTRICITY"},
            "serviceQuantities": [{"serviceQuantityIdentifier": "NET_USAGE", "unit": "KWH", "serviceQuantity": {"value": 30.0}}]
        }]}]}}}
        rows = list(iter_bill_segments(doc))
        self.assertEqual(rows[0]["bill_urn"], "bill-1")
        self.assertEqual(rows[0]["kwh"], 30.0)
        self.assertEqual(rows[0]["effective_price_per_kwh"], 12.0 / 30.0)

    def test_write_optional_warning_separates_noncritical_fetch_failures(self):
        with tempfile.TemporaryDirectory() as td:
            warnings = {}
            details = "synthetic sensitive upstream details"
            write_optional_warning(Path(td), "bill_forecast", RuntimeError(details), warnings)
            self.assertEqual(set(warnings), {"bill_forecast"})
            message = warnings["bill_forecast"]
            self.assertTrue(message)
            self.assertNotIn(details, message)
            self.assertEqual((Path(td) / "bill_forecast_warning.txt").read_text(), message)

    def test_parse_green_button_zip_reads_usage_and_billing_csvs(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("usage.csv", "Name,Test\nService Point,sp-1\n\nTYPE,DATE,START TIME,END TIME,USAGE (kWh),COST,NOTES\nElectric usage,2026-06-03,00:00,00:14,1.25,$0.42,* This data was estimated\n")
            z.writestr("billing.csv", "TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES\nElectric billing,2026-01-01,2026-02-01,30.00,$10.00,\n")
        rows = parse_green_button_zip(buf.getvalue())
        kinds = {r["dataset"] for r in rows}
        self.assertEqual(kinds, {"usage", "billing"})
        self.assertEqual(next(r for r in rows if r["dataset"] == "usage")["service_point"], "sp-1")


class DashboardSummaryTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        backend = self.start_patch(patch("dashboard.subprocess.run", side_effect=AssertionError("Unexpected PDF backend")))
        self.addCleanup(backend.assert_not_called)

    def parse_fixture_bill(self, fixture):
        from dashboard import parse_eversource_bill_pdf

        text = (FIXTURES / f"{fixture}.txt").read_text(encoding="utf-8")
        with patch("dashboard._extract_pdf_text", return_value=text):
            return parse_eversource_bill_pdf(self.root / f"{fixture}.pdf")

    def test_pdf_text_extraction_falls_back_to_native_macos_pdfkit(self):
        from dashboard import _extract_pdf_text

        pdf = write_manual_bill_pdf(self.root, "bill-example.pdf")
        text = (FIXTURES / "meter-change.txt").read_text(encoding="utf-8")
        with patch.dict("sys.modules", {"fitz": None, "pypdf": None}), patch(
            "dashboard.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=text)
        ) as fallback:
            self.assertEqual(_extract_pdf_text(pdf), text)

        fallback.assert_called_once()
        command = fallback.call_args.args[0]
        self.assertEqual(Path(command[0]).name, "swift")
        self.assertIn("import PDFKit", command[2])
        self.assertEqual(Path(command[-1]), pdf)

    def test_bill_cost_modes_exclude_prior_balance_from_calibrated_rates(self):
        from dashboard import _apply_bill_cost_modes
        latest = {"daily": [{"net_kwh": 10.0}], "hourly": [{"net_kwh": 2.0}]}
        bill = {
            "statement_date": "2026-07-10", "total_due": 228.12,
            "total_current_charges": 128.12, "total_billed_kwh": 346.0,
            "customer_charge": 10.0,
        }

        _apply_bill_cost_modes(latest, [bill])

        self.assertAlmostEqual(latest["bill_cost_modes"]["bill_calibrated_variable_rate"], 118.12 / 346.0)
        self.assertAlmostEqual(latest["bill_cost_modes"]["all_in_allocated_rate"], 128.12 / 346.0)

    def test_current_billing_cycle_is_unavailable_without_cycle_evidence(self):
        data_dir = self.root / "data"
        write_usage_run(data_dir, "20240711T100000Z", [(date(2024, 7, 10), 10)])
        latest = build_dashboard_data(data_dir, as_of=datetime(2024, 7, 11, 10, tzinfo=timezone.utc))["latest"]
        self.assertEqual(latest["current_billing_cycle"]["reason"], "unknown_cycle")
        self.assertIsNone(latest["current_billing_cycle"]["projected_cost"])

    def test_current_billing_cycle_uses_statement_rate_and_fixed_charge(self):
        data_dir = self.root / "data"
        manual = self.root / "statements"
        manual.mkdir()
        (manual / "bill-test.json").write_text(json.dumps(self.parse_fixture_bill("all-delivery-charges")))
        write_usage_run(data_dir, "20240912T100000Z", [(date(2024, 9, 10), 10), (date(2024, 9, 11), 20)])

        latest = build_dashboard_data(
            data_dir, manual_bill_dir=manual, as_of=datetime(2024, 9, 12, 10, tzinfo=timezone.utc),
        )["latest"]
        cycle = latest["current_billing_cycle"]
        self.assertEqual(cycle["status"], "available")
        variable_rate = (115.60 - 10.0) / 300.0
        self.assertAlmostEqual(cycle["variable_rate_per_kwh"], variable_rate)
        self.assertAlmostEqual(cycle["bill_calibrated_variable_cost_so_far"], 30.0 * variable_rate)
        self.assertAlmostEqual(cycle["projected_kwh"], 450.0)
        self.assertAlmostEqual(cycle["projected_cost"], 450.0 * variable_rate + 10.0)

    def test_duplicate_intervals_are_counted_once_in_all_dashboard_aggregates(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "fixture-point"}))
            header = "utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n"
            interval = "2026-06-03T00:00/2026-06-03T00:15"
            usage = f"fixture-point,netUsage,NET_USAGE,KWH,{interval},ACTUAL,1.0,,false,sa,sp\n"
            cost = f"fixture-point,netUsage,NET_USAGE,KWH,{interval},ACTUAL,1.0,0.30,false,sa,sp\n"
            (run / "usage.csv").write_text(header + usage + usage)
            (run / "cost.csv").write_text(header + cost + cost)

            latest = build_dashboard_data(Path(td))["latest"]

            self.assertEqual(latest["total_net_kwh"], 1.0)
            self.assertEqual(latest["total_cost"], 0.3)
            self.assertEqual(latest["daily"][0]["net_kwh"], 1.0)
            self.assertEqual(latest["daily"][0]["cost"], 0.3)
            self.assertEqual(latest["hourly"][0]["net_kwh"], 1.0)
            self.assertEqual(len(latest["series"]), 1)

    def test_generated_dashboard_artifacts_are_private(self):
        from dashboard import generate_dashboard
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "data" / "20260607T000000Z"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "fixture-point"}))

            output = root / "dashboard.html"
            generate_dashboard(root / "data", output)

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(list(root.glob("*.js")), [])

    def test_build_dashboard_data_includes_new_recommendation_datasets(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "retrieved_at_utc": "20260607T000000Z", "account": "fixture-point", "available_interval": "2026-06-03/2026-06-06",
                "requested_interval": "2026-06-03/2026-06-06", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
                "service_points": [{"utility_id": "fixture-point", "uuid": "sp"}], "latest_available_end": "2026-06-06T00:00:00-04:00"
            }))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,0.34,false,sa,sp\n")
            (run / "pricing.csv").write_text("utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes\nfixture-point,sp,2026-06-03T00:00/2026-06-03T00:15,netUsage,NET_USAGE,KWH,ACTUAL,1.0,0.34,0.33626,FLAT,,,,\n")
            (run / "weather.csv").write_text("premise_uuid,timeInterval,date,min_temperature,mean_temperature,max_temperature\nprem,2026-06-03/2026-06-04,2026-06-03,42,65,82\n")
            (run / "bills.csv").write_text("bill_urn,segment_urn,timeInterval,usageInterval,service_agreement_uuid,serviceType,estimated,kwh,usageCharges,currentAmount,effective_price_per_kwh\nbill,seg,2026-01-01/2026-02-01,2026-01-01/2026-02-01,sa,ELECTRICITY,False,30,10,12,0.333333\n")
            (run / "demand_maxima.csv").write_text("utility_id,service_point_uuid,timeInterval,value,unit,readType,isMaximum\nfixture-point,sp,2026-06-03T00:00/2026-06-03T00:15,4.0,KW,ACTUAL,True\n")
            (run / "green_button_rows.csv").write_text("dataset,source_file,service_point,type,date,start_time,end_time,usage_kwh,cost,notes\nusage,u.csv,sp,Electric usage,2026-06-03,00:00,00:14,1.0,0.34,\n")
            data = build_dashboard_data(Path(td))
            latest = data["latest"]
            self.assertEqual(latest["price_source"], "rated_components")
            self.assertEqual(latest["weather"][0]["mean_temperature"], 65.0)
            self.assertEqual(latest["bill_history"][0]["kwh"], 30.0)
            self.assertEqual(latest["peak_demand_events"][0]["kw"], 4.0)
            self.assertEqual(latest["service_points"][0]["utility_id"], "fixture-point")
            self.assertEqual(latest["green_button"]["row_count"], 1)

    def test_dashboard_aggregates_historical_price_and_bills_across_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            for stamp, bill_start, price, kwh, cost in [
                ("20260607T000000Z", "2026-01-01/2026-02-01", 0.31, 10, 3.10),
                ("20260608T000000Z", "2026-02-01/2026-03-01", 0.33, 20, 6.60),
            ]:
                run = Path(td) / stamp
                run.mkdir()
                (run / "manifest.json").write_text(json.dumps({
                    "retrieved_at_utc": stamp, "account": "fixture-point", "available_interval": "2026-06-03/2026-06-06",
                    "requested_interval": "2026-06-03/2026-06-06", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
                    "service_points": [{"utility_id": "fixture-point", "uuid": "sp"}], "latest_available_end": "2026-06-06T00:00:00-04:00"
                }))
                (run / "usage.csv").write_text(f"utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,{stamp[:8]}T00:00/{stamp[:8]}T00:15,ACTUAL,{kwh},,false,sa,sp\n")
                (run / "cost.csv").write_text(f"utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,{stamp[:8]}T00:00/{stamp[:8]}T00:15,ACTUAL,{kwh},{cost},false,sa,sp\n")
                (run / "pricing.csv").write_text(f"utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes\nfixture-point,sp,{stamp[:8]}T00:00/{stamp[:8]}T00:15,netUsage,NET_USAGE,KWH,ACTUAL,{kwh},{cost},{price},FLAT,,,,\n")
                (run / "bills.csv").write_text(f"bill_urn,segment_urn,timeInterval,usageInterval,service_agreement_uuid,serviceType,estimated,kwh,usageCharges,currentAmount,effective_price_per_kwh\nbill-{stamp},seg-{stamp},{bill_start},{bill_start},sa,ELECTRICITY,False,{kwh},{cost},{cost+1},{cost/kwh}\n")
                (run / "weather.csv").write_text("premise_uuid,timeInterval,date,min_temperature,mean_temperature,max_temperature\nprem,2026-06-03/2026-06-04,2026-06-03,50,70,90\n")
            data = build_dashboard_data(Path(td))
            self.assertEqual([p["price_per_kwh"] for p in data["historical_price_series"]], [0.31, 0.33])
            self.assertEqual([b["timeInterval"] for b in data["bill_trend"]], ["2026-01-01/2026-02-01", "2026-02-01/2026-03-01"])

    def test_latest_series_has_temperature_overlay_and_toggleable_filter_model(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "fixture-point", "resolution": "QUARTER_HOUR", "timezone": "America/New_York"}))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,,false,sa,sp\nfixture-point,demand,DELIVERED,KW,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,2.5,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,0.30,false,sa,sp\n")
            (run / "weather.csv").write_text("premise_uuid,timeInterval,date,min_temperature,mean_temperature,max_temperature\nprem,2026-06-03/2026-06-04,2026-06-03,55,72,88\n")
            data = build_dashboard_data(Path(td))
            latest = data["latest"]
            self.assertEqual(latest["series"][0]["mean_temperature"], 72.0)
            self.assertEqual({d["key"] for d in latest["quarter_hour_defs"]}, {"kwh", "cost", "kw", "mean_temperature"})
            self.assertEqual(data["demand_event_series"][0]["kw"], 2.5)

    def test_bill_trend_has_human_period_labels_and_service_point_aggregates(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "retrieved_at_utc": "20260607T000000Z", "account": "fixture-point-a", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
                "service_points": [{"utility_id": "fixture-point-a", "uuid": "sp-a"}, {"utility_id": "fixture-point-b", "uuid": "sp-b"}],
            }))
            rows = [
                "fixture-point-a,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,1.0,,false,sa,sp-a",
                "fixture-point-a,netUsage,NET_USAGE,KWH,2026-06-02T00:00/2026-06-02T00:15,ACTUAL,2.0,,false,sa,sp-a",
                "fixture-point-b,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,4.0,,false,sa,sp-b",
            ]
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n" + "\n".join(rows) + "\n")
            costs = [
                "fixture-point-a,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,1.0,0.30,false,sa,sp-a",
                "fixture-point-a,netUsage,NET_USAGE,KWH,2026-06-02T00:00/2026-06-02T00:15,ACTUAL,2.0,0.60,false,sa,sp-a",
                "fixture-point-b,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,4.0,1.20,false,sa,sp-b",
            ]
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n" + "\n".join(costs) + "\n")
            (run / "bills.csv").write_text("bill_urn,segment_urn,timeInterval,usageInterval,service_agreement_uuid,serviceType,estimated,kwh,usageCharges,currentAmount,effective_price_per_kwh\nbill,seg,2025-06-10T04:00:00Z/2025-07-11T04:00:00Z,2025-06-10T04:00:00Z/2025-07-11T04:00:00Z,sa,ELECTRICITY,False,167,57.6,67.6,0.345\n")
            data = build_dashboard_data(Path(td))
            latest = data["latest"]
            self.assertEqual(data["bill_trend"][0]["period_label"], "Jun 10 – Jul 11, 2025")
            self.assertEqual(data["bill_trend"][0]["billing_days"], 31)
            self.assertEqual(data["bill_trend"][0]["effective_price_per_kwh"], 67.6 / 167)
            summaries = {s["utility_id"]: s for s in latest["service_point_summaries"]}
            self.assertEqual(summaries["fixture-point-a"]["total_kwh"], 3.0)
            self.assertEqual(summaries["fixture-point-a"]["days"]["2026-06-02"]["kwh"], 2.0)
            self.assertAlmostEqual(summaries["fixture-point-a"]["weeks"]["2026-W23"]["cost"], 0.9)
            self.assertEqual(summaries["fixture-point-b"]["total_cost"], 1.2)

    def test_dashboard_estimates_unrated_cost_from_canonical_price(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "retrieved_at_utc": "20260607T000000Z", "account": "fixture-point", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
            }))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-07T00:00/2026-06-07T00:15,ACTUAL,2.0,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-07T00:00/2026-06-07T00:15,ACTUAL,2.0,,false,sa,sp\n")
            (run / "pricing.csv").write_text("utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes\nfixture-point,sp,2026-06-07T00:00/2026-06-07T00:15,netUsage,NET_USAGE,KWH,ACTUAL,2.0,,0.33626,FLAT,,,,\n")
            data = build_dashboard_data(Path(td))
            latest = data["latest"]
            self.assertAlmostEqual(latest["daily"][0]["cost"], 0.67252)
            self.assertAlmostEqual(latest["total_cost"], 0.67252)
            self.assertEqual(latest["series"][0]["cost_source"], "estimated_from_rate")
            self.assertAlmostEqual(latest["effective_price_per_kwh"], 0.33626)

    def test_dashboard_js_treats_null_series_values_as_missing_not_zero(self):
        html = __import__("dashboard").HTML_TEMPLATE
        self.assertIn("function finiteOrNull", html)
        self.assertIn("raw==null||raw===''", html)

    def test_dashboard_treats_bill_forecast_failure_as_optional_warning(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "retrieved_at_utc": "20260607T000000Z",
                "account": "fixture-point",
                "usage_rows": 1,
                "errors": {"bill_forecast": "DataFetchingException"},
            }))
            data = build_dashboard_data(Path(td))
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["warnings"][0]["warnings"], {"bill_forecast": "DataFetchingException"})
            self.assertIsNone(data["latest"]["error"])

    def test_advanced_analytics_without_stale_projection(self):
        with tempfile.TemporaryDirectory() as td:
            prev = Path(td) / "20260607T000000Z"
            latest_run = Path(td) / "20260608T000000Z"
            prev.mkdir(); latest_run.mkdir()
            manifest = {"account": "fixture-point", "resolution": "QUARTER_HOUR", "timezone": "America/New_York", "retrieved_at_utc": "STAMP"}
            for run, stamp, rows in [
                (prev, "20260607T000000Z", [
                    ("2026-06-01T00:00/2026-06-01T00:15", 0.2, 0.067252, 0.8),
                ]),
                (latest_run, "20260608T000000Z", [
                    ("2026-06-01T00:00/2026-06-01T00:15", 0.2, 0.067252, 0.8),
                    ("2026-06-01T01:00/2026-06-01T01:15", 0.2, 0.067252, 0.7),
                    ("2026-06-02T18:00/2026-06-02T18:15", 5.0, 1.6813, 6.0),
                ]),
            ]:
                m = dict(manifest, retrieved_at_utc=stamp)
                (run / "manifest.json").write_text(json.dumps(m))
                usage_lines = ["utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid"]
                cost_lines = ["utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid"]
                pricing_lines = ["utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes"]
                for interval, kwh, cost, kw in rows:
                    usage_lines.append(f"fixture-point,netUsage,NET_USAGE,KWH,{interval},ACTUAL,{kwh},,false,sa,sp")
                    usage_lines.append(f"fixture-point,demand,DELIVERED,KW,{interval},ACTUAL,{kw},,false,sa,sp")
                    cost_lines.append(f"fixture-point,netUsage,NET_USAGE,KWH,{interval},ACTUAL,{kwh},{cost},false,sa,sp")
                    pricing_lines.append(f"fixture-point,sp,{interval},netUsage,NET_USAGE,KWH,ACTUAL,{kwh},{cost},0.33626,FLAT,,,,")
                (run / "usage.csv").write_text("\n".join(usage_lines) + "\n")
                (run / "cost.csv").write_text("\n".join(cost_lines) + "\n")
                (run / "pricing.csv").write_text("\n".join(pricing_lines) + "\n")
                (run / "weather.csv").write_text("premise_uuid,timeInterval,date,min_temperature,mean_temperature,max_temperature\nprem,2026-06-01/2026-06-02,2026-06-01,60,70,80\nprem,2026-06-02/2026-06-03,2026-06-02,70,85,95\n")
                (run / "bills.csv").write_text("bill_urn,segment_urn,timeInterval,usageInterval,service_agreement_uuid,serviceType,estimated,kwh,usageCharges,currentAmount,effective_price_per_kwh\nbill,seg,2026-05-01/2026-06-01,2026-05-01/2026-06-01,sa,ELECTRICITY,False,300,100,110,0.366\n")
            data = build_dashboard_data(Path(td))
            latest = data["latest"]
            self.assertIsNone((latest.get("bill_projection") or {}).get("projected_cost"))
            self.assertEqual(data["run_diff"]["new_usage_rows"], latest["usage_rows"] - data["runs"][-2]["usage_rows"])
            self.assertIn(18, {r["hour_of_day"] for r in data["hourly_profile"]})
            self.assertIsNotNone(latest["weather_sensitivity"]["correlation"])
            self.assertTrue(latest["anomalies"]["intervals"])
            self.assertGreater(latest["baseload"]["estimated_kw"], 0)
            self.assertGreater(latest["savings_simulator"]["reduce_daily_10pct"]["monthly_savings"], 0)
            html = __import__("dashboard").HTML_TEMPLATE
            for token in ("runDiff", "hourlyProfileChart", "weatherSensitivityChart", "anomalyTable", "baseloadCard", "savingsSimulator"):
                self.assertIn(token, html)

    def test_usage_chart_has_matching_daily_hourly_granularity_controls(self):
        html = __import__("dashboard").HTML_TEMPLATE
        self.assertIn("usageGranularity", html)
        self.assertIn("dailyUsageRows", html)
        self.assertIn("hourlyUsageRows", html)
        self.assertIn("data-usage-granularity", html)

    def test_hourly_cost_series_and_cost_chart_granularity_controls(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "fixture-point", "resolution": "QUARTER_HOUR", "timezone": "America/New_York"}))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,,false,sa,sp\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:15/2026-06-03T00:30,ACTUAL,2.0,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,0.30,false,sa,sp\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:15/2026-06-03T00:30,ACTUAL,2.0,0.60,false,sa,sp\n")
            data = build_dashboard_data(Path(td))
            self.assertEqual(data["latest"]["hourly"][0]["hour"], "2026-06-03T00:00:00-04:00")
            self.assertAlmostEqual(data["latest"]["hourly"][0]["cost"], 0.9)
            html = __import__("dashboard").HTML_TEMPLATE
            self.assertIn("costGranularity", html)
            self.assertIn("dailyCostRows", html)
            self.assertIn("hourlyCostRows", html)

    def test_peak_demand_events_include_duration_and_estimated_spike_cost(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "fixture-point", "resolution": "QUARTER_HOUR", "timezone": "America/New_York"}))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T18:00/2026-06-03T18:15,ACTUAL,0.4,,false,sa,sp\nfixture-point,demand,DELIVERED,KW,2026-06-03T18:00/2026-06-03T18:15,ACTUAL,4.0,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T18:00/2026-06-03T18:15,ACTUAL,0.4,0.134504,false,sa,sp\n")
            (run / "pricing.csv").write_text("utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes\nfixture-point,sp,2026-06-03T18:00/2026-06-03T18:15,netUsage,NET_USAGE,KWH,ACTUAL,0.4,0.134504,0.33626,FLAT,,,,\n")
            data = build_dashboard_data(Path(td))
            event = data["latest"]["peak_demand_events"][0]
            self.assertEqual(event["duration_minutes"], 15)
            self.assertEqual(event["duration_label"], "15 min")
            self.assertAlmostEqual(event["estimated_kwh_at_peak"], 1.0)
            self.assertAlmostEqual(event["estimated_cost_at_peak"], 0.33626)
            html = __import__("dashboard").HTML_TEMPLATE
            self.assertIn("duration_label", html)
            self.assertIn("estimated_cost_at_peak", html)

    def test_price_chart_keeps_canonical_series_separate_with_optional_benchmark_overlays(self):
        html = __import__("dashboard").HTML_TEMPLATE
        self.assertIn("priceFilters", html)
        self.assertIn("priceOverlayDefs", html)
        self.assertIn("default:false", html)
        self.assertIn("buildPriceSeries", html)
        self.assertNotIn("historical_price_series.filter(x=>x.price_per_kwh!=null).concat(eia)", html)

    def test_eia_benchmark_fetch_falls_back_to_seed_data_when_api_unavailable(self):
        from dashboard import _fetch_eia_benchmark_prices

        _fetch_eia_benchmark_prices.cache_clear()
        self.addCleanup(_fetch_eia_benchmark_prices.cache_clear)
        with patch("dashboard.urllib.request.urlopen", side_effect=OSError("offline")) as urlopen:
            rows = _fetch_eia_benchmark_prices("MA")
        urlopen.assert_called()
        self.assertGreaterEqual(len(rows), 2)
        self.assertIn("regional", {r["scope"] for r in rows})
        self.assertIn("national", {r["scope"] for r in rows})

    def test_dashboard_template_uses_global_tooltips_time_aware_x_labels_and_peak_heatmap(self):
        html = __import__("dashboard").HTML_TEMPLATE
        self.assertIn("data-tip-id", html)
        self.assertIn("showRunTip", html)
        self.assertIn("xLabelFor(row,opt", html)
        self.assertIn("renderDemandHeatmap", html)

    def test_eia_benchmark_parser_converts_cents_to_dollars(self):
        from dashboard import _parse_eia_price_rows
        payload = {"response": {"data": [
            {"period": "2026-03", "stateid": "MA", "stateDescription": "Massachusetts", "price": "30.21"},
            {"period": "2026-03", "stateid": "US", "stateDescription": "United States", "price": "17.47"},
        ]}}
        rows = _parse_eia_price_rows(payload, "regional")
        self.assertEqual(rows[0]["period"], "2026-03")
        self.assertEqual(rows[0]["scope"], "regional")
        self.assertAlmostEqual(rows[0]["price_per_kwh"], 0.3021)

    def test_dashboard_canvas_charts_render_x_axis_labels(self):
        from dashboard import generate_dashboard

        data_dir = self.root / "data"
        write_usage_run(data_dir, "20240604T100000Z", [(date(2024, 6, 3), 12)])
        html = generate_dashboard(data_dir, self.root / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("drawXAxisLabels", html)
        self.assertIn("ctx.fillText(label", html)
        self.assertIn("xLabelFor", html)

    def test_temperature_overlay_breaks_lines_across_missing_values(self):
        from dashboard import generate_dashboard
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "fixture-point"}))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,,false,sa,sp\nfixture-point,netUsage,NET_USAGE,KWH,2026-06-06T00:00/2026-06-06T00:15,ACTUAL,1.0,,false,sa,sp\n")
            out = Path(td) / "dashboard.html"
            generate_dashboard(Path(td), out)
            self.assertIn("started=false;return", out.read_text(encoding="utf-8"))

    def test_physical_bill_parser_separates_meter_periods_and_prior_balance(self):
        bill = self.parse_fixture_bill("meter-change")
        self.assertEqual(bill["statement_date"], "2024-06-11")
        self.assertEqual(bill["due_date"], "2024-07-05")
        self.assertEqual(bill["total_due"], 125.0)
        self.assertEqual(bill["total_current_charges"], 100.0)
        self.assertEqual(bill["total_billed_kwh"], 300.0)
        self.assertEqual(bill["old_meter_kwh"], 204.0)
        self.assertEqual(bill["new_meter_kwh"], 96.0)
        self.assertEqual(bill["meter_change_date"], "2024-06-02")
        self.assertAlmostEqual(bill["supply_rate_per_kwh"], 0.15)
        self.assertAlmostEqual(bill["customer_charge"], 10.00)
        self.assertAlmostEqual(bill["variable_rate_per_kwh_excluding_customer_charge"], 0.30)
        self.assertEqual({li["description"]: li["amount"] for li in bill["line_items"]}, {
            "Generation Service Charge": 45.0, "Distribution Charge": 45.0, "Customer Charge": 10.0,
        })
        self.assertEqual([(p["kind"], p["start_date"], p["end_date"]) for p in bill["service_periods"]], [
            ("old_meter", "2024-05-10", "2024-06-02"), ("new_meter", "2024-06-03", "2024-06-10"),
        ])

    def test_regular_smart_meter_bill_is_not_mislabeled_as_old_meter_usage(self):
        bill = self.parse_fixture_bill("smart-meter")
        self.assertEqual(bill["statement_date"], "2024-07-10")
        self.assertEqual(bill["due_date"], "2024-08-05")
        self.assertEqual(bill["billing_period_start"], "2024-06-11")
        self.assertEqual(bill["billing_period_end"], "2024-07-09")
        self.assertEqual(bill["total_billed_kwh"], 320.0)
        self.assertIsNone(bill["old_meter_kwh"])
        self.assertEqual(bill["new_meter_kwh"], 320.0)
        self.assertEqual(bill["service_periods"][0]["kind"], "smart_meter")
        self.assertAlmostEqual(sum(item["amount"] for item in bill["line_items"]), bill["total_current_charges"], places=2)

    def test_pdfkit_text_layout_accounts_for_every_delivery_line_item(self):
        bill = self.parse_fixture_bill("all-delivery-charges")
        self.assertEqual(bill["statement_date"], "2024-09-10")
        self.assertEqual(bill["total_billed_kwh"], 300.0)
        self.assertAlmostEqual(bill["customer_charge"], 10.00)
        self.assertEqual({li["description"]: li["amount"] for li in bill["line_items"]}, {
            "Generation Service Charge": 45.0, "Customer Charge": 10.0,
            "Distribution Charge": 30.0, "Transition Charge": -3.0,
            "Transmission Charge": 18.0, "Net Meter Recovery Surcharge": 1.20,
            "Revenue Decoupling Charge": -0.60, "Distributed Solar Charge": 1.50,
            "Renewable Energy Charge": 2.40, "Energy Efficiency Charge": 9.00,
            "Electric Vehicle Program": 0.60, "Provisional Syst Plan Factor": 1.50,
        })
        self.assertAlmostEqual(sum(item["amount"] for item in bill["line_items"]), bill["total_current_charges"], places=2)

    def test_manual_bill_discovery_accepts_eversource_names_and_deduplicates_same_statement(self):
        manual = self.root / "statements"
        manual.mkdir()
        files = {
            "bill-example.pdf": "meter-change",
            "eversource-second.pdf": "smart-meter",
            "eversource-duplicate.pdf": "smart-meter",
            "eversource-third.pdf": "all-delivery-charges",
        }
        for name in files:
            write_manual_bill_pdf(manual, name)

        def extracted(path):
            return (FIXTURES / f"{files[path.name]}.txt").read_text(encoding="utf-8")

        with patch("dashboard._extract_pdf_text", side_effect=extracted) as extract:
            bills = build_dashboard_data(self.root / "data", manual_bill_dir=manual)["manual_bills"]
        self.assertEqual(extract.call_count, len(files))
        valid = [b for b in bills if not b.get("error")]
        self.assertEqual(len(bills), 3)
        self.assertEqual([b["statement_date"] for b in valid], ["2024-06-11", "2024-07-10", "2024-09-10"])
        self.assertEqual(valid[0]["billing_period_start"], "2024-05-10")
        self.assertEqual(valid[0]["billing_period_end"], "2024-06-10")
        self.assertEqual(valid[-1]["source"], "eversource-third.pdf")

    def test_bill_reconciliation_uses_deduplicated_history_across_capped_runs(self):
        data_dir = self.root / "data"
        manual = self.root / "statements"
        manual.mkdir()
        write_manual_bill_pdf(manual, "bill-example.pdf")
        first = date(2024, 8, 11)
        days = [(first + timedelta(days=i), 10) for i in range(30)]
        write_usage_run(data_dir, "20240901T100000Z", days[:20])
        write_usage_run(data_dir, "20240910T100000Z", days[10:])
        text = (FIXTURES / "all-delivery-charges.txt").read_text(encoding="utf-8")
        with patch("dashboard._extract_pdf_text", return_value=text):
            data = build_dashboard_data(data_dir, manual_bill_dir=manual)
        rec = data["latest"]["bill_reconciliation"]
        self.assertEqual(rec["statement_date"], "2024-09-10")
        self.assertEqual(rec["coverage_status"], "full_overlap")
        self.assertEqual(rec["overlap_start_date"], "2024-08-11")
        self.assertEqual(rec["overlap_end_date"], "2024-09-09")
        self.assertEqual(rec["overlap_days"], 30)
        self.assertEqual(rec["smart_meter_overlap_kwh"], 300.0)
        self.assertEqual(rec["difference_kwh"], 0.0)

    def test_dashboard_payload_compacts_historical_run_detail(self):
        data_dir = self.root / "data"
        write_usage_run(data_dir, "20240901T100000Z", [(date(2024, 8, 31), 10)])
        write_usage_run(data_dir, "20240902T100000Z", [(date(2024, 9, 1), 12)])
        data = build_dashboard_data(data_dir)
        self.assertTrue(data["latest"]["series"])
        self.assertNotIn("series", data["runs"][-2])
        self.assertLess(len(json.dumps(data, separators=(",", ":"))), 10_000_000)

    def test_dashboard_loads_and_redacts_explicit_manual_bills_with_relative_data_dir(self):
        data_dir = Path("data")
        manual = Path("statements")
        manual.mkdir()
        write_manual_bill_pdf(manual, "bill-example.pdf")
        write_usage_run(data_dir, "20240612T100000Z", [(date(2024, 6, 3), 12)])
        text = (FIXTURES / "meter-change.txt").read_text(encoding="utf-8")
        with patch("dashboard._extract_pdf_text", return_value=text):
            data = build_dashboard_data(data_dir, manual_bill_dir=manual)
        (bill,) = data["manual_bills"]
        self.assertEqual(bill["source"], "bill-example.pdf")
        self.assertTrue(bill["pii_redacted"])
        self.assertNotIn("meter_rows", bill["service_periods"][0])

    def test_manual_bill_json_supersedes_matching_pdf_without_extracting_text(self):
        data_dir = self.root / "data"
        manual = self.root / "statements"
        manual.mkdir()
        write_manual_bill_pdf(manual, "bill-test.pdf")
        (manual / "bill-test.json").write_text(json.dumps({
            "source": "bill-test.pdf",
            "statement_date": "2024-06-04",
            "total_billed_kwh": 12,
            "total_current_charges": 14,
            "service_periods": [{"start_date": "2024-06-03", "end_date": "2024-06-03", "usage_kwh": 12, "kind": "smart_meter"}],
        }))
        write_usage_run(data_dir, "20240604T100000Z", [(date(2024, 6, 3), 12)])
        with patch("dashboard._extract_pdf_text", side_effect=RuntimeError("missing pdf backend")) as extract:
            data = build_dashboard_data(data_dir, manual_bill_dir=manual)
        extract.assert_not_called()  # A matching JSON file avoids the optional backend entirely.
        (bill,) = data["manual_bills"]
        self.assertNotIn("error", bill)
        self.assertEqual(bill["source"], "bill-test.pdf")
        self.assertEqual(data["latest"]["bill_reconciliation"]["status"], "matched")

    def test_dashboard_reconciles_manual_bill_new_meter_overlap_and_cost_modes(self):
        data_dir = self.root / "data"
        manual = self.root / "statements"
        manual.mkdir()
        (manual / "bill-test.json").write_text(json.dumps(self.parse_fixture_bill("meter-change")))
        write_usage_run(data_dir, "20240613T100000Z", [(date(2024, 6, n), 12) for n in range(3, 13)])

        latest = build_dashboard_data(
            data_dir, manual_bill_dir=manual, as_of=datetime(2024, 6, 13, 10, tzinfo=timezone.utc),
        )["latest"]
        rec = latest["bill_reconciliation"]
        self.assertEqual(rec["status"], "matched")
        self.assertAlmostEqual(rec["smart_meter_overlap_kwh"], 96.0)
        self.assertAlmostEqual(rec["new_meter_bill_kwh"], 96.0)
        self.assertAlmostEqual(rec["difference_kwh"], 0.0)
        self.assertEqual(latest["current_billing_cycle"]["kwh_so_far"], 24)
        self.assertAlmostEqual(latest["daily"][0]["bill_calibrated_variable_cost"], 12 * 0.3)
        self.assertAlmostEqual(latest["daily"][0]["all_in_allocated_cost"], 12 * 100 / 300)

    def test_dashboard_template_has_bill_reconciliation_meter_change_and_private_exports(self):
        html = __import__("dashboard").HTML_TEMPLATE
        for token in (
            "Bill reconciliation", "Physical bill", "Current billing cycle", "Meter-change awareness",
            "downloadJson", "downloadCsv", "Private dashboard JSON", "private downloads · do not share",
            "separate shareable render", "billCalibratedVariableCost", "allInAllocatedCost",
            "lineItemBreakdown", "costSourceLegend",
            "billing_period_start", "Physical bill history", "aria-live"
        ):
            self.assertTrue(token in html, f"Missing dashboard section or control: {token}")

    def test_generate_dashboard_defaults_to_embedded_data_and_supports_explicit_split(self):
        from dashboard import generate_dashboard
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            run = data_dir / "20260607T000000Z"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "fixture-point"}))
            out = root / "dashboard.html"
            generate_dashboard(data_dir, out)
            html = out.read_text(encoding="utf-8")
            self.assertNotIn("<script src=", html)
            self.assertNotIn("__DASHBOARD_JSON__", html)
            self.assertIn("DASHBOARD_DATA", html)
            self.assertEqual(list(root.glob("*.js")), [])

            split = root / "split.html"
            generate_dashboard(data_dir, split, split_data=True)
            (script,) = re.findall(r'<script src="([^"/]+\.js)"', split.read_text(encoding="utf-8"))
            data_js = root / script
            self.assertEqual(stat.S_IMODE(data_js.stat().st_mode), 0o600)
            self.assertIn("DASHBOARD_DATA", data_js.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
