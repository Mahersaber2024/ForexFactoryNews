"""Short "what this does for you" notes shown under the quoted status block of the main screens.

about(language, "news" | "calendar") -> an italic paragraph (with a leading blank line), ready to append to the screen text.
"""
import html

ABOUT = {
    "news": {
        "fa": "خبرهای مهم بازار رو فقط برای ارزهایی که خودت انتخاب کردی می‌بینی، با زمان انتشار به وقت خودت. می‌تونی بذاری هر روز خودکار برات بیاد یا ۳۰ دقیقه قبل از هر خبر هشدار بگیری، تا هیچ خبر مهمی رو از دست ندی و غافلگیر نشی.",
        "en": "See the market-moving news only for the currencies you pick, with release times in your own time zone. Get it automatically every day, or an alert 30 minutes before each release, so no important event catches you off guard.",
        "hi": "सिर्फ़ आपकी चुनी हुई मुद्राओं की बाज़ार को हिलाने वाली ख़बरें, आपके अपने समय क्षेत्र के अनुसार प्रकाशन समय के साथ देखें। इन्हें हर दिन अपने-आप पाएँ या हर ख़बर से 30 मिनट पहले अलर्ट लें, ताकि कोई अहम घटना आपको चौंका न दे।",
        "ru": "Только важные для рынка новости по выбранным вам валютам, со временем выхода в вашем часовом поясе. Получайте их автоматически каждый день или с оповещением за 30 минут до каждой публикации — чтобы ни одно важное событие не застало вас врасплох.",
        "id": "Lihat berita penggerak pasar hanya untuk mata uang pilihanmu, lengkap dengan jam rilis di zona waktumu. Terima otomatis setiap hari atau dapat peringatan 30 menit sebelum tiap rilis, jadi tidak ada kejadian penting yang mengejutkanmu.",
        "pt-BR": "Veja só as notícias que movem o mercado para as moedas que você escolher, com o horário de divulgação no seu fuso. Receba automaticamente todo dia ou com um aviso 30 minutos antes de cada divulgação, para que nenhum evento importante pegue você de surpresa.",
        "vi": "Chỉ xem các tin làm thị trường biến động của những đồng tiền bạn chọn, kèm giờ công bố theo múi giờ của bạn. Nhận tự động mỗi ngày hoặc được báo trước 30 phút mỗi lần công bố, để không sự kiện quan trọng nào làm bạn bất ngờ.",
    },
    "calendar": {
        "fa": "تاریخ امروز، شمسی و میلادی، به‌شکل یک کارت تصویری مرتب. می‌تونی هر روز سر ساعت دلخواهت برات بیاد یا خودکار توی کانال‌هات منتشر بشه، با استایل خودت و بدون اینکه هر روز بسازی و بفرستی.",
        "en": "Today's date, Persian and Gregorian, as a clean picture card. Have it arrive every day at the time you choose, or post itself to your channels in your own style, with no daily work from you.",
        "hi": "आज की तारीख़, फ़ारसी और ग्रेगोरियन दोनों में, एक साफ़-सुथरे चित्र कार्ड के रूप में। इसे हर दिन अपने चुने समय पर पाएँ या अपने स्टाइल में अपने चैनलों में अपने-आप पोस्ट होने दें, रोज़ कुछ करने की ज़रूरत नहीं।",
        "ru": "Сегодняшняя дата — персидская и григорианская — в виде аккуратной карточки-картинки. Она приходит каждый день в выбранное вами время или сама публикуется в ваших каналах в вашем стиле, без ежедневных усилий.",
        "id": "Tanggal hari ini, Persia dan Masehi, dalam kartu gambar yang rapi. Terima tiap hari pada jam pilihanmu atau biarkan terposting sendiri ke channel-mu dengan gayamu, tanpa repot setiap hari.",
        "pt-BR": "A data de hoje, persa e gregoriana, num cartão em imagem bem-feito. Receba todo dia no horário que escolher ou deixe publicar sozinho nos seus canais, no seu estilo, sem trabalho diário.",
        "vi": "Ngày hôm nay, lịch Ba Tư và dương lịch, trong một thẻ hình ảnh gọn gàng. Nhận mỗi ngày vào giờ bạn chọn hoặc để tự đăng lên các kênh của bạn theo phong cách riêng, không phải làm gì mỗi ngày.",
    },
}


def about(language: str, key: str) -> str:
    text = ABOUT[key].get(language) or ABOUT[key]["en"]
    return f"\n\n<i>{html.escape(text, quote=False)}</i>"
