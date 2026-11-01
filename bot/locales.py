"""User-facing languages and compact translations for the primary screens.

Every user-facing screen is translated (admin-only screens stay English).
"""
import json
from pathlib import Path
LANGUAGE_NAMES = {
    "fa": "فارسی", "en": "English", "hi": "हिन्दी", "ru": "Русский",
    "id": "Bahasa Indonesia", "pt-BR": "Português (Brasil)", "vi": "Tiếng Việt",
}
NEW = ("hi", "ru", "id", "pt-BR", "vi")
TEXT = {
    "hi": dict(news="आर्थिक समाचार", intro="समाचार देखने के लिए नीचे कोई विकल्प चुनें।", currencies="मुद्राएँ", auto_send="स्वचालित भेजना", zone="समय क्षेत्र", language="भाषा", automatic="स्वचालित", info="समाचार, तारीख और अनुस्मारक इसी समय क्षेत्र का उपयोग करते हैं। मैन्युअल विकल्प भाषा बदलने पर भी सुरक्षित रहता है।", daily="रोज़ाना", before="रिलीज़ से 30 मिनट पहले", off="बंद", channel="मेरे चैनल पर भेजें", back="वापस", admin="एडमिन पैनल", calendar="कैलेंडर", reminders="अनुस्मारक", yesterday="कल (पिछला)", today="आज", tomorrow="कल (अगला)", week="इस सप्ताह", select="मुद्रा और महत्त्व चुनें", all="सभी मुद्राएँ", save="सहेजें और वापस जाएँ"),
    "ru": dict(news="Экономические новости", intro="Выберите период, чтобы посмотреть новости.", currencies="Валюты", auto_send="Автоотправка", zone="Часовой пояс", language="Язык", automatic="Автоматически", info="Новости, даты и напоминания используют этот часовой пояс. Выбранный вручную пояс сохраняется при смене языка.", daily="ежедневно", before="за 30 минут до выхода", off="Выключено", channel="Отправить в мой канал", back="Назад", admin="Панель администратора", calendar="Календарь", reminders="Напоминания", yesterday="Вчера", today="Сегодня", tomorrow="Завтра", week="На этой неделе", select="Выбрать валюты и важность", all="Все валюты", save="Сохранить и вернуться"),
    "id": dict(news="Berita Ekonomi", intro="Pilih periode untuk melihat berita.", currencies="Mata uang", auto_send="Kirim otomatis", zone="Zona waktu", language="Bahasa", automatic="Otomatis", info="Berita, tanggal, dan pengingat menggunakan zona waktu ini. Pilihan manual tetap tersimpan saat bahasa diubah.", daily="setiap hari", before="30 menit sebelum rilis", off="Nonaktif", channel="Kirim ke kanal saya", back="Kembali", admin="Panel admin", calendar="Kalender", reminders="Pengingat", yesterday="Kemarin", today="Hari ini", tomorrow="Besok", week="Minggu ini", select="Pilih mata uang dan dampak", all="Semua mata uang", save="Simpan dan kembali"),
    "pt-BR": dict(news="Notícias econômicas", intro="Escolha um período para ver as notícias.", currencies="Moedas", auto_send="Envio automático", zone="Fuso horário", language="Idioma", automatic="Automático", info="Notícias, datas e lembretes usam este fuso. A escolha manual é mantida ao alterar o idioma.", daily="diariamente", before="30 minutos antes da divulgação", off="Desativado", channel="Enviar para meu canal", back="Voltar", admin="Painel de administração", calendar="Calendário", reminders="Lembretes", yesterday="Ontem", today="Hoje", tomorrow="Amanhã", week="Esta semana", select="Escolher moedas e impacto", all="Todas as moedas", save="Salvar e voltar"),
    "vi": dict(news="Tin tức kinh tế", intro="Chọn khoảng thời gian để xem tin.", currencies="Tiền tệ", auto_send="Gửi tự động", zone="Múi giờ", language="Ngôn ngữ", automatic="Tự động", info="Tin tức, ngày tháng và lời nhắc dùng múi giờ này. Múi giờ chọn thủ công được giữ khi đổi ngôn ngữ.", daily="hằng ngày", before="trước công bố 30 phút", off="Tắt", channel="Gửi đến kênh của tôi", back="Quay lại", admin="Bảng quản trị", calendar="Lịch", reminders="Nhắc nhở", yesterday="Hôm qua", today="Hôm nay", tomorrow="Ngày mai", week="Tuần này", select="Chọn tiền tệ và mức tác động", all="Tất cả tiền tệ", save="Lưu và quay lại"),
}

_DIR = Path(__file__).parent / "languages"
NEWS = {lang: json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8")) for lang in NEW}
