"""
Фронтенд и бэкенд обязаны считать одинаково: python -m pytest tests/

История проекта: index.html содержал собственный геокодер и собственную модель
расстояний. Они разошлись с Python (координаты на 0.4-2 км, депо на 3.2 км),
и это заметили только вручную. Тесты ниже фиксируют совпадение.

Тесты, которым нужен Node.js, пропускаются, если его нет в системе.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import pytest
except ImportError:
    class _DummyPytest:
        @staticmethod
        def skip(msg, allow_module_level=False):
            print(f"  пропущено: {msg}")
            raise SystemExit(0)

        class mark:
            @staticmethod
            def parametrize(*a, **k):
                return lambda fn: fn
    pytest = _DummyPytest()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vrptw_4pass_solver as solver  # noqa: E402

HTML = os.path.join(ROOT, "index.html")
NODE = shutil.which("node")


def _js_fragment(pattern: str) -> str:
    html = open(HTML, encoding="utf-8").read()
    m = re.search(pattern, html, re.S)
    assert m, f"не найден фрагмент {pattern[:40]}"
    return m.group(0)


def _run_node(source: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(source)
        path = f.name
    try:
        res = subprocess.run([NODE, path], capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, res.stderr[:1000]
        return json.loads(res.stdout)
    finally:
        os.unlink(path)


def test_hash_matches_javascript():
    """stable_hash32 в Python обязан повторять hashStr из index.html."""
    if not NODE:
        pytest.skip("node не установлен")
    samples = ["аб", "Город Москва, ул.Окская, д. 32Кузьминки",
               "Домодедово, ул.Зеленая, д. 85Домодедово", "", "x" * 120]
    src = _js_fragment(r"function hashStr\(str\) \{.*?\n    \}") + f"""
const samples = {json.dumps(samples, ensure_ascii=False)};
console.log(JSON.stringify(samples.map(hashStr)));
"""
    js_values = _run_node(src)
    assert js_values == [solver.stable_hash32(s) for s in samples]


def test_coordinates_match_javascript():
    """Координаты каждой заявки должны совпадать в обоих движках до метра."""
    if not NODE:
        pytest.skip("node не установлен")
    cases = []
    for path in sorted(glob.glob(os.path.join(ROOT, "test_dataset", "*Синтетические*.csv"))):
        for r in solver.load_dataset(path)[0]:
            cases.append([r.district, r.address, r.lat, r.lon])
    assert cases, "не найдены синтетические датасеты"

    src = "\n".join([
        _js_fragment(r"const DISTRICT_COORDS = \{.*?\n    \};"),
        _js_fragment(r"const GEOCODE_CACHE = \{.*?\};"),
        _js_fragment(r"function hashStr\(str\) \{.*?\n    \}"),
        _js_fragment(r"function getCoords\(district, address\) \{.*?\n    \}"),
    ]) + f"""
const cases = {json.dumps(cases, ensure_ascii=False)};
let worst = 0;
for (const [d, a, plat, plon] of cases) {{
  const c = getCoords(d, a);
  const m = Math.hypot((c[0]-plat)*111000, (c[1]-plon)*111000*Math.cos(plat*Math.PI/180));
  if (m > worst) worst = m;
}}
console.log(JSON.stringify({{total: cases.length, worstMeters: worst}}));
"""
    res = _run_node(src)
    assert res["total"] == len(cases)
    assert res["worstMeters"] < 1.0, f"координаты разошлись на {res['worstMeters']:.1f} м"


def test_embedded_assets_are_up_to_date():
    """Вшитые в index.html таблицы должны соответствовать текущим данным бэкенда."""
    html = open(HTML, encoding="utf-8").read()

    m = re.search(r"const GEOCODE_CACHE = (\{.*?\});", html, re.S)
    assert m, "в index.html нет GEOCODE_CACHE — запустите embed_frontend_assets.py"
    assert json.loads(m.group(1)) == solver._GEOCODE_CACHE, \
        "GEOCODE_CACHE устарел — запустите python3 embed_frontend_assets.py"

    m = re.search(r"const WINDING_FACTORS = (\{.*?\});", html, re.S)
    assert m, "в index.html нет WINDING_FACTORS"
    embedded = json.loads(m.group(1))
    expected = solver.calibrate_winding_factors() or dict(solver.WINDING_FACTORS)
    for mode, value in expected.items():
        assert abs(embedded.get(mode, 0) - value) < 1e-6, \
            f"коэффициент {mode} устарел — запустите python3 embed_frontend_assets.py"


def test_depot_tables_match():
    """Списки депо и ключевых слов обязаны совпадать построчно."""
    html = open(HTML, encoding="utf-8").read()
    block = re.search(r"const DEPOTS = \[(.*?)\n    \];", html, re.S)
    assert block, "в index.html нет таблицы DEPOTS"

    js_ids = re.findall(r'id:\s*"([^"]+)"', block.group(1))
    assert set(js_ids) == set(solver.DEPOTS), f"наборы депо разные: {js_ids} против {list(solver.DEPOTS)}"
    assert js_ids[-1] == "vostok", "запасное депо должно проверяться последним"

    for dep_id, keys_json in re.findall(r'id:\s*"([^"]+)".*?keys:\s*(\[[^\]]*\])', block.group(1), re.S):
        assert json.loads(keys_json) == solver.DEPOTS[dep_id]["keys"], \
            f"ключевые слова депо {dep_id} разошлись"


def test_region_rules_match():
    """Правила регионов (Подмосковье) должны совпадать в обоих движках."""
    html = open(HTML, encoding="utf-8").read()

    m = re.search(r"const SUBURB_DISTRICTS = \[(.*?)\];", html, re.S)
    assert m, "в index.html нет SUBURB_DISTRICTS"
    js_suburbs = re.findall(r"'([^']+)'", m.group(1))
    assert tuple(js_suburbs) == tuple(solver.SUBURB_DISTRICTS), \
        f"списки городов Подмосковья разные: {js_suburbs} против {list(solver.SUBURB_DISTRICTS)}"

    m = re.search(r"const SUBURB_ENGINEERS_PER_REQUESTS = (\d+);", html)
    assert m and int(m.group(1)) == solver.SUBURB_ENGINEERS_PER_REQUESTS, \
        "норма бригад на заявки в городе разошлась"

    assert "regionOfDistrict" in html and "suburbHome" in html, \
        "во фронтенде нет функций региона — бригады снова поедут из Москвы в Каширу"


def test_district_tables_match():
    """Таблица районов должна быть одна и та же по обе стороны."""
    html = open(HTML, encoding="utf-8").read()
    block = re.search(r"const DISTRICT_COORDS = \{(.*?)\n    \};", html, re.S).group(1)
    js = {m.group(1): (float(m.group(2)), float(m.group(3)))
          for m in re.finditer(r"'([^']+)'\s*:\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]", block)}
    assert js == {k: tuple(v) for k, v in solver.DISTRICT_COORDS.items()}


def test_traffic_default_matches_javascript():
    """Умолчание учёта пробок обязано совпадать: иначе браузер и /api/upload
    считают один и тот же файл по-разному, и цифры из консоли несравнимы с экраном."""
    html = open(HTML, encoding="utf-8").read()

    m = re.search(r"let yandexTrafficEnabled = (true|false);", html)
    assert m, "в index.html не найдено объявление yandexTrafficEnabled"
    js_default = m.group(1) == "true"
    assert js_default == solver.TRAFFIC_ENABLED_DEFAULT, (
        f"умолчание разошлось: index.html={js_default}, "
        f"TRAFFIC_ENABLED_DEFAULT={solver.TRAFFIC_ENABLED_DEFAULT}"
    )


def test_shift_and_solver_defaults_match_javascript():
    """Смена бригад и лимит решателя настраиваются в интерфейсе, но умолчания
    обязаны совпадать: иначе автономный режим в браузере и /api/upload посчитают
    один и тот же файл на разных сменах."""
    html = open(HTML, encoding="utf-8").read()

    expected = {
        "SHIFT_START_DEFAULT_MIN": solver.SHIFT_START_DEFAULT_MIN,
        "SHIFT_END_DEFAULT_MIN": solver.SHIFT_END_DEFAULT_MIN,
        "ORTOOLS_TIME_LIMIT_DEFAULT_SEC": solver.ORTOOLS_TIME_LIMIT_DEFAULT_SEC,
    }
    for name, py_value in expected.items():
        m = re.search(r"const " + name + r" = ([\d.]+);", html)
        assert m, f"в index.html нет {name}"
        assert float(m.group(1)) == float(py_value), (
            f"умолчание {name} разошлось: index.html={m.group(1)}, солвер={py_value}"
        )


def test_traffic_lon_scale_matches_javascript():
    """Масштаб долготы — литерал по обе стороны, сверяем побитово."""
    html = open(HTML, encoding="utf-8").read()

    m = re.search(r"const MOSCOW_LON_SCALE = ([\d.]+);", html)
    assert m, "в index.html нет MOSCOW_LON_SCALE"
    assert float(m.group(1)) == solver.MOSCOW_LON_SCALE, (
        f"масштаб долготы разошёлся: {m.group(1)} против {solver.MOSCOW_LON_SCALE}"
    )


def test_traffic_multiplier_matches_javascript():
    """k_traffic в index.html обязан совпадать с yandex_traffic_multiplier до последнего знака.

    Округление тут неочевидное: Python повторяет Math.round через floor(x*100+0.5),
    и любая правка профиля легко расходит движки на 0.01. На таком расхождении
    план в браузере и план с бэкенда разъезжаются молча.
    """
    if not NODE:
        pytest.skip("node не установлен")

    points = [
        (55.7558, 37.6173),   # центр
        (55.70, 37.80),       # восток, Кузьминки
        (55.60, 37.74),       # юг, Братеево
        (55.90, 37.57),       # север, Лианозово
        (55.75, 37.40),       # запад
    ]
    cases = []
    for time_min in range(8 * 60, 22 * 60 + 1, 7):
        for transport in ("car", "foot", "bicycle", "transit"):
            for a in points:
                for b in points:
                    if a != b:
                        cases.append([time_min, list(a), list(b), transport])

    src = "\n".join([
        _js_fragment(r"const MOSCOW_LON_SCALE = [\d.]+;"),
        _js_fragment(r"const YANDEX_HOURLY_SCORE = \{.*?\n    \};"),
        _js_fragment(r"function getYandexTrafficScore\(timeMin\) \{.*?\n    \}"),
        _js_fragment(r"function getYandexTrafficMultiplier\(timeMin, c1, c2, transport\) \{.*?\n    \}"),
    ]) + f"""
// в тесте профиль считается всегда: умолчание проверяет отдельный тест
let yandexTrafficEnabled = true;
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(([t, a, b, tr]) => getYandexTrafficMultiplier(t, a, b, tr))));
"""
    js_values = _run_node(src)
    py_values = [
        solver.yandex_traffic_multiplier(t, tuple(a), tuple(b), tr)
        for t, a, b, tr in cases
    ]
    assert len(js_values) == len(cases)

    mismatches = [
        (c, j, p) for c, j, p in zip(cases, js_values, py_values) if j != p
    ]
    assert not mismatches, (
        f"k_traffic разошёлся в {len(mismatches)} из {len(cases)} кейсов, "
        f"первый: время={mismatches[0][0][0]} мин, js={mismatches[0][1]}, py={mismatches[0][2]}"
    )


if __name__ == "__main__":
    print("=" * 70)
    print("ТЕСТЫ СОГЛАСОВАННОСТИ ФРОНТЕНДА И БЭКЕНДА")
    print("=" * 70)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"▶ {name} ...", end=" ", flush=True)
            fn()
            print("✅ OK")
    print("\n✅ Фронтенд и бэкенд согласованы!")
