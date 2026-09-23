function hybridState(output, operation) {
  const result = t(output === "VALUE" ? "hybridStateAvailable"
    : output === "UNKNOWN" ? "hybridStateUnknown" : "hybridStateSkipped");
  const operational = {
    FAILED: "hybridStateFailed",
    BLOCKED_BY_BUDGET: "hybridStateBudget",
    TRUNCATED: "hybridStateTruncated",
  }[operation];
  return operational ? result + " · " + t(operational) : result;
}

function hybridPanel(id, tag, before) {
  let panel = $(id);
  if (!panel) {
    panel = document.createElement(tag);
    panel.id = id;
    before.parentElement.insertBefore(panel, before);
  }
  panel.replaceChildren();
  return panel;
}

function useHybridCandidate(plan, candidate) {
  if (busy) return;
  const decision = [...(plan.review?.decisions || [])].reverse().find(entry =>
    entry.key === "hybrid_candidate" && entry.editable && entry.options.some(option =>
      option.id === candidate.id && option.sql === candidate.sql));
  const control = decision && [...document.querySelectorAll("[data-decision]")]
    .find(entry => entry.dataset.decision === decision.id);
  if (control) {
    control.value = candidate.id;
    control.dispatchEvent(new Event("change"));
    actOnReview(false);
    return;
  }
  $("mode").value = "sql";
  $("question").value = candidate.sql;
  updateMode();
  $("question").focus();
}

function renderHybridContract(plan) {
  const anchor = $("review-proposal-sql");
  const overview = hybridPanel("hybrid-overview", "section", anchor);
  const choices = hybridPanel("hybrid-choices", "details", anchor);
  const panel = hybridPanel("hybrid-contract", "details", anchor);
  overview.className = "hybrid-overview";
  choices.className = panel.className = "stage-graph";
  const hybrid = plan.hybrid;
  overview.hidden = choices.hidden = panel.hidden = !hybrid;
  if (!hybrid) return;

  text(overview, "h4", t("hybridPlanOverview"));
  const status = !plan.logical_sql ? "hybridNoProposal"
    : plan.review?.requires_confirmation ? "hybridNeedsReview" : "hybridReadyReview";
  text(overview, "p", t(status));
  const flow = text(overview, "ol", "");
  flow.className = "hybrid-flow";
  const review = hybrid.review_state || {};
  const generation = hybrid.generation_state || {
    output: hybrid.contract ? "VALUE" : "NOT_EVALUATED",
    operation: hybrid.generation?.reused ? "REUSED" : "COMPLETED",
  };
  for (const [title, output, operation] of [
    ["hybridFlowData", hybrid.retrieval?.output_state, hybrid.retrieval?.operation_state],
    ["hybridFlowProposal", generation.output, generation.operation],
    ["hybridFlowReview", review.output, review.operation],
  ]) {
    const item = text(flow, "li", "");
    text(item, "strong", t(title));
    text(item, "span", hybridState(output, operation));
  }
  const usage = text(overview, "details", "");
  text(usage, "summary", t("hybridUsageDetails"));
  text(usage, "p", t("hybridUsageSummary", {
    calls: hybrid.llm_calls ?? "—", requests: plan.planning_requests ?? "—",
    fields: hybrid.retrieval?.fields_after ?? "—", total: hybrid.retrieval?.fields_before ?? "—",
    waves: hybrid.review_waves?.length ?? "—",
  }));
  text(usage, "p", t("hybridSampleNote"));
  const repair = hybrid.repair;
  if (repair?.attempted || hybrid.local_alternatives) {
    const key = repair?.operation_state === "FAILED" ? "hybridRepairFailed"
      : repair?.used ? "hybridRepairUsed"
      : hybrid.local_alternatives ? "hybridLocalAlternatives" : "hybridOriginalKept";
    text(overview, "p", t(key)).className = "hybrid-change-note";
  }

  const candidates = hybrid.candidates || [];
  choices.hidden = !candidates.length;
  choices.open = candidates.length > 1;
  text(choices, "summary", t("hybridInterpretations", {count: candidates.length}));
  for (const [index, candidate] of candidates.entries()) {
    const card = text(choices, "article", "");
    card.className = "hybrid-candidate";
    const current = candidate.id === hybrid.selected;
    card.dataset.selected = String(current);
    text(card, "h5", candidate.label || t("hybridInterpretation", {count: index + 1}));
    if (current) text(card, "span", t("hybridCurrentProposal")).className = "hybrid-badge";
    if (candidate.derived_from) text(card, "p", t("hybridDerivedProposal"));
    if (candidate.assumptions?.length) {
      text(card, "h6", t("hybridAssumptions"));
      const list = text(card, "ul", "");
      candidate.assumptions.forEach(value => text(list, "li", value));
    }
    const sql = text(card, "details", "");
    text(sql, "summary", t("hybridViewSql"));
    text(sql, "pre", candidate.sql || "");
    if (!candidate.valid) {
      text(card, "p", t("hybridInvalidProposal"));
      if (candidate.error) text(sql, "pre", candidate.error);
    } else if (!current) {
      const button = text(card, "button", t("hybridUseInterpretation"));
      button.type = "button";
      button.className = "secondary";
      button.dataset.hybridCandidate = candidate.id;
      button.addEventListener("click", () => useHybridCandidate(plan, candidate));
    }
  }
  const contract = hybrid.contract;
  panel.hidden = !contract;
  if (!contract) return;
  text(panel, "summary", t("hybridContract"));
  text(panel, "p", t("hybridFactsNote"));
  for (const source of contract.data_constraints || []) {
    const card = text(panel, "details", "");
    card.className = "stage-card";
    text(card, "summary", source.source + (source.columns.length ? " · " + source.columns.join(", ") : ""));
    for (const key of ["row_scope", "grain", "keys_and_relationships", "units_and_nulls", "purpose"])
      text(card, "p", t("hybrid_" + key) + ": " + source[key]);
  }
  for (const [i, step] of (contract.steps || []).entries()) {
    const card = text(panel, "details", "");
    card.className = "stage-card";
    text(card, "summary", (i + 1) + " · " + step.purpose);
    text(card, "p", t("hybrid_grain") + ": " + step.grain);
    text(card, "p", t("dagInputs") + " " + (step.depends_on.join(", ") || t("dagSource")));
    text(card, "pre", step.expressions.join("\n"));
  }
}

const guideLink = document.querySelector('a[href="#workspace-guide"]');
if (guideLink) guideLink.addEventListener("click", () => { $("workspace-guide").open = true; });
if (location.hash === "#workspace-guide") $("workspace-guide").open = true;
