# hermes-claude-plan

[English](./README.md) | [中文](./README_CN.md)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) 插件，将 Anthropic API 请求路由到 Claude 订阅通道，优化速率限制分配。

**无侵入式设计** -- 完全安装在 `~/.hermes/plugins/` 目录下，不修改 hermes 任何源码文件。与 `hermes update`、`git pull` 升级完全兼容，安装卸载无副作用。

## 前置条件

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.9.0+
- [Claude Code](https://claude.ai/code) 已安装并登录（`claude` CLI 可用）
- 有效的 Claude Max / Team / Enterprise 订阅
- Python >= 3.11

## 安装

```bash
hermes plugins install Jawf/hermes-claude-plan
```

或手动克隆：

```bash
git clone https://github.com/Jawf/hermes-claude-plan.git ~/.hermes/plugins/anthropic_plan
```

## 配置

在 `~/.hermes/config.yaml` 中设置 `anthropic_plan` 为默认 provider：

```yaml
model:
  default: claude-opus-4-6      # 或 claude-sonnet-4-6 等
  provider: anthropic_plan
```

或在运行时切换：

```bash
# 在 hermes 聊天会话中
/model anthropic_plan:claude-opus-4-6
```

## 工作原理

```
hermes chat
    |
    v
[Anthropic Messages API 请求]
    |
    v
[localhost:28765 本地代理]
    |   - 从 ~/.claude/.credentials.json 读取 OAuth token
    |   - 重写请求头（billing、user-agent、betas）
    |   - 重写请求体（system prompt 前缀、tool name 前缀）
    v
[api.anthropic.com/v1/messages]
    |
    v
[SSE 响应流式返回]
```

插件在 `127.0.0.1:28765` 启动一个轻量级 HTTP 代理（仅本地回环）。代理执行以下操作：

1. 每次请求时从 `~/.claude/.credentials.json` 读取当前 OAuth token（自动获取 token 刷新）
2. 添加 Claude Code 身份头（`x-anthropic-billing-header`、`user-agent`、OAuth betas）
3. 在 system prompt 前插入 Claude Code 身份标识块
4. 为工具名添加 `mcp_` 前缀（匹配 Claude Code 规范）
5. 转发到 `api.anthropic.com` 并流式传回 SSE 响应

## 验证

```bash
# 快速检查
hermes chat -q "hi" -Q
# 应收到 Claude 回复

# 详细追踪（检查计费池路由）
HERMES_ANTHROPIC_PLAN_TRACE=/tmp/trace.log hermes chat -q "hi" -Q
grep "representative-claim" /tmp/trace.log
# 预期输出：anthropic-ratelimit-unified-representative-claim: five_hour
```

## 卸载

1. 删除插件：

```bash
rm -rf ~/.hermes/plugins/anthropic_plan
```

2. 在 `~/.hermes/config.yaml` 中切回原 provider：

```yaml
model:
  provider: anthropic    # 或 openrouter 等
```

3. 清理 `custom_providers` 条目（插件自动添加的）：

在 `~/.hermes/config.yaml` 的 `custom_providers:` 列表中删除 `anthropic_plan` 块。

## 环境变量

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `HERMES_ANTHROPIC_PLAN_PORT` | `28765` | 代理监听端口 |
| `HERMES_ANTHROPIC_PLAN_VERBOSE` | 未设置 | 启用代理访问日志 |
| `HERMES_ANTHROPIC_PLAN_TRACE` | 未设置 | 将请求/响应追踪写入指定文件路径 |

## 故障排除

**401 认证错误**
- 运行 `claude /login` 刷新 OAuth token，然后重试

**端口 28765 被占用**
- 代理会自动递增尝试最多 10 个端口（28765-28774）
- 通过 `HERMES_ANTHROPIC_PLAN_PORT=29000` 覆盖

**插件未加载**
- 检查 `hermes plugins list` — 应显示 `anthropic_plan` 为 `enabled`
- 确认 `~/.hermes/plugins/anthropic_plan/plugin.yaml` 存在

## 兼容性

- **Windows / Linux / macOS** — 纯 Python 标准库，无原生依赖
- **Hermes v0.9.0+** — 使用 `custom_providers` 配置格式和插件 API
- Token 刷新自动完成 — 代理每次请求都重新读取 `~/.claude/.credentials.json`

## 许可证

MIT
