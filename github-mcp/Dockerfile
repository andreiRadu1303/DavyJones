# Stage 1: pull the official GitHub MCP server binary (stdio mode only)
FROM ghcr.io/github/github-mcp-server AS github-mcp

# Stage 2: Python base for mcp-proxy (spec-compliant stdio→SSE bridge).
# We previously used `supergateway`, which initialised the connection
# correctly but Claude Code consistently marked the server as `failed`
# and the github tools never appeared in the agent's tool list. The
# Python `mcp-proxy` package handles the SSE/JSON-RPC framing the
# Claude Code MCP client expects.
FROM python:3.12-slim

# CA certs so the underlying github-mcp-server can verify api.github.com.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# mcp-proxy provides `mcp-proxy --sse-port N -- <stdio cmd>` which spawns
# the stdio server and exposes /sse + /messages on the given port.
RUN pip install --no-cache-dir mcp-proxy==0.11.0

# Copy the GitHub MCP stdio binary from the official image
COPY --from=github-mcp /server/github-mcp-server /usr/local/bin/github-mcp-server
RUN chmod +x /usr/local/bin/github-mcp-server

EXPOSE 3003

# `--allow-origin "*"` keeps in-cluster service-to-service calls happy.
# `--pass-environment` forwards GITHUB_PERSONAL_ACCESS_TOKEN to the child.
CMD ["mcp-proxy", \
     "--host", "0.0.0.0", \
     "--port", "3003", \
     "--allow-origin", "*", \
     "--pass-environment", \
     "--", \
     "github-mcp-server", "stdio"]
