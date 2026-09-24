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

# ======================================================================================
# КЕШ И КЛИЕНТ РЕАЛЬНОГО ДОРОЖНОГО ГРАФА OSRM
# ======================================================================================

_OSRM_CACHE: dict = {}
_OSRM_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".osrm_cache.json")

def load_osrm_cache():
    global _OSRM_CACHE
    if os.path.exists(_OSRM_CACHE_FILE):
        try:
            with open(_OSRM_CACHE_FILE, "r", encoding="utf-8") as f:
                _OSRM_CACHE = json.load(f)
        except Exception:
            _OSRM_CACHE = {}

def save_osrm_cache():
    try:
        with open(_OSRM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_OSRM_CACHE, f, ensure_ascii=False)
    except Exception:
        pass

load_osrm_cache()

def get_osrm_road_distance(c1: Tuple[float, float], c2: Tuple[float, float], transport: str = "car") -> float:
    if c1 == c2:
        return 0.0
    key = f"{c1[0]:.4f},{c1[1]:.4f}_{c2[0]:.4f},{c2[1]:.4f}_{transport}"
    if key in _OSRM_CACHE:
        return _OSRM_CACHE[key]

    k_wind = {"car": 1.35, "bicycle": 1.15, "foot": 1.20}.get(transport, 1.25)
    return round(haversine_km(c1[0], c1[1], c2[0], c2[1]) * k_wind, 2)

def batch_fetch_osrm_distances(coords: List[Tuple[float, float]], transport: str = "car"):
    """Пакетно запрашивает матрицу дорожных расстояний через OSRM Table API и сохраняет в кеш."""
    if len(coords) < 2:
        return
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()

    subset = coords[:65]
    coords_str = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in subset)
    profile = "driving" if transport == "car" else ("bike" if transport == "bicycle" else "foot")
    url = f"https://router.project-osrm.org/table/v1/{profile}/{coords_str}?annotations=distance"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BeelineVRP/1.0"})
        with urllib.request.urlopen(req, timeout=3.5, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == "Ok" and "distances" in data:
                dist_matrix = data["distances"]
                for i in range(len(subset)):
                    for j in range(len(subset)):
                        if i != j and dist_matrix[i][j] is not None:
                            c1 = subset[i]
                            c2 = subset[j]
                            km = round(dist_matrix[i][j] / 1000.0, 2)
                            key = f"{c1[0]:.4f},{c1[1]:.4f}_{c2[0]:.4f},{c2[1]:.4f}_{transport}"
                            _OSRM_CACHE[key] = km
                save_osrm_cache()
    except Exception:
        pass

def road_distance_km(lat1: float, lon1: float, lat2: float, lon2: float, transport: str = "car") -> float:
    return get_osrm_road_distance((lat1, lon1), (lat2, lon2), transport)

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


def load_dataset(csv_path: str) -> Tuple[List[Request], Tuple[float, float], str, List[str]]:
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

    # Сбор имён бригад из датасета
    brigade_names: List[str] = []
    for row in raw_rows:
        b_name = (row.get("Бригада") or "").strip()
        if b_name and b_name not in brigade_names:
            brigade_names.append(b_name)

    # Координаты офиса/склада
    depot_coords = DISTRICT_COORDS.get(district_hint, MOSCOW_CENTER)
    if "юных ленинцев" in depot_address.lower():
        depot_coords = (55.7001, 37.7690)
    elif "симферопольский" in depot_address.lower():
        depot_coords = (55.6885, 37.6181)
    elif "бирюлёвская" in depot_address.lower() or "бирюлевская" in depot_address.lower():
        depot_coords = (55.5976, 37.6690)

    return requests, depot_coords, depot_address, brigade_names


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
        d_to = road_distance_km(prev_lat, prev_lon, req.lat, req.lon, eng.transport)
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
            d_from = road_distance_km(req.lat, req.lon, next_lat, next_lon, eng.transport)
            t_from = travel_time_min(d_from, eng.transport)
            if service_end + t_from > next_arrival_limit:
                continue
            delta_d = (d_to + d_from) - road_distance_km(prev_lat, prev_lon, next_lat, next_lon, eng.transport)
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
    # Пакетная подгрузка реального дорожного графа OpenStreetMap (OSRM)
    if requests and engineers:
        sample_coords = [(engineers[0].home_lat, engineers[0].home_lon)] + [(r.lat, r.lon) for r in requests]
        batch_fetch_osrm_distances(sample_coords, engineers[0].transport)
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

    # Фактическая диагностика причины отказа
    skilled = [e for e in engineers if r.req_type in e.skills]
    if not skilled:
        reason = f"Нет инженеров с квалификацией «{r.req_type}» в пуле бригад района."
    elif all(e.equipment_capacity < r.equipment_demand for e in skilled):
        reason = (
            f"Требуемое оборудование ({r.equipment_demand} ед.) превышает вместимость "
            f"всех доступных транспортных средств."
        )
    elif r.window_end_min - r.window_start_min < r.work_duration_min:
        reason = (
            f"Временно́е окно клиента ({fmt_time(r.window_start_min)}–{fmt_time(r.window_end_min)}) "
            f"короче норматива выполнения работ ({r.work_duration_min} мин)."
        )
    elif r.window_start_min > max(e.shift_end_min for e in skilled) - r.work_duration_min:
        shift_end = fmt_time(max(e.shift_end_min for e in skilled))
        reason = (
            f"Начало окна клиента ({fmt_time(r.window_start_min)}) не позволяет завершить работы "
            f"до конца смены инженеров ({shift_end})."
        )
    else:
        reason = (
            "График активных бригад полностью заполнен более приоритетными заявками "
            "в данном временно́м интервале. Маршрутные окна несовместимы."
        )

    return (
        f"  Заявка №{r.id} [{tname}] ({r.district}, окно {fmt_time(r.window_start_min)}–{fmt_time(r.window_end_min)})\n"
        f"    • Причина: {reason}"
    )


# ======================================================================================
# 9. Запуск и сравнительный анализ по датасетам
# ======================================================================================


def optimize_routes_with_ortools(
    routes: List[Route],
    time_limit_sec: int = 2
) -> Tuple[List[Route], str]:
    """
    Полировка маршрутов через Google OR-Tools Constraint Solver (VRPTW).
    Применяет Guided Local Search.
    Гарантия допустимости: если за time_limit_sec OR-Tools не находит улучшенного
    решения или сталкивается со сбоем, гарантированно возвращается исходное
    допустимое решение (Feasible Solution) 4-проходного алгоритма.
    """
    if not HAS_ORTOOLS:
        return routes, "OR-Tools не установлен (использовано допустимое 4-Pass решение)"

    improved_routes = []
    any_improved = False

    for route in routes:
        eng = route.engineer
        visits = route.visits
        if len(visits) <= 2:
            improved_routes.append(route)
            continue

        n_nodes = len(visits) + 1
        manager = pywrapcp.RoutingIndexManager(n_nodes, 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        node_coords = [(eng.home_lat, eng.home_lon)] + [(v.request.lat, v.request.lon) for v in visits]
        node_durations = [0] + [v.request.work_duration_min for v in visits]
        node_windows = [(eng.shift_start_min, eng.shift_end_min)] + [(v.request.window_start_min, v.request.window_end_min) for v in visits]

        def dist_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            c1 = node_coords[from_node]
            c2 = node_coords[to_node]
            return int(road_distance_km(c1[0], c1[1], c2[0], c2[1], eng.transport) * 1000)

        transit_cb_idx = routing.RegisterTransitCallback(dist_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_cb_idx)

        def time_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            c1 = node_coords[from_node]
            c2 = node_coords[to_node]
            d_km = road_distance_km(c1[0], c1[1], c2[0], c2[1], eng.transport)
            drive_m = travel_time_min(d_km, eng.transport)
            service_m = node_durations[from_node]
            return drive_m + service_m

        time_cb_idx = routing.RegisterTransitCallback(time_callback)
        routing.AddDimension(time_cb_idx, 1440, 1440, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")

        for node_idx, (w_start, w_end) in enumerate(node_windows):
            if node_idx == 0:
                time_dim.CumulVar(manager.NodeToIndex(0)).SetRange(eng.shift_start_min, eng.shift_start_min)
            else:
                time_dim.CumulVar(manager.NodeToIndex(node_idx)).SetRange(w_start, w_end)

        # Начальное допустимое решение (Warm Start)
        initial_route = [[i for i in range(1, n_nodes)]]
        initial_assignment = routing.ReadAssignmentFromRoutes(initial_route, False)

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.time_limit.seconds = time_limit_sec
        search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH

        solution = None
        if initial_assignment:
            solution = routing.SolveFromAssignmentWithParameters(initial_assignment, search_parameters)
        if not solution:
            solution = routing.SolveWithParameters(search_parameters)

        if solution and routing.status() in (1, 2):
            new_sequence = []
            index = routing.Start(0)
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node != 0:
                    new_sequence.append(visits[node - 1])
                index = solution.Value(routing.NextVar(index))

            # Пересчет таймингов
            cur_lat, cur_lon = eng.home_lat, eng.home_lon
            cur_time = eng.shift_start_min
            new_visits = []
            is_valid = True

            for v in new_sequence:
                req = v.request
                d_km = road_distance_km(cur_lat, cur_lon, req.lat, req.lon, eng.transport)
                t_m = travel_time_min(d_km, eng.transport)
                arr = cur_time + t_m
                start = max(arr, req.window_start_min)
                end = start + req.work_duration_min
                if start > req.window_end_min or end > eng.shift_end_min:
                    is_valid = False
                    break
                new_visits.append(Visit(
                    request=req,
                    arrival_min=arr,
                    service_start_min=start,
                    service_end_min=end,
                    travel_min=t_m,
                    travel_km=d_km
                ))
                cur_lat, cur_lon = req.lat, req.lon
                cur_time = end

            new_route_km = sum(nv.travel_km for nv in new_visits)
            if is_valid and new_route_km < route.total_km - 0.01:
                improved_route = Route(engineer=eng, visits=new_visits)
                improved_routes.append(improved_route)
                any_improved = True
            else:
                # Оставляем гарантированное допустимое решение
                improved_routes.append(route)
        else:
            # Fallback на допустимое решение
            improved_routes.append(route)

    status_desc = "Улучшено Google OR-Tools" if any_improved else "Допустимое решение (OR-Tools оптимум совпал с базой или лимит времени)"
    return improved_routes, status_desc


def evaluate_dataset(csv_path: str):
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
    )

    # 1. Запуск 4-проходной оптимизации (Гарантированное допустимое решение)
    opt_routes, opt_dropped = run_4pass_optimization(requests, engineers)

    # 2. Оптимизация Google OR-Tools (с гарантированным fallback на допустимое решение)
    opt_routes, ortools_status_str = optimize_routes_with_ortools(opt_routes, time_limit_sec=2)

    # 3. Запуск Baseline (FIFO)
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
    print(f"Статус оптимизатора: {ortools_status_str}")
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
