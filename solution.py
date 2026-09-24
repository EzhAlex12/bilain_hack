"""
Консольный бенчмарк: python3 solution.py [csv_path]

Запускает 4-Pass оптимизацию + OR-Tools полировку на всех датасетах и
выводит сравнительную таблицу метрик (vs Baseline FIFO).

Вся логика алгоритма сосредоточена в vrptw_4pass_solver.py,
этот файл — тонкая консольная обёртка.
"""
from __future__ import annotations

import glob
import os
import sys

# Весь алгоритм живёт здесь — impортируем всё сразу
from vrptw_4pass_solver import (
    HAS_ORTOOLS,
    load_dataset,
    create_engineers_pool,
    run_4pass_optimization,
    optimize_routes_with_ortools,
    run_baseline_fifo,
    explain_visit,
    explain_dropped,
    fmt_time,
)


def evaluate_dataset(csv_path: str) -> None:
    """Загружает датасет, оптимизирует маршруты и печатает сравнительную таблицу."""
    base_name = os.path.basename(csv_path)
    print("\n" + "=" * 95)
    print(f"  ОБРАБОТКА ДАТАСЕТА: {base_name}")
    print("=" * 95)

    requests, depot_coords, depot_addr, brigade_names = load_dataset(csv_path)

    has_suburbs = "юго-восток" in base_name.lower() or any(
        r.district in ("Кашира", "Ступино", "Домодедово") for r in requests
    )
    n_eng = max(11, len(brigade_names)) if brigade_names else 11
    engineers = create_engineers_pool(
        n_engineers=n_eng,
        depot_coords=depot_coords,
        has_suburbs=has_suburbs,
        brigade_names=brigade_names or [],
    )

    # 1. 4-Pass оптимизация (гарантированное допустимое решение)
    opt_routes, opt_dropped = run_4pass_optimization(requests, engineers)

    # 2. Полировка через OR-Tools (с fallback на допустимое решение)
    opt_routes, ortools_status_str = optimize_routes_with_ortools(opt_routes, time_limit_sec=2)

    # 3. Baseline FIFO для сравнения
    base_routes, base_dropped = run_baseline_fifo(requests, engineers)

    # Метрики
    opt_assigned = sum(len(r.visits) for r in opt_routes)
    base_assigned = sum(len(r.visits) for r in base_routes)
    opt_km = sum(r.total_km for r in opt_routes)
    base_km = sum(r.total_km for r in base_routes)
    opt_staff = len(opt_routes)
    base_staff = len(base_routes)
    staff_gain = ((base_staff - opt_staff) / base_staff * 100) if base_staff > 0 else 0
    km_gain = ((base_km - opt_km) / base_km * 100) if base_km > 0 else 0

    print(f"Офис/склад района: {depot_addr or 'Автоопределение по району'}")
    print(f"Статус оптимизатора: {ortools_status_str}")
    print(f"Всего заявок в файле: {len(requests)}")
    print(f"  • Аварии:      {sum(1 for r in requests if r.req_type == 'emergency')}")
    print(f"  • Подключения: {sum(1 for r in requests if r.req_type == 'connection')}")
    print(f"  • Ремонты:     {sum(1 for r in requests if r.req_type == 'repair')}")
    print(f"  • Дозаказы:    {sum(1 for r in requests if r.req_type == 'extra_order')}")
    print("-" * 95)
    print(f"{'Метрика эффективности':<32} | {'Baseline (FIFO)':<18} | {'4-Pass Optimizer':<18} | {'Выигрыш / Эффект':<18}")
    print("-" * 95)
    print(f"{'Задействовано инженеров':<32} | {base_staff:<18} | {opt_staff:<18} | {-staff_gain:+.1f}% персонала")
    print(f"{'Суммарный дневной пробег':<32} | {base_km:<15.1f} км | {opt_km:<15.1f} км | {-km_gain:+.1f}% км")
    print(f"{'Выполнено заявок':<32} | {base_assigned:<18} | {opt_assigned:<18} | +{opt_assigned - base_assigned} заявок")
    print(f"{'Не назначено заявок':<32} | {len(base_dropped):<18} | {len(opt_dropped):<18} | {len(base_dropped) - len(opt_dropped)} спасено")
    print("-" * 95)

    print("\n--- Пример расписания 1-го инженера (Explainable AI) ---")
    if opt_routes:
        r0 = opt_routes[0]
        print(f"Инженер: {r0.engineer.id} ({r0.engineer.transport}), заявок: {len(r0.visits)}, пробег: {r0.total_km:.1f} км:")
        for v in r0.visits:
            print(explain_visit(v, r0.engineer))

    if opt_dropped:
        print("\n--- Пример объяснения неназначенных заявок ---")
        for r in opt_dropped[:2]:
            print(explain_dropped(r, engineers))


def main() -> None:
    target_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(target_dir, "test_dataset")

    if len(sys.argv) > 1:
        csv_files = [sys.argv[1]]
    elif os.path.exists(data_dir):
        csv_files = sorted(glob.glob(os.path.join(data_dir, "*Синтетические*.csv")))
        if not csv_files:
            csv_files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    else:
        csv_files = sorted(glob.glob("*.csv"))

    if not csv_files:
        print("CSV файлы не найдены. Укажите путь к CSV аргументом командной строки.")
        return

    print("=" * 95)
    print("  ХАКАТОН ЛЦТ 2026 | ЗАДАЧА №3: БИЛАЙН БИЗНЕС")
    print("  Тестирование 4-проходной модели маршрутизации на реальных датасетах")
    print(f"  Движок: {'Google OR-Tools + Эвристика' if HAS_ORTOOLS else 'Встроенный чистый Python VRPTW'}")
    print("=" * 95)

    for path in csv_files:
        if "контрольное" in path.lower():
            continue  # По регламенту оптимизируем только синтетические данные
        evaluate_dataset(path)


if __name__ == "__main__":
    main()
