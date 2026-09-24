"""Локальное хранилище дневных котировок MOEX: parquet-файл на бумагу, инкрементальное обновление.

Файлы: ``{prices_dir}/{secid}.parquet`` (схема ``prices``) и ``{prices_dir}/index_{name}.parquet``
(схема ``index``); колонка ``date`` хранится как datetime.date (parquet date32).
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd

from disclosure_alpha.config import Settings
from disclosure_alpha.moex.iss import PRICE_OUTPUT_COLUMNS, DateLike, IssClient, empty_index, empty_prices, to_date

log = logging.getLogger(__name__)

LIQUIDITY_COLUMNS = [
    "secid", "first_date", "last_date", "window_end", "n_days", "n_traded_days",
    "median_value", "mean_value", "zero_volume_share",
]


# ----------------------------------------------------------------------------- нормализация
def _prepare_prices(df: Optional[pd.DataFrame], secid: Optional[str] = None) -> pd.DataFrame:
    """Привести таблицу к схеме prices: колонки, типы, сортировка по дате, уникальные даты (последняя выигрывает)."""
    if df is None or df.empty:
        return empty_prices()
    out = df.copy()
    if "secid" not in out.columns or out["secid"].isna().all():
        out["secid"] = secid
    out["date"] = pd.to_datetime(out["date"]).dt.date
    for c in ("open", "high", "low", "close", "value"):
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64") if c in out.columns else np.nan
    for c in ("volume", "numtrades"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).round().astype("int64") if c in out.columns else 0
    out = out.sort_values("date", kind="stable").drop_duplicates("date", keep="last").reset_index(drop=True)
    extra = [c for c in out.columns if c not in PRICE_OUTPUT_COLUMNS]
    return out[PRICE_OUTPUT_COLUMNS + extra]


def _prepare_index(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Привести таблицу к схеме index (date, close, ...): типы, сортировка, уникальные даты."""
    if df is None or df.empty:
        return empty_index()
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    for c in [c for c in ("close", "open", "high", "low", "value") if c in out.columns]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    out = out.dropna(subset=["close"]).sort_values("date", kind="stable")
    out = out.drop_duplicates("date", keep="last").reset_index(drop=True)
    return out[["date", "close"] + [c for c in out.columns if c not in ("date", "close")]]


def _normalize_dates(df: pd.DataFrame) -> pd.DataFrame:
    """После чтения parquet ``date`` уже datetime.date; на всякий случай приводим явно."""
    if "date" in df.columns and len(df):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


# ----------------------------------------------------------------------------- хранилище
class PriceStore:
    """Хранилище котировок в ``settings.prices_dir``: load/save/update по бумаге, панель, индексы."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.prices_dir = Path(settings.prices_dir)

    # --- пути
    def path(self, secid: str) -> Path:
        return self.prices_dir / f"{secid}.parquet"

    def index_path(self, name: str) -> Path:
        return self.prices_dir / f"index_{name}.parquet"

    def list_secids(self) -> list[str]:
        """Бумаги, для которых есть файл (индексы не считаются)."""
        if not self.prices_dir.exists():
            return []
        return sorted(p.stem for p in self.prices_dir.glob("*.parquet") if not p.name.startswith("index_"))

    # --- чтение/запись
    @staticmethod
    def _read(path: Path) -> Optional[pd.DataFrame]:
        if not path.exists():
            return None
        return _normalize_dates(pd.read_parquet(path))

    def _write(self, path: Path, df: pd.DataFrame) -> Path:
        """Запись через временный файл, чтобы не оставить битый parquet при сбое."""
        self.prices_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        df.reset_index(drop=True).to_parquet(tmp, index=False)
        tmp.replace(path)
        return path

    def load(self, secid: str) -> Optional[pd.DataFrame]:
        """Котировки бумаги (схема prices) или None, если файла нет."""
        return self._read(self.path(secid))

    def save(self, secid: str, df: pd.DataFrame) -> Path:
        return self._write(self.path(secid), _prepare_prices(df, secid))

    def last_date(self, secid: str) -> Optional[dt.date]:
        df = self.load(secid)
        return None if df is None or df.empty else df["date"].max()

    def load_index(self, name: Optional[str] = None) -> Optional[pd.DataFrame]:
        """История индекса (схема index) или None; ``name=None`` -> settings.market_index."""
        return self._read(self.index_path(name or self.settings.market_index))

    def save_index(self, name: str, df: pd.DataFrame) -> Path:
        return self._write(self.index_path(name), _prepare_index(df))

    # --- инкрементальное обновление
    @staticmethod
    def _incremental(
        label: str,
        stored: Optional[pd.DataFrame],
        date_from: DateLike,
        date_till: Optional[DateLike],
        fetch: Callable[[dt.date, dt.date], pd.DataFrame],
        prepare: Callable[[Optional[pd.DataFrame]], pd.DataFrame],
        save: Callable[[pd.DataFrame], Path],
    ) -> pd.DataFrame:
        """Общая схема: запросить только даты после последней сохранённой, слить, сохранить."""
        start = to_date(date_from)
        end = to_date(date_till) if date_till is not None else dt.date.today()
        has_stored = stored is not None and not stored.empty
        if has_stored:
            start = max(start, stored["date"].max() + dt.timedelta(days=1))
        if start > end:
            log.info("%s: up to date (last=%s)", label, stored["date"].max() if has_stored else None)
            return stored if has_stored else prepare(None)
        fresh = fetch(start, end)
        if fresh is None or fresh.empty:
            log.info("%s: no new rows for %s..%s", label, start, end)
            return stored if has_stored else prepare(None)
        merged = prepare(pd.concat([stored, fresh], ignore_index=True) if has_stored else fresh)
        save(merged)
        log.info("%s: +%d rows, total %d (%s..%s)", label, len(fresh), len(merged), merged["date"].min(), merged["date"].max())
        return merged

    def update(
        self,
        secid: str,
        client: IssClient,
        date_from: DateLike,
        date_till: Optional[DateLike] = None,
        board: Optional[str] = None,
    ) -> pd.DataFrame:
        """Догрузить котировки бумаги: запрашиваются только даты после последней сохранённой.

        Нет файла -> полная загрузка за [date_from, date_till]; ``date_till=None`` -> сегодня.
        Возвращает полную таблицу по бумаге (схема prices). Более ранний ``date_from``, чем уже
        сохранённые данные, не дозагружается (для этого удалите файл).
        """
        return self._incremental(
            f"prices {secid}", self.load(secid), date_from, date_till,
            fetch=lambda a, b: client.get_history(secid, a, b, board=board),
            prepare=lambda df: _prepare_prices(df, secid),
            save=lambda df: self._write(self.path(secid), df),
        )

    def update_index(
        self,
        name: Optional[str],
        client: IssClient,
        date_from: DateLike,
        date_till: Optional[DateLike] = None,
    ) -> pd.DataFrame:
        """То же для индекса; ``name=None`` -> settings.market_index."""
        name = name or self.settings.market_index
        return self._incremental(
            f"index {name}", self.load_index(name), date_from, date_till,
            fetch=lambda a, b: client.get_index_history(name, a, b),
            prepare=_prepare_index,
            save=lambda df: self._write(self.index_path(name), df),
        )

    # --- панель
    def load_panel(self, secids: Optional[Iterable[str]] = None) -> pd.DataFrame:
        """Длинная таблица prices по списку бумаг (None -> все сохранённые); бумаги без файла пропускаются."""
        ids = list(secids) if secids is not None else self.list_secids()
        frames: list[pd.DataFrame] = []
        missing: list[str] = []
        for secid in ids:
            df = self.load(secid)
            if df is None or df.empty:
                missing.append(secid)
                continue
            frames.append(df)
        if missing:
            log.warning("load_panel: no stored prices for %d secids: %s", len(missing), missing[:20])
        if not frames:
            return empty_prices()
        panel = pd.concat(frames, ignore_index=True)
        return panel.sort_values(["secid", "date"], kind="stable").reset_index(drop=True)


# ----------------------------------------------------------------------------- ликвидность
def liquidity_stats(
    panel: pd.DataFrame,
    window_days: int = 60,
    as_of: Optional[DateLike] = None,
    calendar: Optional[Iterable] = None,
) -> pd.DataFrame:
    """Ликвидность бумаг за окно ``window_days`` календарных дней -- для отсева неликвидов.

    Окно заканчивается ``as_of`` (по умолчанию -- последняя дата самой бумаги, чтобы делистингованные
    имена оценивались по собственной истории). Торговый календарь -- ``calendar`` (например, даты
    индекса) или объединение дат панели; дни календаря без строки или с нулевым объёмом считаются
    днями без сделок (оборот 0).
    Колонки: secid, first_date, last_date, window_end, n_days (торговых дней в окне), n_traded_days,
    median_value / mean_value (дневной оборот, руб.), zero_volume_share (доля дней без сделок).
    """
    if panel is None or panel.empty:
        return pd.DataFrame(columns=LIQUIDITY_COLUMNS)
    df = panel[["date", "secid", "volume", "value"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0.0)
    if calendar is not None:
        cal = pd.DatetimeIndex(pd.to_datetime(list(calendar)))
    else:
        cal = pd.DatetimeIndex(df["date"].unique())
    cal = cal.sort_values().unique()
    common_end = pd.Timestamp(as_of) if as_of is not None else None

    rows = []
    for secid, g in df.groupby("secid", sort=True):
        end = common_end if common_end is not None else g["date"].max()
        start = end - pd.Timedelta(days=window_days)
        cal_w = cal[(cal > start) & (cal <= end)]
        gw = g[(g["date"] > start) & (g["date"] <= end)]
        traded = gw[gw["volume"] > 0].groupby("date")["value"].sum()
        turnover = traded.reindex(cal_w).fillna(0.0)  # дни без сделок = 0
        n_days = len(cal_w)
        n_traded = int(cal_w.isin(traded.index).sum())
        rows.append({
            "secid": secid,
            "first_date": g["date"].min().date(),
            "last_date": g["date"].max().date(),
            "window_end": end.date(),
            "n_days": n_days,
            "n_traded_days": n_traded,
            "median_value": float(turnover.median()) if n_days else np.nan,
            "mean_value": float(turnover.mean()) if n_days else np.nan,
            "zero_volume_share": float(1.0 - n_traded / n_days) if n_days else np.nan,
        })
    return pd.DataFrame(rows, columns=LIQUIDITY_COLUMNS)
