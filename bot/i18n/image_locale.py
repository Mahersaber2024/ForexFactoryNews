"""Fixed UI labels for rendered news images, not Telegram messages or event names."""
import re

SUPPORTED = ("hi", "ru", "id", "pt-BR", "vi")
_LABELS = {
    "hi": ("आर्थिक कैलेंडर", "आज", "पूर्वानुमान", "पिछला", "वास्तविक", "समय", "मुद्रा", "समाचार", "महत्त्व", "विशेष समाचार", "स्रोत", "कम", "मध्यम", "अधिक", "दिन की स्थिति", "भाषण", "सम्मेलन", "आर्थिक समाचार", "समाचार", "अधिक महत्त्व"),
    "ru": ("Экономический календарь", "Сегодня", "Прогноз", "Предыдущее", "Факт", "Время", "Валюта", "Событие", "Важность", "Главное событие", "Источник", "Низкая", "Средняя", "Высокая", "События дня", "Выступление", "Конференция", "Экономические новости", "событий", "Высокая важность"),
    "id": ("Kalender Ekonomi", "Hari ini", "Perkiraan", "Sebelumnya", "Aktual", "Waktu", "Mata uang", "Peristiwa", "Dampak", "Sorotan", "Sumber", "Rendah", "Sedang", "Tinggi", "Peristiwa hari ini", "Pidato", "Konferensi", "Berita ekonomi", "peristiwa", "Dampak tinggi"),
    "pt-BR": ("Calendário econômico", "Hoje", "Previsão", "Anterior", "Real", "Horário", "Moeda", "Evento", "Impacto", "Destaque", "Fonte", "Baixo", "Médio", "Alto", "Eventos do dia", "Discurso", "Conferência", "Notícias econômicas", "eventos", "Alto impacto"),
    "vi": ("Lịch kinh tế", "Hôm nay", "Dự báo", "Kỳ trước", "Thực tế", "Thời gian", "Tiền tệ", "Sự kiện", "Tác động", "Tin nổi bật", "Nguồn", "Thấp", "Trung bình", "Cao", "Tin trong ngày", "Phát biểu", "Hội nghị", "Tin tức kinh tế", "sự kiện", "Tác động cao"),
}
_KEYS = ("calendar", "today", "forecast", "previous", "actual", "time", "currency", "event", "impact", "spotlight", "source", "low", "medium", "high", "pulse", "speech", "conference", "news", "events", "high_impact")
CATALOG = {lang: dict(zip(_KEYS, values)) for lang, values in _LABELS.items()}
_EXACT = {
    "ECONOMIC CALENDAR": "calendar", "ECONOMIC CALENDAR  ·  DAILY PASS": "calendar",
    "// ECON.CALENDAR  ·  LIVE": "calendar", "TODAY": "today",
    "FORECAST": "forecast", "FCST": "forecast", "F": "forecast",
    "PREVIOUS": "previous", "PREV": "previous", "P": "previous", "ACTUAL": "actual", "ACT": "actual",
    "TIME": "time", "CUR": "currency", "EVENT": "event", "EVENTS": "events",
    "IMPACT": "impact", "SPOTLIGHT": "spotlight", "ADMIT": "event", "Source": "source",
    "Low": "low", "Medium": "medium", "High": "high", "DAY PULSE": "pulse",
    "SPEECH": "speech", "CONFERENCE": "conference", "Forex Economic News": "news",
    "Economic Calendar": "calendar", "High impact": "high_impact",
}
_EXACT_LOWER = {k.lower(): v for k, v in _EXACT.items() if len(k) > 1}
MONTHS = {
    "hi": "जनवरी फ़रवरी मार्च अप्रैल मई जून जुलाई अगस्त सितंबर अक्टूबर नवंबर दिसंबर".split(),
    "ru": "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(),
    "id": "Januari Februari Maret April Mei Juni Juli Agustus September Oktober November Desember".split(),
    "pt-BR": "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro".split(),
    "vi": [f"tháng {i}" for i in range(1, 13)],
}
DAYS = {
    "hi": "सोमवार मंगलवार बुधवार गुरुवार शुक्रवार शनिवार रविवार".split(),
    "ru": "понедельник вторник среда четверг пятница суббота воскресенье".split(),
    "id": "Senin Selasa Rabu Kamis Jumat Sabtu Minggu".split(),
    "pt-BR": "segunda-feira terça-feira quarta-feira quinta-feira sexta-feira sábado domingo".split(),
    "vi": ["thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật"],
}
MON_KEYS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAY_KEYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Words for time cells that are not a clock time, the impact phrase and the names of the 8 designs.
EXTRA = {
    "hi": dict(count="{n} {x}", allday="पूरे दिन", tentative="अनिश्चित", impact="{x} महत्त्व"),
    "ru": dict(count="Важных: {n}", allday="Весь день", tentative="Ориентировочно", impact="{x} важность"),
    "id": dict(count="{n} {x}", allday="Seharian", tentative="Tentatif", impact="Dampak {x}"),
    "pt-BR": dict(count="{n} de {x}", allday="Dia todo", tentative="Previsto", impact="Impacto {x}"),
    "vi": dict(count="{n} {x}", allday="Cả ngày", tentative="Dự kiến", impact="Tác động {x}"),
}
STYLE_NAMES = {   # design key -> name per language (the English names stay in code for fa/en)
    "hi": dict(timeline="टाइमलाइन", bento="बेंटो", editorial="सॉफ्ट लेजर", hud="HUD", ticket="टिकट",
               spotlight="स्पॉटलाइट", aurora_light="ऑरोरा लाइट", aurora="ऑरोरा डार्क"),
    "ru": dict(timeline="Хронология", bento="Бенто", editorial="Мягкий реестр", hud="Табло", ticket="Билет",
               spotlight="Прожектор", aurora_light="Аврора светлая", aurora="Аврора тёмная"),
    "id": dict(timeline="Linimasa", bento="Bento", editorial="Buku Besar Lembut", hud="HUD", ticket="Tiket",
               spotlight="Sorotan", aurora_light="Aurora Terang", aurora="Aurora Gelap"),
    "pt-BR": dict(timeline="Linha do tempo", bento="Bento", editorial="Livro-razão suave", hud="HUD", ticket="Ingresso",
                  spotlight="Destaque", aurora_light="Aurora clara", aurora="Aurora escura"),
    "vi": dict(timeline="Dòng thời gian", bento="Bento", editorial="Sổ cái nhẹ", hud="HUD", ticket="Vé",
               spotlight="Tiêu điểm", aurora_light="Aurora sáng", aurora="Aurora tối"),
}


def style_name(lang, key, default):
    return STYLE_NAMES.get(lang, {}).get(key, default)


def label(lang, key):
    return CATALOG[lang][key]

def translate(lang, text):
    """Only translate fixed labels and known templates; leave free-form events unchanged."""
    catalog = CATALOG[lang]
    key = _EXACT.get(text)
    if key:
        return catalog[key]
    shout = text.isupper() and lang != "hi"       # the HUD / ticket designs draw their labels in capitals
    lowered = _EXACT_LOWER.get(text.lower())
    if lowered:
        out = catalog[lowered]
        return out.upper() if shout else out
    extra = EXTRA[lang]
    if text.lower() == "all day":
        return extra["allday"].upper() if shout else extra["allday"]
    if text.lower() == "tentative":
        return extra["tentative"].upper() if shout else extra["tentative"]
    match = re.fullmatch(r"(?i)(high|medium|low) impact", text)
    if match:
        level = match.group(1).lower()
        word = catalog["high_impact"] if level == "high" else extra["impact"].format(
            x=catalog[level] if lang in ("hi", "ru") else catalog[level].lower())
        return word.upper() if shout else word
    match = re.fullmatch(r"(?i)(\d+) high impact", text)
    if match:
        word = EXTRA[lang]["count"].format(n=match.group(1), x=catalog["high_impact"].lower() if lang != "hi" else catalog["high_impact"])
        return word.upper() if shout else word
    match = re.fullmatch(r"(\d+) (?:events?|EVENTS?)", text)
    if match:
        return f"{match.group(1)} {catalog['events']}"
    match = re.fullmatch(r"\[ (\d+) EVENTS? \]", text)
    if match:
        return f"[ {match.group(1)} {catalog['events']} ]"
    return text

def date(lang, wd, mon, day):
    try:
        weekday = DAYS[lang][DAY_KEYS.index(wd.title()[:3])]
        month = MONTHS[lang][MON_KEYS.index(mon.title()[:3])]
    except (ValueError, KeyError):
        return " ".join(filter(None, (wd, mon, day)))
    if lang == "pt-BR":
        return f"{weekday}, {day} de {month}"
    return f"{weekday}, {day} {month}"

def zone(lang, city, abbreviation):
    """Format a known zone. The caller must resolve the zone separately."""
    plain = {
        "hi": f"{city} का समय", "ru": f"Время: {city}",
        "id": f"Waktu {city}", "pt-BR": f"Horário de {city}",
        "vi": f"Giờ {city}",
    }[lang]
    sub = f"{plain} ({abbreviation})"
    return dict(sub=sub, caps=sub, mono=sub, plain=plain)
