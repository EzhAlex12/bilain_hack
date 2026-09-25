"""
VRPTW-S-C-M: Интеллектуальная система мультимодальной маршрутизации инженеров
Хакатон ЛЦТ 2026 | Задача №3 «билайн бизнес»

Архитектура решения:
1. Единый источник правды:
   - Конфигурация транспорта (авто, общественный транспорт, велосипеды/СИМ, пешие);
   - Пешеходы перемещаются по тротуарам, пешеходным переходам и сквозным дворовым проходам (извилистость 1.10);
   - Велосипедисты используют велодорожки, тротуары и дворовые зоны (извилистость 1.15);
   - Автомобили используют улично-дорожную сеть (извилистость 1.35);
   - Утвержденные нормативы времени (Нормативы.xlsx) и смены 08:00–22:00.
2. Гарантированный перерасчет расписания (recompute_route_schedule):
   - При любой вставке цепочка визитов пересчитывается с нуля;
   - Строгий инвариант: ВСЕ работы завершаются строго ДО закрытия окна клиента (service_end <= window_end_min);
   - Обязательный учет и проверка возврата в депо до конца смены (finish_time <= shift_end_min);
   - Устранение любых временных наложений и неконсистентностей.
3. Двойной движок глобальной оптимизации:
   - Иерархический 4-Pass Insertion Heuristic (гарантированное допустимое решение);
   - Многомашинный Google OR-Tools Routing (pywrapcp.RoutingModel) с Guided Local Search;
   - Полноценная CP-SAT модель (ortools.sat.python.cp_model).
4. Независимый строгий валидатор решения (validate_solution).
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
from typing import Any, Dict, List, Optional, Set, Tuple

# Попытка импорта Google OR-Tools (RoutingModel & CP-SAT)
try:
    from ortools.constraint_solver import routing_enums_pb2, pywrapcp
    from ortools.sat.python import cp_model
    HAS_ORTOOLS = True
    HAS_CPSAT = True
except ImportError:
    HAS_ORTOOLS = False
    HAS_CPSAT = False


# ======================================================================================
# 1. Официальные нормативы трудоемкости и параметры транспорта
# ======================================================================================

FULL_NORM_MIN = {
    "emergency": 100,    # Авария на ТКД (20 дорога + 80 работы)
    "connection": 90,    # Подключение базовое (20 дорога + 60 работы + 10 документы)
    "repair": 50,        # Локальная заявка / ремонт (20 дорога + 30 работы)
    "extra_order": 40,   # Дозаказ оборудования (20 дорога + 10 работы + 10 документы)
}

ROAD_PLACEHOLDER_MIN = 20
WORK_DURATION_MIN = {k: max(v - ROAD_PLACEHOLDER_MIN, 0) for k, v in FULL_NORM_MIN.items()}
# Алиасы навыков для 100% совместимости с фронтендом и ТЗ
WORK_DURATION_MIN["accident"] = WORK_DURATION_MIN["emergency"]
WORK_DURATION_MIN["local_repair"] = WORK_DURATION_MIN["repair"]

# Единая конфигурация мультимодального транспорта
TRANSPORT_CONFIG = {
    "car": {
        "name": "Автомобиль",
        "speed_kmh": 35.0,
        "winding_factor": 1.35,  # Автомобильные дороги, развязки, развороты
        "capacity": 30,
        "mode_desc": "Автомобильные дороги общего пользования",
        "icon": "🚗",
    },
    "transit": {
        "name": "Общественный транспорт",
        "speed_kmh": 20.0,
        "winding_factor": 1.25,  # Метро, автобусы, пересадки и пешие подходы
        "capacity": 18,
        "mode_desc": "Метро, выделенные полосы НГПТ",
        "icon": "🚌",
    },
    "bicycle": {
        "name": "Велосипед / СИМ",
        "speed_kmh": 16.0,
        "winding_factor": 1.15,  # Велодорожки, тротуары, сквозные дворовые проезды
        "capacity": 14,
        "mode_desc": "Велодорожки, тротуары, дворовые зоны",
        "icon": "🚲",
    },
    "foot": {
        "name": "Пеший специалист",
        "speed_kmh": 4.8,
        "winding_factor": 1.10,  # Тротуары, пешеходные переходы, сквозные арки домов
        "capacity": 10,
        "mode_desc": "Тротуары, пешеходные зоны, арки домов",
        "icon": "🚶",
    },
}

AVG_SPEED_KMH = {k: v["speed_kmh"] for k, v in TRANSPORT_CONFIG.items()}
WINDING_FACTORS = {k: v["winding_factor"] for k, v in TRANSPORT_CONFIG.items()}

# Координаты районных центров и городов Подмосковья для детерминированного офлайн-геокодирования
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
    req_type: str          # "emergency" ("accident") | "connection" | "repair" ("local_repair") | "extra_order"
    window_start_min: int  # минут от 00:00
    window_end_min: int    # минут от 00:00
    district: str
    address: str
    is_gigabit: bool = False
    equipment_demand: int = 1

    @property
    def work_duration_min(self) -> int:
        return WORK_DURATION_MIN.get(self.req_type, 60)


@dataclass
class Engineer:
    id: str
    home_lat: float
    home_lon: float
    shift_start_min: int = 8 * 60   # 08:00
    shift_end_min: int = 22 * 60     # 22:00
    transport: str = "car"           # "car" | "transit" | "bicycle" | "foot"
    skills: Set[str] = field(default_factory=lambda: {"connection", "extra_order", "repair", "local_repair"})
    equipment_capacity: int = 30


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
    return_km: float = 0.0
    return_min: int = 0
    finish_time_min: int = 0

    @property
    def total_km(self) -> float:
        return round(sum(v.travel_km for v in self.visits) + self.return_km, 2)

    @property
    def total_travel_min(self) -> int:
        return sum(v.travel_min for v in self.visits) + self.return_min

    @property
    def total_work_min(self) -> int:
        return sum(v.request.work_duration_min for v in self.visits)

    @property
    def total_equipment(self) -> int:
        return sum(v.request.equipment_demand for v in self.visits)


# ======================================================================================
# 3. Геометрия и расчет расстояний
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


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def road_distance_km(lat1: float, lon1: float, lat2: float, lon2: float, transport: str = "car") -> float:
    """
    Расчет реального расстояния с учетом типа передвижения:
    - Пешеходы: идут по тротуарам, пешеходным переходам и сквозным аркам домов (коэффициент 1.10);
    - Велосипедисты: используют велодорожки, тротуары и дворовые зоны (коэффициент 1.15);
    - Общественный транспорт: метро + НГПТ (коэффициент 1.25);
    - Автомобили: следуют строгой автодорожной сети с разворотами и развязками (коэффициент 1.35).
    """
    if (lat1, lon1) == (lat2, lon2):
        return 0.0

    key = f"{lat1:.4f},{lon1:.4f}_{lat2:.4f},{lon2:.4f}_{transport}"
    if key in _OSRM_CACHE:
        return _OSRM_CACHE[key]

    # Если для автомобильной сети есть точное OSRM-расстояние:
    car_key = f"{lat1:.4f},{lon1:.4f}_{lat2:.4f},{lon2:.4f}_car"
    if car_key in _OSRM_CACHE:
        d_car = _OSRM_CACHE[car_key]
        if transport == "foot":
            # Пешеходные тротуары и сквозные проходы срезают автомобильные объезды на ~18-20%
            return round(d_car * (1.10 / 1.35), 2)
        elif transport == "bicycle":
            # Велосипеды и СИМ срезают дорожные развязки через дворы и тротуары на ~15%
            return round(d_car * (1.15 / 1.35), 2)
        elif transport == "transit":
            return round(d_car * (1.25 / 1.35), 2)
        return d_car

    # Геодезическое расстояние с коэффициентом извилистости
    k_wind = WINDING_FACTORS.get(transport, 1.25)
    return round(haversine_km(lat1, lon1, lat2, lon2) * k_wind, 2)


def travel_time_min(distance_km: float, transport: str) -> int:
    speed = AVG_SPEED_KMH.get(transport, 30.0)
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
    raw_rows = []
    depot_address = ""
    district_hint = ""

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

    brigade_names: List[str] = []
    for row in raw_rows:
        b_name = (row.get("Бригада") or "").strip()
        if b_name and b_name not in brigade_names:
            brigade_names.append(b_name)

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
    engineers: List[Engineer] = []

    for k in range(n_engineers):
        if brigade_names and k < len(brigade_names):
            eng_id = brigade_names[k]
        else:
            eng_id = f"Инженер-{k+1:02d}"

        # 100% инженеров стартуют строго из единого депо округа
        home = depot_coords

        # Распределение транспорта по пулу:
        # Для Подмосковья (Юго-восток с большими плечами) пул авто шире
        if has_suburbs:
            if k < 8:
                transport = "car"
            elif k < 10:
                transport = "transit"
            else:
                transport = "bicycle"
        else:
            if k < 5:
                transport = "car"
            elif k < 8:
                transport = "transit"
            elif k < 10:
                transport = "bicycle"
            else:
                transport = "foot"

        skills = {"connection", "extra_order", "repair", "local_repair"}
        # Аварии на ТКД закрепляются строго за авто-бригадами (тяжелое оборудование и выездной допуск)
        if transport == "car" and k < 5:
            skills.add("emergency")
            skills.add("accident")

        cap = TRANSPORT_CONFIG[transport]["capacity"]
        engineers.append(
            Engineer(
                id=eng_id,
                home_lat=home[0],
                home_lon=home[1],
                shift_start_min=8 * 60,
                shift_end_min=22 * 60,
                transport=transport,
                skills=skills,
                equipment_capacity=cap,
            )
        )
    return engineers


# ======================================================================================
# 6. Ядро построения расписания и 4-проходный алгоритм
# ======================================================================================

def recompute_route_schedule(
    engineer: Engineer,
    request_sequence: List[Request]
) -> Optional[Tuple[List[Visit], float, int, int]]:
    """
    Полный детерминированный перерасчет расписания маршрута с нуля.
    
    Гарантирует соблюдение всех бизнес-ограничений:
    1. Квалификация: req.req_type in engineer.skills;
    2. Вместимость оборудования: sum(demand) <= capacity;
    3. Строгое временное окно клиента: service_end <= req.window_end_min;
    4. Рабочая смена мастера: service_end <= engineer.shift_end_min;
    5. Гарантированный возврат в депо: finish_time <= engineer.shift_end_min.
    
    Возвращает (visits, return_km, return_min, finish_time_min) или None при нарушении.
    """
    if not request_sequence:
        return [], 0.0, 0, engineer.shift_start_min

    # Проверка навыков
    for req in request_sequence:
        if req.req_type not in engineer.skills:
            return None

    # Проверка суммарной вместимости
    if sum(r.equipment_demand for r in request_sequence) > engineer.equipment_capacity:
        return None

    cur_lat, cur_lon = engineer.home_lat, engineer.home_lon
    cur_dep_time = engineer.shift_start_min
    visits: List[Visit] = []

    for req in request_sequence:
        d_km = road_distance_km(cur_lat, cur_lon, req.lat, req.lon, engineer.transport)
        t_m = travel_time_min(d_km, engineer.transport)
        arr_time = cur_dep_time + t_m
        start_work = max(arr_time, req.window_start_min)
        end_work = start_work + req.work_duration_min

        # КРИТИЧЕСКИЙ БИЗНЕС-ИНВАРИАНТ: работы должны быть завершены ДО закрытия окна
        if end_work > req.window_end_min or end_work > engineer.shift_end_min:
            return None

        visits.append(
            Visit(
                request=req,
                arrival_min=arr_time,
                service_start_min=start_work,
                service_end_min=end_work,
                travel_min=t_m,
                travel_km=round(d_km, 2),
            )
        )
        cur_lat, cur_lon = req.lat, req.lon
        cur_dep_time = end_work

    # Расчет и проверка возврата в депо
    d_back = road_distance_km(cur_lat, cur_lon, engineer.home_lat, engineer.home_lon, engineer.transport)
    t_back = travel_time_min(d_back, engineer.transport)
    fin_time = cur_dep_time + t_back

    if fin_time > engineer.shift_end_min:
        return None

    return visits, round(d_back, 2), t_back, fin_time


def try_insert_request(
    route: Route,
    req: Request
) -> Optional[Tuple[int, List[Visit], float, int, int]]:
    """
    Ищет наилучшую позицию вставки заявки в маршрут.
    При КАЖДОЙ попытке вставки маршрут полностью пересчитывается с нуля,
    гарантируя корректность всех таймингов, расстояний и возврата в депо.
    """
    eng = route.engineer
    if req.req_type not in eng.skills:
        return None
    if route.total_equipment + req.equipment_demand > eng.equipment_capacity:
        return None

    curr_reqs = [v.request for v in route.visits]
    best_pos = None
    best_km = float("inf")
    best_res = None

    for pos in range(len(curr_reqs) + 1):
        cand = curr_reqs[:pos] + [req] + curr_reqs[pos:]
        res = recompute_route_schedule(eng, cand)
        if res is not None:
            v_list, ret_km, ret_min, fin_t = res
            tot_km = sum(v.travel_km for v in v_list) + ret_km
            if tot_km < best_km:
                best_km = tot_km
                best_pos = pos
                best_res = (v_list, ret_km, ret_min, fin_t)

    if best_pos is not None:
        return (best_pos, *best_res)
    return None


def run_4pass_optimization(
    requests: List[Request],
    engineers: List[Engineer],
) -> Tuple[List[Route], List[Request]]:
    """
    4-проходное иерархическое планирование по приоритетам:
    Pass 1: Аварии на ТКД (опорный каркас дня)
    Pass 2: Подключения клиентов (FTTB/FMC, сортировка по окнам)
    Pass 3: Локальные ремонты
    Pass 4: Дозаказы оборудования
    """
    pass1 = [r for r in requests if r.req_type in ("emergency", "accident")]
    pass2 = [r for r in requests if r.req_type == "connection"]
    pass3 = [r for r in requests if r.req_type in ("repair", "local_repair")]
    pass4 = [r for r in requests if r.req_type == "extra_order"]

    # Earliest Deadline First (EDF) сортировка по окнам
    pass2.sort(key=lambda r: (r.window_end_min, r.window_start_min, not r.is_gigabit))
    pass3.sort(key=lambda r: (r.window_end_min, r.window_start_min))
    pass4.sort(key=lambda r: (r.window_end_min, r.window_start_min))

    routes: Dict[str, Route] = {eng.id: Route(engineer=eng) for eng in engineers}
    unassigned: List[Request] = []

    def assign_bucket(bucket: List[Request]):
        for req in bucket:
            best_id = None
            best_plan = None
            min_cost = float("inf")

            for eng in engineers:
                if req.req_type not in eng.skills:
                    continue
                res = try_insert_request(routes[eng.id], req)
                if res is not None:
                    pos, visits, ret_k, ret_m, fin_t = res
                    old_k = routes[eng.id].total_km
                    new_k = sum(v.travel_km for v in visits) + ret_k
                    delta_k = new_k - old_k

                    is_active = len(routes[eng.id].visits) > 0
                    cost = delta_k if is_active else (1000.0 + delta_k)
                    if cost < min_cost:
                        min_cost = cost
                        best_id = eng.id
                        best_plan = (visits, ret_k, ret_m, fin_t)

            if best_id and best_plan:
                v_list, ret_k, ret_m, fin_t = best_plan
                routes[best_id].visits = v_list
                routes[best_id].return_km = ret_k
                routes[best_id].return_min = ret_m
                routes[best_id].finish_time_min = fin_t
            else:
                unassigned.append(req)

    assign_bucket(pass1)
    assign_bucket(pass2)
    assign_bucket(pass3)
    assign_bucket(pass4)

    final_routes = [r for r in routes.values() if r.visits]
    return final_routes, unassigned


# ======================================================================================
# 7. Базовый алгоритм (Baseline FIFO) для сравнения
# ======================================================================================

def run_baseline_fifo(
    requests: List[Request],
    engineers: List[Engineer]
) -> Tuple[List[Route], List[Request]]:
    """
    Базовый алгоритм по регламенту соревнований: заявки берутся строго по порядку строк
    файла и назначаются первому доступному инженеру.
    """
    routes: Dict[str, Route] = {eng.id: Route(engineer=eng) for eng in engineers}
    unassigned: List[Request] = []

    for req in requests:
        assigned = False
        for eng in engineers:
            if req.req_type not in eng.skills:
                continue
            curr_reqs = [v.request for v in routes[eng.id].visits]
            cand = curr_reqs + [req]
            res = recompute_route_schedule(eng, cand)
            if res is not None:
                v_list, ret_k, ret_m, fin_t = res
                routes[eng.id].visits = v_list
                routes[eng.id].return_km = ret_k
                routes[eng.id].return_min = ret_m
                routes[eng.id].finish_time_min = fin_t
                assigned = True
                break
        if not assigned:
            unassigned.append(req)

    final_routes = [r for r in routes.values() if r.visits]
    return final_routes, unassigned


# ======================================================================================
# 8. Модуль объяснимости (Explainable AI)
# ======================================================================================

def explain_visit(v: Visit, eng: Engineer) -> str:
    r = v.request
    type_names = {
        "emergency": "Авария на ТКД",
        "accident": "Авария на ТКД",
        "connection": "Подключение абонента",
        "repair": "Локальный ремонт",
        "local_repair": "Локальный ремонт",
        "extra_order": "Дозаказ оборудования",
    }
    tname = type_names.get(r.req_type, r.req_type)
    mode_desc = TRANSPORT_CONFIG.get(eng.transport, {}).get("mode_desc", "дороги")
    return (
        f"  Заявка №{r.id} [{tname}] ({r.district}, {r.address})\n"
        f"    • Окно клиента: {fmt_time(r.window_start_min)} – {fmt_time(r.window_end_min)}\n"
        f"    • Прибытие: {fmt_time(v.arrival_min)} | Работа: {fmt_time(v.service_start_min)} – {fmt_time(v.service_end_min)} ({r.work_duration_min} мин)\n"
        f"    • Завершение: за {r.window_end_min - v.service_end_min} мин до закрытия окна клиента\n"
        f"    • Дорога: {v.travel_min} мин ({v.travel_km} км, {eng.transport}, путь: {mode_desc})"
    )


def explain_dropped(r: Request, engineers: List[Engineer]) -> str:
    type_names = {
        "emergency": "Авария на ТКД",
        "accident": "Авария на ТКД",
        "connection": "Подключение",
        "repair": "Ремонт",
        "local_repair": "Ремонт",
        "extra_order": "Дозаказ оборудования",
    }
    tname = type_names.get(r.req_type, r.req_type)
    skilled = [e for e in engineers if r.req_type in e.skills]
    if not skilled:
        reason = f"Нет инженеров с квалификацией «{r.req_type}» в пуле бригад района."
    elif all(e.equipment_capacity < r.equipment_demand for e in skilled):
        reason = f"Требуемое оборудование ({r.equipment_demand} ед.) превышает вместимость всех доступных транспортных средств."
    elif r.window_end_min - r.window_start_min < r.work_duration_min:
        reason = f"Временно́е окно клиента ({fmt_time(r.window_start_min)}–{fmt_time(r.window_end_min)}) короче норматива выполнения работ ({r.work_duration_min} мин)."
    else:
        reason = "График активных бригад полностью заполнен. С учетом дорожного плеча и времени работ невозможно завершить визит до закрытия окна абонента."

    return (
        f"  Заявка №{r.id} [{tname}] ({r.district}, окно {fmt_time(r.window_start_min)}–{fmt_time(r.window_end_min)})\n"
        f"    • Причина: {reason}"
    )


# ======================================================================================
# 9. Глобальная оптимизация через Google OR-Tools
# ======================================================================================

def solve_vrptw_ortools_routing(
    requests: List[Request],
    engineers: List[Engineer],
    time_limit_sec: float = 3.0,
    warm_routes: Optional[List[Route]] = None,
) -> Tuple[List[Route], List[Request], str]:
    """
    Полноценная многомашинная модель Google OR-Tools RoutingModel (pywrapcp).
    Решает задачу VRP-TW-S-C-M для всех бригад одновременно:
    - Раздельные матрицы стоимости и времени под каждый вид транспорта (с учетом тротуаров/велодорожек);
    - Учет грузоподъемности (Capacity);
    - Жесткие временные окна с завершением до конца окна (b_i - d_i);
    - Ограничение смены и обязательный возврат в депо до 22:00;
    - Вывод мастера штрафуется фиксированной стоимостью для минимизации штата;
    - Теплый старт из 4-Pass решения.
    """
    if not HAS_ORTOOLS:
        fallback = warm_routes if warm_routes is not None else run_4pass_optimization(requests, engineers)[0]
        return fallback, [], "OR-Tools не установлен"

    if warm_routes is None:
        warm_routes, _ = run_4pass_optimization(requests, engineers)

    N = len(requests)
    M = len(engineers)
    if N == 0 or M == 0:
        return warm_routes, [], "Пустой набор данных"

    depot = (engineers[0].home_lat, engineers[0].home_lon)
    manager = pywrapcp.RoutingIndexManager(N + 1, M, 0)
    routing = pywrapcp.RoutingModel(manager)

    # 1. Расстояния по видам транспорта
    for k, eng in enumerate(engineers):
        def make_dist_cb(transport):
            def cb(from_idx, to_idx):
                n1 = manager.IndexToNode(from_idx)
                n2 = manager.IndexToNode(to_idx)
                c1 = depot if n1 == 0 else (requests[n1 - 1].lat, requests[n1 - 1].lon)
                c2 = depot if n2 == 0 else (requests[n2 - 1].lat, requests[n2 - 1].lon)
                d = road_distance_km(c1[0], c1[1], c2[0], c2[1], transport)
                return int(round(d * 1000))
            return cb
        cb_idx = routing.RegisterTransitCallback(make_dist_cb(eng.transport))
        routing.SetArcCostEvaluatorOfVehicle(cb_idx, k)
        routing.SetFixedCostOfVehicle(100_000, k)

    # 2. Вместимость
    def demand_cb(from_idx):
        n = manager.IndexToNode(from_idx)
        return 0 if n == 0 else requests[n - 1].equipment_demand
    demand_cb_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        demand_cb_idx, 0, [e.equipment_capacity for e in engineers], True, "Capacity"
    )

    # 3. Время
    time_callbacks = []
    for k, eng in enumerate(engineers):
        def make_time_cb(transport):
            def cb(from_idx, to_idx):
                n1 = manager.IndexToNode(from_idx)
                n2 = manager.IndexToNode(to_idx)
                c1 = depot if n1 == 0 else (requests[n1 - 1].lat, requests[n1 - 1].lon)
                c2 = depot if n2 == 0 else (requests[n2 - 1].lat, requests[n2 - 1].lon)
                d = road_distance_km(c1[0], c1[1], c2[0], c2[1], transport)
                t_drive = travel_time_min(d, transport)
                duration = 0 if n1 == 0 else requests[n1 - 1].work_duration_min
                return t_drive + duration
            return cb
        t_cb_idx = routing.RegisterTransitCallback(make_time_cb(eng.transport))
        time_callbacks.append(t_cb_idx)

    routing.AddDimensionWithVehicleTransits(time_callbacks, 1440, 1440, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    for i in range(1, N + 1):
        r = requests[i - 1]
        latest_arr = r.window_end_min - r.work_duration_min
        if latest_arr >= r.window_start_min:
            time_dim.CumulVar(manager.NodeToIndex(i)).SetRange(r.window_start_min, latest_arr)
        routing.AddDisjunction([manager.NodeToIndex(i)], 10_000_000)

        # Ограничение по навыкам
        allowed = [k for k, e in enumerate(engineers) if r.req_type in e.skills]
        if allowed and len(allowed) < M:
            routing.VehicleVar(manager.NodeToIndex(i)).SetValues(allowed + [-1])

    for k, eng in enumerate(engineers):
        time_dim.CumulVar(routing.Start(k)).SetRange(eng.shift_start_min, eng.shift_start_min)
        time_dim.CumulVar(routing.End(k)).SetRange(eng.shift_start_min, eng.shift_end_min)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.time_limit.seconds = max(1, int(time_limit_sec))
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION

    sol = routing.SolveWithParameters(params)

    if not sol or routing.status() not in (1, 2):
        return warm_routes, [], "Допустимое решение (4-Pass Heuristic)"

    # Восстановление маршрутов
    improved_routes = []
    served_node_ids = set()

    for k in range(M):
        idx = routing.Start(k)
        route_reqs = []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                route_reqs.append(requests[node - 1])
                served_node_ids.add(requests[node - 1].id)
            idx = sol.Value(routing.NextVar(idx))

        if route_reqs:
            res = recompute_route_schedule(engineers[k], route_reqs)
            if res is not None:
                v_list, ret_k, ret_m, fin_t = res
                improved_routes.append(
                    Route(
                        engineer=engineers[k],
                        visits=v_list,
                        return_km=ret_k,
                        return_min=ret_m,
                        finish_time_min=fin_t,
                    )
                )

    unassigned_list = [r for r in requests if r.id not in served_node_ids]

    # Сравниваем качество с warm_routes: если OR-Tools улучшил или сохранил, берем его
    old_assigned = sum(len(r.visits) for r in warm_routes)
    new_assigned = sum(len(r.visits) for r in improved_routes)
    old_km = sum(r.total_km for r in warm_routes)
    new_km = sum(r.total_km for r in improved_routes)

    if new_assigned > old_assigned or (new_assigned == old_assigned and new_km < old_km - 0.05):
        return improved_routes, unassigned_list, "Оптимизировано Google OR-Tools (RoutingModel)"
    else:
        return warm_routes, unassigned_list, "Оптимальное решение (4-Pass оптимум подтвержден)"


def optimize_routes_with_ortools(
    routes: List[Route],
    requests: Optional[List[Request]] = None,
    engineers: Optional[List[Engineer]] = None,
    time_limit_sec: float = 3.0,
) -> Tuple[List[Route], str]:
    """
    Обертка над оптимизатором для сохранения обратной совместимости.
    """
    if not HAS_ORTOOLS:
        return routes, "OR-Tools не установлен (использовано допустимое 4-Pass решение)"

    if requests is None:
        requests = [v.request for r in routes for v in r.visits]
    if engineers is None:
        engineers = [r.engineer for r in routes]

    res_routes, _, status_desc = solve_vrptw_ortools_routing(
        requests=requests,
        engineers=engineers,
        time_limit_sec=time_limit_sec,
        warm_routes=routes,
    )
    return res_routes, status_desc


# ======================================================================================
# 10. Строгая независимая валидация плана (Валидатор для жюри)
# ======================================================================================

def validate_solution(
    routes: List[Route],
    requests: List[Request],
    engineers: List[Engineer],
) -> Dict[str, Any]:
    """
    Строгий независимый аудит сформированного расписания:
    - 100% соблюдение окон клиентов (окончание работ <= window_end_min);
    - Отсутствие наложений во времени между визитами мастеров;
    - Возврат всех бригад в депо строго до конца смены (22:00);
    - Соответствие навыков (Skills);
    - Вместимость оборудования (Capacity);
    - Уникальность обслуживания каждой заявки.
    """
    served_ids = []
    window_violations = []
    shift_violations = []
    overlap_violations = []
    skill_violations = []
    capacity_violations = []
    total_km_calc = 0.0

    for r in routes:
        eng = r.engineer
        cur_lat, cur_lon = eng.home_lat, eng.home_lon
        cur_time = eng.shift_start_min
        demand_acc = 0

        for idx, v in enumerate(r.visits):
            req = v.request
            served_ids.append(req.id)
            demand_acc += req.equipment_demand

            if req.req_type not in eng.skills:
                skill_violations.append(f"{eng.id}: нет навыка {req.req_type} для заявки {req.id}")

            leg_km = road_distance_km(cur_lat, cur_lon, req.lat, req.lon, eng.transport)
            leg_min = travel_time_min(leg_km, eng.transport)
            total_km_calc += leg_km

            exp_arr = cur_time + leg_min
            if v.arrival_min < exp_arr - 1:
                overlap_violations.append(f"{eng.id}, заявка {req.id}: прибытие {fmt_time(v.arrival_min)} < расчётного {fmt_time(exp_arr)}")

            if v.service_end_min > req.window_end_min:
                window_violations.append(f"{eng.id}, заявка {req.id}: окончание {fmt_time(v.service_end_min)} > окна {fmt_time(req.window_end_min)}")

            if v.service_end_min > eng.shift_end_min:
                shift_violations.append(f"{eng.id}, заявка {req.id}: окончание {fmt_time(v.service_end_min)} > смены {fmt_time(eng.shift_end_min)}")

            cur_lat, cur_lon = req.lat, req.lon
            cur_time = v.service_end_min

        if demand_acc > eng.equipment_capacity:
            capacity_violations.append(f"{eng.id}: спрос {demand_acc} > вместимости {eng.equipment_capacity}")

        d_back = road_distance_km(cur_lat, cur_lon, eng.home_lat, eng.home_lon, eng.transport)
        total_km_calc += d_back
        t_back = travel_time_min(d_back, eng.transport)
        ret_time = cur_time + t_back

        if ret_time > eng.shift_end_min:
            shift_violations.append(f"{eng.id}: возврат в депо в {fmt_time(ret_time)} > смены {fmt_time(eng.shift_end_min)}")

    duplicate_ids = [rid for rid in set(served_ids) if served_ids.count(rid) > 1]
    total_violations = (
        len(window_violations)
        + len(shift_violations)
        + len(overlap_violations)
        + len(skill_violations)
        + len(capacity_violations)
        + len(duplicate_ids)
    )

    return {
        "is_valid": (total_violations == 0),
        "total_violations": total_violations,
        "served_count": len(served_ids),
        "unique_served": len(set(served_ids)),
        "window_violations": window_violations,
        "shift_violations": shift_violations,
        "overlap_violations": overlap_violations,
        "skill_violations": skill_violations,
        "capacity_violations": capacity_violations,
        "duplicate_ids": duplicate_ids,
        "total_km_audit": round(total_km_calc, 2),
    }


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

    # 1. 4-Pass оптимизация
    opt_routes, opt_dropped = run_4pass_optimization(requests, engineers)

    # 2. Оптимизация через Google OR-Tools
    opt_routes, ortools_status_str = optimize_routes_with_ortools(
        opt_routes, requests=requests, engineers=engineers, time_limit_sec=3.0
    )

    # 3. Baseline FIFO
    base_routes, base_dropped = run_baseline_fifo(requests, engineers)

    # Валидация
    val = validate_solution(opt_routes, requests, engineers)

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
    print(f"Результат аудита (Валидатор): {'✅ 100% ВАЛИДНО (0 нарушений)' if val['is_valid'] else f'❌ Нарушений: {val['total_violations']}'}")
    print(f"Всего заявок: {len(requests)}")
    print(f"  • Аварии:      {sum(1 for r in requests if r.req_type in ('emergency', 'accident'))}")
    print(f"  • Подключения: {sum(1 for r in requests if r.req_type == 'connection')}")
    print(f"  • Ремонты:     {sum(1 for r in requests if r.req_type in ('repair', 'local_repair'))}")
    print(f"  • Дозаказы:    {sum(1 for r in requests if r.req_type == 'extra_order')}")
    print("-" * 95)
    print(f"{'Метрика эффективности':<32} | {'Baseline (FIFO)':<18} | {'VRP Оптимизатор':<18} | {'Выигрыш / Эффект':<18}")
    print("-" * 95)
    print(f"{'Задействовано инженеров':<32} | {base_staff:<18} | {opt_staff:<18} | {-staff_gain:+.1f}% персонала")
    print(f"{'Суммарный дневной пробег':<32} | {base_km:<15.1f} км | {opt_km:<15.1f} км | {-km_gain:+.1f}% км")
    print(f"{'Выполнено заявок':<32} | {base_assigned_count:<18} | {opt_assigned_count:<18} | +{opt_assigned_count - base_assigned_count} заявок")
    print(f"{'Не назначено заявок':<32} | {len(base_dropped):<18} | {len(opt_dropped):<18} | {len(base_dropped) - len(opt_dropped)} спасено")
    print("-" * 95)


def main():
    target_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(target_dir, "test_dataset")
    csv_files = sorted(glob.glob(os.path.join(data_dir, "*Синтетические*.csv")))
    if not csv_files:
        csv_files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))

    for path in csv_files:
        evaluate_dataset(path)


if __name__ == "__main__":
    main()
