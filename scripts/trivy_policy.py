"""Fail closed on incomplete image scans; block fixable High/Critical findings."""

import argparse
from collections import Counter
from datetime import date
from html import escape
import json
from pathlib import Path
import re


TRIAGE_PATH = Path(__file__).resolve().parents[1] / "docs/security-triage.md"


def read_triage(path):
    """Only the explicit review index counts; incidental CVE mentions do not."""
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"<!-- trivy-triage:start -->\s*```json\s*(.*?)\s*```\s*<!-- trivy-triage:end -->", text, re.S)
    if len(blocks) != 1:
        raise ValueError("Expected one triage index")
    triage = json.loads(blocks[0])
    if (triage["schema_version"] != 1 or triage["distribution"] != "debian"
            or not re.fullmatch(r"[1-9][0-9]*", triage["release"])):
        raise ValueError("Unsupported triage scope")
    if date.fromisoformat(triage["review_due"]) < date.fromisoformat(triage["reviewed"]):
        raise ValueError("Invalid triage review dates")
    if not isinstance(triage["entries"], dict):
        raise ValueError("Invalid triage entries")
    for cve, source in triage["entries"].items():
        if (not re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", cve)
                or not isinstance(source, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", source)):
            raise ValueError("Invalid triage CVE/source entry")
    return triage


def cell(value):
    """Package/advisory strings must not break the Markdown table or inject HTML."""
    return escape(str(value), quote=False).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`").replace("\n", " ")


def source_package(item, finding):
    if item.get("Type") != "debian":
        return finding["PkgName"]
    matches = [package for package in item["Packages"] if package["Name"] == finding["PkgName"]
               and (not finding.get("PkgID") or package.get("ID") == finding["PkgID"])]
    sources = {package.get("SrcName") for package in matches}
    if len(sources) == 1 and None not in sources and "" not in sources:
        return sources.pop()
    return "source unavailable: " + finding["PkgName"]


def fix_description(finding, debian):
    fixed = finding.get("FixedVersion", "").strip()
    if fixed:
        return "patched version: " + fixed
    if debian and finding.get("Status") in {"affected", "fix_deferred", "will_not_fix"}:
        return "no patched package available for this Debian release"
    if debian:
        return "no patched version reported for this Debian release; availability requires review"
    return "no patched version reported by Trivy; upstream status requires review"


def evaluate(report, image_id, triage=None):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Invalid expected image ID")
    if report.get("SchemaVersion") != 2 or report.get("ArtifactType") != "container_image":
        raise ValueError("Expected a complete Trivy container image report")
    metadata = report["Metadata"]
    if metadata["ImageID"] != image_id:
        raise ValueError("Trivy scanned a different image")
    if not metadata.get("OS", {}).get("Family") or metadata["OS"].get("EOSL"):
        raise ValueError("Missing or unsupported operating-system inventory")
    results = report["Results"]
    if not isinstance(results, list):
        raise ValueError("Missing scan results")
    os_results = [item for item in results if item.get("Class") == "os-pkgs"]
    python_results = [item for item in results if item.get("Type") == "python-pkg"]
    if not os_results or not all(item.get("Packages") for item in os_results) or not python_results:
        raise ValueError("OS/Python package coverage is incomplete")
    packages = {package["Name"].lower() for item in python_results for package in item.get("Packages", [])}
    if not {"fastapi", "uvicorn", "asyncpg", "httpx"} <= packages:
        raise ValueError("Application dependency inventory is incomplete")
    counts = Counter()
    blocking = 0
    groups = {}
    for item in results:
        vulnerabilities = item.get("Vulnerabilities")
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list):
            raise ValueError("Invalid vulnerability list")
        for finding in vulnerabilities:
            severity = finding["Severity"]
            if severity not in {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                raise ValueError("Unknown severity classification")
            fixed = finding.get("FixedVersion", "")
            if not isinstance(fixed, str):
                raise ValueError("Invalid fix classification")
            fixable = bool(fixed.strip())
            status = "fixable" if fixable else "unfixed"
            counts[(severity, status)] += 1
            blocks = severity in {"HIGH", "CRITICAL"} and fixable
            blocking += int(blocks)
            if severity in {"HIGH", "CRITICAL"}:
                source = source_package(item, finding)
                key = (severity, finding["VulnerabilityID"], item["Type"], source)
                groups.setdefault(key, []).append(finding)

    def documented(key):
        _severity, cve, ecosystem, source = key
        return bool(triage and ecosystem == "debian" and metadata["OS"]["Family"] == "debian"
                    and metadata["OS"].get("Name", "").split(".")[0] == triage["release"]
                    and triage["entries"].get(cve) == source)

    high_groups = [key for key in groups if key[0] == "HIGH"]
    high_ids = {key[1] for key in high_groups}
    high_cves = {advisory for advisory in high_ids if re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", advisory)}
    high_sources = {key[3] for key in high_groups if key[2] == "debian" and not key[3].startswith("source unavailable:")}
    untriaged = {key[1] for key in high_groups if not documented(key)}
    lines = [f"## Release image security gate: {'FAIL' if blocking else 'PASS'}", "",
             f"**Blocking findings: {blocking}** (fixable HIGH/CRITICAL, OS + application packages).", "",
             f"Image: `{image_id}` · OS: {cell(metadata['OS']['Family'])} {cell(metadata['OS'].get('Name', 'unknown'))}",
             "Trivy 0.69.3. Policy unchanged; no exceptions or ignored vulnerabilities.", "",
             f"High findings: **{sum(counts[('HIGH', status)] for status in ('fixable', 'unfixed'))} package-level findings** "
             f"correspond to **{len(high_cves)} distinct CVEs** across **{len(high_sources)} Debian source packages**."]
    if high_ids - high_cves:
        lines.append(f"Also present: {len(high_ids - high_cves)} distinct non-CVE High advisory IDs (included in package counts).")
    lines.extend(["A CVE can affect several binary packages built from one Debian source; grouping does not suppress findings.",
                  f"High CVEs documented for this release/source: **{len(high_cves - untriaged)}/{len(high_cves)}**."])
    if untriaged:
        lines.append("**NOT TRIAGED:** " + ", ".join(cell(cve) for cve in sorted(untriaged)) + ". Manual review required.")
    if triage:
        lines.append(f"Review index: `docs/security-triage.md` — reviewed {triage['reviewed']}; review due {triage['review_due']}.")
    else:
        lines.append("Triage index unavailable or not supplied; findings are NOT TRIAGED until documented.")
    lines.extend(["DOCUMENTED means a review exists, not that a finding is safe, suppressed or approved for deployment.",
                  "", "### Complete severity counts", "", "Severity | Fixable | Unfixed", "--- | ---: | ---:"])
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"):
        lines.append(f"{severity} | {counts[(severity, 'fixable')]} | {counts[(severity, 'unfixed')]}")
    lines.extend(["", "### High/Critical findings by advisory and source", "",
                  "Decision | Advisory | Debian source / other package | Findings | Affected binaries/packages (installed) | Trivy status | Patch availability in scan DB | Triage",
                  "--- | --- | --- | ---: | --- | --- | --- | ---"])
    for key, findings in sorted(groups.items()):
        severity, advisory, ecosystem, source = key
        decision = "BLOCK" if any(finding.get("FixedVersion", "").strip() for finding in findings) else "REVIEW"
        names = sorted({f"{finding['PkgName']} {finding['InstalledVersion']}" for finding in findings})
        statuses = sorted({finding.get("Status", "unknown") for finding in findings})
        fixes = sorted({fix_description(finding, ecosystem == "debian") for finding in findings})
        values = (f"{decision} {severity}", advisory, f"{source} ({ecosystem})", len(findings),
                  ", ".join(names), ", ".join(statuses), "; ".join(fixes), "DOCUMENTED" if documented(key) else "NOT TRIAGED")
        lines.append(" | ".join(cell(value) for value in values))
    if not groups:
        lines.append("No High/Critical findings.")
    lines.extend(["", "Fix availability is distribution-specific and reflects the scan database; upstream fixes may already exist.",
                  "Missing/unknown Trivy status is not proof that no patch exists. Debian triage and exposure assessments are dated manual reviews.",
                  "Unfixed and NOT TRIAGED findings remain visible without changing the fixable High/Critical blocking policy.",
                  "Full package-level findings, severity sources and inventory remain in `trivy.json` in the security-evidence artifact."])
    return blocking, "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("image_id")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--triage", type=Path, default=TRIAGE_PATH)
    args = parser.parse_args()
    args.receipt.unlink(missing_ok=True)
    args.summary.write_text("## Release image security gate: FAIL\n\n"
                            "**Blocking findings: unknown** — policy evaluation did not complete. No release is approved.\n")
    try:
        triage = read_triage(args.triage)
    except (OSError, ValueError, KeyError, TypeError):
        triage = None  # Missing/malformed documentation cannot silently mark a CVE reviewed.
    blocking, summary = evaluate(json.loads(args.report.read_text()), args.image_id, triage)
    args.summary.write_text(summary)
    print(summary)
    if not blocking:
        args.receipt.write_text(json.dumps({"image_id": args.image_id, "policy": "fixable-high-critical", "trivy": "0.69.3"}) + "\n")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
