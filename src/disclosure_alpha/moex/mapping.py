"""Сопоставление компаний e-disclosure (ИНН, название) с бумагами MOEX (secid).

Основной ключ -- ИНН эмитента (``emitent_inn`` в списке бумаг ISS). Запасной инструмент --
подбор кандидатов по схожести названий (для ручной проверки).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

COMMON_SHARE_TYPES = {"common_share"}
PREFERRED_SHARE_TYPES = {"preferred_share"}

INN_MAP_COLUMNS = ["inn", "secid", "shortname", "is_traded", "type", "is_common", "is_preferred", "primary", "emitent_title"]
COMPANY_MAP_COLUMNS = ["company_id", "inn", "secid", "shortname", "is_traded", "type", "primary"]
CANDIDATE_COLUMNS = ["secid", "shortname", "emitent_title", "emitent_inn", "score"]

# Организационно-правовые формы и прочий "шум" в названиях (нижний регистр, без ё); ао/ап -- тип акций.
LEGAL_FORM_WORDS = {
    "пао", "оао", "ао", "зао", "ооо", "нко", "мкпао", "мкао", "мкооо", "гк", "ап",
    "публичное", "открытое", "закрытое", "акционерное", "общество", "международная", "компания",
    "с", "ограниченной", "ответственностью", "товарищество", "корпорация",
    "pjsc", "ojsc", "jsc", "cjsc", "llc", "ltd", "plc", "inc", "public", "joint", "stock",
    "company", "limited", "corporation", "nv", "sa", "ag",
}
_NON_WORD = re.compile(r"[^0-9a-zа-я]+")
_FLOAT_INT = re.compile(r"^\d+\.0+$")


# ----------------------------------------------------------------------------- ИНН
def normalize_inn(x: Any) -> Optional[str]:
    """ИНН -> строка из 10 (юрлицо) или 12 (физлицо/ИП) цифр; иначе None.

    Принимает str/int/float/NaN; пробелы, дефисы и префикс 'ИНН' отбрасываются, '7707083893.0' -> '7707083893'.
    """
    if x is None:
        return None
    if isinstance(x, float):
        if np.isnan(x):
            return None
        x = int(x)
    s = str(x).strip()
    if _FLOAT_INT.match(s):
        s = s.split(".", 1)[0]
    digits = re.sub(r"\D", "", s)
    return digits if len(digits) in (10, 12) else None


def _companies_with_inn(companies_df: pd.DataFrame) -> pd.DataFrame:
    """Копия таблицы компаний с нормализованной колонкой inn."""
    comp = companies_df.copy()
    comp["inn"] = comp["inn"].map(normalize_inn) if "inn" in comp.columns else None
    comp["inn"] = comp["inn"].astype(object).where(comp["inn"].notna(), None)
    return comp


# ----------------------------------------------------------------------------- ИНН -> бумаги
def build_inn_to_secids(securities_df: pd.DataFrame) -> pd.DataFrame:
    """Таблица ИНН -> бумаги (все строки: обыкновенные, привилегированные, прочие) с флагом ``primary``.

    Правило выбора primary (ровно одна бумага на ИНН):
      1) обыкновенная акция (type == common_share); если её нет -- привилегированная, иначе любая;
      2) среди них торгуемая (is_traded = 1) прежде неторгуемой;
      3) далее самый короткий secid, при равенстве -- первый по алфавиту.
    Строки без валидного ИНН отбрасываются.
    """
    if securities_df is None or securities_df.empty or "emitent_inn" not in securities_df.columns:
        return pd.DataFrame(columns=INN_MAP_COLUMNS)
    df = securities_df.copy()
    df["inn"] = df["emitent_inn"].map(normalize_inn)
    df = df[df["inn"].notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=INN_MAP_COLUMNS)
    df["inn"] = df["inn"].astype(object)
    df["is_traded"] = pd.to_numeric(df["is_traded"], errors="coerce").fillna(0).astype("int64") if "is_traded" in df.columns else 0
    typ = df["type"] if "type" in df.columns else pd.Series([None] * len(df), index=df.index, dtype="object")
    df["is_common"] = typ.isin(COMMON_SHARE_TYPES).astype(bool)
    df["is_preferred"] = typ.isin(PREFERRED_SHARE_TYPES).astype(bool)
    df["_type_rank"] = np.where(df["is_common"], 0, np.where(df["is_preferred"], 1, 2))
    df["_secid_len"] = df["secid"].astype(str).str.len()
    df = df.sort_values(
        ["inn", "_type_rank", "is_traded", "_secid_len", "secid"],
        ascending=[True, True, False, True, True], kind="stable",
    )
    df = df.drop_duplicates(["inn", "secid"], keep="first")
    df["primary"] = ~df.duplicated("inn", keep="first")
    return df.reindex(columns=INN_MAP_COLUMNS).reset_index(drop=True)


def map_companies_to_secids(
    companies_df: pd.DataFrame,
    securities_df: pd.DataFrame,
    primary_only: bool = False,
) -> pd.DataFrame:
    """Сопоставить компании e-disclosure (company_id, name, inn) с бумагами по нормализованному ИНН.

    Возвращает company_id, inn, secid, shortname, is_traded, type, primary (все бумаги эмитента;
    ``primary_only=True`` -- только основную). Несопоставленные компании исключаются, их число
    пишется в лог; список -- в unmatched_companies().
    """
    comp = _companies_with_inn(companies_df)
    inn_map = build_inn_to_secids(securities_df)
    if primary_only:
        inn_map = inn_map[inn_map["primary"]]
    keys = comp[["company_id", "inn"]].dropna(subset=["inn"]).drop_duplicates()
    merged = keys.merge(inn_map, on="inn", how="inner")
    n_total = comp["company_id"].nunique()
    n_matched = merged["company_id"].nunique()
    n_no_inn = comp.loc[comp["inn"].isna(), "company_id"].nunique()
    log.info(
        "map_companies_to_secids: %d/%d companies matched, %d unmatched (%d without valid INN)",
        n_matched, n_total, n_total - n_matched, n_no_inn,
    )
    merged = merged.sort_values(["company_id", "primary", "secid"], ascending=[True, False, True], kind="stable")
    return merged[COMPANY_MAP_COLUMNS].reset_index(drop=True)


def unmatched_companies(companies_df: pd.DataFrame, securities_df: pd.DataFrame) -> pd.DataFrame:
    """Компании без сопоставления: reason = 'no_inn' (нет/невалидный ИНН) или 'no_match' (ИНН нет в ISS)."""
    comp = _companies_with_inn(companies_df)
    known = set(build_inn_to_secids(securities_df)["inn"])
    no_inn = comp["inn"].isna()
    out = comp[no_inn | ~comp["inn"].isin(known)].copy()
    out["reason"] = np.where(out["inn"].isna(), "no_inn", "no_match")
    cols = [c for c in ("company_id", "name", "inn", "reason") if c in out.columns]
    return out[cols].reset_index(drop=True)


# ----------------------------------------------------------------------------- похожесть названий
def name_tokens(name: Any) -> set[str]:
    """Токены названия без ОПФ, кавычек и пунктуации; нижний регистр, ё -> е."""
    if name is None or (not isinstance(name, str) and pd.isna(name)):
        return set()
    s = str(name).lower().replace("ё", "е")
    return {t for t in _NON_WORD.split(s) if t and t not in LEGAL_FORM_WORDS}


def token_similarity(a: set[str], b: set[str]) -> float:
    """Коэффициент Жаккара по множествам токенов."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def fuzzy_name_candidates(name: str, securities_df: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """Похожие по названию бумаги -- подсказка для ручной проверки, когда ИНН не совпал.

    Сходство = max Жаккара по токенам между названием и полями emitent_title / name / shortname.
    Колонки: secid, shortname, emitent_title, emitent_inn, score (0..1); только score > 0.
    """
    query = name_tokens(name)
    fields = [c for c in ("emitent_title", "name", "shortname") if c in securities_df.columns]
    if not query or not fields or securities_df.empty:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)
    df = securities_df.drop_duplicates("secid")
    scores = np.zeros(len(df))
    for c in fields:
        scores = np.maximum(scores, [token_similarity(query, name_tokens(v)) for v in df[c]])
    out = df.reindex(columns=CANDIDATE_COLUMNS[:-1]).copy()
    out["score"] = scores
    out = out[out["score"] > 0].sort_values(["score", "secid"], ascending=[False, True], kind="stable")
    return out.head(top_n).reset_index(drop=True)
