# Physical meter inventory and Gen5 Riva notes

This note enriches the Eversmart scraper/dashboard context with observations from the four meter photos and the added Itron PDF docs.

Important caveats:

- The photo readings are OCR/vision observations through curved meter covers with glare and reflections.
- Treat printed labels, serials, FCC IDs, and barcodes as much more reliable than LCD register readings.
- The current Opower metadata exposes two service-point utility IDs but does not expose the physical meter serial numbers seen in the photos. Do not permanently map meter serial to account/service point until Eversource metadata, field labeling, or another source confirms it.

## Physical meters seen in photos

### Meter METER-SERIAL-1

Observed in:

- IMG_1688.jpeg
- IMG_1689.jpeg

Readable identity:

- Manufacturer: Itron
- Product family: Gen5 Riva
- Printed form/number: `2S METER-SERIAL-1`
- Barcode text: `*1NFMETER-SERIAL-1AMI80*`
- Radio/NIC/module-like ID: `NIC-ID-1` — medium confidence because of glare
- Additional ID: `MODULE-ID-1`
- FCC ID: `SK9G5R1`
- IC ID: `864G-G5R1`
- Model: `G5R1`
- UL/listing file: `E470764`

Service/spec label:

- `CL200`
- `240V`
- `3W`
- `TYPE R2SMD`
- `30TA`
- `1.0Kh`
- `KWH/LP`
- `CA 0.5`
- `FM2S`
- `60Hz`
- `w/Switch`
- `V840613`

LCD/register:

- Not reliably readable from the available photos.
- The display is too obscured by glare/cropping to use as a kWh value.

Mapping status:

- Not yet mapped to either Opower utility ID.

### Meter METER-SERIAL-2

Observed in:

- IMG_1686.jpeg
- IMG_1687.jpeg

Readable identity:

- Manufacturer: Itron
- Product family: Gen5 Riva
- Printed form/number: `2S METER-SERIAL-2`
- Barcode text: `*1NFMETER-SERIAL-2AMI80*`
- Radio/NIC/module-like ID: `NIC-ID-2`
- Additional ID: `MODULE-ID-2`
- FCC ID: `SK9G5R1`
- IC ID: `864G-G5R1`
- Model: `G5R1`
- UL/listing file: `E470764`

Service/spec label:

- `CL200`
- `240V`
- `3W`
- `TYPE R2SMD`
- `30TA`
- `1.0Kh`
- `KWH/LP`
- `CA 0.5`
- `FM2S`
- `60Hz`
- `w/Switch`
- `V840613`

LCD/register:

- IMG_1687 appears to show display code `82` and value about `10098`; likely an energy register, but only medium confidence.
- IMG_1686 appears to show display code `82` with unit `kVARh`; the numeric value is unclear and should not be treated as billing kWh.

Mapping status:

- Not yet mapped to either Opower utility ID.

## Current Opower service points

Latest local metadata currently identifies two service points under the billing account:

- `ACCOUNT-SECONDARY`
- `ACCOUNT-PRIMARY` — current default target

Both have the same service agreement UUID in current metadata but different service point UUIDs:

- `ACCOUNT-SECONDARY`: service point UUID `SERVICE-POINT-UUID-2`
- `ACCOUNT-PRIMARY`: service point UUID `SERVICE-POINT-UUID`

No physical serial number was found in the Opower metadata checked so far, so the serial/account mapping remains open.

## Itron Gen5 Riva capabilities from added PDFs

Source docs:

- `101763SP-03 Gen5 Riva Meter NAM (WEB)_final.pdf`
- `Gen5 Network_Web.pdf`
- `Positive Proof for Distributed Intelligence at the Grid Edge (WEB).pdf`

Relevant product facts:

- Itron Gen5 Riva is a single-phase electric smart meter family with distributed intelligence/edge-computing capabilities.
- The meter supports flexible two-way communication, on-demand reads, configuration updates, and firmware downloads.
- Product sheet says radio output power is 1W.
- Supports three independent profiles:
  - Load Profile: 16 channels, programmable 5/10/15/30/60-minute intervals.
  - Instrumentation Profile: 16 channels, programmable 5/10/15/30/60-minute intervals.
  - Voltage Profile: 16 channels, programmable 5/10/15/30/60-minute intervals.
- Energy quantities supported:
  - Wh delivered, received, unidirectional, net.
  - VAh delivered, received, net.
  - VARh delivered, received, net, Q1/Q2/Q3/Q4.
- Demand quantities supported:
  - Max Watts delivered, received, net, unidirectional.
  - Max VA delivered, received.
  - Max VAR delivered, received, net, Q1/Q2/Q3/Q4.
  - Min power factor delivered/received.
- Distributed intelligence data listed in the product sheet includes:
  - voltage and current waveforms
  - sub-second RMS voltages and currents
  - per-second directional per-phase Wh/VARh
  - per-second directional per-phase W/VAR
  - per-second per-phase VAh/VA
  - per-second temperature
- Events/features:
  - interval data can include recorded events and exceptions sent to head-end software
  - real-time meter event and alarm retrieval is supported by the product family
  - integrated remote disconnect/reconnect switch is supported
  - arc/micro-arcing detection at the meter socket is listed
  - outage notification/last gasp is listed: standard 25-second hold-up and extended 75-second hold-up
- Display:
  - eight-digit LCD
  - two-digit display code number
  - display duration 1–15 seconds

## Implications for Eversmart

- Our Opower DataBrowser access is only exposing a consumer/portal subset: quarter-hour usage, cost, pricing components, weather, demand maxima, bills, and optional Green Button export.
- The physical meters are capable of much richer telemetry than the consumer portal currently surfaces, including sub-second/per-second DI data and voltage/current profiles.
- If future Eversource endpoints expose head-end/AMI operations data, useful search terms and fields to look for are:
  - meter serial / meterNumber / badgeNumber / AMI ID
  - NIC / module ID / endpoint ID
  - delivered / received / net Wh
  - VAh / VARh / power factor
  - voltage profile / instrumentation profile
  - events / exceptions / outage / last gasp / PON
  - disconnect / reconnect / switch status

Structured machine-readable version:

- `meter_inventory.json`
