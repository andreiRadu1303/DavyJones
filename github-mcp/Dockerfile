# Stage 1: Get the official GitHub MCP server binary
FROM ghcr.io/github/github-mcp-server AS github-mcp

# Stage 2: Node image with supergateway to bridge stdio→SSE
FROM node:20-slim

# Install supergateway (stdio-to-SSE bridge)
RUN npm install -g supergateway

# Copy the GitHub MCP server binary from the official image
COPY --from=github-mcp /server/github-mcp-server /usr/local/bin/github-mcp-server
RUN chmod +x /usr/local/bin/github-mcp-server

EXPOSE 3003

# Wrap the stdio MCP server with supergateway to expose SSE on port 3003
CMD ["supergateway", \
     "--stdio", "github-mcp-server stdio", \
     "--port", "3003", \
     "--outputTransport", "sse"]
