"""Build once, smoke-test, save and verify an amd64 release. No publication/deployment."""

import argparse
from contextlib import contextmanager
import gzip
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

# Support both `python scripts/release.py` and unittest imports.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.version import make_metadata, public_version, validate_metadata  # noqa: E402

PLATFORM = "linux/amd64"
DEPLOYMENT_FILES = ("compose.prod.yaml", "db/schema.sql")
PACKAGE_FILES = tuple(sorted(("image.tar.gz", "manifest.json", *DEPLOYMENT_FILES)))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args, timeout=180, env=None):
    result = subprocess.run(args, check=True, capture_output=True, text=True,
                            timeout=timeout, env=env)
    return result.stdout.strip()


def docker(*args, **kwargs):
    return run("docker", *args, **kwargs)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_image(reference):
    return json.loads(docker("image", "inspect", reference))[0]


def validate_image(info, metadata, image_id):
    validate_metadata(metadata)
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id), "Invalid image ID")
    require(info["Id"] == image_id, "Image ID changed")
    require(f"{info['Os']}/{info['Architecture']}" == PLATFORM, "Wrong image platform")
    labels = info["Config"].get("Labels") or {}
    for label, key in (("org.opencontainers.image.version", "version"),
                       ("org.opencontainers.image.revision", "commit"),
                       ("io.asaanpay.release-id", "release_id"),
                       ("io.asaanpay.run-id", "run_id"),
                       ("io.asaanpay.run-attempt", "run_attempt")):
        require(labels.get(label) == metadata[key], f"Image label mismatch: {label}")
    require(info["Config"]["User"] == "10001:10001", "Release must run as the unprivileged user")


def check_image(state):
    metadata = validate_metadata(state["metadata"])
    info = inspect_image(state["image"])
    validate_image(info, metadata, state["image_id"])
    embedded = json.loads(docker("run", "--rm", "--platform", PLATFORM, "--network", "none",
                                "--read-only", state["image_id"], "python", "-m", "app.version"))
    require(embedded == metadata, "Embedded release metadata differs from expected identity")
    return info


def build(args):
    metadata = make_metadata((ROOT / "VERSION").read_text().strip(), args.commit, args.run_id, args.attempt)
    require(run("git", "rev-parse", "HEAD") == metadata["commit"], "Commit does not match checkout HEAD")
    dirty = bool(run("git", "status", "--porcelain", "--untracked-files=all"))
    require(args.local or not dirty, "CI releases require a clean committed checkout; use --local for validation only")
    image = f"asaanpay:{metadata['release_id']}"
    require(not args.state.exists(), "State file already exists; use a fresh validation directory")
    command = ["docker", "build", "--pull", "--platform", PLATFORM, "--target", "release", "--tag", image]
    for key, value in (("APP_VERSION", metadata["version"]), ("GIT_COMMIT", metadata["commit"]),
                       ("RUN_ID", metadata["run_id"]), ("RUN_ATTEMPT", metadata["run_attempt"]),
                       ("RELEASE_ID", metadata["release_id"])):
        command.extend(["--build-arg", f"{key}={value}"])
    subprocess.run([*command, str(ROOT)], check=True, timeout=1200)
    state = {"metadata": metadata, "image": image, "image_id": inspect_image(image)["Id"],
             "source_dirty": dirty, "local_validation": args.local}
    check_image(state)
    write_json(args.state, state)
    print(json.dumps(state, indent=2))


def http(base, path, *, payload=None, key=None):
    address = urlsplit(base)
    require(address.scheme == "http" and address.hostname == "127.0.0.1", "Smoke HTTP must use local loopback")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    connection = HTTPConnection(address.hostname, address.port, timeout=5)
    try:
        connection.request("POST" if payload is not None else "GET", path,
                           body=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", ""), response.read()
    finally:
        connection.close()


def wait_for(probe, description):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except (subprocess.CalledProcessError, HTTPException, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {description}")


@contextmanager
def stack(image_id, *, test_mounts=False, schema=None, db_image="postgres:17-alpine"):
    schema = ROOT / "db/schema.sql" if schema is None else schema
    # UUID-owned resources, no named/anonymous database volume and no .env loading.
    name = "asaanpay-smoke-" + uuid4().hex
    db, api = name + "-db", name + "-api"
    password = secrets.token_hex(32)
    environment = {**os.environ, "POSTGRES_PASSWORD": password}
    created = []
    try:
        docker("network", "create", name)
        created.append(("network", name))
        docker("create", "--name", db, "--network", name, "--network-alias", "db",
               "--tmpfs", "/var/lib/postgresql/data:rw",
               "--mount", f"type=bind,src={schema},dst=/docker-entrypoint-initdb.d/001-schema.sql,readonly",
               "--env", "POSTGRES_USER=asaanpay_smoke", "--env", "POSTGRES_DB=asaanpay_smoke",
               "--env", "POSTGRES_PASSWORD", "--platform", PLATFORM, db_image, env=environment)
        created.append(("container", db))
        docker("start", db)
        wait_for(lambda: docker("exec", db, "pg_isready", "-U", "asaanpay_smoke", "-d", "asaanpay_smoke")
                 and query(db, "SELECT count(*) FROM payment_attempts") == "0", "initialized PostgreSQL")
        mounts = []
        if test_mounts:
            for folder in ("tests", "scripts"):
                mounts.extend(["--mount", f"type=bind,src={ROOT / folder},dst=/app/{folder},readonly"])
        docker("create", "--name", api, "--platform", PLATFORM, "--network", name,
               "--read-only", "--tmpfs", "/run/asaanpay:rw,noexec,nosuid,size=64m,mode=1777",
               "--env", "TMPDIR=/run/asaanpay", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--publish", "127.0.0.1::8000",
               "--env", "POSTGRES_HOST=db", "--env", "POSTGRES_USER=asaanpay_smoke",
               "--env", "POSTGRES_DB=asaanpay_smoke", "--env", "POSTGRES_PASSWORD",
               *mounts, image_id, env=environment)
        created.append(("container", api))
        docker("start", api)
        port = docker("port", api, "8000/tcp").split(":")[-1]
        base = "http://127.0.0.1:" + str(int(port))
        wait_for(lambda: http(base, "/api/health")[0] == 200, "release API")
        yield base, db, api
    finally:
        # Never prune or address any pre-existing project/volume.
        failed = sys.exc_info()[0] is not None
        cleanup_errors = []
        for kind, resource in reversed(created):
            try:
                if kind == "container":
                    if failed:
                        try:
                            print(docker("logs", resource).replace(password, "***"))
                        except subprocess.SubprocessError:
                            print(f"Container diagnostics unavailable: {resource}")
                    docker("rm", "--force", resource)
                else:
                    docker("network", "rm", resource)
            except subprocess.SubprocessError:
                cleanup_errors.append(resource)
        if cleanup_errors:
            raise RuntimeError(f"Failed to remove owned disposable resources: {cleanup_errors}")


def query(db, sql):
    return docker("exec", db, "psql", "-v", "ON_ERROR_STOP=1", "-U", "asaanpay_smoke",
                  "-d", "asaanpay_smoke", "-Atc", sql)


def exercise(base, db, metadata):
    require(json.loads(http(base, "/api/health")[2]) == {"status": "ok", "database": "connected"}, "Unhealthy API")
    require(json.loads(http(base, "/api/version")[2]) == public_version(metadata), "Version endpoint mismatch")
    for path, content_type, marker in (("/", "text/html", b'id="checkout-form"'),
                                      ("/static/index.html", "text/html", b'id="version-badge"'),
                                      ("/static/styles.css", "text/css", b"#version-badge"),
                                      ("/static/core.js", "javascript", b"CheckoutClient"),
                                      ("/static/app.js", "javascript", b"checkout-form"),
                                      ("/static/version.js", "javascript", b"/api/version")):
        status, mime, body = http(base, path)
        require(status == 200 and content_type in mime and marker in body, f"Missing checkout asset: {path}")
    payment = {"amount_pkr": "100.00", "card_preset": "approved", "billing_country": "PK",
               "execution_mode": "concurrent", "workload_profile": "lightweight"}
    key = str(uuid4())
    status, _, body = http(base, "/api/checkouts", payload=payment, key=key)
    result = json.loads(body)
    require(status == 201 and result["state"] == "SUCCEEDED" and result["terminal"], "Unexpected checkout result")
    attempt_id = str(UUID(result["id"]))
    stored = json.loads(query(db, "SELECT row_to_json(p) FROM payment_attempts p WHERE id = '" + attempt_id + "'"))
    require(stored["state"] == "SUCCEEDED" and stored["idempotency_key"] == key
            and stored["rule_set_version"] == "sandbox_v1" and stored["finished_at"], "Checkout was not persisted")
    require(json.loads(http(base, "/api/checkouts/" + attempt_id)[2])["state"] == "SUCCEEDED", "Stored GET failed")
    replay_status, _, replay = http(base, "/api/checkouts", payload=payment, key=key)
    require(replay_status == 200 and json.loads(replay)["id"] == attempt_id, "Idempotent replay failed")
    require(query(db, "SELECT count(*) FROM payment_attempts") == "1", "Replay created an extra record")
    print("PASS: initialized empty PostgreSQL, health, version, checkout/assets, persisted SUCCEEDED and replay")


def smoke(args, state):
    check_image(state)
    with stack(state["image_id"], test_mounts=args.full_tests) as (base, db, api):
        exercise(base, db, state["metadata"])
        if args.full_tests:
            # Only tests/scripts are mounted; the application is the actual release image.
            subprocess.run(["docker", "exec", "-e", "RUN_DB_TESTS=1", api, "python", "-m",
                            "unittest", "discover", "-s", "tests", "-v"], check=True, timeout=300)
        if args.browser:
            subprocess.run([sys.executable, str(ROOT / "tests/run_frontend.py"), "--url", base],
                           check=True, timeout=120)
    state["smoke_passed"] = state["image_id"]
    write_json(args.state, state)


def validate_manifest(manifest):
    metadata = validate_metadata({key: manifest[key] for key in
                                  ("version", "commit", "release_id", "run_id", "run_attempt")})
    require(manifest["schema_version"] == 1 and manifest["platform"] == PLATFORM, "Unsupported manifest")
    require(manifest["image"] == "asaanpay:" + metadata["release_id"], "Image tag differs from release identity")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["image_id"]), "Invalid manifest image ID")
    require(manifest["archive"] == "image.tar.gz", "Unexpected archive name")
    require(re.fullmatch(r"[0-9a-f]{64}", manifest["archive_sha256"]), "Invalid archive digest")
    require(type(manifest["local_validation"]) is bool and type(manifest["source_dirty"]) is bool,
            "Missing source provenance flags")
    require(manifest["local_validation"] or not manifest["source_dirty"], "Dirty source cannot be a CI release")
    return metadata


def check_checksums(directory):
    require(directory.is_dir() and not directory.is_symlink(), "Invalid package directory")
    expected = set(PACKAGE_FILES) | {"SHA256SUMS"}
    found = set()
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            name = path.relative_to(directory).as_posix()
            require(not path.is_symlink(), f"Symlink in release package: {name}")
            if path.is_dir():
                require(name == "db", f"Unexpected package directory: {name}")
                pending.append(path)
            else:
                require(name in expected and path.is_file(), f"Unexpected package file: {name}")
                found.add(name)
    require(found == expected, "Missing release package files")
    entries = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, "Malformed checksum line or unsafe filename")
        checksum, name = match.groups()
        require(name in PACKAGE_FILES, "Unexpected checksum entry or unsafe path")
        require(name not in entries, "Duplicate checksum entry")
        entries[name] = checksum
    require(set(entries) == set(PACKAGE_FILES), "Missing checksum entry")
    for name, checksum in entries.items():
        require(not (directory / name).is_symlink() and digest(directory / name) == checksum, f"Checksum mismatch: {name}")
    manifest = json.loads((directory / "manifest.json").read_text())
    validate_manifest(manifest)
    require(entries["image.tar.gz"] == manifest["archive_sha256"], "Manifest/archive checksum mismatch")
    return manifest


def package(args, state):
    check_image(state)
    require(state.get("smoke_passed") == state["image_id"], "Image has not passed smoke testing")
    # The scan receipt is written only after a complete scan and successful policy evaluation.
    scan = json.loads(args.scan_receipt.read_text())
    require(scan == {"image_id": state["image_id"], "policy": "fixable-high-critical", "trivy": "0.69.3"},
            "Missing/mismatched security approval")
    destination = args.output / ("asaanpay-release-" + state["metadata"]["release_id"])
    destination.mkdir(parents=True, exist_ok=False)
    for name in DEPLOYMENT_FILES:
        source = ROOT / name
        require(not source.is_symlink() and not source.parent.is_symlink() and source.is_file(),
                f"Invalid deployment source: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    archive = destination / "image.tar.gz"
    # Save by the verified unique tag so docker load restores that tag as well as the ID.
    with tempfile.TemporaryDirectory(prefix="asaanpay-export-") as temporary:
        tar_path = Path(temporary) / "image.tar"
        docker("save", "--output", str(tar_path), state["image"], timeout=600)
        with tar_path.open("rb") as source, archive.open("wb") as output:
            with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
    manifest = {**state["metadata"], "schema_version": 1, "platform": PLATFORM,
                "image": state["image"], "image_id": state["image_id"], "archive": archive.name,
                "archive_sha256": digest(archive), "local_validation": state["local_validation"],
                "source_dirty": state["source_dirty"]}
    write_json(destination / "manifest.json", manifest)
    (destination / "SHA256SUMS").write_text("".join(
        f"{digest(destination / name)}  {name}\n" for name in PACKAGE_FILES), encoding="utf-8")
    check_checksums(destination)
    print(destination)


def validate_deployment(directory, manifest):
    # Compose config only renders/validates; it never starts production services.
    # Do not inherit application secrets, COMPOSE_FILE or interpolation overrides.
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG") if key in os.environ}
    environment.update(POSTGRES_USER="asaanpay_validation", POSTGRES_DB="asaanpay_validation",
                       POSTGRES_PASSWORD="dummy-validation-only", ASAANPAY_IMAGE=manifest["image"])
    config = json.loads(docker("compose", "--env-file", "/dev/null", "--project-directory",
                               str(directory.resolve()), "-f", str((directory / "compose.prod.yaml").resolve()),
                               "config", "--format", "json", env=environment))
    services = config["services"]
    require(config.get("name") == "asaanpay", "Unexpected Compose project name")
    require(set(services) == {"api", "db"}, "Unexpected deployment services")
    require(all("build" not in service for service in services.values()), "Deployment must not build images")
    require(services["api"]["image"] == manifest["image"], "Compose image differs from release identity")
    require(all(service.get("platform", PLATFORM) == PLATFORM for service in services.values()),
            "Wrong deployment platform")
    # Normalize absent/null/empty defaults without accepting host namespace sharing.
    for name, service in services.items():
        require(not service.get("privileged") and not service.get("cap_add"),
                f"Privileged service or added capabilities: {name}")
        for field, allowed in (("network_mode", (None, "", "default", "bridge", "none")),
                               ("pid", (None, "", "private")),
                               ("ipc", (None, "", "private", "shareable"))):
            require(service.get(field) in allowed, f"Unsafe {field}: {name}")
        for field in ("devices", "device_cgroup_rules", "gpus", "use_api_socket", "volumes_from",
                      "tmpfs", "configs", "secrets", "pre_start", "post_start", "pre_stop"):
            require(not service.get(field), f"Unexpected {field}: {name}")
        resources = (service.get("deploy") or {}).get("resources") or {}
        reservations = resources.get("reservations") or {}
        require(not reservations.get("devices"), f"Unexpected device reservations: {name}")
    for network in (config.get("networks") or {}).values():
        network = network or {}
        require(network.get("driver") in (None, "", "bridge") and not network.get("external")
                and not network.get("driver_opts"), "Deployment requires managed bridge networks")
    require(not services["db"].get("ports"), "Database must not publish host ports")
    ports = services["api"].get("ports") or []
    require(len(ports) == 1 and ports[0].get("host_ip") == "127.0.0.1"
            and str(ports[0].get("target")) == "8000"
            and str(ports[0].get("published")) == "8000"
            and ports[0].get("protocol", "tcp") == "tcp",
            "API must publish only 127.0.0.1:8000:8000/tcp")
    volumes = config.get("volumes") or {}
    require(set(volumes) == {"postgres_data"}, "Unexpected deployment volumes")
    volume = volumes["postgres_data"] or {}
    require(volume.get("name") == "asaanpay_postgres_data"
            and volume.get("driver") in (None, "", "local")
            and not volume.get("driver_opts") and not volume.get("external"),
            "Database requires the managed asaanpay_postgres_data volume without driver options")
    require(not services["api"].get("volumes"), "API must not mount host files or volumes")
    db_mounts = services["db"].get("volumes") or []
    data_mounts = [mount for mount in db_mounts if mount.get("target") == "/var/lib/postgresql/data"]
    require(len(db_mounts) == 2 and len(data_mounts) == 1
            and data_mounts[0].get("type") == "volume"
            and data_mounts[0].get("source") == "postgres_data"
            and not data_mounts[0].get("read_only")
            and not (data_mounts[0].get("volume") or {}).get("subpath"),
            "Database must mount its persistent data volume and only the packaged schema")
    schema_mounts = [mount for mount in services["db"].get("volumes", [])
                     if mount["target"] == "/docker-entrypoint-initdb.d/001-schema.sql"]
    require(len(schema_mounts) == 1 and schema_mounts[0]["type"] == "bind"
            and schema_mounts[0].get("read_only") is True
            and schema_mounts[0]["source"] == str((directory / "db/schema.sql").resolve()),
            "Compose must mount the packaged schema read-only")
    db_image = services["db"]["image"]
    require(re.fullmatch(r"postgres:17-alpine(?:@sha256:[0-9a-f]{64})?", db_image),
            "Unsupported deployment database image")
    return db_image


def verify(directory):
    manifest = check_checksums(directory)
    db_image = validate_deployment(directory, manifest)
    # Docker itself consumes the compressed archive; no source checkout/build is used.
    docker("load", "--input", str(directory / "image.tar.gz"), timeout=600)
    state = {"metadata": validate_manifest(manifest), "image": manifest["image"], "image_id": manifest["image_id"]}
    check_image(state)
    with stack(state["image_id"], schema=(directory / "db/schema.sql").resolve(), db_image=db_image) as (base, db, _api):
        exercise(base, db, state["metadata"])
    print(f"PASS: checksums, packaged Compose/schema, docker load, image ID/platform/labels/file and HTTP metadata: {manifest['image_id']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--commit", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--attempt", required=True)
    create.add_argument("--local", action="store_true", help="Mark an uncommitted local validation, never a CI release")
    for command in (create, commands.add_parser("smoke"), commands.add_parser("package")):
        command.add_argument("--state", type=Path, required=True)
        if command.prog.endswith("smoke"):
            command.add_argument("--full-tests", action="store_true")
            command.add_argument("--browser", action="store_true")
        if command.prog.endswith("package"):
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--scan-receipt", type=Path, required=True)
    commands.add_parser("verify").add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args)
    elif args.command == "verify":
        verify(args.directory)
    else:
        state = json.loads(args.state.read_text())
        (smoke if args.command == "smoke" else package)(args, state)


if __name__ == "__main__":
    main()
