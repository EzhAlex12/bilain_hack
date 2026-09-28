"""
Геокодирование адресов датасетов через OSM Nominatim -> geocode_cache.json

Запуск (нужен интернет):  python3 geocode_addresses.py
Повторный запуск дозаполняет кеш, уже найденные адреса не перезапрашиваются.

Политика Nominatim: не чаще 1 запроса в секунду и осмысленный User-Agent.
205 адресов -> примерно 4-8 минут.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(ROOT, "geocode_cache.json")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "BeelineVRP-LCT2026/1.0 (hackathon routing project)"
PAUSE_SEC = 1.1

# Центры районов - используются только для проверки правдоподобности ответа геокодера
sys.path.insert(0, ROOT)
from vrptw_4pass_solver import DISTRICT_COORDS, MOSCOW_CENTER, haversine_km  # noqa: E402

SUBURBS = ("Кашира", "Ступино", "Домодедово")

# Официальные офисы/склады округов. Геокодируются наравне с заявками:
# раньше их координаты были захардкожены, причём в Python и в index.html по-разному.
DEPOT_ADDRESSES = [
    "Город Москва, ул.Юных Ленинцев, д. 83 стр. 4",
    "Город Москва, проезд.Симферопольский, д. 7",
    "Город Москва, ул.Бирюлёвская, д. 1 стр. 1",
]
DEPOT_DISTRICT = "__depot__"
MAX_DEVIATION_KM = {"moscow": 12.0, "suburb": 25.0}

STREET_TYPES = {
    "ул": "улица", "пр-кт": "проспект", "пр-т": "проспект", "пер": "переулок",
    "б-р": "бульвар", "бул": "бульвар", "наб": "набережная", "ш": "шоссе",
    "проезд": "проезд", "пр-зд": "проезд", "пл": "площадь", "туп": "тупик",
    "линия": "линия", "аллея": "аллея", "кв-л": "квартал", "тракт": "тракт",
}
# Названия-прилагательные ставятся перед типом (Грайвороновская улица),
# названия-существительные в родительном падеже - после (улица Маяковского)
ADJ_TAIL = ("ая", "ый", "ий", "ое", "ья", "яя", "ой")


def _house_of(address: str) -> str:
    m = re.search(r"\bд\.?\s*([0-9][^,]*)", re.sub(r"\bкв\.?\s*\d+\b", " ", address, flags=re.I), flags=re.I)
    return norm_house(m.group(1)) if m else ""


def norm_house(raw: str) -> str:
    h = re.sub(r"\bкорп?\.?\s*", "к", raw, flags=re.I)
    h = re.sub(r"\bстр\.?\s*", "с", h, flags=re.I)
    h = re.sub(r"\bвл\.?\s*", "", h, flags=re.I)
    h = re.sub(r"\bк\.?\s+", "к", h, flags=re.I)
    return re.sub(r"\s+", "", h).strip(" .,")


def parse_address(address: str, district: str) -> tuple[str, str, str, bool]:
    """
    Разбирает адрес в (город, тип улицы, название улицы, дом).

    Форматы в датасетах кейса несовместимы между собой:
      "Город Москва, ул.Окская, д. 32"          - тип перед названием, запятые есть;
      "МО, г. Кашира Центральная ул. д. 21"     - регион впереди, тип после названия, запятых нет;
      "Москва Бирюлевская ул. д. 44"            - то же без региона.
    Поэтому разбор идёт по токенам, а не по позициям между запятыми.
    """
    a = " " + address.strip() + " "
    a = re.sub(r"\bкв\.?\s*\d+\b", " ", a, flags=re.I)                 # квартира геокодеру не нужна
    a = re.sub(r"^\s*(?:МО\s*,|Московская\s+обл(?:асть)?\.?\s*,?)", " ", a, flags=re.I)

    house = ""
    m = re.search(r"\bд\.?\s*([0-9][^,]*)", a, flags=re.I)
    if m:
        house = norm_house(m.group(1))
        a = a[:m.start()] + " " + a[m.end():]

    city, is_suburb = "Москва", False
    for cand in list(SUBURBS) + ["Москва"]:
        m = re.search(rf"(?:^|[,\s])(?:г\.?\s*)?(?:[Гг]ород\s+)?({cand})(?:[,\s]|$)", a, flags=re.I)
        if m:
            city, is_suburb = cand, cand in SUBURBS
            a = a[:m.start(1)] + " " + a[m.end(1):]
            break
    a = re.sub(r"(?:^|[,\s])(?:г\.|[Гг]ород)(?=[\s,])", " ", a)

    stype = ""
    for abbr, full in STREET_TYPES.items():
        m = re.search(rf"(?:^|[,\s.]){re.escape(abbr)}\.?(?=[\s,.]|$)", a, flags=re.I)
        if m:
            stype = full
            a = a[:m.start()] + " " + a[m.end():]
            break

    name = re.sub(r"[,\s]+", " ", a).strip(" .,")
    return city, stype, name, is_suburb


def street_variants(stype: str, name: str) -> list[str]:
    """Возможные написания улицы в OSM: порядок типа и названия в русских адресах не фиксирован."""
    if not name:
        return []
    if not stype:
        return [name]

    out = []
    m = re.match(r"^(\d+-[а-яё]{1,2})\s+(.+)$", name, flags=re.I)
    if m:
        ordinal, rest = m.group(1), m.group(2)
        out += [f"{ordinal} {stype} {rest}",   # 11-я улица Текстильщиков
                f"{ordinal} {rest} {stype}",   # 2-я Синичкина улица
                f"{stype} {name}"]
    else:
        last = name.split()[-1].lower()
        if last.endswith(ADJ_TAIL):
            out += [f"{name} {stype}", f"{stype} {name}"]     # Центральная улица
        else:
            out += [f"{stype} {name}", f"{name} {stype}"]     # улица Кржижановского
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def build_queries(address: str, district: str) -> list[str]:
    city, stype, name, is_suburb = parse_address(address, district)
    region = "Московская область" if (is_suburb or district in SUBURBS) else ""
    tail = f"{city}, {region}, Россия" if region else f"{city}, Россия"

    house = _house_of(address)
    queries = []
    for street in street_variants(stype, name):
        if house:
            queries.append(f"{street}, {house}, {tail}")
    for street in street_variants(stype, name):
        queries.append(f"{street}, {tail}")            # улица без дома - центр улицы
    if not queries:
        queries.append(f"{address}, Россия")
    return queries[:8]


def nominatim(query: str) -> tuple[float, float] | None:
    url = NOMINATIM + "?" + urllib.parse.urlencode(
        {"format": "json", "limit": "1", "addressdetails": "0", "countrycodes": "ru", "q": query}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def plausible(lat: float, lon: float, district: str) -> bool:
    if district == DEPOT_DISTRICT:                 # депо проверяем по всей Москве
        return haversine_km(lat, lon, MOSCOW_CENTER[0], MOSCOW_CENTER[1]) <= 40.0
    center = DISTRICT_COORDS.get(district, MOSCOW_CENTER)
    limit = MAX_DEVIATION_KM["suburb" if district in SUBURBS else "moscow"]
    return haversine_km(lat, lon, center[0], center[1]) <= limit


def collect_addresses() -> list[tuple[str, str]]:
    """(район, адрес) по всем CSV в test_dataset, включая контрольные - на случай загрузки жюри."""
    seen, out = set(), []
    for a in DEPOT_ADDRESSES:                      # депо идут первыми - они нужны всегда
        seen.add((DEPOT_DISTRICT, a))
        out.append((DEPOT_DISTRICT, a))
    paths = sorted(glob.glob(os.path.join(ROOT, "test_dataset", "*.csv")),
                   key=lambda p: (0 if "Синтетические" in p else 1, p))
    for path in paths:
        for enc in ("utf-8-sig", "cp1251"):
            try:
                with open(path, encoding=enc, newline="") as f:
                    rows = list(csv.DictReader(f, delimiter=";"))
                if rows and any((r.get("Адрес") or "").strip() for r in rows):
                    break
            except UnicodeDecodeError:
                rows = []
        for r in rows:
            addr = (r.get("Адрес") or "").strip()
            dist = (r.get("Район") or "").strip()
            if not addr or "адрес офис" in (r.get("Заявка") or "").lower():
                continue
            if (dist, addr) not in seen:
                seen.add((dist, addr))
                out.append((dist, addr))
    return out


def main() -> None:
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        print(f"Найден кеш: {len(cache)} адресов уже разрешено")

    targets = collect_addresses()
    todo = [(d, a) for d, a in targets if f"{d}||{a}" not in cache]
    print(f"Всего адресов: {len(targets)} | осталось разрешить: {len(todo)}")
    if not todo:
        print("Всё уже в кеше.")
        return

    failed = []
    for n, (district, address) in enumerate(todo, 1):
        hit = None
        for q in build_queries(address, district):
            try:
                res = nominatim(q)
            except Exception as e:
                print(f"  [{n}/{len(todo)}] сетевая ошибка: {type(e).__name__} {e}")
                time.sleep(PAUSE_SEC * 3)
                continue
            time.sleep(PAUSE_SEC)
            if res and plausible(res[0], res[1], district):
                hit = res
                break

        if hit:
            cache[f"{district}||{address}"] = [round(hit[0], 6), round(hit[1], 6)]
            mark = "ok"
        else:
            failed.append((district, address))
            mark = "НЕ НАЙДЕН"
        print(f"  [{n}/{len(todo)}] {district} | {address} -> {mark}")

        if n % 25 == 0:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=0)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)

    print(f"\nГотово. В кеше {len(cache)} адресов, файл: {CACHE_FILE}")
    if failed:
        print(f"Не разрешено {len(failed)} адресов (для них останется запасное размещение по району):")
        for d, a in failed[:20]:
            print(f"  - {d} | {a}")


if __name__ == "__main__":
    main()
