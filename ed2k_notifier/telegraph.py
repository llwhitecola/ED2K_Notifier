"""Telegraph (telegra.ph) API client for publishing clean ED2K link notes."""

from __future__ import annotations

import json
import time
from typing import Any, Optional, Sequence

import requests

from .ed2k import ED2KResult, format_file_size
from .tmdb import TMDBMatch


class TelegraphError(Exception):
    """Raised when a Telegraph API call fails."""


class TelegraphClient:
    """Minimal, resilient client for https://api.telegra.ph."""

    def __init__(
        self,
        access_token: str = "",
        author_name: str = "ED2K Notifier",
        api_base: str = "https://api.telegra.ph",
        proxies: Optional[dict[str, str]] = None,
        timeout: int = 20,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.access_token = access_token.strip()
        self.author_name = (author_name or "ED2K Notifier").strip()[:128]
        self.api_base = api_base.rstrip("/")
        self.proxies = proxies
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self._session = session or requests.Session()

    def _post(self, method: str, data: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/{method.lstrip('/')}"
        last_err = ""
        for attempt in range(self.max_retries):
            try:
                resp = self._session.post(
                    url,
                    data=data,
                    proxies=self.proxies,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_err = f"连接 Telegraph 失败: {exc}"
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_backoff * (2**attempt))
                    continue
                raise TelegraphError(last_err) from exc

            try:
                body = resp.json()
            except ValueError as exc:
                last_err = f"Telegraph 返回非 JSON 响应 ({resp.status_code})"
                if resp.status_code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(self.retry_backoff * (2**attempt))
                    continue
                raise TelegraphError(last_err) from exc

            if resp.ok and body.get("ok"):
                return body.get("result") or {}

            err_msg = body.get("error") or resp.text[:200]
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self.max_retries - 1:
                time.sleep(self.retry_backoff * (2**attempt))
                continue
            raise TelegraphError(f"Telegraph API 错误: {err_msg}")

        raise TelegraphError(last_err or "Telegraph 请求失败")

    def ensure_account(self, short_name: str = "ED2KNotifier") -> str:
        """Ensure an access_token exists, creating an anonymous Telegraph account if needed."""
        if self.access_token:
            return self.access_token
        result = self._post(
            "createAccount",
            {
                "short_name": short_name[:32],
                "author_name": self.author_name,
            },
        )
        token = (result.get("access_token") or "").strip()
        if not token:
            raise TelegraphError("Telegraph createAccount 未返回 access_token。")
        self.access_token = token
        return token

    def create_page(
        self,
        title: str,
        content_nodes: list[dict[str, Any]],
        author_url: str = "",
    ) -> str:
        """Publish a Telegraph page and return its full https://telegra.ph/... URL."""
        token = self.ensure_account()
        payload: dict[str, Any] = {
            "access_token": token,
            "title": (title.strip() or "ED2K Links")[:256],
            "author_name": self.author_name,
            "content": json.dumps(content_nodes, ensure_ascii=False),
            "return_content": "false",
        }
        if author_url:
            payload["author_url"] = author_url
        result = self._post("createPage", payload)
        url = (result.get("url") or "").strip()
        if not url:
            raise TelegraphError("Telegraph createPage 未返回页面链接。")
        return url

    def publish_ed2k_note(
        self,
        ed2k_results: Sequence[ED2KResult],
        tmdb_match: Optional[TMDBMatch] = None,
        season: Optional[int] = None,
        specs_summary: str = "",
        fallback_title: str = "",
    ) -> str:
        """Create a clean Telegraph note listing ED2K links (one per line via <br>)."""
        if not ed2k_results:
            raise TelegraphError("没有可发布的 ED2K 链接。")

        count = len(ed2k_results)
        total_size = sum(r.file_size for r in ed2k_results)

        if tmdb_match is not None:
            base_title = tmdb_match.title or tmdb_match.original_title or fallback_title or "ED2K"
            if tmdb_match.year:
                page_title = f"{base_title} ({tmdb_match.year})"
            else:
                page_title = base_title
            if tmdb_match.media_type == "tv" and season is not None:
                page_title += f" 第 {season} 季"
            elif tmdb_match.episode_label and count == 1:
                page_title += f" {tmdb_match.episode_label}"
        else:
            page_title = fallback_title or ed2k_results[0].file_name
            if season is not None:
                page_title += f" 第 {season} 季"

        summary_parts = [f"{count} 条 ED2K 链接", f"总大小 {format_file_size(total_size)}"]
        if specs_summary:
            summary_parts.append(specs_summary)

        nodes: list[dict[str, Any]] = [
            {"tag": "p", "children": [" · ".join(summary_parts)]},
        ]

        # Pure ED2K links separated by <br> inside a single paragraph (matches telegra.ph note style)
        link_children: list[Any] = []
        for idx, res in enumerate(ed2k_results):
            if idx > 0:
                link_children.append({"tag": "br"})
            link_children.append(res.ed2k_link)
        nodes.append({"tag": "p", "children": link_children})

        author_url = tmdb_match.tmdb_url if tmdb_match is not None else ""
        return self.create_page(title=page_title, content_nodes=nodes, author_url=author_url)
