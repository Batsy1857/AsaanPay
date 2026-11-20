#!/usr/bin/env bash
set -euo pipefail
# Official immutable v0.69.3 release. Checksums were verified against its release assets.
version=0.69.3
destination="${1:?Usage: install_trivy.sh DESTINATION}"
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) platform=Linux-64bit; checksum=1816b632dfe529869c740c0913e36bd1629cb7688bd5634f4a858c1d57c88b75 ;;
  Darwin/arm64) platform=macOS-ARM64; checksum=a2f2179afd4f8bb265ca3c7aefb56a666bc4a9a411663bc0f22c3549fbc643a5 ;;
  *) echo 'Unsupported scanner platform' >&2; exit 1 ;;
esac
mkdir -p "$destination"
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT
curl --fail --silent --show-error --location --retry 3 --connect-timeout 10 --max-time 180 \
  "https://github.com/aquasecurity/trivy/releases/download/v${version}/trivy_${version}_${platform}.tar.gz" \
  --output "$archive"
printf '%s  %s\n' "$checksum" "$archive" | shasum -a 256 --check --strict
tar -xzf "$archive" -C "$destination" trivy
"$destination/trivy" --version
