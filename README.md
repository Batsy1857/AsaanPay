# AsaanPay

An educational synthetic payment-orchestration simulator for a DevOps architecture assignment. Stack: one FastAPI application, PostgreSQL, HTML/CSS/vanilla JS, and Docker Compose. No real financial transactions or payment-provider integrations occur.

Milestone 1 implements application startup, an asyncpg connection pool, `GET /api/health`, the initial one-table schema, containers, and verification tests. Milestone 2 adds deterministic issuer/risk engines and a pure decision coordinator. Milestone 3 adds sequential/concurrent orchestration with artificial waits and measured traces. Milestone 4 integrates persistent POST/GET checkout resources and idempotency. Milestone 5 adds a responsive checkout UI, retry-safe API integration, measured timelines, and an explicit two-mode comparison. Milestone 6 adds controlled HTTP benchmarking and reliability verification; see the [benchmark guide](benchmarks/README.md) and [actual results](benchmarks/results/milestone6-full/report.md). See [architecture](docs/architecture.md) for contracts, decision matrix, state machine, benchmark methodology, tradeoffs, file tree, and seven-day milestones.

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

## Use the checkout sandbox

Open **http://localhost:18000/** when using `API_PORT=18000` (otherwise use your configured port). FastAPI serves the page and `/static/` assets; the browser uses relative `/api/` URLs. No build step, CDN, external images, browser framework, or extra container is needed.

The centered checkout is the primary interface. Milestone 5.3 uses one card: the form is replaced by real processing feedback, then the final outcome. **Make Another Payment** restores the selected inputs; an uncertain request instead offers **Retry same attempt** with its original identity. **Architecture Explorer** sits below it, collapsed on every page load. Open its heading to access **Simulation Settings**, execution evidence, and comparison. Only explicit toggling changes its open state; submitting, retrying, and receiving results never open it. Closing the panel retains selected settings and completed results.

1. Keep **PKR 7,500**, **Approved**, and **Pakistan**. Choose **Sequential** and **Balanced**, then **Pay in sandbox**. Expect SUCCEEDED, issuer APPROVED, risk CLEAR, and nonoverlapping measured bars.
2. Click **Compare with other mode**. This explicitly creates a separate concurrent attempt with a fresh key and otherwise identical inputs. One **Execution Comparison** container replaces the single-run view, with both recorded timelines and a shared summary. The customer card retains the original payment outcome. Balanced concurrent runs should show measured overlap. The percentage is `(sequential − concurrent) / sequential × 100`; the header shows lower or higher orchestration time for this pair, with a neutral result for equal times. The interpretation below retains outcome matching and the illustrative-pair limitation. This is an illustrative two-attempt comparison, **not a formal benchmark**.
3. Click **Make Another Payment** for a new checkout; use **50,000** or a non-PK billing country to see VERIFICATION_REQUIRED. Choose the **Declined** preset to see DECLINED. Technical ERROR is displayed separately and never presented as a business decline. There are no public fault-injection controls.
4. Try **Lightweight** and **Asymmetric**. The timeline uses actual stored start/end offsets, branch durations, total orchestration duration, and measured overlap. Comparison columns share one padded millisecond axis derived from the larger recorded total, so bar widths are directly comparable. They appear side by side on desktop and stack on mobile. Issuer bars are muted blue (#6796BC) and risk bars muted green (#599879); bold white duration labels sit directly on long bars, with dark text outside short ones without changing their measured widths. Expand **Exact recorded offsets & artificial waits** for unrounded values. Tiny durations retain fractional milliseconds, with no invented minimum bar width. Missing or malformed traces show **Timing unavailable**.
5. Check 1440, 1024, 768, 390 and 320 px widths; comparison columns stack on narrow screens inside the same outer container. Tab through inputs, radio groups, submit, comparison, and disclosure controls. Pending requests use an ordinary loading message, never simulated progress.

All configured waits are **artificial external I/O delays**, not real payment-network latency or fraud-computation measurements. Orchestration totals exclude HTTP and database work. The current stored API response exposes risk status and the final explanation, but not individual `HIGH_VALUE`/`CROSS_BORDER` reason codes; the UI explicitly indicates missing rule details instead of reconstructing them. Fictional rules remain available in the explanatory disclosure.

New checkouts and explicit comparisons get fresh `crypto.randomUUID()` keys. Controls lock while pending to prevent duplicate submissions. The UI maps 201/200/202 to created/replayed/processing resources and always reads `state` for the outcome. A processing resource is polled every second for up to 30 seconds, with individual HTTP requests bounded to 10 seconds (the initial request has its own budget). After an uncertain response, 503, or exhausted polling period, **Retry same attempt** reuses the original payload/key, or retrieves the known ID. 409/422 are shown as definitive POST rejections without automatic resubmission.

An unresolved attempt is saved in this tab's `sessionStorage` before POST and survives reload. Reload never automatically submits. If browser storage is unavailable, a notice asks you to keep the tab open; retry still works in memory. Do not close/clear the tab while resolving uncertainty: no account/history or cross-device recovery is provided. Changing form values cannot silently replace an unresolved attempt.

### Browser and UI verification

The Python suite contains three static-serving checks in addition to all 47 Milestone 4 tests. Run live database tests with the existing command below. To also run lightweight JavaScript tests and actual browser smoke, use an **already installed Chrome/Chromium** and a running local sandbox:

```sh
API_PORT=18000 docker compose up --build -d --wait
curl --fail-with-body http://localhost:18000/api/health
curl --fail --output /dev/null http://localhost:18000/
docker compose exec -T -e RUN_DB_TESTS=1 api python -m unittest discover -s tests -v
python3 tests/run_frontend.py --url http://localhost:18000
# If Chrome is elsewhere:
# python3 tests/run_frontend.py --url http://localhost:18000 --chrome /path/to/chromium
```

The browser runner uses a temporary profile and localhost-only test server, then closes both. It needs no npm packages or browser driver. The 43 checks cover comparison-header percentages and unavailable outcomes, Explorer timing formatting and precision, responsive typography, the single-card state transitions, form restoration, collapsed Explorer, unified comparison, all five requested viewport widths, client status handling, idempotency/retry/polling, decimal normalization, calculations against captured real traces, comparison identity, real checkout/compare results, desktop/mobile layout, and safe error rendering. It creates a few synthetic attempts in the sandbox and preserves existing data. The technical ERROR and network-failure UI cases use browser-local response fixtures; they do not add backend fault controls. `tests/fixtures/timing_traces.json` contains actual local orchestrator observations for all six mode/profile pairs, used only by tests, never by the live UI.

For a manual retry check, open browser developer tools, set Network to **Offline**, submit a valid checkout, and confirm **Retry same attempt** appears with inputs locked. Restore the network and retry; inspect POST headers/body to verify that the same key and inputs are sent. Reload while offline after an uncertain submission to check restoration. A server response can already have committed even when the browser fails to receive it, which is why retries must keep their key.

## Verify Milestones 1–6

```sh
docker compose exec api python -m unittest discover -s tests -v
docker compose exec -e RUN_DB_TESTS=1 api python -m unittest discover -s tests -v
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d payment_attempts"'
docker compose logs --tail=100 api db
```

The default suite covers orchestration ordering/overlap, real branch timeouts, cancellation/task cleanup, deterministic engine rules, the full decision matrix, typed technical failures/timeouts, and repeated results. It also mocks database access and checks health success/failure/recovery, startup failure, pool cleanup, and checkout input validation. Checkout integration tests cover business outcomes, exact money parsing, replay/conflict behavior, gated duplicate races, cancellation, durable traces, and storage failure responses. The opt-in live tests connect to PostgreSQL and verify schema constraints and checkout behavior. Schema fixtures are rolled back; checkout tests use unique keys and delete only their own records. The tests do not run global startup recovery against an already-running database. Skipped live tests are not evidence of database connectivity.

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

## Controlled benchmark (Milestone 6)

Use the already running local application and finish regression tests first:

```sh
python3 scripts/benchmark.py --url http://localhost:18000 --trials 2 --warmups 1
python3 scripts/benchmark.py --url http://localhost:18000 --trials 50 --warmups 5
```

Each invocation creates a new directory under `benchmarks/results/` with raw CSV/JSONL, summary JSON, metadata, and a Markdown report. One request is outstanding at a time, with fresh `asaanpay-bench-...` keys and balanced mode ordering. Warm-ups are excluded from latency statistics; failures are retained and cause a nonzero exit status. HTTP and orchestration timings are reported separately. All waits are artificial, and tiny lightweight differences are not treated as speed improvements.

See the [benchmark guide](benchmarks/README.md) for clean-start reproduction, definitions, controls, limitations, and safe removal of only exact benchmark-created keys; [verification](benchmarks/verification.md) for test outcomes; and [the full report](benchmarks/results/milestone6-full/report.md) for actual measurements. No database cleanup runs automatically.

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

## Checkout API

`POST /api/checkouts` waits for execution and return 201 with the terminal state. Same-key identical normalized requests return 200 when finished or 202 when unfinished; changed payloads return 409. `GET /api/checkouts/{id}` retrieves attempts or return 404. Invalid inputs return 422. Malformed IDs also return 422. Amounts may be JSON numbers or decimal strings; responses use fixed two-decimal strings. Extra fields are rejected.

`Idempotency-Key` identifies an immutable SHA-256 fingerprint of canonical `amount_pkr`, `card_preset`, `billing_country`, `execution_mode`, and `workload_profile`. Database transactions remain closed during simulator waits. Merchant country is PK. Presets are approved/declined; risk is VERIFY for amount >= 50000 PKR or non-PK billing, otherwise CLEAR. These are fictional sandbox rules. Amounts use NUMERIC(14,2); canonical decimal formatting and validation are specified in the architecture.

| Issuer | Risk | State |
| --- | --- | --- |
| APPROVED | CLEAR | SUCCEEDED |
| APPROVED | VERIFY | VERIFICATION_REQUIRED |
| DECLINED | Either valid result | DECLINED |
| Technical failure/timeout | Any | ERROR |

Both sequential and concurrent modes must produce identical decisions. Per-branch timeout is two seconds. Workload profiles use explicitly **artificial waits**: lightweight 0/0 ms, balanced 250/250 ms, asymmetric 100/400 ms. The benchmark measures orchestration/API latency, actual branch overlap, correctness, and distribution statistics using paired deterministic inputs and fresh keys. Defaults are 5 warm-ups plus 50 measured attempts per mode/profile (300 measured overall).

Before serving requests, startup recovery marks all CREATED/PROCESSING attempts ERROR with PROCESS_INTERRUPTED after database connectivity is established. It never reruns operations or overwrites terminal results. **This simple policy is unsafe for multiple independent API instances.** Use exactly one API process/instance, with no overlapping deployments. Milestone 6 is complete; [results and verification](benchmarks/README.md) preserve actual data, failures, environment metadata, and reproduction steps. Presentation work remains deferred.

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

All three profiles are implemented: lightweight 0/0 ms, balanced 250/250 ms, asymmetric 100/400 ms. These are **artificial simulated service waits**, not actual issuer-network or fraud-computation measurements. `asyncio` lets waiting branches overlap on one event loop; it does not inherently parallelize CPU work. Milestone 3 added no persistence; Milestone 4 now persists its unchanged results and traces. Milestone 6 measures the persisted endpoint with the standard-library runner in `scripts/benchmark.py`.

### Submit, retrieve, and replay

After `docker compose up --build -d --wait`, run the following from the project root. Set `ASAANPAY_URL=http://localhost:18000` first if using the alternate port.

```sh
ASAANPAY_URL=${ASAANPAY_URL:-http://localhost:8000}
CHECKOUT_KEY="demo-$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl --fail-with-body -sS -D /tmp/asaanpay-headers.txt \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $CHECKOUT_KEY" \
  -d '{"amount_pkr":"100.00","card_preset":"approved","billing_country":"pk","execution_mode":"concurrent","workload_profile":"balanced"}' \
  "$ASAANPAY_URL/api/checkouts" > /tmp/asaanpay-checkout.json
cat /tmp/asaanpay-headers.txt
cat /tmp/asaanpay-checkout.json
CHECKOUT_ID=$(python3 -c 'import json; print(json.load(open("/tmp/asaanpay-checkout.json"))["id"])')
curl --fail-with-body "$ASAANPAY_URL/api/checkouts/$CHECKOUT_ID"
# Same normalized payload/key: 200, same ID and stored trace, no execution.
curl -i -H 'Content-Type: application/json' -H "Idempotency-Key: $CHECKOUT_KEY" \
  -d '{"amount_pkr":100,"card_preset":"approved","billing_country":"PK","execution_mode":"concurrent","workload_profile":"balanced"}' \
  "$ASAANPAY_URL/api/checkouts"
# Changed amount under that key: 409.
curl -i -H 'Content-Type: application/json' -H "Idempotency-Key: $CHECKOUT_KEY" \
  -d '{"amount_pkr":"101.00","card_preset":"approved","billing_country":"PK","execution_mode":"concurrent","workload_profile":"balanced"}' \
  "$ASAANPAY_URL/api/checkouts"
```

The first response is 201/SUCCEEDED. Use a fresh key and amount `50000` for VERIFICATION_REQUIRED, or preset `declined` for DECLINED. Business outcomes and durably persisted ERROR all return 201 for the creating POST. A matching in-progress duplicate returns 202 with `Location` and `Retry-After: 1`; poll GET or retry the same key. GET returns the same resource shape at every state.

Responses contain `id`, `state`, `terminal`, normalized `request`, `rule_set_version`, nullable `issuer_result`/`risk_result`, `decision`, `decision_reason`, `error_reason`, `timing_trace`, and creation/start/finish timestamps. `decision_reason` is a readable business explanation; ERROR has `decision=null`, `decision_reason=null`, and a nonempty sanitized `error_reason`. Branch failures are represented by a null business branch result plus their status in the unchanged trace. The trace is stored as JSONB without recomputing or rounding timings. The key and fingerprint are not returned.

### Persistence and failure boundaries

The unique database constraint decides ownership through `INSERT ... ON CONFLICT DO NOTHING RETURNING`. Only the successful inserter transitions CREATED to PROCESSING and calls the orchestrator. Matching duplicates read stored state; differing canonical inputs return 409. Money is parsed directly as Decimal, normalized to two decimal places, country uppercased, defaults applied, and the five request fields hashed as sorted compact UTF-8 JSON. Mode/profile are part of request identity.

Every database operation is a separate autocommitted statement with a released pool connection before simulation. Final state/results/trace are written together with a PROCESSING guard. Terminal attempts, including ERROR, are never rerun by duplicates. Request fields and fingerprint are never changed by storage updates.

Database failure returns 503 instead of an invented success. A commit may have succeeded even when its acknowledgement was lost; reconcile with the same key. A failed final write may leave PROCESSING until the next exclusive startup recovery. Unexpected orchestration exceptions become a sanitized persisted ERROR when storage is available.

Request cancellation propagates through the orchestrator and its task cleanup, followed by a bounded best-effort ERROR write. No background processing or shielded cleanup task is launched. Repeated cancellation, process loss, or database outage may prevent that write; startup recovery resolves remaining unfinished rows. A client disconnect may leave the request active until it finishes, depending on the server; do not assume it guarantees cancellation or successful completion. Error responses use FastAPI's `detail` envelope and do not expose SQL, passwords, or stack traces.
