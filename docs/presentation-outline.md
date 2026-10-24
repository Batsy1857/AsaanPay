# AsaanPay: seven-slide presentation outline

Milestone 7A source material only; no slide deck is created. Suggested delivery: about 7–8 minutes, followed by the [3–4 minute UI demonstration](demo-script.md) and [technical Q&A](professor-qa.md). Timing is a rehearsal suggestion, not an observed result. Architecture statements describe current code; experiment claims refer only to the preserved Milestone 6 run.

## Slide 1 — Problem: reduce waiting without changing decisions

**Slide bullets**

- Synthetic checkout: issuer outcome plus risk screening determine one decision.
- Question: when does overlapping independent waits improve latency?
- Compare sequential and concurrent execution with identical business inputs.
- Educational sandbox: no real payment processing.

**Speaker notes:** The goal is architectural understanding, not a faster real payment product. Issuer presets and risk rules are deterministic, so we can change scheduling while holding policy constant. The experiment tests latency and correctness together. Say “artificial I/O waits” from the outset; do not describe the fixed risk rules as a production fraud engine.

**Evidence/visual:** [System context diagram](final-architecture.md#system-context), [implemented engines](../app/simulators.py).

## Slide 2 — Architecture: three tiers, two containers

**Slide bullets**

- Browser presentation → FastAPI application → PostgreSQL persistence.
- API serves HTML/CSS/JavaScript and JSON endpoints.
- One API process/worker; Compose API and database containers.
- Short SQL statements bracket orchestration; no connection held during waits.

**Speaker notes:** “Three-tier” describes responsibilities, not three microservices or three containers. The browser is on the host, and the two containers communicate over the Compose network. PostgreSQL persists identity and outcomes; it is not a queue. Reproducibility comes from the existing container setup, source/configuration records and repeatable experiment, not from an implemented Kubernetes or CI/CD platform.

**Evidence/visual:** [Deployment diagram](final-architecture.md#three-tier-deployment), [Compose](../compose.yaml), [storage](../app/storage.py).

## Slide 3 — Components: fork, wait, join, decide

**Slide bullets**

- API validates and owns the request; storage manages durable state.
- Orchestrator: sequential awaits or two tasks in `asyncio.TaskGroup`.
- Issuer and risk consume independent inputs and return typed results.
- TaskGroup joins execution; the pure coordinator applies decision policy.

**Speaker notes:** In sequential mode, issuer completes before risk starts. In concurrent mode, both wrappers can wait at the same time; the group is joined before policy runs. There is no result race: the coordinator receives both results and never selects the first finisher. Async waits release the event loop; blocking CPU work would occupy it. The pure coordinator does not create tasks, call providers or write the database.

**Evidence/visual:** [Logical fork-and-join diagram](final-architecture.md#logical-fork-and-join-execution), [orchestration](../app/orchestration.py), [coordinator](../app/coordinator.py).

## Slide 4 — Correctness: decisions, identity and failures

**Slide bullets**

- APPROVED+CLEAR → SUCCEEDED; APPROVED+VERIFY → VERIFICATION_REQUIRED.
- Valid issuer decline → DECLINED; technical failures take precedence → ERROR.
- Unique key + canonical fingerprint prevent duplicate execution.
- Two-second branch deadlines; cancellation cleanup; recovery requires one instance.

**Speaker notes:** Both branches still run after a valid decline; a risk technical failure must not be hidden. HTTP 201 means a resource was finalized, not necessarily a successful payment. Identical-key duplicates read stored state; a changed mode/profile under that key is a conflict. There is no exactly-once external-payment guarantee. Startup marks unfinished rows ERROR without rerunning them, which makes overlapping workers/deployments unsafe. Milestone 6 recorded 61 backend/benchmark and 40 frontend checks passing; those are separate from its 300 normal benchmark attempts.

**Evidence/visual:** [Decision matrix](final-architecture.md#decision-policy-after-the-join), [idempotency boundaries](final-architecture.md#persistence-and-idempotency-boundaries), [historical verification](../benchmarks/verification.md).

## Slide 5 — Experiment: actual latency and matching outcomes

**Slide bullets**

- 5 warm-ups + 50 measured attempts per mode/profile; 300 measured total.
- One request at a time; paired inputs, fresh keys and balanced mode order.
- Median HTTP reduction: **balanced 49.04%; asymmetric 20.00%**.
- **Zero failures; identical decisions across 150 mode pairs.** Lightweight has no meaningful advantage.

**Suggested slide table — actual HTTP latency, milliseconds**

| Profile | Sequential median | Concurrent median | Sequential P95 | Concurrent P95 |
| --- | ---: | ---: | ---: | ---: |
| lightweight | 6.092 | 6.528 | 13.039 | 15.501 |
| balanced | 517.967 | 263.952 | 531.401 | 281.030 |
| asymmetric | 516.814 | 413.472 | 537.468 | 433.949 |

**Speaker notes:** P95 is nearest rank, position 48 of 50. Balanced median HTTP time falls from about 518 to 264 ms; asymmetric from about 517 to 413 ms. Lightweight concurrency is slightly higher and does not show a meaningful advantage. All 300 decisions match expected policy, not just each other. These HTTP figures include persistence and transport; the UI shows server orchestration timing. A single UI comparison is illustrative and cannot reproduce group medians/P95. The [benchmark summary](benchmark-summary.md) retains the separate orchestration table and interval-overlap evidence.

**Evidence/visual:** [Preserved full report](../benchmarks/results/milestone6-full/report.md), [summary JSON](../benchmarks/results/milestone6-full/summary.json), [raw CSV](../benchmarks/results/milestone6-full/trials.csv).

## Slide 6 — Tradeoffs: what concurrency buys and what it does not

**Slide bullets**

- Artificial waits: lightweight 0/0, balanced 250/250, asymmetric 100/400 ms.
- Concurrent critical path follows the longer independent branch, plus overhead.
- Modular in-process calls avoid extra service/network operations for this study.
- Shared process and event loop; no independent scaling or CPU parallelism.

**Speaker notes:** Theoretical wait contributions are sum versus maximum: 500→250 ms for balanced and 500→400 ms for asymmetric, before overhead. These are explanations, not new observations. Measured delayed concurrent intervals actually overlap. The study did not compare microservices against a monolith, so it cannot claim microservices are slower. With only one local client and a non-isolated host, it also cannot establish throughput, production tail latency or availability. No significance test or confidence interval was reported.

**Evidence/visual:** [Architecture rationale](final-architecture.md#why-keep-async-modules-in-one-process), [experimental limitations](benchmark-summary.md#environment-and-limitations).

## Slide 7 — Conclusion: preserve policy, overlap eligible waits

**Slide bullets**

- Concurrent execution helped the two delayed workloads in this experiment.
- Correctness stayed explicit: deterministic policy, durable identity and measured traces.
- Lightweight work offered no meaningful advantage; independence matters.
- Future work: safe multi-instance ownership, provider-order contracts and capacity testing.

**Speaker notes:** Conclude narrowly: this implemented simulator demonstrates a latency benefit when independent waits can overlap. It does not prove that real authorization should run before risk checks. Real side effects, policy and provider rules would determine that order. Future capabilities are proposals, not implemented features. Transition to the existing UI demo: show equal decisions and measured intervals without promising a specific live percentage. Keep the recorded report ready if the demo cannot complete.

**Evidence/visual:** [Future work, explicitly unimplemented](final-architecture.md#future-production-work--not-implemented), [demo script and fallback](demo-script.md), [professor Q&A](professor-qa.md).

## Presenter checks

Use only the seven sections above as slide content; the supporting documents are notes/evidence. Keep “artificial waits,” “median HTTP latency,” and “single local experiment” attached to the numerical claims. Do not rename the two logical branches as deployed services, claim CPU parallelism, equate 201 with SUCCEEDED, or describe zero observed failures as guaranteed reliability. Cite the local source links; no external literature or unimplemented architectural feature is needed for these claims.
