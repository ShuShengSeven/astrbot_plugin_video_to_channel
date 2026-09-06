"""服务层单元测试（不依赖真实 AstrBot / 真实 CLI / 网络）。

通过注入最小 astrbot/aiohttp stub + 一个返回固定 JSON 的 fake CLI，
覆盖 CLI 托管、登录二维码、轮询、频道/版块解析、目标配置写回与错误归一化。
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import tempfile
import types
import unittest
import unittest.mock as mock
from pathlib import Path

# ----------------------------------------------------------------------
# 1. 安装最小依赖 stub（必须在导入插件模块之前）
# ----------------------------------------------------------------------
def _cleanup_test_data_root() -> None:
    """进程退出时删除本次测试的私有 data 根，避免 /tmp 无限堆积。"""
    import shutil

    if _TEST_DATA_ROOT is not None:
        shutil.rmtree(_TEST_DATA_ROOT, ignore_errors=True)


_TEST_DATA_ROOT: Path | None = None


def _install_stubs():
    global _TEST_DATA_ROOT

    class _Logger:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass

    class AstrBotConfig(dict):
        pass

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = AstrBotConfig
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    # 每次测试进程使用独立的全新 data 根：若把 data 根固定为某个共享临时路径，
    # 上一次运行遗留的 unknown_submits.json（未来时间戳 + >=300s 冷却）会污染本次
    # 运行的固定 plugin_name 目录，导致连续运行同一测试文件时出现不稳定失败。
    # tempfile.mkdtemp() 保证进程内唯一；进程退出时由 atexit 清理，避免 /tmp 堆积。
    _TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="v2c-test-data-"))
    atexit.register(_cleanup_test_data_root)
    path_mod._DATA = _TEST_DATA_ROOT
    path_mod.get_astrbot_data_path = lambda: str(_TEST_DATA_ROOT)
    utils.astrbot_path = path_mod
    core.utils = utils
    astrbot.api = api
    astrbot.core = core
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.utils"] = utils
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod

    # --- main.py 所需的最小事件/指令框架桩（让入口层可被单测覆盖） ---
    class EventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    class PermissionType:
        ADMIN = "admin"
        SUPER_ADMIN = "super_admin"

    # 注意：class 体不会捕获外层函数局部名，所以这里只能逐个属性赋值，
    # 不能写成 `EventMessageType = EventMessageType`。
    def _identity_decorator(*_a, **_k):
        return lambda func: func

    class _CommandGroup:
        """AstrBot 的 command_group 既是装饰器、又提供 .command 子命令注册。"""

        def __call__(self, func):
            # 真实实现里返回自身，因此 `def v2c` 这个变量本身就是命令组，
            # 后续才能写 @v2c.command("login")
            return self

        def command(self, *_arg, **_kw):
            return lambda func: func

    def _command_group(*_a, **_k):
        return _CommandGroup()

    _FilterNS = types.SimpleNamespace(
        EventMessageType=EventMessageType,
        PermissionType=PermissionType,
        event_message_type=_identity_decorator,
        permission_type=_identity_decorator,
        command=_identity_decorator,
        command_group=_command_group,
    )

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def message(self, text):
            self.chain.append(text)
            return self

    class AstrMessageEvent:
        def __init__(self, umo="", text="", segments=None, sender=None, me=None):
            self.unified_msg_origin = umo
            self.message_str = text
            self._segments = segments if segments is not None else []
            self._sender = sender
            self._me = me

        def get_messages(self):
            return self._segments

        def get_sender_id(self):
            return self._sender

        def get_self_id(self):
            return self._me

        def plain_result(self, text):
            return MessageChain([text])

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = _FilterNS
    event_mod.MessageChain = MessageChain
    event_mod.AstrMessageEvent = AstrMessageEvent
    api.event = event_mod
    sys.modules["astrbot.api.event"] = event_mod

    class Context:
        def __init__(self):
            self.sent = []

        async def send_message(self, umo, chain):
            self.sent.append((umo, chain))

    class Star:
        def __init__(self, context=None):
            self.context = context

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = Context
    star_mod.Star = Star
    sys.modules["astrbot.api.star"] = star_mod
    api.star = star_mod

    components = types.ModuleType("astrbot.core.message.components")

    class Json:
        def __init__(self, data=None, **kw):
            self.data = data

    components.Json = Json
    sys.modules["astrbot.core.message.components"] = components
    core.components = components
    message_mod = types.ModuleType("astrbot.core.message")
    message_mod.components = components
    sys.modules["astrbot.core.message"] = message_mod
    core.message = message_mod

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = lambda *a, **k: object()

    class _FakeClientSession:
        """够用的 aiohttp 会话桩：让 Downloader 能被真实实例化。"""

        def __init__(self, *a, **k):
            self.closed = False

        async def close(self):
            self.closed = True

    aiohttp.ClientSession = _FakeClientSession
    aiohttp.ClientError = RuntimeError
    sys.modules["aiohttp"] = aiohttp

    # 以下仅用于让核心解析器/下载器模块可被导入（测试不真正联网）
    sys.modules.setdefault("aiofiles", types.ModuleType("aiofiles"))
    sys.modules.setdefault("yt_dlp", types.ModuleType("yt_dlp"))
    sys.modules.setdefault("tqdm", types.ModuleType("tqdm"))
    tqdm_async = types.ModuleType("tqdm.asyncio")
    tqdm_async.tqdm = lambda *a, **k: None
    sys.modules["tqdm.asyncio"] = tqdm_async

    msgspec = types.ModuleType("msgspec")
    msgspec.Struct = type("Struct", (), {})
    msgspec.field = lambda *a, **k: None
    msgspec.convert = lambda *a, **k: None
    msgspec_json = types.ModuleType("msgspec.json")
    msgspec_json.decode = lambda *a, **k: None
    msgspec.json = msgspec_json
    sys.modules["msgspec"] = msgspec
    sys.modules["msgspec.json"] = msgspec_json

    bili = types.ModuleType("bilibili_api")
    bili.request_settings = type("_RS", (), {"set": staticmethod(lambda *a, **k: None)})()
    bili.select_client = lambda *a, **k: None
    bili.Credential = type("Credential", (), {"from_cookies": staticmethod(lambda *a, **k: None)})
    sys.modules["bilibili_api"] = bili
    sys.modules["bilibili_api.opus"] = types.ModuleType("bilibili_api.opus")
    sys.modules["bilibili_api.opus"].Opus = type("Opus", (), {})
    sys.modules["bilibili_api.login_v2"] = types.ModuleType("bilibili_api.login_v2")
    sys.modules["bilibili_api.login_v2"].QrCodeLogin = type("QrCodeLogin", (), {})
    sys.modules["bilibili_api.login_v2"].QrCodeLoginEvents = type("QrCodeLoginEvents", (), {})
    bili_video = types.ModuleType("bilibili_api.video")
    bili_video.Video = type("Video", (), {})
    bili_video.VideoCodecs = type("VideoCodecs", (), {"AVC": "avc", "HEVC": "hevc", "AV1": "av1"})
    bili_video.VideoQuality = type("VideoQuality", (), {"_360P": 1, "_480P": 2, "_720P": 3, "_1080P": 4, "_4K": 5})
    bili_video.AudioStreamDownloadURL = type("AudioStreamDownloadURL", (), {})
    bili_video.MP4StreamDownloadURL = type("MP4StreamDownloadURL", (), {})
    bili_video.VideoStreamDownloadURL = type("VideoStreamDownloadURL", (), {})
    bili_video.VideoDownloadURLDataDetecter = type("VideoDownloadURLDataDetecter", (), {})
    sys.modules["bilibili_api.video"] = bili_video

_install_stubs()

# 部分沙箱/CI 环境无法真正调度线程（asyncio.to_thread 会挂起）。
# 测试进程内用内联执行替代，仅影响本测试进程；生产代码仍使用真实线程池。
async def _inline_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


asyncio.to_thread = _inline_to_thread

# ----------------------------------------------------------------------
# 2. 导入被测模块
# ----------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(__file__).resolve().parents[2]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_video_to_channel.core.config import PluginConfig
from astrbot_plugin_video_to_channel.service.cli_account import (
    CliAccount,
    GuildSummary,
    ChannelSummary,
)
from astrbot_plugin_video_to_channel.service.cli_binary import (
    CliBinaryManager,
    DownloadInfo,
)
from astrbot_plugin_video_to_channel.service.cli_runner import (
    AlreadyLoggedInError,
    CliError,
    CliRunner,
)
from astrbot_plugin_video_to_channel.service.channel_uploader import ChannelUploader

FAKE_CLI_TEMPLATE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["sleep"]:
    time.sleep(5)

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)

if args[:1] == ["version"]:
    emit({"data": {"version": "9.9.9"}, "success": True})

if args[:2] == ["login", "logout"]:
    emit({"data": {"message": "已清除登录凭证"}, "success": True})

if args[:2] == ["login", "status"]:
    mode = os.environ.get("FAKE_LOGIN_STATUS", "")
    if mode == "text_logged":
        emit({"data": {"message": "已登录，服务连通正常。", "connectivity": "ok"}, "success": True})
    if mode == "error_logged":
        emit({"error": {"message": "当前已登录，如需重新登录请添加 --yes 参数", "type": "internal"}, "success": False})
    if os.environ.get("FAKE_LOGGED") == "1":
        emit({"data": {"logged_in": True, "connectivity": "ok"}, "success": True})
    emit({"data": {"logged_in": False, "message": "未登录"}, "success": True})

if args[:1] == ["login"] and "poll-token" not in args and "--json" in args:
    if os.environ.get("FAKE_QR_ALREADY") == "1" or (
        os.environ.get("FAKE_RELOGIN") == "1" and "--yes" not in args
    ):
        sys.stderr.write(json.dumps(
            {"error": {"message": "当前已登录，如需重新登录请添加 --yes 参数", "type": "internal"}, "success": False},
            ensure_ascii=False,
        ))
        sys.exit(5)
    # 生成二维码：--qrcode-path <path>
    try:
        idx = args.index("--qrcode-path")
        if not os.environ.get("FAKE_QR_SKIP"):
            Path(args[idx + 1]).write_bytes(b"FAKE_QR")
    except Exception:
        pass
    emit({"data": {"verification_uri": "https://example.com/auth", "expires_in_s": 60, "interval": 1}, "success": True})

if args[:2] == ["login", "poll-token"]:
    mode = os.environ.get("FAKE_POLL", "authorized")
    if mode == "waiting":
        emit({"data": {"status": "scanning"}, "success": True})
    if mode == "expired":
        emit({"data": {"retCode": 1, "message": "二维码已过期"}, "success": False})
    if mode == "negative":
        emit({"data": {"status": "pending", "message": "token 未过期，请继续等待"}, "success": True})
    emit({"data": {"status": "authorized", "message": "扫码成功"}, "success": True})

if args[:2] == ["manage", "get-my-join-guild-info"]:
    if os.environ.get("FAKE_ERROR_8011") == "1":
        emit({"data": {"retCode": 8011, "message": "未登录"}, "success": False})
    emit({"data": {
        "created_guilds": [],
        "managed_guilds": [],
        "joined_guilds": [{"guild_id": "111", "guild_name": "测试频道", "guild_number": "pd123", "member_count": 10}],
    }, "success": True})

if args[:2] == ["manage", "get-guild-channel-list"]:
    emit({"data": {"channels": [
        {"channel_id": "222", "channel_name": "灌水区"},
        {"channel_id": "333", "channel_name": "视频区"},
    ]}, "success": True})

if args[:2] == ["feed", "publish-feed"]:
    emit({"data": {"feed_id": "F1", "share_url": "https://pd.qq.com/s/abc"}, "success": True})

emit({"data": {"retCode": 8011, "message": "未登录"}, "success": False})
'''


def _write_fake_cli(tmp: Path) -> Path:
    exe = tmp / "fake_tencent_channel_cli"
    exe.write_text(FAKE_CLI_TEMPLATE, encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _make_cfg(cli_command: str, tmp: Path) -> PluginConfig:
    raw = {
        "session_whitelist": [],
        "cli_command": cli_command,
        "target_guild_id": "",
        "target_channel_id": "",
        "download": {},
        "parsers": {},
    }
    return PluginConfig(raw, plugin_name="astrbot_plugin_video_to_channel_test")


class CliServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v2c-test-"))
        self.fake = _write_fake_cli(self.tmp)
        self.cfg = _make_cfg(str(self.fake), self.tmp)
        self.manager = CliBinaryManager(self.cfg)
        self.runner = CliRunner(self.cfg, self.manager)
        self.account = CliAccount(self.cfg, self.runner)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------- CLI 自托管 ----------------
    def test_platform_key_supported(self):
        from astrbot_plugin_video_to_channel.service.cli_binary import detect_platform_key
        allowed = {"linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64"}
        self.assertIn(detect_platform_key(), allowed)

    def test_managed_mode_auto_download(self):
        cfg = _make_cfg("auto", self.tmp)
        manager = CliBinaryManager(cfg)
        # stub 数据目录是全局共享的，先清理上次测试可能留下的托管二进制
        if manager.managed_bin_path.exists():
            manager.managed_bin_path.unlink()
        info = DownloadInfo(version="1.0.10", platform_key="test", package="pkg", tarball_url="")
        calls = []

        async def fake_fetch():
            return info

        async def fake_download(_info):
            calls.append(_info)
            manager.managed_bin_path.parent.mkdir(parents=True, exist_ok=True)
            manager.managed_bin_path.write_bytes(b"BIN")

        with mock.patch.object(manager, "_fetch_download_info", fake_fetch), \
             mock.patch.object(manager, "_download", fake_download):
            path = asyncio.run(manager.ensure())
        self.assertTrue(Path(path).is_file())
        self.assertEqual(len(calls), 1)
        # 已存在时不应重复下载
        asyncio.run(manager.ensure())
        self.assertEqual(len(calls), 1)

    # ---------------- 登录 ----------------
    def test_qr_login_parses_and_writes_qrcode(self):
        qr = asyncio.run(self.account.qr_login())
        self.assertEqual(qr.verification_uri, "https://example.com/auth")
        self.assertTrue(qr.qrcode_path.exists())
        self.assertEqual(qr.expires_in_s, 60)

    def test_poll_login_authorized(self):
        result = asyncio.run(self.account.poll_login())
        self.assertTrue(result.authorized)

    def test_poll_login_waiting(self):
        os.environ["FAKE_POLL"] = "waiting"
        try:
            result = asyncio.run(self.account.poll_login())
            self.assertFalse(result.authorized)
        finally:
            os.environ.pop("FAKE_POLL", None)

    def test_poll_login_expired(self):
        os.environ["FAKE_POLL"] = "expired"
        try:
            result = asyncio.run(self.account.poll_login())
            self.assertFalse(result.authorized)
            self.assertTrue(result.expired)
            self.assertIn("过期", result.message)
        finally:
            os.environ.pop("FAKE_POLL", None)

    def test_login_status_logged_out(self):
        status = asyncio.run(self.account.login_status())
        self.assertFalse(status.logged_in)

    def test_login_status_logged_in(self):
        os.environ["FAKE_LOGGED"] = "1"
        try:
            status = asyncio.run(self.account.login_status())
            self.assertTrue(status.logged_in)
        finally:
            os.environ.pop("FAKE_LOGGED", None)

    def test_login_status_text_logged_in(self):
        """真实 CLI 仅以中文 message 表达登录态时也应识别为已登录。"""
        os.environ["FAKE_LOGIN_STATUS"] = "text_logged"
        try:
            status = asyncio.run(self.account.login_status())
            self.assertTrue(status.logged_in)
            self.assertIn("已登录", status.message)
        finally:
            os.environ.pop("FAKE_LOGIN_STATUS", None)

    def test_login_status_error_logged_in(self):
        """success:false + error.message=当前已登录… 同样应判定已登录且消息可读。"""
        os.environ["FAKE_LOGIN_STATUS"] = "error_logged"
        try:
            status = asyncio.run(self.account.login_status())
            self.assertTrue(status.logged_in)
            self.assertIn("当前已登录", status.message)
            self.assertNotIn("退出码", status.message)
        finally:
            os.environ.pop("FAKE_LOGIN_STATUS", None)

    def test_qr_login_already_logged_in_raises(self):
        """已登录时申请新码（退出码 5 + 当前已登录）应抛 AlreadyLoggedInError。"""
        os.environ["FAKE_QR_ALREADY"] = "1"
        try:
            with self.assertRaises(AlreadyLoggedInError):
                asyncio.run(self.account.qr_login())
        finally:
            os.environ.pop("FAKE_QR_ALREADY", None)

    def test_qr_login_relogin_passes_yes(self):
        """relogin=True 时应向 CLI 附加 --yes 并正常拿到二维码。"""
        os.environ["FAKE_RELOGIN"] = "1"
        try:
            qr = asyncio.run(self.account.qr_login(relogin=True))
            self.assertEqual(qr.verification_uri, "https://example.com/auth")
            self.assertTrue(qr.qrcode_path.exists())
        finally:
            os.environ.pop("FAKE_RELOGIN", None)

    # ---------------- 频道/版块 ----------------
    def test_list_guilds_parses(self):
        grouped = asyncio.run(self.account.list_guilds())
        rows = grouped.get("我加入的", [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsInstance(row, GuildSummary)
        self.assertEqual(row.guild_id, "111")
        self.assertEqual(row.name, "测试频道")
        self.assertIn("111", row.to_line())

    def test_list_channels_parses(self):
        rows = asyncio.run(self.account.list_channels("111"))
        self.assertEqual(len(rows), 2)
        self.assertIsInstance(rows[0], ChannelSummary)
        self.assertEqual(rows[0].channel_id, "222")
        self.assertIn("222", rows[0].to_line())

    # ---------------- 错误归一化 ----------------
    def test_retcode_8011_raises_cli_error(self):
        os.environ["FAKE_ERROR_8011"] = "1"
        try:
            with self.assertRaises(CliError) as ctx:
                asyncio.run(self.runner.run_json(["manage", "get-my-join-guild-info", "--json"]))
            self.assertIn("8011", str(ctx.exception))
            self.assertIn("未登录", str(ctx.exception))
        finally:
            os.environ.pop("FAKE_ERROR_8011", None)

    # ---------------- 上传 ----------------
    def test_uploader_uses_runner(self):
        uploader = ChannelUploader(self.cfg, self.runner)
        video = self.tmp / "video.mp4"
        video.write_bytes(b"FAKE_VIDEO")
        result = asyncio.run(uploader.publish_video(video, content="测试标题"))
        self.assertEqual(result.feed_id, "F1")
        self.assertEqual(result.share_url, "https://pd.qq.com/s/abc")


class ConfigTests(unittest.TestCase):
    def test_cli_command_auto_means_managed(self):
        class FakeAstrBotConfig(dict):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.saved = 0

            def save_config(self):
                self.saved += 1

        raw = FakeAstrBotConfig({
            "cli_command": "auto", "download": {}, "parsers": {},
        })
        cfg = PluginConfig(raw, plugin_name="astrbot_plugin_video_to_channel_test")
        self.assertTrue(cfg.cli_managed)
        self.assertEqual(cfg.cli_command, "auto")

    def test_kuaishou_registered_in_config(self):
        from astrbot_plugin_video_to_channel.core.config import (
            SUPPORTED_PLATFORMS,
            _PARSER_DEFAULTS,
        )

        self.assertIn("kuaishou", SUPPORTED_PLATFORMS)
        self.assertIn("kuaishou", _PARSER_DEFAULTS)
        self.assertIs(_PARSER_DEFAULTS["kuaishou"]["enable"], True)
        self.assertEqual(_PARSER_DEFAULTS["kuaishou"]["cookies"], "")

    def test_update_target_persists(self):
        class FakeAstrBotConfig(dict):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.saved = 0

            def save_config(self):
                self.saved += 1

        raw = FakeAstrBotConfig({
            "cli_command": "auto", "download": {}, "parsers": {},
        })
        cfg = PluginConfig(raw, plugin_name="astrbot_plugin_video_to_channel_test")
        cfg.update_target("111", "222")
        self.assertEqual(cfg.target_guild_id, "111")
        self.assertEqual(raw["target_channel_id"], "222")
        self.assertEqual(raw.saved, 1)


class DebouncerTests(unittest.TestCase):
    """防抖：语义保持 + 记录数有界。"""

    def test_window_disabled_never_hit(self):
        from astrbot_plugin_video_to_channel.service.debounce import Debouncer

        d = Debouncer(0)
        self.assertFalse(d.hit("https://a"))
        self.assertFalse(d.hit("https://a"))
        self.assertEqual(len(d._records), 0)

    def test_global_dedupe_across_sessions(self):
        from astrbot_plugin_video_to_channel.service.debounce import Debouncer

        d = Debouncer(60)
        self.assertFalse(d.hit("https://same"))
        # 不同会话/来源的同一链接也应命中同一防抖窗口
        self.assertTrue(d.hit("https://same"))

    def test_records_bounded(self):
        from astrbot_plugin_video_to_channel.service.debounce import Debouncer

        d = Debouncer(3600, max_records=32)
        for i in range(100):
            self.assertFalse(d.hit(f"https://link/{i}"))
        self.assertLessEqual(len(d._records), 32)
        # 仍在记录内（最近插入）的链接继续命中
        self.assertTrue(d.hit("https://link/99"))



class UtilsTests(unittest.TestCase):
    """核心工具函数的轻量回归测试。"""

    def test_extract_json_url_meta(self):
        from astrbot_plugin_video_to_channel.core.utils import extract_json_url

        data = {
            "meta": {
                "detail_1": {
                    "qqdocurl": "https://v.douyin.com/AbC123/",
                }
            }
        }
        self.assertEqual(extract_json_url(data), "https://v.douyin.com/AbC123/")

    def test_extract_json_url_none_for_empty(self):
        from astrbot_plugin_video_to_channel.core.utils import extract_json_url

        self.assertIsNone(extract_json_url({"meta": {}}))


    def test_douyin_no_bare_id_fallback(self):
        from astrbot_plugin_video_to_channel.core.parsers import DouyinParser

        keywords = [kw for kw, _ in DouyinParser._key_patterns]
        self.assertNotIn("", keywords)
        # 仍保留带 URL 形态的匹配
        self.assertIn("douyin", keywords)
        self.assertIn("iesdouyin", keywords)



class PipelineCleanupTests(unittest.TestCase):
    """流水线无论上传成功/失败都应清理已下载的本地视频。"""

    def _make_cfg(self):
        from astrbot_plugin_video_to_channel.core.config import PluginConfig

        raw = {
            "session_whitelist": [],
            "cli_command": "auto",
            "target_guild_id": "111",
            "target_channel_id": "222",
            "download": {},
            "parsers": {},
            "max_concurrent": 2,
            "debounce_seconds": 0,
        }
        # 递增 plugin_name => 每次独立 data_dir，避免 UNKNOWN 持久化/防抖跨用例污染
        self.__class__._pipeline_cfg_seq = getattr(self.__class__, "_pipeline_cfg_seq", 0) + 1
        cfg = PluginConfig(
            raw,
            plugin_name=f"v2c_pipeline_test_{self.__class__._pipeline_cfg_seq}",
        )
        return cfg

    def test_failed_upload_cleans_video(self):
        import asyncio

        from astrbot_plugin_video_to_channel.core.data import ParseResult, Platform, VideoContent
        from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

        cfg = self._make_cfg()
        video_path = cfg.cache_dir / "pipeline_fail_video.mp4"
        video_path.write_bytes(b"FAKE_VIDEO")

        class _Match:
            def group(self, *a):
                return "https://example.com/v"

        class _Parser:
            platform = Platform(name="fake", display_name="测试")
            async def parse(self, keyword, searched):
                return ParseResult(
                    platform=self.platform,
                    title="测试标题",
                    contents=[VideoContent(video_path)],
                )

        parser = _Parser()

        class _Router:
            def match(self, text):
                return (parser, "fake", _Match())

        class _Uploader:
            def describe_missing(self):
                return []
            async def publish_video(self, video_path, content=""):
                raise RuntimeError("模拟上传失败")

        pipeline = VideoPipeline(cfg, _Router(), _Uploader())
        with self.assertRaises(RuntimeError):
            asyncio.run(pipeline.process("https://example.com/v"))
        self.assertFalse(video_path.exists(), "上传失败后本地视频应被清理")

    def test_success_upload_cleans_video(self):
        import asyncio

        from astrbot_plugin_video_to_channel.core.data import ParseResult, Platform, VideoContent
        from astrbot_plugin_video_to_channel.service.channel_uploader import PublishResult
        from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

        cfg = self._make_cfg()
        video_path = cfg.cache_dir / "pipeline_ok_video.mp4"
        video_path.write_bytes(b"FAKE_VIDEO")

        class _Match:
            def group(self, *a):
                return "https://example.com/v"

        class _Parser:
            platform = Platform(name="fake", display_name="测试")
            async def parse(self, keyword, searched):
                return ParseResult(
                    platform=self.platform,
                    title="测试标题",
                    contents=[VideoContent(video_path)],
                )

        parser = _Parser()

        class _Router:
            def match(self, text):
                return (parser, "fake", _Match())

        class _Uploader:
            def describe_missing(self):
                return []
            async def publish_video(self, video_path, content=""):
                return PublishResult(raw={}, feed_id="F1", share_url="https://pd.qq.com/s/1")

        pipeline = VideoPipeline(cfg, _Router(), _Uploader())
        result = asyncio.run(pipeline.process("https://example.com/v"))
        self.assertIsNotNone(result)
        self.assertEqual(result.publish.feed_id, "F1")
        self.assertFalse(video_path.exists(), "上传成功后本地视频应被清理")


    def test_inflight_duplicate_skipped(self):
        import asyncio

        from astrbot_plugin_video_to_channel.core.data import ParseResult, Platform, VideoContent
        from astrbot_plugin_video_to_channel.service.channel_uploader import PublishResult
        from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

        cfg = self._make_cfg()
        cfg._raw["debounce_seconds"] = 0
        cfg.debounce_seconds = 0
        video_path = cfg.cache_dir / "pipeline_inflight_video.mp4"
        video_path.write_bytes(b"FAKE_VIDEO")
        started = asyncio.Event()
        release = asyncio.Event()

        class _Parser:
            platform = Platform(name="fake", display_name="测试")
            async def parse(self, keyword, searched):
                started.set()
                await release.wait()
                return ParseResult(
                    platform=self.platform,
                    title="测试标题",
                    contents=[VideoContent(video_path)],
                )

        parser = _Parser()

        class _Router:
            def match(self, text):
                return (parser, "fake", type("_M", (), {"group": lambda self, *a: text})())

        class _Uploader:
            def describe_missing(self):
                return []
            async def publish_video(self, video_path, content=""):
                return PublishResult(raw={}, feed_id="F1", share_url="https://pd.qq.com/s/1")

        async def scenario():
            pipeline = VideoPipeline(cfg, _Router(), _Uploader())
            first_task = asyncio.create_task(pipeline.process("https://example.com/v"))
            await asyncio.wait_for(started.wait(), 5)
            second = await pipeline.process("https://example.com/v")
            release.set()
            first = await first_task
            return first, second

        first, second = asyncio.run(scenario())
        self.assertIsNotNone(first)
        self.assertIsNone(second, "同一链接处理中时，后续请求应被全局去重跳过")
        self.assertFalse(video_path.exists())




# ======================================================================
# 第二轮（正式修复）回归测试
# 每一条都对应审计报告里的一个问题编号，防止退化
# ======================================================================
from http.cookies import SimpleCookie

from astrbot_plugin_video_to_channel.core.data import (
    Author,
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
)
from astrbot_plugin_video_to_channel.core.download import Downloader
from astrbot_plugin_video_to_channel.core.exception import DownloadException
from astrbot_plugin_video_to_channel.core.parsers import BilibiliParser
from astrbot_plugin_video_to_channel.core.parsers.bilibili.login import BilibiliLogin
from astrbot_plugin_video_to_channel.service.cli_account import (
    QrLoginInfo,
    PollResult,
    is_qr_expired_text,
)
from astrbot_plugin_video_to_channel.service.cli_runner import CliTimeoutError
from astrbot_plugin_video_to_channel.service.channel_uploader import (
    PublishResult,
    PublishResultUnknownError,
)
from astrbot_plugin_video_to_channel.service.parser_router import ParserRouter
from astrbot_plugin_video_to_channel.service.debounce import Debouncer
from astrbot_plugin_video_to_channel.service.pipeline import PipelineError, VideoPipeline

_NAME_SEQ = [0]


def _audit_cfg(**over):
    """独立 plugin_name => 独立 cache 目录，用例之间不共享状态。"""
    _NAME_SEQ[0] += 1
    raw = {
        "session_whitelist": [],
        "cli_command": "auto",
        "target_guild_id": "111",
        "target_channel_id": "222",
        "download": {},
        "parsers": {},
        "max_concurrent": 2,
        "debounce_seconds": 120,
    }
    raw.update(over)
    return PluginConfig(raw, plugin_name=f"v2c_audit_{_NAME_SEQ[0]}")


class _Match:
    def __init__(self, link):
        self._link = link

    def group(self, _n=0):
        return self._link


class _Router:
    def __init__(self, parser, link="https://example.com/v"):
        self.parser, self.link = parser, link

    def match(self, _text):
        return (self.parser, "fake", _Match(self.link))


class _Parser:
    platform = Platform(name="fake", display_name="测试")

    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc
        self.calls = 0

    async def parse(self, _keyword, _searched):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result() if callable(self._result) else self._result


class _Uploader:
    def __init__(self, results=(), missing=()):
        self._results = list(results)
        self._missing = list(missing)
        self.published = []

    def describe_missing(self):
        return list(self._missing)

    async def publish_video(self, video_path, content=""):
        self.published.append((video_path, content))
        item = (
            self._results.pop(0)
            if self._results
            else PublishResult(raw={}, feed_id="F1", share_url="https://pd.qq.com/s/1")
        )
        if isinstance(item, Exception):
            # 桩要模拟真实 run_json 的 definite 语义：带 payload 的“业务拒绝”应标记
            # definite=True，纯进程/通信错误 definite=False。否则测的是桩不是代码。
            if (
                isinstance(item, CliError)
                and item.definite is False
                and ("8011" in str(item) or "retCode" in str(item) or "未登录" in str(item))
            ):
                item = CliError(
                    str(item),
                    returncode=item.returncode,
                    stdout=item.stdout,
                    stderr=item.stderr,
                    payload={"data": {"retCode": 8011, "msg": "未登录"}, "success": False},
                    definite=True,
                )
            raise item
        return item


def _video_file(cfg, name="vid.mp4"):
    path = cfg.cache_dir / name
    path.write_bytes(b"FAKE")
    return path


def _result(contents, title="标题"):
    return ParseResult(
        platform=Platform(name="fake", display_name="测试"), title=title, contents=contents
    )


class DebouncerClockTests(unittest.TestCase):
    """A1：monotonic 是“开机以来的秒数”，新链接不得因时钟小而消失。"""

    def test_small_monotonic_does_not_swallow_first_link(self):
        import time as _time

        with mock.patch.object(_time, "monotonic", lambda: 5.0):
            d = Debouncer(600)
            self.assertFalse(d.hit("https://fresh"), "开机早期第一条链接必须能搬运（旧实现 KeyError）")
            self.assertTrue(d.hit("https://fresh"))

    def test_default_window_same_case(self):
        import time as _time

        with mock.patch.object(_time, "monotonic", lambda: 30.0):
            self.assertFalse(Debouncer(120).hit("https://a"))

    def test_forget_releases_record(self):
        d = Debouncer(600)
        d.hit("https://a")
        self.assertTrue(d.hit("https://a"))
        d.forget("https://a")
        self.assertFalse(d.hit("https://a"))


class ConfigSemanticsTests(unittest.TestCase):
    """A2/A3/D6/D7。"""

    def test_explicit_zero_not_replaced_by_default(self):
        cfg = _audit_cfg(debounce_seconds=0, download={"download_retry_times": 0, "common_timeout": 0})
        self.assertEqual(cfg.debounce_seconds, 0)
        self.assertEqual(cfg.download_retry_times, 0)
        self.assertEqual(cfg.common_timeout, 5, "过小值夹到下限，而不是回落到默认")

    def test_missing_field_uses_default(self):
        self.assertEqual(_audit_cfg(debounce_seconds=None).debounce_seconds, 120)

    def test_size_zero_clamped(self):
        cfg = _audit_cfg(download={"max_size_mb": 0})
        self.assertEqual(cfg.source_max_size, 1)
        self.assertGreater(cfg.max_size, 0)

    def test_garbage_values_do_not_crash(self):
        cfg = _audit_cfg(debounce_seconds="abc", download={"max_minutes": None})
        self.assertEqual(cfg.debounce_seconds, 120)
        self.assertEqual(cfg.source_max_minute, 15)

    def test_whitelist_string_normalized(self):
        cfg = _audit_cfg(session_whitelist="QQ_Group_123")
        self.assertEqual(cfg.session_whitelist, ["QQ_Group_123"])

    def test_proxy_off_by_default_for_parsers(self):
        cfg = _audit_cfg(download={"proxy": "http://127.0.0.1:7890"})
        self.assertIs(cfg.parser.bilibili.use_proxy, False)
        self.assertIsNone(BilibiliParser(cfg, object()).proxy, "未开启 use_proxy 必须直连")
        cfg2 = _audit_cfg(
            download={"proxy": "http://127.0.0.1:7890"},
            parsers={"bilibili": {"use_proxy": True}},
        )
        self.assertEqual(BilibiliParser(cfg2, object()).proxy, "http://127.0.0.1:7890")


class PipelineAckTests(unittest.TestCase):
    """B4/D4：承诺式 ACK 只能在真正开工时发出。"""

    def test_no_ack_when_skipped_by_debounce(self):
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg)
        pipeline = VideoPipeline(cfg, _Router(_Parser(_result([VideoContent(path)]))), _Uploader())
        ack = []

        async def accepted():
            ack.append(1)

        async def scenario():
            first = await pipeline.process("https://example.com/v", on_accepted=accepted)
            second = await pipeline.process("https://example.com/v", on_accepted=accepted)
            return first, second

        first, second = asyncio.run(scenario())
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(ack, [1], "被防抖跳过的请求不得再发“开始解析”")

    def test_no_ack_when_inflight_duplicate(self):
        cfg = _audit_cfg(debounce_seconds=0)
        path = _video_file(cfg)
        started, release = asyncio.Event(), asyncio.Event()

        class SlowParser(_Parser):
            async def parse(self, keyword, searched):
                started.set()
                await release.wait()
                return _result([VideoContent(path)])

        async def scenario():
            pipeline = VideoPipeline(cfg, _Router(SlowParser()), _Uploader())
            ack1, ack2 = [], []

            async def a1():
                ack1.append(1)

            async def a2():
                ack2.append(1)

            first = asyncio.create_task(
                pipeline.process("https://example.com/v", on_accepted=a1)
            )
            await asyncio.wait_for(started.wait(), 5)
            second = await pipeline.process("https://example.com/v", on_accepted=a2)
            release.set()
            await first
            return second, ack1, ack2

        second, ack1, ack2 = asyncio.run(scenario())
        self.assertIsNone(second)
        self.assertEqual(ack1, [1])
        self.assertEqual(ack2, [], "进行中去重不得先发承诺")


class PipelineDebounceOutcomeTests(unittest.TestCase):
    """B3/D5：防抖记录要与“成功/确定失败/结果未知”一致。"""

    def _twice(self, cfg, parser, uploader):
        pipeline = VideoPipeline(cfg, _Router(parser), uploader)

        async def scenario():
            first_error = None
            try:
                await pipeline.process("https://example.com/v")
            except Exception as e:  # noqa: BLE001
                first_error = e
            second = await pipeline.process("https://example.com/v")
            return first_error, second

        return asyncio.run(scenario())

    def test_parse_failure_allows_immediate_retry(self):
        cfg = _audit_cfg(debounce_seconds=600)
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("目标视频已删除")
            return _result([VideoContent(_video_file(cfg, "ok.mp4"))])

        parser = _Parser(result=flaky)
        err, second = self._twice(cfg, parser, _Uploader())
        self.assertIsInstance(err, RuntimeError)
        self.assertIsNotNone(second, "确定失败后重发必须被处理")

    def test_cli_reject_allows_retry(self):
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg)
        uploader = _Uploader(
            results=[CliError("tencent-channel-cli 返回错误：retCode=8011：未登录"),
                     PublishResult(raw={}, feed_id="F2", share_url=None)]
        )
        err, second = self._twice(cfg, _Parser(_result([VideoContent(path)])), uploader)
        self.assertIsInstance(err, CliError)
        self.assertIsNotNone(second, "被明确拒绝意味着没有帖子，应允许重试")

    def test_upload_timeout_keeps_debounce(self):
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg)
        uploader = _Uploader(results=[CliTimeoutError("执行超时")])
        err, second = self._twice(cfg, _Parser(_result([VideoContent(path)])), uploader)
        self.assertIsInstance(err, CliTimeoutError)
        self.assertIsNone(second, "超时=结果未知，必须保留防抖以免重复投稿")
        self.assertEqual(len(uploader.published), 1)

    def test_unknown_publish_result_keeps_debounce(self):
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg)
        uploader = _Uploader(results=[PublishResultUnknownError("无结果数据")])
        _err, second = self._twice(cfg, _Parser(_result([VideoContent(path)])), uploader)
        self.assertIsNone(second)

    def test_config_missing_allows_retry_after_fix(self):
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg)
        parser = _Parser(_result([VideoContent(path)]))
        pipeline = VideoPipeline(cfg, _Router(parser), _Uploader(missing=["guild_id 未配置"]))

        async def scenario():
            with self.assertRaises(PipelineError):
                await pipeline.process("https://example.com/v")
            # 用户配好目标后立刻重发
            good = VideoPipeline(cfg, _Router(parser), _Uploader())
            return await good.process("https://example.com/v")

        self.assertIsNotNone(asyncio.run(scenario()))


class PipelineMediaTests(unittest.TestCase):
    """B1/B2：不参与搬运的封面/头像/图集不得被下载或留成孤儿。"""

    def test_cover_not_downloaded_just_to_delete(self):
        cfg = _audit_cfg()
        path = _video_file(cfg)
        ran = []

        async def cover_dl():
            ran.append(1)
            return cfg.cache_dir / "cover.jpg"

        async def scenario():
            content = VideoContent(path, asyncio.ensure_future(cover_dl()))
            pipeline = VideoPipeline(cfg, _Router(_Parser(_result([content]))), _Uploader())
            return await pipeline.process("https://example.com/v"), content

        result, content = asyncio.run(scenario())
        self.assertIsNotNone(result)
        self.assertEqual(ran, [], "不得为了删除封面而先下载它")
        self.assertTrue(content.cover.cancelled())

    def test_finished_cover_still_cleaned(self):
        cfg = _audit_cfg()
        path = _video_file(cfg)
        cover = cfg.cache_dir / "cover_done.jpg"
        cover.write_bytes(b"JPG")

        async def done():
            return cover

        async def scenario():
            content = VideoContent(path, asyncio.ensure_future(done()))
            await asyncio.sleep(0.02)
            pipeline = VideoPipeline(cfg, _Router(_Parser(_result([content]))), _Uploader())
            return await pipeline.process("https://example.com/v")

        self.assertIsNotNone(asyncio.run(scenario()))
        self.assertFalse(cover.exists(), "已下完的封面仍要被清理")

    def test_non_video_rejection_cancels_pending_media(self):
        cfg = _audit_cfg()
        started = []

        async def img(url):
            started.append(url)
            await asyncio.sleep(30)
            return cfg.cache_dir / "img.jpg"

        async def scenario():
            contents = [
                ImageContent(asyncio.ensure_future(img(f"https://i/{n}"))) for n in range(3)
            ]
            avatar = asyncio.ensure_future(img("avatar"))
            result = ParseResult(
                platform=Platform(name="fake", display_name="测试"),
                title="图文作品",
                author=Author(name="UP", avatar=avatar),
                contents=contents,
            )
            pipeline = VideoPipeline(cfg, _Router(_Parser(result)), _Uploader())
            with self.assertRaises(PipelineError):
                await pipeline.process("https://example.com/v")
            await asyncio.sleep(0.05)
            return contents, avatar

        contents, avatar = asyncio.run(scenario())
        self.assertTrue(all(c.path_task.cancelled() for c in contents), "图片任务必须取消")
        self.assertTrue(avatar.cancelled(), "头像任务不得成为孤儿")
        self.assertEqual(started, [], "取消应发生在真正下载之前")

    def test_release_survives_failed_and_cancelled_tasks(self):
        cfg = _audit_cfg()
        path = _video_file(cfg)

        async def boom():
            raise RuntimeError("封面 404")

        async def never():
            await asyncio.sleep(30)

        async def scenario():
            bad = asyncio.ensure_future(boom())
            gone = asyncio.ensure_future(never())
            gone.cancel()
            content = VideoContent(path, bad)
            result = ParseResult(
                platform=Platform(name="fake", display_name="测试"),
                contents=[content, ImageContent(gone)],
                author=Author(name="x", avatar=bad),
            )
            pipeline = VideoPipeline(cfg, _Router(_Parser(result)), _Uploader())
            return await pipeline.process("https://example.com/v")

        self.assertIsNotNone(asyncio.run(scenario()))

    def test_cleanup_never_touches_outside_cache(self):
        cfg = _audit_cfg()
        outside_dir = Path(tempfile.mkdtemp(prefix="v2c-outside-"))
        outside = outside_dir / "keep.mp4"
        outside.write_bytes(b"DO NOT DELETE")

        async def scenario():
            pipeline = VideoPipeline(
                cfg, _Router(_Parser(_result([VideoContent(outside)]))), _Uploader()
            )
            return await pipeline.process("https://example.com/v")

        self.assertIsNotNone(asyncio.run(scenario()))
        self.assertTrue(outside.exists(), "缓存目录之外的文件绝不能删")


class PipelineLimitsTests(unittest.TestCase):
    """D1/D2。"""

    def test_multi_video_refused_without_uploading(self):
        cfg = _audit_cfg()
        p1, p2 = _video_file(cfg, "m1.mp4"), _video_file(cfg, "m2.mp4")
        uploader = _Uploader()
        pipeline = VideoPipeline(
            cfg, _Router(_Parser(_result([VideoContent(p1), VideoContent(p2)]))), uploader
        )
        with self.assertRaises(PipelineError) as ctx:
            asyncio.run(pipeline.process("https://example.com/v"))
        self.assertIn("多视频", str(ctx.exception))
        self.assertEqual(uploader.published, [], "不得“只传第一条然后报成功”")

    def test_duration_limit_rejects_before_download(self):
        cfg = _audit_cfg(download={"max_minutes": 15})
        touched = []

        async def slow():
            touched.append(1)
            await asyncio.sleep(30)
            return cfg.cache_dir / "long.mp4"

        async def scenario():
            content = VideoContent(asyncio.ensure_future(slow()), None, duration=1800)
            pipeline = VideoPipeline(cfg, _Router(_Parser(_result([content]))), _Uploader())
            with self.assertRaises(PipelineError) as ctx:
                await pipeline.process("https://example.com/v")
            await asyncio.sleep(0.02)
            return ctx.exception, content

        err, content = asyncio.run(scenario())
        self.assertIn("时长", str(err))
        self.assertIn("超过限制", str(err))
        self.assertEqual(touched, [], "超时长作品不应先下载再拒绝")
        self.assertTrue(content.path_task.cancelled())

    def test_zero_duration_not_rejected(self):
        cfg = _audit_cfg(download={"max_minutes": 15})
        path = _video_file(cfg)
        pipeline = VideoPipeline(
            cfg, _Router(_Parser(_result([VideoContent(path, None, 0.0)]))), _Uploader()
        )
        self.assertIsNotNone(asyncio.run(pipeline.process("https://example.com/v")))

    def test_absurd_duration_skips_check_not_platform(self):
        cfg = _audit_cfg(download={"max_minutes": 15})
        path = _video_file(cfg)
        pipeline = VideoPipeline(
            cfg, _Router(_Parser(_result([VideoContent(path, None, 999_999_999)]))), _Uploader()
        )
        self.assertIsNotNone(
            asyncio.run(pipeline.process("https://example.com/v")),
            "单位再被搞错时也不能让所有视频都被判超限",
        )

    def test_duration_units_normalized_per_platform(self):
        from astrbot_plugin_video_to_channel.core.parsers.douyin.video import Video as DYVideo
        from astrbot_plugin_video_to_channel.core.parsers.kuaishou import Photo

        # 测试桩里的 msgspec.Struct 没有生成 __init__，因此直接调用 property
        shim = lambda ms: type("S", (), {"duration": ms})()
        self.assertEqual(DYVideo.duration_s.fget(shim(45_000)), 45.0)
        self.assertEqual(Photo.duration_s.fget(shim(90_000)), 90.0)


class ParserRouterAuditTests(unittest.TestCase):
    """B5/D3。"""

    def _router_with(self, classes):
        import astrbot_plugin_video_to_channel.service.parser_router as pr

        router = pr.ParserRouter(_audit_cfg(), object())
        original = pr._PARSER_CLASSES
        pr._PARSER_CLASSES = classes
        try:
            asyncio.run(router.initialize())
        finally:
            pr._PARSER_CLASSES = original
        return router

    def test_broken_platform_does_not_blank_the_table(self):
        class Bad:
            platform = Platform(name="bilibili", display_name="坏平台")
            non_portable_keywords = frozenset()
            _key_patterns = [("bad", "bad")]

            def __init__(self, *_a, **_k):
                raise RuntimeError("依赖缺失")

            async def close_session(self):
                pass

        class Good:
            platform = Platform(name="douyin", display_name="好平台")
            non_portable_keywords = frozenset()
            _key_patterns = [("goodkeyword", "goodkeyword")]

            def __init__(self, *_a, **_k):
                pass

            async def close_session(self):
                pass

        router = self._router_with((Bad, Good))
        self.assertTrue(router.patterns, "单平台失败不得让匹配表清空")
        self.assertIsNotNone(router.match("xx goodkeyword yy"))

    def test_all_broken_does_not_raise(self):
        class Bad:
            platform = Platform(name="bilibili", display_name="坏平台")
            non_portable_keywords = frozenset()
            _key_patterns = [("bad", "bad")]

            def __init__(self, *_a, **_k):
                raise RuntimeError("x")

            async def close_session(self):
                pass

        router = self._router_with((Bad,))  # 不得抛异常，只记录错误
        self.assertEqual(router.patterns, [])

    def test_keyword_collision_keeps_own_parser(self):
        class A:
            platform = Platform(name="bilibili", display_name="A")
            non_portable_keywords = frozenset()
            _key_patterns = [("dup", "dup-a")]

            def __init__(self, *_a, **_k):
                pass

            async def close_session(self):
                pass

        class B:
            platform = Platform(name="douyin", display_name="B")
            non_portable_keywords = frozenset()
            _key_patterns = [("dup", "dup-b")]

            def __init__(self, *_a, **_k):
                pass

            async def close_session(self):
                pass

        router = self._router_with((A, B))
        self.assertIsInstance(router.match("dup-a")[0], A)
        self.assertIsInstance(router.match("dup-b")[0], B)

    def test_real_table_skips_non_portable_entries(self):
        router = ParserRouter(_audit_cfg(), Downloader(_audit_cfg()))
        asyncio.run(router.initialize())
        self.assertTrue(router.patterns)
        for link in (
            "https://www.bilibili.com/opus/123456",
            "https://t.bilibili.com/123456",
            "https://live.bilibili.com/123",
            "https://www.bilibili.com/favlist?fid=2",
            "https://www.bilibili.com/read/cv123",
            "bm BV1xx411c7mD",
        ):
            self.assertIsNone(router.match(link), f"不该触发: {link}")
        for link in (
            "https://b23.tv/abc123",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "BV1xx411c7mD",
            "av123456",
            "https://v.douyin.com/abc123/",
            "https://v.kuaishou.com/abc123",
        ):
            self.assertIsNotNone(router.match(link), f"应保留入口: {link}")


class BilibiliStreamTests(unittest.TestCase):
    """B22：dash=null / durl 都是合法上游响应。"""

    def _call(self, payload, streams=None, detecter_exc=None):
        import bilibili_api.video as bv

        parser = BilibiliParser(_audit_cfg(), object())
        saved = {k: getattr(bv, k) for k in
                 ("VideoDownloadURLDataDetecter", "MP4StreamDownloadURL",
                  "VideoStreamDownloadURL", "AudioStreamDownloadURL")}

        class MP4Stream:
            def __init__(self, url):
                self.url = url

        class Detecter:
            def __init__(self, data):
                self.data = data

            def detect_best_streams(self, **_kw):
                if detecter_exc is not None:
                    raise detecter_exc
                return list(streams or [])

        bv.MP4StreamDownloadURL = MP4Stream
        bv.VideoDownloadURLDataDetecter = Detecter

        class Video:
            async def get_download_url(self, page_index=0):
                return payload

        try:
            with mock.patch.object(BilibiliParser, "_get_video",
                                   mock.AsyncMock(return_value=Video())):
                return asyncio.run(
                    parser.extract_download_urls(bvid="BV1xx411c7mD", page_index=0)
                )
        finally:
            for k, v in saved.items():
                setattr(bv, k, v)

    def test_dash_null_raises_friendly_exception(self):
        with self.assertRaises(DownloadException) as ctx:
            self._call({"code": 0, "dash": None, "durl": None})
        self.assertIn("dash/durl", str(ctx.exception))

    def test_durl_only_reaches_detecter(self):
        """durl（非 DASH）形态必须被交给 detecter，而不是在取 dash 时就炸 AttributeError。"""
        with self.assertRaises(DownloadException) as ctx:
            self._call({"code": 0, "durl": [{"size": 1, "url": "https://cdn/x.mp4"}]})
        self.assertIn("视频流", str(ctx.exception))
        self.assertNotIn("NoneType", str(ctx.exception))

    def test_dash_video_none_is_tolerated(self):
        with self.assertRaises(DownloadException):
            self._call({"code": 0, "dash": {"video": None}})

    def test_detecter_internal_error_is_translated(self):
        with self.assertRaises(DownloadException) as ctx:
            self._call({"code": 0, "dash": {"video": [{"codecs": "avc1"}]}},
                       detecter_exc=IndexError("no stream"))
        self.assertIn("清晰度", str(ctx.exception))

    def test_hvc1_normalization_kept(self):
        payload = {"code": 0, "dash": {"video": [{"codecs": "hvc1.1.6"}]}}
        try:
            self._call(payload)
        except DownloadException:
            pass
        self.assertTrue(payload["dash"]["video"][0]["codecs"].startswith("hev,"))


class UploaderPayloadTests(unittest.TestCase):
    def test_empty_payload_is_unknown_not_success(self):
        cfg = _audit_cfg()
        video = _video_file(cfg)

        class R:
            async def run_json(self, argv, **_kw):
                return {}

        with self.assertRaises(PublishResultUnknownError):
            asyncio.run(ChannelUploader(cfg, R()).publish_video(video, content="t"))
        self.assertTrue(issubclass(PublishResultUnknownError, CliError))

    def test_success_true_without_evidence_is_unknown(self):
        """RT02-F2：success=true 但没有 feed_id/share_url 证据 => UNKNOWN，不算成功。"""
        cfg = _audit_cfg()
        video = _video_file(cfg)

        class R:
            async def run_json(self, argv, **_kw):
                return {"success": True}

        with self.assertRaises(PublishResultUnknownError):
            asyncio.run(ChannelUploader(cfg, R()).publish_video(video, content="t"))

    def test_success_payload_variants_without_evidence_are_unknown(self):
        """RT02-F2：success=true 的各种无证据形态都必须 UNKNOWN。"""
        cfg = _audit_cfg()
        video = _video_file(cfg)
        variants = [
            {"success": True},
            {"success": True, "foo": "bar"},
            {"success": True, "data": None},
            {"success": True, "data": []},
            {"success": True, "data": "xxx"},
            {"success": True, "data": {}},
            {"success": True, "data": {"foo": "bar"}},
        ]
        for payload in variants:
            class R:
                async def run_json(self, argv, **_kw):
                    return payload

            with self.assertRaises(PublishResultUnknownError, msg=f"payload={payload}"):
                asyncio.run(ChannelUploader(cfg, R()).publish_video(video, content="t"))

    def test_valid_success_payload_still_succeeds(self):
        """合法成功 payload（data.feed_id）必须仍返回 SUCCESS。"""
        cfg = _audit_cfg()
        video = _video_file(cfg)

        class R:
            async def run_json(self, argv, **_kw):
                return {"success": True, "data": {"feed_id": "F1", "share_url": "https://pd.qq.com/s/1"}}

        out = asyncio.run(ChannelUploader(cfg, R()).publish_video(video, content="t"))
        self.assertEqual(out.feed_id, "F1")

    def test_business_error_normalization(self):
        from astrbot_plugin_video_to_channel.service.cli_runner import CliRunner

        cases = {
            '{"data": {"retCode": 0}}': False,
            '{"data": {"retCode": "0"}}': False,
            '{"data": {"retCode": "8011", "msg": "未登录"}}': True,
            '{"data": {"retCode": 8011, "msg": "未登录"}}': True,
            '{"success": false, "message": "boom"}': True,
            '{"success": 0, "message": "boom"}': True,
            '{"success": "false", "message": "boom"}': True,
            '{"success": true, "data": {}}': False,
            '{"data": {"nothing": 1}}': False,
        }
        for text, is_error in cases.items():
            self.assertEqual(bool(CliRunner._find_business_error(json.loads(text))),
                             is_error, text)


class CookieExpiryTests(unittest.TestCase):
    """A13：Expires 恒为 UTC，Max-Age 优先。"""

    def _m(self, header):
        sc = SimpleCookie()
        sc.load(header)
        return next(iter(sc.values()))

    def test_cases(self):
        from astrbot_plugin_video_to_channel.core.cookie import resolve_expiry

        now = 1_788_000_000
        self.assertEqual(
            resolve_expiry(self._m("a=b; expires=Wed, 09-Sep-2026 10:00:00 GMT; path=/"), now),
            1_788_948_000, "不得按本地时区解释 GMT",
        )
        self.assertEqual(
            resolve_expiry(self._m("a=b; expires=Wed, 09 Sep 2026 10:00:00 GMT; path=/"), now),
            1_788_948_000, "空格分隔的 RFC1123 也要能解析",
        )
        self.assertEqual(
            resolve_expiry(self._m("a=b; max-age=120; expires=Wed, 09-Sep-2026 10:00:00 GMT"), now),
            now + 120,
        )
        self.assertLess(resolve_expiry(self._m("a=b; max-age=0"), now), now)
        self.assertEqual(resolve_expiry(self._m("a=b; path=/"), now), 0)


class BilibiliLoginAuditTests(unittest.TestCase):
    """A5/A6。"""

    def setUp(self):
        self.login = BilibiliLogin(_audit_cfg())

    def tearDown(self):
        if self.login.credential_file.exists():
            self.login.credential_file.unlink()

    def test_pasted_cookie_variants(self):
        cases = {
            "SESSDATA=a; bili_jct=b; DedeUserID=1": {"SESSDATA": "a", "bili_jct": "b", "DedeUserID": "1"},
            "SESSDATA=a; bili_jct=b;": {"SESSDATA": "a", "bili_jct": "b"},
            "SESSDATA=a ;\n bili_jct=b \n": {"SESSDATA": "a", "bili_jct": "b"},
            "garbage-without-equals": {},
            "": {},
        }
        for text, want in cases.items():
            self.assertEqual(self.login._cookies_to_dict(text), want, repr(text))

    def test_netscape_export(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".bilibili.com\tTRUE\t/\tTRUE\t1899999999\tSESSDATA\tnet_a\n"
            ".bilibili.com\tTRUE\t/\tTRUE\t1899999999\tbili_jct\tnet_b\n"
        )
        self.assertEqual(self.login._cookies_to_dict(text),
                         {"SESSDATA": "net_a", "bili_jct": "net_b"})

    def test_corrupt_credential_file_degrades(self):
        for content in ("", "{not json", "[]", "123", '{"sessdata": }'):
            self.login.credential_file.write_text(content, encoding="utf-8")
            self.login._credential = "sentinel"
            try:
                self.login._load_credential()
            except Exception as e:  # noqa: BLE001
                self.fail(f"{content!r} 不应抛异常：{type(e).__name__}: {e}")
            self.assertIsNone(self.login._credential)

    def test_save_atomic_and_valid(self):
        cred = type("C", (), {
            "has_sessdata": lambda self: True,
            "get_cookies": lambda self: {"SESSDATA": "x"},
        })()
        self.login._credential = cred
        self.login._save_credential()
        saved = json.loads(self.login.credential_file.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"SESSDATA": "x"})
        self.assertEqual(
            list(self.login.credential_file.parent.glob("*.tmp")), [], "不得留下临时文件"
        )


class CliAccountAuditTests(unittest.TestCase):
    """A11/A12/A14。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v2c-audit-"))
        self.cfg = _make_cfg(str(_write_fake_cli(self.tmp)), self.tmp)
        self.account = CliAccount(self.cfg, CliRunner(self.cfg, CliBinaryManager(self.cfg)))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)
        for key in ("FAKE_QR_SKIP", "FAKE_POLL"):
            os.environ.pop(key, None)

    def test_stale_qrcode_not_replayed(self):
        qr = self.cfg.data_dir / "login_qrcode.png"
        qr.write_bytes(b"OLD_QR")
        os.environ["FAKE_QR_SKIP"] = "1"
        with self.assertRaises(Exception) as ctx:
            asyncio.run(self.account.qr_login())
        self.assertIn("二维码", str(ctx.exception))
        self.assertFalse(qr.exists(), "CLI 没写新码时旧码必须已删除")

    def test_fresh_qrcode_replaces_previous(self):
        qr = self.cfg.data_dir / "login_qrcode.png"
        qr.write_bytes(b"OLD_QR")
        info = asyncio.run(self.account.qr_login())
        self.assertEqual(info.qrcode_path.read_bytes(), b"FAKE_QR")

    def test_expired_text_predicate(self):
        for text in ("二维码已过期", "qrcode expired", "已被领取", "已失效"):
            self.assertTrue(is_qr_expired_text(text), text)
        for text in ("token 未过期，请继续等待", "没有过期", "status: scanning", ""):
            self.assertFalse(is_qr_expired_text(text), text)

    def test_poll_login_uses_bounded_timeout(self):
        seen = {}

        class Runner:
            async def run(self, args, **kw):
                seen.update(kw)
                return type("O", (), {
                    "stdout": '{"data":{"status":"scanning"},"success":true}',
                    "stderr": "",
                })()

        self.account.runner = Runner()
        asyncio.run(self.account.poll_login(timeout=25))
        self.assertEqual(seen.get("timeout"), 25)


class ExternalCliAuditTests(unittest.TestCase):
    """A7/A8。"""

    def test_command_name_on_path(self):
        cfg = _audit_cfg(cli_command="python3")
        self.assertFalse(cfg.cli_managed)
        self.assertIsNotNone(CliBinaryManager(cfg).resolve_existing())

    def test_missing_command_rejected(self):
        cfg = _audit_cfg(cli_command="definitely-not-a-real-cli-xyz")
        self.assertIsNone(CliBinaryManager(cfg).resolve_existing())

    def test_registry_error_payload(self):
        manager = CliBinaryManager(_audit_cfg(cli_command="auto"))

        class Resp:
            status = 200

            async def json(self):
                return {"error": "name or url required"}

        class Ctx:
            async def __aenter__(self):
                return Resp()

            async def __aexit__(self, *a):
                return False

        class Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, *a, **k):
                return Ctx()

        with mock.patch("aiohttp.ClientSession", Session):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(manager._fetch_download_info())
        self.assertIn("name or url required", str(ctx.exception))


class MainEntryAuditTests(unittest.TestCase):
    """B4/B6/D5：入口层此前零覆盖。"""

    def setUp(self):
        import importlib

        self.pm = importlib.import_module(
            "astrbot_plugin_video_to_channel.main"
        )

    def _plugin(self, whitelist=("Priv-Admin",), debounce=600):
        cfg = {
            "session_whitelist": list(whitelist),
            "debounce_seconds": debounce,
            "target_guild_id": "",
            "target_channel_id": "",
            "cli_command": "auto",
            "download": {},
            "parsers": {},
            "max_concurrent": 2,
        }
        ctx = self.pm.Context()
        return self.pm.VideoToChannelPlugin(ctx, cfg), ctx

    def test_none_ids_are_not_self_message(self):
        plugin, _ = self._plugin()
        cls = self.pm.VideoToChannelPlugin
        self.assertFalse(cls._is_self_message(self.pm.AstrMessageEvent(sender=None, me=None)))
        self.assertTrue(cls._is_self_message(self.pm.AstrMessageEvent(sender="777", me="777")))
        self.assertFalse(cls._is_self_message(self.pm.AstrMessageEvent(sender="1", me="2")))

        class Broken:
            def get_sender_id(self):
                raise NotImplementedError

            def get_self_id(self):
                raise NotImplementedError

        self.assertFalse(cls._is_self_message(Broken()))

    def test_gates_emit_nothing(self):
        plugin, ctx = self._plugin()
        asyncio.run(plugin.router.initialize())
        seen = []

        async def fake_process(text, *, on_accepted=None):
            seen.append(text)
            return None

        plugin.pipeline.process = fake_process

        async def scenario():
            for umo, text in (
                ("Other", "BV1xx411c7mD"),
                ("Priv-Admin", "/v2c sid"),
                ("Priv-Admin", "今天天气不错"),
                ("Priv-Admin", "https://www.bilibili.com/opus/123"),
            ):
                await plugin.on_message(
                    self.pm.AstrMessageEvent(umo=umo, text=text)
                )
            for task in list(plugin._bg_tasks):
                await task

        asyncio.run(scenario())
        self.assertEqual(seen, [], "被过滤/不可搬运的链接不得进入流水线")
        self.assertEqual(ctx.sent, [], "被过滤的消息不得有任何回执")

    def test_accepted_link_gets_one_ack(self):
        plugin, ctx = self._plugin()
        asyncio.run(plugin.router.initialize())

        async def fake_process(text, *, on_accepted=None):
            await on_accepted()
            return None

        plugin.pipeline.process = fake_process

        async def scenario():
            await plugin.on_message(self.pm.AstrMessageEvent(umo="Priv-Admin", text="BV1xx411c7mD"))
            for task in list(plugin._bg_tasks):
                await task

        asyncio.run(scenario())
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn("开始解析", ctx.sent[0][1].chain[0])

    def test_friendly_error_unknown_results(self):
        plugin, _ = self._plugin()
        msg = plugin._friendly_error(CliTimeoutError("执行超时（>600s）"))
        self.assertIn("结果未知", msg)
        self.assertIn("不要重发", msg)
        self.assertIn("无法确认", plugin._friendly_error(PublishResultUnknownError("空")))
        self.assertIn("登录", plugin._friendly_error(CliError("retCode=8011：未登录")))

    def test_poll_loop_survives_one_timeout(self):
        plugin, ctx = self._plugin()

        class Account:
            calls = []

            async def poll_login(self, timeout=None):
                Account.calls.append(timeout)
                if len(Account.calls) == 1:
                    raise CliTimeoutError("本轮超时")
                return PollResult(authorized=True, message="扫码成功")

        plugin.account = Account()
        qr = QrLoginInfo("https://auth", Path("/tmp/none.png"), 120, 0)
        asyncio.run(plugin._poll_login_loop("Priv-Admin", qr))
        self.assertEqual(len(Account.calls), 2, "单轮轮询超时必须继续而不是终止")
        self.assertTrue(all(t <= plugin.cfg.cli_timeout for t in Account.calls))
        self.assertEqual(
            len([1 for _u, c in ctx.sent if "登录成功" in c.chain[0]]), 1, "成功只回执一次"
        )

    def test_poll_loop_not_fooled_by_negative_text(self):
        plugin, ctx = self._plugin()

        class Account:
            async def poll_login(self, timeout=None):
                raise CliError("token 未过期，请继续等待")

        plugin.account = Account()
        sent = []

        async def fake_send(umo, text):
            sent.append(text)

        plugin._send = fake_send
        clock = [1_000.0]

        def fake_monotonic():
            clock[0] += 5.0
            return clock[0]

        with mock.patch.object(self.pm.time, "monotonic", fake_monotonic):
            asyncio.run(plugin._poll_login_loop("Priv-Admin",
                                                QrLoginInfo("u", Path("/tmp/x.png"), 60, 0)))
        self.assertFalse(any("已失效" in s for s in sent), "正向文案不得被判成二维码失效")
        self.assertTrue(any("登录超时" in s for s in sent))


class AuditBlindSpotTests(unittest.TestCase):
    """补上变异测试暴露出的盲区：验证“行为接线”，而不只是验证工具函数本身。"""

    def test_unknown_error_after_submit_keeps_debounce(self):
        """提交给频道之后出现无法归类的异常 => 帖子存在性未知，不得放开防抖。"""
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg)
        uploader = _Uploader(results=[RuntimeError("子进程输出解析崩了")])
        pipeline = VideoPipeline(
            cfg, _Router(_Parser(_result([VideoContent(path)]))), uploader
        )

        async def scenario():
            with self.assertRaises(RuntimeError):
                await pipeline.process("https://example.com/v")
            return await pipeline.process("https://example.com/v")

        self.assertIsNone(asyncio.run(scenario()))

    def test_definitive_error_before_submit_releases_debounce(self):
        """提交前的确定失败必须放开防抖，否则用户改好配置也重试点燃不了。"""
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg)
        pipeline = VideoPipeline(
            cfg, _Router(_Parser(_result([VideoContent(path)]))), _Uploader()
        )
        # 第一次按“时长超限”确定失败，第二次放行（模拟用户把上限调大后重发）
        with mock.patch.object(
            VideoPipeline,
            "_check_duration",
            side_effect=[PipelineError("视频时长超过限制"), None],
        ):
            async def scenario():
                with self.assertRaises(PipelineError):
                    await pipeline.process("https://example.com/v")
                return await pipeline.process("https://example.com/v")

            second = asyncio.run(scenario())
        self.assertIsNotNone(second, "被时长拦截后，重发应能重新走一遍流程")

    def test_poll_login_expired_via_account(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = _make_cfg(str(_write_fake_cli(tmp)), tmp)
        account = CliAccount(cfg, CliRunner(cfg, CliBinaryManager(cfg)))
        os.environ["FAKE_POLL"] = "expired"
        try:
            result = asyncio.run(account.poll_login())
        finally:
            os.environ.pop("FAKE_POLL", None)
        self.assertTrue(result.expired)
        os.environ["FAKE_POLL"] = "negative"
        try:
            result = asyncio.run(account.poll_login())
        finally:
            os.environ.pop("FAKE_POLL", None)
        self.assertFalse(result.expired, "“未过期”文案不得被判成二维码失效")

    def test_cli_timeout_raises_typed_error(self):
        """超时必须是 CliTimeoutError，上层才能区分“结果未知”和“确定失败”。"""
        tmp = Path(tempfile.mkdtemp())
        cfg = _make_cfg(str(_write_fake_cli(tmp)), tmp)
        runner = CliRunner(cfg, CliBinaryManager(cfg))
        with self.assertRaises(CliTimeoutError):
            asyncio.run(runner.run(["sleep"], timeout=1, ensure=False))

# ======================================================================
# 第四轮回归测试：RT-01 / RT-02 / RT-03 / RT-04 / RT-06
# 要求：新测试在“旧实现”上失败、在“新实现”上通过
# ======================================================================

class RT02OutcomeTests(unittest.TestCase):
    """RT-02：投稿结果三态（SUCCESS / DEFINITE_FAILURE / UNKNOWN）。"""

    def _run(self, cfg, uploader, link="https://x/rt02"):
        from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

        class P:
            platform = Platform(name="f", display_name="测试")
            async def parse(self, k, s):
                f = cfg.cache_dir / "rt02.mp4"
                f.write_bytes(b"x")
                return ParseResult(platform=self.platform, title="t", contents=[VideoContent(f)])

        class R:
            def match(self, t):
                return (P(), "k", type("M", (), {"group": lambda self, *a: link})())

        return VideoPipeline(cfg, R(), uploader)

    def test_cli_published_then_non_json_output_keeps_debounce(self):
        """RT-02 核心：CLI 已执行发布，但输出非 JSON -> UNKNOWN，不得允许重试。"""
        # 走真实 ChannelUploader.publish_video 链路：假 runner 模拟 CLI 已发帖但
        # stdout 不是 JSON。真实 run_json 会抛 CliOutputError -> publish_video 必须
        # 把它收敛为 PublishResultUnknownError（结果未知），pipeline 保留防抖。
        from astrbot_plugin_video_to_channel.service.channel_uploader import ChannelUploader
        from astrbot_plugin_video_to_channel.service.cli_runner import CliOutputError

        cfg = _audit_cfg(debounce_seconds=600)
        calls = {"n": 0}

        class FakeRunner:
            async def run_json(self, argv, **_kw):
                calls["n"] += 1
                # 模拟：CLI 进程已执行（甚至可能已建帖），但 stdout 混入日志非 JSON
                raise CliOutputError(
                    "tencent-channel-cli 返回了非 JSON 内容: posted ok\n{bad"
                )

        uploader = ChannelUploader(cfg, FakeRunner())
        pipeline = self._run(cfg, uploader)

        async def scenario():
            first = None
            try:
                await pipeline.process("https://x/rt02")
            except Exception as e:
                first = e
            second = await pipeline.process("https://x/rt02")
            return first, second

        first, second = asyncio.run(scenario())
        self.assertIsInstance(first, CliError)
        self.assertIsNone(second, "已进入投稿+输出坏掉 => UNKNOWN，重试必须被防抖/冷却拦截")
        self.assertEqual(calls["n"], 1, "不允许用户重试造成第二次投稿")

    def test_cli_published_then_structured_error_releases(self):
        """RT-02：服务端明确业务拒绝（带 payload）-> DEFINITE_FAILURE，允许重试。"""
        cfg = _audit_cfg(debounce_seconds=600)

        class Uploader:
            def describe_missing(self):
                return []

            async def publish_video(self, path, content=""):
                raise CliError(
                    "tencent-channel-cli 返回错误：retCode=8011：未登录",
                    returncode=0,
                    payload={"data": {"retCode": 8011, "msg": "未登录"}, "success": False},
                    definite=True,
                )

        pipeline = self._run(cfg, Uploader())
        async def scenario():
            first = None
            try:
                await pipeline.process("https://x/rt02b")
            except Exception as e:
                first = e
            second = await pipeline.process("https://x/rt02b")
            return first, second

        calls = {"n": 0}

        class CountingUploader(Uploader):
            async def publish_video(self, path, content=""):
                calls["n"] += 1
                await super().publish_video(path, content=content)

        pipeline = self._run(cfg, CountingUploader())
        async def scenario2():
            try:
                await pipeline.process("https://x/rt02b")
            except Exception:
                pass
            try:
                await pipeline.process("https://x/rt02b")
            except Exception:
                pass
            return calls["n"]

        self.assertEqual(asyncio.run(scenario2()), 2, "definite 失败必须允许立刻重试（第二次真正发起）")

    def test_cli_timeout_keeps_debounce(self):
        cfg = _audit_cfg(debounce_seconds=600)

        class Uploader:
            def describe_missing(self):
                return []

            async def publish_video(self, path, content=""):
                raise CliTimeoutError("执行超时（>600s）")

        pipeline = self._run(cfg, Uploader())
        async def scenario():
            try:
                await pipeline.process("https://x/rt02c")
            except Exception:
                pass
            second = await pipeline.process("https://x/rt02c")
            return second

        self.assertIsNone(asyncio.run(scenario()))


class RT03ReloadTests(unittest.TestCase):
    """RT-03：旧实例投稿中 -> reload -> 新实例必须拒绝同一链接。"""

    def _mk_cfg(self, tmp):
        from astrbot_plugin_video_to_channel.core.config import PluginConfig

        class Live(dict):
            def save_config(self):
                pass

        raw = {
            "session_whitelist": [],
            "cli_command": "auto",
            "target_guild_id": "g",
            "target_channel_id": "c",
            "download": {},
            "parsers": {},
            "debounce_seconds": 600,
        }
        c = PluginConfig(Live(raw), plugin_name="rt03_isolated")
        c.data_dir = tmp
        c.cache_dir = tmp / "cache"
        c.cache_dir.mkdir(exist_ok=True)
        return c

    def test_reload_blocks_same_link(self):
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="rt03-"))
        try:
            cfg1 = self._mk_cfg(tmp)
            cfg2 = self._mk_cfg(tmp)  # reload 后新实例，同 data_dir
            uploader_posts = {"n": 0}

            def make_pipeline(cfg):
                from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

                class P:
                    platform = Platform(name="f", display_name="测试")
                    async def parse(self, k, s):
                        f = cfg.cache_dir / "rt03.mp4"
                        f.write_bytes(b"x")
                        return ParseResult(platform=self.platform, title="t", contents=[VideoContent(f)])

                class R:
                    def match(self, t):
                        return (P(), "k", type("M", (), {"group": lambda self, *a: "https://x/rt03"})())

                class U:
                    def describe_missing(self):
                        return []

                    async def publish_video(self, path, content=""):
                        uploader_posts["n"] += 1
                        raise CliTimeoutError("执行超时")

                return VideoPipeline(cfg, R(), U())

            # 旧实例投稿 -> 超时（UNKNOWN 落盘）
            p1 = make_pipeline(cfg1)
            try:
                asyncio.run(p1.process("https://x/rt03"))
            except Exception:
                pass
            self.assertTrue((tmp / "unknown_submits.json").exists(), "UNKNOWN 必须落盘")

            # 新实例（同 data_dir）：必须拒绝
            p2 = make_pipeline(cfg2)
            out = asyncio.run(p2.process("https://x/rt03"))
            self.assertIsNone(out, "reload 后新实例不得立即接受同一链接")
            self.assertEqual(uploader_posts["n"], 1, "不得重复投稿")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_persists_synchronously(self):
        """UNKNOWN 必须同步落盘（而非依赖异步 create_task）。

        说明：process 的 finally 含 await，异步 flush 多数时候也能赶上；
        但在「异常后事件循环立即被 terminate/关闭」的窗口（红队 rt20 实证）会丢失，
        导致新实例放行同一链接造成重复投稿。同步写是无害加固，此处钉死该契约。
        """
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="rt03sync-"))
        try:
            cfg1 = self._mk_cfg(tmp)
            from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

            class P:
                platform = Platform(name="f", display_name="测试")
                async def parse(self, k, s):
                    f = cfg1.cache_dir / "rt03sync.mp4"
                    f.write_bytes(b"x")
                    return ParseResult(platform=self.platform, title="t", contents=[VideoContent(f)])

            class R:
                def match(self, t):
                    return (P(), "k", type("M", (), {"group": lambda self, *a: "https://x/rt03sync"})())

            class U:
                def describe_missing(self):
                    return []
                async def publish_video(self, path, content=""):
                    raise CliTimeoutError("执行超时")

            pipe = VideoPipeline(cfg1, R(), U())
            try:
                asyncio.run(pipe.process("https://x/rt03sync"))
            except Exception:
                pass
            # 关键：不 yield 任何事件循环，直接断言文件已存在
            self.assertTrue(
                (tmp / "unknown_submits.json").exists(),
                "UNKNOWN 必须同步落盘（create_task 异步会在收尾时丢失）",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_terminate_marks_unknown(self):
        """terminate() 必须把进行中链接标记为 UNKNOWN 并落盘。"""
        import tempfile, shutil, importlib

        tmp = Path(tempfile.mkdtemp(prefix="rt03t-"))
        try:
            mm = importlib.import_module("astrbot_plugin_video_to_channel.main")
            cfg = {
                "session_whitelist": ["S"],
                "cli_command": "auto",
                "target_guild_id": "g",
                "target_channel_id": "c",
                "download": {},
                "parsers": {},
                "debounce_seconds": 600,
            }
            plugin = mm.VideoToChannelPlugin(mm.Context(), cfg)
            # 手动把链接放进 _task_links（模拟后台任务正在处理；用任意占位 task）
            async def _dummy():
                pass

            async def scenario():
                plugin._task_links[asyncio.create_task(_dummy())] = "https://x/rt03t"
                await plugin.terminate()

            asyncio.run(scenario())
            unknown_file = plugin.cfg.data_dir / "unknown_submits.json"
            self.assertTrue(unknown_file.exists(), "terminate 必须落盘 UNKNOWN")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RT01MergeRaceTests(unittest.TestCase):
    """RT-01：并发合流不得共享中间文件 / 不互相删除。"""

    def test_concurrent_merge_uses_unique_workdir(self):
        import tempfile, shutil, importlib

        tmp = Path(tempfile.mkdtemp(prefix="rt01-"))
        try:
            dl = importlib.import_module("astrbot_plugin_video_to_channel.core.download")
            from astrbot_plugin_video_to_channel.core.config import PluginConfig

            class Live(dict):
                def save_config(self):
                    pass

            c = PluginConfig(Live({"download": {}, "parsers": {}}), plugin_name="rt01_isolated")
            c.data_dir = tmp
            c.cache_dir = tmp / "cache"
            c.cache_dir.mkdir(exist_ok=True)

            # 用真实 ffmpeg 不可用；模拟 merge_av 记录两个任务的临时目录不同
            seen_workdirs = []

            # 记录每个任务收到的 v/a 路径，验证它们落在各自的唯一 workdir
            seen_workdirs = []
            seen_inputs = []

            async def fake_merge(*, v_path, a_path, output_path):
                seen_workdirs.append(str(output_path.parent))
                seen_inputs.append((str(v_path), str(a_path)))
                await asyncio.sleep(0.1)
                output_path.write_bytes(b"MERGED")

            dl.merge_av = fake_merge

            d = dl.Downloader(c)
            async def fake_streamd(url, *, file_name=None, headers=None, proxy=...):
                # streamd 真实语义：cache_dir / file_name（file_name 可为子目录相对路径）
                f = c.cache_dir / file_name
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"D")
                return f
            d.streamd = fake_streamd

            out = c.cache_dir / "BVrt01-1.mp4"

            async def main():
                await asyncio.gather(
                    d.download_av_and_merge("https://cdn/v", "https://cdn/a", output_path=out),
                    d.download_av_and_merge("https://cdn/v", "https://cdn/a", output_path=out),
                )

            asyncio.run(main())
            self.assertEqual(len(set(seen_workdirs)), 2, "两个并发任务必须用唯一工作目录")
            # 两个任务的中间文件必须落在不同目录（绝不共享）
            self.assertEqual(len(set(seen_inputs)), 2, "v/a 中间文件不得共享")
            self.assertTrue(all("video.m4s" in p and "audio.m4s" in q for p, q in seen_inputs))
            self.assertTrue(out.exists())
            # 不应残留任何工作目录
            leftovers = [p for p in c.cache_dir.iterdir() if p.is_dir()]
            self.assertEqual(leftovers, [], "工作目录必须被清理")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RT04IntervalTests(unittest.TestCase):
    """RT-04：interval 安全归一化。"""

    def _qr(self, interval_raw, expires=120):
        from astrbot_plugin_video_to_channel.service.cli_account import CliAccount
        from astrbot_plugin_video_to_channel.service.cli_runner import CliRunner, RunOutput
        from astrbot_plugin_video_to_channel.service.cli_binary import CliBinaryManager
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        payload = {
            "success": True,
            "data": {
                "verification_uri": "u",
                "expires_in_s": expires,
                "interval": interval_raw,
            },
        }
        cli = tmp / "cli.py"
        cli.write_text(
            "#!" + sys.executable + "\n"
            "import sys, json\n"
            "from pathlib import Path\n"
            f"payload = {payload!r}\n"
            "if '--qrcode-path' in sys.argv:\n"
            "    Path(sys.argv[sys.argv.index('--qrcode-path')+1]).write_bytes(b'QR')\n"
            "print(json.dumps(payload))\n"
        )
        cli.chmod(0o755)
        c = PluginConfig({"cli_command": str(cli), "download": {}, "parsers": {}}, plugin_name="rt04")
        c.data_dir = tmp
        acct = CliAccount(c, CliRunner(c, CliBinaryManager(c)))
        return asyncio.run(acct.qr_login())

    def test_interval_clamped(self):
        # 0/负数/极大值都必须归一化到安全区间
        for bad in (0, -5, 999999999):
            qr = self._qr(bad)
            self.assertGreater(qr.interval, 0, f"interval={bad} 不得忙等")
            self.assertLessEqual(qr.interval, 15.0, f"interval={bad} 不得过大")
        # nan/inf 用字符串形式传给 fake CLI（避免生成非法 python 字面量）
        for bad_str in ("nan", "inf"):
            qr = self._qr(bad_str)
            self.assertGreater(qr.interval, 0, f"interval={bad_str} 不得忙等")
            self.assertLessEqual(qr.interval, 15.0, f"interval={bad_str} 不得过大")
        # 正常值不受影响
        qr = self._qr(3)
        self.assertLessEqual(qr.interval, 3.0)


class RT06ShortlinkTests(unittest.TestCase):
    """RT-06：短链重定向到不支持入口不得进入完整解析。"""

    def test_shortlink_redirect_to_opus_is_blocked(self):
        from astrbot_plugin_video_to_channel.core.parsers import BilibiliParser
        from astrbot_plugin_video_to_channel.core.exception import ParseException
        from astrbot_plugin_video_to_channel.core.config import PluginConfig
        import tempfile

        # 构造一个 b23.tv 短链，重定向到 opus 页面
        parser = BilibiliParser(PluginConfig({"download": {}, "parsers": {}}, plugin_name="rt06"), object())

        async def fake_redirect(url, headers=None):
            return "https://www.bilibili.com/opus/1234567"

        parser.get_redirect_url = fake_redirect
        with self.assertRaises(ParseException):
            asyncio.run(parser.parse_with_redirect("https://b23.tv/abcDEF"))

# ======================================================================
# 第五轮回归测试：RT02-F1 / RT02-F2 / RT03-F1 / RT03-F2 / RT04 语义
# ======================================================================

class RT02F1DefiniteTests(unittest.TestCase):
    """RT02-F1：非零退出码不得自动 definite；只有业务 payload 才算确定失败。"""

    def test_rc_137_without_payload_is_not_definite(self):
        from astrbot_plugin_video_to_channel.service.cli_runner import CliError

        e = CliError("rc 137", returncode=137, stdout="", stderr="Killed")
        self.assertFalse(e.definite, "非零 rc 本身不能证明没建帖")

    def test_rc_1_without_payload_is_not_definite(self):
        from astrbot_plugin_video_to_channel.service.cli_runner import CliError

        e = CliError("rc 1", returncode=1)
        self.assertFalse(e.definite)

    def test_business_payload_is_definite(self):
        from astrbot_plugin_video_to_channel.service.cli_runner import CliError

        e = CliError("biz", returncode=0,
                     payload={"data": {"retCode": 8011, "msg": "未登录"}, "success": False})
        self.assertTrue(e.definite)

    def test_pipeline_rc137_goes_unknown_and_blocks_retry(self):
        """CLI 已投稿 + rc=137 -> UNKNOWN -> 第二次投稿被拦截。"""
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg, "rc137.mp4")
        calls = {"n": 0}

        class U:
            def describe_missing(self):
                return []

            async def publish_video(self, p, content=""):
                calls["n"] += 1
                raise CliError("tencent-channel-cli 退出码 137：Killed",
                               returncode=137, stdout="", stderr="Killed")

        pipeline = VideoPipeline(cfg, _Router(_Parser(_result([VideoContent(path)]))), U())
        async def scenario():
            try:
                await pipeline.process("https://x/rc137")
            except Exception:
                pass
            second = await pipeline.process("https://x/rc137")
            return second, calls["n"]

        second, n = asyncio.run(scenario())
        self.assertIsNone(second, "rc=137 投稿后崩溃 => UNKNOWN，重试必须被拦截")
        self.assertEqual(n, 1, "不得重复投稿")

    def test_pipeline_rc1_goes_unknown_and_blocks_retry(self):
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg, "rc1.mp4")
        calls = {"n": 0}

        class U:
            def describe_missing(self):
                return []

            async def publish_video(self, p, content=""):
                calls["n"] += 1
                raise CliError("tencent-channel-cli 退出码 1：boom",
                               returncode=1, stdout="", stderr="boom")

        pipeline = VideoPipeline(cfg, _Router(_Parser(_result([VideoContent(path)]))), U())
        async def scenario():
            try:
                await pipeline.process("https://x/rc1")
            except Exception:
                pass
            second = await pipeline.process("https://x/rc1")
            return second, calls["n"]

        second, n = asyncio.run(scenario())
        self.assertIsNone(second)
        self.assertEqual(n, 1)

    def test_pipeline_business_reject_still_allows_retry(self):
        """明确业务拒绝 payload -> definite -> 允许重试且第二次真正执行。"""
        cfg = _audit_cfg(debounce_seconds=600)
        path = _video_file(cfg, "biz.mp4")
        calls = {"n": 0}

        class U:
            def describe_missing(self):
                return []

            async def publish_video(self, p, content=""):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise CliError("retCode=8011：未登录", returncode=0,
                                   payload={"data": {"retCode": 8011, "msg": "未登录"},
                                            "success": False}, definite=True)
                return PublishResult(raw={}, feed_id="F2", share_url=None)

        pipeline = VideoPipeline(cfg, _Router(_Parser(_result([VideoContent(path)]))), U())
        async def scenario():
            try:
                await pipeline.process("https://x/biz")
            except Exception:
                pass
            second = await pipeline.process("https://x/biz")
            return second, calls["n"]

        second, n = asyncio.run(scenario())
        self.assertIsNotNone(second, "明确业务拒绝后重试必须真正执行")
        self.assertEqual(n, 2)


class RT03F1CanonicalKeyTests(unittest.TestCase):
    """RT03-F1：terminate/process 使用统一 canonical key。"""

    def _cfg_with_data(self, tmp):
        from astrbot_plugin_video_to_channel.core.config import PluginConfig

        class Live(dict):
            def save_config(self):
                pass

        c = PluginConfig(Live({"session_whitelist": [], "cli_command": "auto",
            "target_guild_id": "g", "target_channel_id": "c",
            "download": {}, "parsers": {}, "debounce_seconds": 600}), "rt3f1")
        c.data_dir = tmp
        c.cache_dir = tmp / "cache"
        c.cache_dir.mkdir(exist_ok=True)
        return c

    def test_canonical_link_normalizes_scheme(self):
        """同一 URL 的 http/https/无 scheme/夹带文字 -> 同一 key。"""
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="rt3f1-"))
        try:
            from astrbot_plugin_video_to_channel.service.parser_router import ParserRouter

            cfg = self._cfg_with_data(tmp)
            router = ParserRouter(cfg, object())
            asyncio.run(router.initialize())
            from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

            pipe = VideoPipeline(cfg, router, object())
            keys = {
                pipe.canonical_link("https://b23.tv/abcDEF"),
                pipe.canonical_link("http://b23.tv/abcDEF"),
                pipe.canonical_link("b23.tv/abcDEF"),
                pipe.canonical_link("看看 https://b23.tv/abcDEF 视频"),
                pipe.canonical_link("https://www.bilibili.com/video/BV1xx411c7mD"),
                pipe.canonical_link("https://v.douyin.com/abc123/"),
            }
            self.assertEqual(keys, {"b23.tv/abcDEF", "bilibili.com/video/BV1xx411c7mD",
                                    "v.douyin.com/abc123"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mark_unknown_with_full_url_blocks_reload(self):
        """用带 scheme 的完整 URL 标记 UNKNOWN，新实例查 canonical key 必须拦截。"""
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="rt3f1b-"))
        try:
            from astrbot_plugin_video_to_channel.service.parser_router import ParserRouter

            cfg1 = self._cfg_with_data(tmp)
            cfg2 = self._cfg_with_data(tmp)  # 新实例，同 data_dir
            router1 = ParserRouter(cfg1, object())
            asyncio.run(router1.initialize())
            pipe1 = VideoPipeline(cfg1, router1, object())
            # terminate 用完整 URL 调 mark_link_unknown
            pipe1.mark_link_unknown("https://b23.tv/abcDEF")
            self.assertTrue((tmp / "unknown_submits.json").exists())
            data = json.loads((tmp / "unknown_submits.json").read_text(encoding="utf-8"))
            self.assertIn("b23.tv/abcDEF", data, "必须写入 canonical key")

            router2 = ParserRouter(cfg2, object())
            asyncio.run(router2.initialize())
            pipe2 = VideoPipeline(cfg2, router2, object())
            self.assertTrue(pipe2.is_unknown("b23.tv/abcDEF"), "新实例必须能查到")
            self.assertTrue(pipe2.is_unknown(pipe2.canonical_link("https://b23.tv/abcDEF")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reload_blocks_same_url_with_scheme(self):
        """端到端：投稿中 -> terminate -> 新实例收到带 scheme 同 URL -> 拦截。"""
        import tempfile, shutil, importlib

        tmp = Path(tempfile.mkdtemp(prefix="rt3f1c-"))
        try:
            mm = importlib.import_module("astrbot_plugin_video_to_channel.main")

            class Ctx:
                async def send_message(self, umo, chain):
                    pass

            raw = {"session_whitelist": ["S"], "cli_command": "auto",
                   "target_guild_id": "g", "target_channel_id": "c",
                   "download": {}, "parsers": {}, "debounce_seconds": 600}
            plugin = mm.VideoToChannelPlugin(Ctx(), dict(raw))
            plugin.cfg.data_dir = tmp
            plugin.cfg.cache_dir = tmp / "cache"
            plugin.cfg.cache_dir.mkdir(exist_ok=True)

            from astrbot_plugin_video_to_channel.service.channel_uploader import ChannelUploader

            class Hang:
                async def run_json(self, argv, **kw):
                    await asyncio.sleep(30)

            from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

            class P:
                platform = Platform(name="f", display_name="测试")
                async def parse(self, k, s):
                    (plugin.cfg.cache_dir / "v.mp4").write_bytes(b"x")
                    return ParseResult(platform=self.platform, title="t",
                                       contents=[VideoContent(plugin.cfg.cache_dir / "v.mp4")])

            class R:
                def match(self, t):
                    return (P(), "k", type("M", (), {"group": lambda self, *a: "b23.tv/abcDEF"})())

            plugin.pipeline = VideoPipeline(plugin.cfg, R(), ChannelUploader(plugin.cfg, Hang()))

            async def scenario():
                await plugin.router.initialize()
                task = asyncio.create_task(plugin._background_handle("S", "https://b23.tv/abcDEF"))
                plugin._bg_tasks.add(task)
                plugin._task_links[task] = plugin.pipeline.canonical_link("https://b23.tv/abcDEF")
                def done(t):
                    plugin._bg_tasks.discard(t)
                    plugin._task_links.pop(t, None)
                task.add_done_callback(done)
                await asyncio.sleep(0.3)
                await plugin.terminate()

                # 新实例
                plugin2 = mm.VideoToChannelPlugin(Ctx(), dict(raw))
                plugin2.cfg.data_dir = tmp
                plugin2.cfg.cache_dir = tmp / "cache"
                from astrbot_plugin_video_to_channel.service.channel_uploader import PublishResult

                class U2:
                    def __init__(self):
                        self.n = 0
                    def describe_missing(self):
                        return []
                    async def publish_video(self, p, content=""):
                        self.n += 1
                        return PublishResult(raw={}, feed_id="F")

                u2 = U2()
                class P2:
                    platform = Platform(name="f", display_name="测试")
                    async def parse(self, k, s):
                        (plugin2.cfg.cache_dir / "v.mp4").write_bytes(b"x")
                        return ParseResult(platform=self.platform, title="t",
                                           contents=[VideoContent(plugin2.cfg.cache_dir / "v.mp4")])

                class R2:
                    def match(self, t):
                        return (P2(), "k", type("M", (), {"group": lambda self, *a: "b23.tv/abcDEF"})())

                plugin2.pipeline = VideoPipeline(plugin2.cfg, R2(), u2)
                await plugin2.router.initialize()
                out = await plugin2.pipeline.process("https://b23.tv/abcDEF")
                return out, u2.n

            out, n = asyncio.run(scenario())
            self.assertIsNone(out, "reload 后带 scheme 的同一 URL 不得重复投稿")
            self.assertEqual(n, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RT03F2ConcurrentWriteTests(unittest.TestCase):
    """RT03-F2：unknown_submits 并发写不互相覆盖。"""

    def _mk_cfg(self, tmp, name):
        from astrbot_plugin_video_to_channel.core.config import PluginConfig

        class Live(dict):
            def save_config(self):
                pass

        c = PluginConfig(Live({"session_whitelist": [], "cli_command": "auto",
            "target_guild_id": "g", "target_channel_id": "c",
            "download": {}, "parsers": {}, "debounce_seconds": 600}), name)
        c.data_dir = tmp
        c.cache_dir = tmp / "cache"
        c.cache_dir.mkdir(exist_ok=True)
        return c

    def _build(self, cfg, link):
        from astrbot_plugin_video_to_channel.service.pipeline import VideoPipeline

        class P:
            platform = Platform(name="f", display_name="测试")
            async def parse(self, k, s):
                (cfg.cache_dir / "v.mp4").write_bytes(b"x")
                return ParseResult(platform=self.platform, title="t",
                                   contents=[VideoContent(cfg.cache_dir / "v.mp4")])

        class R:
            def match(self, t):
                return (P(), "k", type("M", (), {"group": lambda self, *a: link})())

        class U:
            def describe_missing(self):
                return []
            async def publish_video(self, p, content=""):
                raise CliTimeoutError("timeout")

        return VideoPipeline(cfg, R(), U())

    def test_concurrent_write_different_links_keeps_both(self):
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="rt3f2-"))
        try:
            cfgC = self._mk_cfg(tmp, "f2C")
            cfgD = self._mk_cfg(tmp, "f2D")
            pipeC = self._build(cfgC, "https://linkC/v")
            pipeD = self._build(cfgD, "https://linkD/v")

            async def scenario():
                (tmp / "unknown_submits.json").write_text("{}")
                async def wC():
                    try:
                        await pipeC.process("https://linkC/v")
                    except Exception:
                        pass
                async def wD():
                    try:
                        await pipeD.process("https://linkD/v")
                    except Exception:
                        pass
                await asyncio.gather(wC(), wD())

            asyncio.run(scenario())
            data = json.loads((tmp / "unknown_submits.json").read_text(encoding="utf-8"))
            self.assertIn("https://linkC/v", data, "linkC 不得丢失")
            self.assertIn("https://linkD/v", data, "linkD 不得丢失")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_concurrent_write_same_link_consistent(self):
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="rt3f2b-"))
        try:
            cfgA = self._mk_cfg(tmp, "f2bA")
            cfgB = self._mk_cfg(tmp, "f2bB")
            pipeA = self._build(cfgA, "https://same/v")
            pipeB = self._build(cfgB, "https://same/v")

            async def scenario():
                async def wA():
                    try:
                        await pipeA.process("https://same/v")
                    except Exception:
                        pass
                async def wB():
                    try:
                        await pipeB.process("https://same/v")
                    except Exception:
                        pass
                await asyncio.gather(wA(), wB())

            asyncio.run(scenario())
            data = json.loads((tmp / "unknown_submits.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1, "同一链接并发写应只有一条记录")
            self.assertIn("https://same/v", data)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_json_does_not_crash_startup(self):
        import tempfile, shutil

        tmp = Path(tempfile.mkdtemp(prefix="rt3f2c-"))
        try:
            (tmp / "unknown_submits.json").write_text("{corrupted!!")
            cfg = self._mk_cfg(tmp, "f2c")
            pipe = self._build(cfg, "https://x/v")
            self.assertEqual(pipe._unknown, {}, "损坏文件应降级为空")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RT04SemanticsTests(unittest.TestCase):
    """RT04：interval 语义统一为 1.0s 下限（非 0.5 被 max(1.0) 覆盖）。"""

    def _qr(self, interval_raw, expires=120):
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="rt4s-"))
        payload = {"success": True, "data": {
            "verification_uri": "u", "expires_in_s": expires, "interval": interval_raw}}
        cli = tmp / "cli.py"
        cli.write_text(
            "#!" + sys.executable + "\n"
            "import sys, json\n"
            "from pathlib import Path\n"
            f"payload = {payload!r}\n"
            "if '--qrcode-path' in sys.argv:\n"
            "    Path(sys.argv[sys.argv.index('--qrcode-path')+1]).write_bytes(b'QR')\n"
            "print(json.dumps(payload))\n"
        )
        cli.chmod(0o755)
        c = PluginConfig({"cli_command": str(cli), "download": {}, "parsers": {}}, "rt4s")
        c.data_dir = tmp
        acct = CliAccount(c, CliRunner(c, CliBinaryManager(c)))
        qr = asyncio.run(acct.qr_login())
        return qr

    def test_interval_values_safe(self):
        # 数值型：0/负数/极大/极小都必须归一化到安全区间
        for bad in (0, -1, -999999, 999999999, 0.001):
            qr = self._qr(bad)
            self.assertGreaterEqual(qr.interval, 1.0, f"interval={bad} 不得低于下限")
            self.assertLessEqual(qr.interval, 15.0, f"interval={bad} 不得过大")
        # 非有限值用字符串传（避免生成非法 python 字面量）
        for sval in ("nan", "inf", "-inf"):
            qr = self._qr(sval)
            self.assertGreaterEqual(qr.interval, 1.0)
            self.assertLessEqual(qr.interval, 15.0)
        # 语义统一：0 应归一化到下限 1.0（不是被 max(1.0) 悄悄覆盖的 0.5）
        self.assertEqual(self._qr(0).interval, 1.0)
        self.assertEqual(self._qr(3).interval, 3.0)
        self.assertEqual(self._qr(999999999).interval, 15.0)
        # 受剩余有效期约束
        qr_short = self._qr(100, expires=1)
        self.assertLessEqual(qr_short.interval, 15.0)
        self.assertGreater(qr_short.interval, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
