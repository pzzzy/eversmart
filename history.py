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

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS runs (
    account TEXT NOT NULL,
    retrieved TEXT NOT NULL,
    manifest TEXT NOT NULL,
    PRIMARY KEY (account, retrieved)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS runs_chronological ON runs (retrieved, account);
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
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS readings_by_time
    ON readings (account, dataset, start_utc, end_utc, identity);
CREATE INDEX IF NOT EXISTS net_coverage
    ON readings (net_account, cadence, start_utc, end_utc) WHERE dataset = 'usage';
COMMIT;
"""


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


def plan_windows(available: str, resolution: str, existing: Iterable[_Window], *,
                 backfill: bool = False, overlap_hours: int = 72) -> list[str]:
    """Plan UTC requests of at most 720 hours, without using the wall clock.

    Refresh relative to the last observed end, or take only the latest API-sized
    window when there is no prior coverage. Backfill subtracts actual covered
    slots from availability. Subdaily windows use a UTC cadence grid; daily
    availability at local midnight retains that phase and its explicit DST edges.
    """
    resolution = _resolution(resolution)
    step = _STEP[resolution]
    if not math.isfinite(overlap_hours) or overlap_hours < 0:
        raise ValueError("overlap_hours must be finite and nonnegative")
    raw_lo, raw_hi = _parse_window(available, date_ok=True)
    lo, hi = _utc(raw_lo), _utc(raw_hi)
    local_days = resolution == "DAY" and raw_lo.time() == raw_hi.time() == time.min
    anchor = hi if local_days else _EPOCH
    if not local_days:
        lo, hi = _ceil(lo, step, anchor), _floor(hi, step, anchor)
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
        if local_days:
            # With offsets but no zone name we cannot place the DST transition.
            # Accept full-day boundaries in either advertised midnight phase.
            def midnight(t: datetime) -> bool:
                return _floor(t, step, lo) == t or _floor(t, step, hi) == t

            covered = ((a, b) for a, b in covered if midnight(a) and midnight(b) and b - a >= timedelta(hours=23))
        else:
            covered = ((_ceil(a, step, anchor), _floor(b, step, anchor)) for a, b in covered)
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
        start = max(lo, _floor(last_end - overlap, step, anchor))
        gaps = [(start, hi)] if start < hi else []
    else:
        gaps = [(max(lo, hi - _MAX_WINDOW), hi)]

    requests = []
    for a, b in gaps:
        while a < b:
            if b - a <= _MAX_WINDOW:
                end = b
            else:
                end = a + _MAX_WINDOW
            requests.append(f"{_iso(a)}/{_iso(end)}")
            a = end
    return requests


def _private_dirs(path: Path) -> None:
    if not path.is_dir():
        _private_dirs(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)


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


def _rows_from_csv(path: Path) -> Iterator[_Row]:
    try:
        source = path.open(encoding="utf-8-sig", newline="")
    except FileNotFoundError:
        return
    with source:
        reader = csv.DictReader(source, strict=True)
        if not reader.fieldnames:   # the collector emits a blank line for empty optional exports
            return
        if len(reader.fieldnames) != len(set(reader.fieldnames)) or "" in reader.fieldnames:
            raise csv.Error("invalid CSV header")
        for row in reader:
            # Legacy rows sometimes omit trailing columns; preserve their CSV field names.
            if None in row:
                raise csv.Error("CSV row has more values than its header")
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


def _zone(name: object) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if isinstance(name, str) and name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


class History:
    """SQLite-backed account history. Use a separate context per thread/process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._db: sqlite3.Connection

    def __enter__(self) -> History:
        _private_dirs(self.path.parent)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self._db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
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
        except BaseException:
            self._db.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._db.close()

    def ingest_archive(self, data_dir: Path) -> int:
        """Ingest finalized timestamp directories immediately under data_dir."""
        try:
            runs = sorted(Path(data_dir).iterdir())
        except FileNotFoundError:
            return 0
        return sum(self.ingest_run(run) for run in runs
                   if (name := _RUN.fullmatch(run.name)) and _stamp(name["stamp"])
                   and not run.is_symlink() and run.is_dir())

    def ingest_run(self, run_dir: Path) -> bool:
        """Import a run atomically. Missing/incomplete manifests and repeats return False."""
        run_dir = Path(run_dir)
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, NotADirectoryError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(manifest, dict):
            return False
        account = manifest.get("account") or manifest.get("utility_id")
        run_name = _RUN.fullmatch(run_dir.name)
        retrieved = _manifest_time(manifest, _stamp(run_name["stamp"]) if run_name else None)
        if not isinstance(account, str) or not account.strip() or retrieved is None:
            return False
        if run_name:
            retrieved += run_name["suffix"] or ""
        zone = _zone(manifest.get("timezone"))
        accounts_by_uuid = {}
        if isinstance(manifest.get("service_point_uuid"), str) and manifest["service_point_uuid"]:
            accounts_by_uuid[manifest["service_point_uuid"]] = account
        zones_by_account = {account: zone}
        service_points = manifest.get("service_points")
        for point in service_points if isinstance(service_points, list) else ():
            if not isinstance(point, dict):
                continue
            point_account = point.get("account") or point.get("utility_id")
            uuid = point.get("service_point_uuid") or point.get("uuid")
            if isinstance(point_account, str) and point_account:
                if isinstance(uuid, str) and uuid:
                    accounts_by_uuid[uuid] = point_account
                if "timezone" in point:
                    zones_by_account[point_account] = _zone(point["timezone"])
        # Only bridge UUID-less rows when this run identifies exactly one UUID
        # for the utility account; explicit UUIDs always remain distinct.
        uuids_by_account: dict[str, set[str]] = {}
        for uuid, point_account in accounts_by_uuid.items():
            uuids_by_account.setdefault(point_account, set()).add(uuid)
        known_uuids = {a: next(iter(ids)) for a, ids in uuids_by_account.items() if len(ids) == 1}
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)", (account, retrieved, _json(manifest)))
            if not cursor.rowcount:
                self._db.rollback()
                return False
            for dataset in _NUMERIC:
                for seq, row in enumerate(_rows_from_csv(run_dir / f"{dataset}.csv")):
                    row_account = row.get("utility_id") or accounts_by_uuid.get(row.get("service_point_uuid", ""))
                    if dataset == "service_points":
                        row_account = row_account or row.get("account")
                    row_zone = zones_by_account.get(row_account, zone) if row_account else zone
                    identity = _identity(dataset, row, row_zone, known_uuids.get(row_account) if row_account else None)
                    if identity is None:
                        continue
                    key, pair = identity
                    net_account, cadence = None, None
                    if (dataset == "usage" and pair and row.get("stream", "").lower() == "netusage"
                            and row.get("serviceQuantityIdentifier", "").upper() in {"", "NET_USAGE"}):
                        net_account, cadence = row_account, _cadence(pair, row_zone)
                    self._upsert(account, dataset, _json(key), pair, row, retrieved, seq, net_account, cadence)
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        return True

    def _upsert(self, account: str, dataset: str, identity: str, pair: _Window | None,
                row: _Row, retrieved: str, seq: int, net_account: str | None, cadence: str | None) -> None:
        start, end = (_iso(_utc(t), timespec="microseconds") for t in pair) if pair else (None, None)
        rank = _rank(dataset, row)
        new_order = (rank, retrieved, seq)
        old = self._db.execute("""
            SELECT start_utc, end_utc, cadence, net_account, rank, retrieved, seq, payload, field_versions
            FROM readings WHERE account = ? AND dataset = ? AND identity = ?
        """, (account, dataset, identity)).fetchone()
        incoming = row
        previous: _Row = {}
        versions = {}
        if old:
            old_start, old_end, old_cadence, old_account, old_rank, old_time, old_seq, payload, provenance = old
            previous = json.loads(payload)
            versions = json.loads(provenance)
            if new_order >= (old_rank, old_time, old_seq):
                row = {**previous, **incoming}
                if start is None:
                    start, end = old_start, old_end
                net_account = net_account or old_account
                cadence = cadence or old_cadence
            else:
                row = {**incoming, **previous}
                if old_start is not None:
                    start, end = old_start, old_end
                cadence, net_account = old_cadence or cadence, old_account or net_account
                rank, retrieved, seq = old_rank, old_time, old_seq
        # Partial records neither erase usable values nor advance their provenance.
        # Numeric fields share the record's quality but are independently usable.
        for key in row:
            if key in {"readType", "estimated"}:  # Keep the representative record's quality label.
                continue
            value = incoming.get(key, "")
            numeric = key in _NUMERIC[dataset]
            usable = _finite(value) if numeric else bool(value.strip())
            order = (new_order[0] % 3, *new_order[1:]) if numeric else new_order
            prior = versions.get(key)
            if usable and (prior is None or order >= tuple(prior)):
                row[key] = value
                versions[key] = order
            elif prior is not None:
                row[key] = previous[key]
        if dataset == "usage" and not _finite(row.get("value", "")):
            net_account = None
        self._db.execute("INSERT OR REPLACE INTO readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (account, dataset, identity, start, end, cadence, net_account, rank, retrieved, seq,
                          _json(row), _json(versions)))

    def manifests(self, account: str | None = None) -> list[dict]:
        """Return archived manifests in retrieval order, oldest first."""
        return [json.loads(row[0]) for row in self._manifest_rows(account, latest=False)]

    def latest_manifest(self, account: str | None = None) -> dict | None:
        row = self._manifest_rows(account, latest=True).fetchone()
        return json.loads(row[0]) if row is not None else None

    def _manifest_rows(self, account: str | None, *, latest: bool) -> sqlite3.Cursor:
        query = "SELECT manifest FROM runs"
        if account is not None:
            query += " WHERE account = ?"
        query += " ORDER BY retrieved DESC, account DESC LIMIT 1" if latest else " ORDER BY retrieved, account"
        return self._db.execute(query, (account,) if account is not None else ())

    def rows(self, dataset: str, account: str, start: datetime | None = None,
             end: datetime | None = None) -> list[dict]:
        """Return CSV-shaped rows for the collection account, ordered by UTC start.

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
