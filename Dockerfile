FROM python:3.12-slim

# Node 20 for the Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
# Copy the source too, not just pyproject: setuptools needs the declared packages
# (substrate, agent, ...) present to build, and this is what installs the deps
# (asyncpg, mcp, opentelemetry-*). At runtime ./ is bind-mounted over /app, so the
# host source stays authoritative; this layer exists for the dependencies.
COPY . .
RUN pip install --no-cache-dir .

# Source is bind-mounted at runtime (see docker-compose.yml); command is set there.
