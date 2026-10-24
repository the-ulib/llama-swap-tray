"""Disposable installer fixture. Reports fake CUDA memory; never imports torch."""
import argparse
import asyncio
import json
from pathlib import Path
import uuid

from aiohttp import ClientSession, web


async def main(config_path):
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    session = uuid.uuid4().hex

    async def status(request):
        return web.json_response({"protocol": 1, "session": session, "memory_backend": "cuda"})

    async def queue(request):
        return web.json_response({"queue_running": [], "queue_pending": []})

    app = web.Application()
    app.router.add_get("/llama_swap/status", status)
    app.router.add_get("/queue", queue)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", int(cfg["comfy_url"].rsplit(":", 1)[1])).start()
    unloaded = None
    async with ClientSession() as http:
        while True:
            try:
                token = Path(cfg["control_token_file"]).read_text().strip()
                async with http.post(f"http://127.0.0.1:{cfg['listen_port']}/coordination/worker",
                                     json={"session": session, "pending": 0, "unloaded": unloaded},
                                     headers={"X-Coordination-Token": token}) as response:
                    result = await response.json()
                if result.get("action") == "unload":
                    unloaded = {"epoch": result["epoch"], "allocated_mib": 0, "free_mib": 32768}
            except (OSError, ValueError):
                pass
            await asyncio.sleep(0.1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    asyncio.run(main(parser.parse_args().config))
