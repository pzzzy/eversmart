"""Canonical history for finalized collector runs; independent of the collector and UI.

UTC interval endpoints are indexed separately from the original CSV payloads.
All intervals (including query bounds and planner windows) are half-open.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_Window = tuple[datetime, datetime]
_Row = dict[str, str]
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
# The collector appends its eight-character TemporaryDirectory token on collision.
_RUN = re.compile(rf"(?P<stamp>{_STAMP.pattern})(?P<suffix>\.[a-z0-9_]{{8}})?")
_DATE = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8})"
_ISO = re.compile(
    rf"(?P<date>{_DATE})(?:[T ](?:[01][0-9]|2[0-3]):?[0-5][0-9]"
    r"(?::?[0-5][0-9](?:\.[0-9]{1,6})?)?(?:Z|[+-](?:[01][0-9]|2[0-3])(?::?[0-5][0-9])?)?)?"
)
_STEP = {"QUARTER_HOUR": timedelta(minutes=15), "HALF_HOUR": timedelta(minutes=30),
         "HOUR": timedelta(hours=1), "DAY": timedelta(days=1)}
_MAX_WINDOW = timedelta(hours=720)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_STREAM_DATASETS = {"usage", "cost", "pricing", "demand_maxima"}
_NUMERIC = {
    "usage": ("value", "monetaryAmount"),
    "cost": ("monetaryAmount", "value"),
    "pricing": ("cost_per_unit", "monetaryAmount", "value"),
    "weather": ("mean_temperature", "min_temperature", "max_temperature"),
    "bills": ("currentAmount", "kwh", "usageCharges", "deferredNEMCharges", "totalNEMCharges",
              "energyPurchased", "energySold", "totalEnergyCosts", "effective_price_per_kwh"),
    "demand_maxima": ("value",),
    "service_points": (),
    "green_button_rows": ("usage_kwh", "cost"),
}
_PRICING_ID = ("rate_plan_code", "rate_type", "tier", "tou_label", "component_attributes")
_SCHEMA_VERSION = 1

_READINGS_TABLE = """
CREATE TABLE IF NOT EXISTS readings (
    account TEXT NOT NULL,
    dataset TEXT NOT NULL,
    identity TEXT NOT NULL,
    start_utc TEXT,
    end_utc TEXT,
    cadence TEXT,
    net_account TEXT,
    rank INTEGER NOT NULL,
    retrieved TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    field_versions TEXT NOT NULL,
    PRIMARY KEY (account, dataset, identity)
) WITHOUT ROWID
"""
_READINGS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS readings_by_time ON readings (account, dataset, start_utc, end_utc, identity)",
    "CREATE INDEX IF NOT EXISTS net_coverage ON readings (net_account, cadence, start_utc, end_utc) WHERE dataset = 'usage'",
)
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS runs (
    account TEXT NOT NULL,
    retrieved TEXT NOT NULL,
    manifest TEXT NOT NULL,
    PRIMARY KEY (account, retrieved)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS runs_chronological ON runs (retrieved, account);
CREATE TABLE IF NOT EXISTS run_accounts (
    account TEXT NOT NULL,
    retrieved TEXT NOT NULL,
    primary_account TEXT NOT NULL,
    point TEXT NOT NULL,
    row_counts TEXT,
    PRIMARY KEY (account, retrieved, primary_account)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS indexed_runs (
    account TEXT NOT NULL,
    retrieved TEXT NOT NULL,
    PRIMARY KEY (account, retrieved)
) WITHOUT ROWID;
""" + ";\n".join((_READINGS_TABLE, *_READINGS_INDEXES)) + ";"


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("an explicit timezone is required")
    return value.astimezone(UTC)


def _resolve_local(value: datetime, zone: ZoneInfo) -> datetime:
    # Round-trip both folds: overlaps give two instants; gaps give none.
    candidates = set()
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(candidate)
    if len(candidates) != 1:
        raise ValueError("local time is ambiguous or nonexistent")
    return candidates.pop()


def _parse_time(text: str, zone: ZoneInfo | None = None, *, date_ok: bool = False) -> datetime:
    text = text.strip()
    match = _ISO.fullmatch(text)
    if match is None:
        raise ValueError("invalid ISO timestamp")
    value = datetime.fromisoformat(text)
    if len(match["date"]) == len(text):
        if not date_ok:
            raise ValueError("reading timestamps require a time of day")
        if zone is None:
            return value.replace(tzinfo=UTC)
    if value.tzinfo is None:
        if zone is None:
            raise ValueError("timestamp has no timezone")
        return _resolve_local(value, zone)
    return value


def _parse_window(text: str, zone: ZoneInfo | None = None, *, date_ok: bool = False) -> _Window:
    a, b = text.split("/")
    lo, hi = (_parse_time(t, zone, date_ok=date_ok) for t in (a, b))
    if _utc(lo) >= _utc(hi):
        raise ValueError("interval must have positive duration")
    return lo, hi


def _iso(value: datetime, *, timespec: str = "auto") -> str:
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _floor(value: datetime, step: timedelta, anchor: datetime = _EPOCH) -> datetime:
    return value - (value - anchor) % step


def _ceil(value: datetime, step: timedelta, anchor: datetime = _EPOCH) -> datetime:
    return value + (anchor - value) % step


def _merged(windows: Iterable[_Window]) -> Iterator[_Window]:
    start = end = None
    for lo, hi in windows:
        if start is None or end is None:
            start, end = lo, hi
        elif lo > end:
            yield start, end
            start, end = lo, hi
        else:
            end = max(end, hi)
    if start is not None and end is not None:
        yield start, end


def _resolution(resolution: str) -> str:
    name = resolution.strip().upper()
    name = {"HOURLY": "HOUR", "DAILY": "DAY"}.get(name, name)
    if name not in _STEP:
        raise ValueError(f"unsupported read resolution: {resolution}")
    return name


def _planner_zone(timezone: str | ZoneInfo | None) -> ZoneInfo | None:
    if isinstance(timezone, ZoneInfo) or timezone is None:
        return timezone
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError("invalid meter timezone") from None


def _day_boundary(value: datetime, zone: ZoneInfo, *, ceil: bool = False) -> datetime:
    local = value.astimezone(zone)
    day = local.date() + timedelta(days=ceil and local.time() != time.min)
    return _resolve_local(datetime.combine(day, time.min), zone)


def _chunk_windows(windows: Iterable[_Window], zone: ZoneInfo | None = None) -> list[str]:
    requests = []
    for a, b in windows:
        while a < b:
            end = b if b - a <= _MAX_WINDOW else a + _MAX_WINDOW
            if zone is not None and end != b:
                end = _day_boundary(end, zone)
            requests.append(f"{_iso(a)}/{_iso(end)}")
            a = end
    return requests


def plan_explicit_windows(interval: str, resolution: str, *,
                          timezone: str | ZoneInfo | None = None) -> list[str]:
    """Split an explicit range into <=720h UTC requests, without coverage or overlap.

    DAY with a meter timezone selects complete local days; it accepts date-only
    or unambiguous local timestamps as well as offset-aware timestamps. Subdaily
    requests preserve the exact specified bounds and require explicit offsets.
    """
    if _resolution(resolution) == "DAY":
        return plan_windows(interval, "DAY", (), backfill=True, timezone=timezone)
    lo, hi = _parse_window(interval)
    return _chunk_windows([(_utc(lo), _utc(hi))])


def plan_windows(available: str, resolution: str, existing: Iterable[_Window], *,
                 backfill: bool = False, overlap_hours: int = 72,
                 timezone: str | ZoneInfo | None = None) -> list[str]:
    """Plan UTC requests of at most 720 hours, without using the wall clock.

    Refresh relative to the last observed end, or take only the latest API-sized
    window when there is no prior coverage. Backfill subtracts actual covered
    slots from availability. Subdaily windows use a UTC cadence grid. Pass the
    meter's IANA timezone for true DAY midnight boundaries across DST; without
    it, retain the legacy behavior inferred from advertised endpoints.
    """
    resolution = _resolution(resolution)
    step = _STEP[resolution]
    if not math.isfinite(overlap_hours) or overlap_hours < 0:
        raise ValueError("overlap_hours must be finite and nonnegative")
    zone = _planner_zone(timezone) if resolution == "DAY" else None
    raw_lo, raw_hi = _parse_window(available, zone, date_ok=True)
    lo, hi = _utc(raw_lo), _utc(raw_hi)
    local_days = resolution == "DAY" and raw_lo.time() == raw_hi.time() == time.min
    anchor = hi if local_days else _EPOCH
    def floor(t: datetime) -> datetime:
        return _day_boundary(t, zone) if zone is not None else _floor(t, step, anchor)

    def ceil(t: datetime) -> datetime:
        return _day_boundary(t, zone, ceil=True) if zone is not None else _ceil(t, step, anchor)

    if zone is not None or not local_days:
        lo, hi = ceil(lo), floor(hi)
    if lo >= hi:
        return []

    clipped = []
    observed_end = None
    for start, end in existing:
        start, end = _utc(start), _utc(end)
        if start >= end or start >= hi:
            continue
        observed_end = min(hi, end) if observed_end is None else max(observed_end, min(hi, end))
        a, b = max(lo, start), min(hi, end)
        if a < b:
            clipped.append((a, b))
    if backfill:
        covered = _merged(sorted(clipped))
        if local_days and zone is None:
            # With offsets but no zone name we cannot place the DST transition.
            # Accept full-day boundaries in either advertised midnight phase.
            def midnight(t: datetime) -> bool:
                return _floor(t, step, lo) == t or _floor(t, step, hi) == t

            covered = ((a, b) for a, b in covered if midnight(a) and midnight(b) and b - a >= timedelta(hours=23))
        else:
            covered = ((ceil(a), floor(b)) for a, b in covered)
        gaps = []
        cursor = lo
        for a, b in covered:
            if a >= b:
                continue
            if cursor < a:
                gaps.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < hi:
            gaps.append((cursor, hi))
    elif observed_end is not None:
        last_end = max(lo, observed_end)
        overlap = timedelta(hours=min(overlap_hours, (last_end - lo).total_seconds() / 3600))
        start = max(lo, floor(last_end - overlap))
        gaps = [(start, hi)] if start < hi else []
    else:
        start = lo if hi - lo <= _MAX_WINDOW else ceil(hi - _MAX_WINDOW)
        gaps = [(start, hi)]
    return _chunk_windows(gaps, zone)


def _private_db_path(path: Path) -> Path:
    path = path.absolute()
    if sys.platform == "darwin" and path.parts[1:2] in {("tmp",), ("var",)}:
        # The OS exposes these system-owned directory aliases on macOS.
        path = Path("/private") / path.relative_to("/")
    for directory in reversed(path.parents):
        directory.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OSError("History database directory must not be a symlink")
    if path.parent.stat().st_mode & 0o022:
        raise PermissionError("History database directory must not be writable by others")
    return path


def _private_sqlite_file(path: Path, *, create: bool = False) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if create:
            raise
        return  # An optional journal or sidecar need not already exist.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise OSError("History database artifact must be an owned regular file without links")
    if stat.S_IMODE(info.st_mode) != 0o600:
        # Do not open/close a descriptor on already-private live SQLite files;
        # that could release process-associated SQLite advisory locks.
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            if create:
                raise
            return
        try:
            reopened = os.fstat(fd)
            if not os.path.samestat(info, reopened):
                raise OSError("History database artifact changed while opening")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def _finite(value: str) -> bool:
    try:
        return math.isfinite(float(value))
    except ValueError:
        return False


def _point_id(row: _Row, *, inventory: bool = False, known_uuid: str | None = None) -> tuple[str, str] | None:
    uuid = row.get("service_point_uuid") or (row.get("uuid") if inventory else None) or known_uuid
    utility = row.get("utility_id") or (row.get("account") if inventory else None)
    return ("uuid", uuid) if uuid else ("utility", utility) if utility else None


def _cadence(pair: _Window, zone: ZoneInfo | None) -> str | None:
    start, end = map(_utc, pair)
    if end - start >= timedelta(hours=23):
        local_start, local_end = (t.astimezone(zone) for t in pair) if zone else pair
        if (local_start.time() == local_end.time() and local_end.date() - local_start.date() == _STEP["DAY"]
                and timedelta(hours=23) <= end - start <= timedelta(hours=25)):
            return "DAY"
    for name, step in _STEP.items():
        if name != "DAY" and end - start == step and _floor(start, step) == start:
            return name
    return None


def _green_button_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return datetime.strptime(text, "%m/%d/%Y").date()


def _green_button_window(row: _Row, zone: ZoneInfo | None) -> _Window | None:
    # Green Button may supply only a date, or use local wall times without offsets.
    # It is never considered authoritative interval coverage.
    try:
        day = _green_button_date(row.get("date", "").strip())
        start, end = (row.get(k, "").strip() for k in ("start_time", "end_time"))
        if row.get("dataset") == "usage" and (start or end):
            lo = _parse_time(f"{day}T{start}", zone)
            hi = _parse_time(f"{day}T{end}", zone)
            if end in {"00:00", "00:00:00"} and _utc(hi) < _utc(lo):
                hi = _parse_time(f"{day + timedelta(days=1)}T{end}", zone)
        else:
            last = _green_button_date(end) if row.get("dataset") == "billing" and end else day
            lo = _parse_time(day.isoformat(), zone, date_ok=True)
            hi = _parse_time((last + timedelta(days=1)).isoformat(), zone, date_ok=True)
        return (lo, hi) if _utc(lo) < _utc(hi) else None
    except (ValueError, OverflowError):
        return None


def _identity(dataset: str, row: _Row, zone: ZoneInfo | None,
              known_uuid: str | None) -> tuple[tuple, _Window | None] | None:
    get = lambda key: row.get(key, "")
    pair = None
    if dataset in _STREAM_DATASETS or dataset in {"weather", "bills"}:
        source = (get("timeInterval") or get("usageInterval")) if dataset == "bills" else get("timeInterval")
        try:
            if dataset == "weather" and not source and get("date"):
                day = date.fromisoformat(get("date"))
                source = f"{day.isoformat()}/{(day + timedelta(days=1)).isoformat()}"
            pair = _parse_window(source, zone, date_ok=dataset in {"weather", "bills"})
        except (ValueError, OverflowError):
            if dataset != "bills":
                return None
    times = tuple(_iso(_utc(t), timespec="microseconds") for t in pair) if pair else ()
    if dataset in _STREAM_DATASETS:
        point = _point_id(row, known_uuid=known_uuid)
        if point is None:
            return None
        key = (*point, get("stream"), get("serviceQuantityIdentifier"), *times)
        if dataset == "pricing":
            key += tuple(get(k) for k in _PRICING_ID)
    elif dataset == "weather":
        key = (get("premise_uuid") or get("premise_urn"), *times)
    elif dataset == "bills":
        bill, segment = get("bill_urn"), get("segment_urn")
        if bill and segment:
            key = ("ids", bill, segment)
        elif segment:
            key = ("segment", segment)
        elif times or bill:
            key = ("period", bill, get("service_agreement_uuid") or get("service_agreement_urn"),
                   get("serviceType"), *times)
        else:
            return None
    elif dataset == "service_points":
        key = _point_id(row, inventory=True, known_uuid=known_uuid)
        if key is None:
            return None
    else:  # green_button_rows; export filenames are provenance, not identity
        pair = _green_button_window(row, zone)
        key = tuple(get(k) for k in ("dataset", "service_point", "type", "date", "start_time", "end_time"))
        if not get("date"):
            key += (get("source_file"),)
    return key, pair


def _rank(dataset: str, row: _Row) -> int:
    fields = _NUMERIC[dataset]
    primary = fields[:1] if dataset in {"usage", "cost", "demand_maxima"} else fields
    usable = not primary or any(_finite(row.get(field, "")) for field in primary)
    quality = row.get("readType", "").strip().upper()
    if dataset == "bills":
        estimated = row.get("estimated", "").strip().lower()
        quality = "ACTUAL" if estimated in {"false", "0"} else "ESTIMATED" if estimated in {"true", "1"} else ""
    return (3 if usable else 0) + _quality_rank(quality)


def _quality_rank(quality: str) -> int:
    return 2 if quality == "ACTUAL" else 0 if quality.startswith("ESTIMAT") else 1


def _rows_from_csv(path: Path, *, required_usage: bool = False) -> Iterator[_Row]:
    try:
        source = path.open(encoding="utf-8-sig", newline="")
    except FileNotFoundError:
        if required_usage:
            raise csv.Error("required usage CSV is missing") from None
        return
    with source:
        reader = csv.DictReader(source, strict=True)
        if required_usage and (not {"timeInterval", "stream", "value"}.issubset(reader.fieldnames or ())
                               or not {"utility_id", "service_point_uuid"}.intersection(reader.fieldnames or ())):
            raise csv.Error("invalid usage CSV header")
        if not reader.fieldnames:   # the collector emits a blank line for empty optional exports
            return
        if len(reader.fieldnames) != len(set(reader.fieldnames)) or "" in reader.fieldnames:
            raise csv.Error("invalid CSV header")
        for row in reader:
            # Legacy rows sometimes omit trailing columns; preserve their CSV field names.
            if None in row or (required_usage and None in row.values()):
                raise csv.Error("CSV row does not match its header")
            yield {k: v if v is not None else "" for k, v in row.items()}


def _stamp(text: str) -> str | None:
    if not _STAMP.fullmatch(text):
        return None
    try:
        return _iso(_utc(_parse_time(text)), timespec="microseconds")
    except ValueError:
        return None


def _manifest_time(manifest: dict, fallback: str | None) -> str | None:
    raw_stamp = manifest.get("retrieved_at_utc")
    if isinstance(raw_stamp, str):
        compact = _stamp(raw_stamp)
        if compact:
            return compact
        try:
            return _iso(_utc(_parse_time(raw_stamp)), timespec="microseconds")
        except (ValueError, OverflowError):
            pass
    return fallback


def _run_key(manifest: dict, run_name: re.Match[str] | None) -> str | None:
    retrieved = _manifest_time(manifest, _stamp(run_name["stamp"]) if run_name else None)
    if retrieved is None or run_name is None:
        return retrieved
    # The suffix breaks ties only. Fixed-width UTC microseconds sort first.
    return retrieved + (run_name["suffix"] or "")


def _zone(name: object) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if isinstance(name, str) and name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _string(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


class _RunPoints:
    """Resolve data ownership separately from discovery-only inventory."""

    def __init__(self, primary: str, manifest: dict, inventory: Iterable[dict] = ()):
        self.primary = primary
        self.points: dict[str, dict] = {}
        self.accounts_by_uuid: dict[str, set[str]] = {}
        self.uuids_by_account: dict[str, set[str]] = {}
        points = manifest.get("service_points")
        for point in points if isinstance(points, list) else ():
            if isinstance(point, dict):
                self._add_point(point)
        for point in inventory:
            self._add_point(point)
        self._link(primary, _string(manifest.get("service_point_uuid")))
        self.zone = _zone(manifest.get("timezone")) or _zone(self.points.get(primary, {}).get("timezone"))
        self.members = {primary}
        plans = manifest.get("collection_windows")
        if isinstance(plans, dict):
            self.members.update(a for a, windows in plans.items() if a and isinstance(windows, list) and windows)

    def _add_point(self, point: dict) -> None:
        account = _string(point.get("account")) or _string(point.get("utility_id"))
        if account is not None:
            meta = self.points.setdefault(account, {})
            for field, value in point.items():
                if value:
                    meta.setdefault(field, value)
            self._link(account, _string(point.get("service_point_uuid")) or _string(point.get("uuid")))


    def _link(self, account: str, uuid: str | None) -> None:
        if uuid is not None:
            self.accounts_by_uuid.setdefault(uuid, set()).add(account)
            self.uuids_by_account.setdefault(account, set()).add(uuid)

    def known_uuid(self, account: str) -> str | None:
        ids = self.uuids_by_account.get(account, set())
        return next(iter(ids)) if len(ids) == 1 else None

    def point_zone(self, account: str) -> ZoneInfo | None:
        point = self.points.get(account, {})
        return _zone(point.get("timezone")) if account != self.primary and "timezone" in point else self.zone

    def reading_account(self, dataset: str, row: _Row, net_account: str | None = None) -> str:
        # Bills and Green Button are exported for the collection primary.
        if dataset in {"bills", "green_button_rows"}:
            return self.primary
        if dataset == "weather":
            matches = {account for account, point in self.points.items()
                       if any(row.get(k) and row[k] == point.get(k) for k in ("premise_uuid", "premise_urn"))}
            return next(iter(matches)) if len(matches) == 1 else self.primary
        inventory = dataset == "service_points"
        uuid = row.get("service_point_uuid") or (row.get("uuid") if inventory else None)
        accounts = self.accounts_by_uuid.get(uuid, set()) if uuid else set()
        return (net_account or _string(row.get("utility_id")) or (next(iter(accounts)) if len(accounts) == 1 else None)
                or (_string(row.get("account")) if inventory else None) or self.primary)


def _member_manifest(manifest: dict, account: str, primary: str, point: dict, counts: dict | None) -> dict:
    if account == primary:
        return manifest
    points = _RunPoints(primary, manifest)
    plans = manifest.get("collection_windows")
    windows = plans.get(account) if isinstance(plans, dict) else None
    available = _string(point.get("available_interval"))
    resolutions = manifest.get("collection_resolutions")
    resolution = resolutions.get(account) if isinstance(resolutions, dict) else None
    selected, advertised = manifest.get("resolution"), points.points.get(primary, {}).get("resolution")
    if not resolution:
        resolution = selected if advertised and selected and advertised != selected else point.get("resolution") or selected
    window = windows[0] if isinstance(windows, list) and len(windows) == 1 else None
    scoped = {}
    if counts is not None:
        scoped = {f"{d}_rows": counts.get(d, 0) for d in _NUMERIC}
        scoped["primary_usage_rows"] = counts.get("usage", 0)
        scoped["bill_rows"] = 0
        if manifest.get("poll_status") in ("collected", "no_op"):
            succeeded = counts.get("usage", 0) > 0
            scoped.update(poll_status="collected" if succeeded else "no_op", success=succeeded)
    # Preserve run diagnostics but do not attribute the primary's metadata to the secondary.
    return {**manifest, **scoped, "account": account, "utility_id": account,
            "resolution": resolution, "timezone": point.get("timezone"),
            "available_interval": available, "latest_available_end": (available or "").partition("/")[2] or None,
            "collection_interval": window, "requested_interval": window,
            **{k: point.get(k) for k in ("entity_id", "service_agreement_uuid", "service_agreement_urn",
                                         "service_point_uuid", "premise_uuid", "premise_urn",
                                         "billing_account_urn", "billing_account_uuid", "available_bill_interval")}}


def _net_cadence(dataset: str, row: _Row, pair: _Window | None, zone: ZoneInfo | None) -> str | None:
    if (dataset == "usage" and pair and row.get("stream", "").lower() == "netusage"
            and row.get("serviceQuantityIdentifier", "").upper() in {"", "NET_USAGE"}):
        return _cadence(pair, zone)
    return None


class History:
    """SQLite-backed account history. Use a separate context per thread/process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._db: sqlite3.Connection
        self.archive_failures = 0

    def __enter__(self) -> History:
        path = _private_db_path(self.path)
        _private_sqlite_file(path, create=True)
        for suffix in ("-wal", "-shm", "-journal"):
            _private_sqlite_file(path.with_name(path.name + suffix))
        self._db = sqlite3.connect(path, timeout=15, isolation_level=None)
        try:
            # Concurrent first opens can race to enable WAL before the busy handler kicks in.
            deadline = monotonic() + 15
            while True:
                try:
                    self._db.execute("PRAGMA journal_mode=WAL").fetchone()
                    break
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or monotonic() >= deadline:
                        raise
                    sleep(0.05)
            self._db.executescript(_SCHEMA)
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._migrate_legacy()
                self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif version != _SCHEMA_VERSION:
                raise sqlite3.DatabaseError("unsupported history schema version")
            self._db.commit()
        except BaseException:
            self._db.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._db.close()

    def _migrate_legacy(self) -> None:
        # Canonical rows already persisted in v0 remain available even when the
        # archive is offline. Replaying archives later reconstructs memberships
        # for superseded readings whose earlier rows v0 did not store.
        inventory: dict[tuple[str, str], list[dict]] = {}
        for account, retrieved, payload in self._db.execute(
                "SELECT account, retrieved, payload FROM readings WHERE dataset = 'service_points'"):
            inventory.setdefault((account, retrieved), []).append(json.loads(payload))
        contexts = {}
        for account, retrieved, raw_manifest in self._db.execute("SELECT account, retrieved, manifest FROM runs"):
            contexts[account, retrieved] = _RunPoints(account, json.loads(raw_manifest), inventory.get((account, retrieved), ()))
        # Copy only when a v0 database exists; fresh databases have no runs.
        if not contexts:
            return
        self._db.execute("DROP INDEX readings_by_time")
        self._db.execute("DROP INDEX net_coverage")
        self._db.execute("ALTER TABLE readings RENAME TO legacy_readings")
        # executescript would implicitly commit the in-flight migration.
        self._db.execute(_READINGS_TABLE)
        for statement in _READINGS_INDEXES:
            self._db.execute(statement)
        for old_account, dataset, identity, start, end, cadence, net, rank, stamp, seq, raw, provenance in self._db.execute(
                "SELECT * FROM legacy_readings"):
            row, versions = json.loads(raw), json.loads(provenance)
            points = contexts.get((old_account, stamp)) or _RunPoints(old_account, {})
            account = points.reading_account(dataset, row, net)
            if dataset in _STREAM_DATASETS and account != old_account:
                for version in {stamp, *(order[1] for order in versions.values())}:
                    if (context := contexts.get((old_account, version))) is not None:
                        context.members.add(account)
                # v0 may have interpreted a local timestamp using the primary's
                # zone. Re-key it before replay so the old UTC slot cannot survive.
                zone = points.point_zone(account)
                corrected = _identity(dataset, row, zone, points.known_uuid(account))
                if corrected is not None and (pair := corrected[1]) is not None:
                    identity = _json(corrected[0])
                    start, end = (_iso(_utc(t), timespec="microseconds") for t in pair)
                    cadence = _net_cadence(dataset, row, pair, zone)
                    net = account if cadence else None
            self._merge_reading(account, dataset, identity, start, end, cadence, net, rank, stamp, seq, row, versions)
        self._db.execute("DROP TABLE legacy_readings")
        for (account, retrieved), points in contexts.items():
            self._add_members(account, retrieved, points)

    def ingest_archive(self, data_dir: Path) -> int:
        """Import finalized runs; archive_failures counts rejected runs in this scan."""
        self.archive_failures = 0
        try:
            runs = sorted(Path(data_dir).iterdir())
        except FileNotFoundError:
            return 0
        imported = 0
        for run in runs:
            name = _RUN.fullmatch(run.name)
            if not name or not _stamp(name["stamp"]):
                continue
            try:
                if run.is_symlink() or not run.is_dir():
                    continue
                result = self._ingest_run(run)
            except (OSError, UnicodeError, csv.Error):
                result = None
            if result is None:
                self.archive_failures += 1
            elif result:
                imported += 1
        return imported

    def ingest_run(self, run_dir: Path) -> bool:
        """Import one run atomically; missing manifests and repeats return False."""
        return bool(self._ingest_run(Path(run_dir)))

    def _ingest_run(self, run_dir: Path) -> bool | None:
        # None is a rejected manifest, False is a repeat.
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, NotADirectoryError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        account = _string(manifest.get("account")) or _string(manifest.get("utility_id"))
        retrieved = _run_key(manifest, _RUN.fullmatch(run_dir.name))
        if account is None or retrieved is None:
            return None
        stamped = type(manifest.get("archive_format")) is int and manifest["archive_format"] == 1
        required = stamped or (manifest.get("poll_status") in ("collected", "no_op")
                               and isinstance(manifest.get("collection_windows"), dict))
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute("INSERT OR IGNORE INTO indexed_runs VALUES (?, ?)", (account, retrieved))
            if not cursor.rowcount:
                self._db.rollback()
                return False
            inventory = list(_rows_from_csv(run_dir / "service_points.csv"))
            points = _RunPoints(account, manifest, inventory)
            counts: dict[str, dict[str, int]] = {}
            for dataset in _NUMERIC:
                rows = inventory if dataset == "service_points" else _rows_from_csv(
                    run_dir / f"{dataset}.csv", required_usage=required and dataset == "usage")
                count = 0
                for seq, row in enumerate(rows):
                    count += 1
                    row_account = points.reading_account(dataset, row)
                    row_zone = points.point_zone(row_account)
                    identity = _identity(dataset, row, row_zone, points.known_uuid(row_account))
                    if identity is None:
                        continue
                    key, pair = identity
                    if dataset in _STREAM_DATASETS:
                        points.members.add(row_account)
                    by_dataset = counts.setdefault(row_account, {})
                    by_dataset[dataset] = by_dataset.get(dataset, 0) + 1
                    cadence = _net_cadence(dataset, row, pair, row_zone)
                    self._upsert(row_account, dataset, _json(key), pair, row, retrieved, seq,
                                 row_account if cadence else None, cadence)
                if stamped and dataset == "usage":
                    expected = manifest.get("usage_rows")
                    if type(expected) is not int or count != expected:
                        raise csv.Error("usage CSV row count does not match the archive manifest")
            self._db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?)", (account, retrieved, _json(manifest)))
            self._add_members(account, retrieved, points, counts)
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        return True

    def _add_members(self, primary: str, retrieved: str, points: _RunPoints,
                     counts: dict[str, dict[str, int]] | None = None) -> None:
        self._db.executemany("INSERT OR REPLACE INTO run_accounts VALUES (?, ?, ?, ?, ?)",
                             ((a, retrieved, primary, _json(points.points.get(a, {})),
                               _json(counts.get(a, {})) if counts is not None else None) for a in points.members))

    def _upsert(self, account: str, dataset: str, identity: str, pair: _Window | None,
                row: _Row, retrieved: str, seq: int, net_account: str | None, cadence: str | None) -> None:
        start, end = (_iso(_utc(t), timespec="microseconds") for t in pair) if pair else (None, None)
        rank = _rank(dataset, row)
        versions = {}
        for field, value in row.items():
            if field in {"readType", "estimated"}:  # Representative record's quality label.
                continue
            numeric = field in _NUMERIC[dataset]
            usable = _finite(value) if numeric else bool(value.strip())
            if usable:
                versions[field] = (rank % 3 if numeric else rank, retrieved, seq)
        self._merge_reading(account, dataset, identity, start, end, cadence, net_account, rank, retrieved, seq, row, versions)

    def _merge_reading(self, account: str, dataset: str, identity: str, start: str | None, end: str | None,
                       cadence: str | None, net_account: str | None, rank: int, retrieved: str,
                       seq: int, row: _Row, versions: dict) -> None:
        old = self._db.execute("""
            SELECT start_utc, end_utc, cadence, net_account, rank, retrieved, seq, payload, field_versions
            FROM readings WHERE account = ? AND dataset = ? AND identity = ?
        """, (account, dataset, identity)).fetchone()
        if old:
            old_start, old_end, old_cadence, old_account, old_rank, old_time, old_seq, payload, provenance = old
            previous = json.loads(payload)
            incoming = row
            old_versions = json.loads(provenance)
            if (rank, retrieved, seq) >= (old_rank, old_time, old_seq):
                row = {**previous, **row}
                if start is None:
                    start, end = old_start, old_end
                net_account = net_account or old_account
                cadence = cadence or old_cadence
            else:
                row = {**row, **previous}
                if old_start is not None:
                    start, end = old_start, old_end
                cadence, net_account = old_cadence or cadence, old_account or net_account
                rank, retrieved, seq = old_rank, old_time, old_seq
            # Reconcile each field using its original provenance, not the
            # aggregate row's timestamp (in particular during v0 migration).
            merged = versions.copy()
            for key, prior in old_versions.items():
                order = versions.get(key)
                if order is None or tuple(order) < tuple(prior):
                    row[key], merged[key] = previous[key], prior
                else:
                    row[key] = incoming[key]
            for key in versions.keys() - old_versions.keys():
                row[key] = incoming[key]
            versions = merged
        if dataset == "usage" and not _finite(row.get("value", "")):
            net_account = None
        self._db.execute("INSERT OR REPLACE INTO readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (account, dataset, identity, start, end, cadence, net_account, rank, retrieved, seq,
                          _json(row), _json(versions)))

    def manifests(self, account: str | None = None) -> list[dict]:
        """Return run manifests oldest first; account filters include actual members."""
        return [self._read_manifest(row, account) for row in self._manifest_rows(account, latest=False)]

    def latest_manifest(self, account: str | None = None) -> dict | None:
        row = self._manifest_rows(account, latest=True).fetchone()
        return self._read_manifest(row, account) if row is not None else None

    @staticmethod
    def _read_manifest(row: tuple[str, str, str, str | None], account: str | None) -> dict:
        manifest, primary, point, counts = row
        return _member_manifest(json.loads(manifest), account or primary, primary, json.loads(point),
                                json.loads(counts) if counts is not None else None)

    def _manifest_rows(self, account: str | None, *, latest: bool) -> sqlite3.Cursor:
        if account is None:
            query = "SELECT r.manifest, r.account, '{}', NULL FROM runs r"
        else:
            query = """SELECT r.manifest, r.account, a.point, a.row_counts FROM run_accounts a
                       JOIN runs r ON r.account = a.primary_account AND r.retrieved = a.retrieved
                       WHERE a.account = ?"""
        query += " ORDER BY r.retrieved DESC, r.account DESC LIMIT 1" if latest else " ORDER BY r.retrieved, r.account"
        return self._db.execute(query, (account,) if account is not None else ())

    def rows(self, dataset: str, account: str, start: datetime | None = None,
             end: datetime | None = None) -> list[dict]:
        """Return CSV-shaped rows for the actual account, ordered by UTC start.

        Bounds select intervals that overlap [start, end); undated rows are only
        returned by an unbounded query. Pass timezone-aware datetime bounds.
        """
        if dataset not in _NUMERIC:
            raise ValueError(f"unknown history dataset: {dataset}")
        lo = _utc(start) if start is not None else None
        hi = _utc(end) if end is not None else None
        if lo is not None and hi is not None and lo >= hi:
            return []
        query = "SELECT payload FROM readings WHERE account = ? AND dataset = ?"
        args = [account, dataset]
        if lo is not None:
            query += " AND end_utc > ?"
            args.append(_iso(lo, timespec="microseconds"))
        if hi is not None:
            query += " AND start_utc < ?"
            args.append(_iso(hi, timespec="microseconds"))
        query += " ORDER BY start_utc, end_utc, identity"
        return [json.loads(row[0]) for row in self._db.execute(query, args)]

    def coverage(self, account: str, resolution: str) -> list[_Window]:
        """Return coalesced, real net usage coverage for the selected service point."""
        cursor = self._db.execute("""
            SELECT start_utc, end_utc FROM readings
            WHERE dataset = 'usage' AND net_account = ? AND cadence = ?
            ORDER BY start_utc, end_utc
        """, (account, _resolution(resolution)))
        return list(_merged((datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in cursor))
