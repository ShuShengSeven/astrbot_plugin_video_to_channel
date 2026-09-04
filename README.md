# astrbot_plugin_video_to_channel

监听指定群聊/私聊中的视频平台分享链接（v1：B站、抖音、快手），收到后自动：
1. 解析链接并获取视频标题；
2. 下载视频到本地（带大小/时长限制）；
3. 通过 `tencent-channel-cli` 上传到**指定腾讯频道的指定版块**；
4. 向原会话回执结果。

纯代码调用 `tencent-channel-cli`，不依赖 AI / Agent / OpenClaw。插件可**自动下载并托管** CLI 二进制，无需手动安装 Node.js/npm；登录与频道查询全部可在 AstrBot 私聊中通过 `/v2c` 指令完成。

> 灵感与解析核心移植自 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)（MIT）。本插件保留了原项目的 MIT 版权声明，详见 [LICENSE](LICENSE)。

## 架构

```
群聊/私聊消息
   │  (session_whitelist 过滤 + 直接发链接即触发)
   ▼
main.py (Star 消息入口 + /v2c 指令组)
   │
   ├─ 视频搬运: service/pipeline.py  编排：解析 → 下载 → 上传 → 清理
   │      ├─ service/parser_router.py    关键词/正则 → 平台解析器
   │      ├─ service/channel_uploader.py CLI 上传封装
   │      └─ service/debounce.py         同会话同链接防抖
   └─ CLI 管理: service/cli_binary.py + cli_runner.py + cli_account.py
          ├─ 自动下载托管二进制（cli_command=auto）
          ├─ 扫码登录/状态/频道与版块查询
          └─ /v2c target 写回上传目标配置
   ▼
core/  (移植自 astrbot_plugin_parser)
   ├── parsers/bilibili/   B站解析器
   ├── parsers/douyin/     抖音解析器
   ├── parsers/kuaishou.py 快手解析器（仅视频）
   ├── download.py         流式下载（限大小/时长/重试）
   └── data.py             ParseResult/VideoContent 统一数据结构
```

### 模块化设计

- **新增平台**：把平台解析器目录放到 `core/parsers/`，在 `core/parsers/__init__.py` 导入，并在 `core/config.py` 的 `SUPPORTED_PLATFORMS` / `_PARSER_DEFAULTS` 与 `_conf_schema.json` 中登记即可。
- **更换上传目标/增加命令**：业务逻辑集中在 `service/`，命令入口集中在 `main.py`。

## 环境要求

- AstrBot >= 4.9.2
- Python 3.10+
- `ffmpeg`：B站高清视频（音视频分离）合流时需要
- 下载高清视频可能需要代理；可在插件配置中设置
- `tencent-channel-cli`：**默认 auto 自动下载**（首次执行 `/v2c` 指令或首次上传时从 npm registry 获取当前平台二进制到插件数据目录），也可手动安装后在配置中填命令/绝对路径

## 安装

将本目录放入 AstrBot 的 `data/plugins/` 下（或以插件市场/`git clone` 方式安装），然后在 WebUI 中启用并填写配置。

## 配置

在 AstrBot WebUI 的插件配置面板中填写：

| 配置 | 说明 |
|------|------|
| `session_whitelist` | 允许触发的会话 ID（群聊/私聊）。管理员在目标会话发 `/v2c sid` 获取。留空 = 不触发 |
| `target_guild_id` | 目标腾讯频道 ID（也可用 `/v2c target` 在聊天中设置） |
| `target_channel_id` | 目标版块 ID（也可用 `/v2c target` 在聊天中设置） |
| `cli_command` | `auto`（默认，自动托管）或外部命令/绝对路径 |
| `download.*` | 大小/时长/超时/重试/代理 |
| `parsers.bilibili` | B站开关、Cookie、清晰度、编码 |
| `parsers.douyin` | 抖音开关、Cookie（可选） |
| `parsers.kuaishou` | 快手开关、Cookie（可选） |

## 私聊管理指令（ADMIN + 私聊）

| 指令 | 说明 |
|------|------|
| `/v2c login` | 自动准备 CLI → 发送登录二维码图片与授权链接 → 扫码后自动提示登录成功（已登录时提示使用 logout / relogin） |
| `/v2c relogin` | 已登录时强制覆盖旧凭证重新出码（等价 CLI `login --yes`） |
| `/v2c status` | 查看 CLI 来源/路径/版本、登录状态、当前上传目标 |
| `/v2c guilds` | 列出当前账号已加入的频道（我创建的/我管理的/我加入的，含 guild_id） |
| `/v2c channels <guild_id>` | 列出指定频道的版块（含 channel_id） |
| `/v2c target <guild_id> <channel_id>` | 直接设置上传目标并持久化 |
| `/v2c logout` | 清除本地登录凭证 |
| `/v2c sid` | 显示当前会话 ID（旧版 `/v2c_sid` 仍可用） |

推荐首次使用流程：

```
/v2c login        → 扫码，自动收到“登录成功”
/v2c guilds       → 找到目标频道的 guild_id
/v2c channels <guild_id> → 找到目标版块的 channel_id
/v2c target <guild_id> <channel_id>
/v2c status       → 确认一切就绪
```

> 若 CLI 已处于登录态，`/v2c login` 不会重复出码，会提示你先 `/v2c logout` 或使用 `/v2c relogin` 覆盖旧凭证。
>
> 本插件与万能解析器（`astrbot_plugin_parser` 等带 ALL 监听器的插件）可共存：`/v2c` 指令以高优先级执行并在结束后终止事件传播，因此 `/v2c channels <18位ID>`、`/v2c target <ID> <ID>` 中的长数字不会被其他插件误当作视频 ID 抢解析。

## 使用

在已配置的群聊/私聊中**直接发送**受支持的链接（无需 @机器人）：

- B站：`https://b23.tv/xxx`、`BV1xxxx`、`av123456`、完整视频页等
- 抖音：分享口令中的 `https://v.douyin.com/xxx`、`https://www.douyin.com/video/<id>` 等
- 快手：`https://v.kuaishou.com/xxx` 短链、`https://www.kuaishou.com/short-video/<id>`、`https://v.m.chenzhongtech.com/fw/...` 等

插件会回执“开始解析…”，完成后回执上传结果（含分享链接）。

## 行为说明与限制

- 上传帖子内容默认使用解析出的视频标题作为正文；单条视频时按腾讯频道规则不强制长帖标题，CLI 会根据视频参数自动处理。
- 图文、音频、多视频等解析结果 v1 不搬运（会明确提示），后续可扩展；其中快手图文/图集会直接提示“暂不支持，仅搬运视频”。
- 同一链接在防抖窗口内重复发送会被静默跳过。
- 上传成功后会清理本地临时文件（位于 AstrBot `data/plugin_data/astrbot_plugin_video_to_channel/cache/`）；托管 CLI 位于同目录 `bin/`。

## 开发

```bash
# 语法检查（无需安装 AstrBot 依赖）
python3 -m py_compile main.py core/*.py core/parsers/*.py core/parsers/bilibili/*.py core/parsers/douyin/*.py service/*.py

# 服务层单元测试（使用 fake CLI，无需真实登录/网络）
python3 tests/test_service.py

# 格式化建议使用 ruff
ruff format .
```

## License

[MIT](LICENSE)
