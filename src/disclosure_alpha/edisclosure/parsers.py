"""HTML-парсеры страниц e-disclosure.ru.

Разметка сайта восстановлена по открытым источникам (см. docs/site_structure.md) и может отличаться,
поэтому парсеры опираются на устойчивые признаки: ссылки вида ``portal/event.aspx?EventId=...`` и
``portal/company.aspx?id=...``, даты формата ``ДД.ММ.ГГГГ ЧЧ:ММ`` и подписи полей («ИНН», «ОГРН»).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import CompanyInfo, EventPage, MessageRow

EVENT_ID_RE = re.compile(r"EventId=([A-Za-z0-9_\-]+)", re.I)
COMPANY_ID_RE = re.compile(r"company\.aspx\?id=(\d+)", re.I)
DATETIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})(?:[  \t]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
INN_RE = re.compile(r"ИНН\D{0,20}?(\d{10}|\d{12})\b")
OGRN_RE = re.compile(r"ОГРН\D{0,20}?(\d{13}|\d{15})\b")
OKPO_RE = re.compile(r"ОКПО\D{0,20}?(\d{8,10})\b")


# запасные схемы адресов сообщения на случай, если сайт ушёл от event.aspx?EventId=
_EVENT_PATH_RE = re.compile(r"/(?:event|events|soobshhenie|soobshheniya|message|messages|news)/([A-Za-z0-9_\-]{3,})", re.I)


def event_id_from_href(href: str) -> Optional[str]:
    """Идентификатор сообщения из ссылки: параметр EventId либо последний сегмент адреса сообщения."""
    if not href:
        return None
    m = EVENT_ID_RE.search(href)
    if m:
        return m.group(1)
    m = _EVENT_PATH_RE.search(href.split("?")[0])
    if m:
        return m.group(1)
    return None


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_datetime_ru(text: str) -> Optional[datetime]:
    """Первая дата вида ДД.ММ.ГГГГ [ЧЧ:ММ[:СС]] в тексте (московское время, naive)."""
    m = DATETIME_RE.search(text or "")
    if not m:
        return None
    d, mo, y, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None


def clean_text(s: str) -> str:
    return re.sub(r"[ \t ]+", " ", (s or "").replace("\r", "")).strip()


# ----------------------------------------------------------------------------- форма поиска
@dataclass
class FormSpec:
    action: str
    method: str
    fields: dict = field(default_factory=dict)           # name -> значение по умолчанию (text/hidden/select)
    checkbox_groups: dict = field(default_factory=dict)  # name -> [{value,label,checked}]
    radio_groups: dict = field(default_factory=dict)     # name -> [{value,label,checked}]
    selects: dict = field(default_factory=dict)          # name -> [{value,label,selected}]

    def event_type_options(self) -> list[dict]:
        """Эвристика: группа чекбоксов/селект с наибольшим числом вариантов (типы сообщений)."""
        candidates: list[tuple[int, str, list[dict]]] = []
        for name, opts in self.checkbox_groups.items():
            candidates.append((len(opts), name, opts))
        for name, opts in self.selects.items():
            candidates.append((len(opts), name, opts))
        if not candidates:
            return []
        candidates.sort(key=lambda x: (-x[0], x[1]))
        # приоритет группе, в имени которой есть event/type/тип
        for n, name, opts in candidates:
            if re.search(r"event|type|тип", name, re.I) and n >= 5:
                return [{"group": name, **o} for o in opts]
        n, name, opts = candidates[0]
        return [{"group": name, **o} for o in opts]


def _label_for(inp: Tag, soup: BeautifulSoup) -> str:
    iid = inp.get("id")
    if iid:
        lab = soup.find("label", attrs={"for": iid})
        if lab:
            return clean_text(lab.get_text(" "))
    parent = inp.parent
    if parent is not None and parent.name == "label":
        return clean_text(parent.get_text(" "))
    # текст сразу после чекбокса
    nxt = inp.next_sibling
    while nxt is not None and (isinstance(nxt, str) and not nxt.strip()):
        nxt = nxt.next_sibling
    if nxt is not None:
        return clean_text(nxt.get_text(" ") if isinstance(nxt, Tag) else str(nxt))
    return ""


def parse_search_form(html: str, base_url: str = "https://e-disclosure.ru", page_url: Optional[str] = None) -> Optional[FormSpec]:
    """Находит форму поиска по сообщениям (по признакам: поля дат, чекбоксы типов) и описывает её поля."""
    soup = soup_of(html)
    forms = soup.find_all("form")
    if not forms:
        return None

    def score(f: Tag) -> int:
        names = " ".join((i.get("name") or "") for i in f.find_all(["input", "select", "textarea"]))
        s = len(f.find_all("input", attrs={"type": "checkbox"}))
        s += 20 * len(re.findall(r"date|дата", names, re.I))
        s += 10 * len(re.findall(r"event|page", names, re.I))
        if re.search(r"poisk|search", (f.get("action") or "") + (f.get("id") or ""), re.I):
            s += 30
        return s

    form = max(forms, key=score)
    # пустой action означает отправку на адрес самой страницы, а не на корень сайта
    spec = FormSpec(action=urljoin(page_url or base_url, form.get("action") or ""),
                    method=(form.get("method") or "get").lower())
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype == "checkbox":
            spec.checkbox_groups.setdefault(name, []).append(
                {"value": inp.get("value", "on"), "label": _label_for(inp, soup), "checked": inp.has_attr("checked")})
        elif itype == "radio":
            spec.radio_groups.setdefault(name, []).append(
                {"value": inp.get("value", "on"), "label": _label_for(inp, soup), "checked": inp.has_attr("checked")})
        elif itype in ("submit", "button", "image", "file"):
            continue
        else:
            spec.fields[name] = inp.get("value", "")
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opts = [{"value": o.get("value", clean_text(o.get_text())), "label": clean_text(o.get_text()),
                 "selected": o.has_attr("selected")} for o in sel.find_all("option")]
        spec.selects[name] = opts
        selected = [o for o in opts if o["selected"]]
        spec.fields[name] = selected[0]["value"] if selected else (opts[0]["value"] if opts else "")
    for ta in form.find_all("textarea"):
        if ta.get("name"):
            spec.fields[ta["name"]] = clean_text(ta.get_text())
    return spec


# ----------------------------------------------------------------------------- список сообщений
def _extract_row(node: Tag, base_url: str) -> Optional[MessageRow]:
    link, event_id = None, None
    for a in node.find_all("a", href=True):
        candidate = event_id_from_href(a["href"])
        if candidate:
            link, event_id = a, candidate
            break
    if link is None:
        return None
    url = urljoin(base_url, link["href"])

    company_id = None
    company_name = ""
    for a in node.find_all("a", href=True):
        m = COMPANY_ID_RE.search(a["href"])
        if m:
            company_id = int(m.group(1))
            company_name = clean_text(a.get_text(" "))
            break

    text_all = clean_text(node.get_text(" "))
    published_at = parse_datetime_ru(text_all)

    # тип/заголовок сообщения: текст ссылки на событие; если пустой -- ячейка, содержащая ссылку
    event_type = clean_text(link.get_text(" "))
    if not event_type:
        cell = link.find_parent(["td", "div", "li"])
        event_type = clean_text(cell.get_text(" ")) if cell else ""

    if not company_name:
        # лента lastnews: эмитент и тип в div.lastNewsItemInfo ("Эмитент, тип события")
        info = node.find(class_=re.compile("info", re.I))
        if info is not None:
            info_text = clean_text(info.get_text(" "))
            parts = [p.strip() for p in re.split(r"\s*[,;]\s+|\s\|\s|\s—\s|\s–\s", info_text) if p.strip()]
            if parts:
                company_name = parts[0]
                if len(parts) > 1 and not event_type:
                    event_type = ", ".join(parts[1:])
    return MessageRow(event_id=event_id, company_id=company_id, company_name=company_name,
                      published_at=published_at, event_type=event_type, url=url)


RESULTS_CONTAINER_IDS = ("searchResults", "searchResult", "results", "eventsList")


def results_container(soup: BeautifulSoup) -> Optional[Tag]:
    """Блок, куда сайт вставляет результаты поиска (#searchResults и подобные)."""
    for cid in RESULTS_CONTAINER_IDS:
        node = soup.find(id=cid)
        if node is not None:
            return node
    return None


def parse_message_list(html: str, base_url: str = "https://e-disclosure.ru", only_results: bool = False) -> list[MessageRow]:
    """Разбор списка сообщений: строки таблицы результатов или блоки ленты.

    Единицей считается ближайший контейнер (tr / li / div) вокруг ссылки на сообщение.
    ``only_results=True`` -- искать лишь внутри блока результатов (на странице поиска много прочих ссылок).
    """
    soup = soup_of(html)
    scope: Tag | BeautifulSoup = soup
    if only_results:
        container = results_container(soup)
        if container is None:
            return []
        scope = container
    rows: list[MessageRow] = []
    seen: set[str] = set()
    for a in scope.find_all("a", href=True):
        if not event_id_from_href(a["href"]):
            continue
        row_container = a.find_parent("tr")
        if row_container is None:
            row_container = a.find_parent(class_=re.compile(r"table__row|row|item", re.I))
        if row_container is None:
            row_container = a.find_parent("li")
        if row_container is None:
            # ближайший div, который содержит дату (иначе поднимаемся выше)
            row_container = a.find_parent("div")
            hops = 0
            while row_container is not None and hops < 4 and not DATETIME_RE.search(row_container.get_text(" ")):
                row_container = row_container.find_parent("div")
                hops += 1
        if row_container is None:
            row_container = a
        row = _extract_row(row_container, base_url)
        if row is None or row.event_id in seen:
            continue
        seen.add(row.event_id)
        rows.append(row)
    return rows


def parse_search_results(html: str, base_url: str = "https://e-disclosure.ru") -> list[MessageRow]:
    """Результаты поиска: сначала внутри блока результатов, иначе по всей странице (старая вёрстка)."""
    rows = parse_message_list(html, base_url, only_results=True)
    return rows or parse_message_list(html, base_url)


def parse_lastnews(html: str, base_url: str = "https://e-disclosure.ru") -> list[MessageRow]:
    return parse_message_list(html, base_url)


def parse_total_pages(html: str) -> Optional[int]:
    """Пытается извлечь число страниц/записей из пагинации (если есть). None -- не найдено."""
    soup = soup_of(html)
    text = clean_text(soup.get_text(" "))
    m = re.search(r"(?:Страниц[аы]?|Стр\.)\s*\d+\s*(?:из|/)\s*(\d+)", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"Найдено\D{0,20}(\d[\d ]*)", text, re.I)
    if m:
        return int(m.group(1).replace(" ", ""))
    return None


# ----------------------------------------------------------------------------- страница события
_CHROME_TAGS = ("script", "style", "noscript", "header", "footer", "nav", "iframe", "svg", "form")


def _body_text(soup: BeautifulSoup) -> str:
    for t in soup.find_all(_CHROME_TAGS):
        t.decompose()
    # предпочитаем контейнер сообщения, если он опознаётся по id/class
    main = None
    for pat in (r"cont_wrap", r"eventText", r"event", r"news", r"content", r"main"):
        main = soup.find(attrs={"id": re.compile(pat, re.I)}) or soup.find(attrs={"class": re.compile(pat, re.I)})
        if main is not None and len(clean_text(main.get_text(" "))) > 200:
            break
        main = None
    root = main if main is not None else (soup.body or soup)
    for br in root.find_all("br"):
        br.replace_with("\n")
    text = root.get_text("\n")
    lines = [clean_text(l) for l in text.split("\n")]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def parse_event_page(html: str, event_id: Optional[str] = None) -> EventPage:
    soup = soup_of(html)
    title_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
    title = clean_text(title_tag.get_text(" ")) if title_tag else ""

    company_id = None
    company_name = ""
    for a in soup.find_all("a", href=True):
        m = COMPANY_ID_RE.search(a["href"])
        if m:
            company_id = int(m.group(1))
            company_name = clean_text(a.get_text(" "))
            break
    if event_id is None:
        for a in soup.find_all("a", href=True):
            m = EVENT_ID_RE.search(a["href"])
            if m:
                event_id = m.group(1)
                break
        if event_id is None:
            canon = soup.find("link", rel="canonical")
            if canon and canon.get("href"):
                m = EVENT_ID_RE.search(canon["href"])
                event_id = m.group(1) if m else None

    body = _body_text(soup)
    published_at = None
    m = re.search(r"(?:Дата\s+(?:публикации|размещения|раскрытия)|Опубликовано)\D{0,15}(\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)", body, re.I)
    if m:
        published_at = parse_datetime_ru(m.group(1))
    if published_at is None:
        head = body[:600]
        published_at = parse_datetime_ru(head)

    event_type = ""
    m = re.search(r"(?:Тип\s+(?:сообщения|события)|Существенн\S+\s+факт)\s*[:\-–—]?\s*(.+)", body, re.I)
    if m:
        event_type = clean_text(m.group(1))[:300]
    if not company_name:
        m = re.search(r"(?:Полное фирменное наименование эмитента|Эмитент)\D{0,10}[:\-–—]\s*(.+)", body, re.I)
        if m:
            company_name = clean_text(m.group(1))[:300]
    return EventPage(event_id=event_id or "", title=title, company_name=company_name, company_id=company_id,
                     published_at=published_at, body_text=body, event_type=event_type)


# ----------------------------------------------------------------------------- страница компании
def parse_company_page(html: str, company_id: Optional[int] = None) -> CompanyInfo:
    soup = soup_of(html)
    for t in soup.find_all(("script", "style", "noscript")):
        t.decompose()
    text = clean_text(soup.get_text(" "))
    name_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
    name = clean_text(name_tag.get_text(" ")) if name_tag else ""
    inn = INN_RE.search(text)
    ogrn = OGRN_RE.search(text)
    okpo = OKPO_RE.search(text)
    if company_id is None:
        for a in soup.find_all("a", href=True):
            m = COMPANY_ID_RE.search(a["href"])
            if m:
                company_id = int(m.group(1))
                break
    region = None
    lines = [clean_text(l) for l in soup.get_text("\n").split("\n")]
    lines = [l for l in lines if l]
    for i, l in enumerate(lines):
        if re.match(r"Регион\b", l):
            val = l.split(":", 1)[1].strip() if ":" in l else ""
            if not val and i + 1 < len(lines):
                val = lines[i + 1]
            region = val[:80] or None
            break
    return CompanyInfo(company_id=company_id or -1, name=name, inn=inn.group(1) if inn else None,
                       ogrn=ogrn.group(1) if ogrn else None, okpo=okpo.group(1) if okpo else None, region=region)


def find_links(html: str, base_url: str, pattern: str) -> list[str]:
    """Все ссылки страницы, подходящие под регулярное выражение (для discover)."""
    soup = soup_of(html)
    rx = re.compile(pattern, re.I)
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if rx.search(href) and href not in out:
            out.append(href)
    return out


def find_script_endpoints(html: str) -> list[str]:
    """URL-подобные строки внутри <script> (ajax-эндпоинты) -- для discover."""
    soup = soup_of(html)
    found: list[str] = []
    rx = re.compile(r"""["'](/[A-Za-z0-9_./\-?=&%]{3,})["']""")
    keywords = re.compile(r"aspx|ashx|api|event|search|company|page|news|poisk", re.I)
    for s in soup.find_all("script"):
        for m in rx.finditer(s.get_text()):
            cand = m.group(1)
            if keywords.search(cand) and cand not in found:
                found.append(cand)
    return found


def describe_dom(html: str, max_items: int = 40) -> dict:
    """Структурная сводка страницы: формы, поля, ссылки, идентификаторы, классы, адреса скриптов.

    Нужна для диагностики без пересылки самого HTML (в нём бывает обфусцированный сторонний код).
    Текст и содержимое скриптов не включаются -- только структура.
    """
    soup = soup_of(html)
    title = clean_text(soup.title.get_text()) if soup.title else ""
    for t in soup.find_all(("style", "noscript")):
        t.decompose()

    forms = []
    for f in soup.find_all("form"):
        fields = []
        for inp in f.find_all(["input", "select", "textarea"]):
            fields.append({"tag": inp.name, "type": (inp.get("type") or "").lower() or None,
                           "name": inp.get("name"), "id": inp.get("id")})
        forms.append({"action": f.get("action"), "method": (f.get("method") or "get").lower(),
                      "id": f.get("id"), "class": " ".join(f.get("class") or []),
                      "n_fields": len(fields), "fields": fields[:max_items]})

    inputs_outside = []
    for inp in soup.find_all(["input", "select", "textarea"]):
        if inp.find_parent("form") is None:
            inputs_outside.append({"tag": inp.name, "type": (inp.get("type") or "").lower() or None,
                                   "name": inp.get("name"), "id": inp.get("id"),
                                   "placeholder": inp.get("placeholder"),
                                   "class": " ".join(inp.get("class") or [])})

    selects = []
    for sel in soup.find_all("select"):
        opts = sel.find_all("option")
        selects.append({"name": sel.get("name"), "id": sel.get("id"), "n_options": len(opts),
                        "options_head": [clean_text(o.get_text())[:80] for o in opts[:10]]})

    anchors = []
    for a in soup.find_all("a", href=True):
        anchors.append({"href": a["href"][:200], "text": clean_text(a.get_text(" "))[:80]})

    patterns: dict[str, int] = {}
    for a in anchors:
        href = a["href"].split("#")[0]
        path = re.sub(r"^https?://[^/]+", "", href)
        path = re.sub(r"\d+", "{n}", path.split("?")[0])
        query = href.split("?", 1)[1] if "?" in href else ""
        keys = ",".join(sorted({kv.split("=")[0] for kv in query.split("&") if kv})) if query else ""
        key = path + (f"?{keys}" if keys else "")
        patterns[key] = patterns.get(key, 0) + 1

    classes: dict[str, int] = {}
    for el in soup.find_all(class_=True):
        for c in el.get("class") or []:
            classes[c] = classes.get(c, 0) + 1
    ids = [el.get("id") for el in soup.find_all(id=True)][:max_items * 2]

    buttons = [{"tag": b.name, "id": b.get("id"), "type": (b.get("type") or "").lower() or None,
                "text": clean_text(b.get_text(" "))[:60] or b.get("value"),
                "class": " ".join(b.get("class") or [])}
               for b in soup.find_all(["button", "input"]) if b.name == "button" or (b.get("type") or "").lower() in ("submit", "button")]

    return {
        "title": title,
        "counts": {"forms": len(soup.find_all("form")), "inputs": len(soup.find_all("input")),
                   "selects": len(soup.find_all("select")), "textareas": len(soup.find_all("textarea")),
                   "anchors": len(soup.find_all("a", href=True)), "tables": len(soup.find_all("table")),
                   "rows": len(soup.find_all("tr")), "scripts": len(soup.find_all("script")),
                   "iframes": len(soup.find_all("iframe")), "html_len": len(html)},
        "forms": forms[:10],
        "inputs_outside_forms": inputs_outside[:max_items],
        "selects": selects[:max_items],
        "buttons": buttons[:max_items],
        "anchors_sample": anchors[:max_items],
        "anchor_patterns": sorted(patterns.items(), key=lambda kv: -kv[1])[:max_items],
        "text_fields": [{"name": i.get("name"), "id": i.get("id"), "type": (i.get("type") or "text").lower(),
                         "placeholder": i.get("placeholder"), "value": (i.get("value") or "")[:40]}
                        for i in soup.find_all("input")
                        if (i.get("type") or "text").lower() not in ("checkbox", "radio", "submit", "button", "image")][:max_items],
        "event_links": [a["href"] for a in anchors if EVENT_ID_RE.search(a["href"])][:10],
        "company_links": [a["href"] for a in anchors if COMPANY_ID_RE.search(a["href"])][:10],
        "ids": [i for i in ids if i],
        "top_classes": sorted(classes.items(), key=lambda kv: -kv[1])[:max_items],
        "script_srcs": [sc.get("src") for sc in soup.find_all("script", src=True)][:max_items],
        "script_endpoints": find_script_endpoints(html)[:max_items],
        "has_date_placeholder": bool(re.search(r"дата|date|дд\.мм", html, re.I)),
    }


# ----------------------------------------------------------------------------- ответ поиска в формате JSON
# Поиск сайта (api/search/sevents) отвечает JSON вида
# {"foundEventsList": [{"companyID":.., "companyName":.., "eventName":.., "pseudoGUID":.., ...}], ...}
# Имена полей могут меняться, поэтому ищем по смыслу, а не по точному имени.
_JSON_LIST_KEYS = ("foundEventsList", "events", "items", "data", "list", "rows", "result", "results")
_ID_KEYS = ("pseudoguid", "pseudoguid", "eventguid", "eventid", "guid", "id", "code")
_COMPANY_ID_KEYS = ("companyid", "company_id", "companycode", "orgid")
_COMPANY_NAME_KEYS = ("companyname", "company", "orgname", "shortname")
_EVENT_NAME_KEYS = ("eventname", "eventtype", "name", "title", "highlighted")
_DATE_KEYS = ("eventdate", "publishdate", "publicationdate", "date", "datetime", "time", "created", "dt")
_ISO_DT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")
_LONG_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")


def find_event_list(payload) -> list[dict]:
    """Список сообщений внутри ответа поиска (ключ может называться по-разному)."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _JSON_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    for value in payload.values():                      # запасной вариант: первый список словарей
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def _pick(item: dict, keys: tuple, lowered: dict) -> Optional[object]:
    for k in keys:
        if k in lowered and lowered[k] not in (None, ""):
            return lowered[k]
    return None


def _parse_any_datetime(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    m = _ISO_DT_RE.search(value)
    if m:
        y, mo, d, h, mi, sec = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
        except ValueError:
            return None
    return parse_datetime_ru(value)


def _json_event_id(item: dict, lowered: dict) -> Optional[str]:
    value = _pick(item, _ID_KEYS, lowered)
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    for v in item.values():                             # запасной вариант: длинный идентификатор в любом поле
        if isinstance(v, str) and _LONG_ID_RE.match(v):
            return v
    return None


def parse_search_json(payload, base_url: str = "https://e-disclosure.ru") -> list[MessageRow]:
    """Строки сообщений из ответа поиска в формате JSON."""
    rows: list[MessageRow] = []
    for item in find_event_list(payload):
        lowered = {str(k).lower(): v for k, v in item.items()}
        event_id = _json_event_id(item, lowered)
        if not event_id:
            continue
        company_id = _pick(item, _COMPANY_ID_KEYS, lowered)
        try:
            company_id = int(company_id) if company_id is not None else None
        except (TypeError, ValueError):
            company_id = None
        published = None
        for key in _DATE_KEYS:
            published = _parse_any_datetime(lowered.get(key))
            if published:
                break
        if published is None:                            # дата может лежать в поле с другим именем
            for v in item.values():
                published = _parse_any_datetime(v)
                if published:
                    break
        event_type = _pick(item, _EVENT_NAME_KEYS, lowered) or ""
        event_type = clean_text(soup_of(str(event_type)).get_text(" ")) if "<" in str(event_type) else clean_text(str(event_type))
        company_name = clean_text(str(_pick(item, _COMPANY_NAME_KEYS, lowered) or ""))
        url = urljoin(base_url, f"/portal/event.aspx?EventId={event_id}")
        rows.append(MessageRow(event_id=event_id, company_id=company_id, company_name=company_name,
                               published_at=published, event_type=event_type, url=url))
    return rows


def total_from_payload(payload) -> Optional[int]:
    """Общее число найденных сообщений, если сервер его сообщает."""
    if not isinstance(payload, dict):
        return None
    for key in ("totalCount", "total", "count", "found", "recordsTotal", "eventsCount"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None
