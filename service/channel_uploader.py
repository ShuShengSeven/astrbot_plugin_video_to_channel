"""腾讯频道上传器：通过统一 CliRunner 调用 tencent-channel-cli 发布视频帖子。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..core.config import PluginConfig
from .cli_binary import CliBinaryManager
from .cli_runner import CliError, CliOutputError, CliRunner, CliTimeoutError


class PublishResultUnknownError(CliError):
    """投稿已进入 CLI 阶段但结果无法核对。

    与 CliTimeoutError 同属「结果未知」：帖子可能已经发出，
    上层不能按“确定失败”放开防抖、也不能回执“已上传”。
    这个类型会在 channel_uploader 内部对“已开始提交”的未知错误统一收敛，
    因此 pipeline 只需关心一种“投稿结果未知”异常，而不必理解 CLI 的每种坏法。
    """


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
            # 4.2：必须先做“结果未知”分类，再考虑任何自动重试。
            # CliTimeoutError / CliOutputError / PublishResultUnknownError 都表示
            # “无法确认服务端是否已建帖”：此时绝对禁止 Markdown 自动重试，
            # 否则第一次可能已经发帖，第二次会变成重复投稿。
            if isinstance(e, CliTimeoutError):
                raise
            if isinstance(e, (CliOutputError, PublishResultUnknownError)):
                raise PublishResultUnknownError(str(e)) from e
            # 只有服务端明确拒绝（definite=True）且错误确属 Markdown 拒绝，
            # 才允许清理 Markdown 后重新投稿一次。
            if (
                content
                and e.definite
                and self._looks_like_markdown_hint(str(e))
            ):
                cleaned = self._sanitize_plain_content(content)
                if cleaned != content:
                    logger.warning("[uploader] 标题含 Markdown 语法，已清理后重试")
                    return await self.publish_video(video_path, content=cleaned)
            # 其余 CliError（含业务拒绝 payload）由调用方按 definite 语义处理；
            # 重试后的第二次结果同样走上面的三态分类，不会破坏状态机。
            raise

        if not isinstance(payload, dict):
            raise CliOutputError(f"无法解析 CLI 返回结果: {payload}")

        # RT02-F2：success=true 本身不能作为投稿成功凭证。必须有明确成功证据：
        # 优先取 data（须为 dict 且含 feed_id/share_url）；同时兼容 CLI 直接把
        # 业务结果放顶层（unwrap_data 曾支持的形态，data 键缺失时顶层含 feed_id/
        # share_url 也算成功证据）。其余一律 UNKNOWN —— 无法确认帖子已创建。
        data = payload.get("data")
        if not isinstance(data, dict):
            # data 缺失/非对象：看顶层是否有直接证据
            feed_id = self._first_of(payload, "feed_id", "id")
            share_url = self._first_of(payload, "share_url", "url", "short_url")
            if feed_id or share_url:
                return PublishResult(
                    raw=payload,
                    feed_id=feed_id,
                    share_url=share_url,
                )
            raise PublishResultUnknownError(
                "tencent-channel-cli 未返回可核对的结果数据，无法确认帖子是否发布成功；"
                "请先去目标频道核实，确认后再重试。"
            )

        # data 存在且为对象，但仍可能是空对象（无任何投稿证据）
        if not data:
            raise PublishResultUnknownError(
                "tencent-channel-cli 返回的 data 为空，无法确认帖子是否发布成功；"
                "请先去目标频道核实，确认后再重试。"
            )

        feed_id = self._first_of(data, "feed_id", "id")
        share_url = self._first_of(data, "share_url", "url", "short_url")
        if not feed_id and not share_url:
            raise PublishResultUnknownError(
                f"tencent-channel-cli 返回结果中没有 feed_id/share_url，无法确认帖子"
                f"是否发布成功：{data}；请先去目标频道核实，确认后再重试。"
            )

        return PublishResult(
            raw=data,
            feed_id=feed_id,
            share_url=share_url,
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
