"""CLI 账号服务：登录二维码、轮询、状态与频道/版块查询。"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import PluginConfig
from .cli_runner import AlreadyLoggedInError, CliError, CliRunner


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass(slots=True)
class QrLoginInfo:
    verification_uri: str
    qrcode_path: Path
    expires_in_s: int
    interval: float


@dataclass(slots=True)
class PollResult:
    authorized: bool
    message: str
    raw: Any = None


@dataclass(slots=True)
class LoginStatus:
    logged_in: bool
    message: str
    raw: Any = None


@dataclass(slots=True)
class GuildSummary:
    role: str
    guild_id: str
    name: str
    number: str = ""
    member_count: str = ""

    def to_line(self) -> str:
        parts = [f"📢 {self.name or '(未命名)'}", f"ID: {self.guild_id}"]
        if self.number:
            parts.append(f"频道号: {self.number}")
        if self.member_count:
            parts.append(f"成员: {self.member_count}")
        return " ｜ ".join(parts)


@dataclass(slots=True)
class ChannelSummary:
    channel_id: str
    name: str

    def to_line(self) -> str:
        return f"📂 {self.name or '(未命名)'} ｜ ID: {self.channel_id}"


# ----------------------------------------------------------------------
# 取值辅助
# ----------------------------------------------------------------------
def unwrap_data(payload: Any) -> Any:
    """兼容 CLI 把业务数据放在 data 字段或直接返回的两种形态。"""
    if isinstance(payload, dict):
        return payload.get("data", payload)
    return payload


def first_value(obj: Any, *keys: str):
    """从 dict 中按多个候选 key 取第一个非空值。"""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return None


def _walk(obj: Any):
    """递归遍历 dict/list。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def contains_true(obj: Any, *keys: str) -> bool:
    """递归查找值为 True 的布尔字段。"""
    for key, value in _walk(obj):
        if key in keys and value is True:
            return True
    return False


def contains_status(obj: Any, expected: str) -> bool:
    """递归查找 status == expected。"""
    for key, value in _walk(obj):
        if key in ("status", "state") and str(value).lower() == expected.lower():
            return True
    return False


# 真实 CLI 可能只以中文 message 表达登录态（如 "已登录，服务连通正常。"），
# 因此把 stdout/stderr 的原始文本也作为正向信号，避免误判为未登录而重复申请二维码。
_POSITIVE_TEXT_MARKERS = ("已登录", "authorized", "logged in", "登录成功", "重新登录请添加 --yes")
_NEGATIVE_TEXT_MARKERS = ("未登录", "not logged", "未授权", "not authorized")


def text_says_logged_in(text: str) -> bool:
    """根据原始输出文本判断是否处于已登录状态。"""
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _NEGATIVE_TEXT_MARKERS):
        return False
    return any(marker in lowered for marker in _POSITIVE_TEXT_MARKERS)


def collect_message(obj: Any) -> str:
    """尽力从结果中提取可读消息。"""
    if not isinstance(obj, dict):
        return ""
    value = first_value(obj, "message", "msg", "status", "connectivity", "error")
    if isinstance(value, dict):
        # 例如 {"error": {"message": "当前已登录…", "type": "internal"}}
        value = first_value(value, "message", "msg") or value
    return str(value or "")


# ----------------------------------------------------------------------
# 账号服务
# ----------------------------------------------------------------------
class CliAccount:
    def __init__(self, cfg: PluginConfig, runner: CliRunner):
        self.cfg = cfg
        self.runner = runner

    async def qr_login(self, *, relogin: bool = False) -> QrLoginInfo:
        """生成登录二维码（返回本地 PNG 路径 + 授权链接）。

        relogin=True 时向 CLI 附加 --yes，用于已登录时覆盖旧凭证重新出码。
        """
        qrcode_path = self.cfg.data_dir / "login_qrcode.png"
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)

        args = ["login", "--json", "--qrcode-path", str(qrcode_path)]
        if relogin:
            args.append("--yes")
        try:
            payload = await self.runner.run_json(args)
        except CliError as e:
            if self._is_already_logged_in_error(e):
                raise AlreadyLoggedInError(
                    "当前已登录腾讯频道 CLI。如需重新登录：先 /v2c logout，"
                    "或直接 /v2c relogin",
                    returncode=e.returncode,
                    stdout=e.stdout,
                    stderr=e.stderr,
                    payload=e.payload,
                ) from e
            raise
        data = unwrap_data(payload)

        verification_uri = str(first_value(data, "verification_uri") or "")
        expires_in_s = int(first_value(data, "expires_in_s") or 120)
        try:
            interval = float(first_value(data, "interval") or 3)
        except (TypeError, ValueError):
            interval = 3.0

        if not qrcode_path.exists():
            qr_b64 = first_value(data, "qr_code", "qrcode")
            if qr_b64:
                try:
                    qrcode_path.write_bytes(base64.b64decode(qr_b64))
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError("CLI 返回的二维码数据无法解码") from e
            else:
                raise RuntimeError("CLI 未返回二维码图片，无法继续登录")

        if not verification_uri:
            verification_uri = "请查看上方二维码扫码登录"

        return QrLoginInfo(
            verification_uri=verification_uri,
            qrcode_path=qrcode_path,
            expires_in_s=max(30, expires_in_s),
            interval=max(1.0, interval),
        )

    @staticmethod
    def _is_already_logged_in_error(e: CliError) -> bool:
        """识别 CLI 的“当前已登录，请加 --yes 重登”类错误（退出码 5）。"""
        text = f"{e}\n{e.stdout}\n{e.stderr}"
        return (
            e.returncode == 5
            or "当前已登录" in text
            or ("已登录" in text and "--yes" in text)
        )

    async def poll_login(self) -> PollResult:
        """轮询一次登录结果（未扫码时不会抛错）。"""
        out = await self.runner.run(["login", "poll-token", "--json"])
        text = f"{out.stdout}\n{out.stderr}".strip()

        try:
            payload = self.runner.parse_json(out.stdout)
        except Exception:  # noqa: BLE001
            payload = None

        authorized = contains_status(payload, "authorized") or (
            payload is None and '"authorized"' in text
        )
        expired = "过期" in text or "已被领取" in text
        message = collect_message(unwrap_data(payload) if payload else {}) or text

        if authorized:
            return PollResult(authorized=True, message=message, raw=payload)
        if expired:
            return PollResult(authorized=False, message="二维码已过期，请重新 /v2c login", raw=payload)
        return PollResult(authorized=False, message=message, raw=payload)

    async def login_status(self) -> LoginStatus:
        """读取当前登录状态（含连通性诊断）。"""
        out = await self.runner.run(["login", "status", "--json"])
        try:
            payload = self.runner.parse_json(out.stdout)
        except Exception:  # noqa: BLE001
            payload = None

        data = unwrap_data(payload) if payload is not None else {}
        raw_text = f"{out.stdout}\n{out.stderr}".strip()
        logged_in = (
            contains_true(data, "logged_in", "is_login", "login")
            or contains_status(data, "authorized")
            or text_says_logged_in(raw_text)
        )
        message = collect_message(data) or raw_text or "未知状态"
        return LoginStatus(logged_in=logged_in, message=message, raw=payload)

    async def logout(self) -> str:
        """清除本地登录凭证。"""
        payload = await self.runner.run_json(["login", "logout", "--json"])
        data = unwrap_data(payload)
        message = (
            str(first_value(data, "message", "msg") or "")
            if isinstance(data, dict)
            else ""
        )
        return message or "已清除登录凭证"

    async def list_guilds(self) -> dict[str, list[GuildSummary]]:
        """获取已加入频道，按 created/managed/joined 分组。"""
        payload = await self.runner.run_json(
            ["manage", "get-my-join-guild-info", "--json"]
        )
        data = unwrap_data(payload)

        role_map = {
            "created_guilds": "我创建的",
            "managed_guilds": "我管理的",
            "joined_guilds": "我加入的",
        }
        result: dict[str, list[GuildSummary]] = {}

        if isinstance(data, dict):
            for key, label in role_map.items():
                items = data.get(key)
                if isinstance(items, list):
                    result[label] = [self._guild_summary(label, item) for item in items]
        if not result:
            # 兼容某些版本直接返回列表
            items = data if isinstance(data, list) else []
            result["我加入的"] = [self._guild_summary("我加入的", item) for item in items]
        return result

    async def list_channels(self, guild_id: str) -> list[ChannelSummary]:
        """获取指定频道下的版块列表。"""
        payload = await self.runner.run_json(
            ["manage", "get-guild-channel-list", "--json", "--guild-id", guild_id]
        )
        data = unwrap_data(payload)

        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("channels", "channel_list", "channelList", "data", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    items = value
                    break

        return [self._channel_summary(item) for item in items if isinstance(item, dict)]

    # ------------------------------------------------------------------
    @staticmethod
    def _guild_summary(role: str, item: dict) -> GuildSummary:
        return GuildSummary(
            role=role,
            guild_id=str(first_value(item, "guild_id", "guildId") or ""),
            name=str(first_value(item, "guild_name", "guildName", "name") or ""),
            number=str(first_value(item, "guild_number", "guildNumber") or ""),
            member_count=str(
                first_value(item, "member_count", "memberCount", "member_num") or ""
            ),
        )

    @staticmethod
    def _channel_summary(item: dict) -> ChannelSummary:
        return ChannelSummary(
            channel_id=str(first_value(item, "channel_id", "channelId") or ""),
            name=str(first_value(item, "channel_name", "channelName", "name") or ""),
        )
