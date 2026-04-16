# hermes-claude-plan

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that routes Anthropic API requests through the Claude subscription channel for improved rate-limit allocation.

## Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.9.0+
- [Claude Code](https://claude.ai/code) installed and logged in (`claude` CLI available)
- An active Claude Max / Team / Enterprise subscription
- Python >= 3.11

## Install

```bash
hermes plugins install <your-username>/hermes-claude-plan
```

Or clone manually:

```bash
git clone https://github.com/<your-username>/hermes-claude-plan.git ~/.hermes/plugins/anthropic_plan
```

## Configure

Set `anthropic_plan` as your default provider in `~/.hermes/config.yaml`:

```yaml
model:
  default: claude-opus-4-6      # or claude-sonnet-4-6, etc.
  provider: anthropic_plan
```

Or switch at runtime:

```bash
# Inside a hermes chat session
/model anthropic_plan:claude-opus-4-6
```

## How It Works

```
hermes chat
    |
    v
[Anthropic Messages API request]
    |
    v
[localhost:28765 proxy]
    |   - Reads OAuth token from ~/.claude/.credentials.json
    |   - Rewrites headers (billing, user-agent, betas)
    |   - Rewrites body (system prompt prefix, tool name prefix)
    v
[api.anthropic.com/v1/messages]
    |
    v
[SSE response streamed back]
```

The plugin starts a lightweight HTTP proxy on `127.0.0.1:28765` (loopback only). The proxy:

1. Reads the current OAuth token from `~/.claude/.credentials.json` on every request (picks up token refreshes automatically)
2. Adds Claude Code identity headers (`x-anthropic-billing-header`, `user-agent`, OAuth betas)
3. Prepends the Claude Code identity block to the system prompt
4. Prefixes tool names with `mcp_` (matching Claude Code conventions)
5. Forwards to `api.anthropic.com` and streams the SSE response back

## Verify

```bash
# Quick check
hermes chat -q "hi" -Q
# Should receive a Claude response

# Detailed trace (check billing pool routing)
HERMES_ANTHROPIC_PLAN_TRACE=/tmp/trace.log hermes chat -q "hi" -Q
grep "representative-claim" /tmp/trace.log
# Expected: anthropic-ratelimit-unified-representative-claim: five_hour
```

## Uninstall

1. Remove the plugin:

```bash
rm -rf ~/.hermes/plugins/anthropic_plan
```

2. Switch provider back in `~/.hermes/config.yaml`:

```yaml
model:
  provider: anthropic    # or openrouter, etc.
```

3. Clean up the `custom_providers` entry (the plugin added it automatically):

Remove the `anthropic_plan` block from the `custom_providers:` list in `~/.hermes/config.yaml`.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `HERMES_ANTHROPIC_PLAN_PORT` | `28765` | Proxy listen port |
| `HERMES_ANTHROPIC_PLAN_VERBOSE` | unset | Enable proxy access logs |
| `HERMES_ANTHROPIC_PLAN_TRACE` | unset | Write request/response trace to file path |

## Troubleshooting

**401 Authentication Error**
- Run `claude /login` to refresh your OAuth token, then retry

**Port 28765 in use**
- The proxy auto-increments up to 10 ports (28765-28774)
- Override with `HERMES_ANTHROPIC_PLAN_PORT=29000`

**Plugin not loaded**
- Check `hermes plugins list` -- should show `anthropic_plan` as `enabled`
- Ensure `~/.hermes/plugins/anthropic_plan/plugin.yaml` exists

## Compatibility

- **Windows / Linux / macOS** -- pure Python stdlib, no native dependencies
- **Hermes v0.9.0+** -- uses the `custom_providers` config format and plugin API
- Token refresh is automatic -- the proxy re-reads `~/.claude/.credentials.json` on every request

## License

MIT
