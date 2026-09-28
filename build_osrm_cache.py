"""
Пересобирает кеш расстояний .osrm_cache.json по дорожным графам OSM.

Запуск (нужен интернет):  python3 build_osrm_cache.py
Перед этим должен быть собран geocode_cache.json (python3 geocode_addresses.py),
иначе расстояния считаются между приблизительными точками.

Графы по видам транспорта:
- car      — автомобильный граф демо-сервера OSRM (router.project-osrm.org);
- foot     — пешеходный граф FOSSGIS: тротуары, переходы, дворы и сквозные проходы;
- bicycle  — велосипедный граф FOSSGIS: велодорожки, тротуары, дворовые зоны.
Демо-сервер OSRM держит только автомобильный граф и игнорирует профиль foot/bike
в URL, поэтому пешеходы и велосипеды берутся с FOSSGIS. Общественный транспорт
выводится решателем из автомобильного графа (отдельного transit-графа у OSRM нет).

Вся сетевая логика живёт в vrptw_4pass_solver.prefetch_osrm — этот файл только
собирает точки датасетов и запускает досчёт.
"""
from __future__ import annotations

import glob
import os
import sys
from typing import List, Tuple

import vrptw_4pass_solver as solver

Point = Tuple[float, float]


def dataset_points(csv_path: str) -> List[Point]:
    """
    Все точки старта бригад + все заявки датасета — ровно то, между чем считает решатель.

    Точек старта несколько: районный склад для Москвы и база в каждом городе
    Подмосковья, где есть заявки.
    """
    requests, depot, _, brigades = solver.load_dataset(csv_path)
    engineers = solver.build_engineers_for_dataset(
        requests, depot, brigades, dataset_name=os.path.basename(csv_path))

    points: List[Point] = []
    for p in [(e.home_lat, e.home_lon) for e in engineers] + [(r.lat, r.lon) for r in requests]:
        if p not in points:
            points.append(p)
    return points


def main(fetch=None, pause_sec: float = 1.0) -> int:
    root = os.path.dirname(os.path.abspath(__file__))

    if not solver._GEOCODE_CACHE:
        print("ВНИМАНИЕ: geocode_cache.json пуст или отсутствует.")
        print("Сначала запустите: python3 geocode_addresses.py\n")

    # Координаты депо участвуют в каждом маршруте. Если они ещё не разрешены геокодером,
    # собранная матрица окажется привязана к старым точкам и депо выпадет из графа.
    approx_depots = [cfg["display"] for cfg in solver.DEPOTS.values()
                     if solver.resolve_depot(cfg["keys"][0] if cfg["keys"] else "", "")[2] != "osm"]
    if approx_depots:
        print("ВНИМАНИЕ: координаты депо ещё не разрешены геокодером:")
        for d in approx_depots:
            print(f"  - {d}")
        print("Запустите python3 geocode_addresses.py и повторите сборку.\n")

    csv_files = sorted(glob.glob(os.path.join(root, "test_dataset", "*Синтетические*.csv")))
    if not csv_files:
        print("Не найдены синтетические датасеты в test_dataset/")
        return 1

    failed = False
    for csv_path in csv_files:
        name = os.path.basename(csv_path)
        points = dataset_points(csv_path)
        print(f"{name}: {len(points)} точек")
        res = solver.prefetch_osrm(points, ("car", "foot", "bicycle"),
                                   fetch=fetch, pause_sec=pause_sec, quiet=False)
        if res.get("errors"):
            failed = True
            for transport, err in res["errors"].items():
                print(f"  ОШИБКА {transport}: {err}")

    solver.save_osrm_cache()
    factors = solver.calibrate_winding_factors()
    print(f"\nКеш сохранён: {solver._OSRM_CACHE_FILE} ({len(solver._OSRM_CACHE)} записей)")
    print(f"Коэффициенты извилистости, откалиброванные по графу: {factors}")

    # Контроль покрытия: решатель не должен нигде скатываться на приближение по прямой
    print("\nПокрытие графом (депо + заявки, по каждому виду транспорта):")
    incomplete = False
    for csv_path in csv_files:
        points = dataset_points(csv_path)
        missing = solver.missing_osrm_pairs(points, ("car", "foot", "bicycle"))
        total = len(points) * (len(points) - 1)
        if missing:
            incomplete = True
            detail = ", ".join(f"{t}: не хватает {m}" for t, m in missing.items())
            print(f"  {os.path.basename(csv_path)}: {detail} из {total} пар")
        else:
            print(f"  {os.path.basename(csv_path)}: 100% ({total} пар на каждый граф)")

    if failed or incomplete:
        print("\nПокрытие неполное. Повторный запуск дозагрузит недостающее.")
        return 1
    print("\nГотово. Дальше: python3 embed_frontend_assets.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
