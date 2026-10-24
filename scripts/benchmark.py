#!/usr/bin/env python3
"""Benchmark the existing local checkout API; standard library only, no retries.

See benchmarks/README.md for measurement boundaries, validation, and reproduction.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {"lightweight": (0, 0), "balanced": (250, 250), "asymmetric": (100, 400)}
MODES = ("sequential", "concurrent")
# 1 ns in ms: tolerance for subtraction/conversion, not scheduling leeway.
EPSILON_MS = 0.000001
# Explicit oracle independent of application code. Each tuple specifies the risk
# expectation; preset/state expectations are defined here, never read from mode.
CASES = []
for label, amount, country, risk in (
    ("below", "49999.99", "PK", "CLEAR"),
    ("threshold", "50000.00", "PK", "VERIFY"),
    ("above", "50000.01", "PK", "VERIFY"),
    ("foreign", "7500.00", "US", "VERIFY"),
    ("both", "50000.00", "US", "VERIFY"),
):
    for preset in ("approved", "declined"):
        CASES.append({
            "case_id": f"{label}_{preset}",
            "input": {"amount_pkr": amount, "billing_country": country, "card_preset": preset},
            "issuer": preset.upper(), "risk": risk,
            "decision": "DECLINED" if preset == "declined" else (
                "SUCCEEDED" if risk == "CLEAR" else "VERIFICATION_REQUIRED"),
        })
METRICS = ("http_ms", "orchestration_ms", "issuer_ms", "risk_ms", "overlap_ms")
DECISION_FIELDS = ("state", "decision", "issuer_result", "risk_result", "decision_reason", "rule_set_version")
CSV_FIELDS = [
    "sequence", "phase", "pair_id", "case_id", "profile", "mode", "order_in_pair",
    "started_utc", "idempotency_key", "attempt_id", "amount_pkr", "card_preset", "billing_country",
    "expected_decision", "http_status", "state", "decision", "issuer_result", "risk_result",
    "decision_reason", "rule_set_version", "error_reason", "http_elapsed_ns", "http_ms",
    "orchestration_ms", "decision_ms", "issuer_ms", "risk_ms", "overlap_ms", "calculated_overlap_ms",
    "issuer_start_ms", "issuer_end_ms", "issuer_artificial_delay_ms", "issuer_status", "issuer_error_type",
    "risk_start_ms", "risk_end_ms", "risk_artificial_delay_ms", "risk_status", "risk_error_type",
    "decision_correct", "pair_match", "valid", "errors_json", "response_json",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def schedule(warmups, trials, prefix):
    """Adjacent pairs; rotate profiles and counterbalance the first mode."""
    sequence = 0
    profiles = list(PROFILES)
    for phase, count in (("warmup", warmups), ("measured", trials)):
        for index in range(count):
            case = CASES[index % len(CASES)]
            rotated = profiles[index % 3:] + profiles[:index % 3]
            for profile in rotated:
                # Alternate across rounds, profiles, and repeated corpus cycles.
                reverse = (index + index // len(CASES) + profiles.index(profile)) % 2
                modes = MODES[::-1] if reverse else MODES
                pair_id = f"{phase}-{profile}-{index + 1:04d}"
                for order, mode in enumerate(modes, 1):
                    sequence += 1
                    yield dict(sequence=sequence, phase=phase, pair_id=pair_id,
                               case_id=case["case_id"], profile=profile, mode=mode,
                               order_in_pair=order, idempotency_key=f"{prefix}{pair_id}-{mode}",
                               expected_decision=case["decision"], **case["input"])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_trial(opener, url, timeout, trial):
    """Time POST through complete body receipt; parse/validate afterwards."""
    payload = {key: trial[key] for key in ("amount_pkr", "card_preset", "billing_country")}
    payload.update(execution_mode=trial["mode"], workload_profile=trial["profile"])
    request = Request(url + "/api/checkouts", data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Idempotency-Key": trial["idempotency_key"],
        "Connection": "close",
    })
    row = {**trial, "started_utc": utc_now(), "http_status": None, "errors": []}
    raw = b""
    start = time.perf_counter_ns()
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error  # Retain non-2xx status/body as an actual observation.
        with response:
            row["http_status"] = response.status
            raw = response.read()
    except (OSError, URLError, http.client.HTTPException) as error:
        row["errors"].append(f"transport:{type(error).__name__}:{error}")
    except KeyboardInterrupt:
        row["errors"].append("interrupted_during_request")
    finally:
        row["http_elapsed_ns"] = time.perf_counter_ns() - start
        row["http_ms"] = row["http_elapsed_ns"] / 1_000_000
    row["response_json"] = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        body = None
        row["errors"].append("invalid_or_missing_json")
    # Stop if server work might still be outstanding. Never retry or poll in the
    # timed experiment, and never continue a new checkout after uncertain work.
    uncertain = bool(row["errors"]) or row["http_status"] == 202 or row["http_status"] >= 500
    if not isinstance(body, dict) or body.get("terminal") is not True:
        uncertain = True
    return row, body, payload, uncertain


def validate_trial(row, body, payload, seen_ids):
    """Validate actual response fields and recompute the trace interval algebra."""
    errors = row["errors"]
    if row["http_status"] != 201:
        errors.append(f"unexpected_http_status:{row['http_status']} (fresh attempts require 201)")
    row["decision_correct"] = False
    if not isinstance(body, dict):
        errors.append("missing_resource")
        return
    for field in (*DECISION_FIELDS, "error_reason"):
        row[field] = body.get(field)
    row["attempt_id"] = body.get("id") if isinstance(body.get("id"), str) else None
    try:
        attempt_id = str(UUID(body.get("id")))
    except (ValueError, TypeError, AttributeError):
        errors.append("invalid_attempt_id")
    else:
        if attempt_id in seen_ids:
            errors.append("reused_attempt_id")
        seen_ids.add(attempt_id)
    case = next(case for case in CASES if case["case_id"] == row["case_id"])
    row["decision_correct"] = (
        body.get("terminal") is True and body.get("state") == case["decision"]
        and body.get("decision") == case["decision"] and body.get("issuer_result") == case["issuer"]
        and body.get("risk_result") == case["risk"] and body.get("error_reason") is None
    )
    if not row["decision_correct"]:
        errors.append("unexpected_business_or_technical_outcome")
    if body.get("request") != payload:
        errors.append("request_echo_mismatch")
    if body.get("rule_set_version") != "sandbox_v1":
        errors.append("unexpected_rule_set")
    if not isinstance(body.get("decision_reason"), str) or not body["decision_reason"].strip():
        errors.append("missing_decision_reason")
    trace = body.get("timing_trace")
    if not isinstance(trace, dict):
        errors.append("missing_timing_trace")
        return
    expected_trace = dict(execution_mode=row["mode"], workload_profile=row["profile"],
                          measurement="synthetic_orchestration_not_payment_network_latency")
    if any(trace.get(key) != value for key, value in expected_trace.items()) or trace.get("artificial_delays") is not True:
        errors.append("trace_contract_mismatch")
    if not finite_number(trace.get("branch_timeout_ms")) or trace["branch_timeout_ms"] != 2000:
        errors.append("unexpected_branch_timeout")
    numbers_valid = True
    for key in ("orchestration_ms", "decision_ms", "overlap_ms"):
        row[key] = trace.get(key)
        if not finite_number(row[key]):
            errors.append(f"invalid_number:{key}")
            numbers_valid = False
    branches = trace.get("branches")
    if not isinstance(branches, dict):
        errors.append("missing_branches")
        return
    for name, delay in zip(("issuer", "risk"), PROFILES[row["profile"]]):
        branch = branches.get(name)
        if not isinstance(branch, dict):
            errors.append(f"missing_branch:{name}")
            numbers_valid = False
            continue
        for key in ("start_ms", "end_ms", "duration_ms", "artificial_delay_ms", "status", "error_type"):
            target = name + "_ms" if key == "duration_ms" else name + "_" + key
            row[target] = branch.get(key)
        if any(not finite_number(branch.get(key)) for key in ("start_ms", "end_ms", "duration_ms")):
            errors.append(f"invalid_branch_numbers:{name}")
            numbers_valid = False
        if not finite_number(branch.get("artificial_delay_ms")) or branch["artificial_delay_ms"] != delay:
            errors.append(f"unexpected_artificial_delay:{name}")
        if branch.get("status") != "completed" or "error_type" not in branch or branch["error_type"] is not None:
            errors.append(f"branch_failure:{name}:{branch.get('status')}:{branch.get('error_type')}")
    if not numbers_valid:
        return
    issuer, risk = branches["issuer"], branches["risk"]
    for name, branch in (("issuer", issuer), ("risk", risk)):
        if branch["end_ms"] + EPSILON_MS < branch["start_ms"]:
            errors.append(f"reversed_interval:{name}")
        if abs(branch["duration_ms"] - (branch["end_ms"] - branch["start_ms"])) > EPSILON_MS:
            errors.append(f"duration_mismatch:{name}")
        if branch["end_ms"] > trace["decision_ms"] + EPSILON_MS:
            errors.append(f"branch_after_decision:{name}")
    overlap = max(0, min(issuer["end_ms"], risk["end_ms"]) - max(issuer["start_ms"], risk["start_ms"]))
    row["calculated_overlap_ms"] = overlap
    if abs(overlap - trace["overlap_ms"]) > EPSILON_MS:
        errors.append("overlap_mismatch")
    if trace["decision_ms"] > trace["orchestration_ms"] + EPSILON_MS:
        errors.append("decision_after_orchestration")
    if trace["orchestration_ms"] > row["http_ms"] + EPSILON_MS:
        errors.append("orchestration_exceeds_http")
    if row["mode"] == "sequential":
        if overlap > EPSILON_MS or trace["overlap_ms"] > EPSILON_MS or risk["start_ms"] + EPSILON_MS < issuer["end_ms"]:
            errors.append("sequential_overlap_or_order")
    elif row["profile"] != "lightweight" and overlap <= EPSILON_MS:
        errors.append("concurrent_waits_did_not_overlap")


def check_pairs(rows):
    pairs = defaultdict(list)
    for row in rows:
        pairs[row["pair_id"]].append(row)
    for pair in pairs.values():
        match = len(pair) == 2 and all(row["decision_correct"] for row in pair)
        if match:
            match = all(pair[0].get(key) == pair[1].get(key) for key in DECISION_FIELDS)
        for row in pair:
            row["pair_match"] = match
            if not match:
                row["errors"].append("pair_missing_or_decision_mismatch")
            row["valid"] = not row["errors"]


def describe(values):
    """Nearest-rank P95 and sample standard deviation (n-1)."""
    values = sorted(values)
    if not values:
        return {key: None for key in ("mean", "median", "p95", "min", "max", "stdev")} | {"n": 0}
    return dict(n=len(values), mean=statistics.mean(values), median=statistics.median(values),
                p95=values[math.ceil(.95 * len(values)) - 1], min=values[0], max=values[-1],
                stdev=statistics.stdev(values) if len(values) > 1 else None)


def summarize(rows, metadata):
    measured = [row for row in rows if row["phase"] == "measured"]
    warmups = [row for row in rows if row["phase"] == "warmup"]
    groups = []
    for profile in PROFILES:
        for mode in MODES:
            group = [row for row in measured if (row["profile"], row["mode"]) == (profile, mode)]
            valid = [row for row in group if row["valid"]]
            groups.append(dict(
                profile=profile, mode=mode, attempted=len(group), valid=len(valid),
                error_count=len(group) - len(valid), error_rate=(len(group) - len(valid)) / len(group) if group else None,
                decision_correct_count=sum(row["decision_correct"] for row in group),
                decision_correct_rate=sum(row["decision_correct"] for row in group) / len(group) if group else None,
                matching_pairs=sum(row["pair_match"] for row in group),
                decisions=dict(Counter(str(row.get("decision")) for row in group)),
                cases=dict(Counter(row["case_id"] for row in group)),
                first_in_pair=sum(row["order_in_pair"] == 1 for row in group),
                metrics={metric: describe([row[metric] for row in valid]) for metric in METRICS},
                # Diagnostic only: includes failed-request durations; not silently dropped.
                all_attempt_http_ms=describe([row["http_ms"] for row in group]),
            ))
    comparisons = []
    for profile in PROFILES:
        seq, con = [group for group in groups if group["profile"] == profile]
        eligible = (profile != "lightweight" and not metadata.get("abort_reason")
                    and all(group["attempted"] == metadata["trials_per_mode_profile"] and group["error_count"] == 0
                            for group in (seq, con)) and not any(not row["valid"] for row in warmups))
        reductions = {}
        for metric in ("http_ms", "orchestration_ms"):
            a, b = seq["metrics"][metric]["median"], con["metrics"][metric]["median"]
            reductions[metric] = (a - b) / a * 100 if eligible and a and b is not None else None
        comparisons.append(dict(profile=profile, median_reduction_percent=reductions,
                                interpretation="Lightweight differences are overhead/noise; no speed improvement claimed."
                                if profile == "lightweight" else "Descriptive reduction only; synthetic waits, no significance test."
                                if eligible else "Unavailable: failures or incomplete experiment."))
    failed = [row for row in rows if not row["valid"]]
    complete = len(measured) == metadata["trials_per_mode_profile"] * 6 and len(warmups) == metadata["warmups_per_mode_profile"] * 6
    pairs = {row["pair_id"] for row in measured if row["pair_match"]}
    return dict(status="PASS" if complete and not failed and not metadata.get("abort_reason") else "FAIL",
                complete=complete, measured_attempts=len(measured), warmup_attempts=len(warmups),
                measured_errors=sum(not row["valid"] for row in measured),
                warmup_errors=sum(not row["valid"] for row in warmups),
                correct_measured_decisions=sum(row["decision_correct"] for row in measured),
                matching_measured_pairs=len(pairs), expected_measured_pairs=metadata["trials_per_mode_profile"] * 3,
                unique_keys=len({row["idempotency_key"] for row in rows}),
                unique_attempt_ids=len({row["attempt_id"] for row in rows if row.get("attempt_id")}),
                groups=groups, comparisons=comparisons,
                failures=[dict(sequence=row["sequence"], phase=row["phase"], key=row["idempotency_key"], errors=row["errors"]) for row in failed])


def report(summary, metadata):
    def fmt(number):
        return "N/A" if number is None else f"{number:.3f}"

    lines = ["# AsaanPay Milestone 6: actual local measurements", "",
             f"Result: **{summary['status']}**. Started {metadata['started_utc']}; finished {metadata['finished_utc']}.", "",
             f"Measured: {summary['measured_attempts']}; warm-ups (excluded): {summary['warmup_attempts']}. "
             f"Measured errors: {summary['measured_errors']}; warm-up errors: {summary['warmup_errors']}.",
             f"Expected decisions: {summary['correct_measured_decisions']}/{summary['measured_attempts']}; "
             f"matching measured mode pairs: {summary['matching_measured_pairs']}/{summary['expected_measured_pairs']}.",
             f"Unique submitted keys: {summary['unique_keys']}; unique returned attempt IDs: {summary['unique_attempt_ids']}.", "",
             "## Measured latency (milliseconds)", "",
             "Valid measured samples only in this table; warm-ups excluded. Failed samples remain in trials.csv, "
             "failures below, and summary.json. All-attempt HTTP diagnostics are also in summary.json.", "",
             "| Profile | Mode | Valid / attempts | Errors | HTTP median | HTTP P95 | Orchestration median | Orchestration P95 | Issuer median | Risk median | Overlap median |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for group in summary["groups"]:
        m = group["metrics"]
        values = [m["http_ms"]["median"], m["http_ms"]["p95"], m["orchestration_ms"]["median"], m["orchestration_ms"]["p95"],
                  m["issuer_ms"]["median"], m["risk_ms"]["median"], m["overlap_ms"]["median"]]
        lines.append(f"| {group['profile']} | {group['mode']} | {group['valid']} / {group['attempted']} | {group['error_count']} | " + " | ".join(map(fmt, values)) + " |")
    lines += ["", "Median is the middle value (mean of the middle two for even n). "
              "P95 is sorted value at one-based rank ceil(0.95 × n), without interpolation. "
              "summary.json includes n, mean, median, P95, min, max, and sample standard deviation (n−1; N/A for n<2) "
              "for HTTP, orchestration, both branches, and overlap; plus error rates, decision counts, and input/order counts.", "",
              "## Comparisons and interpretation", "",
              "Reduction = (sequential median − concurrent median) / sequential median × 100. Negative means concurrent was slower.", "",
              "| Profile | HTTP median reduction | Orchestration median reduction |",
              "| --- | ---: | ---: |"]
    for comparison in summary["comparisons"]:
        reductions = comparison["median_reduction_percent"]
        lines.append(f"| {comparison['profile']} | {fmt(reductions['http_ms'])} | {fmt(reductions['orchestration_ms'])} |")
    lines += ["", "Balanced has two 250 ms artificial waits; the theoretical wait-only estimates are 500 ms sequential "
              "and 250 ms concurrent. Asymmetric has 100/400 ms waits; estimates are 500 ms and 400 ms. "
              "Those estimates are not measurements. Inspect measured overlap and the table above to assess the actual benefit.", "",
              "Lightweight has no intentional wait. Its tiny timing differences reflect scheduling and measurement noise; "
              "no meaningful speed improvement or reduction percentage is claimed. "
              "Any reductions above describe this run only, without a statistical significance claim.", "",
              "## Method and limits", "",
              "One synchronous POST at a time to " + metadata["url"] + "/api/checkouts; no retries or redirects. "
              "Each request uses a fresh connection and unique key. Adjacent pairs share business inputs; "
              "the first mode alternates and profiles rotate. All warm-ups precede measured attempts. "
              "The ten-case corpus covers both presets, threshold boundaries, foreign billing, and both risk triggers. "
              "Default 50 trials give five observations per case per mode/profile.", "",
              "HTTP uses client perf_counter_ns from immediately before sending through full body receipt, excluding JSON parsing. "
              "Server orchestration_ms excludes HTTP, validation, persistence, and trace serialization. "
              "Branch offsets use the server's single monotonic origin. No clocks are subtracted across processes. "
              "Sequential overlap must be zero and delayed concurrent intervals must overlap. "
              "Trace arithmetic tolerance is 0.000001 ms (1 ns). Numeric fields must be finite, nonnegative numbers.", "",
              "These are artificial simulated waits, never genuine issuer-network or fraud-processing measurements. "
              "This is per-checkout latency, not throughput, CPU parallelism, a multi-client load test, or a production reliability estimate. "
              "Local OS/container scheduling, connection setup, and database work affect HTTP latency. "
              "The host is not isolated and no confidence interval is claimed. Technical fault and cancellation paths are verified "
              "by regression tests separately, without public fault injection in this experiment.", "",
              "Environment, source hashes, full configuration, corpus, and key prefix: [metadata.json](metadata.json). "
              "Raw responses and every submitted trial: [trials.csv](trials.csv). "
              "Unrounded statistics: [summary.json](summary.json). Reproduction and safe record removal: [benchmark guide](../../README.md).", "",
              "## Failures", "", metadata.get("abort_reason") or "No early stop."]
    for failure in summary["failures"]:
        lines.append(f"- Trial {failure['sequence']} ({failure['phase']}): " + "; ".join(failure["errors"]))
    if not summary["failures"]:
        lines.append("No failed submitted samples.")
    return "\n".join(lines) + "\n"


def command_output(command):
    try:
        return subprocess.check_output(command, cwd=ROOT, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def write_outputs(output, rows, metadata):
    check_pairs(rows)
    summary = summarize(rows, metadata)
    with (output / "trials.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "errors_json": json.dumps(row["errors"])})
    for name, data in (("metadata", metadata), ("summary", summary)):
        (output / f"{name}.json").write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    (output / "report.md").write_text(report(summary, metadata))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:18000", help="Running local API origin")
    parser.add_argument("--trials", type=int, default=50, help="Measured trials per mode/profile (default 50)")
    parser.add_argument("--warmups", type=int, default=5, help="Warm-ups per mode/profile (default 5)")
    parser.add_argument("--timeout", type=float, default=10, help="HTTP socket timeout in seconds (default 10)")
    parser.add_argument("--output", type=Path, help="New results directory (must not exist)")
    parser.add_argument("--environment-json", type=Path, help="Optional recorded server/container metadata")
    args = parser.parse_args(argv)
    parsed = urlsplit(args.url)
    if (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        parser.error("Use a plain HTTP localhost API origin without credentials, path, query, or fragment.")
    if args.trials < 1 or args.warmups < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("trials must be positive, warmups nonnegative, timeout finite and positive")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    prefix = "asaanpay-bench-" + run_id + "-"
    output = args.output or ROOT / "benchmarks" / "results" / run_id
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted((ROOT / "app").glob("*.py")) + [ROOT / "scripts/benchmark.py", ROOT / "requirements.txt", ROOT / "compose.yaml", ROOT / "db/schema.sql"]
    metadata = dict(
        started_utc=utc_now(), run_id=run_id, idempotency_key_prefix=prefix, url=args.url.rstrip("/"),
        trials_per_mode_profile=args.trials, warmups_per_mode_profile=args.warmups,
        planned_measured_attempts=args.trials * 6, planned_warmup_attempts=args.warmups * 6,
        http_socket_timeout_seconds=args.timeout, branch_timeout_ms=2000,
        profiles_artificial_delays_ms=PROFILES, corpus=CASES,
        python=sys.version, platform=platform.platform(), machine=platform.machine(),
        processor=platform.processor(), logical_cpu_count=os.cpu_count(),
        perf_counter=vars(time.get_clock_info("perf_counter")),
        git_revision=command_output(["git", "rev-parse", "HEAD"]),
        git_status=command_output(["git", "status", "--short"]),
        source_sha256={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        server_environment=json.loads(args.environment_json.read_text()) if args.environment_json else "Not captured; supply --environment-json.",
        connection_policy="Fresh connection per request; Connection: close; proxies disabled; no redirects/retries",
        ordering="Rotate profiles each round; mode parity = (round + round//10 + profile_index)%2",
        numeric_tolerance_ms=EPSILON_MS, percentile="nearest rank: sorted[ceil(0.95*n)-1]",
        stdev="sample, n-1; unavailable for n<2", abort_reason=None,
        command=[sys.executable, "scripts/benchmark.py", *(argv if argv is not None else sys.argv[1:])],
    )
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    rows, keys, seen_ids = [], set(), set()
    try:
        with opener.open(metadata["url"] + "/api/health", timeout=args.timeout) as response:
            metadata["health"] = json.load(response)
            if response.status != 200 or metadata["health"] != {"status": "ok", "database": "connected"}:
                raise ValueError("API/database is not healthy")
        # Journal before the next request, so an interrupted process retains raw
        # samples even if it cannot finish the summary. CSV is the final table.
        with (output / "observations.jsonl").open("w") as journal:
            for trial in schedule(args.warmups, args.trials, prefix):
                if trial["idempotency_key"] in keys:
                    raise ValueError("Duplicate generated key; refusing to submit")
                keys.add(trial["idempotency_key"])
                row, body, payload, uncertain = post_trial(opener, metadata["url"], args.timeout, trial)
                rows.append(row)
                validate_trial(row, body, payload, seen_ids)
                journal.write(json.dumps(row, allow_nan=True) + "\n")
                journal.flush()
                if len(rows) % 30 == 0:
                    print(f"Recorded {len(rows)} of {6 * (args.warmups + args.trials)} attempts", flush=True)
                if uncertain:
                    metadata["abort_reason"] = "Stopped after uncertain response; server work might remain outstanding. No further requests sent."
                    break
    except (OSError, URLError, http.client.HTTPException, ValueError, KeyboardInterrupt) as error:
        metadata["abort_reason"] = f"Stopped: {type(error).__name__}: {error}"
    finally:
        metadata["finished_utc"] = utc_now()
        summary = write_outputs(output, rows, metadata)
        print(f"{summary['status']}: {summary['measured_attempts']} measured, {summary['warmup_attempts']} warm-ups; {output}")
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
