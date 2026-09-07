"""统一 CLI 子进程执行器：负责二进制定位、超时、JSON 解析与错误归一化。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from astrbot.api import logger

from ..core.config import PluginConfig
from .cli_binary import CliBinaryManager


class CliError(RuntimeError):
    """CLI 执行/解析错误，携带原始输出便于上层做容错重试。

    ``definite``：True 表示这是服务端/CLI 的明确业务拒绝，能够确认没有创建帖子；
    False（默认）表示通信/解析/未知层错误，上层不得据此放开防抖（RT-02）。
    """

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        payload=None,
        definite: bool | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.payload = payload
        # RT02-F1：非零退出码本身不能证明“服务器没有创建帖子”——CLI 可能在
        # 成功建帖后被 kill（rc=137）或崩溃。只有结构化业务 payload 明确表达
        # “服务器拒绝了请求”才视为 definite（确定没建帖）。通信层错误一律非 definite。
        if definite is None:
            self.definite = bool(self._find_error_payload(payload))
        else:
            self.definite = definite

    @staticmethod
    def _find_error_payload(payload) -> object | None:
        if not isinstance(payload, dict):
            return None
        if _is_false(payload.get("success")):
            return payload
        data = payload.get("data")
        if isinstance(data, dict):
            if _as_error_code(data.get("retCode", data.get("retcode"))) is not None:
                return data
            if _is_false(data.get("success")):
                return data
        return None


class CliOutputError(CliError):
    """CLI 进程已执行、但输出无法解读（非 JSON / 结构异常 / 空结果等）。

    对投稿类命令而言，进程已经拿到视频并开始处理，输出无法解读不代表服务端
    没有创建帖子 —— 上层必须按「结果未知」对待，绝不能据此放开防抖允许重发。
    """


class AlreadyLoggedInError(CliError):
    """CLI 当前已登录；需要先登出或用 --yes 覆盖重新登录（仅限登录命令）。"""


class CliTimeoutError(CliError):
    """CLI 执行超时。

    对上传类命令而言超时只代表「结果未知」（帖子可能已经发出），
    上层需要据此避免让用户盲目重试造成重复发帖。
    """


class CliSpawnError(CliError):
    """CLI 进程从未成功启动（二进制准备失败或 spawn 阶段失败）。

    进程从未运行 => 请求必然没有到达腾讯频道服务端。对投稿命令而言这属于
    DEFINITE_FAILURE（确定没有产生帖子），而不是「结果未知」：上层可以据此
    放开防抖、允许同一链接立即重试。

    与 CliTimeoutError / CliOutputError 严格区分：后两者表示 CLI 已经启动、
    结果无法核对（UNKNOWN），绝不能据此放宽防抖。
    """

    def __init__(self, message: str, **kwargs):
        # 语义上进程从未启动必然 = 确定失败，强制 definite=True，
        # 不允许调用方把它构造回“结果未知”的普通 CliError。
        kwargs["definite"] = True
        super().__init__(message, **kwargs)


@dataclass(slots=True)
class RunOutput:
    """原始命令输出。"""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _is_false(value: object) -> bool:
    """把「success 字段表达失败」的多种写法归一化。

    上游 JSON 形态会变（false / 0 / "false"），只判断 `is False`
    会把已经失败的调用当成成功，进而向用户回执「✅ 已上传」。
    字段缺失（None）时不猜测，返回 False。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "no"}
    return False


def _as_error_code(value: object) -> object | None:
    """把 retCode 归一化：无错误返回 None，有错误返回可展示的原值。

    `"0"` 这类字符串数字必须与 0 等价，否则会把成功调用误判为失败。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value or None
    if isinstance(value, (int, float)):
        return None if value == 0 else value
    if isinstance(value, str):
        text = value.strip()
        try:
            number = int(text)
        except ValueError:
            # 非数字取值：只放行明确表达成功的写法，其余按异常码处理
            return None if text.lower() in {"ok", "success", "true"} else value
        return None if number == 0 else value
    return value


class CliRunner:
    """以子进程方式运行 tencent-channel-cli 并解析输出。"""

    def __init__(self, cfg: PluginConfig, manager: CliBinaryManager):
        self.cfg = cfg
        self.manager = manager

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def run(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        input_data: str | None = None,
        ensure: bool = True,
    ) -> RunOutput:
        """执行 CLI 命令，不因返回码非 0 抛异常（由调用方决定如何处理）。"""
        if ensure:
            try:
                binary = await self.manager.ensure()
            except Exception as e:  # noqa: BLE001
                # 二进制未就绪 => CLI 从未启动 => 确定没有产生投稿。
                # asyncio.CancelledError 是 BaseException，不会被这里捕获，
                # 仍按取消语义向上传播。
                raise CliSpawnError(f"tencent-channel-cli 未能启动：{e}") from e
        else:
            binary = self.manager.resolve_existing()
            if binary is None:
                raise CliError("tencent-channel-cli 尚未就绪，请先执行 /v2c login 或重试")

        argv = (binary, *args)
        timeout = timeout or self.cfg.cli_timeout
        proc = None

        try:
            if input_data:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except OSError as e:
            # spawn 阶段失败（FileNotFoundError / PermissionError / ENOEXEC 等）：
            # 子进程没有成功运行，请求必然没到服务端 => 确定失败，可安全重试。
            # 注意：CLI 一旦启动之后的任何错误（超时/崩溃/输出损坏）都不得走这里。
            raise CliSpawnError(f"tencent-channel-cli 进程启动失败：{e}") from e

        try:
            if input_data:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input_data.encode("utf-8")), timeout=timeout
                )
            else:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise CliTimeoutError(
                f"tencent-channel-cli 执行超时（>{timeout}s）",
                returncode=None,
            ) from None
        except asyncio.CancelledError:
            # 任务被取消（如插件重载/停止）时也要回收子进程，避免 CLI 残留
            if proc is not None:
                proc.kill()
                await proc.wait()
            raise

        return RunOutput(
            args=argv,
            returncode=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace").strip(),
            stderr=stderr.decode("utf-8", errors="replace").strip(),
        )

    async def run_json(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        input_data: str | None = None,
        ensure: bool = True,
    ) -> dict:
        """执行命令并解析 JSON；返回码非 0 或业务错误时抛 CliError。"""
        out = await self.run(
            args, timeout=timeout, input_data=input_data, ensure=ensure
        )

        if out.returncode != 0:
            raise CliError(
                f"tencent-channel-cli 退出码 {out.returncode}："
                f"{out.stderr or out.stdout}",
                returncode=out.returncode,
                stdout=out.stdout,
                stderr=out.stderr,
            )

        payload = self.parse_json(out.stdout)
        error = self._find_business_error(payload)
        if error:
            # 服务端明确拒绝（success=false / retCode 非 0 + 业务 payload）：
            # 这是「确定没有创建帖子」的证据，标记 definite=True 供上层释放防抖。
            raise CliError(
                f"tencent-channel-cli 返回错误：{error}",
                returncode=out.returncode,
                stdout=out.stdout,
                stderr=out.stderr,
                payload=payload,
                definite=True,
            )
        if not isinstance(payload, dict):
            raise CliOutputError(
                f"tencent-channel-cli 返回结构异常: {out.stdout[:500]}",
                stdout=out.stdout,
                stderr=out.stderr,
                payload=payload,
            )
        return payload

    # ------------------------------------------------------------------
    # 解析与错误识别
    # ------------------------------------------------------------------
    @staticmethod
    def parse_json(text: str):
        """容错解析 CLI 输出 JSON。"""
        text = (text or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # CLI 可能混入日志/提示文本；尝试截取第一个 { 到最后一个 }
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass
            raise CliOutputError(f"tencent-channel-cli 返回了非 JSON 内容: {text[:500]}")

    @staticmethod
    def _find_business_error(payload) -> str | None:
        """识别 CLI 的业务错误（success=false / retCode 非 0）。"""
        if not isinstance(payload, dict):
            return None

        if _is_false(payload.get("success")):
            return str(payload.get("message") or payload.get("error") or payload)

        data = payload.get("data", payload)
        if isinstance(data, dict):
            ret_code = _as_error_code(data.get("retCode", data.get("retcode")))
            if ret_code is not None:
                return (
                    f"retCode={data.get('retCode', data.get('retcode'))}："
                    f"{data.get('msg') or data.get('message') or data.get('error') or payload}"
                )
            if _is_false(data.get("success")):
                return str(
                    data.get("message") or data.get("error") or data
                )
        return None

    @staticmethod
    def log_run(out: RunOutput, label: str = "") -> None:
        logger.debug(f"[cli] {label or ' '.join(out.args[1:])}: rc={out.returncode}")
