"""Веб-интерфейс закупщика: поиск → карточка → сравнение → шортлист → запрос КП. Запуск: uvicorn app:app"""
import io
import os
import re
import threading
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from openpyxl import Workbook

import collect
import core



@asynccontextmanager
async def lifespan(_):
    async with MCP.session_manager.run():  # MCP-сервер смонтирован в конце файла
        yield


app = FastAPI(title="Поиск поставщиков", lifespan=lifespan)
T = Jinja2Templates(directory=str(core.ROOT / "templates"))
LABELS = {"price": "Цена", "moq": "Мин. заказ", "delivery": "Доставка", "docs": "Документы", "decl": "Декларации",
          "geo": "География", "phone": "Телефон", "email": "Email", "inn": "ИНН", "price_list": "Прайс-лист", "wholesale": "Опт / HoReCa"}
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
# группы в списке категорий; внутри — по алфавиту. Новая категория без группы попадёт в «Другое»
GROUPS = {"Рыба и морепродукты": ["salmon", "seafood", "roe", "nori"],
          "Мясо, молочка, овощи": ["meat", "dairy", "vegetables"],
          "Мука и сыры": ["flour", "cheese"],
          "Бакалея": ["rice", "groats", "oil", "sauces"],
          "Готовая продукция и напитки": ["desserts", "drinks"],
          "Упаковка": ["packaging"]}
abc = lambda c: c["name"].lower().replace("ё", "е")  # noqa: E731
CAT_GROUPS = [(g, sorted((core.CAT[i] for i in ids if i in core.CAT), key=abc)) for g, ids in GROUPS.items()]
CAT_GROUPS += [("Другое", sorted((c for c in core.CATEGORIES if not any(c["id"] in v for v in GROUPS.values())),
                                 key=abc))]
T.env.globals.update(CATS=sorted(core.CATEGORIES, key=abc), CAT_GROUPS=[g for g in CAT_GROUPS if g[1]], CAT=core.CAT, NETS=core.NETS, LABELS=LABELS, STATUSES=STATUSES,
                     CITIES=core.CITIES, NET_CITIES=core.NET_CITIES, FILTERS=core.FILTERS, LIVE=bool(os.environ.get("YANDEX_API_KEY")))


def db():
    return core.connect()


def params(request: Request) -> dict:
    p = request.query_params
    return make_params(p.get("net", ""), p.get("cat", ""), p.get("city"), p.get("q", ""), p.getlist("only"))


def make_params(net="", cat="", city=None, q="", only=()) -> dict:
    net = net if net in core.NETS else ""
    cities = core.NET_CITIES.get(net, core.CITIES)
    city = city if city in cities else cities[0]  # база собрана только по городам сетей
    return {"net": net, "cat": cat if cat in core.CAT else "", "city": city, "q": (q or "").strip(), "only": list(only)}


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
    if k == "egrul":
        return s["legal"].get("status") == "ACTIVE"
    return bool(s["facts"].get(k) or (k == "docs" and s["facts"].get("decl")))  # огонёк «Документы» — и реестр


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


def card_context(p, sid):
    s = core.load(db(), sid)
    if not s:
        return None
    cats = sorted({o["category"] for o in s["offers"]}, key=lambda x: x != p["cat"])
    focus(s, cats, p["city"])
    return {"p": p, "s": s, "cats": cats, "letter": letter(s, p)}


@app.get("/s/{sid}", response_class=HTMLResponse)
def supplier(request: Request, sid: int):
    ctx = card_context(params(request), sid)
    return T.TemplateResponse(request, "supplier.html", ctx) if ctx else RedirectResponse("/")


@app.get("/s/{sid}/panel", response_class=HTMLResponse)
def panel(request: Request, sid: int):
    """Карточка, раскрытая в строке выдачи."""
    ctx = card_context(params(request), sid)
    return T.TemplateResponse(request, "_card.html", ctx) if ctx else HTMLResponse("", status_code=404)


@app.post("/s/{sid}/mark")
async def mark(sid: int, request: Request):
    """Меняет только присланные поля: статус из выдачи не затирает заметку и наоборот.
    Форма читается напрямую: Form(None) превращает пустую строку в «не прислано», и статус «Нет» или стёртая
    заметка не сохранялись."""
    f = await request.form()
    status, note, starred, back = f.get("status"), f.get("note"), f.get("starred"), f.get("back", "")
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


KP_FIELDS = ("price", "moq", "delivery", "docs")


@app.post("/s/{sid}/kp")
async def kp(sid: int, request: Request):
    """Ответ поставщика на КП: закупщик вносит цену, мин. заказ, доставку, документы — они становятся фактами
    карточки (источник «КП»), зажигают огоньки, дают баллы и убирают вопрос из письма. delete — убрать свой факт."""
    f = await request.form()
    c = db()
    if f.get("delete"):
        c.execute("delete from facts where supplier_id=? and field=? and value=? and source=?",
                  (sid, f.get("field"), f.get("delete"), core.KP))
    else:
        core.add_facts(c, sid, {k: [(v.strip()[:200], "Из ответа поставщика на запрос КП, внёс закупщик")]
                                for k in KP_FIELDS if (v := f.get(k) or "").strip()}, "", core.KP)
    c.commit()
    return JSONResponse({"ok": True})


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


# --- MCP: те же поиск, карточка и письмо КП для ИИ-ассистентов (Claude и др.) ---------------------------------
# Только чтение: шортлист и заметки остаются в веб-интерфейсе (публичный сервер без авторизации).
from typing import Any  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://suppliers.alexmedvedev.com")
T.env.globals["PUBLIC_URL"] = PUBLIC_URL
MCP = MCPServer("Поставщики food", instructions=(
    "Поиск поставщиков продуктов питания и упаковки для закупщика сетей «Сушкоф и пицца» и «Жизньмарт» "
    "(Екатеринбург, Тюмень, Челябинск, Уфа). Данные из открытых источников: у каждого факта есть цитата и ссылка "
    "на страницу-источник — приводи их пользователю. Чего нет в карточке (цена, минимальный заказ, документы), "
    "не додумывай: это вопросы в письмо-запрос КП (draft_rfq). Сначала list_categories, потом search_suppliers."))
RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _card_url(sid, p):
    return f"{PUBLIC_URL}/s/{sid}?" + urlencode({"cat": p["cat"], "city": p["city"], "net": p["net"]})


def _brief(s, p):
    return {"id": s["id"], "name": s["name"], "score": s["score"], "category": core.CAT.get(s["cat"], {}).get("name"),
            "legal": legal_line(s), "kind": (kind(s) or [None])[0], "delivery_zone": zone(s),
            "known": f"{sum(has(s, k) for k, _ in COVERAGE)} из 8", "delivery": allv(s, "delivery") or None,
            "min_order": first(s, "moq") or None, "price": first(s, "price") or None, "phone": first(s, "phone") or None,
            "email": first(s, "email") or None, "site": s["site"], "ask_in_rfq": s["miss"], "card": _card_url(s["id"], p)}


def _need(cat):
    if cat and cat not in core.CAT:
        raise ValueError(f"Нет категории «{cat}». Есть: {', '.join(core.CAT)}")


@MCP.tool(annotations=RO, structured_output=True)
def list_categories() -> dict[str, Any]:
    """Категории (id для остальных инструментов) по группам, сети, их города и фильтры выдачи."""
    return {"categories": [{"group": g, "id": c["id"], "name": c["name"], "networks": c["nets"]}
                           for g, cats in CAT_GROUPS for c in cats],
            "networks": core.NETS, "cities": core.NET_CITIES,
            "filters": {k: label for k, (label, _) in core.FILTERS.items()}}


@MCP.tool(annotations=RO, structured_output=True)
def search_suppliers(category: str, city: str = "Екатеринбург", network: str = "", only: list[str] | None = None,
                     query: str = "", limit: int = 10) -> dict[str, Any]:
    """Поставщики категории в городе, лучшие по рейтингу 0–100 сверху.
    only — фильтры из list_categories (local, price, contacts, docs); query — слово в названии или товаре."""
    _need(category)
    p = make_params(network, category, city, query, only or [])
    rows = run_search(p)
    return {"category": core.CAT[category]["name"], "city": p["city"], "found": len(rows),
            "suppliers": [_brief(s, p) for s in rows[:max(1, min(limit, 30))]]}


@MCP.tool(annotations=RO, structured_output=True)
def get_supplier(supplier_id: int, category: str = "", city: str = "Екатеринбург", network: str = "") -> dict[str, Any]:
    """Карточка поставщика: факты с цитатами и ссылками на источник, из чего сложился рейтинг, юрлицо по ЕГРЮЛ."""
    _need(category)
    ctx = card_context(make_params(network, category, city), supplier_id)
    if not ctx:
        raise ValueError(f"Поставщика {supplier_id} нет")
    s, p = ctx["s"], ctx["p"]
    facts = {LABELS.get(k, k): [{"value": v, "quote": q, "source_url": u, "source": src, "date": at[:10]}
                                for v, q, u, src, at in vals[:6]]
             for k, vals in s["facts"].items() if k in LABELS and vals and k != "wholesale"}
    return {**_brief(s, p), "score_breakdown": [{"points": pts, "reason": w} for pts, w in s["why"]],
            "legal_entity": {k: s["legal"].get(k) for k in ("status", "name", "ogrn", "years", "okved", "address")
                             if s["legal"].get(k) is not None} | {"inn": s["inn"]},
            "facts": facts, "other_categories": [core.CAT[c]["name"] for c in ctx["cats"][1:] if c in core.CAT]}


@MCP.tool(annotations=RO, structured_output=True)
def draft_rfq(supplier_id: int, category: str = "", city: str = "Екатеринбург", network: str = "") -> dict[str, Any]:
    """Письмо-запрос КП: вопросы собраны из того, чего нет в карточке. mailto — ссылка «открыть в почте»."""
    _need(category)
    ctx = card_context(make_params(network, category, city), supplier_id)
    if not ctx:
        raise ValueError(f"Поставщика {supplier_id} нет")
    return {"to": first(ctx["s"], "email") or None, **ctx["letter"]}


app.mount("/", MCP.streamable_http_app(  # путь /mcp; монтируется последним — веб-маршруты выше имеют приоритет
    stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(
        allowed_hosts=[PUBLIC_URL.split("//")[1], "127.0.0.1:*", "localhost:*"],
        allowed_origins=[PUBLIC_URL, "https://claude.ai", "http://127.0.0.1:*", "http://localhost:*"])))
