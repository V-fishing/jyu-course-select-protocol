# 嘉应学院教务抢课助手

基于 Selenium + Requests + FastAPI + WebSocket 的教务系统抢课辅助工具，支持 Web 控制台、命令行脚本和 Tkinter GUI 三种使用方式。

---

## 功能简介

- **Web 用户端**：浏览器登录后按校区、学院、教师、学分、课程归属、上课时间筛选课程，一键开始抢课。
- **Web 管理端**：导入课程目录、在线爬取/导入课程抢课数据、手动添加课程、查看日志。
- **课程目录驱动**：以开课信息一览表 CSV/JSON 作为课程库基础，爬取或导入时自动把 `jxb_ids/kch_id` 匹配补全到对应目录条目。
- **用户画像校验**：登录后自动获取用户学院、班级，并在前端显示课程的面向对象/限制对象，自动标红/禁用不符合条件的课程。
- **选课约束校验**：最多 3 门、非网课上课时间不能冲突、非网课不能跨校区。
- **抢课引擎**：多线程轮询提交选课请求，WebSocket 实时推送每门课的成功/失败日志。
- **命令行脚本**：批量登录、课程抓取、Cookie 检测。
- **Tkinter GUI**：本地图形界面抢课主程序。

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
│   └── web_login.py         # 基于 Requests + RSA + 验证码的登录
│
├── scripts/                 # 命令行入口
│   ├── batch_login.py       # 批量登录脚本
│   ├── get_course_json.py   # 课程数据抓取脚本
│   └── cookie_test.py       # Cookie 有效性检测脚本
│
├── gui/                     # 图形界面
│   └── final_spider_GUI.py  # 本地 Tkinter 抢课主界面
│
├── web_server.py            # FastAPI Web 服务后端
├── run_web.py               # Web 服务启动器（支持 ngrok）
│
├── static/                  # Web 前端页面
│   ├── user.html            # 用户端：登录、筛选、抢课
│   └── admin.html           # 管理端：登录、目录导入、课程爬取、日志
│
├── drivers/                 # WebDriver 二进制
│   └── chromedriver.exe     # 不提交到 Git
│
└── data/                    # 运行时数据
    ├── 2026-2027-1-通识选修课.json  # 课程目录（示例）
    ├── courses_export.json          # 课程库导出
    ├── db_users_v7.json             # 用户/Cookie 数据库
    ├── db_courses_v7.json           # 课程抢课数据
    └── app.log                      # 运行日志
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

编辑 `.env`：

```dotenv
BASE_URL=http://210.38.162.118
SINGLE_USERNAME=231000001
SINGLE_PASSWORD=your_password
WEB_ADMIN_TOKEN=your_strong_token_here
LOGIN_WORKERS=3
GRAB_DELAY_MIN=0.8
GRAB_DELAY_MAX=2.0
GRAB_TIMEOUT=5
```

> `WEB_ADMIN_TOKEN` 用于管理端登录，请设置复杂字符串；未设置时回退到 `COOKIE_KEY`，仍为空则默认 `admin`。

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

> `.env`、`accounts.json` 以及 `data/` 下的运行时数据已加入 `.gitignore`，不会被提交到 Git。

---

## 使用流程

### 推荐：Web 控制台

Web 控制台分为用户端和管理端，适合多人临时共用一台电脑抢课，也支持通过 ngrok 暴露公网入口。

#### 启动服务

```bash
# 本地启动
python run_web.py

# 带 ngrok 公网隧道启动
python run_web.py --ngrok --open
```

#### 管理端（先配置课程库）

1. 访问 `http://localhost:8000/admin`。
2. 输入管理口令（`.env` 中的 `WEB_ADMIN_TOKEN`）。
3. 使用一个能登录教务系统的学号密码登录（用于爬取课程）。
4. **导入课程目录**：把开课信息一览表 CSV 导出为 JSON（如 `data/2026-2027-1-通识选修课.json`），在管理端选择并导入。
5. **补充抢课数据**：
   - 方式 A：点击“开始爬取课程”，使用已登录的管理员账号自动爬取全校课程数据。
   - 方式 B：从 `data/` 下的 JSON 文件导入已有的抢课数据。
   - 方式 C：手动添加课程（粘贴选课请求的 cURL 或 form data）。
   系统会自动把 `kch_id/jxb_ids/data` 匹配到目录条目上；未匹配到的数据不会显示在用户端。
6. 查看“当前课程列表”确认数据已更新。

#### 用户端（抢课）

1. 访问 `http://localhost:8000`。
2. 输入学生账号、密码、验证码登录。
3. 页面会自动获取学院/班级，并显示课程的面向对象、限制对象。
4. 使用筛选条件查找课程，勾选符合要求的课程。
5. 点击“开始抢课”，实时查看日志。

> 用户端不会保存密码，只保存登录 Cookie。使用 ngrok 时请确保管理口令足够复杂，并仅分享给可信人员。

---

### 命令行脚本

| 脚本 | 作用 |
|------|------|
| `python scripts/batch_login.py` | 读取 `accounts.json`，并发登录，保存 Cookie 到用户数据库 |
| `python scripts/get_course_json.py` | 单账号登录，自动查询并提取课程表单参数，保存到 `data/courses_export.json` |
| `python scripts/cookie_test.py` | 单个或批量检测 Cookie 存活状态 |

---

### Tkinter GUI

本地图形界面抢课主程序：

```bash
python gui/final_spider_GUI.py
```

---

## 核心模块说明

| 文件 | 作用 |
|------|------|
| `course_grabber/config.py` | 全局配置读取，支持 `.env` 覆盖 |
| `course_grabber/grab_engine.py` | 抢课核心引擎，管理多线程任务队列与状态回调 |
| `course_grabber/web_login.py` | 基于 Requests + RSA + 验证码的登录模块 |
| `course_grabber/data_manager.py` | 课程/用户 JSON 数据管理 |
| `course_grabber/crypto_helper.py` | Cookie 加解密工具 |
| `course_grabber/utils.py` | WebDriver 初始化、登录、Cookie 转换、JSON 读写等公共函数 |
| `web_server.py` | FastAPI Web 服务后端（验证码/登录/课程库/抢课/WebSocket 日志） |
| `run_web.py` | Web 服务启动器，支持本地运行和 ngrok 公网隧道 |

---

## 目录与课程数据说明

- **课程目录**（如 `data/2026-2027-1-通识选修课.json`）是用户端显示和筛选的唯一来源。
- **抢课数据**（`data/db_courses_v7.json`、`data/courses_export.json`）只用于补充目录条目的 `kch_id/jxb_ids/data`。
- 服务启动时只加载目录；`db_courses_v7.json` 不会自动混入用户端列表。
- 含“超星平台”关键字的课程会自动归为“网课”类别。

---

## 选课规则

用户端点击“开始抢课”时会校验：

1. 最多只能选择 3 门课程。
2. 非网课的课程上课时间不能冲突。
3. 非网课的课程不能跨校区。
4. 网课不参与时间冲突和跨校区校验。

---

## 安全提醒

- 账号密码仅保存在本地 `.env` 和 `accounts.json` 中，不要上传到公开仓库。
- Cookie 包含登录会话信息，建议开启 `ENCRYPT_COOKIES=true` 并妥善保管 `COOKIE_KEY`。
- 不要将 `.env`、`accounts.json`、`data/db_users_v7.json` 等文件发送给他人。
- Web 管理端口令请勿使用默认值，公网暴露时请设置强口令。

---

## 免责声明

本项目仅用于学习和研究教务系统交互原理，请遵守学校相关规定，合理使用，不要对教务服务器造成过大压力。因使用不当造成的任何后果由使用者自行承担。
