import asyncio
import json

import httpx
import pytest
from websockets.asyncio.server import serve

from trader.exchange.rest import ExchangeError, RestClient
from trader.exchange.ws import MarketStream

MIN = 60_000


def candle_json(ts, px=100.0):
    return {"t": ts, "o": str(px), "h": str(px + 1), "l": str(px - 1), "c": str(px), "v": "2"}


async def test_candle_windows_page_forward_and_filter():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        calls.append(q)
        start, end = int(q["start_ts"]), int(q["end_ts"])
        data = [candle_json(t) for t in range(start, end + 1, 5 * MIN)]
        data.reverse()  # ordering must not matter
        return httpx.Response(200, json={"code": 0, "result": {"data": data}})

    rest = RestClient(
        "https://x/exchange/v1/", transport=httpx.MockTransport(handler), min_request_interval=0
    )
    start, end = 0, 700 * 5 * MIN
    got = []
    async for batch in rest.iter_candle_windows("BTC_USD", "5m", start, end):
        got.extend(batch)
    await rest.aclose()

    assert len(calls) == 3  # 300 + 300 + 100
    assert calls[0]["instrument_name"] == "BTC_USD" and calls[0]["timeframe"] == "5m"
    assert [c.ts for c in got] == list(range(0, end, 5 * MIN))
    assert got[0].high == 101.0


async def test_error_code_raises_and_private_is_signed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "result": {"data": []}})
        return httpx.Response(200, json={"code": 10004, "message": "BAD_REQUEST"})

    rest = RestClient(
        "https://x/exchange/v1/",
        "K",
        "S",
        transport=httpx.MockTransport(handler),
        min_request_interval=0,
    )
    with pytest.raises(ExchangeError, match="10004"):
        await rest.get_instruments()
    await rest.get_user_balance()
    await rest.aclose()
    assert seen["method"] == "private/user-balance" and seen["api_key"] == "K"
    assert len(seen["sig"]) == 64


async def test_market_stream_heartbeat_data_and_reconnect():
    received: list[dict] = []
    connections = 0

    async def server(ws):
        nonlocal connections
        connections += 1
        sub = json.loads(await ws.recv())
        received.append(sub)
        await ws.send(json.dumps({"id": sub["id"], "method": "subscribe", "code": 0}))
        await ws.send(json.dumps({"id": 42, "method": "public/heartbeat", "code": 0}))
        received.append(json.loads(await ws.recv()))
        await ws.send(
            json.dumps(
                {
                    "id": -1,
                    "method": "subscribe",
                    "code": 0,
                    "result": {
                        "channel": "ticker",
                        "subscription": "ticker.BTC_USD",
                        "instrument_name": "BTC_USD",
                        "data": [{"a": "1", "t": 1}],
                    },
                }
            )
        )
        if connections == 1:
            await ws.close()  # force a reconnect
        else:
            await ws.wait_closed()

    async with serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = MarketStream(
            f"ws://127.0.0.1:{port}", ["ticker.BTC_USD"], connect_delay=0, max_backoff=0.01
        )
        events = []

        async def consume():
            async for ev in stream.events():
                events.append(ev)
                if sum(e.kind == "data" for e in events) == 2:
                    stream.stop()
                    return

        await asyncio.wait_for(consume(), timeout=10)

    assert [e.kind for e in events] == ["connected", "data", "connected", "data"]
    assert events[1].symbol == "BTC_USD" and events[1].channel == "ticker"
    assert received[0]["params"] == {"channels": ["ticker.BTC_USD"]}
    assert received[1]["method"] == "public/respond-heartbeat" and received[1]["id"] == 42


async def test_market_stream_reconnects_when_connection_goes_silent():
    connections = 0

    async def server(ws):
        nonlocal connections
        connections += 1
        await ws.recv()
        await ws.wait_closed()  # never send anything

    async with serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = MarketStream(
            f"ws://127.0.0.1:{port}",
            ["ticker.X"],
            connect_delay=0,
            max_backoff=0.01,
            idle_timeout=0.2,
        )

        async def consume():
            n = 0
            async for ev in stream.events():
                n += ev.kind == "connected"
                if n == 2:
                    stream.stop()
                    return

        await asyncio.wait_for(consume(), timeout=5)
    assert connections == 2
