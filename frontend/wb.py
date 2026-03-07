import asyncio
import json
import time
import logging
from websockets.asyncio.server import serve

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wadar-ws")

devices = []
dishes = {}
device_clients = set()
dish_clients = {}
web_clients = {}


def build_web_state():
    return {
        "type": "state",
        "devices": devices,
        "dishes": dishes,
        "ts": time.time(),
    }


async def broadcast_web():
    payload = json.dumps(build_web_state())
    for cid, ws in list(web_clients.items()):
        try:
            await ws.send(payload)
        except Exception:
            del web_clients[cid]


async def device_handler(websocket):
    addr = websocket.remote_address
    log.info("DEVICE connected from %s", addr)
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
                log.info("DEVICE data received: %d devices", len(devices))
                await broadcast_web()
            except json.JSONDecodeError:
                log.warning("DEVICE bad JSON from %s", addr)
    finally:
        device_clients.discard(websocket)
        log.info("DEVICE disconnected from %s", addr)


async def dish_handler(websocket):
    addr = websocket.remote_address
    dish_id = None
    log.info("DISH connection from %s", addr)
    try:
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
                    continue

                if data.get("data") == "connect":
                    dish_id = data.get("id")
                    dish_clients[dish_id] = websocket
                    dishes[dish_id] = {"id": dish_id, "degrees": 0}
                    log.info("DISH registered: %s from %s", dish_id, addr)
                    await websocket.send(json.dumps({"response": "CONNECTED", "id": dish_id}))
                    await broadcast_web()
                    continue

                if dish_id is None:
                    log.warning("DISH data from unregistered client %s", addr)
                    continue

                degrees = data.get("degrees")
                if degrees is not None:
                    dishes[dish_id] = {"id": dish_id, "degrees": degrees}
                    log.info("DISH %s updated: %s deg", dish_id, degrees)
                    await broadcast_web()
            except json.JSONDecodeError:
                log.warning("DISH bad JSON from %s", addr)
    finally:
        if dish_id and dish_id in dish_clients:
            del dish_clients[dish_id]
            del dishes[dish_id]
            log.info("DISH %s disconnected", dish_id)
            await broadcast_web()
        else:
            log.info("DISH unregistered client disconnected from %s", addr)


async def web_handler(websocket):
    addr = websocket.remote_address
    client_id = None
    log.info("WEB connection from %s", addr)
    try:
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
                    continue

                if data.get("data") == "connect":
                    client_id = data.get("id")
                    web_clients[client_id] = websocket
                    log.info("WEB registered: %s from %s", client_id, addr)
                    await websocket.send(json.dumps({"response": "CONNECTED", "id": client_id}))
                    await websocket.send(json.dumps(build_web_state()))
                    continue
            except json.JSONDecodeError:
                pass
    finally:
        if client_id and client_id in web_clients:
            del web_clients[client_id]
            log.info("WEB %s disconnected", client_id)
        else:
            log.info("WEB unregistered client disconnected from %s", addr)


async def main():
    log.info("Starting WADAR WebSocket servers")
    log.info("  :5003 - device handler")
    log.info("  :5004 - dish handler")
    log.info("  :5005 - web client handler")
    async with serve(device_handler, "localhost", 5003), \
               serve(dish_handler, "localhost", 5004), \
               serve(web_handler, "localhost", 5005):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
