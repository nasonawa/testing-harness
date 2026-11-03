#!/usr/bin/env bash
# Pre-download the registry CLIs used by the Playwright @container / signature
# specs into ./bin.
#
# The runner image only COPYs these files (Containerfile.playwright), so image
# builds never wait on — or fail because of — GitHub release availability. The
# finished binaries are also sanity-checked here, so a truncated download is
# caught before a build starts.
#
# Usage:
#   ./fetch-tools.sh            # download anything missing
#   ./fetch-tools.sh --force    # re-download everything (after a version bump)
#
# Versions match .github/workflows/ci-web.yaml and web/Containerfile.playwright.
# Override by exporting CRANE_VERSION / REGCTL_VERSION / ORAS_VERSION /
# COSIGN_VERSION, then run with --force.
set -euo pipefail
cd "$(dirname "$0")"

CRANE_VERSION="${CRANE_VERSION:-v0.20.6}"
REGCTL_VERSION="${REGCTL_VERSION:-v0.7.1}"
# Note: CI passes "1.2.2" to the setup-oras action, but the GitHub release tag
# and asset name use different forms: tag "v1.2.2", asset "oras_1.2.2_...".
ORAS_VERSION="${ORAS_VERSION:-v1.2.2}"
COSIGN_VERSION="${COSIGN_VERSION:-v3.0.2}"

case "$(uname -s)" in
  Linux) ;;
  *) echo "ERROR: these are Linux binaries; run this on Linux." >&2; exit 2 ;;
esac

# Release asset names differ per project; map the host arch to each.
case "$(uname -m)" in
  x86_64 | amd64) GOARCH=amd64; CRANE_ARCH=x86_64 ;;
  aarch64 | arm64) GOARCH=arm64; CRANE_ARCH=arm64 ;;
  *) echo "ERROR: unsupported host architecture '$(uname -m)'." >&2; exit 2 ;;
esac

BIN_DIR="$PWD/bin"
mkdir -p "$BIN_DIR"

force=0
if [ "${1:-}" = "--force" ]; then
  force=1
fi

# A stalled-but-open connection never ends on its own; --speed-time/--speed-limit
# turns it into a failed attempt so --retry starts a fresh one.
curl_opts=(
  -fL --progress-bar
  --retry 5 --retry-all-errors --retry-delay 2
  --connect-timeout 20 --max-time 900 --speed-time 30 --speed-limit 1024
)

need() {
  [ "$force" = 1 ] || [ ! -x "$BIN_DIR/$1" ]
}

download() { # download <url> <dest>
  echo "[fetch] $2"
  curl "${curl_opts[@]}" -o "$2.tmp" "$1"
  mv "$2.tmp" "$2"
}

if need crane; then
  tmp="$BIN_DIR/.crane.tar.gz"
  download "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_${CRANE_ARCH}.tar.gz" "$tmp"
  tar -xzf "$tmp" -C "$BIN_DIR" crane
  rm -f "$tmp"
  chmod +x "$BIN_DIR/crane"
fi

if need regctl; then
  download "https://github.com/regclient/regclient/releases/download/${REGCTL_VERSION}/regctl-linux-${GOARCH}" "$BIN_DIR/regctl"
  chmod +x "$BIN_DIR/regctl"
fi

if need oras; then
  tmp="$BIN_DIR/.oras.tar.gz"
  download "https://github.com/oras-project/oras/releases/download/${ORAS_VERSION}/oras_${ORAS_VERSION#v}_linux_${GOARCH}.tar.gz" "$tmp"
  tar -xzf "$tmp" -C "$BIN_DIR" oras
  rm -f "$tmp"
  chmod +x "$BIN_DIR/oras"
fi

if need cosign; then
  download "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-${GOARCH}" "$BIN_DIR/cosign"
  chmod +x "$BIN_DIR/cosign"
fi

# Fail here rather than halfway through an image build.
echo "[fetch] verifying binaries"
"$BIN_DIR/crane" version
"$BIN_DIR/regctl" version | sed -n '1p'
"$BIN_DIR/oras" version | sed -n '1p'
"$BIN_DIR/cosign" version | sed -n '1p'
echo "[fetch] tools ready in $BIN_DIR"
