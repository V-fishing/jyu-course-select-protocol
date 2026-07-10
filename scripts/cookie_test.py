"""监控一个或多个账号的 Cookie 有效性。"""

from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from course_grabber.config import Config
from course_grabber.data_manager import DataManager
from course_grabber.utils import logger


TARGET_URL = f"{Config.BASE_URL}/xtgl/index_initMenu.html?jsdm=xs"
INTERVAL_MINUTES = 1


def is_cookie_alive(cookie: str, user_agent: str | None = None) -> bool:
    """如果 Cookie 仍有效则返回 True。"""
    headers = {
        "User-Agent": user_agent or Config.USER_AGENT,
        "Cookie": cookie,
        "Referer": Config.XK_INDEX_URL,
    }
    try:
        response = requests.get(
            TARGET_URL,
            headers=headers,
            allow_redirects=False,
            verify=Config.VERIFY_SSL,
            timeout=10,
        )
        if response.status_code == 200 and "login_slogin.html" not in response.text:
            return True
        if response.status_code == 302 and "login" in response.headers.get("Location", ""):
            return False
        return False
    except requests.RequestException as exc:
        logger.warning("Cookie 检测请求失败: %s", exc)
        return False


def monitor_single(name: str, cookie: str, interval_minutes: int = INTERVAL_MINUTES) -> None:
    """监控单个 Cookie，直到失效或用户中断。"""
    start_time = datetime.datetime.now()
    print(f"开始监控 [{name}] 的 Cookie 存活状态")
    print("-" * 40)

    try:
        while True:
            current_time = datetime.datetime.now()
            alive = is_cookie_alive(cookie)
            duration = current_time - start_time

            if alive:
                print(f"[{current_time.strftime('%H:%M:%S')}] ✅ [{name}] 存活中... (已维持 {duration})")
            else:
                print(f"[{current_time.strftime('%H:%M:%S')}] ❌ [{name}] Cookie 已失效！")
                break

            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        print(f"\n已停止监控 [{name}]")


def monitor_all(interval_minutes: int = INTERVAL_MINUTES) -> None:
    """监控用户数据库中的所有 Cookie。"""
    dm = DataManager()
    users = dm.all_users()
    if not users:
        logger.error("用户数据库为空，请先运行 batch_login.py")
        return

    start_time = datetime.datetime.now()
    print(f"开始批量监控 {len(users)} 个账号的 Cookie")
    print("-" * 60)

    try:
        while True:
            current_time = datetime.datetime.now()
            duration = current_time - start_time
            print(f"\n[{current_time.strftime('%H:%M:%S')}] 已运行 {duration}")

            for name, cookie in users.items():
                alive = is_cookie_alive(cookie)
                status = "✅ 存活" if alive else "❌ 失效"
                print(f"  {status:<6} {name}")

            if not any(is_cookie_alive(cookie) for cookie in users.values()):
                print("所有 Cookie 均已失效，停止监控")
                break

            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        print("\n已停止批量监控")


def main() -> None:
    print("Cookie 监控工具")
    print("1. 监控单个 Cookie（在代码中配置）")
    print("2. 批量监控用户数据库中的所有 Cookie")
    choice = input("请选择 [1/2，默认2]: ").strip() or "2"

    if choice == "1":
        cookie = input("请输入 Cookie: ").strip()
        name = input("请输入备注名 [default]: ").strip() or "default"
        monitor_single(name, cookie)
    else:
        monitor_all()


if __name__ == "__main__":
    main()
