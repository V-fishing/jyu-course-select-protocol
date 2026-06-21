"""批量 Selenium 登录：并发获取多个账号的 Cookie。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from config import Config
from data_manager import DataManager
from utils import (
    build_chrome_driver,
    cookies_to_string,
    detect_captcha,
    load_json,
    logger,
    perform_login,
    random_delay,
    wait_for_login_complete,
)


def load_accounts(path: str | None = None) -> list[dict[str, str]]:
    """从外部 JSON 加载账号列表。"""
    path = path or Config.ACCOUNTS_FILE
    accounts = load_json(path, default=[])
    if not isinstance(accounts, list):
        logger.error("%s 必须包含一个 JSON 数组", path)
        return []
    valid = []
    for item in accounts:
        if isinstance(item, dict) and item.get("username") and item.get("password"):
            valid.append(item)
        else:
            logger.warning("跳过无效账号条目: %s", item)
    return valid


def login_one(account: dict[str, str]) -> tuple[str, str]:
    """登录单个账号，返回 (备注名, cookie字符串)。"""
    username = account["username"]
    password = account["password"]
    remark = account.get("name", username)

    driver = build_chrome_driver(headless=True)
    cookie_str = ""
    try:
        logger.info("[%s] 开始登录", remark)
        perform_login(driver, username, password)

        if detect_captcha(driver):
            logger.warning("[%s] 检测到验证码，跳过", remark)
            return remark, ""

        if not wait_for_login_complete(driver, timeout=30):
            logger.warning("[%s] 登录超时或失败", remark)
            return remark, ""

        cookie_str = cookies_to_string(driver.get_cookies())
        logger.info("[%s] 登录成功", remark)

    except Exception as exc:
        logger.exception("[%s] 登录异常: %s", remark, exc)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return remark, cookie_str


def main() -> None:
    accounts = load_accounts()
    if not accounts:
        logger.error("未找到有效账号，请填写 %s", Config.ACCOUNTS_FILE)
        return

    logger.info("开始批量登录 %d 个账号，并发数 %d", len(accounts), Config.LOGIN_WORKERS)

    dm = DataManager()
    success = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=Config.LOGIN_WORKERS) as executor:
        futures = {executor.submit(login_one, acc): acc for acc in accounts}
        for future in as_completed(futures):
            remark, cookie = future.result()
            if cookie:
                dm.add_user(remark, cookie)
                success += 1
            else:
                failed += 1
            random_delay(0.5, 1.5)

    logger.info("批量登录完成：成功 %d，失败 %d", success, failed)
    print(f"\n完成！成功 {success}，失败 {failed}。数据已保存到 {Config.USER_DB_FILE}")


if __name__ == "__main__":
    main()
