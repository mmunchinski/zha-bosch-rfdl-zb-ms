"""Test fixtures for Bosch RFDL-ZB-MS quirk tests."""

from unittest import mock

import pytest
import zigpy.device
import zigpy.types

from zhaquirks.const import (
    DEVICE_TYPE,
    ENDPOINTS,
    INPUT_CLUSTERS,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
)


@pytest.fixture
def zigpy_device_from_quirk():
    """Create a mock zigpy device and apply a quirk class to it."""

    def _create(quirk_cls):
        app = mock.MagicMock()
        app.get_device.return_value = mock.MagicMock()
        ieee = zigpy.types.EUI64.convert("00:11:22:33:44:55:66:77")
        nwk = 0x1234

        # Build a base device matching the quirk's signature
        device = zigpy.device.Device(app, ieee, nwk)
        for ep_id, ep_data in quirk_cls.signature[ENDPOINTS].items():
            ep = device.add_endpoint(ep_id)
            ep.profile_id = ep_data[PROFILE_ID]
            ep.device_type = ep_data[DEVICE_TYPE]
            for cluster_id in ep_data[INPUT_CLUSTERS]:
                ep.add_input_cluster(cluster_id)
            for cluster_id in ep_data[OUTPUT_CLUSTERS]:
                ep.add_output_cluster(cluster_id)

        # Apply the quirk
        return quirk_cls(app, ieee, nwk, device)

    return _create
