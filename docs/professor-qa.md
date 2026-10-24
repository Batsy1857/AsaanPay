# Professor Q&A: ten technical challenges

Answers refer to implemented AsaanPay behavior and the recorded Milestone 6 experiment. Proposed production work is labeled explicitly. Use the [architecture brief](final-architecture.md) and [benchmark summary](benchmark-summary.md) for supporting diagrams and numbers.

## 1. Where exactly is the fork-and-join, and does the coordinator wait for tasks?

The concurrent orchestrator creates issuer and risk tasks inside `asyncio.TaskGroup`; group exit waits for both. It then reads their results and calls the synchronous coordinator. The execution join belongs to orchestration; the coordinator joins the meanings of the two results using a deterministic policy. Sequential mode reaches the same coordinator after two ordered awaits. “Fork” here means logical task fan-out, not OS process creation. [Implementation](../app/orchestration.py)

## 2. Does asyncio execute both branches on different CPUs, and is the timeout a hard CPU deadline?

No. The single event loop switches between tasks at cooperative awaits. `asyncio.sleep` releases it while a branch waits. CPU-bound blocking code would delay its peer and timeout handling; two async tasks alone do not provide CPU parallelism. Each implemented branch has a two-second timeout from its own start, including its wait and engine call, but cancellation must be able to run. [Implementation](../app/orchestration.py)

## 3. How can this be loosely coupled if everything runs in one process?

Separation is through explicit inputs and immutable result contracts: engines do not know mode, storage or HTTP, while the coordinator knows only results. Scheduling, persistence and UI concerns live in separate modules. This is modular coupling, not deployment independence: modules still share contracts, release lifecycle and process resources. Microservices were unnecessary to isolate the scheduling question and would add network/operational variables. [Contracts](../app/contracts.py), [component responsibilities](final-architecture.md#components-communication-and-contracts)

## 4. Can two simultaneous requests bypass idempotency, and is this exactly once?

PostgreSQL's unique key and `INSERT ... ON CONFLICT DO NOTHING RETURNING` select one original owner. A competitor reads stored state; an identical normalized payload returns 200/202, a changed payload 409. Mode/profile are fingerprinted, so comparisons need fresh keys. This prevents duplicate orchestration through this protocol, but a crash can leave work unfinished; it does not establish exactly-once external payment effects. An uncertain retry must retain its key. [Checkout API](../app/checkouts.py), [storage](../app/storage.py)

## 5. What is atomic in persistence, and why not hold a transaction for the whole checkout?

Reservation, PROCESSING transition and finalization are separate short autocommitted statements. The guarded final UPDATE atomically stores the terminal state, branch results and unchanged JSONB trace together; the whole checkout is not one transaction. Releasing the pool connection during waits avoids occupying database resources for simulated I/O. A lost final acknowledgement can still make the client uncertain even if PostgreSQL committed. [Storage](../app/storage.py), [schema](../db/schema.sql)

## 6. What happens after a branch failure, client disconnect or process crash?

Ordinary branch errors/timeouts become failure markers; both branches are awaited and technical failure yields ERROR even after a decline. Task cancellation propagates, cleans up owned work and triggers a best-effort API error write. A disconnect alone does not prove cancellation. Process loss can leave CREATED/PROCESSING rows; the next exclusive startup marks them ERROR without re-execution. Recovery has no leases or resume mechanism and is unsafe with overlapping instances. [Failure policy](final-architecture.md#failure-handling-and-recovery-limits)

## 7. If delays are artificial, what has the benchmark actually demonstrated?

It demonstrates that this implementation overlaps controlled waits and reduces per-checkout latency while preserving expected decisions. It measures real HTTP and orchestration elapsed times around synthetic workloads, not real issuer-network latency or fraud computation. Identical paired inputs, fresh keys, warm-ups and balanced order support the comparison. One local client, fixed waits and 50 samples per group cannot establish production latency, throughput or availability. [Method and limitations](benchmark-summary.md)

## 8. Why about 49% improvement for balanced but only 20% for asymmetric?

With independent waits, the theoretical waiting path changes from sum to maximum. Balanced 250/250 ms can overlap almost the full shorter wait; asymmetric 100/400 ms still waits for the 400 ms risk branch. Actual median HTTP reductions were **49.04%** and **20.00%**, calculated from recorded medians; orchestration reductions are different. HTTP adds persistence/transport overhead. Lightweight has no meaningful advantage, and one sample or P95 is not a guarantee. [Actual results](benchmark-summary.md#actual-latency-results)

## 9. Is authorizing before risk screening safe? Why run risk after an issuer decline?

Here “issuer authorization” is a side-effect-free local preset lookup, so concurrent evaluation changes no external financial state. The final decision still waits for both results. Risk runs after a decline because the policy requires a technical risk failure to override that business decline. For a real provider, risk-before-authorization requirements, side effects, provider idempotency and any reversal/reconciliation policy must be designed explicitly; this simulator does not justify reordering real authorization. [Engines](../app/simulators.py), [coordinator](../app/coordinator.py)

## 10. Can we scale by adding Uvicorn workers, replicas or one microservice per branch?

Not safely with the current startup recovery: a new instance could mark another instance's live work ERROR. Unique-key ownership does not solve that recovery race. Safe multi-instance ownership/recovery, capacity tests and resource limits would be prerequisites. Separate services might later support independent scaling or isolation if justified, but add distributed failures and operational cost. No multi-client throughput, horizontal scaling or microservice comparison was measured. [Deployment and recovery](final-architecture.md), [future work](final-architecture.md#future-production-work--not-implemented)
