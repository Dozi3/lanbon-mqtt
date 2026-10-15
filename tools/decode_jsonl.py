"""Translate saved MQTT payloads to JSON; stdlib only, no network or HA runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lanbon_portable_protocol", ROOT / "custom_components/lanbon_mqtt/protocol.py"
)
PROTOCOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROTOCOL)


def decode_row(row: dict) -> dict:
    """Keep structural decoding apart from optional transport provenance.

    Saved rows never select an installed-device sync profile. A recognized
    layout can therefore have unqualified model semantics; extra input fields
    cannot turn its raw output into normalized power or brightness.
    """
    if not isinstance(row, dict) or not isinstance(row.get("payload_hex"), str):
        raise ValueError("Each row needs topic and payload_hex")
    if len(row["payload_hex"]) > PROTOCOL.MAX_PAYLOAD * 2:
        raise ValueError("Payload exceeds decoder limit")
    if "retained" in row and not isinstance(row["retained"], bool):
        raise ValueError("retained must be a JSON boolean when supplied")
    topic = row.get("topic")
    if not isinstance(topic, str):
        raise ValueError("topic must be a string")
    payload = bytes.fromhex(row["payload_hex"])
    event = PROTOCOL.parse_message(topic, payload)
    return {
        "protocol": event,
        "transport": {
            "retained": row.get("retained"),
            "received_utc": row.get("received_utc"),
            "freshness_evaluated": False,
        },
        "physical_state_verified": False,
        "supported_layout": event is not None
        and event["kind"] not in {"unresolved_binary", "unresolved_direction"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="UTF-8 JSON Lines file, or - for stdin")
    args = parser.parse_args()
    source = sys.stdin if args.input == "-" else Path(args.input).open(encoding="utf-8")
    try:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            if len(line) > 200000:
                raise ValueError(f"Input row {line_number} is too long")
            try:
                result = decode_row(json.loads(line))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid input row {line_number}: {exc}") from None
            print(json.dumps(result, separators=(",", ":")))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if source is not sys.stdin:
            source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
