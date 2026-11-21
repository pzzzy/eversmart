# Eversmart

Eversmart collects your Eversource smart-meter readings through the Opower DataBrowser and builds an offline dashboard for energy, prices, demand and bills. These are delayed utility readings, not live meter telemetry. The upstream DataBrowser is not a supported public API, so authentication or responses can change.

## Set up

Use Python 3.11 or newer on macOS or Linux. The core uses the standard library. To parse physical bill PDFs, use `pypdf`, `pdfplumber`, `pdftotext`, or the native macOS PDFKit fallback. Without a physical statement, Eversmart can chart Opower data but does not invent a statement-calibrated forecast.

```sh
export EVERSMART_STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/eversmart"
mkdir -p -m 700 "$EVERSMART_STATE_DIR"
[ -e "$EVERSMART_STATE_DIR/.env" ] || install -m 600 .env.example "$EVERSMART_STATE_DIR/.env"
```

Fill in `EVERSOURCE_LOGIN` and `EVERSOURCE_PASS` there, or supply them in the process environment. Use `EVERSMART_ACCOUNT` or `--account` to choose a service point if more than one is visible. The Opower entity is normally discovered; `EVERSMART_ENTITY_ID` or `--entity-id` can override discovery. `--state-dir` overrides the environment. The private state directory holds the cookie jar, history and dashboard; bearer tokens are not written to disk.

## Upgrade from snapshot-only versions

Import before collecting. No utility calls are made, and the original archive is not modified. Repeating the import skips already ingested snapshots. Use a dedicated private bills folder; bill files are not searched for in the checkout.

```sh
python3.11 eversmart.py --import-archive /path/to/old/data --render
```

Place physical PDFs or structured bill summaries in `$EVERSMART_STATE_DIR/bills/`, or use `--manual-bill-dir /path/to/private/bills`. If more than one service point is visible, put `utility_id` in each structured summary or use `--manual-bill-account SERVICE_POINT` (or `EVERSMART_MANUAL_BILL_ACCOUNT` in the private `.env`) only for a directory whose unassigned bills all belong to that service point. To migrate authentication settings, copy the old `.env` into the private state directory (do not overwrite a new configuration); set its mode to `600`. You can also choose to copy the existing Netscape jar there as `.eversource_cookies.txt`. A cached Opower bearer token is not needed.

## Collect and view

```sh
python3.11 eversmart.py --login           # Optional: verify authentication
python3.11 eversmart.py                   # One collection
open "$EVERSMART_STATE_DIR/dashboard.html"  # macOS; on Linux open in your browser
python3.11 eversmart.py --poll 900        # Poll with retry/backoff
python3.11 eversmart.py --backfill        # Fetch gaps in advertised history
```

The first collection downloads the latest API-sized window. Later runs refresh an overlap for delayed reads and corrections, and catch up if the collector was offline. Explicit `--interval '2026-06-03T00:00:00-04:00/2026-08-06T00:00:00-04:00'` requests are split into windows of at most 720 elapsed hours rather than silently truncated. `--resolution` selects the desired read cadence. Use `--green-button` only for occasional one-shot audits; it starts a remote export job. Use `--out` to choose a private location for new audit snapshots.

Collection options do not start network requests when mixed with an offline action unless you also provide `--collect`.

For scheduled use, prefer running the one-shot command under `launchd` or a systemd timer. Keep the state directory and service account consistent across invocations. The CLI rejects overlapping operations on the same state. `status.json` carries the latest operation status; the current dashboard remains available if collection or rendering fails. `--render` rebuilds it offline from the database.

## How to read the numbers

- SQLite reconciles overlapping polls per service point, stream, and UTC interval, preferring usable actual readings over estimates. Raw snapshots (gzip JSON and audit CSV) remain available for review; old snapshots are not deleted. Apply a local retention policy only after backing up `history.sqlite` and any raw evidence you need.
- The dashboard separates the last successful poll, the latest actual net reading, and upstream-advertised availability. It shows local billing dates and keeps the two fall-back hours separate. Missing data is not zero. Cost-stream amounts and rate-based estimates are labeled; interval totals are not the statement balance.
- Billing uses the newest relevant electricity usage boundary and a separately dated physical statement rate. Reconciliation distinguishes partial days from complete coverage. Forecasts are withheld if the cycle or rate cannot be established or required actual coverage is missing. Monthly EIA benchmarks are optional comparisons, not your tariff.

## Share deliberately

The normal dashboard, database, meter photos, statements and CSV/JSON archives are private. A display mask is not sanitization. For a standalone export containing only aggregate net kWh, estimated interval cost and peak demand — no identifiers, dates, interval data or diagnostics — run:

```sh
python3.11 eversmart.py --shareable /path/to/energy-summary.html
```

Choose a new filename outside the private state, archive, and bills directories. The export never replaces an existing file. Those three numbers still come from your meter; review them before sharing. Keep downloaded materials and secrets out of Git. The checked-in `meter_inventory` files are sanitized model notes, not a mapping to a real customer account. See [SECURITY.md](SECURITY.md).

## Develop

```sh
python3.11 -m unittest discover -v
python3.11 -m compileall -q -x '(^|/)\.(git|venv)(/|$)' .
```

Tests use synthetic fixtures and do not require utility access, a real bill, or a PDF tool. CI runs on Python 3.11 and 3.12. MIT licensed.
