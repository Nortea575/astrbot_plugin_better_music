# 翻唱服务接入与接口说明

## 适用范围

本插件只包含 HTTP 客户端，不包含翻唱推理服务、模型、依赖或部署脚本。要使用翻唱功能，需自行提供兼容本说明的 HTTP 服务；第三方服务也必须完整实现下述 API、管线版本和临时任务清理约定。普通点歌不依赖翻唱服务。

插件将完整歌曲音频上传给服务处理。请只处理有权使用的音频和音色文件，并限制服务的网络访问与数据留存。

## 配置连接

在插件管理页的翻唱服务设置中填写服务根地址和访问密钥。地址填写到主机与端口即可，插件会在其后请求 `/v1/...`；若配置了密钥，插件会发送 `Authorization: Bearer <密钥>`。不配置密钥时不会发送该请求头。

- 同机运行：AstrBot 与服务进程位于同一主机和网络命名空间时，可填 `http://127.0.0.1:17866`。
- 跨机器：填写 AstrBot 主机可访问的服务地址。建议使用内网地址或 HTTPS，并通过防火墙限制来源；服务端和插件设置中的密钥需一致。
- 容器：AstrBot 与服务容器在同一容器网络时，使用服务名和容器端口，例如 `http://cover-service:17866`。从一个容器访问另一个容器时，`127.0.0.1` 指向调用方容器自身。若 AstrBot 在容器外，应填写已发布且可访问的主机地址。

插件与服务之间通过 HTTP 上传和下载文件，不要求共享文件系统。

接入原有服务时不需要改变地址、访问密钥或音色配置。增量输出是可选能力；未实现该能力的兼容服务仍使用完整结果接口。

## 兼容性要求

所有 JSON 响应（包括错误响应）都必须包含数值型 `api_version: 1`。健康响应还必须声明字符串型 `pipeline_version: "2"`，并确认任务 ACK、取消与临时任务能力：

```json
{
  "api_version": 1,
  "ready": true,
  "pipeline_version": "2",
  "capabilities": {
    "job_ack": true,
    "job_cancel": true,
    "ephemeral_jobs": true
  }
}
```

服务就绪时返回 `ready: true`；未就绪时应返回 `ready: false`。插件短片段测试只对已就绪服务提交任务。插件只接受 `pipeline_version` 为 `"2"` 且三个能力均为 `true` 的服务。该版本标识音频处理与缓存格式；音色的 `version` 则应在对应模型或索引内容变化时更新。

如果服务启用了鉴权，所有端点都应验证 Bearer 密钥。HTTP 服务的访问应限于可信网络；跨公网连接应使用 HTTPS。

支持边生成边发送的服务还应在 `capabilities` 中声明 `"incremental_segments": true`。插件仅在用户启用此功能且服务明确声明支持后，才提交增量模式字段；不能仅根据服务版本号推断支持。该扩展沿用 API v1、管线 v2，三个原有清理能力仍为必需。

## API

所有路径均相对于配置的服务根地址，前缀为 `/v1`。除音频下载外，响应均为带有 `api_version: 1` 的 JSON。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /v1/health` | 返回 API、管线版本及任务清理能力 |
| `GET /v1/voices` | 返回可用音色列表 |
| `POST /v1/voices` | 导入音色 |
| `POST /v1/jobs` | 提交翻唱任务 |
| `GET /v1/jobs/{job_id}` | 查询任务状态 |
| `GET /v1/jobs/{job_id}/audio` | 下载已完成的 WAV 音频 |
| `GET /v1/jobs/{job_id}/segments/{sequence}/audio` | 可选：下载已生成的指定片段 |
| `POST /v1/jobs/{job_id}/segments/{sequence}/ack` | 可选：确认接收并释放指定片段 |
| `POST /v1/jobs/{job_id}/ack` | 确认接收结果并释放任务数据 |
| `DELETE /v1/jobs/{job_id}` | 取消或丢弃任务并释放任务数据 |

### 音色

`GET /v1/voices` 返回 `{"api_version":1,"voices":[...]}`。每个可选音色至少提供非空字符串 `id`、`name` 和 `version`。`version` 必须随模型或索引内容变化。可额外返回 `sample_rate`、`rvc_version`、`f0_enabled` 等描述字段。

`POST /v1/voices` 使用 `multipart/form-data`：必填字段 `name`、`model`，可选字段 `index`。成功时返回 HTTP 201 和 `{"api_version":1,"voice":{"id":"...","name":"...","version":"..."}}`。

### 提交与查询任务

`POST /v1/jobs` 使用 `multipart/form-data`，字段如下：

- `file`：待处理的完整音频文件。
- `voice_id`：`GET /v1/voices` 返回的音色编号。
- `max_seconds`：结果时长上限，`0` 表示不限制。
- `request_id`：本次请求的唯一编号。
- `output_mode`：可选，仅在健康检查声明支持时使用值 `incremental`，请求逐段生成与交付。不传时必须保持原有整首流程。

服务接受任务后返回 HTTP 200 或 202，并提供 `job_id`；允许直接放在根对象，也允许放在 `job.job_id` 或 `job.id`。例如：

```json
{"api_version":1,"job_id":"job-123","status":"queued"}
```

插件用 `GET /v1/jobs/{job_id}` 轮询。状态对象可直接放在根对象或 `job` 字段中；状态可为 `queued`、`pending`、`running`、`processing`、`separating`、`converting`、`mixing`、`completed` 或 `failed`。成功结果必须在任务对象本身提供与提交一致的 `voice_id`、`voice_version` 和 `pipeline_version: "2"`。若任务对象包含 `result` 对象，该对象也必须提供 `pipeline_version: "2"`。例如：

```json
{
  "api_version": 1,
  "job": {
    "status": "completed",
    "voice_id": "voice-1",
    "voice_version": "model-v1",
    "pipeline_version": "2",
    "result": {"pipeline_version": "2"}
  }
}
```

失败响应应提供简短的 `error` 或 `message`。

### 可选：逐段生成与交付

增量任务在仍处于处理中时就可以发布已完成片段。任务状态除原有身份、音色与管线字段外，还应提供以下字段；状态对象仍可位于根对象或 `job` 内：

```json
{
  "api_version": 1,
  "status": "converting",
  "voice_id": "voice-1",
  "voice_version": "model-v1",
  "pipeline_version": "2",
  "output_mode": "incremental",
  "segment_seconds": 30,
  "total_segments": 3,
  "segments": [
    {
      "sequence": 1,
      "start_seconds": 0,
      "duration_seconds": 30,
      "size_bytes": 7938044,
      "sha256": "文件内容的64位小写十六进制SHA-256",
      "released": false
    }
  ]
}
```

`sequence` 从 1 连续递增，时间范围连续且不得重叠。`segment_seconds` 为本任务采用的分段长度，须大于 0 且不超过 60 秒，并在任务中保持不变。`total_segments` 是裁切播放时长后的片段总数；`segments` 按序列出已完成片段，包括已经确认释放的元数据。每段时长须大于 0 且不超过声明的分段长度，末段可较短。片段发布后，其内容和身份元数据必须保持不变；只有全部片段生成成功，任务才能进入 `completed`。失败或取消不能伪装为完整结果。

`GET /v1/jobs/{job_id}/segments/{sequence}/audio` 返回指定片段的 WAV 文件，使用 `Content-Type: audio/wav`。插件先校验大小、SHA-256、可解码性与时长，再持久保存并转换为 QQ 语音，然后调用该片段的 ACK。已确认释放的片段可以不再提供下载；重复 ACK 必须幂等。

增量片段使用线性 PCM 编码，同一任务的采样率、通道数和位深保持一致。插件会另行转换发送格式，完整缓存中的原始翻唱文件保留服务返回的采样率、通道数和位深。

单段 ACK 成功响应为 `{"api_version":1,"acknowledged":true,"sequence":1,"released":true,"pending":false}`；若仍有下载占用，可返回 HTTP 202、`acknowledged:true`、`released:false` 与 `pending:true`，并在读者结束后释放。清理失败应返回错误，不能声明已确认或已释放。单段 ACK 只释放该片段，不能删除仍用于后续推理的输入。所有片段下载完成且任务终态成功后，插件再通过原有任务 ACK 释放任务目录。

服务端应限制尚未确认的片段数量，达到上限时等待客户端消费，避免发送间隔较长时无限生成文件。还应对等待确认且无客户端活动的活跃任务设置有限超时；客户端消失后，停止推理子进程并清理临时数据。合法的状态查询、下载与 ACK 应更新客户端活动时间。插件在等待发送间隔或队列腾出空间时继续低频查询，服务不能把较长的播放间隔、正常模型加载或推理误判成客户端失联。

插件只将全部片段生成并校验成功的结果标记为完整缓存。部分已发送后若推理或发送失败，反馈与记录会保留实际发送数量；已经确认发送的片段不会在同一任务中自动重发。

### 下载与确认

任务完成后，`GET /v1/jobs/{job_id}/audio` 返回非空、可解码的 WAV 文件（建议 `Content-Type: audio/wav`）。插件在本地校验并保存结果后会调用 ACK；随后插件将保留自己的歌曲与翻唱缓存，服务端应删除该任务的输入、处理中间文件和输出。

`POST /v1/jobs/{job_id}/ack` 成功返回 `{"api_version":1,"released":true,"pending":false}`。若仍有并发下载，服务可返回 HTTP 202 与 `pending:true`，并在最后一个下载结束后清理。任务尚未完成时应以 HTTP 409 拒绝 ACK。对已释放或不存在的任务，ACK 应幂等返回已释放状态。

任务失败、用户取消或插件不再需要结果时，会发送 `DELETE /v1/jobs/{job_id}`。服务应停止对应任务并清理任务数据，返回 `released:true`；`cancelled` 可说明是否取消了正在运行的任务。删除不存在的任务应幂等返回已释放状态。

### 临时数据与 TTL

翻唱任务数据必须是临时数据。服务需在 ACK 或 DELETE 后清理输入音频、中间产物和输出文件；对已完成但未收到 ACK 的任务，必须设置有限 TTL 并自动清理。TTL 应给正常结果下载留出时间，且不得在推理仍运行或客户端下载仍进行时提前删除数据。若 ACK 时仍有下载占用，应返回 `pending:true`，并在最后一次下载完成后清理。删除失败时不得返回 `released:true`。服务重启时还应清理上次进程遗留的任务临时数据，避免任务文件无限累积。已导入音色及其所需模型可以按服务设计持久保存，不属于单次任务临时文件。

## 连通与 8 秒检测

1. 从 AstrBot 实际运行的主机或容器网络访问服务，分别确认 `GET /v1/health` 和 `GET /v1/voices` 可用。健康结果应为 API v1、管线 v2，并包含三个必需能力；音色响应应包含可选音色。插件页面的“检查资源与音色”会请求这两个端点，检测总时限为 10 秒。
2. 在插件管理页选择一首已下载歌曲和一个音色，运行“真实短片段测试”。插件会从歌曲中裁出最多 8 秒音频，提交任务、轮询状态、下载 WAV、验证 QQ 语音格式并确认服务端清理。
3. 该测试不会向 QQ 发送消息，也不会新增歌曲记录。若检查失败，先从 AstrBot 所在网络空间确认 URL、端口、鉴权和服务健康状态，再核对 `api_version`、`pipeline_version`、任务能力和 TTL/ACK/DELETE 行为。
