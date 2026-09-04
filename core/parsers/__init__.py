"""解析器注册入口。

移植自 astrbot_plugin_parser（MIT）。新增平台时：
1. 将平台目录放到 core/parsers/<platform>/
2. 在此处 import 该平台的 Parser 类
3. 在 core/config.py 的 SUPPORTED_PLATFORMS 与 _PARSER_DEFAULTS 中登记
"""
from .base import BaseParser
from .bilibili import BilibiliParser
from .douyin import DouyinParser
from .kuaishou import KuaiShouParser

__all__ = ["BaseParser", "BilibiliParser", "DouyinParser", "KuaiShouParser"]
