# 设计说明

本文记录设计取舍。使用方法见 [README.md](README.md)，测试见 [TESTING.md](TESTING.md)。

## 1. 目标

把 DeepSeek Harness（dsh）作为一个 agent 服务部署在亚马逊云科技中国区的 Amazon Bedrock AgentCore Runtime 上。调用方通过 `InvokeAgentRuntime` 发起任务，在同一个会话里可以多轮对话，并能看到流式进度。

## 2. 范围

用到的服务：

| 服务 | 用途 |
|---|---|
| AgentCore Runtime（开启 `sessionStorage`） | 运行 dsh，按会话隔离，保留会话文件 |
| ECR | 存放 ARM64 镜像 |
| IAM | Runtime 执行角色、调用方权限。模板用 `${AWS::Partition}`，中国区自动是 `arn:aws-cn` |
| CloudWatch Logs | Runtime 自带的日志 |

不使用：Secrets Manager、AgentCore Memory、Gateway、Identity、EFS、S3 Files、Lambda。

模型：一个 OpenAI Chat Completions 兼容的 endpoint，通过 dsh 的 `llm-pi-ai` 适配器接入（见 README"模型配置"）。也可以切回 DeepSeek 官方 API。

区域：主要面向 `cn-north-1`（北京）和 `cn-northwest-1`（宁夏）。中国区 AgentCore 不提供 Memory、Policy、EC2 实例容量和 S3 Files，本设计都没有用到。

## 3. 架构

```
调用方 (boto3 / CLI)
   │  InvokeAgentRuntime(runtimeSessionId, payload)
   ▼
AgentCore Runtime ── 每个 runtimeSessionId 一台独立 microVM
   │
   │  容器 (ARM64, 0.0.0.0:8080)
   │  ┌──────────────────────────────────────────────┐
   │  │ app.py                                       │
   │  │  BedrockAgentCoreApp  (/ping, /invocations)  │
   │  │        │                                     │
   │  │        ▼                                     │
   │  │  DeepSeekHarness (Python SDK)                │
   │  │        │ stdio JSON-RPC                      │
   │  │        ▼                                     │
   │  │  dsh --profile sdk  (子进程, 常驻)            │
   │  └──────────────────────────────────────────────┘
   │        │                          │
   │        ▼                          ▼
   │  /mnt/workspace (sessionStorage)  OpenAI 兼容 endpoint
   │   ├── dsh-home/
   │   └── project/
```

组件：

- **`bedrock-agentcore` SDK**：`BedrockAgentCoreApp` 提供 `/ping` 和 `/invocations`，入口函数是 async generator，返回值作为流式响应。
- **`deepseek-harness-sdk`**：Python SDK，wheel 自带 Linux arm64 的 dsh 原生运行时，不需要系统 Node.js。SDK 以子进程方式启动 `dsh --profile sdk`，通过 stdio JSON-RPC 驱动。
- **dsh 进程**：每台 microVM 一个，第一次请求时启动，被多次请求复用，microVM 回收时随之结束。

## 4. 会话模型

| AgentCore | dsh | 说明 |
|---|---|---|
| `runtimeSessionId` | `session_id` | 直接复用同一个 ID |
| 一台 microVM | 一个 `DeepSeekHarness` 实例 | 进程内单例 |
| `sessionStorage` 挂载点 | `cwd` 和 dsh 状态镜像 | 见下 |

目录约定：

```
/home/app/.dsh-home/      # DSH_HOME，容器本地磁盘，dsh 实际读写
/mnt/workspace/
├── project/              # agent 工作目录 (cwd)
└── .dsh-state/           # DSH_HOME 的镜像副本（不含 profiles/、cache/、凭据）
```

DSH_HOME 不能直接放在挂载点：dsh 的 JSONL 会话存储第一次写入时用 `link()` 发布文件，而 sessionStorage 不支持硬链接。所以 DSH_HOME 放在本地磁盘，每次 `run()` 结束后把它复制到 `.dsh-state/`；新 microVM 第一次处理请求时，如果本地没有会话，就先从 `.dsh-state/` 复制回来，再 `resume_session()`。

### 请求处理流程

1. 进入 `@app.entrypoint`，读取 `context.session_id` 和 `payload.prompt`。
2. 如果进程内还没有 harness，就懒加载：
   - 创建 `/mnt/workspace/project` 和 `/mnt/workspace/.dsh-state`。
   - 本地 DSH_HOME 没有会话时，从 `.dsh-state/` 恢复。
   - 创建 `DeepSeekHarness(dsh_home=..., cwd=..., profile="sdk", ...)`。
   - 有已保存的会话就 `resume_session(session_id)`，失败或没有就 `start_session(session_id)` 新建。
3. 在工作线程里调用 `session.run(prompt, on_notification=cb)`。`cb` 通过 `loop.call_soon_threadsafe` 把事件放进 asyncio 队列。
4. 入口函数从队列里取事件，转成 SSE 推出去，直到收到最终结果或错误。
5. 最后一条消息是 `{final_response, finish_reason, session_id}`。

harness 必须懒加载：挂载点只在处理请求时可用，容器初始化阶段不可用。

### 并发

同一个会话同时只允许一个 `run()`，用进程内锁实现。后来的请求直接返回 `SESSION_BUSY`，不排队：排队会让调用方的连接一直挂着，而且 AgentCore 本身也会把同一会话的请求发到同一台 microVM。

## 5. 接口约定

请求 payload：

```json
{
  "prompt": "修复 project 里失败的测试",
  "stream": true
}
```

只有 `prompt` 必填，`stream` 默认 `true`。模型由部署配置决定，调用方不能指定，避免被用来调用部署方没打算开放的模型。

流式响应（SSE）的事件类型：

```
data: {"type": "status", "status": "running|idle"}
data: {"type": "tool", "state": "start|end", "name": "bash", "call_id": "...", "is_error": false}
data: {"type": "text", "text": "..."}             # 每一步的完整助手消息
data: {"type": "subagent", "state": "start|end", "child": "..."}
data: {"type": "turn_end", "reason": "completed|error", "error": {...}}
data: {"type": "heartbeat"}
data: {"type": "final", "final_response": "...", "finish_reason": "completed"}
data: {"type": "error", "code": "BAD_REQUEST|SESSION_BUSY|START_FAILED|RUN_FAILED", "message": "..."}
```

dsh 的通知要过滤后再转发：只推状态、工具名和起止、助手文本。工具的完整输出不推，避免响应过大和泄露数据。

`stream=false` 时只返回最后一条 `final` 或 `error`。`final_response` 只包含最后一条助手消息，前面步骤的文字要从流式的 `text` 事件里取。

## 6. dsh 配置

- **profile**：`sdk`，完整工具集。如果只想要 bash，可以改成 `sdk-minimal`。
- **patch**（`config/runtime.patch.yml`，构建时放进镜像，启动时通过 `patches=` 传入）：
  - 关闭会话日志上传（`session-log-deepseek`），除非设 `DSH_UPLOAD_SESSION_LOG=true`。
  - 关闭 `plugin_manager`（运行时装插件需要硬链接和人工审批）和定时提醒（microVM 回收后无法触发）。
- **提问**：SDK 的 `on_user_question` 回调自动回复"没有用户可以交互，按你的判断继续"。
- **审批**：镜像里设 `DSH_PERMISSION_MODE=danger-full-access`，对应 dsh 的 `approval: never`。隔离靠 microVM，不靠 dsh 沙箱。
- **模型**：OpenAI 兼容 endpoint 的 patch 由 `app/llm.py` 在启动时生成，见 README"模型配置"。
- **遥测**：镜像里设 `DSH_TELEMETRY_DISABLED=1`。
- **插件**：构建镜像时装好。不要在 `/mnt/workspace` 里执行 `dsh plugin add`，因为 pnpm 用硬链接，而 sessionStorage 不支持硬链接。
- **版本**：`deepseek-harness-sdk` 和 `bedrock-agentcore` 都锁定确切版本。dsh 还是预览版，版本之间会有不兼容的改动。

## 7. 容器与构建

- 基础镜像：`python:3.12-slim-bookworm`（默认从 public.ecr.aws 拉取），`linux/arm64`。
- 用非 root 用户运行。
- 依赖：`bedrock-agentcore`、`deepseek-harness-sdk`，外加 `git` 和常用 CLI 工具，供 agent 的 shell 使用。
- pip 走国内镜像源，或者在海外机器上构建后推送到中国区 ECR。
- 构建命令：`docker buildx build --platform linux/arm64`。

## 8. 部署

以下由 `infra/base.yaml`、`infra/runtime.yaml` 两个 CloudFormation 模板完成，ARN 用 `${AWS::Partition}`，同一份模板在中国区和全球区都能用。

1. 创建 ECR 仓库：`<account>.dkr.ecr.<region>.amazonaws.com.cn/dsh-agent`，标签不可变，开启推送时扫描。
2. 创建 IAM 执行角色，信任方是 `bedrock-agentcore.amazonaws.com`，策略包括：
   - 拉取 ECR 镜像（`ecr:GetAuthorizationToken`、`BatchGetImage`、`GetDownloadUrlForLayer`）。
   - 写 CloudWatch Logs。
   - 信任策略带 `aws:SourceAccount` 和 `aws:SourceArn` 条件，防止混淆代理。
3. 创建运行时，`create-agent-runtime` 的主要参数：
   - `agentRuntimeArtifact.containerConfiguration.containerUri`
   - `networkConfiguration.networkMode = PUBLIC`，需要访问模型 endpoint
   - `filesystemConfigurations = [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]`
   - `lifecycleConfiguration`：`idleRuntimeSessionTimeout`（建议 1800 到 3600 秒），`maxLifetime`（28800 秒）
   - `environmentVariables`：`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` 等
4. 调用方需要的权限：`bedrock-agentcore:InvokeAgentRuntime`，主动停止会话还需要 `StopRuntimeSession`。

## 9. 安全

- **API Key**：放在运行时的环境变量里，用 IAM 限制谁能读运行时配置（`GetAgentRuntime`）。dsh 会把名字里含 `KEY`/`TOKEN`/`SECRET` 的变量从它执行的命令里去掉，但 agent 仍能通过 `/proc/1/environ` 读到，详见 README"安全"。
- **执行权限**：`sdk` profile 的 shell 在容器内权限很大。主要靠 microVM 按会话隔离来限制影响范围，容器内不要放任何其他凭据。执行角色只给最小权限，不给 S3、Bedrock 等不需要的权限。
- **输入**：`prompt` 只作为 SDK 参数传入，不拼接进任何 shell 命令。限制 payload 大小。
- **出网**：PUBLIC 模式下 agent 的 shell 可以访问任意公网。如果要限制，需要改成 VPC 模式，加 NAT 和出口白名单，这部分不在当前范围内。

## 10. 已知限制

| 项 | 影响 | 应对 |
|---|---|---|
| sessionStorage 是预览版 | 行为可能变化 | 上线前在中国区账号里验证 |
| 更新运行时版本会清空所有会话存储 | 换镜像、改环境变量（包括换 Key）后对话历史丢失 | 安排在维护窗口；需要的话做 S3 备份（第 11 节） |
| 14 天没有调用的会话会被重置 | 长期不用的会话丢失 | 当前假设 14 天足够；不够时做 S3 备份 |
| 每个会话最多 1 GB | 大仓库、大量依赖会超限 | 监控用量，必要时清理 |
| 不支持硬链接、FIFO、UNIX socket、xattr | pnpm 等工具在挂载点里可能失败 | 依赖装在镜像里，不装在挂载点 |
| 单个 microVM 最长 8 小时 | 长任务会被中断 | 回收后用同一个 session ID 恢复会话 |
| dsh 是预览版 | API 不兼容变更 | 锁定版本，升级前先回归测试 |

## 11. 可选扩展

- **会话历史备份到 S3**：每次 `run()` 结束后把 `dsh-home/sessions/<id>/` 上传到 S3。恢复时如果挂载点是空的，就先下载再 `resume_session()`。需要给执行角色加对应 bucket 的权限。
- **多个会话共享文件**：EFS 接入点，需要 VPC 模式加 NAT。中国区差异说明没有把它列为不可用，但没实测过。
- **长期记忆**：中国区没有 AgentCore Memory。可以按用户固定 `runtimeSessionId`，把记忆文件存在该会话的 sessionStorage 里，仍受 14 天和版本更新的限制。
- **Key 不进 agent 进程**：让模型请求经过一个持有 Key 的代理，agent 只拿到一个无权限的占位值。
- **鉴权**：调用方默认用 IAM SigV4。如果要给外部用户用，再考虑 AgentCore Identity 加 OIDC。

## 12. 已确认的决定

| 问题 | 决定 |
|---|---|
| profile | `sdk`（完整工具集）。可通过 `DSH_PROFILE=sdk-minimal` 换成只有 bash 的版本 |
| 会话日志上传给 DeepSeek | 默认关闭 |
| 同一会话并发请求 | 拒绝，返回 `SESSION_BUSY` |
| S3 备份会话历史 | 不做。假设 14 天的 sessionStorage 足够 |
| 模型来源 | OpenAI Chat Completions 兼容 endpoint，也可切回 DeepSeek 官方 API |

## 13. 部署后才能确认的

- 中国区 sessionStorage 的实际行为，包括是否确实不支持硬链接。
- PUBLIC 网络模式能否访问模型 endpoint。
- CloudFormation 在中国区是否支持 `AWS::BedrockAgentCore::Runtime` 的 `FilesystemConfigurations`。
- `InvokeAgentRuntimeCommand`（`agentcore runtime exec`）在中国区是否可用。

这些都由 `scripts/verify_cli.sh` 检查，见 TESTING.md。
