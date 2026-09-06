"""轻量防抖：同一链接在窗口期内只处理一次（全局去重）。

记录数有上限（默认 1024），优先淘汰已过期记录，仍超限时淘汰最旧记录，
避免长时间/高并发运行导致内存无界增长。
"""
from __future__ import annotations

import time
from collections import OrderedDict


class Debouncer:
    def __init__(self, window_seconds: int, max_records: int = 1024):
        self._window = max(0, int(window_seconds))
        self._max_records = max(1, int(max_records))
        self._records: OrderedDict[str, float] = OrderedDict()

    def hit(self, link: str) -> bool:
        """若命中防抖窗口返回 True，否则记录并返回 False。

        注意：必须用 None 区分「没见过」和「见过但已过期」。用 0.0 兜底会让
        从未出现过的链接在 time.monotonic() < window 时（容器/WSL 刚启动、
        debounce_seconds 配得较大）被误判为“刚刚处理过”，进而被静默跳过，
        并且 move_to_end 会因键不存在直接抛 KeyError。
        """
        if self._window <= 0:
            return False
        now = time.monotonic()
        last = self._records.get(link)
        if last is not None and now - last < self._window:
            self._records.move_to_end(link)
            return True
        self._records[link] = now
        self._records.move_to_end(link)
        self._prune(now)
        return False

    def peek(self, link: str) -> bool:
        """只查询是否会命中防抖，不写入记录。用于消息入口避免给出误导性的“开始解析”回执。"""
        if self._window <= 0:
            return False
        last = self._records.get(link)
        return last is not None and time.monotonic() - last < self._window

    def forget(self, link: str) -> None:
        """撤销一次防抖记录。

        用于「还没有把内容提交给频道就已经失败」的场景（解析失败、配置缺失、
        下载失败等）：此时应当允许用户立刻重试同一条链接，而不是让防抖窗口
        把合理的重试一起吞掉。
        """
        self._records.pop(link, None)

    def _prune(self, now: float) -> None:
        """控制记录数量：先清过期，仍超限则淘汰最旧记录。"""
        if len(self._records) <= self._max_records:
            return
        expired = [
            k for k, t in self._records.items() if now - t >= self._window
        ]
        for k in expired:
            self._records.pop(k, None)
        while len(self._records) > self._max_records:
            self._records.popitem(last=False)
