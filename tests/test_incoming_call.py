"""Entity state follows receiver updates; this is not a real panel call test."""

from types import SimpleNamespace
import logging
from homeassistant.helpers.entity_component import EntityComponent
from custom_components.rosdomofon.binary_sensor import IncomingCall


async def test_incoming_call_on_off_and_unavailable(hass):
    listeners = []
    receiver = SimpleNamespace(
        available=True,
        state="idle",
        call=None,
        received=0,
        reason="",
        subscribe=lambda cb: (listeners.append(cb), lambda: listeners.remove(cb))[1],
    )
    entity = IncomingCall(receiver, "entry-test")
    entity.entity_id = "binary_sensor.test_incoming_call"
    component = EntityComponent(logging.getLogger(__name__), "binary_sensor", hass)
    await component.async_add_entities([entity])
    entity.async_write_ha_state()
    assert hass.states.get(entity.entity_id).state == "off"
    receiver.state = "ringing"
    receiver.call = {"public_id": "public-test", "entity_id": "lock.test", "expires": 123}
    listeners[0]({})
    assert hass.states.get(entity.entity_id).state == "on"
    assert hass.states.get(entity.entity_id).attributes["entity_id"] == "lock.test"
    receiver.state = "ended"
    listeners[0]({})
    assert hass.states.get(entity.entity_id).state == "off"
    receiver.available = False
    listeners[0]({})
    assert hass.states.get(entity.entity_id).state == "unavailable"
    await entity.async_remove()
    assert not listeners
