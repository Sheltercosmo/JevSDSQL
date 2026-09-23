let semanticFeatures = [];

async function loadFeatures() {
  const data = await request("/features");
  semanticFeatures = data.features;
  const selectedId = $("features").value;
  $("features").replaceChildren(new Option(t("chooseFeature"), ""));
  const ids = selected();
  for (const feature of semanticFeatures.filter(
    (item) => !ids.length || ids.includes(item.dataset_id),
  )) {
    $("features").add(
      new Option(
        t("featureRevision", {
          name: feature.name,
          revision: feature.revision,
          status: t(feature.status),
        }),
        feature.id,
      ),
    );
  }
  $("features").value = selectedId;
}

function chosenFeature() {
  const feature = semanticFeatures.find((item) => item.id === $("features").value);
  if (!feature) throw new Error(t("featureRequired"));
  return feature;
}

async function featureAction(button, action) {
  button.disabled = true;
  $("feature-status").textContent = t("working");
  try {
    await action();
  } catch (error) {
    $("feature-status").textContent = errorMessage(error);
  } finally {
    button.disabled = false;
  }
}

function fillDefinition(feature) {
  const definition = feature.definition;
  $("feature-json").value = JSON.stringify(
    {
      dataset_id: feature.dataset_id,
      name: feature.name,
      column: definition.column,
      definition: definition.text,
      kind: definition.kind,
      criteria: definition.criteria,
      context_columns: definition.context_columns,
      aliases: definition.aliases,
      confidence: definition.confidence,
      maintain: Boolean(feature.maintain),
    },
    null,
    2,
  );
  $("feature-details").textContent = JSON.stringify(feature, null, 2);
}

document.addEventListener("sdd:connected", () =>
  loadFeatures().catch((error) => {
    $("feature-status").textContent = errorMessage(error);
  }),
);
$("datasets").addEventListener("change", () =>
  loadFeatures().catch((error) => {
    $("feature-status").textContent = errorMessage(error);
  }),
);
$("features").addEventListener("change", () => {
  const feature = semanticFeatures.find((item) => item.id === $("features").value);
  if (feature) fillDefinition(feature);
});
$("new-feature").addEventListener("click", () => {
  const dataset = catalog.find((item) => selected().includes(item.id)) || catalog[0];
  if (!dataset) {
    $("feature-status").textContent = t("datasetRequired");
    return;
  }
  const source = dataset.columns.find((column) => column.type === "text");
  if (!source) {
    $("feature-status").textContent = t("textColumnRequired");
    return;
  }
  $("feature-json").value = JSON.stringify(
    {
      dataset_id: dataset.id,
      name: "",
      column: source.name,
      definition: "",
      kind: "noul",
      context_columns: [],
      aliases: [],
      maintain: false,
    },
    null,
    2,
  );
  $("feature-status").textContent = t("kindsHint");
});
$("create-feature").addEventListener("click", (event) =>
  featureAction(event.currentTarget, async () => {
    const feature = await request("/features", JSON.parse($("feature-json").value));
    await loadFeatures();
    $("features").value = feature.id;
    fillDefinition(feature);
    $("feature-status").textContent = t("featureSaved", {
      name: feature.name,
      revision: feature.revision,
    });
  }),
);
$("preview-feature").addEventListener("click", (event) =>
  featureAction(event.currentTarget, async () => {
    const feature = chosenFeature();
    const result = await request(`/features/${feature.id}/preview`, {
      max_evaluations: Number($("budget").value),
    });
    $("feature-details").textContent = JSON.stringify(result, null, 2);
    $("feature-details").parentElement.open = true;
    $("feature-status").textContent = t("featureTested", result.coverage);
  }),
);
$("activate-feature").addEventListener("click", (event) =>
  featureAction(event.currentTarget, async () => {
    const feature = chosenFeature();
    const review = JSON.parse($("feature-review").value);
    const result = await request(`/features/${feature.id}/review`, { ...review, status: "active" });
    await loadFeatures();
    $("features").value = result.id;
    fillDefinition(result);
    $("feature-status").textContent = t("featureActivated", { name: result.name });
  }),
);
$("refresh-features").addEventListener("click", (event) =>
  featureAction(event.currentTarget, async () => {
    const feature = chosenFeature();
    const result = await request("/features/refresh", {
      dataset_id: feature.dataset_id,
      max_evaluations: Number($("budget").value),
    });
    const jobs = await request("/feature-jobs");
    $("feature-details").textContent = JSON.stringify(
      { result, jobs: jobs.jobs.filter((job) => job.dataset_id === feature.dataset_id) },
      null,
      2,
    );
    $("feature-details").parentElement.open = true;
    $("feature-status").textContent = result.manifest.complete
      ? t("coverageComplete")
      : t("coveragePartial");
  }),
);
