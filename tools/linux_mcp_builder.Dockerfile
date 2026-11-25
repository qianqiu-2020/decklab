ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}
RUN export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -qq \
    && apt-get install -y --no-install-recommends binutils ca-certificates curl gnupg \
    && ppa_key=F23C5A6CF475977595C89F51BA6932366A755776 \
    && curl --fail --silent --show-error --retry 2 "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${ppa_key}" --output /tmp/deadsnakes.asc \
    && test "$(gpg --batch --show-keys --with-colons /tmp/deadsnakes.asc | awk -F: '$1=="fpr" {print $10; exit}')" = "$ppa_key" \
    && gpg --batch --dearmor --output /usr/share/keyrings/deadsnakes.gpg /tmp/deadsnakes.asc \
    && rm /tmp/deadsnakes.asc \
    && echo 'deb [signed-by=/usr/share/keyrings/deadsnakes.gpg] https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu jammy main' > /etc/apt/sources.list.d/deadsnakes.list \
    && apt-get update -qq
RUN export DEBIAN_FRONTEND=noninteractive \
    && apt-get -o Acquire::Retries=2 install -y --no-install-recommends python3.12 python3.12-venv libpython3.12 \
    && rm -rf /var/lib/apt/lists/*
COPY linux-mcp-requirements.lock /tmp/linux-mcp-requirements.lock
RUN python3.12 -m venv /opt/mcp-build \
    && /opt/mcp-build/bin/python -m pip download --disable-pip-version-check --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r /tmp/linux-mcp-requirements.lock -d /opt/wheelhouse \
    && /opt/mcp-build/bin/python -m pip install --disable-pip-version-check --no-index --find-links /opt/wheelhouse --only-binary=:all: --require-hashes -r /tmp/linux-mcp-requirements.lock
