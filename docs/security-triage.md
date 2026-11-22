# Release image security triage

Reviewed **2026-11-20 (Asia/Karachi)**; next review **2026-12-04**, or sooner when
an image, dependency, runtime configuration, advisory or Debian package changes.
Review owner: the AsaanPay repository maintainer. This is documentation, not an
ignore list, VEX statement, risk acceptance or deployment approval.

## Evidence and limits

The independent audit was read in full before implementation. Its claims were
checked against the official sources linked below and the actual security artifact
from [CI run 44779970253](https://github.com/Batsy1857/AsaanPay/actions/runs/44779970253),
commit `ea8d2d2edf91c7fb190b47a24619c36de9e44411`. The downloaded artifact matched
GitHub's SHA-256 `a89f9b64a8a2d1f671f608572c9a5a5c24103335c7ec468a0663dcec133abaef`.

The reviewed image is
`sha256:ba433b8236d900c06edd927f6d6336f934fc22cc4312c79f7db81675551eded0`,
Debian 13.7, scanned at `2026-11-20T17:07:59Z` with Trivy 0.69.3 and database
`UpdatedAt=2026-11-20T13:11:33Z`. It contains 87 Debian and 17 Python packages.
There are **47 High package-level findings corresponding to 11 distinct CVEs,
18 binary packages and six Debian source packages**. All are unfixed in this
Debian release according to this scan. The 36 util-linux findings are four CVEs
reported against nine binaries each. Counts in the CI summary are computed from
each supplied scan; these numbers describe only this reviewed evidence.

**Verified facts** below mean observed in that JSON, current repository source,
official advisory/tracker, or the explicitly identified local image inspection.
**Inference** means an exposure assessment, not proof of non-exploitability.
A CVSS local-access vector does not prove a library is unreachable from a network
application: untrusted HTTP input can reach a library through application code.
No exploit testing or complete transitive call-graph analysis was performed.

Debian still marks these source packages vulnerable in trixie. Nine CVEs have a
`no-dsa` minor-issue note; acl has a `no-dsa` note describing a planned point-release
update; perl is `postponed` pending upstream regression resolution. Those decisions
are not a blanket declaration that every package is safe. A `no-dsa` designation
does not guarantee a particular fix date. Upstream or unstable fixes do not imply
that a patched Debian 13 package is available. Trivy's High severity is preserved.

## Existing controls and application observations

Verified in the unchanged repository at the commit above:

- `Dockerfile` selects UID/GID `10001:10001` for the release. It runs a single
  Uvicorn worker. Operators can override the image user; future runtime settings
  must therefore be checked separately.
- `scripts/release.py` applies `--read-only`, a writable tmpfs, `--cap-drop ALL`
  and `no-new-privileges:true` in smoke tests. These are verified **test controls**,
  not a claim that an EC2 deployment already exists or has these protections.
- `app/main.py`, `app/checkouts.py` and `app/storage.py` use asyncpg/PostgreSQL.
  Inspection and `rg -n 'sqlite|dbm|shelve|subprocess|os\.system|ctypes|tarfile' app`
  found no such use in application code. The reviewed handlers accept structured
  checkout input; they provide no SQL, changeset, terminal-definition, archive
  extraction, mount, ACL or process-execution endpoint.

Read-only probes of the retained **local M4 candidate**,
`sha256:ed23dacb7b955f1a48b7449b736ee5612b28cd1e65769630f7ba6dbcf4cc511c`,
confirmed UID 10001; `infocmp`, `mount`, `nsenter` and Python `sqlite3` are present;
`getfacl`, `setfacl` and `systemd-homed` are not on PATH; no homed executable was
found under `/usr/lib/systemd`; no `Archive/Tar.pm` was found under
`/usr/share/perl/*`; `/etc/fstab` contains only the base-image comment.
These observations are limited to that local image and the checked paths, not
proof about every file or future release. The CI inventory reports only
`libsystemd0`/`libudev1` from systemd, and `perl-base` from perl. Verify exact
component presence again on any image selected for deployment.

**Inference:** no direct route from the currently reviewed checkout handlers to
these vulnerable operations was identified. That is narrower than the audit's
categorical claim that none is HTTP-reachable. An application compromise, new
feature, administrative command or changed container privileges can alter exposure.
Non-root execution and the smoke controls reduce opportunities for escalation;
they are not a universal mitigation for memory corruption or denial of service.

## util-linux: four High CVEs, 36 package-level findings

Affected source version: `2.41.5-0+deb13u1`. Flagged binaries: `bsdutils`,
`libblkid1`, `liblastlog2-2`, `libmount1`, `libsmartcols1`, `libuuid1`, `login`,
`mount`, `util-linux` (binary versions/epochs are retained in the JSON and summary).
All four have Trivy status `affected` and Debian trixie `no-dsa` / minor issue.

| CVE | Verified trigger and upstream status | Debian fixed-version evidence |
| --- | --- | --- |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | Failed mount helpers can still trigger privileged post-hooks. Requires a local mounting path with privileges, suitable fstab configuration and helper behavior. [Upstream advisory](https://github.com/util-linux/util-linux/security/advisories/GHSA-m25x-3hj9-m26f) lists 2.41.6 / 2.42.3 patched. | Unstable fixed from `2.42.3-1`; no patched trixie package in the reviewed tracker. |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | A privileged operator using `nsenter --join-cgroup` against an attacker-controlled target can pass root-opened cgroup authority to that target. [Upstream advisory](https://github.com/util-linux/util-linux/security/advisories/GHSA-55fx-f4gg-cfhj) lists 2.41.6 / 2.42.3. | Debian records an additional fixed commit tagged v2.42.4 and unstable `2.42.4-1`. Preserve this distinction instead of treating the upstream version list as a Debian fix. |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | `X-mount.subdir` can follow intermediate symlinks outside the intended tree. [Upstream advisory](https://github.com/util-linux/util-linux/security/advisories/GHSA-8f2p-47x3-43mv) describes the restricted-user path on Linux >=6.15, fstab authorization, affected versions >=2.42, and patched 2.41.6 / 2.42.3. | Debian nevertheless marks its trixie 2.41.5 package vulnerable; unstable fixed from `2.42.3-1`. Backport/version applicability requires maintainer review; no local exception is inferred. |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | A local attacker controlling a bind-mount source or ancestor can redirect privileged ownership/mode post-hooks through a suitable fstab entry. [Upstream advisory](https://github.com/util-linux/util-linux/security/advisories/GHSA-rh77-686x-2f2m) lists 2.41.6 / 2.42.3 patched. | Unstable fixed from `2.42.3-1`; no patched trixie package in the reviewed tracker. |

**Inference and controls:** the application has no mount/nsenter execution path.
The smoke test's non-root user, dropped capabilities and no-new-privileges reduce
mount/setuid escalation exposure. They do not protect a root host operator who
independently invokes vulnerable nsenter against an untrusted process. Keep these
operations out of application flows; recheck fstab, host kernel, runtime privileges
and Debian package updates before deployment. No setuid stripping or package removal
is implemented in M4B.

## ncurses: CVE-2025-69720

Flagged binaries: `libncursesw6`, `libtinfo6`, `ncurses-base`, `ncurses-bin`, all
`6.5+20250216-2`. Four findings, one CVE. Trivy: `affected`.

**Verified:** [Debian tracker](https://security-tracker.debian.org/tracker/CVE-2025-69720)
records an `infocmp` stack overflow, trixie `no-dsa` / minor issue, and unstable
fixed from `6.6+20251231-1`. Its notes still request checking upstream status.
The [upstream changelog](https://invisible-island.net/ncurses/NEWS.html#index-t20251213)
records an `infocmp -i` limit check on 2025-12-13; this supports the named upstream
fix rather than a claim that all uses of ncurses libraries are affected.

**Inference and controls:** exposure needs someone to process attacker-influenced
terminal data with the affected CLI path. `infocmp` exists in the local image,
but no application invocation was identified. Avoid running it on untrusted
terminfo; retain non-root/read-only smoke controls and recheck Debian updates.
CLI presence remains visible, and this review is not a suppression.

## sqlite3: three High CVEs, three package-level findings

Affected binary: `libsqlite3-0` at `3.46.1-7+deb13u2`. All three have Trivy status
`affected`; Debian trixie `no-dsa` / minor issue; unstable fixed from `3.54.1-1`.
[SQLite's release history](https://sqlite.org/changes.html) and the commits linked
by each tracker identify fixes in the 3.54.0 release line.

| CVE | Verified exposure conditions |
| --- | --- |
| [CVE-2026-119373](https://security-tracker.debian.org/tracker/CVE-2026-119373) | FTS5 must be built/enabled; malicious rowids/data in a detail=none table and a prefix MATCH query reach an undersized merge buffer. |
| [CVE-2026-119374](https://security-tracker.debian.org/tracker/CVE-2026-119374) | A crafted database with corrupted FTS5 index data must be processed with secure-delete enabled. |
| [CVE-2026-119379](https://security-tracker.debian.org/tracker/CVE-2026-119379) | SQLite's session extension must process an attacker-supplied changeset through the affected apply path. |

**Inference and controls:** the application currently uses PostgreSQL and exposes
none of those input paths; Python's optional sqlite3 module is nevertheless present
in the local image. This is reduced observed exposure, not removal of vulnerable
code. Do not add untrusted SQLite/database-file/changeset ingestion without a new
review. Non-root and read-only execution limit some consequences but do not prevent
heap corruption in a process using SQLite. FTS5/session build options and indirect
third-party use were not exhaustively audited. No SQLite package is removed.

## systemd: CVE-2026-16742

Flagged binaries: `libsystemd0`, `libudev1`, both `257.13-1~deb13u1`; two findings.
Trivy: `affected`.

**Verified:** [Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-16742)
records trixie `no-dsa` / minor issue and unstable fixed from `261.2-1`.
The [upstream advisory](https://github.com/systemd/systemd/security/advisories/GHSA-jm29-p7hh-vjhv)
describes a missing signature check in systemd-homed's authentication path, requiring
a logged-in homed-managed user. Patched upstream lines include 258.10, 259.8, 260.4,
261.2 and 262. The source-package finding alone does not establish that homed is installed.

**Inference and controls:** the CI inventory and local observations above support
low apparent exposure to this particular homed path. The application starts
Uvicorn, not systemd-homed. Verify the target image still lacks the service and
homed-managed accounts; retain non-root execution and recheck package updates.
Do not convert that inference to an ignore or a universal claim about systemd libraries.

## acl: CVE-2026-54369

Affected binary: `libacl1` at `2.3.2-2+b1`; one finding. Trivy: `affected`.

**Verified:** the [Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-54369)
marks trixie `no-dsa` with a planned point-release update, not the audit's generic
minor-issue wording. Unstable is fixed from `2.4.0-1`. The
[maintainer announcement](https://www.openwall.com/lists/oss-security/2026/06/29/1)
identifies fixes in acl 2.4.0. Exploitation requires a privileged caller using
pathname-based ACL operations while an attacker controls a path component.
The fix introduces new interfaces and has compatibility considerations; do not
mix unstable packages into this release as a reporting change.

**Inference and controls:** no application ACL API or privileged file-operation
path was identified. Absence of `getfacl`/`setfacl` does **not** prove libacl is
unused by other programs. Non-root, read-only execution and avoiding privileged
operations on untrusted paths reduce exposure. Recheck callers and the Debian
point-release status; libacl remains installed and reported.

## perl: CVE-2026-9538

Affected binary reported at source-package level: `perl-base` at
`5.40.1-6+deb13u1`; one finding. Trivy: `fix_deferred`.

**Verified:** [Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-9538)
describes unbounded allocation when Archive::Tar processes a crafted tar size.
It links the upstream 3.10 fix and records unstable fixed from `5.42.3-1`.
Trixie is `postponed` with a note about waiting for upstream regression resolution.
That is a deferred distribution fix, not absence of an upstream patch.

**Inference and controls:** the CI inventory lists perl-base only, no application
Perl/archive-processing path was found, and local checked paths lacked Archive::Tar.
Confirm absence with module loading/file inventory on the actual deployment image.
Do not add an untrusted Perl tar-processing flow without review. Non-root/read-only
execution does not prevent memory exhaustion if the vulnerable module is used;
future resource limits are a separate deployment task, not an existing control.

## Review index used by CI

Only this explicit index establishes that a CVE has a documented review. A matching
CVE **and source package and Debian major release** are required. Merely mentioning
a CVE in prose does not mark it reviewed. Missing or malformed documentation makes
findings `NOT TRIAGED`; a new/mismatched High advisory stays visible and requires
manual review. `DOCUMENTED` means this dated assessment exists, never "safe".
Update the analysis and review dates before adding an index entry. The gate still
blocks only fixable High/Critical findings, plus its existing scanner/inventory
failure conditions; triage does not influence its approval receipt.

<!-- trivy-triage:start -->
```json
{
  "schema_version": 1,
  "distribution": "debian",
  "release": "13",
  "reviewed": "2026-11-20",
  "review_due": "2026-12-04",
  "entries": {
    "CVE-2025-69720": "ncurses",
    "CVE-2026-119373": "sqlite3",
    "CVE-2026-119374": "sqlite3",
    "CVE-2026-119379": "sqlite3",
    "CVE-2026-16742": "systemd",
    "CVE-2026-54369": "acl",
    "CVE-2026-76642": "util-linux",
    "CVE-2026-78408": "util-linux",
    "CVE-2026-78409": "util-linux",
    "CVE-2026-78410": "util-linux",
    "CVE-2026-9538": "perl"
  }
}
```
<!-- trivy-triage:end -->

Recheck the individual Debian/upstream links and a fresh image scan by the review
date. Deployment hardening, deployment/rollback rescans, scanner upgrades, broader
APT upgrades, base changes, extra package removals and stricter vulnerability
thresholds remain outside M4B. No future control is credited as already implemented.
