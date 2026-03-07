import asyncio
import json
import time
from websockets.asyncio.server import serve

devices = []
dishes = {}
device_clients = set()
dish_clients = set()
web_clients = set()


def build_web_state():
    return {
        "type": "state",
        "devices": devices,
        "dishes": dishes,
        "ts": time.time(),
    }


async def broadcast_web():
    payload = json.dumps(build_web_state())
    for ws in list(web_clients):
        try:
            await ws.send(payload)
        except Exception:
            web_clients.discard(ws)


async def device_handler(websocket):
    device_clients.add(websocket)
    try:
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
                    continue
                devices.clear()
                if isinstance(data.get("devices"), list):
                    devices.extend(data["devices"])
                await broadcast_web()
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
                    continue
                dish_id = data.get("id")
                degrees = data.get("degrees")
                if dish_id is not None:
                    dishes[dish_id] = {"id": dish_id, "degrees": degrees}
                await broadcast_web()
            except json.JSONDecodeError:
                pass
    finally:
        dish_clients.discard(websocket)


async def web_handler(websocket):
    web_clients.add(websocket)
    try:
        await websocket.send(json.dumps(build_web_state()))
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    finally:
        web_clients.discard(websocket)


async def main():
    async with serve(device_handler, "localhost", 5003), \
               serve(dish_handler, "localhost", 5004), \
               serve(web_handler, "localhost", 5005):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
