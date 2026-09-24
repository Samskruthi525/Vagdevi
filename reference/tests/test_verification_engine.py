"""Unit tests for the reference verification engine (INPUT -> EXPECTED -> ACTUAL)."""
import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge.verification_engine import VerificationEngine, Status, Verification  # noqa: E402
from edge.simulator import water_stream  # noqa: E402

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def base(seq=1, minutes=0, **raw_over):
    raw = {"ph": 7.1, "turbidity_ntu": 4.0, "tds_ppm": 210.0, "ec_us_cm": 420.0,
           "temperature_c": 25.0}
    raw.update(raw_over)
    return {
        "schema_version": 1, "record_id": f"SWN-0001-00001-{seq:08d}",
        "node_id": "SWN-0001", "sensor_ids": {"ph": "PH-01"},
        "timestamp": (T0 + timedelta(minutes=minutes)).isoformat(),
        "seq": seq, "mode": "NORMAL", "raw": raw,
        "power": {"battery_v": 12.5}, "comm": {"link": "UART"},
    }


def rx(minutes):
    return T0 + timedelta(minutes=minutes, seconds=2)


def rules(result):
    return {h.rule_id for h in result.rules_triggered}


def test_valid_reading_passes():
    r = VerificationEngine().verify(base(), rx(0))
    assert r.status == Status.VALID and r.verification == Verification.PASSED


def test_not_a_dict_is_invalid():
    r = VerificationEngine().verify("garbage", rx(0))
    assert r.status == Status.INVALID and "FMT-001" in rules(r)


def test_missing_field_is_invalid():
    m = base(); del m["timestamp"]
    r = VerificationEngine().verify(m, rx(0))
    assert "FMT-002" in rules(r)


def test_out_of_range_is_suspected_sensor_error():
    r = VerificationEngine().verify(base(ph=15.2), rx(0))
    assert r.status == Status.SUSPECTED_SENSOR_ERROR and "RNG-001" in rules(r)


def test_missing_sensor_value():
    r = VerificationEngine().verify(base(turbidity_ntu=None), rx(0))
    assert "MIS-001" in rules(r)


def test_non_numeric_value():
    r = VerificationEngine().verify(base(ph="7.1"), rx(0))
    assert "FMT-005" in rules(r) and r.status == Status.INVALID


def test_sudden_change_flagged():
    e = VerificationEngine()
    e.verify(base(1, 0), rx(0))
    r = e.verify(base(2, 5, ph=10.5), rx(5))
    assert "ROC-001" in rules(r) and r.status == Status.ABNORMAL


def test_future_timestamp_rejected():
    r = VerificationEngine().verify(base(minutes=60), rx(0))
    assert "TS-002" in rules(r)


def test_naive_timestamp_rejected():
    m = base(); m["timestamp"] = "2026-09-01T00:00:00"
    assert "TS-001" in rules(VerificationEngine().verify(m, rx(0)))


def test_duplicate_record_rejected():
    e = VerificationEngine()
    m = base()
    e.verify(m, rx(0))
    r = e.verify(copy.deepcopy(m), rx(0))
    assert "DUP-001" in rules(r)


def test_unregistered_node():
    e = VerificationEngine({"registered_nodes": {"SWN-0002": {"sensor_ids": []}}})
    assert "NID-001" in rules(e.verify(base(), rx(0)))


def test_sequence_gap_is_comm_error():
    e = VerificationEngine()
    e.verify(base(1, 0), rx(0))
    r = e.verify(base(5, 5), rx(5))
    assert "COM-001" in rules(r) and r.status == Status.COMMUNICATION_ERROR


def test_stuck_sensor():
    e = VerificationEngine()
    r = None
    for i in range(6):
        r = e.verify(base(i + 1, i * 5, ph=7.1 + i * 0.01), rx(i * 5))
    assert "HLT-001" in rules(r)  # temperature identical 6 times


def test_tds_ec_consistency():
    r = VerificationEngine().verify(base(tds_ppm=400.0), rx(0))
    assert "CON-001" in rules(r)


def test_attention_band_only_when_configured():
    assert "ABN-001" not in rules(VerificationEngine().verify(base(ph=5.0), rx(0)))
    e = VerificationEngine({"attention_bands": {"ph": [6.0, 9.0]}})
    assert "ABN-001" in rules(e.verify(base(ph=5.0), rx(0)))


def test_raw_value_not_modified():
    m = base(ph=15.2)
    snapshot = copy.deepcopy(m)
    r = VerificationEngine().verify(m, rx(0))
    assert m == snapshot and r.raw_snapshot["ph"] == 15.2


def test_hash_is_stable():
    a = VerificationEngine().verify(base(), rx(0)).raw_sha256
    b = VerificationEngine().verify(base(), rx(0)).raw_sha256
    assert a == b and len(a) == 64


def test_simulated_stream_triggers_expected_rules():
    e = VerificationEngine()
    seen = set()
    for m in water_stream():
        ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
        seen |= rules(e.verify(m, ts + timedelta(seconds=3)))
    assert {"ROC-001", "MIS-001", "RNG-001", "COM-001", "HLT-001", "CON-001"} <= seen
