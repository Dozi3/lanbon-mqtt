"""The exported JSON translator works without starting Home Assistant or MQTT."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_saved_samples_translate_without_conflating_off_and_brightness():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/decode_jsonl.py"),
            str(ROOT / "examples/messages.jsonl"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[0]["protocol"]["fields"] == {"power": "OFF"}
    assert rows[1]["protocol"]["fields"] == {"brightness": 73}
    profile = rows[2]["protocol"]["candidate_profile"]
    assert profile["interpretation"] == "unqualified_sync_profile"
    assert profile["confidence"] == "conditional_on_unverified_type_extension"
    assert "output_percent" not in profile and "reported_power" not in profile
    assert rows[2]["protocol"]["fields"]["output_code"] == 0
    assert rows[2]["protocol"]["confidence"] == "structural"
    assert rows[3]["protocol"]["devices"][0]["firmware_version"] == "3.0.48"
    assert all(row["physical_state_verified"] is False for row in rows)
    assert all(row["transport"]["freshness_evaluated"] is False for row in rows)


def test_invalid_transport_provenance_is_rejected():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/decode_jsonl.py"), "-"],
        input='{"topic":"some/topic","payload_hex":"00","retained":"false"}\n',
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "retained must be a JSON boolean" in result.stderr


def test_saved_row_cannot_opt_itself_into_a_qualified_sync_model():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/decode_jsonl.py"), "-"],
        input=json.dumps(
            {
                "topic": "lanbon/a/MAC:0200000000F0/s2m/cmdStatusSynchronous",
                "payload_hex": "e4000000000200000000024d",
                "qualified_sync": True,
            }
        )
        + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    event = json.loads(result.stdout)["protocol"]
    assert event["fields"]["output_code"] == 228
    assert event["candidate_profile"]["interpretation"] == "unqualified_sync_profile"
    assert "output_percent" not in event["candidate_profile"]
