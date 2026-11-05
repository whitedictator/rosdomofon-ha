"""A dead socket must not spin forever when a concurrent reset holds its lock."""

from unittest.mock import AsyncMock
from firebase_messaging import FcmPushClientConfig, FcmPushClientRunState
from custom_components.rosdomofon.push import FreshPushClient


async def test_dead_reader_has_bounded_errors_even_when_reset_cannot_progress():
    client = FreshPushClient.__new__(FreshPushClient)
    client.config = FcmPushClientConfig(abort_on_sequential_error_count=3)
    client.run_state = FcmPushClientRunState.STARTED
    client.do_listen = True
    client.tasks = []
    client.sequential_error_counters = {}
    client._connect_with_retry = AsyncMock(return_value=True)
    client._login = AsyncMock()
    client._receive_msg = AsyncMock(side_effect=ConnectionResetError("simulated offline"))
    client._reset = AsyncMock()  # Another task owns reset_lock: no progress.
    client._do_writer_close = AsyncMock()
    await client._listen()
    assert client._receive_msg.await_count == 3
    assert client._reset.await_count == 2
    assert not client.do_listen
    assert client.run_state == FcmPushClientRunState.STOPPING
    client._do_writer_close.assert_awaited_once()


async def test_receiver_uses_bounded_client_and_closes_it(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from custom_components.rosdomofon import push

    options = []
    receiver = None

    async def start():
        receiver.closed = True

    client = SimpleNamespace(
        checkin_or_register=AsyncMock(return_value="test-token"), start=start, stop=AsyncMock()
    )

    def factory(*args, **kwargs):
        options.append(kwargs["config"])
        return client

    monkeypatch.setattr(push, "FreshPushClient", factory)
    monkeypatch.setattr(push, "async_get_clientsession", lambda _: None)
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        async_add_executor_job=AsyncMock(return_value=None),
    )
    receiver = push.CallReceiver(hass, SimpleNamespace(entry_id="test"), None)
    receiver.api = AsyncMock(side_effect=[[], {"accessKey": "test-key"}, None])
    await receiver.run()
    assert options[0].abort_on_sequential_error_count == 3
    client.stop.assert_awaited_once()
