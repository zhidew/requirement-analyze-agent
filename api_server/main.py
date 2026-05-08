import warnings

# Suppress annoying dependency and deprecation warnings at the very beginning
# This must happen before other imports like 'fastapi' or 'requests'
warnings.filterwarnings("ignore", message=".*urllib3.*doesn't match a supported version.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="asyncio")
warnings.filterwarnings("ignore", message="Core Pydantic V1 functionality isn't compatible with Python 3.14")

import asyncio
import sys
from pathlib import Path
from contextlib import asynccontextmanager

import os
os.environ['NO_PROXY'] = '127.0.0.1,localhost,.huawei.com'

# 将项目根目录 (it-design-agent) 加入 sys.path 以便导入 scripts
api_server_dir = Path(__file__).resolve().parent
root_dir = api_server_dir.parent
for path_str in [str(root_dir), str(api_server_dir)]:
    if path_str in sys.path:
        sys.path.remove(path_str)
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(api_server_dir))

# Ensure ProactorEventLoop on Windows for subprocess support
# In Python 3.14+, ProactorEventLoop is the default and set_event_loop_policy is deprecated.
if sys.platform == 'win32' and sys.version_info < (3, 14):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from models.events import dump_event, validate_event_payload
from routers import config, projects, management
from services import orchestrator_service as orch
from registry.expert_registry import ExpertRegistry


# --- Lifespan for startup/shutdown ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup, cleanup on shutdown."""
    # Startup: Initialize ExpertRegistry
    base_dir = Path(__file__).resolve().parent.parent
    try:
        registry = ExpertRegistry.initialize(base_dir)
        stats = registry.get_stats()
        print(f"[Startup] ExpertRegistry initialized: {stats['total_experts']} experts loaded")
        if stats['load_errors']:
            for error in stats['load_errors']:
                print(f"[Startup] Warning: {error}")
    except Exception as e:
        print(f"[Startup] Failed to initialize ExpertRegistry: {e}")

    try:
        await orch.restore_scheduled_runs()
        print("[Startup] Scheduled runs restored")
    except Exception as e:
        print(f"[Startup] Failed to restore scheduled runs: {e}")
    
    yield  # Application runs here
    
    # Shutdown: Cleanup
    ExpertRegistry.reset()
    print("[Shutdown] ExpertRegistry cleaned up")


# --- 巧妙的日志过滤器 ---
class PollingLogFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.state_poll_count = 0

    def filter(self, record):
        # 检查是否是 /state 接口的访问日志
        if "/state" in record.getMessage():
            self.state_poll_count += 1
            # 每 20 次轮询才打印一个打点，或者你可以完全返回 False 屏蔽
            if self.state_poll_count >= 20:
                print(".", end="", flush=True) # 在控制台打印一个点表示"心跳"
                self.state_poll_count = 0
            return False # 返回 False 表示不记录这条日志到标准输出
        return True

# 应用过滤器到 uvicorn 的访问日志
logging.getLogger("uvicorn.access").addFilter(PollingLogFilter())

app = FastAPI(
    title="IT Detailed Design Agent API",
    description="Backend API for the IT Detailed Design Agent UI",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # For dev only, restrict in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(management.router)
app.include_router(management.expert_center_router)
app.include_router(config.router)
app.include_router(config.system_router)

@app.get("/api/v1/jobs/{job_id}/status")
async def get_job_status_stream(request: Request, job_id: str):
    """
    Server-Sent Events (SSE) endpoint to stream structured orchestrator events.
    """
    async def event_generator():
        queue = None
        backlog = orch.get_job_events(job_id)
        for payload in backlog:
            if await request.is_disconnected():
                return
            event = validate_event_payload(payload)
            yield {"event": event.event_type, "data": json.dumps(dump_event(event), ensure_ascii=False)}

        queue = orch.subscribe_job_events(job_id)
        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    continue

                event = validate_event_payload(payload)
                yield {"event": event.event_type, "data": json.dumps(dump_event(event), ensure_ascii=False)}

                if event.event_type in {"run_completed", "run_failed"}:
                    break
        finally:
            if queue is not None:
                orch.unsubscribe_job_events(job_id, queue)

    return EventSourceResponse(event_generator(), ping=15)

if __name__ == "__main__":
    import uvicorn
    # Make sure this is run from the design-system/api_server directory or set pythonpath
    uvicorn.run("main:app", host="0.0.0.0", port=9090, reload=True)
