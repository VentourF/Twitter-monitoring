# -*- coding: utf-8 -*-
"""Twitter monitoring Web 面板。

监控循环跑在本进程里（复用 monitor.py 的全部逻辑），浏览器打开
http://127.0.0.1:8787 即可查看推送记录、修改监控配置、手动检查。

    .venv/Scripts/python app.py
"""

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import monitor as m

BASE_DIR = Path(__file__).resolve().parent
PANEL_DIR = BASE_DIR / "static"

stop_event = asyncio.Event()
monitor_task = None
SESSIONS: set[str] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    m.init_db()
    m.setup_logging()
    # 把持久化的情报状态（上次日报时间等）灌进运行状态，面板重启后仍可显示
    m.MONITOR_STATE["intel"] = dict(m.load_state().get("intel") or {})
    global monitor_task
    stop_event.clear()
    monitor_task = asyncio.create_task(m.run_forever(stop_event))
    yield
    stop_event.set()


app = FastAPI(title="Twitter monitoring", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=PANEL_DIR), name="static")


def panel_cfg() -> dict:
    return m.load_config().get("panel") or {}


def authed(request: Request) -> bool:
    pwd = (panel_cfg().get("password") or "").strip()
    if not pwd:
        return True
    return request.cookies.get("panel_session") in SESSIONS


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path != "/api/login" and not authed(request):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    return await call_next(request)


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    pwd = (panel_cfg().get("password") or "").strip()
    if not pwd or body.get("password") == pwd:
        token = secrets.token_hex(16)
        SESSIONS.add(token)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("panel_session", token, httponly=True, samesite="lax")
        return resp
    return JSONResponse({"detail": "密码错误"}, status_code=401)


@app.get("/")
async def index():
    return FileResponse(PANEL_DIR / "index.html")


@app.get("/api/status")
async def status():
    stats = m.store.get().stats()
    cfg = m.load_config()
    state = m.load_state()
    return {
        "running": m.MONITOR_STATE["running"],
        "last_check": m.MONITOR_STATE["last_check"],
        "last_error": m.MONITOR_STATE["last_error"],
        "stats": {"total": stats["total"], "today": stats["today"],
                  "reset": stats["reset"], "intel_today": stats["intel_today"],
                  "monitors": len(cfg.get("twitter", {}).get("monitor") or [])},
        "display_names": state.get("display_names", {}),
        "intel": m.MONITOR_STATE.get("intel") or {},
        "health": m.MONITOR_STATE.get("health") or {},
        "monitor": m.MONITOR_STATE,
    }


@app.get("/api/records")
async def records(limit: int = 100, source: str = ""):
    limit = max(1, min(limit, 500))
    # source=intel 同时匹配 intel_search / intel_digest；source=monitor 只看主监控
    rows = m.store.get().records(limit, source.strip())
    out = []
    for d in rows:
        d["time_conv"] = json.loads(d["time_conv"] or "[]")
        d["channels"] = json.loads(d["channels"] or "{}")
        out.append(d)
    return out


@app.post("/api/records/clear")
async def records_clear():
    m.store.get().clear()
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    return m.load_config()


@app.post("/api/config")
async def save_config(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"detail": "配置格式错误"}, status_code=400)
    with open(m.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    warning = None
    try:
        m.validate_config(m.load_config())
    except RuntimeError as e:
        warning = str(e)
    return {"ok": True, "warning": warning}


@app.post("/api/check")
async def check_now():
    cfg = m.load_config()
    handles = m.validate_config(cfg)
    async with m.CHECK_LOCK:
        client = await m.ensure_client(cfg, handles[0])
        state = m.load_state()
        async with m.make_http(cfg) as http:
            n = await m.poll_once(client, http, cfg, state)
    return {"ok": True, "pushed": n}


@app.post("/api/intel/search")
async def intel_search_now():
    cfg = m.load_config()
    handles = m.validate_config(cfg)
    async with m.CHECK_LOCK:
        client = await m.ensure_client(cfg, handles[0])
        state = m.load_state()
        async with m.make_http(cfg) as http:
            r = await m.search_intel(client, http, cfg, state, force=True)
    return {"ok": True, "found": r.get("found", 0), "pushed": r.get("pushed", 0),
            "note": r.get("note", "")}


@app.post("/api/intel/digest")
async def intel_digest_now():
    cfg = m.load_config()
    async with m.CHECK_LOCK:
        async with m.make_http(cfg) as http:
            r = await m.build_intel_digest(cfg, http, force=True)
    return {"ok": True, "items": r.get("items", 0), "sent": r.get("sent", False)}


@app.post("/api/test")
async def test_channels():
    return await m.test_channels(m.load_config())


@app.get("/api/log")
async def tail_log(lines: int = 150):
    lines = max(10, min(lines, 1000))
    if not m.LOG_PATH.exists():
        return {"log": "(暂无日志)"}
    data = m.LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    return {"log": "\n".join(data[-lines:])}


@app.post("/api/monitor/stop")
async def monitor_stop():
    global monitor_task
    stop_event.set()
    if monitor_task:
        try:
            await asyncio.wait_for(asyncio.shield(monitor_task), timeout=5)
        except Exception:
            pass
    return {"ok": True, "running": m.MONITOR_STATE["running"]}


@app.post("/api/monitor/start")
async def monitor_start():
    global monitor_task, stop_event
    if m.MONITOR_STATE["running"]:
        return {"ok": True, "running": True}
    stop_event = asyncio.Event()
    monitor_task = asyncio.create_task(m.run_forever(stop_event))
    return {"ok": True, "running": True}


if __name__ == "__main__":
    import os
    import uvicorn
    p = m.load_config().get("panel") or {}
    # 环境变量优先（Docker 部署用 PANEL_HOST/PANEL_PORT 覆盖配置文件）
    host = os.environ.get("PANEL_HOST", p.get("host", "127.0.0.1"))
    port = int(os.environ.get("PANEL_PORT", p.get("port", 8787)))
    uvicorn.run(app, host=host, port=port, log_level="warning")
