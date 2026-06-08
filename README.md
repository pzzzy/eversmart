# Eversmart

Eversmart is a small Python utility for authenticated Eversource / Oracle Opower smart-meter data retrieval. It logs in to the Eversource DataBrowser experience, discovers Opower metadata, downloads available interval reads, and builds a self-contained local dashboard.

This repository is designed as a privacy-preserving public release: no live account data, cookies, tokens, generated dashboard output, photos, or scraped data snapshots are included.

## Features

- Direct Eversource login using credentials from `.env`.
- Optional reuse of a Netscape-format cookie jar.
- Opower DataBrowser token/entity discovery.
- GraphQL retrieval for:
  - usage interval reads
  - cost interval reads
  - rated pricing components
  - weather rows when available
  - bill-history summaries
  - demand maxima
  - service-point metadata
- Optional Green Button export audit/backfill.
- Timestamped local run directories under `data/`.
- Self-contained `dashboard.html` generation from local CSV/JSON snapshots.
- Regression tests for parsing, token refresh, dashboard aggregation, warning handling, and chart null handling.

## Privacy and safety model

This tool handles authenticated utility-account information. Treat output as private.

Never commit or share:

- `.env`
- `.eversource_cookies.txt`
- `cookieinfo.txt`
- `.opower_access_token`
- `data/`
- generated `dashboard.html` files containing real account data
- screenshots or meter photos tied to a live account
- account IDs, service agreement UUIDs, service point UUIDs, cookies, access tokens, or bill data

The included `.gitignore` blocks the common sensitive artifacts.

## Requirements

- Python 3.11+
- Standard-library-only runtime for the core scraper/dashboard
- Network access to Eversource and Opower endpoints

## Configuration

Copy the example env file and fill in local credentials:

```sh
cp .env.example .env
chmod 600 .env
```

`.env`:

```sh
EVERSOURCE_LOGIN=your-login-here
EVERSOURCE_PASS=your-password-here
EVERSMART_ACCOUNT=your-account-id-here
EVERSMART_ENTITY_ID=your-opower-entity-id-here  # optional; normally discovered
```

You can also pass the account/service-point id explicitly:

```sh
python3 eversmart.py --account 'your-account-id' --out data
```

## Usage

Run tests:

```sh
python3 -m unittest discover -v
```

Verify login and DataBrowser token extraction:

```sh
python3 eversmart.py --login
```

Retrieve currently available data:

```sh
python3 eversmart.py --account 'your-account-id' --out data
```

Poll periodically. This is near-real-time in the sense of repeatedly collecting newly published utility intervals; it is not live meter telemetry.

```sh
python3 eversmart.py --account 'your-account-id' --poll 900 --out data
```

Request a specific interval/resolution:

```sh
python3 eversmart.py \
  --account 'your-account-id' \
  --interval '2026-06-03T00:00:00-04:00/2026-06-06T00:00:00-04:00' \
  --resolution QUARTER_HOUR \
  --out data
```

Optional Green Button audit/backfill:

```sh
python3 eversmart.py --account 'your-account-id' --out data --green-button
```

Do not run Green Button exports on every poll; they start server-side export jobs.

## Output layout

Each successful poll creates a timestamped directory under `data/` with files such as:

- `metadata.json`
- `usage_*.json`, `cost_*.json`, `pricing_*.json`, `weather_*.json`, `demand_maxima_*.json`
- `usage.csv`, `cost.csv`, `pricing.csv`, `weather.csv`, `bills.csv`, `demand_maxima.csv`, `service_points.csv`
- `manifest.json`
- optional `green_button.zip`, `green_button_rows.csv`, `green_button_manifest.json`

The scraper also regenerates a self-contained dashboard:

```sh
open dashboard.html
```

Do not publish the generated dashboard if it was built from real data.

## Dashboard interpretation notes

- Opower may publish new usage rows before corresponding monetary cost rows.
- The dashboard estimates missing/zero same-day cost from rated component price when available.
- Weather data is sparse and daily; missing temperature values are treated as missing, not zero.
- `billForecast` can fail upstream while core usage/cost data succeeds; the tool records that as an optional warning.

## Physical meter notes

`meter_inventory.md` and `meter_inventory.json` document sanitized Itron Gen5 Riva observations and capability notes. Live meter serials, barcodes, module IDs, photos, and account mappings were redacted for the public release.

## Development

```sh
python3 -m unittest discover -v
python3 -m py_compile eversmart.py dashboard.py cookie_convert.py test_eversmart.py
```

## Security policy

See `SECURITY.md`. Do not disclose credentials, cookies, tokens, account numbers, service UUIDs, or account-derived data in public issues.

## License

MIT
