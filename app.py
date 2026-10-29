"""Веб-интерфейс закупщика: поиск → карточка → сравнение → шортлист → запрос КП. Запуск: uvicorn app:app"""
import io
import os
import threading
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
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
JOB = {"running": False, "log": [], "title": "", "day": "", "count": 0}
LIVE_LIMIT = int(os.environ.get("LIVE_LIMIT", "30"))
T.env.globals.update(CATS=core.CATEGORIES, CAT=core.CAT, NETS=core.NETS, LABELS=LABELS, STATUSES=STATUSES,
                     CITIES=list(collect.REGION), NET_CITIES=core.NET_CITIES, LIVE=bool(os.environ.get("YANDEX_API_KEY")))


def db():
    return core.connect()


def params(request: Request) -> dict:
    p = request.query_params
    return {"net": p.get("net", ""), "cat": p.get("cat", ""), "city": p.get("city") or "Екатеринбург",
            "q": p.get("q", "").strip(), "only": p.getlist("only")}


def run_search(p):
    return core.search(db(), p["cat"] or None, p["city"], p["net"] or None, p["q"], p["only"])


def first(s, field):
    v = s["facts"].get(field)
    return v[0][0] if v else ""


def allv(s, field):
    return ", ".join(dict.fromkeys(v[0] for v in s["facts"].get(field) or []))


def focus(s, cats, city):
    """Рейтинг под первую категорию из списка (выбранную в поиске, если она есть у поставщика)."""
    return core.focus(s, cats[0], city) if cats else s.update(score=0, why=[], miss=[], cat="") or s


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


T.env.globals.update(first=first, allv=allv)
T.env.filters["host"] = core.host
T.env.filters["tel"] = lambda p: f"{p[:2]} {p[2:5]} {p[5:8]}-{p[8:10]}-{p[10:]}" if p and len(p) == 12 else p


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    p = params(request)
    rows = run_search(p) if (p["cat"] or p["q"] or p["net"]) else []
    stats = {"n": len(rows), "price": sum(bool(r["facts"].get("price")) for r in rows),
             "local": sum(any(t.startswith("Работает в регионе") for _, t in r["why"]) for r in rows)}
    return T.TemplateResponse(request, "index.html", {"p": p, "rows": rows, "stats": stats, "job": JOB})


@app.get("/s/{sid}", response_class=HTMLResponse)
def supplier(request: Request, sid: int):
    p = params(request)
    s = core.load(db(), sid)
    if not s:
        return RedirectResponse("/")
    cats = sorted({o["category"] for o in s["offers"]}, key=lambda x: x != p["cat"])
    focus(s, cats, p["city"])
    return T.TemplateResponse(request, "supplier.html", {"p": p, "s": s, "cats": cats, "letter": letter(s, p)})


@app.post("/s/{sid}/mark")
def mark(sid: int, status: str = Form(""), note: str = Form(""), starred: str = Form(""), back: str = Form("/")):
    c = db()
    c.execute("insert or replace into marks values(?,?,?,?,?)", (sid, status, note[:2000], int(bool(starred)),
                                                                 core.now()))
    c.commit()
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


@app.post("/s/{sid}/star")
def star(sid: int):
    c = db()
    m = c.execute("select * from marks where supplier_id=?", (sid,)).fetchone()
    on = 0 if m and m["starred"] else 1
    c.execute("insert or replace into marks values(?,?,?,?,?)",
              (sid, m["status"] if m else "", m["note"] if m else "", on, core.now()))
    c.commit()
    return JSONResponse({"starred": on})


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
    today = core.now()[:10]
    if JOB.get("day") != today:
        JOB.update(day=today, count=0)
    if JOB["count"] >= LIVE_LIMIT:  # публичный прототип: каждый запуск — платные запросы к Yandex Search API
        JOB.update(log=[f"Лимит живых поисков на сегодня исчерпан ({LIVE_LIMIT})."], title="")
    elif not JOB["running"] and cat in core.CAT and os.environ.get("YANDEX_API_KEY"):
        JOB["count"] += 1
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
