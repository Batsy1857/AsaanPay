#!/usr/bin/env bash
set -euo pipefail
scanner="${1:?Usage: trivy_gate.sh TRIVY IMAGE_ID OUTPUT_DIRECTORY}"
image_id="${2:?Image ID required}"
output="${3:?Output directory required}"
mkdir -p "$output"
rm -f "$output/approved.json"
test "$("$scanner" --version | head -n 1)" = 'Version: 0.69.3'
# A fresh cache requires an available DB on every run; no silent stale-cache fallback.
cache="$(mktemp -d)"
trap 'rm -rf "$cache"' EXIT
printf '{}\n' > "$cache/config.yaml"
"$scanner" image --config "$cache/config.yaml" --cache-dir "$cache" --download-db-only \
  --db-repository ghcr.io/aquasecurity/trivy-db:2 --timeout 10m --no-progress
cp "$cache/db/metadata.json" "$output/db-metadata.json"
# One unfiltered inventory of the exact local image ID. Policy is evaluated below.
# Scan exit 0 means execution succeeded; it is never itself treated as policy approval.
"$scanner" image --config "$cache/config.yaml" --cache-dir "$cache" --skip-db-update \
  --image-src docker --scanners vuln --pkg-types os,library --list-all-pkgs \
  --ignorefile /dev/null --ignore-unfixed=false --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
  --format json --output "$output/trivy.json" --timeout 10m --no-progress "$image_id"
python3 scripts/trivy_policy.py "$output/trivy.json" "$image_id" \
  --summary "$output/summary.md" --receipt "$output/approved.json"
