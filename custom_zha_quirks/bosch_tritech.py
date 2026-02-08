"""Quirk for Bosch RFDL-ZB-MS TriTech motion sensor.

This quirk provides:
1. Virtual occupancy cluster with configurable timeout for reliable motion clear
2. Aggressive poll control configuration for better responsiveness
3. Communication health tracking and stuck state detection
4. Enhanced logging for diagnostics

Originally designed for iControl security systems, these sensors need extra
handling to work reliably in consumer Zigbee networks.

https://github.com/zigpy/zha-device-handlers
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from zigpy.quirks import CustomCluster, CustomDevice
from zigpy.profiles import zha
from zigpy.zcl.clusters.general import (
    Basic,
    Identify,
    Ota,
    PollControl,
    PowerConfiguration,
)
from zigpy.zcl.clusters.homeautomation import Diagnostic
from zigpy.zcl.clusters.measurement import (
    IlluminanceMeasurement,
    OccupancySensing,
    TemperatureMeasurement,
)
from zigpy.zcl.clusters.security import IasZone
from zigpy.zcl import foundation

from zhaquirks import Bus, LocalDataCluster, PowerConfigurationCluster
from zhaquirks.bosch import BOSCH
from zhaquirks.const import (
    DEVICE_TYPE,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    MOTION_EVENT,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
)

_LOGGER = logging.getLogger(__name__)

# Motion handling configuration
MOTION_TIMEOUT_S = 120  # Clear occupancy after this many seconds of no motion
STUCK_MOTION_THRESHOLD_S = 30  # If occupied longer than this when clear arrives, treat as new motion
STUCK_WARNING_THRESHOLD_S = 1800  # Warn if occupied for 30+ minutes without new events
MOTION_CLEAR_EVENT = "motion_clear"
COMMUNICATION_EVENT = "device_communication"

# Poll control configuration (in quarter-seconds)
CHECKIN_INTERVAL = 3600  # 15 minutes (3600 quarter-seconds) - more aggressive than default
FAST_POLL_TIMEOUT = 40  # 10 seconds of fast polling after check-in


class BoschPowerConfiguration(PowerConfigurationCluster):
    """Power configuration with voltage-to-percentage conversion for Bosch sensors."""

    MIN_VOLTS = 1.9  # Minimum voltage (0%)
    MAX_VOLTS = 3.0  # Maximum voltage (100%)


class BoschPollControl(CustomCluster, PollControl):
    """Poll control cluster with aggressive check-in configuration.

    Configures the device to check in more frequently than the default,
    allowing faster detection of communication failures.
    """

    cluster_id = PollControl.cluster_id

    async def bind(self):
        """Bind cluster and configure aggressive polling."""
        result = await super().bind()

        _LOGGER.info(
            "Bosch RFDL-ZB-MS [%s]: Configuring poll control - "
            "check-in interval: %d quarter-sec (%d min), fast poll timeout: %d quarter-sec",
            self.endpoint.device.ieee,
            CHECKIN_INTERVAL,
            CHECKIN_INTERVAL // 240,
            FAST_POLL_TIMEOUT,
        )

        try:
            # Write check-in interval (attribute 0x0000)
            await self.write_attributes({
                "checkin_interval": CHECKIN_INTERVAL,
                "fast_poll_timeout": FAST_POLL_TIMEOUT,
            })
            _LOGGER.info(
                "Bosch RFDL-ZB-MS [%s]: Poll control configured successfully",
                self.endpoint.device.ieee,
            )
        except Exception as e:
            _LOGGER.warning(
                "Bosch RFDL-ZB-MS [%s]: Failed to configure poll control: %s",
                self.endpoint.device.ieee,
                e,
            )

        return result

    def handle_cluster_request(
        self,
        hdr: foundation.ZCLHeader,
        args: list[Any],
        *,
        dst_addressing: None = None,
    ) -> None:
        """Handle check-in from device - indicates device is alive."""
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

        if hdr.command_id == 0:  # Check-in command
            _LOGGER.debug(
                "Bosch RFDL-ZB-MS [%s]: Device checked in (poll control)",
                self.endpoint.device.ieee,
            )
            # Notify that we received communication
            self.endpoint.device.motion_bus.listener_event(COMMUNICATION_EVENT)


class BoschIasZone(CustomCluster, IasZone):
    """IAS Zone cluster with enhanced tracking and forwarding to virtual occupancy."""

    cluster_id = IasZone.cluster_id

    def __init__(self, *args, **kwargs):
        """Initialize with tracking state."""
        super().__init__(*args, **kwargs)
        self._last_zone_status = None
        self._zone_status_count = 0

    def handle_cluster_request(
        self,
        hdr: foundation.ZCLHeader,
        args: list[Any],
        *,
        dst_addressing: None = None,
    ) -> None:
        """Handle zone status change notifications with enhanced logging."""
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

        if hdr.command_id == 0:  # Zone status change notification
            zone_status = args[0] if args else 0
            self._zone_status_count += 1

            # Parse zone status bits
            alarm1 = bool(zone_status & 0x01)  # Motion
            tamper = bool(zone_status & 0x04)  # Tamper
            battery_low = bool(zone_status & 0x08)  # Low battery
            supervision = bool(zone_status & 0x20)  # Supervision reports

            _LOGGER.debug(
                "Bosch RFDL-ZB-MS [%s]: Zone status 0x%04X - motion=%s, tamper=%s, "
                "low_battery=%s, supervision=%s (msg #%d)",
                self.endpoint.device.ieee,
                zone_status,
                alarm1,
                tamper,
                battery_low,
                supervision,
                self._zone_status_count,
            )

            # Warn on concerning status bits
            if tamper:
                _LOGGER.warning(
                    "Bosch RFDL-ZB-MS [%s]: TAMPER ALERT - check device mounting",
                    self.endpoint.device.ieee,
                )
            if battery_low:
                _LOGGER.warning(
                    "Bosch RFDL-ZB-MS [%s]: LOW BATTERY reported by device",
                    self.endpoint.device.ieee,
                )

            self._last_zone_status = zone_status

            # Notify communication received
            self.endpoint.device.motion_bus.listener_event(COMMUNICATION_EVENT)

            # Forward motion events
            if alarm1:
                self.endpoint.device.motion_bus.listener_event(MOTION_EVENT)
            else:
                self.endpoint.device.motion_bus.listener_event(MOTION_CLEAR_EVENT)


class BoschOccupancy(LocalDataCluster, OccupancySensing):
    """Virtual occupancy cluster with guaranteed timeout and health tracking.

    Provides:
    - Software-based motion timeout that clears occupancy reliably
    - Communication health tracking
    - Stuck state detection with warnings
    - Event counting for diagnostics
    """

    cluster_id = OccupancySensing.cluster_id

    _CONSTANT_ATTRIBUTES = {
        0x0010: 0,  # PIR sensor type
    }

    def __init__(self, *args, **kwargs):
        """Initialize cluster with tracking state."""
        super().__init__(*args, **kwargs)
        self._update_attribute(0x0000, 0)  # occupancy = unoccupied
        self.endpoint.device.motion_bus.add_listener(self)

        # Timers
        self._timer_handle = None
        self._stuck_check_handle = None
        self._init_clear_handle = None

        # Tracking state
        self._occupied_since = None
        self._last_communication = time.monotonic()
        self._last_motion_event = None
        self._motion_event_count = 0
        self._clear_event_count = 0
        self._communication_count = 0
        self._stuck_warnings = 0

        # Start periodic stuck state check
        self._schedule_stuck_check()

        # Schedule a delayed clear to override HA's state restoration
        self._schedule_init_clear()

    def _cancel_timers(self):
        """Cancel all pending timers for clean teardown."""
        for handle in (self._timer_handle, self._stuck_check_handle, self._init_clear_handle):
            if handle is not None:
                handle.cancel()
        self._timer_handle = None
        self._stuck_check_handle = None
        self._init_clear_handle = None

    def _schedule_stuck_check(self):
        """Schedule periodic check for stuck state."""
        try:
            loop = asyncio.get_running_loop()
            self._stuck_check_handle = loop.call_later(300, self._check_stuck_state)
        except RuntimeError:
            _LOGGER.debug(
                "Bosch RFDL-ZB-MS: No running event loop for stuck check scheduling"
            )

    def _schedule_init_clear(self):
        """Schedule a delayed clear to override HA's state restoration."""
        try:
            loop = asyncio.get_running_loop()
            self._init_clear_handle = loop.call_later(5, self._init_clear)
        except RuntimeError:
            _LOGGER.debug(
                "Bosch RFDL-ZB-MS: No running event loop for init clear scheduling"
            )

    def _init_clear(self):
        """Clear occupancy on startup if no motion detected."""
        self._init_clear_handle = None
        # Only clear if we haven't received any motion events since init
        if self._motion_event_count == 0:
            _LOGGER.info(
                "Bosch RFDL-ZB-MS [%s]: Clearing stale occupancy state on startup",
                self.endpoint.device.ieee,
            )
            self._update_attribute(0x0000, 0)  # occupancy = unoccupied
            self._occupied_since = None

    def _check_stuck_state(self):
        """Check if device appears stuck and log warning."""
        try:
            now = time.monotonic()

            # Check if occupied for too long without new motion events
            if self._occupied_since is not None:
                occupied_duration = now - self._occupied_since
                if self._last_motion_event is None:
                    _LOGGER.warning(
                        "Bosch RFDL-ZB-MS [%s]: Occupied but no motion event recorded - "
                        "clearing invalid state",
                        self.endpoint.device.ieee,
                    )
                    self._clear_occupancy()
                    self._schedule_stuck_check()
                    return
                time_since_last_motion = now - self._last_motion_event

                if (occupied_duration > STUCK_WARNING_THRESHOLD_S and
                    time_since_last_motion > STUCK_WARNING_THRESHOLD_S):
                    self._stuck_warnings += 1
                    _LOGGER.warning(
                        "Bosch RFDL-ZB-MS [%s]: STUCK STATE DETECTED - "
                        "occupied for %d min with no new motion events. "
                        "Consider checking device. (warning #%d)",
                        self.endpoint.device.ieee,
                        int(occupied_duration / 60),
                        self._stuck_warnings,
                    )

            # Check communication health
            time_since_comm = now - self._last_communication
            if time_since_comm > 3600:  # 1 hour without any communication
                _LOGGER.warning(
                    "Bosch RFDL-ZB-MS [%s]: NO COMMUNICATION for %d minutes - "
                    "device may be offline or stuck",
                    self.endpoint.device.ieee,
                    int(time_since_comm / 60),
                )

            # Reschedule
            self._schedule_stuck_check()

        except Exception as e:
            _LOGGER.debug("Stuck check error: %s", e)
            self._schedule_stuck_check()

    def device_communication(self):
        """Record that we received any communication from device."""
        self._last_communication = time.monotonic()
        self._communication_count += 1
        _LOGGER.debug(
            "Bosch RFDL-ZB-MS [%s]: Communication received (total: %d)",
            self.endpoint.device.ieee,
            self._communication_count,
        )

    def motion_event(self):
        """Handle motion event - set occupied and start/reset timer."""
        now = time.monotonic()
        self._motion_event_count += 1
        self._last_motion_event = now
        self._last_communication = now

        # Calculate time since last motion for logging
        was_occupied = self._occupied_since is not None

        if not was_occupied:
            _LOGGER.info(
                "Bosch RFDL-ZB-MS [%s]: Motion #%d detected, starting %ds timer",
                self.endpoint.device.ieee,
                self._motion_event_count,
                MOTION_TIMEOUT_S,
            )
        else:
            _LOGGER.debug(
                "Bosch RFDL-ZB-MS [%s]: Motion #%d detected (already occupied), resetting %ds timer",
                self.endpoint.device.ieee,
                self._motion_event_count,
                MOTION_TIMEOUT_S,
            )

        self._update_attribute(0x0000, 1)  # occupancy = occupied
        self._occupied_since = now

        if self._timer_handle:
            self._timer_handle.cancel()

        try:
            loop = asyncio.get_running_loop()
            self._timer_handle = loop.call_later(MOTION_TIMEOUT_S, self._clear_occupancy)
        except RuntimeError:
            _LOGGER.error(
                "Bosch RFDL-ZB-MS [%s]: No running event loop - cannot schedule clear timer",
                self.endpoint.device.ieee,
            )

    def motion_clear(self):
        """Handle clear event from hardware.

        Treats clear as new motion in two scenarios:
        1. Not currently occupied - sensor was stuck, this clear indicates new activity
        2. Occupied longer than STUCK_MOTION_THRESHOLD_S - sensor reset due to new motion
           but sent clear instead of motion event (known iControl firmware behavior)

        Clears arriving within the threshold are normal operation and ignored.
        """
        self._clear_event_count += 1
        self._last_communication = time.monotonic()

        if self._occupied_since is None:
            _LOGGER.info(
                "Bosch RFDL-ZB-MS [%s]: Clear #%d received while NOT occupied - "
                "treating as motion (stuck sensor reset)",
                self.endpoint.device.ieee,
                self._clear_event_count,
            )
            self.motion_event()
            return

        occupied_duration = time.monotonic() - self._occupied_since

        if occupied_duration >= STUCK_MOTION_THRESHOLD_S:
            _LOGGER.info(
                "Bosch RFDL-ZB-MS [%s]: Clear #%d after %ds (>%ds threshold) - "
                "treating as new motion (sensor reset)",
                self.endpoint.device.ieee,
                self._clear_event_count,
                int(occupied_duration),
                STUCK_MOTION_THRESHOLD_S,
            )
            self.motion_event()
        else:
            _LOGGER.debug(
                "Bosch RFDL-ZB-MS [%s]: Clear #%d after %ds - "
                "normal clear, timer will handle",
                self.endpoint.device.ieee,
                self._clear_event_count,
                int(occupied_duration),
            )

    def _clear_occupancy(self):
        """Clear occupancy after timeout."""
        if self._occupied_since is not None:
            occupied_duration = time.monotonic() - self._occupied_since
            _LOGGER.info(
                "Bosch RFDL-ZB-MS [%s]: Clearing occupancy after %ds timeout "
                "(was occupied for %ds)",
                self.endpoint.device.ieee,
                MOTION_TIMEOUT_S,
                int(occupied_duration),
            )

        self._update_attribute(0x0000, 0)  # occupancy = unoccupied
        self._timer_handle = None
        self._occupied_since = None


class BoschRFDLZBMS(CustomDevice):
    """Bosch RFDL-ZB-MS RADION TriTech motion sensor.

    Commercial security sensor repurposed for consumer Zigbee networks.
    Requires special handling for reliable operation.
    """

    def __init__(self, *args, **kwargs):
        """Initialize device with motion bus for inter-cluster communication."""
        self.motion_bus = Bus()
        super().__init__(*args, **kwargs)
        _LOGGER.info(
            "Bosch RFDL-ZB-MS [%s]: Device initialized with enhanced quirk",
            self.ieee,
        )

    signature = {
        MODELS_INFO: [(BOSCH, "RFDL-ZB-MS")],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0402,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    PowerConfiguration.cluster_id,
                    Identify.cluster_id,
                    PollControl.cluster_id,
                    IlluminanceMeasurement.cluster_id,
                    TemperatureMeasurement.cluster_id,
                    IasZone.cluster_id,
                    Diagnostic.cluster_id,
                ],
                OUTPUT_CLUSTERS: [
                    Ota.cluster_id,
                ],
            }
        },
    }

    replacement = {
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0402,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    BoschPowerConfiguration,
                    Identify.cluster_id,
                    BoschPollControl,  # Custom poll control with aggressive check-in
                    IlluminanceMeasurement.cluster_id,
                    TemperatureMeasurement.cluster_id,
                    BoschIasZone,  # Enhanced IAS Zone with tracking
                    Diagnostic.cluster_id,
                    BoschOccupancy,  # Virtual occupancy with health monitoring
                ],
                OUTPUT_CLUSTERS: [
                    Ota.cluster_id,
                ],
            }
        },
    }
