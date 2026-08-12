# FreebuffProxy (Python)

Freebuff 免费模型的 OpenAI 兼容 API 代理（Python 参考实现）。

## 特性

- **OpenAI 兼容**：`POST /v1/chat/completions`，可直接对接任意 OpenAI SDK/客户端
- **免费模式**：cost: 0，无需付费额度
- **模型支持**：
  - `deepseek/deepseek-v4-flash` → base2-free-deepseek-flash
  - `mimo/mimo-v2.5` → base2-free-mimo
- **完整协议复刻**（对齐官方 CLI）：
  - `Bun/1.3.14` session UA + `ai-sdk/openai-compatible/0.0.0-test/codebuff` chat UA
  - 每次请求 DELETE 旧 session → sleep(1s) → 用目标模型重建（规避 409 model_locked / model_mismatch）
  - session 轮询直到 active
  - START/FINISH agent run 生命周期管理
  - `You are Buffy` system marker（free mode 必检）
  - `ACTING_USER_ID` 标识
- **代理支持**：HTTPS_PROXY 走住宅 IP 节点解锁 FULL 模式

## 快速开始

```bash
# 1. 配置环境变量（或用默认值）
export FREEBUFF_AUTH_TOKEN="你的freebuff token"
export FREEBUFF_ACTING_USER_ID="你的acting user id"
export HTTPS_PROXY="http://127.0.0.1:8078"   # 住宅IP代理节点

# 2. 启动服务（OpenAI 兼容）
python3 freebuff_api.py --listen :18082 --proxy http://127.0.0.1:8078

# 3. 调用
curl -X POST http://127.0.0.1:18082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
```

### 直接调用模式

```bash
python3 freebuff_api.py --proxy http://127.0.0.1:8078 \
  --prompt "请只回复「连接正常」四个字"
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FREEBUFF_AUTH_TOKEN` | - | freebuff 认证 token |
| `FREEBUFF_ACTING_USER_ID` | - | acting user id |
| `HTTPS_PROXY` | `http://127.0.0.1:8078` | 代理（住宅 IP 解锁 FULL） |

## 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--listen` | `:18082` | 监听地址 |
| `--proxy` | `http://127.0.0.1:8078` | 代理地址 |
| `--prompt` | - | 直接调用模式 |
| `--model` | `deepseek/deepseek-v4-flash` | 模型名称 |

## 内存占用

实测 Linux 上空闲约 23MB RSS（Go 版本约 12MB），适合低配服务器。

## 与 Go 版本的关系

本仓库是 **Python 参考实现**（协议验证用），生产推荐使用 Go 版本 [FreebuffProxy](https://github.com/csy87704403/FreebuffProxy)（约 12MB 内存 + 原生并发）。
