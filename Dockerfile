FROM node:20-bookworm-slim

ENV TZ=UTC \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip \
      sudo cron tini ca-certificates tzdata curl \
 && rm -rf /var/lib/apt/lists/*

# --break-system-packages: Debian 12 marks the system python externally-managed
# (PEP 668); this image is single-purpose so we accept it.
RUN pip3 install --break-system-packages --no-cache-dir jsonschema

# Pin to match the version the chassis MCP server is built against
# (harness/claude-mcp/package.json). Bump both together.
RUN npm install -g @anthropic-ai/claude-code@2.1.143

# UID 2000 to avoid clashing with the host's first user (usually 1000).
RUN useradd -m -u 2000 -s /bin/bash agent

# /mnt/protected is mode 700 root: the bind-mounted ./tools dir lives inside,
# but the agent cannot traverse the parent so the implementations stay hidden.
RUN install -d -m 700 -o root -g root /mnt/protected

# --- MCP server build (cached unless package.json or src/ change) -------------
WORKDIR /build/claude-mcp
COPY harness/claude-mcp/package*.json ./
RUN npm ci
COPY harness/claude-mcp/tsconfig.json ./
COPY harness/claude-mcp/src ./src
RUN npx tsc \
 && cp -r dist /usr/local/lib/chassis-mcp \
 && cp -r node_modules /usr/local/lib/chassis-mcp/node_modules \
 && printf '%s\n' '{"type":"module"}' > /usr/local/lib/chassis-mcp/package.json \
 && chmod -R a+rX /usr/local/lib/chassis-mcp

# Root-owned scripts on PATH.
COPY harness/run-tool       /usr/local/bin/run-tool
COPY harness/run-claude     /usr/local/bin/run-claude
COPY harness/entrypoint.sh  /usr/local/bin/chassis-entrypoint
RUN chmod 755 /usr/local/bin/run-tool /usr/local/bin/run-claude /usr/local/bin/chassis-entrypoint

# Sudoers: agent can call the dispatcher with no password, nothing else.
RUN echo 'agent ALL=(root) NOPASSWD: /usr/local/bin/run-tool' > /etc/sudoers.d/chassis \
 && chmod 440 /etc/sudoers.d/chassis \
 && visudo -c -f /etc/sudoers.d/chassis

# Seed for a virgin chassis-agent-home volume. The entrypoint copies this into
# /home/agent/ on first boot (tracked by a marker file in the home dir).
COPY harness/agent-template/ /usr/local/share/chassis/agent-template/
RUN chmod -R a+rX /usr/local/share/chassis/agent-template

RUN mkdir -p /var/log/chassis

ENTRYPOINT ["/usr/local/bin/chassis-entrypoint"]
