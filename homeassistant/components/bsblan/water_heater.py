"""BSBLAN platform to control a compatible Water Heater Device."""

from typing import Any, override

from bsblan import BSBLANError, HotWaterConfig, HotWaterState, SetHotWaterParam

from homeassistant.components.water_heater import (
    STATE_ECO,
    STATE_OFF,
    STATE_PERFORMANCE,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BSBLanConfigEntry, BSBLanData
from .const import DOMAIN
from .entity import BSBLanWaterHeaterDeviceEntity
from .helpers import first_temperature, validate_setpoint_order

PARALLEL_UPDATES = 1

# Mapping between BSBLan operating mode values and HA operation modes
BSBLAN_TO_HA_OPERATION_MODE: dict[int, str] = {
    0: STATE_OFF,  # Protection mode
    1: STATE_PERFORMANCE,  # Continuous comfort mode
    2: STATE_ECO,  # Eco/automatic mode
}

HA_TO_BSBLAN_OPERATION_MODE: dict[str, int] = {
    v: k for k, v in BSBLAN_TO_HA_OPERATION_MODE.items()
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BSBLanConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up BSBLAN water heater based on a config entry."""
    data = entry.runtime_data

    # Only create water heater entity if DHW (Domestic Hot Water) is available
    dhw_data = data.fast_coordinator.data.dhw
    if dhw_data is None:
        # Device does not support DHW, skip water heater setup
        return
    if (
        dhw_data.operating_mode is None
        and dhw_data.nominal_setpoint is None
        and dhw_data.dhw_actual_value_top_temperature is None
    ):
        # No DHW functionality available, skip water heater setup
        return

    async_add_entities([BSBLANWaterHeater(data)])


class BSBLANWaterHeater(BSBLanWaterHeaterDeviceEntity, WaterHeaterEntity):
    """Defines a BSBLAN water heater entity."""

    _attr_name = None
    _attr_min_temp = 10.0
    _attr_operation_list = list(HA_TO_BSBLAN_OPERATION_MODE.keys())
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.ON_OFF
    )

    def __init__(self, data: BSBLanData) -> None:
        """Initialize BSBLAN water heater."""
        super().__init__(data.fast_coordinator, data.slow_coordinator, data)
        self._attr_unique_id = format_mac(data.device.MAC)

        # Set temperature unit
        self._attr_native_temperature_unit = (
            data.fast_coordinator.client.get_temperature_unit
        )
        # Initialize available attribute to resolve multiple inheritance conflict
        self._attr_available = True

    @property
    @override
    def max_temp(self) -> float:
        """Return the maximum temperature.

        Derived from the slow-coordinator DHW config, which may still be
        pending when the platform is set up. Falls back to the default until
        the config becomes available.
        """
        dhw_config = self._dhw_config
        if (
            dhw_config is not None
            and dhw_config.nominal_setpoint_max is not None
            and dhw_config.nominal_setpoint_max.value is not None
        ):
            return dhw_config.nominal_setpoint_max.value
        return 65.0  # Default maximum

    @property
    def _dhw(self) -> HotWaterState:
        """Return DHW state data.

        This entity is only created when DHW data is available.
        """
        dhw = self.coordinator.data.dhw
        assert dhw is not None
        return dhw

    @property
    def _dhw_config(self) -> HotWaterConfig | None:
        """Return the slow-coordinator DHW config.

        It may still be pending when the platform is set up.
        """
        return (
            self.slow_coordinator.data.dhw_config
            if self.slow_coordinator.data
            else None
        )

    @property
    def _nominal_setpoint(self) -> float | None:
        """Return the nominal DHW setpoint."""
        return first_temperature(self._dhw.nominal_setpoint)

    @property
    def _reduced_setpoint(self) -> float | None:
        """Return the reduced DHW setpoint."""
        if (dhw_config := self._dhw_config) is None:
            return None
        return first_temperature(dhw_config.reduced_setpoint)

    @property
    def _reduced_setpoint_active(self) -> bool:
        """Return whether the controller currently applies the reduced setpoint."""
        current = first_temperature(self._dhw.dhw_current_setpoint)
        return (
            current is not None
            and current != self._nominal_setpoint
            and current == self._reduced_setpoint
        )

    @property
    @override
    def current_operation(self) -> str | None:
        """Return current operation."""
        if (
            operating_mode := self._dhw.operating_mode
        ) is None or operating_mode.value is None:
            return None
        return BSBLAN_TO_HA_OPERATION_MODE.get(operating_mode.value)

    @property
    @override
    def native_current_temperature(self) -> float | None:
        """Return the current temperature."""
        if (current_temp := self._dhw.dhw_actual_value_top_temperature) is None:
            return None
        return current_temp.value

    @property
    @override
    def native_target_temperature(self) -> float | None:
        """Return the setpoint the controller currently applies."""
        return first_temperature(
            self._dhw.dhw_current_setpoint, self._dhw.nominal_setpoint
        )

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the setpoint currently in effect."""
        temperature = kwargs[ATTR_TEMPERATURE]
        reduced = self._reduced_setpoint_active
        self._validate_setpoint(temperature, reduced=reduced)
        await self._async_set_setpoint(temperature, reduced=reduced)

    @override
    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Set new operation mode."""
        # Base class validates operation_mode is in operation_list before calling
        bsblan_mode = HA_TO_BSBLAN_OPERATION_MODE[operation_mode]
        # Send numeric value as string - BSB-LAN API expects numeric mode values
        await self._async_set_hot_water(
            SetHotWaterParam(operating_mode=str(bsblan_mode)),
            "set_operation_mode_error",
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the water heater on."""
        await self.async_set_operation_mode(STATE_PERFORMANCE)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the water heater off."""
        await self.async_set_operation_mode(STATE_OFF)

    def _validate_setpoint(self, temperature: float, *, reduced: bool) -> None:
        """Keep the reduced setpoint at or below the nominal setpoint."""
        if reduced:
            validate_setpoint_order(
                temperature, "reduced_above_nominal", maximum=self._nominal_setpoint
            )
        else:
            validate_setpoint_order(
                temperature, "nominal_below_reduced", minimum=self._reduced_setpoint
            )

    async def _async_set_setpoint(self, temperature: float, *, reduced: bool) -> None:
        """Write the nominal or reduced setpoint."""
        if reduced:
            await self._async_set_hot_water(
                SetHotWaterParam(reduced_setpoint=temperature),
                "set_temperature_error",
                refresh_config=True,
            )
        else:
            await self._async_set_hot_water(
                SetHotWaterParam(nominal_setpoint=temperature),
                "set_temperature_error",
            )

    async def _async_set_hot_water(
        self,
        params: SetHotWaterParam,
        error_translation_key: str,
        *,
        refresh_config: bool = False,
    ) -> None:
        """Write a hot water parameter, then refresh the state."""
        try:
            await self.coordinator.client.set_hot_water(params)
        except BSBLANError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=error_translation_key,
            ) from err
        if refresh_config:
            await self.slow_coordinator.async_request_refresh()
        await self.coordinator.async_request_refresh()
