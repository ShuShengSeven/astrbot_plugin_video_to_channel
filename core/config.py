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
SUPPORTED_PLATFORMS: tuple[str, ...] = ("bilibili", "douyin", "kuaishou")

# 每个平台的默认配置，避免解析器访问缺省字段时抛异常
#
# use_proxy 决定「download.proxy」是否作用于该平台的解析与下载请求。
# 默认保持 False：代理此前对解析/下载不生效，改成默认开启会改变现网用户的网络路径
# （B站对海外出口常返回 -352/-403），因此这里只补“可用性 + 可见性”，不改默认行为。
# 需要代理的用户必须在面板里按平台显式勾选。
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
    "kuaishou": {
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

    @staticmethod
    def _as_int(raw: Any, default: int, minimum: int, field: str) -> int:
        """把配置值转成整数并夹到合法下限。

        注意：不能写成 ``int(raw or default)``——那样用户显式填写的 0
        （例如 debounce_seconds=0 表示关闭防抖、download_retry_times=0 表示不重试）
        会被当成“未填写”而悄悄换成默认值。
        """
        try:
            value = int(raw)
        except (TypeError, ValueError):
            if raw is None or raw == "":
                return default
            logger.warning(
                f"[config] {field}={raw!r} 不是合法整数，使用默认值 {default}"
            )
            return default
        if value < minimum:
            logger.warning(
                f"[config] {field}={value} 小于允许的最小值 {minimum}，已按 {minimum} 处理"
            )
            return minimum
        return value

    @staticmethod
    def _as_str_list(raw: Any, field: str) -> list[str]:
        """容错读取列表配置：手工编辑配置时写成字符串/元组也很常见。"""
        if raw is None:
            return []
        if isinstance(raw, str):
            logger.warning(
                f"[config] {field} 应为列表，但配置成了字符串 {raw!r}，已按单条处理"
            )
            raw = [raw]
        try:
            items = list(raw)
        except TypeError:
            logger.warning(f"[config] {field}={raw!r} 无法解析为列表，已忽略")
            return []
        return [str(item).strip() for item in items if str(item).strip()]

    def __init__(self, config: AstrBotConfig, plugin_name: str = "astrbot_plugin_video_to_channel"):
        self._raw = config

        # ---------- 会话与触发 ----------
        self.session_whitelist: list[str] = self._as_str_list(
            config.get("session_whitelist"), "session_whitelist"
        )
        # 0 是合法值：表示关闭防抖
        self.debounce_seconds: int = self._as_int(
            config.get("debounce_seconds"), 120, 0, "debounce_seconds"
        )

        # ---------- 上传目标 ----------
        self.target_guild_id: str = str(config.get("target_guild_id") or "").strip()
        self.target_channel_id: str = str(config.get("target_channel_id") or "").strip()

        # ---------- tencent-channel-cli ----------
        # cli_command 语义：auto/空 = 插件自动下载并托管二进制；其他值 = 外部命令/绝对路径
        raw_cli_command: str = str(config.get("cli_command") or "").strip()
        self.cli_command: str = raw_cli_command or "auto"
        self.cli_managed: bool = raw_cli_command in ("", "auto")
        self.cli_timeout: int = self._as_int(
            config.get("cli_timeout"), 600, 30, "cli_timeout"
        )
        self.max_concurrent: int = self._as_int(
            config.get("max_concurrent"), 2, 1, "max_concurrent"
        )

        # ---------- 下载 ----------
        download = config.get("download") or {}
        if not isinstance(download, dict):
            logger.warning("[config] download 配置不是对象，已回退为默认值")
            download = {}
        # MB，Downloader 读取；0/负数会让任何下载都被判为超限，因此下限取 1
        self.source_max_size: int = self._as_int(
            download.get("max_size_mb"), 90, 1, "download.max_size_mb"
        )
        self.source_max_minute: int = self._as_int(
            download.get("max_minutes"), 15, 1, "download.max_minutes"
        )
        self.download_timeout: int = self._as_int(
            download.get("download_timeout"), 280, 5, "download.download_timeout"
        )
        # 0 是合法值：表示不重试
        self.download_retry_times: int = self._as_int(
            download.get("download_retry_times"), 2, 0, "download.download_retry_times"
        )
        self.common_timeout: int = self._as_int(
            download.get("common_timeout"), 15, 5, "download.common_timeout"
        )
        proxy = str(download.get("proxy") or "")
        self.proxy: str | None = proxy or None

        # 派生限制
        self.max_duration: int = self.source_max_minute * 60  # 秒
        self.max_size: int = self.source_max_size * 1024 * 1024  # 字节

        # ---------- 解析器 ----------
        parsers_raw = config.get("parsers") or {}
        if not isinstance(parsers_raw, dict):
            logger.warning("[config] parsers 配置不是对象，已回退为默认值")
            parsers_raw = {}
        self.parser = ParserConfig(parsers_raw)
        self._warn_value_zero()

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

    def _warn_value_zero(self) -> None:
        """提示语义修正带来的行为变化。

        修复前这些字段填 0 会被悄悄当成“未填写”而回落默认值（120 / 2），
        现在 0 按字面生效。这里只告警、不阻止启动：有人可能一直依赖着
        “填 0 其实还是 120”的旧行为，需要给他一条可查的线索。
        """
        raw = self._raw or {}
        download = raw.get("download") or {}
        if not isinstance(download, dict):
            return
        if str(raw.get("debounce_seconds", "")).strip() == "0":
            logger.warning(
                "[config] debounce_seconds=0：链接防抖已完全关闭，"
                "同一链接重复发送会各自搬运一次（修复前 0 被当作未填写、实际按 120s 生效）"
            )
        if str(download.get("download_retry_times", "")).strip() == "0":
            logger.warning(
                "[config] download_retry_times=0：下载与短链跳转不再重试"
                "（修复前 0 被当作未填写、实际按 2 次重试）"
            )
