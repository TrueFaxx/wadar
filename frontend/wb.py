import asyncio
import json
import time
from websockets.asyncio.server import serve

devices = []
device_clients = set()
dish_clients = set()


def build_device_state():
    return {
        "type": "devices",
        "devices": devices,
        "ts": time.time(),
    }


async def device_handler(websocket):
    device_clients.add(websocket)
    try:
        await websocket.send(json.dumps(build_device_state()))
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    finally:
        device_clients.discard(websocket)


async def dish_handler(websocket):
    dish_clients.add(websocket)
    try:
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    finally:
        dish_clients.discard(websocket)


async def main():
    async with serve(device_handler, "localhost", 5003), \
               serve(dish_handler, "localhost", 5004):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
