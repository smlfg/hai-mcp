from __future__ import annotations

import os
from dataclasses import dataclass

_ALLOWED_TRANSPORTS = frozenset({"stdio", "streamable-http"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class RuntimeConfig:
    transport: str
    host: str | None = None
    port: int | None = None


def parse_runtime(env: dict[str, str] | None = None) -> RuntimeConfig:
    """Parse HAI_MCP_TRANSPORT / HTTP host+port. Raises SystemExit on invalid config."""
    e = env if env is not None else os.environ
    transport = e.get("HAI_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in _ALLOWED_TRANSPORTS:
        raise SystemExit(
            f"HAI_MCP_TRANSPORT must be one of: {', '.join(sorted(_ALLOWED_TRANSPORTS))}; got {transport!r}"
        )
    if transport == "stdio":
        return RuntimeConfig(transport=transport)

    host = e.get("HAI_MCP_HTTP_HOST", "127.0.0.1").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise SystemExit(
            f"HAI_MCP_HTTP_HOST must be a loopback address ({', '.join(sorted(_LOOPBACK_HOSTS))}); "
            f"got {host!r} (fail-closed: no auth on HTTP)"
        )

    port_raw = e.get("HAI_MCP_HTTP_PORT", "8765").strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(f"HAI_MCP_HTTP_PORT must be an integer 1..65535; got {port_raw!r}") from exc
    if port < 1 or port > 65535:
        raise SystemExit(f"HAI_MCP_HTTP_PORT must be in range 1..65535; got {port}")

    return RuntimeConfig(transport=transport, host=host, port=port)
