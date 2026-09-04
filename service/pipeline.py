"""处理流水线：链接匹配 → 解析下载 → CLI 上传 → 清理本地文件。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..core.config import PluginConfig
from ..core.utils import safe_unlink
from .channel_uploader import ChannelUploader, PublishResult
from .debounce import Debouncer
from .parser_router import ParserRouter


class PipelineError(RuntimeError):
    """流水线业务错误（用于向用户展示友好信息）。"""


@dataclass(slots=True)
class ProcessResult:
    platform_name: str
    title: str
    source_link: str
    publish: PublishResult
    local_path: Path


class VideoPipeline:
    """编排一次「链接 -> 腾讯频道帖子」的完整搬运。"""

    def __init__(self, cfg: PluginConfig, router: ParserRouter, uploader: ChannelUploader):
        self.cfg = cfg
        self.router = router
        self.uploader = uploader
        self._semaphore = asyncio.Semaphore(max(1, cfg.max_concurrent))
        self._debouncer = Debouncer(cfg.debounce_seconds)

    # ------------------------------------------------------------------
    def has_supported_link(self, text: str) -> bool:
        return self.router.match(text) is not None

    async def process(self, session_id: str, text: str) -> ProcessResult | None:
        """完整处理一条消息；返回 None 表示被防抖跳过。"""
        matched = self.router.match(text)
        if matched is None:
            return None
        parser, keyword, searched = matched
        link = searched.group(0)

        if self._debouncer.hit(session_id, link):
            logger.info(f"[pipeline] 链接 {link} 处于防抖窗口内，跳过")
            return None

        missing = self.uploader.describe_missing()
        if missing:
            raise PipelineError("；".join(missing))

        async with self._semaphore:
            logger.info(f"[pipeline] 开始解析 {link}")
            parse_result = await parser.parse(keyword, searched)
            videos = parse_result.video_contents
            if not videos:
                raise PipelineError(
                    f"解析到的是{parse_result.platform.display_name}的非视频内容"
                    "（图文/音频等），本插件 v1 仅搬运视频"
                )

            video = videos[0]
            video_path = await video.get_path()
            title = (parse_result.title or "").strip() or f"{parse_result.platform.display_name}视频"

            logger.info(f"[pipeline] 视频下载完成: {video_path.name}，标题: {title}")
            publish = await self.uploader.publish_video(video_path, content=title)

            # 上传成功后再清理本地文件
            cover = await self._get_cover_path(video)
            await self._cleanup(video_path, cover)

            return ProcessResult(
                platform_name=parse_result.platform.display_name,
                title=title,
                source_link=link,
                publish=publish,
                local_path=video_path,
            )

    # ------------------------------------------------------------------
    async def _get_cover_path(self, video) -> Path | None:
        try:
            return await video.get_cover_path()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[pipeline] 封面下载/读取失败（忽略）: {e}")
            return None

    async def _cleanup(self, video_path: Path, cover: Path | None) -> None:
        """删除缓存目录中本次下载的视频/封面，避免长期占用磁盘。"""
        cache_dir = self.cfg.cache_dir.resolve()
        targets = [video_path]
        if cover is not None:
            targets.append(cover)
        for target in targets:
            try:
                # 只清理本插件缓存目录内的文件，绝不触碰外部路径
                if target.resolve().is_relative_to(cache_dir):
                    await safe_unlink(target)
                    logger.debug(f"[pipeline] 已清理临时文件: {target.name}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[pipeline] 清理临时文件失败 {target}: {e}")
