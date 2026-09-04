"""tencent-channel-cli 自托管二进制管理。

首次使用时从 npm registry 下载当前平台的官方二进制到插件数据目录，
无需在 AstrBot 机器上安装 Node.js/npm。
"""
from __future__ import annotations

import asyncio
import io
import os
import platform
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from astrbot.api import logger

from ..core.config import PluginConfig

REGISTRY_URL = "https://registry.npmjs.org/tencent-channel-cli"
REGISTRY_TIMEOUT = aiohttp.ClientTimeout(total=90)

# (sys.platform, platform.machine()) -> npm 平台包后缀
_PLATFORM_MAP: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
    ("darwin", "x86_64"): "darwin-x64",
    ("darwin", "arm64"): "darwin-arm64",
    ("win32", "AMD64"): "win32-x64",
    ("win32", "x86_64"): "win32-x64",
}


@dataclass(slots=True)
class DownloadInfo:
    """一次下载所需的信息。"""

    version: str
    platform_key: str
    package: str
    tarball_url: str


def detect_platform_key() -> str:
    """返回当前平台对应的 npm 平台包后缀，如 linux-x64。"""
    key = (sys.platform, platform.machine())
    mapped = _PLATFORM_MAP.get(key)
    if mapped is None:
        raise RuntimeError(
            f"暂不支持当前平台 {key[0]}/{key[1]}；"
            "tencent-channel-cli 支持 linux-x64/linux-arm64/darwin-x64/darwin-arm64/win32-x64"
        )
    return mapped


def cli_binary_filename() -> str:
    """托管二进制在磁盘上的文件名。"""
    return "tencent-channel-cli.exe" if os.name == "nt" else "tencent-channel-cli"


class CliBinaryManager:
    """负责解析、下载、托管 tencent-channel-cli 二进制。"""

    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg
        self.bin_dir = cfg.data_dir / "bin"
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 路径与模式
    # ------------------------------------------------------------------
    @property
    def managed_bin_path(self) -> Path:
        return self.bin_dir / cli_binary_filename()

    def resolve_existing(self) -> str | None:
        """返回当前可用的二进制路径；找不到时返回 None。"""
        if not self.cfg.cli_managed:
            candidate = Path(self.cfg.cli_command)
            return str(candidate) if candidate.is_file() else None
        path = self.managed_bin_path
        return str(path) if path.is_file() else None

    async def ensure(self, force: bool = False) -> str:
        """确保二进制可用并返回路径；managed 模式下缺失时自动下载。"""
        if not self.cfg.cli_managed:
            existing = self.resolve_existing()
            if existing is not None:
                return existing
            raise RuntimeError(
                f"配置的 tencent-channel-cli 不存在：{self.cfg.cli_command}。"
                "可在插件配置中改为 auto 让插件自动下载。"
            )

        async with self._lock:
            if self.managed_bin_path.is_file() and not force:
                return str(self.managed_bin_path)

            logger.info("[cli] 开始下载托管 tencent-channel-cli 二进制…")
            info = await self._fetch_download_info()
            await self._download(info)
            return str(self.managed_bin_path)

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    async def _fetch_download_info(self) -> DownloadInfo:
        """查询 npm registry，确定最新版本与当前平台包下载地址。"""
        platform_key = detect_platform_key()
        package = f"tencent-channel-cli-{platform_key}"

        async with aiohttp.ClientSession(timeout=REGISTRY_TIMEOUT) as session:
            async with session.get(REGISTRY_URL, proxy=self.cfg.proxy) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"查询 npm registry 失败（HTTP {resp.status}），请检查网络后重试"
                    )
                doc = await resp.json()

        latest = str((doc.get("dist-tags") or {}).get("latest") or "")
        version_meta = (doc.get("versions") or {}).get(latest) or {}
        if not latest or not version_meta:
            raise RuntimeError("无法从 npm registry 获取 tencent-channel-cli 最新版本")

        optional = version_meta.get("optionalDependencies") or {}
        pkg_version = str(optional.get(package) or latest)
        tarball = (
            f"https://registry.npmjs.org/{package}/-/{package}-{pkg_version}.tgz"
        )
        logger.info(
            f"[cli] 平台包: {package}@{pkg_version}（CLI {latest}）"
        )
        return DownloadInfo(
            version=latest,
            platform_key=platform_key,
            package=package,
            tarball_url=tarball,
        )

    async def _download(self, info: DownloadInfo) -> None:
        """下载 tarball 并解压出二进制文件。"""
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        target = self.managed_bin_path
        tmp_path = target.with_name(target.name + ".tmp")

        async with aiohttp.ClientSession(timeout=REGISTRY_TIMEOUT) as session:
            async with session.get(info.tarball_url, proxy=self.cfg.proxy) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"下载 {info.package} 失败（HTTP {resp.status}）"
                    )
                data = await resp.read()

        bin_name = cli_binary_filename()
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                matched = None
                for member in tar.getmembers():
                    normalized = member.name.replace("\\", "/")
                    if member.isfile() and normalized.endswith(f"/bin/{bin_name}"):
                        matched = member
                        break
                if matched is None:
                    raise RuntimeError(
                        f"npm 包内未找到 {bin_name}，可能包结构已变化"
                    )
                content = tar.extractfile(matched)
                if content is None:
                    raise RuntimeError("解压 tencent-channel-cli 失败")
                tmp_path.write_bytes(content.read())

            if os.name != "nt":
                tmp_path.chmod(0o755)
            os.replace(tmp_path, target)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        logger.info(f"[cli] tencent-channel-cli 就绪: {target}")
