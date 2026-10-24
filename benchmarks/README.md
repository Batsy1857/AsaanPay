# Milestone 6 benchmark

`scripts/benchmark.py` measures the existing `POST /api/checkouts` endpoint. It uses only Python's standard library. It creates ordinary synthetic payment attempts; it does not change business rules, artificial waits, orchestration, persistence, trace semantics, or the UI.

The recorded experiment is in [results/milestone6-full/report.md](results/milestone6-full/report.md), with a separate [smoke experiment](results/milestone6-smoke/report.md). [Verification notes](verification.md) record regression results and environment checks. These files contain **actual local measurements**. Theoretical wait-only estimates below are labeled separately.

## Reproduce from a clean startup

Use Python 3.13 and Docker Desktop/Engine with Compose. From the repository root:

```sh
[ -f .env ] || cp .env.example .env
API_PORT=18000 docker compose up --build -d --wait
curl --fail-with-body http://localhost:18000/api/health
# Exactly one API service/process/worker. Finish tests before timing the API.
docker compose exec -T -e RUN_DB_TESTS=1 api python -m unittest discover -s tests -v
python3 tests/run_frontend.py --url http://localhost:18000
# Choose new output paths: existing directories are never overwritten.
python3 scripts/benchmark.py --url http://localhost:18000 \
  --trials 2 --warmups 1 --output benchmarks/results/my-smoke
python3 scripts/benchmark.py --url http://localhost:18000 \
  --trials 50 --warmups 5 --output benchmarks/results/my-full
```

Use the app's actual configured port when it is already running. Do not restart an application with unfinished requests just for the benchmark. Existing database volumes can be reused; this benchmark requires no schema changes or reset. The frontend test requires an installed Chrome/Chromium; pass `--chrome /path/to/browser` when needed. If unavailable, report that check as uncompleted.

Do not use the checkout UI or run another benchmark/regression suite while collecting timings. The runner sends exactly one synchronous request at a time. Compose health checks still run. It neither creates concurrent clients nor measures system throughput.

Defaults are **5 warm-ups and 50 measured attempts per mode/profile**, giving **30 warm-ups + 300 measured attempts**, grouped into 150 measured mode pairs. Both counts are configurable; `--trials` must be positive and `--warmups` may be zero. Omitting `--output` creates a UTC timestamp/UUID directory under `benchmarks/results/`. `--timeout` is a socket-operation timeout, default 10 seconds, not a hard whole-experiment deadline. No HTTP or business retries are performed.

An optional `--environment-json path/to/environment.json` embeds additional recorded environment details. The default metadata records UTC timestamps, client Python/OS/CPU count, clock information, command/configuration, source hashes, Git revision/status, input corpus, and idempotency prefix. Without the optional file, it explicitly says server metadata was not captured. To record the server/container environment, collect these non-secret outputs in a JSON object before the run:

```sh
docker compose ps
docker compose exec -T api python --version
docker compose exec -T db postgres --version
docker inspect asaanpay-api-1 asaanpay-db-1 --format '{{.Name}} image={{.Image}} command={{json .Config.Cmd}} memory_bytes={{.HostConfig.Memory}} nano_cpus={{.HostConfig.NanoCpus}} cpu_quota={{.HostConfig.CpuQuota}} cpu_period={{.HostConfig.CpuPeriod}}'
docker info --format 'CPUs={{.NCPU}} memory_bytes={{.MemTotal}} version={{.ServerVersion}} architecture={{.Architecture}}'
```

Container names can differ with the Compose project name. Do not dump container environment variables or `.env` credentials into result files. For the committed runs, metadata includes actual server versions, image hashes, memory/CPU settings, and verification that all running backend Python source hashes match the workspace.

## Inputs and execution order

The fixed ten-case corpus crosses both card presets with five amount/country combinations:

| Amount PKR | Billing country | Expected risk | Approved preset | Declined preset |
| ---: | --- | --- | --- | --- |
| 49999.99 | PK | CLEAR | SUCCEEDED | DECLINED |
| 50000.00 | PK | VERIFY | VERIFICATION_REQUIRED | DECLINED |
| 50000.01 | PK | VERIFY | VERIFICATION_REQUIRED | DECLINED |
| 7500.00 | US | VERIFY | VERIFICATION_REQUIRED | DECLINED |
| 50000.00 | US | VERIFY | VERIFICATION_REQUIRED | DECLINED |

The oracle is specified independently in the runner, without importing the application coordinator. Each pair uses exactly the same synthetic payment inputs and workload; only mode and attempt identity differ. With 50 trials, every case appears five times per mode/profile. Short smoke runs cover only the beginning of the corpus; they validate collection, not full rule coverage.

All warm-ups precede measured attempts. Profiles rotate each round. Mode order is determined by `(round_index + round_index // 10 + profile_index) % 2`: sequential first for zero, concurrent first for one. At the default size each mode runs first 25 times per profile; repeated corpus cycles also reverse first-mode preference. Odd/custom sizes can differ by a small number; actual order and case counts are retained.

Every request gets a unique key:

```text
asaanpay-bench-<UTC run timestamp>-<random UUID hex>-<phase>-<profile>-<pair index>-<mode>
```

The runner rejects generated duplicate keys, reused returned attempt IDs, and any POST status other than 201. A 201 alone is insufficient: the business outcome and timing trace must also pass validation. There is no accidental replay benchmark. It stops after transport errors, unreadable/nonterminal responses, or HTTP 5xx because server work might remain outstanding. It preserves partial results and exits nonzero; do not launch another run until uncertainty is resolved. Other invalid terminal samples remain recorded and flagged. A failed warm-up also makes the experiment FAIL.

## What is measured

| Metric | Boundary |
| --- | --- |
| `http_ms` | Client `time.perf_counter_ns()` immediately before sending POST until the complete response body has been read. Includes fresh TCP connection, HTTP, validation, database work, orchestration, and serialization. JSON parsing, validation, file writes, and summary generation are outside this interval. |
| `orchestration_ms` | Actual server trace field, from scheduling/execution start through join, decision, and overlap calculation. Excludes HTTP, database work, and trace construction/serialization. |
| `issuer_ms`, `risk_ms` | Actual `timing_trace.branches.<name>.duration_ms`; includes artificial wait and synthetic engine call. |
| `overlap_ms` | Actual trace value, cross-checked against `max(0, min(end offsets) - max(start offsets))`. |

Both modes use fresh HTTP connections (`Connection: close`); proxies and redirects are disabled. The client and server clocks are independent: no cross-process timestamp subtraction is used. UTC timestamps are audit metadata, not latency clocks.

Trace validation requires finite, nonnegative numbers; matching mode/profile/delay/measurement labels and the 2000 ms branch timeout; completed branch statuses without error types; end ≥ start; duration = end − start; branch ends ≤ decision ≤ orchestration ≤ HTTP elapsed; and matching recomputed overlap. Equality/order tolerance is **0.000001 ms (1 ns)** solely for numeric conversion/subtraction. Sequential intervals must be ordered with overlap zero within that tolerance. Concurrent balanced/asymmetric intervals must overlap by more than the tolerance. Lightweight concurrency has no minimum overlap requirement. Timings are never rounded before analysis.

Each sample must have the expected state, decision, issuer/risk results, `sandbox_v1` rules, normalized request echo, terminal flag, nonempty explanation, and no technical error. Pairs must agree on state, business decision, both branch results, decision explanation, and rule version. Pair agreement alone cannot excuse two wrong decisions.

## Statistics and failure accounting

For each mode/profile, summary JSON contains mean, median, P95, minimum, maximum, and **sample** standard deviation for all five metrics. Median is the middle value or the mean of the two middle values. **P95 uses nearest rank: sorted value at one-based index `ceil(0.95 × n)`**, without interpolation (rank 48 for n=50). Standard deviation uses n−1 and is unavailable for n<2. Empty sets are N/A/null, never zero.

Normal performance summaries use valid measured attempts. Warm-ups never enter these summaries. Failed samples are **not silently discarded**: their raw responses/timings, validation errors, HTTP status, and decisions remain in CSV/JSONL; summary JSON contains failure details, per-group attempted/valid counts, error rate (`invalid / attempted`), correctness rate (`expected decisions / attempted`), and all-attempt HTTP diagnostics including failures. Invalid numeric traces are kept as diagnostic raw response text and excluded from normal metrics. Missing pairs invalidate their submitted members. A partial run fails even if no completed sample was itself invalid.

Median reduction is `(sequential − concurrent) / sequential × 100`, separately for HTTP and orchestration. Negative values mean concurrent was slower. Reductions are unavailable for incomplete/failed comparisons and for lightweight; no percentage speed claim is made for tiny zero-wait differences. Statistics are computed only from recorded trial values; no theoretical timing is inserted into results.

## Artifacts

Each new run directory contains:

- `trials.csv`: every warm-up and measured trial, identity/input/order fields, HTTP/decision fields, nanosecond client elapsed time, branch fields, trace validation, pair agreement, and full raw response text.
- `observations.jsonl`: an append/flush journal after every request, before the next request; useful if a process is interrupted before final summaries. Pair validation happens at finalization and is reflected in CSV/summary, not retroactively in the journal.
- `summary.json`: unrounded statistics, counts, error/decision rates, comparisons, and failures.
- `metadata.json`: timestamps, configuration, complete corpus, machine/runtime details, key prefix, source revision/hashes, and optional server metadata.
- `report.md`: concise actual-measurement tables, interpretation, methodology, and failure status.

CSV and journal retain full response JSON, including `timing_trace`, so calculations can be independently checked. A hard process kill or disk failure can prevent final files from completing; retain the journal and report the run as incomplete. No runner can promise successful artifact writes after a filesystem failure.

## Interpretation and limits

**Theoretical wait-only estimates, not measurements:** balanced sequential 500 ms versus concurrent 250 ms; asymmetric sequential 500 ms versus concurrent 400 ms; lightweight has no intentional wait. Overlapping waits can reduce the decision critical path. Asymmetry limits the benefit because the 400 ms branch still has to finish. HTTP includes extra work, so its reduction need not match orchestration's. Lightweight primarily exposes overhead and scheduling noise.

All configured waits are artificial simulations, not issuer-network latency or fraud computation. This is a small local, single-client experiment, with no throughput, CPU-parallelism, production availability, or statistical significance claim. Host scheduling, Docker, database state, fresh connections, and ordinary health probes affect measurements. Regression tests exercise injected technical failures/timeouts/cancellation separately; the measured normal-input run does not inject faults. No presentation slides are included in Milestone 6.

## Retention and optional safe removal

The runner never deletes records or resets tables/volumes. Keep its CSV and metadata as the record manifest. To remove **one run's rows**, an operator can load its exact CSV `idempotency_key` values into a temporary table and join those keys to `payment_attempts` inside a transaction. First verify every key starts with that run's full `metadata.json` `idempotency_key_prefix`, contains only letters/digits/hyphens, and is unique. Do not use a broad `asaanpay-bench-%` delete.

Generate a reviewable SQL dry run from one exact manifest (this command only writes a local file):

```sh
python3 - benchmarks/results/milestone6-full > /tmp/asaanpay-benchmark-cleanup.sql <<'PYTHON'
import csv, json, pathlib, re, sys
run = pathlib.Path(sys.argv[1])
prefix = json.loads((run / "metadata.json").read_text())["idempotency_key_prefix"]
keys = [r["idempotency_key"] for r in csv.DictReader((run / "trials.csv").open())]
assert prefix.startswith("asaanpay-bench-") and re.fullmatch(r"[A-Za-z0-9-]+", prefix)
assert keys and len(keys) == len(set(keys))
assert all(k.startswith(prefix) and re.fullmatch(r"[A-Za-z0-9-]+", k) for k in keys)
print("BEGIN;")
print("CREATE TEMP TABLE benchmark_keys (idempotency_key text PRIMARY KEY) ON COMMIT DROP;")
print("INSERT INTO benchmark_keys VALUES " + ",".join("('" + k + "')" for k in keys) + ";")
print("SELECT p.id, p.idempotency_key, p.state FROM payment_attempts p JOIN benchmark_keys b USING (idempotency_key);")
print("DELETE FROM payment_attempts p USING benchmark_keys b WHERE p.idempotency_key = b.idempotency_key AND p.state IN ('SUCCEEDED','DECLINED','VERIFICATION_REQUIRED','ERROR') RETURNING p.id, p.idempotency_key;")
print("ROLLBACK; -- Preview only; deliberately change to COMMIT after reviewing the exact rows.")
PYTHON
```

Review the generated keys and SQL, then preview it in PostgreSQL (it rolls back):

```sh
docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < /tmp/asaanpay-benchmark-cleanup.sql
```

Check the returned count and identities against the artifacts. For intentional removal, replace the final `ROLLBACK` with `COMMIT` only after review and rerun the same exact-key script. The terminal-state guard leaves uncertain in-flight rows alone. A deletion should never target unrelated keys, tables, or Docker volumes. The single pre-implementation contract probe is separately identified in [verification.md](verification.md); it is not in either benchmark's counts. No cleanup was performed for Milestone 6.
