#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import html as html_lib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.cookiejar import MozillaCookieJar
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, Iterable

from queries import QUERIES
from runtime import (
    RETRYABLE_HTTP, LocalError, LockBusy, UpstreamError, atomic_destination, atomic_json,
    is_transient, make_private_directory, process_lock, require_private_directory, retry_delay, safe_error, state_directory,
)

EVERSOURCE_DATABROWSER_URL = "https://www.eversource.com/cg/customer/UsageHistory/DataBrowser"
OPOWER_GRAPHQL_URL = "https://ever.opower.com/ei/edge/apis/dsm-graphql-v1/cws/graphql"

LOGIN_URL = "https://www.eversource.com/security/account/login"
MSLOGIN_URL = "https://www.eversource.com/security/account/MSLogin"
RETURN_URL = "/cg/customer/UsageHistory/DataBrowser"
OKTA_OPOWER_CLIENT_ID = "0oail6c2lbhNdffx71t7"
OKTA_OPOWER_ISSUER = "https://eversource-external.okta.com/oauth2/default"
OKTA_OPOWER_REDIRECT_URI = "https://www.eversource.com/cg/customer/accountoverview"


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def multipart_form(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----EversmartBoundary{int(time.time() * 1000000)}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def catalog_query(name: str, fallback: str | None = None) -> str:
    query = QUERIES.get(name) or fallback
    if not query:
        raise RuntimeError(f"GraphQL query {name!r} was not bundled")
    return query


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
    entity_id: str | None
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


def select_target(targets: list[Target], account: str | None) -> Target:
    matches = [target for target in targets if target.account and (not account or target.account == account)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LocalError("No matching service point was found in authenticated metadata.")
    raise LocalError("Service point selection is ambiguous; specify an unambiguous --account or EVERSMART_ACCOUNT.")


class EversourceClient:
    def __init__(self, cookie_file: Path | None = None, cookieinfo: Path | None = None, *,
                 state_dir: Path | None = None, entity_id: str | None = None) -> None:
        state = state_directory(state_dir)
        self.cookie_file = Path(cookie_file) if cookie_file is not None else state / ".eversource_cookies.txt"
        self.cookieinfo = Path(cookieinfo) if cookieinfo is not None else state / "cookieinfo.txt"
        self.config_file = state / ".env"
        self.configured_entity_id = entity_id or os.environ.get("EVERSMART_ENTITY_ID")
        if not self.cookie_file.exists() and self.cookieinfo.exists():
            self.convert_cookieinfo()
        self.jar = MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            self.jar.load(ignore_discard=True, ignore_expires=True)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.access_token: str | None = None
        self.entity_id: str | None = self.configured_entity_id

    def convert_cookieinfo(self) -> None:
        from cookie_convert import parse_cookieinfo
        make_private_directory(self.cookie_file.parent)
        with atomic_destination(self.cookie_file) as staged:
            parse_cookieinfo(self.cookieinfo, staged)

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
        make_private_directory(self.cookie_file.parent)
        with atomic_destination(self.cookie_file) as staged:
            self.jar.save(str(staged), ignore_discard=True, ignore_expires=True)
        return out

    def reset_session(self) -> None:
        self.access_token = None
        # The authenticated page's unambiguous entity selection survives refresh.
        self.jar = MozillaCookieJar(str(self.cookie_file))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        try:
            self.cookie_file.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _is_invalid_webid_password(result: dict[str, Any]) -> bool:
        errors = result.get("Errors") if isinstance(result, dict) else None
        text = json.dumps(errors or result, sort_keys=True, default=str).lower()
        return "invalidwebidpassword" in text or "password reset" in text or "caps lock" in text

    def _credential_sources(self) -> list[tuple[str, str | None, str | None]]:
        sources = [("environment", os.environ.get("EVERSOURCE_LOGIN"), os.environ.get("EVERSOURCE_PASS"))]
        dotenv = read_dotenv(self.config_file)
        if dotenv:
            env_pair = (sources[0][1], sources[0][2])
            dotenv_pair = (dotenv.get("EVERSOURCE_LOGIN"), dotenv.get("EVERSOURCE_PASS"))
            if dotenv_pair != env_pair:
                sources.append((".env", dotenv_pair[0], dotenv_pair[1]))
        return sources

    def _login_with_credentials(self, login_url: str, return_url: str, web_id: str, password: str) -> dict[str, Any]:
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
        return json.loads(raw.decode("utf-8", "replace"))

    def login(self, return_url: str = RETURN_URL, force_refresh: bool = False) -> dict[str, Any]:
        if force_refresh:
            self.reset_session()
        login_url = f"{LOGIN_URL}?ReturnUrl={urllib.parse.quote(return_url)}"
        sources = [(name, web_id, password) for name, web_id, password in self._credential_sources() if web_id and password]
        if not sources:
            raise LocalError("Set EVERSOURCE_LOGIN and EVERSOURCE_PASS in the environment or the private state .env file.")
        failures: list[tuple[str, dict[str, Any]]] = []
        for name, web_id, password in sources:
            result = self._login_with_credentials(login_url, return_url, web_id or "", password or "")
            if result.get("IsSuccess"):
                if name == ".env":
                    os.environ["EVERSOURCE_LOGIN"] = web_id or ""
                    os.environ["EVERSOURCE_PASS"] = password or ""
                return result
            failures.append((name, result))
            if not self._is_invalid_webid_password(result):
                break
        detail = {name: (result.get("Errors") or result) for name, result in failures}
        raise RuntimeError(f"Eversource login failed: {json.dumps(detail, sort_keys=True)}")

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

    @staticmethod
    def _looks_like_auth_failure(detail: Any) -> bool:
        text = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True, default=str)
        text = text.lower()
        return any(token in text for token in (
            "unauthorized", "unauthenticated", "not authenticated", "authentication",
            "authorization", "forbidden", "invalid token", "expired token", "token expired",
            "jwt expired", "access token", "bearer",
        ))

    def _refresh_opower_session(self) -> None:
        self.access_token = None
        try:
            self.obtain_fresh_opower_token(force_login=True)
        except Exception as exc:
            if is_transient(exc):
                raise
            self.load_databrowser()

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
            with exc:
                location = exc.headers.get("Location") if exc.headers else None
                if not location:
                    raise UpstreamError(f"Okta token exchange failed with HTTP {exc.code}",
                                        status=exc.code, retry_after=retry_delay(exc)) from exc
        fragment = urllib.parse.urlparse(location).fragment
        token = (urllib.parse.parse_qs(fragment).get("access_token") or [None])[0]
        if not token:
            raise RuntimeError("Okta token exchange completed but no access_token was present in redirect fragment.")
        self.access_token = token
        return token

    def load_databrowser(self) -> str:
        html = self.request(EVERSOURCE_DATABROWSER_URL).decode("utf-8", "replace")
        if "App-LogIn" in html or "/security/account/login" in html[:20000]:
            self.login()
            html = self.request(EVERSOURCE_DATABROWSER_URL).decode("utf-8", "replace")
        if "App-LogIn" in html or "/security/account/login" in html[:20000]:
            raise RuntimeError("Eversource session is not authenticated after login; refresh cookieinfo.txt or check MFA state.")
        token_match = re.search(r"accessToken:\s*'([^']+)'", html)
        selections = re.findall(r"setEntityIds\(\s*\[([^]]*)\]\s*\)", html)
        single = [re.fullmatch(r"\s*(['\"])([^'\"]+)\1\s*", value) for value in selections]
        entities = {m.group(2) for m in single if m}
        if token_match:
            self.access_token = token_match.group(1)
        else:
            self.access_token = None
        self.entity_id = self.configured_entity_id or (
            next(iter(entities)) if len(entities) == 1 and all(single) else None
        )
        if not self.access_token or self._is_token_expired_or_stale(self.access_token):
            self.obtain_fresh_opower_token(force_login=True)
        return html

    def graphql(self, query: str, variables: dict[str, Any], entity_id: str | None = None, _token_retry: bool = True,
                *, retry_transient: bool = True) -> dict[str, Any]:
        if not self.access_token:
            self.load_databrowser()
        selected = self.configured_entity_id or entity_id or self.entity_id
        if not selected:
            raise LocalError("No unambiguous entity was discovered; set --entity-id or EVERSMART_ENTITY_ID.")
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
        attempts = 4 if retry_transient else 1
        sleep = getattr(self, "_sleep", time.sleep)
        for attempt in range(attempts):
            try:
                raw = self.request(OPOWER_GRAPHQL_URL, body, headers)
                data = json.loads(raw.decode())
                if data.get("errors"):
                    if _token_retry and self._looks_like_auth_failure(data["errors"]):
                        self._refresh_opower_session()
                        return self.graphql(query, variables, entity_id, _token_retry=False, retry_transient=retry_transient)
                    raise UpstreamError(json.dumps(data["errors"], indent=2))
                return data
            except urllib.error.HTTPError as exc:
                retry_after = retry_delay(exc)
                if _token_retry and exc.code in (401, 403):
                    exc.close()
                    self._refresh_opower_session()
                    return self.graphql(query, variables, entity_id, _token_retry=False, retry_transient=retry_transient)
                if exc.code in RETRYABLE_HTTP and attempt < attempts - 1 and retry_after <= 60:
                    exc.close()
                    sleep(max(2 ** attempt, retry_after))
                    continue
                try:
                    with exc:
                        detail = exc.read(4096).decode("utf-8", "replace") if exc.fp is not None else ""
                except OSError:
                    detail = ""
                raise UpstreamError(f"Opower GraphQL HTTP {exc.code}: {detail}",
                                    status=exc.code, retry_after=retry_after) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < attempts - 1:
                    sleep(min(2 ** attempt, 8))
                    continue
                raise UpstreamError(f"Opower GraphQL transient network error: {exc}", network=True) from exc
        raise AssertionError("GraphQL retry loop fell through")

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
                    entity_id=self.configured_entity_id or acct.get("utilityId") or self.entity_id,
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

    def find_target(self, metadata: dict[str, Any], account: str | None = None) -> Target:
        return select_target(self.service_points(metadata), account or os.environ.get("EVERSMART_ACCOUNT"))

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
        # A timeout or gateway failure may occur after Opower starts the export job.
        data = self.graphql(catalog_query("WUE_GenerateUsageExportFile"), variables, target.entity_id,
                            retry_transient=False)
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
    os.chmod(path, 0o600)


def write_json(path: Path, obj: Any) -> None:
    atomic_json(path, obj)


def write_raw(path: Path, obj: Any) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(obj, stream, separators=(",", ":"), ensure_ascii=True)
    path.chmod(0o600)


def _response_account(doc) -> dict[str, Any]:
    data = doc.get("data") if isinstance(doc, dict) else None
    account = data.get("billingAccountByAuthContext") if isinstance(data, dict) else None
    if not isinstance(account, dict) or doc.get("errors"):
        raise UpstreamError("Opower returned an incomplete account response.")
    return account


def _response_nodes(parent, connection):
    container = parent.get(connection)
    edges = container.get("edges") if isinstance(container, dict) else None
    if not isinstance(edges, list):
        raise UpstreamError("Opower returned an incomplete connection.")
    return [edge["node"] for edge in edges if isinstance(edge, dict) and isinstance(edge.get("node"), dict)]


_USAGE_STREAMS = ("netUsage", "energyDelivered", "energyReceived", "demand")


def _point_response(doc, target: Target, stream_names: tuple[str, ...]) -> dict[str, Any]:
    account = _response_account(doc)
    # Some queries include billing-account IDs; when returned, they must also match.
    for field, expected in (("uuid", target.billing_account_uuid), ("urn", target.billing_account_urn)):
        if expected and field in account and account[field] != expected:
            raise UpstreamError("Opower returned a different billing account.")
    agreements = _response_nodes(account, "serviceAgreementsConnection")
    matches = [(sa, sp) for sa in agreements if sa.get("uuid") == target.service_agreement_uuid
               for sp in _response_nodes(sa, "servicePointsConnection")
               if sp.get("uuid") == target.service_point_uuid and sp.get("utilityId") == target.account]
    if len(matches) != 1:
        raise UpstreamError("Opower did not identify the requested service point unambiguously.")
    sa, sp = matches[0]
    streams = sp.get("readStreams")
    if not isinstance(streams, dict) or not any(name in streams for name in stream_names):
        raise UpstreamError("Opower omitted the requested read streams.")
    for name in stream_names:
        entries = streams.get(name)
        if entries is None:  # Nullable stream, or this query did not request it.
            continue
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("reads"), list)
            or any(not isinstance(read, dict) for read in entry["reads"]) for entry in entries
        ):
            raise UpstreamError("Opower returned a malformed read stream.")
    # Keep the original response for audit; the CSV and history use only the requested node.
    return {"data": {"billingAccountByAuthContext": {"serviceAgreementsConnection": {"edges": [
        {"node": {**sa, "servicePointsConnection": {"edges": [{"node": sp}]}}}
    ]}}}}


def _required_usage(client, target: Target, interval: str, resolution: str) -> tuple[dict, list[dict]]:
    from math import isfinite
    from history import _parse_window, _zone

    doc = client.usage(target, interval, resolution)
    scoped = _point_response(doc, target, _USAGE_STREAMS)
    zone = _zone(target.timezone)
    rows = []
    saw_padding = saw_requested = False
    try:
        requested_start, requested_end = _parse_window(interval)
        for row in iter_stream_reads(scoped):
            start, end = _parse_window(row["timeInterval"], zone)
            if end <= requested_start or start >= requested_end:
                # Opower can pad the requested range at either end. Keep the
                # raw response, but never count those slots toward this poll.
                saw_padding = True
                continue
            saw_requested = True
            value = row["value"]
            if value is not None:
                if isinstance(value, bool) or not isfinite(float(value)):
                    raise ValueError("invalid measurement")
                rows.append(row)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise UpstreamError("Opower returned a malformed usage reading.") from exc
    if saw_padding and not saw_requested:
        raise UpstreamError("Opower returned only out-of-window usage readings.")
    return doc, rows


def write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)


def write_optional_error(run_dir: Path, name: str, exc: Exception, errors: dict[str, str]) -> None:
    errors[name] = safe_error(exc, "Optional upstream request failed; details withheld.")
    path = run_dir / f"{name}_error.txt"
    path.write_text(errors[name])
    os.chmod(path, 0o600)


def write_optional_warning(run_dir: Path, name: str, exc: Exception, warnings: dict[str, str]) -> None:
    warnings[name] = safe_error(exc, "Optional upstream request unavailable; details withheld.")
    path = run_dir / f"{name}_warning.txt"
    path.write_text(warnings[name])
    os.chmod(path, 0o600)


def is_transient_upstream_error(exc: Exception) -> bool:
    return is_transient(exc)


def write_noncritical_issue(run_dir: Path, name: str, exc: Exception, errors: dict[str, str], warnings: dict[str, str], *, transient_as_warning: bool = True) -> None:
    if transient_as_warning and is_transient_upstream_error(exc):
        write_optional_warning(run_dir, name, exc, warnings)
    else:
        write_optional_error(run_dir, name, exc, errors)


def _parse_opower_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_opower_datetime(value: datetime) -> str:
    text = value.isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def cap_interval_hours(interval: str | None, max_hours: int = 720) -> tuple[str | None, str | None]:
    """Return an Opower-safe interval capped to max_hours, preserving the original end.

    Opower advertises register availableReadsTimeInterval values that can grow beyond
    the 720-hour limit accepted by quarter-hour/hour read-stream GraphQL calls. The
    dashboard update should therefore retrieve the latest safe window by default
    instead of failing once the meter has more than 30 days of AMI history.
    """
    if not interval or "/" not in interval:
        return interval, None
    start_s, end_s = interval.split("/", 1)
    start = _parse_opower_datetime(start_s)
    end = _parse_opower_datetime(end_s)
    if not start or not end or end <= start:
        return interval, None
    max_delta = timedelta(hours=max_hours)
    if end - start <= max_delta:
        return interval, None
    capped_start = end - max_delta
    capped = f"{_format_opower_datetime(capped_start)}/{_format_opower_datetime(end)}"
    return capped, f"Capped Opower read interval from {interval} to {capped}; read-stream APIs reject ranges over {max_hours} hours."


def run_tests() -> int:
    return subprocess.call([sys.executable, "-m", "unittest", "discover", "-v"])


def chunk_interval(interval: str) -> list[str]:
    parts = interval.split("/")
    if len(parts) != 2:
        raise LocalError("Expected a start/end interval with increasing ISO timestamps and UTC offsets.")
    start, end = map(_parse_opower_datetime, parts)
    if start is None or end is None or start.tzinfo is None or end.tzinfo is None or end <= start:
        raise LocalError("Expected a start/end interval with increasing ISO timestamps and UTC offsets.")
    windows = []
    while start < end:
        stop = start + timedelta(hours=720) if end - start > timedelta(hours=720) else end
        windows.append(f"{_format_opower_datetime(start)}/{_format_opower_datetime(stop)}")
        start = stop
    return windows


def publish_dashboard(outdir: Path, output: Path, *, db_path: Path, account: str | None,
                      shareable: bool = False, manual_bill_dir: Path | None = None,
                      manual_bill_account: str | None = None, renderer=None) -> Path:
    if renderer is None:
        from dashboard import generate_dashboard
        renderer = generate_dashboard
    params = signature(renderer).parameters
    accepts_keywords = any(param.kind == Parameter.VAR_KEYWORD for param in params.values())
    options: dict[str, Any] = {"db_path": db_path, "account": account, "shareable": shareable}
    if "split_data" in params or accepts_keywords:
        options["split_data"] = False
    if manual_bill_dir is not None and ("manual_bill_dir" in params or accepts_keywords):
        options["manual_bill_dir"] = manual_bill_dir
    if manual_bill_account is not None and ("manual_bill_account" in params or accepts_keywords):
        options["manual_bill_account"] = manual_bill_account
    make_private_directory(output.parent)
    with atomic_destination(output, replace=not shareable) as staged:
        renderer(outdir, staged, **options)
        if not staged.stat().st_size:
            raise LocalError("Dashboard renderer did not produce an artifact.")
    return output.resolve()


def _history_account(db_path: Path, account: str | None) -> str | None:
    if not db_path.is_file():
        if account:
            raise LocalError("No imported history for the selected service point.")
        return None
    from history import History

    with History(db_path) as store:
        if account:
            if store.latest_manifest(account) is None:
                raise LocalError("No imported history for the selected service point.")
            return account
        accounts = {record.get("account") or record.get("utility_id") for record in store.manifests()}
    accounts.discard(None)
    if len(accounts) > 1:
        raise LocalError("History has multiple accounts; specify --account or EVERSMART_ACCOUNT.")
    return next(iter(accounts), None)


_AUX_DATASETS = ("cost", "pricing", "demand_maxima", "weather")
_AUX_WINDOWS_PER_DATASET = 4
_MAX_READ_WINDOW = timedelta(hours=720)
_ReadWindow = tuple[datetime, datetime]
_AuxKey = tuple[str, str, str, str, str]


def _ledger_window(text) -> _ReadWindow:
    start, end = map(datetime.fromisoformat, text.split("/"))
    if start.utcoffset() is None or end.utcoffset() is None or start >= end or end - start > _MAX_READ_WINDOW:
        raise ValueError("invalid archived auxiliary window")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _window_text(pair: _ReadWindow) -> str:
    return "/".join(_format_opower_datetime(t) for t in pair)


def _merge_aux_windows(windows: Iterable[_ReadWindow]) -> list[_ReadWindow]:
    merged: list[_ReadWindow] = []
    for start, end in sorted(windows):
        if merged:
            low, high = merged[-1]
            if end <= high:
                continue
            if start <= high:
                if end - low <= _MAX_READ_WINDOW:
                    merged[-1] = low, end
                    continue
                # Split on an existing request boundary, not an arbitrary UTC
                # hour (daily requests can cross a DST transition).
                start = high
        merged.append((start, end))
    return merged


def _subtract_aux_windows(windows: list[_ReadWindow], repaired: list[_ReadWindow]) -> list[_ReadWindow]:
    remaining = []
    repairs = iter(_merge_aux_windows(repaired))
    done = next(repairs, None)
    for start, end in _merge_aux_windows(windows):
        while done and done[0] < end:
            if done[0] > start:
                remaining.append((start, done[0]))
            start = max(start, done[1])
            if done[1] >= end:
                break
            done = next(repairs, None)
        if start < end:
            remaining.append((start, end))
    return remaining


def _aux_ledger(records: list[dict]) -> tuple[dict[_AuxKey, list[_ReadWindow]], dict[_AuxKey, list[_ReadWindow]],
                                             dict[_AuxKey, datetime]]:
    """Replay window obligations and completions from finalized audit manifests."""
    pending: dict[_AuxKey, list[_ReadWindow]] = {}
    complete: dict[_AuxKey, list[_ReadWindow]] = {}
    cursors: dict[_AuxKey, datetime] = {}
    for record in records:
        entries = record.get("auxiliary_updates")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            try:
                account, agreement, point, resolution, dataset = entry["target"]
                if (not all(isinstance(value, str) for value in (account, agreement, point, resolution, dataset))
                        or dataset not in _AUX_DATASETS):
                    continue
                key = account, agreement, point, resolution, dataset
                due = [_ledger_window(text) for text in entry["due"]]
                repaired = [_ledger_window(text) for text in entry["repaired"]]
                if entry.get("cursor"):
                    cursors[key] = _ledger_window(entry["cursor"])[0]
            except (AttributeError, TypeError, KeyError, ValueError, OverflowError):
                continue
            confirmed = _merge_aux_windows([*complete.get(key, ()), *repaired])
            if confirmed:
                complete[key] = confirmed
            # A failed overlap refresh does not invalidate an older successful
            # response. Keep the new tail pending without retrying the good part.
            remaining = _subtract_aux_windows([*pending.get(key, ()), *due], confirmed)
            if remaining:
                pending[key] = remaining
            else:
                pending.pop(key, None)
                cursors.pop(key, None)
    return pending, complete, cursors


def _aux_rows(dataset: str, doc, target: Target, parser):
    from history import _parse_window, _zone

    if dataset == "weather":
        premises = _response_nodes(_response_account(doc), "premisesConnection")
        matches = [p for p in premises if not target.premise_uuid or p.get("uuid") == target.premise_uuid]
        if len(matches) != 1 or not isinstance(matches[0].get("weather"), list):
            raise UpstreamError("Opower omitted the requested premise weather response.")
        doc = {"data": {"billingAccountByAuthContext": {"premisesConnection": {"edges": [{"node": matches[0]}]}}}}
    else:
        doc = _point_response(doc, target, ("demand",) if dataset == "demand_maxima" else _USAGE_STREAMS)
    rows = list(parser(doc))
    zone = _zone(target.timezone)
    for row in rows:
        _parse_window(row["timeInterval"], zone, date_ok=dataset == "weather")
    return rows


def _harvest(client, run_dir: Path, published: Path, account: str | None, interval: str | None,
             resolution: str | None, all_service_points: bool, green_button: bool, history,
             backfill: bool, overlap_hours: int, *, retrieved: datetime, records: list[dict],
             archive_failures: int) -> dict[str, Any]:
    from history import _resolution, plan_explicit_windows, plan_windows

    explicit = interval is not None
    metadata = client.metadata()
    targets = client.service_points(metadata)
    primary = select_target(targets, account or os.environ.get("EVERSMART_ACCOUNT"))
    selected = targets if all_service_points else [primary]
    write_raw(run_dir / "metadata.json.gz", metadata)
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    if archive_failures:
        warnings["archive_recovery"] = f"Archive recovery skipped {archive_failures} invalid or unreadable run(s)."
    usage_rows: list[dict[str, Any]] = []
    primary_usage_rows = 0
    cost_rows: list[dict[str, Any]] = []
    pricing_rows: list[dict[str, Any]] = []
    demand_rows: list[dict[str, Any]] = []
    weather_rows: list[dict[str, Any]] = []
    bill_rows: list[dict[str, Any]] = []
    service_point_rows = [asdict(t) for t in targets]

    plans: dict[str, list[str]] = {}
    work: list[tuple[Target, str, str, list[str]]] = []
    filenames: set[str] = set()
    for target in selected:
        if not target.account or target.account in plans or not target.service_agreement_uuid or not target.service_point_uuid:
            raise LocalError("Authenticated metadata did not identify unique service points.")
        actual_resolution = resolution or target.resolution
        if not actual_resolution:
            raise LocalError("The selected service point did not advertise a read resolution.")
        if not explicit:
            windows = plan_windows(target.available_interval, actual_resolution,
                                   history.coverage(target.account, actual_resolution),
                                   backfill=backfill, overlap_hours=overlap_hours, timezone=target.timezone)
        else:
            try:
                windows = plan_explicit_windows(interval, actual_resolution, timezone=target.timezone)
            except (ValueError, OverflowError):
                raise LocalError("Invalid interval for the requested read resolution and meter timezone.") from None
            if not windows:
                raise LocalError("The requested interval contains no complete meter-local days.")
        plans[target.account] = windows
        # Never allow upstream identifiers to escape the private staging directory.
        suffix = re.sub(r"[^a-zA-Z0-9_-]", "_", target.account).replace("-", "_")
        while suffix in filenames:
            suffix += "_"
        filenames.add(suffix)
        work.append((target, actual_resolution, suffix, windows))
        for index, actual_interval in enumerate(windows, 1):
            raw_suffix = suffix if len(windows) == 1 else f"{suffix}.{index:04d}"
            usage, rows = _required_usage(client, target, actual_interval, actual_resolution)
            usage_rows.extend(rows)
            if target is primary:
                primary_usage_rows += len(rows)
            write_raw(run_dir / f"usage_{raw_suffix}.json.gz", usage)

    cooldown = 0.0

    def optional(name, fetch, *, issue=None, sink=None, parser=None, warning=False, transient_as_warning=False) -> bool:
        nonlocal cooldown
        if cooldown:
            return False  # Respect cooldown before making any additional upstream calls.
        try:
            doc = fetch()
        except Exception as exc:
            cooldown = retry_delay(exc)
            record_optional_issue(issue or name, exc, warning, transient_as_warning)
            return False
        write_raw(run_dir / f"{name}.json.gz", doc)
        if sink is not None and parser is not None:
            try:
                rows = list(parser(doc))
            except Exception as exc:
                record_optional_issue(issue or name, exc, warning, transient_as_warning)
                return False
            else:
                sink.extend(rows)
        return True

    def record_optional_issue(name, exc, warning, transient_as_warning):
        if warning:
            write_optional_warning(run_dir, name, exc, warnings)
        else:
            write_noncritical_issue(run_dir, name, exc, errors, warnings,
                                    transient_as_warning=transient_as_warning)

    pending, complete, cursors = _aux_ledger(records)
    updates = []
    endpoints = {
        "cost": (cost_rows, iter_stream_reads), "pricing": (pricing_rows, iter_pricing_components),
        "demand_maxima": (demand_rows, iter_demand_maxima), "weather": (weather_rows, iter_weather_rows),
    }
    for target, actual_resolution, suffix, windows in work:
        identity = target.account, target.service_agreement_uuid, target.service_point_uuid
        fresh = [_ledger_window(window) for window in windows]
        for dataset in _AUX_DATASETS:
            cadence = _resolution(actual_resolution) if dataset in ("cost", "pricing") else ""
            key = (*identity, cadence, dataset)
            keys: list[_AuxKey] = [key]
            if not explicit:
                # Do not abandon an older pending cadence when --resolution changes.
                keys.extend(sorted(k for k in pending if k != key and k[:3] == identity and k[-1] == dataset))
            sink, parser = endpoints[dataset]
            budget = _AUX_WINDOWS_PER_DATASET
            number = 0
            for item in keys:
                due = fresh if item == key else []
                existing = pending.get(item, []) if not explicit else []
                planned = _merge_aux_windows([*existing, *due]) if existing else due
                cursor = cursors.get(item)
                if cursor is not None and not explicit and len(planned) > budget:
                    planned.sort(key=lambda pair, after=cursor: (pair[0] <= after, pair[0]))
                requests = planned if explicit else planned[:budget]
                repaired = []
                last_attempt = None
                for request_index, pair in enumerate(requests):
                    if cooldown:
                        break
                    raw_interval = windows[request_index] if item == key and not existing else _window_text(pair)
                    number += 1
                    raw_suffix = suffix if len(keys) == len(requests) == 1 else f"{suffix}.{number:04d}"

                    def fetch():
                        method = getattr(client, dataset)
                        if cadence:
                            read_resolution = actual_resolution if item == key else item[3]
                            return method(target, raw_interval, read_resolution)
                        return method(target, raw_interval)

                    last_attempt = _window_text(pair)
                    if optional(f"{dataset}_{raw_suffix}", fetch, issue=f"{dataset}_{suffix}", sink=sink,
                                parser=lambda doc: _aux_rows(dataset, doc, target, parser)):
                        repaired.append(last_attempt)
                budget -= len(requests)
                if due or last_attempt:
                    completed = complete.get(item, [])
                    gaps = _subtract_aux_windows(due, completed) if completed else due
                    new_repairs = _subtract_aux_windows([_ledger_window(text) for text in repaired], completed)
                    if gaps or new_repairs or (existing and last_attempt):
                        updates.append({"target": list(item), "due": [_window_text(pair) for pair in gaps],
                                        "repaired": [_window_text(pair) for pair in new_repairs], "cursor": last_attempt})

    optional("bills", lambda: client.bills(primary), sink=bill_rows, parser=iter_bill_segments)
    optional("bill_forecast", lambda: client.forecast(primary), warning=True)
    optional("rate_plan", lambda: client.rate_static_content(primary), transient_as_warning=True)

    gb_rows: list[dict[str, Any]] = []
    gb_manifest: dict[str, Any] | None = None
    if green_button and not cooldown:
        try:
            job_uuid = client.generate_usage_export(primary, interval or primary.available_interval)
            job = None
            for _ in range(10):
                job = client.export_job(primary, job_uuid).get("data", {}).get("exportJob")
                if job and job.get("isFinished"):
                    break
                if job and job.get("isFailed"):
                    raise LocalError("Green Button export failed.")
                getattr(client, "_sleep", time.sleep)(2)
            if not job or not job.get("isFinished") or not job.get("result"):
                raise LocalError("Green Button export did not finish promptly.")
            blob = client.download_url(job["result"])
            write_bytes(run_dir / "green_button.zip", blob)
            gb_rows = parse_green_button_zip(blob)
            gb_manifest = {"job_uuid": job_uuid, "bytes": len(blob), "row_count": len(gb_rows), "result_url_present": True}
            write_json(run_dir / "green_button_manifest.json", gb_manifest)
        except Exception as exc:
            cooldown = retry_delay(exc)
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

    primary_windows = plans[primary.account]
    return {
        "archive_format": 1,
        "retrieved_at_utc": retrieved.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "poll_status": "collected" if primary_usage_rows else "no_op",
        "success": bool(primary_usage_rows),
        "account": primary.account,
        "entity_id": primary.entity_id,
        "service_agreement_uuid": primary.service_agreement_uuid,
        "service_point_uuid": primary.service_point_uuid,
        "available_interval": primary.available_interval,
        "latest_available_end": (primary.available_interval or "/").split("/")[-1],
        "collection_interval": primary_windows[0] if len(selected) == len(primary_windows) == 1 else None,
        "collection_windows": plans,
        "available_bill_interval": primary.available_bill_interval,
        "requested_interval": interval or primary.available_interval,
        "resolution": resolution or primary.resolution,
        "timezone": primary.timezone,
        "target_count": len(selected),
        "service_points": service_point_rows,
        "usage_rows": len(usage_rows), "primary_usage_rows": primary_usage_rows,
        "cost_rows": len(cost_rows), "pricing_rows": len(pricing_rows),
        "weather_rows": len(weather_rows), "bill_rows": len(bill_rows), "demand_maxima_rows": len(demand_rows),
        "green_button_rows": len(gb_rows), "green_button": gb_manifest,
        "errors": errors, "warnings": warnings, "cost_error": errors.get(f"cost_{primary.account.replace('-', '_')}"),
        "output_dir": str(published.resolve()),
        "retry_after_seconds": cooldown,
        "retry_not_before_utc": _format_opower_datetime(datetime.now(timezone.utc) + timedelta(seconds=cooldown)) if cooldown else None,
        "auxiliary_updates": updates,
        "archive_failures": archive_failures,
    }


def _archived_time(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", text):
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        value = datetime.fromisoformat(text)
        return value.astimezone(timezone.utc) if value.utcoffset() is not None else None
    except (OverflowError, ValueError):
        return None


def _last_required_result(records: list[dict], account: str) -> str | None:
    # Pre-existing manifests used `collected` even for empty responses. Do not
    # infer a new success from them without a positive reading count.
    for record in reversed(records):
        if (record.get("account") or record.get("utility_id")) != account:
            continue
        count = record.get("primary_usage_rows", record.get("usage_rows"))
        if (isinstance(count, int) and count > 0 and record.get("success") is not False
                and record.get("poll_status", "collected") == "collected"
                and _archived_time(record.get("retrieved_at_utc")) is not None):
            return record["retrieved_at_utc"]
    return None


def collect_once(client: EversourceClient, outdir: Path, account: str | None, interval: str | None,
                 resolution: str | None, all_service_points: bool = False, green_button: bool = False,
                 *, db_path: Path | None = None, history=None, output: Path | None = None,
                 renderer=None, manual_bill_dir: Path | None = None,
                 manual_bill_account: str | None = None, backfill: bool = False,
                 overlap_hours: int = 72) -> dict[str, Any]:
    """Harvest to a private staging directory, commit the audit run, then ingest and render."""
    outdir = Path(outdir)
    db_path = Path(db_path) if db_path is not None else outdir / "history.sqlite"
    output = Path(output) if output is not None else outdir / "dashboard.html"
    make_private_directory(outdir)
    make_private_directory(db_path.parent)
    with process_lock(db_path.with_suffix(db_path.suffix + ".lock")), process_lock(outdir / ".collect.lock"):
        if history is None:
            from history import History
            history_context = History(db_path)
        else:
            history_context = nullcontext(history)
        with history_context as store:
            if history is None:
                # Also recovers a completed audit run left by a crash or failed ingestion.
                store.ingest_archive(outdir)
            archive_failures = getattr(store, "archive_failures", 0)
            manifests = getattr(store, "manifests", None)
            records = manifests() if manifests else []
            now = datetime.now(timezone.utc)
            latest = max((stamp for record in records if (stamp := _archived_time(record.get("retrieved_at_utc")))
                          is not None), default=None)
            retry_at = max((stamp for record in records if (stamp := _archived_time(record.get("retry_not_before_utc")))
                            is not None), default=None)
            if retry_at and retry_at > now:
                return {"poll_status": "deferred", "dashboard_state": "retained",
                        "retry_after_seconds": (retry_at - now).total_seconds(), "archive_failures": archive_failures}
            retrieved = max(now, latest + timedelta(microseconds=1)) if latest is not None else now
            with tempfile.TemporaryDirectory(prefix=".collect-", dir=outdir) as directory:
                staging = Path(directory)
                # Keep the staged manifest out of the archive's */manifest.json namespace.
                run_dir = staging / "run"
                make_private_directory(run_dir)
                stamp = retrieved.strftime("%Y%m%dT%H%M%SZ")
                published = outdir / stamp
                # Keep the legacy timestamp prefix without overwriting same-second runs.
                if published.exists():
                    published = outdir / f"{stamp}.{staging.name.removeprefix('.collect-')}"
                manifest = _harvest(client, run_dir, published, account, interval, resolution,
                                    all_service_points, green_button, store, backfill, overlap_hours,
                                    retrieved=retrieved, records=records, archive_failures=archive_failures)
                write_json(run_dir / "manifest.json", manifest)
                run_dir.rename(published)
            # The archive is append-only, so an ingestion failure does not lose data.
            if not store.ingest_run(published):
                raise LocalError("History did not ingest the completed audit run; dashboard retained.")
            previous_success = _last_required_result(records, manifest["account"])
            # A legacy/imported archive might have readings without a row count
            # on its manifest. This allows a repair to render from that history.
            has_history = bool(previous_success or (not manifest["primary_usage_rows"]
                               and manifests and store.rows("usage", manifest["account"])))
        result = {**manifest, "last_successful_poll_at_utc": (
            manifest["retrieved_at_utc"] if manifest["primary_usage_rows"] else previous_success
        ), "dashboard_state": "retained"}
        repaired = any(manifest[f"{dataset}_rows"] for dataset in _AUX_DATASETS)
        if not manifest["primary_usage_rows"] and not (has_history and (repaired or not output.is_file())):
            return result
        # Dashboard publication is not part of the immutable acquisition record.
        try:
            path = publish_dashboard(outdir, output, db_path=db_path, account=manifest["account"],
                                     manual_bill_dir=manual_bill_dir, manual_bill_account=manual_bill_account,
                                     renderer=renderer)
            result.update(dashboard=str(path), dashboard_state="published")
        except Exception as exc:
            result["dashboard_error"] = safe_error(exc, "Dashboard rendering failed; details withheld.")
        return result


def _report_status(state: Path, status: dict[str, Any], *, failed: bool = False, persist: bool = True) -> None:
    if persist:
        write_json(state / "status.json", {
            "updated_at_utc": datetime.now(timezone.utc).isoformat(), **status,
        })
    print(json.dumps(status, sort_keys=True), file=sys.stderr if failed else sys.stdout)


def _supervise(collect, state: Path, poll: int | None, sleep) -> int:
    failures = 0
    try:
        previous = json.loads((state / "status.json").read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    last_success = previous.get("last_successful_poll_at_utc")
    while True:
        cooldown = 0.0
        status: dict[str, Any]
        failed = False
        degraded = False
        try:
            result = collect()
            cooldown = result.get("retry_after_seconds", 0)
            degraded = bool(result.get("errors") or result.get("warnings"))
            failed = "dashboard_error" in result
            if "last_successful_poll_at_utc" in result:
                last_success = result["last_successful_poll_at_utc"]
            operation = result.get("poll_status", "collected")
            if failed:
                status = {"status": "render_failed", "error": result["dashboard_error"]}
            else:
                status = {"status": "degraded" if degraded and operation == "collected" else operation}
            if "dashboard_state" in result:
                status["dashboard"] = result["dashboard_state"]
            if degraded:
                status.update(optional_errors=len(result.get("errors", {})), optional_warnings=len(result.get("warnings", {})))
            if result.get("archive_failures"):
                status["archive_failures"] = result["archive_failures"]
            if cooldown:
                status["retry_after_seconds"] = cooldown
        except LockBusy:
            raise
        except Exception as exc:
            failed = True
            cooldown = retry_delay(exc)
            status = {"status": "collection_failed", "error": safe_error(exc, "Collection failed; details withheld.")}
        if last_success is not None:
            status["last_successful_poll_at_utc"] = last_success
        if poll is not None:
            failures = min(failures + 1, 8) if failed or degraded else 0
            status["next_attempt_in_seconds"] = max(poll, min(3600, poll * 2 ** failures), cooldown)
        try:
            _report_status(state, status, failed=failed)
        except OSError:
            # Writing the receipt is not the commit point for dashboard publication.
            status["status_write_error"] = "Unable to persist collection status."
            _report_status(state, status, failed=failed, persist=False)
        if poll is None:
            return int(failed)
        sleep(status["next_attempt_in_seconds"])


def main(argv: list[str] | None = None, *, client_factory=None, renderer=None, sleep=None) -> int:
    ap = argparse.ArgumentParser(description="Retrieve Eversource smart-meter data via the embedded Opower DataBrowser API.")
    ap.add_argument("--account", help="Service point utility ID (or EVERSMART_ACCOUNT)")
    ap.add_argument("--entity-id", help="Opower entity ID (or EVERSMART_ENTITY_ID)")
    ap.add_argument("--state-dir", type=Path, help="Private app state (EVERSMART_STATE_DIR or XDG state directory)")
    ap.add_argument("--out", type=Path, help="Audit run directory (defaults to STATE_DIR/data)")
    ap.add_argument("--cookie-file", type=Path, help="Override private Netscape cookie jar")
    ap.add_argument("--cookieinfo", type=Path, help="Override private copied cookie table to convert if jar is absent")
    ap.add_argument("--login", action="store_true", help="Only perform credential login and verify DataBrowser token extraction")
    ap.add_argument("--collect", action="store_true", help="Also collect when specifying an offline action")
    ap.add_argument("--import-archive", type=Path, metavar="PATH", help="Ingest existing audit runs without changing the archive")
    ap.add_argument("--render", action="store_true", help="Render an offline dashboard from local history")
    ap.add_argument("--shareable", type=Path, metavar="PATH", help="Export a redacted dashboard instead of updating the live view")
    ap.add_argument("--manual-bill-dir", type=Path, help="Override the private manual bill directory")
    ap.add_argument("--manual-bill-account", help="Service point to trust for otherwise unassigned manual bills")
    ap.add_argument("--backfill", action="store_true", help="Fetch gaps in the available history")
    ap.add_argument("--interval", help="Override Opower time interval, e.g. 2026-06-03T00:00:00-04:00/2026-06-06T00:00:00-04:00")
    ap.add_argument("--resolution", help="Override read resolution: QUARTER_HOUR, HALF_HOUR, HOUR, DAY")
    ap.add_argument("--all-service-points", action="store_true", help="Collect every service point under the active agreement instead of only --account")
    ap.add_argument("--green-button", action="store_true", help="Generate and parse a Green Button export for verification/backfill; use sparingly, not every poll")
    ap.add_argument("--poll", type=int, metavar="SECONDS", help="Poll forever; useful for near-real-time retrieval of newly published intervals")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args(argv)
    if args.test:
        return run_tests()
    if args.poll is not None and args.poll <= 0:
        ap.error("--poll must be positive")
    if args.poll is not None and args.green_button:
        ap.error("--green-button is a one-shot action and cannot run with --poll")
    if args.poll is not None and args.shareable:
        ap.error("--shareable is a one-shot export and cannot run with --poll")
    offline = bool(args.import_archive or args.render or args.shareable)
    collection_options = bool(args.poll or args.backfill or args.interval or args.green_button
                              or args.all_service_points or args.resolution)
    explicit_collection = bool(args.collect or collection_options)
    if args.login and (offline or explicit_collection):
        ap.error("--login cannot be combined with collection or offline actions")
    if offline and collection_options and not args.collect:
        ap.error("Use --collect to combine a collection option with an offline action")
    collect = not args.login and (explicit_collection or not offline)
    state = state_directory(args.state_dir)
    outdir = args.out or state / "data"
    db_path = state / "history.sqlite"
    output = state / "dashboard.html"
    manual_bill_dir = args.manual_bill_dir or state / "bills"
    if args.shareable:
        try:
            target = args.shareable.resolve()
            roots = (state, outdir, args.import_archive, manual_bill_dir)
            if any(target.is_relative_to(root.resolve()) for root in roots if root is not None):
                ap.error("--shareable must be outside the state, audit, import, and bills directories")
        except (OSError, RuntimeError):
            ap.error("Unable to resolve the shareable output or its private directory boundaries")
        if args.shareable.exists() or args.shareable.is_symlink():
            ap.error("The export destination already exists; choose a new file")
        args.shareable = target
    sleep = sleep or time.sleep
    stage = "startup"
    try:
        require_private_directory(state)
        with process_lock(state / "eversmart.lock"):
            try:
                # Account and entity arguments and environment win over private config.
                config = read_dotenv(state / ".env")
                manual_bill_account = (args.manual_bill_account or os.environ.get("EVERSMART_MANUAL_BILL_ACCOUNT")
                                       or config.get("EVERSMART_MANUAL_BILL_ACCOUNT"))
                account = args.account or os.environ.get("EVERSMART_ACCOUNT") or config.get("EVERSMART_ACCOUNT")
                entity = args.entity_id or os.environ.get("EVERSMART_ENTITY_ID") or config.get("EVERSMART_ENTITY_ID")
                if args.import_archive:
                    stage = "import"
                    if not args.import_archive.is_dir():
                        raise LocalError("Archive directory does not exist.")
                    from history import History
                    with process_lock(db_path.with_suffix(db_path.suffix + ".lock")), History(db_path) as store:
                        imported = store.ingest_archive(args.import_archive)
                        skipped = store.archive_failures
                    _report_status(state, {"status": "imported", "imported_runs": imported,
                                           "skipped_invalid_runs": skipped})
                if args.login:
                    stage = "login"
                    client = (client_factory or EversourceClient)(cookie_file=args.cookie_file,
                                                                 cookieinfo=args.cookieinfo, state_dir=state, entity_id=entity)
                    client.login()
                    client.load_databrowser()
                    _report_status(state, {"status": "authenticated"})
                if collect:
                    stage = "collection"
                    client = None

                    def collect_operation():
                        nonlocal client
                        if client is None:
                            client = (client_factory or EversourceClient)(cookie_file=args.cookie_file,
                                                                         cookieinfo=args.cookieinfo, state_dir=state, entity_id=entity)
                        return collect_once(client, outdir, account, args.interval, args.resolution,
                                            args.all_service_points, args.green_button, db_path=db_path, output=output,
                                            backfill=args.backfill, renderer=renderer, manual_bill_dir=manual_bill_dir,
                                            manual_bill_account=manual_bill_account)

                    code = _supervise(collect_operation, state, args.poll, sleep)
                    if code:
                        return code
                if args.shareable or (args.render and not collect):
                    stage = "export" if args.shareable else "render"
                    with process_lock(db_path.with_suffix(db_path.suffix + ".lock")):
                        publish_dashboard(outdir, args.shareable or output, db_path=db_path,
                                          account=_history_account(db_path, account),
                                          shareable=bool(args.shareable), manual_bill_dir=manual_bill_dir,
                                          manual_bill_account=manual_bill_account, renderer=renderer)
                    _report_status(state, {"status": "exported" if args.shareable else "rendered"},
                                   persist=not args.shareable)
                return 0
            except LockBusy:
                raise
            except Exception as exc:
                _report_status(state, {"status": f"{stage}_failed",
                                       "error": safe_error(exc, f"Eversmart {stage} failed; details withheld.")},
                               failed=True, persist=stage != "export")
                return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(safe_error(exc, "Unable to use private Eversmart state; details withheld."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
