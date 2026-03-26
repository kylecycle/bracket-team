FROM mcr.microsoft.com/devcontainers/python:3.12

# Install Node.js and uv via system packages (avoids needing to reach github.com during build)
RUN apt-get update \
  && apt-get install -y --no-install-recommends nodejs npm \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

RUN pip install uv

# Install Claude Code globally (requires Node.js, runs as root so no permission issues)
RUN npm install -g @anthropic-ai/claude-code

# Shell aliases for the vscode user
RUN echo "alias ll='ls -la'" >> /home/vscode/.bashrc
