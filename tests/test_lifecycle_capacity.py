"""Every mature survivor of graduation review must participate in capacity."""
import itertools

import pytest

from kitharness import config
from kitharness.hook import on_session_start
from kitharness.lib import counters, layer, lifecycle, sidecar, skill_store


def member(name, use=0, view=0, anchor=0):
    return {"name": name, "use": use, "view": view, "anchor": anchor}


def test_viewed_only_skill_competes_at_zero_rate_with_called_skills():
    members = [member("viewed", view=100), member("used", use=1)]
    assert lifecycle.evaluate(members, 100, maturity=10, capacity=1) == ["viewed"]


def test_viewed_only_pool_is_bounded_and_hash_ties_are_order_independent():
    members = [member("a", view=1), member("b", view=100), member("c", view=5)]
    outcomes = {tuple(lifecycle.evaluate(list(order), 100, maturity=10, capacity=1))
                for order in itertools.permutations(members)}
    assert len(outcomes) == 1
    assert len(next(iter(outcomes))) == 2


@pytest.mark.parametrize("capacity", [0, 1, 2])
def test_suspended_review_still_obeys_capacity_for_zero_use_skills(capacity):
    members = [member("a"), member("b"), member("used", use=1)]
    archived = lifecycle.evaluate(members, 100, maturity=10, capacity=capacity, review_suspended=True)
    assert len(archived) == 3 - capacity
    if capacity:
        assert "used" not in archived


def test_review_rejections_do_not_consume_survivor_capacity():
    members = [member("dead"), member("viewed", view=1), member("used", use=1)]
    assert lifecycle.evaluate(members, 100, maturity=10, capacity=2) == ["dead"]


@pytest.mark.parametrize("suspended", [False, True])
def test_probation_and_future_anchors_stay_outside_capacity(suspended):
    members = [member("mature", view=1), member("new", anchor=95), member("future", anchor=110)]
    assert lifecycle.evaluate(members, 100, maturity=10, capacity=0, review_suspended=suspended) == ["mature"]


def test_zero_maturity_and_zero_denominator_do_not_divide_by_zero():
    assert lifecycle.evaluate([member("viewed", view=1)], 0, maturity=0, capacity=0) == ["viewed"]


def seed(roots, lyr, name, *, use=0, view=0, anchor=0, author="agent"):
    root = roots[lyr]
    skill_store.write_body(lyr, name, f"---\nname: {name}\ndescription: Use when formatting dates.\n---\nUse ISO.\n", root)
    if author is not None:
        data = sidecar.create(lyr, name, anchor=anchor, root=root)
        sidecar.write(lyr, name, {**data, "use": use, "view": view, "created_by": author}, root)


@pytest.mark.parametrize("suspended", [False, True])
def test_session_start_archives_overflow_before_recall_and_restore_is_complete(tmp_path, monkeypatch, suspended):
    monkeypatch.setattr(config, "RECALL_MODE", "catalog")
    monkeypatch.delenv(config.CHILD_SESSION_ENV, raising=False)
    monkeypatch.setattr(config, "DISABLE_GLOBAL", False)
    monkeypatch.setattr(config, "GRADUATION_REVIEW_SUSPENDED", suspended)
    monkeypatch.setattr(config, "MATURITY_THRESHOLD", {"global": 10, "project": 10})
    monkeypatch.setattr(config, "CAPACITY", {"global": 1, "project": 1})
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    for lyr in roots:
        for _ in range(20):
            counters.bump_request(lyr, roots[lyr])
        seed(roots, lyr, f"viewed-{lyr}", view=3)
        seed(roots, lyr, f"used-{lyr}", use=1)
        seed(roots, lyr, f"baby-{lyr}", anchor=19)
        seed(roots, lyr, f"native-{lyr}", author=None)
        seed(roots, lyr, f"user-{lyr}", author="user")
    original = layer.symbol_dir("project", "viewed-project", roots["project"])
    (original / "references").mkdir()
    (original / "references" / "detail.md").write_text("complete attachment")
    before = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    result = on_session_start.on_session_start(roots=roots)
    for lyr in roots:
        assert result["archived"][lyr] == [f"viewed-{lyr}"]
        assert f"viewed-{lyr}" not in result["context"]
        for name in (f"used-{lyr}", f"baby-{lyr}", f"native-{lyr}", f"user-{lyr}"):
            assert skill_store.exists(lyr, name, roots[lyr])
    assert on_session_start.on_session_start(roots=roots)["archived"] == {"global": [], "project": []}
    skill_store.restore("project", "viewed-project", roots["project"])
    assert {str(p.relative_to(original)): p.read_bytes() for p in original.rglob("*") if p.is_file()} == before


def test_disabled_global_layer_is_untouched_under_capacity_pressure(tmp_path, monkeypatch):
    monkeypatch.delenv(config.CHILD_SESSION_ENV, raising=False)
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    seed(roots, "global", "viewed", view=1)
    for _ in range(20):
        counters.bump_request("global", roots["global"])
    before = {str(p.relative_to(roots["global"])): p.read_bytes() for p in roots["global"].rglob("*") if p.is_file()}
    monkeypatch.setattr(config, "DISABLE_GLOBAL", True)
    monkeypatch.setattr(config, "CAPACITY", {"global": 0, "project": 0})
    monkeypatch.setattr(config, "MATURITY_THRESHOLD", {"global": 0, "project": 0})
    assert on_session_start.on_session_start(roots=roots)["archived"] == {"project": []}
    assert {str(p.relative_to(roots["global"])): p.read_bytes() for p in roots["global"].rglob("*") if p.is_file()} == before
