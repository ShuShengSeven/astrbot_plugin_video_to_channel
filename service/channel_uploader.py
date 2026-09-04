"""腾讯频道上传器：通过统一 CliRunner 调用 tencent-channel-cli 发布视频帖子。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..core.config import PluginConfig
from .cli_binary import CliBinaryManager
from .cli_runner import CliError, CliRunner


@dataclass(slots=True)
class PublishResult:
    """上传成功后的可读结果。"""

    raw: dict
    feed_id: str | None = None
    share_url: str | None = None


class ChannelUploader:
    """封装 tencent-channel-cli feed publish-feed 的调用。"""

    def __init__(self, cfg: PluginConfig, runner: CliRunner | None = None):
        self.cfg = cfg
        self.runner = runner or CliRunner(cfg, CliBinaryManager(cfg))

    # ------------------------------------------------------------------
    # 状态检查
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """managed(auto) 模式视为可配置——二进制会在首次使用时自动下载。"""
        if not self.cfg.target_guild_id or not self.cfg.target_channel_id:
            return False
        if self.cfg.cli_managed:
            return True
        return self.runner.manager.resolve_existing() is not None

    def describe_missing(self) -> list[str]:
        """返回需要用户处理的缺失项；auto 模式未下载不算缺失。"""
        missing: list[str] = []
        if not self.cfg.target_guild_id:
            missing.append("目标频道 guild_id 未配置（可用 /v2c target 设置）")
        if not self.cfg.target_channel_id:
            missing.append("目标版块 channel_id 未配置（可用 /v2c target 设置）")
        if not self.cfg.cli_managed and self.runner.manager.resolve_existing() is None:
            missing.append(
                f"配置的外部 tencent-channel-cli 不存在：{self.cfg.cli_command}"
            )
        return missing

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------
    async def publish_video(self, video_path: Path, content: str) -> PublishResult:
        """发布单条视频帖子；标题含 Markdown 语法时自动清理重试一次。"""
        if not video_path.exists():
            raise RuntimeError(f"待上传视频文件不存在: {video_path}")

        argv = [
            "feed",
            "publish-feed",
            "--json",
            "--guild-id",
            self.cfg.target_guild_id,
            "--channel-id",
            self.cfg.target_channel_id,
            "--video",
            str(video_path),
        ]
        if content:
            argv += ["--content", content]

        logger.info(
            f"[uploader] 调用 CLI 上传视频: {video_path.name} -> "
            f"guild={self.cfg.target_guild_id}, channel={self.cfg.target_channel_id}"
        )
        try:
            payload = await self.runner.run_json(argv, timeout=self.cfg.cli_timeout)
        except CliError as e:
            # CLI 纯文本模式会拒绝 Markdown 语法：用清理后的标题重试一次
            if content and self._looks_like_markdown_hint(str(e)):
                cleaned = self._sanitize_plain_content(content)
                if cleaned != content:
                    logger.warning("[uploader] 标题含 Markdown 语法，已清理后重试")
                    return await self.publish_video(video_path, content=cleaned)
            raise

        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            raise CliError(f"无法解析 CLI 返回结果: {payload}")

        return PublishResult(
            raw=data,
            feed_id=self._first_of(data, "feed_id", "id"),
            share_url=self._first_of(data, "share_url", "url", "short_url"),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _sanitize_plain_content(text: str) -> str:
        """去掉行首/行内常见 Markdown 结构符号，供纯文本模式兜底重试。"""
        import re

        lines = []
        for line in text.splitlines():
            s = re.sub(r"^#{1,6}\s+", "", line)
            s = re.sub(r"^>\s?", "", s)
            s = re.sub(r"^[-*+]\s+", "", s)
            s = s.replace("**", "").replace("__", "")
            lines.append(s)
        result = "\n".join(lines).strip()
        return result or text

    @staticmethod
    def _looks_like_markdown_hint(err_text: str) -> bool:
        return "markdown" in err_text.lower()

    @staticmethod
    def _first_of(data: dict, *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        return None
