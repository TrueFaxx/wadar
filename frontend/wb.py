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
dish_config = {"distance": 3.82, "bearing": 94.0}
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


async def handle_command(websocket, cmd, data):
    parts = cmd.strip().upper().split()
    if len(parts) == 0:
        await websocket.send(json.dumps({"cmd": cmd, "error": "EMPTY COMMAND"}))
        return

    action = parts[0]
    target = parts[1] if len(parts) > 1 else None

    if action == "SET" and target == "CONFIG":
        if "distance" in data:
            dish_config["distance"] = float(data["distance"])
        if "bearing" in data:
            dish_config["bearing"] = float(data["bearing"])
        log.info("Config updated: dist=%.1fm, bearing=%.1f deg",
                 dish_config["distance"], dish_config["bearing"])
        # Broadcast config to all web clients (triangulator listens on :5005)
        config_msg = json.dumps({"type": "config", **dish_config})
        for cid, ws in list(web_clients.items()):
            try:
                await ws.send(config_msg)
            except Exception:
                pass
        await websocket.send(json.dumps({"cmd": cmd, "result": "OK"}))
    elif action == "COUNT" and target == "DISH":
        await websocket.send(json.dumps({"cmd": cmd, "result": len(dishes)}))
    elif action == "COUNT" and target == "DEVICE":
        await websocket.send(json.dumps({"cmd": cmd, "result": len(devices)}))
    elif action == "PING" and target == "SERVER":
        await websocket.send(json.dumps({"cmd": cmd, "result": "OPERATIONAL"}))
    else:
        await websocket.send(json.dumps({"cmd": cmd, "error": "UNKNOWN COMMAND"}))


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

                cmd = data.get("cmd")
                if cmd:
                    await handle_command(websocket, cmd, data)
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
