"""Telegram Bot API notifier supporting poster photos and rich HTML captions."""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import requests

from .ed2k import ED2KResult, format_file_size
from .media_parser import MediaSpecs, extract_media_specs
from .telegraph import TelegraphClient, TelegraphError
from .tmdb import TMDBMatch

TELEGRAM_PHOTO_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_FOLLOWUP_LIMIT = 3900

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def parse_chat_ids(chat_id: str | Sequence[str]) -> list[str]:
    """Parse single or multi-target Chat IDs separated by comma, semicolon, or newline."""
    if isinstance(chat_id, str):
        raw = chat_id.replace(";", ",").replace("\n", ",").replace("，", ",")
        return [c.strip() for c in raw.split(",") if c.strip()]
    return [str(c).strip() for c in chat_id if str(c).strip()]


def _sanitize_filename_part(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", text).strip(" ._")
    return cleaned or "ED2K_Batch"


@dataclass
class BatchItem:
    """Single computed file entry used for grouping and bundled Telegram delivery."""

    ed2k_result: ED2KResult
    tmdb_match: Optional[TMDBMatch] = None
    poster_bytes: Optional[bytes] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_label: str = ""
    parsed_title: str = ""
    media_type: str = "movie"
    specs: Optional[MediaSpecs] = None


def group_batch_items(items: Sequence[BatchItem]) -> list[list[BatchItem]]:
    """Group items so episodes of the same show & season are bundled together.

    - Matched TV shows group by (tmdb_id, season).
    - Matched movies group by (tmdb_id, "movie") so multi-part movies bundle while distinct movies stay separate.
    - Unmatched TV files group by (parent_folder, parsed_title, season).
    - Unmatched movies remain individual.
    """
    groups: dict[tuple, list[BatchItem]] = {}
    order: list[tuple] = []

    for idx, item in enumerate(items):
        if item.tmdb_match is not None:
            if item.tmdb_match.media_type == "tv":
                key: tuple = ("tmdb_tv", item.tmdb_match.tmdb_id, item.season)
            else:
                key = ("tmdb_movie", item.tmdb_match.tmdb_id)
        else:
            parent_dir = str(Path(item.ed2k_result.file_path).parent).lower()
            if item.media_type == "tv" and item.parsed_title:
                key = ("raw_tv", parent_dir, item.parsed_title.lower(), item.season)
            else:
                key = ("raw_single", idx)

        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    result: list[list[BatchItem]] = []
    for key in order:
        grp = sorted(
            groups[key],
            key=lambda x: (
                x.season if x.season is not None else 0,
                x.episode if x.episode is not None else 0,
                x.ed2k_result.file_name.lower(),
            ),
        )
        result.append(grp)
    return result


class TelegramError(Exception):
    """Raised when a Telegram Bot API call fails."""


def _visible_length(html_text: str) -> int:
    """Approximate Telegram's rendered text length (HTML tags stripped, entities unescaped)."""
    plain = html.unescape(_HTML_TAG_RE.sub("", html_text))
    return len(plain)


def build_notification_html(
    ed2k_result: ED2KResult,
    tmdb_match: Optional[TMDBMatch] = None,
    include_overview: bool = False,
    include_specs: bool = True,
    include_hashtags: bool = True,
    specs: Optional[MediaSpecs] = None,
    telegraph_url: str = "",
) -> tuple[str, Optional[str]]:
    """Format notification HTML for Telegram.

    Returns:
        (primary_html, followup_html):
            If telegraph_url is provided, links cleanly to the Telegraph note with no followup.
            Otherwise, if the full notification fits within Telegram's 1024-char photo caption limit,
            followup_html is None; else primary_html holds the summary and followup_html holds the ED2K link.
    """
    eff_specs = specs if specs is not None else extract_media_specs(ed2k_result.file_name)
    media_type = tmdb_match.media_type if tmdb_match is not None else "movie"
    lines: list[str] = []

    if tmdb_match is not None:
        icon = "📺" if tmdb_match.media_type == "tv" else "🎬"
        title_esc = html.escape(tmdb_match.title or tmdb_match.original_title)
        year_part = f" ({html.escape(tmdb_match.year)})" if tmdb_match.year else ""
        lines.append(f"{icon} <b>{title_esc}</b>{year_part}")

        if tmdb_match.original_title and tmdb_match.original_title != tmdb_match.title:
            lines.append(f"<i>{html.escape(tmdb_match.original_title)}</i>")

        if tmdb_match.episode_label:
            ep_str = html.escape(tmdb_match.episode_label)
            if tmdb_match.episode_name:
                ep_str += f" · {html.escape(tmdb_match.episode_name)}"
            lines.append(f"📌 <b>剧集:</b> {ep_str}")

        meta_parts: list[str] = [f"🏷 {tmdb_match.type_label_zh}"]
        if tmdb_match.vote_average > 0:
            meta_parts.append(f"⭐ {tmdb_match.vote_average:.1f}/10")
        meta_parts.append(f'🔗 <a href="{html.escape(tmdb_match.tmdb_url)}">TMDB 详情</a>')
        lines.append(" | ".join(meta_parts))

        if include_overview and tmdb_match.overview:
            overview = tmdb_match.overview.strip()
            if len(overview) > 160:
                overview = overview[:157] + "..."
            lines.append(f"📝 {html.escape(overview)}")

        lines.append("")
    else:
        lines.append("🎞 <b>ED2K 计算完成</b>")
        lines.append("")

    file_name_esc = html.escape(ed2k_result.file_name)
    size_human = format_file_size(ed2k_result.file_size)
    lines.append(f"📁 <b>文件:</b> <code>{file_name_esc}</code>")
    lines.append(f"💾 <b>大小:</b> {size_human} ({ed2k_result.file_size:,} 字节)")
    if include_specs and eff_specs.summary:
        lines.append(f"🎞 <b>规格:</b> {html.escape(eff_specs.summary)}")
    if include_hashtags:
        tags = eff_specs.to_hashtags(media_type=media_type)
        if tags:
            lines.append(" ".join(tags))

    summary_html = "\n".join(lines)

    if telegraph_url:
        tg_esc = html.escape(telegraph_url)
        note_html = f'{summary_html}\n\n🔗 <b>ED2K 链接笔记:</b> <a href="{tg_esc}">{tg_esc}</a>'
        if _visible_length(note_html) > TELEGRAM_PHOTO_CAPTION_LIMIT:
            trimmed = "\n".join(ln for ln in lines if not ln.startswith("📝 "))
            note_html = f'{trimmed}\n\n🔗 <b>ED2K 链接笔记:</b> <a href="{tg_esc}">{tg_esc}</a>'
        return note_html, None

    ed2k_block = f"\n\n🧲 <b>ED2K 链接:</b>\n<code>{html.escape(ed2k_result.ed2k_link)}</code>"
    full_html = summary_html + ed2k_block

    if _visible_length(full_html) <= TELEGRAM_PHOTO_CAPTION_LIMIT:
        return full_html, None

    # Caption would exceed Telegram's 1024-char photo caption limit;
    # keep poster caption concise and send the ED2K link in a reply message.
    if _visible_length(summary_html) > TELEGRAM_PHOTO_CAPTION_LIMIT - 20:
        trimmed_lines = [ln for ln in lines if not ln.startswith("📝 ")]
        summary_html = "\n".join(trimmed_lines)

    followup_html = f"🧲 <b>ED2K 链接 ({file_name_esc}):</b>\n<code>{html.escape(ed2k_result.ed2k_link)}</code>"
    return summary_html, followup_html


def build_batch_notification_html(
    items: Sequence[BatchItem],
    tmdb_match: Optional[TMDBMatch] = None,
    season: Optional[int] = None,
    include_overview: bool = False,
    include_specs: bool = True,
    include_hashtags: bool = True,
    telegraph_url: str = "",
) -> tuple[str, list[str]]:
    """Format a bundled multi-file / full-season notification for Telegram.

    Returns:
        (primary_html, followup_html_list):
            - If telegraph_url is provided, returns one clean poster caption containing
              the Telegraph link and an empty followup_html_list.
            - Otherwise, fits all ED2K links inline if <= 1024 chars, or packs them into
              one or more <pre> reply messages (each <= 4096 chars).
    """
    if not items:
        raise ValueError("items must not be empty")

    sorted_items = sorted(
        items,
        key=lambda x: (
            x.season if x.season is not None else 0,
            x.episode if x.episode is not None else 0,
            x.ed2k_result.file_name.lower(),
        ),
    )

    match = tmdb_match or next((it.tmdb_match for it in sorted_items if it.tmdb_match is not None), None)
    eff_season = season
    if eff_season is None:
        seasons = {it.season for it in sorted_items if it.season is not None}
        if len(seasons) == 1:
            eff_season = next(iter(seasons))

    total_size = sum(it.ed2k_result.file_size for it in sorted_items)
    count = len(sorted_items)
    is_tv = (match is not None and match.media_type == "tv") or any(it.media_type == "tv" for it in sorted_items)
    media_type = "tv" if is_tv else "movie"

    # Extract representative specs from first item (typical for a consistent season pack)
    rep_specs = sorted_items[0].specs or extract_media_specs(sorted_items[0].ed2k_result.file_name)

    # Episode range label e.g. "S02E01 - S02E10"
    ep_labels = [it.episode_label for it in sorted_items if it.episode_label]
    range_str = ""
    if len(ep_labels) >= 2:
        range_str = f" ({ep_labels[0]} - {ep_labels[-1]})"
    elif len(ep_labels) == 1:
        range_str = f" ({ep_labels[0]})"

    lines: list[str] = []
    if match is not None:
        icon = "📺" if match.media_type == "tv" else "🎬"
        title_esc = html.escape(match.title or match.original_title)
        year_part = f" ({html.escape(match.year)})" if match.year else ""
        season_part = f" · <b>第 {eff_season} 季</b>" if (match.media_type == "tv" and eff_season is not None) else ""
        lines.append(f"{icon} <b>{title_esc}</b>{year_part}{season_part}")

        if match.original_title and match.original_title != match.title:
            lines.append(f"<i>{html.escape(match.original_title)}</i>")

        meta_parts: list[str] = [f"🏷 {match.type_label_zh}"]
        if match.vote_average > 0:
            meta_parts.append(f"⭐ {match.vote_average:.1f}/10")
        meta_parts.append(f'🔗 <a href="{html.escape(match.tmdb_url)}">TMDB 详情</a>')
        lines.append(" | ".join(meta_parts))

        if include_overview and match.overview:
            overview = match.overview.strip()
            if len(overview) > 140:
                overview = overview[:137] + "..."
            lines.append(f"📝 {html.escape(overview)}")

        lines.append("")
    else:
        fallback_title = html.escape(sorted_items[0].parsed_title or "ED2K 合集计算完成")
        season_part = f" · <b>第 {eff_season} 季</b>" if eff_season is not None else ""
        lines.append(f"{'📺' if is_tv else '🎞'} <b>{fallback_title}</b>{season_part}")
        lines.append("")

    unit = "集" if is_tv else "个文件"
    lines.append(f"📦 <b>合集:</b> 共 {count} {unit}{html.escape(range_str)}")
    lines.append(f"💾 <b>总大小:</b> {format_file_size(total_size)}")
    if include_specs and rep_specs.summary:
        lines.append(f"🎞 <b>规格:</b> {html.escape(rep_specs.summary)}")
    if include_hashtags:
        tags = rep_specs.to_hashtags(media_type=media_type)
        if tags:
            lines.append(" ".join(tags))

    summary_html = "\n".join(lines)
    if _visible_length(summary_html) > TELEGRAM_PHOTO_CAPTION_LIMIT - 20:
        summary_html = "\n".join(ln for ln in lines if not ln.startswith("📝 "))

    if telegraph_url:
        tg_esc = html.escape(telegraph_url)
        note_html = f'{summary_html}\n\n🔗 <b>ED2K 链接笔记 (共 {count} 条):</b>\n<a href="{tg_esc}">{tg_esc}</a>'
        if _visible_length(note_html) > TELEGRAM_PHOTO_CAPTION_LIMIT:
            trimmed = "\n".join(ln for ln in lines if not ln.startswith("📝 "))
            note_html = f'{trimmed}\n\n🔗 <b>ED2K 链接笔记 (共 {count} 条):</b>\n<a href="{tg_esc}">{tg_esc}</a>'
        return note_html, []

    escaped_links = [html.escape(it.ed2k_result.ed2k_link) for it in sorted_items]
    all_links_block = "\n".join(escaped_links)

    # Check if the whole bundle fits inside a single photo caption
    inline_bundle = f"{summary_html}\n\n🧲 <b>ED2K 链接 (点击复制全部):</b>\n<pre>{all_links_block}</pre>"
    if _visible_length(inline_bundle) <= TELEGRAM_PHOTO_CAPTION_LIMIT:
        return inline_bundle, []

    # Otherwise, pack links into one or more reply messages (each <= 4096 visible chars)
    title_hint = (
        html.escape(match.title or match.original_title)
        if match is not None
        else html.escape(sorted_items[0].parsed_title or "合集")
    )
    season_tag = f" 第 {eff_season} 季" if eff_season is not None else ""

    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_len = 0
    header_budget = 140  # reserve space for header text

    for link_esc in escaped_links:
        link_visible_len = len(html.unescape(link_esc)) + 1
        if current_chunk and (current_len + link_visible_len + header_budget > SAFE_FOLLOWUP_LIMIT):
            chunks.append(current_chunk)
            current_chunk = [link_esc]
            current_len = link_visible_len
        else:
            current_chunk.append(link_esc)
            current_len += link_visible_len
    if current_chunk:
        chunks.append(current_chunk)

    total_parts = len(chunks)
    followups: list[str] = []
    for part_idx, chunk_links in enumerate(chunks, 1):
        part_suffix = f" ({part_idx}/{total_parts})" if total_parts > 1 else ""
        header = f"🧲 <b>{title_hint}{season_tag} ED2K 合集{part_suffix}（共 {len(chunk_links)} 条，点击代码块一键复制）：</b>"
        body = "\n".join(chunk_links)
        followups.append(f"{header}\n<pre>{body}</pre>")

    return summary_html, followups


def build_batch_txt_attachment(
    items: Sequence[BatchItem],
    tmdb_match: Optional[TMDBMatch] = None,
    season: Optional[int] = None,
) -> tuple[str, bytes]:
    """Build a UTF-8 .txt filename and bytes containing one ED2K link per line."""
    sorted_items = sorted(
        items,
        key=lambda x: (
            x.season if x.season is not None else 0,
            x.episode if x.episode is not None else 0,
            x.ed2k_result.file_name.lower(),
        ),
    )
    match = tmdb_match or next((it.tmdb_match for it in sorted_items if it.tmdb_match is not None), None)
    eff_season = season
    if eff_season is None:
        seasons = {it.season for it in sorted_items if it.season is not None}
        if len(seasons) == 1:
            eff_season = next(iter(seasons))

    base_name = (
        (match.title or match.original_title)
        if match is not None
        else (sorted_items[0].parsed_title or "ED2K_合集")
    )
    season_part = f"_S{eff_season:02d}" if eff_season is not None else ""
    filename = f"{_sanitize_filename_part(base_name)}{season_part}_ED2K.txt"
    content = "\n".join(it.ed2k_result.ed2k_link for it in sorted_items) + "\n"
    return filename, content.encode("utf-8")


class TelegramNotifier:
    """Sends ED2K calculation results with TMDB poster and metadata to one or more Telegram chats."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str | Sequence[str],
        thread_id: str = "",
        api_base: str = "https://api.telegram.org",
        proxies: Optional[dict[str, str]] = None,
        timeout: int = 30,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.chat_ids = parse_chat_ids(chat_id)
        self.chat_id = self.chat_ids[0] if self.chat_ids else ""
        self.thread_id = str(thread_id).strip()
        self.api_base = api_base.rstrip("/")
        self.proxies = proxies
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self._session = session or requests.Session()

    def _endpoint(self, method: str) -> str:
        if not self.bot_token:
            raise TelegramError("未配置 Telegram Bot Token。")
        return f"{self.api_base}/bot{self.bot_token}/{method}"

    def _base_payload(self, target_chat_id: Optional[str] = None) -> dict[str, Any]:
        cid = (target_chat_id or self.chat_id).strip()
        if not cid:
            raise TelegramError("未配置 Telegram Chat ID。")
        payload: dict[str, Any] = {"chat_id": cid}
        if self.thread_id:
            try:
                payload["message_thread_id"] = int(self.thread_id)
            except ValueError:
                pass
        return payload

    def _post(
        self,
        method: str,
        data: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = self._endpoint(method)
        last_error = ""

        for attempt in range(self.max_retries):
            try:
                resp = self._session.post(
                    url,
                    data=data,
                    files=files,
                    proxies=self.proxies,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"连接 Telegram 失败: {exc}"
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_backoff * (2**attempt))
                    continue
                raise TelegramError(last_error) from exc

            try:
                body = resp.json()
            except ValueError as exc:
                last_error = f"Telegram 返回非 JSON 响应 ({resp.status_code}): {resp.text[:200]}"
                if resp.status_code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(self.retry_backoff * (2**attempt))
                    continue
                raise TelegramError(last_error) from exc

            if resp.ok and body.get("ok"):
                return body

            desc = body.get("description") or resp.text[:200]
            # Retry on rate limit (429) or transient Telegram server errors (5xx)
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self.max_retries - 1:
                retry_after = (body.get("parameters") or {}).get("retry_after")
                delay = float(retry_after) if isinstance(retry_after, (int, float)) else (self.retry_backoff * (2**attempt))
                time.sleep(min(delay, 15.0))
                continue

            raise TelegramError(f"Telegram API 错误 ({resp.status_code}): {desc}")

        raise TelegramError(last_error or "Telegram 请求失败")

    @staticmethod
    def _build_telegraph_markup(telegraph_url: str, count: int = 1) -> Optional[dict[str, Any]]:
        if not telegraph_url:
            return None
        btn_text = (
            f"📄 打开 Telegraph 查看全部 {count} 条 ED2K"
            if count > 1
            else "📄 打开 Telegraph 查看 ED2K 链接"
        )
        return {"inline_keyboard": [[{"text": btn_text, "url": telegraph_url}]]}

    def send_message(
        self,
        text_html: str,
        reply_to_message_id: Optional[int] = None,
        disable_web_page_preview: bool = True,
        target_chat_id: Optional[str] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Send an HTML text message."""
        payload = self._base_payload(target_chat_id)
        payload["text"] = text_html
        payload["parse_mode"] = "HTML"
        payload["disable_web_page_preview"] = "true" if disable_web_page_preview else "false"
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._post("sendMessage", data=payload)

    def send_photo(
        self,
        caption_html: str,
        poster_bytes: Optional[bytes] = None,
        poster_url: str = "",
        target_chat_id: Optional[str] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Send a photo (preferring direct byte upload over remote URL fetch)."""
        payload = self._base_payload(target_chat_id)
        payload["caption"] = caption_html
        payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        if poster_bytes:
            files = {"photo": ("poster.jpg", poster_bytes, "image/jpeg")}
            return self._post("sendPhoto", data=payload, files=files)

        if poster_url:
            payload["photo"] = poster_url
            return self._post("sendPhoto", data=payload)

        raise TelegramError("缺少海报图片数据或 URL。")

    def send_document(
        self,
        file_bytes: bytes,
        filename: str,
        caption_html: str = "",
        reply_to_message_id: Optional[int] = None,
        target_chat_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send a document file (e.g., .txt ED2K link list)."""
        payload = self._base_payload(target_chat_id)
        if caption_html:
            payload["caption"] = caption_html
            payload["parse_mode"] = "HTML"
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        files = {"document": (filename, file_bytes, "text/plain; charset=utf-8")}
        return self._post("sendDocument", data=payload, files=files)

    def _send_notification_to_chat(
        self,
        target_chat_id: str,
        primary_html: str,
        followup_html: Optional[str],
        poster_bytes: Optional[bytes],
        poster_url: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        has_poster = bool(poster_bytes or poster_url)
        if has_poster:
            try:
                resp = self.send_photo(
                    caption_html=primary_html,
                    poster_bytes=poster_bytes,
                    poster_url=poster_url,
                    target_chat_id=target_chat_id,
                    reply_markup=reply_markup,
                )
            except TelegramError:
                if not poster_bytes and poster_url:
                    full_fallback = primary_html if not followup_html else f"{primary_html}\n\n{followup_html}"
                    return self.send_message(
                        full_fallback,
                        target_chat_id=target_chat_id,
                        disable_web_page_preview=not bool(reply_markup),
                        reply_markup=reply_markup,
                    )
                raise

            if followup_html:
                msg_id = (resp.get("result") or {}).get("message_id")
                self.send_message(followup_html, reply_to_message_id=msg_id, target_chat_id=target_chat_id)
            return resp

        full_text = primary_html if not followup_html else f"{primary_html}\n\n{followup_html}"
        return self.send_message(
            full_text,
            target_chat_id=target_chat_id,
            disable_web_page_preview=not bool(reply_markup),
            reply_markup=reply_markup,
        )

    def send_notification(
        self,
        ed2k_result: ED2KResult,
        tmdb_match: Optional[TMDBMatch] = None,
        poster_bytes: Optional[bytes] = None,
        include_overview: bool = False,
        include_specs: bool = True,
        include_hashtags: bool = True,
        specs: Optional[MediaSpecs] = None,
        telegraph_client: Optional[TelegraphClient] = None,
        telegraph_url: str = "",
        use_telegraph: bool = False,
    ) -> dict[str, Any]:
        """Send ED2K result + TMDB title & poster to all configured Telegram chats."""
        if not self.chat_ids:
            raise TelegramError("未配置 Telegram Chat ID。")

        eff_specs = specs if specs is not None else extract_media_specs(ed2k_result.file_name)
        tg_url = telegraph_url
        if not tg_url and use_telegraph and telegraph_client is not None:
            try:
                tg_url = telegraph_client.publish_ed2k_note(
                    [ed2k_result],
                    tmdb_match=tmdb_match,
                    specs_summary=eff_specs.summary if include_specs else "",
                )
            except TelegraphError:
                # Graceful fallback to inline ED2K code block if Telegraph is unreachable
                tg_url = ""

        primary_html, followup_html = build_notification_html(
            ed2k_result,
            tmdb_match=tmdb_match,
            include_overview=include_overview,
            include_specs=include_specs,
            include_hashtags=include_hashtags,
            specs=eff_specs,
            telegraph_url=tg_url,
        )
        poster_url = tmdb_match.poster_url if tmdb_match else ""
        markup = self._build_telegraph_markup(tg_url, count=1)

        last_resp: dict[str, Any] = {}
        errors: list[str] = []
        for cid in self.chat_ids:
            try:
                last_resp = self._send_notification_to_chat(
                    cid, primary_html, followup_html, poster_bytes, poster_url, reply_markup=markup
                )
            except TelegramError as exc:
                errors.append(f"[{cid}] {exc}")

        if errors:
            raise TelegramError("; ".join(errors))
        if tg_url:
            last_resp["telegraph_url"] = tg_url
        return last_resp

    def send_batch_notification(
        self,
        items: Sequence[BatchItem],
        tmdb_match: Optional[TMDBMatch] = None,
        poster_bytes: Optional[bytes] = None,
        season: Optional[int] = None,
        include_overview: bool = False,
        include_specs: bool = True,
        include_hashtags: bool = True,
        attach_txt: bool = False,
        telegraph_client: Optional[TelegraphClient] = None,
        telegraph_url: str = "",
        notify_link_mode: str = "inline",
    ) -> dict[str, Any]:
        """Send a bundled notification for a full season or multi-file group to all configured chats."""
        if not items:
            raise TelegramError("没有可推送的文件条目。")
        if not self.chat_ids:
            raise TelegramError("未配置 Telegram Chat ID。")

        if len(items) == 1:
            single = items[0]
            return self.send_notification(
                single.ed2k_result,
                tmdb_match=tmdb_match or single.tmdb_match,
                poster_bytes=poster_bytes or single.poster_bytes,
                include_overview=include_overview,
                include_specs=include_specs,
                include_hashtags=include_hashtags,
                specs=single.specs,
                telegraph_client=telegraph_client,
                telegraph_url=telegraph_url,
                use_telegraph=(notify_link_mode == "telegraph_all"),
            )

        sorted_items = sorted(
            items,
            key=lambda x: (
                x.season if x.season is not None else 0,
                x.episode if x.episode is not None else 0,
                x.ed2k_result.file_name.lower(),
            ),
        )
        match = tmdb_match or next((it.tmdb_match for it in sorted_items if it.tmdb_match is not None), None)
        img_bytes = poster_bytes or next((it.poster_bytes for it in sorted_items if it.poster_bytes), None)
        poster_url = match.poster_url if match else ""

        eff_season = season
        if eff_season is None:
            seasons = {it.season for it in sorted_items if it.season is not None}
            if len(seasons) == 1:
                eff_season = next(iter(seasons))

        rep_specs = sorted_items[0].specs or extract_media_specs(sorted_items[0].ed2k_result.file_name)

        tg_url = telegraph_url
        use_tg = notify_link_mode in ("telegraph_all", "telegraph_batch")
        if not tg_url and use_tg and telegraph_client is not None:
            try:
                tg_url = telegraph_client.publish_ed2k_note(
                    [it.ed2k_result for it in sorted_items],
                    tmdb_match=match,
                    season=eff_season,
                    specs_summary=rep_specs.summary if include_specs else "",
                    fallback_title=sorted_items[0].parsed_title,
                )
            except TelegraphError:
                tg_url = ""

        primary_html, followups = build_batch_notification_html(
            sorted_items,
            tmdb_match=match,
            season=eff_season,
            include_overview=include_overview,
            include_specs=include_specs,
            include_hashtags=include_hashtags,
            telegraph_url=tg_url,
        )
        txt_filename, txt_bytes = (
            build_batch_txt_attachment(sorted_items, tmdb_match=match, season=eff_season) if attach_txt else ("", b"")
        )
        markup = self._build_telegraph_markup(tg_url, count=len(sorted_items))

        last_resp: dict[str, Any] = {}
        errors: list[str] = []
        has_poster = bool(img_bytes or poster_url)

        for cid in self.chat_ids:
            try:
                if has_poster:
                    try:
                        resp = self.send_photo(
                            caption_html=primary_html,
                            poster_bytes=img_bytes,
                            poster_url=poster_url,
                            target_chat_id=cid,
                            reply_markup=markup,
                        )
                    except TelegramError:
                        if not img_bytes and poster_url:
                            resp = self.send_message(
                                primary_html,
                                target_chat_id=cid,
                                disable_web_page_preview=not bool(markup),
                                reply_markup=markup,
                            )
                        else:
                            raise
                else:
                    resp = self.send_message(
                        primary_html,
                        target_chat_id=cid,
                        disable_web_page_preview=not bool(markup),
                        reply_markup=markup,
                    )

                msg_id = (resp.get("result") or {}).get("message_id")
                for followup in followups:
                    self.send_message(followup, reply_to_message_id=msg_id, target_chat_id=cid)
                if attach_txt and txt_bytes:
                    self.send_document(
                        txt_bytes,
                        txt_filename,
                        caption_html=f"📄 <b>ED2K 链接清单</b>（共 {len(sorted_items)} 条）",
                        reply_to_message_id=msg_id,
                        target_chat_id=cid,
                    )
                last_resp = resp
            except TelegramError as exc:
                errors.append(f"[{cid}] {exc}")

        if errors:
            raise TelegramError("; ".join(errors))
        if tg_url:
            last_resp["telegraph_url"] = tg_url
        return last_resp

    def test_connection(self, send_test_message: bool = False) -> tuple[bool, str]:
        """Verify Bot Token via getMe and optionally send a test message to all configured chats."""
        try:
            url = self._endpoint("getMe")
            resp = self._session.get(url, proxies=self.proxies, timeout=self.timeout)
            body = resp.json()
            if not resp.ok or not body.get("ok"):
                return False, f"Bot Token 无效: {body.get('description', resp.status_code)}"
            bot_info = body.get("result") or {}
            bot_name = bot_info.get("username") or bot_info.get("first_name") or "Bot"

            if send_test_message:
                if not self.chat_ids:
                    return False, "请至少填写一个 Chat ID。"
                for cid in self.chat_ids:
                    self.send_message(
                        f"✅ <b>ED2K Notifier</b> 连接测试成功！\n🤖 Bot: @{html.escape(str(bot_name))}",
                        target_chat_id=cid,
                    )
                targets = ", ".join(self.chat_ids)
                return True, f"连接成功！已通过 @{bot_name} 向 {targets} 发送测试消息。"
            return True, f"Bot 连接成功: @{bot_name}"
        except (requests.RequestException, ValueError, TelegramError) as exc:
            return False, f"Telegram 测试失败: {exc}"
