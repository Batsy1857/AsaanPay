"""Image-owned release identity; runtime environment variables cannot override it."""

import argparse
import json
from pathlib import Path
import re

METADATA_PATH = Path(__file__).with_name("release.json")
DEV_VERSION = {"version": "dev", "commit": "dev"}


def validate_metadata(value):
    fields = {"version", "commit", "release_id", "run_id", "run_attempt"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Release metadata must contain exactly the required fields")
    patterns = {
        "version": r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
        "commit": r"[0-9a-f]{40}",
        "run_id": r"[1-9][0-9]*",
        "run_attempt": r"[1-9][0-9]*",
    }
    for field, pattern in patterns.items():
        if not isinstance(value[field], str) or not re.fullmatch(pattern, value[field]):
            raise ValueError(f"Invalid release metadata field: {field}")
    expected = f"{value['version']}-g{value['commit']}-r{value['run_id']}-a{value['run_attempt']}"
    if value["release_id"] != expected or len(expected) > 128:
        raise ValueError("Release identity is inconsistent or too long for an image tag")
    return value


def make_metadata(version, commit, run_id, run_attempt):
    return validate_metadata({
        "version": version, "commit": commit, "run_id": run_id, "run_attempt": run_attempt,
        "release_id": f"{version}-g{commit}-r{run_id}-a{run_attempt}",
    })


def read_metadata(path=None, *, required=False):
    path = METADATA_PATH if path is None else path
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise ValueError("Release metadata is required") from None
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("Release metadata is unreadable or malformed") from exc
    return validate_metadata(value)


def public_version(metadata):
    return {key: metadata[key] for key in ("version", "commit")} if metadata else DEV_VERSION.copy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", nargs=4, metavar=("VERSION", "COMMIT", "RUN_ID", "ATTEMPT"))
    args = parser.parse_args()
    if args.write:
        metadata = make_metadata(*args.write)
        with METADATA_PATH.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(metadata, sort_keys=True) + "\n")
        METADATA_PATH.chmod(0o444)
    else:
        print(json.dumps(read_metadata(required=True), sort_keys=True))


if __name__ == "__main__":
    main()
