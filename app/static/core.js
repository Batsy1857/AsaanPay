/* Pure presentation calculations and a small, retry-safe API client. No DOM. */
(function (root) {
  "use strict";
  const states = Object.freeze({
    SUCCEEDED: { title: "Sandbox payment successful", tone: "success", symbol: "✓" },
    DECLINED: { title: "Declined by simulated issuer", tone: "declined", symbol: "−" },
    VERIFICATION_REQUIRED: { title: "Sandbox verification required", tone: "verify", symbol: "!" },
    ERROR: { title: "Technical execution failure", tone: "error", symbol: "!" }
  });
  const modes = ["sequential", "concurrent"];
  const profiles = ["lightweight", "balanced", "asymmetric"];

  function normalize(input) {
    // Keep money as decimal text; never round a binary float into a request.
    const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(String(input.amount_pkr).trim());
    if (!match) throw new Error("Enter a positive amount with at most two decimal places.");
    const whole = match[1].replace(/^0+(?=\d)/, "");
    const fraction = (match[2] || "").padEnd(2, "0");
    if (whole.length > 12 || (whole === "0" && fraction === "00")) {
      throw new Error("Amount must be between PKR 0.01 and PKR 999,999,999,999.99.");
    }
    const country = String(input.billing_country).toUpperCase();
    if (!/^[A-Z]{2}$/.test(country) || !["approved", "declined"].includes(input.card_preset)
        || !modes.includes(input.execution_mode) || !profiles.includes(input.workload_profile)) {
      throw new Error("Choose a valid sandbox preset, country, execution mode and workload.");
    }
    return Object.freeze({ amount_pkr: whole + "." + fraction, card_preset: input.card_preset,
      billing_country: country, execution_mode: input.execution_mode, workload_profile: input.workload_profile });
  }

  function otherMode(input) {
    return normalize({ ...input, execution_mode: input.execution_mode === "sequential" ? "concurrent" : "sequential" });
  }

  function traceLayout(trace) {
    // Refuse incomplete/inconsistent evidence instead of fabricating a chart.
    const number = (x) => typeof x === "number" && Number.isFinite(x) && x >= 0;
    const near = (a, b) => Math.abs(a - b) <= 1e-7 * Math.max(1, a, b);
    if (!trace || trace.artificial_delays !== true
        || trace.measurement !== "synthetic_orchestration_not_payment_network_latency"
        || !modes.includes(trace.execution_mode) || !profiles.includes(trace.workload_profile)
        || !number(trace.orchestration_ms) || !number(trace.decision_ms) || !number(trace.overlap_ms)
        || trace.decision_ms > trace.orchestration_ms) return null;
    const total = trace.orchestration_ms;
    const rows = [];
    for (const key of ["issuer", "risk"]) {
      const b = trace.branches && trace.branches[key];
      if (!b || ![b.start_ms, b.end_ms, b.duration_ms, b.artificial_delay_ms].every(number)
          || b.end_ms < b.start_ms || b.end_ms > trace.decision_ms
          || !near(b.duration_ms, b.end_ms - b.start_ms)
          || !["completed", "technical_error", "timeout"].includes(b.status)) return null;
      rows.push({ key, ...b, left: total ? b.start_ms / total * 100 : 0,
        width: total ? (b.end_ms - b.start_ms) / total * 100 : 0 });
    }
    const overlap = Math.max(0, Math.min(rows[0].end_ms, rows[1].end_ms) - Math.max(rows[0].start_ms, rows[1].start_ms));
    if (!near(overlap, trace.overlap_ms)) return null;
    return { rows, total, overlap: trace.overlap_ms, decision: trace.decision_ms,
      ticks: [0, .25, .5, .75, 1].map((fraction) => fraction * total) };
  }

  function comparison(first, second) {
    if (!first || !second || first.state === "ERROR" || second.state === "ERROR") return null;
    const a = normalize(first.request), b = normalize(second.request);
    if (JSON.stringify(otherMode(a)) !== JSON.stringify(b) || first.state !== second.state
        || first.issuer_result !== second.issuer_result || first.risk_result !== second.risk_result) return null;
    const ta = traceLayout(first.timing_trace), tb = traceLayout(second.timing_trace);
    if (!ta || !tb) return null;
    const sequential = a.execution_mode === "sequential" ? ta.total : tb.total;
    const concurrent = a.execution_mode === "concurrent" ? ta.total : tb.total;
    return { sequential, concurrent, reduction: sequential > 0 ? (sequential - concurrent) / sequential * 100 : null };
  }

  function milliseconds(value) {
    // Significant digits keep tiny lightweight timings visible. Full values remain in titles.
    return Number(value.toPrecision(6)).toString() + " ms";
  }

  class ApiError extends Error {
    constructor(message, status = null) { super(message); this.status = status; }
  }

  class CheckoutClient {
    constructor({ fetcher = (...args) => fetch(...args), keygen = () => crypto.randomUUID(),
      sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)), now = () => performance.now(),
      onChange = () => {}, pending = null, pollMs = 1000, waitMs = 30000, requestMs = 10000 } = {}) {
      Object.assign(this, { fetcher, keygen, sleep, now, onChange, pollMs, waitMs, requestMs });
      this.pending = pending;
      this.busy = false;
    }
    save() { this.onChange(this.pending); }
    async start(input, base = null) {
      if (this.busy || this.pending) throw new ApiError("Resolve the pending attempt before starting another.");
      this.pending = { key: this.keygen(), payload: normalize(input), id: null, base };
      this.save();
      return this.retry();
    }
    async request(url, options, timeoutMs = this.requestMs) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await this.fetcher(url, { ...options, signal: controller.signal });
        if (![200, 201, 202].includes(response.status)) {
          const messages = {
            409: "This idempotency key belongs to different inputs. No new attempt was started. Submit again to use a new key.",
            422: "The API rejected these inputs. Check the amount and sandbox selections before submitting again.",
            404: "The attempt could not be found. Its outcome is uncertain; keep this page and retry.",
            503: "The database is unavailable. The outcome is uncertain; retry the same attempt."
          };
          throw new ApiError(messages[response.status] || "The API could not confirm the outcome. Retry the same attempt.", response.status);
        }
        const body = await response.json();
        if (!body || typeof body.id !== "string" || !body.id
            || (!Object.hasOwn(states, body.state) && !["CREATED", "PROCESSING"].includes(body.state))) {
          throw new ApiError("The API returned an unrecognized response. Retry the same attempt.");
        }
        return { body, status: response.status };
      } catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError("Connection interrupted or response unreadable. The outcome is uncertain; retry the same attempt.");
      } finally { clearTimeout(timer); }
    }
    async retry() {
      if (this.busy || !this.pending) throw new ApiError("No attempt is ready for retry.");
      this.busy = true;
      const saved = this.pending;
      const posting = !saved.id;
      try {
        let reply;
        try {
          reply = posting
            ? await this.request("/api/checkouts", { method: "POST", headers: {
              "Content-Type": "application/json", "Idempotency-Key": saved.key }, body: JSON.stringify(saved.payload) })
            : await this.request("/api/checkouts/" + encodeURIComponent(saved.id), { method: "GET" });
        } catch (error) {
          // Only a definitive POST rejection releases these inputs for a new attempt.
          if (posting && [409, 422].includes(error.status)) { this.pending = null; this.save(); }
          throw error;
        }
        const receipt = posting ? ({ 201: "New attempt", 200: "Stored replay", 202: "Processing duplicate" })[reply.status] : "Retrieved attempt";
        saved.id = reply.body.id;
        this.save();
        const deadline = this.now() + this.waitMs;
        while (!Object.hasOwn(states, reply.body.state)) {
          if (this.now() + this.pollMs >= deadline) throw new ApiError("Still processing after the waiting period. Retry to retrieve this same attempt.");
          await this.sleep(this.pollMs);
          reply = await this.request("/api/checkouts/" + encodeURIComponent(saved.id), { method: "GET" }, Math.min(this.requestMs, deadline - this.now()));
        }
        this.pending = null;
        this.save();
        return { attempt: reply.body, base: saved.base, receipt };
      } finally { this.busy = false; }
    }
  }

  root.AsaanPay = Object.freeze({ states, normalize, otherMode, traceLayout, comparison, milliseconds, CheckoutClient, ApiError });
})(globalThis);
