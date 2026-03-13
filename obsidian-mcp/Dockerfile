FROM node:20-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Install supergateway (stdio-to-SSE bridge)
RUN npm install -g supergateway

# Clone and build mcp-obsidian-tools (disk-based vault reader)
RUN git clone https://github.com/dp-veritas/mcp-obsidian-tools.git /opt/obsidian-mcp
WORKDIR /opt/obsidian-mcp
RUN npm install && npm run build

EXPOSE 3010

# Wrap the stdio MCP server with supergateway to expose SSE on port 3010
CMD ["supergateway", \
     "--stdio", "node /opt/obsidian-mcp/dist/index.js /vault", \
     "--port", "3010", \
     "--outputTransport", "streamableHttp"]
