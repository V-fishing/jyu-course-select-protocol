"""基于 Requests + RSA + 验证码的教务登录（供 Web 服务使用）。"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
import rsa
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from course_grabber.config import Config


def _now_ms() -> str:
    return str(int(time.time() * 1000))


class WebLoginSession:
    """使用 requests 完成教务系统登录，不依赖 Selenium。"""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or Config.BASE_URL).rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": Config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        })
        self._rsa_pub_key: rsa.PublicKey | None = None
        self.csrftoken = ""
        self.hidden_fields: dict[str, str] = {}

    def get_captcha(self) -> dict[str, str]:
        """访问登录页并获取验证码。返回 base64 图片、时间戳、csrftoken、隐藏字段。"""
        login_url = urljoin(self.base_url, "/xtgl/login_slogin.html")
        resp = self.session.get(login_url, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.content, "lxml")
        csrf_input = soup.find("input", {"name": "csrftoken"}) or soup.find("input", {"id": "csrftoken"})
        raw_token = csrf_input.get("value", "") if csrf_input else ""
        self.csrftoken = raw_token.split(",")[0] if "," in raw_token else raw_token

        self.hidden_fields = {}
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = str(inp.get("name", ""))
            val = str(inp.get("value", ""))
            if name and name not in self.hidden_fields:
                self.hidden_fields[name] = val

        captcha_url = urljoin(self.base_url, "/kaptcha")
        ts = _now_ms()
        resp = self.session.get(
            f"{captcha_url}?time={ts}",
            headers={"Referer": login_url},
            timeout=30,
        )
        resp.raise_for_status()
        image_b64 = base64.b64encode(resp.content).decode()

        return {
            "image_base64": f"data:image/png;base64,{image_b64}",
            "captcha_ts": ts,
            "csrftoken": self.csrftoken,
            "hidden_fields": self.hidden_fields,
        }

    def _rsa_encrypt(self, password: str) -> str:
        """用教务系统公钥加密密码。"""
        if self._rsa_pub_key is None:
            pub_url = urljoin(self.base_url, "/xtgl/login_getPublicKey.html")
            ts = _now_ms()
            r = self.session.get(
                f"{pub_url}?time={ts}",
                headers={
                    "Referer": urljoin(self.base_url, "/xtgl/login_slogin.html"),
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=30,
            )
            r.raise_for_status()
            key_data = r.json()
            modulus = int.from_bytes(base64.b64decode(key_data["modulus"]), "big")
            exponent = int.from_bytes(base64.b64decode(key_data["exponent"]), "big")
            self._rsa_pub_key = rsa.PublicKey(modulus, exponent)

        crypto = rsa.encrypt(password.encode("utf-8"), self._rsa_pub_key)
        return base64.b64encode(crypto).decode()

    def login(
        self,
        username: str,
        password: str,
        captcha: str,
        captcha_ts: str,
        csrftoken: str,
        hidden_fields: dict[str, str] | None = None,
        need_rsa: bool = True,
    ) -> dict[str, str]:
        """执行登录，成功返回 Cookie 字符串。"""
        login_url = urljoin(self.base_url, "/xtgl/login_slogin.html")

        encrypted_pwd = self._rsa_encrypt(password) if need_rsa else password

        ts = captcha_ts or _now_ms()
        login_data: list[tuple[str, str]] = []
        explicit_keys = {"csrftoken", "language", "ydType", "yhm", "mm", "yzm"}
        for k, v in (hidden_fields or self.hidden_fields).items():
            if k and k not in explicit_keys:
                login_data.append((k, v))
        login_data.extend([
            ("csrftoken", csrftoken or self.csrftoken),
            ("language", "zh_CN"),
            ("ydType", ""),
            ("yhm", username),
            ("mm", encrypted_pwd),
            ("mm", encrypted_pwd),
            ("yzm", captcha),
        ])

        post_url = f"{login_url}?time={ts}"
        resp = self.session.post(
            post_url,
            data=login_data,
            headers={"Referer": login_url},
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()

        if "login_slogin.html" in resp.url:
            tips_text = ""
            try:
                soup = BeautifulSoup(resp.text, "lxml")
                tips_tag = soup.find("p", {"id": "tips"})
                if tips_tag:
                    tips_text = tips_tag.get_text(strip=True)
            except Exception:
                pass
            if "验证码" in tips_text:
                raise LoginError("验证码错误，请重新输入")
            if "密码" in tips_text or "账号" in tips_text or "用户名" in tips_text:
                raise LoginError("账号或密码不正确")
            raise LoginError(tips_text or "登录失败，请检查账号密码和验证码")

        cookies = "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)
        return {
            "cookie_str": cookies,
            "user_agent": Config.USER_AGENT,
        }


class LoginError(Exception):
    """登录失败异常。"""


if __name__ == "__main__":
    s = WebLoginSession()
    captcha_info = s.get_captcha()
    print("请访问验证码图片:", captcha_info["image_base64"][:80] + "...")
    code = input("请输入验证码: ").strip()
    result = s.login(
        username=input("账号: ").strip(),
        password=input("密码: ").strip(),
        captcha=code,
        captcha_ts=captcha_info["captcha_ts"],
        csrftoken=captcha_info["csrftoken"],
        hidden_fields=captcha_info["hidden_fields"],
    )
    print("登录成功，Cookie:", result["cookie_str"])
