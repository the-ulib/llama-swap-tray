"""Install this directory as custom_nodes/llama_swap_coordination."""
import gc
import json
import os
from pathlib import Path
import types
import weakref

from aiohttp import web
import torch
import execution
import comfy.model_management
from server import PromptServer

from .guard import WorkerGuard
from .control import install_control

NODE_CLASS_MAPPINGS = {}
settings_path = Path(__file__).with_name("settings.json")
CONFIG = os.environ.get("LLAMA_SWAP_COORDINATOR_CONFIG")
if CONFIG is None:
    CONFIG = json.loads(settings_path.read_text(encoding="utf-8-sig"))["config_path"]
executor_ref = None


def cleanup():
    executor = executor_ref() if executor_ref is not None else None
    if executor is None:
        raise RuntimeError("ComfyUI executor is not registered; restart ComfyUI")
    comfy.model_management.unload_all_models()
    executor.reset()
    gc.collect()
    comfy.model_management.soft_empty_cache()
    allocated = 0
    free = None
    if torch.cuda.is_available():
        for index in range(torch.cuda.device_count()):
            torch.cuda.synchronize(index)
            allocated += torch.cuda.memory_allocated(index)
        free = torch.cuda.mem_get_info()[0]
    return {"allocated_mib": allocated / 1048576,
            "free_mib": free / 1048576 if free is not None else None}


guard = WorkerGuard(CONFIG, cleanup)
original_init = execution.PromptExecutor.__init__


def executor_init(self, *args, **kwargs):
    global executor_ref
    original_init(self, *args, **kwargs)
    executor_ref = weakref.ref(self)


execution.PromptExecutor.__init__ = executor_init
queue = PromptServer.instance.prompt_queue
original_get = queue.get


def guarded_get(self, timeout=None):
    return guard.before_get(self, original_get, timeout)


queue.get = types.MethodType(guarded_get, queue)
install_control(PromptServer.instance.routes, guard, queue)


@PromptServer.instance.routes.get("/llama_swap/status")
async def status(request):
    return web.json_response({"protocol": 1, "session": guard.session,
                              "state": guard.state, "error": guard.error,
                              "stop_supported": True,
                              "memory_backend": "cuda" if torch.cuda.is_available() else "unsupported"})
