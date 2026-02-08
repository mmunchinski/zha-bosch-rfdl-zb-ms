"""Tests for Bosch RFDL-ZB-MS TriTech motion sensor quirk."""

import asyncio
import time
from unittest import mock

import custom_zha_quirks.bosch_tritech
from custom_zha_quirks.bosch_tritech import (
    BoschIasZone,
    BoschOccupancy,
    BoschRFDLZBMS,
    MOTION_TIMEOUT_S,
    STUCK_MOTION_THRESHOLD_S,
)

# ZCL IAS Zone Status Change Notification frames
# Format: frame_ctrl(0x09), seq(0x21), cmd(0x00), zone_status(u16le), ext(u8), zone_id(u8), delay(u16le)
ZCL_IAS_MOTION_COMMAND = b"\x09\x21\x00\x01\x00\x00\x00\x00\x00"  # alarm1=1 (motion)
ZCL_IAS_CLEAR_COMMAND = b"\x09\x21\x00\x00\x00\x00\x00\x00\x00"  # alarm1=0 (clear)


class ClusterListener:
    """Records attribute updates and cluster commands from a cluster."""

    def __init__(self, cluster):
        self.attribute_updates = []
        self.cluster_commands = []
        cluster.add_listener(self)

    def attribute_updated(self, attrid, value, *args, **kwargs):
        self.attribute_updates.append((attrid, value))

    def cluster_command(self, tsn, command_id, args):
        self.cluster_commands.append((tsn, command_id, args))


# ---------------------------------------------------------------------------
# Device structure tests
# ---------------------------------------------------------------------------


def test_signature_structure():
    """Quirk signature has expected device identifiers and clusters."""
    sig = BoschRFDLZBMS.signature
    assert ("Bosch", "RFDL-ZB-MS") in sig["models_info"]

    ep = sig["endpoints"][1]
    assert ep["profile_id"] == 260  # ZHA profile
    assert ep["device_type"] == 0x0402
    assert 0x0500 in ep["input_clusters"]  # IAS Zone
    assert 0x0001 in ep["input_clusters"]  # Power Configuration
    assert 0x0020 in ep["input_clusters"]  # Poll Control
    assert 0x0019 in ep["output_clusters"]  # OTA


def test_device_has_motion_bus(zigpy_device_from_quirk):
    """Device initializes with a motion bus for inter-cluster communication."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    assert hasattr(device, "motion_bus")
    assert device.motion_bus is not None


def test_replacement_clusters(zigpy_device_from_quirk):
    """Replacement clusters are correctly applied."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    ep = device.endpoints[1]

    assert isinstance(ep.ias_zone, BoschIasZone)
    assert hasattr(ep, "occupancy")
    assert isinstance(ep.occupancy, BoschOccupancy)


def test_occupancy_pir_sensor_type(zigpy_device_from_quirk):
    """Occupancy cluster reports PIR sensor type."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy
    assert occupancy._CONSTANT_ATTRIBUTES.get(0x0010) == 0


# ---------------------------------------------------------------------------
# Motion / occupancy behavior tests
# ---------------------------------------------------------------------------


async def test_motion_event_sets_occupancy(zigpy_device_from_quirk):
    """Motion event transitions occupancy from 0 to 1."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy
    listener = ClusterListener(occupancy)

    assert occupancy._attr_cache.get(0x0000, 0) == 0

    occupancy.motion_event()

    assert len(listener.attribute_updates) == 1
    assert listener.attribute_updates[0] == (0x0000, 1)
    assert occupancy._occupied_since is not None


async def test_motion_timeout_clears_occupancy(zigpy_device_from_quirk):
    """Occupancy clears automatically after timeout expires."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy
    listener = ClusterListener(occupancy)

    with mock.patch.object(
        custom_zha_quirks.bosch_tritech, "MOTION_TIMEOUT_S", 0.05
    ):
        occupancy.motion_event()
        assert listener.attribute_updates[-1] == (0x0000, 1)

        await asyncio.sleep(0.1)

        assert listener.attribute_updates[-1] == (0x0000, 0)
        assert occupancy._occupied_since is None
        assert occupancy._timer_handle is None


async def test_clear_when_not_occupied_triggers_motion(zigpy_device_from_quirk):
    """Clear event while unoccupied is treated as motion (stuck sensor recovery)."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy
    listener = ClusterListener(occupancy)

    assert occupancy._occupied_since is None

    occupancy.motion_clear()

    assert len(listener.attribute_updates) == 1
    assert listener.attribute_updates[0] == (0x0000, 1)
    assert occupancy._occupied_since is not None


async def test_clear_within_threshold_ignored(zigpy_device_from_quirk):
    """Clear arriving within STUCK_MOTION_THRESHOLD_S is normal — timer handles it."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy
    listener = ClusterListener(occupancy)

    occupancy.motion_event()
    assert len(listener.attribute_updates) == 1

    # Clear arrives immediately (within 30s threshold) — normal clear, ignored
    occupancy.motion_clear()

    assert len(listener.attribute_updates) == 1
    assert occupancy._clear_event_count == 1


async def test_clear_after_threshold_triggers_motion(zigpy_device_from_quirk):
    """Clear arriving after STUCK_MOTION_THRESHOLD_S is a sensor reset — treat as motion."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy
    listener = ClusterListener(occupancy)

    occupancy.motion_event()
    assert len(listener.attribute_updates) == 1

    # Simulate time passing beyond threshold (sensor was stuck, now resetting)
    occupancy._occupied_since = time.monotonic() - STUCK_MOTION_THRESHOLD_S - 1

    occupancy.motion_clear()

    # Should have triggered new motion (timer reset)
    assert len(listener.attribute_updates) == 2
    assert listener.attribute_updates[1] == (0x0000, 1)
    assert occupancy._motion_event_count == 2


async def test_multiple_motion_events_reset_timer(zigpy_device_from_quirk):
    """Subsequent motion events cancel and restart the clear timer."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy

    with mock.patch.object(
        custom_zha_quirks.bosch_tritech, "MOTION_TIMEOUT_S", 0.1
    ):
        occupancy.motion_event()
        first_handle = occupancy._timer_handle

        await asyncio.sleep(0.05)

        occupancy.motion_event()
        second_handle = occupancy._timer_handle

        assert first_handle is not second_handle

        # 0.07s after second event — not yet timed out
        await asyncio.sleep(0.07)
        assert occupancy._attr_cache.get(0x0000, 0) == 1

        # Full timeout from second event
        await asyncio.sleep(0.05)
        assert occupancy._attr_cache.get(0x0000, 0) == 0


# ---------------------------------------------------------------------------
# IAS Zone → bus → occupancy integration tests
# ---------------------------------------------------------------------------


async def test_ias_zone_forwards_motion_to_bus(zigpy_device_from_quirk):
    """IAS Zone motion alarm propagates through the bus to set occupancy."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    ias = device.endpoints[1].ias_zone
    occupancy = device.endpoints[1].occupancy
    listener = ClusterListener(occupancy)

    hdr, args = ias.deserialize(ZCL_IAS_MOTION_COMMAND)
    ias.handle_message(hdr, args)

    assert len(listener.attribute_updates) == 1
    assert listener.attribute_updates[0] == (0x0000, 1)


async def test_ias_zone_forwards_clear_to_bus(zigpy_device_from_quirk):
    """IAS Zone clear propagates through bus to occupancy cluster."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    ias = device.endpoints[1].ias_zone
    occupancy = device.endpoints[1].occupancy
    listener = ClusterListener(occupancy)

    # Set occupied first
    occupancy.motion_event()
    initial_count = len(listener.attribute_updates)

    # Simulate stuck sensor — occupied beyond threshold
    occupancy._occupied_since = time.monotonic() - STUCK_MOTION_THRESHOLD_S - 1

    # Send clear via IAS Zone — should trigger new motion (sensor reset)
    hdr, args = ias.deserialize(ZCL_IAS_CLEAR_COMMAND)
    ias.handle_message(hdr, args)

    assert len(listener.attribute_updates) == initial_count + 1
    assert occupancy._clear_event_count == 1


# ---------------------------------------------------------------------------
# Timer management tests
# ---------------------------------------------------------------------------


async def test_cancel_timers(zigpy_device_from_quirk):
    """_cancel_timers cancels all scheduled callbacks and nulls the handles."""
    device = zigpy_device_from_quirk(BoschRFDLZBMS)
    occupancy = device.endpoints[1].occupancy

    occupancy.motion_event()
    assert occupancy._timer_handle is not None

    occupancy._cancel_timers()

    assert occupancy._timer_handle is None
    assert occupancy._stuck_check_handle is None
    assert occupancy._init_clear_handle is None
