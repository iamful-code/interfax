"""Настройки проекта: пути, лимиты запросов, адреса источников.

Все значения можно переопределить переменными окружения с префиксом ``DA_``
(например ``DA_DATA_DIR=/mnt/data``) или через ``Settings(...)`` в коде.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(f"DA_{name}", default)


@dataclass
class Settings:
    # --- каталоги ---
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(PROJECT_ROOT / "data"))))
    config_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "config")

    # --- e-disclosure.ru ---
    edisclosure_base_url: str = field(default_factory=lambda: _env("EDISCLOSURE_BASE_URL", "https://e-disclosure.ru"))
    edisclosure_min_interval_sec: float = field(default_factory=lambda: float(_env("EDISCLOSURE_MIN_INTERVAL", "1.5")))

    # --- MOEX ISS ---
    iss_base_url: str = field(default_factory=lambda: _env("ISS_BASE_URL", "https://iss.moex.com/iss"))
    iss_min_interval_sec: float = field(default_factory=lambda: float(_env("ISS_MIN_INTERVAL", "0.2")))
    iss_board: str = field(default_factory=lambda: _env("ISS_BOARD", "TQBR"))
    market_index: str = field(default_factory=lambda: _env("MARKET_INDEX", "IMOEX"))

    # --- HTTP ---
    user_agent: str = field(default_factory=lambda: _env(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    ))
    # cookies браузера для обхода антибот-проверки: файл cookies.txt (Netscape) или строка заголовка Cookie
    cookies_file: Optional[Path] = field(default_factory=lambda: (
        Path(os.environ["DA_COOKIES_FILE"]) if os.environ.get("DA_COOKIES_FILE") else PROJECT_ROOT / "config" / "cookies.txt"))
    timeout_sec: float = field(default_factory=lambda: float(_env("TIMEOUT", "60")))
    max_retries: int = field(default_factory=lambda: int(_env("MAX_RETRIES", "4")))

    # --- производные пути ---
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def prices_dir(self) -> Path:
        return self.data_dir / "prices"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def discovery_dir(self) -> Path:
        return self.data_dir / "discovery"

    def ensure_dirs(self) -> None:
        for p in (self.raw_dir, self.cache_dir, self.processed_dir, self.prices_dir, self.reports_dir, self.discovery_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()
