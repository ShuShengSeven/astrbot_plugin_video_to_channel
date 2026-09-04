"""解析器路由：负责把消息文本匹配到对应平台解析器，并维护解析器实例生命周期。

设计参考 astrbot_plugin_parser（MIT）的 main.py 注册逻辑，并抽象成独立服务，
便于未来增加更多平台/解析策略。
"""
from __future__ import annotations

import re

from astrbot.api import logger

from ..core.config import PluginConfig
from ..core.download import Downloader
from ..core.parsers import BaseParser, BilibiliParser, DouyinParser

# 参与注册的解析器类；未来新增平台在此追加即可
_PARSER_CLASSES: tuple[type[BaseParser], ...] = (BilibiliParser, DouyinParser)


class ParserRouter:
    """管理解析器实例与「关键词 + 正则」匹配表。"""

    def __init__(self, cfg: PluginConfig, downloader: Downloader):
        self.cfg = cfg
        self.downloader = downloader
        self.parser_map: dict[str, BaseParser] = {}
        self.patterns: list[tuple[str, re.Pattern[str]]] = []

    def initialize(self) -> None:
        """按配置启用平台并构建匹配表（插件加载/重载时调用）。"""
        self.parser_map.clear()
        self.patterns.clear()

        enabled = set(self.cfg.parser.enabled_platforms())
        for cls in _PARSER_CLASSES:
            platform_name = cls.platform.name
            if platform_name not in enabled:
                logger.debug(f"[parser] 平台未启用: {platform_name}")
                continue
            parser = cls(self.cfg, self.downloader)
            for keyword, _ in cls._key_patterns:  # type: ignore[attr-defined]
                self.parser_map[keyword] = parser
            logger.info(f"[parser] 已启用平台: {cls.platform.display_name}")

        patterns: list[tuple[str, re.Pattern[str]]] = []
        for cls in _PARSER_CLASSES:
            if cls.platform.name not in enabled:
                continue
            for keyword, pat in cls._key_patterns:  # type: ignore[attr-defined]
                patterns.append(
                    (keyword, re.compile(pat) if isinstance(pat, str) else pat)
                )

        # 长关键词优先匹配，避免短关键词抢占
        patterns.sort(key=lambda item: -len(item[0]))
        self.patterns = patterns

        enabled_names = "、".join(
            cls.platform.display_name for cls in _PARSER_CLASSES
            if cls.platform.name in enabled
        )
        logger.info(f"[parser] 解析器就绪: {enabled_names or '无'}")

    def match(self, text: str) -> tuple[BaseParser, str, re.Match[str]] | None:
        """在文本中寻找第一个受支持的视频链接。"""
        if not text:
            return None
        for keyword, pattern in self.patterns:
            if keyword not in text:
                continue
            searched = pattern.search(text)
            if searched is None:
                continue
            return self.parser_map[keyword], keyword, searched
        return None

    async def close(self) -> None:
        """关闭所有解析器持有的网络会话。"""
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            try:
                await parser.close_session()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[parser] 关闭会话失败: {e}")
        self.parser_map.clear()
        self.patterns.clear()
