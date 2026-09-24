"""
Reference verification engine for the Smart Water Monitoring & Purification System.

This module is the reference implementation used by the main repository to
generate documentation charts and to demonstrate the documented rule set.
The production implementation lives in `smart-water-raspberry-pi/src/validator/`.

Design principles
-----------------
* The raw reading is NEVER modified. The engine returns a separate
  VerificationResult that references the raw record by its record_id.
* Every rule has a stable identifier (e.g. RNG-001) that is documented in
  the README "Verification Rule Catalogue".
* The engine classifies data according to documented rules only. It does not
  claim to determine whether a chemical reading is scientifically "true".
* All thresholds are configuration values (TBD / CONFIGURABLE). The defaults
  below are PLACEHOLDER sensor operating ranges for simulation only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


class Status(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    ABNORMAL = "ABNORMAL"
    SUSPECTED_SENSOR_ERROR = "SUSPECTED_SENSOR_ERROR"
    COMMUNICATION_ERROR = "COMMUNICATION_ERROR"


class Verification(str, Enum):
    PASSED = "VERIFICATION_PASSED"
    FAILED = "VERIFICATION_FAILED"


PARAMETERS = ("ph", "turbidity_ntu", "tds_ppm", "ec_us_cm", "temperature_c")

REQUIRED_FIELDS = (
    "schema_version", "record_id", "node_id", "sensor_ids", "timestamp",
    "seq", "mode", "raw", "power", "comm",
)

# PLACEHOLDER defaults. Replace with the datasheet operating ranges of the
# sensors actually purchased. These are NOT water-safety limits.
DEFAULT_CONFIG: Dict[str, Any] = {
    "ranges": {
        "ph": [0.0, 14.0],
        "turbidity_ntu": [0.0, 3000.0],
        "tds_ppm": [0.0, 1000.0],
        "ec_us_cm": [0.0, 2000.0],
        "temperature_c": [-10.0, 60.0],
    },
    # Maximum plausible change per minute (TBD / CONFIGURABLE)
    "max_rate_per_min": {
        "ph": 0.5,
        "turbidity_ntu": 200.0,
        "tds_ppm": 100.0,
        "ec_us_cm": 200.0,
        "temperature_c": 2.0,
    },
    # Project-configured "attention" bands. Leaving them None disables ABNORMAL
    # classification for that parameter. They must come from a verified source
    # or project decision, never invented.
    "attention_bands": {
        "ph": None,
        "turbidity_ntu": None,
        "tds_ppm": None,
        "ec_us_cm": None,
        "temperature_c": None,
    },
    # EC/TDS consistency: TDS ~= k * EC. k is sensor/water dependent (TBD).
    "tds_ec_factor": 0.5,
    "tds_ec_tolerance": 0.35,           # relative tolerance
    "stuck_value_window": 6,            # identical consecutive readings
    "max_clock_skew_s": 300,
    "max_age_s": 7 * 24 * 3600,         # older than this is rejected as stale
    "registered_nodes": {},             # node_id -> {"sensor_ids": {...}}
}


@dataclass
class RuleHit:
    rule_id: str
    parameter: Optional[str]
    status: Status
    reason: str


@dataclass
class VerificationResult:
    record_id: str
    node_id: Optional[str]
    verified_at: str
    status: Status
    verification: Verification
    rules_triggered: List[RuleHit] = field(default_factory=list)
    raw_snapshot: Dict[str, Any] = field(default_factory=dict)
    raw_sha256: str = ""
    engine_version: str = "0.1.0"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["verification"] = self.verification.value
        for h in d["rules_triggered"]:
            h["status"] = h["status"].value if isinstance(h["status"], Status) else h["status"]
        return d


# Severity order used to derive the overall status from all rule hits.
_SEVERITY = {
    Status.VALID: 0,
    Status.ABNORMAL: 1,
    Status.SUSPECTED_SENSOR_ERROR: 2,
    Status.COMMUNICATION_ERROR: 3,
    Status.INVALID: 4,
}


def canonical_sha256(message: Dict[str, Any]) -> str:
    """Hash of the canonical JSON form of the raw message (tamper evidence)."""
    blob = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    return ts


class VerificationEngine:
    """Stateful per-node verification engine (keeps short history per node)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if config:
            for k, v in config.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        self.cfg = cfg
        self._last: Dict[str, Dict[str, Any]] = {}
        self._history: Dict[str, List[Dict[str, float]]] = {}
        self._seen_ids: set = set()

    # ------------------------------------------------------------------ API
    def verify(self, msg: Any, received_at: Optional[datetime] = None) -> VerificationResult:
        received_at = received_at or datetime.now(timezone.utc)
        hits: List[RuleHit] = []

        # FMT: format validation ------------------------------------------
        if not isinstance(msg, dict):
            return self._result(None, None, [RuleHit("FMT-001", None, Status.INVALID,
                                "Message is not a JSON object")], {}, received_at)
        missing = [f for f in REQUIRED_FIELDS if f not in msg]
        if missing:
            hits.append(RuleHit("FMT-002", None, Status.INVALID,
                                f"Missing required fields: {', '.join(missing)}"))
            return self._result(msg.get("record_id"), msg.get("node_id"), hits, msg, received_at)
        if msg["schema_version"] != 1:
            hits.append(RuleHit("FMT-003", None, Status.INVALID,
                                f"Unsupported schema_version {msg['schema_version']}"))
        raw = msg["raw"]
        if not isinstance(raw, dict):
            hits.append(RuleHit("FMT-004", None, Status.INVALID, "raw block is not an object"))
            return self._result(msg["record_id"], msg["node_id"], hits, msg, received_at)

        # DUP: duplicate record id ----------------------------------------
        if msg["record_id"] in self._seen_ids:
            hits.append(RuleHit("DUP-001", None, Status.INVALID,
                                "Duplicate record_id (already processed)"))
            return self._result(msg["record_id"], msg["node_id"], hits, msg, received_at)

        # NID: node identity ----------------------------------------------
        nodes = self.cfg["registered_nodes"]
        node = nodes.get(msg["node_id"]) if nodes else None
        if nodes and node is None:
            hits.append(RuleHit("NID-001", None, Status.INVALID,
                                f"Node {msg['node_id']} is not registered"))
        elif node is not None:
            expected = set(node.get("sensor_ids", []))
            got = set((msg.get("sensor_ids") or {}).values())
            if expected and not got.issubset(expected):
                hits.append(RuleHit("NID-002", None, Status.INVALID,
                                    "Sensor ID not registered to this node"))

        # TS: timestamp validation ----------------------------------------
        ts = _parse_ts(msg["timestamp"])
        if ts is None:
            hits.append(RuleHit("TS-001", None, Status.INVALID,
                                "Timestamp missing, malformed, or without timezone"))
        else:
            skew = (ts - received_at).total_seconds()
            if skew > self.cfg["max_clock_skew_s"]:
                hits.append(RuleHit("TS-002", None, Status.INVALID,
                                    f"Timestamp {int(skew)} s in the future"))
            elif -skew > self.cfg["max_age_s"]:
                hits.append(RuleHit("TS-003", None, Status.INVALID,
                                    "Timestamp older than configured max age"))
            last = self._last.get(msg["node_id"])
            if last and last.get("ts") and ts <= last["ts"]:
                hits.append(RuleHit("TS-004", None, Status.INVALID,
                                    "Timestamp not monotonic for this node"))

        # COM: communication indicators -----------------------------------
        last = self._last.get(msg["node_id"])
        if last and isinstance(msg.get("seq"), int) and msg["seq"] > last["seq"] + 1:
            gap = msg["seq"] - last["seq"] - 1
            hits.append(RuleHit("COM-001", None, Status.COMMUNICATION_ERROR,
                                f"Sequence gap: {gap} message(s) missing"))

        # Per-parameter checks --------------------------------------------
        values: Dict[str, float] = {}
        for p in PARAMETERS:
            v = raw.get(p, None)
            if v is None:
                hits.append(RuleHit("MIS-001", p, Status.SUSPECTED_SENSOR_ERROR,
                                    f"{p} missing (sensor not responding)"))
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                hits.append(RuleHit("FMT-005", p, Status.INVALID, f"{p} is not numeric"))
                continue
            lo, hi = self.cfg["ranges"][p]
            if v < lo or v > hi:
                hits.append(RuleHit("RNG-001", p, Status.SUSPECTED_SENSOR_ERROR,
                                    f"{p}={v} outside sensor operating range [{lo}, {hi}]"))
                continue
            values[p] = float(v)
            band = self.cfg["attention_bands"].get(p)
            if band:
                blo, bhi = band
                if (blo is not None and v < blo) or (bhi is not None and v > bhi):
                    hits.append(RuleHit("ABN-001", p, Status.ABNORMAL,
                                        f"{p}={v} outside project attention band {band}"))

        # ROC: rate of change ---------------------------------------------
        if last and ts is not None and last.get("ts") and ts > last["ts"]:
            minutes = max((ts - last["ts"]).total_seconds() / 60.0, 1e-6)
            for p, v in values.items():
                pv = last["values"].get(p)
                if pv is None:
                    continue
                rate = abs(v - pv) / minutes
                if rate > self.cfg["max_rate_per_min"][p]:
                    hits.append(RuleHit("ROC-001", p, Status.ABNORMAL,
                                        f"{p} changed {rate:.2f}/min (limit "
                                        f"{self.cfg['max_rate_per_min'][p]})"))

        # HLT: stuck sensor -----------------------------------------------
        hist = self._history.setdefault(msg["node_id"], [])
        window = self.cfg["stuck_value_window"]
        for p, v in values.items():
            recent = [h.get(p) for h in hist[-(window - 1):]]
            if len(recent) == window - 1 and all(r == v for r in recent):
                hits.append(RuleHit("HLT-001", p, Status.SUSPECTED_SENSOR_ERROR,
                                    f"{p} identical for {window} consecutive samples"))

        # CON: TDS/EC consistency -----------------------------------------
        if "tds_ppm" in values and "ec_us_cm" in values and values["ec_us_cm"] > 0:
            expected_tds = self.cfg["tds_ec_factor"] * values["ec_us_cm"]
            if expected_tds > 0:
                rel = abs(values["tds_ppm"] - expected_tds) / expected_tds
                if rel > self.cfg["tds_ec_tolerance"]:
                    hits.append(RuleHit("CON-001", "tds_ppm", Status.SUSPECTED_SENSOR_ERROR,
                                        f"TDS/EC inconsistent ({rel*100:.0f}% deviation)"))

        # Update state only for structurally acceptable records
        if not any(h.status == Status.INVALID for h in hits):
            self._seen_ids.add(msg["record_id"])
            self._last[msg["node_id"]] = {"ts": ts, "seq": msg["seq"], "values": values}
            hist.append(values)
            del hist[:-50]

        return self._result(msg["record_id"], msg["node_id"], hits, msg, received_at)

    # --------------------------------------------------------------- helpers
    def _result(self, record_id, node_id, hits, msg, received_at) -> VerificationResult:
        status = Status.VALID
        for h in hits:
            if _SEVERITY[h.status] > _SEVERITY[status]:
                status = h.status
        verification = Verification.PASSED if status == Status.VALID else Verification.FAILED
        snapshot = msg.get("raw", {}) if isinstance(msg, dict) else {}
        return VerificationResult(
            record_id=record_id or "UNKNOWN",
            node_id=node_id,
            verified_at=received_at.isoformat(),
            status=status,
            verification=verification,
            rules_triggered=hits,
            raw_snapshot=dict(snapshot) if isinstance(snapshot, dict) else {},
            raw_sha256=canonical_sha256(msg) if isinstance(msg, dict) else "",
        )


def make_record_id(node_id: str, boot_id: int, seq: int) -> str:
    """Deterministic, collision-resistant record id: <node>-<boot>-<seq>."""
    return f"{node_id}-{boot_id:05d}-{seq:08d}"
