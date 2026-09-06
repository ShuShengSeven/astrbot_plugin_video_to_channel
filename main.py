"""AstrBot 插件：视频链接自动搬运到腾讯频道 + tencent-channel-cli 自托管账号管理。

- 监听白名单会话（群聊/私聊）中的 B站/抖音视频分享链接，自动下载并上传到腾讯频道；
- 私聊中通过 /v2c 指令完成扫码登录、状态查看、频道/版块查询与上传目标设置；
- tencent-channel-cli 由插件自动下载托管（cli_command=auto），无需手动安装。

架构（模块化，便于扩展）：
- main.py          : AstrBot Star 入口，消息监听 + /v2c 指令组
- service/         : CLI 托管、执行器、账号服务、上传、编排、防抖
- core/            : 移植自 astrbot_plugin_parser 的解析/下载核心（MIT）
"""
import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Json

from .core.config import PluginConfig
from .core.utils import extract_json_url, safe_rmtree, safe_unlink
from .core.download import Downloader
from .service.channel_uploader import ChannelUploader, PublishResultUnknownError
from .service.cli_account import CliAccount, QrLoginInfo, is_qr_expired_text
from .service.cli_binary import CliBinaryManager
from .service.cli_runner import (
    AlreadyLoggedInError,
    CliError,
    CliRunner,
    CliTimeoutError,
)
from .service.parser_router import ParserRouter
from .service.pipeline import VideoPipeline

PLUGIN_NAME = "astrbot_plugin_video_to_channel"
PRIVATE_ONLY = filter.EventMessageType.PRIVATE_MESSAGE
ADMIN_ONLY = filter.PermissionType.ADMIN


class VideoToChannelPlugin(Star):
    """视频搬运工：链接进来 → 本地视频 → 腾讯频道指定版块。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, plugin_name=PLUGIN_NAME)

        # CLI 自托管与统一执行器
        self.cli_manager = CliBinaryManager(self.cfg)
        self.cli = CliRunner(self.cfg, self.cli_manager)
        self.account = CliAccount(self.cfg, self.cli)

        # 下载器：所有解析器共用（内部维护 aiohttp 会话）
        self.downloader = Downloader(self.cfg)
        # 解析器路由：关键词/正则 -> 平台解析器
        self.router = ParserRouter(self.cfg, self.downloader)
        # 腾讯频道上传器：封装 tencent-channel-cli
        self.uploader = ChannelUploader(self.cfg, self.cli)
        # 编排流水线：解析 → 下载 → 上传 → 清理
        self.pipeline = VideoPipeline(self.cfg, self.router, self.uploader)

        # 每个会话进行中的登录轮询任务
        self._login_tasks: dict[str, asyncio.Task] = {}
        # 正在执行的视频搬运后台任务（持有引用，防止被 GC 且便于卸载时取消）
        self._bg_tasks: set[asyncio.Task] = set()
        # 后台任务 -> 正在处理的链接（terminate 标记 UNKNOWN 用）
        self._task_links: dict[asyncio.Task, str] = {}

    async def initialize(self):
        """插件加载/重载时初始化解析器。"""
        await self._cleanup_stale_cache()
        # 新表构建成功后才关闭旧解析器会话（见 ParserRouter.initialize）。
        # 旧写法是 close() 在前：构建一旦抛错，匹配表就永久为空，
        # 表现为“插件已加载但任何链接都不再触发”。
        await self.router.initialize()
        missing = self.uploader.describe_missing()
        if missing:
            logger.warning("[v2c] 上传配置不完整: " + "；".join(missing))

    async def terminate(self):
        """插件卸载/停用时释放资源。"""
        # RT-03：任何正在执行的搬运任务此刻都可能在投稿阶段（结果未知）。
        # 先把它们处理的链接标记为跨实例 UNKNOWN（持久化冷却），再取消任务；
        # 否则 reload 后新实例会立刻接受同一链接，可能重复投稿。
        for link in self._task_links.values():
            try:
                self.pipeline.mark_link_unknown(link)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[v2c] 标记 UNKNOWN 失败 {link}: {e}")
        tasks = list(self._bg_tasks) + list(self._login_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()
        self._task_links.clear()
        self._login_tasks.clear()
        await self.router.close()
        await self.downloader.close()

    async def _cleanup_stale_cache(self) -> None:
        """启动时清理下载残留（无副作用策略）。

        - `.part` 一定是未完成的临时文件，可安全删除；
        - 超过 24h 的最终文件只可能是历史遗留（正常流程上传后即清理），可安全删除；
        - 近期文件可能仍被进行中的任务使用，不删除。
        """
        cache = self.cfg.cache_dir
        if not cache.exists():
            return
        try:
            entries = list(cache.iterdir())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[v2c] 扫描缓存目录失败，跳过启动清理: {e}")
            return
        now = time.time()
        removed = 0
        for path in entries:
            try:
                stale = False
                if path.is_dir():
                    # RT-01 任务级工作目录（<stem>-<8hex>）崩溃后残留；
                    # 只清 mtime 超 24h 的目录，避免误删进行中任务
                    stale = now - path.stat().st_mtime > 24 * 3600
                    if stale:
                        await safe_rmtree(path)
                        removed += 1
                    continue
                stale = path.name.endswith(".part")
                if not stale:
                    stale = now - path.stat().st_mtime > 24 * 3600
                if stale:
                    await safe_unlink(path)
                    removed += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[v2c] 启动清理跳过 {path.name}: {e}")
        if removed:
            logger.info(f"[v2c] 启动时清理缓存残留 {removed} 个文件")

    # ==================================================================
    # 消息入口：白名单会话内直接发链接即触发
    # ==================================================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """统一消息入口：白名单会话内直接发链接即触发（无需 @机器人）。"""
        umo = event.unified_msg_origin
        if not umo or not self.cfg.session_whitelist:
            return
        if umo not in self.cfg.session_whitelist:
            return

        text = (event.message_str or "").strip()
        # QQ/频道分享常以 JSON 卡片形式发送，纯文本可能不含 URL
        if not text:
            text = self._extract_card_url(event) or ""
            if not text:
                return
        # 不处理指令文本，避免把 /v2c 等命令内容也当作链接
        if text.startswith("/"):
            return

        # 不处理机器人自己发出的消息（某些平台会回推）
        if self._is_self_message(event):
            return

        if not self.router.patterns or not self.pipeline.has_supported_link(text):
            card_url = self._extract_card_url(event)
            if not card_url:
                return
            text = card_url

        task = asyncio.create_task(self._background_handle(umo, text))
        self._bg_tasks.add(task)
        # 记录这个后台任务正在处理的链接（统一 canonical key），供 terminate 在
        # 取消时标记 UNKNOWN（RT-03）。必须用 pipeline.canonical_link 而不是原始
        # 消息文本，否则 terminate 写入的 key 与 process 查询的 key 不一致，
        # 新实例会放行同一链接导致重复投稿。
        canonical = self.pipeline.canonical_link(text) or text
        self._task_links[task] = canonical

        def _done(_task: asyncio.Task) -> None:
            self._bg_tasks.discard(_task)
            self._task_links.pop(_task, None)

        task.add_done_callback(_done)
        # “开始解析”的回执改由流水线在真正开工时发出（见 _background_handle）：
        # 被去重/防抖跳过的请求此前也会先收到这句承诺，然后就再无下文。

    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        """识别机器人自己回推的消息。

        不能直接比较 str(sender) == str(self)：两个取值都为 None 时
        （部分平台适配器未回填）会得到 "None" == "None" → 判定为“自己发的”，
        于是该平台上所有链接都被丢弃，插件整体静默失效。
        """
        try:
            sender_id = event.get_sender_id()
            self_id = event.get_self_id()
        except Exception:  # noqa: BLE001
            # 某些平台未实现 get_self_id()，此时不做自消息过滤
            return False
        if sender_id is None or self_id is None:
            return False
        return str(sender_id) == str(self_id)

    def _extract_card_url(self, event: AstrMessageEvent) -> str | None:
        """从 JSON 分享卡片中提取第一个受支持的链接。"""
        try:
            chain = event.get_messages() or []
        except Exception:  # noqa: BLE001
            return None
        for seg in chain:
            if not isinstance(seg, Json):
                continue
            url = extract_json_url(getattr(seg, "data", None))
            if url and self.pipeline.has_supported_link(url):
                return url
        return None

    async def _background_handle(self, umo: str, text: str):
        """后台任务：真正开工时回执“开始解析”，完成后回执结果。"""

        async def _accepted() -> None:
            await self._send(umo, "⏳ 检测到视频分享链接，开始解析并上传到腾讯频道…")

        try:
            result = await self.pipeline.process(text, on_accepted=_accepted)
            if result is None:
                # 去重/防抖/UNKNOWN 冷却跳过：按设计保持静默
                return
            share_url = result.publish.share_url
            msg = f"✅ 已上传《{result.title}》（{result.platform_name}）到腾讯频道指定版块"
            if share_url:
                msg += f"\n分享链接: {share_url}"
            await self._send(umo, msg)
        except asyncio.CancelledError:
            # RT-03：任务在投稿阶段被 terminate/reload 取消 => 结果未知。
            # 调用方（terminate）已负责把进行中链接标记为 UNKNOWN，这里直接重抛。
            raise
        except Exception as e:  # noqa: BLE001
            # 并发搬运时没有上下文的日志无法定位是哪一条
            logger.exception(f"[v2c] 视频搬运失败 | session={umo} | 内容={text[:120]}")
            await self._send(umo, f"❌ 视频搬运失败：{self._friendly_error(e)}")

    # ==================================================================
    # /v2c 指令组（私聊 + 管理员）
    #
    # 说明：这些 handler 统一 priority=100（高于第三方插件默认的 0），并在
    # finally 中调用 event.stop_event()，确保 /v2c 指令先被本插件处理、处理完
    # 后不再被其他插件（例如 astrbot_plugin_parser 的 ALL 监听器）把 18 位
    # 数字 ID 当作视频链接抢解析。
    # ==================================================================
    @filter.command_group("v2c")
    def v2c(self):
        """腾讯频道 CLI 管理（私聊 + 管理员）。"""
        pass

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("login")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_login(self, event: AstrMessageEvent):
        """生成登录二维码并自动检测登录结果。"""
        umo = event.unified_msg_origin
        try:
            yield event.plain_result("⏳ 正在准备 tencent-channel-cli…")
            await self.cli_manager.ensure()
            status = await self.account.login_status()
            if status.logged_in:
                yield event.plain_result(
                    "ℹ️ 当前已登录腾讯频道 CLI。如需重新登录：先 /v2c logout，"
                    "或直接 /v2c relogin。"
                )
                return

            qr = await self.account.qr_login()
            if qr.qrcode_path and qr.qrcode_path.exists():
                yield event.image_result(str(qr.qrcode_path))
            yield event.plain_result(
                f"请使用手机 QQ 扫码，或打开授权链接完成登录（{qr.expires_in_s}s 内有效）：\n"
                f"{qr.verification_uri}\n"
                "登录成功后我会自动通知你。"
            )
            self._start_login_poll(umo, qr)
        except AlreadyLoggedInError as e:
            yield event.plain_result(f"ℹ️ {self._friendly_error(e)}")
        except CliError as e:
            yield event.plain_result(f"❌ 登录失败：{self._friendly_error(e)}")
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] login 命令失败")
            yield event.plain_result(f"❌ 登录失败：{self._friendly_error(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("relogin")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_relogin(self, event: AstrMessageEvent):
        """已登录时强制覆盖旧凭证，重新生成登录二维码（等价 login --yes）。"""
        umo = event.unified_msg_origin
        try:
            yield event.plain_result("⏳ 正在准备重新登录…")
            await self.cli_manager.ensure()
            qr = await self.account.qr_login(relogin=True)
            if qr.qrcode_path and qr.qrcode_path.exists():
                yield event.image_result(str(qr.qrcode_path))
            yield event.plain_result(
                f"请使用手机 QQ 扫码，或打开授权链接完成登录（{qr.expires_in_s}s 内有效）：\n"
                f"{qr.verification_uri}\n"
                "登录成功后我会自动通知你。"
            )
            self._start_login_poll(umo, qr)
        except AlreadyLoggedInError as e:
            yield event.plain_result(f"ℹ️ {self._friendly_error(e)}")
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] relogin 命令失败")
            yield event.plain_result(f"❌ 重新登录失败：{self._friendly_error(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("status")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_status(self, event: AstrMessageEvent):
        """查看 CLI 版本、登录状态与当前上传目标。"""
        try:
            yield event.plain_result("⏳ 正在检测 CLI 状态…")
            binary = await self.cli_manager.ensure()
            version_payload = await self.cli.run_json(["version"], ensure=False)
            version_data = version_payload.get("data", version_payload)
            version = ""
            if isinstance(version_data, dict):
                version = str(version_data.get("version") or "")
            status = await self.account.login_status()

            lines = [
                f"CLI 模式：{'自动托管' if self.cfg.cli_managed else '外部命令'}",
                f"CLI 路径：{binary}",
                f"CLI 版本：{version or '未知'}",
                f"登录状态：{status.message}",
                "上传目标：",
                f"  频道 guild_id：{self.cfg.target_guild_id or '未设置'}",
                f"  版块 channel_id：{self.cfg.target_channel_id or '未设置'}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] status 命令失败")
            yield event.plain_result(f"❌ 状态查询失败：{self._friendly_error(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("guilds")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_guilds(self, event: AstrMessageEvent):
        """列出当前 CLI 账号已加入的频道（含 ID）。"""
        try:
            yield event.plain_result("⏳ 正在拉取频道列表…")
            grouped = await self.account.list_guilds()
            if not grouped or all(not rows for rows in grouped.values()):
                yield event.plain_result("当前账号还没有加入任何频道。")
                return

            lines: list[str] = []
            for role, rows in grouped.items():
                if not rows:
                    continue
                lines.append(f"【{role}】({len(rows)})")
                lines.extend(f"{idx}. {row.to_line()}" for idx, row in enumerate(rows, 1))
            yield event.plain_result("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] guilds 命令失败")
            yield event.plain_result(f"❌ 频道列表获取失败：{self._friendly_error(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("channels")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_channels(self, event: AstrMessageEvent, guild_id: str):
        """列出指定频道下的版块（含 ID），用法：/v2c channels <频道ID>。"""
        try:
            yield event.plain_result("⏳ 正在拉取版块列表…")
            rows = await self.account.list_channels(guild_id.strip())
            if not rows:
                yield event.plain_result(f"频道 {guild_id} 下没有找到版块。")
                return
            lines = [f"频道 {guild_id} 的版块："]
            lines.extend(f"{idx}. {row.to_line()}" for idx, row in enumerate(rows, 1))
            yield event.plain_result("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] channels 命令失败")
            yield event.plain_result(f"❌ 版块列表获取失败：{self._friendly_error(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("target")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_target(self, event: AstrMessageEvent, guild_id: str, channel_id: str):
        """设置上传目标频道与版块：/v2c target <频道ID> <版块ID>。"""
        guild_id = guild_id.strip()
        channel_id = channel_id.strip()
        try:
            if not guild_id or not channel_id:
                yield event.plain_result("用法：/v2c target <频道ID> <版块ID>")
                return
            self.cfg.update_target(guild_id, channel_id)
            yield event.plain_result(
                f"✅ 已保存上传目标：\n频道 guild_id：{guild_id}\n版块 channel_id：{channel_id}"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] target 命令失败")
            yield event.plain_result(f"❌ 设置失败：{self._friendly_error(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("logout")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_logout(self, event: AstrMessageEvent):
        """清除本地 CLI 登录凭证。"""
        umo = event.unified_msg_origin
        try:
            yield event.plain_result("⏳ 正在清除登录凭证…")
            # 登出后旧二维码轮询已无意义，取消避免稍后误报“登录成功”
            old = self._login_tasks.get(umo)
            if old and not old.done():
                old.cancel()
            message = await self.account.logout()
            yield event.plain_result(f"✅ {message}")
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] logout 命令失败")
            yield event.plain_result(f"❌ 登出失败：{self._friendly_error(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(ADMIN_ONLY)
    @v2c.command("sid")
    @filter.event_message_type(PRIVATE_ONLY, priority=100)
    async def v2c_sid(self, event: AstrMessageEvent):
        """查看当前会话 ID，用于填写 session_whitelist。"""
        try:
            yield event.plain_result(
                f"当前会话 ID：{event.unified_msg_origin}\n"
                "请把它加入插件配置的 session_whitelist。"
            )
        finally:
            event.stop_event()

    # 兼容旧版单条指令
    @filter.permission_type(ADMIN_ONLY)
    @filter.command("v2c_sid", priority=100)
    async def v2c_sid_legacy(self, event: AstrMessageEvent):
        """查看当前会话 ID（旧版指令，推荐使用 /v2c sid）。"""
        try:
            yield event.plain_result(
                f"当前会话 ID：{event.unified_msg_origin}\n"
                "请把它加入插件配置的 session_whitelist。"
            )
        finally:
            event.stop_event()

    # ==================================================================
    # 登录轮询
    # ==================================================================
    def _start_login_poll(self, umo: str, qr: QrLoginInfo) -> None:
        old = self._login_tasks.get(umo)
        if old and not old.done():
            old.cancel()

        task = asyncio.create_task(self._poll_login_loop(umo, qr))
        self._login_tasks[umo] = task

        def _done(_task: asyncio.Task) -> None:
            if self._login_tasks.get(umo) is _task:
                self._login_tasks.pop(umo, None)

        task.add_done_callback(_done)

    async def _poll_login_loop(self, umo: str, qr: QrLoginInfo) -> None:
        """自动轮询 login poll-token，直到登录成功/超时/二维码失效。"""
        deadline = time.monotonic() + max(30, qr.expires_in_s)
        last_message = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # 单次轮询必须自己带上限：poll-token 可能是长轮询，
                # 沿用 cli_timeout（默认 600s）会让一次轮询就跨过二维码有效期，
                # 上面的 deadline 和 qr.interval 全部形同虚设。
                result = await self.account.poll_login(
                    timeout=max(10, min(int(remaining) + 5, self.cfg.cli_timeout))
                )
                if result.expired:
                    await self._send(
                        umo,
                        f"❌ 登录二维码已失效：{result.message}\n请重新发送 /v2c login",
                    )
                    return
                if result.authorized:
                    text = "✅ 登录成功！"
                    if result.message and result.message != "authorized":
                        text += f"\n{result.message}"
                    await self._send(umo, text)
                    return
                last_message = result.message or last_message
            except CliTimeoutError as e:
                # 只是这一轮没问出结果，登录流程仍然有效：继续下一轮
                logger.debug(f"[v2c] 轮询登录状态超时（继续）: {e}")
            except CliError as e:
                last_message = str(e)
                if is_qr_expired_text(last_message):
                    await self._send(
                        umo, f"❌ 登录二维码已失效：{last_message}\n请重新发送 /v2c login"
                    )
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[v2c] 轮询登录状态异常（继续）: {e}")
                last_message = str(e)
            # RT-04：sleep 受剩余有效期约束，避免 interval 过大导致几乎不轮询
            await asyncio.sleep(min(qr.interval, max(0.0, remaining)))

        tail = f"\n{last_message}" if last_message else ""
        await self._send(umo, f"⏰ 登录超时，未检测到扫码完成{tail}\n请重新发送 /v2c login")

    # ==================================================================
    # 工具
    # ==================================================================
    def _friendly_error(self, e: Exception) -> str:
        """把 CLI 错误转成用户可读提示。"""
        if isinstance(e, AlreadyLoggedInError):
            return (
                "当前已登录腾讯频道 CLI。如需重新登录：先 /v2c logout，"
                "或直接 /v2c relogin。"
            )
        # D5：超时/结果无法核对 != 失败。误导用户重发 = 重复投稿。
        if isinstance(e, CliTimeoutError):
            return (
                "上传超时，结果未知：帖子可能已经发布成功。"
                "请先去目标频道确认，确认前不要重发同一链接，以免重复发帖。"
            )
        if isinstance(e, PublishResultUnknownError):
            return (
                "CLI 已结束但没有返回可核对的结果，无法确认是否发布成功。"
                "请先去目标频道确认，确认前不要重发同一链接。"
            )
        text = str(e)
        if "8011" in text or "未登录" in text or "not logged" in text.lower():
            return "尚未登录腾讯频道 CLI，请先私聊发送 /v2c login"
        return text

    async def _send(self, umo: str, text: str) -> None:
        """向会话发送纯文本消息（用于后台任务回执）。"""
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[v2c] 回执消息发送失败: {e}")
