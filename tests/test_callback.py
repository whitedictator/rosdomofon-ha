"""Real HA config-flow and HTTP page; mock provider, never an actual call."""

import asyncio
from contextlib import suppress
import re
import time
from unittest.mock import patch
from urllib.parse import urlencode
import pytest
import pytest_asyncio
from homeassistant import config_entries, data_entry_flow
from custom_components.rosdomofon.callback_auth import (
    CallbackApi,
    CallbackAuthError,
    CallbackChallenge,
    METHODS,
)
from custom_components.rosdomofon.callback_web import KEY

PHONE = "79991234567"
BASE = "https://rdba.rosdomofon.com"
METHOD_URL = (
    BASE
    + f"/abonents-service/api/v1/abonents/{PHONE}/auth_available_methods?"
    + urlencode({"appName": "rd_android", "supportedMethods": ",".join(METHODS)})
)
CODE_URL = BASE + f"/abonents-service/api/v3/abonents/{PHONE}/auth_code"
STATUS_URL = BASE + f"/abonents-service/api/v1/abonents/{PHONE}/auth_status?deliveryMethod=callback"
TOKEN_URL = BASE + "/authserver-service/oauth/token"


@pytest.fixture(autouse=True)
def enable(enable_custom_integrations):
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    with patch.object(
        AiohttpClientMockResponse,
        "content_length",
        property(lambda self: len(self._response)),
        create=True,
    ):
        yield


@pytest_asyncio.fixture(autouse=True)
async def cleanup_pages(hass):
    yield
    if KEY in hass.data:
        for session_id in list(hass.data[KEY].sessions):
            hass.data[KEY].discard(session_id)
    await hass.async_block_till_done()


def register(mock, number="74951487925"):
    mock.get(
        METHOD_URL,
        json=[{"deliveryMethod": "callback", "requestTimeout": 60, "statusCheckInterval": 5}],
    )
    mock.post(
        CODE_URL,
        json={"phone": number, "secret": "private-challenge-secret", "triggeredMethod": "callback"},
    )


async def begin(hass):
    r = await hass.config_entries.flow.async_init(
        "rosdomofon", context={"source": config_entries.SOURCE_USER}
    )
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "callback"})
    return await hass.config_entries.flow.async_configure(r["flow_id"], {"phone": PHONE})


async def stop_wait(record):
    record["task"].cancel()
    with suppress(asyncio.CancelledError):
        await record["task"]


async def test_page_number_and_timer(hass, hass_client, aioclient_mock):
    register(aioclient_mock)
    r = await begin(hass)
    assert r["type"] == data_entry_flow.FlowResultType.EXTERNAL_STEP
    assert r["description_placeholders"]["number"] == "+7 (495) 148-79-25"
    client = await hass_client()
    response = await client.get(r["url"])
    assert response.status == 200
    text = await response.text()
    assert "+7 (495) 148-79-25" in text and "tel:+74951487925" in text
    assert re.search(r'id="timer"[^>]*>\d\d:\d\d<', text)
    assert "__NUMBER__" not in text and "__STATE__" not in text
    assert "private-challenge-secret" not in text and PHONE not in text
    assert "setInterval(tick,200)" in text and response.headers["Cache-Control"] == "no-store"
    hass.config_entries.flow.async_abort(r["flow_id"])


async def test_confirmed_callback_creates_entry(hass, aioclient_mock):
    register(aioclient_mock)
    aioclient_mock.get(STATUS_URL, json={"statusCode": 1, "status": "confirmed"})
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "test-access", "refresh_token": "test-refresh", "expires_in": 3600},
    )
    r = await begin(hass)
    pages = hass.data[KEY]
    record = next(iter(pages.sessions.values()))
    await stop_wait(record)
    c = record["challenge"]
    c.state = "pending"
    c.secret = "private-challenge-secret"
    c.interval = 0.001
    pages.start_wait(record)
    await asyncio.wait_for(record["task"], 1)
    assert c.state == "confirmed"
    with patch("custom_components.rosdomofon.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"])
    assert r["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert (
        r["data"]["token_data"]["refresh_token"] == "test-refresh"
        and r["options"]["incoming_calls"]
    )
    assert c.secret == ""


async def test_expiry_retry_csrf_and_new_number(hass, hass_client, aioclient_mock):
    register(aioclient_mock)
    r = await begin(hass)
    pages = hass.data[KEY]
    record = next(iter(pages.sessions.values()))
    await stop_wait(record)
    c = record["challenge"]
    c.state = "expired"
    c.deadline = time.monotonic() - 1
    client = await hass_client()
    status = await (await client.get(r["url"] + "/status")).json()
    assert status["state"] == "expired" and status["remaining_ms"] == 0 and status["can_retry"]
    assert (
        await client.post(r["url"] + "/retry", headers={"X-Rosdomofon-Callback": "wrong"})
    ).status == 403
    aioclient_mock.clear_requests()
    register(aioclient_mock, "74951487926")
    response = await client.post(
        r["url"] + "/retry", headers={"X-Rosdomofon-Callback": record["csrf"]}
    )
    assert response.status == 200
    status = await response.json()
    assert status["state"] == "pending" and status["remaining_ms"] > 58000
    assert (
        status["number"] == "+7 (495) 148-79-26"
        and "secret" not in status
        and "token_data" not in status
    )
    assert (
        await client.post(r["url"] + "/retry", headers={"X-Rosdomofon-Callback": record["csrf"]})
    ).status == 409
    hass.config_entries.flow.async_abort(r["flow_id"])
    assert record["closed"] and c.phone == "" and c.secret == ""


async def test_callback_unavailable(hass, aioclient_mock):
    aioclient_mock.get(METHOD_URL, json=[{"deliveryMethod": "sms"}])
    r = await begin(hass)
    assert (
        r["type"] == data_entry_flow.FlowResultType.FORM
        and r["errors"]["base"] == "callback_unavailable"
    )


async def test_no_exchange_before_confirmation():
    c = CallbackChallenge(
        PHONE, "+7 (495) 148-79-25", "+74951487925", "secret", time.monotonic() + 60, 60, 5
    )
    with pytest.raises(CallbackAuthError, match="not_confirmed"):
        await CallbackApi(None).finish(c)
