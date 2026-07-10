"""启动抢课 Web 服务，可选自动创建 ngrok 公网隧道。"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from course_grabber.config import Config

_WEAK_ADMIN_TOKENS = frozenset({"", "admin", "course-grabber-web"})


def _admin_token() -> str:
    return Config.WEB_ADMIN_TOKEN or Config.COOKIE_KEY or "admin"


def _is_public_host(host: str) -> bool:
    return host not in ("127.0.0.1", "localhost", "::1")


def _check_admin_token(host: str, force_expose: bool) -> None:
    token = _admin_token()
    if _is_public_host(host) and token in _WEAK_ADMIN_TOKENS and not force_expose:
        raise SystemExit(
            "[安全错误] 管理口令为弱默认口令，禁止绑定公网地址。\n"
            "请执行以下任一操作：\n"
            "  1) 设置强 WEB_ADMIN_TOKEN 环境变量（推荐）\n"
            "  2) 使用 --host 127.0.0.1 仅本地访问\n"
            "  3) 使用 --force-expose 强制暴露（仅测试环境）"
        )
    if token in _WEAK_ADMIN_TOKENS:
        print(f"[安全警告] 当前管理口令为弱默认口令 {token!r}，建议设置 WEB_ADMIN_TOKEN")


def main() -> None:
    parser = argparse.ArgumentParser(description="抢课 Web 服务启动器")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="本地端口")
    parser.add_argument("--ngrok", action="store_true", help="启用 ngrok 公网隧道")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument(
        "--force-expose",
        action="store_true",
        help="强制允许弱口令绑定公网地址（不安全，仅测试环境）",
    )
    args = parser.parse_args()

    _check_admin_token(args.host, args.force_expose)

    local_url = f"http://localhost:{args.port}"
    public_url = None

    if args.ngrok:
        try:
            from pyngrok import ngrok
            tunnel = ngrok.connect(args.port, "http")
            public_url = tunnel.public_url
            print(f"[ngrok] 公网地址: {public_url}")
            print(f"[ngrok] 控制台: http://127.0.0.1:4040")
        except Exception as exc:
            print(f"[ngrok] 启动失败: {exc}")
            print("[提示] 请确保已安装 pyngrok 并在 ngrok 官网注册获取 authtoken")
            print("       ngrok config add-authtoken <your_token>")

    print(f"[本地服务] {local_url}")
    token = _admin_token()
    masked = token[:4] + "*" * max(0, len(token) - 4) if len(token) > 4 else "****"
    print(f"[访问口令] {masked}")
    print("按 Ctrl+C 停止服务")

    if args.open:
        webbrowser.open(public_url or local_url)

    import uvicorn
    uvicorn.run("web_server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
