"""通过 Selenium 或 HTTP 协议直接提取课程表单数据并导出为 JSON。"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from course_grabber.config import Config
from course_grabber.data_manager import DataManager
from course_grabber.utils import (
    build_chrome_driver,
    cookies_to_string,
    detect_captcha,
    extract_course_name,
    logger,
    perform_login,
    save_json,
    wait_for_login_complete,
)

# 作为全局参数处理的隐藏表单字段（适用于每一行）
GLOBAL_FIELDS = [
    "njdm_id",
    "zyh_id",
    "xkkz_id",
    "kklxdm",
    "xklc",
    "xkxnm",
    "xkxqm",
    "rlkz",
]

# 查询按钮定位策略（method, locator）
SEARCH_LOCATORS = [
    ("id", "search_go"),
    ("id", "btn_cx"),
    ("xpath", "//button[contains(text(),'查询')]"),
    ("xpath", "//button[contains(text(),'Query')]"),
    ("xpath", "//input[@type='button' and @value='查询']"),
]


def _extract_global_params(driver: WebDriver) -> dict[str, str]:
    from selenium.webdriver.common.by import By

    params: dict[str, str] = {}
    try:
        for inp in driver.find_elements(By.XPATH, "//input[@type='hidden']"):
            name = inp.get_attribute("name")
            value = inp.get_attribute("value") or ""
            if name in GLOBAL_FIELDS:
                params[name] = value
    except Exception as exc:
        logger.warning("Failed to extract global params: %s", exc)
    return params


def _click_search_button(driver: WebDriver) -> bool:
    from selenium.webdriver.common.by import By

    by_map = {"id": By.ID, "xpath": By.XPATH}
    for method, locator in SEARCH_LOCATORS:
        try:
            by_method = by_map[method]
            btn = driver.find_element(by_method, locator)
            if btn.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView();", btn)
                driver.execute_script("arguments[0].click();", btn)
                logger.info("Clicked search button via %s", locator)
                return True
        except Exception:
            continue
    return False


def _expand_all(driver: WebDriver) -> None:
    """点击所有可见的“更多”按钮，直到没有为止。"""
    from selenium.webdriver.common.by import By

    for _ in range(50):  # 安全上限
        try:
            buttons = driver.find_elements(By.XPATH, "//*[contains(text(), '更多')]")
            clicked = False
            for btn in buttons:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView();", btn)
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    clicked = True
            if not clicked:
                break
        except Exception:
            break


def _parse_onclick_ids(onclick: str) -> tuple[str | None, str | None]:
    """从 onclick 参数中提取 jxb_ids（32 位）和 kch_id（6-24 位）。"""
    ids = re.findall(r"'([^']+)'", onclick)
    jxb_ids = None
    kch_id = None
    for value in ids:
        if len(value) == 32 and not jxb_ids:
            jxb_ids = value
        elif 5 < len(value) < 25 and not kch_id:
            kch_id = value
    return jxb_ids, kch_id


def _extract_course_name_from_row(row, row_params: dict[str, str]) -> str:
    from selenium.webdriver.common.by import By

    name = row_params.get("kcmc", "")
    if name:
        return name

    try:
        header = row.find_element(
            By.XPATH,
            "./ancestor::div[contains(@class,'panel-collapse')]/preceding-sibling::div[contains(@class,'panel-heading')]",
        )
        name = header.text.strip().split("\n")[0]
        name = re.sub(r"^\(.*?\)\s*", "", name)
        return name
    except Exception:
        return f"Course_{row_params.get('kch_id', 'unknown')}"


def extract_courses(driver: WebDriver) -> dict[str, dict]:
    from selenium.webdriver.common.by import By

    global_params = _extract_global_params(driver)
    courses: dict[str, dict] = {}
    seen_data: set[str] = set()

    try:
        buttons = driver.find_elements(By.XPATH, "//*[@onclick][contains(@onclick, 'xk')]")
    except Exception as exc:
        logger.error("Could not locate course buttons: %s", exc)
        return courses

    logger.info("Scanning %d candidate buttons", len(buttons))

    for btn in buttons:
        try:
            row = btn.find_element(By.XPATH, "./ancestor::tr")
            onclick = btn.get_attribute("onclick") or ""

            row_params = global_params.copy()
            for inp in row.find_elements(By.XPATH, ".//input"):
                name = inp.get_attribute("name")
                value = inp.get_attribute("value") or ""
                if name:
                    row_params[name] = value

            jxb_ids, kch_id = _parse_onclick_ids(onclick)
            if jxb_ids:
                row_params["jxb_ids"] = jxb_ids
            if kch_id:
                row_params["kch_id"] = kch_id

            if not row_params.get("jxb_ids") or not row_params.get("kch_id"):
                continue

            row_params.setdefault("rwlx", "1")
            row_params.setdefault("rlkz", "0")

            data_str = "&".join(f"{k}={v}" for k, v in row_params.items())
            if data_str in seen_data:
                continue

            name = _extract_course_name_from_row(row, row_params)
            if not name or name.startswith("Course_undefined"):
                # 尝试从 payload 本身解码名称
                decoded = extract_course_name(data_str)
                if decoded:
                    name = decoded

            uid = str(uuid.uuid4())
            courses[uid] = {"kch_id": row_params["kch_id"], "name": name, "data": data_str}
            seen_data.add(data_str)

        except Exception as exc:
            logger.debug("Skipping one course row: %s", exc)
            continue

    return courses


def generate_courses_json() -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    username = Config.SINGLE_USERNAME
    password = Config.SINGLE_PASSWORD

    if not username or not password:
        logger.error("请在 .env 中设置 SINGLE_USERNAME 和 SINGLE_PASSWORD")
        return

    driver = build_chrome_driver(headless=False)
    try:
        logger.info("Logging in as %s", username)
        perform_login(driver, username, password)

        if detect_captcha(driver):
            logger.warning("检测到验证码，请手动处理后按回车继续...")
            input()

        if not wait_for_login_complete(driver, timeout=30):
            logger.error("登录超时，请检查账号密码或网络")
            return
        logger.info("登录成功")

        driver.get(Config.XK_INDEX_URL)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        if not _click_search_button(driver):
            logger.warning("自动点击查询按钮失败，请手动点击后按回车继续...")
            input()

        # 等待课程表格加载完成
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//*[@onclick][contains(@onclick, 'xk')]"))
        )

        _expand_all(driver)
        courses = extract_courses(driver)

        if not courses:
            logger.warning("未提取到课程，请确认页面已加载")
            return

        save_json(Config.COURSE_DB_FILE, courses)

        # 同时把 Cookie 保存到以用户名命名的用户记录中，方便后续使用
        cookies = driver.get_cookies()
        cookie_str = cookies_to_string(cookies)
        user_agent = driver.execute_script("return navigator.userAgent;")

        dm = DataManager()
        dm.add_user(username, cookie_str)

        logger.info("成功提取 %d 条课程数据", len(courses))
        logger.info("Cookie 已保存到用户 [%s]", username)
        logger.info("User-Agent: %s", user_agent)

        print("\n" + "=" * 60)
        print(f"成功提取 {len(courses)} 条数据")
        print(f"Cookie: {cookie_str}")
        print(f"User-Agent: {user_agent}")
        print("=" * 60)

    except Exception as exc:
        logger.exception("抓取课程时出错: %s", exc)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        input("按回车关闭...")


def _extract_global_params_from_soup(soup: BeautifulSoup) -> dict[str, str]:
    """从选课首页 HTML 解析全局隐藏参数。"""
    params: dict[str, str] = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = str(inp.get("name", ""))
        value = str(inp.get("value", ""))
        if name in GLOBAL_FIELDS:
            params[name] = value
    return params


def _extract_courses_from_soup(
    soup: BeautifulSoup, global_params: dict[str, str]
) -> dict[str, dict]:
    """从课程查询结果 HTML 解析课程数据（复用 Selenium 版的解析规则）。"""
    courses: dict[str, dict] = {}
    seen_data: set[str] = set()

    buttons = soup.find_all(attrs={"onclick": re.compile("xk")})
    logger.info("API 模式：扫描 %d 个候选按钮", len(buttons))

    for btn in buttons:
        try:
            onclick = str(btn.get("onclick", ""))
            row = btn.find_parent("tr")
            if not row:
                continue

            row_params = global_params.copy()
            for inp in row.find_all("input"):
                name = str(inp.get("name", ""))
                value = str(inp.get("value", ""))
                if name:
                    row_params[name] = value

            jxb_ids, kch_id = _parse_onclick_ids(onclick)
            if jxb_ids:
                row_params["jxb_ids"] = jxb_ids
            if kch_id:
                row_params["kch_id"] = kch_id

            if not row_params.get("jxb_ids") or not row_params.get("kch_id"):
                continue

            row_params.setdefault("rwlx", "1")
            row_params.setdefault("rlkz", "0")

            data_str = "&".join(f"{k}={v}" for k, v in row_params.items())
            if data_str in seen_data:
                continue

            name = _extract_course_name_from_row(None, row_params)
            if not name or name.startswith("Course_undefined"):
                decoded = extract_course_name(data_str)
                if decoded:
                    name = decoded

            uid = str(uuid.uuid4())
            courses[uid] = {"kch_id": row_params["kch_id"], "name": name, "data": data_str}
            seen_data.add(data_str)

        except Exception as exc:
            logger.debug("API 模式：跳过一行课程数据: %s", exc)
            continue

    return courses


def fetch_courses_via_api(
    cookie_str: str,
    user_agent: str | None = None,
    search_url: str | None = None,
) -> dict[str, dict]:
    """使用已有 Cookie 直接请求教务系统查询接口获取课程数据。

    Args:
        cookie_str: 登录后的 Cookie 字符串。
        user_agent: 可选的 User-Agent。
        search_url: 课程查询接口地址，默认使用常见端点；若抓包发现不同可覆盖。

    Returns:
        课程字典，键为 uuid，值为 {"kch_id": ..., "name": ..., "data": ...}。
    """
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": user_agent or Config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": Config.XK_INDEX_URL,
        "Cookie": cookie_str,
    })

    # 1. 拉取选课首页，解析全局隐藏参数
    resp = session.get(Config.XK_INDEX_URL, timeout=30, verify=Config.VERIFY_SSL)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "lxml")
    global_params = _extract_global_params_from_soup(soup)
    logger.info("API 模式：全局参数 %s", global_params)

    # 2. 请求课程查询接口
    target = search_url or f"{Config.BASE_URL}/xsxk/zzxkyzb_cxZzxkYzbPart.html"
    post_data = global_params.copy()
    # 常见额外参数，不同学校版本可能有差异
    post_data.setdefault("rwlx", "1")
    post_data.setdefault("rlkz", "0")
    post_data.setdefault("xh", "")

    resp = session.post(
        target,
        data=post_data,
        headers={
            "Referer": Config.XK_INDEX_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        timeout=30,
        verify=Config.VERIFY_SSL,
    )
    resp.raise_for_status()

    # 3. 解析课程列表
    result_soup = BeautifulSoup(resp.content, "lxml")
    courses = _extract_courses_from_soup(result_soup, global_params)
    logger.info("API 模式：提取到 %d 门课程", len(courses))
    return courses


def save_courses_json(courses: dict[str, dict]) -> None:
    """把课程字典保存到配置文件路径。"""
    save_json(Config.COURSE_DB_FILE, courses)


if __name__ == "__main__":
    generate_courses_json()
