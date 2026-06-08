import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from cookie_convert import parse_cookieinfo
from dashboard import build_dashboard_data
from eversmart import (
    EversourceClient,
    iter_bill_segments,
    iter_pricing_components,
    iter_stream_reads,
    iter_weather_rows,
    parse_green_button_zip,
    write_optional_warning,
)


class CookieConvertTests(unittest.TestCase):
    def test_parse_cookieinfo_writes_netscape_cookie(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "cookieinfo.txt"
            out = Path(td) / "cookies.txt"
            src.write_text("ASP.NET_SessionId\tabc123\twww.eversource.com\t/\tSession\t41 B\t✓\t✓\tLax\n")
            parse_cookieinfo(src, out)
            text = out.read_text()
            self.assertIn("# Netscape HTTP Cookie File", text)
            self.assertIn("www.eversource.com\tFALSE\t/\tTRUE\t0\tASP.NET_SessionId\tabc123", text)

    def test_parse_cookieinfo_handles_subdomain_and_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "cookieinfo.txt"
            out = Path(td) / "cookies.txt"
            src.write_text(".SEGMENT\tema\t.eversource.com\t/\t6/6/2027, 8:48:58 PM\t11 B\t✓\t\tLax\n")
            parse_cookieinfo(src, out)
            text = out.read_text()
            self.assertIn(".eversource.com\tTRUE\t/\tTRUE\t", text)
            self.assertIn("\t.SEGMENT\tema", text)


class EversmartParserTests(unittest.TestCase):
    def test_load_databrowser_extracts_token_and_entity(self):
        client = EversourceClient(cookie_file=Path("/tmp/no-such-cookie-file"), cookieinfo=Path("/tmp/no-such-cookieinfo"))
        future_token = "eyJhbGciOiJub25lIn0.eyJleHAiOjk5OTk5OTk5OTl9."
        html = f"""
        <script>
        window.addEventListener('opower:unauthorized', function (event) {{
          var authorization = {{ accessToken: '{future_token}' }};
        }});
        window.opowerApi.setEntityIds(['ENTITY-PRIMARY']);
        </script>
        """
        with patch.object(client, "request", return_value=html.encode()):
            client.load_databrowser()
        self.assertEqual(client.access_token, future_token)
        self.assertEqual(client.entity_id, "ENTITY-PRIMARY")

    def test_load_databrowser_replaces_expired_embedded_token(self):
        client = EversourceClient(cookie_file=Path("/tmp/no-such-cookie-file"), cookieinfo=Path("/tmp/no-such-cookieinfo"))
        expired_token = "eyJhbGciOiJub25lIn0.eyJleHAiOjF9."
        html = f"""
        <script>
          var authorization = {{ accessToken: '{expired_token}' }};
          window.opowerApi.setEntityIds(['ENTITY-PRIMARY']);
        </script>
        """
        client.obtain_fresh_opower_token = lambda force_login=False: setattr(client, "access_token", "fresh-token") or "fresh-token"
        with patch.object(client, "request", return_value=html.encode()):
            client.load_databrowser()
        self.assertEqual(client.access_token, "fresh-token")
        self.assertEqual(client.entity_id, "ENTITY-PRIMARY")

    def test_obtain_fresh_opower_token_uses_okta_session_redirect(self):
        client = EversourceClient(cookie_file=Path("/tmp/no-such-cookie-file"), cookieinfo=Path("/tmp/no-such-cookieinfo"))
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
        token = client.obtain_fresh_opower_token(force_login=True)
        self.assertEqual(token, "fresh-token")
        self.assertEqual(client.access_token, "fresh-token")
        self.assertIn("client_id=0oail6c2lbhNdffx71t7", fake.url)
        self.assertIn("redirect_uri=https%3A%2F%2Fwww.eversource.com%2Fcg%2Fcustomer%2Faccountoverview", fake.url)

    def test_find_target_picks_requested_service_point_and_register(self):
        client = EversourceClient(cookie_file=Path("/tmp/no-such-cookie-file"), cookieinfo=Path("/tmp/no-such-cookieinfo"))
        metadata = {
            "data": {"billingAccountByAuthContext": {
                "utilityId": "ENTITY-PRIMARY",
                "urn": "acct-urn",
                "serviceAgreementsConnection": {"edges": [{"node": {
                    "uuid": "sa-1",
                    "availableBillSegmentsInterval": "bill-start/bill-end",
                    "servicePointsConnection": {"edges": [{"node": {
                        "uuid": "sp-1", "utilityId": "ACCOUNT-PRIMARY",
                        "premise": {"timeZone": "America/New_York", "uuid": "prem-1", "urn": "prem-urn"},
                        "registers": [
                            {"serviceQuantityIdentifier": "DELIVERED", "availableReadsTimeInterval": "x/y", "readResolution": "QUARTER_HOUR"},
                            {"serviceQuantityIdentifier": "NET_USAGE", "availableReadsTimeInterval": "a/b", "readResolution": "QUARTER_HOUR"},
                        ],
                    }}]},
                }}]},
            }}
        }
        target = client.find_target(metadata, "ACCOUNT-PRIMARY")
        self.assertEqual(target.service_agreement_uuid, "sa-1")
        self.assertEqual(target.service_point_uuid, "sp-1")
        self.assertEqual(target.available_interval, "a/b")
        self.assertEqual(target.entity_id, "ENTITY-PRIMARY")
        self.assertEqual(target.billing_account_urn, "acct-urn")
        self.assertEqual(target.premise_uuid, "prem-1")
        self.assertEqual(target.available_bill_interval, "bill-start/bill-end")

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
            write_optional_warning(Path(td), "bill_forecast", RuntimeError("forecast unavailable"), warnings)
            self.assertEqual(warnings, {"bill_forecast": "forecast unavailable"})
            self.assertEqual((Path(td) / "bill_forecast_warning.txt").read_text(), "forecast unavailable")

    def test_parse_green_button_zip_reads_usage_and_billing_csvs(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("usage.csv", "Name,Test\nService Point,sp-1\n\nTYPE,DATE,START TIME,END TIME,USAGE (kWh),COST,NOTES\nElectric usage,2026-06-03,00:00,00:14,1.25,$0.42,* This data was estimated\n")
            z.writestr("billing.csv", "TYPE,START DATE,END DATE,USAGE (kWh),COST,NOTES\nElectric billing,2026-01-01,2026-02-01,30.00,$10.00,\n")
        rows = parse_green_button_zip(buf.getvalue())
        kinds = {r["dataset"] for r in rows}
        self.assertEqual(kinds, {"usage", "billing"})
        self.assertEqual(next(r for r in rows if r["dataset"] == "usage")["service_point"], "sp-1")


class DashboardSummaryTests(unittest.TestCase):
    def test_build_dashboard_data_includes_new_recommendation_datasets(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "retrieved_at_utc": "20260607T000000Z", "account": "006", "available_interval": "2026-06-03/2026-06-06",
                "requested_interval": "2026-06-03/2026-06-06", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
                "service_points": [{"utility_id": "006", "uuid": "sp"}], "latest_available_end": "2026-06-06T00:00:00-04:00"
            }))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,0.34,false,sa,sp\n")
            (run / "pricing.csv").write_text("utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes\n006,sp,2026-06-03T00:00/2026-06-03T00:15,netUsage,NET_USAGE,KWH,ACTUAL,1.0,0.34,0.33626,FLAT,,,,\n")
            (run / "weather.csv").write_text("premise_uuid,timeInterval,date,min_temperature,mean_temperature,max_temperature\nprem,2026-06-03/2026-06-04,2026-06-03,42,65,82\n")
            (run / "bills.csv").write_text("bill_urn,segment_urn,timeInterval,usageInterval,service_agreement_uuid,serviceType,estimated,kwh,usageCharges,currentAmount,effective_price_per_kwh\nbill,seg,2026-01-01/2026-02-01,2026-01-01/2026-02-01,sa,ELECTRICITY,False,30,10,12,0.333333\n")
            (run / "demand_maxima.csv").write_text("utility_id,service_point_uuid,timeInterval,value,unit,readType,isMaximum\n006,sp,2026-06-03T00:00/2026-06-03T00:15,4.0,KW,ACTUAL,True\n")
            (run / "green_button_rows.csv").write_text("dataset,source_file,service_point,type,date,start_time,end_time,usage_kwh,cost,notes\nusage,u.csv,sp,Electric usage,2026-06-03,00:00,00:14,1.0,0.34,\n")
            data = build_dashboard_data(Path(td))
            latest = data["latest"]
            self.assertEqual(latest["price_source"], "rated_components")
            self.assertEqual(latest["weather"][0]["mean_temperature"], 65.0)
            self.assertEqual(latest["bill_history"][0]["kwh"], 30.0)
            self.assertEqual(latest["peak_demand_events"][0]["kw"], 4.0)
            self.assertEqual(latest["service_points"][0]["utility_id"], "006")
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
                    "retrieved_at_utc": stamp, "account": "006", "available_interval": "2026-06-03/2026-06-06",
                    "requested_interval": "2026-06-03/2026-06-06", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
                    "service_points": [{"utility_id": "006", "uuid": "sp"}], "latest_available_end": "2026-06-06T00:00:00-04:00"
                }))
                (run / "usage.csv").write_text(f"utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,{stamp[:8]}T00:00/{stamp[:8]}T00:15,ACTUAL,{kwh},,false,sa,sp\n")
                (run / "cost.csv").write_text(f"utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,{stamp[:8]}T00:00/{stamp[:8]}T00:15,ACTUAL,{kwh},{cost},false,sa,sp\n")
                (run / "pricing.csv").write_text(f"utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes\n006,sp,{stamp[:8]}T00:00/{stamp[:8]}T00:15,netUsage,NET_USAGE,KWH,ACTUAL,{kwh},{cost},{price},FLAT,,,,\n")
                (run / "bills.csv").write_text(f"bill_urn,segment_urn,timeInterval,usageInterval,service_agreement_uuid,serviceType,estimated,kwh,usageCharges,currentAmount,effective_price_per_kwh\nbill-{stamp},seg-{stamp},{bill_start},{bill_start},sa,ELECTRICITY,False,{kwh},{cost},{cost+1},{cost/kwh}\n")
                (run / "weather.csv").write_text("premise_uuid,timeInterval,date,min_temperature,mean_temperature,max_temperature\nprem,2026-06-03/2026-06-04,2026-06-03,50,70,90\n")
            data = build_dashboard_data(Path(td))
            self.assertEqual([p["price_per_kwh"] for p in data["historical_price_series"]], [0.31, 0.33])
            self.assertEqual([b["timeInterval"] for b in data["bill_trend"]], ["2026-01-01/2026-02-01", "2026-02-01/2026-03-01"])

    def test_latest_series_has_temperature_overlay_and_toggleable_filter_model(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "006", "resolution": "QUARTER_HOUR", "timezone": "America/New_York"}))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,,false,sa,sp\n006,demand,DELIVERED,KW,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,2.5,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,0.30,false,sa,sp\n")
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
                "retrieved_at_utc": "20260607T000000Z", "account": "006-A", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
                "service_points": [{"utility_id": "006-A", "uuid": "sp-a"}, {"utility_id": "006-B", "uuid": "sp-b"}],
            }))
            rows = [
                "006-A,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,1.0,,false,sa,sp-a",
                "006-A,netUsage,NET_USAGE,KWH,2026-06-02T00:00/2026-06-02T00:15,ACTUAL,2.0,,false,sa,sp-a",
                "006-B,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,4.0,,false,sa,sp-b",
            ]
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n" + "\n".join(rows) + "\n")
            costs = [
                "006-A,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,1.0,0.30,false,sa,sp-a",
                "006-A,netUsage,NET_USAGE,KWH,2026-06-02T00:00/2026-06-02T00:15,ACTUAL,2.0,0.60,false,sa,sp-a",
                "006-B,netUsage,NET_USAGE,KWH,2026-06-01T00:00/2026-06-01T00:15,ACTUAL,4.0,1.20,false,sa,sp-b",
            ]
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n" + "\n".join(costs) + "\n")
            (run / "bills.csv").write_text("bill_urn,segment_urn,timeInterval,usageInterval,service_agreement_uuid,serviceType,estimated,kwh,usageCharges,currentAmount,effective_price_per_kwh\nbill,seg,2025-06-10T04:00:00Z/2025-07-11T04:00:00Z,2025-06-10T04:00:00Z/2025-07-11T04:00:00Z,sa,ELECTRICITY,False,167,57.6,67.6,0.345\n")
            data = build_dashboard_data(Path(td))
            latest = data["latest"]
            self.assertEqual(data["bill_trend"][0]["period_label"], "Jun 10 – Jul 11, 2025")
            self.assertEqual(data["bill_trend"][0]["billing_days"], 31)
            self.assertEqual(data["bill_trend"][0]["effective_price_per_kwh"], 67.6 / 167)
            summaries = {s["utility_id"]: s for s in latest["service_point_summaries"]}
            self.assertEqual(summaries["006-A"]["total_kwh"], 3.0)
            self.assertEqual(summaries["006-A"]["days"]["2026-06-02"]["kwh"], 2.0)
            self.assertEqual(summaries["006-A"]["weeks"]["2026-W23"]["cost"], 0.9)
            self.assertEqual(summaries["006-B"]["total_cost"], 1.2)

    def test_dashboard_estimates_unrated_cost_from_canonical_price(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({
                "retrieved_at_utc": "20260607T000000Z", "account": "006", "resolution": "QUARTER_HOUR", "timezone": "America/New_York",
            }))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,2026-06-07T00:00/2026-06-07T00:15,ACTUAL,2.0,,false,sa,sp\n")
            (run / "cost.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,2026-06-07T00:00/2026-06-07T00:15,ACTUAL,2.0,0.0,false,sa,sp\n")
            (run / "pricing.csv").write_text("utility_id,service_point_uuid,timeInterval,stream,serviceQuantityIdentifier,unit,readType,value,monetaryAmount,cost_per_unit,rate_type,tier,tou_label,component_attributes\n006,sp,2026-06-07T00:00/2026-06-07T00:15,netUsage,NET_USAGE,KWH,ACTUAL,2.0,0.0,0.33626,FLAT,,,,\n")
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
                "account": "006",
                "usage_rows": 1,
                "errors": {"bill_forecast": "DataFetchingException"},
            }))
            data = build_dashboard_data(Path(td))
            self.assertEqual(data["errors"], [])
            self.assertEqual(data["warnings"][0]["warnings"], {"bill_forecast": "DataFetchingException"})
            self.assertIsNone(data["latest"]["error"])

    def test_dashboard_canvas_charts_render_x_axis_labels(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"account": "acct", "retrieved_at_utc": "20260607T000000Z"}))
            html = __import__("dashboard").HTML_TEMPLATE.replace("__DASHBOARD_DATA__", json.dumps(build_dashboard_data(Path(td))))
            self.assertIn("drawXAxisLabels", html)
            self.assertIn("ctx.fillText(label", html)
            self.assertIn("xLabelFor", html)

    def test_temperature_overlay_breaks_lines_across_missing_values(self):
        from dashboard import generate_dashboard
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "20260607T000000Z"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"retrieved_at_utc": "20260607T000000Z", "account": "006"}))
            (run / "usage.csv").write_text("utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value,monetaryAmount,isPeakPeriod,service_agreement_uuid,service_point_uuid\n006,netUsage,NET_USAGE,KWH,2026-06-03T00:00/2026-06-03T00:15,ACTUAL,1.0,,false,sa,sp\n006,netUsage,NET_USAGE,KWH,2026-06-06T00:00/2026-06-06T00:15,ACTUAL,1.0,,false,sa,sp\n")
            out = Path(td) / "dashboard.html"
            generate_dashboard(Path(td), out)
            self.assertIn("started=false;return", out.read_text())


if __name__ == "__main__":
    unittest.main()
