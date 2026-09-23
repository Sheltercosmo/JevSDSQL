"""SQL expression compilation including Kleene three-valued semantic logic."""

from sqlalchemy import select, and_, or_, not_, case, null, true, false, func, distinct
from . import schema as s


def population(plan, tenant):
    # Arbitrary Boolean filters stay in the final tree: OR cannot be pushed down as AND.
    stmt = (
        select(s.versions)
        .join(
            s.records,
            and_(
                s.records.c.current_version == s.versions.c.id,
                s.records.c.tenant == s.versions.c.tenant,
            ),
        )
        .where(s.versions.c.tenant == tenant, s.records.c.tenant == tenant)
    )
    for restriction in plan.scope:
        stmt = stmt.where(s.versions.c[restriction.field] == restriction.value)
    if plan.start:
        stmt = stmt.where(s.versions.c.event_time >= plan.start)
    if plan.end:
        stmt = stmt.where(s.versions.c.event_time < plan.end)
    return stmt.order_by(s.versions.c.id)


def boolean_expr(predicate, decisions):
    if predicate.op == "true":
        return true()
    if predicate.op == "semantic":
        yes = [vid for vid, labels in decisions.items() if labels.get(predicate.concept_id) is True]
        no = [vid for vid, labels in decisions.items() if labels.get(predicate.concept_id) is False]
        return case(
            (s.versions.c.id.in_(yes), true()), (s.versions.c.id.in_(no), false()), else_=null()
        )
    if predicate.op == "eq":
        return s.versions.c[predicate.field] == predicate.value
    children = [boolean_expr(p, decisions) for p in predicate.args]
    if predicate.op == "not":
        return not_(children[0])
    return (and_ if predicate.op == "and" else or_)(*children)


def final_relation(plan, tenant, snapshot, decisions):
    predicate = boolean_expr(plan.predicate, decisions)
    return (
        select(
            s.versions.c.id,
            s.versions.c.customer_id,
            s.versions.c.segment,
            s.versions.c.product,
            s.versions.c.text,
            predicate.label("matches"),
        )
        .where(s.versions.c.tenant == tenant, s.versions.c.id.in_(snapshot))
        .subquery()
    )


def aggregate(plan, relation):
    if plan.quantifier == "not_exists":
        customers = (
            select(
                relation.c.customer_id,
                func.max(case((relation.c.matches.is_(True), 1), else_=0)).label("has_positive"),
                func.max(case((relation.c.matches.is_(None), 1), else_=0)).label("has_unknown"),
            )
            .group_by(relation.c.customer_id)
            .subquery()
        )
        resolved_absence = and_(customers.c.has_positive == 0, customers.c.has_unknown == 0)
        if plan.operation == "count":
            return (
                select(func.count().label("count")).select_from(customers).where(resolved_absence)
            )
        return (
            select(customers.c.customer_id.label("id"))
            .where(resolved_absence)
            .order_by(customers.c.customer_id)
            .limit(plan.limit)
        )

    column = relation.c.id if plan.grain == "message" else relation.c.customer_id
    base = relation.c.matches.is_(True)
    if plan.operation == "count":
        return select(func.count(distinct(column)).label("count")).where(base)
    if plan.operation == "list":
        if plan.grain == "message":
            return (
                select(column.label("id"), relation.c.customer_id, relation.c.text)
                .where(base)
                .order_by(column)
                .limit(plan.limit)
            )
        return select(column.label("id")).where(base).distinct().order_by(column).limit(plan.limit)
    group = relation.c[plan.group_by]
    count = func.count(distinct(column)).label("count")
    stmt = select(group.label(plan.group_by), count).where(base).group_by(group)
    return (
        stmt.order_by(count.desc(), group).limit(plan.limit)
        if plan.operation == "rank"
        else stmt.order_by(group)
    )
