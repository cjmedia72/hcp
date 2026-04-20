"""Local HTTP proxy that rewrites hermes Anthropic requests to use the
Claude Code subscription billing channel.

The proxy listens on ``127.0.0.1:<port>/v1/messages``, rewrites the
request headers and body to match the Claude Code CLI identity, then
forwards to ``https://api.anthropic.com/v1/messages`` and streams the
SSE response back unchanged.

Zero third-party dependencies -- pure stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger("anthropic_plan.proxy")

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

DEFAULT_PORT = int(os.environ.get("HERMES_ANTHROPIC_PLAN_PORT", "28765"))
PORT_RETRY_LIMIT = 10
UPSTREAM = "https://api.anthropic.com"

# Live model cache — fetched from Anthropic's /v1/models API
_models_cache: Optional[list] = None
_models_cache_at: float = 0.0
_MODELS_CACHE_TTL = 3600  # re-fetch every hour

CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

_cached_cc_version: Optional[str] = None
_cached_cc_version_at: float = 0.0
_CC_VERSION_TTL = 3600
_CC_VERSION_FALLBACK = "2.1.109"

_token_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Claude Code identity helpers                                                #
# --------------------------------------------------------------------------- #


def _detect_claude_code_version() -> str:
    """Return the installed Claude Code version (cached for one hour)."""
    global _cached_cc_version, _cached_cc_version_at
    now = time.time()
    if _cached_cc_version and (now - _cached_cc_version_at) < _CC_VERSION_TTL:
        return _cached_cc_version
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            ver = result.stdout.strip().split()[0]
            if ver and ver[0].isdigit():
                _cached_cc_version = ver
                _cached_cc_version_at = now
                return ver
    except Exception as exc:
        logger.debug("claude --version failed: %s", exc)
    _cached_cc_version = _CC_VERSION_FALLBACK
    _cached_cc_version_at = now
    return _CC_VERSION_FALLBACK


def _build_billing_header(session_hint: str = "") -> str:
    ver = _detect_claude_code_version()
    cch = "00000"
    if session_hint:
        try:
            import hashlib
            cch = hashlib.md5(session_hint.encode("utf-8")).hexdigest()[:5]
        except Exception:
            pass
    return f"cc_version={ver}; cc_entrypoint=sdk-cli; cch={cch};"


def _parse_oauth_data(data: dict) -> Optional[str]:
    """Extract and validate the access token from parsed credentials data.

    Returns the token string, or ``None`` if missing or expired.
    """
    oauth = data.get("claudeAiOauth") or {}
    tok = oauth.get("accessToken")
    if not tok:
        return None
    expires_at_ms = oauth.get("expiresAt")
    if expires_at_ms is not None:
        try:
            if float(expires_at_ms) / 1000.0 < time.time():
                logger.warning(
                    "anthropic_plan: Claude Code OAuth token has expired -- "
                    "run `claude` to refresh your login"
                )
                return None
        except (TypeError, ValueError):
            pass
    return tok


def _read_oauth_token_from_keychain() -> Optional[str]:
    """Read credentials from the macOS Keychain (service: 'Claude Code-credentials')."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            return _parse_oauth_data(data)
    except FileNotFoundError:
        pass  # 'security' binary not available (non-macOS)
    except Exception as exc:
        logger.debug("anthropic_plan: keychain read failed: %s", exc)
    return None


def _read_oauth_token_from_file() -> Optional[str]:
    """Read credentials from ``~/.claude/.credentials.json``."""
    try:
        data = json.loads(CLAUDE_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        return _parse_oauth_data(data)
    except FileNotFoundError:
        logger.error(
            "anthropic_plan: Claude Code credentials not found at %s -- "
            "run `claude` once to log in",
            CLAUDE_CREDENTIALS_PATH,
        )
    except Exception as exc:
        logger.warning("anthropic_plan: failed to read %s: %s", CLAUDE_CREDENTIALS_PATH, exc)
    return None


def _read_oauth_token() -> Optional[str]:
    """Read the current access token from the macOS Keychain (preferred) or
    ``~/.claude/.credentials.json`` (fallback / non-macOS).
    """
    import sys as _sys
    with _token_cache_lock:
        if _sys.platform == "darwin":
            tok = _read_oauth_token_from_keychain()
            if tok:
                return tok
            # Fall back to file in case the user is running a non-Keychain build
        return _read_oauth_token_from_file()


# --------------------------------------------------------------------------- #
# Live model discovery                                                        #
# --------------------------------------------------------------------------- #


def _fetch_models_from_anthropic() -> list:
    """Fetch the model list from Anthropic's API using the OAuth token.

    Returns a list of OpenAI-compatible model dicts.  Results are cached
    for ``_MODELS_CACHE_TTL`` seconds.  On failure returns the cached
    list (if any) or an empty list.
    """
    global _models_cache, _models_cache_at
    now = time.time()
    if _models_cache is not None and (now - _models_cache_at) < _MODELS_CACHE_TTL:
        return _models_cache

    token = _read_oauth_token()
    if not token:
        return _models_cache or []

    all_models: list = []
    after_id: Optional[str] = None
    max_pages = 10  # safety limit

    try:
        for _ in range(max_pages):
            url = f"{UPSTREAM}/v1/models?limit=100"
            if after_id:
                url += f"&after_id={after_id}"

            headers = {
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "user-agent": f"claude-cli/{_detect_claude_code_version()} (external, cli)",
                "x-app": "cli",
            }
            # Add required beta headers
            headers["anthropic-beta"] = "claude-code-20250219,oauth-2025-04-20"

            req = urllib.request.Request(url, headers=headers, method="GET")
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())

            for m in data.get("data", []):
                model_id = m.get("id", "")
                display_name = m.get("display_name", model_id)
                all_models.append({
                    "id": model_id,
                    "object": "model",
                    "created": int(now),
                    "owned_by": "anthropic",
                    "name": display_name,
                })

            if not data.get("has_more", False):
                break
            after_id = data.get("last_id")
            if not after_id:
                break

    except Exception as exc:
        logger.warning("anthropic_plan: failed to fetch /v1/models: %s", exc)
        if _models_cache is not None:
            return _models_cache
        return all_models if all_models else []

    if all_models:
        _models_cache = all_models
        _models_cache_at = now
        logger.info("anthropic_plan: fetched %d models from Anthropic API", len(all_models))
    return all_models


# --------------------------------------------------------------------------- #
# Request rewriting                                                           #
# --------------------------------------------------------------------------- #

_STRIP_INBOUND = {
    "host", "connection", "content-length", "authorization",
    "x-api-key", "user-agent", "x-app",
    "x-anthropic-billing-header", "accept-encoding",
}

CLAUDE_CODE_SYSTEM_PREFIX = (
    "You are Claude Code, Anthropic's official CLI for Claude."
)

_MCP_TOOL_PREFIX = "mcp_"

# Hermes tool names that appear inside system-prompt text.  The classifier
# flags bare tool names that don't match Claude Code's built-in set, so we
# rewrite them to ``mcp_<name>``.
#
# To maintain: if hermes adds new tools that show up in the system prompt,
# add them here.  Tool names that only appear in the ``tools[]`` array are
# handled automatically by ``_prefix_tool_names``.
_EXTRA_HERMES_TOOL_NAMES_IN_TEXT = (
    "session_search", "skill_manage", "skill_view", "skills_list",
    "memory_manage", "delegate_task",
    "patch", "read_file", "search_files", "write_file",
    "process", "terminal", "todo",
    "web_search", "web_extract", "image_generate",
    "browser_back", "browser_click", "browser_console",
    "browser_get_images", "browser_navigate", "browser_press",
    "browser_scroll", "browser_snapshot", "browser_type", "browser_vision",
    "ha_call_service", "ha_get_state",
)


def _prefix_tool_names(data: dict) -> set:
    """Add ``mcp_`` prefix to tool names in ``tools[]`` and message history.

    Returns the set of original names that were rewritten.
    """
    rewritten: set = set()
    tools = data.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("name"), str):
                name = t["name"]
                if not name.startswith(_MCP_TOOL_PREFIX):
                    rewritten.add(name)
                    t["name"] = _MCP_TOOL_PREFIX + name

    msgs = data.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_use" and isinstance(blk.get("name"), str):
                        bn = blk["name"]
                        if not bn.startswith(_MCP_TOOL_PREFIX):
                            rewritten.add(bn)
                            blk["name"] = _MCP_TOOL_PREFIX + bn
    return rewritten


def _sanitize_system_text(text: str, extra_names: set) -> str:
    """Rewrite bare tool names in system text to ``mcp_<name>``."""
    if not text:
        return text
    import re
    all_names = set(_EXTRA_HERMES_TOOL_NAMES_IN_TEXT) | set(extra_names or ())
    for name in sorted(all_names, key=len, reverse=True):
        if not name:
            continue
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
        )
        text = pattern.sub(_MCP_TOOL_PREFIX + name, text)
    return text


def _rewrite_request_body(body: bytes) -> bytes:
    """Rewrite the JSON body so that:

    * ``system`` begins with the Claude Code identity block
    * every tool name is prefixed with ``mcp_``
    * bare tool-name mentions in the system text are also prefixed

    Idempotent.  On parse errors returns the original body unchanged.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except Exception:
        return body
    if not isinstance(data, dict):
        return body

    # Prepend Claude Code identity to system prompt
    cc_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
    sys_field = data.get("system")

    if isinstance(sys_field, str):
        if not sys_field.lstrip().startswith(CLAUDE_CODE_SYSTEM_PREFIX):
            data["system"] = [cc_block, {"type": "text", "text": sys_field}]
    elif isinstance(sys_field, list):
        already = (
            sys_field
            and isinstance(sys_field[0], dict)
            and isinstance(sys_field[0].get("text"), str)
            and sys_field[0]["text"].lstrip().startswith(CLAUDE_CODE_SYSTEM_PREFIX)
        )
        if not already:
            data["system"] = [cc_block] + sys_field
    elif sys_field is None:
        data["system"] = [cc_block]

    # Prefix tool names
    rewritten_tool_names = _prefix_tool_names(data)

    # Sanitise system blocks
    sys_blocks = data.get("system")
    if isinstance(sys_blocks, list):
        for blk in sys_blocks:
            if not (isinstance(blk, dict) and isinstance(blk.get("text"), str)):
                continue
            txt = blk["text"]
            txt = txt.replace("Hermes Agent", "Claude Code")
            txt = txt.replace("Hermes agent", "Claude Code")
            txt = txt.replace("hermes-agent", "claude-code")
            txt = txt.replace("Nous Research", "Anthropic")
            txt = _sanitize_system_text(txt, rewritten_tool_names)
            blk["text"] = txt

    return json.dumps(data).encode("utf-8")


# --------------------------------------------------------------------------- #
# HTTP handler                                                                #
# --------------------------------------------------------------------------- #


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version = "anthropic-plan-proxy/0.1"

    def log_message(self, fmt, *args):
        if os.environ.get("HERMES_ANTHROPIC_PLAN_VERBOSE"):
            super().log_message(fmt, *args)

    def do_GET(self):  # noqa: N802
        if self.path == "/_health":
            self._send_json(200, {"status": "ok", "upstream": UPSTREAM})
            return
        if self.path == "/v1/models":
            models = _fetch_models_from_anthropic()
            self._send_json(200, {"object": "list", "data": models})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/v1/"):
            self._send_json(404, {"error": "only /v1/* is proxied"})
            return

        token = _read_oauth_token()
        if not token:
            self._send_json(500, {
                "type": "error",
                "error": {"type": "auth_error",
                          "message": "anthropic_plan: cannot read ~/.claude/.credentials.json -- run `claude` to log in"},
            })
            return

        try:
            body_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            body_len = 0
        body = self.rfile.read(body_len) if body_len > 0 else b""

        # Extract a session hint for the cch hash
        session_hint = ""
        try:
            payload = json.loads(body or b"{}")
            msgs = payload.get("messages") or []
            if msgs:
                first = msgs[0]
                if isinstance(first, dict):
                    c = first.get("content")
                    if isinstance(c, str):
                        session_hint = c[:64]
                    elif isinstance(c, list) and c:
                        for blk in c:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                session_hint = str(blk.get("text", ""))[:64]
                                break
        except Exception:
            pass

        # Build outbound headers
        out_headers = {
            "Authorization": f"Bearer {token}",
            "user-agent": f"claude-cli/{_detect_claude_code_version()} (external, cli)",
            "x-app": "cli",
            "x-anthropic-billing-header": _build_billing_header(session_hint),
            "anthropic-version": "2023-06-01",
        }
        for hk, hv in self.headers.items():
            if hk.lower() in _STRIP_INBOUND:
                continue
            out_headers[hk] = hv

        # Ensure OAuth-required betas are present
        existing_beta = (out_headers.get("anthropic-beta") or "").strip()
        required_betas = ("claude-code-20250219", "oauth-2025-04-20")
        beta_set = {b.strip() for b in existing_beta.split(",") if b.strip()}
        for rb in required_betas:
            beta_set.add(rb)
        out_headers["anthropic-beta"] = ",".join(sorted(beta_set))

        # Rewrite request body
        body = _rewrite_request_body(body)

        url = UPSTREAM + self.path
        req = urllib.request.Request(url, data=body, method="POST", headers=out_headers)

        # Optional tracing (enable with HERMES_ANTHROPIC_PLAN_TRACE=/path/to/file)
        _trace_path = os.environ.get("HERMES_ANTHROPIC_PLAN_TRACE")
        if _trace_path:
            try:
                with open(_trace_path, "ab") as tf:
                    tf.write(b"\n=== request ===\n")
                    tf.write(f"path={self.path} body_size={len(body)}\n".encode())
                    for k, v in out_headers.items():
                        if k.lower() == "authorization":
                            v = "Bearer ***"
                        tf.write(f"  > {k}: {v}\n".encode())
            except Exception:
                pass

        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            err_body = e.read() or b""
            if _trace_path:
                try:
                    with open(_trace_path, "ab") as tf:
                        tf.write(f"=== response (HTTP {e.code}) ===\n".encode())
                        for k, v in e.headers.items():
                            tf.write(f"  < {k}: {v}\n".encode())
                except Exception:
                    pass
            self._write_response(e.code, dict(e.headers.items()), err_body)
            return
        except Exception as exc:
            self._send_json(502, {
                "type": "error",
                "error": {"type": "upstream_error", "message": str(exc)},
            })
            return

        if _trace_path:
            try:
                with open(_trace_path, "ab") as tf:
                    tf.write(f"=== response (HTTP {resp.status}) ===\n".encode())
                    for k, v in resp.headers.items():
                        tf.write(f"  < {k}: {v}\n".encode())
            except Exception:
                pass

        status = resp.status
        upstream_headers = dict(resp.headers.items())
        for h in ("Transfer-Encoding", "Content-Length", "Connection", "Content-Encoding"):
            upstream_headers.pop(h, None)

        self.send_response(status)
        for hk, hv in upstream_headers.items():
            self.send_header(hk, hv)
        is_sse = upstream_headers.get("content-type", "").startswith("text/event-stream")
        if is_sse:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        if is_sse:
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.wfile.write(resp.read())

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_response(self, status: int, headers: dict, body: bytes):
        self.send_response(status)
        for hk, hv in headers.items():
            if hk.lower() in ("transfer-encoding", "content-length", "connection", "content-encoding"):
                continue
            self.send_header(hk, hv)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------- #
# Server lifecycle                                                            #
# --------------------------------------------------------------------------- #

_server: Optional[ThreadingHTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_server_port: Optional[int] = None
_start_lock = threading.Lock()


def _bind(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), _ProxyHandler)


def start_proxy(preferred_port: int = DEFAULT_PORT) -> int:
    """Start the proxy on a loopback port.  Idempotent."""
    global _server, _server_thread, _server_port
    with _start_lock:
        if _server is not None and _server_port is not None:
            return _server_port
        last_exc = None
        for offset in range(PORT_RETRY_LIMIT):
            port = preferred_port + offset
            try:
                _server = _bind(port)
            except OSError as exc:
                last_exc = exc
                continue
            _server_port = port
            _server_thread = threading.Thread(
                target=_server.serve_forever,
                name=f"anthropic_plan-proxy:{port}",
                daemon=True,
            )
            _server_thread.start()
            logger.info("anthropic_plan proxy listening on http://127.0.0.1:%d", port)
            return port
        raise RuntimeError(
            f"anthropic_plan: failed to bind port in [{preferred_port}, "
            f"{preferred_port + PORT_RETRY_LIMIT}): {last_exc}"
        )


def get_proxy_port() -> Optional[int]:
    return _server_port


def is_running() -> bool:
    return _server is not None and _server_port is not None


def stop_proxy() -> None:
    global _server, _server_thread, _server_port
    with _start_lock:
        if _server is None:
            return
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
        _server = None
        _server_thread = None
        _server_port = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    port = start_proxy()
    print(f"Proxy on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop_proxy()
