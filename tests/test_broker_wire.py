"""Exercise the observer against an isolated, genuine MQTT broker."""

from __future__ import annotations

import asyncio
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import paho.mqtt.client as paho
import pytest
from homeassistant.components.mqtt.models import ReceiveMessage

from custom_components.lanbon_mqtt.hub import LanbonHub
from custom_components.lanbon_mqtt.sensor import (
    DESCRIPTIONS,
    LanbonObservationSensor,
)

BROKER_EXE = Path(os.environ.get("MOSQUITTO_EXE") or shutil.which("mosquitto") or "mosquitto")
GROUP = "112233445566"
DEVICE = "AABBCCDDEEFF"
BASE = f"homeassistant/{GROUP}/light/{DEVICE}"


def wait_until(predicate, description: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        time.sleep(0.02)


class LocalBroker:
    """A child broker with one ephemeral loopback listener and no persistence."""

    def __init__(self, directory: Path) -> None:
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            self.port = port_probe.getsockname()[1]
        self.config = directory / "mosquitto.conf"
        self.config.write_text(
            f"listener {self.port} 127.0.0.1\n"
            "allow_anonymous true\n"
            "persistence false\n"
            "log_type all\n",
            encoding="ascii",
        )
        self.log_path = directory / "mosquitto.log"
        self.log = self.log_path.open("ab", buffering=0)
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [str(BROKER_EXE), "-c", str(self.config)],
            cwd=BROKER_EXE.parent,
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def ready() -> bool:
            assert self.process is not None
            if self.process.poll() is not None:
                raise AssertionError(
                    f"Broker exited early: {self.log_path.read_text(errors='replace')}"
                )
            with socket.socket() as probe:
                probe.settimeout(0.1)
                return probe.connect_ex(("127.0.0.1", self.port)) == 0

        wait_until(ready, "loopback broker listener")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def close(self) -> None:
        self.stop()
        self.log.close()


class BrokerReceiver:
    """Feed real broker deliveries into the observer's HA MQTT callback seam."""

    def __init__(self, port: int) -> None:
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.connected = threading.Event()
        self.subscriptions: list[tuple[str, object]] = []
        self.subscription_acks = 0
        self.connection_callback = None
        self.client = paho.Client(
            callback_api_version=paho.CallbackAPIVersion.VERSION2,
            client_id="lanbon-wire-observer",
            protocol=paho.MQTTv5,
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=1)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe
        self.client.connect("127.0.0.1", port, keepalive=5)
        self.client.loop_start()
        wait_until(self.connected.is_set, "observer MQTT connection")

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self.events.put(("error", str(reason_code)))
            return
        self.connected.set()
        self.events.put(("status", True))
        for topic, _ in tuple(self.subscriptions):
            client.subscribe(topic, options=paho.SubscribeOptions(qos=0, retainHandling=0))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected.clear()
        self.events.put(("status", False))

    def _on_message(self, client, userdata, message) -> None:
        self.events.put(("message", message))

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties) -> None:
        self.subscription_acks += 1

    async def subscribe(self, hass, topic, callback, *, qos, encoding):
        assert qos == 0 and encoding is None
        self.subscriptions.append((topic, callback))
        result, _ = self.client.subscribe(
            topic, options=paho.SubscribeOptions(qos=0, retainHandling=0)
        )
        assert result == paho.MQTT_ERR_SUCCESS

        def unsubscribe() -> None:
            self.subscriptions.remove((topic, callback))
            if self.connected.is_set():
                self.client.unsubscribe(topic)

        return unsubscribe

    def subscribe_connection(self, hass, callback):
        self.connection_callback = callback

        def unsubscribe() -> None:
            self.connection_callback = None

        return unsubscribe

    def drain_until(self, predicate, description: str, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Timed out waiting for {description}")
            try:
                kind, value = self.events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if kind == "error":
                raise AssertionError(f"Observer MQTT connection failed: {value}")
            if kind == "status" and self.connection_callback is not None:
                self.connection_callback(value)
            if kind == "message":
                for topic, callback in tuple(self.subscriptions):
                    if paho.topic_matches_sub(topic, value.topic):
                        callback(
                            ReceiveMessage(
                                topic=value.topic,
                                payload=bytes(value.payload),
                                qos=value.qos,
                                retain=value.retain,
                                subscribed_topic=topic,
                                timestamp=time.time(),
                            )
                        )

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


def test_observer_with_isolated_mosquitto(tmp_path: Path) -> None:
    if not BROKER_EXE.is_file():
        pytest.skip("Install Mosquitto on PATH or set MOSQUITTO_EXE to run the broker wire test")

    broker = LocalBroker(tmp_path)
    broker.start()
    sender = paho.Client(
        callback_api_version=paho.CallbackAPIVersion.VERSION2,
        client_id="lanbon-wire-seed",
        protocol=paho.MQTTv5,
    )
    sender_connected = threading.Event()
    sender_connections = 0

    def sender_on_connect(client, userdata, flags, reason, properties) -> None:
        nonlocal sender_connections
        if not reason.is_failure:
            sender_connections += 1
            sender_connected.set()

    def sender_on_disconnect(client, userdata, flags, reason, properties) -> None:
        sender_connected.clear()

    sender.on_connect = sender_on_connect
    sender.on_disconnect = sender_on_disconnect
    receiver = None
    hub = None
    try:
        sender.reconnect_delay_set(min_delay=1, max_delay=1)
        sender.connect("127.0.0.1", broker.port, keepalive=5)
        sender.loop_start()
        wait_until(sender_connected.is_set, "seed MQTT connection")

        def send(topic: str, payload: str, *, retained: bool = False) -> None:
            result = sender.publish(topic, payload, qos=1, retain=retained)
            result.wait_for_publish(timeout=5)
            assert result.is_published()

        send(f"{BASE}/state", "ON", retained=True)
        receiver = BrokerReceiver(broker.port)
        hub = LanbonHub(object(), GROUP, "homeassistant", 300)
        with (
            patch("custom_components.lanbon_mqtt.hub.mqtt.is_connected", return_value=True),
            patch("custom_components.lanbon_mqtt.hub.mqtt.async_subscribe", receiver.subscribe),
            patch(
                "custom_components.lanbon_mqtt.hub.mqtt.async_subscribe_connection_status",
                receiver.subscribe_connection,
            ),
            patch(
                "custom_components.lanbon_mqtt.hub.async_track_time_interval",
                return_value=lambda: None,
            ),
            patch(
                "custom_components.lanbon_mqtt.hub.mqtt.async_publish", new_callable=AsyncMock
            ) as observer_publish,
        ):
            asyncio.run(hub.async_start())
            receiver.drain_until(
                lambda: (
                    (DEVICE, None) in hub.store.devices
                    and "power" in hub.store.devices[(DEVICE, None)].cache
                ),
                "retained ON replay",
            )
            device = hub.store.devices[(DEVICE, None)]
            power = LanbonObservationSensor(hub, device, DESCRIPTIONS[0])
            brightness = LanbonObservationSensor(hub, device, DESCRIPTIONS[1])
            assert not power.available
            assert power.native_value is None
            assert power.extra_state_attributes["cached_replay_value"] == "ON"

            send(f"{BASE}/brightnessState", "50")
            send(f"{BASE}/state", "OFF")
            receiver.drain_until(
                lambda: (
                    device.fields.get("power") is not None
                    and device.fields["power"].value == "OFF"
                    and device.fields.get("brightness") is not None
                ),
                "fresh OFF and brightness",
            )
            assert power.available and power.native_value == "OFF"
            assert brightness.available and brightness.native_value == 50
            assert power.extra_state_attributes["cached_replay_value"] == "ON"

            # A new subscription still receives the broker's older retained ON.
            # It must remain cache evidence after the newer live OFF.
            previous_replays = hub.store.retained_messages
            result, _ = receiver.client.subscribe(
                f"homeassistant/{GROUP}/#",
                options=paho.SubscribeOptions(qos=0, retainHandling=0),
            )
            assert result == paho.MQTT_ERR_SUCCESS
            receiver.drain_until(
                lambda: hub.store.retained_messages > previous_replays,
                "stale ON replay after live OFF",
            )
            assert power.native_value == "OFF"
            assert power.extra_state_attributes["cached_replay_value"] == "ON"

            previous_epoch = hub.store.epoch
            previous_acks = receiver.subscription_acks
            previous_sender_connections = sender_connections
            broker.stop()
            receiver.drain_until(lambda: not hub.store.connected, "broker disconnect")
            assert not power.available and not brightness.available
            broker.start()
            receiver.drain_until(
                lambda: hub.store.connected and hub.store.epoch > previous_epoch,
                "observer reconnect",
            )
            assert not power.available and not brightness.available
            wait_until(
                lambda: receiver.subscription_acks >= previous_acks + 3,
                "all observer subscriptions restored",
            )
            wait_until(
                lambda: sender_connections > previous_sender_connections,
                "seed reconnect",
            )
            send(f"{BASE}/brightnessState", "30")
            send(f"{BASE}/state", "OFF")
            receiver.drain_until(
                lambda: power.available and brightness.available,
                "fresh reports after reconnect",
            )
            assert power.native_value == "OFF"
            assert brightness.native_value == 30
            observer_publish.assert_not_called()
            hub.async_stop()
    finally:
        if hub is not None:
            hub.async_stop()
        if receiver is not None:
            receiver.close()
        sender.disconnect()
        sender.loop_stop()
        broker.close()

    broker_log = broker.log_path.read_text(errors="replace")
    assert "Received PUBLISH from lanbon-wire-seed" in broker_log
    assert "Received PUBLISH from lanbon-wire-observer" not in broker_log
