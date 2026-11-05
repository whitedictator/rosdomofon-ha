"""Native callback authentication using the provider's current public app API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout

BASE = "https://rdba.rosdomofon.com"
METHODS = ["callback", "call", "sms", "telegram_or_sms", "telegram"]


class CallbackAuthError(Exception):
    """A safe public error code, never an HTTP body, phone or credential."""


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        raise CallbackAuthError("invalid_phone")
    return digits


def format_service_phone(raw: str) -> tuple[str, str]:
    if not isinstance(raw, str) or not re.fullmatch(r"\+?[\d ()-]{5,30}", raw):
        raise CallbackAuthError("invalid_challenge")
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        raise CallbackAuthError("invalid_challenge")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}", "+" + digits
    return "+" + digits, "+" + digits


class CallbackApi:
    def __init__(self, session: ClientSession):
        self.session = session

    async def request(self, path: str, *, data=None, form=None):
        try:
            kwargs = {"timeout": ClientTimeout(total=12), "allow_redirects": False}
            method = "GET"
            if data is not None:
                kwargs["json"] = data
                method = "POST"
            if form is not None:
                kwargs["data"] = form
                method = "POST"
            async with self.session.request(method, BASE + path, **kwargs) as response:
                if response.status == 429:
                    raise CallbackAuthError("rate_limited")
                if response.status != 200:
                    raise CallbackAuthError("service_error")
                if response.content_length and response.content_length > 65536:
                    raise CallbackAuthError("service_error")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise CallbackAuthError("service_error")
                import json

                return json.loads(raw)
        except CallbackAuthError:
            raise
        except (asyncio.TimeoutError, ValueError, OSError):
            raise CallbackAuthError("cannot_connect") from None
        except Exception:
            raise CallbackAuthError("service_error") from None

    async def begin(self, raw_phone: str) -> CallbackChallenge:
        phone = normalize_phone(raw_phone)
        from urllib.parse import urlencode

        methods = await self.request(
            f"/abonents-service/api/v1/abonents/{phone}/auth_available_methods?"
            + urlencode({"appName": "rd_android", "supportedMethods": ",".join(METHODS)})
        )
        if not isinstance(methods, list):
            raise CallbackAuthError("service_error")
        callback = next(
            (m for m in methods if isinstance(m, dict) and m.get("deliveryMethod") == "callback"),
            None,
        )
        if callback is None:
            raise CallbackAuthError("callback_unavailable")
        issued_at = time.monotonic()
        challenge = await self.request(
            f"/abonents-service/api/v3/abonents/{phone}/auth_code",
            data={
                "appName": "rd_android",
                "deliveryMethod": "callback",
                "supportedMethods": METHODS,
            },
        )
        if not isinstance(challenge, dict) or challenge.get("triggeredMethod") not in (
            None,
            "callback",
        ):
            raise CallbackAuthError("invalid_challenge")
        number, tel = format_service_phone(challenge.get("phone"))
        secret = challenge.get("secret")
        if not isinstance(secret, str) or not 1 <= len(secret) <= 4096:
            raise CallbackAuthError("invalid_challenge")
        try:
            duration = max(15, min(180, int(callback.get("requestTimeout", 60))))
            interval = max(3, min(15, int(callback.get("statusCheckInterval", 5))))
        except (TypeError, ValueError):
            raise CallbackAuthError("invalid_challenge") from None
        return CallbackChallenge(
            phone, number, tel, secret, issued_at + duration, duration, interval
        )

    async def status(self, phone):
        result = await self.request(
            f"/abonents-service/api/v1/abonents/{phone}/auth_status?deliveryMethod=callback"
        )
        return (
            isinstance(result, dict)
            and type(result.get("statusCode")) is int
            and result["statusCode"] == 1
        )

    async def finish(self, challenge: CallbackChallenge) -> dict:
        # Caller must have observed statusCode=1 for this exact challenge.
        if not challenge.confirmed:
            raise CallbackAuthError("not_confirmed")
        token = await self.request(
            "/authserver-service/oauth/token",
            form={
                "grant_type": "mobile_callback",
                "client_id": "abonent",
                "phone": challenge.phone,
                "secret": challenge.secret,
            },
        )
        if (
            not isinstance(token, dict)
            or not token.get("access_token")
            or not token.get("refresh_token")
        ):
            raise CallbackAuthError("service_error")
        try:
            if int(token["expires_in"]) <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise CallbackAuthError("service_error") from None
        return {**token, "timestamp": int(time.time())}


@dataclass
class CallbackChallenge:
    phone: str = field(repr=False)
    number: str
    tel: str
    secret: str = field(repr=False)
    deadline: float
    duration: int
    interval: int
    confirmed: bool = False
    state: str = "pending"
    error: str = ""
    token_data: dict[str, Any] | None = field(default=None, repr=False)

    def public_status(self):
        remaining = max(0, self.deadline - time.monotonic()) if self.state == "pending" else 0
        return {
            "state": self.state,
            "remaining_ms": round(remaining * 1000),
            "duration": self.duration,
            "number": self.number,
            "tel": self.tel,
            "error": self.error,
        }

    async def wait(self, api: CallbackApi):
        try:
            while time.monotonic() < self.deadline:
                await asyncio.sleep(min(self.interval, max(0, self.deadline - time.monotonic())))
                if time.monotonic() >= self.deadline:
                    break
                if await api.status(self.phone):
                    self.confirmed = True
                    self.token_data = await api.finish(self)
                    self.state = "confirmed"
                    return
            self.state = "expired"
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        except CallbackAuthError as error:
            self.error = str(error)
            self.state = "error"
        finally:
            self.secret = ""
