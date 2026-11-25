ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}
RUN export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -qq \
    && apt-get install -y --no-install-recommends software-properties-common gpg-agent binutils ca-certificates \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get install -y --no-install-recommends python3.12 python3.12-venv \
    && rm -rf /var/lib/apt/lists/*
COPY linux-mcp-requirements.lock /tmp/linux-mcp-requirements.lock
RUN python3.12 -m venv /opt/mcp-build \
    && /opt/mcp-build/bin/python -m pip download --disable-pip-version-check --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r /tmp/linux-mcp-requirements.lock -d /opt/wheelhouse \
    && /opt/mcp-build/bin/python -m pip install --disable-pip-version-check --no-index --find-links /opt/wheelhouse --only-binary=:all: --require-hashes -r /tmp/linux-mcp-requirements.lock
