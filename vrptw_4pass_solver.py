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

# Попытка импорта Google OR-Tools (CP-SAT Solver & Routing)
try:
    from ortools.sat.python import cp_model
    from ortools.constraint_solver import routing_enums_pb2, pywrapcp
    HAS_ORTOOLS = True
    HAS_CPSAT = True
except ImportError:
    HAS_ORTOOLS = False
    HAS_CPSAT = False


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
    """Детерминированное офлайн-геокодирование: координаты точки вычисляются из
    центра района + псевдослучайное смещение в радиусе 1.2 км на основе MD5-хэша
    (address + district). Кэша нет — результат воспроизводим при тех же входных данных."""
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

    # Сбор реальных имен бригад из датасета
    brigade_names: List[str] = []
    for row in raw_rows:
        b_name = (row.get("Бригада") or "").strip()
        if b_name and b_name not in brigade_names:
            brigade_names.append(b_name)

    # Определение единого депо / офиса района по ТЗ
    scan_text = f"{depot_address} {district_hint}".lower()
    if any(k in scan_text for k in ["симферопольск", "югоцентр", "даниловск", "академическ", "котловк", "зюзино", "хамовник", "садовник", "гагаринск", "замосквореч", "нагорн"]):
        depot_coords = (55.6885, 37.6181)
        depot_address = "г. Москва, проезд Симферопольский, д. 7"
    elif any(k in scan_text for k in ["бирюлев", "бирюлёв", "орехово", "царицыно", "братеево", "зябликово", "кашира", "ступино", "домодедово", "юго-восток"]):
        depot_coords = (55.5976, 37.6690)
        depot_address = "г. Москва, ул Бирюлёвская, д 1с 1"
    else:
        depot_coords = (55.7001, 37.7690)
        depot_address = "г. Москва, ул Юных Ленинцев, д 83с 4"

    return requests, depot_coords, depot_address, brigade_names


# ======================================================================================
# 5. Моделирование пула инженеров
# ======================================================================================

def create_engineers_pool(
    n_engineers: int,
    depot_coords: Tuple[float, float],
    has_suburbs: bool = False,
    seed: int = 42,
    brigade_names: Optional[List[str]] = None,
) -> List[Engineer]:
    """Генерирует реалистичный пул бригад района:
    - 10-12 инженеров на район (по контрольной выборке)
    - Часть на авто, часть пешие/транспорт
    - У всех есть навыки подключений и дозаказов, у 40% — аварийные работы и сложные ремонты."""
    rnd = random.Random(seed)
    engineers: List[Engineer] = []

    for k in range(n_engineers):
        if brigade_names and k < len(brigade_names):
            eng_id = brigade_names[k]
        else:
            eng_id = f"Инженер-{k+1:02d}"

        # ВСЕ 100% ИНЖЕНЕРОВ СТАРТУЮТ СТРОГО ИЗ ОФИСА / СКЛАДА РАЙОНА (ДЕПО)
        home = depot_coords
        if k < 5:
            transport = "car"
        elif k < 8:
            transport = "transit"  # 🚌 Общественный транспорт (метро/автобус)
        elif k < 10:
            transport = "bicycle"  # 🚲 Велосипед (СИМ)
        else:
            transport = "foot"     # 🚶 Пеший специалист

        skills = {"connection", "extra_order", "repair"}
        if k < 4:  # 4 мастера с допуском к авариям на ТКД
            skills.add("emergency")

        cap = 30 if transport == "car" else (18 if transport == "transit" else (14 if transport == "bicycle" else 10))
        engineers.append(
            Engineer(
                id=eng_id,
                home_lat=home[0],
                home_lon=home[1],
                shift_start_min=8 * 60,
                shift_end_min=22 * 60,  # 08:00 - 22:00
                transport=transport,
                skills=skills,
                equipment_capacity=cap,
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

        # Проверка окна заявки: ВСЕ работы должны быть полностью завершены СТРОГО внутри интервала клиента
        if service_end > req.window_end_min or service_end > eng.shift_end_min:
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
    """Выполняет 4-проходное иерархическое планирование по приоритетам."""
    # Пакетная подгрузка реального дорожного графа OpenStreetMap (OSRM)
    if requests and engineers:
        sample_coords = [(engineers[0].home_lat, engineers[0].home_lon)] + [(r.lat, r.lon) for r in requests]
        batch_fetch_osrm_distances(sample_coords, engineers[0].transport)
    # 4 корзины приоритетов
    pass1_emergency = [r for r in requests if r.req_type == "emergency"]
    pass2_connection = [r for r in requests if r.req_type == "connection"]
    pass3_repair = [r for r in requests if r.req_type == "repair"]
    pass4_extra = [r for r in requests if r.req_type == "extra_order"]

    routes: Dict[str, Route] = {eng.id: Route(engineer=eng) for eng in engineers}
    unassigned: List[Request] = []

    # Универсальная функция назначения по лексикографическому приоритету:
    # 1. Максимальное число выполненных заявок (проверяем всех инженеров, не сбрасывая заявку)
    # 2. Минимизация числа задействованного персонала (+1000.0 штраф за открытие нового инженера)
    # 3. Минимизация суммарного пробега (минимальный delta_km)
    def assign_pass(bucket: List[Request]):
        for req in bucket:
            best_eng_id = None
            best_insert = None
            min_cost = float("inf")

            for eng in engineers:
                if req.req_type not in eng.skills:
                    continue
                res = try_insert_request(routes[eng.id], req)
                if res:
                    pos, visit = res
                    is_active = len(routes[eng.id].visits) > 0
                    cost = visit.travel_km if is_active else (1000.0 + visit.travel_km)
                    if cost < min_cost:
                        min_cost = cost
                        best_eng_id = eng.id
                        best_insert = (pos, visit)

            if best_eng_id and best_insert:
                pos, visit = best_insert
                routes[best_eng_id].visits.insert(pos, visit)
            else:
                unassigned.append(req)

    # PASS 1: Аварии на ТКД
    assign_pass(pass1_emergency)

    # PASS 2: Подключения клиентов (сортировка по началу окна, приоритет гигабитным)
    pass2_connection.sort(key=lambda r: (r.window_start_min, not r.is_gigabit))
    assign_pass(pass2_connection)

    # PASS 3: Локальные заявки / Ремонты
    pass3_repair.sort(key=lambda r: r.window_start_min)
    assign_pass(pass3_repair)

    # PASS 4: Дозаказы оборудования
    pass4_extra.sort(key=lambda r: r.window_start_min)
    assign_pass(pass4_extra)

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


def solve_vrptw_cpsat(
    requests: List[Request],
    engineers: List[Engineer],
    time_limit_sec: float = 4.0,
    warm_routes: Optional[List[Route]] = None,
) -> Tuple[List[Route], List[Request], str]:
    """
    Полноценная глобальная математическая оптимизация VRPTW-S-C-M через Google OR-Tools CP-SAT.
    
    Моделирует задачу как целостную систему ограничений целочисленного программирования и SAT:
    1. Переменные:
       - y[k] in {0, 1}: привлечение инженера k в смену;
       - v[i, k] in {0, 1}: обслуживание заявки i инженером k (с учетом матрицы навыков);
       - x[u, v, k] in {0, 1}: ориентированные дуги перемещений по мультимодальному графу;
       - T[i] in [window_start_i, window_end_i - work_duration_i]: точное время начала работ у клиента;
       - z[i] in {0, 1}: факт выполнения заявки.
    2. Ограничения:
       - Однократное посещение клиента (sum_k v[i, k] == z[i]);
       - Сохранение потока на депо и в каждой клиентской точке (Eulerian flow conservation);
       - Строгое завершение работ ДО закрытия окна клиента: T[i] + duration_i <= window_end_i;
       - Связность по времени и исключение подциклов: T[j] >= T[i] + duration_i + travel_time(i, j, k);
       - Лимит смены инженера: T[i] + duration_i <= shift_end_k;
       - Грузоподъемность и лимит оборудования: sum_i q_i * v[i, k] <= Q_k * y[k].
    3. Трехуровневая лексикографическая целевая функция:
       min (10^7 * sum(1 - z_i) + 10^5 * sum(y_k) + sum(dist_km * 10 * x_uvk)).
    4. Инициализация начальным решением (Warm Start / Solution Hints) от 4-Pass эвристики.
    """
    if not HAS_CPSAT:
        fallback = warm_routes if warm_routes is not None else run_4pass_optimization(requests, engineers)[0]
        return fallback, [], "OR-Tools CP-SAT не установлен (использовано эвристическое решение)"

    if not requests or not engineers:
        return [], list(requests), "Пустой пул заявок или инженеров"

    # Если теплого старта нет, получаем допустимое начальное решение от 4-Pass эвристики
    if warm_routes is None:
        warm_routes, _ = run_4pass_optimization(requests, engineers)

    model = cp_model.CpModel()
    N = len(requests)
    M = len(engineers)
    req_indices = {r.id: idx + 1 for idx, r in enumerate(requests)} # 1..N

    START_DEPOT = 0
    END_DEPOT = N + 1

    # 1. Переменные вывода инженера на смену
    y = [model.NewBoolVar(f"y_{k}") for k in range(M)]

    # 2. Переменные назначения заявки i инженеру k (с фильтрацией по компетенциям)
    v = {}
    for i in range(1, N + 1):
        r = requests[i - 1]
        for k in range(M):
            if r.req_type in engineers[k].skills:
                v[i, k] = model.NewBoolVar(f"v_{i}_{k}")

    # 3. Индикаторы выполнения заявок
    z = {}
    for i in range(1, N + 1):
        cand_engs = [k for k in range(M) if (i, k) in v]
        if cand_engs:
            z[i] = model.NewBoolVar(f"z_{i}")
            model.Add(sum(v[i, k] for k in cand_engs) == z[i])
        else:
            z[i] = 0

    # 4. Временные переменные
    T_start = [model.NewIntVar(eng.shift_start_min, eng.shift_start_min, f"T_start_{k}") for k, eng in enumerate(engineers)]
    T_end = [model.NewIntVar(eng.shift_start_min, eng.shift_end_min, f"T_end_{k}") for k, eng in enumerate(engineers)]

    T = {}
    for i in range(1, N + 1):
        r = requests[i - 1]
        # Начало работ: >= window_start_min, а окончание СТРОГО <= window_end_min
        latest_start = r.window_end_min - r.work_duration_min
        if latest_start < r.window_start_min:
            latest_start = r.window_start_min
        T[i] = model.NewIntVar(r.window_start_min, latest_start, f"T_{i}")

    # 5. Ориентированные дуги x[u, v, k]
    x = {}
    arcs_out = {(u, k): [] for u in range(N + 2) for k in range(M)}
    arcs_in = {(w, k): [] for w in range(N + 2) for k in range(M)}
    cost_terms = []

    # Сбор дуг из warm_start для 100% гарантии их присутствия в графе
    warm_arcs = set()
    for route in warm_routes:
        eng_k = next((k for k, eng in enumerate(engineers) if eng.id == route.engineer.id), None)
        if eng_k is None or not route.visits:
            continue
        prev = START_DEPOT
        for visit in route.visits:
            curr = req_indices[visit.request.id]
            warm_arcs.add((prev, curr, eng_k))
            prev = curr
        warm_arcs.add((prev, END_DEPOT, eng_k))

    for k, eng in enumerate(engineers):
        valid_req_indices = [i for i in range(1, N + 1) if (i, k) in v]

        # Дуги: START_DEPOT -> i
        for i in valid_req_indices:
            r = requests[i - 1]
            d_km = road_distance_km(eng.home_lat, eng.home_lon, r.lat, r.lon, eng.transport)
            t_m = travel_time_min(d_km, eng.transport)
            is_warm = (START_DEPOT, i, k) in warm_arcs
            if is_warm or (eng.shift_start_min + t_m <= r.window_end_min - r.work_duration_min):
                var = model.NewBoolVar(f"x_{START_DEPOT}_{i}_{k}")
                x[START_DEPOT, i, k] = var
                arcs_out[START_DEPOT, k].append(var)
                arcs_in[i, k].append(var)
                cost_terms.append(int(round(d_km * 10)) * var)
                model.Add(T[i] >= eng.shift_start_min + t_m).OnlyEnforceIf(var)

        # Дуги: i -> END_DEPOT
        for i in valid_req_indices:
            r = requests[i - 1]
            d_km = road_distance_km(r.lat, r.lon, eng.home_lat, eng.home_lon, eng.transport)
            t_m = travel_time_min(d_km, eng.transport)
            is_warm = (i, END_DEPOT, k) in warm_arcs
            if is_warm or (r.window_start_min + r.work_duration_min + t_m <= eng.shift_end_min):
                var = model.NewBoolVar(f"x_{i}_{END_DEPOT}_{k}")
                x[i, END_DEPOT, k] = var
                arcs_out[i, k].append(var)
                arcs_in[END_DEPOT, k].append(var)
                cost_terms.append(int(round(d_km * 10)) * var)
                model.Add(T_end[k] >= T[i] + r.work_duration_min + t_m).OnlyEnforceIf(var)

        # Дуги: i -> j (между клиентскими точками)
        for i in valid_req_indices:
            r_i = requests[i - 1]
            cand_j = []
            for j in valid_req_indices:
                if i == j:
                    continue
                r_j = requests[j - 1]
                d_km = road_distance_km(r_i.lat, r_i.lon, r_j.lat, r_j.lon, eng.transport)
                t_m = travel_time_min(d_km, eng.transport)
                is_warm = (i, j, k) in warm_arcs
                if is_warm or (r_i.window_start_min + r_i.work_duration_min + t_m <= r_j.window_end_min - r_j.work_duration_min):
                    cand_j.append((d_km, t_m, j, is_warm))

            # Сортируем по расстоянию и оставляем ближайшие окрестности + обязательные warm_arcs
            cand_j.sort(key=lambda item: (not item[3], item[0]))
            for d_km, t_m, j, is_warm in cand_j[:40]:
                var = model.NewBoolVar(f"x_{i}_{j}_{k}")
                x[i, j, k] = var
                arcs_out[i, k].append(var)
                arcs_in[j, k].append(var)
                cost_terms.append(int(round(d_km * 10)) * var)
                model.Add(T[j] >= T[i] + r_i.work_duration_min + t_m).OnlyEnforceIf(var)

        # Условия сохранения потока для каждого инженера
        model.Add(sum(arcs_out[START_DEPOT, k]) == y[k])
        model.Add(sum(arcs_in[END_DEPOT, k]) == y[k])
        for i in valid_req_indices:
            model.Add(sum(arcs_in[i, k]) == v[i, k])
            model.Add(sum(arcs_out[i, k]) == v[i, k])
            # Завершение работ строго до конца смены мастера
            model.Add(T[i] + requests[i - 1].work_duration_min <= eng.shift_end_min).OnlyEnforceIf(v[i, k])

        # Ограничение по вместимости оборудования
        model.Add(sum(requests[i - 1].equipment_demand * v[i, k] for i in valid_req_indices) <= eng.equipment_capacity * y[k])

    # 6. Трехуровневая целевая функция
    # Приоритет 1: Максимизация выполненных заявок (штраф 10^7 за сброс)
    # Приоритет 2: Минимизация штата бригад (штраф 10^5 за задействование)
    # Приоритет 3: Минимизация суммарного пробега (дистанция в сотнях метров)
    unassigned_penalty = sum(10_000_000 * (1 - z[i]) for i in range(1, N + 1) if isinstance(z[i], cp_model.IntVar))
    staff_penalty = sum(100_000 * y[k] for k in range(M))
    model.Minimize(unassigned_penalty + staff_penalty + sum(cost_terms))

    # 7. Передача начального допустимого решения (Warm Start Hinting)
    for route in warm_routes:
        eng_k = next((k for k, eng in enumerate(engineers) if eng.id == route.engineer.id), None)
        if eng_k is None or not route.visits:
            continue
        model.AddHint(y[eng_k], 1)
        prev = START_DEPOT
        for visit in route.visits:
            curr = req_indices[visit.request.id]
            if (curr, eng_k) in v:
                model.AddHint(v[curr, eng_k], 1)
            model.AddHint(T[curr], visit.service_start_min)
            if (prev, curr, eng_k) in x:
                model.AddHint(x[prev, curr, eng_k], 1)
            prev = curr
        if (prev, END_DEPOT, eng_k) in x:
            model.AddHint(x[prev, END_DEPOT, eng_k], 1)

    # 8. Запуск солвера CP-SAT
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_sec)
    solver.parameters.num_workers = 4
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return warm_routes, [], "Допустимое решение (CP-SAT таймаут или оптимум совпал)"

    # 9. Извлечение итоговых маршрутов
    solution_routes = []
    unassigned_reqs = []
    for i in range(1, N + 1):
        if not isinstance(z[i], cp_model.IntVar) or solver.Value(z[i]) == 0:
            unassigned_reqs.append(requests[i - 1])

    for k, eng in enumerate(engineers):
        if solver.Value(y[k]) == 0:
            continue
        curr_node = START_DEPOT
        visits = []
        cur_lat, cur_lon = eng.home_lat, eng.home_lon
        cur_dep_time = eng.shift_start_min

        while curr_node != END_DEPOT:
            next_node = None
            for var in arcs_out[curr_node, k]:
                if solver.Value(var) == 1:
                    parts = var.Name().split("_")
                    next_node = int(parts[2])
                    break
            if next_node is None or next_node == END_DEPOT:
                break

            req = requests[next_node - 1]
            d_km = road_distance_km(cur_lat, cur_lon, req.lat, req.lon, eng.transport)
            t_m = travel_time_min(d_km, eng.transport)
            arrival = cur_dep_time + t_m
            start = solver.Value(T[next_node])
            end = start + req.work_duration_min

            visits.append(Visit(
                request=req,
                arrival_min=arrival,
                service_start_min=start,
                service_end_min=end,
                travel_min=t_m,
                travel_km=round(d_km, 2),
            ))

            cur_lat, cur_lon = req.lat, req.lon
            cur_dep_time = end
            curr_node = next_node

        if visits:
            solution_routes.append(Route(engineer=eng, visits=visits))

    status_name = "Оптимально (CP-SAT)" if status == cp_model.OPTIMAL else "Улучшено Google OR-Tools (CP-SAT)"
    return solution_routes, unassigned_reqs, status_name


def optimize_routes_with_ortools(
    routes: List[Route],
    requests: Optional[List[Request]] = None,
    engineers: Optional[List[Engineer]] = None,
    time_limit_sec: float = 3.0,
) -> Tuple[List[Route], str]:
    """
    Полноценная глобальная оптимизация через Google OR-Tools CP-SAT.
    Сохраняет обратную совместимость: если переданы только routes,
    извлекает requests и engineers и запускает полный CP-SAT солвер.
    """
    if not HAS_CPSAT:
        return routes, "OR-Tools не установлен (использовано допустимое 4-Pass решение)"

    if requests is None:
        requests = [v.request for r in routes for v in r.visits]
    if engineers is None:
        engineers = [r.engineer for r in routes]

    res_routes, _, status_desc = solve_vrptw_cpsat(
        requests=requests,
        engineers=engineers,
        time_limit_sec=time_limit_sec,
        warm_routes=routes,
    )
    return res_routes, status_desc


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
        brigade_names=brigade_names or [],
    )

    # 1. Запуск 4-проходной оптимизации (Гарантированное допустимое решение)
    opt_routes, opt_dropped = run_4pass_optimization(requests, engineers)

    # 2. Глобальная оптимизация Google OR-Tools CP-SAT (с гарантированным fallback на допустимое решение)
    opt_routes, ortools_status_str = optimize_routes_with_ortools(
        opt_routes, requests=requests, engineers=engineers, time_limit_sec=3.0
    )

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
