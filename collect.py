"""Сбор поставщиков из открытых источников → data/suppliers.sqlite.

Источники: Yandex Search API (сайты по городу), productcenter.ru (рубрики), сайты поставщиков, DaData (ЕГРЮЛ по ИНН).
Соблюдаем robots.txt, паузы между запросами; капчу и антибот-защиту не обходим — такой источник пропускаем
(agroserver.ru и Пульс цен закрыты для ботов). Запускать с российского IP — часть сайтов режет зарубежные:
    python collect.py seed [--cats salmon,rice] [--city Екатеринбург] [--pages 3]
Ключи в окружении: YANDEX_API_KEY, YANDEX_FOLDER_ID, DADATA_TOKEN (без них соответствующий шаг пропускается).
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

import core

UA = "Mozilla/5.0 (compatible; SupplierFinder/0.1; +https://suppliers.alexmedvedev.com/about)"
HTTP = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ru"}, timeout=15, follow_redirects=True)
DELAY = {"productcenter.ru": 3}
REGION = {"Екатеринбург": "54", "Тюмень": "55", "Челябинск": "56", "Уфа": "172", "Москва": "213",
          "Санкт-Петербург": "2", "Пермь": "50", "Новосибирск": "65", "Казань": "43", "Нижний Новгород": "47", "Самара": "51"}
# не поставщики: маркетплейсы, справочники, соцсети, отзывы, сами каталоги
SKIP = re.compile(r"(avito|ozon|wildberries|yandex|ya\.ru|2gis|pulscen|agroserver|productcenter|tiu\.ru|satom|"
                  r"flagma|deal\.by|hh\.ru|vk\.com|ok\.ru|t\.me|youtube|wikipedia|irecommend|otzovik|restoclub|"
                  r"zoon|yell|spr\.ru|rusprofile|list-org|checko|sbis|e1\.ru|dzen|pikabu|market\.|sber|"
                  r"tbank|vkusvill|perekrestok|magnit|lenta|metro-cc|auchan|okey|globus|kuper|samokat|"
                  r"orgpage|cataloxy|bizly|aport|b2b-center|tender|fishretail|fishnews|gov\.ru|regtorg|u2b\.ru|"
                  r"blizko|allbiz|optlist|bizorg|fis\.ru|promportal|metaprom|yp\.ru|unibo|lavka\.yandex|"
                  r"tradedir|gastronom\.ru|povar\.ru|eda\.ru|food\.ru|delivery-club|edadeal)", re.I)
LINKS = re.compile(r"контакт|contact|kontakt|доставк|dostavk|delivery|shipping|оплат|oplat|payment|о[ -]компании|"
                   r"about|o-kompanii|company|реквизит|rekvizit|сертифик|sertif|certif|документ|document|прайс|"
                   r"price|оптов|optov|wholesale|сотрудничеств|partner|horeca|условия", re.I)

_robots, _last, _lock = {}, {}, threading.Lock()
# сырые ответы (страницы, Яндекс, DaData): базу можно пересобрать новыми правилами без обхода и платных запросов.
# ponytail: без срока годности — для свежих данных удалить data/cache
CACHE = core.ROOT / "data" / "cache"


def cached(key, fetch, fresh=False):
    """Ответ из кэша или fetch() с записью в кэш. None не кэшируется: неудачный запрос повторится."""
    f = CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".json.gz")
    if f.exists() and not fresh:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    v = fetch()
    if v is not None:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f"{f.name}.{threading.get_ident()}.tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False)
        tmp.replace(f)
    return v


class Blocked(Exception):
    """Источник показал капчу или запретил доступ — дальше не идём."""


def allowed(url) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = RobotFileParser()
        try:
            r = HTTP.get(base + "/robots.txt", timeout=8)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def get(url, delay=1.0):
    """HTML страницы или None: из кэша, иначе с сайта."""
    return cached("page:" + url, lambda: fetch(url, delay))


def fetch(url, delay=1.0):
    """HTML страницы или None. Пауза на хост, robots.txt, лимит 3 МБ."""
    host = core.domain(url)
    if not allowed(url):
        return None
    with _lock:
        wait = _last.get(host, 0) + DELAY.get(host, delay) - time.time()
        _last[host] = time.time() + max(0, wait)
    if wait > 0:
        time.sleep(wait)
    try:
        r = HTTP.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code in (403, 429) and host in DELAY:
        raise Blocked(f"{host}: HTTP {r.status_code}")
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html") or len(r.content) > 3_000_000:
        return None
    if "не робот" in r.text[:20000] or "captcha" in str(r.url):
        raise Blocked(f"{host}: капча")
    return r.text


def text_of(tree: HTMLParser) -> str:
    for n in tree.css("script, style, noscript, svg"):
        n.decompose()
    body = tree.body or tree.root
    return re.sub(r"\s+", " ", body.text(separator=" ")) if body else ""


def cats_for(text, source_key, ref):
    """Категории объявления: по ключевым словам среди категорий этой рубрики; рубрика одной категории — она."""
    own = [c["id"] for c in core.CATEGORIES if ref in c.get(source_key, [])]
    hit = [x for x in core.categorize(text) if x in own]
    return hit or (own if len(own) == 1 else [])


# --- productcenter ----------------------------------------------------------------------------------------

def productcenter(c, catalog, pages, log):
    n_found = 0
    for n in range(1, pages + 1):
        url = f"https://productcenter.ru/producers/{catalog}" + (f"/page-{n}" if n > 1 else "")
        html = get(url)
        if not html:
            break
        for card in HTMLParser(html).css(".card_item.firm"):
            a = card.css_first(".text a.link")
            if not a:
                continue
            name, href = a.text(strip=True), urljoin(url, a.attributes.get("href", ""))
            desc = card.css_first(".item_descriptor").text(strip=True) if card.css_first(".item_descriptor") else ""
            cats = cats_for(f"{name}. {desc}", "productcenter", catalog)
            if not cats:
                continue
            page = get(href)
            if not page:
                continue
            t = text_of(HTMLParser(page))
            site = re.search(r"Официальный сайт\s+((?:https?://)?[\w.-]+\.[a-zа-я]{2,}\S*)", t)
            addr = re.search(r"Адрес\s+(.{5,160}?)\s+(?:Электронная почта|Официальный сайт|Телефон|Реквизиты)", t)
            parts = [p.strip() for p in addr.group(1).split(",")] if addr else []
            city = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
            site_url = ("http://" + site.group(1) if site and "//" not in site.group(1) else
                        site.group(1) if site else "")
            sid = core.upsert(c, name, core.supplier_keys(name, site_url), site=site_url, city=city)
            i = t.find("Контактная информация")
            contacts = t[i:t.find("Компания на карте", i)][:600] if i >= 0 else ""
            f = core.extract(contacts + " " + desc)
            core.add_facts(c, sid, f, href, "productcenter")
            for cat in cats:
                core.add_offer(c, sid, cat, desc[:120] or name, href, "productcenter", text=desc)
            c.commit()  # короткие транзакции: интерфейс читает базу во время сбора
            n_found += 1
        c.commit()
    log(f"productcenter/{catalog}: {n_found} производителей")


# --- Yandex Search API ------------------------------------------------------------------------------------

def yandex(query, city, fresh=False):
    """Выдача Яндекса по запросу в регионе города: [{url, title, text}]. fresh — мимо кэша (живой поиск)."""
    key, folder = os.environ.get("YANDEX_API_KEY"), os.environ.get("YANDEX_FOLDER_ID")
    if not key or not folder:
        return []
    return cached(f"yandex:{REGION.get(city, '225')}:{query}", lambda: yandex_api(query, city, key, folder), fresh)


def yandex_api(query, city, key, folder):
    body = {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": query, "familyMode": "FAMILY_MODE_MODERATE"},
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": "20", "docsInGroup": "1"},
            "maxPassages": "3", "region": REGION.get(city, "225"), "l10n": "LOCALIZATION_RU",
            "folderId": folder, "responseFormat": "FORMAT_XML"}
    r = HTTP.post("https://searchapi.api.cloud.yandex.net/v2/web/search", json=body,
                  headers={"Authorization": f"Api-Key {key}"}, timeout=40)
    r.raise_for_status()
    root = ET.fromstring(base64.b64decode(r.json()["rawData"]))
    err = root.find(".//error")
    if err is not None:
        raise httpx.HTTPError(f"Yandex: {err.get('code')} {''.join(err.itertext())}")
    out = []
    for doc in root.iter("doc"):
        url = doc.findtext("url") or ""
        title = "".join(doc.find("title").itertext()) if doc.find("title") is not None else ""
        passages = " ".join("".join(p.itertext()) for p in doc.iter("passage"))
        out.append({"url": url, "title": title, "text": passages})
    return out


def yandex_category(c, cat, city, log, extra="", fresh=False):
    n_found = 0
    for q in ([extra] if extra else core.CAT[cat]["queries"]):
        for hit in yandex(f"{q} оптом {city}" if extra else f"{q} {city}", city, fresh):
            if SKIP.search(core.domain(hit["url"])):
                continue
            p = urlparse(hit["url"])
            site = f"{p.scheme}://{p.netloc}"
            name = core.pick_name(hit["title"], core.host(site))
            sid = core.upsert(c, name, core.supplier_keys(name, site), site=site)
            core.add_offer(c, sid, cat, hit["title"], hit["url"], "yandex", text=hit["text"])
            core.add_facts(c, sid, core.extract(hit["text"]), hit["url"], "yandex")
            n_found += 1
    c.commit()
    log(f"yandex/{cat}: {n_found} сайтов")


# --- сайты поставщиков ------------------------------------------------------------------------------------

def crawl(site):
    """Главная + до 4 страниц (контакты, доставка, о компании, документы).
    → [(url, весь текст, основной контент, название, ссылки на прайсы)]"""
    html = get(site)
    if not html:
        return []
    tree = HTMLParser(html)
    og = tree.css_first('meta[property="og:site_name"]')
    title = tree.css_first("title")
    name = core.pick_name((og.attributes.get("content") or "") if og else (title.text() if title else ""), "")
    pages, seen, files = [], {site.rstrip("/")}, []
    cand = []
    for a in tree.css("a[href]"):
        href, label = a.attributes.get("href") or "", a.text(strip=True)
        u = urljoin(site + "/", href).split("#")[0]
        if core.domain(u) != core.domain(site):
            continue
        if re.search(r"\.(pdf|xlsx?|docx?)$", u, re.I) and re.search(r"прайс|price|каталог|catalog", u + label, re.I):
            files.append((u, label or "прайс-лист"))
        elif LINKS.search(href + " " + label) and u.rstrip("/") not in seen and not re.search(r"\.\w{2,4}$", u):
            seen.add(u.rstrip("/"))
            cand.append(u)
    pages.append((site, text_of(tree), main_of(html), name))
    for u in cand[:4]:
        h = get(u)
        if h:
            pages.append((u, text_of(HTMLParser(h)), main_of(h), ""))
    return [(*p, files if i == 0 else []) for i, p in enumerate(pages)]


def main_of(html):
    """Основной контент страницы без меню, боковых блоков и футера (алгоритм класса Readability)."""
    return trafilatura.extract(html, include_tables=True, favor_recall=True) or ""


def enrich_sites(c, log, only_ids=None):
    rows = c.execute("select id, site from suppliers where site != '' and site is not null "
                     "and site not in (select url from pages)").fetchall()
    rows = [r for r in rows if only_ids is None or r["id"] in only_ids]
    log(f"сайты: {len(rows)} к обходу")

    def job(r):
        try:
            return r, crawl(r["site"])
        except Exception as e:  # noqa: BLE001 — один сломанный сайт не должен ронять сбор
            return r, e

    with ThreadPoolExecutor(6) as ex:
        for i, (r, res) in enumerate(ex.map(job, rows), 1):
            ok = isinstance(res, list) and res
            c.execute("insert or replace into pages values(?,?,?,?)", (r["site"], 200 if ok else 0, "", core.now()))
            if ok:
                core.add_facts(c, r["id"], {"cats": [(x, "") for x in core.categorize(" ".join(p[1] for p in res))]},
                               r["site"], "сайт")
                for url, text, main, name, files in res:
                    core.add_facts(c, r["id"], core.extract(text, main), url, "сайт")
                    for f_url, label in files:
                        core.add_facts(c, r["id"], {"price_list": [(f_url, label)]}, url, "сайт")
                    if name:  # og:site_name / title главной точнее, чем заголовок страницы из выдачи
                        c.execute("update suppliers set name=? where id=?", (name[:80], r["id"]))
                inns = {v for (v,) in c.execute("select value from facts where supplier_id=? and field='inn'",
                                                (r["id"],))}
                if len(inns) == 1:  # несколько ИНН на сайте (агрегатор, партнёры) — не склеиваем
                    inn = inns.pop()
                    other = c.execute("select supplier_id from keys where key=?", ("inn:" + inn,)).fetchone()
                    if other and other[0] != r["id"]:
                        core.merge(c, other[0], r["id"])
                    else:
                        c.execute("insert or ignore into keys values(?,?)", ("inn:" + inn, r["id"]))
                        c.execute("update suppliers set inn=? where id=?", (inn, r["id"]))
            if i % 10 == 0:
                c.commit()
                log(f"сайты: {i}/{len(rows)}")
    c.commit()


# --- DaData -----------------------------------------------------------------------------------------------

def dadata(c, log):
    token = os.environ.get("DADATA_TOKEN")
    if not token:
        log("DaData: нет DADATA_TOKEN — пропускаю")
        return
    rows = c.execute("select s.id, s.name, coalesce(nullif(s.inn, ''), min(f.value)) inn from suppliers s join facts f "
                     "on f.supplier_id=s.id and f.field='inn' where s.legal is null or s.legal='' "
                     "or ((s.city is null or s.city='') and s.legal not like '%НЕ НАЙДЕН%') group by s.id").fetchall()
    for r in rows:
        try:
            sug = cached("dadata:" + r["inn"], lambda: HTTP.post(
                "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
                json={"query": r["inn"]}, headers={"Authorization": f"Token {token}"}).json().get("suggestions") or [])
        except (httpx.HTTPError, ValueError):
            continue
        if not sug:
            c.execute("update suppliers set legal=? where id=?", (json.dumps({"status": "НЕ НАЙДЕН"}), r["id"]))
            continue
        d = sug[0]["data"]
        reg = (d.get("state") or {}).get("registration_date")
        legal = {"status": (d.get("state") or {}).get("status"), "name": sug[0]["value"], "ogrn": d.get("ogrn"),
                 "okved": d.get("okved"), "address": (d.get("address") or {}).get("value"),
                 "years": (datetime.now().year - datetime.fromtimestamp(reg / 1000).year) if reg else None}
        addr = (d.get("address") or {}).get("data") or {}
        c.execute("update suppliers set legal=?, inn=?, name=?, city=coalesce(nullif(city, ''), ?) where id=?",
                  (json.dumps(legal, ensure_ascii=False), r["inn"], core.better_name(r["name"], legal["name"]),
                   addr.get("city") or addr.get("settlement"), r["id"]))
    c.commit()
    log(f"DaData: проверено {len(rows)} ИНН")


# --- сценарии ---------------------------------------------------------------------------------------------

def web(c, cats, city, log):
    """Главный проход: сайты местных поставщиков из Яндекса → обход сайтов → ЕГРЮЛ."""
    for x in cats:
        try:
            yandex_category(c, x, city, log)
        except httpx.HTTPError as e:
            log(f"yandex/{x}: ошибка {e}")
    enrich_sites(c, log)
    dadata(c, log)


def catalogs(c, cats, pages, log):
    """Производители из рубрик productcenter (по всей России) → их сайты → ЕГРЮЛ. Медленно: ~3 мин на рубрику."""
    for catalog in sorted({r for x in cats for r in core.CAT[x].get("productcenter", [])}):
        try:
            productcenter(c, catalog, pages, log)
        except Blocked as e:
            log(f"productcenter: стоп — {e}")
            break
    enrich_sites(c, log)
    dadata(c, log)


def seed(cats, city, pages, log=print):
    c = core.connect()
    web(c, cats, city, log)
    catalogs(c, cats, pages, log)


def live(cat, city, extra, log):
    """Живой поиск из интерфейса: Яндекс по категории (+ уточнение) → сайты → DaData."""
    c = core.connect()
    before = {r[0] for r in c.execute("select id from suppliers")}
    yandex_category(c, cat, city, log, extra, fresh=True)
    new ={r[0] for r in c.execute("select id from suppliers")} - before
    enrich_sites(c, log, only_ids=new)
    dadata(c, log)
    log(f"готово: новых поставщиков {len(new)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["seed", "rebuild", "web", "catalogs", "sites", "dadata"])
    ap.add_argument("--cats", default=",".join(core.CAT))
    ap.add_argument("--city", default="Екатеринбург")
    ap.add_argument("--pages", type=int, default=3)
    a = ap.parse_args()
    cats = a.cats.split(",")
    log = lambda m: print(time.strftime("%H:%M:%S"), m, flush=True)  # noqa: E731
    if a.cmd == "seed":
        seed(cats, a.city, a.pages, log)
    elif a.cmd == "web":
        web(core.connect(), cats, a.city, log)
    elif a.cmd == "catalogs":
        catalogs(core.connect(), cats, a.pages, log)
    elif a.cmd == "rebuild":  # все города сетей, потом каталоги — по очереди: параллельные запуски упираются в блокировку SQLite
        c = core.connect()
        for city in dict.fromkeys(x for v in core.NET_CITIES.values() for x in v):
            web(c, cats, city, log)
        catalogs(c, cats, a.pages, log)
    elif a.cmd == "sites":
        enrich_sites(core.connect(), log)
    else:
        dadata(core.connect(), log)
