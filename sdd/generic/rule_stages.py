"""Relational operations shared by business rules and the typed stage DAG."""

from sqlglot import exp

from .stage_dag import operation as op, ref, value


def latest_source(dag, table, columns, *, partition, order):
    if not partition or not order:
        raise ValueError("Latest-record selection needs entity keys and chronology")
    query = exp.select(*[exp.column(k, quoted=True) for k in columns]).from_(
        exp.Table(this=exp.to_identifier(table, quoted=True))
    )
    node = dag.source(
        query, columns, description="Observation source before latest-record selection"
    )
    return dag.latest(node, partition=partition, order=order)


def validate_bindings(bindings, links, fields, root, entity, primary_keys=None):
    """Every written equality must be a selected FK or an entity-key binding."""

    def names(table, column):
        return {column.casefold(), (table + "." + column).casefold()}

    identity_columns = {(root, fields[key].name) for key in entity}
    primary_keys = primary_keys or {}
    while True:
        previous = set(identity_columns)
        for link in links:
            source, target = (link.source, link.source_column), (link.target, link.target_column)
            if primary_keys.get(link.source) == [link.source_column] and primary_keys.get(
                link.target
            ) == [link.target_column]:
                if source in identity_columns or target in identity_columns:
                    identity_columns.update((source, target))
        if previous == identity_columns:
            break

    for left, right in bindings:
        left, right = left.casefold(), right.casefold()
        matched = False
        for link in links:
            a, b = names(link.source, link.source_column), names(link.target, link.target_column)
            if (left in a and right in b) or (right in a and left in b):
                matched = True
            for field_name, placeholder in ((left, right), (right, left)):
                if len(placeholder) != 1:
                    continue
                if (link.target, link.target_column) in identity_columns and field_name in a:
                    matched = True
                if (link.source, link.source_column) in identity_columns and field_name in b:
                    matched = True
        if not matched:
            raise ValueError(
                f"Aggregate binding is not established by the selected relationships: {left} = {right}"
            )


def compile_values(dag, source, graph, required, *, restrictions=(), scopes=None):
    """Compile dependency waves, branching only when window populations differ."""
    scopes = scopes or {}
    pending = set(required)
    node = source
    identity = "_population_row"
    if any(scopes.get(key) for key in pending):
        while identity in dag.nodes[node].columns:
            identity += "_"
        node = dag.row_identity(node, identity)
    while pending:
        ready = []
        for key in sorted(pending):
            if key.startswith("k"):
                dependencies = graph.terms[key[1:]].columns()
            else:
                _, operand, partition, _ = graph.windows[key]
                dependencies = operand.columns() | set().union(
                    *(item.columns() for item in partition)
                )
                for index in scopes.get(key, ()):
                    dependencies |= restrictions[index].columns()
            if dependencies <= dag.nodes[node].columns.keys():
                ready.append(key)
        if not ready:
            raise ValueError("Rule stages have unresolved or cyclic population dependencies")
        formulas = {key: graph.terms[key[1:]] for key in ready if key.startswith("k")}
        if formulas:
            node = dag.project(
                node,
                formulas,
                keep=True,
                description="Independent formulas at the same dependency barrier",
            )
        groups = {}
        for key in ready:
            if key in graph.windows:
                groups.setdefault(tuple(sorted(scopes.get(key, ()))), []).append(key)
        branches = []
        for scope, keys in groups.items():
            branch = node
            for index in scope:
                branch = dag.filter(
                    branch, restrictions[index], "Restrict this window's comparison population"
                )
            branch = compile_windows(dag, branch, graph, keys)
            branches.append((scope, keys, branch))
        for scope, keys, branch in sorted(branches, key=lambda item: bool(item[0])):
            if not scope:
                node = branch
            else:
                node = dag.join(
                    node,
                    branch,
                    [(identity, identity)],
                    how="left",
                    outputs={
                        **{key: ref("l." + key) for key in dag.nodes[node].columns},
                        **{key: ref("r." + key) for key in keys},
                    },
                    description="Merge statistics without altering the output population",
                )
        pending.difference_update(ready)
    return node


def compile_windows(dag, source, graph, keys):
    inputs, specs, quartiles = {}, {}, {}
    for key in keys:
        kind, operand, partition, descending = graph.windows[key]
        input_key = key + "_operand"
        inputs[input_key] = operand
        partition_keys = tuple(f"{key}_partition_{i}" for i in range(len(partition)))
        inputs.update(zip(partition_keys, partition))
        target = key + "_rank" if kind == "quartile" else key
        specs[target] = {
            "term": op(kind, ref(input_key))
            if kind in {"avg", "sum"}
            else op("percent_rank" if kind == "quartile" else kind),
            "partition": partition_keys,
            "order": [] if kind in {"avg", "sum"} else [(input_key, descending)],
        }
        if kind == "quartile":
            bucket = value(4)
            for boundary, number in reversed([(0.25, 1), (0.5, 2), (0.75, 3)]):
                bucket = op("case", op("le", ref(target), value(boundary)), value(number), bucket)
            quartiles[key] = op("case", op("is_null", ref(input_key)), value(None), bucket)
    node = dag.project(source, inputs, keep=True)
    node = dag.windows(node, specs)
    return dag.project(node, quartiles, keep=True) if quartiles else node
