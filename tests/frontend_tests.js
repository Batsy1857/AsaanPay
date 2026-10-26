/* Dependency-free browser tests: isolated client fixtures, then real UI + API smoke. */
(async function () {
  "use strict";
  const C = window.AsaanPay;
  const results = [];
  const assert = (value, message = "Assertion failed") => { if (!value) throw new Error(message); };
  const equal = (a, b) => assert(JSON.stringify(a) === JSON.stringify(b), JSON.stringify(a) + " != " + JSON.stringify(b));
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const payload = { amount_pkr: "7500.00", card_preset: "approved", billing_country: "PK", execution_mode: "concurrent", workload_profile: "balanced" };
  const attempt = (state = "SUCCEEDED") => ({ id: "test-attempt", state, terminal: true, request: payload });
  const response = (status, body) => ({ status, json: async () => body });
  const fails = async (fn) => { let error; try { await fn(); } catch (e) { error = e; } assert(error, "Expected rejection"); return error; };
  async function test(name, fn) {
    try { await fn(); results.push("PASS " + name); }
    catch (e) { results.push("FAIL " + name + ": " + e.stack); }
    document.getElementById("test-results").textContent = results.join("\n");
  }
  const fixture = await (await fetch("/tests/fixtures/timing_traces.json")).json();

  await test("all terminal states map to distinct presentation, including technical ERROR", () => {
    equal(Object.keys(C.states), ["SUCCEEDED", "DECLINED", "VERIFICATION_REQUIRED", "ERROR"]);
    assert(C.states.ERROR.title.includes("Technical"));
    assert(C.states.ERROR.title !== C.states.DECLINED.title);
  });
  await test("canonical decimal inputs and selected mode survive normalization", () => {
    equal(C.normalize({ ...payload, amount_pkr: "007500.5", billing_country: "pk", execution_mode: "sequential" }),
      { ...payload, amount_pkr: "7500.50", execution_mode: "sequential" });
    equal(C.normalize({ ...payload, amount_pkr: "999999999999.99" }).amount_pkr, "999999999999.99");
  });
  await test("invalid amounts and unsupported controls rejected", async () => {
    for (const amount of ["0", "-1", "NaN", "1.001", "1000000000000", "1e3", ""]) {
      await fails(() => C.normalize({ ...payload, amount_pkr: amount }));
    }
    await fails(() => C.normalize({ ...payload, execution_mode: "parallel" }));
  });
  await test("actual trace fixtures preserve offsets, durations and proportional bars", () => {
    for (const trace of Object.values(fixture.traces)) {
      const layout = C.traceLayout(trace);
      assert(layout);
      equal(layout.total, trace.orchestration_ms);
      equal(layout.overlap, trace.overlap_ms);
      for (const row of layout.rows) {
        equal(row.duration_ms, trace.branches[row.key].duration_ms);
        equal(row.left, row.start_ms / layout.total * 100);
        equal(row.width, (row.end_ms - row.start_ms) / layout.total * 100);
      }
      if (trace.execution_mode === "sequential") equal(layout.overlap, 0);
      if (trace.execution_mode === "concurrent" && trace.workload_profile !== "lightweight") assert(layout.overlap > 0);
    }
  });
  await test("lightweight fractions are retained, no invented minimum duration", () => {
    const trace = fixture.traces.lightweight_sequential;
    const layout = C.traceLayout(trace);
    assert(layout.rows[0].duration_ms > 0 && layout.rows[0].duration_ms < 1);
    assert(C.milliseconds(layout.rows[0].duration_ms) !== "0 ms");
    const zero = clone(trace);
    Object.assign(zero.branches.issuer, { end_ms: zero.branches.issuer.start_ms, duration_ms: 0 });
    equal(C.traceLayout(zero).rows[0].width, 0);
  });
  await test("missing, incomplete, inconsistent and nonnumeric traces are unavailable", () => {
    for (const trace of [null, {}, { branches: {} }]) equal(C.traceLayout(trace), null);
    for (const change of [t => t.branches.issuer.end_ms = -1, t => t.orchestration_ms = Infinity,
      t => t.branches.risk.duration_ms = "250", t => t.overlap_ms = 999,
      t => t.decision_ms = t.orchestration_ms + 1, t => delete t.branches.risk]) {
      const trace = clone(fixture.traces.balanced_concurrent); change(trace); equal(C.traceLayout(trace), null);
    }
  });
  await test("201 uses state, never implies payment success", async () => {
    for (const state of Object.keys(C.states)) {
      const client = new C.CheckoutClient({ keygen: () => "key", fetcher: async () => response(201, attempt(state)) });
      equal((await client.start(payload)).attempt.state, state);
      equal(client.pending, null);
    }
  });
  await test("200 stored replay does not trigger an extra POST", async () => {
    let count = 0;
    const client = new C.CheckoutClient({ keygen: () => "key", fetcher: async () => { count++; return response(200, attempt()); } });
    equal((await client.start(payload)).receipt, "Stored replay"); equal(count, 1);
  });
  await test("202 processing polls GET and returns the terminal state", async () => {
    const requests = []; let clock = 0;
    const client = new C.CheckoutClient({ keygen: () => "key", now: () => clock, sleep: async ms => { clock += ms; },
      fetcher: async (url, options) => { requests.push([url, options.method]); return requests.length === 1
        ? response(202, { ...attempt("PROCESSING"), terminal: false }) : response(200, attempt("DECLINED")); } });
    equal((await client.start(payload)).attempt.state, "DECLINED");
    equal(requests, [["/api/checkouts", "POST"], ["/api/checkouts/test-attempt", "GET"]]);
  });
  await test("bounded polling retains the original attempt for GET retry", async () => {
    let clock = 0; let done = false; const methods = [];
    const client = new C.CheckoutClient({ keygen: () => "key", now: () => clock, waitMs: 2500,
      sleep: async ms => { clock += ms; }, fetcher: async (_, options) => { methods.push(options.method);
        return response(200, done ? attempt() : { ...attempt("PROCESSING"), terminal: false }); } });
    await fails(() => client.start(payload));
    equal(client.pending.key, "key"); equal(client.pending.id, "test-attempt");
    done = true; await client.retry(); equal(methods.filter(x => x === "POST").length, 1);
  });
  await test("uncertain network failure retries identical key and payload", async () => {
    const requests = []; const saved = []; let keys = 0;
    const client = new C.CheckoutClient({ keygen: () => "key-" + ++keys, onChange: p => saved.push(clone(p)),
      fetcher: async (_, options) => { requests.push(options); if (requests.length === 1) throw new TypeError("offline"); return response(200, attempt()); } });
    await fails(() => client.start(payload));
    await fails(() => client.start({ ...payload, amount_pkr: "8000" }));
    equal(client.pending.payload, payload); assert(saved[0]);
    await client.retry();
    equal(requests[0].body, requests[1].body);
    equal(requests[0].headers["Idempotency-Key"], requests[1].headers["Idempotency-Key"]); equal(keys, 1);
  });
  await test("persisted pending inputs survive a client recreation", async () => {
    let saved;
    const first = new C.CheckoutClient({ keygen: () => "original", onChange: p => saved = clone(p), fetcher: async () => { throw new Error("offline"); } });
    await fails(() => first.start(payload));
    const restored = new C.CheckoutClient({ pending: saved, fetcher: async (_, options) => {
      equal(options.headers["Idempotency-Key"], "original"); equal(JSON.parse(options.body), payload); return response(200, attempt()); } });
    await restored.retry();
  });
  await test("pending double submissions cannot start a second request", async () => {
    let release; let calls = 0;
    const client = new C.CheckoutClient({ keygen: () => "key", fetcher: () => { calls++; return new Promise(resolve => { release = resolve; }); } });
    const work = client.start(payload);
    await fails(() => client.start(payload)); await fails(() => client.retry());
    release(response(201, attempt())); await work; equal(calls, 1);
  });
  await test("409 and 422 are explicit rejections; 503 retains a safe retry", async () => {
    for (const status of [409, 422, 503]) {
      const client = new C.CheckoutClient({ keygen: () => "key", fetcher: async () => response(status, {}) });
      equal((await fails(() => client.start(payload))).status, status);
      equal(!!client.pending, status === 503);
    }
  });
  await test("unreadable response retains original key", async () => {
    const client = new C.CheckoutClient({ keygen: () => "key", fetcher: async () => ({ status: 201, json: async () => { throw new Error("bad json"); } }) });
    await fails(() => client.start(payload)); equal(client.pending.key, "key");
  });
  await test("HTTP timeout aborts the fetch and preserves retry identity", async () => {
    let aborted = false;
    const client = new C.CheckoutClient({ keygen: () => "timeout-key", requestMs: 5,
      fetcher: (_, options) => new Promise((_, reject) => {
        options.signal.addEventListener("abort", () => { aborted = true; reject(new Error("aborted")); });
      }) });
    await fails(() => client.start(payload));
    assert(aborted); equal(client.pending.key, "timeout-key"); equal(client.pending.payload, payload);
  });
  await test("comparison switches only mode and uses a distinct fresh key", async () => {
    let keys = 0; const requests = [];
    const client = new C.CheckoutClient({ keygen: () => "key-" + ++keys, fetcher: async (_, options) => { requests.push(options); return response(201, attempt()); } });
    await client.start(payload); await client.start(C.otherMode(payload), attempt());
    assert(requests[0].headers["Idempotency-Key"] !== requests[1].headers["Idempotency-Key"]);
    equal(JSON.parse(requests[1].body), { ...payload, execution_mode: "sequential" });
  });
  await test("comparison calculates observed reduction and excludes technical failures", () => {
    const base = { ...attempt(), issuer_result: "APPROVED", risk_result: "CLEAR", timing_trace: fixture.traces.balanced_concurrent };
    const second = { ...base, request: C.otherMode(payload), timing_trace: fixture.traces.balanced_sequential };
    const stats = C.comparison(base, second);
    equal(stats.reduction, (stats.sequential - stats.concurrent) / stats.sequential * 100);
    equal(C.comparison(base, { ...second, state: "ERROR" }), null);
    equal(C.comparison(base, { ...second, request: { ...second.request, amount_pkr: "1.00" } }), null);
  });
  await test("comparison permits negative reductions and guards zero denominators", () => {
    const base = { ...attempt(), timing_trace: clone(fixture.traces.balanced_concurrent) };
    const second = { ...attempt(), request: C.otherMode(payload), timing_trace: clone(fixture.traces.balanced_sequential) };
    // Scale a copy solely to exercise arithmetic edge cases, never runtime data.
    function scale(trace, factor) {
      for (const key of ["orchestration_ms", "overlap_ms", "decision_ms"]) trace[key] *= factor;
      for (const branch of Object.values(trace.branches)) {
        for (const key of ["start_ms", "end_ms", "duration_ms"]) branch[key] *= factor;
      }
    }
    scale(base.timing_trace, 4);
    assert(C.comparison(base, second).reduction < 0);
    scale(second.timing_trace, 0);
    equal(C.comparison(base, second).reduction, null);
  });

  // Real browser smoke against the running FastAPI service via the local test proxy.
  const frame = document.getElementById("checkout");
  async function until(condition) {
    const deadline = performance.now() + 12000;
    while (!condition()) { if (performance.now() > deadline) throw new Error("UI wait expired"); await new Promise(r => setTimeout(r, 50)); }
  }
  // The form can exist while deferred app.js is still loading. Wait for the
  // iframe load to finish before clicking controls or submitting a checkout.
  await until(() => frame.contentDocument && frame.contentDocument.readyState === "complete"
    && frame.contentDocument.getElementById("pay-button"));
  const doc = frame.contentDocument;
  const win = frame.contentWindow;
  const form = doc.getElementById("checkout-form");
  const toggle = doc.getElementById("explorer-toggle");
  const panel = doc.getElementById("explorer-panel");
  function setExplorer(open) {
    if ((toggle.getAttribute("aria-expanded") === "true") !== open) toggle.click();
  }
  async function resizeFrame(width) {
    frame.style.width = width + "px";
    // Force real layout, then deliver the viewport event explicitly: the headless
    // virtual-time runner can defer compositor-generated resize callbacks.
    frame.getBoundingClientRect();
    doc.documentElement.getBoundingClientRect();
    win.dispatchEvent(new win.Event("resize"));
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  const uiRequests = [];
  const uiResponses = [];
  const originalFetch = win.fetch.bind(win);
  win.fetch = async (url, options) => {
    const posting = options && options.method === "POST";
    if (posting) uiRequests.push(clone({ url, headers: options.headers, body: options.body }));
    const reply = await originalFetch(url, options);
    if (posting) uiResponses.push(await reply.clone().json());
    return reply;
  };
  function beginCheckout() {
    if (!doc.getElementById("checkout-result").hidden) doc.getElementById("another-payment").click();
    form.requestSubmit();
  }
  async function submit(values) {
    for (const [name, value] of Object.entries(values)) form.elements[name].value = value;
    beginCheckout();
    await until(() => !doc.getElementById("pay-button").disabled && doc.querySelector(".attempt-card"));
  }
  await test("Explorer is collapsed initially with an accessible heading and downward chevron", () => {
    equal(win.getComputedStyle(doc.querySelector(".explorer-heading")).backgroundColor, "rgb(231, 244, 234)");
    equal(toggle.tagName, "BUTTON");
    equal(toggle.type, "button");
    equal(toggle.getAttribute("aria-controls"), panel.id);
    equal(toggle.getAttribute("aria-expanded"), "false");
    assert(panel.hidden);
    equal(win.getComputedStyle(doc.querySelector(".explorer-chevron")).transform, "none");
    assert(!doc.getElementById("profile").getClientRects().length);
    const checkout = doc.querySelector(".checkout-column").getBoundingClientRect();
    const explorer = doc.getElementById("architecture-explorer").getBoundingClientRect();
    assert(explorer.top > checkout.bottom);
    assert(checkout.width <= 520 && Math.abs(checkout.left - (win.innerWidth - checkout.right)) < 2);
  });
  await test("Architecture Explorer owns simulation controls without duplicating the checkout form", () => {
    setExplorer(true);
    const explorer = doc.getElementById("architecture-explorer");
    assert(explorer.textContent.includes("Architecture Explorer"));
    assert(explorer.textContent.includes("A real customer would normally see only the checkout and payment outcome"));
    assert(explorer.contains(doc.getElementById("simulation-settings")));
    assert(explorer.contains(doc.getElementById("compare-button")));
    assert(!form.contains(doc.getElementById("profile")));
    assert(!explorer.contains(doc.getElementById("amount")));
    equal(doc.querySelectorAll('select[name="workload_profile"]').length, 1);
    equal(doc.querySelectorAll('input[name="execution_mode"]').length, 2);
    equal(doc.querySelectorAll("form").length, 1);
    const sequential = doc.querySelector('[name="execution_mode"][value="sequential"]');
    sequential.click();
    assert(doc.getElementById("mode-help").textContent.includes("then await risk"));
    equal(Object.fromEntries(new win.FormData(form)), { ...payload, execution_mode: "sequential" });
    assert(!doc.getElementById("empty-state").hidden);
  });
  await test("manual toggles retain simulation values and reverse the chevron", () => {
    const before = Object.fromEntries(new win.FormData(form));
    form.elements.workload_profile.value = "asymmetric";
    setExplorer(false);
    assert(panel.hidden);
    equal(toggle.getAttribute("aria-expanded"), "false");
    setExplorer(true);
    assert(!panel.hidden);
    assert(win.getComputedStyle(doc.querySelector(".explorer-chevron")).transform.includes("-1"));
    equal(form.elements.workload_profile.value, "asymmetric");
    form.elements.workload_profile.value = before.workload_profile;
    equal(Object.fromEntries(new win.FormData(form)), before);
    setExplorer(false);
  });
  await test("desktop UI renders balanced checkout and measured bars", async () => {
    await submit({ execution_mode: "sequential" });
    equal(JSON.parse(uiRequests[0].body), { ...payload, execution_mode: "sequential" });
    equal(doc.querySelector(".attempt-card").dataset.state, "SUCCEEDED");
    assert(doc.querySelectorAll(".timeline-bar").length === 2);
    assert(doc.documentElement.scrollWidth <= win.innerWidth);
    assert(getComputedStyle(doc.querySelector(".workspace")).gridTemplateColumns.split(" ").length === 1);
    assert(panel.hidden, "Checkout completion must not open Explorer");
    assert(doc.querySelector(".checkout-column").contains(doc.querySelector(".attempt-card")));
    assert(doc.getElementById("architecture-explorer").contains(doc.querySelector(".timeline")));
    assert(!doc.querySelector(".checkout-column .timeline"));
  });
  await test("Make Another Payment restores inputs in the same card and retains execution evidence", () => {
    const checkout = doc.getElementById("checkout-card");
    const evidence = doc.querySelector(".execution-card");
    const values = Object.fromEntries(new win.FormData(form));
    const count = uiRequests.length;
    assert(form.hidden && !doc.getElementById("checkout-result").hidden);
    assert(checkout.contains(doc.querySelector(".attempt-card")));
    doc.getElementById("another-payment").click();
    assert(doc.getElementById("checkout-card") === checkout);
    equal(checkout.dataset.state, "FORM");
    assert(!form.hidden && doc.getElementById("checkout-result").hidden);
    equal(doc.querySelectorAll(".attempt-card").length, 0);
    equal(Object.fromEntries(new win.FormData(form)), values);
    assert(doc.querySelector(".execution-card") === evidence);
    equal(doc.activeElement.id, "amount");
    equal(uiRequests.length, count);
    assert(panel.hidden);
  });
  await test("checkout form and compact Explorer fit all five requested viewport widths", async () => {
    for (const width of [1440, 1024, 768, 390, 320]) {
      await resizeFrame(width);
      const card = doc.getElementById("checkout-card").getBoundingClientRect();
      assert(card.width <= 520 && card.left >= 0 && card.right <= width);
      assert(doc.documentElement.scrollWidth <= width);
      for (const selector of [".amount-field", ".preset-options", "#country", "#pay-button", "#explorer-toggle"]) {
        const box = doc.querySelector(selector).getBoundingClientRect();
        assert(box.left >= 0 && box.right <= width, selector + " exceeds viewport");
      }
      for (const id of ["pay-button", "country", "explorer-toggle"]) {
        assert(doc.getElementById(id).getBoundingClientRect().height >= 44);
        assert(doc.getElementById(id).tabIndex >= 0);
      }
      assert(doc.getElementById("architecture-explorer").getBoundingClientRect().top > card.bottom);
    }
    await resizeFrame(1200);
  });
  await test("explicit UI comparison shows both real attempts with new keys", async () => {
    setExplorer(true);
    doc.getElementById("compare-button").click();
    await until(() => doc.querySelectorAll(".execution-card").length === 2);
    equal(doc.querySelectorAll(".attempt-card").length, 1);
    const [a, b] = uiRequests;
    assert(a.headers["Idempotency-Key"] !== b.headers["Idempotency-Key"]);
    equal(JSON.parse(b.body), { ...JSON.parse(a.body), execution_mode: "concurrent" });
    assert(doc.querySelectorAll(".timeline-bar").length === 4);
    assert(doc.getElementById("comparison-summary").textContent.includes("same business outcome"));
    const details = [...doc.querySelectorAll(".trace-details")].map(element => element.textContent);
    assert(details[0].endsWith("overlap 0 ms"));
    assert(Number(/overlap ([\d.]+) ms$/.exec(details[1])[1]) > 0, "Real concurrent overlap must be positive");
    const stats = C.comparison(...uiResponses);
    const summary = doc.getElementById("comparison-summary");
    const change = doc.getElementById("comparison-change");
    assert(!change.hidden && doc.querySelector(".execution-output-header").contains(change));
    equal(change.querySelector("strong").textContent, Math.abs(stats.reduction).toFixed(1) + "%");
    equal(change.querySelector("span").textContent, `${stats.reduction >= 0 ? "Lower" : "Higher"} orchestration time in this pair`);
    equal(summary.querySelector("h4").textContent, "Interpretation & limitations");
    assert(!summary.textContent.includes("%") && !summary.textContent.includes(" ms"));
    for (const result of uiResponses) {
      const stored = await (await originalFetch("/api/checkouts/" + result.id)).json();
      equal(stored.request, result.request);
      equal(stored.timing_trace, result.timing_trace);
    }
  });
  await test("desktop comparisons align equal cards and share one measured time scale", () => {
    equal(doc.querySelectorAll("#execution-output").length, 1);
    equal(doc.getElementById("execution-output-heading").textContent, "Execution Comparison");
    assert(doc.getElementById("execution-output").contains(doc.getElementById("comparison-summary")));
    equal(doc.querySelectorAll(".execution-card.card").length, 0);
    const cards = [...doc.querySelectorAll(".execution-card")];
    equal(win.getComputedStyle(doc.getElementById("execution-output-heading")).fontSize, "19px");
    for (const metric of doc.querySelectorAll(".metric strong")) {
      equal(win.getComputedStyle(metric).fontSize, "28px");
      assert(win.getComputedStyle(metric).fontVariantNumeric.includes("tabular-nums"));
    }
    assert(win.getComputedStyle(doc.querySelector(".comparison-summary p")).fontVariantNumeric.includes("tabular-nums"));
    equal(win.getComputedStyle(doc.querySelector("#comparison-change strong")).fontSize, "30px");
    assert(win.getComputedStyle(doc.querySelector("#comparison-change strong")).fontVariantNumeric.includes("tabular-nums"));
    equal(cards.map(card => card.dataset.mode), ["sequential", "concurrent"]);
    const [a, b] = cards.map(card => card.getBoundingClientRect());
    assert(Math.abs(a.width - b.width) < 1 && Math.abs(a.right - b.left) < 1 && a.top === b.top);
    for (const selector of [".mode-title", ".execution-state", ".timeline h4", ".metrics", ".axis"]) {
      equal(cards[0].querySelector(selector).getBoundingClientRect().top, cards[1].querySelector(selector).getBoundingClientRect().top);
    }
    const timelines = cards.map(card => card.querySelector(".timeline"));
    equal(timelines[0].dataset.axisMaxMs, timelines[1].dataset.axisMaxMs);
    equal(cards[0].querySelector(".axis").textContent, cards[1].querySelector(".axis").textContent);
    const maximum = Number(timelines[0].dataset.axisMaxMs);
    for (const card of cards) {
      for (const bar of card.querySelectorAll(".timeline-bar")) {
        const start = Number(bar.dataset.startMs), end = Number(bar.dataset.endMs);
        assert(maximum > end);
        assert(Math.abs(parseFloat(bar.style.width) - (end - start) / maximum * 100) < 1e-4);
        assert(Math.abs(parseFloat(bar.style.left) - start / maximum * 100) < 1e-4);
      }
    }
    const [issuerA, issuerB] = cards.map(card => card.querySelector(".timeline-bar.issuer").getBoundingClientRect());
    const issuerBars = cards.map(card => card.querySelector(".timeline-bar.issuer"));
    const densities = [issuerA, issuerB].map((box, index) => box.width / (Number(issuerBars[index].dataset.endMs) - Number(issuerBars[index].dataset.startMs)));
    assert(Math.abs(densities[0] - densities[1]) < .001, "Both charts must use the same pixels per measured millisecond");
    
  });
  await test("toggling preserves completed comparison nodes, scales and selected inputs", () => {
    const cards = [...doc.querySelectorAll(".execution-card")];
    const summary = doc.getElementById("comparison-summary").textContent;
    const change = doc.getElementById("comparison-change").textContent;
    const inputs = Object.fromEntries(new win.FormData(form));
    setExplorer(false);
    assert(panel.hidden);
    setExplorer(true);
    assert(cards.every((card, index) => doc.querySelectorAll(".execution-card")[index] === card));
    equal(doc.getElementById("comparison-summary").textContent, summary);
    equal(doc.getElementById("comparison-change").textContent, change);
    equal(Object.fromEntries(new win.FormData(form)), inputs);
    equal(uiRequests.length, 2);
  });
  await test("comparison preserves geometry at desktop, zoom-equivalent widths, tablet and mobile", async () => {
    const scales = [...doc.querySelectorAll(".timeline")].map(chart => chart.dataset.axisMaxMs);
    const geometry = [...doc.querySelectorAll(".timeline-bar")].map(bar => [bar.style.left, bar.style.width, { ...bar.dataset }]);
    // Browser zoom shrinks the CSS viewport: cover 1440px desktop at 110% / 125%.
    for (const width of [1440, Math.floor(1440 / 1.1), 1440 / 1.25, 1024, 850, 768, 681, 680, 390, 320]) {
      await resizeFrame(width);
      const heading = doc.getElementById("execution-output-heading").getBoundingClientRect();
      const change = doc.getElementById("comparison-change").getBoundingClientRect();
      const header = doc.querySelector(".execution-output-header").getBoundingClientRect();
      assert(change.left >= header.left && change.right <= header.right);
      assert(change.bottom <= header.bottom && heading.bottom <= header.bottom);
      if (width <= 390) {
        assert(change.top >= heading.bottom, "Header must stack on mobile");
        equal(change.left, heading.left);
      } else if (width >= 768) {
        assert(change.left > heading.right, "Desktop percentage must sit to the right");
        assert(heading.top < change.bottom && heading.bottom > change.top);
      }
      assert(doc.getElementById("comparison-change").scrollWidth <= doc.getElementById("comparison-change").clientWidth);
      const cards = [...doc.querySelectorAll(".execution-card")].map(card => card.getBoundingClientRect());
      if (width <= 680) assert(cards[1].top >= cards[0].bottom && cards[0].left === cards[1].left);
      else {
        assert(Math.abs(cards[0].width - cards[1].width) < 1 && cards[0].top === cards[1].top);
        const tracks = [...doc.querySelectorAll(".execution-card")].map(card => card.querySelector(".timeline-track").getBoundingClientRect());
        assert(Math.abs(tracks[0].width - tracks[1].width) < .1, "Shared scale needs equal plot widths");
        for (const selector of [".mode-title", ".timeline-label", ".metrics", ".axis"]) {
          const rows = [...doc.querySelectorAll(".execution-card")].map(card => card.querySelector(selector).getBoundingClientRect());
          equal(rows[0].top, rows[1].top);
        }
      }
      equal([...doc.querySelectorAll(".timeline")].map(chart => chart.dataset.axisMaxMs), scales);
      equal([...doc.querySelectorAll(".timeline-bar")].map(bar => [bar.style.left, bar.style.width, { ...bar.dataset }]), geometry);
      assert(doc.documentElement.scrollWidth <= win.innerWidth);
      for (const metric of doc.querySelectorAll(".metric")) {
        const bounds = metric.getBoundingClientRect();
        const value = metric.querySelector("strong").getBoundingClientRect();
        assert(value.left >= bounds.left - 1 && value.right <= bounds.right + 1, "Primary timing clipped at " + width);
      }
      for (const axis of doc.querySelectorAll(".axis")) {
        const bounds = axis.getBoundingClientRect();
        for (const tick of axis.querySelectorAll("span")) {
          if (!tick.getClientRects().length) continue;
          const box = tick.getBoundingClientRect();
          assert(box.left >= bounds.left - 1 && box.right <= bounds.right + 1, "Axis tick clipped");
        }
      }
      for (const track of doc.querySelectorAll(".timeline-track")) {
        const bounds = track.getBoundingClientRect(), label = track.querySelector(".timeline-duration").getBoundingClientRect();
        assert(label.left >= bounds.left - 1 && label.right <= bounds.right + 1, "Duration label clipped");
        const next = track.closest(".timeline-row").nextElementSibling;
        if (next) assert(label.bottom <= next.getBoundingClientRect().top, "Duration label overlaps next row");
      }
      for (const details of doc.querySelectorAll(".trace-details")) {
        const text = details.textContent;
        details.open = true;
        assert(details.scrollWidth <= details.clientWidth, "Expanded metadata overflow");
        equal(details.textContent, text);
        details.open = false;
      }
    }
    await resizeFrame(1200);
  });
  await test("UI displays cross-border verification and issuer decline", async () => {
    await submit({ billing_country: "US", workload_profile: "lightweight" });
    equal(doc.querySelector(".attempt-card").dataset.state, "VERIFICATION_REQUIRED");
    await submit({ card_preset: "declined" });
    equal(doc.querySelector(".attempt-card").dataset.state, "DECLINED");
  });
  await test("single execution fits all five requested widths with readable timeline axes", async () => {
    for (const width of [1440, 1024, 768, 390, 320]) {
      await resizeFrame(width);
      equal(getComputedStyle(doc.querySelector(".workspace")).gridTemplateColumns.split(" ").length, 1);
      assert(doc.documentElement.scrollWidth <= win.innerWidth, "Mobile overflow");
      const checkout = doc.querySelector(".checkout-column").getBoundingClientRect();
      const explorer = doc.getElementById("architecture-explorer").getBoundingClientRect();
      assert(explorer.top >= checkout.bottom, "Explorer must stack below checkout");
      const ticks = [...doc.querySelectorAll(".axis span")].filter(tick => tick.getClientRects().length)
        .map(tick => tick.getBoundingClientRect());
      for (let index = 1; index < ticks.length; index++) assert(ticks[index].left >= ticks[index - 1].right, "Axis labels overlap");
    }
    equal(doc.querySelector(".explorer-link").getAttribute("href"), "#architecture-explorer");
    doc.querySelector('[name="execution_mode"][value="sequential"]').click();
    equal(form.elements.execution_mode.value, "sequential");
  });
  function uiResource(state = "SUCCEEDED", input = C.normalize(Object.fromEntries(new win.FormData(form)))) {
    return { ...attempt(state), request: input, issuer_result: state === "DECLINED" ? "DECLINED" : "APPROVED",
      risk_result: state === "VERIFICATION_REQUIRED" ? "VERIFY" : "CLEAR", decision: state === "ERROR" ? null : state,
      timing_trace: state === "ERROR" ? null : fixture.traces[input.workload_profile + "_" + input.execution_mode] };
  }
  function comparisonPair(profile = "balanced") {
    return ["sequential", "concurrent"].map(mode => clone(uiResource("SUCCEEDED", {
      ...payload, workload_profile: profile, execution_mode: mode
    })));
  }
  async function showComparison(first, second) {
    const original = JSON.stringify([first, second]);
    win.fetch = async () => response(201, first);
    await submit(first.request);
    assert(doc.getElementById("comparison-change").hidden, "A single execution must clear the previous percentage");
    equal(doc.getElementById("comparison-change").textContent, "");
    win.fetch = async (_, options) => {
      equal(JSON.parse(options.body), C.otherMode(first.request));
      return response(201, second);
    };
    doc.getElementById("compare-button").click();
    await until(() => doc.querySelectorAll(".execution-card").length === 2);
    equal(JSON.stringify([first, second]), original);
  }
  await test("comparison header shows one honest percentage for faster, slower, equal and tiny differences", async () => {
    const equalPair = comparisonPair();
    // Display-only edge-case fixtures; never replace measured runtime or benchmark data.
    equalPair[1].timing_trace.orchestration_ms = equalPair[0].timing_trace.orchestration_ms;
    const tinyLower = clone(equalPair), tinyHigher = clone(equalPair);
    tinyLower[1].timing_trace.orchestration_ms *= .9996;
    tinyHigher[1].timing_trace.orchestration_ms *= 1.0004;
    for (const [pair, direction, color] of [
      [comparisonPair(), "lower", "rgb(8, 113, 91)"],
      [comparisonPair("lightweight"), "higher", "rgb(136, 97, 18)"],
      [equalPair, "equal", "rgb(97, 117, 124)"],
      [tinyLower, "lower", "rgb(8, 113, 91)"],
      [tinyHigher, "higher", "rgb(136, 97, 18)"]
    ]) {
      await showComparison(...pair);
      const change = doc.getElementById("comparison-change"), summary = doc.getElementById("comparison-summary");
      const reduction = C.comparison(...pair).reduction;
      assert(!change.hidden);
      equal(change.dataset.direction, direction);
      equal(change.querySelector("strong").textContent,
        reduction !== 0 && Math.abs(reduction) < .05 ? "<0.1%" : Math.abs(reduction).toFixed(1) + "%");
      equal(win.getComputedStyle(change.querySelector("strong")).color, color);
      equal(change.querySelector("span").textContent, direction === "equal" ? "No orchestration time difference in this pair"
        : `${direction === "lower" ? "Lower" : "Higher"} orchestration time in this pair`);
      equal((doc.getElementById("execution-output").textContent.match(/%/g) || []).length, 1);
      assert(!summary.textContent.includes("%") && !summary.textContent.includes(" ms"));
      assert(summary.textContent.includes("Both attempts produced the same business outcome."));
      assert(summary.textContent.includes("Two observations are not a formal benchmark."));
      for (const width of [1200, 1152, 390, 320]) {
        await resizeFrame(width);
        const heading = doc.getElementById("execution-output-heading").getBoundingClientRect();
        const bounds = change.getBoundingClientRect();
        const header = doc.querySelector(".execution-output-header").getBoundingClientRect();
        assert(bounds.left >= header.left && bounds.right <= header.right && bounds.bottom <= header.bottom);
        assert(bounds.left >= heading.right || bounds.top >= heading.bottom, "Header content overlaps");
        assert(change.scrollWidth <= change.clientWidth && doc.documentElement.scrollWidth <= width);
      }
    }
    await resizeFrame(1200);
  });
  await test("unavailable comparisons hide stale percentages and only claim recorded matching outcomes", async () => {
    const missing = comparisonPair(), invalid = comparisonPair(), different = comparisonPair();
    const error = comparisonPair(), zero = comparisonPair(), inputs = comparisonPair(), unknown = comparisonPair();
    missing[1].timing_trace = null;
    invalid[1].timing_trace.branches.risk.duration_ms = -1;
    Object.assign(different[1], { state: "DECLINED", issuer_result: "DECLINED" });
    Object.assign(error[1], { state: "ERROR", issuer_result: null, risk_result: null });
    inputs[1].request.amount_pkr = "8000.00";
    unknown[0].issuer_result = unknown[1].issuer_result = null;
    for (const name of ["orchestration_ms", "decision_ms", "overlap_ms"]) zero[0].timing_trace[name] = 0;
    for (const branch of Object.values(zero[0].timing_trace.branches)) {
      for (const name of ["start_ms", "end_ms", "duration_ms"]) branch[name] = 0;
    }
    for (const [pair, outcome] of [[missing, "same"], [invalid, "same"], [different, "different"],
      [error, "unknown"], [zero, "same"], [inputs, "same"], [unknown, "unknown"]]) {
      await showComparison(...pair);
      const change = doc.getElementById("comparison-change"), summary = doc.getElementById("comparison-summary").textContent;
      assert(change.hidden);
      equal(change.textContent, "");
      assert(!change.hasAttribute("data-direction"));
      assert(!doc.getElementById("execution-output").textContent.includes("%"));
      assert(summary.includes("unavailable"));
      equal(summary.includes("Both attempts produced the same business outcome."), outcome === "same");
      equal(summary.includes("The attempts produced different business outcomes."), outcome === "different");
      equal(summary.includes("Matching business outcomes could not be confirmed"), outcome === "unknown");
      assert(summary.includes("Two observations are not a formal benchmark."));
    }
  });
  await test("two-decimal headline strings preserve raw traces, geometry, bar labels and exact details", async () => {
    function freeze(value) {
      if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); }
      return value;
    }
    for (const trace of Object.values(fixture.traces)) {
      const input = { ...payload, workload_profile: trace.workload_profile, execution_mode: trace.execution_mode };
      const body = freeze(clone(uiResource("SUCCEEDED", input)));
      const original = JSON.stringify(body);
      win.fetch = async () => response(201, body);
      await submit(input);
      const timeline = doc.querySelector(".timeline");
      const metrics = [...timeline.querySelectorAll(".metric strong")];
      equal(metrics.map(metric => metric.textContent), [trace.orchestration_ms, trace.overlap_ms].map(value => value.toFixed(2) + " ms"));
      equal(metrics.map(metric => metric.title), [trace.orchestration_ms, trace.overlap_ms].map(value => value + " ms"));
      const maximum = Number(timeline.dataset.axisMaxMs);
      for (const [key, branch] of Object.entries(trace.branches)) {
        const bar = timeline.querySelector(".timeline-bar." + key);
        equal({ ...bar.dataset }, { startMs: String(branch.start_ms), endMs: String(branch.end_ms), durationMs: String(branch.duration_ms) });
        assert(Math.abs(parseFloat(bar.style.left) - branch.start_ms / maximum * 100) < 1e-4);
        assert(Math.abs(parseFloat(bar.style.width) - (branch.end_ms - branch.start_ms) / maximum * 100) < 1e-4);
        equal(timeline.querySelector(".timeline-duration." + key).textContent, C.milliseconds(branch.duration_ms));
      }
      equal([...timeline.querySelectorAll(".trace-details p")].map(p => p.textContent), [
        ...Object.entries(trace.branches).map(([key, b]) =>
          `${key}: start ${b.start_ms} ms · end ${b.end_ms} ms · duration ${b.duration_ms} ms · ${b.status} · artificial wait ${b.artificial_delay_ms} ms`),
        `Decision at ${trace.decision_ms} ms · total ${trace.orchestration_ms} ms · overlap ${trace.overlap_ms} ms`
      ]);
      equal(JSON.stringify(body), original);
    }
  });
  await test("processing feedback follows the pending request without flashing the introduction", async () => {
    setExplorer(false);
    let release;
    const body = uiResource();
    win.fetch = () => new Promise(resolve => { release = resolve; });
    const empty = doc.getElementById("empty-state");
    const changes = [];
    const observer = new win.MutationObserver(records => changes.push(...records));
    observer.observe(empty, { attributes: true, attributeOldValue: true, attributeFilter: ["hidden"] });
    try {
      beginCheckout();
      equal(doc.getElementById("request-status").textContent, "Processing your sandbox payment...");
      assert(doc.getElementById("checkout-fields").disabled);
      assert(doc.getElementById("simulation-settings").disabled);
      assert(!doc.getElementById("execution-status").hidden);
      assert(empty.hidden);
      assert(doc.querySelector(".execution-card"), "Keep the last recorded evidence while waiting");
      assert(form.hidden && doc.getElementById("checkout-result").hidden);
      equal(doc.getElementById("checkout-card").dataset.state, "PROCESSING");
      assert(panel.hidden);
      await new Promise(r => setTimeout(r, 100));
      assert(doc.getElementById("request-status").classList.contains("pending"));
      assert(doc.getElementById("pay-button").disabled);
      release(response(201, body));
      await until(() => !doc.getElementById("pay-button").disabled);
      assert(!doc.getElementById("request-status").classList.contains("pending"));
      assert(doc.getElementById("execution-status").hidden);
      assert(doc.querySelector(".execution-explanation"));
      assert(panel.hidden, "A result must not open Explorer");
      assert(changes.every(record => record.oldValue !== null), "Idle introduction was briefly re-shown");
    } finally { observer.disconnect(); }
  });
  await test("completed outcomes and execution evidence persist across idle time and setting edits", async () => {
    for (const state of Object.keys(C.states)) {
      const body = uiResource(state);
      win.fetch = async () => response(201, body);
      beginCheckout();
      await until(() => !doc.getElementById("pay-button").disabled);
      const outcome = doc.querySelector(".attempt-card");
      const execution = doc.querySelector(".execution-card");
      const explanation = execution.textContent;
      equal(outcome.dataset.state, state);
      equal(execution.querySelector(".execution-state").textContent, {
        SUCCEEDED: "Succeeded", DECLINED: "Declined", VERIFICATION_REQUIRED: "Verification required", ERROR: "Error"
      }[state]);
      equal(body.state, state);
      equal(outcome.querySelector("h3").textContent, { SUCCEEDED: "Payment Approved", DECLINED: "Payment Declined",
        VERIFICATION_REQUIRED: "Sandbox Verification Required", ERROR: "Payment Processing Error" }[state]);
      assert(doc.getElementById("checkout-card").contains(outcome));
      assert(form.hidden && !doc.getElementById("checkout-result").hidden);
      equal(doc.getElementById("checkout-card").dataset.state, "RESULT");
      const other = C.otherMode(body.request).execution_mode;
      doc.querySelector('[name="execution_mode"][value="' + other + '"]').click();
      await new Promise(r => setTimeout(r, 150));
      assert(doc.querySelector(".attempt-card") === outcome);
      assert(doc.querySelector(".execution-card") === execution);
      equal(execution.textContent, explanation);
      assert(doc.getElementById("empty-state").hidden);
      assert(doc.getElementById("execution-status").hidden);
      assert(!doc.getElementById("request-status").classList.contains("pending"));
      assert(panel.hidden);
    }
  });
  await test("comparison keeps original execution visible while pending and both traces after completion", async () => {
    setExplorer(true);
    const body = uiResource();
    win.fetch = async () => response(201, body);
    beginCheckout();
    await until(() => !doc.getElementById("pay-button").disabled);
    const original = doc.querySelector(".execution-card");
    let release, submitted;
    win.fetch = (_, options) => { submitted = JSON.parse(options.body); return new Promise(resolve => { release = resolve; }); };
    doc.getElementById("compare-button").click();
    equal(submitted, C.otherMode(body.request));
    assert(doc.querySelector(".execution-card") === original);
    assert(doc.getElementById("simulation-settings").disabled);
    assert(doc.getElementById("compare-button").disabled);
    setExplorer(false);
    release(response(201, uiResource("SUCCEEDED", submitted)));
    await until(() => !doc.getElementById("pay-button").disabled);
    const explorer = doc.getElementById("architecture-explorer");
    equal(explorer.querySelectorAll(".execution-card").length, 2);
    equal(explorer.querySelectorAll(".timeline-bar").length, 4);
    assert(!doc.getElementById("comparison-summary").hidden);
    await new Promise(r => setTimeout(r, 150));
    equal(explorer.querySelectorAll(".execution-card").length, 2);
    assert(panel.hidden, "Updating comparison must respect manual collapse");
    setExplorer(true);
  });
  await test("blue/green bars use direct white labels inside and dark labels outside without altering intervals", async () => {
    const input = { ...payload, workload_profile: "asymmetric" };
    win.fetch = async () => response(201, uiResource("SUCCEEDED", input));
    await resizeFrame(1200);
    beginCheckout();
    await until(() => !doc.getElementById("pay-button").disabled);
    setExplorer(true);
    await new Promise(r => setTimeout(r, 100));
    const tracks = [...doc.querySelectorAll(".timeline-track")];
    const widths = tracks.map(track => track.querySelector(".timeline-bar").style.width);
    for (const [index, track] of tracks.entries()) {
      const bar = track.querySelector(".timeline-bar"), label = track.querySelector(".timeline-duration");
      equal(win.getComputedStyle(bar).backgroundColor, index === 0 ? "rgb(103, 150, 188)" : "rgb(89, 152, 121)");
      equal(label.textContent, C.milliseconds(Number(bar.dataset.durationMs)));
      assert(label.classList.contains("inside"));
      equal(win.getComputedStyle(label).color, "rgb(255, 255, 255)");
      equal(win.getComputedStyle(label).fontSize, "12px");
      equal(win.getComputedStyle(label).fontWeight, "600");
      equal(win.getComputedStyle(label).backgroundColor, "rgba(0, 0, 0, 0)");
      equal(win.getComputedStyle(label).backgroundImage, "none");
      const a = bar.getBoundingClientRect(), b = label.getBoundingClientRect();
      assert(Math.abs((a.left + a.right) / 2 - (b.left + b.right) / 2) < 1);
    }
    await resizeFrame(320);
    equal(tracks.map(track => track.querySelector(".timeline-bar").style.width), widths);
    const label = tracks[0].querySelector(".timeline-duration");
    assert(!label.classList.contains("inside"), `Short bar label: track=${tracks[0].clientWidth}, label=${label.offsetWidth}, bar=${tracks[0].querySelector(".timeline-bar").getBoundingClientRect().width}, class=${label.className}`);
    equal(win.getComputedStyle(label).color, "rgb(25, 59, 72)");
    const barBox = tracks[0].querySelector(".timeline-bar").getBoundingClientRect();
    assert(label.getBoundingClientRect().left >= barBox.right || label.getBoundingClientRect().right <= barBox.left || label.classList.contains("below"));
  });
  await test("UI uncertain failure locks inputs and safely retries original request", async () => {
    let first;
    win.fetch = async (url, options) => { first = clone({ headers: options.headers, body: options.body }); throw new TypeError("offline"); };
    beginCheckout();
    await until(() => !doc.getElementById("retry-button").hidden);
    assert(doc.getElementById("checkout-fields").disabled);
    assert(doc.getElementById("simulation-settings").disabled);
    assert(!doc.getElementById("request-status").classList.contains("pending"));
    assert(doc.getElementById("execution-status").textContent.includes("unresolved"));
    assert(win.sessionStorage.getItem("asaanpay.pending.v1"));
    assert(form.hidden && doc.getElementById("checkout-result").hidden);
    assert(!doc.getElementById("another-payment").getClientRects().length);
    assert(doc.getElementById("retry-button").getClientRects().length);
    win.fetch = async (url, options) => {
      equal(options.headers["Idempotency-Key"], first.headers["Idempotency-Key"]); equal(options.body, first.body);
      return originalFetch(url, options);
    };
    doc.getElementById("retry-button").click();
    await until(() => !doc.getElementById("checkout-fields").disabled);
    equal(win.sessionStorage.getItem("asaanpay.pending.v1"), null);
  });
  await test("definitive API rejections restore the form without a pending retry or false result", async () => {
    for (const status of [409, 422]) {
      win.fetch = async () => response(status, {});
      beginCheckout();
      await until(() => !doc.getElementById("pay-button").disabled);
      assert(!form.hidden && doc.getElementById("checkout-result").hidden);
      equal(doc.getElementById("checkout-card").dataset.state, "FORM");
      assert(doc.getElementById("retry-button").hidden);
      assert(doc.getElementById("request-status").classList.contains("error"));
      equal(win.sessionStorage.getItem("asaanpay.pending.v1"), null);
      assert(!doc.getElementById("checkout-fields").disabled);
    }
  });
  await test("invalid local amount keeps the form and does not start a request", () => {
    let calls = 0;
    win.fetch = async () => { calls++; return response(201, uiResource()); };
    const amount = form.elements.amount_pkr.value;
    form.elements.amount_pkr.value = "-1";
    beginCheckout();
    equal(calls, 0);
    assert(!form.hidden && doc.getElementById("checkout-result").hidden);
    assert(doc.getElementById("request-status").classList.contains("error"));
    form.elements.amount_pkr.value = amount;
  });
  await test("UI technical ERROR and missing trace are distinct and safely rendered", async () => {
    win.fetch = async () => response(201, { ...attempt("ERROR"), decision: null, error_reason: '<img src=x onerror="window.injected=true">', timing_trace: null });
    beginCheckout();
    await until(() => doc.querySelector('.attempt-card[data-state="ERROR"]'));
    assert(doc.querySelector(".decision-reason").textContent.includes("<img"));
    assert(!doc.querySelector(".decision-reason img") && !win.injected);
    assert(doc.querySelector(".timeline").textContent.includes("Timing unavailable"));
  });
  await test("page reload restores the unresolved key, inputs and explicit retry", async () => {
    win.fetch = async () => { throw new TypeError("offline"); };
    beginCheckout();
    await until(() => !doc.getElementById("retry-button").hidden);
    const saved = JSON.parse(win.sessionStorage.getItem("asaanpay.pending.v1"));
    frame.src = "/?reload-check=1";
    await until(() => frame.contentDocument !== doc && frame.contentDocument.getElementById("pay-button")
      && !frame.contentDocument.getElementById("retry-button").hidden);
    const restoredWindow = frame.contentWindow, restoredDoc = frame.contentDocument;
    assert(restoredDoc.getElementById("checkout-fields").disabled);
    assert(restoredDoc.getElementById("simulation-settings").disabled);
    assert(restoredDoc.getElementById("empty-state").hidden);
    equal(restoredDoc.getElementById("amount").value, saved.payload.amount_pkr);
    equal(restoredDoc.getElementById("checkout-form").elements.execution_mode.value, saved.payload.execution_mode);
    equal(restoredDoc.getElementById("profile").value, saved.payload.workload_profile);
    assert(restoredDoc.getElementById("explorer-panel").hidden);
    equal(restoredDoc.getElementById("explorer-toggle").getAttribute("aria-expanded"), "false");
    const liveFetch = restoredWindow.fetch.bind(restoredWindow);
    restoredWindow.fetch = async (url, options) => {
      equal(options.headers["Idempotency-Key"], saved.key);
      equal(JSON.parse(options.body), saved.payload);
      return liveFetch(url, options);
    };
    restoredDoc.getElementById("retry-button").click();
    await until(() => !restoredDoc.getElementById("checkout-fields").disabled);
    equal(restoredWindow.sessionStorage.getItem("asaanpay.pending.v1"), null);
  });
  const failed = results.filter(x => x.startsWith("FAIL")).length;
  document.getElementById("test-results").textContent = results.join("\n") + `\nRESULT: ${results.length - failed} passed; ${failed} failed`;
  document.body.dataset.result = failed ? "failed" : "passed";
  // A recognizable marker for the dependency-free Chrome runner.
  document.title = failed ? "ASAANPAY_TESTS_FAILED" : "ASAANPAY_TESTS_PASSED";
})().catch(error => {
  document.getElementById("test-results").textContent += "\nFATAL: " + error.stack;
  document.title = "ASAANPAY_TESTS_FAILED";
});
