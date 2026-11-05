"""Incoming call received directly from the provider, without Android."""

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    receiver = hass.data[DOMAIN][entry.entry_id]["call_receiver"]
    async_add_entities([IncomingCall(receiver, entry.entry_id)])


class IncomingCall(BinarySensorEntity):
    _attr_name = "Росдомофон: входящий звонок"
    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_should_poll = False

    def __init__(self, receiver, entry_id):
        self.receiver = receiver
        self._attr_unique_id = entry_id + "_incoming_call"

    @property
    def available(self):
        return self.receiver.available

    @property
    def is_on(self):
        return self.receiver.state == "ringing"

    @property
    def extra_state_attributes(self):
        c = self.receiver.call or {}
        return {
            "source": "direct_fcm_rdnp",
            "last_received": self.receiver.received,
            "call_id": c.get("public_id", 0),
            "entity_id": c.get("entity_id", ""),
            "expires": c.get("expires", 0),
            "reason": self.receiver.reason,
        }

    async def async_added_to_hass(self):
        self.async_on_remove(self.receiver.subscribe(lambda _event: self.async_write_ha_state()))
