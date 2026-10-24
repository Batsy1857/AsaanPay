# AsaanPay Milestone 6: actual local measurements

Result: **PASS**. Started 2026-10-24T10:57:18.268559+00:00; finished 2026-10-24T10:57:24.038980+00:00.

Measured: 12; warm-ups (excluded): 6. Measured errors: 0; warm-up errors: 0.
Expected decisions: 12/12; matching measured mode pairs: 6/6.
Unique submitted keys: 18; unique returned attempt IDs: 18.

## Measured latency (milliseconds)

Valid measured samples only in this table; warm-ups excluded. Failed samples remain in trials.csv, failures below, and summary.json. All-attempt HTTP diagnostics are also in summary.json.

| Profile | Mode | Valid / attempts | Errors | HTTP median | HTTP P95 | Orchestration median | Orchestration P95 | Issuer median | Risk median | Overlap median |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| lightweight | sequential | 2 / 2 | 0 | 12.010 | 19.201 | 0.094 | 0.145 | 0.029 | 0.049 | 0.000 |
| lightweight | concurrent | 2 / 2 | 0 | 5.856 | 6.324 | 0.071 | 0.079 | 0.016 | 0.028 | 0.008 |
| balanced | sequential | 2 / 2 | 0 | 528.604 | 540.354 | 508.026 | 511.646 | 253.064 | 254.835 | 0.000 |
| balanced | concurrent | 2 / 2 | 0 | 266.468 | 270.906 | 253.397 | 255.559 | 253.092 | 253.267 | 253.078 |
| asymmetric | sequential | 2 / 2 | 0 | 517.340 | 518.900 | 505.449 | 506.735 | 103.289 | 401.984 | 0.000 |
| asymmetric | concurrent | 2 / 2 | 0 | 415.691 | 419.040 | 402.021 | 402.990 | 101.313 | 401.755 | 101.301 |

Median is the middle value (mean of the middle two for even n). P95 is sorted value at one-based rank ceil(0.95 × n), without interpolation. summary.json includes n, mean, median, P95, min, max, and sample standard deviation (n−1; N/A for n<2) for HTTP, orchestration, both branches, and overlap; plus error rates, decision counts, and input/order counts.

## Comparisons and interpretation

Reduction = (sequential median − concurrent median) / sequential median × 100. Negative means concurrent was slower.

| Profile | HTTP median reduction | Orchestration median reduction |
| --- | ---: | ---: |
| lightweight | N/A | N/A |
| balanced | 49.590 | 50.121 |
| asymmetric | 19.648 | 20.463 |

Balanced has two 250 ms artificial waits; the theoretical wait-only estimates are 500 ms sequential and 250 ms concurrent. Asymmetric has 100/400 ms waits; estimates are 500 ms and 400 ms. Those estimates are not measurements. Inspect measured overlap and the table above to assess the actual benefit.

Lightweight has no intentional wait. Its tiny timing differences reflect scheduling and measurement noise; no meaningful speed improvement or reduction percentage is claimed. Any reductions above describe this run only, without a statistical significance claim.

## Method and limits

One synchronous POST at a time to http://localhost:18000/api/checkouts; no retries or redirects. Each request uses a fresh connection and unique key. Adjacent pairs share business inputs; the first mode alternates and profiles rotate. All warm-ups precede measured attempts. The ten-case corpus covers both presets, threshold boundaries, foreign billing, and both risk triggers. Default 50 trials give five observations per case per mode/profile.

HTTP uses client perf_counter_ns from immediately before sending through full body receipt, excluding JSON parsing. Server orchestration_ms excludes HTTP, validation, persistence, and trace serialization. Branch offsets use the server's single monotonic origin. No clocks are subtracted across processes. Sequential overlap must be zero and delayed concurrent intervals must overlap. Trace arithmetic tolerance is 0.000001 ms (1 ns). Numeric fields must be finite, nonnegative numbers.

These are artificial simulated waits, never genuine issuer-network or fraud-processing measurements. This is per-checkout latency, not throughput, CPU parallelism, a multi-client load test, or a production reliability estimate. Local OS/container scheduling, connection setup, and database work affect HTTP latency. The host is not isolated and no confidence interval is claimed. Technical fault and cancellation paths are verified by regression tests separately, without public fault injection in this experiment.

Environment, source hashes, full configuration, corpus, and key prefix: [metadata.json](metadata.json). Raw responses and every submitted trial: [trials.csv](trials.csv). Unrounded statistics: [summary.json](summary.json). Reproduction and safe record removal: [benchmark guide](../../README.md).

## Failures

No early stop.
No failed submitted samples.
