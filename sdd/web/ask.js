const $ = (id) => document.getElementById(id);
const saved = sessionStorage.getItem("sdd-token");
if (saved) $("token").value = saved;
async function request(path, body) {
  const token = $("token").value.trim();
  if (!token)
    throw new Error(
      "Connect with your local API token first. It is in SDD_API_TOKENS in the project .env file.",
    );
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
async function connect() {
  try {
    const catalog = await request("/catalog");
    sessionStorage.setItem("sdd-token", $("token").value.trim());
    for (const [id, rows] of [
      ["evaluator", catalog.evaluators],
      ["policy", catalog.policies],
    ]) {
      $(id).replaceChildren(new Option("Choose automatically", ""));
      for (const row of rows)
        $(id).add(
          new Option(
            id === "evaluator"
              ? `${row.provider} / ${row.model}`
              : `Yes ≥ ${row.accept} · No ≤ ${row.reject}`,
            row.id,
          ),
        );
    }
    $("connection").textContent = "Connected";
  } catch (error) {
    $("connection").textContent = error.message;
  }
}
function textBlock(parent, tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.append(node);
  return node;
}
function show(data) {
  $("output").hidden = false;
  $("metrics").replaceChildren();
  $("table").replaceChildren();
  $("answer").textContent = "";
  $("interpretation").replaceChildren();
  if (data.interpretation) {
    const i = data.interpretation;
    const scope = i.scope.map((s) => `${s.field}: ${s.value}`).join(", ");
    textBlock(
      $("interpretation"),
      "p",
      `${i.operation.charAt(0).toUpperCase() + i.operation.slice(1)} ${i.grain === "customer" ? "distinct customers" : "messages"}${scope ? " · " + scope : ""}${i.group_by ? " · grouped by " + i.group_by : ""}.`,
    );
    textBlock(
      $("interpretation"),
      "p",
      i.quantifier === "not_exists"
        ? "Include only customers with no matching messages and no unresolved membership."
        : "Include records that satisfy this condition:",
    );
    textBlock($("interpretation"), "p", i.condition);
    if (i.start_inclusive || i.end_exclusive)
      textBlock(
        $("interpretation"),
        "p",
        `Date range (UTC): ${i.start_inclusive || "any start"} inclusive to ${i.end_exclusive || "any end"} exclusive.`,
      );
  }
  $("details").textContent = JSON.stringify(data, null, 2);
  if (!data.supported) {
    $("status").className = "incomplete";
    $("status").textContent = "Please clarify your question";
    $("answer").textContent = data.reason;
    return;
  }
  if (!data.manifest) {
    $("status").className = "success";
    $("status").textContent = "Plan ready — no evaluations performed";
    return;
  }
  const m = data.manifest;
  $("status").className = m.complete ? "success" : "incomplete";
  $("status").textContent = m.complete
    ? "Complete under the selected decision policy"
    : "Partial answer — unresolved judgments remain";
  for (const [value, label] of [
    [m.eligible_subjects, "Messages in scope"],
    [m.reused_observations, "Observations reused"],
    [m.unresolved_subjects, "Unresolved messages"],
    [m.scheduled_pairs, "Evaluation pairs scheduled"],
  ]) {
    const card = textBlock($("metrics"), "div", "");
    card.className = "metric";
    textBlock(card, "strong", String(value));
    textBlock(card, "span", label);
  }
  if (m.count_bounds)
    $("answer").textContent =
      m.count_bounds.confirmed === m.count_bounds.possible_maximum
        ? `${m.count_bounds.confirmed} matching ${data.plan.grain}s.`
        : `${m.count_bounds.confirmed} confirmed matching ${data.plan.grain}s; up to ${m.count_bounds.possible_maximum} could match once unresolved cases are decided.`;
  const rows = data.result || [];
  if (rows.length) {
    const table = textBlock($("table"), "table", ""),
      head = textBlock(table, "thead", ""),
      hr = textBlock(head, "tr", "");
    Object.keys(rows[0]).forEach((k) => textBlock(hr, "th", k));
    const body = textBlock(table, "tbody", "");
    rows.forEach((row) => {
      const tr = textBlock(body, "tr", "");
      Object.values(row).forEach((value) => textBlock(tr, "td", String(value)));
    });
  } else $("answer").textContent = "No resolved matches in the selected population.";
}
async function ask(execute) {
  $("run").disabled = $("preview").disabled = true;
  $("output").hidden = false;
  $("status").textContent = execute ? "Evaluating your question…" : "Interpreting your question…";
  $("status").className = "";
  try {
    const body = {
      question: $("question").value,
      execute,
      max_evaluations: Number($("budget").value),
    };
    if ($("evaluator").value) body.evaluator_id = $("evaluator").value;
    if ($("policy").value) body.policy_id = $("policy").value;
    show(await request("/ask", body));
  } catch (error) {
    $("status").className = "error";
    $("status").textContent = error.message;
  } finally {
    $("run").disabled = $("preview").disabled = false;
  }
}
$("connect").addEventListener("click", connect);
$("query-form").addEventListener("submit", (event) => {
  event.preventDefault();
  ask(true);
});
$("preview").addEventListener("click", () => ask(false));
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.altKey)) {
    event.preventDefault();
    ask(event.ctrlKey);
  }
});
document.querySelectorAll(".examples button").forEach((button) =>
  button.addEventListener("click", () => {
    $("question").value = button.textContent;
    $("question").focus();
  }),
);
if (saved) connect();
