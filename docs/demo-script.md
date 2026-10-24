# AsaanPay live demo script

Milestone 7A rehearsal instructions for the existing UI; these actions have **not** been executed as part of documentation preparation. Allow about 3–4 minutes. The normal script creates four synthetic attempts: sequential, concurrent comparison, verification and decline. It uses no real payment data.

## Prepare before presenting

Use the existing running sandbox. The Milestone 6 address was `http://localhost:18000/`; substitute the configured API port if different. Keep the [recorded full report](../benchmarks/results/milestone6-full/report.md), [benchmark summary](benchmark-summary.md), and [architecture diagrams](final-architecture.md) open in a local Markdown viewer for fallback.

An optional read-only preflight from the repository root is:

```sh
curl --fail-with-body http://localhost:18000/api/health
```

Expected health response: HTTP 200 with `{"status":"ok","database":"connected"}`. It checks connectivity, not table completeness. If unavailable, use the fallback below instead of restarting services during the presentation. If setup is needed beforehand, follow the existing [startup guide](../README.md#start-locally-with-docker-compose) separately, with one API instance and no in-flight work.

On page load, **Architecture Explorer** is collapsed. Click that heading to expand it; submissions do not expand it automatically. If an unresolved attempt is restored, use **Retry same attempt** to reconcile it before starting; do not clear browser storage or abandon its key just to reset the demo. If a previous terminal result is shown, click **Make Another Payment**.

## 1. Balanced sequential checkout

**Exact UI actions**

1. Set **Payment amount** to `7500.00`.
2. Select **Approved** under **Sandbox card preset**.
3. Set **Billing country** to **Pakistan (PK)**.
4. Expand **Architecture Explorer**. Under **Simulation Settings**, select **Sequential** in **Execution mode** and **Balanced · 250 / 250 ms** in **Artificial workload**.
5. Click **Pay in sandbox** and wait for a terminal result; leave controls alone while processing.

**Show and say:** The checkout card should say **Payment Approved**. Explorer's execution state should be **SUCCEEDED**, with **Issuer APPROVED** and **Risk CLEAR**. Point to issuer followed by risk with no measured interval overlap. Expand **Exact recorded offsets & artificial waits** to show actual starts, ends, durations and total. “These configured waits are artificial; the bars use recorded orchestration intervals, excluding HTTP and database time.” Do not announce an exact expected duration.

## 2. Compare with concurrent, keeping inputs fixed

Click **Compare with other mode** in the open Explorer after the first result. Do not change inputs or manually submit a second form. Wait for **Execution Comparison** to show Sequential and Concurrent sections on a shared millisecond scale.

**Expected evidence:** Both execution states are **SUCCEEDED**, with **Issuer APPROVED** and **Risk CLEAR**. The concurrent issuer/risk intervals should overlap for this delayed workload. Point to the distinct **Attempt ID** values and identical **Synthetic inputs** and **Artificial workload**. The customer card retains the original outcome; the Explorer shows both attempts. The comparison uses a fresh key because mode is part of request identity.

**Say:** “The scheduling changed; the policy and inputs did not. The displayed percentage compares these two orchestration measurements. The separate 49.04% benchmark result is a reduction in median HTTP latency over repeated trials.” Read the live values only after they appear. If concurrent is unexpectedly slower in this pair, report that observation honestly and distinguish it from the recorded distribution; do not repeat submissions until a favorable value appears.

## 3. Verification required

Click **Make Another Payment**. Set **Payment amount** to `50000.00`, keep **Approved** and **Pakistan (PK)**, and explicitly select **Concurrent** and **Balanced · 250 / 250 ms** in Explorer. Click **Pay in sandbox**.

**Expected evidence:** The card says **Sandbox Verification Required**; Explorer shows **VERIFICATION_REQUIRED**, **Issuer APPROVED**, **Risk VERIFY**. “The amount meets the fictional PKR 50,000 threshold. This is a terminal sandbox decision, not a completed customer-verification workflow.” The UI does not expose individual stored risk reason codes; open **How the sandbox decides** to explain the rule, rather than claiming a reason code was returned.

If time permits, an alternative extra demonstration is `7500.00` with **United States (US)** and **Approved**, which also yields verification because billing is outside PK. It creates an additional attempt and is not required for the four-attempt script.

## 4. Issuer decline

Click **Make Another Payment**. Keep `50000.00` and **Pakistan (PK)**, select the **Declined** preset, and leave **Concurrent** / **Balanced · 250 / 250 ms** selected. Click **Pay in sandbox**.

**Expected evidence:** The card says **Payment Declined**; Explorer shows **DECLINED**, **Issuer DECLINED**, **Risk VERIFY**. “Risk still executes. A valid issuer decline determines this business outcome, but a technical failure would take precedence and produce ERROR.” Do not call this decline a timeout or a failed benchmark sample.

Close with: “Independent waits can overlap while one coordinator preserves the decision rules. The repeated experiment showed 49.04% balanced and 20.00% asymmetric median HTTP reductions, with no meaningful lightweight advantage.” Use the [recorded median/P95 table](benchmark-summary.md#actual-latency-results), not the live pair, to support those percentages.

## If the live demo fails

| What happens | Action and explanation |
| --- | --- |
| Page/API unavailable | Say the live system is unavailable. Show the preserved report, three-tier diagram and fork-and-join diagram locally. Do not present recorded data as a fresh execution. |
| Uncertain request or **Retry same attempt** | Keep the tab/key and inputs intact. Retry once if connectivity is restored; this retrieves the same attempt using its known ID or original key. If unresolved, stop live submissions and switch to recorded evidence. |
| **Payment Processing Error** / state ERROR | Explain it is a technical outcome, separate from issuer decline. Show any returned sanitized reason; do not describe it as successful execution or invent a trace. |
| **Timing unavailable** | The UI cannot validate a trace. Use the recorded full-run CSV/report to explain measured intervals; do not redraw bars from configured delays. |
| Different timing or a slower concurrent pair | Keep the result visible and identify it as a single observation affected by overhead/noise. Refer to the recorded median and P95 without promising a live speedup. |

For an offline evidence walkthrough, show [trials.csv](../benchmarks/results/milestone6-full/trials.csv), filter `phase=measured`, and choose both rows with one balanced `pair_id` (for example `measured-balanced-0001`). Compare state, branch results, normalized inputs and `response_json` traces; explain the distinct keys/IDs. Then show the full report's 150 matching pairs and zero observed failures. These are authentic saved observations, not a simulated UI replay.

Do not restart/reset the database, delete records, clear unresolved browser state, run the benchmark again, or inject faults as a demo repair. The existing UI has no public fault-injection controls. For technical-failure discussion, cite the [recorded regression verification](../benchmarks/verification.md) and [Q&A](professor-qa.md).

UI labels and behavior were checked against [index.html](../app/static/index.html), [app.js](../app/static/app.js) and [core.js](../app/static/core.js).
