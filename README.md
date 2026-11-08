# 更好的点歌

**AstrBot 的 QQ 点歌插件**，可搜索并分段发送网易云音乐、酷狗音乐和 QQ 音乐的歌曲。可选接入外部兼容 HTTP 服务，为歌曲生成翻唱语音。普通点歌无需配置推理服务，也不要求额外调用 LLM。

- 版本：1.1.1（修复设置页无法保存“边生成边发送”开关的问题。）
- [源码仓库](https://github.com/Nortea575/astrbot_plugin_better_music)
- [翻唱服务接入与接口说明](cover_deployment.md)

## 界面预览

![音乐账号登录页](assets/provider-login.png)

*界面示例：未接入真实账号。*

![插件设置页](assets/plugin-settings.png)

*界面示例：主动演唱分页，音色与参数为演示数据。*

## 主要功能

- 在“聚合登录”页登录或退出网易云音乐、酷狗音乐和 QQ 音乐账号；点击登录按钮后才会生成二维码，并显示登录与会员状态。
- 在聊天中搜索歌曲、查看最多五条候选，选择后下载并按顺序分段发送。
- 可选启用主动 LLM：在 AstrBot 的 LLM 对话中，由模型决定是否调用唱歌工具；这会暂停固定点歌指令。
- 可选启用被动 LLM：向当前会话的后续对话提供已发送歌曲的时间与先后记录，不额外触发一次模型调用。
- 自定义聊天回复、回复提示开关、点歌日志目标和分段播放限制。
- 插件设置按被动 LLM、主动 LLM、翻唱服务、日志与存储分页管理；可选为主动演唱开启边生成边发送。
- 在“文件管理”页按平台查看文件，手动发送原曲或可用翻唱缓存；本机已有的歌曲与翻唱结果可复用。

## 快速开始

### 环境要求

- AstrBot 4.29.3 或更高版本，并配置可用于 QQ 的消息平台。已适配 SnowLuma；其他 OneBot/QQ 实现可自行尝试。
- 可运行的 FFmpeg 与 FFprobe，以及 Node.js 22 或更高版本。环境页可检查依赖并补齐部分工具。
- AstrBot 插件依赖：QQ 音乐登录所用的 Python 包、二维码生成包和异步 HTTP 客户端；AstrBot 可根据 `requirements.txt` 安装。

普通点歌不需要 LLM 或翻唱推理服务。主动 LLM 还需支持工具调用（tool calling）的模型，以及符合接口约定的外部翻唱服务和可用音色。

### 安装

在 AstrBot 插件管理中使用[本仓库地址](https://github.com/Nortea575/astrbot_plugin_better_music)安装；也可以下载发布 ZIP，将插件文件解压到名为 `astrbot_plugin_better_music` 的目录，再放入 AstrBot 插件目录。两种方式任选其一。

安装后启动或重启 AstrBot，在插件管理页启用插件，再从侧边栏打开“更好的点歌”。进入“环境监测”页，按需安装或配置 FFmpeg、FFprobe、Node.js 和音乐平台依赖；再到“聚合登录”页完成所需平台的登录。

环境页提供常规依赖检查和安装。FFmpeg、Node.js 的默认下载包覆盖 Windows/Linux x64；其他系统或架构需自行提供匹配的工具包。自定义下载地址必须对应当前系统并符合工具要求，不能视为所有系统均可一键安装。

## 使用说明

### 聊天点歌

以下 `%A` 仅作示例，请替换为当前 AstrBot 会话配置的唤醒前缀：

```text
%A点歌
%A搜索 歌名
%A播放 1
```

关闭主动 LLM 时，固定指令按“网易云音乐 → 酷狗音乐 → QQ 音乐”的顺序查找已验证登录的账号，只搜索优先级最高的一个平台，并返回最多五条结果。播放序号须在五分钟内、由发起搜索的同一会话和用户选择；超时或重新搜索后请使用新结果。

启用主动 LLM 后，固定点歌指令会暂停。在 AstrBot 的 LLM 对话中，模型决定是否调用唱歌工具；调用后会搜索歌曲、选择候选并提交翻唱，后台按序分段发送。

被动 LLM 与主动 LLM 使用独立开关。被动模式按当前会话保存点歌时间与顺序，供后续正常对话参考；它本身不额外发起模型调用。被动播放时长和片段间隔只影响聊天点歌，主动演唱使用独立的限制设置。

### 分段、缓存与日志

音频会转换并切成每段不超过 60 秒的语音。配置片段间隔后，插件会在上一段发送成功后等待再发送下一段。主动与被动播放的时长上限和间隔可分别设置。

“插件设置 → 主动 LLM”提供“边生成边发送”，默认关闭。开启后，支持增量输出的翻唱服务每完成一个片段，插件就会接收并按顺序发送；首段仍需等待模型加载和该片段推理。后续片段须同时满足“已生成”和“距离上一段发送成功已达到所设间隔”才会发送，推理耗时较长时，实际间隔可能更长。只有整首生成并校验成功，结果才会成为可复用的翻唱缓存。

连接的服务未声明增量输出能力时，插件继续等待完整翻唱结果后分段发送，连接地址、密钥和音色无需更改。此开关仅影响主动 LLM 演唱；普通点歌与文件管理中的手动发送沿用原流程。

所有可变数据保存在 AstrBot 为插件提供的专属数据目录中。实际位置由框架接口定位，默认通常为 AstrBot 工作目录下的 `data/plugin_data/astrbot_plugin_better_music/`。

```text
data/plugin_data/astrbot_plugin_better_music/
├─ settings.json                 # 插件设置，包括翻唱服务客户端 URL 和 token
├─ accounts/                     # 音乐平台登录凭证
├─ providers/                    # 平台依赖、配置与缓存
├─ tools/                         # 插件安装的 Node.js、FFmpeg 等工具
├─ runtime/                       # 临时文件与运行缓存
├─ passive_music_events.json     # 会话点歌记录
├─ music_usage.json              # 按会话、日期保存的成功点歌次数
└─ downloads/                    # 原曲、语音分段与翻唱缓存
```

同一歌曲的原曲与翻唱归在同一歌曲目录。本地已有的歌曲和适配当前设置的翻唱缓存可复用。默认最多保留 30 首歌曲，按所有音乐平台合计；超出上限时按下载时间清理较早的歌曲及其文件。外部翻唱服务的模型和服务端密钥由该服务自行管理；插件本地只保存客户端连接 URL 和 token。

聊天回复模板可编辑，每类回复可以单独启用或关闭。点歌日志也有独立开关；启用后需指定机器人实例及接收日志的私聊或群聊目标。

升级、重装或卸载前如需保留设置、登录状态、歌曲或缓存，请备份整个 AstrBot 插件专属数据目录。1.0.0 将 `data/` 和 `downloads/` 放在插件安装目录内；升级到 1.0.1 前请先备份旧目录，并手动将原 `data/` 内容整理到上述标准数据目录、将原 `downloads/` 内容放入标准目录下的 `downloads/`。1.0.1 不会自动搬迁旧版数据；确认整理完成后再移除旧插件目录。

### 翻唱服务

翻唱是可选功能。插件会把歌曲音频提交给外部兼容 HTTP 服务，并按约定接收结果及确认清理。仓库不提供该服务的实现、推理模型或训练权重；普通点歌不受此项影响。并非任意 RVC 服务都能直接连接，服务必须实现[接口说明](cover_deployment.md)中约定的版本、任务状态和 ACK/DELETE/TTL 清理行为。

歌曲搜索和播放能力受各音乐平台、账号权限及其规则限制；插件不绕过会员权限或平台风控。

### 插件间配额接口

插件预留了进程内配额接口，供 Better_balance 等插件查询点歌用量并决定是否放行。它没有配置面板，也不提供默认次数上限；未注册判定器时，聊天点歌照常运行。点歌统计与 LLM 聊天配额相互独立。

统计按 `event.unified_msg_origin` 区分机器人实例及群聊／私聊会话，不随 LLM 对话切换重置。日期采用北京时间自然日，并在请求开始时固定：成功发送至少一段语音计一次，多段仍只计一次，本地缓存播放同样计数；完全失败或未发送语音的取消任务不计。菜单、搜索、环境测试和管理页手动发送不计入聊天点歌用量。

通过 `context.get_registered_star("astrbot_plugin_better_music")` 获取已启用插件的元数据，再调用其 `star_cls.get_music_quota_api()` 获取 API v2。调用方应先确认插件已就绪且提供此方法；旧版本可能没有该接口。

| 接口 | 用途 |
| --- | --- |
| `api.api_version` | 当前为整数 `2` |
| `await api.get_usage(session_id, day=None)` | 查询用量，返回 `session_id`、`day`、`timezone`、`used`、`reserved` 等字段；`day` 为 `YYYY-MM-DD`，省略时查询当前日期 |
| `api.register_checker(owner, callback, *, on_commit=None, on_release=None)` | 注册唯一的异步放行判定器和可选结算回调，返回注册令牌；`owner` 为调用方插件名 |
| `api.unregister_checker(token)` | 注销对应注册；过期令牌不能移除后续的新注册 |
| `await api.reserve(session_id, *, source, kind, actor_id=None, scope=None)` | 预占一次点歌；`scope` 是可选的原始平台和目标信息 |

判定回调接收请求快照，包括 `request_id`、`session_id`、`day`、`used`、`reserved`、`actor_id`，以及 `source`（`command` 或 `active_llm`）、`kind`（`original` 或 `cover`）和 `scope`。`scope` 格式为 `{"platform_id": "qq", "kind": "group", "target_id": "群号"}`；私聊时 `kind` 为 `private`，`target_id` 为用户 QQ 号。它取自实际事件的 ID，不依赖会话字符串。回调返回 `{"allowed": true}` 放行，或 `{"allowed": false, "reason": "拒绝说明"}` 拒绝；如在放行时创建了外部预占，可附加不透明字符串 `reservation_id`。回调内可以调用 `get_usage`，应传入请求中的 `day`，使跨日请求采用同一统计周期。例如判定某会话是否达到调用方设定的 `limit`：

```python
async def check_music(request):
    usage = await api.get_usage(request["session_id"], day=request["day"])
    return {
        "allowed": usage["used"] + usage["reserved"] < limit,
        "reason": "当前会话的今日点歌次数已用完。",
    }

token = api.register_checker("astrbot_plugin_better_balance", check_music)
```

上例中的 `api` 为已获取的配额接口，`limit` 由调用方策略提供。Better_music 会在下载或推理前完成判定并预占，首段发送成功后将预占转为成功次数，未成功发送时释放预占。并发判定会看到其他在途请求的 `reserved`，不会因尚未生成语音而遗漏已占名额。判定器异常、超时或返回无效结果时，本次请求停止执行。

如判定器在外部额度服务创建了预占，应同时提供 `on_commit(request)` 和 `on_release(request)`。回调收到原请求字段及可选的 `reservation_id`，并可按 `request_id` 幂等处理：首段音频发送成功后只调用一次 `on_commit`；发送失败、取消或未能进入播放时只调用一次 `on_release`。判定注册被替换或注销后，已放行的请求仍由原注册的回调结算；Music 卸载时会释放尚未发送的预占。回调在 Music 内部状态锁之外运行。

调用方卸载时应执行 `api.unregister_checker(token)`；Better_music 重载后，会在插件生命周期中与已加载的 Balance 握手，随后由调用方注册接口。成功次数保存在插件专属数据目录内的 `music_usage.json`，重启后保留；未完成的预占只在内存中，重启后释放。

用量文件损坏、不受支持或无法读取时，查询会报告不可用，相关点歌请求停止执行，并保留原文件，避免把异常数据当成零次数。修复文件后需重载插件。

## 反馈、授权与依赖

问题反馈请提交到[项目 Issues](https://github.com/Nortea575/astrbot_plugin_better_music/issues)。插件作者：Xing。

本插件按 **GPL-3.0** 授权，完整许可文本见 [`LICENSE`](LICENSE)。

依赖与致谢：网易云音乐接口 [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)（MIT）；QQ 音乐接口 [L-1124/QQMusicApi](https://github.com/L-1124/QQMusicApi)（GPL-3.0-or-later）；酷狗音乐接口 [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（MIT）；Python 包 [qrcode](https://pypi.org/project/qrcode/) 和 [aiohttp](https://docs.aiohttp.org/en/stable/)。各依赖均按其自身许可使用。
