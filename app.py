"""
Веб-приложение для планирования маршрутов инженеров (Хакатон ЛЦТ 2026, Задача №3)
Запуск: python3 app.py
Открытие: http://localhost:8000
"""

import csv
import io
import json
import os
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Импортируем классы и алгоритм из нашего решателя
import vrptw_4pass_solver as solver


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_dataset")


def parse_csv_content(csv_text: str) -> tuple[list[solver.Request], tuple[float, float], str, list[str]]:
    """Парсит CSV из текста (формата датасетов кейса)."""
    raw_rows = []
    depot_address = ""
    district_hint = ""

    f = io.StringIO(csv_text.strip())
    reader = csv.DictReader(f, delimiter=";")
    for row in reader:
        req_id = (row.get("Заявка") or "").strip()
        if "адрес офис" in req_id.lower():
            depot_address = (row.get("Тип заявки BK") or "").strip()
            continue
        if not req_id or not (row.get("Тип заявки BK") or "").strip():
            continue
        raw_rows.append(row)

    requests: list[solver.Request] = []
    for row in raw_rows:
        bk_type = (row.get("Тип заявки BK") or "").strip()
        req_type = solver.BK_TYPE_MAP.get(bk_type, "repair")
        district = (row.get("Район") or "").strip()
        address = (row.get("Адрес") or "").strip()
        district_hint = district or district_hint

        lat, lon = solver.geocode_district_cached(address, district)
        w_start = solver.parse_minutes(row["Начало"])
        w_end = solver.parse_minutes(row["Окончание"])
        is_gigabit = (row.get("Гигабитное подключение") or "").strip() == "Да"
        demand = 2 if (req_type == "emergency" or is_gigabit) else 1

        requests.append(
            solver.Request(
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

    brigade_names = []
    for row in raw_rows:
        b = (row.get("Бригада") or "").strip()
        if b and b not in brigade_names:
            brigade_names.append(b)

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


def run_full_pipeline(requests: list[solver.Request], depot_coords: tuple[float, float], depot_address: str, n_engineers: int = 12, brigade_names: list[str] = None):
    has_suburbs = False  # Все инженеры стартуют строго из офиса
    n = max(n_engineers, len(brigade_names)) if brigade_names else n_engineers
    engineers = solver.create_engineers_pool(n_engineers=n, depot_coords=depot_coords, has_suburbs=has_suburbs, brigade_names=brigade_names)

    # 1. 4-Pass Optimizer (Гарантированное допустимое решение)
    opt_routes, opt_dropped = solver.run_4pass_optimization(requests, engineers)

    # 2. Оптимизация через Google OR-Tools (с гарантированным fallback на допустимое решение)
    ortools_status = '4-Pass Feasible (OR-Tools не установлен)'
    if solver.HAS_ORTOOLS:
        opt_routes, ortools_status = solver.optimize_routes_with_ortools(opt_routes, time_limit_sec=1)

    # 3. Baseline FIFO
    base_routes, base_dropped = solver.run_baseline_fifo(requests, engineers)

    # Метрики
    opt_assigned = sum(len(r.visits) for r in opt_routes)
    base_assigned = sum(len(r.visits) for r in base_routes)

    opt_km = round(sum(r.total_km for r in opt_routes), 1)
    base_km = round(sum(r.total_km for r in base_routes), 1)

    opt_staff = len(opt_routes)
    base_staff = len(base_routes)

    staff_gain = round(((base_staff - opt_staff) / base_staff * 100), 1) if base_staff > 0 else 0
    km_gain = round(((base_km - opt_km) / base_km * 100), 1) if base_km > 0 else 0

    # Сериализуемые маршруты
    serialized_routes = []
    colors = [
        "#E6194B", "#3CBL58", "#FFE119", "#4363D8", "#F58231",
        "#911EB4", "#42D4F4", "#F032E6", "#BFEF45", "#FABED4",
        "#469990", "#DCBEFF", "#9A6324", "#FFFAC8", "#800000"
    ]

    for idx, r in enumerate(opt_routes):
        eng = r.engineer
        color = colors[idx % len(colors)]
        visits_data = []
        for step_idx, v in enumerate(r.visits, 1):
            visits_data.append({
                "step": step_idx,
                "request_id": v.request.id,
                "req_type": v.request.req_type,
                "district": v.request.district,
                "address": v.request.address,
                "lat": v.request.lat,
                "lon": v.request.lon,
                "window_start": solver.fmt_time(v.request.window_start_min),
                "window_end": solver.fmt_time(v.request.window_end_min),
                "arrival": solver.fmt_time(v.arrival_min),
                "work_start": solver.fmt_time(v.service_start_min),
                "work_end": solver.fmt_time(v.service_end_min),
                "work_duration": v.request.work_duration_min,
                "travel_min": v.travel_min,
                "travel_km": v.travel_km,
                "is_gigabit": v.request.is_gigabit,
                "demand": v.request.equipment_demand,
                "explanation": solver.explain_visit(v, eng)
            })

        serialized_routes.append({
            "engineer_id": eng.id,
            "transport": eng.transport,
            "color": color,
            "home_lat": eng.home_lat,
            "home_lon": eng.home_lon,
            "shift_start": solver.fmt_time(eng.shift_start_min),
            "shift_end": solver.fmt_time(eng.shift_end_min),
            "skills": list(eng.skills),
            "total_km": round(r.total_km, 1),
            "total_travel_min": r.total_travel_min,
            "visits_count": len(r.visits),
            "visits": visits_data
        })

    serialized_dropped = []
    for req in opt_dropped:
        serialized_dropped.append({
            "request_id": req.id,
            "req_type": req.req_type,
            "district": req.district,
            "address": req.address,
            "lat": req.lat,
            "lon": req.lon,
            "window_start": solver.fmt_time(req.window_start_min),
            "window_end": solver.fmt_time(req.window_end_min),
            "explanation": solver.explain_dropped(req, engineers)
        })

    # Формируем объект solution в точном формате для веб-фронтенда
    frontend_engineers = []
    for idx, r in enumerate(opt_routes):
        eng = r.engineer
        color = colors[idx % len(colors)]
        schedule = []
        for step_idx, v in enumerate(r.visits, 1):
            schedule.append({
                "task": {
                    "id": v.request.id,
                    "typeBk": "Глобальная проблема" if v.request.req_type == "emergency" else (
                        "Подключение" if v.request.req_type == "connection" else (
                            "Дозаказ" if v.request.req_type == "extra_order" else "Локальная заявка"
                        )
                    ),
                    "typeHd": v.request.address,
                    "category": "accident" if v.request.req_type == "emergency" else (
                        "additional_order" if v.request.req_type == "extra_order" else v.request.req_type
                    ),
                    "reqSkill": v.request.req_type,
                    "durationMin": v.request.work_duration_min,
                    "district": v.request.district,
                    "address": v.request.address,
                    "gigabit": "Да" if v.request.is_gigabit else "Нет",
                    "winStart": v.request.window_start_min,
                    "winEnd": v.request.window_end_min,
                    "coords": [v.request.lat, v.request.lon]
                },
                "arrTime": v.arrival_min,
                "startWork": v.service_start_min,
                "endWork": v.service_end_min,
                "legKm": v.travel_km,
                "legDrive": v.travel_min,
                "explanation": solver.explain_visit(v, eng)
            })

        frontend_engineers.append({
            "id": eng.id,
            "name": eng.id,
            "role": ("🚗 Авто-инженер (Аварийщик)" if "emergency" in eng.skills and eng.transport == "car" else
                     ("🚗 Авто-инженер" if eng.transport == "car" else
                      ("🚲 Вело-инженер" if eng.transport == "bicycle" else "🚶 Пеший специалист"))),
            "transport": eng.transport,
            "color": color,
            "startCoords": [eng.home_lat, eng.home_lon],
            "tasks": [s["task"] for s in schedule],
            "schedule": schedule,
            "totalKm": round(r.total_km, 1),
            "totalDriveMin": r.total_travel_min
        })

    frontend_unassigned = []
    for req in opt_dropped:
        expl = solver.explain_dropped(req, [r.engineer for r in opt_routes])
        reason = expl.split("• Причина: ")[-1].strip() if "• Причина: " in expl else expl
        frontend_unassigned.append({
            "req": {
                "id": req.id,
                "category": "accident" if req.req_type == "emergency" else (
                    "additional_order" if req.req_type == "extra_order" else req.req_type
                ),
                "typeHd": "Авария на ТКД" if req.req_type == "emergency" else ("Подключение" if req.req_type == "connection" else "Ремонт"),
                "district": req.district,
                "address": req.address,
                "gigabit": "Да" if req.is_gigabit else "Нет",
                "winStart": req.window_start_min,
                "winEnd": req.window_end_min,
                "durationMin": req.work_duration_min,
                "coords": [req.lat, req.lon]
            },
            "reason": reason
        })

    frontend_solution = {
        "engineers": frontend_engineers,
        "unassigned": frontend_unassigned,
        "totalKm": opt_km,
        "totalDrive": sum(r.total_travel_min for r in opt_routes),
        "assignedCount": opt_assigned
    }

    frontend_baseline = {
        "totalKm": base_km,
        "assignedCount": base_assigned,
        "engineersCount": base_staff
    }

    return {
        "engine": "Google OR-Tools (VRPTW)" if solver.HAS_ORTOOLS else "4-Pass Solver",
        "ortools_status": ortools_status,
        "solution": frontend_solution,
        "baseline": frontend_baseline,
        "depot": {
            "address": depot_address or "Районный склад/офис",
            "lat": depot_coords[0],
            "lon": depot_coords[1]
        },
        "stats": {
            "total_requests": len(requests),
            "emergency_count": sum(1 for r in requests if r.req_type == "emergency"),
            "connection_count": sum(1 for r in requests if r.req_type == "connection"),
            "repair_count": sum(1 for r in requests if r.req_type == "repair"),
            "extra_count": sum(1 for r in requests if r.req_type == "extra_order"),
            "opt_staff": opt_staff,
            "base_staff": base_staff,
            "staff_gain": staff_gain,
            "opt_km": opt_km,
            "base_km": base_km,
            "km_gain": km_gain,
            "opt_assigned": opt_assigned,
            "base_assigned": base_assigned,
            "opt_dropped": len(opt_dropped),
            "base_dropped": len(base_dropped),
        },
        "routes": serialized_routes,
        "dropped": serialized_dropped
    }


class Handler(BaseHTTPRequestHandler):
    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_cors_headers()
            self.end_headers()
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            with open(html_path, "rb") as f:
                self.wfile.write(f.read())
            return

        if path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_cors_headers()
            self.end_headers()
            health_data = {
                "status": "ok",
                "has_ortools": solver.HAS_ORTOOLS,
                "engine": "Google OR-Tools" if solver.HAS_ORTOOLS else "4-Pass Solver"
            }
            self.wfile.write(json.dumps(health_data, ensure_ascii=False).encode("utf-8"))
            return

        if path == "/api/demo":
            query = parse_qs(parsed.query)
            region = query.get("region", ["vostok"])[0].lower()

            target_file = None
            if "vostok" in region or "восток" in region:
                target_file = "Восток Синтетические данные.csv"
            elif "yugocentr" in region or "югоцентр" in region or "yug" in region:
                target_file = "Югоцентр Синтетические данные.csv"
            elif "yugovostok" in region or "юго-восток" in region:
                target_file = "Юго-восток Синтетические данные.csv"

            if not target_file:
                target_file = "Восток Синтетические данные.csv"

            csv_path = os.path.join(DATA_DIR, target_file)
            if not os.path.exists(csv_path):
                self.send_error(404, f"Файл {target_file} не найден на диске")
                return

            requests, depot_coords, depot_addr, brigade_names = solver.load_dataset(csv_path)
            res = run_full_pipeline(requests, depot_coords, depot_addr, brigade_names=brigade_names)

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            content_length = int(self.headers.get("Content-Length", 0))
            raw_data = self.rfile.read(content_length)

            # Пробуем декодировать utf-8, затем cp1251 (стандарт Excel в РФ)
            csv_text = None
            for enc in ["utf-8-sig", "utf-8", "cp1251"]:
                try:
                    dec = raw_data.decode(enc)
                    if "\ufffd" not in dec:
                        csv_text = dec
                        break
                except UnicodeDecodeError:
                    continue

            if not csv_text:
                try:
                    csv_text = raw_data.decode("cp1251", errors="replace")
                except Exception:
                    pass

            if not csv_text:
                self.send_error(400, "Не удалось распознать кодировку файла (требуется UTF-8 или CP1251)")
                return

            try:
                requests, depot_coords, depot_addr, brigade_names = parse_csv_content(csv_text)
                if not requests:
                    self.send_error(400, "В CSV-файле не найдено строк с заявками в формате кейса")
                    return
                res = run_full_pipeline(requests, depot_coords, depot_addr, brigade_names=brigade_names)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
            return

        self.send_error(404, "Not Found")


def run_server(port=8000):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"\n=======================================================")
    print(f"  ВЕБ-СЕРВИС ПЛАНИРОВАНИЯ МАРШРУТОВ ИНЖЕНЕРОВ ЗАПУЩЕН")
    print(f"  Откройте браузер: http://localhost:{port}")
    print(f"=======================================================\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
        server.server_close()


if __name__ == "__main__":
    venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    if os.path.exists(venv_python) and os.path.abspath(sys.executable) != os.path.abspath(venv_python):
        try:
            import ortools
        except ImportError:
            os.execv(venv_python, [venv_python] + sys.argv)

    p = 8000
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        p = int(sys.argv[1])
    run_server(p)
