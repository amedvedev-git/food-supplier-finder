"""Проверки чистой логики: извлечение фактов из текста и рейтинг. Запуск: python test_core.py"""
from core import extract, inn_valid, norm_phone, score, categorize, pick_name, host

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
    v, q = f["delivery"][0]
    assert "быструю доставку" in v and "Богишамол" not in v and len(q) < 230, (v, q)
    f = extract("Минимальный заказ (от 1000р) За 1-2 дня до доставки менеджеры согласовывают время")
    assert f["moq"][0][0] == "от 1000р", f["moq"]


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
