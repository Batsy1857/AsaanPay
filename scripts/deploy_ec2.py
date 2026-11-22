#!/usr/bin/env python3
"""Standalone EC2 API deployment (Python 3.9+ stdlib, AWS CLI, Docker/Compose).

Never manages the database lifecycle; automatic rollback is disabled. Copy this
single file to the host; no checkout is required. Docker 25 / Compose 2.40.3 only.

Recovery requires --resume-interrupted --operations-stopped after an operator
has verified CLI/plugin AND daemon quiescence. Restoring current from a failed
candidate additionally requires --rollback-safe-from CANDIDATE, attesting live
database compatibility. SQL hash equality alone is never such an attestation.
Other transitions require --rollback-safe-from CURRENT. Same-release requests
verify health first and repair the API only when that verification fails.

Future SSM integration should supervise the entire script with a dedicated
systemd service and KillMode=control-group. SIGKILL/power loss cannot be handled
in Python: the durable journal prevents unattended recovery in that case.
"""

import argparse
from copy import deepcopy
from contextlib import contextmanager
import ctypes
import fcntl
import ipaddress
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib

PLATFORM = "linux/amd64"
PACKAGE_FILES = ("compose.prod.yaml", "db/schema.sql", "image.tar.gz", "manifest.json")
FILES = (*PACKAGE_FILES, "SHA256SUMS")
BASE = Path("/opt/asaanpay")
ACCOUNT = "642195693459"
VOLUME = "asaanpay_postgres_data"
NETWORK = 'asaanpay_default'
MIB = 1024 * 1024
FILE_LIMITS = {'image.tar.gz': 2048 * MIB, 'compose.prod.yaml': MIB,
               'db/schema.sql': 16 * MIB, 'manifest.json': MIB, 'SHA256SUMS': 65536}
PACKAGE_LIMIT = 2060 * MIB
EXPANDED_LIMIT = 4096 * MIB
DISK_RESERVE = 2048 * MIB
ARCHIVE_SECONDS = 120
OUTPUT_LIMIT = 8 * MIB


class Cancelled(BaseException):
    """Cancellation must never fall through into rollback or a retry."""


@contextmanager
def cancellation_handlers():
    def cancel(signum, frame):
        raise Cancelled()
    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def remaining(deadline, maximum=180):
    budget = deadline - time.monotonic()
    require(budget > 0, 'Verification deadline exceeded')
    return min(maximum, budget)


def bounded_read(path, limit):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'Metadata size limit exceeded')
    return data


def disk_headroom(path, needed):
    require(shutil.disk_usage(path).free >= needed + DISK_RESERVE, 'Insufficient disk headroom')


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Cannot prove absence; never treat permission denial as exit.


def reap_group(process):
    process.poll()  # Reap the direct child through Popen first.
    if process.returncode is not None:
        while True:
            try:
                pid, _ = os.waitpid(-process.pid, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break


def stop_group(process):
    # Never return (and consequently never unlock) while group termination is
    # unproven. An unkillable process requires manual intervention. SIGKILL or
    # host failure leaves the durable journal, which requires explicit recovery.
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 2)):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + grace
            while True:
                reap_group(process)
                if not group_alive(process.pid):
                    process.wait()
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        print('Operation has not stopped; retaining lock. Manual recovery required.', file=sys.stderr)
        while group_alive(process.pid):
            reap_group(process)
            time.sleep(0.1)
        process.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class DeploymentError(ValueError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


# Deliberately self-contained copies of release.py/app.version integrity rules.
# Regression tests check parity; EC2 needs neither module nor application dependencies.
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
    sizes = {name: (directory / name).stat().st_size for name in FILES}
    require(all(0 < size <= FILE_LIMITS[name] for name, size in sizes.items())
            and sum(sizes.values()) <= PACKAGE_LIMIT, 'Package size limit exceeded')
    entries = {}
    for line in bounded_read(directory / 'SHA256SUMS', FILE_LIMITS['SHA256SUMS']).decode().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, "Malformed checksum line or unsafe filename")
        checksum, name = match.groups()
        require(name in PACKAGE_FILES, "Unexpected checksum entry or unsafe path")
        require(name not in entries, "Duplicate checksum entry")
        entries[name] = checksum
    require(set(entries) == set(PACKAGE_FILES), "Missing checksum entry")
    for name, checksum in entries.items():
        require(not (directory / name).is_symlink() and digest(directory / name) == checksum, f"Checksum mismatch: {name}")
    manifest = json.loads(bounded_read(directory / 'manifest.json', FILE_LIMITS['manifest.json']))
    validate_manifest(manifest)
    require(entries["image.tar.gz"] == manifest["archive_sha256"], "Manifest/archive checksum mismatch")
    return manifest


def validate_compose(config, directory, manifest):
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
    networks = config.get('networks') or {'default': {'name': NETWORK}}
    require(set(networks) == {'default'}, 'Unexpected shared networks')
    network = networks['default'] or {}
    require(set(network) <= {'name', 'driver', 'internal', 'external', 'attachable',
                             'enable_ipv6', 'enable_ipv4', 'ipam', 'labels', 'driver_opts'}
            and network.get('name', NETWORK) == NETWORK
            and network.get('driver') in (None, '', 'bridge')
            and network.get('enable_ipv4', True) is True
            and not any(network.get(key) for key in ('internal', 'external', 'attachable',
                                                     'enable_ipv6', 'ipam', 'labels', 'driver_opts')),
            'Shared network configuration must remain unchanged')
    for service in services.values():
        require(not service.get('network_mode'), 'Network mode overrides are forbidden')
        attachments = service.get('networks') or {'default': None}
        require(isinstance(attachments, dict) and set(attachments) == {'default'}
                and not attachments['default'], 'Unexpected service network attachment')
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
    require(re.fullmatch(r"postgres:17-alpine@sha256:[0-9a-f]{64}", db_image),
            "Unsupported deployment database image")
    return db_image


def release_identity(release_id):
    match = re.fullmatch(r'((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-g([0-9a-f]{40})-r([1-9][0-9]*)-a([1-9][0-9]*)', release_id)
    require(match is not None, 'Malformed release ID')
    version, commit, run_id, attempt = match.groups()
    return validate_metadata(dict(version=version, commit=commit, run_id=run_id,
                                  run_attempt=attempt, release_id=release_id))


def validate_package(directory, release_id):
    expected = release_identity(release_id)
    manifest = check_checksums(directory)
    require(validate_manifest(manifest) == expected, 'Release does not match requested identity')
    require(not manifest['local_validation'] and not manifest['source_dirty'], 'Only clean CI packages are accepted')
    return manifest


def validate_archive(path, manifest):
    """Verify classic Docker-save or a Docker-save/OCI hybrid, without extraction.

    OCI-only exports, multi-platform runnable images and zstd OCI layers are
    deliberately unsupported. The hybrid must have one Docker manifest entry.
    Both the publisher digest and the platform config digest remain authenticated.
    """
    deadline = time.monotonic() + ARCHIVE_SECONDS
    members, payloads, hashes, diff_ids = {}, {}, {}, {}
    expanded = metadata_size = 0
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            remaining(deadline)
            name = member.name.rstrip('/')
            pure = PurePosixPath(name)
            require(name and not pure.is_absolute() and '..' not in pure.parts and '\\' not in name
                    and str(pure) == name and (member.isfile() or member.isdir()), 'Unsafe image archive member')
            require(name not in members and len(members) < 4096, 'Duplicate or excessive archive members')
            expanded += member.size
            require(expanded <= EXPANDED_LIMIT, 'Expanded archive size limit exceeded')
            members[name] = member
            if member.isfile():
                small = member.size <= MIB
                if small:
                    metadata_size += member.size
                    require(metadata_size <= 32 * MIB, 'Excessive archive metadata')
                chunks, checksum, uncompressed = [], hashlib.sha256(), hashlib.sha256()
                inflater = None
                first = True
                with archive.extractfile(member) as stream:
                    while True:
                        remaining(deadline)
                        data = stream.read(MIB)
                        if not data:
                            break
                        if first:
                            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if data.startswith(b'\x1f\x8b') else None
                            first = False
                        checksum.update(data)
                        if inflater is not None:
                            compressed = data
                            while compressed:
                                remaining(deadline)
                                plain = inflater.decompress(compressed, MIB)
                                expanded += len(plain)
                                require(expanded <= EXPANDED_LIMIT, 'Expanded archive size limit exceeded')
                                uncompressed.update(plain)
                                compressed = inflater.unconsumed_tail
                            require(not inflater.unused_data, 'Unsupported concatenated gzip layer')
                        if small:
                            chunks.append(data)
                hashes[name] = 'sha256:' + checksum.hexdigest()
                if inflater is not None:
                    require(inflater.eof, 'Truncated gzip layer')
                diff_ids[name] = 'sha256:' + uncompressed.hexdigest() if inflater is not None else hashes[name]
                if small:
                    payloads[name] = b''.join(chunks)
    remaining(deadline)

    def read_json(name):
        require(name in payloads, 'Missing or excessive archive metadata; unsupported Docker-save format')
        return json.loads(payloads[name])

    index = read_json('manifest.json')
    require(isinstance(index, list) and len(index) == 1, 'Archive must contain exactly one Docker image')
    entry = index[0]
    require(entry.get('RepoTags') == [manifest['image']], 'Archive tag mismatch')
    config = read_json(entry['Config'])
    config_id = hashes[entry['Config']]
    layers = entry.get('Layers')
    require(isinstance(layers, list) and layers and all(name in hashes for name in layers), 'Missing archive layers')
    require(config.get('rootfs', {}).get('type') == 'layers'
            and config['rootfs'].get('diff_ids') == [diff_ids[name] for name in layers], 'Archive layer identity mismatch')
    accepted_ids = {config_id}
    if 'index.json' in members:
        require(read_json('oci-layout') == {'imageLayoutVersion': '1.0.0'}, 'Unsupported OCI layout')
        roots = read_json('index.json')
        require(roots.get('schemaVersion') == 2 and len(roots.get('manifests', [])) == 1, 'Unsupported OCI roots')
        annotations = roots['manifests'][0].get('annotations') or {}
        require(annotations.get('org.opencontainers.image.ref.name', manifest['release_id'])
                in (manifest['release_id'], manifest['image'])
                and annotations.get('io.containerd.image.name', manifest['image'])
                in (manifest['image'], 'docker.io/library/' + manifest['image']), 'OCI archive tag mismatch')
        runnable = []
        visited = set()

        def visit(descriptor, depth=0):
            identity = descriptor.get('digest', '')
            require(re.fullmatch(r'sha256:[0-9a-f]{64}', identity) and depth <= 3
                    and identity not in visited, 'Invalid OCI descriptor graph')
            visited.add(identity)
            name = 'blobs/sha256/' + identity[7:]
            require(hashes.get(name) == identity and members[name].size == descriptor.get('size'), 'OCI blob identity mismatch')
            obj = read_json(name)
            media = descriptor.get('mediaType')
            if media in ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'):
                require(obj.get('schemaVersion') == 2 and 0 < len(obj.get('manifests', [])) <= 8, 'Unsupported OCI index')
                for child in obj['manifests']:
                    # BuildKit attestations are not runnable images; their blobs are
                    # authenticated by their descriptors but cannot supply runtime identity.
                    if child.get('annotations', {}).get('vnd.docker.reference.type') == 'attestation-manifest':
                        require(child.get('platform') == {'architecture': 'unknown', 'os': 'unknown'}, 'Invalid attestation platform')
                        attestation = child['digest']
                        require(re.fullmatch(r'sha256:[0-9a-f]{64}', attestation)
                                and hashes.get('blobs/sha256/' + attestation[7:]) == attestation
                                and members['blobs/sha256/' + attestation[7:]].size == child.get('size'),
                                'Attestation digest mismatch')
                        continue
                    visit(child, depth + 1)
            elif media in ('application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'):
                require(obj.get('schemaVersion') == 2 and obj.get('config', {}).get('digest') == config_id
                        and obj['config'].get('size') == members[entry['Config']].size,
                        'OCI config differs from Docker-save config')
                descriptors = obj.get('layers', [])
                require(len(descriptors) == len(layers), 'OCI layer count mismatch')
                for descriptor, layer in zip(descriptors, layers):
                    require(descriptor.get('digest') == hashes[layer]
                            and descriptor.get('size') == members[layer].size
                            and descriptor.get('mediaType') in ('application/vnd.oci.image.layer.v1.tar',
                                                               'application/vnd.oci.image.layer.v1.tar+gzip',
                                                               'application/vnd.docker.image.rootfs.diff.tar',
                                                               'application/vnd.docker.image.rootfs.diff.tar.gzip'),
                            'Unsupported OCI layer format or layer identity mismatch')
                runnable.append(identity)
            else:
                raise DeploymentError('Unsupported OCI media type')
            accepted_ids.add(identity)

        visit(roots['manifests'][0])
        require(len(runnable) == 1, 'Unsupported multi-platform OCI archive')
    require(manifest['image_id'] in accepted_ids, 'Archive image ID mismatch')
    validate_image({'Id': config_id, 'Os': config.get('os'), 'Architecture': config.get('architecture'),
                    'Config': config.get('config', {})}, validate_manifest(manifest), config_id)
    return {'config_id': config_id, 'publisher_id': manifest['image_id'], 'expanded_bytes': expanded}


def secure_path(path, *, directory=False, mode=None):
    info = path.lstat()
    require(not path.is_symlink() and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
            'Deployment path has an unsafe file type')
    require(info.st_uid == 0 and not info.st_mode & 0o022, 'Deployment path must be root-owned and not writable by others')
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode, 'Production environment/state permissions are unsafe')


@contextmanager
def deployment_lock(base):
    # Never unlink the lock: doing so would permit different inodes to be locked concurrently.
    fd = os.open(str(base / '.deploy.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'Unsafe deployment lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentError('Another deployment holds the lock', 3) from None
        yield fd
    finally:
        os.close(fd)


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_state(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.state-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()


class Deployer:
    def __init__(self, base=BASE):
        self.base = Path(base)
        self.releases = self.base / 'releases'
        self.env_file = self.base / 'config/production.env'
        self.state_file = self.base / 'deployment-state.json'
        self.lock_fd = None
        self.active_state = None
        self.identities = {}
        self.deadline = None
        self.network_snapshot = None
        # Empty credential/config files force AWS CLI to use EC2 instance metadata.
        # Never inherit static credentials, profiles, endpoints, proxies or Compose overrides.
        self.environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'AWS_CONFIG_FILE': '/dev/null',
                            'AWS_SHARED_CREDENTIALS_FILE': '/dev/null', 'AWS_EC2_METADATA_DISABLED': 'false',
                            'AWS_REGION': 'us-east-1', 'AWS_PAGER': '', 'AWS_CLI_AUTO_PROMPT': 'off',
                            'AWS_MAX_ATTEMPTS': '2'}

    def run(self, *args, env=None, timeout=180, input_text=None):
        if self.deadline is not None:
            timeout = remaining(self.deadline, timeout)
        deadline = time.monotonic() + timeout
        # Linux subreaper adopts orphaned CLI/plugin descendants so they can be
        # reaped before releasing the lock. No third-party Python dependency.
        if sys.platform == 'linux':
            require(ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0,
                    'Cannot enable child subreaper')
        process = None
        output = {1: bytearray(), 2: bytearray()}
        try:
            # Defer cancellation across Popen/assignment/journaling so a signal
            # cannot strand a launched process before we hold its handle.
            cancelled = []
            handlers = {sig: signal.signal(sig, lambda signum, frame: cancelled.append(signum))
                        for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=self.environment if env is None else env, start_new_session=True,
                                           pass_fds=() if self.lock_fd is None else (self.lock_fd,))
                if self.active_state is not None:
                    self.active_state['in_progress']['operation_pgid'] = process.pid
                    atomic_state(self.state_file, self.active_state)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
            if cancelled:
                raise Cancelled()
            pending = memoryview((input_text or '').encode())
            with selectors.DefaultSelector() as selector:
                for stream, kind in ((process.stdout, 1), (process.stderr, 2)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, kind)
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, 0)
                else:
                    process.stdin.close()
                while selector.get_map():
                    for key, _ in selector.select(remaining(deadline, 0.1)):
                        if key.data == 0:
                            count = os.write(key.fd, pending[:65536])
                            pending = pending[count:]
                            if not pending:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            data = os.read(key.fd, 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                            else:
                                output[key.data].extend(data)
                                require(sum(map(len, output.values())) <= OUTPUT_LIMIT, 'Command output limit exceeded')
                process.wait(timeout=remaining(deadline))
            reap_group(process)
            require(not group_alive(process.pid), 'Command left live descendants; manual recovery required')
            if process.returncode:
                # No output/argv can escape through exception formatting.
                code = ''
                if args[0] == 'aws':
                    match = re.search(rb'\((AccessDenied|NoSuchKey|NoSuchBucket|ExpiredToken|InvalidToken|RequestTimeout|SlowDown|ServiceUnavailable|InternalError)\)', output[2])
                    code = ' (' + match.group(1).decode() + ')' if match else ''
                raise DeploymentError('Command failed with exit ' + str(process.returncode) + code + '; output withheld', 4)
            remaining(deadline)
            return output[1].decode().strip()
        except BaseException:
            if process is not None:
                stop_group(process)
            raise
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


    def docker(self, *args, **kwargs):
        return self.run('docker', '--host', 'unix:///var/run/docker.sock', *args, **kwargs)

    def preflight(self):
        require(os.geteuid() == 0, 'Run as root through SSM or sudo')
        require(platform.system() == 'Linux' and platform.machine() == 'x86_64', 'Host must be Linux x86_64')
        for path in (self.base, self.base / 'config'):
            secure_path(path, directory=True)
        secure_path(self.env_file, mode=0o600)
        self.releases.mkdir(mode=0o700, exist_ok=True)
        secure_path(self.releases, directory=True)
        require(self.docker('compose', 'version', '--short').lstrip('v') == '2.40.3',
                'Compose version not validated; this deployment requires v2.40.3')
        require(self.docker('version', '--format', '{{.Server.Version}}').startswith('25.'),
                'Docker Engine version not validated; this deployment requires Docker 25')

    def read_state(self):
        if not self.state_file.exists() and not self.state_file.is_symlink():
            return {'current': None, 'in_progress': None}
        secure_path(self.state_file, mode=0o600)
        state = json.loads(bounded_read(self.state_file, MIB))
        require(isinstance(state, dict) and 'current' in state and 'in_progress' in state, 'Invalid deployment state')
        return state

    def stage(self, release_id):
        destination = self.releases / release_id
        if destination.exists() or destination.is_symlink():
            for path in (destination, destination / 'db'):
                secure_path(path, directory=True)
            for name in FILES:
                secure_path(destination / name)
            manifest = validate_package(destination, release_id)
            return destination, manifest
        # HEAD every object before creating downloads. Conditional ranged GETs
        # bind each transfer to the checked ETag and cannot write beyond its size.
        objects = {}
        for name in FILES:
            info = json.loads(self.run('aws', 's3api', 'head-object', '--region', 'us-east-1',
                                       '--bucket', 'asaanpay-releases', '--expected-bucket-owner', ACCOUNT,
                                       '--key', f'releases/{release_id}/{name}'))
            size, etag = info.get('ContentLength'), info.get('ETag', '')
            require(type(size) is int and 0 < size <= FILE_LIMITS[name], 'S3 object size limit exceeded')
            require(re.fullmatch(r'"[0-9a-fA-F]+(?:-[0-9]+)?"', etag), 'Invalid S3 object ETag')
            objects[name] = (size, etag)
        total = sum(size for size, _ in objects.values())
        require(total <= PACKAGE_LIMIT, 'Aggregate download size limit exceeded')
        disk_headroom(self.releases, total)
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=self.releases) as temporary:
            staging = Path(temporary)
            for name in ('SHA256SUMS', *PACKAGE_FILES):
                size, etag = objects[name]
                disk_headroom(self.releases, size)
                path = staging / name
                path.parent.mkdir(mode=0o700, exist_ok=True)
                response = json.loads(self.run('aws', 's3api', 'get-object', '--region', 'us-east-1',
                         '--bucket', 'asaanpay-releases', '--expected-bucket-owner', ACCOUNT,
                         '--key', f'releases/{release_id}/{name}', '--if-match', etag,
                         '--range', f'bytes=0-{size - 1}', str(path), timeout=600))
                require(path.stat().st_size == size and response.get('ContentLength') == size
                        and response.get('ETag') == etag, 'Downloaded object size or identity changed')
                path.chmod(0o600)
            manifest = validate_package(staging, release_id)
            for name in FILES:
                with (staging / name).open('rb') as stream:
                    os.fsync(stream.fileno())
            sync_directory(staging / 'db')
            sync_directory(staging)
            # Publish an immutable local cache atomically, never overwrite a release.
            staging.rename(destination)
            sync_directory(self.releases)
        return destination, manifest

    def compose(self, directory, manifest, *args, dummy=False, **kwargs):
        environment = {**self.environment, 'ASAANPAY_IMAGE': manifest['image']}
        if dummy:
            environment.update(POSTGRES_USER='validation', POSTGRES_DB='validation', POSTGRES_PASSWORD='dummy-only')
        return self.docker('compose', '--env-file', '/dev/null' if dummy else str(self.env_file),
                           '--project-directory', str(directory), '-f', str(directory / 'compose.prod.yaml'),
                           *args, env=environment, **kwargs)

    def configuration(self, directory, manifest):
        # Check both dummy interpolation and the actual production configuration.
        # Neither rendering nor its captured output is printed or saved.
        for dummy in (True, False):
            config = json.loads(self.compose(directory, manifest, 'config', '--format', 'json', dummy=dummy))
            validate_compose(config, directory, manifest)
            for name, service in config['services'].items():
                require(not service.get('command') and not service.get('entrypoint')
                        and not service.get('security_opt') and not service.get('profiles')
                        and not service.get('env_file'), 'Unexpected service execution override')
                if name == 'api':
                    require(service.get('user') in (None, '', '10001:10001'), 'Unsafe API user override')
            api = config['services']['api']['environment']
            db = config['services']['db']['environment']
            require(api.get('POSTGRES_HOST') == 'db' and str(api.get('POSTGRES_PORT')) == '5432', 'Unexpected database endpoint')
            require(all(api.get(key) and api.get(key) == db.get(key)
                        for key in ('POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PASSWORD')), 'Database environment mismatch')
        return config

    def inspect_archive(self, directory, manifest):
        result = json.loads(self.run(sys.executable, str(Path(__file__).resolve()), '--inspect-archive',
                                     str(directory), timeout=ARCHIVE_SECONDS))
        self.identities[manifest['release_id']] = result
        return result

    def image(self, manifest):
        info = json.loads(self.docker('image', 'inspect', manifest['image']))[0]
        identity = self.identities.get(manifest['release_id'], {'config_id': manifest['image_id']})
        require(info['Id'] in (manifest['image_id'], identity['config_id']), 'Loaded image identity mismatch')
        validate_image(info, validate_manifest(manifest), info['Id'])
        return info['Id']

    def network(self, db_id):
        network = json.loads(self.docker('network', 'inspect', NETWORK))[0]
        require(network.get('Name') == NETWORK and network.get('Driver') == 'bridge'
                and network.get('Scope') == 'local' and not network.get('Internal')
                and not network.get('Attachable') and not network.get('Ingress')
                and not network.get('EnableIPv6') and network.get('EnableIPv4', True)
                and not network.get('Options') and not network.get('ConfigOnly'), 'Unsafe live shared network')
        labels = network.get('Labels') or {}
        require(labels.get('com.docker.compose.project') == 'asaanpay'
                and labels.get('com.docker.compose.network') == 'default', 'Shared network ownership mismatch')
        ipam = network.get('IPAM') or {}
        require(ipam.get('Driver') == 'default' and not ipam.get('Options')
                and len(ipam.get('Config', [])) == 1, 'Unexpected live network IPAM')
        allocation = ipam['Config'][0]
        require(set(allocation) == {'Subnet', 'Gateway'}
                and ipaddress.ip_network(allocation['Subnet']).version == 4
                and ipaddress.ip_address(allocation['Gateway']) in ipaddress.ip_network(allocation['Subnet']),
                'Unexpected live network allocation')
        db = self.container('db')
        attachment = db.get('NetworkSettings', {}).get('Networks', {}).get(NETWORK, {})
        require(db['Id'] == db_id and db_id in network.get('Containers', {})
                and attachment.get('NetworkID') == network['Id'] and 'db' in (attachment.get('Aliases') or []),
                'Database shared-network attachment mismatch')
        # Engine-allocated subnet is pinned on first adoption; every later check
        # compares it, the network ID and all shared properties exactly.
        snapshot = {key: value for key, value in network.items() if key not in ('Containers', 'Peers')}
        if self.network_snapshot is not None:
            require(snapshot == self.network_snapshot, 'Live shared network changed')
        self.network_snapshot = snapshot
        return network['Id']

    def container(self, service):
        ids = self.docker('ps', '--all', '--quiet', '--filter', 'label=com.docker.compose.project=asaanpay',
                          '--filter', 'label=com.docker.compose.service=' + service).splitlines()
        require(len(ids) == 1, 'Expected exactly one existing ' + service + ' container')
        return json.loads(self.docker('container', 'inspect', ids[0], timeout=20))[0]

    def database(self, config):
        # Existing installation only: never start/init/recreate PostgreSQL or create its volume.
        volume = json.loads(self.docker('volume', 'inspect', VOLUME))[0]
        require(volume.get('Name') == VOLUME and volume.get('Driver') == 'local' and not volume.get('Options'),
                'Existing PostgreSQL volume is missing or has unsafe driver options')
        db = self.container('db')
        require(db.get('State', {}).get('Running') and db['State'].get('Health', {}).get('Status') == 'healthy',
                'Existing PostgreSQL container must be healthy')
        image = json.loads(self.docker('image', 'inspect', config['services']['db']['image']))[0]
        require(db['Image'] == image['Id'] and image['Os'] == 'linux' and image['Architecture'] == 'amd64',
                'Database image differs from the pinned package; database upgrades require separate review')
        mounts = [m for m in db.get('Mounts', []) if m.get('Destination') == '/var/lib/postgresql/data']
        require(len(mounts) == 1 and mounts[0].get('Type') == 'volume' and mounts[0].get('Name') == VOLUME
                and mounts[0].get('RW') is True, 'Existing database is not using the expected persistent volume')
        actual = dict(item.split('=', 1) for item in db['Config'].get('Env', []) if '=' in item)
        require(all(actual.get(key) == config['services']['db']['environment'].get(key)
                    for key in ('POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PASSWORD')), 'Production environment differs from existing database')
        return db['Id']

    def responder(self, api, manifest):
        bindings = {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8000'}]}
        require(api.get('HostConfig', {}).get('PortBindings') == bindings
                and api.get('NetworkSettings', {}).get('Ports') == bindings, 'Actual API port bindings differ')
        # Probe inside this exact container's network namespace, never a possibly
        # unrelated host listener. The image's stdlib Python reads full embedded
        # release metadata; host Python imports no application dependencies.
        probe = """import json,time,urllib.request
from app.version import read_metadata
end=time.monotonic()+float(__import__('sys').argv[1])
def get(path):
    budget=end-time.monotonic()
    if budget<=0: raise TimeoutError()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8000'+path, timeout=min(5,budget)) as r:
        if r.status!=200: raise ValueError()
        data=r.read(65537)
        if len(data)>65536: raise ValueError()
        return json.loads(data)
print(json.dumps({'health':get('/api/health'),'version':get('/api/version'),'metadata':read_metadata()}))
"""
        budget = remaining(self.deadline, 10)
        data = json.loads(self.docker('exec', api['Id'], 'python', '-c', probe, str(budget), timeout=budget))
        require(data == {'health': {'status': 'ok', 'database': 'connected'},
                         'version': {key: manifest[key] for key in ('version', 'commit')},
                         'metadata': validate_manifest(manifest)}, 'Container HTTP or full release identity mismatch')

    def health(self, manifest, config, db_id):
        self.deadline = time.monotonic() + 120
        try:
            while True:
                remaining(self.deadline)
                try:
                    runtime_id = self.image(manifest)
                    require(self.database(config) == db_id, 'Database container changed during deployment')
                    network_id = self.network(db_id)
                    api = self.container('api')
                    require(api['Image'] == runtime_id and api['State'].get('Running')
                            and api['State'].get('Health', {}).get('Status') == 'healthy', 'API image or health mismatch')
                    labels = api.get('Config', {}).get('Labels') or {}
                    require(labels.get('io.asaanpay.release-id') == manifest['release_id'], 'API release label mismatch')
                    require(api.get('NetworkSettings', {}).get('Networks', {}).get(NETWORK, {}).get('NetworkID') == network_id,
                            'API network identity mismatch')
                    remaining(self.deadline)
                    self.responder(api, manifest)
                    require(self.container('api')['Id'] == api['Id'], 'API changed during verification')
                    remaining(self.deadline)  # Slow successful calls cannot bypass the deadline.
                    return
                except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
                    time.sleep(remaining(self.deadline, 2))
        finally:
            self.deadline = None

    def api_configuration(self, config, network_id):
        api = deepcopy(config['services']['api'])
        api.pop('depends_on', None)
        api['networks'] = {'default': None}
        result = {'name': 'asaanpay', 'services': {'api': api},
                  'networks': {'default': {'external': True, 'name': network_id}}}
        # Already-interpolated production values must remain literal during the
        # second Compose parse. Feed via stdin; never persist or log credentials.
        def literal(value):
            if isinstance(value, str):
                return value.replace('$', '$$')
            if isinstance(value, list):
                return [literal(item) for item in value]
            if isinstance(value, dict):
                return {key: literal(item) for key, item in value.items()}
            return value
        return json.dumps(literal(result))

    def start_api(self, directory, manifest):
        config = self.configuration(directory, manifest)
        db_id = self.database(config)
        network_id = self.network(db_id)
        projected = self.api_configuration(config, network_id)
        self.docker('compose', '--env-file', '/dev/null', '--project-name', 'asaanpay', '-f', '-',
                    'up', '--detach', '--no-deps', '--no-build', '--pull', 'never', '--wait',
                    '--wait-timeout', '120', 'api', input_text=projected, timeout=180)

    def verified_previous(self, record):
        if record is None:
            return None
        release_id = record['release_id']
        release_identity(release_id)
        directory = self.releases / release_id
        for path in (directory, directory / 'db'):
            secure_path(path, directory=True)
        for name in FILES:
            secure_path(directory / name)
        manifest = validate_package(directory, release_id)
        require(record['hashes'] == {name: digest(directory / name) for name in FILES}
                and record['image_id'] == manifest['image_id'], 'Previously verified deployment record no longer matches its package')
        config = self.configuration(directory, manifest)
        self.inspect_archive(directory, manifest)
        return directory, manifest, config

    def deploy(self, release_id, *, rollback_safe_from=None, resume_interrupted=False,
               operations_stopped=False):
        release_identity(release_id)
        if rollback_safe_from is not None:
            release_identity(rollback_safe_from)
        self.preflight()
        with deployment_lock(self.base) as lock_fd, cancellation_handlers():
            self.lock_fd = lock_fd
            try:
                self._deploy(release_id, rollback_safe_from, resume_interrupted, operations_stopped)
            finally:
                self.lock_fd = None
                self.active_state = None

    def _deploy(self, release_id, rollback_safe_from, resume_interrupted, operations_stopped):
        state = self.read_state()
        pending, current = state['in_progress'], state['current']
        self.network_snapshot = (current or {}).get('network') or (pending or {}).get('network')
        restoring = bool(pending and release_id != pending['release_id'])
        if pending:
            require(resume_interrupted and operations_stopped,
                    'Interrupted deployment: confirm CLI/plugin AND Docker daemon operations stopped, then use --resume-interrupted --operations-stopped')
            pgid = pending.get('operation_pgid')
            require(pgid is None or not group_alive(pgid), 'Previous process group is still present; manual recovery required')
            require(release_id in (pending['release_id'], (current or {}).get('release_id')), 'Recovery target is not recorded')
            if restoring:
                require(current is not None and rollback_safe_from == pending['release_id'],
                        'Restoration requires --rollback-safe-from matching the interrupted candidate and explicit live-database compatibility approval')
        else:
            require(not resume_interrupted and not operations_stopped, 'No interrupted deployment to recover')
        previous = self.verified_previous(current)
        if rollback_safe_from and not restoring:
            require(current and rollback_safe_from == current['release_id'], 'Compatibility approval must identify the source release')
        # Cached historical targets can also be downgrades. Explicit approval is
        # required for every transition from a recorded current release. Equality
        # of init SQL is only a conservative veto, never live-schema proof.
        if current and release_id != current['release_id'] and not pending:
            require(rollback_safe_from == current['release_id'], 'Release transition requires explicit live-database compatibility approval via --rollback-safe-from')
        directory, manifest = self.stage(release_id)
        hashes = {name: digest(directory / name) for name in FILES}
        identity = self.inspect_archive(directory, manifest)
        config = self.configuration(directory, manifest)
        db_id = self.database(config)
        self.network(db_id)
        if restoring:
            candidate_dir, candidate = self.stage(pending['release_id'])
            candidate_config = self.configuration(candidate_dir, candidate)
            require(digest(candidate_dir / 'db/schema.sql') == hashes['db/schema.sql']
                    and candidate_config['services']['db']['image'] == config['services']['db']['image'],
                    'Schema/database change prohibits restoration; separate operational review required')
        elif rollback_safe_from and previous:
            require(hashes['db/schema.sql'] == current['hashes']['db/schema.sql']
                    and config['services']['db']['image'] == previous[2]['services']['db']['image'],
                    'Schema/database changes require separate operational review')
        if current and current['release_id'] == release_id and not pending:
            try:
                self.health(manifest, config, db_id)
            except DeploymentError:
                pass  # Explicit request for this same approved release may repair it.
            else:
                print('PASS: requested release is already deployed and healthy')
                return
        docker_root = Path(json.loads(self.docker('info', '--format', '{{json .DockerRootDir}}')))
        require(docker_root.is_absolute(), 'Invalid Docker storage location')
        secure_path(docker_root, directory=True)
        disk_headroom(docker_root, identity['expanded_bytes'] * 2)
        disk_headroom(self.releases, MIB)
        # Journal BEFORE image loading. SIGKILL, loss of SSM, or uncertain daemon
        # work can never silently turn into an ordinary retry. Future SSM wiring
        # should run this entire script in a systemd service with KillMode=control-group.
        state['in_progress'] = {'release_id': release_id, 'previous_release_id': (current or {}).get('release_id'),
                                'network': self.network_snapshot, 'operation_pgid': None}
        atomic_state(self.state_file, state)
        self.active_state = state
        try:
            self.docker('load', '--input', str(directory / 'image.tar.gz'), timeout=600)
            self.image(manifest)
            self.start_api(directory, manifest)
            self.health(manifest, config, db_id)
        except Exception as error:
            detail = str(error) if isinstance(error, DeploymentError) else 'details withheld'
            raise DeploymentError('Update failed; automatic rollback disabled; interrupted journal retained for explicit recovery: ' + detail, 5) from None
        atomic_state(self.state_file, {'current': {'release_id': release_id, 'image_id': manifest['image_id'],
                     'hashes': hashes, 'network': self.network_snapshot}, 'in_progress': None})
        self.active_state = None
        print('PASS: deployed and verified release ' + release_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release_id', help='Exact version-gCOMMIT-rRUN-aATTEMPT release ID')
    parser.add_argument('--rollback-safe-from', metavar='RELEASE_ID',
                        help='Explicit operational approval that the target is compatible with the live database after RELEASE_ID; never enables automatic rollback')
    parser.add_argument('--resume-interrupted', action='store_true',
                        help='After reviewing live state, retry the journaled target or restore the recorded current release')
    parser.add_argument('--operations-stopped', action='store_true',
                        help='Operator has verified all previous CLI/plugin and Docker daemon operations have stopped')
    parser.add_argument('--inspect-archive', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.inspect_archive:
            directory = Path(args.release_id)
            manifest = check_checksums(directory)
            print(json.dumps(validate_archive(directory / 'image.tar.gz', manifest)))
            return 0
        Deployer().deploy(args.release_id, rollback_safe_from=args.rollback_safe_from,
                          resume_interrupted=args.resume_interrupted, operations_stopped=args.operations_stopped)
    except (Cancelled, KeyboardInterrupt):
        print('FAIL: cancelled; any interrupted journal requires explicit recovery', file=sys.stderr)
        return 130
    except DeploymentError as error:
        print('FAIL: ' + str(error), file=sys.stderr)
        return error.code
    except Exception:
        # Parsing, filesystem and daemon responses may contain secrets: no raw traceback.
        print('FAIL: invalid input, unavailable tool or local state error; details withheld', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
