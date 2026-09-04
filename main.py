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

from .core.config import PluginConfig
from .core.download import Downloader
from .service.channel_uploader import ChannelUploader
from .service.cli_account import CliAccount, QrLoginInfo
from .service.cli_binary import CliBinaryManager
from .service.cli_runner import AlreadyLoggedInError, CliError, CliRunner
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

    async def initialize(self):
        """插件加载/重载时初始化解析器。"""
        self.router.initialize()
        missing = self.uploader.describe_missing()
        if missing:
            logger.warning("[v2c] 上传配置不完整: " + "；".join(missing))

    async def terminate(self):
        """插件卸载/停用时释放资源。"""
        for task in self._login_tasks.values():
            task.cancel()
        self._login_tasks.clear()
        await self.router.close()
        await self.downloader.close()

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
        if not text:
            return
        # 不处理指令文本，避免把 /v2c 等命令内容也当作链接
        if text.startswith("/"):
            return

        # 不处理机器人自己发出的消息（某些平台会回推）
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:  # noqa: BLE001
            pass

        if not self.router.patterns or not self.pipeline.has_supported_link(text):
            return

        asyncio.create_task(self._background_handle(umo, text))
        yield event.plain_result("⏳ 检测到视频分享链接，开始解析并上传到腾讯频道…")

    async def _background_handle(self, umo: str, text: str):
        """后台任务：完整搬运并回执结果。"""
        try:
            result = await self.pipeline.process(umo, text)
            if result is None:
                return
            share_url = result.publish.share_url
            msg = f"✅ 已上传《{result.title}》（{result.platform_name}）到腾讯频道指定版块"
            if share_url:
                msg += f"\n分享链接: {share_url}"
            await self._send(umo, msg)
        except Exception as e:  # noqa: BLE001
            logger.exception("[v2c] 视频搬运失败")
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
        try:
            yield event.plain_result("⏳ 正在清除登录凭证…")
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
        while time.monotonic() < deadline:
            try:
                result = await self.account.poll_login()
                if result.authorized:
                    text = "✅ 登录成功！"
                    if result.message and result.message != "authorized":
                        text += f"\n{result.message}"
                    await self._send(umo, text)
                    return
                last_message = result.message or last_message
            except CliError as e:
                last_message = str(e)
                if "过期" in last_message or "已被领取" in last_message:
                    await self._send(
                        umo, f"❌ 登录二维码已失效：{last_message}\n请重新发送 /v2c login"
                    )
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[v2c] 轮询登录状态异常（继续）: {e}")
                last_message = str(e)
            await asyncio.sleep(qr.interval)

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
