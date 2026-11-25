#!/bin/sh
# Preload the Python packages from their original Launchpad file store to
# avoid serial downloads and intermittent 503s from the APT archive endpoint.
# The SHA-256 for each chosen package comes from the signed APT index.
set -eu

fetch_package() {
    package=$1
    version=$(apt-cache policy "$package" | awk '$1 == "Candidate:" {print $2}')
    info=$(apt-cache show "$package=$version")
    filename=$(printf '%s\n' "$info" | awk '$1 == "Filename:" {print $2; exit}')
    digest=$(printf '%s\n' "$info" | awk '$1 == "SHA256:" {print $2; exit}')
    basename=$(basename "$filename")
    archive="/var/cache/apt/archives/$basename"
    if [ -z "$filename" ] || [ -z "$digest" ]; then return 1; fi
    if curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 8 --max-time 25 --retry 1 \
        "https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa/+files/$basename" \
        --output "$archive.partial"; then
        if printf '%s  %s\n' "$digest" "$archive.partial" | sha256sum --check --status; then
            mv "$archive.partial" "$archive"
            return
        fi
    fi
    # APT retries from its configured archive if this mirror is unavailable.
    printf 'APT will fetch %s from the package archive.\n' "$package" >&2
}

for package in python3.12 python3.12-minimal python3.12-venv \
               libpython3.12 libpython3.12-minimal libpython3.12-stdlib; do
    fetch_package "$package" &
done
wait
