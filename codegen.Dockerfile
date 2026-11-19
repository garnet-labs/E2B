FROM golang:1.26.10 AS go-tools

ENV GOPROXY=https://proxy.golang.org

# Install Golang deps
RUN go install github.com/bufbuild/buf/cmd/buf@v1.50.1 && \
    go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.28.1 && \
    go install connectrpc.com/connect/cmd/protoc-gen-connect-go@v1.18.1


# Pinned to match .tool-versions.
FROM node:22.23.4-bookworm-slim AS node-tools

FROM python:3.11

# Set working directory
WORKDIR /workspace

# Copy installed Go deps from previous build step
COPY --from=go-tools /go /go

# Add Go binary to PATH
ENV PATH="/go/bin:${PATH}"

# Install Python deps (e2b-openapi-python-client is patched version to fix issue with explode)
# https://github.com/openapi-generators/openapi-python-client/pull/1296
# protoc-gen-py generates the Python envd protobuf-py messages and
# protoc-gen-connectrpc the Connect stubs.
RUN pip install black==26.10.1 e2b-openapi-python-client==0.26.2 datamodel-code-generator==0.87.0 protoc-gen-connectrpc==0.12.1 protoc-gen-py==0.6.0

COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# Install Node.js deps
# pnpm is pinned to match .tool-versions and the root `packageManager` field —
# pnpm manages its own version from `packageManager` since v10, so a mismatch
# would make it re-download itself on every `make generate`.
ENV PNPM_VERSION=10.34.5
ENV REDOCLY_TELEMETRY=off
RUN npm install -g \
    pnpm@${PNPM_VERSION} \
    @connectrpc/protoc-gen-connect-es@1.6.1 \
    @bufbuild/protoc-gen-es@2.6.2 \
    @redocly/cli@2.76.0

CMD ["make", "generate"]
