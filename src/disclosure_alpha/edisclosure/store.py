"""Хранение собранных данных e-disclosure: сообщения, события, компании (parquet + json)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from ..config import Settings
from ..models import MESSAGE_COLUMNS, CompanyInfo, EventPage, MessageRow

log = logging.getLogger(__name__)


class DisclosureStore:
    def __init__(self, settings: Settings):
        self.s = settings
        self.s.ensure_dirs()
        self.messages_path = settings.processed_dir / "messages.parquet"
        self.companies_path = settings.processed_dir / "companies.parquet"
        self.events_dir = settings.raw_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)

    # --- сообщения (результаты поиска) ---
    def load_messages(self) -> pd.DataFrame:
        if self.messages_path.exists():
            return pd.read_parquet(self.messages_path)
        return pd.DataFrame(columns=MESSAGE_COLUMNS)

    def append_messages(self, rows: Iterable[MessageRow]) -> pd.DataFrame:
        new = pd.DataFrame([r.to_dict() for r in rows], columns=MESSAGE_COLUMNS)
        old = self.load_messages()
        df = pd.concat([old, new], ignore_index=True) if len(old) else new
        df = df.drop_duplicates(subset=["event_id"], keep="last").reset_index(drop=True)
        df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce")
        df["company_id"] = pd.to_numeric(df["company_id"], errors="coerce").astype("Int64")
        df.to_parquet(self.messages_path, index=False)
        log.info("сообщений в хранилище: %d (+%d)", len(df), len(new))
        return df

    # --- события (полный текст) ---
    def event_path(self, event_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in event_id)
        return self.events_dir / f"{safe}.json"

    def has_event(self, event_id: str) -> bool:
        return self.event_path(event_id).exists()

    def save_event(self, ev: EventPage) -> None:
        d = ev.to_dict()
        d["published_at"] = ev.published_at.isoformat() if ev.published_at else None
        self.event_path(ev.event_id).write_text(json.dumps(d, ensure_ascii=False), "utf-8")

    def load_event(self, event_id: str) -> Optional[EventPage]:
        p = self.event_path(event_id)
        if not p.exists():
            return None
        d = json.loads(p.read_text("utf-8"))
        pa = d.get("published_at")
        d["published_at"] = pd.Timestamp(pa).to_pydatetime() if pa else None
        return EventPage(**d)

    def iter_events(self) -> Iterable[EventPage]:
        for p in sorted(self.events_dir.glob("*.json")):
            d = json.loads(p.read_text("utf-8"))
            pa = d.get("published_at")
            d["published_at"] = pd.Timestamp(pa).to_pydatetime() if pa else None
            yield EventPage(**d)

    # --- компании ---
    def load_companies(self) -> pd.DataFrame:
        if self.companies_path.exists():
            return pd.read_parquet(self.companies_path)
        return pd.DataFrame(columns=["company_id", "name", "inn", "ogrn", "okpo", "region"])

    def upsert_companies(self, infos: Iterable[CompanyInfo]) -> pd.DataFrame:
        new = pd.DataFrame([{k: v for k, v in c.to_dict().items() if k != "tickers"} for c in infos])
        old = self.load_companies()
        df = pd.concat([old, new], ignore_index=True) if len(old) else new
        if len(df):
            df = df.drop_duplicates(subset=["company_id"], keep="last").reset_index(drop=True)
            df.to_parquet(self.companies_path, index=False)
        return df
