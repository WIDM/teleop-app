"""本地 echo stub：hello 回 welcome，其他原樣 echo。之後換成 Leo 的 server 網址即可。"""

import asyncio
import json
import time

import websockets


async def handle(ws):
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("type") == "hello":
            reply = {
                "v": 0,
                "type": "welcome",
                "ts": int(time.time() * 1000),
                "seq": msg["seq"],
                "payload": {
                    "session": "stub",
                    "config": {"coarse_cm": 2, "fine_cm": 0.5},
                },
            }
            await ws.send(json.dumps(reply))
        else:
            await ws.send(raw)


async def main():
    async with websockets.serve(handle, "0.0.0.0", 8765):
        print("echo stub on ws://0.0.0.0:8765")
        await asyncio.Future()


asyncio.run(main())
