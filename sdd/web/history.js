let historyParentId = draft.history_parent_id || null;
let historyEntry = null;
let historyCursor = null;
let historyRequest = 0;
let missingHistoryDatasets = false;

function historyDate(value) {
  return new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-GB", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function showHistoryContext(entry) {
  historyEntry = entry;
  historyParentId = entry.id;
  $("history-context").hidden = false;
  $("history-original").textContent = entry.input.text;
  $("history-edit-question").hidden = entry.input.mode !== "natural";
  $("history-edit-sql").hidden = !entry.output.logical_sql && entry.input.mode !== "sql";
  $("history-review").hidden = !entry.has_decisions;
  for (const button of document.querySelectorAll("[data-history-id]"))
    button.setAttribute("aria-current", String(button.dataset.historyId === entry.id));
}

async function refreshHistory(older = false) {
  if (!$("token").value.trim()) return;
  const generation = ++historyRequest;
  $("refresh-history").disabled = true;
  $("history-more").disabled = true;
  try {
    const data = await request(
      "/query-history?limit=20" +
        (older && historyCursor ? `&before=${encodeURIComponent(historyCursor)}` : ""),
    );
    if (generation !== historyRequest) return;
    if (!older) $("history-list").replaceChildren();
    for (const item of data.items) {
      const row = text($("history-list"), "li", "");
      const button = text(row, "button", "");
      button.className = "history-item";
      button.dataset.historyId = item.id;
      button.setAttribute("aria-current", String(item.id === historyParentId));
      button.disabled = item.status === "redacted";
      text(
        button,
        "span",
        item.status === "redacted" ? t("history_redacted") : item.title,
      ).className = "history-item-title";
      text(
        button,
        "span",
        `${historyDate(item.created_at)} · ${t("history_" + item.status)}${item.parent_id ? " · " + t("historyRevision") : ""}`,
      ).className = "history-item-meta";
      button.addEventListener("click", () => openHistory(item.id));
    }
    historyCursor = data.next_cursor;
    $("history-more").hidden = !historyCursor;
    $("history-status").textContent = $("history-list").children.length ? "" : t("historyEmpty");
    if (historyParentId && !historyEntry) {
      try {
        const entry = await request(`/query-history/${encodeURIComponent(historyParentId)}`);
        if (generation === historyRequest) showHistoryContext(entry);
      } catch {
        if (generation === historyRequest) resetHistoryContext();
      }
    }
  } catch (error) {
    if (generation === historyRequest) $("history-status").textContent = errorMessage(error);
  } finally {
    if (generation === historyRequest)
      $("refresh-history").disabled = $("history-more").disabled = false;
  }
}

function restoreHistoryInput(entry, mode = entry.input.mode) {
  $("mode").value = mode;
  $("planner-mode").value = entry.input.planner_mode === "hybrid" ? "hybrid" : "jev";
  $("question").value =
    mode === "sql" ? entry.output.logical_sql || entry.input.text : entry.input.text;
  $("budget").value = entry.input.max_evaluations ?? 100;
  const ids = entry.dataset_ids || [];
  for (const option of $("datasets").options) option.selected = ids.includes(option.value);
  missingHistoryDatasets = ids.some(
    (id) => !catalog.some((dataset) => dataset.id === id || dataset.name === id),
  );
  $("history-warning").hidden = !missingHistoryDatasets;
  $("history-warning").textContent = t("historyMissingDatasets");
  schema();
  updateMode();
  $("question").focus();
}

async function openHistory(identity) {
  if (busy) return;
  busy = true;
  const token = $("token").value;
  $("run").disabled = $("preview").disabled = true;
  try {
    const entry = await request(`/query-history/${encodeURIComponent(identity)}`);
    if (token !== $("token").value) return;
    showHistoryContext(entry);
    restoreHistoryInput(entry);
    show(entry.output);
    $("status").className = "";
    $("status").textContent = t("historyLoaded");
    $("query-form").scrollIntoView({ block: "center", behavior: "smooth" });
  } catch (error) {
    $("history-status").textContent = errorMessage(error);
  } finally {
    busy = false;
    $("run").disabled = false;
    updateMode();
  }
}

function ensureHistoryScope() {
  if (missingHistoryDatasets) throw new Error(t("historyMissingDatasets"));
}

function resetHistoryContext() {
  historyParentId = null;
  historyEntry = null;
  missingHistoryDatasets = false;
  $("history-context").hidden = true;
  $("history-warning").hidden = true;
  for (const button of document.querySelectorAll("[data-history-id]"))
    button.removeAttribute("aria-current");
}

$("refresh-history").addEventListener("click", () => refreshHistory());
$("history-more").addEventListener("click", () => refreshHistory(true));
$("history-new").addEventListener("click", () => {
  if (busy) return;
  resetHistoryContext();
  clearOutput();
  $("question").value = "";
  $("status").textContent = t("ready");
  $("question").focus();
});
for (const [id, mode] of [
  ["history-edit-question", "natural"],
  ["history-edit-sql", "sql"],
]) {
  $(id).addEventListener("click", () => {
    if (!historyEntry || busy) return;
    clearOutput();
    restoreHistoryInput(historyEntry, mode);
    $("status").textContent = t("historyLoaded");
  });
}
$("history-review").addEventListener("click", async () => {
  if (!historyEntry || busy) return;
  busy = true;
  $("run").disabled = $("preview").disabled = $("history-review").disabled = true;
  try {
    const data = await request(`/query-history/${encodeURIComponent(historyEntry.id)}/review`, {});
    restoreHistoryInput(historyEntry, "natural");
    show(data);
    if (data.review_required) $("status").textContent = t("reviewRequired");
  } catch (error) {
    $("history-warning").textContent = errorMessage(error);
    $("history-warning").hidden = false;
  } finally {
    busy = false;
    $("run").disabled = $("history-review").disabled = false;
    updateMode();
  }
});
$("datasets").addEventListener("change", () => {
  missingHistoryDatasets = false;
  $("history-warning").hidden = true;
});
$("token").addEventListener("input", () => {
  ++historyRequest;
  resetHistoryContext();
  $("history-list").replaceChildren();
  $("history-more").hidden = true;
  $("history-status").textContent = t("historyConnect");
});
document.addEventListener("sdd:connected", () => refreshHistory());
document.addEventListener("sdd:result", (event) => {
  if (!event.detail.history_id) return;
  historyParentId = event.detail.history_id;
  historyEntry = null;
  $("history-context").hidden = true;
  refreshHistory();
});

function invalidateDraftActions() {
  activeReview = null;
  pending = null;
  $("planning-review").hidden = true;
  $("mutation").hidden = true;
}
$("question").addEventListener("input", invalidateDraftActions);
$("mode").addEventListener("change", invalidateDraftActions);
$("planner-mode").addEventListener("change", invalidateDraftActions);
$("datasets").addEventListener("change", invalidateDraftActions);
