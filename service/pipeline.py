"""处理流水线：链接匹配 → 解析下载 → CLI 上传 → 清理本地文件。"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..core.config import PluginConfig
from ..core.data import ParseResult, VideoContent
from ..core.utils import safe_unlink
from .channel_uploader import (
    ChannelUploader,
    PublishResult,
    PublishResultUnknownError,
)
from .debounce import Debouncer
from .parser_router import ParserRouter


class PipelineError(RuntimeError):
    """流水线业务错误（用于向用户展示友好信息）。"""


# 「投稿结果」三态。RT-02：不允许用异常类型 / 中文文案去猜“服务端是否创建了帖子”，
# 只有能证明「服务端明确拒绝且未创建帖子」才属于 DEFINITE_FAILURE。
OUTCOME_SUCCESS = "success"
OUTCOME_DEFINITE_FAILURE = "definite_failure"
OUTCOME_UNKNOWN = "unknown"


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
        # 正在处理中的链接（全局去重，避免同一链接跨会话并发重复上传）
        self._active_links: set[str] = set()
        # 跨 reload 的「投稿结果未知」冷却（RT-03）。落盘在插件数据目录：
        # 旧实例在投稿阶段被 terminate/reload 时，把已开始的链接记成 UNKNOWN，
        # 新实例在冷却期内不得接受同一链接（防重复投稿）。
        self._unknown_file = cfg.data_dir / "unknown_submits.json"
        # RT03-F2：跨实例并发写通过「写前重读磁盘并合并」保证不丢记录；
        # 单实例 asyncio.Lock 保护不了跨实例，故不引入。
        self._unknown = self._load_unknown()

    # ------------------------------------------------------------------
    def _load_unknown(self) -> dict[str, float]:
        """加载跨实例的 UNKNOWN 记录（绝对时间戳，epoch 秒）。"""
        try:
            if not self._unknown_file.exists():
                return {}
            raw = json.loads(self._unknown_file.read_text(encoding="utf-8") or "{}")
            if not isinstance(raw, dict):
                return {}
            now = time.time()
            keep = {}
            for link, ts in raw.items():
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                # 只保留仍在冷却期内的
                if ts > now:
                    keep[link] = ts
            # 顺带清理已过期的记录，避免 JSON 无限累积
            if len(keep) != len(raw):
                self._unknown = keep
                self._flush_unknown()
            return keep
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pipeline] 读取跨实例投稿状态失败: {e}")
            return {}

    def _flush_unknown(self) -> None:
        """把 UNKNOWN 记录原子写盘（同步；RT-03 要求异常返回前落盘完成）。

        RT03-F2：不能拿内存全量 dict 直接覆盖文件——并发实例/任务会互相丢掉对方
        刚写入的记录。写盘前先重读磁盘上的当前记录，与本次要写的记录合并
        （同一链接取更晚的过期时间），再整体原子写回。
        """
        try:
            self._unknown_file.parent.mkdir(parents=True, exist_ok=True)
            merged = self._merge_with_disk(self._unknown)
            # 唯一临时文件名：多实例并发 flush 时避免写同一 tmp 交错
            tmp = self._unknown_file.with_name(
                f"{self._unknown_file.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                tmp.write_text(
                    json.dumps(merged, ensure_ascii=False), encoding="utf-8"
                )
                os.replace(tmp, self._unknown_file)
            finally:
                tmp.unlink(missing_ok=True)
            # 合并结果同步回内存，避免后续基于过期内存继续写
            self._unknown = merged
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pipeline] 写入跨实例投稿状态失败: {e}")

    @staticmethod
    def _read_disk_unknown(path: Path) -> dict[str, float]:
        """读磁盘记录；损坏/缺文件时返回空 dict，绝不抛异常。"""
        try:
            if not path.exists():
                return {}
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(raw, dict):
                return {}
            out = {}
            for link, ts in raw.items():
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                out[link] = ts
            return out
        except Exception:  # noqa: BLE001
            return {}

    def _merge_with_disk(self, memory: dict[str, float]) -> dict[str, float]:
        """把内存记录与磁盘现有记录合并：同一链接取更晚的过期时间。

        合并时丢弃已过期的磁盘记录，避免过期条目在文件里无限累积
        （功能上过期不拦截，但会撑大文件）。
        """
        now = time.time()
        disk = self._read_disk_unknown(self._unknown_file)
        merged = {}
        for link, ts in disk.items():
            if ts > now:
                merged[link] = ts
        for link, ts in memory.items():
            if ts > now:
                merged[link] = max(merged.get(link, 0.0), ts)
        return merged

    def _remember_unknown(self, link: str, until: float) -> None:
        self._unknown[link] = max(self._unknown.get(link, 0.0), until)
        # 立即同步写盘：异步 create_task 在异常路径/事件循环收尾时可能来不及执行，
        # 导致新实例读不到 UNKNOWN、重复投稿（红队 rt20 实证）。
        self._flush_unknown()

    def _forget_unknown(self, link: str) -> None:
        if link in self._unknown:
            self._unknown.pop(link, None)
            self._flush_unknown()

    def is_unknown(self, link: str) -> bool:
        return self._unknown.get(link, 0.0) > time.time()

    # ------------------------------------------------------------------
    def has_supported_link(self, text: str) -> bool:
        return self.router.match(text) is not None

    def canonical_link(self, text: str) -> str | None:
        """把消息文本规整成统一的链接 key（RT03-F1）。

        与 process() 内部使用的 key 完全一致：router 正则匹配后取 group(0)。
        同一 URL 的 http/https/无 scheme/夹带文字/前后标点都规整到同一 key。
        terminate 等外部调用方必须通过这里取得 key，而不是直接传原始消息文本。
        """
        matched = self.router.match(text)
        if matched is None:
            return None
        _parser, _keyword, searched = matched
        return searched.group(0)

    def mark_link_unknown(self, text: str) -> None:
        """把链接标记为「结果未知」并写入持久化冷却（terminate/reload 时调用）。

        入参可以是完整消息文本或链接；内部统一 canonicalize 成与 process 查询
        完全一致的 key。旧实例在投稿阶段被取消，无法知道帖子是否已创建，把链接
        记成 UNKNOWN，让新实例在冷却期内拒绝同一链接（RT-03）。
        """
        link = self.canonical_link(text)
        if link is None:
            # 传入的不是受支持链接（不应发生），保守起见仍记录原文本避免误放行
            link = text
        cooldown = max(self.cfg.debounce_seconds, 300)
        self._remember_unknown(link, time.time() + cooldown)

    async def process(
        self,
        text: str,
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ) -> ProcessResult | None:
        """完整处理一条消息；返回 None 表示被去重/防抖跳过。

        ``on_accepted`` 只在取得并发额度、真正开始解析之前被调用一次。
        “开始解析”之类的承诺式回执必须由它发出：被跳过的请求不会触发它，
        也就不会再出现“先说开始处理、然后一声不吭”的情况。
        """
        matched = self.router.match(text)
        if matched is None:
            return None
        parser, keyword, searched = matched
        # RT03-F1：统一 canonical key，与 mark_link_unknown / terminate 完全一致
        link = self.canonical_link(text)
        if link is None:
            link = searched.group(0)

        # 跨实例「结果未知」冷却（RT-03）：旧实例若在投稿阶段被杀，会把这个链接
        # 记为 UNKNOWN；新实例在冷却期内必须拒绝同一链接，而不是立刻再投一票。
        if self.is_unknown(link):
            logger.warning(f"[pipeline] 链接 {link} 处于跨实例 UNKNOWN 冷却期，跳过")
            return None
        # 全局进行中去重：同一链接已在处理中时，其他会话/消息直接跳过
        if link in self._active_links:
            logger.info(f"[pipeline] 链接 {link} 正在处理中，跳过重复请求")
            return None
        self._active_links.add(link)

        reserved = False  # 是否已写入防抖记录
        upload_attempted = False  # 是否已把视频提交给腾讯频道
        outcome = OUTCOME_DEFINITE_FAILURE  # 默认；成功/未知会显式覆盖
        parse_result: ParseResult | None = None
        video: VideoContent | None = None
        video_path: Path | None = None
        try:
            if self._debouncer.hit(link):
                logger.info(f"[pipeline] 链接 {link} 处于防抖窗口内，跳过")
                return None
            reserved = True

            missing = self.uploader.describe_missing()
            if missing:
                raise PipelineError("；".join(missing))

            async with self._semaphore:
                if on_accepted is not None:
                    await on_accepted()
                logger.info(f"[pipeline] 开始解析 {link}")
                parse_result = await parser.parse(keyword, searched)
                videos = parse_result.video_contents
                if not videos:
                    raise PipelineError(
                        f"解析到的是{parse_result.platform.display_name}的非视频内容"
                        "（图文/音频/直播/动态等），本插件仅搬运单条视频"
                    )
                if len(videos) > 1:
                    # 多视频作品不“只传第一条却回执成功”，避免与用户预期不一致
                    raise PipelineError(
                        f"该作品包含 {len(videos)} 条视频，本插件暂不支持多视频作品，"
                        "请分享只含一条视频的作品"
                    )

                video = videos[0]
                # 先按时长拦截，再等下载：长视频不该先被完整拉下来再拒绝
                self._check_duration(video, parse_result)
                video_path = await video.get_path()
                title = (
                    (parse_result.title or "").strip()
                    or f"{parse_result.platform.display_name}视频"
                )
                logger.info(f"[pipeline] 视频下载完成: {video_path.name}，标题: {title}")
                upload_attempted = True
                publish = await self.uploader.publish_video(video_path, content=title)
                outcome = OUTCOME_SUCCESS
                logger.info(
                    f"[pipeline] 搬运完成: {link} -> feed_id={publish.feed_id}"
                )
                return ProcessResult(
                    platform_name=parse_result.platform.display_name,
                    title=title,
                    source_link=link,
                    publish=publish,
                    local_path=video_path,
                )
        except BaseException as exc:
            # RT-02 状态机（不允许猜）：
            #   1) 投稿前失败（解析/下载/配置/超限/时长/多视频）-> DEFINITE_FAILURE
            #   2) 已进入投稿，CLI 明确业务拒绝（exc.definite=True，服务端确认没建帖）
            #      -> DEFINITE_FAILURE
            #   3) 已进入投稿，其余任何异常（输出坏掉/通信中断/未知/被取消）
            #      -> UNKNOWN：保留防抖 + 写跨实例冷却
            if upload_attempted and not getattr(exc, "definite", False):
                outcome = OUTCOME_UNKNOWN
            else:
                outcome = OUTCOME_DEFINITE_FAILURE

            if outcome == OUTCOME_UNKNOWN:
                logger.warning(f"[pipeline] 投稿结果未知，保留防抖并写 UNKNOWN 冷却: {link}")
                # 冷却期至少等于防抖窗口，且不小于 5 分钟；避免新实例立刻重投
                cooldown = max(self.cfg.debounce_seconds, 300)
                self._remember_unknown(link, time.time() + cooldown)
            else:
                logger.debug(f"[pipeline] 确定失败，解除防抖允许重试: {link}")
                if reserved:
                    self._debouncer.forget(link)
            raise
        finally:
            leftovers = self._release_media(parse_result, video)
            if video_path is not None:
                leftovers.append(video_path)
            if leftovers:
                await self._cleanup(leftovers)
            self._active_links.discard(link)

    # ------------------------------------------------------------------
    def _check_duration(self, video: VideoContent, parse_result: ParseResult) -> None:
        """按 download.max_minutes 拦截超长视频（各平台时长已统一为秒）。"""
        duration = float(getattr(video, "duration", 0.0) or 0.0)
        limit = self.cfg.max_duration
        if duration <= 0 or limit <= 0:
            return  # 平台没给时长时不做猜测性拒绝
        if duration > self._MAX_PLAUSIBLE_DURATION:
            # 单位又对不上时的安全网：宁可放行也不要“全部误判超限”直接罢工
            logger.warning(
                f"[pipeline] {parse_result.platform.name} 时长字段异常"
                f"（{duration:.0f}s），跳过时长校验"
            )
            return
        if duration > limit:
            raise PipelineError(
                f"视频时长超过限制：约 {duration / 60:.1f} 分钟，"
                f"当前上限 {limit // 60} 分钟"
                "（可调整插件配置 download.max_minutes）"
            )

    _MAX_PLAUSIBLE_DURATION = 24 * 3600

    def _release_media(
        self, parse_result: ParseResult | None, keep_video: VideoContent | None
    ) -> list[Path]:
        """取消本次用不到的下载任务，并返回已落盘、需要清理的文件。

        封面/头像/图集在 v1 都不参与上传：
        - 旧实现“为了删封面而先 await 封面”，等于白白下载一遍再删掉；
        - 作品被拒绝（图文/多视频/超限）时，解析阶段起的下载任务会留在后台继续跑。
        未结束的任务直接取消（下载内部会清掉 .part），已结束的才回收路径。
        """
        if parse_result is None:
            return []
        paths: list[Path] = []

        author = getattr(parse_result, "author", None)
        if author is not None:
            self._release(getattr(author, "avatar", None))

        for content in parse_result.contents:
            if content is keep_video:
                # 只为“必要时取消”，文件本身由 video_path 统一清理
                self._release(getattr(content, "path_task", None))
                cover = self._release(getattr(content, "cover", None))
                if cover is not None:
                    paths.append(cover)
                continue
            for attr in ("path_task", "cover"):
                leftover = self._release(getattr(content, attr, None))
                if leftover is not None:
                    paths.append(leftover)
        return paths

    @staticmethod
    def _release(value: object) -> Path | None:
        """未结束 -> 取消；已结束 -> 返回已落盘路径（供清理）。"""
        if isinstance(value, Path):
            return value
        if not isinstance(value, asyncio.Future):
            return None
        if not value.done():
            value.cancel()
            return None
        if value.cancelled() or value.exception() is not None:
            return None
        result = value.result()
        return result if isinstance(result, Path) else None

    async def _cleanup(self, targets: list[Path]) -> None:
        """删除缓存目录中本次产生的文件，避免长期占用磁盘。"""
        cache_dir = self.cfg.cache_dir.resolve()
        for target in targets:
            try:
                # 只清理本插件缓存目录内的文件，绝不触碰外部路径
                if target.resolve().is_relative_to(cache_dir):
                    await safe_unlink(target)
                    logger.debug(f"[pipeline] 已清理临时文件: {target.name}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[pipeline] 清理临时文件失败 {target}: {e}")
