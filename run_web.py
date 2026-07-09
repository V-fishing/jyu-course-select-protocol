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


def main() -> None:
    parser = argparse.ArgumentParser(description="抢课 Web 服务启动器")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址")
    parser.add_argument("--port", type=int, default=8000, help="本地端口")
    parser.add_argument("--ngrok", action="store_true", help="启用 ngrok 公网隧道")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

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
    print(f"[访问口令] {Config.COOKIE_KEY or 'course-grabber-web'}")
    print("按 Ctrl+C 停止服务")

    if args.open:
        webbrowser.open(public_url or local_url)

    import uvicorn
    uvicorn.run("web_server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
