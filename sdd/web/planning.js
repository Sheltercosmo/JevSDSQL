let activeReview = null;

function decisionTitle(decision) {
  const key = decision.key;
  if (key === "hybrid_candidate") return t("hybridCandidate");
  if (key.startsWith("check_role_")) return t("hybridAnswerFieldCheck");
  if (/^check_[a-z]+\d+_overall$/.test(key)) return t("hybridOverallCheck");
  if (/^check_[a-z]+\d+_operation\d+$/.test(key)) return t("hybridOperationCheck") + " · " + (Number(key.match(/operation(\d+)$/)[1]) + 1);
  if (key.startsWith("check_operation_")) return t("hybridOperationCheck") + " · " + (Number(key.split("_").at(-1)) + 1);
  if (key.startsWith("check_additional_")) return t("hybridDataCheck");
  if (key.startsWith("check_retrieve_")) return t("hybridRetrievalCheck");
  if (key === "check_overall_appropriate") return t("hybridOverallCheck");
  if (key === "check_concept_expansion") return t("hybridConceptCheck");
  if (key.startsWith("check_preserve_")) return t("hybridPopulationCheck");
  if (/^check_[a-z]+\d+_/.test(key)) return t("hybridCandidateCheck") + " · " + key.split("_")[1];
  const kind = key.startsWith("dag_")
    ? key.replace(/_(?:[cf]?\d+).*$/, "")
    : key.startsWith("metric_")
    ? "metric"
    : key.startsWith("check_graph_")
      ? "check_graph"
      : key.startsWith("ratio_numerator_")
        ? "ratio_numerator"
        : key.startsWith("ratio_denominator_")
          ? "ratio_denominator"
          : key.replace(/_(?:[cf]?\d+).*$/, "");
  const title = t("decision_" + (/^j\d+$/.test(kind) ? "join" : kind));
  const field = decision.field;
  const position = key.match(/^(?:output|sort|direction|period)_(\d+)/);
  const suffix = position ? " · " + (Number(position[1]) + 1) : key.startsWith("metric_") ? " · " + key.split("_").at(-1).toUpperCase() : "";
  return title + (field ? ` · ${field.table}.${field.column || field.name}` : "") + suffix;
}

function optionTitle(decision, option) {
  if (option.sql) return option.sql;
  if (decision.key.startsWith("check_role_") || /^check_[a-z]+\d+_issue$/.test(decision.key))
    return t("hybridReviewOption_" + option.id);
  if (decision.key.startsWith("dag_")) {
    const title = stageOptionTitle(decision, option);
    if (title !== null) return title;
  }
  if (decision.key === "arithmetic_expression") {
    if (option.id === "0") return t("option_identity");
    const expression = option.label.match(/^(.+?) \([^)]*\)[\s\S]*? ([*+/-]) (.+?) \(/u);
    if (expression) return expression[1] + " " + expression[2] + " " + expression[3];
  }
  if (/^(output|sort)_/.test(decision.key) && option.id === "baseline") return t("expression_average");
  if (["candidate", "repair_candidate"].includes(decision.key))
    return t("reviewCandidate", { count: Number(option.id) + 1 }) + " · " + option.label.split("\n").slice(1).join(" ");
  if (/^f\d+$/.test(option.id)) return option.label.split(" (")[0];
  if (locale === "zh" && /^f\d+@/.test(option.id))
    return option.label.split(" (")[0] + " · " + option.label.split(" observed at ").at(-1);
  if (locale === "zh" && /^(gain|loss|percent):/.test(option.id))
    return t("expression_" + option.id.split(":")[0]) + " · " + option.label.split(" of ")[1].split(" (")[0] + " · " + option.label.split(" from ").at(-1).replace(" to ", " → ");
  if (locale === "zh" && option.id.startsWith("weighted_gain"))
    return t("expression_weighted_gain") + " · " + option.label.split(" from ").at(-1).split(",")[0].replace(" to ", " → ");
  if (decision.key === "root")
    return option.id === "none" ? t("option_none") : option.label.split(" — ")[0];
  if (locale === "zh" && /^v\d+$/.test(option.id))
    return option.label.split(" — ")[0];
  if (locale === "zh" && option.id.startsWith("metric:")) {
    const [, operation] = option.id.split(":");
    return t("option_" + operation) + " · " + option.label.split(" of ")[1].split(" (")[0];
  }
  if (decision.key === "result_interval" && option.id !== "none") return option.id.replace(":", " → ");
  if (option.id === "none") return t("option_none");
  if (decision.key === "limit") return t("reviewLimit", { count: option.id });
  if (decision.key === "order")
    return option.label.replace(/ ascending$/, " ↑").replace(/ descending$/, " ↓");
  if (decision.key === "outer_metric") return t("outer_" + option.id);
  if (messages[locale]["option_" + option.id]) return t("option_" + option.id);
  if (locale === "en") return option.label;
  return option.label
    .replace(/^All conditions \(AND\)$/, "所有条件同时满足（AND）")
    .replace(/^Any condition \(OR\)$/, "至少满足一个条件（OR）")
    .replace(/^One required plus either other: /, "必须满足此条件，且满足其余任一条件：")
    .replace(/^One sufficient or both others: /, "满足此条件，或同时满足其余条件：");
}

function stageOptionTitle(decision, option) {
  if (/^dag_(source|root)/.test(decision.key) && option.id !== "none") return option.id;
  if (locale !== "zh") return null;
  if (messages.zh["dagOption_" + option.id]) return t("dagOption_" + option.id);
  if (/^[mb]\d+$/.test(option.id)) {
    const functionName = option.label.match(/ (COUNT_DISTINCT|COUNT|SUM|AVG|MIN|MAX) of /)?.[1]?.toLowerCase();
    const operand = option.label.split(" of ")[1]?.split(" (")[0]?.split(";")[0];
    return t(option.id.startsWith("m") ? "dagPrimary" : "dagReference") + " " + (Number(option.id.slice(1)) + 1) + " · " + (functionName ? t("option_" + functionName) : "") + (operand && operand !== "source rows" ? " · " + operand : "");
  }
  if (/^v\d+_/.test(option.id)) {
    const symbols = { eq: "=", ne: "≠", gt: ">", ge: "≥", lt: "<", le: "≤" };
    return symbols[option.id.split("_").at(-1)] + " " + option.label.replace(/^(?:does not equal|greater than|less than|at least|at most|equals) /, "");
  }
  if (option.id === "matched_set") return t("dagMatches") + option.label.split(": ").slice(1).join(": ");
  return null;
}

function renderStageGraph(plan) {
  let panel = $("stage-graph");
  if (!panel) {
    panel = document.createElement("details");
    panel.id = "stage-graph";
    panel.className = "stage-graph";
    $("review-proposal-sql").parentElement.append(panel);
  }
  panel.replaceChildren();
  const graph = plan.stage_dag;
  panel.hidden = !graph;
  if (!graph) return;
  text(panel, "summary", t("dagSteps", { count: graph.stages.length }));
  text(panel, "p", t("dagShared", { count: graph.shared_stages.length }));
  for (const layer of graph.layers) {
    const row = text(panel, "div", "");
    row.className = "stage-layer";
    for (const identity of layer) {
      const stage = graph.stages.find(item => item.id === identity);
      const card = text(row, "details", "");
      card.className = "stage-card";
      const number = Number(identity.split("_").at(-1)) + 1;
      text(card, "summary", number + " · " + t("dagOperator_" + stage.operator));
      text(card, "p", t("dagInputs") + " " + (stage.inputs.map(id => Number(id.split("_").at(-1)) + 1).join(", ") || t("dagSource")));
      const label = key => {
        const name = stage.columns[key]?.label.split(" (")[0] || key;
        return locale === "zh" && messages.zh["option_" + name] ? t("option_" + name) : name;
      };
      const grain = stage.grain === null ? t("dagRows") : stage.grain.length ? stage.grain.map(label).join(", ") : t("dagScalar");
      text(card, "p", t("dagGrain") + " " + grain);
      const columns = Object.entries(stage.columns).map(([key, column]) => key + " · " + t("dagType_" + column.kind) + ": " + (column.lineage.length ? column.lineage.join(", ") : label(key)));
      text(card, "pre", columns.join("\n"));
      const sql = text(card, "details", "");
      text(sql, "summary", "SQL");
      text(sql, "pre", stage.sql);
    }
  }
}

function renderDecision(parent, decision, failed) {
  const row = text(parent, "div", "");
  row.className = "review-decision";
  row.dataset.failed = String(failed);
  row.dataset.uncertain = String(decision.uncertain && !decision.overridden);
  const label = text(row, "label", decisionTitle(decision));
  const id = "decision-" + decision.id;
  label.htmlFor = id;
  if (locale === "en") label.title = decision.instruction;
  const control = document.createElement("select");
  control.id = id;
  control.dataset.decision = decision.id;
  control.dataset.kind = decision.kind;
  control.dataset.initial = String(decision.selected);
  control.disabled = !decision.editable;
  const choices =
    decision.kind === "choice"
      ? decision.options.map((option) => [
          option.id,
          optionTitle(decision, option) +
            (Number.isFinite(option.probability)
              ? ` · ${(option.probability * 100).toFixed(0)}%`
              : ""),
        ])
      : [
          ["true", t("reviewYes")],
          ["false", t("reviewNo")],
        ];
  for (const [value, title] of choices) control.add(new Option(title, value));
  control.value = String(decision.selected);
  control.addEventListener("change", () => {
    $("confirm-plan").disabled = true;
    $("rebuild-plan").disabled = false;
  });
  row.append(control);
  text(
    row,
    "span",
    t(decision.overridden ? "reviewCorrected" : decision.selected_by === "relational_search" ? "reviewSearchSelected" : "reviewScore", {
      score: (decision.probability * 100).toFixed(0),
    }),
  ).className = "hint";
  if (failed || (decision.uncertain && !decision.overridden))
    text(row, "strong", t("reviewStoppedHere")).className = "review-stop";
}

function renderProposal(plan, review) {
  renderStageGraph(plan);
  renderHybridContract(plan);
  const sql = plan.logical_sql || "";
  const status = plan.proposal?.status || (sql ? "candidate" : "unresolved");
  $("review-proposal-title").textContent = t(
    status === "partial" ? "reviewPartialTitle" : "reviewProposalTitle",
  );
  $("review-proposal-note").textContent = t(
    status === "partial"
      ? "reviewPartialHint"
      : sql
        ? "reviewProposalHint"
        : "reviewNoProposalHint",
  );
  $("review-proposal-sql").textContent = sql || t("reviewNoProposal");
  $("edit-proposal-sql").hidden = !sql;
  $("review-unresolved-list").replaceChildren();
  const unresolved = review.unresolved || [];
  $("review-unresolved").hidden = !unresolved.length;
  for (const item of unresolved) {
    const key = "unresolved_" + item.code;
    const message = t(messages[locale][key] ? key : "reviewUnresolvedPart");
    const row = text($("review-unresolved-list"), "li", message);
    const decision = (review.decisions || []).find((entry) => entry.id === item.decision_id);
    if (decision) text(row, "span", " · " + decisionTitle(decision));
    if (locale === "en" && item.detail) row.title = item.detail;
  }
}

function renderPlanningReview(data) {
  activeReview = null;
  const plan = data.plan || data;
  const review = plan.review || data.review;
  if (!review || data.manifest || data.mutation_preview) return;
  const identity = plan.review_id || data.review_id;
  if (!identity) return;
  activeReview = { identity, plan };
  $("planning-review").hidden = false;
  $("review-request").textContent = plan.request || "";
  const reasonKey = "review_" + (review.reason || "ready");
  $("review-reason").textContent = t(messages[locale][reasonKey] ? reasonKey : "reviewUnresolvedPart");
  renderProposal(plan, review);
  if (review.reason === "incomplete_plan" && review.detail) {
    const detail = /No unambiguous output calculation/.test(review.detail)
      ? t("reviewMissingOutput")
      : locale === "en"
        ? review.detail
        : "";
    if (detail) $("review-reason").textContent += " " + detail;
  }
  $("review-decisions").replaceChildren();
  $("review-other-decisions").replaceChildren();
  $("review-more").open = false;
  const decisions = review.decisions || [];
  for (const decision of decisions) {
    const failed = decision.id === review.failed_decision;
    const important = failed || decision.overridden || (decision.uncertain && decision.editable);
    renderDecision($(important ? "review-decisions" : "review-other-decisions"), decision, failed);
  }
  $("rebuild-plan").disabled = false;
  $("confirm-plan").disabled = review.can_confirm_sql !== true;
  $("confirm-plan").textContent = t(
    plan.operation && plan.operation !== "select" ? "reviewConfirmWrite" : "reviewConfirm",
  );
}

async function actOnReview(confirm) {
  if (!activeReview || busy) return;
  if (confirm && activeReview.plan.review.can_confirm_sql !== true) return;
  const body = { review_id: activeReview.identity };
  if (typeof historyParentId !== "undefined" && historyParentId)
    body.parent_history_id = historyParentId;
  if (confirm) body.max_evaluations = Number($("budget").value);
  else {
    body.corrections = {};
    for (const control of document.querySelectorAll("[data-decision]")) {
      if (control.value === control.dataset.initial || control.disabled) continue;
      body.corrections[control.dataset.decision] =
        control.dataset.kind === "noul" ? control.value === "true" : control.value;
    }
    // Selecting the existing uncertain candidate is also an explicit correction.
    for (const decision of activeReview.plan.review.decisions) {
      if (
        decision.id === activeReview.plan.review.failed_decision &&
        decision.editable &&
        !(decision.id in body.corrections)
      )
        body.corrections[decision.id] = decision.selected;
    }
  }
  busy = true;
  $("run").disabled =
    $("preview").disabled =
    $("rebuild-plan").disabled =
    $("confirm-plan").disabled =
      true;
  $("status").textContent = t("planning");
  try {
    show(await request(confirm ? "/ask/confirm" : "/ask/review", body));
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    $("run").disabled = false;
    updateMode();
    if (typeof refreshHistory === "function") refreshHistory();
  }
}

$("rebuild-plan").addEventListener("click", () => actOnReview(false));
$("confirm-plan").addEventListener("click", () => actOnReview(true));

$("edit-proposal-sql").addEventListener("click", () => {
  if (!activeReview?.plan.logical_sql || busy) return;
  $("mode").value = "sql";
  updateMode();
  $("question").value = activeReview.plan.logical_sql;
  $("confirm-plan").disabled = true;
  $("status").textContent = t("reviewEditingSql");
  $("question").focus();
});
