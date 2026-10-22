# AsaanPay

An educational synthetic payment-orchestration simulator for a DevOps architecture assignment. Planned stack: one FastAPI application, PostgreSQL, HTML/CSS/vanilla JS, and Docker Compose. No real financial transactions or payment-provider integrations occur.

Milestone 1 implements application startup, an asyncpg connection pool, `GET /api/health`, the initial one-table schema, containers, and verification tests. Milestone 2 adds deterministic issuer/risk engines and a pure decision coordinator. Milestone 3 adds sequential/concurrent orchestration with artificial waits and measured traces. Checkout endpoints, persistence operations, and UI are **not implemented**. See [architecture](docs/architecture.md) for contracts, decision matrix, state machine, benchmark methodology, tradeoffs, future file tree, and seven-day milestones.

## Start locally with Docker Compose

Prerequisites: Docker Engine/Desktop running and Docker Compose supporting `up --wait`. Run commands from the repository root. Create `.env` only if it does not already exist:

```sh
[ -f .env ] || cp .env.example .env
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps
curl --fail-with-body http://localhost:8000/api/health
```

If port 8000 is already occupied, start with `API_PORT=18000 docker compose up --build -d --wait` and verify `http://localhost:18000/api/health`, or set `API_PORT=18000` in `.env` for subsequent runs.

Expected HTTP 200: `{"status":"ok","database":"connected"}`. If you change `API_PORT` in `.env`, use that port in host curl commands. The API is bound to localhost; PostgreSQL is accessible inside Compose. Use exactly one API instance and one Uvicorn worker.

PostgreSQL must become healthy before the API starts. The lifespan creates the pool and closes it on shutdown. Initial database connection/configuration failure stops API startup with a clear message. After startup, every health request runs `SELECT 1`; an unavailable database returns HTTP 503 with `{"status":"unhealthy","database":"unavailable"}`. The query and pool wait are bounded to three seconds. Health does not verify table existence.

## Verify Milestones 1–3

```sh
docker compose exec api python -m unittest discover -s tests -v
docker compose exec -e RUN_DB_TESTS=1 api python -m unittest discover -s tests -v
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d payment_attempts"'
docker compose logs --tail=100 api db
```

The default suite covers orchestration ordering/overlap, real branch timeouts, cancellation/task cleanup, deterministic engine rules, the full decision matrix, typed technical failures/timeouts, and repeated results. It also mocks database access and checks health success/failure/recovery, startup failure, pool cleanup, and absent checkout routes. The opt-in live test connects to PostgreSQL, verifies schema defaults, and checks unique-key/positive-amount constraints using fixtures rolled back afterward. Skipped live tests are not evidence of database connectivity.

Optional outage check on this local sandbox:

```sh
docker compose stop db
curl -i http://localhost:8000/api/health
docker compose start db
# Once PostgreSQL is healthy again, expect HTTP 200:
curl -i http://localhost:8000/api/health
```

Run the unit suite without Docker using Python 3.13:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Shutdown and reset

Stop and remove containers while preserving the named database volume:

```sh
docker compose down
```

`db/schema.sql` is mounted into PostgreSQL's initialization directory. It runs only when the database data directory is empty. Initialization scripts **do not automatically rerun on existing volumes**; rebuilding the API or restarting PostgreSQL does not update the schema. Changing `.env` credentials also does not rewrite existing database users.

For an intentional fresh local sandbox only, the following **deletes this Compose project's database volume and all its attempts**. Back up anything needed first. This reset is never performed automatically:

```sh
docker compose down --volumes
docker compose up --build -d --wait
```

For existing data that must be retained, review and apply an explicit SQL change instead of resetting. There is no ORM or Alembic.

## Resolved future checkout behavior

`POST /api/checkouts` will wait for execution and return 201 with the terminal state. Same-key identical normalized requests return 200 when finished or 202 when unfinished; changed payloads return 409. `GET /api/checkouts/{id}` will retrieve attempts or return 404. Invalid inputs return 422. These routes remain planned only.

`Idempotency-Key` identifies an immutable SHA-256 fingerprint of canonical `amount_pkr`, `card_preset`, `billing_country`, `execution_mode`, and `workload_profile`. Database transactions will remain closed during simulator waits. Merchant country is PK. Presets are approved/declined; risk is VERIFY for amount >= 50000 PKR or non-PK billing, otherwise CLEAR. These are fictional sandbox rules. Amounts use NUMERIC(14,2); canonical decimal formatting and validation are specified in the architecture.

| Issuer | Risk | State |
| --- | --- | --- |
| APPROVED | CLEAR | SUCCEEDED |
| APPROVED | VERIFY | VERIFICATION_REQUIRED |
| DECLINED | Either valid result | DECLINED |
| Technical failure/timeout | Any | ERROR |

Both sequential and concurrent modes must produce identical decisions. Per-branch timeout is two seconds. Workload profiles use explicitly **artificial waits**: lightweight 0/0 ms, balanced 250/250 ms, asymmetric 100/400 ms. Benchmarks will measure orchestration/API latency, actual branch overlap, correctness, median, and P95 using paired deterministic inputs and fresh keys.

Future startup recovery may mark unfinished attempts ERROR with an interruption reason after database connectivity is established, without rerunning operations. **This simple policy is unsafe for multiple independent API instances.** Recovery writes are not part of Milestone 1. Milestone 3 is complete; Milestone 4 has not begun.

## Milestone 2 interfaces

- `await simulate_issuer(card_preset)` in `app/simulators.py` accepts only `approved` or `declined`, returning the `IssuerResult` enum. Unsupported values raise `ValueError`.
- `await screen_risk(amount_pkr, billing_country)` accepts an exact `Decimal` amount and a two-letter ASCII country code, normalized to uppercase. Invalid inputs are rejected. It returns an immutable `RiskResult` containing status, ordered reason codes (`HIGH_VALUE`, then `CROSS_BORDER`), and a readable explanation. Both triggered reasons are retained.
- `coordinate_decision(issuer, risk)` in `app/coordinator.py` is synchronous and pure. It returns an immutable `Decision` with state, reason code, and readable reason. Explicit `BranchFailure.TECHNICAL_ERROR` or `BranchFailure.TIMEOUT` markers take precedence over business results, including declines. Missing/invalid results produce ERROR.

The async engines return immediately: they perform no I/O, sleeps, task scheduling, or database access. Failure-marker tests represent technical failures without adding fault injection or real timeout execution. Milestone 3 adds exception capture, deadlines, and scheduling around these unchanged engines. The schema and dependencies are unchanged.

## Milestone 3 orchestration

`await orchestrate_payment(amount_pkr, card_preset, billing_country, execution_mode, workload_profile="lightweight")` in `app/orchestration.py` accepts the same synthetic inputs as the engines. Amounts remain `Decimal`. Sequential mode awaits issuer, then risk. Concurrent mode starts both wrappers inside `asyncio.TaskGroup` and joins them before calling the same pure coordinator.

Each branch has a default two-second timeout covering its artificial `asyncio.sleep` and engine call. Both branches are awaited even after an ordinary branch failure; failures/timeouts become the existing typed failure markers. Genuine `CancelledError` propagates, and owned tasks are awaited during cleanup. Internal async-function injection and a timeout override support tests; there is no HTTP fault-injection API.

The result exposes `issuer_result`, `risk_result`, `outcome` (state/reason code/readable reason), `explanation`, `decision` (None for ERROR), and `timing_trace`. The JSON-compatible trace uses a single `perf_counter_ns` origin. Branch start/end offsets, durations, final decision offset, total orchestration duration, and interval overlap are expressed in milliseconds without rounding. It includes configured artificial delays and completion/error status. See [trace format](docs/architecture.md#milestone-3-timing-trace) for exact fields.

Run one real local measurement without PostgreSQL:

```sh
.venv/bin/python - <<'PYTHON'
import asyncio
import json
from decimal import Decimal
from app.orchestration import orchestrate_payment
result = asyncio.run(orchestrate_payment(Decimal("100"), "approved", "PK", "concurrent", "balanced"))
print(result.outcome.state, result.explanation)
print(json.dumps(result.timing_trace, indent=2))
PYTHON
```

All three profiles are implemented: lightweight 0/0 ms, balanced 250/250 ms, asymmetric 100/400 ms. These are **artificial simulated service waits**, not actual issuer-network or fraud-computation measurements. `asyncio` lets waiting branches overlap on one event loop; it does not inherently parallelize CPU work. No benchmark framework or checkout persistence is added in this milestone.
