# AsaanPay Milestone 4 — Independent DevSecOps Audit

Nov 20, 2026 · @Sadiq

## Scope and evidence

This is a read-only audit of GitHub Actions run 44779970253 (commit `ea8d2d2`, image `sha256:ba433b82…`). No repository files were changed.

- **Inspected:** Dockerfile, `ci.yml`, `trivy_gate.sh`, `install_trivy.sh`, `release.py`, `requirements.txt`, the full `trivy.json`, `summary.md`, `db-metadata.json`, `approved.json`, the Milestone 4 report, `security.md`, `releases.md` and the Actions screenshot.
- **Consistency:** `trivy.json`, `summary.md`, `approved.json` and `db-metadata.json` all describe the same CI image (`ba433b82…`, release `1.0.4-gea8d2d2…-r44779970253-a1`). The image ID `ed23dac…` in the M4 report is the earlier local candidate. That difference is expected.
- **Not available:** `scripts/trivy_policy.py`, `.github/scripts/test_trivy_gate.py`, `app/` source and `compose.yaml`. The policy is judged by its outputs and by the receipt check in `release.py`. Whether the app imports `sqlite3` is unverified.
- **Checked externally (2026-11-20):** the Debian security tracker entry for each of the 11 CVEs, and the official tags and `action.yml` files for upload-artifact, checkout, setup-python and Trivy (read with `git`).

**Verified** means read from the report, the source or an official page. **Inferred** means reasoning; each inferred point comes with a check that would confirm it.

## 1. Executive assessment

The Milestone 4 security implementation is acceptable, and stronger than most coursework pipelines. The 47 High findings do not show an incomplete implementation. The problem is that the current summary does not explain them.

**What the 47 actually are (verified):**

- The 47 findings are **11 CVEs in 6 Debian source packages**, counted once per binary package. util-linux alone accounts for 36: 4 CVEs × 9 binaries.
- Debian's security team has triaged all 11 for Debian 13. Ten are `<no-dsa> (Minor issue)` and one (perl) is `<postponed>`. All 11 are fixed upstream and in Debian testing/unstable. None has a Debian 13 update yet.
- For 42 of the 47, the HIGH label did not come from Debian or NVD; the report has no Debian or NVD severity for them. It comes from other vendors' ratings, mostly Red Hat's CVSS 7.0–8.8. The remaining 5 (ncurses ×4, perl ×1) use NVD's score.
- All 11 need local access (CVSS `AV:L`), or sit in code that is not installed (Archive::Tar is not in `perl-base`). **Inferred, high confidence:** none is reachable through the FastAPI HTTP interface.

**Gate quality (verified):**

- The gate scans the exact image ID that is later packaged.
- It downloads a fresh vulnerability database on every run; the database was 3 h 56 min old at scan time.
- It fails closed, configures no ignores, and binds approval to the image through a receipt.

**Most important remaining risks, in priority order:**

1. **The EC2 runtime is not hardened yet.** The util-linux escalation paths rely on a setuid `mount` binary and root. The smoke test already runs non-root with `--read-only`, `--cap-drop ALL` and `no-new-privileges`. Production must use the same flags. *(Must fix, Milestone 5.)*
2. **Findings are dated, and the gate only runs at build time.** An approved image can gain fixable or new findings later. Nothing re-scans it before deployment or rollback. *(Must fix before AWS.)*
3. **The summary wording is wrong.** It says "no published fix", but fixes exist upstream; Debian has simply not backported them to Debian 13 yet. The summary also gives no triage context. This is the part most likely to mislead your professor. *(Must fix; cheap.)*
4. **The targeted `--only-upgrade zlib1g` patch does not scale.** When Debian ships its next security update for any other package, the build will fail until someone edits the Dockerfile. *(Recommended.)*
5. **Reproducibility drifts.** The base tag and transitive Python dependencies float. Evidence: the image contains pydantic 2.14.4, pydantic-core 2.50.5 and annotated-doc 0.0.7, while the Milestone 3 snapshot listed 2.14.3, 2.50.4 and 0.0.6. *(Recommended.)*
6. **upload-artifact v4.6.2 still targets Node 20.** This is maintenance, not a vulnerability. *(Cheap; do it now so the demo run shows no warnings.)*

**Verdict:** keep the blocking policy. Improve how the findings are explained, harden the runtime, and add a re-scan before deployment.

## 2. Vulnerability analysis

The 47 High findings reduce to 11 CVEs. Every one is unfixed in Debian 13, fixed upstream and in Debian unstable, and unreachable over HTTP in this container. Each CVE link goes to its Debian tracker entry, as checked on 2026-11-20.

| CVE | Debian source (binaries flagged) | Findings | Flaw | What an attacker needs | Debian 13 status | Fixed in | HTTP exposure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | util-linux (9) | 9 | `mount` runs privileged X-mount post-hooks after a failed helper | Local user, setuid `mount`, user-mountable fstab entry | no-dsa, minor | util-linux 2.42.3; Debian unstable 2.42.3-1 | None |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | util-linux (9) | 9 | `nsenter --join-cgroup` leaks a root-opened fd | Root operator runs `nsenter --join-cgroup` into the attacker's process | no-dsa, minor | 2.41.6 / 2.42.3 ([Red Hat](https://access.redhat.com/security/cve/CVE-2026-78408)); Debian unstable 2.42.4-1 | None |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | util-linux (9) | 9 | X-mount.subdir symlink escape (Linux 6.15+) | Local user with an fstab-authorized X-mount.subdir entry | no-dsa, minor | Debian unstable 2.42.3-1 | None |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | util-linux (9) | 9 | Restricted bind mount source not pinned | Local user, setuid `mount`, fstab entry with X-mount.owner/group/mode | no-dsa, minor | 2.42.3; Debian unstable 2.42.3-1 | None |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | ncurses (4) | 4 | Stack overflow in the `infocmp` CLI | Someone runs `infocmp` on a crafted terminfo entry | no-dsa, minor | ncurses 6.5-20251213; Debian unstable 6.6+20251231-1 | None |
| [CVE-2026-119373](https://security-tracker.debian.org/tracker/CVE-2026-119373) | sqlite3 (1) | 1 | FTS5 heap overflow on prefix query | Attacker-controlled SQL and FTS5 data | no-dsa, minor | SQLite 3.54.0; Debian unstable 3.54.1-1 | None: app uses PostgreSQL |
| [CVE-2026-119374](https://security-tracker.debian.org/tracker/CVE-2026-119374) | sqlite3 (1) | 1 | FTS5 secure-delete overflow | Attacker-supplied database file | no-dsa, minor | SQLite 3.54.0; Debian unstable 3.54.1-1 | None |
| [CVE-2026-119379](https://security-tracker.debian.org/tracker/CVE-2026-119379) | sqlite3 (1) | 1 | Session changeset heap overflow | Attacker-supplied changeset | no-dsa, minor | SQLite 3.54.0; Debian unstable 3.54.1-1 | None |
| [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | systemd (2) | 2 | systemd-homed skips a home-record signature check | Logged-in user managed by systemd-homed | no-dsa, minor | systemd 258.10 / 261.2; Debian unstable 261.2-1 | None: homed absent, only `libsystemd.so` present |
| [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | acl (1) | 1 | libacl path functions follow symlinks | Privileged process does ACL operations on an attacker-controlled path | No DSA; fix via point release | acl 2.4.0; Debian unstable 2.4.0-1 | None: no getfacl/setfacl installed |
| [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | perl (1) | 1 | Archive::Tar memory exhaustion | A Perl program reads an untrusted tar | postponed, minor | Archive::Tar 3.10; Debian unstable 5.42.3-1 | None: Archive::Tar not in `perl-base` |

Total: 4 × 9 + 4 + 3 + 2 + 1 + 1 = 47. Trivy marks 46 as `affected` and 1 (perl) as `fix_deferred`, which matches Debian's `<postponed>` entry.

**Answers to your seven questions**

1. **Which vulnerabilities are present:** the 11 CVEs above. There are also 109 Medium, 67 Low and 9 Unknown findings, none with a Debian 13 fix. Python packages: 0 findings across 17 packages.
2. **Which packages introduce them:** 18 binaries from 6 source packages, as listed above. Seventeen come from the Debian base layer; `libncursesw6` was added by the official Python image's install step.
3. **Genuinely unfixed in Debian 13:** all 11 (verified on the tracker). Debian classes them as minor and chose not to issue a security advisory (DSA). Fixes usually arrive in a later point release.
4. **Fixes through newer packages or bases:** only Debian testing/unstable ships them, and those have no production security support. The scanned base is the 2026-11-04 Debian 13 snapshot, pulled fresh with `--pull`. Bases that leave these packages out remove most findings; see Remediation.
5. **Duplicates:** yes. 36 of 47 are one util-linux source package counted 9 times per CVE. ncurses counts 4 times, systemd twice.
6. **Unnecessary at runtime:** the app uses none of the vulnerable code paths. Most packages are still part of Debian's essential base set, as the next table shows.
7. **HTTP exposure:** none. Each attack needs a local process, and most need setuid binaries or root. The realistic risk is a chain: if an attacker first gets code execution through an application or library bug, these become privilege-escalation steps. That is why running non-root with `no-new-privileges` and `--cap-drop ALL` matters more than the count.

**Runtime need, by source package**

| Source package | Why it is in the image | Used by AsaanPay? | Removable inside Debian 13? |
| --- | --- | --- | --- |
| util-linux | Essential base tools (`mount`, `login`, `su`, `nsenter`). `libuuid1` is also used by Python's optional `_uuid` module. | No | No: essential packages; dpkg and apt maintainer scripts expect them |
| ncurses | `ncurses-base` and `ncurses-bin` are essential. `libtinfo6` is required by bash. `libncursesw6` serves Python's curses and readline. | No | No |
| sqlite3 | Debian base layer; Python's `sqlite3` module links it; `liblastlog2-2` depends on it | No *(check: `grep -rnE "sqlite3\|dbm\|shelve" app/`)* | Yes, together with `liblastlog2-2`: −7 findings, −3 CVEs |
| systemd | `libsystemd0` is needed by apt, libapt-pkg and libpam-modules-bin; `libudev1` by libapt-pkg | No | Not without removing apt and PAM |
| acl | `libacl1` is needed by `passwd` | No | No |
| perl | `perl-base` is essential (dpkg and debconf scripts) | No | No |

Dependency data comes from Trivy's `DependsOn` field. Confirm any removal with `apt-get -s purge <pkg>` before relying on it.

## 3. Remediation options

The changes that most improve real security barely move the 47. The changes that do cut the count mean switching base images, and that is a bigger job than this assignment needs. Options are ranked by security value for the effort.

| Rank | Option | Security gain | Effort | Compatibility risk | Effect on the 47 | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Run the container on EC2 with the smoke-test hardening: non-root, `--read-only` plus tmpfs, `--cap-drop ALL`, `no-new-privileges` | High: closes the setuid paths used by CVE-2026-76642/-78409/-78410 | Low | Low; already exercised by `release.py smoke` | 0 | **Must** (Milestone 5) |
| 2 | Replace `--only-upgrade zlib1g` with `apt-get upgrade -y` | Medium: applies every published Debian 13 fix automatically | Trivial | Low; stable-release updates only | 0 today, since all fixable findings are already 0 | Recommended |
| 3 | Strip setuid/setgid bits in the release stage | Medium: removes setuid `mount`/`su`/`passwd` even if runtime flags are forgotten | Low | Low; the app never needs setuid | 0 | Recommended |
| 4 | Pin the base by digest, let Dependabot bump it, and use a hash-locked requirements file | Medium: reproducible, reviewable supply chain | Low–medium | Low | 0 | Recommended |
| 5 | Purge `libsqlite3-0` and `liblastlog2-2` | Low: the code was unreachable anyway | Low | Low–medium: breaks `import sqlite3` and `dbm.sqlite3` | 47 → 40 (−3 CVEs) | Optional; mostly cosmetic, so say so |
| 6 | [Docker Hardened Images](https://hub.docker.com/hardened-images/catalog/dhi/python/guides) `dhi.io/python:3.13`: `-dev` stage to build, runtime stage to run | High: no shell, apt, util-linux or perl | Medium: new multi-stage build, no shell (rewrite `CMD`), Docker ID login and secret in CI, default UID 65532 | Medium | Most findings likely disappear *(unverified until built and scanned)* | Optional stretch, on a separate branch |
| 7 | Distroless `gcr.io/distroless/python3-debian13` | High | Medium–high: uses Debian's Python 3.13 build rather than upstream 3.13.18, no pip, site-packages copied over with `PYTHONPATH`, no shell | Medium | util-linux, perl and ncurses-bin go; Python's own library dependencies stay | Not now |
| 8 | `python:3.13-alpine` | Medium | Medium | Medium–high: musl libc, needs musllinux wheels for asyncpg and pydantic-core; counts come from a different CVE feed and aren't comparable | Large drop, partly an artifact of the feed | Not recommended |
| 9 | Chainguard free images | — | — | The free tier has historically tracked only the latest Python, which would break the 3.13 constraint *(verify)* | — | Not recommended |
| 10 | Debian testing (forky) or Debian 12 (bookworm) base | — | — | Testing has no production security support; bookworm is older and mostly worse | — | Not recommended |
| 11 | Force-remove essential packages (`util-linux`, `perl-base`) | — | — | High: unsupported, breaks dpkg and apt scripts | — | Do not do |

Upgrading Python (3.14 or later) changes none of these findings, because they are all operating-system packages. Splitting the test-only `httpx` out of the production requirements removes 3 packages but breaks `smoke --full-tests` unless tests install it separately (optional).

**Sketch for options 2 and 3** (release stage only; the development target is untouched):

```dockerfile
FROM runtime AS release
# Apply every published Debian 13 update; drop pip and setuid/setgid bits.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} +
```

**Sketch for option 1** (Milestone 5 EC2 run; mirrors `release.py`'s smoke flags):

```sh
docker run -d --name asaanpay --restart unless-stopped \
  --read-only --tmpfs /run/asaanpay:rw,noexec,nosuid,size=64m --env TMPDIR=/run/asaanpay \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 256 --memory 512m \
  --env-file /etc/asaanpay/runtime.env \
  -p 80:8000 \
  "asaanpay:${RELEASE_ID}"
```

Restrict port 80 with the EC2 security group, or bind to `127.0.0.1` behind a reverse proxy.

**Dockerfile observations that change no counts:**

- The "multi-stage" build is really a target selection, because `release` inherits from `runtime`. Removing pip deletes it from the runtime filesystem, which is what Trivy scans, but its bytes remain in a lower layer of `image.tar.gz`. That is harmless; just describe it as "removed from the runtime filesystem".
- The ensurepip bundled pip wheel is probably still present at `/usr/local/lib/python3.13/ensurepip/_bundled/` *(inferred)*. Trivy does not scan wheels, and the container is non-root and read-only, so it cannot be installed. Check with `docker run --rm --entrypoint sh IMAGE -c 'ls /usr/local/lib/python3.13/ensurepip/_bundled'`.
- Strengths to keep: non-root UID 10001, root-owned mode-0444 metadata, no secrets in build args, an allowlisted build context, and `CMD` using `exec`. `CMD` needs `/bin/sh` (dash), which is one reason distroless or DHI would need a rewrite.

## 4. Trivy gate and policy review

The gate is implemented correctly, and the fixable-High/Critical threshold is defensible for a sandbox. Keep it. The gaps are around it, not in it: there is no re-scan after build, no check on the database's own age, evidence retention is short, and the summary is weak.

| Question | Finding | Evidence | Action |
| --- | --- | --- | --- |
| Covers OS and Python packages? | Yes | `--pkg-types os,library`; the report lists 87 deb and 17 Python packages | None |
| Vulnerability database fresh? | Yes: a new empty cache every run. Gap: nothing checks the database's own age. | `UpdatedAt` 13:11:33Z, scan 17:07:59Z (3 h 56 min) | Fail if `UpdatedAt` is older than 48 h (recommended) |
| Do scanner failures fail the job? | Yes | `set -euo pipefail`; `approved.json` deleted first and written only after the policy passes; `package` requires a receipt for the same image ID | None |
| Can it be bypassed by accident? | Unlikely | Empty `--config`, `--ignorefile /dev/null`, `--ignore-unfixed=false`, every severity listed, no `continue-on-error`; the release upload has no `always()` condition | Optional: refuse to run if any `TRIVY_*` variable is set, since environment variables are not neutralized |
| Severity threshold appropriate? | Yes for OS packages. Gap for Python packages. | pip-audit blocks every advisory, but only for its own resolution of `requirements.txt`. The image's Python packages can drift from that resolution (section 1, risk 5), and Trivy blocks them only at High/Critical. | Block fixable Python-package findings at any severity (recommended) |
| Policy implemented correctly? | Yes, judged by its outputs | 0 fixable at every severity; 47 High shown as REVIEW; receipt `{image_id: ba433b82…, policy: fixable-high-critical, trivy: 0.69.3}`. `trivy_policy.py` itself was not reviewed. | None |
| Is the scanned image the packaged image? | Yes | Scan by image ID; `ArtifactName` and `RepoTags` match the release ID; `package()` rejects a mismatched receipt; `verify` reloads the archive and compares the ID | None |
| Unfixed findings preserved? | Yes, but evidence expires after 14 days | Full `trivy.json` (232 OS findings) uploaded with `retention-days: 14` | Raise to 90 days (the default repository maximum) so it survives grading |
| Summary accurate? | Partly | "no published fix" is wrong; there is no Debian status, no CVE grouping and no remediation history; the image ID is repeated 47 times | See section 6 |
| Scanner version | Safe, but behind | v0.69.3 (2026-03-03) is listed safe in [Aqua's advisory](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23), where v0.69.4–v0.69.6 were compromised. Current tag: v0.76.2 (2026-11-05). | Upgrade deliberately, with checksum and cosign verification (recommended, not urgent) |

**Recommended policy for an academic sandbox that will run on AWS**

- **BLOCK the release** on:
  - any fixable Critical or High finding in an OS or Python package (unchanged)
  - any fixable Python-package finding at any severity (new)
  - a scanner, database or inventory failure (unchanged)
  - a vulnerability database older than 48 h (new)
- **REVIEW, without blocking:** unfixed Critical and High findings. Record each CVE in a reviewed `docs/security-triage.md`: Debian status, whether it applies, the compensating control, the owner and a recheck date. This file documents the findings and must never feed an ignore list or VEX suppression. The summary marks any High CVE missing from it as `NOT TRIAGED`.
- **Unfixed Critical:** the gate stays green, but deployment needs a written sign-off in the triage file.
- **RE-SCAN:** the Milestone 5 deploy job re-scans the exact artifact with a fresh database and the same policy before rolling out to EC2, and again before any rollback. Optionally, add a weekly scheduled re-scan of the deployed release.
- **KEEP:** full JSON evidence for 90 days, plus a CycloneDX SBOM if wanted (`--format cyclonedx`).

This policy is defensible because it separates what you can act on (fixable findings, which block) from what you can only mitigate (unfixed findings, which are triaged and visible). It never hides findings to get a clean number.

## 5. GitHub Actions warning resolution

Update all three `actions/upload-artifact` steps to **v7.0.2, pinned at `cf430e030ddbb5b0abf93d22962f4752f3646cd9`**. No inputs need to change. Every action and SHA below was checked against the official repositories by cloning each tag and reading its `action.yml`.

| Action | Pinned SHA | Tag | Runtime | Status |
| --- | --- | --- | --- | --- |
| upload-artifact (current) | `ea165f8d65b6e75b540449e92b4886f43607fa02` | v4.6.2 | node20 | Causes both warnings |
| upload-artifact | `330a01c490aca151604b8cf639adc76d48f6c5d4` | v5.0.0 | node20 | Would not clear the warning |
| upload-artifact | `b7c566a772e6b6bfb58ed0dc250532a479d7789f` | v6.0.0 (2025-12-12) | node24 | Works; needs runner ≥ 2.327.1, which GitHub-hosted runners meet |
| **upload-artifact (recommended)** | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` | **v7.0.2 (2026-10-07)** | node24 | Latest; adds an optional `archive` input that defaults to `true`, so behaviour is unchanged |
| checkout | `d23441a48e516b6c34aea4fa41551a30e30af803` | v6.1.0 | node24 | Fine; comment says only `# v6` |
| setup-python | `ece7cb06caefa5fff74198d8649806c4678c61a1` | v6.3.0 (= `v6`) | node24 | Fine |

The workflow uses upload-artifact in three places: `ci-logs` in `tests`, plus the release package and security evidence in `build-release`. The screenshot shows only two warnings because GitHub reports one per job. Inputs in use (`name`, `path`, `retention-days`, `compression-level`, `if-no-files-found`) are the same in v7. See the [release notes](https://github.com/actions/upload-artifact/releases).

```diff
-        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
+        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
```

Make the version comments precise while you are there. This is cosmetic:

```yaml
uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0
uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0
```

You can check the pin yourself: `git ls-remote https://github.com/actions/upload-artifact refs/tags/v7.0.2` prints `cf430e03…`. If you want a smaller jump, v6.0.0 is acceptable.

To keep SHA pins maintained, add `.github/dependabot.yml` (optional). Dependabot updates both the SHA and the trailing version comment:

```yaml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule: { interval: weekly }
  - package-ecosystem: docker
    directory: /
    schedule: { interval: weekly }
```

No other workflow dependency is deprecated. Ubuntu 24.04 runners and Python 3.13 are current. The only other aging pin is Trivy 0.69.3 (section 4), which is installed as a binary, not as an Action.

## 6. Improved reporting

The summary should open with the gate verdict and the triage, then the counts. It should keep every finding, grouped by CVE rather than by binary package.

**Suggested GitHub step summary.** The policy script can generate the top table and the CVE grouping from `trivy.json`. The "Debian 13" and "Reachable over HTTP?" columns come from the reviewed triage file. The remediation line is static history from the Milestone 4 review, labelled as such, because a CI run never scans the pre-fix image.

```markdown
## Release image security gate: PASSED

Image `sha256:ba433b82…` · release `1.0.4-gea8d2d2…-r44779970253-a1` · Debian 13.7 · Python 3.13.18
Trivy 0.69.3 · vulnerability DB updated 2026-11-20 13:11 UTC (3 h 56 min before scan)

| Check | Result |
|---|---|
| Blocking findings (fixable High/Critical, OS + Python) | **0** |
| Python package findings (17 packages) | 0 |
| Ignored / suppressed findings | none |
| Remaining High, no Debian 13 fix yet | 47 findings = **11 CVEs in 6 Debian source packages** |
| High CVEs triaged | 11 / 11 (docs/security-triage.md, recheck 2026-12-04) |
| Remediated during M4 review | 10 fixable (6 High, 3 Medium, 1 Low): Debian zlib1g update; pip and its vendored libraries removed |

### Remaining High findings by CVE
| CVE | Debian source (binaries) | Trivy status | Debian 13 | Reachable over HTTP? |
|---|---|---|---|---|
| CVE-2026-76642, -78408, -78409, -78410 | util-linux (9) | affected | no-dsa, minor | No: local; needs setuid mount or root |
| CVE-2025-69720 | ncurses (4) | affected | no-dsa, minor | No: `infocmp` CLI only |
| CVE-2026-119373, -119374, -119379 | sqlite3 (1) | affected | no-dsa, minor | No: app uses PostgreSQL |
| CVE-2026-16742 | systemd (2) | affected | no-dsa, minor | No: systemd-homed not installed |
| CVE-2026-54369 | acl (1) | affected | no DSA | No: needs a privileged caller |
| CVE-2026-9538 | perl (1) | fix_deferred | postponed, minor | No: Archive::Tar not installed |

Medium 109 · Low 67 · Unknown 9, none with a Debian 13 fix. Full list in artifact `image-security-<run>-<attempt>`.

**Limitations:** "unfixed" does not mean "not exploitable". Debian status is reviewed by hand. Results reflect the DB date above; the image is re-scanned before deployment.
```

The recheck date is illustrative. Set your own.

**Short explanation for your professor** (you can paste this into the report or say it in the video):

> The release gate blocks any High or Critical vulnerability that has a published fix, in either the operating system or the Python packages, and it passed with zero blocking findings. During Milestone 4 it caught 10 fixable issues. We fixed them by applying a Debian zlib security update and removing pip from the production image. Trivy still lists 47 High findings, but those are 11 CVEs counted once per Debian binary package. Debian's security team has classed all 11 as minor for Debian 13 and has not released updates yet; all are fixed upstream. Each needs local access or sits in code that isn't installed, and most also need root or a setuid binary. None is reachable through the web API. We keep them visible rather than suppressing them, re-scan before deployment, and run the container as a non-root user with privilege escalation disabled.

**Terms that may be unfamiliar:**

- **Fixable:** the distribution has shipped a patched package.
- **Unfixed:** no patched package exists for this release yet.
- **no-dsa:** Debian judged the issue too minor for an emergency security advisory; it will be fixed in a routine point release.

For the two-minute video, show the top table. "PASSED, 0 blocking, 47 = 11 CVEs, 11/11 triaged" answers the obvious question in one screen.

## 7. Recommended implementation plan

Six small changes must land before AWS. None of them touches payment logic, the schema or idempotency. Do each as its own commit so it can be reverted on its own.

**Must fix before AWS deployment**

- [ ] Bump all three upload-artifact steps to v7.0.2 (`cf430e03…`) and make the checkout/setup-python comments precise (section 5).
- [ ] Rewrite the summary: "no Debian 13 fix yet" instead of "no published fix", group by CVE, add a Trivy status column and a top table (section 6).
- [ ] Add `docs/security-triage.md` covering the 11 CVEs. Have the summary flag any High CVE it doesn't list as `NOT TRIAGED`, without blocking.
- [ ] Raise `retention-days` for the release and `image-security` artifacts to 90 so the evidence outlasts grading.
- [ ] Milestone 5: define the EC2 `docker run` with the smoke-test hardening (section 3, option 1) and test it locally first.
- [ ] Milestone 5: re-scan the exact artifact with a fresh database before deploying and before any rollback.

**Recommended improvements**

- [ ] Replace `--only-upgrade zlib1g` with `apt-get upgrade -y`.
- [ ] Strip setuid/setgid bits in the release stage.
- [ ] Block fixable Python-package findings at any severity in `trivy_policy.py`.
- [ ] Fail the gate when the vulnerability database's `UpdatedAt` is more than 48 h old.
- [ ] Pin `python:3.13-slim` by digest and add Dependabot for `github-actions` and `docker`.
- [ ] Generate a hash-locked requirements file (pip-tools or uv) and use it in tests, pip-audit and the image.
- [ ] Plan a reviewed Trivy upgrade (0.69.3 → current), with checksum and cosign verification.

**Optional enhancements**

- [ ] Purge `libsqlite3-0` and `liblastlog2-2` (−7 findings), after confirming the app never imports `sqlite3`/`dbm`/`shelve`. Present it honestly as attack-surface trimming.
- [ ] Try `dhi.io/python:3.13` on a separate branch and compare scans side by side.
- [ ] Move `httpx` to a test-only requirements file, and adjust `smoke --full-tests` to match.
- [ ] Add a `HEALTHCHECK`, add `secret` to the image scanners, and publish a CycloneDX SBOM artifact.
- [ ] Delete ensurepip's bundled pip wheel, after confirming it is present.

## 8. Testing and rollback plan

Prove each change with the existing gates plus a targeted check, one commit at a time. To roll back, revert that commit. The last green run's verified artifacts remain the known-good fallback.

1. **Freeze a baseline now.** Download run 44779970253's three artifacts before their 14-day retention ends. Record image `ba433b82…` and the counts 47 / 109 / 67 / 9.
2. **Order the commits:** upload-artifact bump → summary and triage file → retention → Dockerfile changes → policy changes. One concern per commit keeps any failure easy to locate.
3. **For each Dockerfile change, run locally:**
   - `release.py build --local`
   - `trivy_gate.sh`
   - `release.py smoke --full-tests --browser` (expect 79 tests with 0 skipped and 46 browser checks)
   - `release.py package` and `release.py verify`
4. **Targeted checks after the Dockerfile changes:**
   - setuid strip: `docker run --rm --entrypoint sh IMG -c 'find / -xdev -perm /6000 -type f'` prints nothing.
   - imports still work: `docker run --rm IMG python -c "import ssl, zlib, uuid, asyncpg, fastapi, uvicorn"`.
   - inventory diff: compare the package lists in the old and new `trivy.json` with `jq`. Only the intended packages should change.
   - sqlite purge (if chosen): run `apt-get -s purge libsqlite3-0 liblastlog2-2` first, grep `app/` for `sqlite3`/`dbm`/`shelve`, then expect `import sqlite3` to fail.
5. **Policy changes:** extend `test_trivy_gate.py` with fixture reports:
   - a fixable Medium Python finding blocks
   - a database with `UpdatedAt` 72 h old fails
   - an untriaged High CVE shows `NOT TRIAGED` but still passes
   - the existing scanner-failure and database-failure cases still fail
6. **Workflow change:** run actionlint and ShellCheck, then push to the feature branch. Expect:
   - all 5 jobs green
   - no Node 20 annotations
   - the same three artifact names
   - downloaded package passes `sha256sum --check --strict`, `docker load` and the manifest comparison
7. **Hardened runtime (Milestone 5):** start the release image locally with the exact EC2 flags against a disposable PostgreSQL. Run the health, version, checkout and idempotent-replay checks that `release.py` already performs.
8. **Acceptance criteria:**
   - 0 blocking findings and 11/11 High CVEs triaged
   - `/api/health` still returns exactly `{"status":"ok","database":"connected"}`
   - `/api/version` matches the manifest
   - schema files unchanged
   - Compose development build and its 79 tests still pass, since the development target is untouched
9. **Rollback:**
   - Code: `git revert <sha>` on the branch, then let CI rebuild.
   - Dockerfile: changes are confined to the `release` stage, so the Compose development workflow is unaffected.
   - Deployment (Milestone 5): redeploy the previous verified release ID from S3 after re-scanning it. Never rebuild for a rollback.

`apt-get upgrade` can change counts between builds whenever Debian publishes updates. That is expected; the gate and the triage file absorb it.

## Sources

- Debian security tracker entries linked per CVE in section 2 (checked 2026-11-20)
- [util-linux 2.42.3 release notes](https://mirrors.edge.kernel.org/pub/linux/utils/util-linux/v2.42/v2.42.3-ReleaseNotes)
- [Ubuntu priority note for CVE-2025-69720](https://ubuntu.com/security/CVE-2025-69720)
- [actions/upload-artifact releases](https://github.com/actions/upload-artifact/releases); tags, SHAs and `action.yml` runtimes verified by cloning the official repositories
- [Aqua Trivy advisory GHSA-69fq-xp46-6x23](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23)
- [Docker Hardened Images: Python guide](https://hub.docker.com/hardened-images/catalog/dhi/python/guides)
- Uploaded `trivy.json` from run 44779970253 (image `sha256:ba433b82…`)
