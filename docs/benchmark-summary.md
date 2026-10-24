# Milestone 6 benchmark: presentation evidence

**Balanced: 49.04% lower median HTTP latency. Asymmetric: 20.00% lower median HTTP latency. Lightweight: no meaningful advantage.** These describe one controlled local experiment with artificial waits, not real payment-provider performance.

Sources are the preserved [full report](../benchmarks/results/milestone6-full/report.md), [unrounded summary](../benchmarks/results/milestone6-full/summary.json), [metadata](../benchmarks/results/milestone6-full/metadata.json), [raw CSV](../benchmarks/results/milestone6-full/trials.csv), and [per-request journal](../benchmarks/results/milestone6-full/observations.jsonl). This document summarizes those files; it is not a new benchmark run.

## Experiment and measurement boundaries

The full run took place on **24 October 2026, 15:59:51–16:01:27 Asia/Karachi** (10:59:51–11:01:27 UTC), against the existing `POST /api/checkouts` endpoint at localhost:18000.

- Three profiles × two modes × **50 measured attempts = 300 measured attempts**, forming **150 mode pairs**.
- **5 warm-ups per mode/profile = 30 warm-ups**, all excluded from statistics and preceding measurement.
- Identical paired synthetic inputs, fresh unique keys, fresh HTTP connections, no retries, one outstanding checkout request at a time.
- Profiles rotated; first mode counterbalanced, 25 times each per profile. Ten cases, five observations per case per mode/profile: both issuer presets crossed with domestic below/at/above-threshold amounts, low foreign amount and both risk triggers.
- A separate [smoke run](../benchmarks/results/milestone6-smoke/report.md) had 12 measured attempts and 6 warm-ups. It is not pooled with the full run.

| Metric | What it measures |
| --- | --- |
| Client HTTP latency | `time.perf_counter_ns` from sending POST through full response receipt. Includes connection/transport, validation, database work, orchestration and response serialization/handling; excludes subsequent client JSON parsing and analysis. |
| Server orchestration latency | Actual `timing_trace.orchestration_ms`: scheduling/execution through both branch results, decision and overlap calculation. Excludes HTTP, persistence and trace construction/serialization. |
| Branch duration / overlap | Actual server start/end intervals, not configured delay values; overlap is `max(0, min(ends) − max(starts))`. |

Median is the middle value or mean of the middle two. **P95 uses nearest rank**, sorted one-based position `ceil(0.95 × n)`; with n=50 this is position **48**, without interpolation. Values below are rounded to three decimals only for presentation. The original summary also retains mean, min, max and sample standard deviation (n−1) for HTTP, orchestration, issuer, risk and overlap.

## Actual latency results

All values are **milliseconds**, with **50 measured observations per row**.

| Profile | Mode | HTTP median | HTTP P95 | Orchestration median | Orchestration P95 |
| --- | --- | ---: | ---: | ---: | ---: |
| lightweight | sequential | 6.092 | 13.039 | 0.053 | 0.140 |
| lightweight | concurrent | 6.528 | 15.501 | 0.087 | 0.221 |
| balanced | sequential | 517.967 | 531.401 | 506.526 | 510.263 |
| balanced | concurrent | 263.952 | 281.030 | 252.471 | 257.834 |
| asymmetric | sequential | 516.814 | 537.468 | 504.439 | 512.433 |
| asymmetric | concurrent | 413.472 | 433.949 | 401.915 | 407.223 |

Reduction is `(sequential median − concurrent median) / sequential median × 100`, calculated from unrounded medians. It is a ratio of group medians, not the average of individual pair percentages.

| Profile | Median HTTP reduction | Median orchestration reduction | Interpretation |
| --- | ---: | ---: | --- |
| balanced | **49.04%** | 50.16% | The two artificial waits substantially overlap. |
| asymmetric | **20.00%** | 20.32% | Overlap removes much of the shorter branch's contribution; the longer risk branch remains. |
| lightweight | Not claimed | Not claimed | Concurrent medians were slightly higher; tiny differences provide no meaningful advantage or demonstrated regression. |

The UI's two-attempt comparison percentage uses **orchestration** latency. It must not be introduced as the 49.04%/20.00% median **HTTP** benchmark result.

## Correctness and overlap evidence

There were **zero measured failures (0/300; 0%) and zero warm-up failures (0/30)**. All 300 measured decisions matched the deterministic oracle; **all 150 mode pairs had identical decisions**, branch business results, final explanations and rule versions. Outcomes were 30 SUCCEEDED, 120 VERIFICATION_REQUIRED and 150 DECLINED. A business decline or verification result is a valid outcome, not a technical failure.

All 330 full-run requests used unique keys, returned distinct IDs with HTTP 201, and passed trace validation. There were no timeouts, technical failures, unexpected outcomes, accidental replays or invalid traces. The runner retains failed samples and flags failure if they occur; no failed samples were discarded or replaced in this run.

| Measured interval evidence | Count | Minimum overlap (ms) | Median overlap (ms) |
| --- | ---: | ---: | ---: |
| Sequential, all profiles | 150 | 0.000 | 0.000 |
| Concurrent, balanced | 50 | 250.172 | 252.065 |
| Concurrent, asymmetric | 50 | 100.314 | 102.127 |
| Concurrent, lightweight | 50 | 0.005 | 0.009 |

Sequential overlap was exactly zero before rounding. Delayed concurrent intervals all overlapped. Trace arithmetic used a **1 ns (0.000001 ms) numeric tolerance**, not a claim that hardware clock resolution or experiment accuracy is 1 ns. Branches completed before coordination; durations matched their intervals. The [Milestone 6 verification record](../benchmarks/verification.md) separately reports 61 backend/benchmark tests and 40 frontend checks passed, plus read-only persistence verification. These are historical test outcomes, not tests rerun for Milestone 7A.

## Artificial workloads and theoretical interpretation

The wrapper uses `await asyncio.sleep`; no real issuer network request or expensive fraud computation is being measured. Even the zero-wait profile yields the event loop. Without overhead, sequential waiting contributes approximately `issuer wait + risk wait`, while concurrent waiting contributes approximately `max(issuer wait, risk wait)`.

| Profile | Configured issuer / risk waits | Theoretical sequential / concurrent wait contribution |
| --- | --- | --- |
| lightweight | 0 / 0 ms | No intentional waiting; scheduling and surrounding work remain. |
| balanced | 250 / 250 ms | Approximately 500 / 250 ms. |
| asymmetric | 100 / 400 ms | Approximately 500 / 400 ms. |

These are explanatory predictions, **not measured results or live-demo guarantees**. The measured tables above include real scheduling overhead; HTTP also includes database/transport work. Cooperative overlap of waiting tasks does not establish simultaneous CPU execution.

## Environment and limitations

Recorded environment: macOS 15.6 arm64, Python 3.13.2 client, 8 logical CPUs and 8 GiB host memory; one Uvicorn worker in Python 3.13.17/Linux aarch64; PostgreSQL 17.11; Docker 29.9.1 with an 8-CPU VM and about 6.21 GB VM memory. No explicit per-container CPU/memory limits were configured. Source/image hashes and configuration are preserved in metadata, including confirmation that running backend source matched the workspace at collection.

This is a single local study with 50 observations per group, not a production latency forecast, availability guarantee or throughput/load test. The host was not isolated; OS/container scheduling, fresh connection setup, database work and ordinary health probes contribute variation. Rotating/alternating order reduces systematic bias but cannot remove all noise. No confidence intervals or significance test were reported, and P95 from 50 samples has limited tail resolution. Normal benchmark inputs did not inject failures; separate regression tests exercised faults and cancellation.

The recorded balanced-concurrent HTTP maximum was **489.182 ms**, despite its 263.952 ms median; the raw evidence does not identify a cause. Do not promise every concurrent request is faster or infer a specific database/network bottleneck from that outlier. Use the [methodology and reproduction guide](../benchmarks/README.md) when explaining controls; preserve the original artifacts as evidence.
