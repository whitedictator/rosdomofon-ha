"""Run the real callback page and retry handler with a fake provider, localhost only.
No real phone calls, cloud requests, credentials or HA installation are needed.
"""

import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.rosdomofon.callback_auth import CallbackChallenge, format_service_phone
from custom_components.rosdomofon.callback_web import CallbackPages, CallbackPageView, TEMPLATE


class DemoApi:
    def __init__(self):
        self.requests = 0

    async def begin(self, phone):
        self.requests += 1
        number, tel = format_service_phone("74951487925" if self.requests == 1 else "74951487926")
        return CallbackChallenge(phone, number, tel, "demo-secret", time.monotonic() + 60, 60, 3)

    async def status(self, phone):
        return False

    async def finish(self, challenge):
        raise AssertionError("Preview never authenticates a real account")


async def main():
    tasks = []

    def task(coro, name):
        t = asyncio.create_task(coro, name=name)
        tasks.append(t)
        return t

    async def configured(flow):
        pass

    hass = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        async_create_background_task=task,
        config_entries=SimpleNamespace(flow=SimpleNamespace(async_configure=configured)),
    )
    template = TEMPLATE.read_text().replace(
        "<main>",
        '<main><div style="font-size:12px;color:#ad6408;font-weight:700">ДЕМОНСТРАЦИЯ · ЗВОНИТЬ НЕ НУЖНО</div>',
    )
    pages = CallbackPages(hass, template)
    api = DemoApi()
    for state in ["pending", "expired", "confirmed", "error"]:
        c = await api.begin("79991234567")
        c.state = state
        if state == "expired":
            c.deadline = time.monotonic() - 1
        if state == "error":
            c.error = "cannot_connect"
        pages.sessions["demo-" + state] = {
            "challenge": c,
            "task": None,
            "api": api,
            "flow_id": "preview",
            "csrf": "preview-csrf-token",
            "retries": 0,
            "retry_after": 0,
            "lock": asyncio.Lock(),
            "closed": False,
        }
        if state == "pending":
            pages.start_wait(pages.sessions["demo-" + state])
    view = CallbackPageView(pages)
    app = web.Application()

    async def get(request):
        return await view.get(request, request.match_info["session_id"])

    async def post(request):
        return await view.post(request, request.match_info["session_id"])

    app.router.add_get("/api/rosdomofon/callback/{session_id}", get)
    app.router.add_get("/api/rosdomofon/callback/{session_id}/status", get)
    app.router.add_post("/api/rosdomofon/callback/{session_id}/retry", post)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18088)
    await site.start()
    print("Preview ready: http://127.0.0.1:18088/api/rosdomofon/callback/demo-pending", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
