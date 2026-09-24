"""Разбор сообщений «Об изменении размера доли участия лица, входящего в состав органов управления эмитента».

Структура сообщения задана Положением Банка России (454-П гл. 53, затем 714-П): поля
«ФИО лица», «Должность», «наименование подконтрольной организации» (если применимо),
«Размер доли в уставном капитале до изменения», «Размер доли обыкновенных акций до изменения»,
«... после изменения», «Дата, в которую эмитент узнал об изменении». Нумерация пунктов и точные
формулировки варьируются, поэтому разбор ведётся по ключевым словам построчно.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from ..models import StakeChange

_NUM_RE = re.compile(r"[-+]?\d[\d\s ]*(?:[.,]\d+)?")
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_EMPTY_VALUES = {"", "-", "—", "–", "нет", "не применимо", "неприменимо", "отсутствует", "не имеется",
                 "не имеет", "не указано", "n/a", "0", "не является"}


def parse_number(s: str) -> Optional[float]:
    """«0,0012 %» -> 0.0012; «1 234,5» -> 1234.5; None если числа нет."""
    if s is None:
        return None
    s = s.replace(" ", " ")
    m = _NUM_RE.search(s)
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _split_lines(text: str) -> list[str]:
    text = text.replace("\r", "")
    # разбиваем и по переводам строк, и по нумерации пунктов вида "2.4." внутри строки
    parts: list[str] = []
    # граница пункта: пробел + "N." или "N.N." + пробел + заглавная буква; не режем даты (перед пробелом не цифра/точка)
    splitter = re.compile(r"(?<![\d.])\s+(?=\d{1,2}(?:\.\d{1,2})?\.\s+[А-ЯЁA-Z«\"])")
    for line in text.split("\n"):
        chunks = splitter.split(line)
        parts.extend(c.strip() for c in chunks if c.strip())
    return parts


def _value_after_colon(line: str) -> str:
    if ":" not in line:
        return ""
    return line.rsplit(":", 1)[1].strip().strip(";").strip()


def _norm(s: str) -> str:
    return s.replace("ё", "е").replace("Ё", "Е").lower()


def parse_stake_change(text: str, event_id: str = "") -> StakeChange:
    lines = _split_lines(text)
    person = position = organization = None
    is_subsidiary = False
    share_before = share_after = common_before = common_after = None
    sub_share_before = sub_share_after = None
    change_date: Optional[date] = None

    for raw in lines:
        line = _norm(raw)
        val = _value_after_colon(raw)
        if person is None and re.search(r"фамилия,?\s*имя|ф\.\s*и\.\s*о|фио\b", line) and val:
            person = val[:200]
            continue
        if position is None and re.search(r"должност", line) and val and not re.search(r"до изменения|после изменения", line):
            position = val[:200]
            continue
        if organization is None and re.search(r"подконтрольн", line) and re.search(r"наименовани", line) and ":" in raw:
            v = val
            if _norm(v).strip(" .") not in _EMPTY_VALUES and not re.search(r"^не\s", _norm(v)):
                organization = v[:300]
                is_subsidiary = True
            continue
        if re.search(r"дата", line) and re.search(r"узнал|уведомлен|изменени|получ", line) and change_date is None:
            tail = raw.split(":", 1)[1] if ":" in raw else raw
            m = _DATE_RE.search(tail) or _DATE_RE.search(raw)
            if m:
                d, mo, y = (int(x) for x in m.groups())
                try:
                    change_date = date(y, mo, d)
                except ValueError:
                    pass
            continue
        before = bool(re.search(r"до изменения", line))
        after = bool(re.search(r"после изменения", line))
        if not (before or after) or not re.search(r"дол[яи]", line):
            continue
        num = parse_number(val) if val else None
        if num is None:
            continue
        is_common = bool(re.search(r"обыкновенн", line))
        about_subsidiary = bool(re.search(r"подконтрольн|организац", line)) and not re.search(r"эмитента", line)
        if is_common:
            if before and common_before is None:
                common_before = num
            elif after and common_after is None:
                common_after = num
        elif about_subsidiary:
            if before and sub_share_before is None:
                sub_share_before = num
            elif after and sub_share_after is None:
                sub_share_after = num
        else:
            if before and share_before is None:
                share_before = num
            elif after and share_after is None:
                share_after = num

    # если долей по эмитенту нет, но есть по подконтрольной организации -- используем их
    if share_before is None and share_after is None and (sub_share_before is not None or sub_share_after is not None):
        share_before, share_after = sub_share_before, sub_share_after
        is_subsidiary = True

    delta = None
    direction = 0
    if share_before is not None and share_after is not None:
        delta = share_after - share_before
    elif common_before is not None and common_after is not None:
        delta = common_after - common_before
    if delta is not None:
        if delta > 1e-12:
            direction = 1
        elif delta < -1e-12:
            direction = -1

    return StakeChange(event_id=event_id, person=person, position=position, organization=organization,
                       is_subsidiary=is_subsidiary, share_before=share_before, share_after=share_after,
                       common_before=common_before, common_after=common_after, change_date=change_date,
                       direction=direction, delta_pp=delta)


_DEPUTY_RE = re.compile(r"заместител|вице-|врио|и\.\s*о\.|исполняющ\S* обязанност|советник")
_BOARD_CHAIR_RE = re.compile(r"председател\S* (совета директоров|наблюдательного совета)")
_CEO_RE = re.compile(r"генеральн\S* директор|(?<!вице-)президент|председател\S* правления|единоличн\S* исполнительн|управляющ\S* директор")
_BOARD_RE = re.compile(r"член\S* совета директоров|член\S* наблюдательного совета|совет\S* директоров|наблюдательн\S* совет")
_MGMT_RE = re.compile(r"член\S* правления|правлени|заместител|вице-|директор|руководител|начальник|казначей")


def classify_role(position: Optional[str]) -> str:
    """Грубая классификация должности инсайдера: board_chair / ceo / board / management_board / other / unknown.

    Приоритет: председатель СД > CEO (без приставок «заместитель/вице-/и.о.») > член СД > менеджмент.
    """
    p = _norm(position or "")
    if not p:
        return "unknown"
    if _BOARD_CHAIR_RE.search(p):
        return "board_chair"
    deputy = bool(_DEPUTY_RE.search(p))
    if _CEO_RE.search(p) and not deputy:
        return "ceo"
    if _BOARD_RE.search(p):
        return "board"
    if _MGMT_RE.search(p) or deputy:
        return "management_board"
    return "other"


def is_stake_change_text(text: str) -> bool:
    t = _norm(text)
    return bool(re.search(r"до изменения", t) and re.search(r"после изменения", t) and re.search(r"дол[яи]", t))


__all__ = ["parse_stake_change", "parse_number", "classify_role", "is_stake_change_text"]
