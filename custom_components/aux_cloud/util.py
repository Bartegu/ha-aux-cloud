from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.const import AuxProducts
from .const import _LOGGER, DOMAIN, MANUFACTURER, MAX_FAILED_POLLS


class BaseEntity(CoordinatorEntity):
    def __init__(self, coordinator, device_id, entity_description):
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._device = self.coordinator.get_device_by_endpoint_id(self._device_id)
        self._attr_has_entity_name = True
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{DOMAIN}_{self._device_id.lstrip('0')}_{self.entity_description.key}"
        )

        self._cached_params = self._device.get("params", {}) if self._device else {}
        self._failed_poll_count = 0
        self._max_failed_polls = MAX_FAILED_POLLS

    @property
    def unique_id(self):
        """Return a unique ID for the sensor."""
        return self._attr_unique_id

    @property
    def device_info(self):
        """Return the device info."""
        return DeviceInfo(
            connections=(
                {(CONNECTION_NETWORK_MAC, self._device["mac"])}
                if "mac" in self._device
                else None
            ),
            identifiers={(DOMAIN, self._device_id)},
            name=self._device.get("friendlyName", "AUX"),
            manufacturer=MANUFACTURER,
            model=AuxProducts.get_device_name(self._device.get("productId", None)),
        )

    @property
    def available(self):
        """Return True if entity is available."""
        current_params = self._device.get("params", {}) if self._device else {}

        has_fresh_data = len(current_params) > 0
        has_valid_cache = len(self._cached_params) > 0 and self._failed_poll_count <= self._max_failed_polls

        return (
                self._device is not None
                and self._device.get("endpointId") is not None
                and (has_fresh_data or has_valid_cache)
        )

    @callback
    def _handle_coordinator_update(self):
        device_from_coordinator = self.coordinator.get_device_by_endpoint_id(
            self._device_id
        )
        self._device = device_from_coordinator or {}

        self.async_write_ha_state()

    def _get_device_params(self):
        """Get device parameters, falling back to cache if the API drops a packet."""
        current_params = self._device.get("params", {})
        device_name = self._device.get("friendlyName", self._device_id)

        if current_params and len(current_params) > 0:
            if self._failed_poll_count > 0:
                _LOGGER.info(
                    "Device %s connection restored after %s failed attempts.",
                    device_name,
                    self._failed_poll_count
                )
            self._failed_poll_count = 0
            self._cached_params = current_params
            return current_params

        self._failed_poll_count += 1

        if self._cached_params and self._failed_poll_count <= self._max_failed_polls:
            _LOGGER.warning(
                "Missing params for device %s (Attempt %s/%s). Using cached parameters to prevent flapping.",
                device_name,
                self._failed_poll_count,
                self._max_failed_polls
            )
            return self._cached_params
        if self._failed_poll_count == self._max_failed_polls + 1:
            _LOGGER.error(
                "Device %s has not returned valid parameters for %s consecutive polls. Marking as unavailable.",
                device_name,
                self._failed_poll_count
            )

        return {}

    async def _set_device_params(self, params: dict):
        """Set parameters on the device."""
        _LOGGER.debug("Setting %s for device %s", params, self._device.get("friendlyName", "AUX"))

        await self.coordinator.api.set_device_params(self._device, params)
        await self.coordinator.async_request_refresh()