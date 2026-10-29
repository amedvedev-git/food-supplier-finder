"""Проверки чистой логики: извлечение фактов из текста и рейтинг. Запуск: python test_core.py"""
from core import extract, inn_valid, norm_phone, score, focus, categorize, pick_name, better_name, host, price_list, price_ok

# Реальное объявление с agroserver (29.10.2026)
AGRO = ("Предлагаем к оптовой продаже минтай размера 25+ по ГОСТ. Производитель: Крабозаводск / Ясный. "
        "Фасовка: коробка по 20 кг. Цена: 255 руб./кг. Минимальный заказ: от 10 тонн. "
        "Самовывоз: склад в Екатеринбурге. Свяжитесь с нами для согласования")

SITE = ("ООО «Рыбный двор». ИНН 7707083893, ОГРН 1027700132195. Телефон: 8 (343) 222-33-44, "
        "e-mail: sale@fish-ekb.ru. Работаем с HoReCa. Доставка по Екатеринбургу бесплатно при заказе от 15 000 руб. "
        "Отгрузка по всей России транспортными компаниями. Вся продукция сопровождается ветеринарными "
        "свидетельствами (ФГИС «Меркурий») и декларациями соответствия. Внедрена система ХАССП. "
        "Минимальная сумма заказа — 5 000 ₽.")


def test_inn():
    assert inn_valid("7707083893")          # Сбербанк
    assert inn_valid("7736207543")          # Яндекс
    assert not inn_valid("7707083894")
    assert not inn_valid("1234567890")


def test_phone():
    assert norm_phone("+7 (900) 000-00-00") == "+79000000000"
    assert norm_phone("8 (343) 222-33-44") == "+73432223344"
    assert norm_phone("12-34") is None


def test_extract_agro():
    f = extract(AGRO)
    assert f["moq"][0][0] == "от 10 тонн", f["moq"]
    assert f["price"][0][0] == "255 руб./кг", f["price"]
    assert "Самовывоз" in f["delivery"][0][1]
    assert "ГОСТ" in [v for v, _ in f["docs"]]
    assert "Екатеринбург" in [v for v, _ in f["geo"]]


def test_extract_site():
    f = extract(SITE)
    assert [v for v, _ in f["inn"]] == ["7707083893"]
    assert [v for v, _ in f["phone"]] == ["+73432223344"]
    assert [v for v, _ in f["email"]] == ["sale@fish-ekb.ru"]
    assert f["moq"][0][0] == "5 000 ₽", f["moq"]
    docs = {v for v, _ in f["docs"]}
    assert {"Ветеринарные документы (Меркурий)", "Декларация соответствия", "ХАССП"} <= docs, docs
    assert {"по России", "Екатеринбург"} <= {v for v, _ in f["geo"]}
    assert f["wholesale"]
    assert any("15 000" in q for _, q in f["delivery"])


def test_extract_long_nav_text_and_moq_tail():
    # текст сайта без точек: меню, адрес, телефоны и доставка одним куском
    nav = ("Каталог Рыба Красная рыба Белая рыба Филе Богишамол, 57 Показать на карте 8 (800) 000-00-00 "
           "office@example.ru Международные оптовые поставки свежей рыбы и морепродуктов "
           "Наша логистика обеспечивает быструю доставку и оптимальные условия хранения Бренды Статьи "
           "Получить прайс-лист на рыбу После отправки мы вышлем Вам прайс по указанному номеру")
    f = extract(nav)
    assert f["delivery"] == [], f["delivery"]  # упоминание доставки без условий — не факт
    f = extract("Минимальный заказ (от 1000р) За 1-2 дня до доставки менеджеры согласовывают время")
    assert f["moq"][0][0] == "от 1000р", f["moq"]


def test_delivery_is_conditions_not_mention():
    f = extract(SITE)
    assert {"по городу", "бесплатно от 15 000 ₽", "по России", "через ТК"} <= {v for v, _ in f["delivery"]}, f["delivery"]
    assert any("15 000" in q for v, q in f["delivery"] if v == "бесплатно от 15 000 ₽")
    f = extract("Бесплатная доставка при заказе от 5 тыс. руб., самовывоз со склада")
    assert {v for v, _ in f["delivery"]} == {"бесплатно от 5 000 ₽", "самовывоз"}, f["delivery"]


def test_moq_needs_number_and_unit():
    assert extract("Минимальная сумма заказа составляет 100.000 рублей.")["moq"][0][0] == "100.000 рублей"
    assert extract("Минимальная сумма заказа всего от 6500 рублей")["moq"][0][0] == "от 6500 рублей"
    assert extract("Минимальный заказ? Уточняйте у менеджера")["moq"] == []
    assert extract("Минимальная сумма заказа и условия доставки уточняйте")["moq"] == []


def test_conditions_from_main_content_contacts_from_whole_page():
    page = "Меню Доставка и оплата Каталог Телефон 8 (343) 222-33-44 Лосось 29 900 руб./кг"
    main = "Доставка по Екатеринбургу собственным транспортом, на следующий день."
    f = extract(page + " " + main, main)
    assert [v for v, _ in f["phone"]] == ["+73432223344"]
    assert f["price"] == []  # цена из меню и боковых блоков не берётся
    assert {v for v, _ in f["delivery"]} == {"по городу", "свой транспорт", "на следующий день"}, f["delivery"]


def test_price_counts_only_for_category_product():
    s = {"city": "", "offers": [{"category": "salmon", "source": "yandex"}], "legal": {},
         "facts": extract("Каракатица целая 1032 руб./кг. Филе лосося охлаждённое 1890 руб./кг. "
                          "Лосось слабосолёный 29 900 руб./кг.")}
    focus(s, "salmon", "Екатеринбург")
    assert [v for v, _ in s["facts"]["price"]] == ["1890 руб./кг"], s["facts"]["price"]
    assert s["cat"] == "salmon" and "цена" not in s["miss"]


def test_better_name():
    assert better_name("prod66.ru", 'ООО "ОБИЛИЕ"') == 'ООО "ОБИЛИЕ"'
    assert better_name("Атлант Групп", 'ООО "АТЛАНТ-ФИШ"') == "Атлант Групп"
    assert better_name("рыба66.рф", "") == "рыба66.рф"


def test_extract_ignores_invalid_inn_and_restaurant():
    f = extract("ИНН 1234567890. Доставка роллов и пиццы за 60 минут, наше меню")
    assert f["inn"] == []
    assert f["restaurant"]
    assert not f["wholesale"]


def test_pick_name():
    assert pick_name("Гросс Фуд — оптовые поставки продуктов питания", "grossfood.ru") == "Гросс Фуд"
    assert pick_name("Красная рыба оптом | Defa group", "defagroup.com") == "Defa group"
    assert pick_name("Лосось купить оптом по низким ценам с доставкой от поставщика", "gfc-russia.ru") == "gfc-russia.ru"
    assert pick_name("Главная", "tara.ru") == "tara.ru"
    # слоганы и названия товаров — не имя компании
    assert pick_name("Охлажденная рыба оптом — лосось, форель, сибас, дорадо", "ekorfish.com") == "ekorfish.com"
    assert pick_name("SuomiFish — c нами не заштормит", "suomifish.ru") == "SuomiFish"
    assert pick_name("Рис для суши Российский Сэн Сой", "aidigo-shop.ru") == "aidigo-shop.ru"
    assert host("http://xn--66-6kcd8e2c.xn--p1ai") == "рыба66.рф"


def test_categorize():
    assert categorize("Филе лосося охлажденное, Норвегия") == ["salmon"]
    assert "rice" in categorize("Рис для суши Камолино, мешок 25 кг")
    assert "rice" not in categorize("Рисунок на коробке")


def test_score_orders_complete_local_supplier_first():
    local = {"city": "Екатеринбург", "facts": extract(SITE), "offers": [{"category": "salmon", "source": "agroserver"}],
             "legal": {"status": "ACTIVE", "years": 12}}
    far = {"city": "Москва", "facts": extract("Продаём рыбу."), "offers": [{"category": "salmon", "source": "yandex"}],
           "legal": {}}
    s1, why1, miss1 = score(local, "salmon", "Екатеринбург")
    s2, why2, miss2 = score(far, "salmon", "Екатеринбург")
    assert s1 > s2 and 0 <= s2 <= s1 <= 100, (s1, s2)
    assert "цена" in miss1 and "цена" in miss2  # у сайта нет цены за кг
    assert any("Екатеринбург" in w for _, w in why1), why1


def test_score_city_list_is_not_local():
    # федеральный поставщик перечисляет Екатеринбург среди многих городов — это доставка, не местный
    cities = "Доставка в Москва, Екатеринбург, Пермь, Санкт-Петербург, Челябинск, Тюмень."
    fed = {"city": "Москва", "facts": extract(cities), "offers": [{"category": "salmon", "source": "yandex"}],
           "legal": {}}
    loc = {"city": "Екатеринбург", "facts": extract("Доставка по Екатеринбургу."),
           "offers": [{"category": "salmon", "source": "yandex"}], "legal": {}}
    geo = lambda s: [p for p, w in score(s, "salmon", "Екатеринбург")[1] if "регион" in w]  # noqa: E731
    assert geo(fed) == [10] and geo(loc) == [20], (geo(fed), geo(loc))


def test_score_other_network_cities():
    ufa = {"city": "", "facts": extract("Склад в Уфе, доставка по Республике Башкортостан."),
           "offers": [{"category": "salmon", "source": "yandex"}], "legal": {}}
    tmn = {"city": "Тюмень", "facts": extract("Продаём рыбу."), "offers": [{"category": "salmon", "source": "yandex"}],
           "legal": {}}
    geo = lambda s, city: [p for p, w in score(s, "salmon", city)[1] if "регион" in w]  # noqa: E731
    assert geo(ufa, "Уфа") == [20] and geo(tmn, "Тюмень") == [20] and geo(ufa, "Екатеринбург") == []


def test_okved_retail_and_wholesale():
    def pts(okved):
        s = {"city": "Екатеринбург", "facts": extract("Продаём рыбу."), "offers": [{"category": "salmon", "source": "yandex"}],
             "legal": {"status": "ACTIVE", "okved": okved}}
        return [p for p, w in score(s, "salmon", "Екатеринбург")[1] if "ОКВЭД" in w]
    assert pts("46.38") == [5] and pts("10.20") == [5] and pts("47.91") == [-10] and pts("56.10") == [-20], \
        (pts("46.38"), pts("47.91"))
    assert pts("") == []


def test_price_list():
    # строки реальных прайсов (prod66.ru XLSX и defagroup.com PDF, 29.10.2026)
    xlsx = [["Наименование ТМЦ", "Ед.изм.", "Розничная цена", "Валюта", "Мелкий опт цена", "Валюта"],
            ["ГОЛЕЦ н/р 40+ Камчатпродукт штуч.замор.1*25", "кг.", "875", "РУБ", "825", "РУБ"],
            ["Горбуша ПСГ штучной замороз.  1*11*2*22", "кг."],                     # без цены
            ["Кижуч б/г Камчатка 1,8-2,0кг. 1*10*2*20", "кг.", "830", "РУБ", "795", "РУБ"],
            ["Минтай б/г", "кг.", "250", "РУБ", "230", "РУБ"]]                     # голец, минтай — не категории
    got = price_list(xlsx)["price"]
    assert [v for v, _ in got] == ["795 ₽/кг — Кижуч б/г Камчатка 1,8-2,0кг. 1*10*2*20"], got  # оптовая колонка
    pdf = [["№", "Наименование товара", "кг", "Цена", "Производитель"],
           ["18", "Стейк форели г/к", "1", "1 950 ₽", "Московская обл."],
           ["3", "Треска г/к", "1", "под заказ", "Московская обл."]]
    assert [v for v, _ in price_list(pdf)["price"]] == ["1 950 ₽ — Стейк форели г/к"], price_list(pdf)
    line = [["БРЮШКИ форели слабосол.500гр.1*0,5*20*10 пачка 325,00 РУБ 310,00 РУБ"]]  # PDF без колонок
    assert [v for v, _ in price_list(line)["price"]] == ["310 ₽/уп — БРЮШКИ форели слабосол.500гр.1*0,5*20*10"]
    v, q = price_list(line)["price"][0]
    assert price_ok(v, q, "salmon") and not price_ok(v, q, "rice")
    v, q = "25 ₽/уп — Приправа д/говядины 15гр. 1*40", "Приправа д/говядины 15гр. 1*40"
    assert not price_ok(v, q, "meat")  # товар позиции — приправа, а не говядина


def test_score_max_is_100():
    s = {"city": "Екатеринбург", "facts": extract(SITE + " Филе лосося 1890 руб./кг."),
         "offers": [{"category": "salmon", "source": "productcenter"}],
         "legal": {"status": "ACTIVE", "years": 12, "okved": "46.38"}}
    assert score(s, "salmon", "Екатеринбург")[0] == 100, score(s, "salmon", "Екатеринбург")


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
