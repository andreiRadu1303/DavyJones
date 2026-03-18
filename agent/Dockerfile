FROM node:20-slim

RUN apt-get update && apt-get install -y curl git python3 && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI + Google Workspace CLI
RUN npm install -g @anthropic-ai/claude-code @googleworkspace/cli

WORKDIR /app

# Create non-root user (required for --dangerously-skip-permissions)
ARG HOST_UID=501
ARG HOST_GID=20
RUN groupadd -g ${HOST_GID} -o agent 2>/dev/null || true
RUN useradd -m -s /bin/bash -u ${HOST_UID} -g ${HOST_GID} agent
RUN chown -R agent:agent /app

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER agent

ENTRYPOINT ["/app/entrypoint.sh"]
