const $ = (id) => document.getElementById(id);
let catalog = [],
  pending = null,
  busy = false;
let draft = {};
try {
  draft = JSON.parse(sessionStorage.getItem("sdd-workspace") || "{}");
} catch {
  draft = {};
}
const saved = sessionStorage.getItem("sdd-token");
if (saved) $("token").value = saved;
$("question").value = draft.question || "";
$("mode").value = draft.mode === "sql" ? "sql" : "natural";
$("budget").value = draft.budget ?? 100;
$("planner-mode").value = draft.plannerMode === "hybrid" ? "hybrid" : "jev";
for (const id of ["import-json", "feature-json", "feature-review"]) $(id).value = draft[id] || "";

function text(parent, tag, value) {
  const element = document.createElement(tag);
  element.textContent = value;
  parent.append(element);
  return element;
}

async function request(path, body) {
  const token = $("token").value.trim();
  if (!token) throw new Error(t("tokenRequired"));
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(apiErrorMessage(response.status, data));
    error.data = data;
    throw error;
  }
  return data;
}

function selected() {
  return [...$("datasets").selectedOptions].map((option) => option.value);
}

function describeDataset(dataset) {
  const columns = dataset.columns.map((column) => {
    const primaryKey = dataset.primary_key.includes(column.name) ? ` · ${t("primaryKey")}` : "";
    return `  ${column.name} · ${t(column.type + "Type")}${primaryKey}`;
  });
  const relationships = dataset.links.map((link) => {
    const target = catalog.find((table) => table.id === link.target_id);
    return `  ${link.source_column} → ${target?.name}.${link.target_column}`;
  });
  return `${dataset.name}: ${dataset.description}\n${columns.join("\n")}\n${relationships.join("\n")}`;
}

function schema() {
  const ids = selected();
  const datasets = ids.length ? catalog.filter((dataset) => ids.includes(dataset.id)) : catalog;
  $("schema").textContent = datasets.map(describeDataset).join("\n\n") || t("catalogEmpty");
  $("scope").textContent = ids.length ? t("scopeSelected", { count: ids.length }) : t("scopeAll");
  $("catalog-count").textContent = t("catalogCount", { count: catalog.length });
  $("examples").replaceChildren();
  text($("examples"), "span", t("examples"));
  const dataset = datasets[0];
  if (!dataset) return;
  const numericColumn = dataset.columns.find(
    (column) =>
      ["integer", "number"].includes(column.type) && !dataset.primary_key.includes(column.name),
  );
  const values = { dataset: dataset.name, column: numericColumn?.name };
  const questions = [t("countExample", values), t("rowsExample", values)];
  if (numericColumn) questions.push(t("averageExample", values));
  for (const question of questions) {
    const button = text($("examples"), "button", question);
    button.addEventListener("click", () => {
      $("mode").value = "natural";
      updateMode();
      $("question").value = question;
      $("question").focus();
    });
  }
}

async function connect() {
  $("connect").disabled = true;
  $("connection-error").hidden = true;
  $("connection").textContent = t("connecting");
  try {
    const old = catalog.length ? selected() : draft.datasets || [];
    catalog = (await request("/datasets")).datasets;
    sessionStorage.setItem("sdd-token", $("token").value.trim());
    $("datasets").replaceChildren();
    for (const dataset of catalog) {
      const option = new Option(dataset.name, dataset.id);
      option.selected = old.includes(dataset.id);
      $("datasets").add(option);
    }
    schema();
    $("connection").textContent = t("connected");
    $("connection-badge").dataset.connected = "true";
    $("connection-panel").open = false;
    document.dispatchEvent(new Event("sdd:connected"));
  } catch (error) {
    $("connection").textContent = t("disconnected");
    $("connection-error").textContent = errorMessage(error);
    $("connection-error").hidden = false;
    $("connection-badge").dataset.connected = "false";
    $("connection-panel").open = true;
  } finally {
    $("connect").disabled = false;
  }
}

function renderTable(rows, isPlan = false) {
  $("table").replaceChildren();
  if (!rows.length) {
    text($("table"), "p", t(isPlan ? "planRows" : "noRows")).className = "table-message";
    return;
  }
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const table = text($("table"), "table", "");
  const head = text(text(table, "thead", ""), "tr", "");
  keys.forEach((key) => {
    const th = text(head, "th", key);
    th.scope = "col";
  });
  const body = text(table, "tbody", "");
  for (const row of rows) {
    const tr = text(body, "tr", "");
    keys.forEach((key) =>
      text(
        tr,
        "td",
        row[key] === null
          ? "NULL"
          : typeof row[key] === "object"
            ? JSON.stringify(row[key])
            : String(row[key] ?? ""),
      ),
    );
  }
}

function selectResultTab(name) {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const active = tab.id === `tab-${name}`;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !active;
  }
}

function clearOutput() {
  $("metrics").replaceChildren();
  $("table").replaceChildren();
  $("answer").textContent = "";
  $("sql").textContent = t("noSql");
  $("details").textContent = t("noEvidence");
  $("review-sql").hidden = true;
  $("mutation").hidden = true;
  $("output-empty").hidden = false;
  pending = null;
  $("planning-review").hidden = true;
}

function show(data) {
  clearOutput();
  $("output-empty").hidden = true;
  $("sql").textContent = data.logical_sql || data.plan?.logical_sql || t("noSql");
  $("details").textContent = JSON.stringify(data, null, 2);
  renderTable(data.result || data.before_sample || [], !data.manifest);
  $("review-sql").hidden = !(data.review_required && (data.logical_sql || data.plan?.logical_sql));
  renderPlanningReview(data);
  const manifest = data.manifest;
  selectResultTab(!manifest || data.review_required ? "sql" : "table");
  document.dispatchEvent(new CustomEvent("sdd:result", { detail: data }));
  $("status").className = manifest?.complete === false ? "incomplete" : "success";
  if (data.mutation_preview) {
    pending = data.preview_token;
    $("status").textContent = t("previewReady");
    $("mutation").hidden = false;
    $("mutation-description").textContent = t("mutationDescription", { count: data.affected_rows });
    $("mutation-values").textContent = JSON.stringify(data.changes_sample, null, 2);
  } else if (!manifest) {
    $("status").textContent = t("planReady");
  } else if (manifest.committed) {
    $("status").textContent = t("committed", { count: manifest.affected_rows });
  } else {
    $("status").textContent = t(manifest.complete ? "complete" : "partial");
    $("answer").textContent =
      t("resultRows", { count: data.result?.length || 0 }) +
      (manifest.truncated ? t("truncated") : "");
    if (!manifest.complete) $("answer").textContent += " · " + t("partialHint");
  }
  if (manifest) {
    for (const [value, label] of [
      [manifest.source_rows, "sourceRows"],
      [manifest.semantic_coverage?.reused, "reused"],
      [manifest.semantic_coverage?.evaluated, "evaluated"],
      [manifest.semantic_coverage?.unknown, "unknown"],
    ]) {
      const metric = text($("metrics"), "div", "");
      metric.className = "metric";
      text(metric, "strong", String(value ?? 0));
      text(metric, "span", t(label));
    }
  }
}

function showError(error) {
  if (error.data?.review_required) {
    show(error.data);
    $("status").className = "incomplete";
  } else {
    clearOutput();
    selectResultTab("table");
    $("status").className = "error";
    if (error.data) $("details").textContent = JSON.stringify(error.data, null, 2);
  }
  $("status").textContent = errorMessage(error);
}

function updateMode() {
  $("question").placeholder = t(
    $("mode").value === "sql" ? "sqlPlaceholder" : "questionPlaceholder",
  );
  $("preview").disabled = busy || $("mode").value === "sql";
  $("planner-mode").disabled = busy || $("mode").value === "sql";
  const helpKey = $("mode").value === "sql"
    ? "plannerSqlHelp"
    : $("planner-mode").value === "hybrid" ? "plannerHybridHelp" : "plannerJevHelp";
  $("planner-help").textContent = t(helpKey);
}

async function run(execute) {
  if (busy) return;
  busy = true;
  $("run").disabled = $("preview").disabled = true;
  $("run").firstElementChild.textContent = t("running");
  $("output").setAttribute("aria-busy", "true");
  clearOutput();
  $("status").className = "";
  $("status").textContent = t("planning");
  try {
    ensureHistoryScope();
    const question = $("question").value;
    const budget = Number($("budget").value);
    if (!question.trim()) throw new Error(t("queryRequired"));
    if (!Number.isInteger(budget) || budget < 0 || budget > 1000)
      throw new Error(t("invalidInput"));
    if ($("mode").value === "sql") {
      if (!execute) throw new Error(t("sqlRunRequired"));
      show(
        await request("/data/sql", {
          sql: question,
          max_evaluations: budget,
          parent_history_id: historyParentId,
        }),
      );
    } else {
      show(
        await request("/ask", {
          question,
          execute,
          parent_history_id: historyParentId,
          dataset_ids: selected().length ? selected() : null,
          planner_mode: $("planner-mode").value,
          max_evaluations: budget,
        }),
      );
    }
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    $("run").disabled = false;
    $("run").firstElementChild.textContent = t("run");
    $("output").setAttribute("aria-busy", "false");
    updateMode();
    refreshHistory();
  }
}

$("connect").addEventListener("click", connect);
$("token").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("connect").disabled) {
    event.preventDefault();
    connect();
  }
});
$("datasets").addEventListener("change", schema);
$("all-datasets").addEventListener("click", () => {
  for (const option of $("datasets").options) option.selected = false;
  $("datasets").dispatchEvent(new Event("change"));
});
$("mode").addEventListener("change", updateMode);
$("planner-mode").addEventListener("change", updateMode);
$("query-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(true);
});
$("preview").addEventListener("click", () => run(false));
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey || event.altKey)) {
    event.preventDefault();
    run(event.ctrlKey || event.metaKey);
  }
});
$("review-sql").addEventListener("click", () => {
  $("mode").value = "sql";
  updateMode();
  $("question").value = $("sql").textContent;
  $("question").focus();
});
$("commit").addEventListener("click", async () => {
  if (!pending || busy) return;
  busy = true;
  $("commit").disabled = true;
  $("commit").textContent = t("committing");
  $("run").disabled = $("preview").disabled = true;
  try {
    show(await request(`/data/mutations/${pending}/commit`, {}));
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    $("commit").disabled = $("run").disabled = false;
    $("commit").textContent = t("commit");
    updateMode();
  }
});
$("import").addEventListener("click", async () => {
  $("import").disabled = true;
  try {
    const dataset = await request("/datasets", JSON.parse($("import-json").value));
    $("import-status").textContent = t("imported", { name: dataset.name });
    await connect();
  } catch (error) {
    $("import-status").textContent = errorMessage(error);
  } finally {
    $("import").disabled = false;
  }
});
const tabs = [...document.querySelectorAll('[role="tab"]')];
for (const tab of tabs) {
  tab.addEventListener("click", () => selectResultTab(tab.id.slice(4)));
  tab.addEventListener("keydown", (event) => {
    const index = tabs.indexOf(tab);
    const target =
      event.key === "ArrowRight"
        ? (index + 1) % tabs.length
        : event.key === "ArrowLeft"
          ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? tabs.length - 1
              : null;
    if (target === null) return;
    event.preventDefault();
    selectResultTab(tabs[target].id.slice(4));
    tabs[target].focus();
  });
}
for (const link of document.querySelectorAll("[data-language]")) {
  link.addEventListener("click", () => {
    sessionStorage.setItem(
      "sdd-workspace",
      JSON.stringify({
        question: $("question").value,
        mode: $("mode").value,
        plannerMode: $("planner-mode").value,
        budget: $("budget").value,
        datasets: selected(),
        history_parent_id: historyParentId,
        ...Object.fromEntries(
          ["import-json", "feature-json", "feature-review"].map((id) => [id, $(id).value]),
        ),
      }),
    );
  });
}
$("catalog-count").textContent = t("catalogCount", { count: 0 });
$("brand-home")?.setAttribute("href", `/ask/${locale}`);
updateMode();
if (saved) connect();
