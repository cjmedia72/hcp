# Changelog

## 0.1.0 (2026-04-16)

- Initial release
- Local HTTP proxy on 127.0.0.1:28765
- Auto-injects `custom_providers` entry into `~/.hermes/config.yaml`
- Claude Code identity: system prompt prefix, billing header, tool name prefixing
- System text sanitization for hermes tool name references
- SSE streaming pass-through
- Optional trace logging via `HERMES_ANTHROPIC_PLAN_TRACE`
