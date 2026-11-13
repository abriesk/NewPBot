"""Time-zone names a client can read: a city in their language and the offset from GMT.

The select on /book used to show raw IANA ids or the practice's English labels; "Asia/Yerevan" is not something a
client in Gyumri recognises. The `value` of an option stays the IANA id (that is what is stored and what the server
converts with), only the label is localised: "Ереван (GMT+4)".

The table lists the zones a client of this practice is likely to be in: the practice's own seeded options and the places
its clients travel from. A zone not in the table falls back to the city part of its IANA id, in Latin letters, which is
better than a raw id and never wrong. The ru and hy names are drafts for a native check (`# TODO: review`).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# iana: (en, ru, hy)
CITIES: dict[str, tuple[str, str, str]] = {
    "Asia/Yerevan": ("Yerevan", "Ереван", "Երևան"),
    "Asia/Tbilisi": ("Tbilisi", "Тбилиси", "Թբիլիսի"),
    "Asia/Baku": ("Baku", "Баку", "Բաքու"),
    "Asia/Dubai": ("Dubai", "Дубай", "Դուբայ"),
    "Asia/Tehran": ("Tehran", "Тегеран", "Թեհրան"),
    "Asia/Almaty": ("Almaty", "Алматы", "Ալմաթի"),
    "Asia/Tashkent": ("Tashkent", "Ташкент", "Տաշքենդ"),
    "Asia/Jerusalem": ("Jerusalem", "Иерусалим", "Երուսաղեմ"),
    "Asia/Beirut": ("Beirut", "Бейрут", "Բեյրութ"),
    "Asia/Bangkok": ("Bangkok", "Бангкок", "Բանգկոկ"),
    "Asia/Shanghai": ("Shanghai", "Шанхай", "Շանհայ"),
    "Asia/Tokyo": ("Tokyo", "Токио", "Տոկիո"),
    "Europe/Moscow": ("Moscow", "Москва", "Մոսկվա"),
    "Europe/Minsk": ("Minsk", "Минск", "Մինսկ"),
    "Europe/Istanbul": ("Istanbul", "Стамбул", "Ստամբուլ"),
    "Europe/Kyiv": ("Kyiv", "Киев", "Կիև"),
    "Europe/Kiev": ("Kyiv", "Киев", "Կիև"),
    "Europe/Bucharest": ("Bucharest", "Бухарест", "Բուխարեստ"),
    "Europe/Athens": ("Athens", "Афины", "Աթենք"),
    "Europe/Berlin": ("Berlin", "Берлин", "Բեռլին"),
    "Europe/Paris": ("Paris", "Париж", "Փարիզ"),
    "Europe/Madrid": ("Madrid", "Мадрид", "Մադրիդ"),
    "Europe/Rome": ("Rome", "Рим", "Հռոմ"),
    "Europe/Warsaw": ("Warsaw", "Варшава", "Վարշավա"),
    "Europe/Prague": ("Prague", "Прага", "Պրահա"),
    "Europe/Vienna": ("Vienna", "Вена", "Վիեննա"),
    "Europe/Amsterdam": ("Amsterdam", "Амстердам", "Ամստերդամ"),
    "Europe/Brussels": ("Brussels", "Брюссель", "Բրյուսել"),
    "Europe/Zurich": ("Zurich", "Цюрих", "Ցյուրիխ"),
    "Europe/London": ("London", "Лондон", "Լոնդոն"),
    "Europe/Dublin": ("Dublin", "Дублин", "Դուբլին"),
    "Europe/Lisbon": ("Lisbon", "Лиссабон", "Լիսաբոն"),
    "America/New_York": ("New York", "Нью-Йорк", "Նյու Յորք"),
    "America/Toronto": ("Toronto", "Торонто", "Տորոնտո"),
    "America/Chicago": ("Chicago", "Чикаго", "Չիկագո"),
    "America/Denver": ("Denver", "Денвер", "Դենվեր"),
    "America/Los_Angeles": ("Los Angeles", "Лос-Анджелес", "Լոս Անջելես"),
    "America/Vancouver": ("Vancouver", "Ванкувер", "Վանկուվեր"),
    "UTC": ("UTC", "UTC", "UTC"),
}
_LANG_INDEX = {"en": 0, "ru": 1, "hy": 2}


def city_name(iana: str, lang: str) -> str:
    """The city in `lang`; for an unlisted zone, the last part of its IANA id."""
    names = CITIES.get(iana)
    if names is not None:
        return names[_LANG_INDEX.get(lang, 0)]
    return iana.rsplit("/", 1)[-1].replace("_", " ")


def gmt_offset(iana: str, at: datetime) -> str:
    """`GMT+4`, `GMT-5`, `GMT+5:30`, `GMT` -- the offset in force at `at` (daylight saving included)."""
    delta = at.astimezone(ZoneInfo(iana)).utcoffset()
    minutes = int(delta.total_seconds() // 60) if delta is not None else 0
    if minutes == 0:
        return "GMT"
    sign = "+" if minutes > 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"GMT{sign}{hours}" + (f":{rest:02d}" if rest else "")


def zone_label(iana: str, lang: str, at: datetime) -> str:
    """`Ереван (GMT+4)` -- what a client reads next to a time or in the zone select."""
    return f"{city_name(iana, lang)} ({gmt_offset(iana, at)})"
