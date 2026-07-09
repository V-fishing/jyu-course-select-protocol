"""抢课服务 Web 后端（用户端 + 管理端）。"""

from __future__ import annotations

import asyncio
import base64
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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from course_grabber.config import Config
from course_grabber.grab_engine import GrabEngine
from course_grabber.web_login import LoginError, WebLoginSession

# ---------------------- 全局状态 ----------------------

# 课程库：启动时从 db_courses_v7.json 加载，管理端也可更新
_courses: dict[str, dict[str, Any]] = {}

# 用户会话：用户登录后只保存在内存，服务重启失效
_user_sessions: dict[str, dict[str, Any]] = {}

# 管理员会话：用于爬取课程
_admin_session: dict[str, Any] | None = None

# 验证码会话池
_captcha_sessions: dict[str, WebLoginSession] = {}

# 任务日志
_tasks: dict[str, dict[str, Any]] = {}

# 口令
_ADMIN_TOKEN = Config.COOKIE_KEY or "admin"


def _require_admin(token: str) -> None:
    if token != _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="管理口令错误")


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


def _push_log(task_id: str, user: str, course: str, status: str, message: str | None = None, finished: bool = False) -> None:
    msg = {
        "time": time.strftime("%H:%M:%S"),
        "user": user,
        "course": course,
        "status": status,
        "message": message,
        "finished": finished,
    }
    _tasks.setdefault(task_id, {"logs": [], "running": True, "created_at": time.time()})
    _tasks[task_id]["logs"].append(msg)
    if len(_tasks[task_id]["logs"]) > 2000:
        _tasks[task_id]["logs"] = _tasks[task_id]["logs"][-2000:]
    if finished:
        _tasks[task_id]["running"] = False
    try:
        loop = asyncio.get_running_loop()
        asyncio.create_task(manager.broadcast(task_id, msg))
    except RuntimeError:
        try:
            asyncio.run(manager.broadcast(task_id, msg))
        except Exception:
            pass


# ---------------------- 课程加载 ----------------------

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


def _load_default_courses() -> None:
    """启动时默认加载 db_courses_v7.json。"""
    default_file = Config.DATA_DIR / "db_courses_v7.json"
    if not default_file.exists():
        return
    try:
        raw = json.loads(default_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        _courses.clear()
        for uid, item in raw.items():
            if isinstance(item, dict) and item.get("data"):
                _courses[uid] = {
                    "name": _extract_course_name(item),
                    "kch_id": str(item.get("kch_id", "")).strip(),
                    "data": item["data"],
                }
    except Exception as exc:
        print(f"[警告] 加载默认课程库失败: {exc}")


def _import_courses_from_file(filename: str) -> dict[str, int]:
    """从 data/ 下的 JSON 文件导入课程。"""
    data_dir = Config.DATA_DIR
    target = (data_dir / filename).resolve()
    if not str(target).startswith(str(data_dir.resolve())) or not target.exists():
        raise ValueError("课程文件不存在或路径非法")

    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("课程文件格式不正确")

    imported = 0
    skipped = 0
    existing_data = {info["data"] for info in _courses.values()}

    for uid, item in raw.items():
        if not isinstance(item, dict):
            continue
        data = item.get("data", "")
        if not data or data in existing_data:
            skipped += 1
            continue
        _courses[uid] = {
            "name": _extract_course_name(item),
            "kch_id": str(item.get("kch_id", "")).strip(),
            "data": data,
        }
        existing_data.add(data)
        imported += 1

    return {"imported": imported, "skipped": skipped, "total": len(raw)}


# ---------------------- 启动/关闭 ----------------------

_engine: GrabEngine | None = None
_engine_lock = threading.Lock()


def _get_engine() -> GrabEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = GrabEngine()
            _engine.start_dispatch()
        return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_default_courses()
    print(f"[课程库] 已加载 {_courses.__len__()} 门课程")
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
async def user_portal():
    return FileResponse(str(static_dir / "user.html"))


@app.get("/admin")
async def admin_portal():
    return FileResponse(str(static_dir / "admin.html"))


# ---------------------- 公共接口：验证码 ----------------------

@app.get("/api/captcha")
async def api_captcha():
    session_id = str(uuid.uuid4())
    session = WebLoginSession()
    try:
        info = session.get_captcha()
        _captcha_sessions[session_id] = session
        return {"session_id": session_id, **info}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取验证码失败: {exc}")


# ---------------------- 用户端接口 ----------------------

@app.post("/api/user/login")
async def api_user_login(payload: dict[str, Any]):
    session_id = payload.get("session_id", "")
    session = _captcha_sessions.pop(session_id, None)
    if not session:
        raise HTTPException(status_code=400, detail="验证码会话已过期，请重新获取验证码")

    username = payload.get("username", "").strip()
    password = payload.get("password", "").strip()
    captcha = payload.get("captcha", "").strip()

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

    user_session_id = str(uuid.uuid4())
    _user_sessions[user_session_id] = {
        "username": username,
        "cookie": result["cookie_str"],
        "user_agent": result["user_agent"],
        "login_at": time.time(),
    }
    return {
        "success": True,
        "username": username,
        "session_id": user_session_id,
        "message": "登录成功",
    }


@app.get("/api/user/courses")
async def api_user_courses():
    return {
        uid: {"name": info["name"], "kch_id": info["kch_id"]}
        for uid, info in _courses.items()
    }


@app.post("/api/user/tasks")
async def api_user_start_task(payload: dict[str, Any]):
    session_id = payload.get("session_id", "")
    user = _user_sessions.get(session_id)
    if not user:
        raise HTTPException(status_code=401, detail="用户未登录或会话已过期，请重新登录")

    course_uids = payload.get("courses", [])
    if not course_uids:
        raise HTTPException(status_code=400, detail="至少选择一门课程")

    engine = _get_engine()
    task_id = f"user_{user['username']}_{int(time.time())}"
    _tasks[task_id] = {"logs": [], "running": True, "created_at": time.time()}
    engine.status_callback = lambda e: _push_log(
        task_id, e.user_name, e.course_name, e.status, e.message, e.finished
    )

    _push_log(task_id, user["username"], "-", "开始抢课", f"共选择 {len(course_uids)} 门课程")

    launched = 0
    for uid in course_uids:
        course = _courses.get(uid)
        if not course:
            _push_log(task_id, user["username"], "-", "课程不存在", f"uid={uid}")
            continue
        engine.launch(
            user_name=user["username"],
            cookie=user["cookie"],
            course_name=course["name"],
            data_str=course["data"],
            user_agent=user.get("user_agent"),
        )
        launched += 1

    if launched == 0:
        _push_log(task_id, user["username"], "-", "没有可抢的课程", finished=True)

    return {"success": True, "task_id": task_id, "launched": launched}


@app.post("/api/user/tasks/{task_id}/stop")
async def api_user_stop_task(task_id: str):
    engine = _get_engine()
    engine.stop_all()
    if task_id in _tasks:
        _tasks[task_id]["running"] = False
        _push_log(task_id, "-", "-", "已停止全部任务", finished=True)
    return {"success": True}


@app.get("/api/user/tasks/{task_id}/logs")
async def api_user_task_logs(task_id: str):
    task = _tasks.get(task_id, {"logs": [], "running": False})
    return {"running": task.get("running", False), "logs": task.get("logs", [])}


# ---------------------- WebSocket：用户日志 ----------------------

@app.websocket("/ws/user/{task_id}")
async def websocket_user(websocket: WebSocket, task_id: str):
    await manager.connect(task_id, websocket)
    try:
        task = _tasks.get(task_id, {"logs": []})
        for log in task.get("logs", [])[-200:]:
            await websocket.send_json(log)
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(task_id, websocket)
    except Exception:
        manager.disconnect(task_id, websocket)


# ---------------------- 管理端接口 ----------------------

@app.post("/api/admin/login")
async def api_admin_login(payload: dict[str, Any]):
    _require_admin(payload.get("token", ""))
    session_id = payload.get("session_id", "")
    session = _captcha_sessions.pop(session_id, None)
    if not session:
        raise HTTPException(status_code=400, detail="验证码会话已过期")

    username = payload.get("username", "").strip()
    password = payload.get("password", "").strip()
    captcha = payload.get("captcha", "").strip()

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

    global _admin_session
    _admin_session = {
        "username": username,
        "cookie": result["cookie_str"],
        "user_agent": result["user_agent"],
        "login_at": time.time(),
    }
    return {"success": True, "username": username, "message": "管理员登录成功"}


@app.get("/api/admin/courses")
async def api_admin_courses(token: str = Query(...)):
    _require_admin(token)
    return _courses


@app.post("/api/admin/courses/import")
async def api_admin_import_courses(payload: dict[str, Any]):
    _require_admin(payload.get("token", ""))
    filename = payload.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="请选择课程文件")
    try:
        result = _import_courses_from_file(filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"success": True, **result}


@app.get("/api/admin/course_files")
async def api_admin_course_files(token: str = Query(...)):
    _require_admin(token)
    files = []
    for path in sorted(Config.DATA_DIR.glob("*.json")):
        if path.name in ("db_users_v7.json",):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = len(data) if isinstance(data, dict) else 0
            files.append({"name": path.name, "count": count})
        except Exception:
            pass
    return {"files": files}


@app.post("/api/admin/courses/scrape")
async def api_admin_scrape_courses(payload: dict[str, Any]):
    """使用管理员账号通过 Selenium 爬取课程（后台运行）。"""
    _require_admin(payload.get("token", ""))

    def _scrape():
        try:
            from scripts.get_course_json import generate_courses_json
            # 临时覆盖配置中的单用户账号为管理员账号
            original_user = Config.SINGLE_USERNAME
            original_pwd = Config.SINGLE_PASSWORD
            if _admin_session:
                Config.SINGLE_USERNAME = _admin_session["username"]
                Config.SINGLE_PASSWORD = ""
            generate_courses_json()
            Config.SINGLE_USERNAME = original_user
            Config.SINGLE_PASSWORD = original_pwd
            _load_default_courses()
        except Exception as exc:
            print(f"[爬取课程失败] {exc}")

    thread = threading.Thread(target=_scrape, daemon=True)
    thread.start()
    return {"success": True, "message": "课程爬取任务已在后台启动，完成后自动更新课程库"}


# ---------------------- 运行入口 ----------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
