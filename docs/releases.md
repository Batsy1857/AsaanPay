# Milestone 4 release operations

The pipeline is now **tests + Bandit + Gitleaks + pip-audit → one release image →
Trivy → smoke test → Docker archive → reload verification → Actions artifact**.
AWS S3 publication, EC2 infrastructure and deployment are later milestones.

## Identity and runtime behavior

`VERSION` contains the application version, initially `1.0.4`. Update it deliberately
when making an application release. A build generates:

```text
<version>-g<full lowercase 40-character commit SHA>-r<run ID>-a<run attempt>
```

The Docker tag is `asaanpay:<release-id>` and the artifact name is
`asaanpay-release-<release-id>`. Attempts and distinct workflow runs cannot collide.
On pull requests, the SHA identifies the checked-out merge commit used by Actions,
not necessarily the source branch's head commit.

`app/version.py` validates the version, full SHA, positive numeric run ID/attempt
and their relationship to the release ID. The Docker release target writes
`/app/app/release.json` as a root-owned, mode `0444` file. Matching OCI/custom image
labels record the same identity. Runtime environment variables do not override
this data. The release runs as `10001:10001`, validates metadata before starting
one Uvicorn worker, and fails startup if metadata is absent or invalid.

`GET /api/version` returns only `version` and `commit`. It needs no database or
checkout operation. Without a metadata file, local development explicitly returns
`{"version":"dev","commit":"dev"}`. Present but malformed/inconsistent metadata
returns HTTP 503 with a generic error. It never falls back to a fabricated release.
`/api/health`, payment rules, `rule_set_version="sandbox_v1"`, schema and other
existing contracts remain unchanged.

The footer uses `/static/version.js` to fetch `/api/version` with a three-second
abort timeout. It uses `textContent` and validates response fields before showing
`AsaanPay v1.0.4 · a1b2c3d`, or `AsaanPay dev`. On network, HTTP, parsing or metadata
failure it shows `AsaanPay · version unavailable`. The request neither blocks nor
modifies checkout/Explorer controls, checkout state or idempotency storage.

## Build and scan

The Ubuntu 24.04 job `build-release` requires all four original jobs and has only
`contents: read` permission. Existing SHA-pinned checkout/setup-python/upload
Actions are reused, with checkout credential persistence disabled.

`Dockerfile` has a shared runtime stage, an explicit `release` target, and the
last/default `development` target. Compose still builds the default target with
tests and scripts available. The release contains only requirements and `app/`;
scanner tools, tests, scripts, schema and benchmark results are absent. The build
context allowlist excludes `.env`, Git history, keys and database data. No runtime
credentials are passed as build arguments.

`scripts/release.py build` executes exactly one `docker build --pull --platform
linux/amd64 --target release`. It checks HEAD and requires a clean committed
checkout unless explicitly doing local validation. Its state file records the
Docker image ID. Every later stage checks that ID; nothing rebuilds the release.
The earlier independent `tests` job builds its separate development image as before.

The existing `python:3.13-slim` base and direct dependency pins are preserved.
The release target applies the Debian zlib security patch (`--only-upgrade zlib1g`)
and uninstalls pip after dependency installation; its bundled vulnerable installer
libraries are not needed to run FastAPI. Development retains pip. This intentionally
prevents package installation inside a production container; update the Docker
image instead. No Python/application dependency or distribution major upgrade occurs.
`--pull` obtains current patches, but the base tag and transitive Python resolution
are not locked: repeated builds need not produce identical bytes. The saved image
ID and archive checksum identify the actual tested bytes. A reviewed base digest
and complete dependency lock are future reproducibility improvements.

Trivy 0.69.3 is installed from a fixed official archive with a committed checksum.
The scan requires a fresh vulnerability DB, covers OS and language packages, and
binds its report/approval to the image ID. Its summary starts with PASS/FAIL and
the blocking count, preserves complete severity counts, and groups High/Critical
findings by advisory and Debian source package. Counts come from the current JSON
inventory. [Security triage](security-triage.md) records dated reviews; missing CVE,
source or Debian-release matches are `NOT TRIAGED` and require review. That label
does not suppress findings or change the blocking decision. Fixable High/Critical findings block;
unfixed findings remain visible for review. Scanner/DB/coverage failures block.
See [the complete security policy](security.md#milestone-4-release-image-vulnerability-gate).

## Smoke testing and isolation

The helper creates a UUID-named network and two UUID-named containers. PostgreSQL
17 initializes the unchanged `db/schema.sql` into a **tmpfs** data directory; it
never mounts an existing named or anonymous database volume. A random temporary
password is passed through process environment, not saved in the image, state or
artifact. The API runs by image ID with a read-only filesystem, dropped capabilities
and no-new-privileges; its host port binds only to loopback with an ephemeral port.

Verification requires an initialized empty table, exact healthy response, matching
HTTP version, checkout HTML and all CSS/JS files, a terminal `SUCCEEDED` synthetic
checkout, a matching PostgreSQL record, successful retrieval, and idempotent replay
without a second record. Cleanup removes only the resources created by that helper.
On a hard process kill or runner failure, the ephemeral GitHub runner is the final
cleanup boundary; never use volume/system prune as a cleanup shortcut.

The optional `smoke --full-tests --browser` mounts only tests/scripts into the
release container for the full PostgreSQL suite and invokes the existing browser
runner. That browser runner serves assets from the checkout; the independent smoke
HTTP assertions verify assets served by the release image itself.

## Artifact contract

```text
asaanpay-release-<release-id>/
  image.tar.gz
  manifest.json
  SHA256SUMS
```

`docker save` exports the verified tag (and therefore the same verified image ID).
Gzip omits filename/time headers. `manifest.json` includes:

| Field | Meaning |
| --- | --- |
| `schema_version` | Artifact schema, currently `1` |
| `release_id`, `version`, `commit` | Release identity, application version, full Git SHA |
| `run_id`, `run_attempt` | GitHub Actions identity as strings |
| `platform` | `linux/amd64` |
| `image`, `image_id` | Unique tag and Docker image ID |
| `archive`, `archive_sha256` | `image.tar.gz` and its SHA-256 |
| `local_validation`, `source_dirty` | Explicit provenance flags; CI requires both false |

`SHA256SUMS` covers both the compressed archive and the manifest. The verifier
rejects missing/extra package files, unsafe or duplicate checksum names, bad
checksums, inconsistent metadata, wrong tags/IDs/platforms and dirty CI provenance.
It loads the compressed file with `docker load`, compares image labels and embedded
metadata, then starts the loaded image against another empty disposable database
and checks `/api/version` and the complete smoke path again. No rebuild occurs.
Checksums detect corruption; they are not signatures. Obtain the package from the
trusted successful Actions run and review its SHA/run identity. A future deployment
must also reject packages marked local/dirty.

## Reproduce locally

Run from the repository root with Python 3.13, Docker and (for browser validation)
Chrome/Chromium. Python helpers use the standard library except for the existing
browser/integration test dependencies. Avoid running against any existing database.

```sh
release_work="$(mktemp -d)"
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v
python3 scripts/release.py build --commit "$(git rev-parse HEAD)" \
  --run-id "$(date +%s)" --attempt 1 --local --state "$release_work/state.json"
image_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_id"])' "$release_work/state.json")"
release_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["metadata"]["release_id"])' "$release_work/state.json")"
bash .github/scripts/install_trivy.sh "$release_work/bin"
bash .github/scripts/trivy_gate.sh "$release_work/bin/trivy" "$image_id" "$release_work/security"
.venv/bin/python scripts/release.py smoke --state "$release_work/state.json" --full-tests --browser
python3 scripts/release.py package --state "$release_work/state.json" \
  --scan-receipt "$release_work/security/approved.json" --output "$release_work/packages"
python3 scripts/release.py verify "$release_work/packages/asaanpay-release-$release_id"
```

Stop on any failed command. Local run numbers are validation identifiers, not
actual Actions runs; `--local` is recorded in the manifest. Do not deploy these
packages. Only committed Actions builds produce the later release candidate.

## Verify and load a downloaded artifact

Download **asaanpay-release-…**, unzip it into its own directory and run:

```sh
cd /path/to/downloaded/asaanpay-release-RELEASE_ID
sha256sum --check --strict SHA256SUMS
# macOS equivalent: shasum -a 256 --check --strict SHA256SUMS
python3 -m json.tool manifest.json
docker load --input image.tar.gz
image="$(python3 -c 'import json; print(json.load(open("manifest.json"))["image"])')"
docker image inspect "$image" --format '{{.Id}} {{.Os}}/{{.Architecture}} {{json .Config.Labels}}'
docker run --rm --platform linux/amd64 --network none --read-only "$image" python -m app.version
```

Compare the printed image ID, platform, labels and metadata to the manifest. For
automated verification including live HTTP and persistence, from the source
checkout run `python3 scripts/release.py verify /absolute/path/to/package`. This
uses the checkout's unchanged schema solely to initialize a disposable database;
it does not build or replace application code. The container requires PostgreSQL
settings for normal service startup. Never supply local secrets while checking an
untrusted image.

A later AWS milestone will transfer this same three-file package to S3, retrieve
it on an amd64 EC2 host, verify it, load it, and start the recorded image with
separately supplied runtime configuration and persistent PostgreSQL. That stage
must validate identity/provenance, retain rollback artifacts, re-evaluate dated
security findings, and check health/version. None of those AWS actions are
implemented here.

## Verify GitHub Actions after review

1. Commit and push the reviewed changes to `feature/aws-cicd-devsecops`; no workflow
   dispatch trigger is added. The implementation session itself does neither.
2. Open **Batsy1857/AsaanPay → Actions → CI** for that exact pushed SHA. Expect the
   original four jobs, followed by **Versioned image, Trivy and release artifact**.
3. Confirm 89 integration tests with zero skips, all 11 database methods and 46
   browser checks. Verify the original Bandit, Gitleaks and pip-audit policies pass.
4. In the new job, confirm `linux/amd64`, full SHA/run/attempt, exactly one release
   build, Trivy 0.69.3 checksum verification, fresh DB evidence, policy summary,
   smoke success and successful reload/integrity verification.
5. Download the uniquely named release artifact (90-day requested retention, subject to repository/organization limits), verify its
   checksums and manifest as above. `local_validation` and `source_dirty` must both
   be false. Download `image-security-<run-id>-<attempt>` for full scan evidence.
6. A scanner/DB/policy failure must produce a failed job and no release artifact.
   Fixable High/Critical findings require reviewed remediation, not weaker gates.
   A rerun uses a new attempt suffix. No AWS credentials or publication should appear.


## M4B artifact maintenance

All three upload steps use official `actions/upload-artifact` **v7.0.2**, pinned to
`cf430e030ddbb5b0abf93d22962f4752f3646cd9`. On 2026-11-20 the official
[latest release](https://github.com/actions/upload-artifact/releases/tag/v7.0.2),
[tag reference](https://api.github.com/repos/actions/upload-artifact/git/ref/tags/v7.0.2)
and [action definition at that exact commit](https://github.com/actions/upload-artifact/blob/cf430e030ddbb5b0abf93d22962f4752f3646cd9/action.yml)
were checked independently of the audit: the runtime is `node24` and archive
upload remains enabled by default. Existing inputs/names and upload conditions
are preserved. Checkout/setup-python pins are unchanged.

Release packages and image-security evidence now request **90 days**. Test logs
still request seven days. Repository/organization/enterprise retention limits
remain authoritative; confirm effective `expires_at` on the next hosted run.
No account retention setting is changed, and this does not extend the lifetime
of artifacts uploaded by previous workflow runs.
