# ── Stage 1: Build the SvelteKit frontend ──────────────────
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend-builder

WORKDIR /build/frontend
COPY cptr/frontend/package.json cptr/frontend/package-lock.json ./
RUN npm ci
COPY cptr/frontend/ ./
RUN npm run build


# ── Stage 2: Install Python dependencies & build wheel ─────
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS backend-builder

WORKDIR /build
COPY pyproject.toml uv.lock LICENSE README.md CHANGELOG.md ./
COPY cptr/ cptr/

# Drop the pre-built frontend into the package tree
COPY --from=frontend-builder /build/frontend/build cptr/frontend/build

# Build the wheel (includes frontend build as an artifact via hatch)
RUN uv build --wheel --out-dir /dist


# ── Stage 3: Minimal runtime image ─────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/open-webui/computer"
LABEL org.opencontainers.image.description="cptr - your computer, from anywhere"
LABEL org.opencontainers.image.licenses="BSL-1.1"

# Runtime deps: git for git operations, tini for PID 1
RUN apt-get update && \
    apt-get install -y --no-install-recommends git tini && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash cptr
USER cptr
WORKDIR /home/cptr

# Install the wheel into an isolated venv
COPY --chown=cptr:cptr --from=backend-builder /dist/*.whl /tmp/
RUN uv venv /home/cptr/.venv && \
    uv pip install --python /home/cptr/.venv/bin/python /tmp/*.whl && \
    rm /tmp/*.whl

ENV PATH="/home/cptr/.venv/bin:$PATH"
ENV CPTR_DATA_DIR="/data"

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["tini", "--"]
CMD ["cptr", "run", "--host", "0.0.0.0", "--port", "8000", "--headless"]
