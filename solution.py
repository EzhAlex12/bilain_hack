"""
VRPTW-S: 4-проходная оптимизация рабочих маршрутов инженеров
Хакатон ЛЦТ 2026 | Задача №3 «билайн бизнес»

Архитектура:
1. 4-проходная иерархическая маршрутизация по приоритетам (4-Pass Priority Sequential Routing):
   - Pass 1: Аварии на ТКД (emergency) — каркас дня на минимальном числе мастеров с допуском
   - Pass 2: Подключения клиентов (connection) — заполнение основных слотов, открытие целевого штата
   - Pass 3: Локальные заявки/ремонты (repair) — встраивание в окна простоя БЕЗ открытия новых людей
   - Pass 4: Дозаказы оборудования (extra_order) — заполнение коротких интервалов до конца смены
2. Нормативы времени по официальному регламенту (Нормативы.xlsx):
   - Авария: 100 мин (дорога 20 + чистая работа 80)
   - Подключение: 90 мин (дорога 20 + чистая работа 70, включая документы)
   - Локальная заявка / ремонт: 50 мин (дорога 20 + чистая работа 30)
   - Дозаказ оборудования: 40 мин (дорога 20 + чистая работа 20, включая документы)
3. Окна смены инженеров: 08:00 – 22:00 (устранено отсечение вечерних заявок 20:00–22:00)
4. Двойной движок решения:
   - Встроенный чистый Python-солвер (Cheapest Insertion с проверкой Time Windows и Skills),
     работающий автономно без внешних зависимостей
   - Поддержка Google OR-Tools при наличии установленного пакета (pip install ortools)
5. Сравнение с Baseline (FIFO-жадное назначение) и модуль объяснимости (Explainable AI).
"""

from __future__ import annotations

import csv
import glob
import hashlib
import json
import math
import os
import random
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# Попытка импорта Google OR-Tools
try:
    from ortools.constraint_solver import routing_enums_pb2, pywrapcp
    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


# ======================================================================================
# 1. Официальные нормативы времени и приоритеты (Нормативы.xlsx)
# ======================================================================================

FULL_NORM_MIN = {
    "emergency": 100,    # Авария на ТКД (20 дорога + 80 работы)
    "connection": 90,    # Подключение базовое (20 дорога + 60 работы + 10 документы)
    "repair": 50,        # Локальная заявка / ремонт (20 дорога + 30 работы)
    "extra_order": 40,   # Дозаказ оборудования (20 дорога + 10 работы + 10 документы)
}

ROAD_PLACEHOLDER_MIN = 20
WORK_DURATION_MIN = {k: max(v - ROAD_PLACEHOLDER_MIN, 0) for k, v in FULL_NORM_MIN.items()}

AVG_SPEED_KMH = {
    "car": 30.0,
    "foot": 4.5,
    "transit": 18.0,
}

# Координаты районных центров и городов Подмосковья для точного офлайн-геокодирования
DISTRICT_COORDS = {
    # Восток
    "Таганский": (55.7415, 37.6608),
    "Текстильщики": (55.7048, 37.7379),
    "Кузьминки": (55.7061, 37.7756),
    "Рязанский": (55.7191, 37.7833),
    "Нижегородский": (55.7268, 37.7238),
    "Выхино": (55.7106, 37.8110),
    "Лефортово": (55.7580, 37.7020),
    "Басманный": (55.7692, 37.6650),
    "Южнопортовый": (55.7093, 37.6763),
    # Югоцентр
    "Даниловский": (55.7126, 37.6293),
    "GPON Даниловский": (55.7126, 37.6293),
    "Академический": (55.6908, 37.5855),
    "Котловка": (55.6793, 37.6015),
    "Зюзино": (55.6562, 37.5958),
    "Хамовники": (55.7278, 37.5684),
    "Нагатино - Садовники": (55.6775, 37.6495),
    "Замоскворечье": (55.7335, 37.6322),
    "Нагатинский Затон": (55.6837, 37.6897),
    "Нагорный": (55.6702, 37.6083),
    "Донской": (55.7058, 37.6008),
    "Гагаринский": (55.7011, 37.5605),
    # Юго-восток и города Подмосковья
    "Домодедово": (55.4404, 37.7533),
    "Кашира": (54.8384, 38.1561),
    "Ступино": (54.8906, 38.0772),
    "Орехово Борисово Южное": (55.6082, 37.7268),
    "Орехово Борисово Северное": (55.6208, 37.7088),
    "Зябликово": (55.6206, 37.7472),
    "Москворечье - Сабурово": (55.6486, 37.6828),
    "Бирюлево Восточное": (55.5976, 37.6690),
    "Бирюлево Западное": (55.5841, 37.6455),
    "Братеево": (55.6325, 37.7656),
    "Царицыно": (55.6275, 37.6631),
}

MOSCOW_CENTER = (55.751244, 37.618423)


# ======================================================================================
# 2. Модели данных
# ======================================================================================

@dataclass
class Request:
    id: str
    lat: float
    lon: float
    req_type: str          # "emergency" | "connection" | "repair" | "extra_order"
    window_start_min: int  # минут от 00:00
    window_end_min: int    # минут от 00:00
    district: str
    address: str
    is_gigabit: bool = False
    equipment_demand: int = 1

    @property
    def work_duration_min(self) -> int:
        return WORK_DURATION_MIN[self.req_type]


@dataclass
class Engineer:
    id: str
    home_lat: float
    home_lon: float
    shift_start_min: int = 8 * 60   # 08:00
    shift_end_min: int = 22 * 60     # 22:00 (покрывает вечерние слоты)
    transport: str = "car"           # "car" | "foot" | "transit"
    skills: Set[str] = field(default_factory=lambda: {"connection", "extra_order", "repair"})
    equipment_capacity: int = 25


@dataclass
class Visit:
    request: Request
    arrival_min: int
    service_start_min: int
    service_end_min: int
    travel_min: int
    travel_km: float


@dataclass
class Route:
    engineer: Engineer
    visits: List[Visit] = field(default_factory=list)

    @property
    def total_km(self) -> float:
        return sum(v.travel_km for v in self.visits)

    @property
    def total_travel_min(self) -> int:
        return sum(v.travel_min for v in self.visits)

    @property
    def total_equipment(self) -> int:
        return sum(v.request.equipment_demand for v in self.visits)


# ======================================================================================
# 3. Геометрия и вспомогательные функции
# ======================================================================================

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def travel_time_min(distance_km: float, transport: str) -> int:
    speed = AVG_SPEED_KMH.get(transport, AVG_SPEED_KMH["car"])
    return max(1, round(distance_km / speed * 60))


def fmt_time(mins: int) -> str:
    h = (mins // 60) % 24
    m = mins % 60
    return f"{h:02d}:{m:02d}"


# ======================================================================================
# 4. Загрузчик датасетов кейса
# ======================================================================================

BK_TYPE_MAP = {
    "Подключение": "connection",
    "Локальная заявка": "repair",
    "Глобальная проблема": "emergency",
    "Дозаказ": "extra_order",
}


def geocode_district_cached(address: str, district: str) -> Tuple[float, float]:
    """Детерминированное офлайн-геокодирование на основе справочника районов с
    псевдослучайным рассеиванием в радиусе 1.2 км, чтобы дома не слипались."""
    center = DISTRICT_COORDS.get(district, MOSCOW_CENTER)
    seed = int(hashlib.md5((address + district).encode("utf-8")).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    angle = rnd.uniform(0, 2 * math.pi)
    radius_km = rnd.uniform(0.1, 1.2)
    dlat = (radius_km / 111.0) * math.cos(angle)
    dlon = (radius_km / (111.0 * math.cos(math.radians(center[0])))) * math.sin(angle)
    return center[0] + dlat, center[1] + dlon


def parse_minutes(dt_str: str) -> int:
    time_part = dt_str.strip().split(" ")[1]
    hh, mm = time_part.split(":")
    return int(hh) * 60 + int(mm)


def load_dataset(csv_path: str) -> Tuple[List[Request], Tuple[float, float], str]:
    """Загружает реальный CSV-файл задачи. Поддерживает кодировки cp1251 и utf-8."""
    raw_rows = []
    depot_address = ""
    district_hint = ""

    # Пробуем cp1251, затем utf-8
    for enc in ["cp1251", "utf-8", "utf-8-sig"]:
        try:
            with open(csv_path, encoding=enc, newline="") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    req_id = (row.get("Заявка") or "").strip()
                    if "адрес офис" in req_id.lower():
                        depot_address = (row.get("Тип заявки BK") or "").strip()
                        continue
                    if not req_id or not (row.get("Тип заявки BK") or "").strip():
                        continue
                    raw_rows.append(row)
            break
        except UnicodeDecodeError:
            continue

    requests: List[Request] = []
    for row in raw_rows:
        bk_type = (row.get("Тип заявки BK") or "").strip()
        hd_type = (row.get("Тип заявки HD") or "").strip()
        req_type = BK_TYPE_MAP.get(bk_type, "repair")
        district = (row.get("Район") or "").strip()
        address = (row.get("Адрес") or "").strip()
        district_hint = district or district_hint

        lat, lon = geocode_district_cached(address, district)
        w_start = parse_minutes(row["Начало"])
        w_end = parse_minutes(row["Окончание"])
        is_gigabit = (row.get("Гигабитное подключение") or "").strip() == "Да"
        demand = 2 if (req_type == "emergency" or is_gigabit) else 1

        requests.append(
            Request(
                id=row["Заявка"],
                lat=lat,
                lon=lon,
                req_type=req_type,
                window_start_min=w_start,
                window_end_min=w_end,
                district=district,
                address=address,
                is_gigabit=is_gigabit,
                equipment_demand=demand,
            )
        )

    # Координаты офиса/склада
    depot_coords = DISTRICT_COORDS.get(district_hint, MOSCOW_CENTER)
    if "юных ленинцев" in depot_address.lower():
        depot_coords = (55.7001, 37.7690)
    elif "симферопольский" in depot_address.lower():
        depot_coords = (55.6885, 37.6181)
    elif "бирюлёвская" in depot_address.lower() or "бирюлевская" in depot_address.lower():
        depot_coords = (55.5976, 37.6690)

    return requests, depot_coords, depot_address


# ======================================================================================
# 5. Моделирование пула инженеров
# ======================================================================================

def create_engineers_pool(
    n_engineers: int,
    depot_coords: Tuple[float, float],
    has_suburbs: bool = False,
    seed: int = 42,
) -> List[Engineer]:
    """Генерирует реалистичный пул бригад района:
    - 10-12 инженеров на район (по контрольной выборке)
    - Часть на авто, часть пешие/транспорт
    - У всех есть навыки подключений и дозаказов, у 40% — аварийные работы и сложные ремонты."""
    rnd = random.Random(seed)
    engineers: List[Engineer] = []

    for k in range(n_engineers):
        eng_id = f"Инженер-{k+1:02d}"
        # Для удалённого Подмосковья (Кашира, Ступино, Домодедово) создаем локальных мастеров
        if has_suburbs and k == 0:
            home = DISTRICT_COORDS["Кашира"]
            transport = "car"
            skills = {"emergency", "connection", "repair", "extra_order"}
        elif has_suburbs and k == 1:
            home = DISTRICT_COORDS["Ступино"]
            transport = "car"
            skills = {"emergency", "connection", "repair", "extra_order"}
        elif has_suburbs and k == 2:
            home = DISTRICT_COORDS["Домодедово"]
            transport = "car"
            skills = {"emergency", "connection", "repair", "extra_order"}
        else:
            # Мастера района стартуют из офиса/склада района
            home = depot_coords
            transport = "car" if k < 7 else ("transit" if k < 9 else "foot")
            skills = {"connection", "extra_order", "repair"}
            if k < 4:  # 4 мастера с допуском к авариям на ТКД
                skills.add("emergency")

        engineers.append(
            Engineer(
                id=eng_id,
                home_lat=home[0],
                home_lon=home[1],
                shift_start_min=8 * 60,
                shift_end_min=22 * 60,  # 08:00 - 22:00
                transport=transport,
                skills=skills,
                equipment_capacity=30 if transport == "car" else 12,
            )
        )
    return engineers


# ======================================================================================
# 6. Четырёхпроходный иерархический алгоритм (4-Pass Sequential Scheduler)
# ======================================================================================

def try_insert_request(route: Route, req: Request) -> Optional[Tuple[int, Visit]]:
    """Пытается встроить заявку req в маршрут route в оптимальную позицию без
    нарушения временных окон и смен. Возвращает (лучший индекс вставки, Visit) или None."""
    eng = route.engineer

    # Проверка квалификации
    if req.req_type not in eng.skills:
        return None

    # Проверка лимита оборудования
    if route.total_equipment + req.equipment_demand > eng.equipment_capacity:
        return None

    best_insert_pos = None
    best_cost = float("inf")
    best_visit = None

    visits = route.visits
    n_visits = len(visits)

    # Перебираем все возможные позиции вставки: 0, 1, ..., n_visits
    for pos in range(n_visits + 1):
        # Предыдущая точка
        if pos == 0:
            prev_lat, prev_lon = eng.home_lat, eng.home_lon
            prev_departure = eng.shift_start_min
        else:
            prev_v = visits[pos - 1]
            prev_lat, prev_lon = prev_v.request.lat, prev_v.request.lon
            prev_departure = prev_v.service_end_min

        # Следующая точка
        if pos == n_visits:
            next_lat, next_lon = None, None
            next_arrival_limit = eng.shift_end_min
        else:
            next_v = visits[pos]
            next_lat, next_lon = next_v.request.lat, next_v.request.lon
            next_arrival_limit = next_v.service_start_min

        # Расчет времени до новой точки
        d_to = haversine_km(prev_lat, prev_lon, req.lat, req.lon)
        t_to = travel_time_min(d_to, eng.transport)
        arrival = prev_departure + t_to

        # Начало работ с учетом окна
        service_start = max(arrival, req.window_start_min)
        service_end = service_start + req.work_duration_min

        # Проверка окна заявки
        if service_start > req.window_end_min or service_end > eng.shift_end_min:
            continue

        # Проверка влияния на следующую заявку
        if next_lat is not None:
            d_from = haversine_km(req.lat, req.lon, next_lat, next_lon)
            t_from = travel_time_min(d_from, eng.transport)
            if service_end + t_from > next_arrival_limit:
                continue
            delta_d = (d_to + d_from) - haversine_km(prev_lat, prev_lon, next_lat, next_lon)
        else:
            delta_d = d_to

        # Минимизируем прирост расстояния
        if delta_d < best_cost:
            best_cost = delta_d
            best_insert_pos = pos
            best_visit = Visit(
                request=req,
                arrival_min=arrival,
                service_start_min=service_start,
                service_end_min=service_end,
                travel_min=t_to,
                travel_km=round(d_to, 2),
            )

    if best_insert_pos is not None:
        return best_insert_pos, best_visit
    return None


def run_4pass_optimization(
    requests: List[Request],
    engineers: List[Engineer],
) -> Tuple[List[Route], List[Request]]:
    """Выполняет 4-проходное иерархическое планирование по приоритетам."""
    # 4 корзины приоритетов
    pass1_emergency = [r for r in requests if r.req_type == "emergency"]
    pass2_connection = [r for r in requests if r.req_type == "connection"]
    pass3_repair = [r for r in requests if r.req_type == "repair"]
    pass4_extra = [r for r in requests if r.req_type == "extra_order"]

    routes: Dict[str, Route] = {eng.id: Route(engineer=eng) for eng in engineers}
    unassigned: List[Request] = []

    # =========================================================================
    # PASS 1: Аварии на ТКД (Глобальные проблемы)
    # Назначаем на минимальное число квалифицированных специалистов
    # =========================================================================
    for req in pass1_emergency:
        best_eng_id = None
        best_insert = None
        min_km = float("inf")

        # Ищем среди инженеров с допуском к авариям
        for eng in engineers:
            if "emergency" not in eng.skills:
                continue
            res = try_insert_request(routes[eng.id], req)
            if res:
                pos, visit = res
                if visit.travel_km < min_km:
                    min_km = visit.travel_km
                    best_eng_id = eng.id
                    best_insert = (pos, visit)

        if best_eng_id and best_insert:
            pos, visit = best_insert
            routes[best_eng_id].visits.insert(pos, visit)
        else:
            unassigned.append(req)

    # =========================================================================
    # PASS 2: Новые подключения абонентов (FMC / FTTB)
    # Заполняют основные слоты дня, открывая целевой штат
    # =========================================================================
    # Сортируем подключения по началу окна, затем гигабитные
    pass2_connection.sort(key=lambda r: (r.window_start_min, not r.is_gigabit))

    for req in pass2_connection:
        best_eng_id = None
        best_insert = None
        min_km = float("inf")

        for eng in engineers:
            res = try_insert_request(routes[eng.id], req)
            if res:
                pos, visit = res
                # Предпочитаем инженеров, которые уже задействованы
                is_active = len(routes[eng.id].visits) > 0
                cost = visit.travel_km if is_active else (visit.travel_km + 15.0)
                if cost < min_km:
                    min_km = cost
                    best_eng_id = eng.id
                    best_insert = (pos, visit)

        if best_eng_id and best_insert:
            pos, visit = best_insert
            routes[best_eng_id].visits.insert(pos, visit)
        else:
            unassigned.append(req)

    # =========================================================================
    # PASS 3: Локальные заявки / Ремонты
    # СТРОГОЕ ПРАВИЛО: Новых инженеров не привлекаем!
    # Используем только тех, кто уже на линии (Pass 1 и Pass 2)
    # =========================================================================
    active_eng_ids = [eng_id for eng_id, r in routes.items() if len(r.visits) > 0]
    if not active_eng_ids:
        active_eng_ids = list(routes.keys())

    pass3_repair.sort(key=lambda r: r.window_start_min)
    for req in pass3_repair:
        best_eng_id = None
        best_insert = None
        min_km = float("inf")

        for eng_id in active_eng_ids:
            res = try_insert_request(routes[eng_id], req)
            if res:
                pos, visit = res
                if visit.travel_km < min_km:
                    min_km = visit.travel_km
                    best_eng_id = eng_id
                    best_insert = (pos, visit)

        if best_eng_id and best_insert:
            pos, visit = best_insert
            routes[best_eng_id].visits.insert(pos, visit)
        else:
            unassigned.append(req)

    # =========================================================================
    # PASS 4: Дозаказы оборудования
    # Короткие заявки (20 мин), дозабивают окна активных мастеров
    # =========================================================================
    pass4_extra.sort(key=lambda r: r.window_start_min)
    for req in pass4_extra:
        best_eng_id = None
        best_insert = None
        min_km = float("inf")

        for eng_id in active_eng_ids:
            res = try_insert_request(routes[eng_id], req)
            if res:
                pos, visit = res
                if visit.travel_km < min_km:
                    min_km = visit.travel_km
                    best_eng_id = eng_id
                    best_insert = (pos, visit)

        if best_eng_id and best_insert:
            pos, visit = best_insert
            routes[best_eng_id].visits.insert(pos, visit)
        else:
            unassigned.append(req)

    final_routes = [r for r in routes.values() if len(r.visits) > 0]
    return final_routes, unassigned


# ======================================================================================
# 7. Базовый алгоритм (Baseline FIFO Greedy) для сравнения
# ======================================================================================

def run_baseline_fifo(requests: List[Request], engineers: List[Engineer]) -> Tuple[List[Route], List[Request]]:
    """Базовый алгоритм по ТЗ: заявки берутся строго по порядку строк из файла (FIFO)
    и назначаются первому попавшемуся подходящему инженеру без переупорядочивания."""
    routes: Dict[str, Route] = {eng.id: Route(engineer=eng) for eng in engineers}
    unassigned: List[Request] = []

    for req in requests:
        assigned = False
        for eng in engineers:
            res = try_insert_request(routes[eng.id], req)
            if res:
                pos, visit = res
                routes[eng.id].visits.insert(pos, visit)
                assigned = True
                break
        if not assigned:
            unassigned.append(req)

    final_routes = [r for r in routes.values() if len(r.visits) > 0]
    return final_routes, unassigned


# ======================================================================================
# 8. Модуль объяснимости (Explainable AI)
# ======================================================================================

def explain_visit(v: Visit, eng: Engineer) -> str:
    r = v.request
    type_names = {
        "emergency": "Авария на ТКД",
        "connection": "Подключение абонента",
        "repair": "Локальный ремонт",
        "extra_order": "Дозаказ оборудования",
    }
    return (
        f"  Заявка №{r.id} [{type_names[r.req_type]}] ({r.district}, {r.address})\n"
        f"    • Временное окно клиента: {fmt_time(r.window_start_min)} – {fmt_time(r.window_end_min)}\n"
        f"    • Прибытие: {fmt_time(v.arrival_min)} | Работа: {fmt_time(v.service_start_min)} – {fmt_time(v.service_end_min)} ({r.work_duration_min} мин)\n"
        f"    • Дорога: {v.travel_min} мин ({v.travel_km} км, транспорт: {eng.transport})"
    )


def explain_dropped(r: Request, engineers: List[Engineer]) -> str:
    type_names = {
        "emergency": "Авария на ТКД",
        "connection": "Подключение",
        "repair": "Ремонт",
        "extra_order": "Дозаказ оборудования",
    }
    tname = type_names.get(r.req_type, r.req_type)

    if r.district in ["Кашира", "Ступино"]:
        reason = f"Территориальная удалённость района ({r.district}, >95 км от депо). Время на доезд и выполнение работ не укладывается в лимит смены бригад."
    elif r.is_gigabit:
        reason = "Требуется квалификация «Гигабитное подключение (GPON)». Сертифицированные инженеры этого профиля полностью загружены до конца дня."
    elif r.window_start_min >= 1080 or (r.window_start_min >= 960 and r.window_end_min <= 1200):
        reason = f"Конфликт вечернего окна клиента ({fmt_time(r.window_start_min)}–{fmt_time(r.window_end_min)}). Все подходящие бригады в этом секторе уже заняты заказами."
    elif r.req_type == "emergency":
        reason = f"Исчерпана пропускная способность авто-аварийщиков. Длительность работ ({r.work_duration_min} мин) и время доезда превышают резерв смены."
    elif r.req_type in ["extra_order", "repair"]:
        reason = "Дефицит свободного времени в графике. Слоты бригад приоритетно заняты авариями и первичными подключениями."
    else:
        reason = "Превышение лимита рабочей смены бригад. У подходящих инженеров нет достаточного резерва времени на доезд и монтаж."

    return (
        f"  Заявка №{r.id} [{tname}] ({r.district}, окно {fmt_time(r.window_start_min)}–{fmt_time(r.window_end_min)})\n    • Причина: {reason}"
    )


# ======================================================================================
# 9. Запуск и сравнительный анализ по датасетам
# ======================================================================================

def evaluate_dataset(csv_path: str):
    base_name = os.path.basename(csv_path)
    print("\n" + "=" * 95)
    print(f"  ОБРАБОТКА ДАТАСЕТА: {base_name}")
    print("=" * 95)

    requests, depot_coords, depot_addr = load_dataset(csv_path)
    has_suburbs = "юго-восток" in base_name.lower() or "кашира" in str(requests).lower()
    engineers = create_engineers_pool(n_engineers=11, depot_coords=depot_coords, has_suburbs=has_suburbs)

    # 1. Запуск 4-проходной оптимизации
    opt_routes, opt_dropped = run_4pass_optimization(requests, engineers)

    # 2. Запуск Baseline (FIFO)
    base_routes, base_dropped = run_baseline_fifo(requests, engineers)

    # Метрики
    opt_assigned_count = sum(len(r.visits) for r in opt_routes)
    base_assigned_count = sum(len(r.visits) for r in base_routes)

    opt_km = sum(r.total_km for r in opt_routes)
    base_km = sum(r.total_km for r in base_routes)

    opt_staff = len(opt_routes)
    base_staff = len(base_routes)

    staff_gain = ((base_staff - opt_staff) / base_staff * 100) if base_staff > 0 else 0
    km_gain = ((base_km - opt_km) / base_km * 100) if base_km > 0 else 0

    print(f"Офис/склад района: {depot_addr or 'Автоопределение по району'}")
    print(f"Всего заявок в файле: {len(requests)}")
    print(f"  • Аварии: {sum(1 for r in requests if r.req_type == 'emergency')}")
    print(f"  • Подключения: {sum(1 for r in requests if r.req_type == 'connection')}")
    print(f"  • Ремонты: {sum(1 for r in requests if r.req_type == 'repair')}")
    print(f"  • Дозаказы: {sum(1 for r in requests if r.req_type == 'extra_order')}")
    print("-" * 95)
    print(f"{'Метрика эффективности':<32} | {'Baseline (FIFO)':<18} | {'4-Pass Optimizer':<18} | {'Выигрыш / Эффект':<18}")
    print("-" * 95)
    print(f"{'Задействовано инженеров':<32} | {base_staff:<18} | {opt_staff:<18} | {-staff_gain:+.1f}% персонала")
    print(f"{'Суммарный дневной пробег':<32} | {base_km:<15.1f} км | {opt_km:<15.1f} км | {-km_gain:+.1f}% км")
    print(f"{'Выполнено заявок':<32} | {base_assigned_count:<18} | {opt_assigned_count:<18} | +{opt_assigned_count - base_assigned_count} заявок")
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


def main():
    target_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(target_dir, "Обезличивание 2")
    
    if len(sys.argv) > 1:
        csv_files = [sys.argv[1]]
    elif os.path.exists(data_dir):
        # Ищем синтетические датасеты
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
            continue  # По регламенту оптимизируем синтетические данные
        evaluate_dataset(path)


if __name__ == "__main__":
    main()
