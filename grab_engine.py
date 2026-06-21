"""抢课任务核心引擎，包含状态队列与响应解析。"""

from __future__ import annotations

import queue
import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import requests

from config import Config
from utils import logger, random_delay


class TaskStatus(str, Enum):
    STARTING = "启动中"
    RUNNING = "请求中..."
    SUCCESS = "抢课成功"
    STOPPED = "已停止"
    VALIDATION_ERROR = "校验失败(请重抓包)"
    FINGERPRINT_ERROR = "指纹不符(UA错误)"
    COURSE_LIMIT = "门次超限(不可选)"
    CREDIT_LIMIT = "学分超限"
    TIME_CONFLICT = "时间冲突"
    FULL = "已满员"
    COOKIE_EXPIRED = "Cookie失效"
    NETWORK_ERROR = "网络波动"
    UNKNOWN = "未知响应"


@dataclass
class StatusEvent:
    task_id: str
    user_name: str
    course_name: str
    status: str
    message: str | None = None
    finished: bool = False


class GrabTask(threading.Thread):
    """单个抢课工作线程。"""

    MAX_CONSECUTIVE_ERRORS = 10

    def __init__(
        self,
        task_id: str,
        user_name: str,
        cookie: str,
        course_name: str,
        data_str: str,
        status_queue: queue.Queue,
        user_agent: str | None = None,
        referer: str | None = None,
    ):
        super().__init__(daemon=True)
        self.task_id = task_id
        self.user_name = user_name
        self.cookie = cookie
        self.course_name = course_name
        self.data_str = data_str
        self.status_queue = status_queue
        self.user_agent = user_agent or Config.USER_AGENT
        self.referer = referer or Config.XK_INDEX_URL

        self.running = True
        self.paused = False
        self.error_count = 0
        self.request_count = 0

    def emit(self, status: str, message: str | None = None, finished: bool = False) -> None:
        self.status_queue.put(
            StatusEvent(
                task_id=self.task_id,
                user_name=self.user_name,
                course_name=self.course_name,
                status=status,
                message=message,
                finished=finished,
            )
        )

    def build_headers(self) -> dict[str, str]:
        headers = dict(Config.DEFAULT_HEADERS)
        headers["User-Agent"] = self.user_agent
        headers["Referer"] = self.referer
        headers["Cookie"] = self.cookie
        return headers

    def parse_response(self, response: requests.Response) -> tuple[str, bool]:
        """返回 (展示状态, 是否应停止)。"""
        text = response.text

        try:
            res_json = response.json()
        except ValueError:
            res_json = None

        # 成功判定
        if "success" in text or (res_json and str(res_json.get("flag")) == "1"):
            return TaskStatus.SUCCESS, True

        # JSON 消息解析
        if res_json and isinstance(res_json, dict):
            msg = str(res_json.get("msg", ""))
            if msg:
                return self._classify_msg(msg)

        # 纯文本兜底
        if "冲突" in text:
            return TaskStatus.TIME_CONFLICT, False
        if "满" in text or "已满" in text:
            return TaskStatus.FULL, False
        if "登录" in text or "会话" in text:
            return TaskStatus.COOKIE_EXPIRED, True

        return TaskStatus.UNKNOWN, False

    def _classify_msg(self, msg: str) -> tuple[str, bool]:
        if "校验不通过" in msg:
            return TaskStatus.VALIDATION_ERROR, True
        if "加密串" in msg:
            return TaskStatus.FINGERPRINT_ERROR, True
        if "最高门次" in msg:
            return TaskStatus.COURSE_LIMIT, True
        if "学分" in msg and "上限" in msg:
            return TaskStatus.CREDIT_LIMIT, True
        if "冲突" in msg:
            return TaskStatus.TIME_CONFLICT, False
        if "满" in msg:
            return TaskStatus.FULL, False
        if "登录" in msg or "会话" in msg:
            return TaskStatus.COOKIE_EXPIRED, True

        return TaskStatus.UNKNOWN, False

    def run(self) -> None:
        self.emit(TaskStatus.STARTING)
        headers = self.build_headers()

        while self.running:
            while self.paused and self.running:
                time.sleep(0.2)
            if not self.running:
                break

            try:
                self.emit(TaskStatus.RUNNING)
                response = requests.post(
                    Config.SUBMIT_URL,
                    headers=headers,
                    data=self.data_str,
                    verify=False,
                    timeout=Config.GRAB_TIMEOUT,
                )
                self.request_count += 1
                status, should_stop = self.parse_response(response)

                if should_stop:
                    self.emit(status, finished=True)
                    self.running = False
                else:
                    self.emit(status)

                self.error_count = 0

            except requests.RequestException as exc:
                self.error_count += 1
                logger.warning("Network error for %s/%s: %s", self.user_name, self.course_name, exc)
                self.emit(TaskStatus.NETWORK_ERROR)
                if self.error_count > self.MAX_CONSECUTIVE_ERRORS:
                    self.emit(TaskStatus.NETWORK_ERROR, message="连续网络错误，自动停止", finished=True)
                    self.running = False

            random_delay()

        # 若被外部停止，确保发出最终 STOPPED 事件
        if self.running is False and not getattr(self, "_finished_emitted", False):
            self.emit(TaskStatus.STOPPED, finished=True)

    def stop(self) -> None:
        self.running = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


class GrabEngine:
    """管理多个 GrabTask 工作线程并聚合状态事件。"""

    def __init__(self, status_callback: Callable[[StatusEvent], None] | None = None):
        self.tasks: dict[str, GrabTask] = {}
        self.status_queue: queue.Queue = queue.Queue()
        self.status_callback = status_callback
        self._global_paused = False
        self._dispatcher_thread: threading.Thread | None = None
        self._running = True

    def start_dispatch(self) -> None:
        self._dispatcher_thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher_thread.start()

    def _dispatch_loop(self) -> None:
        while self._running:
            try:
                event = self.status_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event.finished:
                self.tasks.pop(event.task_id, None)
            if self.status_callback:
                self.status_callback(event)

    def launch(
        self,
        user_name: str,
        cookie: str,
        course_name: str,
        data_str: str,
        user_agent: str | None = None,
    ) -> str:
        task_id = f"{user_name}_{course_name}_{random.randint(1000, 9999)}"
        task = GrabTask(
            task_id=task_id,
            user_name=user_name,
            cookie=cookie,
            course_name=course_name,
            data_str=data_str,
            status_queue=self.status_queue,
            user_agent=user_agent,
        )
        self.tasks[task_id] = task
        task.start()
        return task_id

    def stop(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task:
            task.stop()
            return True
        return False

    def stop_all(self) -> None:
        for task in list(self.tasks.values()):
            task.stop()

    def pause_all(self) -> None:
        self._global_paused = True
        for task in self.tasks.values():
            task.pause()

    def resume_all(self) -> None:
        self._global_paused = False
        for task in self.tasks.values():
            task.resume()

    def is_paused(self) -> bool:
        return self._global_paused

    def stats(self) -> dict[str, int]:
        return {
            "total": len(self.tasks),
            "running": sum(1 for t in self.tasks.values() if t.running),
        }

    def shutdown(self) -> None:
        self.stop_all()
        self._running = False
        if self._dispatcher_thread:
            self._dispatcher_thread.join(timeout=2)


if __name__ == "__main__":
    engine = GrabEngine(status_callback=lambda e: print(e))
    engine.start_dispatch()
    print("引擎已启动")
    # engine.launch("test", "cookie", "course", "data=jxb")
    time.sleep(1)
    engine.shutdown()
