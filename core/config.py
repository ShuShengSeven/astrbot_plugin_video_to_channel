"""插件配置封装。

移植自 astrbot_plugin_parser（MIT）的配置思想，但针对“搬运到腾讯频道”场景做了精简：
- 顶层配置直接读 AstrBot 的 AstrBotConfig（对应 _conf_schema.json）
- 为每个解析器提供统一的 ParserItem 接口，后续新增平台只需扩展 _conf_schema.json 与 ParserConfig
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 本插件目前支持的解析器平台；新增平台时在此登记
SUPPORTED_PLATFORMS: tuple[str, ...] = ("bilibili", "douyin")

# 每个平台的默认配置，避免解析器访问缺省字段时抛异常
_PARSER_DEFAULTS: dict[str, dict[str, Any]] = {
    "bilibili": {
        "enable": True,
        "use_proxy": False,
        "cookies": "",
        "video_quality": "_720P",
        "video_codec_list": ["AVC"],
    },
    "douyin": {
        "enable": True,
        "use_proxy": False,
        "cookies": "",
    },
}


class ParserItem:
    """单个平台的配置对象，行为类似 dict，但支持属性访问。

    CookieJar / 解析器会访问 item.name、item.cookies、item.use_proxy 等。
    """

    def __init__(self, name: str, data: dict[str, Any] | None = None):
        self.name = name
        self._data = data or {}

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if key in self._data:
            return self._data[key]
        defaults = _PARSER_DEFAULTS.get(self.name, {})
        if key in defaults:
            return defaults[key]
        # 可选字段：解析器通常以 `xxx or [...]` 方式兜底
        return None

    def __repr__(self) -> str:
        return f"ParserItem({self.name!r}, {self._data!r})"


class ParserConfig:
    """按平台名称访问的解析器配置集合。"""

    def __init__(self, raw: dict[str, Any] | None = None):
        raw = raw or {}
        self._items: dict[str, ParserItem] = {
            name: ParserItem(name, raw.get(name) or {}) for name in SUPPORTED_PLATFORMS
        }

    def __getattr__(self, name: str) -> ParserItem:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._items:
            # 新平台尚未在 SUPPORTED_PLATFORMS 登记时先给一个空配置，方便渐进开发
            logger.warning(f"[config] 平台 {name} 未在 SUPPORTED_PLATFORMS 中登记，使用默认配置")
            self._items[name] = ParserItem(name, {})
        return self._items[name]

    def platforms(self) -> list[str]:
        return list(self._items.keys())

    def enabled_platforms(self) -> list[str]:
        return [name for name, item in self._items.items() if getattr(item, "enable", True)]


class PluginConfig:
    """统一配置入口：把 AstrBot 面板配置转成解析器/下载器/上传器需要的强类型字段。"""

    def __init__(self, config: AstrBotConfig, plugin_name: str = "astrbot_plugin_video_to_channel"):
        self._raw = config

        # ---------- 会话与触发 ----------
        self.session_whitelist: list[str] = list(config.get("session_whitelist") or [])
        self.debounce_seconds: int = int(config.get("debounce_seconds") or 120)

        # ---------- 上传目标 ----------
        self.target_guild_id: str = str(config.get("target_guild_id") or "").strip()
        self.target_channel_id: str = str(config.get("target_channel_id") or "").strip()

        # ---------- tencent-channel-cli ----------
        # cli_command 语义：auto/空 = 插件自动下载并托管二进制；其他值 = 外部命令/绝对路径
        raw_cli_command: str = str(config.get("cli_command") or "").strip()
        self.cli_command: str = raw_cli_command or "auto"
        self.cli_managed: bool = raw_cli_command in ("", "auto")
        self.cli_timeout: int = int(config.get("cli_timeout") or 600)
        self.max_concurrent: int = int(config.get("max_concurrent") or 2)

        # ---------- 下载 ----------
        download = config.get("download") or {}
        self.source_max_size: int = int(download.get("max_size_mb") or 90)  # MB，Downloader 读取
        self.source_max_minute: int = int(download.get("max_minutes") or 15)
        self.download_timeout: int = int(download.get("download_timeout") or 280)
        self.download_retry_times: int = int(download.get("download_retry_times") or 2)
        self.common_timeout: int = int(download.get("common_timeout") or 15)
        proxy = str(download.get("proxy") or "")
        self.proxy: str | None = proxy or None

        # 派生限制
        self.max_duration: int = self.source_max_minute * 60  # 秒
        self.max_size: int = self.source_max_size * 1024 * 1024  # 字节

        # ---------- 解析器 ----------
        self.parser = ParserConfig(config.get("parsers") or {})

        # ---------- 数据目录（遵循 AstrBot 官方存储规范） ----------
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.cookie_dir = self.data_dir / "cookies"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

    def update_target(self, guild_id: str, channel_id: str) -> None:
        """更新上传目标并持久化到 AstrBot 配置。"""
        self.target_guild_id = str(guild_id or "").strip()
        self.target_channel_id = str(channel_id or "").strip()
        self._raw["target_guild_id"] = self.target_guild_id
        self._raw["target_channel_id"] = self.target_channel_id
        save = getattr(self._raw, "save_config", None)
        if callable(save):
            save()
