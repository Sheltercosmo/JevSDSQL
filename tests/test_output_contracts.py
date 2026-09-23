from sdd.generic.output_contracts import output_candidates, output_tuple


def test_complete_outputs_can_recover_a_weak_component_and_restore_order():
    answers = {
        "output_0": {"probabilities": {"first": 0.98, "last": 0.02}},
        "output_1": {"probabilities": {"amount": 0.99, "last": 0.01}},
    }
    phases = []

    def ask(tenant, state, questions, phase):
        phases.append(phase)
        key, question = next(iter(questions.items()))
        expected = (
            "Return First AND Amount AND Last" if key.endswith("_set") else "First | Last | Amount"
        )
        return {key: next(k for k, label in question["criteria"].items() if label == expected)}

    result = output_tuple(
        ask,
        "t",
        "Return the complete name and amount",
        {"first": "First", "last": "Last", "amount": "Amount"},
        ["first", "amount"],
        candidates=output_candidates(answers, "output_"),
    )
    assert result == ["first", "last", "amount"]
    assert phases == ["output_coverage_reconciliation", "output_tuple_reconciliation"]


def test_user_output_correction_bypasses_reconciliation():
    def unexpected_call(*args):
        raise AssertionError("A confirmed correction must not be reconsidered")

    assert output_tuple(
        unexpected_call,
        "t",
        "request",
        {"x": "X", "y": "Y"},
        ["y", "x"],
        locked=True,
    ) == ["y", "x"]


def test_output_candidate_space_is_bounded():
    sizes = []

    def ask(tenant, state, questions, phase):
        sizes.extend(len(q["criteria"]) for q in questions.values())
        return {key: next(iter(q["criteria"])) for key, q in questions.items()}

    labels = {str(i): str(i) for i in range(100)}
    output_tuple(ask, "t", "request", labels, ["0", "1"], candidates=list(labels))
    assert max(sizes) <= 30
