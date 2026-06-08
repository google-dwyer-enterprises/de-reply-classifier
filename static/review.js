/* Lead Reviewer — selection + apply logic for the per-batch review page.
 *
 * Owns:
 *   - per-row checkbox toggle
 *   - shift-click range selection
 *   - header checkbox: select/deselect all visible
 *   - quick-action buttons (all / pending only / none / mass-approve / mass-reject)
 *   - Apply button: POST /batch/<token>/bulk-update with selected ids
 *   - toast feedback
 *
 * No framework. Single closure. ~130 lines.
 */
(function () {
  const ab = document.getElementById("action-bar");
  if (!ab) return;
  const token = ab.dataset.token;

  const headerCheck = document.getElementById("header-check");
  const rowChecks = () => Array.from(document.querySelectorAll(".row-check"));
  const allRows = () => Array.from(document.querySelectorAll("#lead-table tbody tr[data-id]"));

  const selectedCount = document.getElementById("selected-count");
  const applyBtn = document.getElementById("apply-btn");
  const statusSelect = document.getElementById("bulk-status");

  let lastClickedIndex = null;

  function rowIndex(checkbox) {
    return rowChecks().indexOf(checkbox);
  }

  function selectedIds() {
    return rowChecks().filter(c => c.checked).map(c => parseInt(c.value, 10));
  }

  function syncSelectionState() {
    const ids = selectedIds();
    selectedCount.textContent = `${ids.length} lead${ids.length === 1 ? "" : "s"} selected`;
    applyBtn.disabled = ids.length === 0;
    rowChecks().forEach(c => {
      const tr = c.closest("tr");
      if (tr) tr.classList.toggle("selected", c.checked);
    });
    const allChecked = rowChecks().length > 0 && rowChecks().every(c => c.checked);
    if (headerCheck) headerCheck.checked = allChecked;
  }

  // Per-row checkbox click — with shift-click range support
  function onRowCheckClick(ev) {
    const cb = ev.currentTarget;
    const idx = rowIndex(cb);
    if (ev.shiftKey && lastClickedIndex !== null) {
      const checks = rowChecks();
      const [from, to] = [lastClickedIndex, idx].sort((a, b) => a - b);
      const targetState = cb.checked;
      for (let i = from; i <= to; i++) {
        checks[i].checked = targetState;
      }
    }
    lastClickedIndex = idx;
    syncSelectionState();
  }

  rowChecks().forEach(cb => cb.addEventListener("click", onRowCheckClick));

  // Header checkbox
  if (headerCheck) {
    headerCheck.addEventListener("change", () => {
      const target = headerCheck.checked;
      rowChecks().forEach(c => { c.checked = target; });
      lastClickedIndex = null;
      syncSelectionState();
    });
  }

  // Quick actions
  document.getElementById("qa-all")?.addEventListener("click", () => {
    rowChecks().forEach(c => { c.checked = true; });
    syncSelectionState();
  });
  document.getElementById("qa-none")?.addEventListener("click", () => {
    rowChecks().forEach(c => { c.checked = false; });
    syncSelectionState();
  });
  document.getElementById("qa-pending")?.addEventListener("click", () => {
    allRows().forEach(tr => {
      const isPending = (tr.dataset.status || "pending") === "pending";
      const cb = tr.querySelector(".row-check");
      if (cb) cb.checked = isPending;
    });
    syncSelectionState();
  });

  // Mass-approve / mass-reject
  document.getElementById("qa-mass-approve")?.addEventListener("click", () => mass("approve"));
  document.getElementById("qa-mass-reject")?.addEventListener("click", () => mass("reject"));

  async function mass(kind) {
    const verb = kind === "approve" ? "approve" : "reject";
    if (!confirm(`Mass-${verb} the ENTIRE batch?\n\nThe worker will flip every still-pending lead to ${verb}d and ${verb === "approve" ? "move approved ones into the 200K pool" : "leave them as audit only"} within ~30 seconds.`)) {
      return;
    }
    const url = `/batch/${token}/mass-${verb}`;
    try {
      const r = await fetch(url, { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      toast(`Batch mass-${verb}d. Worker will finish within ~30 seconds.`);
      setTimeout(() => location.reload(), 1500);
    } catch (e) {
      toast(`Mass-${verb} failed: ${e.message}`, /*error=*/true);
    }
  }

  // Apply button: bulk-update
  applyBtn?.addEventListener("click", async () => {
    const ids = selectedIds();
    if (!ids.length) return;
    const status = statusSelect.value;
    applyBtn.disabled = true;
    applyBtn.textContent = "Applying…";
    try {
      const r = await fetch(`/batch/${token}/bulk-update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_ids: ids, status }),
      });
      const body = await r.json();
      if (!r.ok || !body.ok) throw new Error(body.error || `HTTP ${r.status}`);

      toast(`${body.updated} lead${body.updated === 1 ? "" : "s"} set to ${status}. Worker will move approved leads within ~30s.`);

      // Fade out the rows whose status is no longer "pending" (and therefore
      // shouldn't display in a freshly-loaded review page). For the static
      // page, we keep them visible until the user refreshes — but mark them
      // so the chip updates and they're not still in the selection set.
      ids.forEach(id => {
        const tr = document.querySelector(`tr[data-id="${id}"]`);
        if (!tr) return;
        tr.dataset.status = status;
        const chip = tr.querySelector("td:last-child .badge");
        if (chip) {
          chip.className = `badge badge-${status}`;
          chip.textContent = status;
        }
        const cb = tr.querySelector(".row-check");
        if (cb) cb.checked = false;
      });
      syncSelectionState();
    } catch (e) {
      toast(`Apply failed: ${e.message}`, /*error=*/true);
    } finally {
      applyBtn.textContent = "Apply →";
      applyBtn.disabled = selectedIds().length === 0;
    }
  });

  function toast(msg, isError) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle("error", !!isError);
    el.style.display = "block";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.style.display = "none"; }, 4500);
  }

  syncSelectionState();
})();
