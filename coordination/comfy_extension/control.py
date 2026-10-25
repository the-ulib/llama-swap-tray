"""Local tray controls, independent of the coordinator's process lifetime."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import secrets
import types
import uuid

from aiohttp import web


def prepare_stop(guard, queue, force):
    # The same mutex protects dispatch, new submissions and this snapshot.
    with queue.mutex:
        running, pending = queue.get_current_queue()
        result = {"running": len(running), "pending": len(pending)}
        if (running or pending) and not force:
            return 409, dict(result, error="ComfyUI has running or queued jobs")
        if guard.shutting_down:
            return 200, dict(result, stopping=True)
        backup = None
        if running or pending:
            token_path = Path(guard.config()["control_token_file"])
            if not token_path.is_absolute():
                token_path = Path(guard.config_path).parent / token_path
            backup = token_path.parent / ("comfyui-queue-before-stop-" + uuid.uuid4().hex + ".json")
            # ComfyUI's optional sixth tuple field can contain sensitive data.
            # Save prompt/workflow data only, before changing any queue state.
            data = json.dumps({"queue_running": [item[:5] for item in running],
                               "queue_pending": [item[:5] for item in pending]}, indent=2)
            with backup.open("x", encoding="utf-8") as handle:
                handle.write(data)
        guard.shutting_down = True
        guard.state = "stopping"
        return 200, dict(result, stopping=True, backup=str(backup) if backup else None)


def install_control(routes, guard, queue, exit_process=os._exit):
    original_put = queue.put

    def guarded_put(self, item):
        with self.mutex:
            if guard.shutting_down:
                raise web.HTTPServiceUnavailable(text="ComfyUI is stopping")
            return original_put(item)

    queue.put = types.MethodType(guarded_put, queue)

    @routes.post("/llama_swap/stop")
    async def stop(request):
        try:
            local = ipaddress.ip_address(request.remote or "").is_loopback
        except ValueError:
            local = False
        # Browser requests are unnecessary: the native tray sends no Origin.
        if not local or request.headers.get("Origin"):
            raise web.HTTPForbidden()
        try:
            token_path = Path(guard.config()["control_token_file"])
            if not token_path.is_absolute():
                token_path = Path(guard.config_path).parent / token_path
            token = token_path.read_text(encoding="ascii").strip()
        except (OSError, ValueError, KeyError):
            raise web.HTTPServiceUnavailable(text="ComfyUI control token unavailable")
        if not token or not secrets.compare_digest(request.headers.get("X-Coordination-Token", ""), token):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Expected a JSON object with boolean force")
        if not isinstance(body, dict) or type(body.get("force", False)) is not bool:
            raise web.HTTPBadRequest(text="Expected a JSON object with boolean force")
        try:
            status, result = prepare_stop(guard, queue, body.get("force", False))
        except (OSError, ValueError, TypeError, KeyError):
            raise web.HTTPServiceUnavailable(text="Could not save the queue; ComfyUI remains running")
        if status == 200:
            # Return the acknowledgement before exiting. Windows then releases
            # CUDA and database handles, including for independently started apps.
            asyncio.get_running_loop().call_later(0.75, exit_process, 0)
        return web.json_response(result, status=status)
