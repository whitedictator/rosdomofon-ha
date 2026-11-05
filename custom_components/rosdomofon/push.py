"""Direct FCM receiver and read-only RDNP signalling, independent of phones."""

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import time
from firebase_messaging import FcmPushClient, FcmRegisterConfig, FcmPushClientConfig
from firebase_messaging.fcmregister import FcmRegister
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_registry import async_get as entity_registry
from .rdnp import observe_call

_LOGGER = logging.getLogger(__name__)
BASE = "https://rdba.rosdomofon.com/"
EVENT = "rosdomofon_call_state"
# Public Firebase application configuration from the vendor application.
FCM_CONFIG = FcmRegisterConfig(
    "test-push-41d3f",
    "1:79381280520:android:8e3155987e615606",
    "AIzaSyDe6MmlfA-vRH0SpNFuOpJgJ41Nf1SlOGI",
    "79381280520",
    bundle_id="homeassistant.rosdomofon",
)


def read_private(path):
    if not path.exists():
        return None
    if path.stat().st_size > 65536:
        raise ValueError("Invalid push credential file")
    os.chmod(path, 0o600)
    return json.loads(path.read_text())


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class FreshPushClient(FcmPushClient):
    def _handle_data_message(self, message):
        # Do not ring for queued calls received after a long HA outage.
        sent = getattr(message, "sent", 0)
        if sent and time.time() - sent / 1000 > 90:
            return
        super()._handle_data_message(message)


class CallReceiver:
    def __init__(self, hass, entry, token_manager):
        self.hass = hass
        self.entry = entry
        self.tokens = token_manager
        self.path = Path(hass.config.path(".rosdomofon", entry.entry_id + "-push.json"))
        self.client = None
        self.task = None
        self.signal_task = None
        self.expire = None
        self.closed = False
        self.listeners = []
        self.seen = []
        self.keys = []
        self.call = None
        self.access_key = None
        self.push_token = None
        self.state = "idle"
        self.available = False
        self.reason = "starting"
        self.received = 0
        self.save_lock = asyncio.Lock()

    def subscribe(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback) if callback in self.listeners else None

    def publish(self, state, reason=""):
        self.state = state
        self.reason = reason
        payload = {
            "state": state,
            "reason": reason,
            "entry_id": self.entry.entry_id,
            "time": int(time.time()),
            "call_id": self.call["public_id"] if self.call else 0,
            "entity_id": self.call["entity_id"] if self.call else "",
            "expires": self.call["expires"] if self.call else 0,
        }
        self.hass.bus.async_fire(EVENT, payload)
        for callback in tuple(self.listeners):
            callback(payload)

    async def api(self, path, data=None):
        if not await self.tokens.ensure_valid_token():
            raise RuntimeError("Rosdomofon session expired")
        session = async_get_clientsession(self.hass)
        method = "GET" if data is None else "POST"
        async with session.request(
            method,
            BASE + path,
            headers={"Authorization": "Bearer " + self.tokens.access_token},
            json=data,
            timeout=ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                raise RuntimeError("Rosdomofon API HTTP " + str(response.status))
            return await response.json() if response.content_type == "application/json" else None

    async def save(self, credentials):
        async with self.save_lock:
            await self.hass.async_add_executor_job(write_private, self.path, credentials)

    def credentials_updated(self, credentials):
        self.hass.async_create_task(self.save(credentials))

    async def run(self):
        delay = 5
        while not self.closed:
            try:
                self.keys = await self.api("abonents-service/api/v2/abonents/keys")
                access = await self.api("abonents-service/api/v1/abonents/access_key")
                self.access_key = access["accessKey"]
                credentials = await self.hass.async_add_executor_job(read_private, self.path)
                self.client = FreshPushClient(
                    self.on_push,
                    FCM_CONFIG,
                    credentials,
                    self.credentials_updated,
                    config=FcmPushClientConfig(abort_on_sequential_error_count=3),
                    http_client_session=async_get_clientsession(self.hass),
                )
                # FCM registration may need more than the library's 3s default.
                FcmRegister.CLIENT_TIMEOUT = ClientTimeout(total=20)
                async with asyncio.timeout(120):
                    self.push_token = await self.client.checkin_or_register()
                await self.api(
                    "abonents-service/api/v1/push_tokens",
                    {"appName": "rd_android", "value": self.push_token, "localizationName": "ru"},
                )
                await self.client.start()
                delay = 5
                while not self.closed:
                    was_available = self.available
                    self.available = self.client.is_started()
                    if was_available != self.available:
                        self.publish(self.state, self.reason)
                    await asyncio.sleep(10)
                    if not self.client.do_listen or any(task.done() for task in self.client.tasks):
                        raise RuntimeError("Push connection stopped")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.available = False
                # No exception bodies/URLs: they may include account credentials.
                _LOGGER.warning("Direct push connection retry: %s", type(error).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            finally:
                if self.client:
                    await self.client.stop()
                    self.client = None

    def on_push(self, message, persistent_id, _context):
        data = message.get("data", message) if isinstance(message, dict) else {}
        if not isinstance(data, dict):
            return
        call_id = data.get("call-id")
        adapter = data.get("adapter-id")
        if (
            not isinstance(call_id, str)
            or not 1 <= len(call_id) <= 128
            or any(c.isspace() for c in call_id)
        ):
            return
        if not isinstance(adapter, str) or not 1 <= len(adapter) <= 128:
            return
        if (persistent_id and persistent_id in self.seen) or (
            self.call and self.call["source_id"] == call_id
        ):
            return
        allowed = next((k for k in self.keys if str(k.get("adapterId")) == adapter), None)
        if allowed is None:
            return
        entity = entity_registry(self.hass).async_get_entity_id(
            "lock", "rosdomofon", f"rosdomofon_{adapter}_{allowed['relay']}"
        )
        if not entity:
            return
        if persistent_id:
            self.seen = (self.seen + [persistent_id])[-100:]
        if self.expire:
            self.expire.cancel()
        if self.signal_task:
            self.signal_task.cancel()
        self.received = int(time.time())
        self.call = {
            "source_id": call_id,
            "public_id": secrets.randbelow(0xFFFFFFFE) + 1,
            "adapter": adapter,
            "entity_id": entity,
            "expires": self.received + 90,
        }
        self.publish("ringing", "push")
        current = self.call["public_id"]
        self.expire = self.hass.loop.call_later(90, lambda: self.end(current, "timeout"))
        self.signal_task = self.hass.async_create_task(self.observe(data, current))

    async def observe(self, data, current):
        try:
            hosts = data.get("hosts", [])
            if isinstance(hosts, str):
                hosts = json.loads(hosts)
            port = int(data.get("port", 0))
            if not isinstance(hosts, list):
                return
            for host in hosts[:3]:
                if self.closed or not self.call or self.call["public_id"] != current:
                    return
                try:
                    await observe_call(
                        host,
                        port,
                        self.push_token,
                        self.access_key,
                        data["adapter-id"],
                        data["call-id"],
                        lambda: self.end(current, "caller_hung_up"),
                        lambda: None,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.debug("Call signalling endpoint failed: %s", type(error).__name__)
            # A failed control connection is not proof that the caller hung up.
            self.reason = "signalling_unavailable"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.debug("Invalid call signalling: %s", type(error).__name__)

    def end(self, current, reason):
        if not self.call or self.call["public_id"] != current or self.state != "ringing":
            return
        if self.expire:
            self.expire.cancel()
            self.expire = None
        self.publish("ended", reason)

    async def stop(self):
        self.closed = True
        if self.expire:
            self.expire.cancel()
            self.expire = None
        for task in [self.task, self.signal_task]:
            if task:
                task.cancel()
        if self.client:
            await self.client.stop()
        for task in [self.task, self.signal_task]:
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.listeners.clear()
