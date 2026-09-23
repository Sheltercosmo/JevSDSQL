import pytest
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService


@pytest.fixture
def generic(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "generic.db"))
    db.initialize()
    catalog = Catalog(db)
    catalog.create(
        "a",
        "products",
        [
            {"id": 1, "name": "A", "price": 10, "quantity": 2, "category": "book"},
            {"id": 2, "name": "B", "price": 20, "quantity": 3, "category": "book"},
            {"id": 3, "name": "C", "price": 5, "quantity": 0, "category": "food"},
        ],
        primary_key=["id"],
    )
    return db, catalog, SQLService(db)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT COUNT(*) AS n FROM products", [{"n": 3}]),
        ("SELECT name FROM products WHERE category = 'book' AND price >= 15", [{"name": "B"}]),
        ("SELECT SUM(price * quantity) AS total FROM products", [{"total": 80}]),
        (
            "SELECT category,COUNT(*) AS n FROM products GROUP BY category HAVING COUNT(*) > 1",
            [{"category": "book", "n": 2}],
        ),
        ("SELECT name FROM products ORDER BY price DESC LIMIT 1", [{"name": "B"}]),
        (
            "WITH x AS (SELECT * FROM products WHERE quantity > 0) SELECT COUNT(*) AS n FROM x",
            [{"n": 2}],
        ),
        (
            "SELECT name FROM products WHERE price > (SELECT AVG(price) FROM products)",
            [{"name": "B"}],
        ),
        (
            "SELECT name,ROW_NUMBER() OVER (ORDER BY price DESC) AS rank FROM products ORDER BY rank LIMIT 1",
            [{"name": "B", "rank": 1}],
        ),
        (
            "SELECT name FROM products WHERE price=5 UNION SELECT name FROM products WHERE name='A'",
            [{"name": "A"}, {"name": "C"}],
        ),
        (
            "SELECT COALESCE(NULLIF(category,'book'),'other') AS c, COUNT(*) AS n FROM products GROUP BY 1 ORDER BY 1",
            [{"c": "food", "n": 1}, {"c": "other", "n": 2}],
        ),
    ],
)
def test_general_sql(generic, sql, expected):
    _, _, service = generic
    r = service.execute("a", sql)
    assert r["result"] == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM source_versions",
        "SELECT * FROM dataset_catalog",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT pg_read_file('/etc/passwd') FROM products",
        "SELECT pg_sleep(10) FROM products",
        "SELECT public.lower(name) FROM products",
        "SELECT name::public.customtype FROM products",
        "DROP TABLE products",
        "SELECT * INTO stolen FROM products",
        "SELECT * FROM products; DELETE FROM products",
        "WITH x AS (DELETE FROM products RETURNING *) SELECT * FROM x",
        "SELECT unknown FROM products",
    ],
)
def test_sql_guard(generic, sql):
    _, _, service = generic
    with pytest.raises(Exception):
        service.execute("a", sql)


def test_parameterization_and_tenant_boundary(generic):
    _, _, service = generic
    r = service.execute("a", "SELECT * FROM products WHERE name = 'x'' OR 1=1 --'")
    assert r["result"] == [] and "x' OR" not in r["compiled_sql"]
    with pytest.raises(ValueError):
        service.execute("b", "SELECT * FROM products")


def test_update_preview_commit_replay(generic):
    _, catalog, service = generic
    p = service.execute("a", "UPDATE products SET price = price * 2 WHERE id = 1", actor="reviewer")
    assert p["affected_rows"] == 1 and p["changes_sample"] == [{"price": 20}]
    assert service.execute("a", "SELECT price FROM products WHERE id=1")["result"] == [
        {"price": 10}
    ]
    result = service.commit("a", p["preview_token"], "reviewer")
    assert result["manifest"]["committed"] and result["manifest"]["affected_rows"] == 1
    assert service.execute("a", "SELECT price FROM products WHERE id=1")["result"] == [
        {"price": 20}
    ]
    with pytest.raises(ValueError):
        service.commit("a", p["preview_token"], "reviewer")


def test_insert_delete_and_stale_previews(generic):
    _, _, service = generic
    stale = service.execute("a", "DELETE FROM products WHERE quantity=0", actor="r")
    p = service.execute(
        "a",
        "INSERT INTO products (id,name,price,quantity,category) VALUES (4,'D',4,2,'food')",
        actor="r",
    )
    assert p["affected_rows"] == 1
    service.commit("a", p["preview_token"], "r")
    with pytest.raises(ValueError, match="stale"):
        service.commit("a", stale["preview_token"], "r")
    fresh = service.execute("a", "DELETE FROM products WHERE quantity=0", actor="r")
    service.commit("a", fresh["preview_token"], "r")
    assert service.execute("a", "SELECT COUNT(*) AS n FROM products")["result"] == [{"n": 3}]


def test_full_table_writes_and_actor(generic):
    _, _, service = generic
    with pytest.raises(ValueError):
        service.execute("a", "DELETE FROM products")
    p = service.execute("a", "UPDATE products SET price=1", allow_all=True, actor="owner")
    with pytest.raises(ValueError):
        service.commit("a", p["preview_token"], "other")
    with pytest.raises(ValueError):
        service.execute("a", "UPDATE products SET price=1", allow_all=True, max_affected=2)


def test_unrelated_chinese_schema(generic):
    _, catalog, service = generic
    catalog.create(
        "a",
        "实验结果",
        [{"样本": "甲", "温度": 20, "压力": 10}, {"样本": "乙", "温度": 30, "压力": 15}],
    )
    r = service.execute(
        "a", 'SELECT AVG("温度") AS "平均温度", MAX("压力") AS "最高压力" FROM "实验结果"'
    )
    assert r["result"] == [{"平均温度": 25.0, "最高压力": 15}]


def test_registered_relationship_join(generic):
    _, catalog, service = generic
    catalog.create("a", "departments", [{"id": 1, "title": "Science"}], primary_key=["id"])
    catalog.create(
        "a",
        "employees",
        [{"id": 1, "dept_id": 1, "salary": 100}, {"id": 2, "dept_id": 1, "salary": 200}],
        primary_key=["id"],
    )
    catalog.link("a", "employees", "departments", "dept_id", "id")
    r = service.execute(
        "a",
        (
            "SELECT d.title, SUM(e.salary) AS payroll FROM employees e JOIN departments d"
            " ON e.dept_id=d.id GROUP BY d.title"
        ),
    )
    assert r["result"] == [{"title": "Science", "payroll": 300}]


def test_iso_dates_inferred_without_domain_rules(generic):
    _, catalog, sql = generic
    d = catalog.create(
        "a",
        "events",
        [{"id": 1, "when": "2026-09-21", "created": "2026-09-21T14:00:00Z"}],
        primary_key=["id"],
    )
    assert {c["name"]: c["type"] for c in d["columns"]} == {
        "id": "integer",
        "when": "date",
        "created": "datetime",
    }
    assert sql.execute("a", "SELECT id FROM events WHERE \"when\">='2026-09-01'")["result"] == [
        {"id": 1}
    ]
