"""抢课服务 Web 后端（最小可用版本）。"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from course_grabber.config import Config
from course_grabber.crypto_helper import CookieCrypto
from course_grabber.data_manager import DataManager
from course_grabber.grab_engine import GrabEngine
from course_grabber.utils import logger, parse_curl_data
from course_grabber.web_login import LoginError, WebLoginSession

# ---------------------- 内存存储 ----------------------

_users: dict[str, dict[str, Any]] = {}          # remark -> {cookie, user_agent, username}
_courses: dict[str, dict[str, Any]] = {}        # uid -> {name, kch_id, data}
_tasks: dict[str, dict[str, Any]] = {}          # task_id -> {created_at, logs, running}
_login_sessions: dict[str, WebLoginSession] = {}  # session_id -> WebLoginSession

# 简单的访问口令，启动时从环境变量读取或生成
_ADMIN_TOKEN = Config.COOKIE_KEY or "course-grabber-web"


def _require_token(token: str) -> None:
    if token != _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="访问口令错误")


# ---------------------- WebSocket 管理 ----------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, task_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.setdefault(task_id, []).append(websocket)

    def disconnect(self, task_id: str, websocket: WebSocket) -> None:
        conns = self.active.get(task_id, [])
        if websocket in conns:
            conns.remove(websocket)

    async def broadcast(self, task_id: str, message: dict[str, Any]) -> None:
        conns = self.active.get(task_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(task_id, ws)


manager = ConnectionManager()


def _log_callback(task_id: str, event) -> None:
    """把 grab_engine 的状态事件推送给 WebSocket 并保存到内存。"""
    msg = {
        "time": time.strftime("%H:%M:%S"),
        "user": event.user_name,
        "course": event.course_name,
        "status": event.status,
        "message": event.message,
        "finished": event.finished,
    }
    _tasks.setdefault(task_id, {"logs": [], "running": True, "created_at": time.time()})
    _tasks[task_id]["logs"].append(msg)
    # 保留最近 2000 条
    if len(_tasks[task_id]["logs"]) > 2000:
        _tasks[task_id]["logs"] = _tasks[task_id]["logs"][-2000:]
    if event.finished:
        _tasks[task_id]["running"] = False
    try:
        asyncio.run(manager.broadcast(task_id, msg))
    except Exception:
        pass


# ---------------------- 启动/关闭 ----------------------

_engine: GrabEngine | None = None
_engine_lock = threading.Lock()


def _get_engine() -> GrabEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = GrabEngine(status_callback=lambda e: _log_callback("global", e))
            _engine.start_dispatch()
        return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    _get_engine()
    yield
    if _engine:
        _engine.shutdown()


app = FastAPI(title="抢课 Web 服务", lifespan=lifespan)

static_dir = ROOT / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------- 页面入口 ----------------------

@app.get("/")
async def index():
    return FileResponse(str(static_dir / "index.html"))


# ---------------------- 登录/验证码 ----------------------

@app.get("/api/captcha")
async def api_captcha(token: str = Query(...)):
    _require_token(token)
    session_id = str(uuid.uuid4())
    session = WebLoginSession()
    try:
        info = session.get_captcha()
        _login_sessions[session_id] = session
        return {"session_id": session_id, **info}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取验证码失败: {exc}")


@app.post("/api/login")
async def api_login(payload: dict[str, Any]):
    _require_token(payload.get("token", ""))
    session_id = payload.get("session_id", "")
    session = _login_sessions.pop(session_id, None)
    if not session:
        raise HTTPException(status_code=400, detail="验证码会话已过期，请重新获取验证码")

    username = payload.get("username", "").strip()
    password = payload.get("password", "").strip()
    captcha = payload.get("captcha", "").strip()
    remark = payload.get("remark", username).strip() or username

    if not username or not password or not captcha:
        raise HTTPException(status_code=400, detail="账号、密码、验证码不能为空")

    try:
        result = session.login(
            username=username,
            password=password,
            captcha=captcha,
            captcha_ts=payload.get("captcha_ts", ""),
            csrftoken=payload.get("csrftoken", ""),
            hidden_fields=payload.get("hidden_fields") or {},
        )
    except LoginError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"登录异常: {exc}")

    # 登录成功后立即丢弃密码，只保留 Cookie
    _users[remark] = {
        "username": username,
        "cookie": result["cookie_str"],
        "user_agent": result["user_agent"],
        "updated_at": time.time(),
    }
    return {"success": True, "remark": remark, "cookie": result["cookie_str"]}


# ---------------------- Cookie/用户管理 ----------------------

@app.get("/api/users")
async def api_users(token: str = Query(...)):
    _require_token(token)
    return {
        remark: {
            "username": info["username"],
            "updated_at": info["updated_at"],
        }
        for remark, info in _users.items()
    }


@app.delete("/api/users/{remark}")
async def api_delete_user(remark: str, token: str = Query(...)):
    _require_token(token)
    if remark in _users:
        del _users[remark]
    return {"success": True}


# ---------------------- 课程管理 ----------------------

# 合法的课程数据文件，限制在 data/ 目录内
def _list_course_files() -> list[dict[str, Any]]:
    data_dir = Config.DATA_DIR
    files = []
    for path in sorted(data_dir.glob("*.json")):
        if path.name in ("db_users_v7.json",):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = len(data) if isinstance(data, dict) else 0
            files.append({"name": path.name, "count": count, "path": str(path)})
        except Exception:
            pass
    return files


def _extract_course_name(item: dict[str, Any]) -> str:
    """从课程条目中提取可读名称。"""
    name = str(item.get("name", "")).strip()
    if name and not name.lower().startswith("course_") and not name.lower().startswith("undefined"):
        return name

    data = item.get("data", "")
    match = re.search(r"kcmc=([^&]+)", data)
    if match:
        try:
            decoded = urllib.parse.unquote(match.group(1)).replace("+", " ")
            decoded = re.sub(r"^\(\d+\)\s*", "", decoded)
            decoded = re.sub(r"\s*[-+]?\d+\.0\s*学分\s*$", "", decoded)
            decoded = decoded.strip()
            if decoded:
                return decoded
        except Exception:
            pass

    kch_id = str(item.get("kch_id", "")).strip()
    if kch_id and kch_id not in ("undefined", "leftpage", "rightpage"):
        return f"课程 {kch_id}"

    return name or "未命名课程"


@app.get("/api/course_files")
async def api_course_files(token: str = Query(...)):
    _require_token(token)
    return {"files": _list_course_files()}


@app.post("/api/courses/import")
async def api_import_courses(payload: dict[str, Any]):
    _require_token(payload.get("token", ""))
    filename = payload.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="请选择课程文件")

    # 严格限制文件路径，防止目录遍历
    data_dir = Config.DATA_DIR
    target = (data_dir / filename).resolve()
    if not str(target).startswith(str(data_dir.resolve())) or not target.exists():
        raise HTTPException(status_code=400, detail="课程文件不存在或路径非法")

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取课程文件失败: {exc}")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="课程文件格式不正确，应为对象")

    imported = 0
    skipped = 0
    existing_data = {info["data"] for info in _courses.values()}

    for uid, item in raw.items():
        if not isinstance(item, dict):
            continue
        data = item.get("data", "")
        if not data:
            continue
        if data in existing_data:
            skipped += 1
            continue

        name = _extract_course_name(item)
        kch_id = str(item.get("kch_id", "")).strip()
        new_uid = str(uuid.uuid4())
        _courses[new_uid] = {"name": name, "kch_id": kch_id, "data": data}
        existing_data.add(data)
        imported += 1

    return {"success": True, "imported": imported, "skipped": skipped, "total": len(raw)}


@app.get("/api/courses")
async def api_courses(token: str = Query(...)):
    _require_token(token)
    return _courses


@app.post("/api/courses")
async def api_add_course(payload: dict[str, Any]):
    _require_token(payload.get("token", ""))
    name = payload.get("name", "").strip()
    data = payload.get("data", "").strip()

    if not name or not data:
        # 尝试从 cURL 中解析
        data = parse_curl_data(payload.get("curl", "")) or ""
        if not data:
            raise HTTPException(status_code=400, detail="课程名称和提交数据不能为空")

    uid = str(uuid.uuid4())
    _courses[uid] = {"name": name, "kch_id": payload.get("kch_id", ""), "data": data}
    return {"success": True, "uid": uid}


@app.delete("/api/courses/{uid}")
async def api_delete_course(uid: str, token: str = Query(...)):
    _require_token(token)
    if uid in _courses:
        del _courses[uid]
    return {"success": True}


# ---------------------- 抢课任务 ----------------------

@app.post("/api/tasks")
async def api_start_task(payload: dict[str, Any]):
    _require_token(payload.get("token", ""))
    user_remarks = payload.get("users", [])
    course_uids = payload.get("courses", [])

    if not user_remarks or not course_uids:
        raise HTTPException(status_code=400, detail="至少选择一个用户和一门课程")

    engine = _get_engine()
    task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    _tasks[task_id] = {"logs": [], "running": True, "created_at": time.time()}

    # 重定向 grab_engine 的回调到本任务的 task_id
    engine.status_callback = lambda e: _log_callback(task_id, e)

    for remark in user_remarks:
        user = _users.get(remark)
        if not user:
            _tasks[task_id]["logs"].append({
                "time": time.strftime("%H:%M:%S"),
                "user": remark,
                "course": "-",
                "status": "用户不存在",
                "message": None,
                "finished": True,
            })
            continue
        for uid in course_uids:
            course = _courses.get(uid)
            if not course:
                continue
            engine.launch(
                user_name=remark,
                cookie=user["cookie"],
                course_name=course["name"],
                data_str=course["data"],
                user_agent=user.get("user_agent"),
            )

    return {"success": True, "task_id": task_id}


@app.post("/api/tasks/{task_id}/stop")
async def api_stop_task(task_id: str, payload: dict[str, Any]):
    _require_token(payload.get("token", ""))
    engine = _get_engine()
    engine.stop_all()
    if task_id in _tasks:
        _tasks[task_id]["running"] = False
    return {"success": True}


@app.get("/api/tasks/{task_id}/logs")
async def api_task_logs(task_id: str, token: str = Query(...)):
    _require_token(token)
    task = _tasks.get(task_id, {"logs": [], "running": False})
    return {"running": task.get("running", False), "logs": task.get("logs", [])}


# ---------------------- WebSocket ----------------------

@app.websocket("/ws/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    await manager.connect(task_id, websocket)
    try:
        # 发送历史日志
        task = _tasks.get(task_id, {"logs": []})
        for log in task.get("logs", [])[-200:]:
            await websocket.send_json(log)
        while True:
            # 保持连接，客户端可发 ping
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(task_id, websocket)
    except Exception:
        manager.disconnect(task_id, websocket)


# ---------------------- 运行入口 ----------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
