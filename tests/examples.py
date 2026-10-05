"""Artificial HTML examples for offline tests, NOT saved NRAT responses."""

from html import escape
from urllib.parse import urlencode

DAY = "2010-03-12"
ID = "b94c567e9c5bb8b69a1eb95a11b7a40a"
URL = f"https://nddkr.ukrintei.ua/view/ok/{ID}"


def card(url=URL, number="0210U000704", kind="НДДКР ОК", extra=""):
    return f'''<div class="my-card"><div class="typeBase">{escape(kind)}</div>
    <div name="title"><a class="view_card" data="02WRONG_ID" href="#">Тестова робота / Test study</a></div>
    <a href="/searchdoc/0210U000704/">Permanent link, NOT doc_id</a>
    <div class="my-card-body">Керівник: Тест. Test description.
    <a href="{escape(url, quote=True)}">{escape(number)}</a>{extra}</div></div>'''


def pagination(page=1, next_page=2, day=DAY, kind="ok"):
    params = dict(typeSearch2=kind, dateFromSearch=day, dateToSearch=day,
                  pa=str(next_page), sortOrder="registration_date", sortDir="asc", tab="big")
    link = f'<a rel="next" href="/searchdb?{escape(urlencode(params), quote=True)}">Next</a>' if next_page else ""
    return f'<ul class="pagination"><li class="page-item active"><span class="page-link">{page}</span></li>{link}</ul>'


def html_page(*, cards=None, count=1, second_count=None, day=DAY, kind="ok", selected=False, pages="", limited=False):
    if cards is None:
        cards = card()
    selection = ' selected' if selected else ''
    script = '' if selected else f"<script>var typeSearch2 = '{kind}';</script>"
    form = f'''<form method="get"><select name="typeSearch2">
    <option value="1">Всі</option><option value="ok"{selection}>Звіти</option></select>
    <input name="dateFromSearch" value="{day}"><input name="dateToSearch" value="{day}"></form>{script}'''
    def counter(value):
        if value is None:
            return ''
        warning = '<span class="limited_search">Результати пошуку обмежено</span>' if limited else ''
        return f'<div class="page_control"><div class="page_info">Знайдено документів: {value}{warning}</div></div>'
    bottom = count if second_count is None else second_count
    return f'<!doctype html><html><body>{form}{counter(count)}{cards}{pages}{counter(bottom)}</body></html>'
