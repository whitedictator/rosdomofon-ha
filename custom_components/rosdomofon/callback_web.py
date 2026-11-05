"""Short-lived HA-hosted callback page. The browser never receives login secrets."""

from __future__ import annotations

import asyncio
from dataclasses import fields
import time
import html
import json
import logging
from pathlib import Path
import secrets
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from .callback_auth import CallbackApi, CallbackChallenge

_LOGGER = logging.getLogger(__name__)
KEY = "rosdomofon_callback_sessions"
TEMPLATE = Path(__file__).with_name("callback_page.html")


def render_page(status: dict, nonce: str, template: str) -> str:
    seconds = max(0, (status["remaining_ms"] + 999) // 1000)
    replacements = {
        "__TEL__": html.escape(status["tel"], quote=True),
        "__NUMBER__": html.escape(status["number"]),
        "__TIME__": f"{seconds // 60:02d}:{seconds % 60:02d}",
        "__NONCE__": html.escape(nonce, quote=True),
        "__RETURN_URL__": "/config/integrations/dashboard",
        "__CSRF__": html.escape(status.get("csrf", ""), quote=True),
        "__STATE__": json.dumps(status, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026"),
    }
    for old, new in replacements.items():
        template = template.replace(old, new)
    return template


def page_headers(nonce):
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    }


class CallbackPages:
    def __init__(self, hass, template):
        self.hass = hass
        self.template = template
        self.sessions = {}

    def add(self, challenge: CallbackChallenge, api: CallbackApi, flow_id: str):
        if len(self.sessions) >= 16:
            raise RuntimeError("Too many active callback flows")
        session_id = secrets.token_urlsafe(32)
        record = {
            "challenge": challenge,
            "task": None,
            "api": api,
            "flow_id": flow_id,
            "csrf": secrets.token_urlsafe(24),
            "retries": 0,
            "retry_after": 0,
            "lock": asyncio.Lock(),
            "closed": False,
        }
        self.sessions[session_id] = record
        self.start_wait(record)
        record["cleanup"] = self.hass.loop.call_later(600, self.discard, session_id)
        return session_id

    def start_wait(self, record):
        async def wait():
            challenge = record["challenge"]
            await challenge.wait(record["api"])
            if challenge.state != "confirmed" or record["closed"]:
                return
            try:
                await self.hass.config_entries.flow.async_configure(record["flow_id"])
            except Exception as error:
                _LOGGER.debug("Callback flow completion: %s", type(error).__name__)

        record["task"] = self.hass.async_create_background_task(wait(), "rosdomofon callback login")

    def public_status(self, record):
        status = record["challenge"].public_status()
        status["can_retry"] = (
            not record["closed"]
            and record["retries"] < 3
            and status["state"] in ("expired", "error")
        )
        status["retry_remaining_ms"] = max(
            0, round((record["retry_after"] - time.monotonic()) * 1000)
        )
        return status

    async def retry(self, session_id, csrf):
        record = self.sessions.get(session_id)
        if not record or record["closed"]:
            raise web.HTTPGone()
        if not secrets.compare_digest(str(csrf), record["csrf"]):
            raise web.HTTPForbidden()
        if record["lock"].locked():
            raise web.HTTPConflict()
        async with record["lock"]:
            public = self.public_status(record)
            if not public["can_retry"] or public["retry_remaining_ms"]:
                raise web.HTTPConflict()
            record["retries"] += 1
            challenge = record["challenge"]
            try:
                new = await record["api"].begin(challenge.phone)
                if record["closed"]:
                    new.secret = ""
                    raise web.HTTPGone()
                for field in fields(CallbackChallenge):
                    setattr(challenge, field.name, getattr(new, field.name))
                self.start_wait(record)
            except web.HTTPException:
                raise
            except Exception as error:
                challenge.state = "error"
                challenge.error = (
                    str(error)
                    if str(error) in ("rate_limited", "cannot_connect", "callback_unavailable")
                    else "service_error"
                )
                record["retry_after"] = time.monotonic() + 30
            return self.public_status(record)

    def release(self, session_id):
        record = self.sessions.get(session_id)
        if not record:
            return
        record["closed"] = True
        challenge = record["challenge"]
        if challenge.state == "pending" and record["task"] and not record["task"].done():
            challenge.state = "cancelled"
            record["task"].cancel()
        challenge.secret = ""
        challenge.token_data = None
        challenge.phone = ""
        # Keep only a public success/expired tombstone for the open browser page.

    def discard(self, session_id):
        record = self.sessions.get(session_id)
        if record and record.get("cleanup"):
            record["cleanup"].cancel()
        self.release(session_id)
        self.sessions.pop(session_id, None)


class CallbackPageView(HomeAssistantView):
    url = "/api/rosdomofon/callback/{session_id}"
    extra_urls = [url + "/status", url + "/retry"]
    name = "api:rosdomofon:callback"
    requires_auth = False  # 256-bit short-lived nonce, read-only status only.

    def __init__(self, pages):
        self.pages = pages

    async def get(self, request, session_id):
        record = self.pages.sessions.get(session_id)
        if not record:
            raise web.HTTPNotFound()
        public = self.pages.public_status(record)
        if request.path.endswith("/status"):
            return web.json_response(
                public, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            )
        public["csrf"] = record["csrf"]
        nonce = secrets.token_urlsafe(24)
        return web.Response(
            text=render_page(public, nonce, self.pages.template),
            content_type="text/html",
            headers=page_headers(nonce),
        )

    async def post(self, request, session_id):
        if not request.path.endswith("/retry") or (request.content_length or 0) > 256:
            raise web.HTTPBadRequest()
        public = await self.pages.retry(
            session_id, request.headers.get("X-Rosdomofon-Callback", "")
        )
        return web.json_response(
            public, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )


async def async_get_pages(hass):
    if KEY not in hass.data:
        template = await hass.async_add_executor_job(TEMPLATE.read_text)
        pages = CallbackPages(hass, template)
        hass.http.register_view(CallbackPageView(pages))
        hass.data[KEY] = pages
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.core import callback

        @callback
        def stop_pages(event):
            for session_id in list(pages.sessions):
                pages.discard(session_id)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_pages)
    return hass.data[KEY]
