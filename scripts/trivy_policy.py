"""Fail closed on incomplete image scans; block fixable High/Critical findings."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re


def evaluate(report, image_id):
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
    lines = ["Trivy 0.69.3 — OS and application dependency vulnerabilities", f"Image: {image_id}",
             "Policy: block every fixable HIGH/CRITICAL; report all unfixed findings.", ""]
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
                lines.append(f"{'BLOCK' if blocks else 'REVIEW'} {severity} {finding['VulnerabilityID']} "
                             f"{finding['PkgName']} {finding['InstalledVersion']} -> {fixed or 'no published fix'} "
                             f"({item['Target']})")
    lines.extend(["", "Severity | Fixable | Unfixed", "--- | ---: | ---:"])
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"):
        lines.append(f"{severity} | {counts[(severity, 'fixable')]} | {counts[(severity, 'unfixed')]}")
    lines.append(f"\nBlocking findings: {blocking}. No exceptions or ignored vulnerabilities.")
    return blocking, "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("image_id")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    args.receipt.unlink(missing_ok=True)
    blocking, summary = evaluate(json.loads(args.report.read_text()), args.image_id)
    args.summary.write_text(summary)
    print(summary)
    if not blocking:
        args.receipt.write_text(json.dumps({"image_id": args.image_id, "policy": "fixable-high-critical", "trivy": "0.69.3"}) + "\n")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
