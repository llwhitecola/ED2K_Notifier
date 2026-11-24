"""SQLite-backed local hash cache and history store."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .config import get_app_dir
from .ed2k import ED2KResult, build_ed2k_link
from .media_parser import ParsedMedia, extract_media_specs
from .tmdb import TMDBMatch

DEFAULT_HISTORY_DB_PATH = get_app_dir() / "history.db"


@dataclass
class HistoryRecord:
    """Single row stored in local history.db."""

    id: int
    file_path: str
    file_name: str
    file_size: int
    mtime_ns: int
    ed2k_mode: str
    ed2k_hash: str
    ed2k_link: str
    media_type: str = "movie"
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_label: str = ""
    specs_summary: str = ""
    tmdb_id: Optional[int] = None
    tmdb_title: str = ""
    tmdb_original_title: str = ""
    tmdb_year: str = ""
    tmdb_rating: float = 0.0
    tmdb_poster_url: str = ""
    tmdb_overview: str = ""
    notified: bool = False
    notified_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_ed2k_result(
        self,
        uppercase_hash: bool = True,
        urlencode_non_ascii: bool = False,
    ) -> ED2KResult:
        hash_out = self.ed2k_hash.upper() if uppercase_hash else self.ed2k_hash.lower()
        link = build_ed2k_link(
            self.file_name,
            self.file_size,
            hash_out,
            uppercase_hash=uppercase_hash,
            urlencode_non_ascii=urlencode_non_ascii,
        )
        return ED2KResult(
            file_path=self.file_path,
            file_name=self.file_name,
            file_size=self.file_size,
            ed2k_hash=hash_out,
            ed2k_link=link,
            elapsed_seconds=0.0,
            mode=self.ed2k_mode,
        )

    def to_tmdb_match(self) -> Optional[TMDBMatch]:
        if not self.tmdb_id:
            return None
        return TMDBMatch(
            tmdb_id=int(self.tmdb_id),
            media_type="tv" if self.media_type == "tv" else "movie",
            title=self.tmdb_title,
            original_title=self.tmdb_original_title,
            release_date=f"{self.tmdb_year}-01-01" if self.tmdb_year else "",
            overview=self.tmdb_overview,
            vote_average=float(self.tmdb_rating or 0.0),
            poster_path="/cached" if self.tmdb_poster_url else "",
            poster_url=self.tmdb_poster_url,
            episode_label=self.episode_label,
        )


def _norm_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class HistoryStore:
    """Thread-safe SQLite store for ED2K instant-hash caching and history browsing."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_HISTORY_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path_norm TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    ed2k_mode TEXT NOT NULL,
                    ed2k_hash TEXT NOT NULL,
                    ed2k_link TEXT NOT NULL,
                    media_type TEXT DEFAULT 'movie',
                    season INTEGER,
                    episode INTEGER,
                    episode_label TEXT DEFAULT '',
                    specs_summary TEXT DEFAULT '',
                    tmdb_id INTEGER,
                    tmdb_title TEXT DEFAULT '',
                    tmdb_original_title TEXT DEFAULT '',
                    tmdb_year TEXT DEFAULT '',
                    tmdb_rating REAL DEFAULT 0.0,
                    tmdb_poster_url TEXT DEFAULT '',
                    tmdb_overview TEXT DEFAULT '',
                    notified INTEGER DEFAULT 0,
                    notified_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(file_path_norm, ed2k_mode)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_cache ON file_history(file_path_norm, file_size, mtime_ns, ed2k_mode)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_updated ON file_history(updated_at DESC)"
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> HistoryRecord:
        return HistoryRecord(
            id=int(row["id"]),
            file_path=row["file_path"],
            file_name=row["file_name"],
            file_size=int(row["file_size"]),
            mtime_ns=int(row["mtime_ns"]),
            ed2k_mode=row["ed2k_mode"],
            ed2k_hash=row["ed2k_hash"],
            ed2k_link=row["ed2k_link"],
            media_type=row["media_type"] or "movie",
            season=row["season"],
            episode=row["episode"],
            episode_label=row["episode_label"] or "",
            specs_summary=row["specs_summary"] or "",
            tmdb_id=row["tmdb_id"],
            tmdb_title=row["tmdb_title"] or "",
            tmdb_original_title=row["tmdb_original_title"] or "",
            tmdb_year=row["tmdb_year"] or "",
            tmdb_rating=float(row["tmdb_rating"] or 0.0),
            tmdb_poster_url=row["tmdb_poster_url"] or "",
            tmdb_overview=row["tmdb_overview"] or "",
            notified=bool(row["notified"]),
            notified_at=row["notified_at"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def lookup_cache(
        self,
        file_path: str | Path,
        file_size: int,
        mtime_ns: int,
        ed2k_mode: str = "blue",
    ) -> Optional[HistoryRecord]:
        """Return cached record if the exact file (path + size + mtime + mode) was computed before."""
        norm = _norm_path(file_path)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM file_history
                WHERE file_path_norm = ? AND file_size = ? AND mtime_ns = ? AND ed2k_mode = ?
                LIMIT 1
                """,
                (norm, file_size, mtime_ns, ed2k_mode),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def upsert_record(
        self,
        ed2k_result: ED2KResult,
        mtime_ns: int,
        parsed: Optional[ParsedMedia] = None,
        tmdb_match: Optional[TMDBMatch] = None,
        notified: Optional[bool] = None,
    ) -> None:
        """Insert or update a computed file record."""
        norm = _norm_path(ed2k_result.file_path)
        now = _now_str()
        media_type = (
            tmdb_match.media_type
            if tmdb_match is not None
            else (parsed.media_type if parsed is not None else "movie")
        )
        season = parsed.season if parsed else None
        episode = parsed.episode if parsed else None
        episode_label = (
            tmdb_match.episode_label
            if (tmdb_match and tmdb_match.episode_label)
            else (parsed.episode_label if parsed else "")
        )
        specs_summary = (
            parsed.specs.summary if parsed else extract_media_specs(ed2k_result.file_name).summary
        )

        tmdb_id = tmdb_match.tmdb_id if tmdb_match else None
        tmdb_title = tmdb_match.title if tmdb_match else ""
        tmdb_orig = tmdb_match.original_title if tmdb_match else ""
        tmdb_year = tmdb_match.year if tmdb_match else ""
        tmdb_rating = tmdb_match.vote_average if tmdb_match else 0.0
        tmdb_poster = tmdb_match.poster_url if tmdb_match else ""
        tmdb_overview = tmdb_match.overview if tmdb_match else ""

        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT notified, notified_at, created_at FROM file_history WHERE file_path_norm = ? AND ed2k_mode = ?",
                (norm, ed2k_result.mode),
            ).fetchone()

            if notified is True:
                notified_val = 1
                notified_at = now
            elif notified is False:
                notified_val = 0
                notified_at = ""
            elif existing is not None:
                notified_val = int(existing["notified"])
                notified_at = existing["notified_at"] or ""
            else:
                notified_val = 0
                notified_at = ""

            created_at = existing["created_at"] if existing is not None else now

            conn.execute(
                """
                INSERT INTO file_history (
                    file_path_norm, file_path, file_name, file_size, mtime_ns,
                    ed2k_mode, ed2k_hash, ed2k_link, media_type, season, episode,
                    episode_label, specs_summary, tmdb_id, tmdb_title,
                    tmdb_original_title, tmdb_year, tmdb_rating, tmdb_poster_url,
                    tmdb_overview, notified, notified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path_norm, ed2k_mode) DO UPDATE SET
                    file_path = excluded.file_path,
                    file_name = excluded.file_name,
                    file_size = excluded.file_size,
                    mtime_ns = excluded.mtime_ns,
                    ed2k_hash = excluded.ed2k_hash,
                    ed2k_link = excluded.ed2k_link,
                    media_type = excluded.media_type,
                    season = excluded.season,
                    episode = excluded.episode,
                    episode_label = excluded.episode_label,
                    specs_summary = excluded.specs_summary,
                    tmdb_id = COALESCE(excluded.tmdb_id, file_history.tmdb_id),
                    tmdb_title = CASE WHEN excluded.tmdb_title != '' THEN excluded.tmdb_title ELSE file_history.tmdb_title END,
                    tmdb_original_title = CASE WHEN excluded.tmdb_original_title != '' THEN excluded.tmdb_original_title ELSE file_history.tmdb_original_title END,
                    tmdb_year = CASE WHEN excluded.tmdb_year != '' THEN excluded.tmdb_year ELSE file_history.tmdb_year END,
                    tmdb_rating = CASE WHEN excluded.tmdb_rating > 0 THEN excluded.tmdb_rating ELSE file_history.tmdb_rating END,
                    tmdb_poster_url = CASE WHEN excluded.tmdb_poster_url != '' THEN excluded.tmdb_poster_url ELSE file_history.tmdb_poster_url END,
                    tmdb_overview = CASE WHEN excluded.tmdb_overview != '' THEN excluded.tmdb_overview ELSE file_history.tmdb_overview END,
                    notified = excluded.notified,
                    notified_at = excluded.notified_at,
                    updated_at = excluded.updated_at
                """,
                (
                    norm,
                    ed2k_result.file_path,
                    ed2k_result.file_name,
                    ed2k_result.file_size,
                    mtime_ns,
                    ed2k_result.mode,
                    ed2k_result.ed2k_hash,
                    ed2k_result.ed2k_link,
                    media_type,
                    season,
                    episode,
                    episode_label,
                    specs_summary,
                    tmdb_id,
                    tmdb_title,
                    tmdb_orig,
                    tmdb_year,
                    tmdb_rating,
                    tmdb_poster,
                    tmdb_overview,
                    notified_val,
                    notified_at,
                    created_at,
                    now,
                ),
            )

    def mark_notified(self, file_paths: Sequence[str], ed2k_mode: str = "blue") -> None:
        """Mark records as successfully pushed to Telegram."""
        if not file_paths:
            return
        now = _now_str()
        norms = [_norm_path(p) for p in file_paths]
        placeholders = ",".join("?" for _ in norms)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE file_history
                SET notified = 1, notified_at = ?, updated_at = ?
                WHERE ed2k_mode = ? AND file_path_norm IN ({placeholders})
                """,
                [now, now, ed2k_mode, *norms],
            )

    def list_records(self, query: str = "", limit: int = 500) -> list[HistoryRecord]:
        """Search and list history records ordered by most recently updated."""
        q = query.strip()
        with self._lock, self._connect() as conn:
            if q:
                like = f"%{q}%"
                rows = conn.execute(
                    """
                    SELECT * FROM file_history
                    WHERE file_name LIKE ? OR tmdb_title LIKE ? OR tmdb_original_title LIKE ? OR ed2k_hash LIKE ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (like, like, like, like, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM file_history ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def delete_records(self, record_ids: Sequence[int]) -> None:
        if not record_ids:
            return
        placeholders = ",".join("?" for _ in record_ids)
        with self._lock, self._connect() as conn:
            conn.execute(f"DELETE FROM file_history WHERE id IN ({placeholders})", list(record_ids))

    def clear_all(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM file_history")
