"""Publish an approved same-run package with conditional S3 writes; never build images."""

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import release  # noqa: E402

ACCOUNT = "642195693459"
BUCKET = "asaanpay-releases"
REGION = "us-east-1"
FILES = (*release.PACKAGE_FILES, "SHA256SUMS")
# Conservatively cap at decimal 5 GB, within S3's single-PUT limit.
MAX_PUT_BYTES = 5_000_000_000


def expected_identity(environment):
    release.require(environment.get("GITHUB_EVENT_NAME") == "push"
                    and environment.get("GITHUB_REPOSITORY") == "Batsy1857/AsaanPay"
                    and environment.get("GITHUB_REF") == "refs/heads/feature/aws-cicd-devsecops",
                    "Publication requires a push to the approved repository and branch")
    return release.make_metadata((release.ROOT / "VERSION").read_text().strip(),
                                 environment["GITHUB_SHA"], environment["GITHUB_RUN_ID"],
                                 environment["GITHUB_RUN_ATTEMPT"])


def validate_package(directory, expected):
    manifest = release.check_checksums(directory)
    release.require(release.validate_manifest(manifest) == expected, "Package identity differs from this run")
    release.require(not manifest["local_validation"] and not manifest["source_dirty"],
                    "Only clean CI release packages can be published")
    for name in FILES:
        release.require((directory / name).stat().st_size <= MAX_PUT_BYTES,
                        f"File exceeds the single-PUT size limit: {name}")
    return manifest


def aws(*args):
    # Preserve stderr for precise AWS diagnostics. Never log credentials or the environment.
    return subprocess.run(["aws", *args, "--region", REGION, "--output", "json"],
                          check=True, capture_output=True, text=True, timeout=600,
                          env={**os.environ, "AWS_PAGER": "", "AWS_CLI_AUTO_PROMPT": "off",
                               "AWS_MAX_ATTEMPTS": "1"}).stdout


def publish(directory, expected):
    manifest = validate_package(directory, expected)
    hashes = {name: release.digest(directory / name) for name in FILES}
    session = f"AsaanPay-Publish-{expected['run_id']}-{expected['run_attempt']}"
    identity = json.loads(aws("sts", "get-caller-identity"))
    arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/AsaanPay-GitHubActions-Role/{session}"
    release.require(identity.get("Account") == ACCOUNT and identity.get("Arn") == arn,
                    "STS returned an unexpected account, role or session")
    print(f"Verified AWS identity: {arn}")
    prefix = f"releases/{expected['release_id']}/"
    with tempfile.TemporaryDirectory(prefix="asaanpay-s3-verify-") as temporary:
        downloaded = Path(temporary)
        # SHA256SUMS is deliberately last. A failure stops immediately; no cleanup,
        # overwrite, multipart fallback or reuse of an existing completion marker.
        for name in FILES:
            source, target = directory / name, downloaded / name
            release.require(source.stat().st_size <= MAX_PUT_BYTES, f"File grew beyond PUT limit: {name}")
            release.require(release.digest(source) == hashes[name], f"Local package changed: {name}")
            key = prefix + name
            common = ("--bucket", BUCKET, "--key", key, "--expected-bucket-owner", ACCOUNT)
            print(f"Uploading s3://{BUCKET}/{key}", flush=True)
            aws("s3api", "put-object", *common, "--body", str(source), "--if-none-match", "*",
                "--checksum-algorithm", "SHA256", "--checksum-sha256",
                base64.b64encode(bytes.fromhex(hashes[name])).decode("ascii"))
            target.parent.mkdir(parents=True, exist_ok=True)
            aws("s3api", "get-object", *common, str(target))
            release.require(release.digest(target) == hashes[name], f"S3 SHA-256 mismatch: {name}")
            print(f"Verified downloaded object: {name}", flush=True)
        release.require(validate_package(downloaded, expected) == manifest, "Downloaded manifest changed")
        release.validate_deployment(downloaded, manifest)
    print(f"PASS: all {len(FILES)} S3 files and complete release package verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check", "publish", "summary"))
    parser.add_argument("directory", type=Path, nargs="?")
    args = parser.parse_args()
    expected = expected_identity(os.environ)
    if args.command == "prepare":
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"artifact-name=asaanpay-release-{expected['release_id']}\n")
    elif args.command == "summary":
        result = "PASS" if os.environ.get("PUBLISH_RESULT") == "success" else "NOT VERIFIED / FAILED"
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(f"## S3 release publication\n\nRelease: {expected['release_id']}\n\n"
                         f"Git commit: {expected['commit']}\n\n"
                         f"S3 prefix: s3://{BUCKET}/releases/{expected['release_id']}/\n\n"
                         f"Expected file count: {len(FILES)}\n\nVerification: {result}\n")
    else:
        release.require(args.directory is not None, "Package directory is required")
        if args.command == "check":
            manifest = validate_package(args.directory, expected)
            release.validate_deployment(args.directory, manifest)
            print("PASS: local package integrity, deployment configuration and current run identity")
        else:
            publish(args.directory, expected)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr or "AWS/Compose command failed without stderr", file=sys.stderr)
        raise SystemExit(error.returncode or 1) from None
