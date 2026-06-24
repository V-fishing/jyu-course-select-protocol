"""抢课任务管理的 Tkinter GUI。"""

from __future__ import annotations

import datetime
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from course_grabber.config import Config
from course_grabber.data_manager import DataManager
from course_grabber.grab_engine import GrabEngine, StatusEvent, TaskStatus
from course_grabber.utils import extract_course_name, logger, parse_curl_data


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("选课系统 v12.0 (重构版)")
        self.root.geometry("1200x850")

        self.db = DataManager(on_change=self._on_data_changed)
        self.engine = GrabEngine(status_callback=self._on_status_event)
        self.engine.start_dispatch()

        # UI 状态
        self.course_vars: dict[str, tk.IntVar] = {}
        self.user_selections: dict[str, set[str]] = {}
        self.current_selected_user: str | None = None
        self.paused = False

        self._build_notebook()
        self._setup_assign_tab()
        self._setup_monitor_tab()
        self._setup_course_tab()
        self._setup_user_tab()
        self._setup_status_bar()

        self.refresh_assign_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # 笔记本 / 标签页
    # ------------------------------------------------------------------
    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_assign = ttk.Frame(self.notebook)
        self.tab_monitor = ttk.Frame(self.notebook)
        self.tab_courses = ttk.Frame(self.notebook)
        self.tab_users = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_assign, text="任务派发")
        self.notebook.add(self.tab_monitor, text="监控中心")
        self.notebook.add(self.tab_courses, text="课程添加")
        self.notebook.add(self.tab_users, text="用户管理")

    # ------------------------------------------------------------------
    # 标签页 1：任务派发
    # ------------------------------------------------------------------
    def _setup_assign_tab(self) -> None:
        paned = ttk.PanedWindow(self.tab_assign, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=5, pady=5)

        # 左侧：用户列表
        frame_left = ttk.LabelFrame(paned, text="第一步：选择用户")
        paned.add(frame_left, weight=1)
        self.user_listbox = tk.Listbox(frame_left, font=("微软雅黑", 10), exportselection=False)
        self.user_listbox.pack(fill="both", expand=True, padx=5, pady=5)
        self.user_listbox.bind("<<ListboxSelect>>", self.on_user_switch)

        # 右侧：带搜索的课程列表
        frame_right = ttk.LabelFrame(paned, text="第二步：勾选课程")
        paned.add(frame_right, weight=3)

        search_frame = ttk.Frame(frame_right)
        search_frame.pack(fill="x", padx=5, pady=5)
        ttk.Label(search_frame, text="搜索:").pack(side="left")
        self.entry_search = ttk.Entry(search_frame)
        self.entry_search.pack(side="left", fill="x", expand=True, padx=5)
        self.entry_search.bind("<KeyRelease>", self.on_search_input)
        ttk.Button(search_frame, text="清空", command=self.clear_search).pack(side="left")

        # 批量选择按钮
        bulk_frame = ttk.Frame(frame_right)
        bulk_frame.pack(fill="x", padx=5)
        ttk.Button(bulk_frame, text="全选", command=self.select_all).pack(side="left", padx=2)
        ttk.Button(bulk_frame, text="反选", command=self.invert_selection).pack(side="left", padx=2)
        ttk.Button(bulk_frame, text="清空", command=self.clear_selection).pack(side="left", padx=2)

        # 可滚动课程列表
        self.canvas = tk.Canvas(frame_right, bg="white")
        self.scrollbar = ttk.Scrollbar(frame_right, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True, padx=5)
        self.scrollbar.pack(side="right", fill="y")

        # 底部启动按钮
        btn_frame = ttk.Frame(self.tab_assign)
        btn_frame.pack(fill="x", pady=10)
        ttk.Button(btn_frame, text="启动选中任务", command=self.launch_tasks).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="刷新列表", command=self.refresh_assign_ui).pack(side="left", padx=5)

    def on_user_switch(self, _event=None) -> None:
        try:
            idx = self.user_listbox.curselection()
            if not idx:
                return
            new_user = self.user_listbox.get(idx[0])
            if new_user == self.current_selected_user:
                return
            self.save_current_user_selection()
            self.load_user_selection(new_user)
            self.current_selected_user = new_user
        except Exception as exc:
            logger.error("切换用户出错: %s", exc)

    def save_current_user_selection(self) -> None:
        if self.current_selected_user:
            selected_ids = {uid for uid, var in self.course_vars.items() if var.get() == 1}
            self.user_selections[self.current_selected_user] = selected_ids

    def load_user_selection(self, user_name: str) -> None:
        saved_ids = self.user_selections.get(user_name, set())
        for uid, var in self.course_vars.items():
            var.set(1 if uid in saved_ids else 0)

    def on_search_input(self, _event=None) -> None:
        keyword = self.entry_search.get().strip()
        self.refresh_assign_ui(keyword)

    def clear_search(self) -> None:
        self.entry_search.delete(0, tk.END)
        self.refresh_assign_ui()

    def select_all(self) -> None:
        for var in self.course_vars.values():
            var.set(1)
        self.save_current_user_selection()

    def invert_selection(self) -> None:
        for var in self.course_vars.values():
            var.set(0 if var.get() else 1)
        self.save_current_user_selection()

    def clear_selection(self) -> None:
        for var in self.course_vars.values():
            var.set(0)
        self.save_current_user_selection()

    def refresh_assign_ui(self, keyword: str | None = None) -> None:
        # 左侧用户列表
        if self.user_listbox.size() != len(self.db.users):
            self.user_listbox.delete(0, tk.END)
            for user in self.db.user_names():
                self.user_listbox.insert(tk.END, user)
                if user not in self.user_selections:
                    self.user_selections[user] = set()

        # 维护 IntVar 池
        db_ids = set(self.db.courses.keys())
        for uid in db_ids:
            if uid not in self.course_vars:
                self.course_vars[uid] = tk.IntVar()
        for uid in list(self.course_vars.keys()):
            if uid not in db_ids:
                del self.course_vars[uid]

        # 渲染右侧课程列表
        for w in self.scrollable_frame.winfo_children():
            w.destroy()

        count_visible = 0
        keyword_lower = (keyword or "").lower()
        for uid, info in self.db.courses.items():
            name = info.get("name", "")
            kch_id = info.get("kch_id", "")
            display = f"【{name}】 (ID: {kch_id})"

            if keyword_lower and keyword_lower not in name.lower() and keyword not in kch_id:
                continue

            chk = ttk.Checkbutton(
                self.scrollable_frame,
                text=display,
                variable=self.course_vars[uid],
                command=self.save_current_user_selection,
            )
            chk.pack(anchor="w", padx=10, pady=2, fill="x")
            count_visible += 1

        if count_visible == 0:
            ttk.Label(self.scrollable_frame, text="无匹配课程", foreground="gray").pack(pady=10)

    def launch_tasks(self) -> None:
        idx = self.user_listbox.curselection()
        if not idx:
            messagebox.showwarning("提示", "请选择用户")
            return

        user = self.user_listbox.get(idx[0])
        cookie = self.db.get_cookie(user)
        if not cookie:
            messagebox.showwarning("提示", f"用户 [{user}] 没有 Cookie")
            return

        self.current_selected_user = user
        self.save_current_user_selection()
        selected_ids = self.user_selections.get(user, set())

        count = 0
        for uid in selected_ids:
            info = self.db.get_course(uid)
            if not info:
                continue
            self.engine.launch(user, cookie, info["name"], info["data"])
            count += 1

        if count > 0:
            self.notebook.select(self.tab_monitor)
            messagebox.showinfo("成功", f"已为 [{user}] 启动 {count} 个任务")
        else:
            messagebox.showwarning("提示", "请至少勾选一门课程")

    # ------------------------------------------------------------------
    # 标签页 2：监控中心
    # ------------------------------------------------------------------
    def _setup_monitor_tab(self) -> None:
        # 统计栏
        stats_frame = ttk.Frame(self.tab_monitor)
        stats_frame.pack(fill="x", padx=5, pady=5)
        self.lbl_stats = ttk.Label(stats_frame, text="总任务: 0 | 运行中: 0 | 成功: 0 | 失败: 0")
        self.lbl_stats.pack(side="left")

        # 控制按钮
        ctrl_frame = ttk.Frame(self.tab_monitor)
        ctrl_frame.pack(fill="x", padx=5)
        ttk.Button(ctrl_frame, text="全部停止", command=self.stop_all_tasks).pack(side="left", padx=2)
        self.btn_pause = ttk.Button(ctrl_frame, text="全部暂停", command=self.toggle_pause)
        self.btn_pause.pack(side="left", padx=2)
        ttk.Button(ctrl_frame, text="清空日志", command=self.clear_log).pack(side="left", padx=2)

        # 树形表格
        columns = ("id", "user", "course", "status")
        self.tree = ttk.Treeview(self.tab_monitor, columns=columns, show="headings", height=12)
        self.tree.heading("user", text="用户")
        self.tree.heading("course", text="课程")
        self.tree.heading("status", text="状态")
        self.tree.column("id", width=0, stretch=False)
        self.tree.pack(fill="x", padx=5, pady=5)

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="停止任务", command=self.stop_task)
        self.tree.bind("<Button-3>", lambda e: menu.post(e.x_root, e.y_root))

        # 日志区域
        self.log_area = scrolledtext.ScrolledText(self.tab_monitor, height=14)
        self.log_area.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_area.tag_config("SUCCESS", foreground="green")
        self.log_area.tag_config("ERROR", foreground="red")
        self.log_area.tag_config("WARNING", foreground="orange")
        self.log_area.tag_config("INFO", foreground="black")

        # 日志过滤
        filter_frame = ttk.Frame(self.tab_monitor)
        filter_frame.pack(fill="x", padx=5)
        ttk.Label(filter_frame, text="日志过滤:").pack(side="left")
        self.log_filter = ttk.Combobox(filter_frame, values=["全部", "INFO", "SUCCESS", "ERROR"], state="readonly")
        self.log_filter.set("全部")
        self.log_filter.pack(side="left", padx=5)

    def _on_status_event(self, event: StatusEvent) -> None:
        self.root.after(0, self._apply_status_event, event)

    def _apply_status_event(self, event: StatusEvent) -> None:
        # 更新树形表格
        exist_item = None
        for item in self.tree.get_children():
            if self.tree.item(item, "values")[0] == event.task_id:
                exist_item = item
                break
        if exist_item:
            self.tree.set(exist_item, "status", event.status)
        else:
            self.tree.insert("", "end", values=(event.task_id, event.user_name, event.course_name, event.status))

        # 记录有意义的事件到日志
        if event.status in (TaskStatus.SUCCESS, TaskStatus.COOKIE_EXPIRED, TaskStatus.VALIDATION_ERROR,
                            TaskStatus.FINGERPRINT_ERROR, TaskStatus.COURSE_LIMIT, TaskStatus.CREDIT_LIMIT):
            self.log(f"[{event.user_name}] {event.status}: {event.course_name}")

        self._update_stats()

    def _update_stats(self) -> None:
        total = len(self.tree.get_children())
        running = sum(1 for item in self.tree.get_children() if "成功" not in str(self.tree.item(item, "values")[3]))
        success = sum(1 for item in self.tree.get_children() if "成功" in str(self.tree.item(item, "values")[3]))
        failed = sum(1 for item in self.tree.get_children() if any(k in str(self.tree.item(item, "values")[3]) for k in ("失效", "失败", "超限")))
        self.lbl_stats.config(text=f"总任务: {total} | 运行中: {running} | 成功: {success} | 失败: {failed}")

    def stop_task(self) -> None:
        sel = self.tree.selection()
        if sel:
            tid = self.tree.item(sel[0], "values")[0]
            self.engine.stop(tid)
            self.tree.delete(sel[0])
            self._update_stats()

    def stop_all_tasks(self) -> None:
        self.engine.stop_all()
        for item in self.tree.get_children():
            self.tree.set(item, "status", TaskStatus.STOPPED)
        self.log("全部任务已停止")
        self._update_stats()

    def toggle_pause(self) -> None:
        if self.paused:
            self.engine.resume_all()
            self.paused = False
            self.btn_pause.config(text="全部暂停")
            self.log("全部任务已恢复")
        else:
            self.engine.pause_all()
            self.paused = True
            self.btn_pause.config(text="全部恢复")
            self.log("全部任务已暂停")

    def clear_log(self) -> None:
        self.log_area.delete("1.0", tk.END)

    def log(self, msg: str) -> None:
        def _update():
            level = "INFO"
            if "成功" in msg:
                level = "SUCCESS"
            elif any(k in msg for k in ("失败", "失效", "错误")):
                level = "ERROR"
            elif "暂停" in msg or "恢复" in msg:
                level = "WARNING"

            selected_filter = self.log_filter.get()
            if selected_filter != "全部" and selected_filter != level:
                return

            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_area.insert(tk.END, f"[{ts}] {msg}\n", level)
            self.log_area.see(tk.END)

        self.root.after(0, _update)

    # ------------------------------------------------------------------
    # 标签页 3：课程添加
    # ------------------------------------------------------------------
    def _setup_course_tab(self) -> None:
        frame = ttk.Frame(self.tab_courses)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(frame, text="粘贴 cURL (支持同课程不同班级):").pack(anchor="w")
        self.txt_curl = scrolledtext.ScrolledText(frame, height=5)
        self.txt_curl.pack(fill="x")
        ttk.Button(frame, text="解析添加", command=self.add_curl).pack(pady=5)

        self.course_tree = ttk.Treeview(frame, columns=("uid", "kid", "name"), show="headings")
        self.course_tree.heading("kid", text="课程ID")
        self.course_tree.heading("name", text="课程备注 (班级)")
        self.course_tree.column("uid", width=0, stretch=False)
        self.course_tree.pack(fill="both", expand=True)

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="删除", command=self.del_course)
        self.course_tree.bind("<Button-3>", lambda e: menu.post(e.x_root, e.y_root))
        self.refresh_course_ui()

    def add_curl(self) -> None:
        text = self.txt_curl.get("1.0", tk.END).strip()
        if not text:
            return

        data = parse_curl_data(text)
        if not data:
            messagebox.showerror("失败", "无法解析 Data")
            return

        kid_match = re.search(r"kch_id=([a-zA-Z0-9]+)", data)
        kid = kid_match.group(1) if kid_match else "Unknown"
        default_name = extract_course_name(data) or f"课程_{kid}"

        exist_name = self.db.find_duplicate_data(data)
        if exist_name:
            messagebox.showinfo("提示", f"这个班级已经添加过了！\n名称：{exist_name}")
            return

        name = simpledialog.askstring("新班级", f"检测到新班级 (ID:{kid})\n请给它起个名:", initialvalue=default_name)
        if name:
            self.db.add_course(kid, name, data)
            self.refresh_course_ui()
            current_search = self.entry_search.get().strip()
            self.refresh_assign_ui(current_search)
            self.txt_curl.delete("1.0", tk.END)
            messagebox.showinfo("成功", "课程/班级已存入字典")

    def refresh_course_ui(self) -> None:
        for i in self.course_tree.get_children():
            self.course_tree.delete(i)
        for uid, info in self.db.courses.items():
            self.course_tree.insert("", "end", values=(uid, info["kch_id"], info["name"]))

    def del_course(self) -> None:
        sel = self.course_tree.selection()
        if sel:
            uid = self.course_tree.item(sel[0], "values")[0]
            self.db.delete_course(uid)
            self.refresh_course_ui()
            for selections in self.user_selections.values():
                selections.discard(uid)
            self.refresh_assign_ui(self.entry_search.get().strip())

    # ------------------------------------------------------------------
    # 标签页 4：用户管理
    # ------------------------------------------------------------------
    def _setup_user_tab(self) -> None:
        input_frame = ttk.LabelFrame(self.tab_users, text="添加/更新用户")
        input_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(input_frame, text="用户备注:").grid(row=0, column=0, padx=5, pady=10, sticky="e")
        self.e_u = ttk.Entry(input_frame, width=20)
        self.e_u.grid(row=0, column=1, padx=5, pady=10, sticky="w")

        ttk.Label(input_frame, text="Cookie:").grid(row=0, column=2, padx=5, pady=10, sticky="e")
        self.e_c = ttk.Entry(input_frame, width=60)
        self.e_c.grid(row=0, column=3, padx=5, pady=10, sticky="w")

        ttk.Button(input_frame, text="保存用户", command=self.add_u).grid(row=0, column=4, padx=10, pady=10)

        tree_frame = ttk.Frame(self.tab_users)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("n", "c")
        self.u_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=15)
        self.u_tree.heading("n", text="用户备注")
        self.u_tree.heading("c", text="Cookie (前60位预览)")
        self.u_tree.column("n", width=150, anchor="center")
        self.u_tree.column("c", width=700, anchor="w")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.u_tree.yview)
        self.u_tree.configure(yscrollcommand=scrollbar.set)

        self.u_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        user_menu = tk.Menu(self.root, tearoff=0)
        user_menu.add_command(label="删除用户", command=self.del_u)
        self.u_tree.bind("<Button-3>", lambda e: user_menu.post(e.x_root, e.y_root))

        self.refresh_user_ui()

    def add_u(self) -> None:
        n = self.e_u.get().strip()
        c = self.e_c.get().strip()
        if n and c:
            self.db.add_user(n, c)
            self.refresh_user_ui()
            self.refresh_assign_ui()
            self.e_u.delete(0, tk.END)
            self.e_c.delete(0, tk.END)
            messagebox.showinfo("成功", f"用户 [{n}] 已保存")

    def refresh_user_ui(self) -> None:
        for i in self.u_tree.get_children():
            self.u_tree.delete(i)
        for n, c in self.db.users.items():
            preview = c[:60] + "..." if len(c) > 60 else c
            self.u_tree.insert("", "end", values=(n, preview))

    def del_u(self) -> None:
        sel = self.u_tree.selection()
        if sel:
            n = self.u_tree.item(sel[0], "values")[0]
            if messagebox.askyesno("确认", f"删除用户 {n}?"):
                self.db.delete_user(n)
                if n in self.user_selections:
                    del self.user_selections[n]
                self.refresh_user_ui()
                self.refresh_assign_ui()

    # ------------------------------------------------------------------
    # 状态栏与生命周期
    # ------------------------------------------------------------------
    def _setup_status_bar(self) -> None:
        self.status_bar = ttk.Label(self.root, text="就绪", relief=tk.SUNKEN, anchor="w")
        self.status_bar.pack(side="bottom", fill="x")

    def _on_data_changed(self) -> None:
        self.status_bar.config(text=f"数据已更新 | 课程 {len(self.db.courses)} | 用户 {len(self.db.users)}")

    def _on_close(self) -> None:
        self.engine.shutdown()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
