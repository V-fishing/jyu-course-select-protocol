"""课程与用户的持久化数据管理。"""

from __future__ import annotations

import uuid
from typing import Callable

from course_grabber.config import Config
from course_grabber.crypto_helper import CookieCrypto
from course_grabber.utils import load_json, save_json


class DataManager:
    """管理 courses_export.json 和 db_users_v7.json，支持可选的 Cookie 加密。"""

    def __init__(
        self,
        course_file: str | None = None,
        user_file: str | None = None,
        crypto: CookieCrypto | None = None,
        on_change: Callable | None = None,
    ):
        self.course_file = course_file or Config.COURSE_DB_FILE
        self.user_file = user_file or Config.USER_DB_FILE
        self.crypto = crypto or CookieCrypto()
        self.on_change = on_change

        self.courses: dict[str, dict] = {}
        self.users: dict[str, str] = {}
        # data -> uid，用于 O(1) 重复检测
        self._data_hash_index: dict[str, str] = {}

        self.load_all()

    def load_all(self) -> None:
        self.courses = load_json(self.course_file, default={})
        raw_users = load_json(self.user_file, default={})

        self.users = {}
        for name, cookie in raw_users.items():
            self.users[name] = self.crypto.decrypt(cookie)

        self._rebuild_hash_index()

    def _rebuild_hash_index(self) -> None:
        self._data_hash_index = {}
        for uid, info in self.courses.items():
            data = info.get("data", "")
            if data:
                self._data_hash_index[data] = uid

    def _persist_courses(self) -> None:
        save_json(self.course_file, self.courses)
        self._rebuild_hash_index()

    def _persist_users(self) -> None:
        out = {}
        for name, cookie in self.users.items():
            out[name] = self.crypto.encrypt(cookie)
        save_json(self.user_file, out)

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    # ---------------- 课程 ----------------

    def add_course(self, kch_id: str, name: str, data: str) -> str | None:
        """添加课程。成功返回 uid，重复数据返回 None。"""
        if not data or data in self._data_hash_index:
            return None

        uid = str(uuid.uuid4())
        self.courses[uid] = {"kch_id": kch_id, "name": name, "data": data}
        self._persist_courses()
        self._notify()
        return uid

    def update_course(self, uid: str, kch_id: str | None = None, name: str | None = None, data: str | None = None) -> bool:
        if uid not in self.courses:
            return False
        info = self.courses[uid]
        if kch_id is not None:
            info["kch_id"] = kch_id
        if name is not None:
            info["name"] = name
        if data is not None and data != info.get("data"):
            if data in self._data_hash_index:
                return False
            info["data"] = data
        self._persist_courses()
        self._notify()
        return True

    def delete_course(self, uid: str) -> bool:
        if uid not in self.courses:
            return False
        del self.courses[uid]
        self._persist_courses()
        self._notify()
        return True

    def get_course(self, uid: str) -> dict | None:
        return self.courses.get(uid)

    def find_duplicate_data(self, data: str) -> str | None:
        """如果存在相同 data 的课程，返回其名称。"""
        uid = self._data_hash_index.get(data)
        if uid:
            return self.courses[uid].get("name")
        return None

    # ---------------- 用户 ----------------

    def add_user(self, name: str, cookie: str) -> bool:
        if not name or not cookie:
            return False
        self.users[name] = cookie
        self._persist_users()
        self._notify()
        return True

    def delete_user(self, name: str) -> bool:
        if name not in self.users:
            return False
        del self.users[name]
        self._persist_users()
        self._notify()
        return True

    def get_cookie(self, name: str) -> str | None:
        return self.users.get(name)

    def user_names(self) -> list[str]:
        return list(self.users.keys())

    # ---------------- 导入 / 导出 ----------------

    def import_courses(self, courses_dict: dict[str, dict]) -> int:
        """导入课程字典，跳过重复项，返回新增数量。"""
        added = 0
        for uid, info in courses_dict.items():
            data = info.get("data", "")
            if not data or data in self._data_hash_index:
                continue

            # 处理 uid 冲突：若已存在且 data 不同，先移除旧索引
            if uid in self.courses:
                old_data = self.courses[uid].get("data")
                if old_data and old_data in self._data_hash_index:
                    del self._data_hash_index[old_data]

            self.courses[uid] = {
                "kch_id": info.get("kch_id", ""),
                "name": info.get("name", ""),
                "data": data,
            }
            self._data_hash_index[data] = uid
            added += 1

        if added:
            self._persist_courses()
            self._notify()
        return added

    def all_courses(self) -> dict[str, dict]:
        return self.courses.copy()

    def all_users(self) -> dict[str, str]:
        return self.users.copy()


if __name__ == "__main__":
    dm = DataManager()
    print("Loaded courses:", len(dm.courses))
    print("Loaded users:", len(dm.users))

    test_uid = dm.add_course("12345", "测试课程", "jxb_ids=abc&kch_id=12345")
    print("Added:", test_uid)
    print("Duplicate check:", dm.find_duplicate_data("jxb_ids=abc&kch_id=12345"))
    dm.delete_course(test_uid)
    print("Deleted test course")
