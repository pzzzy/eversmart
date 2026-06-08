#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import html as html_lib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Iterable

EVERSOURCE_DATABROWSER_URL = "https://www.eversource.com/cg/customer/UsageHistory/DataBrowser"
OPOWER_GRAPHQL_URL = "https://ever.opower.com/ei/edge/apis/dsm-graphql-v1/cws/graphql"
DEFAULT_ACCOUNT = "0061362115-51545561"
DEFAULT_ENTITY_ID = "74005127621"
LOGIN_URL = "https://www.eversource.com/security/account/login"
MSLOGIN_URL = "https://www.eversource.com/security/account/MSLogin"
RETURN_URL = "/cg/customer/UsageHistory/DataBrowser"
OKTA_OPOWER_CLIENT_ID = "0oail6c2lbhNdffx71t7"
OKTA_OPOWER_ISSUER = "https://eversource-external.okta.com/oauth2/default"
OKTA_OPOWER_REDIRECT_URI = "https://www.eversource.com/cg/customer/accountoverview"
QUERY_CATALOG = Path("api_research/extracted_graphql_queries.json")


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def multipart_form(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----EversmartBoundary{int(time.time() * 1000000)}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def catalog_query(name: str, fallback: str | None = None) -> str:
    if QUERY_CATALOG.exists():
        try:
            for item in json.loads(QUERY_CATALOG.read_text()):
                if item.get("name") == name and item.get("query"):
                    return item["query"]
        except Exception:
            pass
    if fallback is None:
        raise RuntimeError(f"GraphQL query {name!r} was not found in {QUERY_CATALOG}")
    return fallback


METADATA_QUERY = r"""
query WDB_GetMetadata(
  $selectedAccount: ID,
  $customerURN: ID,
  $forceLegacyData: Boolean,
  $lastForServicePoints: Int,
  $aliased: Boolean,
  $first: Int,
  $endCursor: String
) {
  billingAccountByAuthContext(selectedAccount: $selectedAccount, singlePremise: $customerURN, forceLegacyData: $forceLegacyData) {
    name urn utilityCode utilityId uuid customerClass
    mappedCustomers { urn uuid id utilityCustomerId utilityInternalId utilityInternalId2 accountNumber }
    premisesConnection(first: 100) {
      edges {
        mappedCustomer { urn uuid id utilityCustomerId utilityInternalId utilityInternalId2 accountNumber }
        node { urn uuid utilityId }
      }
    }
    serviceAgreementsConnection(first: $first, after: $endCursor, onlyActive: true, aliased: $aliased) {
      pageInfo { hasPreviousPage hasNextPage startCursor endCursor }
      edges {
        node {
          name nickname urn uuid serviceType utilityId utilityCode
          mappedUtilityAccounts { uuid }
          startDateTime availableBillSegmentsInterval
          historicalSolarBillingModels { isNetBilling isNetMetering isBuyAllSellAll startDateTime endDateTime }
          isSolarAnnualBilling
          ratePlan { code title titleShort descriptionShort currentRatePlan rateTags }
          servicePointsConnection(last: $lastForServicePoints) {
            edges {
              node {
                name serviceType urn uuid utilityId nickname
                premise { timeZone name uuid urn utilityId address { addressLines locality adminArea postalCode } }
                registers { readResolution availableReadsTimeInterval serviceQuantityIdentifier unitOfMeasure }
                deviceInstallations { device { utilityId uuid } }
              }
            }
          }
        }
      }
    }
  }
}
"""

USAGE_QUERY = r"""
query WDB_GetUsageReadsForDayAndHourWithIntervalReads(
  $selectedAccount: ID,
  $customerURN: ID,
  $timeInterval: TimeInterval,
  $resolution: ReadResolution,
  $units: [UnitOfMeasure!]!,
  $serviceQuantityIdentifier: [ServiceQuantityIdentifier!]!,
  $forceLegacyData: Boolean,
  $aliased: Boolean,
  $saUuid: String,
  $spUuid: String,
  $includeReadStreams: Boolean!,
  $includeIntervalReads: Boolean!
) {
  billingAccountByAuthContext(selectedAccount: $selectedAccount, singlePremise: $customerURN, forceLegacyData: $forceLegacyData) {
    serviceAgreementsConnection(onlyActive: true, aliased: $aliased, matching: $saUuid) {
      edges {
        node {
          uuid serviceType
          intervalReads(timeInterval: $timeInterval, units: $units, readResolution: $resolution, serviceQuantityIdentifier: $serviceQuantityIdentifier) @include(if: $includeIntervalReads) {
            serviceType serviceQuantityIdentifier unit reads { timeInterval readType measuredAmount { value } }
          }
          servicePointsConnection(matching: $spUuid) {
            edges {
              node {
                serviceType utilityId uuid
                readStreams(timeInterval: $timeInterval, readResolution: $resolution) @include(if: $includeReadStreams) {
                  timeInterval
                  netUsage { serviceQuantityIdentifier unit reads { readType timeInterval measuredAmount { unit value } isPeakPeriod } }
                  energyDelivered { serviceQuantityIdentifier unit reads { readType timeInterval measuredAmount { unit value } isPeakPeriod } }
                  demand { serviceQuantityIdentifier unit reads { readType timeInterval measuredAmount { unit value } isPeakPeriod isMaximum } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

COST_QUERY = r"""
query WDB_GetCostReadsForDayAndHour(
  $selectedAccount: ID,
  $customerURN: ID,
  $timeInterval: TimeInterval,
  $resolution: ReadResolution,
  $forceLegacyData: Boolean,
  $includePeakTimeRebates: Boolean,
  $aliased: Boolean,
  $saUuid: String,
  $spUuid: String,
  $includeAdditionalUOM: Boolean!
) {
  billingAccountByAuthContext(selectedAccount: $selectedAccount, singlePremise: $customerURN, forceLegacyData: $forceLegacyData) {
    serviceAgreementsConnection(onlyActive: true, aliased: $aliased, matching: $saUuid) {
      edges {
        node {
          uuid serviceType ratePlan { code currentRatePlan rateTags titleShort descriptionShort }
          servicePointsConnection(matching: $spUuid) {
            edges {
              node {
                serviceType utilityId uuid
                readStreams(timeInterval: $timeInterval, readResolution: $resolution, includePeakTimeRebates: $includePeakTimeRebates) {
                  timeInterval
                  rates { rateType tier costPerUnit { value } activationDate inactivationDate timeOfUsePeriod { label timeOfUse season times } }
                  netUsage {
                    serviceQuantityIdentifier unit isOpowerDerived registerId
                    reads { readType timeInterval measuredAmount { unit value } monetaryAmount { value } rebateAmount { value } isPeakPeriod ratedReadComponents { measuredAmount { unit value } monetaryAmount { value } costPerUnit { value } attributes { key value } } }
                  }
                  energyDelivered @include(if: $includeAdditionalUOM) {
                    serviceQuantityIdentifier unit isOpowerDerived registerId
                    reads { readType timeInterval measuredAmount { unit value } monetaryAmount { value } rebateAmount { value } isPeakPeriod ratedReadComponents { measuredAmount { unit value } monetaryAmount { value } costPerUnit { value } attributes { key value } } }
                  }
                  demand @include(if: $includeAdditionalUOM) {
                    serviceQuantityIdentifier unit isOpowerDerived registerId
                    reads { readType timeInterval measuredAmount { unit value } monetaryAmount { value } rebateAmount { value } isPeakPeriod isMaximum ratedReadComponents { measuredAmount { unit value } monetaryAmount { value } costPerUnit { value } attributes { key value } } }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass
class Target:
    account: str
    entity_id: str
    service_agreement_uuid: str
    service_point_uuid: str
    available_interval: str
    resolution: str
    timezone: str | None
    billing_account_urn: str | None = None
    billing_account_uuid: str | None = None
    service_agreement_urn: str | None = None
    premise_uuid: str | None = None
    premise_urn: str | None = None
    available_bill_interval: str | None = None
    rate_plan_code: str | None = None


class EversourceClient:
    def __init__(self, cookie_file: Path = Path(".eversource_cookies.txt"), cookieinfo: Path = Path("cookieinfo.txt")) -> None:
        self.cookie_file = Path(cookie_file)
        self.cookieinfo = Path(cookieinfo)
        if not self.cookie_file.exists() and self.cookieinfo.exists():
            self.convert_cookieinfo()
        self.jar = MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            self.jar.load(ignore_discard=True, ignore_expires=True)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.access_token: str | None = None
        self.entity_id: str | None = None
        load_dotenv()

    def convert_cookieinfo(self) -> None:
        from cookie_convert import parse_cookieinfo
        parse_cookieinfo(self.cookieinfo, self.cookie_file)
        try:
            os.chmod(self.cookie_file, 0o600)
        except OSError:
            pass

    def request(self, url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
        base = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" if data is None else "application/json",
        }
        if headers:
            base.update(headers)
        req = urllib.request.Request(url, data=data, headers=base)
        with self.opener.open(req, timeout=60) as resp:
            out = resp.read()
        self.jar.save(ignore_discard=True, ignore_expires=True)
        return out

    def reset_session(self) -> None:
        self.access_token = None
        self.entity_id = None
        self.jar = MozillaCookieJar(str(self.cookie_file))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        try:
            self.cookie_file.unlink()
        except FileNotFoundError:
            pass

    def login(self, return_url: str = RETURN_URL, force_refresh: bool = False) -> dict[str, Any]:
        if force_refresh:
            self.reset_session()
        web_id = os.environ.get("EVERSOURCE_LOGIN")
        password = os.environ.get("EVERSOURCE_PASS")
        if not web_id or not password:
            raise RuntimeError("Missing EVERSOURCE_LOGIN/EVERSOURCE_PASS in environment or .env")
        login_url = f"{LOGIN_URL}?ReturnUrl={urllib.parse.quote(return_url)}"
        html = self.request(login_url).decode("utf-8", "replace")
        config_match = re.search(r'data-config="([^"]+)" id="App-LogIn"', html)
        if not config_match:
            raise RuntimeError("Could not find Eversource login data-config/anti-forgery token")
        config = json.loads(html_lib.unescape(config_match.group(1)))
        form_token = config.get("formToken")
        anti_header = config.get("antiForgeryHeaderName", "__RequestVerificationToken")
        body, boundary = multipart_form({
            "WebId": web_id,
            "Password": password,
            "RememberID": "false",
            "ReturnUrl": return_url,
            "MfaRememberMeData": "",
        })
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Origin": "https://www.eversource.com",
            "Referer": login_url,
            "X-Requested-With": "XMLHttpRequest",
        }
        if form_token:
            headers[anti_header] = form_token
        raw = self.request(MSLOGIN_URL, body, headers)
        result = json.loads(raw.decode("utf-8", "replace"))
        if not result.get("IsSuccess"):
            raise RuntimeError(f"Eversource login failed: {json.dumps(result.get('Errors') or result, sort_keys=True)}")
        return result

    @staticmethod
    def _jwt_expiry_epoch(token: str | None) -> int | None:
        if not token or token.count(".") < 2:
            return None
        try:
            payload = token.split(".", 2)[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
            exp = data.get("exp")
            return int(exp) if exp is not None else None
        except Exception:
            return None

    @staticmethod
    def _is_token_expired_or_stale(token: str | None, skew_seconds: int = 120) -> bool:
        exp = EversourceClient._jwt_expiry_epoch(token)
        return exp is None or exp <= int(time.time()) + skew_seconds

    def obtain_fresh_opower_token(self, force_login: bool = False) -> str:
        """Exchange Eversource's Okta session token for a fresh Opower-accepted access token.

        The DataBrowser HTML can contain a stale embedded token even after a successful
        first-party login. The token's Okta client id shows it belongs to a different
        OIDC app than the header sign-in widget, and that app accepts accountoverview
        as its redirect URI. Using the MSLogin SessionToken here avoids relying on the
        stale server-rendered script block.
        """
        result = self.login(force_refresh=force_login)
        session_token = result.get("SessionToken")
        if not session_token:
            raise RuntimeError("Eversource login did not return an Okta SessionToken; cannot mint a fresh Opower token.")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
                return None

        params = {
            "client_id": OKTA_OPOWER_CLIENT_ID,
            "response_type": "token",
            "scope": "openid offline_access",
            "redirect_uri": OKTA_OPOWER_REDIRECT_URI,
            "state": "eversmart",
            "nonce": str(int(time.time() * 1000)),
            "sessionToken": session_token,
        }
        url = f"{OKTA_OPOWER_ISSUER}/v1/authorize?{urllib.parse.urlencode(params)}"
        opener = getattr(self, "okta_opener", None) or urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))
        location = None
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as resp:
                location = resp.geturl()
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            if not location:
                detail = exc.read().decode("utf-8", "replace")[:500]
                raise RuntimeError(f"Okta token exchange failed with HTTP {exc.code}: {detail}") from exc
        fragment = urllib.parse.urlparse(location).fragment
        token = (urllib.parse.parse_qs(fragment).get("access_token") or [None])[0]
        if not token:
            raise RuntimeError("Okta token exchange completed but no access_token was present in redirect fragment.")
        self.access_token = token
        Path(".opower_access_token").write_text(f"{self.access_token}\n{self.entity_id or DEFAULT_ENTITY_ID}\n")
        try:
            os.chmod(".opower_access_token", 0o600)
        except OSError:
            pass
        return token

    def load_databrowser(self) -> str:
        html = self.request(EVERSOURCE_DATABROWSER_URL).decode("utf-8", "replace")
        if "App-LogIn" in html or "/security/account/login" in html[:20000]:
            self.login()
            html = self.request(EVERSOURCE_DATABROWSER_URL).decode("utf-8", "replace")
        if "App-LogIn" in html or "/security/account/login" in html[:20000]:
            raise RuntimeError("Eversource session is not authenticated after login; refresh cookieinfo.txt or check MFA state.")
        token_match = re.search(r"accessToken:\s*'([^']+)'", html)
        entity_match = re.search(r"setEntityIds\(\['([^']+)'\]\)", html)
        if token_match:
            self.access_token = token_match.group(1)
        else:
            self.access_token = None
        self.entity_id = entity_match.group(1) if entity_match else DEFAULT_ENTITY_ID
        if not self.access_token or self._is_token_expired_or_stale(self.access_token):
            self.obtain_fresh_opower_token(force_login=True)
        Path(".opower_access_token").write_text(f"{self.access_token}\n{self.entity_id}\n")
        try:
            os.chmod(".opower_access_token", 0o600)
        except OSError:
            pass
        return html

    def graphql(self, query: str, variables: dict[str, Any], entity_id: str | None = None, _token_retry: bool = True) -> dict[str, Any]:
        if not self.access_token:
            self.load_databrowser()
        selected = entity_id or self.entity_id or DEFAULT_ENTITY_ID
        body = json.dumps({"query": query, "variables": variables}).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept-Language": "en-US",
            "Origin": "https://ever.opower.com",
            "Referer": "https://www.eversource.com/cg/customer/UsageHistory/DataBrowser",
            "Authorization": f"Bearer {self.access_token}",
            "Opower-Selected-Entities": json.dumps([f"urn:external:opower:entity:id:{selected}"]),
        }
        retry_codes = {429, 500, 502, 503, 504}
        attempts = 4
        last_detail = ""
        sleep = getattr(self, "_sleep", time.sleep)
        for attempt in range(attempts):
            try:
                raw = self.request(OPOWER_GRAPHQL_URL, body, headers)
                data = json.loads(raw.decode())
                if data.get("errors"):
                    raise RuntimeError(json.dumps(data["errors"], indent=2))
                return data
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                last_detail = f"Opower GraphQL HTTP {exc.code}: {detail}"
                if _token_retry and exc.code in (401, 403) and ("expired" in detail.lower() or "invalid token" in detail.lower()):
                    self.login(force_refresh=True)
                    self.load_databrowser()
                    return self.graphql(query, variables, entity_id, _token_retry=False)
                if exc.code in retry_codes and attempt < attempts - 1:
                    sleep(min(2 ** attempt, 8))
                    continue
                raise RuntimeError(last_detail) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_detail = f"Opower GraphQL transient network error: {exc}"
                if attempt < attempts - 1:
                    sleep(min(2 ** attempt, 8))
                    continue
                raise RuntimeError(last_detail) from exc
        raise RuntimeError(last_detail or "Opower GraphQL request failed")

    def metadata(self) -> dict[str, Any]:
        return self.graphql(METADATA_QUERY, {
            "selectedAccount": None, "customerURN": None, "forceLegacyData": False,
            "lastForServicePoints": 50, "aliased": False, "first": 75, "endCursor": None,
        })

    def service_points(self, metadata: dict[str, Any]) -> list[Target]:
        acct = metadata["data"]["billingAccountByAuthContext"]
        targets: list[Target] = []
        for edge in (acct.get("serviceAgreementsConnection") or {}).get("edges") or []:
            sa = edge.get("node") or {}
            for sp_edge in ((sa.get("servicePointsConnection") or {}).get("edges") or []):
                sp = sp_edge.get("node") or {}
                regs = sp.get("registers") or []
                net_reg = next((r for r in regs if r.get("serviceQuantityIdentifier") == "NET_USAGE"), None)
                reg = net_reg or (regs[0] if regs else None)
                if not reg:
                    continue
                premise = sp.get("premise") or {}
                rate_plan = sa.get("ratePlan") or {}
                targets.append(Target(
                    account=sp.get("utilityId"),
                    entity_id=acct.get("utilityId") or DEFAULT_ENTITY_ID,
                    service_agreement_uuid=sa.get("uuid"),
                    service_point_uuid=sp.get("uuid"),
                    available_interval=reg.get("availableReadsTimeInterval"),
                    resolution=reg.get("readResolution"),
                    timezone=premise.get("timeZone"),
                    billing_account_urn=acct.get("urn"),
                    billing_account_uuid=acct.get("uuid"),
                    service_agreement_urn=sa.get("urn"),
                    premise_uuid=premise.get("uuid"),
                    premise_urn=premise.get("urn"),
                    available_bill_interval=sa.get("availableBillSegmentsInterval"),
                    rate_plan_code=rate_plan.get("code"),
                ))
        return targets

    def find_target(self, metadata: dict[str, Any], account: str = DEFAULT_ACCOUNT) -> Target:
        for target in self.service_points(metadata):
            if target.account == account:
                return target
        raise RuntimeError(f"Account/service point {account} not found in metadata")

    def usage(self, target: Target, interval: str | None = None, resolution: str | None = None) -> dict[str, Any]:
        return self.graphql(USAGE_QUERY, {
            "selectedAccount": None, "customerURN": None,
            "timeInterval": interval or target.available_interval,
            "resolution": resolution or target.resolution,
            "units": ["KWH"], "serviceQuantityIdentifier": [],
            "forceLegacyData": False, "aliased": False,
            "saUuid": target.service_agreement_uuid, "spUuid": target.service_point_uuid,
            "includeReadStreams": True, "includeIntervalReads": False,
        }, target.entity_id)

    def cost(self, target: Target, interval: str | None = None, resolution: str | None = None) -> dict[str, Any]:
        return self.graphql(COST_QUERY, {
            "selectedAccount": None, "customerURN": None,
            "timeInterval": interval or target.available_interval,
            "resolution": resolution or target.resolution,
            "forceLegacyData": False, "includePeakTimeRebates": False,
            "aliased": False, "saUuid": target.service_agreement_uuid, "spUuid": target.service_point_uuid,
            "includeAdditionalUOM": True,
        }, target.entity_id)

    def pricing(self, target: Target, interval: str | None = None, resolution: str | None = None) -> dict[str, Any]:
        return self.graphql(catalog_query("WDB_GetHourlyPricingReadsForDayAndHour", COST_QUERY), {
            "selectedAccount": None, "customerURN": None,
            "timeInterval": interval or target.available_interval,
            "resolution": resolution or target.resolution,
            "forceLegacyData": False, "aliased": False,
            "saUuid": target.service_agreement_uuid, "spUuid": target.service_point_uuid,
            "fillMissingIntervals": False,
        }, target.entity_id)

    def weather(self, target: Target, interval: str | None = None) -> dict[str, Any]:
        return self.graphql(catalog_query("WDB_GetWeather"), {
            "selectedAccount": None, "customerURN": None, "unit": "FAHRENHEIT",
            "timeInterval": [interval or target.available_interval], "weatherResolution": "DAILY",
            "forceLegacyData": False, "premiseUuid": target.premise_uuid,
        }, target.entity_id)

    def bills(self, target: Target, interval: str | None = None, last: int = 36) -> dict[str, Any]:
        return self.graphql(catalog_query("WDB_GetCostUsageReadsForBills"), {
            "selectedAccount": None, "customerURN": None, "last": last,
            "timeInterval": interval or target.available_bill_interval, "forceLegacyData": False, "aliased": False,
        }, target.entity_id)

    def demand_maxima(self, target: Target, interval: str | None = None) -> dict[str, Any]:
        return self.graphql(catalog_query("WDH_GetAmiUsage"), {
            "timeInterval": interval or target.available_interval,
            "selectedAccount": None, "forceLegacyData": False,
            "saUuid": target.service_agreement_uuid, "spUuid": target.service_point_uuid,
        }, target.entity_id)

    def forecast(self, target: Target) -> dict[str, Any]:
        return self.graphql(catalog_query("GetBillForecast"), {"selectedAccount": None}, target.entity_id)

    def rate_static_content(self, target: Target) -> dict[str, Any]:
        return self.graphql(catalog_query("WDB_GetRateStaticContent"), {
            "selectedAccount": None, "customerURN": None, "forceLegacyData": False,
            "aliased": False, "saUuid": target.service_agreement_uuid,
        }, target.entity_id)

    def generate_usage_export(self, target: Target, interval: str | None = None, fmt: str = "CSV") -> str:
        urns = [target.billing_account_urn] if target.billing_account_urn else []
        variables = {"usageExportFileConfigurationInput": {
            "urns": urns,
            "utilityCode": "ever",
            "format": fmt,
            "forceLegacyData": False,
        }}
        if interval:
            variables["usageExportFileConfigurationInput"]["timeInterval"] = interval
        data = self.graphql(catalog_query("WUE_GenerateUsageExportFile"), variables, target.entity_id)
        return data["data"]["generateUsageExportFile"]["uuid"]

    def export_job(self, target: Target, job_uuid: str) -> dict[str, Any]:
        return self.graphql(catalog_query("WUE_GetExportJob"), {"jobUuid": job_uuid}, target.entity_id)

    def download_url(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()


def money_value(obj: Any) -> Any:
    return (obj or {}).get("value") if isinstance(obj, dict) else None


def iter_stream_reads(doc: dict[str, Any]) -> Iterable[dict[str, Any]]:
    edges = (((doc.get("data") or {}).get("billingAccountByAuthContext") or {})
             .get("serviceAgreementsConnection") or {}).get("edges") or []
    for sa_edge in edges:
        sa = sa_edge.get("node") or {}
        for sp_edge in (((sa.get("servicePointsConnection") or {}).get("edges")) or []):
            sp = sp_edge.get("node") or {}
            streams = sp.get("readStreams") or {}
            for stream_name in ("netUsage", "energyDelivered", "energyReceived", "demand"):
                for stream in streams.get(stream_name) or []:
                    for read in stream.get("reads") or []:
                        amt = read.get("measuredAmount") or {}
                        money = read.get("monetaryAmount") or {}
                        yield {
                            "service_agreement_uuid": sa.get("uuid"),
                            "service_point_uuid": sp.get("uuid"),
                            "utility_id": sp.get("utilityId"),
                            "stream": stream_name,
                            "serviceQuantityIdentifier": stream.get("serviceQuantityIdentifier"),
                            "unit": amt.get("unit") or stream.get("unit"),
                            "timeInterval": read.get("timeInterval"),
                            "readType": read.get("readType"),
                            "value": amt.get("value"),
                            "monetaryAmount": money.get("value"),
                            "isPeakPeriod": read.get("isPeakPeriod"),
                            "isMaximum": read.get("isMaximum"),
                        }


def _attrs_text(attrs: list[dict[str, Any]] | None) -> str:
    return ";".join(f"{a.get('key')}={a.get('value')}" for a in (attrs or []) if a.get("key") is not None)


def iter_pricing_components(doc: dict[str, Any]) -> Iterable[dict[str, Any]]:
    edges = (((doc.get("data") or {}).get("billingAccountByAuthContext") or {})
             .get("serviceAgreementsConnection") or {}).get("edges") or []
    for sa_edge in edges:
        sa = sa_edge.get("node") or {}
        rate_plan = sa.get("ratePlan") or {}
        for sp_edge in ((sa.get("servicePointsConnection") or {}).get("edges") or []):
            sp = sp_edge.get("node") or {}
            streams = sp.get("readStreams") or {}
            default_rate = (streams.get("rates") or [{}])[0] or {}
            for stream_name in ("netUsage", "energyDelivered", "energyReceived", "demand"):
                for stream in streams.get(stream_name) or []:
                    for read in stream.get("reads") or []:
                        components = read.get("ratedReadComponents") or [{}]
                        for comp in components:
                            tou = (comp.get("timeOfUsePeriod") if comp else None) or default_rate.get("timeOfUsePeriod") or {}
                            amt = read.get("measuredAmount") or {}
                            comp_amt = (comp or {}).get("measuredAmount") or {}
                            yield {
                                "utility_id": sp.get("utilityId"),
                                "service_agreement_uuid": sa.get("uuid"),
                                "service_point_uuid": sp.get("uuid"),
                                "rate_plan_code": rate_plan.get("code"),
                                "timeInterval": read.get("timeInterval"),
                                "stream": stream_name,
                                "serviceQuantityIdentifier": stream.get("serviceQuantityIdentifier"),
                                "unit": comp_amt.get("unit") or amt.get("unit") or stream.get("unit"),
                                "readType": read.get("readType"),
                                "value": comp_amt.get("value", amt.get("value")),
                                "monetaryAmount": money_value((comp or {}).get("monetaryAmount")) or money_value(read.get("monetaryAmount")),
                                "cost_per_unit": money_value((comp or {}).get("costPerUnit")) or money_value(default_rate.get("costPerUnit")),
                                "rate_type": default_rate.get("rateType"),
                                "tier": (comp or {}).get("tier") or default_rate.get("tier"),
                                "tou_label": tou.get("label"),
                                "component_attributes": _attrs_text((comp or {}).get("attributes")),
                            }


def iter_weather_rows(doc: dict[str, Any]) -> Iterable[dict[str, Any]]:
    edges = (((doc.get("data") or {}).get("billingAccountByAuthContext") or {}).get("premisesConnection") or {}).get("edges") or []
    for edge in edges:
        premise = edge.get("node") or {}
        for row in premise.get("weather") or []:
            interval = row.get("timeInterval") or ""
            yield {
                "premise_uuid": premise.get("uuid"),
                "timeInterval": interval,
                "date": interval[:10],
                "min_temperature": money_value(row.get("minTemperature")),
                "mean_temperature": money_value(row.get("meanTemperature")),
                "max_temperature": money_value(row.get("maxTemperature")),
            }


def iter_bill_segments(doc: dict[str, Any]) -> Iterable[dict[str, Any]]:
    bills = ((doc.get("data") or {}).get("billingAccountByAuthContext") or {}).get("bills") or []
    for bill in bills:
        for seg in bill.get("segments") or []:
            kwh = None
            unit = None
            sqi = None
            for sq in seg.get("serviceQuantities") or []:
                if sq.get("serviceQuantityIdentifier") in ("NET_USAGE", "ENERGY_DELIVERED", "DELIVERED") or kwh is None:
                    kwh = money_value(sq.get("serviceQuantity"))
                    unit = sq.get("unit")
                    sqi = sq.get("serviceQuantityIdentifier")
                    if sqi == "NET_USAGE":
                        break
            usage_charges = money_value(seg.get("usageCharges"))
            current_amount = money_value(seg.get("currentAmount"))
            try:
                eff = float(current_amount) / float(kwh) if current_amount is not None and kwh else None
            except (TypeError, ValueError, ZeroDivisionError):
                eff = None
            sa = seg.get("serviceAgreement") or {}
            yield {
                "bill_urn": bill.get("urn"),
                "segment_urn": seg.get("urn"),
                "timeInterval": bill.get("timeInterval") or seg.get("timeInterval"),
                "usageInterval": seg.get("usageInterval"),
                "service_agreement_uuid": sa.get("uuid"),
                "service_agreement_urn": sa.get("urn"),
                "serviceType": sa.get("serviceType"),
                "estimated": seg.get("estimated"),
                "serviceQuantityIdentifier": sqi,
                "unit": unit,
                "kwh": kwh,
                "usageCharges": usage_charges,
                "currentAmount": current_amount,
                "deferredNEMCharges": money_value(seg.get("deferredNEMCharges")),
                "totalNEMCharges": money_value(seg.get("totalNEMCharges")),
                "energyPurchased": money_value(seg.get("energyPurchased")),
                "energySold": money_value(seg.get("energySold")),
                "totalEnergyCosts": money_value(seg.get("totalEnergyCosts")),
                "effective_price_per_kwh": eff,
            }


def iter_demand_maxima(doc: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for row in iter_stream_reads(doc):
        if row.get("stream") == "demand" and str(row.get("isMaximum")).lower() == "true":
            yield {
                "utility_id": row.get("utility_id"), "service_point_uuid": row.get("service_point_uuid"),
                "timeInterval": row.get("timeInterval"), "value": row.get("value"), "unit": row.get("unit"),
                "readType": row.get("readType"), "isMaximum": row.get("isMaximum"),
            }


def _csv_value(row: dict[str, str], *names: str) -> str:
    lowered = {k.strip().lower(): ("" if v is None else str(v)) for k, v in row.items() if k is not None}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()].strip()
    return ""


def _num(text: str) -> float | None:
    s = str(text or "").replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_green_button_zip(blob: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".csv"):
                continue
            text = zf.read(info).decode("utf-8-sig", "replace")
            lines = text.splitlines()
            service_point = ""
            for line in lines[:20]:
                if line.lower().startswith("service point"):
                    parts = next(csv.reader([line]))
                    service_point = parts[1].strip() if len(parts) > 1 else ""
            header_idx = next((i for i, line in enumerate(lines) if line.upper().startswith("TYPE,")), None)
            if header_idx is None:
                continue
            reader = csv.DictReader(lines[header_idx:])
            for row in reader:
                typ = _csv_value(row, "TYPE")
                is_billing = "billing" in typ.lower() or _csv_value(row, "START DATE")
                out = {
                    "dataset": "billing" if is_billing else "usage",
                    "source_file": info.filename,
                    "service_point": service_point,
                    "type": typ,
                    "date": _csv_value(row, "DATE", "START DATE"),
                    "start_time": _csv_value(row, "START TIME"),
                    "end_time": _csv_value(row, "END TIME", "END DATE"),
                    "usage_kwh": _num(_csv_value(row, "USAGE (kWh)", "USAGE", "USAGE KWH")),
                    "cost": _num(_csv_value(row, "COST", "USAGE CHARGES")),
                    "notes": _csv_value(row, "NOTES"),
                }
                rows.append(out)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in fields})


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True))


def write_optional_error(run_dir: Path, name: str, exc: Exception, errors: dict[str, str]) -> None:
    errors[name] = str(exc)
    (run_dir / f"{name}_error.txt").write_text(str(exc))


def write_optional_warning(run_dir: Path, name: str, exc: Exception, warnings: dict[str, str]) -> None:
    warnings[name] = str(exc)
    (run_dir / f"{name}_warning.txt").write_text(str(exc))


def is_transient_upstream_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in (
        "http 500", "http 502", "http 503", "http 504", "http 429",
        "bad gateway", "service unavailable", "gateway timeout", "too many requests",
        "timed out", "temporarily unavailable",
    ))


def write_noncritical_issue(run_dir: Path, name: str, exc: Exception, errors: dict[str, str], warnings: dict[str, str], *, transient_as_warning: bool = True) -> None:
    if transient_as_warning and is_transient_upstream_error(exc):
        write_optional_warning(run_dir, name, exc, warnings)
    else:
        write_optional_error(run_dir, name, exc, errors)


def run_tests() -> int:
    return subprocess.call([sys.executable, "-m", "unittest", "discover", "-v"])


def collect_once(client: EversourceClient, outdir: Path, account: str, interval: str | None, resolution: str | None,
                 all_service_points: bool = False, green_button: bool = False) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    metadata = client.metadata()
    targets = client.service_points(metadata)
    primary = next((t for t in targets if t.account == account), None)
    if primary is None:
        raise RuntimeError(f"Account/service point {account} not found in metadata")
    selected = targets if all_service_points else [primary]
    run_dir = outdir / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "metadata.json", metadata)
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    usage_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []
    pricing_rows: list[dict[str, Any]] = []
    demand_rows: list[dict[str, Any]] = []
    weather_rows: list[dict[str, Any]] = []
    bill_rows: list[dict[str, Any]] = []
    service_point_rows = [asdict(t) for t in targets]

    for target in selected:
        suffix = target.account.replace("-", "_")
        actual_interval = interval or target.available_interval
        actual_resolution = resolution or target.resolution
        usage = client.usage(target, actual_interval, actual_resolution)
        write_json(run_dir / f"usage_{suffix}.json", usage)
        usage_rows.extend(iter_stream_reads(usage))
        for name, func, sink, iterator in [
            ("cost", client.cost, cost_rows, iter_stream_reads),
            ("pricing", client.pricing, pricing_rows, iter_pricing_components),
            ("demand_maxima", client.demand_maxima, demand_rows, iter_demand_maxima),
        ]:
            try:
                doc = func(target, actual_interval, actual_resolution) if name in {"cost", "pricing"} else func(target, actual_interval)
                write_json(run_dir / f"{name}_{suffix}.json", doc)
                sink.extend(iterator(doc))
            except Exception as exc:
                write_optional_error(run_dir, f"{name}_{suffix}", exc, errors)
        try:
            wdoc = client.weather(target, actual_interval)
            write_json(run_dir / f"weather_{suffix}.json", wdoc)
            weather_rows.extend(iter_weather_rows(wdoc))
        except Exception as exc:
            write_optional_error(run_dir, f"weather_{suffix}", exc, errors)

    try:
        bdoc = client.bills(primary)
        write_json(run_dir / "bills.json", bdoc)
        bill_rows.extend(iter_bill_segments(bdoc))
    except Exception as exc:
        write_optional_error(run_dir, "bills", exc, errors)
    try:
        fdoc = client.forecast(primary)
        write_json(run_dir / "bill_forecast.json", fdoc)
    except Exception as exc:
        write_optional_warning(run_dir, "bill_forecast", exc, warnings)
    try:
        rdoc = client.rate_static_content(primary)
        write_json(run_dir / "rate_plan.json", rdoc)
    except Exception as exc:
        write_noncritical_issue(run_dir, "rate_plan", exc, errors, warnings)

    gb_rows: list[dict[str, Any]] = []
    gb_manifest: dict[str, Any] | None = None
    if green_button:
        try:
            job_uuid = client.generate_usage_export(primary, interval or primary.available_interval)
            job = None
            for _ in range(10):
                job = client.export_job(primary, job_uuid).get("data", {}).get("exportJob")
                if job and job.get("isFinished"):
                    break
                if job and job.get("isFailed"):
                    raise RuntimeError(f"Green Button export failed: {job}")
                time.sleep(2)
            if not job or not job.get("isFinished") or not job.get("result"):
                raise RuntimeError(f"Green Button export did not finish promptly: {job}")
            blob = client.download_url(job["result"])
            (run_dir / "green_button.zip").write_bytes(blob)
            gb_rows = parse_green_button_zip(blob)
            gb_manifest = {"job_uuid": job_uuid, "bytes": len(blob), "row_count": len(gb_rows), "result_url_present": True}
            write_json(run_dir / "green_button_manifest.json", gb_manifest)
        except Exception as exc:
            write_optional_error(run_dir, "green_button", exc, errors)

    stream_fields = ["utility_id", "stream", "serviceQuantityIdentifier", "unit", "timeInterval", "readType", "value", "monetaryAmount", "isPeakPeriod", "isMaximum", "service_agreement_uuid", "service_point_uuid"]
    write_csv(run_dir / "usage.csv", usage_rows, stream_fields)
    write_csv(run_dir / "cost.csv", cost_rows, stream_fields)
    write_csv(run_dir / "pricing.csv", pricing_rows)
    write_csv(run_dir / "weather.csv", weather_rows)
    write_csv(run_dir / "bills.csv", bill_rows)
    write_csv(run_dir / "demand_maxima.csv", demand_rows)
    write_csv(run_dir / "service_points.csv", service_point_rows)
    if gb_rows:
        write_csv(run_dir / "green_button_rows.csv", gb_rows)

    manifest = {
        "retrieved_at_utc": stamp,
        "account": account,
        "entity_id": primary.entity_id,
        "service_agreement_uuid": primary.service_agreement_uuid,
        "service_point_uuid": primary.service_point_uuid,
        "available_interval": primary.available_interval,
        "latest_available_end": (primary.available_interval or "/").split("/")[-1],
        "available_bill_interval": primary.available_bill_interval,
        "requested_interval": interval or primary.available_interval,
        "resolution": resolution or primary.resolution,
        "timezone": primary.timezone,
        "target_count": len(selected),
        "service_points": service_point_rows,
        "usage_rows": len(usage_rows), "cost_rows": len(cost_rows), "pricing_rows": len(pricing_rows),
        "weather_rows": len(weather_rows), "bill_rows": len(bill_rows), "demand_maxima_rows": len(demand_rows),
        "green_button_rows": len(gb_rows), "green_button": gb_manifest,
        "errors": errors, "warnings": warnings, "cost_error": errors.get(f"cost_{primary.account.replace('-', '_')}"),
        "output_dir": str(run_dir.resolve()),
    }
    write_json(run_dir / "manifest.json", manifest)
    try:
        from dashboard import generate_dashboard
        manifest["dashboard"] = str(generate_dashboard(outdir, Path("dashboard.html")))
        write_json(run_dir / "manifest.json", manifest)
    except Exception as exc:
        manifest["dashboard_error"] = str(exc)
        write_json(run_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Retrieve Eversource smart-meter data via the embedded Opower DataBrowser API.")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--out", default="data")
    ap.add_argument("--cookie-file", default=".eversource_cookies.txt", help="Netscape cookie jar to read/write")
    ap.add_argument("--cookieinfo", default="cookieinfo.txt", help="Safari/Chrome copied cookie table to convert if cookie jar is absent")
    ap.add_argument("--login", action="store_true", help="Only perform credential login and verify DataBrowser token extraction")
    ap.add_argument("--interval", help="Override Opower time interval, e.g. 2026-06-03T00:00:00-04:00/2026-06-06T00:00:00-04:00")
    ap.add_argument("--resolution", help="Override read resolution: QUARTER_HOUR, HALF_HOUR, HOUR, DAY")
    ap.add_argument("--all-service-points", action="store_true", help="Collect every service point under the active agreement instead of only --account")
    ap.add_argument("--green-button", action="store_true", help="Generate and parse a Green Button export for verification/backfill; use sparingly, not every poll")
    ap.add_argument("--poll", type=int, metavar="SECONDS", help="Poll forever; useful for near-real-time retrieval of newly published intervals")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        return run_tests()

    outdir = Path(args.out)
    client = EversourceClient(cookie_file=Path(args.cookie_file), cookieinfo=Path(args.cookieinfo))
    if args.login:
        result = client.login()
        client.load_databrowser()
        print(json.dumps({
            "login_status": result.get("status"),
            "is_success": result.get("IsSuccess"),
            "opower_token_chars": len(client.access_token or ""),
            "entity_id": client.entity_id,
            "cookie_file": str(Path(args.cookie_file).resolve()),
        }, indent=2, sort_keys=True))
        return 0
    while True:
        manifest = collect_once(client, outdir, args.account, args.interval, args.resolution, args.all_service_points, args.green_button)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        if not args.poll:
            break
        time.sleep(args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
