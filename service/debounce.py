"""轻量防抖：同一会话内同一链接在窗口期内只处理一次，避免刷屏重复上传。"""
from __future__ import annotations

import time


class Debouncer:
    def __init__(self, window_seconds: int):
        self._window = max(0, int(window_seconds))
        self._records: dict[tuple[str, str], float] = {}

    def hit(self, session_id: str, link: str) -> bool:
        """若命中防抖窗口返回 True，否则记录并返回 False。"""
        if self._window <= 0:
            return False
        key = (session_id, link)
        now = time.monotonic()
        last = self._records.get(key, 0.0)
        if now - last < self._window:
            return True
        self._records[key] = now
        # 简单清理过期记录，避免无限增长
        if len(self._records) > 512:
            expired = [k for k, t in self._records.items() if now - t >= self._window]
            for k in expired:
                self._records.pop(k, None)
        return False
