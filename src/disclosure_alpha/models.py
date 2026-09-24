"""Общие модели данных (dataclasses) и схемы pandas-таблиц.

Схемы таблиц (long-format), которыми обмениваются модули:

``messages`` (результат поиска e-disclosure):
    event_id: str        -- идентификатор сообщения (из ссылки event.aspx?EventId=...)
    company_id: int      -- id компании на e-disclosure (company.aspx?id=...)
    company_name: str
    published_at: datetime (naive, московское время)
    event_type: str      -- заголовок/тип сообщения как показан на сайте
    url: str

``companies``:
    company_id: int, name: str, inn: str|None, ogrn: str|None

``prices`` (MOEX ISS, дневные свечи):
    date: date, secid: str, open, high, low, close: float, volume: int, value: float (руб.)

``index`` (бенчмарк):
    date: date, close: float

``events`` (вход event study / бэктеста):
    event_id: str, secid: str, published_at: datetime, category: str,
    direction: int (+1 покупка/позитив, -1 продажа/негатив, 0 нейтрально/неизвестно),
    size: float|NaN (напр. изменение доли, п.п.), плюс произвольные доп. колонки
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional


@dataclass
class MessageRow:
    event_id: str
    company_id: Optional[int]
    company_name: str
    published_at: Optional[datetime]
    event_type: str
    url: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompanyInfo:
    company_id: int
    name: str
    inn: Optional[str] = None
    ogrn: Optional[str] = None
    okpo: Optional[str] = None
    region: Optional[str] = None
    tickers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EventPage:
    event_id: str
    title: str
    company_name: str
    company_id: Optional[int]
    published_at: Optional[datetime]
    body_text: str
    event_type: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StakeChange:
    """Разобранное сообщение «Об изменении размера доли участия лица, входящего в органы управления»."""

    event_id: str
    person: Optional[str]
    position: Optional[str]
    organization: Optional[str]          # эмитент или подконтрольная организация, если указана
    is_subsidiary: bool
    share_before: Optional[float]        # доля в УК до изменения, %
    share_after: Optional[float]         # доля в УК после изменения, %
    common_before: Optional[float]       # доля обыкновенных акций до, %
    common_after: Optional[float]        # доля обыкновенных акций после, %
    change_date: Optional[date]          # дата изменения / дата, когда эмитент узнал
    direction: int                       # +1 увеличение (покупка), -1 уменьшение (продажа), 0 неизвестно
    delta_pp: Optional[float]            # изменение доли, процентные пункты

    def to_dict(self) -> dict:
        return asdict(self)


PRICE_COLUMNS = ["date", "secid", "open", "high", "low", "close", "volume", "value"]
MESSAGE_COLUMNS = ["event_id", "company_id", "company_name", "published_at", "event_type", "url"]
EVENT_COLUMNS = ["event_id", "secid", "published_at", "category", "direction", "size"]
