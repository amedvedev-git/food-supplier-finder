"""Веб-интерфейс закупщика: поиск → карточка → сравнение → шортлист → запрос КП. Запуск: uvicorn app:app"""
import io
import os
import re
import threading
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from openpyxl import Workbook

import collect
import core

app = FastAPI(title="Поиск поставщиков")
T = Jinja2Templates(directory=str(core.ROOT / "templates"))
LABELS = {"price": "Цена", "moq": "Мин. заказ", "delivery": "Доставка", "docs": "Документы", "geo": "География",
          "phone": "Телефон", "email": "Email", "inn": "ИНН", "price_list": "Прайс-лист", "wholesale": "Опт / HoReCa"}
STATUSES = ["", "Написали", "КП получено", "Договариваемся", "Отказ"]
ASK = {"цена": "актуальный прайс-лист и цены для HoReCa", "минимальный заказ": "минимальный объём заказа",
       "условия доставки": "условия и график доставки", "документы":
       "какие документы сопровождают продукцию (декларации, ветеринарные свидетельства)",
       "ИНН": "реквизиты компании"}
JOB = {"running": False, "log": [], "title": "", "day": "", "count": 0, "done": []}
# публичный прототип: каждый запуск — платные запросы к Yandex Search API (≈1–2 ₽). Одна пара «категория + город»
# раз в день (повтор найдёт те же сайты), всего не больше LIVE_LIMIT запусков в день.
# ponytail: счётчики в памяти, сбрасываются при перезапуске сервиса
LIVE_LIMIT = int(os.environ.get("LIVE_LIMIT", "10"))
T.env.globals.update(CATS=core.CATEGORIES, CAT=core.CAT, NETS=core.NETS, LABELS=LABELS, STATUSES=STATUSES,
                     CITIES=core.CITIES, NET_CITIES=core.NET_CITIES, FILTERS=core.FILTERS, LIVE=bool(os.environ.get("YANDEX_API_KEY")))


def db():
    return core.connect()


def params(request: Request) -> dict:
    p = request.query_params
    net = p.get("net", "")
    cities = core.NET_CITIES.get(net, core.CITIES)
    city = p.get("city") if p.get("city") in cities else cities[0]  # база собрана только по городам сетей
    return {"net": net, "cat": p.get("cat", ""), "city": city, "q": p.get("q", "").strip(), "only": p.getlist("only")}


def run_search(p):
    return core.search(db(), p["cat"] or None, p["city"], p["net"] or None, p["q"], p["only"])


def first(s, field):
    v = s["facts"].get(field)
    return v[0][0] if v else ""


def allv(s, field):
    vals = list(dict.fromkeys(v[0] for v in s["facts"].get(field) or []))
    if field == "delivery":  # одинаковый порядок признаков во всех строках — колонку можно читать сверху вниз
        rank = lambda v: next((i for i, k in enumerate(core.DELIVERY_ORDER) if v.startswith(k)), 99)  # noqa: E731
        vals.sort(key=rank)
    return ", ".join(vals)


def focus(s, cats, city):
    """Рейтинг под первую категорию из списка (выбранную в поиске, если она есть у поставщика)."""
    return core.focus(s, cats[0], city) if cats else s.update(score=0, why=[], miss=[], cat="") or s


# --- для шаблонов: полоса покрытия, зона доставки, вид деятельности ------------------------------------------
COVERAGE = [("price", "Цена"), ("moq", "Мин. заказ"), ("delivery", "Доставка"), ("docs", "Документы"),
            ("phone", "Телефон"), ("email", "Email"), ("inn", "ИНН"), ("egrul", "ЕГРЮЛ")]
LEGAL_RU = {"ACTIVE": "действует", "LIQUIDATED": "ликвидировано", "LIQUIDATING": "ликвидируется",
            "BANKRUPT": "банкрот", "REORGANIZING": "реорганизация", "НЕ НАЙДЕН": "нет в ЕГРЮЛ"}


def has(s, k):
    return s["legal"].get("status") == "ACTIVE" if k == "egrul" else bool(s["facts"].get(k))


def zone(s):
    for p, t in s["why"]:
        if p > 0 and t.startswith("Работает в регионе"):
            return "в регионе"
        if t.startswith("Доставляет в регион"):
            return "доставляют в регион"
        if t.startswith("Доставка по России"):
            return "по всей России"
    return "зона доставки не указана"


def kind(s):
    o = s["legal"].get("okved") or ""
    return (("опт", "good") if o.startswith("46") else ("производитель", "good") if o.startswith(("10", "11")) else
            ("розница", "warn") if o.startswith("47") else ("общепит", "bad") if o.startswith("56") else None)


def plural(n, one, few, many):
    return one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def legal_line(s):
    l = s["legal"]  # noqa: E741
    if not l.get("status"):
        return "ИНН найден, ЕГРЮЛ не проверен" if s["inn"] or s["facts"].get("inn") else "ИНН не найден"
    years = f" · {l['years']} {plural(l['years'], 'год', 'года', 'лет')}" if l.get("years") is not None else ""
    return f"ЕГРЮЛ: {LEGAL_RU.get(l['status'], l['status'])}{years}"


def letter(s, p):
    cat = core.CAT.get(s.get("cat") or p["cat"], {}).get("name", "продукция").lower()
    net = f"сеть «{core.NETS[p['net']]}»" if p["net"] in core.NETS else "сеть ресторанов"
    asks = [ASK[m] for m in s["miss"] if m in ASK] or ["актуальные цены и условия поставки"]
    body = (f"Здравствуйте!\n\nМы — {net}, {p['city']}. Ищем поставщика: {cat}.\n"
            f"Нашли вашу компанию в открытых источниках и хотим получить коммерческое предложение.\n\n"
            f"Просим уточнить:\n" + "".join(f"— {a};\n" for a in asks + ["возможна ли отсрочка платежа",
                                                                         "можно ли получить образцы"]) +
            f"\nОриентировочный объём: ___ в неделю, доставка: {p['city']}, ___ точек.\n\nСпасибо!")
    subj = f"Запрос КП: {cat}"
    email = first(s, "email")
    return {"subject": subj, "body": body,
            "mailto": f"mailto:{email}?subject={quote(subj)}&body={quote(body)}" if email else ""}


def shortlist_n(c=None):
    return (c or db()).execute("select count(*) from marks where starred=1 or status!=''").fetchone()[0]


T.env.globals.update(first=first, allv=allv, has=has, zone=zone, kind=kind, plural=plural, legal_line=legal_line,
                     COVERAGE=COVERAGE, LEGAL_RU=LEGAL_RU, known=lambda s: sum(has(s, k) for k, _ in COVERAGE),
                     shortlist_n=shortlist_n)
# у Pixelify Sans «5» похожа на «S»: рисуем свою — зеркальная «2» с прямой верхней перекладиной.
# Прямоугольники в сетке шрифта (единицы — 1/700 em, y от базовой линии), снятые с глифа «2»
FIVE = [(-443, -364, 42, 372), (-364, -256, 42, 119), (-256, -195, 42, 308), (-195, -178, 42, 372),
        (-178, -133, 294, 372), (-133, -70, 42, 119), (-133, -70, 294, 372), (-70, -54, 42, 372), (-54, 9, 105, 308)]
FIVE_SVG = Markup('<svg class="d5" viewBox="0 -460 414 470" role="img" aria-label="5"><path d="' + "".join(
    f"M{a} {t}H{b}V{u}H{a}Z" for t, u, a, b in FIVE) + '"/></svg>')  # один path — без швов между частями


def px(n):
    return Markup("").join(FIVE_SVG if ch == "5" else ch for ch in str(n))


T.env.filters["px"] = px
T.env.filters["host"] = core.host
T.env.filters["tel"] = lambda p: f"{p[:2]} {p[2:5]} {p[5:8]}-{p[8:10]}-{p[10:]}" if p and len(p) == 12 else p
T.env.filters["telify"] = lambda t: re.sub(r"\+7\d{10}", lambda m: T.env.filters["tel"](m.group()), t)
T.env.globals["score_cls"] = lambda n: "" if n >= 75 else "mid" if n >= 55 else "low"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    p = params(request)
    found = run_search({**p, "only": []}) if (p["cat"] or p["q"] or p["net"]) else []
    rows = [s for s in found if all(core.FILTERS[k][1](s) for k in p["only"] if k in core.FILTERS)]
    counts = {k: sum(f(s) for s in found) for k, (_, f) in core.FILTERS.items()}  # счётчики на чипах фильтров
    return T.TemplateResponse(request, "index.html", {"p": p, "rows": rows, "found": len(found), "counts": counts,
                                                      "job": JOB})


def card_context(request, sid):
    p = params(request)
    s = core.load(db(), sid)
    if not s:
        return None
    cats = sorted({o["category"] for o in s["offers"]}, key=lambda x: x != p["cat"])
    focus(s, cats, p["city"])
    return {"p": p, "s": s, "cats": cats, "letter": letter(s, p)}


@app.get("/s/{sid}", response_class=HTMLResponse)
def supplier(request: Request, sid: int):
    ctx = card_context(request, sid)
    return T.TemplateResponse(request, "supplier.html", ctx) if ctx else RedirectResponse("/")


@app.get("/s/{sid}/panel", response_class=HTMLResponse)
def panel(request: Request, sid: int):
    """Карточка, раскрытая в строке выдачи."""
    ctx = card_context(request, sid)
    return T.TemplateResponse(request, "_card.html", ctx) if ctx else HTMLResponse("", status_code=404)


@app.post("/s/{sid}/mark")
def mark(sid: int, status: str = Form(None), note: str = Form(None), starred: str = Form(None), back: str = Form("")):
    """Меняет только присланные поля: статус из выдачи не затирает заметку и наоборот."""
    c = db()
    m = c.execute("select * from marks where supplier_id=?", (sid,)).fetchone()
    old = dict(m) if m else {"status": "", "note": "", "starred": 0}
    c.execute("insert or replace into marks values(?,?,?,?,?)",
              (sid, old["status"] if status is None else status, old["note"] if note is None else note[:2000],
               old["starred"] if starred is None else int(starred == "1"), core.now()))
    c.commit()
    if back.startswith("/"):
        return RedirectResponse(back, status_code=303)
    return JSONResponse({"ok": True, "n": shortlist_n(c)})


@app.post("/s/{sid}/star")
def star(sid: int):
    c = db()
    m = c.execute("select * from marks where supplier_id=?", (sid,)).fetchone()
    on = 0 if m and m["starred"] else 1
    c.execute("insert or replace into marks values(?,?,?,?,?)",
              (sid, m["status"] if m else "", m["note"] if m else "", on, core.now()))
    c.commit()
    return JSONResponse({"starred": on, "n": shortlist_n(c)})


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, ids: str = ""):
    p = params(request)
    c = db()
    items = []
    for i in [int(x) for x in ids.split(",") if x.isdigit()][:4]:
        s = core.load(c, i)
        if s:
            items.append(focus(s, sorted({o["category"] for o in s["offers"]}, key=lambda x: x != p["cat"]), p["city"]))
    return T.TemplateResponse(request, "compare.html", {"p": p, "items": items})


@app.get("/shortlist", response_class=HTMLResponse)
def shortlist(request: Request):
    p = params(request)
    rows = shortlisted(p)
    return T.TemplateResponse(request, "shortlist.html", {"p": p, "rows": rows})


def shortlisted(p):
    c = db()
    out = []
    for (sid,) in c.execute("select supplier_id from marks where starred=1 or status!='' order by at desc"):
        s = core.load(c, sid)
        if s:
            out.append(focus(s, sorted({o["category"] for o in s["offers"]}), p["city"]))
    return out


@app.get("/export.xlsx")
def export(request: Request, what: str = "search"):
    p = params(request)
    rows = shortlisted(p) if what == "shortlist" else run_search(p)
    wb = Workbook()
    ws = wb.active
    ws.title = "Поставщики"
    ws.append(["Рейтинг", "Поставщик", "Категория", "Город", "Цена", "Мин. заказ", "Доставка", "Документы",
               "Телефон", "Email", "Сайт", "ИНН", "Статус ЕГРЮЛ", "Не хватает", "Статус", "Заметка", "Источник"])
    for s in rows:
        ws.append([s["score"], s["name"], core.CAT.get(s.get("cat"), {}).get("name", ""), s["city"] or "",
                   first(s, "price"), first(s, "moq"), allv(s, "delivery"),
                   ", ".join(v[0] for v in s["facts"].get("docs", [])), first(s, "phone"), first(s, "email"),
                   s["site"] or "", s["inn"] or first(s, "inn"), s["legal"].get("status", ""), ", ".join(s["miss"]),
                   s["mark"]["status"], s["mark"]["note"], s["offers"][0]["url"] if s["offers"] else ""])
    for col, w in zip("ABCDEFGHIJKLMNOPQ", (8, 34, 22, 16, 16, 18, 40, 30, 16, 26, 28, 14, 12, 30, 14, 30, 40)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=suppliers.xlsx"})


@app.post("/live")
def live(cat: str = Form(...), city: str = Form("Екатеринбург"), q: str = Form(""), net: str = Form("")):
    today, key = core.now()[:10], f"{cat}|{city}"
    if JOB.get("day") != today:
        JOB.update(day=today, count=0, done=[])
    if JOB["running"]:
        pass
    elif key in JOB["done"]:
        JOB.update(log=["Этот поиск сегодня уже запускали: новые сайты в выдаче Яндекса появятся не раньше завтра."],
                   title=f"{core.CAT.get(cat, {}).get('name', cat)} · {city}")
    elif JOB["count"] >= LIVE_LIMIT:
        JOB.update(log=[f"Лимит живых поисков на сегодня исчерпан ({LIVE_LIMIT})."], title="")
    elif cat in core.CAT and city in core.CITIES and os.environ.get("YANDEX_API_KEY"):
        JOB["count"] += 1
        JOB["done"].append(key)
        JOB.update(running=True, log=[], title=f"{core.CAT[cat]['name']} · {city}")

        def run():
            try:
                collect.live(cat, city, q, lambda m: JOB["log"].append(m))
            except Exception as e:  # noqa: BLE001 — ошибку показываем в журнале, сервис живёт
                JOB["log"].append(f"ошибка: {e}")
            JOB["running"] = False

        threading.Thread(target=run, daemon=True).start()
    return RedirectResponse(f"/?cat={cat}&city={quote(city)}&net={net}&q={quote(q)}", status_code=303)


@app.get("/live/status")
def live_status():
    return JOB


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    c = db()
    n = {"suppliers": c.execute("select count(*) from suppliers").fetchone()[0],
         "offers": c.execute("select count(*) from offers").fetchone()[0],
         "facts": c.execute("select count(*) from facts").fetchone()[0],
         "src": c.execute("select source, count(distinct supplier_id) from offers group by source").fetchall()}
    return T.TemplateResponse(request, "about.html", {"p": params(request), "n": n})
