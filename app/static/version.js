/* Optional release display: no checkout state, storage or request dependencies. */
(async function () {
  "use strict";
  const badge = document.getElementById("version-badge");
  if (!badge) return;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch("/api/version", { signal: controller.signal, cache: "no-store" });
    if (!response.ok) throw new Error("Version unavailable");
    const metadata = await response.json();
    if (metadata.version === "dev" && metadata.commit === "dev") {
      badge.textContent = "AsaanPay dev";
    } else if (typeof metadata.version === "string" && /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(metadata.version)
      && typeof metadata.commit === "string" && /^[0-9a-f]{40}$/.test(metadata.commit)) {
      badge.textContent = `AsaanPay v${metadata.version} · ${metadata.commit.slice(0, 7)}`;
    } else throw new Error("Invalid version");
  } catch (_) {
    badge.textContent = "AsaanPay · version unavailable";
  } finally {
    clearTimeout(timeout);
  }
})();
