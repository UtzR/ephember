# Ember API & Protocol Specification (Updated)

This document consolidates all protocol, scheduling, and PointIndex behavior
discovered so far through reverse-engineering of the Ember mobile application,
HTTP traffic, and MQTT payloads.

---

## 1. Time Encoding & Scheduling

### 1.1 Time Unit
All schedule times are encoded as:

    encoded_time = minutes_since_midnight / 10

Examples:
- 07:00 → 42
- 08:20 → 50
- 10:10 → 61
- 23:50 → 143

UI enforces **10‑minute granularity only**.

### 1.2 Program Structure
Each day supports **up to three periods (P1, P2, P3)** with startTime and endTime.

### 1.3 Disabled Periods
If startTime == endTime, the period is disabled but retained by backend.

---

## 2. Weekday Encoding

0 = Sunday … 6 = Saturday

---

## 3. Temperature Encoding

Temperature values are stored as integer ×10.

---

## 4. MQTT Payload Structure

MQTT topic:
productId/uid/download/pointdata

pointData is base64 encoded PointIndex/value pairs.

---

## 5. PointIndex Summary

3 = Mode  
5 = Current temperature  
6 = Target temperature  
7 = Operating mode  
10 = System state  
14 = Schedule temperature  
15 = Schedule bitmap  
16 = Constant  
17 = Status flags  

---

## 6. Schedule Bitmap

PointIndex 15 encodes all daily schedules (P1–P3, 7 days).

---

## 7. Observed Rules

• 10-minute resolution  
• Zero-length periods allowed  
• Schedule independent from setpoint  

---

## 8. Status

Document updated from live logs and MQTT captures.
