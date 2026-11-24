"""TMDB API client for searching movie/TV metadata and downloading posters."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import time
from typing import Any, Optional

import requests

from .media_parser import ParsedMedia


@dataclass
class TMDBMatch:
    """Normalized TMDB movie or TV show record."""

    tmdb_id: int
    media_type: str  # "movie" or "tv"
    title: str
    original_title: str = ""
    release_date: str = ""
    overview: str = ""
    vote_average: float = 0.0
    popularity: float = 0.0
    poster_path: str = ""
    poster_url: str = ""
    episode_label: str = ""
    episode_name: str = ""

    @property
    def year(self) -> str:
        return self.release_date[:4] if len(self.release_date) >= 4 else ""

    @property
    def tmdb_url(self) -> str:
        kind = "tv" if self.media_type == "tv" else "movie"
        return f"https://www.themoviedb.org/{kind}/{self.tmdb_id}"

    @property
    def display_title(self) -> str:
        base = self.title or self.original_title or f"TMDB #{self.tmdb_id}"
        if self.year:
            return f"{base} ({self.year})"
        return base

    @property
    def type_label_zh(self) -> str:
        return "电视剧" if self.media_type == "tv" else "电影"


class TMDBError(Exception):
    """Raised when a TMDB API request fails."""


class TMDBClient:
    """Client for The Movie Database (TMDB) API v3/v4."""

    def __init__(
        self,
        api_key: str,
        language: str = "zh-CN",
        api_base: str = "https://api.themoviedb.org/3",
        image_base: str = "https://image.tmdb.org/t/p",
        poster_size: str = "w780",
        proxies: Optional[dict[str, str]] = None,
        timeout: int = 15,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.language = language or "zh-CN"
        self.api_base = api_base.rstrip("/")
        self.image_base = image_base.rstrip("/")
        self.poster_size = poster_size or "w780"
        self.proxies = proxies
        self.timeout = timeout
        self._session = session or requests.Session()

    @property
    def _is_bearer_token(self) -> bool:
        return self.api_key.startswith("eyJ") or len(self.api_key) > 40

    def _request(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not self.api_key:
            raise TMDBError("未配置 TMDB API Key / Read Access Token。")

        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        req_params = dict(params or {})
        headers = {"Accept": "application/json"}

        if self._is_bearer_token:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            req_params["api_key"] = self.api_key

        max_attempts = 3
        resp = None
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                resp = self._session.get(
                    url,
                    params=req_params,
                    headers=headers,
                    proxies=self.proxies,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < max_attempts - 1:
                    time.sleep(1.0 * (2**attempt))
                    continue
                raise TMDBError(f"连接 TMDB 失败: {exc}") from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_attempts - 1:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after and retry_after.isdigit()) else (1.0 * (2**attempt))
                    time.sleep(min(delay, 10.0))
                    continue
            break

        assert resp is not None
        if resp.status_code == 401:
            raise TMDBError("TMDB 鉴权失败 (401)，请检查 API Key 或 Read Access Token。")
        if resp.status_code == 404:
            raise TMDBError("TMDB 未找到对应条目 (404)。")
        if not resp.ok:
            raise TMDBError(f"TMDB 请求出错 ({resp.status_code}): {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as exc:
            raise TMDBError("TMDB 返回了无效的 JSON 响应。") from exc

    def build_poster_url(self, poster_path: Optional[str], size: Optional[str] = None) -> str:
        if not poster_path:
            return ""
        clean_path = poster_path if poster_path.startswith("/") else f"/{poster_path}"
        return f"{self.image_base}/{size or self.poster_size}{clean_path}"

    def _parse_item(self, item: dict[str, Any], forced_type: Optional[str] = None) -> Optional[TMDBMatch]:
        media_type = forced_type or item.get("media_type")
        if media_type not in ("movie", "tv"):
            return None

        tmdb_id = item.get("id")
        if not isinstance(tmdb_id, int):
            return None

        if media_type == "tv":
            title = item.get("name") or item.get("original_name") or ""
            original_title = item.get("original_name") or ""
            release_date = item.get("first_air_date") or ""
        else:
            title = item.get("title") or item.get("original_title") or ""
            original_title = item.get("original_title") or ""
            release_date = item.get("release_date") or ""

        poster_path = item.get("poster_path") or ""
        return TMDBMatch(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title.strip(),
            original_title=original_title.strip(),
            release_date=release_date,
            overview=(item.get("overview") or "").strip(),
            vote_average=float(item.get("vote_average") or 0.0),
            popularity=float(item.get("popularity") or 0.0),
            poster_path=poster_path,
            poster_url=self.build_poster_url(poster_path),
        )

    def search(
        self,
        query: str,
        media_type: str = "multi",
        year: Optional[int] = None,
        limit: int = 8,
    ) -> list[TMDBMatch]:
        """Search TMDB for movies, TV shows, or both."""
        q = query.strip()
        if not q:
            return []

        endpoint_map = {
            "movie": "search/movie",
            "tv": "search/tv",
            "multi": "search/multi",
        }
        endpoint = endpoint_map.get(media_type, "search/multi")
        params: dict[str, Any] = {
            "query": q,
            "language": self.language,
            "include_adult": "false",
            "page": 1,
        }
        if year is not None:
            if media_type == "movie":
                params["year"] = year
            elif media_type == "tv":
                params["first_air_date_year"] = year

        data = self._request(endpoint, params)
        results = data.get("results") or []
        forced = media_type if media_type in ("movie", "tv") else None

        matches: list[TMDBMatch] = []
        for item in results:
            parsed = self._parse_item(item, forced_type=forced)
            if parsed is not None:
                matches.append(parsed)
                if len(matches) >= limit:
                    break
        return matches

    def fetch_by_id(self, tmdb_id: int, media_type: str = "movie") -> TMDBMatch:
        """Fetch a specific movie or TV show directly by its TMDB ID."""
        kind = "tv" if media_type == "tv" else "movie"
        data = self._request(f"{kind}/{tmdb_id}", {"language": self.language})
        match = self._parse_item(data, forced_type=kind)
        if match is None:
            raise TMDBError(f"无法解析 TMDB 条目 {kind}/{tmdb_id}。")
        return match

    def fetch_episode_name(self, tv_id: int, season: int, episode: int) -> str:
        """Best-effort lookup of a TV episode's localized name."""
        try:
            data = self._request(
                f"tv/{tv_id}/season/{season}/episode/{episode}",
                {"language": self.language},
            )
            return (data.get("name") or "").strip()
        except TMDBError:
            return ""

    def fetch_season_poster_url(self, tv_id: int, season: int) -> str:
        """Best-effort lookup of a specific season's poster URL."""
        try:
            data = self._request(
                f"tv/{tv_id}/season/{season}",
                {"language": self.language},
            )
            poster_path = data.get("poster_path") or ""
            return self.build_poster_url(poster_path)
        except TMDBError:
            return ""

    @staticmethod
    def _score_candidate(match: TMDBMatch, parsed: ParsedMedia) -> float:
        score = 0.0

        # Best title similarity across all extracted queries
        best_sim = 0.0
        for q in parsed.search_queries or [parsed.title]:
            q_low = q.lower().strip()
            if not q_low:
                continue
            for candidate_title in (match.title, match.original_title):
                c_low = candidate_title.lower().strip()
                if not c_low:
                    continue
                if q_low == c_low:
                    best_sim = max(best_sim, 1.0)
                else:
                    sim = SequenceMatcher(None, q_low, c_low).ratio()
                    best_sim = max(best_sim, sim)
        score += best_sim * 60.0

        # Media type match bonus
        if match.media_type == parsed.media_type:
            score += 12.0

        # Year match bonus / penalty
        if parsed.year and match.year.isdigit():
            diff = abs(int(match.year) - parsed.year)
            if diff == 0:
                score += 20.0
            elif diff == 1:
                score += 8.0
            else:
                score -= min(15.0, diff * 2.0)

        # Poster & popularity tie-breakers
        if match.poster_path:
            score += 5.0
        score += min(match.popularity, 100.0) * 0.05
        return score

    def match_parsed_media(self, parsed: ParsedMedia, limit: int = 6) -> list[TMDBMatch]:
        """Find and rank TMDB candidates for a ParsedMedia object."""
        queries = parsed.search_queries or ([parsed.title] if parsed.title else [])
        if not queries:
            return []

        dedup: dict[tuple[str, int], TMDBMatch] = {}

        def _collect(items: list[TMDBMatch]) -> None:
            for item in items:
                key = (item.media_type, item.tmdb_id)
                if key not in dedup:
                    dedup[key] = item

        # Pass 1: search expected media type with year (if known)
        for q in queries:
            if parsed.year is not None:
                _collect(self.search(q, media_type=parsed.media_type, year=parsed.year, limit=limit))
            if not dedup:
                _collect(self.search(q, media_type=parsed.media_type, year=None, limit=limit))
            if dedup:
                break

        # Pass 2: if still empty, try multi-search across all queries
        if not dedup:
            for q in queries:
                _collect(self.search(q, media_type="multi", year=None, limit=limit))
                if dedup:
                    break

        ranked = sorted(
            dedup.values(),
            key=lambda m: self._score_candidate(m, parsed),
            reverse=True,
        )[:limit]

        # Enrich TV episode info for the top match
        if ranked and parsed.episode_label:
            for item in ranked:
                if item.media_type == "tv":
                    item.episode_label = parsed.episode_label
            top = ranked[0]
            if top.media_type == "tv" and parsed.season is not None and parsed.episode is not None:
                top.episode_name = self.fetch_episode_name(top.tmdb_id, parsed.season, parsed.episode)

        return ranked

    def download_poster(self, poster_url: str) -> Optional[bytes]:
        """Download poster image bytes using the configured proxy/session."""
        if not poster_url:
            return None
        try:
            resp = self._session.get(poster_url, proxies=self.proxies, timeout=self.timeout)
            if resp.ok and resp.content:
                return resp.content
        except requests.RequestException:
            return None
        return None

    def test_connection(self) -> tuple[bool, str]:
        """Verify TMDB API key/token and network connectivity."""
        try:
            data = self._request("configuration")
            if "images" in data:
                return True, "TMDB 连接成功，API 凭证有效！"
            return True, "TMDB 连接成功！"
        except TMDBError as exc:
            return False, str(exc)
