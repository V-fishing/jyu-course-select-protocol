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

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from course_grabber.config import Config
from course_grabber.grab_engine import GrabEngine, StatusEvent
from course_grabber.utils import logger
from course_grabber.web_login import LoginError, WebLoginSession

# ---------------------- 全局状态 ----------------------

# 课程库：启动时从 db_courses_v7.json 加载，管理端也可更新
_courses: dict[str, dict[str, Any]] = {}

# 课程目录：从通识选修课等目录 JSON 加载，用于展示友好信息和筛选
_catalog: dict[str, dict[str, Any]] = {}

# 用户会话：用户登录后只保存在内存，服务重启失效
_user_sessions: dict[str, dict[str, Any]] = {}

# 管理员会话：用于爬取课程
_admin_session: dict[str, Any] | None = None

# 验证码会话池
_captcha_sessions: dict[str, WebLoginSession] = {}

# 任务日志
_tasks: dict[str, dict[str, Any]] = {}

# 子任务 task_id -> 所属聚合任务 task_id（防止多用户任务覆盖全局 callback）
_aggregate_map: dict[str, str] = {}
_aggregate_map_lock = threading.Lock()

# 口令
_ADMIN_TOKEN = Config.WEB_ADMIN_TOKEN or Config.COOKIE_KEY or "admin"
_WEAK_ADMIN_TOKENS = frozenset({"", "admin", "course-grabber-web"})


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


def _route_event(event: StatusEvent) -> None:
    """将 GrabEngine 子任务事件路由到对应的聚合任务日志。"""
    with _aggregate_map_lock:
        aggregate_task_id = _aggregate_map.get(event.task_id)
    if not aggregate_task_id:
        return
    _push_log(
        aggregate_task_id,
        event.user_name,
        event.course_name,
        event.status,
        event.message,
        event.finished,
    )
    if event.finished:
        with _aggregate_map_lock:
            _aggregate_map.pop(event.task_id, None)


# ---------------------- 课程加载 ----------------------

def _extract_course_name(item: dict[str, Any]) -> str:
    """从课程条目中提取可读名称；优先使用 data 中 kcmc 的教务系统官方名称。"""
    data = str(item.get("data", "")).strip()
    if data:
        match = re.search(r"kcmc=([^&]+)", data)
        if match:
            try:
                decoded = urllib.parse.unquote(match.group(1)).replace("+", " ")
                # kcmc 格式如 "(13102702)中国共产党简史 - 2.0 学分"
                decoded = re.sub(r"^\([^)]+\)\s*", "", decoded)
                decoded = re.sub(r"\s*[-+]?\d+\.0\s*学分\s*$", "", decoded)
                decoded = re.sub(r"\s*-\s*$", "", decoded)
                decoded = decoded.strip()
                if decoded:
                    return decoded
            except Exception:
                pass

    name = str(item.get("name", "")).strip()
    if name and not name.lower().startswith("course_") and not name.lower().startswith("undefined"):
        return name

    kch_id = str(item.get("kch_id", "")).strip()
    if kch_id and kch_id not in ("undefined", "leftpage", "rightpage"):
        return f"课程 {kch_id}"

    return name or "未命名课程"


def _normalize_course_name(name: str) -> str:
    """去除课程名中的班号、周次、学分等后缀，用于目录匹配。"""
    name = str(name).strip()
    if not name:
        return ""
    # 去除学分后缀
    name = re.sub(r"\s*[-+]?\d+\.0\s*学分\s*$", "", name)
    # 去除尾部班号及附加后缀，如 "-02"、"-02-(1~8周)"、"-02-人文社会科学"
    name = re.sub(r"-\d{1,2}(?:-.+)?$", "", name)
    return name.strip()


def _catalog_category(entry: dict[str, Any]) -> str:
    """根据目录条目推断课程归属；含"超星平台"关键字的课程归为网课。"""
    fields_to_check = [
        entry.get("教师姓名", ""),
        entry.get("上课地点", ""),
        entry.get("上课时间", ""),
        entry.get("课程名称", ""),
        entry.get("选课备注", ""),
        entry.get("课程简介", ""),
    ]
    if any("超星平台" in str(f) for f in fields_to_check):
        return "网课"
    return str(entry.get("课程归属", "")).strip()


def _load_catalog(filename: str = "2026-2027-1-通识选修课.json") -> None:
    """加载课程目录 JSON（如通识选修课开课信息表），并作为课程库基础条目。"""
    global _catalog
    target = Config.DATA_DIR / filename
    if not target.exists():
        return
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
        _catalog.clear()
        for idx, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("课程名称", f"__{idx}")).strip()
            if key:
                _catalog[key] = entry
                # 以目录条目作为课程库基础，等待爬取后补充 kch_id/data
                uid = f"catalog_{idx}"
                _courses[uid] = {
                    "name": key,
                    "kch_id": "",
                    "data": "",
                    "meta": {
                        "college": entry.get("开课学院", ""),
                        "teacher": entry.get("教师姓名", ""),
                        "credits": entry.get("学分", ""),
                        "location": entry.get("上课地点", ""),
                        "time": entry.get("上课时间", ""),
                        "campus": entry.get("校区名称", ""),
                        "category": _catalog_category(entry),
                        "capacity": entry.get("选课人数", ""),
                        "weeks": entry.get("起始结束周", ""),
                        "notes": entry.get("选课备注", ""),
                        "target": entry.get("面向对象", ""),
                        "restriction": entry.get("限制对象", ""),
                    },
                    "has_data": False,
                }
        print(f"[课程目录] 已加载 {_catalog.__len__()} 条目录记录")
    except Exception as exc:
        print(f"[警告] 加载课程目录失败: {exc}")


def _match_catalog(name: str) -> dict[str, Any] | None:
    """根据课程名称匹配目录条目，返回最匹配的元数据。"""
    if not _catalog:
        return None
    base = _normalize_course_name(name)
    if not base:
        return None

    # 优先：规范化后完全相等
    for entry in _catalog.values():
        catalog_base = _normalize_course_name(entry.get("课程名称", ""))
        if catalog_base and catalog_base == base:
            return entry

    # 次优：互相包含
    for entry in _catalog.values():
        catalog_name = str(entry.get("课程名称", "")).strip()
        if catalog_name and (base in catalog_name or catalog_name in base):
            return entry

    return None


def _merge_catalog_meta(course: dict[str, Any]) -> dict[str, Any]:
    """将目录元数据合并到课程记录中；若课程已有 data，尝试更新同名目录条目的 kch_id/data。"""
    course = dict(course)
    course["has_data"] = bool(course.get("data"))
    if not _catalog:
        return course

    name = course.get("name", "")
    entry = _match_catalog(name)
    if entry:
        meta = {
            "college": entry.get("开课学院", ""),
            "teacher": entry.get("教师姓名", ""),
            "credits": entry.get("学分", ""),
            "location": entry.get("上课地点", ""),
            "time": entry.get("上课时间", ""),
            "campus": entry.get("校区名称", ""),
            "category": _catalog_category(entry),
            "capacity": entry.get("选课人数", ""),
            "weeks": entry.get("起始结束周", ""),
            "notes": entry.get("选课备注", ""),
            "target": entry.get("面向对象", ""),
            "restriction": entry.get("限制对象", ""),
        }
        course["meta"] = meta

    return course


def _upsert_course(uid: str, course: dict[str, Any], *, allow_new: bool = True) -> bool:
    """添加或更新课程；如果课程名已存在且新记录有 data，则合并到现有记录。

    返回 True 表示成功写入/更新，False 表示因 allow_new=False 且未匹配到现有条目而跳过。
    """
    course = _merge_catalog_meta(course)
    course_name = _normalize_course_name(course.get("name", ""))
    # 按规范化名称查找是否已有同课程
    if course.get("data") and course_name:
        for existing_uid, existing in _courses.items():
            if _normalize_course_name(existing.get("name", "")) == course_name:
                # 更新现有条目（通常是目录条目）的抢课数据
                existing.update({
                    "kch_id": course.get("kch_id", existing.get("kch_id", "")),
                    "data": course["data"],
                    "has_data": True,
                })
                if "meta" in course:
                    existing["meta"] = course["meta"]
                return True
    if not allow_new:
        return False
    _courses[uid] = course
    return True


def _load_default_courses() -> None:
    """启动时默认加载 db_courses_v7.json，仅用于补充/更新现有课程库（目录或手动添加的条目）。"""
    default_file = Config.DATA_DIR / "db_courses_v7.json"
    if not default_file.exists():
        return
    try:
        raw = json.loads(default_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        for uid, item in raw.items():
            if isinstance(item, dict) and item.get("data"):
                course = {
                    "name": _extract_course_name(item),
                    "kch_id": str(item.get("kch_id", "")).strip(),
                    "data": item["data"],
                }
                _upsert_course(uid, course, allow_new=False)
    except Exception as exc:
        print(f"[警告] 加载默认课程库失败: {exc}")


def _parse_course_submit_data(raw: str) -> str:
    """从 cURL 命令或原始 form data 中提取课程提交数据字符串。"""
    raw = raw.strip()
    if raw.lower().startswith("curl"):
        # 优先匹配 --data-raw / --data / -d 后的引号内容
        match = re.search(r"--data(?:-raw)?\s+[\"']([^\"']+)[\"']", raw)
        if match:
            return match.group(1)
        match = re.search(r"-d\s+[\"']([^\"']+)[\"']", raw)
        if match:
            return match.group(1)
        raise ValueError("无法从 cURL 中提取提交数据，请直接粘贴 form data")
    return raw


def _import_courses_from_file(filename: str) -> dict[str, int]:
    """从 data/ 下的 JSON 文件导入课程；仅补充/更新已有课程条目，不新增无关条目。"""
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
        course = {
            "name": _extract_course_name(item),
            "kch_id": str(item.get("kch_id", "")).strip(),
            "data": data,
        }
        if _upsert_course(uid, course, allow_new=False):
            existing_data.add(data)
            imported += 1
        else:
            skipped += 1

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
    _load_catalog()
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

def _fetch_user_profile(cookie_str: str, user_agent: str | None = None) -> dict[str, str]:
    """使用登录 Cookie 获取用户学院、班级等基本信息。"""
    profile = {"college": "", "class_name": ""}
    try:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": user_agent or Config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{Config.BASE_URL}/xtgl/login_slogin.html",
            "Cookie": cookie_str,
        })
        ts = int(time.time() * 1000)
        url = f"{Config.BASE_URL}/xtgl/index_cxYhxxIndex.html?xt=jw&localeKey=zh_CN&_={ts}&gnmkdm=index"
        resp = session.get(url, timeout=15, verify=Config.VERIFY_SSL)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "lxml")
        # 示例格式：<p>计算机学院 计算机2301班</p>
        p_tag = soup.find("p")
        if p_tag:
            text = p_tag.get_text(strip=True)
            parts = text.split()
            if len(parts) >= 2:
                profile["college"] = parts[0]
                profile["class_name"] = parts[1]
            elif len(parts) == 1:
                profile["college"] = parts[0]
    except Exception as exc:
        logger.warning("获取用户 profile 失败: %s", exc)
    return profile


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
    profile = _fetch_user_profile(result["cookie_str"], result.get("user_agent"))
    _user_sessions[user_session_id] = {
        "username": username,
        "cookie": result["cookie_str"],
        "user_agent": result["user_agent"],
        "login_at": time.time(),
        "college": profile["college"],
        "class_name": profile["class_name"],
    }
    return {
        "success": True,
        "username": username,
        "session_id": user_session_id,
        "college": profile["college"],
        "class_name": profile["class_name"],
        "message": "登录成功",
    }


@app.get("/api/user/courses")
async def api_user_courses():
    return {
        uid: {
            "name": info["name"],
            "kch_id": info["kch_id"],
            "has_data": info.get("has_data", bool(info.get("data"))),
            "meta": info.get("meta", {}),
        }
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
    aggregate_task_id = f"user_{user['username']}_{int(time.time())}"
    _tasks[aggregate_task_id] = {"logs": [], "running": True, "created_at": time.time()}
    # 全局路由回调只需设置一次；后续任务复用同一逻辑
    engine.status_callback = _route_event

    _push_log(aggregate_task_id, user["username"], "-", "开始抢课", f"共选择 {len(course_uids)} 门课程")

    launched = 0
    for uid in course_uids:
        course = _courses.get(uid)
        if not course:
            _push_log(aggregate_task_id, user["username"], "-", "课程不存在", f"uid={uid}")
            continue
        sub_task_id = f"{aggregate_task_id}__{uid}"
        with _aggregate_map_lock:
            _aggregate_map[sub_task_id] = aggregate_task_id
        engine.launch(
            user_name=user["username"],
            cookie=user["cookie"],
            course_name=course["name"],
            data_str=course["data"],
            user_agent=user.get("user_agent"),
            task_id=sub_task_id,
        )
        launched += 1

    if launched == 0:
        _push_log(aggregate_task_id, user["username"], "-", "没有可抢的课程", finished=True)

    return {"success": True, "task_id": aggregate_task_id, "launched": launched}


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


@app.post("/api/admin/courses/manual")
async def api_admin_add_course_manual(payload: dict[str, Any]):
    """手动添加单门课程：支持粘贴 cURL 命令或原始 form data。"""
    _require_admin(payload.get("token", ""))
    name = payload.get("name", "").strip()
    raw = payload.get("data", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="课程名称不能为空")
    if not raw:
        raise HTTPException(status_code=400, detail="课程提交数据不能为空")

    try:
        data_str = _parse_course_submit_data(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"提交数据解析失败: {exc}")

    existing_data = {info["data"] for info in _courses.values()}
    if data_str in existing_data:
        raise HTTPException(status_code=400, detail="该课程提交数据已存在")

    uid = str(uuid.uuid4())
    course = {
        "name": name,
        "kch_id": "",
        "data": data_str,
    }
    _upsert_course(uid, course)
    return {"success": True, "uid": uid, "name": name, "message": "课程添加成功"}


@app.get("/api/admin/course_files")
async def api_admin_course_files(token: str = Query(...)):
    _require_admin(token)
    files = []
    for path in sorted(Config.DATA_DIR.glob("*.json")):
        if path.name in ("db_users_v7.json",):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                count = len(data)
            elif isinstance(data, list):
                count = len(data)
            else:
                count = 0
            files.append({"name": path.name, "count": count})
        except Exception:
            pass
    return {"files": files}


@app.post("/api/admin/catalog/import")
async def api_admin_import_catalog(payload: dict[str, Any]):
    """导入课程目录 JSON（如通识选修课开课信息表），用于 enriched 展示和筛选。"""
    _require_admin(payload.get("token", ""))
    filename = payload.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="请选择目录文件")
    try:
        _load_catalog(filename)
        # 重新加载课程库以合并新目录
        _load_default_courses()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "success": True,
        "count": len(_catalog),
        "message": f"已导入目录 {_catalog.__len__()} 条，课程库已重新合并",
    }


@app.post("/api/admin/courses/scrape")
async def api_admin_scrape_courses(payload: dict[str, Any]):
    """使用管理员 Cookie 直接请求教务系统查询接口爬取课程（后台运行）。"""
    _require_admin(payload.get("token", ""))

    if not _admin_session:
        raise HTTPException(status_code=401, detail="管理员未登录")

    def _scrape():
        courses: dict[str, Any] | None = None
        error_msg = ""

        # 优先尝试直接走 HTTP 协议拉取
        try:
            from scripts.get_course_json import fetch_courses_via_api, save_courses_json
            courses = fetch_courses_via_api(
                cookie_str=_admin_session["cookie"],
                user_agent=_admin_session.get("user_agent"),
            )
            if courses:
                save_courses_json(courses)
                _load_default_courses()
                print(f"[爬取课程成功] API 模式，共 {len(courses)} 门")
                return
            error_msg = "API 模式未返回课程"
        except Exception as exc:
            logger.warning("API 模式爬取失败: %s", exc)
            error_msg = str(exc)

        # API 失败时，若 .env 中配置了账号密码，回退到 Selenium
        try:
            from scripts.get_course_json import generate_courses_json
            original_user = Config.SINGLE_USERNAME
            original_pwd = Config.SINGLE_PASSWORD
            if Config.SINGLE_USERNAME and Config.SINGLE_PASSWORD:
                generate_courses_json()
                Config.SINGLE_USERNAME = original_user
                Config.SINGLE_PASSWORD = original_pwd
                _load_default_courses()
                print("[爬取课程成功] Selenium 模式")
                return
        except Exception as exc:
            logger.error("Selenium 模式爬取也失败: %s", exc)
            error_msg = f"{error_msg}; Selenium: {exc}"

        print(f"[爬取课程失败] {error_msg}")

    thread = threading.Thread(target=_scrape, daemon=True)
    thread.start()
    return {"success": True, "message": "课程爬取任务已在后台启动，完成后自动更新课程库"}


# ---------------------- 运行入口 ----------------------

if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="抢课 Web 服务 standalone 入口")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="本地端口")
    parser.add_argument(
        "--force-expose",
        action="store_true",
        help="强制允许弱口令绑定公网地址（不安全，仅测试环境）",
    )
    args = parser.parse_args()

    if (
        args.host not in ("127.0.0.1", "localhost", "::1")
        and _ADMIN_TOKEN in _WEAK_ADMIN_TOKENS
        and not args.force_expose
    ):
        raise SystemExit(
            "[安全错误] 管理口令为弱默认口令，禁止绑定公网地址。\n"
            "请执行以下任一操作：\n"
            "  1) 设置强 WEB_ADMIN_TOKEN 环境变量（推荐）\n"
            "  2) 使用 --host 127.0.0.1 仅本地访问\n"
            "  3) 使用 --force-expose 强制暴露（仅测试环境）"
        )
    if _ADMIN_TOKEN in _WEAK_ADMIN_TOKENS:
        print(f"[安全警告] 当前管理口令为弱默认口令 {_ADMIN_TOKEN!r}，建议设置 WEB_ADMIN_TOKEN")

    uvicorn.run(app, host=args.host, port=args.port)
