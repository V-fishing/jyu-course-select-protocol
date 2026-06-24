"""抢课脚本共享工具函数。"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

import urllib3

from course_grabber.config import Config

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

# 针对内网自签名证书，关闭 verify=False 产生的 TLS 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def setup_logging(name: str = "course_grabber") -> logging.Logger:
    """配置简单的文件 + 控制台日志。"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        file_handler = logging.FileHandler(Config.LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Could not create log file: %s", exc)

    return logger


logger = setup_logging()


def load_json(path: str | Path, default: Any = None) -> Any:
    """加载 JSON，失败时返回安全默认值并记录错误。"""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        logger.error("JSON decode failed for %s: %s", path, exc)
    except OSError as exc:
        logger.error("Failed to read %s: %s", path, exc)
    return default if default is not None else {}


def save_json(path: str | Path, data: Any, backup: bool = True) -> None:
    """原子方式保存 JSON，可选备份旧文件。"""
    path = Path(path)
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup_path)
        except OSError as exc:
            logger.warning("Could not backup %s: %s", path, exc)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        tmp_path.replace(path)
    except OSError as exc:
        logger.error("Failed to save %s: %s", path, exc)
        raise


def build_chrome_driver(
    headless: bool = False,
    user_agent: str | None = None,
    window_size: tuple[int, int] = (1280, 900),
) -> "WebDriver":
    """创建 Chrome WebDriver，统一选项并加入反检测参数。"""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--window-size={window_size[0]},{window_size[1]}")

    ua = user_agent or Config.USER_AGENT
    options.add_argument(f"user-agent={ua}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    try:
        service = Service(ChromeDriverManager().install())
    except Exception as exc:
        logger.warning("ChromeDriverManager failed (%s), falling back to chromedriver.exe", exc)
        service = Service(str(Config.BASE_DIR / "drivers" / "chromedriver.exe"))

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        },
    )
    return driver


def perform_login(
    driver: "WebDriver",
    username: str,
    password: str,
    timeout: int = 10,
) -> bool:
    """填写账号密码并提交登录表单。"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(Config.LOGIN_URL)
    wait = WebDriverWait(driver, timeout)
    user_input = wait.until(EC.presence_of_element_located((By.ID, "yhm")))
    pwd_input = driver.find_element(By.ID, "mm")
    login_btn = driver.find_element(By.ID, "dl")

    user_input.clear()
    user_input.send_keys(username)
    pwd_input.clear()
    pwd_input.send_keys(password)
    login_btn.click()
    return True


def wait_for_login_complete(driver: "WebDriver", timeout: int = 30) -> bool:
    """等待当前 URL 不再包含登录页面路径。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "login_slogin.html" not in driver.current_url:
            return True
        time.sleep(0.5)
    return False


def cookies_to_string(cookies: list[dict[str, Any]]) -> str:
    """将 Selenium cookies 转换为 Cookie 请求头字符串。"""
    parts = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def string_to_cookie_dict(cookie_str: str) -> dict[str, str]:
    """将 Cookie 请求头字符串解析为字典。"""
    result: dict[str, str] = {}
    if not cookie_str:
        return result
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            result[name.strip()] = value.strip()
    return result


def random_delay(min_seconds: float | None = None, max_seconds: float | None = None) -> None:
    """随机睡眠一段时间，避免请求间隔过于规律。"""
    min_s = min_seconds if min_seconds is not None else Config.GRAB_DELAY_MIN
    max_s = max_seconds if max_seconds is not None else Config.GRAB_DELAY_MAX
    if min_s > max_s:
        min_s, max_s = max_s, min_s
    time.sleep(random.uniform(min_s, max_s))


def parse_curl_data(text: str) -> str | None:
    """从复制的 cURL 命令中提取 urlencoded 表单数据。"""
    # --data-raw '...' 或 --data '...'
    match = re.search(r"--data(?:-raw)?\s+['\"](.+?)['\"]", text, re.DOTALL)
    if match:
        return match.group(1).replace("^", "")

    # 宽松兜底：匹配 jxb_ids=... 直到引号、空格或结尾
    match = re.search(r"(jxb_ids=[^'\"\s]+)", text)
    if match:
        return match.group(1).replace("^", "")

    return None


def detect_captcha(driver: "WebDriver") -> bool:
    """启发式检测页面中是否存在验证码图片或输入框。"""
    captcha_indicators = ["captcha", "yzm", "验证码", "verifyCode"]
    page_source = driver.page_source.lower()
    return any(indicator in page_source for indicator in captcha_indicators)


def extract_course_name(data: str) -> str:
    """尝试从 urlencoded 表单数据中解码课程名称 kcmc。"""
    match = re.search(r"kcmc=([^&]+)", data)
    if match:
        try:
            decoded = urllib.parse.unquote(match.group(1))
            return decoded.split("(")[0].strip()
        except Exception as exc:
            logger.debug("Could not decode course name: %s", exc)
    return ""


def ensure_dir(path: str | Path) -> None:
    """确保文件所在目录存在。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def safe_remove(path: str | Path) -> None:
    """删除文件（若存在），失败时记录日志。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path, exc)


if __name__ == "__main__":
    print("Utils loaded. Config sample:")
    print("BASE_URL:", Config.BASE_URL)
    print("LOG_FILE:", Config.LOG_FILE)
    sample = "curl 'http://x' --data-raw 'jxb_ids=abc&kch_id=123&kcmc=%E8%AE%A1%E7%AE%97%E6%9C%BA'"
    print("Parsed cURL data:", parse_curl_data(sample))
    print("Course name:", extract_course_name(parse_curl_data(sample)))
