"""Smart filename & folder parser for movies and TV shows (Chinese & English)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".ts",
    ".m2ts",
    ".mov",
    ".wmv",
    ".flv",
    ".rmvb",
    ".rm",
    ".mpg",
    ".mpeg",
    ".m4v",
    ".webm",
    ".iso",
    ".vob",
}

_CN_NUM_MAP = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# Technical / release tags that mark the end of the media title
_TECH_TAG_PATTERN = re.compile(
    r"(?:^|[\s._\-\[\(【（])("
    r"2160p|1080p|1080i|720p|576p|480p|4k|8k|uhd|fhd|hd|"
    r"bluray|blu-ray|bdrip|bdremux|brrip|remux|web-dl|webdl|webrip|web|hdtv|dvdrip|dvd|hdtvrip|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|av1|xvid|divx|vc-?1|mpeg2|"
    r"10bit|8bit|hdr10\+?|hdr|dovi|dv|dolby[\s._-]*vision|sdr|imax|"
    r"dts-?hd|dts-?x|dts|truehd|atmos|ddp?[\s._-]*[257]\.?[01]|ac3|eac3|aac|flac|lpcm|opus|mp3|"
    r"proper|repack|extended|unrated|directors?[\s._-]*cut|theatrical|criterion|internal|complete|"
    r"nf|netflix|amzn|amazon|dsnp|disney\+?|hbo|hmax|atvp|appletv|pcok|hulu|baha|bilibili|iqiyi|youku|viki|"
    r"中字|中英|双语|雙語|国粤|國粵|国语|國語|粤语|粵語|简繁|簡繁|简体|繁體|内封|內封|内嵌|特效字幕|硬字幕|官方中字|无水印"
    r")(?=$|[\s._\-\]\)】）])",
    re.IGNORECASE,
)

# Leading bracket tags such as [SubtitleGroup], 【高清首发】, etc.
_LEADING_BRACKET_RE = re.compile(r"^\s*(?:[\[\(【（][^\]\)】）]{1,40}[\]\)】）]\s*)+")

# Generic folder names to skip when climbing up parent directories
_GENERIC_FOLDER_RE = re.compile(
    r"^(?:season\s*\d+|s\d{1,2}|第[零一二三四五六七八九十百\d]+季|bdmv|stream|video_ts|specials?|extras?|movies?|tv|series|downloads?|电影|电视剧|剧集|动漫|番剧)$",
    re.IGNORECASE,
)

# Season + Episode patterns
_SXXEXX_RE = re.compile(r"(?<![a-zA-Z0-9])S(\d{1,2})[\s._-]*E(\d{1,3})(?![a-zA-Z0-9])", re.IGNORECASE)
_NXNN_RE = re.compile(r"(?<![a-zA-Z0-9])(\d{1,2})x(\d{2,3})(?![a-zA-Z0-9])", re.IGNORECASE)
_CN_SEASON_RE = re.compile(r"第([零一二三四五六七八九十\d]+)季")
_EN_SEASON_RE = re.compile(r"(?<![a-zA-Z])(?:Season|S)[\s._-]*(\d{1,2})(?![a-zA-Z0-9])", re.IGNORECASE)
_CN_EPISODE_RE = re.compile(r"第\s*(\d{1,3})\s*[集话話期]")
_EP_ONLY_RE = re.compile(r"(?<![a-zA-Z0-9])(?:EP|E)[\s._-]?(\d{1,3})(?![a-zA-Z0-9])", re.IGNORECASE)
_ANIME_EP_RE = re.compile(r"[\s._]-[\s._](\d{2,3})(?=$|[\s._\[\(【（])")

# Year pattern: 1920..2039
_YEAR_RE = re.compile(r"(?<!\d)((?:19[2-9]\d)|(?:20[0-3]\d))(?!\d)")

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_LATIN_RE = re.compile(r"[A-Za-z]")


@dataclass
class MediaSpecs:
    """Technical video/audio/source specifications parsed from the filename."""

    resolution: str = ""
    source: str = ""
    platform: str = ""
    video_codec: str = ""
    hdr: str = ""
    audio: str = ""
    subtitles: str = ""

    @property
    def summary(self) -> str:
        parts = [
            self.resolution,
            self.hdr,
            self.source,
            self.platform,
            self.video_codec,
            self.audio,
            self.subtitles,
        ]
        return " · ".join(p for p in parts if p)

    def to_hashtags(self, media_type: str = "movie") -> list[str]:
        tags: list[str] = ["#电视剧" if media_type == "tv" else "#电影"]
        if self.resolution in ("2160p", "4K"):
            tags.append("#4K")
        elif self.resolution == "8K":
            tags.append("#8K")
        elif self.resolution == "1080p":
            tags.append("#1080p")

        if self.hdr:
            for token in self.hdr.split("/"):
                clean = token.strip()
                if clean == "杜比视界":
                    tags.append("#杜比视界")
                elif clean == "HDR10+":
                    tags.append("#HDR10Plus")
                elif clean == "HDR":
                    tags.append("#HDR")

        if self.source:
            src_map = {
                "REMUX": "#REMUX",
                "BluRay": "#BluRay",
                "WEB-DL": "#WEBDL",
                "WEBRip": "#WEBRip",
                "HDTV": "#HDTV",
            }
            if self.source in src_map:
                tags.append(src_map[self.source])

        if self.platform:
            plat_clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_]", "", self.platform)
            if plat_clean:
                tags.append(f"#{plat_clean}")

        if self.subtitles:
            tags.append("#中字")

        # Deduplicate while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for t in tags:
            k = t.lower()
            if k not in seen:
                seen.add(k)
                out.append(t)
        return out


def extract_media_specs(filename: str) -> MediaSpecs:
    """Extract resolution, source, streaming platform, HDR, codec, audio, and subtitle tags."""
    s = filename
    specs = MediaSpecs()

    # Resolution
    if re.search(r"(?:^|[\s._\-\[\(【])(4320p|8k)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.resolution = "8K"
    elif re.search(r"(?:^|[\s._\-\[\(【])(2160p|4k|uhd)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.resolution = "2160p"
    elif re.search(r"(?:^|[\s._\-\[\(【])(1080[pi])(?=$|[\s._\-\]\)】])", s, re.I):
        specs.resolution = "1080p"
    elif re.search(r"(?:^|[\s._\-\[\(【])(720p)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.resolution = "720p"

    # Source
    if re.search(r"(?:^|[\s._\-\[\(【])(bdremux|remux)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.source = "REMUX"
    elif re.search(r"(?:^|[\s._\-\[\(【])(bluray|blu-ray|bdrip|brrip)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.source = "BluRay"
    elif re.search(r"(?:^|[\s._\-\[\(【])(web-?dl)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.source = "WEB-DL"
    elif re.search(r"(?:^|[\s._\-\[\(【])(webrip)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.source = "WEBRip"
    elif re.search(r"(?:^|[\s._\-\[\(【])(hdtv|hdtvrip)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.source = "HDTV"

    # Streaming platform
    platform_patterns = [
        (r"(?:^|[\s._\-\[\(【])(nf|netflix)(?=$|[\s._\-\]\)】])", "Netflix"),
        (r"(?:^|[\s._\-\[\(【])(dsnp|disney\+?)(?=$|[\s._\-\]\)】])", "DisneyPlus"),
        (r"(?:^|[\s._\-\[\(【])(atvp|appletv\+?)(?=$|[\s._\-\]\)】])", "AppleTV"),
        (r"(?:^|[\s._\-\[\(【])(amzn|amazon)(?=$|[\s._\-\]\)】])", "Amazon"),
        (r"(?:^|[\s._\-\[\(【])(hmax|hbo)(?=$|[\s._\-\]\)】])", "HBO"),
        (r"(?:^|[\s._\-\[\(【])(hulu)(?=$|[\s._\-\]\)】])", "Hulu"),
        (r"(?:^|[\s._\-\[\(【])(baha|bilibili)(?=$|[\s._\-\]\)】])", "Bilibili"),
        (r"(?:^|[\s._\-\[\(【])(iqiyi)(?=$|[\s._\-\]\)】])", "爱奇艺"),
        (r"(?:^|[\s._\-\[\(【])(youku)(?=$|[\s._\-\]\)】])", "优酷"),
    ]
    for pat, label in platform_patterns:
        if re.search(pat, s, re.I):
            specs.platform = label
            break

    # HDR / Dolby Vision
    hdr_parts: list[str] = []
    if re.search(r"(?:^|[\s._\-\[\(【])(dovi|dv|dolby[\s._-]*vision)(?=$|[\s._\-\]\)】])", s, re.I):
        hdr_parts.append("杜比视界")
    if re.search(r"(?:^|[\s._\-\[\(【])(hdr10\+)(?=$|[\s._\-\]\)】])", s, re.I):
        hdr_parts.append("HDR10+")
    elif re.search(r"(?:^|[\s._\-\[\(【])(hdr10|hdr)(?=$|[\s._\-\]\)】])", s, re.I):
        hdr_parts.append("HDR")
    specs.hdr = "/".join(hdr_parts)

    # Video codec
    if re.search(r"(?:^|[\s._\-\[\(【])(x265|h\.?265|hevc)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.video_codec = "H.265"
    elif re.search(r"(?:^|[\s._\-\[\(【])(x264|h\.?264|avc)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.video_codec = "H.264"
    elif re.search(r"(?:^|[\s._\-\[\(【])(av1)(?=$|[\s._\-\]\)】])", s, re.I):
        specs.video_codec = "AV1"

    # Audio
    audio_parts: list[str] = []
    m_ddp = re.search(r"(?:^|[\s._\-\[\(【])(ddp|dd\+|eac3)[\s._-]?([257]\.?[01])?(?=$|[\s._\-\]\)】])", s, re.I)
    if re.search(r"(?:^|[\s._\-\[\(【])(truehd)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("TrueHD")
    elif re.search(r"(?:^|[\s._\-\[\(【])(dts-?x)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("DTS:X")
    elif re.search(r"(?:^|[\s._\-\[\(【])(dts-?hd)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("DTS-HD")
    elif m_ddp:
        ch = m_ddp.group(2) or ""
        audio_parts.append(f"DDP{ch}")
    elif re.search(r"(?:^|[\s._\-\[\(【])(dts)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("DTS")
    elif re.search(r"(?:^|[\s._\-\[\(【])(ac3|dd5\.1)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("AC3")
    elif re.search(r"(?:^|[\s._\-\[\(【])(flac)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("FLAC")
    elif re.search(r"(?:^|[\s._\-\[\(【])(aac)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("AAC")

    if re.search(r"(?:^|[\s._\-\[\(【])(atmos)(?=$|[\s._\-\]\)】])", s, re.I):
        audio_parts.append("Atmos")
    specs.audio = " ".join(audio_parts)

    # Subtitles / Chinese audio hints
    if re.search(r"(简繁|簡繁|双语|雙語|中英)", s):
        specs.subtitles = "简繁/中英字幕"
    elif re.search(r"(中字|内封|內封|官方中字)", s):
        specs.subtitles = "中字"
    elif re.search(r"(国粤|國粵)", s):
        specs.subtitles = "国粤双语"

    return specs


@dataclass
class ParsedMedia:
    """Structured metadata extracted from a media filename and its folder path."""

    raw_name: str
    title: str
    search_queries: list[str] = field(default_factory=list)
    media_type: str = "movie"  # "movie" or "tv"
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    specs: MediaSpecs = field(default_factory=MediaSpecs)

    @property
    def episode_label(self) -> str:
        if self.season is not None and self.episode is not None:
            return f"S{self.season:02d}E{self.episode:02d}"
        if self.episode is not None:
            return f"E{self.episode:02d}"
        if self.season is not None:
            return f"S{self.season:02d}"
        return ""


def _parse_cn_number(text: str) -> Optional[int]:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        parts = text.split("十", 1)
        tens = _CN_NUM_MAP.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM_MAP.get(parts[1], 0) if parts[1] else 0
        return tens * 10 + ones
    if len(text) == 1 and text in _CN_NUM_MAP:
        return _CN_NUM_MAP[text]
    return None


def _clean_title_segment(text: str) -> str:
    # Strip trailing/leading brackets and separators
    cleaned = _LEADING_BRACKET_RE.sub("", text)
    # Remove any remaining bracketed technical/note chunks like [1080p] or (BD)
    cleaned = re.sub(r"[\[\(【（][^\]\)】）]*[\]\)】）]", " ", cleaned)
    # Replace dots and underscores with spaces (preserve apostrophes and hyphens inside words)
    cleaned = re.sub(r"[._]+", " ", cleaned)
    # Collapse spaces and trim punctuation and dangling brackets
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:：|/\\([【（)]】）")
    return cleaned


def _split_bilingual_queries(title: str) -> list[str]:
    """Extract Chinese, English, and combined search queries from a bilingual title.

    Example: "流浪地球2 The Wandering Earth II"
    -> ["流浪地球2", "The Wandering Earth II", "流浪地球2 The Wandering Earth II"]
    """
    if not title:
        return []

    queries: list[str] = []
    has_cjk = bool(_CJK_RE.search(title))
    has_latin = bool(_LATIN_RE.search(title))

    if has_cjk and has_latin:
        # Case 1: CJK part followed by Latin part (most common in Chinese PT/ED2K releases)
        m_cjk_first = re.match(
            r"^([\u4e00-\u9fff\u3400-\u4dbf0-9：:·\-·\s]+?)\s+([A-Za-z][A-Za-z0-9\s:'\-&!?,]+)$",
            title,
        )
        if m_cjk_first:
            cn_part = m_cjk_first.group(1).strip(" -–—:：")
            en_part = m_cjk_first.group(2).strip(" -–—:：")
            if cn_part:
                queries.append(cn_part)
            if en_part and len(en_part) >= 2:
                queries.append(en_part)
        else:
            # Case 2: Latin part followed by CJK part
            m_en_first = re.match(
                r"^([A-Za-z][A-Za-z0-9\s:'\-&!?,]+?)\s+([\u4e00-\u9fff\u3400-\u4dbf0-9：:·\-·\s]+)$",
                title,
            )
            if m_en_first:
                en_part = m_en_first.group(1).strip(" -–—:：")
                cn_part = m_en_first.group(2).strip(" -–—:：")
                if cn_part:
                    queries.append(cn_part)
                if en_part and len(en_part) >= 2:
                    queries.append(en_part)

    if title not in queries:
        queries.append(title)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


def _parse_single_name(name: str) -> ParsedMedia:
    """Parse a single filename stem or directory name."""
    working = name.strip()
    working = _LEADING_BRACKET_RE.sub("", working)

    season: Optional[int] = None
    episode: Optional[int] = None
    media_type = "movie"
    cut_positions: list[int] = []

    # 1. Detect season & episode
    m_sxe = _SXXEXX_RE.search(working)
    if m_sxe:
        season = int(m_sxe.group(1))
        episode = int(m_sxe.group(2))
        media_type = "tv"
        cut_positions.append(m_sxe.start())
    else:
        m_nxn = _NXNN_RE.search(working)
        if m_nxn:
            season = int(m_nxn.group(1))
            episode = int(m_nxn.group(2))
            media_type = "tv"
            cut_positions.append(m_nxn.start())

    m_cn_season = _CN_SEASON_RE.search(working)
    if m_cn_season:
        parsed_s = _parse_cn_number(m_cn_season.group(1))
        if parsed_s is not None and season is None:
            season = parsed_s
        media_type = "tv"
        cut_positions.append(m_cn_season.start())

    m_cn_ep = _CN_EPISODE_RE.search(working)
    if m_cn_ep:
        if episode is None:
            episode = int(m_cn_ep.group(1))
        media_type = "tv"
        cut_positions.append(m_cn_ep.start())

    if episode is None:
        m_ep = _EP_ONLY_RE.search(working)
        if m_ep:
            episode = int(m_ep.group(1))
            media_type = "tv"
            cut_positions.append(m_ep.start())

    if season is None and media_type == "movie":
        m_en_s = _EN_SEASON_RE.search(working)
        if m_en_s:
            season = int(m_en_s.group(1))
            media_type = "tv"
            cut_positions.append(m_en_s.start())

    if episode is None:
        m_anime = _ANIME_EP_RE.search(working)
        if m_anime:
            episode = int(m_anime.group(1))
            media_type = "tv"
            cut_positions.append(m_anime.start())

    # 2. Detect release year (avoid mistaking numeric movie titles like "1917" or "2046" at index 0)
    year: Optional[int] = None
    year_matches = list(_YEAR_RE.finditer(working))
    for ym in year_matches:
        # Must have non-empty title text before the year, otherwise it's probably the title itself
        prefix = _clean_title_segment(working[: ym.start()])
        if prefix:
            year = int(ym.group(1))
            cut_positions.append(ym.start())
            break

    # 3. Detect technical tags (1080p, BluRay, WEB-DL, x265, etc.)
    m_tech = _TECH_TAG_PATTERN.search(working)
    if m_tech:
        cut_positions.append(m_tech.start())

    # 4. Extract title before the earliest cutoff marker
    if cut_positions:
        cut_at = min(cut_positions)
        title_raw = working[:cut_at]
    else:
        title_raw = working

    title = _clean_title_segment(title_raw)
    queries = _split_bilingual_queries(title)

    return ParsedMedia(
        raw_name=name,
        title=title,
        search_queries=queries,
        media_type=media_type,
        year=year,
        season=season,
        episode=episode,
    )


def parse_media_path(file_path: str | Path) -> ParsedMedia:
    """Parse movie/TV metadata from a file path, falling back to parent folders when needed."""
    path = Path(file_path)
    stem = path.stem if path.suffix.lower() in VIDEO_EXTENSIONS else path.name
    parsed = _parse_single_name(stem)

    # If the filename only contained episode numbers (e.g., "S01E01.mkv", "01.mkv"),
    # inspect parent folders for the show/movie title and year.
    is_generic_stem = not parsed.title or parsed.title.isdigit() or len(parsed.title) <= 1
    if is_generic_stem:
        for parent in list(path.parents)[:3]:
            folder_name = parent.name
            if not folder_name or _GENERIC_FOLDER_RE.match(folder_name.strip()):
                # Still extract season from "Season 2" / "第2季" folder if missing
                if parsed.season is None and folder_name:
                    m_s = _EN_SEASON_RE.search(folder_name) or _CN_SEASON_RE.search(folder_name)
                    if m_s:
                        val = _parse_cn_number(m_s.group(1))
                        if val is not None:
                            parsed.season = val
                            parsed.media_type = "tv"
                continue

            folder_parsed = _parse_single_name(folder_name)
            if folder_parsed.title:
                parsed.title = folder_parsed.title
                parsed.search_queries = folder_parsed.search_queries
                if parsed.year is None:
                    parsed.year = folder_parsed.year
                if parsed.season is None and folder_parsed.season is not None:
                    parsed.season = folder_parsed.season
                if folder_parsed.media_type == "tv":
                    parsed.media_type = "tv"
                break

    # Final fallback: use cleaned stem if still empty
    if not parsed.title:
        fallback = _clean_title_segment(stem) or stem
        parsed.title = fallback
        parsed.search_queries = [fallback]

    parsed.specs = extract_media_specs(stem)
    return parsed
