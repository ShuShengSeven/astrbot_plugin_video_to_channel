import asyncio
import json
import os
import re
from collections.abc import AsyncGenerator

from bilibili_api import Credential
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar


class BilibiliLogin:
    """哔哩哔哩登录类"""

    def __init__(self, config: PluginConfig):
        self.credential_file = config.data_dir / "cookies" / "bilibili_credential.json"
        self.raw_cookies = config.parser.bilibili.cookies
        self._credential: Credential | None = None

    def _save_credential(self):
        """存储哔哩哔哩登录凭证"""
        if self._credential is None:
            return
        if not self._credential.has_sessdata():
            logger.warning("哔哩哔哩凭证缺少 SESSDATA, 跳过保存")
            return

        # 先写临时文件再原子替换：并发刷新/中途被杀时不会留下半截 JSON，
        # 否则下次启动读取会一直抛异常，整个 B站解析彻底不可用。
        payload = json.dumps(self._credential.get_cookies(), ensure_ascii=False)
        tmp_path = self.credential_file.with_name(self.credential_file.name + ".tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.credential_file)
        except OSError as e:
            tmp_path.unlink(missing_ok=True)
            logger.warning(f"哔哩哔哩凭证保存失败: {e}")

    def _load_credential(self):
        """从文件加载哔哩哔哩登录凭证"""
        if not self.credential_file.exists():
            return

        try:
            raw = json.loads(self.credential_file.read_text(encoding="utf-8") or "{}")
            if not isinstance(raw, dict) or not raw:
                raise ValueError("凭证文件内容不是非空的 Cookie 字典")
        except (OSError, ValueError, TypeError) as e:
            # 凭证文件损坏时必须降级为“未登录”，否则每次解析都会在读文件时抛错，
            # 用户不手动删文件就永远恢复不了。
            self._credential = None
            logger.error(
                f"哔哩哔哩凭证文件不可用（{self.credential_file}: {e}），本次按未登录处理；"
                "请重新在插件配置中填写 cookies"
            )
            return

        cookies = raw
        if "SESSDATA" not in cookies and "sessdata" in cookies:
            cookies["SESSDATA"] = cookies["sessdata"]
        if "DedeUserID" not in cookies and "dedeuserid" in cookies:
            cookies["DedeUserID"] = cookies["dedeuserid"]
        try:
            credential = Credential.from_cookies(cookies)
        except Exception as e:  # noqa: BLE001
            self._credential = None
            logger.error(
                f"哔哩哔哩凭证文件无法转成 Credential（{e}），本次按未登录处理"
            )
            return
        if credential.has_sessdata():
            self._credential = credential
        else:
            logger.warning(f"哔哩哔哩凭证文件缺少 SESSDATA: {self.credential_file}")

    async def login_with_qrcode(self) -> bytes:
        """通过二维码登录获取哔哩哔哩登录凭证"""
        self._qr_login = QrCodeLogin()
        await self._qr_login.generate_qrcode()

        qr_pic = self._qr_login.get_qrcode_picture()
        return qr_pic.content

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """检查二维码登录状态"""
        scan_tip_pending = True

        for _ in range(30):
            state = await self._qr_login.check_state()
            match state:
                case QrCodeLoginEvents.DONE:
                    yield "登录成功"
                    self._credential = self._qr_login.get_credential()
                    self._save_credential()
                    break
                case QrCodeLoginEvents.CONF:
                    if scan_tip_pending:
                        yield "二维码已扫描, 请确认登录"
                        scan_tip_pending = False
                case QrCodeLoginEvents.TIMEOUT:
                    yield "二维码过期, 请重新生成"
                    break
            await asyncio.sleep(2)
        else:
            yield "二维码登录超时, 请重新生成"

    def _cookies_to_dict(self, cookies_str: str) -> dict[str, str]:
        """将 cookies 字符串转换为字典"""
        res = {}
        text = cookies_str or ""
        # 浏览器插件导出的 Cookie 常是 Netscape 格式（制表符分隔），
        # 这类输入里没有 “key=value;” 结构，直接按请求头解析会得到空字典，
        # 表现为“明明填了 Cookie 却仍然未登录”。
        if CookieJar._is_netscape_cookie_file(text):
            for line in text.splitlines():
                parsed = CookieJar._parse_netscape_cookie_line(line)
                if parsed is None:
                    continue
                _domain, _sub, _path, _secure, _expires, name, value = parsed
                res[name] = value
            return res

        # 从浏览器复制 Cookie 时结尾带 “;”、中间带换行/空格都极其常见，
        # 早先按 “;” 切开后直接解包会让插件抛 ValueError 而整个 B站解析失败。
        for chunk in re.split(r"[;\n\r]+", text):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name = name.strip()
            if name:
                res[name] = value.strip()
        return res

    async def _init_credential(self):
        """初始化哔哩哔哩登录凭证"""
        if not self.raw_cookies:
            self._load_credential()
            return

        credential = Credential.from_cookies(self._cookies_to_dict(self.raw_cookies))
        if await credential.check_valid():
            logger.info(f"`parser_bili_ck` 有效, 保存到 {self.credential_file}")
            self._credential = credential
            self._save_credential()
        else:
            logger.info(f"`parser_bili_ck` 已过期, 尝试从 {self.credential_file} 加载")
            self._load_credential()

    @property
    async def credential(self) -> Credential | None:
        """哔哩哔哩登录凭证"""

        if self._credential is None:
            await self._init_credential()
            return self._credential

        if not self._credential.has_sessdata():
            self._load_credential()
            if self._credential is None or not self._credential.has_sessdata():
                logger.warning("哔哩哔哩凭证缺少 SESSDATA, 请重新登录")
                return None

        if not await self._credential.check_valid():
            logger.warning("哔哩哔哩凭证已过期, 请重新配置")
            return None

        if await self._credential.check_refresh():
            logger.info("哔哩哔哩凭证需要刷新")
            if self._credential.has_ac_time_value() and self._credential.has_bili_jct():
                await self._credential.refresh()
                logger.info(f"哔哩哔哩凭证刷新成功, 保存到 {self.credential_file}")
                self._save_credential()
            else:
                logger.warning(
                    "哔哩哔哩凭证刷新需要包含 `SESSDATA`, `ac_time_value` 项"
                )

        return self._credential
