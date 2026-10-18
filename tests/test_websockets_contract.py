"""The notebook kernel proxies (ui/main.py, backend/notebooks_router.py) open an upstream socket
with ``websockets.connect(url)`` and then use ``send`` and ``recv``. This runs that usage against a
real local server so a ``websockets`` upgrade that changes it is caught. No network is used."""

from __future__ import annotations

import asyncio

import websockets


async def _roundtrip():
    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    async with websockets.serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/api/kernels/k1/channels?token=t"
        ) as up:
            await up.send('{"msg_type": "kernel_info_request"}')
            text = await up.recv()
            await up.send(b"\x00\x01binary")
            binary = await up.recv()
        return text, binary


def test_text_and_binary_frames_round_trip_as_the_proxies_expect():
    text, binary = asyncio.run(_roundtrip())
    assert isinstance(text, str) and "kernel_info_request" in text
    assert isinstance(binary, bytes) and binary == b"\x00\x01binary"


def test_connection_refused_raises_an_exception_the_proxies_catch():
    async def attempt():
        async with websockets.connect("ws://127.0.0.1:9/x", open_timeout=2):
            pass

    try:
        asyncio.run(attempt())
    except Exception as exc:  # the proxies wrap the connect in `except Exception`
        assert exc is not None
    else:
        raise AssertionError("expected the connection to fail")
