"""统一 CLI 子进程执行器：负责二进制定位、超时、JSON 解析与错误归一化。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from astrbot.api import logger

from ..core.config import PluginConfig
from .cli_binary import CliBinaryManager


class CliError(RuntimeError):
    """CLI 执行/解析错误，携带原始输出便于上层做容错重试。"""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        payload=None,
    ):
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.payload = payload


class AlreadyLoggedInError(CliError):
    """CLI 当前已登录；需要先登出或用 --yes 覆盖重新登录。"""


@dataclass(slots=True)
class RunOutput:
    """原始命令输出。"""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


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
            binary = await self.manager.ensure()
        else:
            binary = self.manager.resolve_existing()
            if binary is None:
                raise CliError("tencent-channel-cli 尚未就绪，请先执行 /v2c login 或重试")

        argv = (binary, *args)
        timeout = timeout or self.cfg.cli_timeout

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
            raise CliError(
                f"tencent-channel-cli 执行超时（>{timeout}s）",
                returncode=None,
            ) from None

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
            raise CliError(
                f"tencent-channel-cli 返回错误：{error}",
                returncode=out.returncode,
                stdout=out.stdout,
                stderr=out.stderr,
                payload=payload,
            )
        if not isinstance(payload, dict):
            raise CliError(
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
            raise CliError(f"tencent-channel-cli 返回了非 JSON 内容: {text[:500]}")

    @staticmethod
    def _find_business_error(payload) -> str | None:
        """识别 CLI 的业务错误（success=false / retCode 非 0）。"""
        if not isinstance(payload, dict):
            return None

        if payload.get("success") is False:
            return str(payload.get("message") or payload.get("error") or payload)

        data = payload.get("data", payload)
        if isinstance(data, dict):
            ret_code = data.get("retCode", data.get("retcode"))
            if ret_code not in (None, 0):
                return (
                    f"retCode={ret_code}："
                    f"{data.get('msg') or data.get('message') or data.get('error') or payload}"
                )
            if data.get("success") is False:
                return str(
                    data.get("message") or data.get("error") or data
                )
        return None

    @staticmethod
    def log_run(out: RunOutput, label: str = "") -> None:
        logger.debug(f"[cli] {label or ' '.join(out.args[1:])}: rc={out.returncode}")
