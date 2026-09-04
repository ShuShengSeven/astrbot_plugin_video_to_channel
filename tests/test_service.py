"""服务层单元测试（不依赖真实 AstrBot / 真实 CLI / 网络）。

通过注入最小 astrbot/aiohttp stub + 一个返回固定 JSON 的 fake CLI，
覆盖 CLI 托管、登录二维码、轮询、频道/版块解析、目标配置写回与错误归一化。
"""
from __future__ import annotations

import asyncio
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
def _install_stubs():
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
    path_mod._DATA = Path(tempfile.gettempdir()) / "v2c-test-data"
    path_mod.get_astrbot_data_path = lambda: str(path_mod._DATA)
    utils.astrbot_path = path_mod
    core.utils = utils
    astrbot.api = api
    astrbot.core = core
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.utils"] = utils
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = lambda *a, **k: object()
    aiohttp.ClientSession = object
    aiohttp.ClientError = RuntimeError
    sys.modules["aiohttp"] = aiohttp

_install_stubs()

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
import json, os, sys
from pathlib import Path

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)

args = sys.argv[1:]

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
