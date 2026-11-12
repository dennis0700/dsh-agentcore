# 测试与验证

本项目的测试分两层：

| 层 | 脚本 | 在哪里跑 | 需要什么 |
|---|---|---|---|
| 本地端到端 | `tests/local_e2e.sh` | 任何装了 Docker 的机器 | Docker、python3。默认用模拟模型，不需要 AWS 账号和模型 Key |
| 部署后验证 | `scripts/verify_cli.sh` | 能访问部署区域的机器 | 已部署的 Runtime、AWS CLI v2、AgentCore CLI、`deploy.env` |

建议的顺序：改完代码先跑本地模拟测试，再用真实模型跑一次本地测试，部署后跑 `verify_cli.sh`。

## 1. 本地端到端测试

### 运行

```bash
tests/local_e2e.sh                 # 模拟模型，不需要任何凭据
tests/local_e2e.sh --real          # 真实模型，读 tests/e2e.env
```

| 参数 | 说明 |
|---|---|
| `--real` | 用 `tests/e2e.env` 里的真实 endpoint，代替模拟模型 |
| `--no-build` | 不重新构建镜像，直接用已有的 |
| `--keep` | 结束后保留容器、卷和网络，方便排查。注意：`docker inspect` 保留下来的 agent 容器能看到 API Key |
| `--image NAME` | 镜像名，默认 `dsh-agent:e2e` |

| 环境变量 | 说明 |
|---|---|
| `DOCKER` | Docker 命令，默认 `docker`。当前用户不在 docker 组时设为 `"sudo docker"` |
| `E2E_ENV_FILE` | `--real` 用的配置文件，默认 `tests/e2e.env` |
| `PIP_INDEX_URL`、`APT_MIRROR`、`BASE_IMAGE` | 构建时的镜像源，和 `deploy.env` 里的含义一样 |

结果：每一项输出 `PASS`、`FAIL` 或 `SKIP`，最后一行是 `LOCAL E2E PASS` 或 `LOCAL E2E FAIL`，退出码对应 0 或 1，可以直接放进 CI。失败时会打印输出目录，里面有 agent 日志和每次调用的原始响应。

### 怎么模拟 AgentCore

| AgentCore | 本地替代 |
|---|---|
| microVM | 一个 Docker 容器，用的是要部署的同一个 arm64 镜像 |
| sessionStorage | 一个 Docker 卷，挂载到 `/mnt/workspace` |
| `InvokeAgentRuntime` | 同一网络里的探针容器用 curl 请求 `/invocations`，带上 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` 头 |
| microVM 回收后在新 microVM 上恢复 | 删掉 agent 容器，用同一个卷起一个新容器 |
| Runtime 环境变量 | `docker run --env-file`，文件权限 600，测试结束删除 |

脚本创建的容器、卷、网络名字都带一次运行一个的随机后缀，结束时全部删除。它不占用宿主机端口，也不读写宿主机上除了临时输出目录以外的任何路径。

### 测什么

| 步骤 | 检查项 | 模拟 | 真实 |
|---|---|---|---|
| 1. HTTP 约定 | `/ping` 返回 Healthy；空 prompt 返回 `BAD_REQUEST` | ✓ | ✓ |
| 2. 第一轮 | 流式事件以 `final`/`completed` 结束；有 `bash` 工具调用且成功；有 `status` 事件；文件写进 `/mnt/workspace/project`；dsh 状态复制到了 `/mnt/workspace/.dsh-state` | ✓ | ✓ |
| 3. 请求格式 | 发给 endpoint 的请求是流式、用 `max_tokens`、没有 `store` 和 `developer` 角色、带 Bearer 头；`LLM_HEADERS` 生效 | ✓ | 跳过 |
| 4. 并发 | 同一会话上一次还没结束时，新请求返回 `SESSION_BUSY`，第一个请求仍能完成 | ✓ | 跳过 |
| 5. 恢复 | 新容器启动时本地没有会话；调用后从卷恢复，日志里有 `restored` 和 `resumed`；模拟模式检查请求里带着之前的历史，真实模式检查模型记得暗号、能读出上一轮写的文件 | ✓ | ✓ |
| 6. 密钥 | 容器和卷里任何文件都不含 API Key；镜像里没有 `LLM_*` 环境变量 | ✓ | ✓ |

模拟模型（`tests/mock_openai.py`）按固定脚本回复：没有工具结果时让模型调 `bash` 写 `mock.txt`，有工具结果时返回文字。它测的是链路和协议，不是模型能力。

### 用真实模型测试

```bash
cp tests/e2e.env.example tests/e2e.env      # 已在 .gitignore 里
vi tests/e2e.env                            # 填 LLM_BASE_URL、LLM_MODEL、LLM_API_KEY
tests/local_e2e.sh --real
```

- 脚本只把 `e2e.env` 里白名单内的 `LLM_*` 等变量传给容器，不读当前 shell 的环境变量。
- 测试容器里的 `localhost` 是容器自己。endpoint 在本机时，写成 `http://host.docker.internal:<端口>/v1`，脚本已经给容器加了这个主机名。写成 `127.0.0.1` 或 `localhost` 时脚本会直接报错退出。
- 真实模式的判断依赖模型照着提示做，比如回答 `code=pelican`。模型不听话时这两项可能失败，失败时先看输出目录里的 `run2.json`、`run3.json`。

### 本地测不到的

- **sessionStorage 不支持硬链接**：Docker 卷支持硬链接，所以测不出直接把 DSH_HOME 放在挂载点会失败。这一点只能靠部署后的 `verify_cli.sh` 第 2 步确认。
- **挂载点只在处理调用时可用**：本地卷一直都挂着。
- **优雅关机**：本地用强制删除容器代替停止会话，更严格。代码在每次调用结束后就写入状态，不依赖关机时刷盘。
- **AgentCore 本身**：ECR 拉取、IAM 执行角色、PUBLIC 网络出站、CloudFormation、`InvokeAgentRuntime` 的流式行为。

## 2. 部署后验证

### 前提

- 已按 README 完成部署，`deploy.env` 和部署时用的是同一份。
- AWS CLI v2，凭据指向部署的账号和区域。
- AgentCore CLI：`npm install -g @aws/agentcore`（国内可加 `--registry=https://registry.npmmirror.com`）。
- python3。

### 运行

```bash
scripts/verify_cli.sh                 # 新建一个随机会话
scripts/verify_cli.sh <session-id>    # 指定会话 ID（至少 33 个字符）
```

| 步骤 | 命令 | 检查什么 |
|---|---|---|
| 1 | `agentcore runtime invoke` | 写入随机标记文件，让模型记住暗号，返回 `completed` |
| 2 | `agentcore runtime exec` | 在 microVM 里看文件内容、`.dsh-state`、用量，并测试是否支持硬链接（输出 `HARDLINK=...`）。接口不可用时显示 `SKIP` |
| 3 | `aws bedrock-agentcore stop-runtime-session` | 停止会话，等 30 秒 |
| 4 | 同一个会话 ID 调用两次 | 一次问暗号，检查对话历史；一次 `cat` 文件，检查工作目录 |
| 5 | `agentcore runtime logs` | 日志里有 `restored ... state files` 和 `resumed dsh session` |

最后输出 `VERIFY PASS` 或 `VERIFY FAIL`。失败时去打印出来的临时目录看 `1.json`、`2.exec`、`4.json`、`4b.json`、`5.logs`。

### 只用 boto3 的简化版

没装 AgentCore CLI 时，可以用 `scripts/invoke.py --smoke`。它做第 1、3、4 步，不做 microVM 内部检查和日志检查。

## 3. 静态检查

```bash
bash -n scripts/*.sh tests/*.sh
python3 -m pyflakes app scripts tests          # pip install pyflakes
cfn-lint --regions cn-north-1 -- infra/*.yaml   # pip install cfn-lint
```

推送到 GitHub 前扫描一遍密钥（不需要安装，用 Docker 跑）：

```bash
docker run --rm -v "$PWD":/repo:ro ghcr.io/gitleaks/gitleaks:latest dir /repo --no-banner
git status --ignored --short    # 确认 deploy.env、tests/e2e.env 显示为 !! (ignored)
```

## 4. 排查

| 现象 | 先看 |
|---|---|
| `cannot reach the Docker daemon` | 当前用户没有 Docker 权限，设 `DOCKER="sudo docker"` |
| 构建很慢或超时 | 设 `PIP_INDEX_URL`、`APT_MIRROR`、`BASE_IMAGE` 用国内源 |
| `agent did not become healthy` | 脚本会打印容器最后 30 行日志，通常是 `LLM_*` 配置错误 |
| 第 2 步 `finish_reason` 不是 `completed` | 看 `run1.sse` 里 `final` 事件的 `error` 字段，里面是上游返回的原文 |
| 上游报字段不认识 | 按 README"兼容开关"调 `LLM_COMPAT` |
| 真实模式恢复检查失败 | 先确认日志检查两项是否通过：通过说明恢复没问题，是模型没按提示回答 |
