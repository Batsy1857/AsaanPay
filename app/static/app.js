/* DOM adapter. API-provided strings are rendered only as text. */
(function () {
  "use strict";
  const Core = window.AsaanPay;
  const $ = (id) => document.getElementById(id);
  const form = $("checkout-form");
  const storageKey = "asaanpay.pending.v1";
  let baseline = null;
  let comparisonDone = false;
  let pending = null;
  let showingResult = false;
  const outcomeTitles = {
    SUCCEEDED: "Payment Approved", DECLINED: "Payment Declined",
    VERIFICATION_REQUIRED: "Sandbox Verification Required", ERROR: "Payment Processing Error"
  };
  const executionStates = {
    SUCCEEDED: "Succeeded", DECLINED: "Declined",
    VERIFICATION_REQUIRED: "Verification required", ERROR: "Error"
  };

  function storageWarning() {
    $("storage-notice").hidden = false;
    $("storage-notice").textContent = "Browser storage is unavailable. Safe retry works in this tab; keep it open until the attempt is resolved.";
  }
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey));
    if (saved && typeof saved.key === "string" && /^[!-~]{1,128}$/.test(saved.key)) {
      pending = { key: saved.key, payload: Core.normalize(saved.payload),
        id: typeof saved.id === "string" ? saved.id : null, base: saved.base || null };
    }
  } catch (_) { storageWarning(); }

  const client = new Core.CheckoutClient({ pending, onChange(value) {
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch (_) { storageWarning(); }
  } });

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function message(text, kind = "") {
    const status = $("request-status");
    status.textContent = text;
    status.className = "message " + kind;
    status.hidden = !text;
  }
  function modeHelp() {
    $("mode-help").textContent = form.elements.execution_mode.value === "sequential"
      ? "Await issuer authorization, then await risk screening."
      : "Start both independent checks, then join their results.";
  }
  function setForm(payload) {
    for (const [key, value] of Object.entries(payload)) if (form.elements[key]) form.elements[key].value = value;
    modeHelp();
  }
  function controls() {
    const waiting = client.busy || !!client.pending;
    form.hidden = waiting || showingResult;
    $("checkout-result").hidden = waiting || !showingResult;
    $("checkout-card").dataset.state = waiting ? "PROCESSING" : showingResult ? "RESULT" : "FORM";
    $("retry-button").hidden = client.busy || !client.pending;
    $("checkout-fields").disabled = client.busy || !!client.pending;
    $("simulation-settings").disabled = client.busy || !!client.pending;
    $("pay-button").disabled = client.busy;
    $("pay-button").firstElementChild.textContent = client.busy ? "Processing…" : client.pending ? "Retry same attempt" : "Pay in sandbox";
    $("compare-button").hidden = !baseline || comparisonDone;
    $("compare-button").disabled = client.busy || !!client.pending;
    $("comparison-note").hidden = !baseline;
    $("attempts").setAttribute("aria-busy", String(client.busy));
    $("payment-outcomes").setAttribute("aria-busy", String(client.busy));
    $("execution-status").hidden = !client.busy && !client.pending;
    $("execution-status").textContent = client.busy
      ? "Waiting for the recorded execution. Timings appear when this attempt finishes."
      : "This attempt is unresolved. Retry the same attempt to retrieve its recorded result.";
    if (client.busy || client.pending) $("empty-state").hidden = true;
  }

  // Round only headline display strings; trace geometry and calculations use raw values.
  function headlineMilliseconds(value) { return value.toFixed(2) + " ms"; }

  function metric(label, value) {
    const item = node("div", "metric");
    item.append(node("span", "", label));
    const amount = node("strong", "", headlineMilliseconds(value));
    amount.title = String(value) + " ms";
    item.append(amount);
    return item;
  }
  function chartScale(attempts) {
    // Presentation-only padding/ticks. Core measurements and trace validation stay unchanged.
    const maximum = Math.max(0, ...attempts.map(attempt => Core.traceLayout(attempt.timing_trace)?.total || 0));
    if (!maximum) return { maximum: 0, ticks: [0] };
    const padded = maximum * 1.05;
    const power = 10 ** Math.floor(Math.log10(padded / 4));
    const step = [1, 2, 2.5, 5, 10].find(value => value * power >= padded / 4) * power;
    const count = Math.ceil(padded / step);
    return { maximum: count * step, ticks: Array.from({ length: count + 1 }, (_, index) => index * step) };
  }

  function fitDurationLabels() {
    for (const track of $("attempts").querySelectorAll(".timeline-track")) {
      const width = track.clientWidth;
      if (!width) continue; // Hidden Explorer: measure when explicitly opened.
      const bar = track.querySelector(".timeline-bar");
      const label = track.querySelector(".timeline-duration");
      const start = parseFloat(bar.style.left) / 100 * width;
      const length = parseFloat(bar.style.width) / 100 * width;
      const size = label.offsetWidth;
      label.classList.remove("inside", "below");
      let left;
      if (length >= size + 12) {
        label.classList.add("inside");
        left = start + (length - size) / 2;
      } else if (width - start - length >= size + 6) {
        left = start + length + 6;
      } else if (start >= size + 6) {
        left = start - size - 6;
      } else {
        label.classList.add("below");
        left = Math.max(0, Math.min(start, width - size));
      }
      label.style.left = left + "px";
    }
  }

  // Resizing or opening the panel repositions text only, never operation intervals.
  const labelObserver = new ResizeObserver(fitDurationLabels);
  labelObserver.observe($("attempts"));
  window.addEventListener("resize", fitDurationLabels);

  function timeline(trace, scale) {
    const section = node("section", "timeline");
    section.setAttribute("aria-label", "Measured branch execution timeline");
    section.append(node("h4", "", "Actual execution timeline"));
    const layout = Core.traceLayout(trace);
    if (!layout) {
      section.append(node("p", "trace-note", "Timing unavailable. This attempt has no complete, valid recorded trace."));
      return section;
    }
    section.dataset.axisMaxMs = scale.maximum;
    const metrics = node("div", "metrics");
    metrics.append(metric("Total orchestration", layout.total), metric("Measured overlap", layout.overlap));
    section.append(metrics);
    for (const branch of layout.rows) {
      const label = branch.key === "issuer" ? "Issuer authorization" : "Risk screening";
      const row = node("div", "timeline-row");
      const heading = node("div", "timeline-label");
      heading.append(node("strong", "", label));
      const track = node("div", "timeline-track");
      track.style.setProperty("--tick-spacing", (scale.maximum ? scale.ticks[1] / scale.maximum * 100 : 100) + "%");
      const bar = node("div", "timeline-bar " + branch.key);
      // No minimum width: a zero-duration interval remains zero-width.
      bar.style.left = (scale.maximum ? branch.start_ms / scale.maximum * 100 : 0) + "%";
      bar.style.width = (scale.maximum ? (branch.end_ms - branch.start_ms) / scale.maximum * 100 : 0) + "%";
      bar.dataset.startMs = branch.start_ms;
      bar.dataset.endMs = branch.end_ms;
      bar.dataset.durationMs = branch.duration_ms;
      bar.title = `${label}: ${branch.start_ms}–${branch.end_ms} ms; duration ${branch.duration_ms} ms; ${branch.status}`;
      track.setAttribute("role", "img");
      track.setAttribute("aria-label", bar.title);
      const duration = node("span", "timeline-duration " + branch.key, Core.milliseconds(branch.duration_ms));
      duration.title = String(branch.duration_ms) + " ms";
      duration.setAttribute("aria-hidden", "true"); // The track already exposes the full interval.
      track.append(bar, duration);
      row.append(heading, track);
      section.append(row);
    }
    const axis = node("div", "axis");
    axis.setAttribute("aria-label", "Milliseconds from orchestration start");
    scale.ticks.forEach((tick, index) => {
      const label = node("span", "", Core.milliseconds(tick).replace(" ms", index === scale.ticks.length - 1 ? " ms" : ""));
      label.style.left = (scale.maximum ? tick / scale.maximum * 100 : 0) + "%";
      label.title = String(tick) + " ms";
      axis.append(label);
    });
    section.append(axis);
    section.append(node("p", "trace-note", "Measured from orchestration start. Bars include artificial I/O waits; HTTP and database time are excluded."));
    const details = node("details", "trace-details");
    details.append(node("summary", "", "Exact recorded offsets & artificial waits"));
    for (const b of layout.rows) {
      details.append(node("p", "", `${b.key}: start ${b.start_ms} ms · end ${b.end_ms} ms · duration ${b.duration_ms} ms · ${b.status} · artificial wait ${b.artificial_delay_ms} ms`));
    }
    details.append(node("p", "", `Decision at ${layout.decision} ms · total ${layout.total} ms · overlap ${layout.overlap} ms`));
    section.append(details);
    return section;
  }

  function attemptCard(attempt, label) {
    const view = Core.states[attempt.state];
    const card = node("article", "attempt-card " + view.tone);
    card.dataset.state = attempt.state;
    const top = node("div", "attempt-top");
    top.append(node("p", "attempt-label", label));
    const heading = node("div", "outcome-line");
    const icon = node("span", "outcome-icon", view.symbol);
    icon.setAttribute("aria-hidden", "true");
    const text = node("div");
    const title = node("h3", "", outcomeTitles[attempt.state]);
    title.tabIndex = -1;
    text.append(title);
    heading.append(icon, text);
    top.append(heading);
    top.append(node("p", "decision-reason", attempt.state === "ERROR"
      ? (attempt.error_reason || "A technical failure occurred. This is not a business decline.")
      : (attempt.decision_reason || "No decision explanation was returned.")));
    if (attempt.state === "ERROR") top.append(node("p", "risk-detail", "No business decision was made. This is separate from an issuer decline."));
    const details = node("dl", "customer-details");
    const request = attempt.request || {};
    for (const [key, value] of [["Sandbox amount", request.amount_pkr === undefined ? undefined : "PKR " + request.amount_pkr],
      ["Billing country", request.billing_country]]) {
      if (value !== undefined) details.append(node("dt", "", key), node("dd", "", value));
    }
    top.append(details);
    card.append(top);
    return card;
  }

  function executionCard(attempt, label, scale) {
    const card = node("article", "execution-card");
    const top = node("div", "attempt-top");
    const mode = attempt.request && attempt.request.execution_mode;
    card.dataset.mode = mode;
    const heading = node("header", "execution-heading");
    heading.append(node("p", "attempt-label", label), node("h3", "mode-title", mode === "sequential" ? "Sequential" : mode === "concurrent" ? "Concurrent" : "Execution"));
    heading.append(node("p", "execution-state", executionStates[attempt.state] || attempt.state));
    const explanation = mode === "sequential"
      ? "Sequential mode awaits issuer authorization, then risk screening. The coordinator combines both results."
      : mode === "concurrent"
        ? "Concurrent mode starts both independent checks with asyncio.TaskGroup, then joins their results for the coordinator."
        : "Recorded branch results and timing evidence for this attempt.";
    top.append(node("p", "execution-explanation", explanation));
    const results = node("div", "branch-results");
    for (const [label, value] of [["Issuer", attempt.issuer_result], ["Risk", attempt.risk_result]]) {
      const item = node("span", "branch-result", label);
      item.append(node("strong", "", value || "Unavailable"));
      results.append(item);
    }
    top.append(results);
    // M4 stores status only, not RiskResult.reasons. Do not reconstruct missing evidence.
    if (Array.isArray(attempt.risk_reasons) && attempt.risk_reasons.length) {
      top.append(node("p", "risk-detail", "Risk reasons: " + attempt.risk_reasons.join(", ")));
    } else if (attempt.risk_result === "VERIFY") {
      top.append(node("p", "risk-detail", "Individual risk reason codes are not included in the stored API response."));
    }
    const meta = node("dl", "attempt-meta");
    const request = attempt.request || {};
    for (const [key, value] of [["Attempt ID", attempt.id], ["Execution", request.execution_mode],
      ["Artificial workload", request.workload_profile], ["Synthetic inputs", `PKR ${request.amount_pkr} · ${request.card_preset} · ${request.billing_country}`]]) {
      meta.append(node("dt", "", key), node("dd", key === "Attempt ID" ? "id" : "", value));
    }
    top.append(meta);
    card.append(heading, timeline(attempt.timing_trace, scale), top);
    return card;
  }
  function render(attempt, base = null, receipt = null) {
    baseline = base || attempt;
    comparisonDone = !!base;
    $("empty-state").hidden = true;
    showingResult = true;
    // A mode comparison is educational; the customer's original outcome stays singular.
    $("payment-outcomes").replaceChildren(attemptCard(base || attempt, "Sandbox payment"));
    $("execution-output").hidden = false;
    $("execution-output-heading").textContent = base ? "Execution Comparison" : "Execution Details";
    $("attempts").replaceChildren();
    $("attempts").classList.toggle("comparison-grid", !!base);
    const executionLabel = (base ? "Comparison attempt" : "Latest attempt") + (receipt ? " · " + receipt : "");
    const entries = base ? [{ attempt: base, label: "Original attempt" }, { attempt, label: executionLabel }]
      : [{ attempt, label: executionLabel }];
    entries.sort((a, b) => Number(a.attempt.request.execution_mode === "concurrent") - Number(b.attempt.request.execution_mode === "concurrent"));
    const scale = chartScale(entries.map(entry => entry.attempt));
    for (const entry of entries) $("attempts").append(executionCard(entry.attempt, entry.label, scale));
    fitDurationLabels();
    const summary = $("comparison-summary");
    summary.hidden = !base;
    summary.replaceChildren();
    const change = $("comparison-change");
    change.hidden = true;
    change.replaceChildren();
    delete change.dataset.direction;
    if (base) {
      summary.append(node("h4", "", "Interpretation & limitations"));
      const stats = Core.comparison(base, attempt);
      const recordedOutcomes = [base, attempt].every(entry => entry.state !== "ERROR"
        && Object.hasOwn(Core.states, entry.state) && ["APPROVED", "DECLINED"].includes(entry.issuer_result)
        && ["CLEAR", "VERIFY"].includes(entry.risk_result));
      if (recordedOutcomes && stats && Number.isFinite(stats.reduction)) {
        const reduction = stats.reduction;
        change.dataset.direction = reduction > 0 ? "lower" : reduction < 0 ? "higher" : "equal";
        const rounded = Math.abs(reduction).toFixed(1);
        // Keep small nonzero changes distinct from an actually equal pair.
        change.append(node("strong", "", reduction !== 0 && rounded === "0.0" ? "<0.1%" : rounded + "%"));
        change.append(node("span", "", reduction === 0 ? "No orchestration time difference in this pair"
          : `${reduction > 0 ? "Lower" : "Higher"} orchestration time in this pair`));
        change.hidden = false;
      } else {
        summary.append(node("p", "", recordedOutcomes && stats && stats.reduction === null
          ? "Percentage unavailable: the sequential duration is zero."
          : "Timing comparison unavailable: both attempts need matching inputs and business outcomes, plus complete valid traces."));
      }
      if (recordedOutcomes) {
        const matching = base.state === attempt.state && base.issuer_result === attempt.issuer_result
          && base.risk_result === attempt.risk_result;
        summary.append(node("p", "", matching ? "Both attempts produced the same business outcome."
          : "The attempts produced different business outcomes."));
      } else {
        summary.append(node("p", "", "Matching business outcomes could not be confirmed from both recorded results."));
      }
      summary.append(node("p", "", "Illustrative only. Two observations are not a formal benchmark."));
    }
  }

  async function run(compare = false) {
    if (client.busy) return;
    let work;
    try {
      if (client.pending) work = client.retry();
      else {
        const input = compare ? Core.otherMode(baseline.request) : Core.normalize(Object.fromEntries(new FormData(form)));
        setForm(input);
        if (!compare) {
          baseline = null;
          comparisonDone = false;
          showingResult = false;
          // Retain recorded evidence until a completed response replaces it.
          // The introduction is initial idle content, never a processing placeholder.
          $("empty-state").hidden = true;
        }
        work = client.start(input, compare ? baseline : null);
      }
      message("Processing your sandbox payment...", "pending");
      controls();
      const result = await work;
      render(result.attempt, result.base, result.receipt);
      message("");
      controls();
      $("payment-outcomes").querySelector("h3").focus({ preventScroll: true });
    } catch (error) {
      message(error.message || "The attempt could not be confirmed. Retry the same attempt.", "error");
    } finally { controls(); }
  }

  form.addEventListener("submit", (event) => { event.preventDefault(); if (!form.hidden) run(); });
  $("retry-button").addEventListener("click", () => { if (client.pending) run(); });
  $("another-payment").addEventListener("click", () => {
    if (client.busy || client.pending) return;
    showingResult = false;
    $("payment-outcomes").replaceChildren();
    message("");
    controls();
    $("amount").focus();
  });
  $("compare-button").addEventListener("click", () => run(true));
  $("simulation-settings").addEventListener("change", modeHelp);
  $("explorer-toggle").addEventListener("click", () => {
    const expanded = $("explorer-toggle").getAttribute("aria-expanded") !== "true";
    $("explorer-toggle").setAttribute("aria-expanded", String(expanded));
    $("explorer-panel").hidden = !expanded;
    if (expanded) fitDurationLabels();
  });
  document.querySelector(".explorer-link").addEventListener("click", () => $("explorer-toggle").focus());
  if (pending) {
    setForm(pending.payload);
    if (pending.base && Object.hasOwn(Core.states, pending.base.state)) render(pending.base);
    message("An unresolved attempt was restored. Retry uses its original key and inputs; it will not create a second payment.", "error");
  }
  controls();
})();
