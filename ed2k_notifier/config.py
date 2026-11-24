"""Configuration management for ED2K Notifier."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def get_app_dir() -> Path:
    """Return the persistent application directory (next to .exe when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


DEFAULT_CONFIG_PATH = get_app_dir() / "config.json"


@dataclass
class AppConfig:
    """User configuration persisted to config.json."""

    # Telegram Bot settings
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_thread_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"

    # TMDB settings (supports both v3 API Key and v4 Read Access Token)
    tmdb_api_key: str = ""
    tmdb_language: str = "zh-CN"
    tmdb_api_base: str = "https://api.themoviedb.org/3"
    tmdb_image_base: str = "https://image.tmdb.org/t/p"
    tmdb_poster_size: str = "w780"

    # Telegraph (telegra.ph) note settings
    notify_link_mode: str = "telegraph_all"  # "telegraph_all" | "telegraph_batch" | "inline"
    telegraph_access_token: str = ""
    telegraph_author_name: str = "ED2K Notifier"
    telegraph_api_base: str = "https://api.telegra.ph"

    # Network proxy (e.g., http://127.0.0.1:7890 or socks5h://127.0.0.1:1080)
    proxy: str = ""

    # Behavior settings
    auto_notify_telegram: bool = True
    bundle_season_notify: bool = True
    enable_hash_cache: bool = True
    include_specs: bool = True
    include_hashtags: bool = True
    attach_txt_for_batch: bool = False
    ed2k_mode: str = "blue"  # "blue" (eMule standard) or "red" (legacy)
    ed2k_uppercase_hash: bool = True
    ed2k_urlencode_non_ascii: bool = False
    include_overview: bool = False

    # UI preferences (auto-persisted on close)
    theme: str = "dark"  # "dark" or "light"
    window_width: int = 1280
    window_height: int = 760
    splitter_sizes: list[int] = field(default_factory=lambda: [840, 410])
    column_widths: list[int] = field(default_factory=lambda: [350, 78, 74, 140, 88, 110])

    def get_proxies(self) -> dict[str, str] | None:
        """Return requests-compatible proxies dict, or None if no proxy configured."""
        proxy_url = self.proxy.strip()
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    def get_chat_ids(self) -> list[str]:
        """Parse one or more Chat IDs separated by comma, semicolon, or newline."""
        raw = self.telegram_chat_id.replace(";", ",").replace("\n", ",").replace("，", ",")
        return [cid.strip() for cid in raw.split(",") if cid.strip()]


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from JSON file, with optional environment variable overrides."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    valid_fields = {f.name for f in AppConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    cfg = AppConfig(**filtered)

    # Allow environment variables as fallback when config fields are empty
    if not cfg.telegram_bot_token:
        cfg.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not cfg.telegram_chat_id:
        cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not cfg.tmdb_api_key:
        cfg.tmdb_api_key = os.environ.get("TMDB_API_KEY", "") or os.environ.get("TMDB_TOKEN", "")
    if not cfg.proxy:
        cfg.proxy = os.environ.get("ED2K_PROXY", "")

    return cfg


def save_config(cfg: AppConfig, path: str | Path | None = None) -> Path:
    """Persist configuration to JSON file."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
    return config_path
