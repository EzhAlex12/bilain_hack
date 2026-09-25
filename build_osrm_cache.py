"""
Досчитывает кеш дорожных расстояний .osrm_cache.json для всех точек синтетических датасетов.

Запуск (нужен интернет): python3 build_osrm_cache.py

Графы по видам транспорта:
- car      — автомобильный граф демо-сервера OSRM (router.project-osrm.org);
- foot     — пешеходный граф FOSSGIS (routing.openstreetmap.de/routed-foot):
             тротуары, пешеходные переходы, дворы и сквозные проходы;
- bicycle  — велосипедный граф FOSSGIS (routing.openstreetmap.de/routed-bike):
             велодорожки, тротуары, дворовые зоны.
Демо-сервер router.project-osrm.org держит только автомобильный граф и игнорирует
профиль foot/bike в URL — поэтому пешеходы и велосипеды берутся с FOSSGIS.
Общественный транспорт (transit) выводится решателем из автомобильного графа.
"""
from __future__ import annotations

import glob
import json
import os
import time
import urllib.request
from typing import Callable, Dict, List, Tuple

import vrptw_4pass_solver as solver

TABLE_URLS = {
    "car": "https://router.project-osrm.org/table/v1/driving/",
    "foot": "https://routing.openstreetmap.de/routed-foot/table/v1/driving/",
    "bicycle": "https://routing.openstreetmap.de/routed-bike/table/v1/driving/",
}
MAX_TABLE_COORDS = 100  # лимит координат в одном запросе table у публичных серверов OSRM
BLOCK = MAX_TABLE_COORDS // 2

Point = Tuple[float, float]


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "BeelineVRP/1.0 (hackathon LCT 2026)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def table_urls(points: List[Point], transport: str) -> List[Tuple[str, List[int], List[int]]]:
    """Разбивает матрицу points x points на запросы table (источники/назначения блоками)."""
    base = TABLE_URLS[transport]
    if len(points) <= MAX_TABLE_COORDS:
        blocks = [(list(range(len(points))), list(range(len(points))))]
    else:
        idx = list(range(len(points)))
        chunks = [idx[i:i + BLOCK] for i in range(0, len(idx), BLOCK)]
        blocks = [(a, b) for a in chunks for b in chunks]

    urls = []
    for src, dst in blocks:
        coords_idx = src + [j for j in dst if j not in src]
        coords = ";".join(f"{points[i][1]:.6f},{points[i][0]:.6f}" for i in coords_idx)
        pos = {j: p for p, j in enumerate(coords_idx)}
        query = (
            "?annotations=distance"
            f"&sources={';'.join(str(pos[i]) for i in src)}"
            f"&destinations={';'.join(str(pos[j]) for j in dst)}"
        )
        urls.append((base + coords + query, src, dst))
    return urls


def fetch_matrix(points: List[Point], transport: str,
                 fetch: Callable[[str], dict] = fetch_json) -> Dict[str, float]:
    """Возвращает записи кеша {ключ: км} для всех пар points по графу transport."""
    entries: Dict[str, float] = {}
    for url, src, dst in table_urls(points, transport):
        data = fetch(url)
        if data.get("code") != "Ok":
            raise RuntimeError(f"{transport}: OSRM ответил {data.get('code')}: {data.get('message')}")
        for a, row in zip(src, data["distances"]):
            for b, meters in zip(dst, row):
                if a == b or meters is None:
                    continue
                p1, p2 = points[a], points[b]
                entries[solver.osrm_cache_key(p1[0], p1[1], p2[0], p2[1], transport)] = round(meters / 1000.0, 2)
    return entries


def dataset_points(csv_path: str) -> List[Point]:
    """Депо + все заявки датасета — ровно те точки, между которыми считает решатель."""
    requests, depot, _, _ = solver.load_dataset(csv_path)
    points: List[Point] = [depot]
    for r in requests:
        if (r.lat, r.lon) not in points:
            points.append((r.lat, r.lon))
    return points


def main(fetch: Callable[[str], dict] = fetch_json, pause_sec: float = 1.0) -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    csv_files = sorted(glob.glob(os.path.join(root, "test_dataset", "*Синтетические*.csv")))
    for csv_path in csv_files:
        points = dataset_points(csv_path)
        for transport in TABLE_URLS:
            entries = fetch_matrix(points, transport, fetch)
            solver._OSRM_CACHE.update(entries)
            print(f"{os.path.basename(csv_path)}: {transport} — {len(entries)} пар")
            time.sleep(pause_sec)  # бережём публичные серверы
    solver.save_osrm_cache()
    print(f"Кеш сохранён: {solver._OSRM_CACHE_FILE} ({len(solver._OSRM_CACHE)} записей)")


if __name__ == "__main__":
    main()
