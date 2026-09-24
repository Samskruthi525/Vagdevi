"""
Deterministic simulators used ONLY to generate illustrative documentation charts.

Nothing produced here is a field measurement. All figures generated from this
module are labelled "SIMULATED" in the README.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from .verification_engine import make_record_id


def water_stream(node_id: str = "SWN-0001", n: int = 288, seed: int = 7,
                 start: datetime | None = None, interval_min: int = 5,
                 inject_faults: bool = True) -> List[Dict]:
    """Generate n raw messages (default = 24 h at 5-min interval)."""
    rnd = random.Random(seed)
    start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
    msgs = []
    seq = 0
    for i in range(n):
        t = start + timedelta(minutes=interval_min * i)
        hour = (t.hour + t.minute / 60)
        temp = 24 + 4 * math.sin((hour - 9) / 24 * 2 * math.pi) + rnd.gauss(0, 0.15)
        ph = 7.2 + 0.08 * math.sin(i / 30) + rnd.gauss(0, 0.03)
        ec = 420 + 25 * math.sin(i / 50) + rnd.gauss(0, 4)
        tds = 0.5 * ec + rnd.gauss(0, 3)
        turb = 4 + 1.5 * abs(math.sin(i / 40)) + abs(rnd.gauss(0, 0.3))
        raw = {"ph": round(ph, 3), "turbidity_ntu": round(turb, 2),
               "tds_ppm": round(tds, 1), "ec_us_cm": round(ec, 1),
               "temperature_c": round(temp, 2)}
        seq += 1
        if inject_faults:
            if i == 60:
                raw["ph"] = 9.9                      # sudden jump -> ROC-001
            if 120 <= i < 124:
                raw["turbidity_ntu"] = None          # sensor disconnected -> MIS-001
            if i == 150:
                raw["tds_ppm"] = 1450.0              # out of range -> RNG-001
            if i == 200:
                seq += 3                             # lost packets -> COM-001
            if 230 <= i < 238:
                raw["temperature_c"] = 25.00         # stuck sensor -> HLT-001
            if i == 260:
                raw["tds_ppm"] = round(raw["tds_ppm"] * 1.9, 1)  # CON-001
        msgs.append({
            "schema_version": 1,
            "record_id": make_record_id(node_id, 1, seq),
            "node_id": node_id,
            "sensor_ids": {"ph": "PH-01", "turbidity_ntu": "TU-01", "tds_ppm": "TD-01",
                           "ec_us_cm": "EC-01", "temperature_c": "TP-01"},
            "timestamp": t.isoformat().replace("+00:00", "Z"),
            "seq": seq,
            "mode": "NORMAL",
            "raw": raw,
            "power": {"battery_v": 12.6, "source": "SOLAR"},
            "comm": {"link": "UART", "rssi": None},
        })
    return msgs


def power_profile(hours: int = 96, step_min: int = 10, seed: int = 11) -> Dict[str, List[float]]:
    """Normalised (0..1) generation profiles for a 4-day weather sequence:
    Day1 sunny/low wind, Day2 cloudy/windy, Day3 rain/intermittent wind,
    Day4 storm/low sun, high gusty wind (turbine may furl/brake)."""
    rnd = random.Random(seed)
    n = hours * 60 // step_min
    t, solar, wind, load = [], [], [], []
    cloud = [0.05, 0.6, 0.85, 0.9]
    wind_mean = [0.12, 0.55, 0.3, 0.8]
    for i in range(n):
        h = i * step_min / 60
        day = min(int(h // 24), 3)
        hod = h % 24
        sun = max(0.0, math.sin((hod - 6) / 12 * math.pi)) if 6 <= hod <= 18 else 0.0
        s = sun * (1 - cloud[day]) * (0.9 + 0.1 * rnd.random())
        w = max(0.0, rnd.gauss(wind_mean[day], 0.12))
        if day == 3 and w > 0.95:
            w = 0.0          # over-speed protection: turbine braked
        w = min(w, 1.0) ** 3 * 1.0
        t.append(h)
        solar.append(s)
        wind.append(min(w, 1.0))
        load.append(0.18 + (0.05 if (i % 6 == 0) else 0.0))
    return {"t": t, "solar": solar, "wind": wind, "load": load}
