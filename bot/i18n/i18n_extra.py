"""Small helpers to add translated texts next to the code that uses them (the big tables stay untouched)."""
from bot.i18n import language as i18n


def X(en: str, hi: str, ru: str, id_: str, pt: str, vi: str) -> str:
    """Registers the translations of a new English text (hi, ru, id, pt-BR, vi) and returns the English text.
    A translation that already exists is kept."""
    sid = i18n._BY_EN.get(en)
    if sid is None:
        sid = f"r{len(i18n.SOURCE) + 1:04d}"
        i18n.SOURCE[sid] = en
        i18n._BY_EN[en] = sid
    for lang, value in zip(i18n.NEW, (hi, ru, id_, pt, vi)):
        i18n.TRANSLATIONS.setdefault(lang, {}).setdefault(sid, value)
    return en


def derive(old_en: str, new_en: str, edit) -> str:
    """The new text gets the old text's translations, each passed through edit(lang, translation)."""
    sid_old = i18n._BY_EN.get(old_en)
    if sid_old is None or i18n._BY_EN.get(new_en):
        return new_en
    sid = f"r{len(i18n.SOURCE) + 1:04d}"
    i18n.SOURCE[sid] = new_en
    i18n._BY_EN[new_en] = sid
    for lang in i18n.NEW:
        old = i18n.TRANSLATIONS.get(lang, {}).get(sid_old)
        if old:
            i18n.TRANSLATIONS[lang][sid] = edit(lang, old)
    return new_en
