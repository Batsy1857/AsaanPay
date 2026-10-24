# Milestone 6 verification — 2026-10-24

Milestone 6 is complete. The actual experiment used the application already running at `http://localhost:18000`; it was not restarted for measurement. Application Python, frontend assets, rules, delays, coordinator, schema, and timing-trace semantics are unchanged. No database cleanup or volume removal occurred. No slides were created.

## Regression results

| Check | Outcome |
| --- | --- |
| Existing backend suite in the running container, `docker compose exec -T -e RUN_DB_TESTS=1 api python -m unittest discover -s tests -v` | **50 passed**, no skips, 3.219 s; includes PostgreSQL/API/idempotency races, durable failures, real branch timeouts, cancellation, and recovery checks. |
| New benchmark suite, `python3 -m unittest discover -s tests -p test_benchmark.py -v` | **11 passed**, 0.083 s on final test revision. Covers balanced scheduling/fresh keys, paired inputs, known statistics, all six captured trace contracts, invalid traces, overlap, replay, incorrect decisions, pair mismatch, failure retention, timeout/503 stopping. |
| Final complete backend + benchmark suite in the rebuilt image, `docker compose run --rm --no-deps -e RUN_DB_TESTS=1 api python -m unittest discover -s tests -v` | **61 passed**, no skips, 3.045 s. This launches a disposable test process, not another Uvicorn application; it does not restart the serving application. |
| Local Python suite, `.venv/bin/python -m unittest discover -s tests -v` | 61 collected; **50 passed, 11 live-database tests explicitly skipped**, 2.590 s. Those 11 passed in the container run above. |
| Frontend/client/browser suite, `python3 tests/run_frontend.py --url http://localhost:18000` | **40 passed, 0 failed**, including real checkout/comparison and all five viewport widths. |
| Image packaging, `docker compose build api` | Passed; Dockerfile now includes `scripts` so benchmark imports work in regression tests. |
| Independent artifact audit | All smoke/full statistics exactly reproduced from CSV using separate calculations; expected decisions, measured/warm-up counts, fresh keys/IDs, HTTP statuses, timings, and interval overlap checked. |
| Read-only PostgreSQL audit | **348/348** exact smoke/full keys matched their stored IDs, states/decisions, and full timing traces. |

The first frontend run exposed a pre-existing readiness race: the test could find the form before deferred `app.js` finished loading and then click controls without handlers. The test now waits for the iframe document to reach `readyState === "complete"`. The full rerun passed; no UI implementation changed.

An initial combined-image test run also exposed a test setup dependency on Compose/schema files intentionally absent from the API image. The new mocked-503 test now creates its own temporary source-metadata fixture. All 61 tests subsequently passed. These test-run failures are disclosed here; they were not benchmark samples. A pre-existing Starlette/httpx deprecation warning remains non-fatal; dependencies were not changed.

## Experiments and correctness

| Run | Warm-ups per mode/profile | Measured per mode/profile | Total warm-ups | Total measured | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| [Smoke](results/milestone6-smoke/report.md) | 1 | 2 | 6 | 12 | PASS; 12 expected decisions, 6 matching pairs, no failures. |
| [Full](results/milestone6-full/report.md) | 5 | 50 | 30 | 300 | PASS; 300 expected decisions, 150 matching pairs, no failures. |

Full run: **2026-10-24 15:59:51–16:01:27 Asia/Karachi** (10:59:51–11:01:27 UTC). Full-run prefix: `asaanpay-bench-20261024T105951Z-40bccc8ed17f4e05b9e79e2963e245d9-`. The exact prefix and configuration are also in metadata.

All 330 full-run POSTs returned fresh 201 resources with unique keys and unique attempt IDs. Warm-ups and measured samples all passed trace and outcome validation. Measured outcomes were **30 SUCCEEDED, 120 VERIFICATION_REQUIRED, and 150 DECLINED**, as expected from the fixed corpus. Each mode/profile had five measured observations of every input case; each mode ran first in 25 of its 50 pairs per profile.

There were **zero measured errors (0%), zero warm-up errors, zero timeouts, zero technical failures, zero unexpected outcomes, zero replays, and zero missing/invalid traces** in both experiments. No failed samples were removed or replaced. Smoke only exercised the first two corpus cases and validates collection, while the full run covers all ten cases.

The initial live contract inspection submitted one additional, separately excluded lightweight checkout before implementation: key `asaanpay-bench-contract-8bad18e7cab34948b80e360bbc6b164a`, ID `d3a99a44-808e-407c-b35c-d9a39df06c53`, HTTP 201/SUCCEEDED. It is neither a measured trial nor a warm-up. Thus these experiments retained 348 rows plus one contract probe; frontend regressions independently create their usual synthetic attempts.

## Actual measured results

Milliseconds, **50 measured samples per row**, warm-ups excluded. P95 is nearest rank, one-based `ceil(0.95 × 50) = 48`. These are observations, not configured delay values.

| Profile | Mode | HTTP median | HTTP P95 | Orchestration median | Orchestration P95 |
| --- | --- | ---: | ---: | ---: | ---: |
| lightweight | sequential | 6.092 | 13.039 | 0.053 | 0.140 |
| lightweight | concurrent | 6.528 | 15.501 | 0.087 | 0.221 |
| balanced | sequential | 517.967 | 531.401 | 506.526 | 510.263 |
| balanced | concurrent | 263.952 | 281.030 | 252.471 | 257.834 |
| asymmetric | sequential | 516.814 | 537.468 | 504.439 | 512.433 |
| asymmetric | concurrent | 413.472 | 433.949 | 401.915 | 407.223 |

All **150 measured sequential** intervals had exactly **0 ms** overlap, without needing the 1 ns arithmetic tolerance. All **50 balanced concurrent** intervals overlapped, minimum **250.172 ms**, median **252.065 ms**. All **50 asymmetric concurrent** intervals overlapped, minimum **100.314 ms**, median **102.127 ms**. Lightweight concurrent median overlap was **0.009 ms**, with no minimum imposed. The raw CSV includes unrounded values and the full response trace.

Balanced concurrency reduced median HTTP latency by **49.041%** and orchestration latency by **50.156%** in this run: the measured roughly 252 ms overlap allows both artificial waits to proceed together. Asymmetric reductions were **19.996% HTTP** and **20.324% orchestration**: the roughly 102 ms issuer interval overlaps, but the 400 ms risk wait remains the longer path. Lightweight concurrent median was slightly higher than sequential (HTTP +0.436 ms; orchestration +0.034 ms). These tiny differences do not establish a meaningful performance improvement or regression.

The theoretical wait-only predictions are 500→250 ms for balanced and 500→400 ms for asymmetric. They explain the pattern but were never inserted into measured data. HTTP latency includes transport, connection setup, validation, persistence, and response handling; orchestration does not.

## Environment and limitations

Client: Python 3.13.2, macOS 15.6 arm64, Mac14,7, 8 logical CPUs, 8 GiB host memory. Serving container: Python 3.13.17 on Linux/aarch64, one Uvicorn worker. PostgreSQL: 17.11. Docker Engine: 29.9.1, VM capacity 8 CPUs and 6,210,043,904 bytes memory, with no explicit per-container CPU/memory limits. All running backend Python source hashes matched the workspace before collection; recorded runner/backend hashes still matched at final audit. Metadata preserves original serving image hashes; subsequent test-image builds did not replace those running containers.

The full experiment used one synchronous HTTP client, fresh connections, balanced mode order, and rotated profiles. Regression requests completed before timing started; combined-image packaging tests ran after timing finished. Ordinary health checks remained enabled. The host was not isolated from normal OS activity. No load, throughput, CPU parallelism, confidence-interval/significance study, real issuer network, or real fraud-processing claim is supported. Reliability evidence is deterministic local validation plus fault/cancellation regression tests, not a production availability estimate. No requested measurements remain uncompleted; the optional figure was omitted because tables show all six comparisons clearly.

## Exact file inventory

Modified:

- `Dockerfile` — include the standalone script in the image for tests.
- `README.md` — Milestone 6 status, benchmark commands, and links.
- `docs/architecture.md` — implemented procedure, 5/50 counts, validation/statistical definitions, file tree, and status.
- `tests/frontend_tests.js` — wait for completed iframe load before browser smoke interactions.

Added:

- `scripts/benchmark.py` — standard-library endpoint runner, trace/outcome validation, failure accounting, statistics, and artifacts.
- `tests/test_benchmark.py` — eleven focused validation/accounting/transport tests.
- `benchmarks/README.md` — clean-start reproduction, methodology, limitations, and exact-key cleanup dry run.
- `benchmarks/verification.md` — this audit and result interpretation.
- `benchmarks/results/milestone6-smoke/trials.csv`
- `benchmarks/results/milestone6-smoke/observations.jsonl`
- `benchmarks/results/milestone6-smoke/summary.json`
- `benchmarks/results/milestone6-smoke/metadata.json`
- `benchmarks/results/milestone6-smoke/report.md`
- `benchmarks/results/milestone6-full/trials.csv`
- `benchmarks/results/milestone6-full/observations.jsonl`
- `benchmarks/results/milestone6-full/summary.json`
- `benchmarks/results/milestone6-full/metadata.json`
- `benchmarks/results/milestone6-full/report.md`
