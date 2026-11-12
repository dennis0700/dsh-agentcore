# dsh-agentcore：在 AgentCore Runtime 上运行 DeepSeek Harness

把 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）部署成 [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/) 上的一个 agent 服务。调用方通过 `InvokeAgentRuntime` 发任务，同一个会话里可以多轮对话、拿到流式进度，microVM 被回收后会话也能恢复。主要面向亚马逊云科技中国区（北京、宁夏），全球区域也能用。

> 本项目是社区项目，不是 DeepSeek 或 AWS 的官方产品。DeepSeek Harness 还是开发者预览版，版本之间会有不兼容的改动，所以 `requirements.txt` 锁定了确切版本。

## 在 DeepSeek Harness 基础上做了什么

本项目没有修改或复制 dsh 的代码。dsh 通过 pip 包 `deepseek-harness-sdk` 引入，本项目只用它公开的 Python SDK 和 profile patch 机制。dsh 负责 agent 本身：模型调用、工具、会话日志、上下文压缩、子 agent。本项目补上的是 dsh 自己没有的、在 AgentCore 上运行需要的部分。

| dsh 本身 | 本项目补上的 |
|---|---|
| 以 CLI、Web UI、桌面应用或 stdio JSON-RPC SDK 的方式运行，没有 HTTP 服务 | 用 `bedrock-agentcore` SDK 提供 AgentCore 要求的 `/ping` 和 `/invocations`，并做请求校验、payload 大小限制 |
| SDK 是同步阻塞的，`run()` 结束才返回结果 | 在线程里跑 `run()`，把 dsh 的通知转成 SSE 事件流式返回。只转发状态、工具名、助手文本，工具参数和输出不转发；长任务每 15 秒发一次心跳 |
| 会话 ID 由调用方自己管理 | AgentCore 的 `runtimeSessionId` 直接作为 dsh 的会话 ID。一台 microVM 只有一个常驻 dsh 进程；同一会话的并发请求返回 `SESSION_BUSY` |
| 会话日志写在 `DSH_HOME`，第一次写入时用硬链接发布文件 | sessionStorage 不支持硬链接，所以 DSH_HOME 放在容器本地磁盘，每次调用结束后复制到 sessionStorage。新 microVM 第一次处理请求时先复制回来，再调 `resume_session()`。agent 的工作目录直接放在 sessionStorage 上 |
| 模型配置在 YAML profile 里，或者通过 Web UI 设置 | 用环境变量配置任意 OpenAI Chat Completions 兼容的 endpoint，启动时生成 `llm-pi-ai` 的 patch。默认兼容设置适配 vLLM、SGLang、LiteLLM 这类网关，API Key 不写进文件 |
| 默认交互式运行：需要人来回答提问、审批工具调用 | 自动回答 `ask_user_question`（"没有用户可以交互，按你的判断继续"）；关掉运行时安装插件和定时提醒，因为 microVM 回收后它们没法用 |
| 默认会把会话日志上传给 DeepSeek，并上报遥测 | 两者默认都关闭。没有 DeepSeek Key 时，`web_search` 也会关闭 |
| 没有部署方案 | arm64 Dockerfile（可配国内镜像源）、CloudFormation 模板（ECR、最小权限的执行角色、Runtime）、构建推送和部署脚本，支持 `aws-cn` 分区 |
| — | 测试：本地端到端测试（用 Docker 模拟 microVM 和 sessionStorage，自带模拟模型，不需要任何凭据）、部署后用 AgentCore CLI 验证的脚本 |

代码量不大：`app/` 一共约 650 行 Python。设计上的取舍和推导过程见 [DESIGN.md](DESIGN.md)，测试说明见 [TESTING.md](TESTING.md)。

## 目录结构

```
app/
  main.py        AgentCore 入口：/invocations、流式 SSE、请求校验
  harness.py     DeepSeekHarness 单例、会话新建/恢复、并发控制
  state.py       本地 DSH_HOME 与 sessionStorage 之间的状态同步
  events.py      dsh 通知 → 客户端事件（过滤工具输出）
  config.py      环境变量
  llm.py         生成 OpenAI 兼容 endpoint 的 dsh patch
config/runtime.patch.yml   叠加在 sdk profile 上的 dsh patch
infra/base.yaml            CloudFormation：ECR 仓库 + 执行角色
infra/runtime.yaml         CloudFormation：AgentCore Runtime
scripts/                   部署、调用、部署后验证、清理脚本
tests/local_e2e.sh         本地端到端测试（见 TESTING.md）
tests/mock_openai.py       测试用的模拟 OpenAI 兼容服务
tests/e2e.env.example      真实模型测试的配置模板
Dockerfile                 linux/arm64 镜像
deploy.env.example         部署参数模板
```

## 前提条件

在中国区（北京）的一台 **arm64** EC2 上部署，比如 Graviton 实例。x86_64 也可以，但要先装 QEMU 做跨架构构建，见 `scripts/build_push.sh` 里的注释。

- Docker（带 buildx）
- AWS CLI v2，凭据指向北京区账号
- Python 3.10+ 和 boto3（只有 `scripts/invoke.py` 需要）
- 一个 OpenAI Chat Completions 兼容的模型 endpoint，要支持流式工具调用

部署用的 IAM 身份需要这些权限：CloudFormation、ECR 推送、`iam:CreateRole`/`PutRolePolicy`/`PassRole`（限定在 `AmazonBedrockAgentCoreRuntime-*`）、`bedrock-agentcore:*AgentRuntime*`。

## 部署

```bash
cp deploy.env.example deploy.env
vi deploy.env                 # 至少填 LLM_BASE_URL、LLM_MODEL、LLM_API_KEY

scripts/deploy_base.sh        # 1. ECR 仓库 + 执行角色
scripts/build_push.sh         # 2. 构建 arm64 镜像并推送，标签默认是 UTC 时间戳
scripts/deploy_runtime.sh     # 3. 创建或更新 Runtime，默认用上一步推送的镜像
```

## 调用

```bash
export AWS_REGION=cn-north-1
S=my-session-$(uuidgen | tr -d -)        # 会话 ID 要求 33 个字符以上

scripts/invoke.py "在当前目录写一个 hello.py 并运行" --session "$S"
scripts/invoke.py "把它改成打印当前时间"       --session "$S"   # 同一个会话，多轮对话
scripts/invoke.py --stop                       --session "$S"   # 主动停止，回收 microVM
scripts/invoke.py "hello.py 现在是什么内容？"  --session "$S"   # 在新 microVM 上恢复

scripts/invoke.py --smoke    # 自动验证：写文件 → 停止会话 → 恢复 → 检查文件和对话记忆
```

部署后的完整验证（包括 microVM 内部检查和日志检查）见 [TESTING.md](TESTING.md#2-部署后验证)。

请求 payload：

```json
{"prompt": "任务描述", "stream": true}
```

`stream` 默认是 `true`，返回 SSE 事件流：

| type | 说明 |
|---|---|
| `status` | agent 状态，`running` 或 `idle` |
| `text` | 助手完整消息（每一步一条） |
| `tool` | 工具调用开始/结束，只有工具名和 call_id，不含参数和输出 |
| `subagent` | 子 agent 开始/结束 |
| `turn_end` | 一轮结束，`reason` 为 `completed`/`error` 等，出错时带 `error` |
| `heartbeat` | 15 秒内没有其他事件时发一条，防止连接空闲断开 |
| `final` | 最后一条：`final_response`、`finish_reason`，出错时带 `error` |
| `error` | 请求被拒绝或运行失败，带 `code`：`BAD_REQUEST`、`SESSION_BUSY`、`START_FAILED`、`RUN_FAILED` |

`stream=false` 时只返回 `final` 或 `error` 这一条 JSON。

同一个会话里，上一次调用还没结束就发起新调用，会直接返回 `SESSION_BUSY`。

## 模型配置

默认用一个 OpenAI Chat Completions 兼容的 endpoint（`LLM_PROVIDER=openai-compatible`）。容器启动时，`app/llm.py` 会根据环境变量生成一个 dsh patch，通过 dsh 自带的 `llm-pi-ai` 适配器把请求发到 `$LLM_BASE_URL/chat/completions`，并带上 `Authorization: Bearer $LLM_API_KEY`。API Key 不会写进 patch 文件。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | 无 | 必填，要带版本段，例如 `https://llm.example.com/v1` |
| `LLM_MODEL` | 无 | 必填，请求里 `model` 字段的值 |
| `LLM_API_KEY` | 空 | Bearer token，不需要鉴权的网关留空 |
| `LLM_CONTEXT_WINDOW` | `128000` | 模型真实的上下文长度。自动压缩按这个值触发，设大了会在上游报"超出上下文" |
| `LLM_MAX_OUTPUT_TOKENS` | 空 | 每次请求的输出上限 |
| `LLM_REASONING_EFFORTS` | 空 | 空表示不推理的模型。支持 `reasoning_effort` 的模型可设 `{"off":null,"high":"high"}` |
| `LLM_COMPAT` | 空 | 兼容开关，JSON，合并到默认值上，见下文 |
| `LLM_HEADERS` | 空 | 额外请求头，JSON |
| `LLM_SUPPORTS_IMAGES` | `false` | 模型是否接受图片输入 |

### 兼容开关

dsh 用的 pi-ai 库遇到不认识的 baseURL 时，会把它当成 OpenAI 官方 API，发送 `store`、`developer` 角色和 `max_completion_tokens`。大部分兼容网关（vLLM、SGLang、LiteLLM、one-api 等）不接受这些，所以默认改成：

```json
{"supportsStore": false, "supportsDeveloperRole": false, "maxTokensField": "max_tokens"}
```

上游报错时，再通过 `LLM_COMPAT` 调整。常用的几个：

| 现象 | 设置 |
|---|---|
| 不支持 `stream_options` | `{"supportsUsageInStreaming": false}` |
| 流式响应里没有 `finish_reason` | `{"supportsFinishReason": false}` |
| 工具结果必须带 `name` | `{"requiresToolResultName": true}` |
| 工具结果后面不能直接跟 user 消息 | `{"requiresAssistantAfterToolResult": true}` |
| Qwen 系推理模型 | `{"thinkingFormat": "qwen"}` |
| 推理内容走 `reasoning_content` 的 DeepSeek 风格模型 | `{"thinkingFormat": "deepseek", "requiresReasoningContentOnAssistantMessages": true}` |

完整列表见 dsh 文档 `docs/config-catalog.md` 里 `@deepseek-ai/dsh-llm-pi-ai` 的 `PiAiCompatProfile`。

### 模型要求

- **必须支持流式工具调用**（`tools` 加 `stream: true`）。dsh 每次请求会带二十多个工具，不支持函数调用的模型无法工作。
- **工具调用参数要可靠**。参数不符合 schema 时，dsh 会把错误返回给模型重试，小模型容易在这一步反复出错。

### web_search

dsh 的 `web_search` 工具调用的是 DeepSeek 的搜索 API。没有设 `DEEPSEEK_API_KEY` 时，这个工具会被关掉，`web_fetch` 不受影响。

### 改用 DeepSeek 官方 API

设 `LLM_PROVIDER=deepseek`，再填 `DEEPSEEK_API_KEY` 和 `DSH_MODEL`。

## 其他配置项

在 `deploy.env` 里修改，再执行 `scripts/deploy_runtime.sh` 生效。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DSH_PROFILE` | `sdk` | `sdk` 是完整工具集，`sdk-minimal` 只有 bash |
| `DSH_PERMISSION_MODE` | `danger-full-access` | 见"安全"一节 |
| `DSH_UPLOAD_SESSION_LOG` | `false` | 是否把会话日志随请求上传给 DeepSeek，只在用 DeepSeek 官方 API 时有效 |
| `IDLE_RUNTIME_SESSION_TIMEOUT` | `1800` | microVM 空闲多少秒后回收，最大 28800 |
| `MAX_LIFETIME` | `28800` | microVM 最长存活秒数，最大 28800 |

## 会话持久化的工作方式

dsh 的会话日志在第一次写入时用硬链接发布文件，而 sessionStorage 不支持硬链接。所以：

- dsh 的 home 放在容器本地 `/home/app/.dsh-home`。
- 每次调用结束后，home 被复制到 `/mnt/workspace/.dsh-state/`。
- agent 的工作目录直接用 `/mnt/workspace/project`。
- 新 microVM 第一次处理请求时，如果本地没有会话，就先从 `.dsh-state/` 复制回来，再调用 `resume_session()`。

能保留的：工作目录里的文件、对话历史。不能保留的：shell 进程、后台任务、环境变量等运行时状态。

调用进行中 microVM 被强制回收，这一轮的进度会丢，下一次从上一次调用结束时的状态继续。

## 已知限制

- **更新 Runtime 版本会清空所有会话**：每次 `deploy_runtime.sh` 改了镜像或配置，都会产生新版本，sessionStorage 全部重置。
- **sessionStorage 是预览版**：每个会话最多 1 GB，14 天不调用会被清空。
- **dsh 是预览版**：`requirements.txt` 锁定了版本。升级前先在本地跑一遍"本地测试"。
- **单次调用时长**：microVM 最长 8 小时。

## 安全

- **权限模式**：默认 `danger-full-access`，agent 的 shell 在容器里不受 dsh 沙箱限制。隔离靠的是 AgentCore 每个会话一台 microVM。AgentCore 的 microVM 里能不能跑 bubblewrap 沙箱我没有验证过，如果改成 `workspace-write`，请先实测 bash 工具能否正常执行。
- **执行角色**：只有拉取镜像、写日志和指标的权限。microVM 里的任何代码都能通过元数据服务拿到这个角色的凭据，所以不要给它加其他权限。
- **API Key 在哪里**：`LLM_API_KEY`（以及可选的 `DEEPSEEK_API_KEY`）保存在 Runtime 的环境变量里，有 `bedrock-agentcore:GetAgentRuntime` 权限的人能看到明文。CloudFormation 参数设了 `NoEcho`，栈的控制台和 API 不显示它。部署时 Key 通过权限 600 的临时文件传给 CloudFormation，不出现在命令行上。
- **agent 能读到 API Key**：dsh 会从 agent 执行的命令的环境变量里去掉名字含 `KEY`/`TOKEN`/`SECRET` 的变量，所以 `env` 看不到。但 agent 的 shell 和服务进程是同一个用户，`cat /proc/1/environ` 能读到明文，我在本地实测过。所以要假设 **agent 和任何能让 agent 执行命令的人都拿得到这个 Key**。缓解办法：
  - 给 agent 专门发一个 Key，只开它要用的模型，设用量上限，定期轮换。
  - 不要把有其他权限的 Key（比如能管理账号或调别的服务的 Key）给它用。
  - 不让不可信的用户直接调用 Runtime。
- **数据发到哪里**：用户的 prompt、对话历史、工具输出（包括工作目录里被读取的文件内容）都会发给 `LLM_BASE_URL`。默认配置下不会发给其他地方：
  - 会话日志上传给 DeepSeek（`session-log-deepseek`）默认关闭，只有设 `DSH_UPLOAD_SESSION_LOG=true` 才开。
  - dsh 的遥测在镜像里用 `DSH_TELEMETRY_DISABLED=1` 关掉了。
  - 没设 `DEEPSEEK_API_KEY` 时 `web_search` 关闭，不会调 DeepSeek 搜索 API。
  - 模型调用 `web_fetch` 或 shell 里的 `curl` 时，会访问它自己选的网址。PUBLIC 网络模式下这不受限制，要限制出网需要改成 VPC 模式加出口控制。
- **用 https**：`LLM_BASE_URL` 用 `http://` 时 Key 和对话内容都是明文传输，启动时日志会打一条警告。
- **日志**：服务日志只记录会话 ID、模型名和文件数量，不记录 prompt、回复和 Key。上游模型返回的错误原文会放进调用方收到的 `error.message`，但不写日志。服务自身出异常时（比如 dsh 进程退出），`log.exception` 会把异常和 dsh 最后几行 stderr 写进 CloudWatch。
- **换 Key 会清空会话**：改任何环境变量都会生成新的 Runtime 版本，所有会话的 sessionStorage 被重置。
- **模型 endpoint 的网络**：Runtime 用 PUBLIC 网络模式，endpoint 必须能从公网访问。如果 endpoint 在你的 VPC 里，要把 Runtime 改成 VPC 模式，这部分模板里还没有。
- **调用方**：AgentCore 不检查会话属于哪个用户。如果给多个用户用，要在你的后端维护用户和会话 ID 的对应关系，否则知道会话 ID 的人能读到别人的对话和文件。

## 测试

完整说明见 [TESTING.md](TESTING.md)。常用命令：

```bash
tests/local_e2e.sh            # 本地端到端，模拟模型，不需要任何凭据
tests/local_e2e.sh --real     # 本地端到端，真实模型（先填 tests/e2e.env）
scripts/verify_cli.sh         # 部署后，用 AgentCore CLI 验证
```

## 清理

```bash
scripts/destroy.sh          # 删除 Runtime 和所有会话存储
scripts/destroy.sh --all    # 同时删除 ECR 仓库和执行角色（仓库必须先清空）
```

## 许可证

[MIT](LICENSE)。依赖各自的许可证：DeepSeek Harness 是 MIT，`bedrock-agentcore` SDK 是 Apache-2.0。DeepSeek 和 Amazon Bedrock AgentCore 的名称和商标归各自所有者。
