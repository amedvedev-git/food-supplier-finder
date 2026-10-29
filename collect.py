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
import io
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import openpyxl
import pypdf
import trafilatura
import xlrd
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
# какие страницы обходить первыми: условия поставки чаще всего на «доставке и оплате», реже в «о компании»
PRIORITY = [re.compile(rx, re.I) for rx in (
    r"доставк|dostavk|delivery|shipping|оплат|oplat|payment|услови|uslovi",
    r"(?<![а-яё])опт|optov|wholesale|horeca|хорека|сотрудничеств|partner|прайс|price",
    r"контакт|contact|kontakt|реквизит|rekvizit",
    r"сертифик|sertif|certif|документ|document")]
CATALOG = re.compile(r"каталог|catalog|продукци|produkci|ассортимент|assortiment|products?(?![a-z])|shop", re.I)

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


def fetch(url, delay=1.0, raw=False):
    """HTML страницы (raw — байты файла) или None. Пауза на хост, robots.txt, лимит 3 МБ (файл — 10 МБ)."""
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
    if r.status_code != 200 or len(r.content) > (10_000_000 if raw else 3_000_000):
        return None
    if raw:
        return r.content
    if "html" not in r.headers.get("content-type", "html"):
        return None
    if "не робот" in r.text[:20000] or "captcha" in str(r.url):
        raise Blocked(f"{host}: капча")
    return r.text


def price_file(url):
    """Строки прайс-листа (XLSX, XLS, PDF) списками ячеек. В кэше — строки, а не сам файл."""
    return cached("pricelist:" + url, lambda: (lambda b: None if b is None else table_rows(b))(fetch(url, raw=True)))


def table_rows(data: bytes) -> list:
    try:
        if data[:4] == b"%PDF":  # layout сохраняет колонки отступами
            return [re.split(r"\s{2,}", ln.strip()) for pg in pypdf.PdfReader(io.BytesIO(data)).pages[:40]
                    for ln in (pg.extract_text(extraction_mode="layout") or "").splitlines() if ln.strip()]
        if data[:2] == b"PK":
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            return [["" if x is None else str(x) for x in r] for ws in wb.worksheets
                    for r in ws.iter_rows(values_only=True)][:20000]
        if data[:4] == b"\xd0\xcf\x11\xe0":
            book = xlrd.open_workbook(file_contents=data)
            return [[str(x) for x in sh.row_values(i)] for sh in book.sheets() for i in range(sh.nrows)][:20000]
    except Exception:  # noqa: BLE001 — битый или зашифрованный файл: пустой результат кэшируется, повторно не качаем
        pass
    return []


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
    """Главная + до 6 страниц (сначала доставка и оплата, опт, контакты, документы, о компании) + страница каталога.
    → [(url, весь текст, основной контент, название, ссылки на прайсы)]"""
    html = get(site)
    if not html:
        return []
    tree = HTMLParser(html)
    og = tree.css_first('meta[property="og:site_name"]')
    title = tree.css_first("title")
    name = core.pick_name((og.attributes.get("content") or "") if og else (title.text() if title else ""), "")
    pages, seen, files = [], {site.rstrip("/")}, []
    cand, shop = [], []
    for a in tree.css("a[href]"):
        href, label = a.attributes.get("href") or "", a.text(strip=True)
        u = urljoin(site + "/", href).split("#")[0]
        if core.domain(u) != core.domain(site):
            continue
        if re.search(r"\.(pdf|xlsx?|docx?)$", u, re.I) and re.search(r"прайс|price|каталог|catalog", u + label, re.I):
            files.append((u, label or "прайс-лист"))
        elif u.rstrip("/") not in seen and not re.search(r"\.\w{2,4}$", u):
            for rx, lst in ((LINKS, cand), (CATALOG, shop)):
                if rx.search(href + " " + label):
                    seen.add(u.rstrip("/"))
                    lst.append((next((i for i, p in enumerate(PRIORITY) if p.search(href + " " + label)), 9), u))
                    break
    pages.append((site, text_of(tree), main_of(html), name))
    cand.sort(key=lambda x: x[0])  # sort устойчивая: внутри приоритета — порядок ссылок на странице
    for _, u in cand[:6] + shop[:1]:  # страница каталога — там цены за кг у интернет-магазинов опта
        h = get(u)
        if h:
            pages.append((u, text_of(HTMLParser(h)), main_of(h), ""))
    return [(*p, files if i == 0 else []) for i, p in enumerate(pages)]


_extract_lock = threading.Lock()


def main_of(html):
    """Основной контент страницы без меню, боковых блоков и футера (алгоритм класса Readability).
    По одному потоку: параллельный trafilatura (lxml) портит кучу — «malloc_consolidate(): unaligned fastbin chunk»
    и SEGV на сервере при обходе из кэша. Разбор быстрый, параллельной остаётся сеть."""
    with _extract_lock:
        return trafilatura.extract(html, include_tables=True, favor_recall=True) or ""


def enrich_sites(c, log, only_ids=None, again=False):
    """again — обойти и уже обойдённые сайты (новые правила; страницы берутся из кэша, факты только добавляются)."""
    rows = c.execute("select id, site from suppliers where site != '' and site is not null" +
                     ("" if again else " and site not in (select url from pages)")).fetchall()
    rows = [r for r in rows if only_ids is None or r["id"] in only_ids]
    log(f"сайты: {len(rows)} к обходу")

    def job(r):
        try:
            res = crawl(r["site"])
            return r, res, {u: price_file(u) for p in res for u, _ in p[4][:3]}
        except Exception as e:  # noqa: BLE001 — один сломанный сайт не должен ронять сбор
            return r, e, {}

    with ThreadPoolExecutor(6) as ex:
        for i, (r, res, prices) in enumerate(ex.map(job, rows), 1):
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
                for f_url, table in prices.items():
                    core.add_facts(c, r["id"], core.price_list(table or []), f_url, "прайс")
                    # в шапке прайса — реквизиты и контакты: ИНН, телефон, email (условия — только из позиций)
                    head = "\n".join(" ".join(x for x in row if x) for row in (table or [])[:40])
                    core.add_facts(c, r["id"], {k: v for k, v in core.extract(head, "").items()
                                                if k in ("inn", "phone", "email")}, f_url, "прайс")
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


# --- Росаккредитация --------------------------------------------------------------------------------------

FSA_URL = "https://pub.fsa.gov.ru/rds/declaration/view/{}/common"
FSA_ACTIVE = 6
_MAKER = re.compile(r'"manufacturerName":\s*"((?:[^"\\]|\\.)*)"')


def fsa(c, path, log):
    """Действующие декларации о соответствии из открытых данных Росаккредитации (fsa.gov.ru/opendata, rds-full.7z,
    ~15 ГБ JSONL). В наборе нет ИНН: сверяем название изготовителя с юрназванием из ЕГРЮЛ и регион адреса.
    Импортёров и дистрибьюторов так не найти — только производителей. Архив распаковывается потоком."""
    names = {}
    for sid, legal in c.execute("select id, legal from suppliers where legal like '%\"name\"%'"):
        d = json.loads(legal)
        key, region = core.org_key(d.get("name")), core.region_keys(d.get("address"))
        if len(key) >= 4 and region:  # без региона совпадение по одному названию слишком ненадёжно
            names.setdefault(key, []).append((sid, region))
    log(f"fsa: ищем декларации {sum(map(len, names.values()))} поставщиков с юрназванием и адресом")
    today, found, n = core.now()[:10], {}, 0
    raw = str(path).endswith(".jsonl")  # распакованный файл (или выборка для проверки) — без 7z
    proc = None if raw else subprocess.Popen(["7z", "x", "-so", str(path)], stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL)
    lines = open(path, encoding="utf-8", errors="replace") if raw else \
        io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
    for n, line in enumerate(lines, 1):
        m = _MAKER.search(line)
        hits = m and names.get(core.org_key(m.group(1)))
        if hits:
            d = json.loads(line)
            # «действует» в выгрузке — idStatus 6 (у статуса 1 срок истёк у 11 тыс. из 11 тыс. совпадений, 29.10.2026)
            if d.get("idStatus") != FSA_ACTIVE or (d.get("endDate") or "9999") < today:
                continue
            addr = (d.get("manufacturerAddress") or "").lower().replace("ё", "е")
            for sid, region in hits:
                if any(r in addr for r in region):
                    found.setdefault(sid, []).append(d)
        if n % 2_000_000 == 0:
            log(f"fsa: {n // 1_000_000} млн записей, найдены декларации у {len(found)} поставщиков")
    if proc and proc.wait() != 0:
        raise RuntimeError(f"7z завершился с ошибкой {proc.returncode}")
    c.execute("delete from facts where field='decl'")  # снимок реестра целиком: истёкшие уходят
    for sid, ds in found.items():
        for d in sorted(ds, key=lambda d: d.get("regDate") or "", reverse=True)[:5]:
            prod = next((p.get("productName") for p in d.get("products") or [] if p.get("productName")), "")
            value = f"{d['numberDoc']} — {core._cut(prod or d.get('techRegs') or '', 90)}"
            quote = (f"Действует до {d.get('endDate') or 'бессрочно'}, зарегистрирована {d.get('regDate')}. "
                     f"{core._cut(d.get('techRegs') or '', 120)}. Изготовитель: {d.get('manufacturerName')}, "
                     f"{core._cut(d.get('manufacturerAddress') or '', 90)}")
            core.add_facts(c, sid, {"decl": [(value, quote)]}, FSA_URL.format(d["id"]), "Росаккредитация")
    c.commit()
    log(f"fsa: просмотрено записей {n}, действующие декларации у {len(found)} поставщиков")


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
    ap.add_argument("cmd", choices=["seed", "rebuild", "web", "catalogs", "sites", "dadata", "fsa"])
    ap.add_argument("--cats", default=",".join(core.CAT))
    ap.add_argument("--city", default="Екатеринбург")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--again", action="store_true", help="sites: обойти заново и уже обойдённые сайты")
    ap.add_argument("--file", default=str(core.ROOT / "data" / "fsa" / "rds.7z"), help="fsa: архив rds-full.7z")
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
        for city in core.CITIES:
            web(c, cats, city, log)
        catalogs(c, cats, a.pages, log)
    elif a.cmd == "sites":
        enrich_sites(core.connect(), log, again=a.again)
    elif a.cmd == "fsa":
        fsa(core.connect(), a.file, log)
    else:
        dadata(core.connect(), log)
