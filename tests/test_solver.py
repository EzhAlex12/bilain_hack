"""
Регрессионные тесты решателя: python -m pytest tests/

Проверяют на всех синтетических датасетах, что план проходит независимую валидацию,
каждая заявка учтена ровно один раз, а консольный и веб-пайплайн согласованы.
"""
import glob
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app  # noqa: E402
import vrptw_4pass_solver as solver  # noqa: E402

DATASETS = sorted(glob.glob(os.path.join(ROOT, "test_dataset", "*Синтетические*.csv")))


@pytest.mark.parametrize("csv_path", DATASETS, ids=os.path.basename)
def test_4pass_is_valid(csv_path):
    requests, depot, _, brigades = solver.load_dataset(csv_path)
    engineers = solver.build_engineers_for_dataset(requests, depot, brigades, os.path.basename(csv_path))
    routes, dropped = solver.run_4pass_optimization(requests, engineers)

    val = solver.validate_solution(routes, requests, engineers, unassigned=dropped)
    assert val["is_valid"], val


@pytest.mark.parametrize("csv_path", DATASETS, ids=os.path.basename)
def test_optimizer_is_valid_and_complete(csv_path):
    requests, depot, _, brigades = solver.load_dataset(csv_path)
    engineers = solver.build_engineers_for_dataset(requests, depot, brigades, os.path.basename(csv_path))
    warm, _ = solver.run_4pass_optimization(requests, engineers)
    routes, dropped, _ = solver.optimize_routes_with_ortools(
        warm, requests=requests, engineers=engineers, time_limit_sec=1.0
    )

    val = solver.validate_solution(routes, requests, engineers, unassigned=dropped)
    assert val["is_valid"], val
    assert sum(len(r.visits) for r in routes) + len(dropped) == len(requests)
    # Оптимизатор не может вернуть план хуже стартового 4-Pass решения
    assert solver.solution_quality_key(routes) >= solver.solution_quality_key(warm)


@pytest.mark.parametrize("csv_path", DATASETS, ids=os.path.basename)
def test_api_has_no_request_in_both_routes_and_unassigned(csv_path):
    requests, depot, depot_addr, brigades = solver.load_dataset(csv_path)
    res = app.run_full_pipeline(requests, depot, depot_addr, brigade_names=brigades,
                                dataset_name=os.path.basename(csv_path))
    sol = res["solution"]
    routed = [t["id"] for e in sol["engineers"] for t in e["tasks"]]
    unassigned = [u["req"]["id"] for u in sol["unassigned"]]

    assert len(routed) == len(set(routed))
    assert not set(routed) & set(unassigned)
    assert len(routed) + len(unassigned) == len(requests)
    assert res["stats"]["opt_dropped"] == len(unassigned)
