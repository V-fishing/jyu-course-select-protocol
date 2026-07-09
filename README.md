# 教务抢课脚本套件

基于 Selenium + Requests + Tkinter 的教务系统抢课辅助工具。

---

## 功能简介

- **批量登录**：并发获取多个账号的 Cookie。
- **课程抓取**：通过 Selenium 登录并提取课程表单数据，导出为 JSON。
- **抢课引擎**：多线程轮询提交选课请求，实时解析成功/失败状态。
- **图形界面**：集成的 Tkinter GUI，支持任务派发、状态监控、全局启停。
- **Cookie 监控**：检测账号 Cookie 是否仍然有效。
- **可选加密**：支持对存储的 Cookie 进行对称加密。

---

## 项目结构

```text
.
├── README.md                # 本文件
├── requirements.txt         # Python 依赖
├── .gitignore               # Git 忽略规则
├── .env.example             # 环境配置示例
├── .env                     # 真实配置（本地编辑，不提交）
├── accounts.json.example    # 账号列表示例
├── accounts.json            # 真实账号（本地编辑，不提交）
│
├── course_grabber/          # 核心库包
│   ├── __init__.py
│   ├── config.py            # 全局配置
│   ├── utils.py             # 公共工具（WebDriver、登录、Cookie、JSON、延迟等）
│   ├── data_manager.py      # 课程/用户数据持久化
│   ├── crypto_helper.py     # Cookie 加密/解密
│   ├── grab_engine.py       # 抢课任务调度与状态队列
│   └── web_login.py         # 基于 Requests + RSA 的登录（Web 服务用）
│
├── scripts/                 # 命令行入口
│   ├── batch_login.py       # 批量登录脚本
│   ├── get_course_json.py   # 课程数据抓取脚本
│   └── cookie_test.py       # Cookie 有效性检测脚本
│
├── gui/                     # 图形界面
│   └── final_spider_GUI.py  # 主程序图形界面
│
├── web_server.py            # FastAPI Web 服务后端
├── run_web.py               # Web 服务启动器（支持 ngrok）
├── static/                  # Web 前端页面
│   └── index.html
│
├── drivers/                 # WebDriver 二进制
│   └── chromedriver.exe     # 不提交到 Git
│
└── data/                    # 运行时数据
    ├── courses_export.json  # 课程库
    ├── db_users_v7.json     # 用户/Cookie 数据库
    ├── db_courses_v7.json   # 旧课程库
    └── app.log              # 运行日志
```

---

## 环境准备

1. 安装 Python 3.10+。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 下载与当前 Chrome 版本匹配的 `chromedriver.exe`，放置到 `drivers/` 目录，或确保系统 PATH 中可用。

---

## 配置说明

### 1. 复制环境配置模板

```bash
cp .env.example .env
```

编辑 `.env`，填写教务系统地址、默认账号、并发数、延迟范围等：

```dotenv
BASE_URL=http://210.38.162.118
SINGLE_USERNAME=231000001
SINGLE_PASSWORD=your_password
LOGIN_WORKERS=3
GRAB_DELAY_MIN=0.8
GRAB_DELAY_MAX=2.0
GRAB_TIMEOUT=5
```

### 2. 复制账号列表模板

```bash
cp accounts.json.example accounts.json
```

按格式填写需要批量登录的账号：

```json
[
    {
        "name": "示例用户",
        "username": "231000001",
        "password": "your_password_here"
    }
]
```

> 注意：`.env` 和 `accounts.json` 已加入 `.gitignore`，不会被提交到 Git。

---

## 使用流程

### 方式一：命令行脚本

1. **批量登录，获取 Cookie**

```bash
python scripts/batch_login.py
```

2. **抓取课程数据**

```bash
python scripts/get_course_json.py
```

3. **启动抢课监控（图形界面）**

```bash
python gui/final_spider_GUI.py
```

4. **检测 Cookie 是否有效**

```bash
python scripts/cookie_test.py
```

### 方式二：图形界面

直接运行主程序：

```bash
python gui/final_spider_GUI.py
```

### 方式三：Web 控制台（支持远程访问）

适合多人临时共用一台电脑抢课，可通过 ngrok 暴露公网入口。

```bash
# 本地启动
python run_web.py

# 带 ngrok 公网隧道启动
python run_web.py --ngrok --open
```

启动后：

1. 打开浏览器访问控制台（本地 `http://localhost:8000`，ngrok 模式下使用分配的 HTTPS 地址）。
2. 输入访问口令（`.env` 中的 `COOKIE_KEY`，未设置则默认 `course-grabber-web`）。
3. 在“登录”页输入账号、密码、验证码，获取 Cookie。
4. 在“课程管理”页粘贴选课请求的 cURL 或 form data。
5. 选择用户和课程，点击“开始抢课”，实时查看日志。

> 注意：Web 服务会接收用户的账号密码进行登录，但**不会保存密码**，只保存 Cookie。使用 ngrok 时请确保访问口令足够复杂，并仅分享给可信人员。

---

## 各脚本说明

| 脚本 | 作用 |
|------|------|
| `scripts/batch_login.py` | 读取 `accounts.json`，并发登录，保存 Cookie 到用户数据库 |
| `scripts/get_course_json.py` | 单账号登录，自动点击查询，提取课程表单参数 |
| `scripts/cookie_test.py` | 单个或批量检测 Cookie 存活状态 |
| `web_server.py` | FastAPI Web 服务后端（验证码/登录/Cookie/抢课/WebSocket 日志） |
| `run_web.py` | Web 服务启动器，支持本地运行和 ngrok 公网隧道 |
| `gui/final_spider_GUI.py` | 本地 Tkinter 抢课主界面 |
| `course_grabber/grab_engine.py` | 抢课核心引擎，管理多线程任务队列 |
| `course_grabber/web_login.py` | 基于 Requests + RSA + 验证码的登录模块 |
| `course_grabber/data_manager.py` | 课程/用户 JSON 数据管理 |
| `course_grabber/crypto_helper.py` | Cookie 加解密工具 |
| `course_grabber/config.py` | 全局配置读取 |
| `course_grabber/utils.py` | WebDriver 初始化、登录、Cookie 转换、JSON 读写等公共函数 |

---

## 安全提醒

- 账号密码仅保存在本地 `.env` 和 `accounts.json` 中，不要上传到公开仓库。
- Cookie 包含登录会话信息，建议开启 `ENCRYPT_COOKIES=true` 并妥善保管 `COOKIE_KEY`。
- 不要将 `.env`、`accounts.json`、`data/db_users_v7.json` 等文件发送给他人。

---

## 免责声明

本项目仅用于学习和研究教务系统交互原理，请遵守学校相关规定，合理使用，不要对教务服务器造成过大压力。因使用不当造成的任何后果由使用者自行承担。
