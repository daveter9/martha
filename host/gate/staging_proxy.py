#!/usr/bin/env python3
"""Forward LAN port 8124 to staging Home Assistant (172.30.53.10:8123).

Staging sits on an internal Docker network that cannot publish ports, so this plain TCP
forward makes it reachable for the browser and the Companion app (websockets included).
Needs no privileges: runs as a systemd DynamicUser (martha-staging-proxy.service).
"""
import asyncio
import os

LISTEN = (os.environ.get("LISTEN_HOST", "0.0.0.0"), int(os.environ.get("LISTEN_PORT", "8124")))
TARGET = (os.environ.get("TARGET_HOST", "172.30.53.10"), int(os.environ.get("TARGET_PORT", "8123")))


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(client_reader, client_writer):
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(*TARGET), timeout=5)
    except (OSError, asyncio.TimeoutError):
        client_writer.close()   # staging is not running
        return
    await asyncio.gather(pipe(client_reader, upstream_writer),
                         pipe(upstream_reader, client_writer))


async def main():
    server = await asyncio.start_server(handle, *LISTEN)
    print(f"forwarding {LISTEN[0]}:{LISTEN[1]} -> {TARGET[0]}:{TARGET[1]}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
