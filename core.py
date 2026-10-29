"""Чистая логика сервиса: извлечение фактов из текста, рейтинг поставщика, хранилище SQLite.

Без LLM: регулярки + словари. Каждый факт хранится с цитатой и ссылкой на страницу, откуда он взят.
"""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).parent
CATEGORIES = yaml.safe_load((ROOT / "categories.yaml").read_text(encoding="utf-8"))
CAT = {c["id"]: c for c in CATEGORIES}
_CAT_RE = {c["id"]: re.compile("|".join(c["kw"]), re.I) for c in CATEGORIES}
NETS = {"sushkof": "Сушкоф и пицца", "zhiznmart": "Жизньмарт"}

# --- извлечение -------------------------------------------------------------------------------------------

_SENT = re.compile(r"(?<=[.!?;])\s+|\n+")
_INN = re.compile(r"ИНН\D{0,12}?(\d{12}|\d{10})(?!\d)", re.I)
_PHONE = re.compile(r"(?:\+7|(?<!\d)8)[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-zа-я]{2,}", re.I)
_MOQ = re.compile(r"минимальн\w*\s+(?:сумм\w*\s+)?(?:заказ\w*|парти\w*|объ[её]м\w*(?:\s+заказ\w*)?)"
                  r"|мин\.\s*(?:заказ\w*|парти\w*|сумм\w*(?:\s+заказ\w*)?)", re.I)
_PRICE = re.compile(r"(?:от\s*)?\d{1,3}(?:[  ]?\d{3})*(?:[.,]\d{1,2})?\s*(?:руб\.?|₽|р\.)\s*"
                    r"(?:/|за)\s*(?:кг|л|шт|уп\w*|тонн\w*|т(?![а-яё])|короб\w*|пач\w*|ящ\w*)", re.I)
_DELIVERY = re.compile(r"доставк|самовывоз|отгрузк|отгружа", re.I)
DOCS = [
    ("Декларация соответствия", r"декларац\w*\s+(?:о\s+)?соответстви"),
    ("Сертификат соответствия", r"сертификат\w*\s+соответстви"),
    ("Сертификаты качества", r"сертификат\w*\s+качеств"),
    ("Ветеринарные документы (Меркурий)", r"ветеринарн|меркури"),
    ("ХАССП", r"хассп|haccp"),
    ("ISO 22000", r"iso\s*22000"),
    ("ГОСТ", r"(?<![а-яё])гост(?![а-яё])"),
    ("Халяль", r"халял|halal"),
]
GEO = [
    ("по России", r"по\s+(?:всей\s+)?(?:территории\s+)?росси|по\s+рф(?![а-яё])|во\s+все\s+регион|в\s+регионы"),
    ("УрФО", r"урфо|уральск\w+\s+федеральн|по\s+уралу"),
    ("Екатеринбург", r"екатеринбург|(?<![а-яё])екб(?![а-яё])"),
    ("Свердловская обл.", r"свердловск"),
    ("Челябинск", r"челябинск"),
    ("Тюмень", r"тюмен"),
    ("Уфа", r"(?<![а-яё])уф[аеуы](?![а-яё])"),
    ("Башкортостан", r"башкорт|башкири"),
    ("Пермь", r"(?<![а-яё])перм[ьиск]"),
    ("Москва", r"(?<![а-яё])москв"),
    ("Санкт-Петербург", r"санкт-петербург|(?<![а-яё])спб(?![а-яё])"),
]
URAL = {"Екатеринбург", "Челябинск", "Тюмень", "Пермь", "Уфа"}
NEAR = {"Екатеринбург": {"Екатеринбург", "Свердловская обл."}, "Уфа": {"Уфа", "Башкортостан"}}
# города сетей (проверено 29.10.2026: сайт франшизы «Сушкоф и пицца», o.lifemart.ru/cities);
# малые города Свердловской области засчитываются как регион Екатеринбурга
NET_CITIES = {"sushkof": ["Екатеринбург", "Тюмень"], "zhiznmart": ["Екатеринбург", "Челябинск", "Тюмень", "Уфа"]}
_WHOLESALE = re.compile(r"(?<![а-яё])опт(?:ом|у|а|ов\w*)?(?![а-яё])|horeca|хорека|поставк|поставщик|дистрибь?ют"
                        r"|для\s+(?:ресторан|кафе|общепит)|производител|b2b", re.I)
_RESTAURANT = re.compile(r"доставк\w*\s+(?:роллов|суши|пицц|еды|обедов)|наше\s+меню|заказать\s+(?:роллы|пиццу|суши)",
                         re.I)


def inn_valid(inn: str) -> bool:
    d = [int(c) for c in inn]
    chk = lambda coef: sum(a * b for a, b in zip(coef, d)) % 11 % 10  # noqa: E731
    if len(d) == 10:
        return chk([2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[9]
    if len(d) == 12:
        return (chk([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[10]
                and chk([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[11])
    return False


def norm_phone(s: str):
    d = re.sub(r"\D", "", s)
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    return "+7" + d if len(d) == 10 else None


def _cut(s: str, n=160) -> str:
    s = s.strip(" .;:—–-\t")
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


_MOQ_VAL = re.compile(r"[\s(:—–-]*((?:от|до)?\s*\d[\d\s.,]*\s*(?:₽|руб\w*\.?|р\.?|кг|тонн\w*|т(?![а-яё])|шт\w*|"
                      r"короб\w*|уп\w*|л(?![а-яё])|мест\w*)?)", re.I)


def _win(s: str, m, before=50, after=110) -> str:
    """Окно вокруг совпадения: текст сайта часто идёт без точек (меню, адрес, телефоны одним куском)."""
    if len(s) <= 200:
        return s
    a = s.rfind(" ", 0, m.start() - before) + 1 if m.start() > before else 0
    b = s.find(" ", m.end() + after)
    b = len(s) if b < 0 else b
    return ("…" if a else "") + s[a:b] + ("…" if b < len(s) else "")


def extract(text: str) -> dict:
    """Факты из текста: {поле: [(значение, цитата), ...]}, плюс флаги wholesale / restaurant."""
    text = re.sub(r"[ \t ]+", " ", text)
    sents = [s for s in _SENT.split(text) if s.strip()]
    f = {k: [] for k in ("inn", "phone", "email", "moq", "price", "delivery", "docs", "geo")}

    def add(field, value, s, m):
        if value and value not in [v for v, _ in f[field]]:
            f[field].append((value, _cut(_win(s, m), 220)))

    for s in sents:
        for m in _INN.finditer(s):
            if inn_valid(m.group(1)):
                add("inn", m.group(1), s, m)
        for m in _PHONE.finditer(s):
            add("phone", norm_phone(m.group()), s, m)
        for m in _EMAIL.finditer(s):
            e = m.group().lower()
            if not e.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "svg", "webp", "gif"):
                add("email", e, s, m)
        m = _MOQ.search(s)
        if m:
            num = _MOQ_VAL.match(s[m.end():])
            add("moq", num.group(1).strip() if num else _cut(s[m.end():], 60), s, m)
        for m in _PRICE.finditer(s):
            add("price", re.sub(r"\s+", " ", m.group()), s, m)
        m = _DELIVERY.search(s)
        if m:
            add("delivery", _cut(_win(s, m), 140), s, m)
        for label, rx in DOCS:
            m = re.search(rx, s, re.I)
            if m:
                add("docs", label, s, m)
        for label, rx in GEO:
            m = re.search(rx, s, re.I)
            if m:
                add("geo", label, s, m)
    f["wholesale"] = bool(_WHOLESALE.search(text))
    f["restaurant"] = bool(_RESTAURANT.search(text))
    return f


def categorize(text: str) -> list:
    return [cid for cid, rx in _CAT_RE.items() if rx.search(text)]


def domain(url: str) -> str:
    host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


_SEO = re.compile(r"купить|оптом|цен[аы]|недорого|доставк|магазин|в екатеринбурге|предложени|главная|каталог|"
                  r"страница|(?<![а-яё])home(?![a-z])", re.I)


def host(url: str) -> str:
    d = domain(url or "")
    try:
        return d.encode().decode("idna")  # рыба66.рф вместо xn--66-…
    except UnicodeError:
        return d


def pick_name(title: str, fallback: str) -> str:
    """Название компании из <title>: самая короткая часть, похожая на имя («Гросс Фуд — оптовые поставки» → «Гросс Фуд»).
    Не имя: SEO-фразы, слоганы со строчной буквы, названия товаров (слова категорий)."""
    parts = [p.strip() for p in re.split(r"\s[|—–-]\s|\s::\s", title or "") if p.strip()]
    good = [p for p in parts if 2 < len(p) <= 40 and not _SEO.search(p) and not p[0].islower() and not categorize(p)]
    return min(good, key=len) if good else fallback


def norm_name(name: str) -> str:
    n = re.sub(r"\b(ооо|оао|зао|пао|ао|ип|тд|гк|ltd|llc)\b", " ", name.lower())
    return re.sub(r"[^a-zа-я0-9]+", "", n.replace("ё", "е"))


# --- рейтинг ----------------------------------------------------------------------------------------------

def score(s: dict, cat: str, city: str):
    """Прозрачный рейтинг 0–100: (баллы, [(баллы, причина)], [чего не хватает]). s — load() или эквивалент."""
    facts, why, miss = s.get("facts", {}), [], []
    has = lambda k: bool(facts.get(k))  # noqa: E731
    vals = lambda k: [x[0] for x in facts.get(k) or []]  # noqa: E731

    offers = [o for o in s.get("offers", []) if o["category"] == cat]
    if any(o["source"] != "yandex" for o in offers):
        why.append((30, "Есть предложение в рубрике каталога (" + offers[0]["source"] + ")"))
    elif offers:
        on_site = cat in vals("cats") or any(cat in categorize(o.get("title", "") + " " + (o.get("text") or ""))
                                             for o in offers)
        why.append((20, "Найден в Яндексе, категория есть на сайте") if on_site else
                   (5, "Найден в Яндексе, но на сайте категория не подтверждена"))
        if has("wholesale"):
            why.append((10, "На сайте — опт / HoReCa"))
    if has("restaurant") and not has("wholesale"):
        why.append((-20, "Похоже на ресторан или доставку еды, а не поставщика"))

    near = NEAR.get(city, {city})
    geo = set(vals("geo"))
    many = len(geo - {"по России", "УрФО"}) > 3  # длинный список городов на сайте — это зона доставки, не адрес
    if s.get("city") in near or (geo & near and not many):
        why.append((20, "Работает в регионе: " + ", ".join(sorted((geo | {s.get("city") or ""}) & near))))
    elif geo & near:
        why.append((10, "Доставляет в регион: " + ", ".join(sorted(geo & near)) + " — среди других городов"))
    elif "по России" in geo or (city in URAL and "УрФО" in geo):
        why.append((10, "Доставка по России / региону"))
    else:
        miss.append("доставка в " + city)

    for k, pts, label in (("phone", 5, "телефон"), ("email", 3, "email"), ("price", 5, "цена"),
                          ("moq", 5, "минимальный заказ"), ("delivery", 5, "условия доставки")):
        if has(k):
            why.append((pts, label.capitalize() + ": " + vals(k)[0]))
        else:
            miss.append(label)

    legal = s.get("legal") or {}
    if has("inn") or s.get("inn"):
        why.append((5, "ИНН указан"))
    else:
        miss.append("ИНН")
    if legal.get("status") == "ACTIVE":
        why.append((5, "Действующее юрлицо (ЕГРЮЛ)"))
        if (legal.get("years") or 0) >= 3:
            why.append((5, f"На рынке {legal['years']} лет"))
    elif legal.get("status") == "НЕ НАЙДЕН":
        miss.append("ИНН не найден в ЕГРЮЛ")
    elif legal.get("status"):  # LIQUIDATING, LIQUIDATED, BANKRUPT, REORGANIZING
        why.append((-30, "Статус в ЕГРЮЛ: " + legal["status"]))

    if has("docs"):
        why.append((10, "Документы: " + ", ".join(vals("docs")[:3])))
    else:
        miss.append("документы")

    total = max(0, min(100, sum(p for p, _ in why)))
    return total, sorted(why, key=lambda x: -x[0]), miss


# --- хранилище --------------------------------------------------------------------------------------------

SCHEMA = """
create table if not exists suppliers(id integer primary key, name text, site text, city text, inn text,
  legal text, created_at text, updated_at text);
create table if not exists keys(key text primary key, supplier_id int);
create table if not exists offers(id integer primary key, supplier_id int, category text, title text, price text,
  geo text, url text, source text, text text, at text, unique(supplier_id, category, url));
create table if not exists facts(supplier_id int, field text, value text, quote text, url text, source text, at text,
  primary key(supplier_id, field, value));
create table if not exists marks(supplier_id int primary key, status text, note text, starred int default 0, at text);
create table if not exists pages(url text primary key, status int, text text, at text);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def connect(path=ROOT / "data" / "suppliers.sqlite") -> sqlite3.Connection:
    Path(path).parent.mkdir(exist_ok=True)
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")  # интерфейс читает, пока сборщик пишет
    c.executescript(SCHEMA)
    return c


def supplier_keys(name="", site="", phones=(), inn="") -> list:
    k = []
    if inn:
        k.append("inn:" + inn)
    if site:
        k.append("dom:" + domain(site))
    k += ["tel:" + p for p in phones if p]
    if not k and norm_name(name):
        k.append("name:" + norm_name(name))
    return k


def upsert(c, name, keys, **fields) -> int:
    """Найти поставщика по любому ключу (ИНН / домен / телефон / имя) или создать; склеить дубли."""
    ids = sorted({r[0] for r in c.execute(
        f"select supplier_id from keys where key in ({','.join('?' * len(keys))})", keys)}) if keys else []
    if ids:
        sid = ids[0]
        for other in ids[1:]:
            merge(c, sid, other)
    else:
        sid = c.execute("insert into suppliers(name, created_at, updated_at) values(?,?,?)",
                        (name, now(), now())).lastrowid
    for k in keys:
        c.execute("insert or ignore into keys values(?,?)", (k, sid))
    row = c.execute("select * from suppliers where id=?", (sid,)).fetchone()
    upd = {k: v for k, v in fields.items() if v and not row[k]}
    if name and (not row["name"] or len(row["name"]) > 60):
        upd["name"] = name
    if upd:
        c.execute(f"update suppliers set {', '.join(k + '=?' for k in upd)}, updated_at=? where id=?",
                  (*upd.values(), now(), sid))
    return sid


def merge(c, keep, drop):
    for t in ("keys", "offers", "facts", "marks"):
        c.execute(f"update or ignore {t} set supplier_id=? where supplier_id=?", (keep, drop))
        c.execute(f"delete from {t} where supplier_id=?", (drop,))
    c.execute("delete from suppliers where id=?", (drop,))


def add_facts(c, sid, facts: dict, url, source):
    for field, items in facts.items():
        if isinstance(items, bool):
            items = [("да", "")] if items else []
        for value, quote in items:
            c.execute("insert or ignore into facts values(?,?,?,?,?,?,?)",
                      (sid, field, value, quote, url, source, now()))


def add_offer(c, sid, category, title, url, source, price="", geo="", text=""):
    c.execute("insert or ignore into offers(supplier_id, category, title, price, geo, url, source, text, at) "
              "values(?,?,?,?,?,?,?,?,?)", (sid, category, title, price, geo, url, source, text, now()))


def load(c, sid) -> dict:
    r = c.execute("select * from suppliers where id=?", (sid,)).fetchone()
    if not r:
        return None
    s = dict(r)
    s["legal"] = json.loads(s["legal"] or "{}")
    s["facts"] = {}
    for f in c.execute("select * from facts where supplier_id=? order by at", (sid,)):
        s["facts"].setdefault(f["field"], []).append((f["value"], f["quote"], f["url"], f["source"], f["at"]))
    if s["facts"].get("restaurant") and s["facts"].get("wholesale"):
        s["facts"].pop("restaurant")
    s["offers"] = [dict(o) for o in c.execute("select * from offers where supplier_id=? order by id", (sid,))]
    m = c.execute("select * from marks where supplier_id=?", (sid,)).fetchone()
    s["mark"] = dict(m) if m else {"status": "", "note": "", "starred": 0}
    return s


def search(c, cat=None, city="Екатеринбург", net=None, q="", only=()):
    """Поставщики категории (или по тексту) с рейтингом, лучшие сверху."""
    cats = [cat] if cat else [x["id"] for x in CATEGORIES if not net or net in x["nets"]]
    sql = (f"select distinct supplier_id from offers where category in ({','.join('?' * len(cats))})")
    args = list(cats)
    if q:
        sql += " and (title like ? or text like ? or supplier_id in (select id from suppliers where name like ?))"
        args += [f"%{q}%"] * 3
    out = []
    for (sid,) in c.execute(sql, args).fetchall():
        s = load(c, sid)
        best = max((score(s, x, city) + (x,) for x in cats if any(o["category"] == x for o in s["offers"])),
                   key=lambda t: t[0])
        s["score"], s["why"], s["miss"], s["cat"] = best
        if "price" in only and not s["facts"].get("price"):
            continue
        if "docs" in only and not s["facts"].get("docs"):
            continue
        if "contacts" in only and not (s["facts"].get("phone") or s["facts"].get("email")):
            continue
        if "local" in only and not any(p > 0 and t.startswith("Работает в регионе") for p, t in s["why"]):
            continue
        out.append(s)
    return sorted(out, key=lambda s: -s["score"])
