from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.const import AuxProducts
from .const import _LOGGER, DOMAIN, MANUFACTURER, MAX_FAILED_POLLS


class DeviceStateHelper:
    """Helper class to manage device parameters cache, failsafe, and optimistic updates."""

    def __init__(self, initial_params: dict, max_failed_polls: int):
        self._cached_params = initial_params.copy() if initial_params else {}
        self._failed_poll_count = 0
        self._max_failed_polls = max_failed_polls
        self._backup_params = {}

    def is_available(self, current_params: dict) -> bool:
        """Determines if the entity should be marked as available."""
        has_fresh_data = len(current_params) > 0
        has_valid_cache = len(self._cached_params) > 0 and self._failed_poll_count <= self._max_failed_polls
        return has_fresh_data or has_valid_cache

    def get_safe_params(self, current_params: dict, device_name: str) -> dict:
        """Returns valid params from API or falls back to cache."""
        if current_params and len(current_params) > 0:
            if self._failed_poll_count > 0:
                _LOGGER.info(
                    "Device %s connection restored after %s failed attempts.",
                    device_name, self._failed_poll_count
                )
            self._failed_poll_count = 0
            self._cached_params = current_params.copy()
            return current_params

        self._failed_poll_count += 1

        if self._cached_params and self._failed_poll_count <= self._max_failed_polls:
            _LOGGER.warning(
                "Missing params for device %s (Attempt %s/%s). Using cached parameters to prevent flapping.",
                device_name, self._failed_poll_count, self._max_failed_polls
            )
            return self._cached_params

        if self._failed_poll_count == self._max_failed_polls + 1:
            _LOGGER.error(
                "Device %s has not returned valid parameters for %s consecutive polls. Marking as unavailable.",
                device_name, self._failed_poll_count
            )

        return {}

    def apply_optimistic(self, target_params_dict: dict, new_params: dict):
        """Applies new params optimistically and saves a backup for rollback."""
        self._backup_params.clear()

        # Backup only the keys we are about to change
        for key in new_params:
            if key in target_params_dict:
                self._backup_params[key] = target_params_dict[key]

        # Apply optimistically to both the device dictionary and our internal cache
        target_params_dict.update(new_params)
        self._cached_params.update(new_params)

    def rollback(self, target_params_dict: dict, failed_params: dict):
        """Rolls back the optimistic update if API call fails."""
        for key in failed_params:
            if key in self._backup_params:
                target_params_dict[key] = self._backup_params[key]
                self._cached_params[key] = self._backup_params[key]
            else:
                target_params_dict.pop(key, None)
                self._cached_params.pop(key, None)
        self._backup_params.clear()


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

        initial_params = self._device.get("params", {}) if self._device else {}
        self._state_helper = DeviceStateHelper(initial_params, MAX_FAILED_POLLS)

    @property
    def unique_id(self):
        """Return a unique ID for the sensor."""
        return self._attr_unique_id

    @property
    def device_info(self):
        """Return the device info."""
        info = DeviceInfo(
                identifiers={(DOMAIN, str(self._device_id))},
                name=str(self._device.get("friendlyName", "AUX")),
                manufacturer=MANUFACTURER,
                model=str(AuxProducts.get_device_name(self._device.get("productId", None))),
            )

        if "mac" in self._device and self._device["mac"]:
            info["connections"] = {(CONNECTION_NETWORK_MAC, str(self._device["mac"]))}

        return info

    @property
    def available(self):
        """Return True if entity is available."""
        current_params = self._device.get("params", {}) if self._device else {}
        return (
                self._device is not None
                and self._device.get("endpointId") is not None
                and self._state_helper.is_available(current_params)
        )

    @callback
    def _handle_coordinator_update(self):
        device_from_coordinator = self.coordinator.get_device_by_endpoint_id(self._device_id)
        self._device = device_from_coordinator or {}
        self.async_write_ha_state()

    def _get_device_params(self):
        """Get device parameters securely via Helper."""
        current_params = self._device.get("params", {})
        device_name = self._device.get("friendlyName", self._device_id)

        return self._state_helper.get_safe_params(current_params, device_name)

    async def _set_device_params(self, params: dict):
        """Set parameters on the device using Optimistic Updates via Helper."""
        device_name = self._device.get("friendlyName", self._device_id)
        _LOGGER.debug("Optimistically setting %s for device %s", params, device_name)

        if self._device is not None:
            if "params" not in self._device:
                self._device["params"] = {}

            self._state_helper.apply_optimistic(self._device["params"], params)

        self.async_write_ha_state()

        try:
            await self.coordinator.api.set_device_params(self._device, params)
        except Exception as err:
            _LOGGER.error("Failed to apply setting %s to %s: %s", params, device_name, err)

            if self._device is not None and "params" in self._device:
                self._state_helper.rollback(self._device["params"], params)

            self.async_write_ha_state()
            raise