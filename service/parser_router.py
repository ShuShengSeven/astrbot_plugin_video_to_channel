"""解析器路由：负责把消息文本匹配到对应平台解析器，并维护解析器实例生命周期。

设计参考 astrbot_plugin_parser（MIT）的 main.py 注册逻辑，并抽象成独立服务，
便于未来增加更多平台/解析策略。
"""
from __future__ import annotations

import re

from astrbot.api import logger

from ..core.config import PluginConfig
from ..core.download import Downloader
from ..core.parsers import BaseParser, BilibiliParser, DouyinParser, KuaiShouParser

# 参与注册的解析器类；未来新增平台在此追加即可
_PARSER_CLASSES: tuple[type[BaseParser], ...] = (BilibiliParser, DouyinParser, KuaiShouParser)


class ParserRouter:
    """管理解析器实例与「关键词 + 正则」匹配表。"""

    def __init__(self, cfg: PluginConfig, downloader: Downloader):
        self.cfg = cfg
        self.downloader = downloader
        self.parser_map: dict[str, BaseParser] = {}
        # (关键词, 已编译正则, 解析器实例)：解析器随表项一起保存，
        # 避免两个平台使用同名关键词时 parser_map 被互相覆盖。
        self.patterns: list[tuple[str, re.Pattern[str], BaseParser]] = []

    async def initialize(self) -> None:
        """按配置重建匹配表（插件加载/热重载时调用）。

        两条硬要求：
        1. 先建后换：绝不在构建前清空正在工作的表。旧实现先 clear()，
           任何一个平台构造失败都会让 patterns 永久为空 —— 整个插件对所有消息哑火。
        2. 单平台故障隔离：某个平台解析器构造失败只跳过该平台，其余照常可用。
        """
        enabled = set(self.cfg.parser.enabled_platforms())
        new_map: dict[str, BaseParser] = {}
        new_patterns: list[tuple[str, re.Pattern[str], BaseParser]] = []
        ready: list[str] = []
        failed: list[str] = []

        for cls in _PARSER_CLASSES:
            platform_name = cls.platform.name
            if platform_name not in enabled:
                logger.debug(f"[parser] 平台未启用: {platform_name}")
                continue
            try:
                parser = cls(self.cfg, self.downloader)
                entries = [
                    (
                        keyword,
                        re.compile(pat) if isinstance(pat, str) else pat,
                        parser,
                    )
                    for keyword, pat in cls._key_patterns  # type: ignore[attr-defined]
                    if keyword not in cls.non_portable_keywords
                ]
            except Exception as e:  # noqa: BLE001
                failed.append(cls.platform.display_name)
                logger.exception(f"[parser] 平台初始化失败，已跳过该平台: {platform_name}")
                continue

            skipped = len(cls._key_patterns) - len(entries)  # type: ignore[attr-defined]
            if skipped:
                logger.debug(
                    f"[parser] {cls.platform.display_name} 跳过 {skipped} 个不可搬运的入口"
                )
            for keyword, _pat, owner in entries:
                new_map[keyword] = owner
            new_patterns.extend(entries)
            ready.append(cls.platform.display_name)
            logger.info(f"[parser] 已启用平台: {cls.platform.display_name}")

        # 长关键词优先匹配，避免短关键词抢占
        new_patterns.sort(key=lambda item: -len(item[0]))

        old_parsers = list(self.parser_map.values())
        self.parser_map, self.patterns = new_map, new_patterns

        if failed:
            logger.error(
                f"[parser] 以下平台初始化失败（其余平台不受影响）：{'、'.join(failed)}"
            )
        if not new_patterns:
            logger.error(
                "[parser] 没有任何可用的解析入口：白名单会话里的链接将不会被搬运，"
                "请检查平台开关与插件依赖"
            )
        else:
            logger.info(f"[parser] 解析器就绪: {'、'.join(ready) or '无'}")

        # 换表之后再关旧会话，避免构建失败时把可用实例也关掉
        await self._close_parsers(old_parsers)

    def match(self, text: str) -> tuple[BaseParser, str, re.Match[str]] | None:
        """在文本中寻找第一个受支持的视频链接。"""
        if not text:
            return None
        for keyword, pattern, parser in self.patterns:
            if keyword not in text:
                continue
            searched = pattern.search(text)
            if searched is None:
                continue
            return parser, keyword, searched
        return None

    async def close(self) -> None:
        """关闭所有解析器持有的网络会话。"""
        parsers = list(self.parser_map.values())
        self.parser_map.clear()
        self.patterns.clear()
        await self._close_parsers(parsers)

    @staticmethod
    async def _close_parsers(parsers) -> None:
        for parser in dict.fromkeys(parsers):  # 去重且保持顺序
            try:
                await parser.close_session()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[parser] 关闭会话失败: {e}")
