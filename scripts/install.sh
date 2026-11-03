#!/bin/sh
# Install the api-qovrix standalone binary (no Python needed).
#
#   curl -fsSL https://github.com/jsxd3v/api-qovrix/releases/latest/download/install.sh | sh
#
# Options (environment variables):
#   API_QOVRIX_VERSION   version to install, e.g. 0.1.0 (default: latest)
#   API_QOVRIX_INSTALL   install folder (default: ~/.local/bin)
set -eu

repo="jsxd3v/api-qovrix"
version="${API_QOVRIX_VERSION:-latest}"
dest="${API_QOVRIX_INSTALL:-$HOME/.local/bin}"

os=$(uname -s)
arch=$(uname -m)
case "$os" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "Unsupported OS: $os (on Windows, download the .exe from the releases page)" >&2; exit 1 ;;
esac
case "$arch" in
  x86_64|amd64) arch=x64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) echo "Unsupported CPU: $arch" >&2; exit 1 ;;
esac

asset="api-qovrix-$os-$arch"
if [ "$version" = "latest" ]; then
  base="https://github.com/$repo/releases/latest/download"
else
  base="https://github.com/$repo/releases/download/v${version#v}"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "Downloading $asset ($version)..."
curl -fsSL "$base/$asset" -o "$tmp/$asset"
curl -fsSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS"

expected=$(grep " $asset\$" "$tmp/SHA256SUMS" | cut -d' ' -f1)
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$tmp/$asset" | cut -d' ' -f1)
else
  actual=$(shasum -a 256 "$tmp/$asset" | cut -d' ' -f1)
fi
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  echo "Checksum mismatch for $asset; not installing." >&2
  exit 1
fi

mkdir -p "$dest"
install -m 0755 "$tmp/$asset" "$dest/api-qovrix"
echo "Installed api-qovrix to $dest/api-qovrix"
case ":$PATH:" in
  *":$dest:"*) ;;
  *) echo "Add $dest to your PATH to run 'api-qovrix'." ;;
esac
"$dest/api-qovrix" --version
