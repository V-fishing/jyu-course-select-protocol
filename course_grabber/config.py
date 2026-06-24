"""抢课工具套件的全局配置。"""

import os
from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _BASE_DIR / "data"
_ENV_PATH = _BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH, override=True)


class Config:
    """项目级设置，支持通过 .env 文件覆盖。"""

    # 路径
    BASE_DIR = _BASE_DIR
    DATA_DIR = _DATA_DIR
    COURSE_DB_FILE = str(DATA_DIR / "courses_export.json")
    USER_DB_FILE = str(DATA_DIR / "db_users_v7.json")
    ACCOUNTS_FILE = str(BASE_DIR / "accounts.json")
    LOG_FILE = str(DATA_DIR / "app.log")

    # URL
    BASE_URL = os.getenv("BASE_URL", "http://210.38.162.118").rstrip("/")
    LOGIN_URL = f"{BASE_URL}/xtgl/login_slogin.html"
    XK_INDEX_URL = (
        f"{BASE_URL}/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512&layout=default"
    )
    SUBMIT_URL = f"{BASE_URL}/xsxk/zzxkyzbjk_xkBcZyZzxkYzb.html?gnmkdm=N253512"

    # 单用户脚本使用的默认账号
    SINGLE_USERNAME = os.getenv("SINGLE_USERNAME", "")
    SINGLE_PASSWORD = os.getenv("SINGLE_PASSWORD", "")

    # HTTP / 请求设置
    USER_AGENT = os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    GRAB_TIMEOUT = int(os.getenv("GRAB_TIMEOUT", "5"))
    GRAB_DELAY_MIN = float(os.getenv("GRAB_DELAY_MIN", "0.8"))
    GRAB_DELAY_MAX = float(os.getenv("GRAB_DELAY_MAX", "2.0"))

    # 并发
    LOGIN_WORKERS = int(os.getenv("LOGIN_WORKERS", "3"))

    # 安全
    ENCRYPT_COOKIES = os.getenv("ENCRYPT_COOKIES", "false").lower() == "true"
    COOKIE_KEY = os.getenv("COOKIE_KEY", "")

    # 默认请求头（基于请求的脚本应复制并填入 Cookie）
    DEFAULT_HEADERS = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": XK_INDEX_URL,
    }


if __name__ == "__main__":
    for key, value in sorted(vars(Config).items()):
        if not key.startswith("_"):
            print(f"{key}: {value}")
