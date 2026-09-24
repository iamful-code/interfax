"""Классификация типов сообщений e-disclosure по заголовку (config/event_types.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from ..config import PROJECT_ROOT

DEFAULT_TAXONOMY_PATH = PROJECT_ROOT / "config" / "event_types.yaml"


@dataclass
class Category:
    name: str
    title_ru: str
    direction_rule: str  # stake_change | positive | negative | neutral
    patterns: list[re.Pattern] = field(default_factory=list)

    def matches(self, title: str) -> bool:
        return any(p.search(title) for p in self.patterns)


def _normalize(title: str) -> str:
    t = title.replace(" ", " ").replace("ё", "е").replace("Ё", "Е")
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


class Taxonomy:
    """Порядок категорий важен: возвращается первая подошедшая; последняя (без паттернов) -- запасная."""

    def __init__(self, categories: Iterable[Category]):
        self.categories = list(categories)
        self.by_name = {c.name: c for c in self.categories}
        fallback = [c for c in self.categories if not c.patterns]
        self.fallback = fallback[-1] if fallback else Category("other", "Прочее", "neutral")

    @classmethod
    def from_yaml(cls, path: Optional[Path] = None) -> "Taxonomy":
        path = Path(path) if path else DEFAULT_TAXONOMY_PATH
        data = yaml.safe_load(path.read_text("utf-8"))
        cats = []
        for item in data["categories"]:
            pats = [re.compile(_normalize(p).replace("\\s*", "\\S*") if False else _normalize_pattern(p), re.I) for p in item.get("patterns", [])]
            cats.append(Category(item["name"], item.get("title_ru", item["name"]), item.get("direction_rule", "neutral"), pats))
        return cls(cats)

    def classify(self, title: str) -> Category:
        t = _normalize(title or "")
        for c in self.categories:
            if c.patterns and c.matches(t):
                return c
        return self.fallback

    def classify_name(self, title: str) -> str:
        return self.classify(title).name

    def static_direction(self, category_name: str) -> int:
        rule = self.by_name.get(category_name, self.fallback).direction_rule
        return {"positive": 1, "negative": -1}.get(rule, 0)


def _normalize_pattern(p: str) -> str:
    # паттерны в yaml написаны в нижнем регистре без "ё"; нормализуем так же, как заголовки
    return p.replace("ё", "е")


_default: Optional[Taxonomy] = None


def default_taxonomy() -> Taxonomy:
    global _default
    if _default is None:
        _default = Taxonomy.from_yaml()
    return _default
