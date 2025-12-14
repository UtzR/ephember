# Ember API – Updated Reverse‑Engineered Notes

> **Disclaimer**: I have no connection with EPH Controls. This API and protocol behaviour has been reverse‑engineered from traffic captures and may change at any time. Use at your own risk.

This document is an **updated and extended version** of the original API.md, incorporating **all discoveries so far**, including:

* Schedule encoding behaviour (P1/P2/P3)
* Time granularity and rounding rules
* Clarification of `deviceDays`, `startTime`, `endTime`
* Extended PointIndex understanding
* Behavioural notes observed via HTTP + MQTT + mobile UI

---

## High‑Level Architecture

The Ember system uses **dual transport**:

* **HTTPS + JSON** – authentication, metadata, schedules, zone state
* **MQTT + binary pointdata (base64)** – real‑time updates and control

---

## HTTP Endpoints

Base URL:

```
https://eu-https.topband-cloud.com/ember-back
```

Authentication and basic home/zone discovery remain unchanged from the original document and are preserved verbatim (Login, Refresh Token, Select User, Homes List, Homes Detail).

---

## Zone & Schedule Model (Important Updates)

### deviceDays

Each zone contains a `deviceDays` array with **7 entries**:

| dayType | Meaning |
|-------|--------|
| 0 | Sunday |
| 1 | Monday |
| 2 | Tuesday |
| 3 | Wednesday |
| 4 | Thursday |
| 5 | Friday |
| 6 | Saturday |

Each `deviceDays[x]` entry defines **up to 3 schedule periods**: `p1`, `p2`, `p3`.

---

## Schedule Period Encoding (P1 / P2 / P3)

### Time Encoding

**startTime** and **endTime** are encoded as:

```
minutes_since_midnight / 10
```

Examples:

| Time | Encoded value |
|-----|--------------|
| 07:00 | 42 |
| 08:20 | 50 |
| 10:00 | 60 |
| 10:10 | 61 |
| 19:00 | 114 |
| 19:10 | 115 |
| 23:50 | 143 |

> ⚠️ The UI only allows **10‑minute steps**. Values are always multiples of 10 minutes.

---

### Zero‑Length Periods (Critical Discovery)

If a period start time equals end time:

```
startTime == endTime
```

Then that schedule period is treated as **disabled / inactive**.

Examples observed:

* `10:00 → 10:00`
* `19:10 → 19:10`

These produce valid `p1` / `p2` entries in JSON but **do not activate heating**.

---

### Period Ordering Rules

* Periods must be **monotonically increasing** in time
* Overlaps are silently corrected or rejected by the backend
* A later period may start immediately after a previous one ends

Example (valid):

```
P1: 07:00 → 08:20
P2: 19:00 → 22:00
P3: 23:50 → 23:50 (disabled)
```

---

### Program Names

`pmName` values observed:

* `"P1"`, `"P2"`, `"P3"`
* Empty string when the UI saves zero‑length or cleared periods

`pmName` has **no functional effect**; scheduling relies solely on `startTime` / `endTime`.

---

## Target Temperature vs Schedule

* Schedule periods define **when the zone is active**
* The **actual target temperature** is controlled via **PointIndex 6** (MQTT / HTTP state)
* The schedule does **not** encode temperature per period

This means Ember uses a **single scheduled on/off window** combined with a **global target temp**.

---

## MQTT Overview (Unchanged Core, Extended Interpretation)

MQTT endpoint:

```
eu-base-mqtt.topband-cloud.com:18883 (TLS)
```

Authentication uses:

* Username: `app/<refresh_token>`
* Password: `<refresh_token>`

---

## PointData Encoding (Confirmed)

Binary format (base64‑encoded):

```
[Header][Index][Type][Value...]
```

* Header: always observed as `0x00`
* Index: PointIndex
* Type: PointType
* Value: 1–4 bytes

Multiple point updates may be packed into a single payload.

---

## Point Types (Updated)

| Type | Bytes | Meaning |
|----|------|--------|
| 1 | 1 | Boolean / small integer |
| 2 | 2 | Temperature (signed, ×10) |
| 4 | 2 | Temperature (signed, ×10) |
| 5 | 4 | Epoch timestamp |

Temperature example:

```
195 → 19.5°C
206 → 20.6°C
```

---

## Zone PointIndex Map (Updated)

| Index | Meaning | Notes |
|------|--------|------|
| 3 | Unknown (Zone State?) | Often constant `12` |
| 4 | Advance Active | Boolean |
| 5 | Current Temperature | ×10 |
| 6 | Target Temperature | ×10 |
| 7 | Mode | 0=Auto,1=All Day,2=On,3=Off |
| 8 | Boost Hours | Integer |
| 9 | Boost Start Timestamp | Epoch |
| 10 | Boiler / Relay State | 1=Off,2=On |
| 11 | Unknown | Usually `0` |
| 13 | Enable Flag | Always `1` when active |
| 14 | Boost Target Temperature | ×10 |
| 15 | Schedule Bitmap / CRC | Changes when schedule edited |
| 16 | Status Bitmap | Changes with runtime state |
| 17 | Error / Alarm Code | Usually `0` |
| 18 | Unknown Counter | Changes rarely |

> Indices 15 & 16 appear to encode **schedule/state bitfields** and are updated automatically by backend.

---

## Key Behavioural Findings

* UI enforces **10‑minute schedule granularity**
* Backend accepts only encoded multiples of 10 minutes
* Zero‑length periods are treated as disabled
* Schedules do not carry temperature values
* MQTT is authoritative for live temperature changes
* HTTP `zoneProgram` reflects *derived state* after backend validation

---

## Open Questions / TODO

* Full decode of PointIndex 15 & 16 bitfields
* Meaning of PointIndex 3 & 11
* Multi‑zone synchronization logic
* Hot Water vs Heating zone subtle differences

---

## Changelog

**2025‑12**

* Added full schedule encoding model
* Documented zero‑length period behaviour
* Confirmed 10‑minute quantization
* Expanded PointIndex table
* Clarified schedule vs temperature separation

---

End of document

