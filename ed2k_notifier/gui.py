"""PySide6 desktop GUI for ED2K Notifier."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QSize, Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import AppConfig, load_config, save_config
from .ed2k import (
    ED2KResult,
    HAS_FAST_MD4,
    HashCancelledError,
    calculate_ed2k_file,
    format_file_size,
)
from .history import HistoryRecord, HistoryStore
from .media_parser import VIDEO_EXTENSIONS, ParsedMedia, extract_media_specs, parse_media_path
from .telegram_bot import BatchItem, TelegramError, TelegramNotifier, group_batch_items
from .telegraph import TelegraphClient
from .tmdb import TMDBClient, TMDBError, TMDBMatch
from .ui_widgets import (
    EmptyDropZone,
    PillBadgeCell,
    SlimProgressWidget,
    SpecChipsWidget,
    ToggleSwitch,
    apply_windows_titlebar_theme,
    classify_status_tone,
    get_app_icon,
    get_stylesheet,
    make_icon,
    rounded_pixmap,
    set_windows_app_id,
)

DARK_STYLESHEET = get_stylesheet("dark")


@dataclass
class TaskItem:
    """State for a single queued file."""

    file_path: str
    file_name: str
    file_size: int
    mtime_ns: int
    parsed: ParsedMedia
    status: str = "等待计算"
    progress: int = 0
    ed2k_result: Optional[ED2KResult] = None
    tmdb_match: Optional[TMDBMatch] = None
    tmdb_candidates: list[TMDBMatch] = field(default_factory=list)
    poster_bytes: Optional[bytes] = None
    telegraph_url: str = ""
    notified: bool = False
    cached_hit: bool = False
    force_recompute: bool = False
    error_message: str = ""


def _format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds > 86400 * 7:
        return "--:--"
    secs = int(round(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _clone_tmdb_match(match: TMDBMatch) -> TMDBMatch:
    return TMDBMatch(
        tmdb_id=match.tmdb_id,
        media_type=match.media_type,
        title=match.title,
        original_title=match.original_title,
        release_date=match.release_date,
        overview=match.overview,
        vote_average=match.vote_average,
        popularity=match.popularity,
        poster_path=match.poster_path,
        poster_url=match.poster_url,
        episode_label=match.episode_label,
        episode_name=match.episode_name,
    )


def _task_to_batch_item(task: TaskItem) -> BatchItem:
    assert task.ed2k_result is not None
    return BatchItem(
        ed2k_result=task.ed2k_result,
        tmdb_match=task.tmdb_match,
        poster_bytes=task.poster_bytes,
        season=task.parsed.season,
        episode=task.parsed.episode,
        episode_label=task.parsed.episode_label,
        parsed_title=task.parsed.title,
        media_type=task.parsed.media_type,
        specs=task.parsed.specs,
    )


def _history_record_to_batch_item(rec: HistoryRecord, cfg: AppConfig) -> BatchItem:
    res = rec.to_ed2k_result(
        uppercase_hash=cfg.ed2k_uppercase_hash,
        urlencode_non_ascii=cfg.ed2k_urlencode_non_ascii,
    )
    return BatchItem(
        ed2k_result=res,
        tmdb_match=rec.to_tmdb_match(),
        poster_bytes=None,
        season=rec.season,
        episode=rec.episode,
        episode_label=rec.episode_label,
        parsed_title=rec.tmdb_title or rec.file_name,
        media_type=rec.media_type,
        specs=extract_media_specs(rec.file_name),
    )


def _make_telegraph_client(cfg: AppConfig) -> Optional[TelegraphClient]:
    if cfg.notify_link_mode == "inline":
        return None
    return TelegraphClient(
        access_token=cfg.telegraph_access_token,
        author_name=cfg.telegraph_author_name,
        api_base=cfg.telegraph_api_base,
        proxies=cfg.get_proxies(),
    )


def _persist_telegraph_token_if_created(cfg: AppConfig, tg_client: Optional[TelegraphClient]) -> None:
    if tg_client is not None and tg_client.access_token and tg_client.access_token != cfg.telegraph_access_token:
        cfg.telegraph_access_token = tg_client.access_token
        try:
            save_config(cfg)
        except OSError:
            pass


class DropTableWidget(QTableWidget):
    """QTableWidget supporting drag-and-drop of local files and folders."""

    files_dropped = Signal(list)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class ProcessWorker(QThread):
    """Sequential disk-hashing worker overlapped with async TMDB/poster network threads."""

    progress_updated = Signal(int, int, str)  # row, percent, status_text
    batch_progress = Signal(int, int, int, int, float, float)
    item_updated = Signal(int)  # row
    log_message = Signal(str)
    finished_all = Signal()

    def __init__(
        self,
        tasks: list[TaskItem],
        indices: list[int],
        config: AppConfig,
        history: HistoryStore,
    ) -> None:
        super().__init__()
        self.tasks = tasks
        self.indices = indices
        self.config = config
        self.history = history
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _make_tmdb_client(self) -> Optional[TMDBClient]:
        if not self.config.tmdb_api_key.strip():
            return None
        return TMDBClient(
            api_key=self.config.tmdb_api_key,
            language=self.config.tmdb_language,
            api_base=self.config.tmdb_api_base,
            image_base=self.config.tmdb_image_base,
            poster_size=self.config.tmdb_poster_size,
            proxies=self.config.get_proxies(),
        )

    def _make_tg_notifier(self) -> Optional[TelegramNotifier]:
        if not self.config.telegram_bot_token.strip() or not self.config.get_chat_ids():
            return None
        return TelegramNotifier(
            bot_token=self.config.telegram_bot_token,
            chat_id=self.config.get_chat_ids(),
            thread_id=self.config.telegram_thread_id,
            api_base=self.config.telegram_api_base,
            proxies=self.config.get_proxies(),
        )

    def run(self) -> None:
        tg_notifier = self._make_tg_notifier()
        telegraph_client = _make_telegraph_client(self.config)
        has_tmdb = bool(self.config.tmdb_api_key.strip())

        cache_lock = threading.Lock()
        search_cache: dict[tuple, list[TMDBMatch]] = {}
        season_poster_cache: dict[tuple[int, int], str] = {}
        poster_bytes_cache: dict[str, Optional[bytes]] = {}
        query_locks: dict[tuple, threading.Lock] = {}

        bundle_mode = self.config.auto_notify_telegram and self.config.bundle_season_notify
        computed_rows: list[int] = []

        valid_indices = [r for r in self.indices if 0 <= r < len(self.tasks)]
        total_files = len(valid_indices)
        total_bytes = sum(self.tasks[r].file_size for r in valid_indices)
        completed_bytes_before_current = 0
        disk_hashed_bytes = 0
        disk_elapsed_total = 0.0
        done_files = 0

        def _enrich_tmdb_and_save(row: int) -> None:
            task = self.tasks[row]
            if self._cancelled or task.ed2k_result is None:
                return

            if has_tmdb:
                tmdb_client = self._make_tmdb_client()
                assert tmdb_client is not None

                if task.tmdb_match is None:
                    self.progress_updated.emit(row, 100, "查询 TMDB")
                    cache_key = (
                        tuple(q.lower() for q in (task.parsed.search_queries or [task.parsed.title])),
                        task.parsed.media_type,
                        task.parsed.year,
                    )
                    with cache_lock:
                        q_lock = query_locks.setdefault(cache_key, threading.Lock())

                    try:
                        with q_lock:
                            with cache_lock:
                                cached_candidates = search_cache.get(cache_key)
                            if cached_candidates is not None:
                                candidates = [_clone_tmdb_match(c) for c in cached_candidates]
                            else:
                                candidates = tmdb_client.match_parsed_media(task.parsed)
                                with cache_lock:
                                    search_cache[cache_key] = [_clone_tmdb_match(c) for c in candidates]

                        task.tmdb_candidates = candidates
                        if candidates:
                            top = _clone_tmdb_match(candidates[0])
                            if top.media_type == "tv":
                                top.episode_label = task.parsed.episode_label
                                if task.parsed.season is not None:
                                    s_key = (top.tmdb_id, task.parsed.season)
                                    with cache_lock:
                                        s_poster = season_poster_cache.get(s_key)
                                    if s_poster is None:
                                        s_poster = tmdb_client.fetch_season_poster_url(
                                            top.tmdb_id, task.parsed.season
                                        )
                                        with cache_lock:
                                            season_poster_cache[s_key] = s_poster
                                    if s_poster:
                                        top.poster_url = s_poster
                                if task.parsed.season is not None and task.parsed.episode is not None:
                                    top.episode_name = tmdb_client.fetch_episode_name(
                                        top.tmdb_id, task.parsed.season, task.parsed.episode
                                    )
                            task.tmdb_match = top
                    except TMDBError as exc:
                        task.error_message = f"TMDB 查询失败: {exc}"
                        self.log_message.emit(f"TMDB 查询失败 [{task.file_name}]: {exc}")

                if task.tmdb_match is not None and task.poster_bytes is None and task.tmdb_match.poster_url:
                    url = task.tmdb_match.poster_url
                    with cache_lock:
                        cached_img = poster_bytes_cache.get(url, False)
                    if cached_img is False:
                        self.progress_updated.emit(row, 100, "下载海报")
                        img = tmdb_client.download_poster(url)
                        with cache_lock:
                            poster_bytes_cache[url] = img
                        task.poster_bytes = img
                    else:
                        task.poster_bytes = cached_img  # type: ignore[assignment]

            try:
                self.history.upsert_record(
                    task.ed2k_result,
                    mtime_ns=task.mtime_ns,
                    parsed=task.parsed,
                    tmdb_match=task.tmdb_match,
                )
            except Exception as exc:
                self.log_message.emit(f"写入缓存失败 [{task.file_name}]: {exc}")

            if self.config.auto_notify_telegram:
                if tg_notifier is None:
                    task.status = "已完成 (未配 TG)"
                elif bundle_mode:
                    task.status = "缓存命中 · 待推送" if task.cached_hit else "算完 · 待打包"
                else:
                    task.status = "推送中"
                    self.progress_updated.emit(row, 100, task.status)
                    try:
                        resp = tg_notifier.send_notification(
                            task.ed2k_result,
                            tmdb_match=task.tmdb_match,
                            poster_bytes=task.poster_bytes,
                            include_overview=self.config.include_overview,
                            include_specs=self.config.include_specs,
                            include_hashtags=self.config.include_hashtags,
                            specs=task.parsed.specs,
                            telegraph_client=telegraph_client,
                            use_telegraph=(self.config.notify_link_mode == "telegraph_all"),
                        )
                        _persist_telegraph_token_if_created(self.config, telegraph_client)
                        task.notified = True
                        task.telegraph_url = resp.get("telegraph_url", "")
                        task.status = "已推送"
                        self.history.mark_notified([task.file_path], ed2k_mode=self.config.ed2k_mode)
                        suffix = f" · {task.telegraph_url}" if task.telegraph_url else ""
                        self.log_message.emit(f"Telegram 推送成功: {task.file_name}{suffix}")
                    except TelegramError as exc:
                        task.status = "推送失败"
                        task.error_message = f"Telegram 推送失败: {exc}"
                        self.log_message.emit(f"Telegram 推送失败 [{task.file_name}]: {exc}")
            else:
                if task.cached_hit:
                    task.status = "缓存命中" + (" · 已推" if task.notified else "")
                else:
                    task.status = "计算完成"

            self.item_updated.emit(row)

        futures: list[concurrent.futures.Future] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for row in valid_indices:
                if self._cancelled:
                    break
                task = self.tasks[row]
                task.error_message = ""

                cached_rec: Optional[HistoryRecord] = None
                if self.config.enable_hash_cache and not task.force_recompute:
                    try:
                        cached_rec = self.history.lookup_cache(
                            task.file_path,
                            task.file_size,
                            task.mtime_ns,
                            ed2k_mode=self.config.ed2k_mode,
                        )
                    except Exception:
                        cached_rec = None

                if cached_rec is not None:
                    task.ed2k_result = cached_rec.to_ed2k_result(
                        uppercase_hash=self.config.ed2k_uppercase_hash,
                        urlencode_non_ascii=self.config.ed2k_urlencode_non_ascii,
                    )
                    task.cached_hit = True
                    task.notified = cached_rec.notified
                    if task.tmdb_match is None:
                        cached_match = cached_rec.to_tmdb_match()
                        if cached_match is not None:
                            task.tmdb_match = cached_match
                    task.progress = 100
                    task.status = "缓存命中"
                    completed_bytes_before_current += task.file_size
                    done_files += 1
                    avg_speed = (disk_hashed_bytes / (1024 * 1024)) / disk_elapsed_total if disk_elapsed_total > 0 else 0.0
                    rem_bytes = max(total_bytes - completed_bytes_before_current, 0)
                    eta = (rem_bytes / (1024 * 1024)) / avg_speed if avg_speed > 0 else 0.0
                    self.progress_updated.emit(row, 100, "缓存命中")
                    self.batch_progress.emit(done_files, total_files, completed_bytes_before_current, total_bytes, avg_speed, eta)
                    self.log_message.emit(f"秒算缓存命中: {task.file_name}")
                    computed_rows.append(row)
                    futures.append(pool.submit(_enrich_tmdb_and_save, row))
                    continue

                task.cached_hit = False
                task.force_recompute = False
                task.status = "计算中"
                self.progress_updated.emit(row, 0, task.status)
                file_start = time.perf_counter()

                try:

                    def _on_progress(
                        done: int,
                        total: int,
                        speed: float,
                        r: int = row,
                        base_bytes: int = completed_bytes_before_current,
                        f_start: float = file_start,
                    ) -> None:
                        pct = int((done * 100) / total) if total > 0 else 100
                        task.progress = pct
                        self.progress_updated.emit(r, pct, f"{speed:.0f} MB/s")

                        cur_total_done = base_bytes + done
                        elapsed_now = disk_elapsed_total + max(time.perf_counter() - f_start, 1e-6)
                        overall_speed = ((disk_hashed_bytes + done) / (1024 * 1024)) / elapsed_now
                        rem_b = max(total_bytes - cur_total_done, 0)
                        eta_s = (rem_b / (1024 * 1024)) / overall_speed if overall_speed > 0 else 0.0
                        self.batch_progress.emit(done_files, total_files, cur_total_done, total_bytes, overall_speed, eta_s)

                    result = calculate_ed2k_file(
                        task.file_path,
                        mode=self.config.ed2k_mode,
                        uppercase_hash=self.config.ed2k_uppercase_hash,
                        urlencode_non_ascii=self.config.ed2k_urlencode_non_ascii,
                        progress_callback=_on_progress,
                        cancel_check=lambda: self._cancelled,
                    )
                    task.ed2k_result = result
                    task.progress = 100
                    disk_hashed_bytes += task.file_size
                    disk_elapsed_total += max(result.elapsed_seconds, 1e-6)
                    completed_bytes_before_current += task.file_size
                    done_files += 1

                    overall_speed = (disk_hashed_bytes / (1024 * 1024)) / disk_elapsed_total
                    rem_b = max(total_bytes - completed_bytes_before_current, 0)
                    eta_s = (rem_b / (1024 * 1024)) / overall_speed if overall_speed > 0 else 0.0
                    self.batch_progress.emit(done_files, total_files, completed_bytes_before_current, total_bytes, overall_speed, eta_s)
                    self.log_message.emit(f"ED2K 计算完成: {task.file_name} ({result.speed_mb_s:.1f} MB/s)")
                    computed_rows.append(row)
                    futures.append(pool.submit(_enrich_tmdb_and_save, row))
                except HashCancelledError:
                    task.status = "已取消"
                    self.item_updated.emit(row)
                    break
                except Exception as exc:
                    task.status = "计算失败"
                    task.error_message = str(exc)
                    completed_bytes_before_current += task.file_size
                    done_files += 1
                    self.item_updated.emit(row)
                    self.log_message.emit(f"计算失败 [{task.file_name}]: {exc}")
                    continue

            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:
                    self.log_message.emit(f"后台元数据任务异常: {exc}")

        if bundle_mode and tg_notifier is not None and computed_rows and not self._cancelled:
            row_by_result_id = {
                id(self.tasks[r].ed2k_result): r for r in computed_rows if self.tasks[r].ed2k_result
            }
            batch_items = [
                _task_to_batch_item(self.tasks[r]) for r in computed_rows if self.tasks[r].ed2k_result
            ]
            groups = group_batch_items(batch_items)

            for group in groups:
                if self._cancelled:
                    break
                group_rows = [
                    row_by_result_id[id(it.ed2k_result)]
                    for it in group
                    if id(it.ed2k_result) in row_by_result_id
                ]
                for r in group_rows:
                    self.tasks[r].status = "打包推送中" if len(group) > 1 else "推送中"
                    self.progress_updated.emit(r, 100, self.tasks[r].status)

                label = (
                    group[0].tmdb_match.display_title
                    if group[0].tmdb_match
                    else (group[0].parsed_title or group[0].ed2k_result.file_name)
                )
                try:
                    resp = tg_notifier.send_batch_notification(
                        group,
                        include_overview=self.config.include_overview,
                        include_specs=self.config.include_specs,
                        include_hashtags=self.config.include_hashtags,
                        attach_txt=self.config.attach_txt_for_batch,
                        telegraph_client=telegraph_client,
                        notify_link_mode=self.config.notify_link_mode,
                    )
                    _persist_telegraph_token_if_created(self.config, telegraph_client)
                    tg_url = resp.get("telegraph_url", "")
                    done_text = "已打包推送" if len(group) > 1 else "已推送"
                    pushed_paths = [self.tasks[r].file_path for r in group_rows]
                    self.history.mark_notified(pushed_paths, ed2k_mode=self.config.ed2k_mode)
                    for r in group_rows:
                        self.tasks[r].notified = True
                        if tg_url:
                            self.tasks[r].telegraph_url = tg_url
                        self.tasks[r].status = done_text
                        self.tasks[r].error_message = ""
                        self.item_updated.emit(r)
                    suffix = f" · {tg_url}" if tg_url else ""
                    self.log_message.emit(f"Telegram 打包推送成功: {label}（共 {len(group)} 个文件）{suffix}")
                except TelegramError as exc:
                    for r in group_rows:
                        self.tasks[r].status = "推送失败"
                        self.tasks[r].error_message = f"Telegram 推送失败: {exc}"
                        self.item_updated.emit(r)
                    self.log_message.emit(f"Telegram 打包推送失败 [{label}]: {exc}")

        self.finished_all.emit()


class SingleActionWorker(QThread):
    """Lightweight worker for one-off network calls."""

    finished_result = Signal(bool, object)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            res = self._fn()
            self.finished_result.emit(True, res)
        except Exception as exc:
            self.finished_result.emit(False, str(exc))


class SettingsDialog(QDialog):
    """Configuration dialog for Telegram, Telegraph, TMDB, proxy, cache, and ED2K options."""

    def __init__(self, config: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置 — ED2K Notifier")
        self.setMinimumWidth(610)
        self._config = config
        self._active_worker: Optional[SingleActionWorker] = None
        self._build_ui()
        self._load_values(config)
        apply_windows_titlebar_theme(self, dark=(config.theme != "light"))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        tabs = QTabWidget()

        # --- Tab 1: Telegram ---
        tg_tab = QWidget()
        tg_form = QFormLayout(tg_tab)
        tg_form.setContentsMargins(14, 14, 14, 14)
        tg_form.setSpacing(10)

        self.tg_token_edit = QLineEdit()
        self.tg_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tg_token_edit.setPlaceholderText("从 @BotFather 获取，例如 123456:ABC-DEF...")

        token_row = QHBoxLayout()
        token_row.addWidget(self.tg_token_edit)
        self.tg_show_btn = QPushButton("显示")
        self.tg_show_btn.setFixedWidth(56)
        self.tg_show_btn.setCheckable(True)
        self.tg_show_btn.toggled.connect(
            lambda checked: self.tg_token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        token_row.addWidget(self.tg_show_btn)

        self.tg_chat_edit = QLineEdit()
        self.tg_chat_edit.setPlaceholderText("支持多个目标用逗号分隔，如: 123456, -100987654, @mychannel")

        self.tg_thread_edit = QLineEdit()
        self.tg_thread_edit.setPlaceholderText("可选：超级群组 Topic 的 message_thread_id")

        self.tg_api_base_edit = QLineEdit()
        self.tg_api_base_edit.setPlaceholderText("https://api.telegram.org")

        self.link_mode_combo = QComboBox()
        self.link_mode_combo.addItem("Telegraph 链接笔记（全部适用，海报 + 笔记链接，推荐）", "telegraph_all")
        self.link_mode_combo.addItem("仅合集用 Telegraph（单文件直接显示链接，整季生成笔记）", "telegraph_batch")
        self.link_mode_combo.addItem("直接发消息代码块（不使用 Telegraph）", "inline")

        self.telegraph_author_edit = QLineEdit()
        self.telegraph_author_edit.setPlaceholderText("ED2K Notifier")

        self.bundle_season_cb = QCheckBox("同剧同季打包合并推送（整季算完发一张海报 + 全部合集）")
        self.include_specs_cb = QCheckBox("推送附带视频规格信息（2160p / HDR / H.265 / DDP5.1 等）")
        self.include_hashtags_cb = QCheckBox("推送附带 Telegram #标签（如 #电视剧 #4K #Netflix）")
        self.attach_txt_cb = QCheckBox("合集推送时额外附带 .txt 链接清单文件")
        self.include_overview_cb = QCheckBox("推送附带 TMDB 简介 (前 140~160 字)")

        self.test_tg_btn = QPushButton("测试 Telegram 并发送测试消息")
        self.test_tg_btn.clicked.connect(self._test_telegram)
        self.tg_test_label = QLabel("")
        self.tg_test_label.setWordWrap(True)

        tg_form.addRow("Bot Token:", token_row)
        tg_form.addRow("Chat ID:", self.tg_chat_edit)
        tg_form.addRow("Topic ID:", self.tg_thread_edit)
        tg_form.addRow("API 地址:", self.tg_api_base_edit)
        tg_form.addRow("链接推送方式:", self.link_mode_combo)
        tg_form.addRow("Telegraph 作者:", self.telegraph_author_edit)
        tg_form.addRow("", self.bundle_season_cb)
        tg_form.addRow("", self.include_specs_cb)
        tg_form.addRow("", self.include_hashtags_cb)
        tg_form.addRow("", self.attach_txt_cb)
        tg_form.addRow("", self.include_overview_cb)
        tg_form.addRow("", self.test_tg_btn)
        tg_form.addRow("", self.tg_test_label)
        tabs.addTab(tg_tab, "Telegram Bot")

        # --- Tab 2: TMDB ---
        tmdb_tab = QWidget()
        tmdb_form = QFormLayout(tmdb_tab)
        tmdb_form.setContentsMargins(14, 14, 14, 14)
        tmdb_form.setSpacing(10)

        self.tmdb_key_edit = QLineEdit()
        self.tmdb_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tmdb_key_edit.setPlaceholderText("支持 v3 API Key 或 v4 API Read Access Token (自动识别)")

        tmdb_key_row = QHBoxLayout()
        tmdb_key_row.addWidget(self.tmdb_key_edit)
        self.tmdb_show_btn = QPushButton("显示")
        self.tmdb_show_btn.setFixedWidth(56)
        self.tmdb_show_btn.setCheckable(True)
        self.tmdb_show_btn.toggled.connect(
            lambda checked: self.tmdb_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        tmdb_key_row.addWidget(self.tmdb_show_btn)

        self.tmdb_lang_combo = QComboBox()
        self.tmdb_lang_combo.setEditable(True)
        self.tmdb_lang_combo.addItems(["zh-CN", "zh-TW", "zh-HK", "en-US", "ja-JP"])

        self.tmdb_size_combo = QComboBox()
        self.tmdb_size_combo.addItems(["w500", "w780", "original"])

        self.tmdb_api_base_edit = QLineEdit()
        self.tmdb_api_base_edit.setPlaceholderText("https://api.themoviedb.org/3")

        self.tmdb_img_base_edit = QLineEdit()
        self.tmdb_img_base_edit.setPlaceholderText("https://image.tmdb.org/t/p")

        self.test_tmdb_btn = QPushButton("测试 TMDB 连接")
        self.test_tmdb_btn.clicked.connect(self._test_tmdb)
        self.tmdb_test_label = QLabel("")
        self.tmdb_test_label.setWordWrap(True)

        tmdb_form.addRow("API Key / Token:", tmdb_key_row)
        tmdb_form.addRow("首选语言:", self.tmdb_lang_combo)
        tmdb_form.addRow("海报尺寸:", self.tmdb_size_combo)
        tmdb_form.addRow("API 地址:", self.tmdb_api_base_edit)
        tmdb_form.addRow("图片 CDN:", self.tmdb_img_base_edit)
        tmdb_form.addRow("", self.test_tmdb_btn)
        tmdb_form.addRow("", self.tmdb_test_label)
        tabs.addTab(tmdb_tab, "TMDB 影视库")

        # --- Tab 3: Network & ED2K ---
        adv_tab = QWidget()
        adv_form = QFormLayout(adv_tab)
        adv_form.setContentsMargins(14, 14, 14, 14)
        adv_form.setSpacing(10)

        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("留空直连，例如 http://127.0.0.1:7890 或 socks5h://127.0.0.1:1080")

        self.ed2k_mode_combo = QComboBox()
        self.ed2k_mode_combo.addItem("blue — eMule 标准（推荐）", "blue")
        self.ed2k_mode_combo.addItem("red — 旧版 eDonkey2000", "red")

        self.cache_cb = QCheckBox("启用本地 SQLite 秒算缓存（相同文件再次拖入 0 秒出结果）")
        self.uppercase_cb = QCheckBox("ED2K Hash 使用大写字母 (推荐)")
        self.urlencode_cb = QCheckBox("对链接中的中文/非 ASCII 文件名进行 URL 百分号编码")

        md4_engine = "PyCryptodome C 加速引擎" if HAS_FAST_MD4 else "纯 Python 回退引擎 (建议安装 pycryptodome)"
        engine_label = QLabel(md4_engine)
        engine_label.setStyleSheet("color: #38bdf8; font-weight: 600;")

        adv_form.addRow("网络代理:", self.proxy_edit)
        adv_form.addRow("ED2K 算法模式:", self.ed2k_mode_combo)
        adv_form.addRow("", self.cache_cb)
        adv_form.addRow("", self.uppercase_cb)
        adv_form.addRow("", self.urlencode_cb)
        adv_form.addRow("当前 MD4 引擎:", engine_label)
        tabs.addTab(adv_tab, "网络与 ED2K")

        layout.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_btn.setText("保存设置")
        save_btn.setObjectName("primaryBtn")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self, cfg: AppConfig) -> None:
        self.tg_token_edit.setText(cfg.telegram_bot_token)
        self.tg_chat_edit.setText(cfg.telegram_chat_id)
        self.tg_thread_edit.setText(cfg.telegram_thread_id)
        self.tg_api_base_edit.setText(cfg.telegram_api_base or "https://api.telegram.org")
        idx_lm = self.link_mode_combo.findData(cfg.notify_link_mode or "telegraph_all")
        if idx_lm >= 0:
            self.link_mode_combo.setCurrentIndex(idx_lm)
        self.telegraph_author_edit.setText(cfg.telegraph_author_name or "ED2K Notifier")
        self.bundle_season_cb.setChecked(cfg.bundle_season_notify)
        self.include_specs_cb.setChecked(cfg.include_specs)
        self.include_hashtags_cb.setChecked(cfg.include_hashtags)
        self.attach_txt_cb.setChecked(cfg.attach_txt_for_batch)
        self.include_overview_cb.setChecked(cfg.include_overview)

        self.tmdb_key_edit.setText(cfg.tmdb_api_key)
        self.tmdb_lang_combo.setCurrentText(cfg.tmdb_language or "zh-CN")
        idx_size = self.tmdb_size_combo.findText(cfg.tmdb_poster_size or "w780")
        if idx_size >= 0:
            self.tmdb_size_combo.setCurrentIndex(idx_size)
        self.tmdb_api_base_edit.setText(cfg.tmdb_api_base or "https://api.themoviedb.org/3")
        self.tmdb_img_base_edit.setText(cfg.tmdb_image_base or "https://image.tmdb.org/t/p")

        self.proxy_edit.setText(cfg.proxy)
        idx_mode = self.ed2k_mode_combo.findData(cfg.ed2k_mode)
        if idx_mode >= 0:
            self.ed2k_mode_combo.setCurrentIndex(idx_mode)
        self.cache_cb.setChecked(cfg.enable_hash_cache)
        self.uppercase_cb.setChecked(cfg.ed2k_uppercase_hash)
        self.urlencode_cb.setChecked(cfg.ed2k_urlencode_non_ascii)

    def build_config(self) -> AppConfig:
        return AppConfig(
            telegram_bot_token=self.tg_token_edit.text().strip(),
            telegram_chat_id=self.tg_chat_edit.text().strip(),
            telegram_thread_id=self.tg_thread_edit.text().strip(),
            telegram_api_base=self.tg_api_base_edit.text().strip() or "https://api.telegram.org",
            tmdb_api_key=self.tmdb_key_edit.text().strip(),
            tmdb_language=self.tmdb_lang_combo.currentText().strip() or "zh-CN",
            tmdb_api_base=self.tmdb_api_base_edit.text().strip() or "https://api.themoviedb.org/3",
            tmdb_image_base=self.tmdb_img_base_edit.text().strip() or "https://image.tmdb.org/t/p",
            tmdb_poster_size=self.tmdb_size_combo.currentText() or "w780",
            notify_link_mode=self.link_mode_combo.currentData() or "telegraph_all",
            telegraph_access_token=self._config.telegraph_access_token,
            telegraph_author_name=self.telegraph_author_edit.text().strip() or "ED2K Notifier",
            telegraph_api_base=self._config.telegraph_api_base or "https://api.telegra.ph",
            proxy=self.proxy_edit.text().strip(),
            auto_notify_telegram=self._config.auto_notify_telegram,
            bundle_season_notify=self.bundle_season_cb.isChecked(),
            enable_hash_cache=self.cache_cb.isChecked(),
            include_specs=self.include_specs_cb.isChecked(),
            include_hashtags=self.include_hashtags_cb.isChecked(),
            attach_txt_for_batch=self.attach_txt_cb.isChecked(),
            ed2k_mode=self.ed2k_mode_combo.currentData() or "blue",
            ed2k_uppercase_hash=self.uppercase_cb.isChecked(),
            ed2k_urlencode_non_ascii=self.urlencode_cb.isChecked(),
            include_overview=self.include_overview_cb.isChecked(),
            theme=self._config.theme,
            window_width=self._config.window_width,
            window_height=self._config.window_height,
            splitter_sizes=list(self._config.splitter_sizes),
            column_widths=list(self._config.column_widths),
        )

    def _test_telegram(self) -> None:
        cfg = self.build_config()
        self.test_tg_btn.setEnabled(False)
        self.tg_test_label.setText("正在测试连接...")
        self.tg_test_label.setStyleSheet("color: #94a3b8;")

        def _do():
            notifier = TelegramNotifier(
                bot_token=cfg.telegram_bot_token,
                chat_id=cfg.get_chat_ids(),
                thread_id=cfg.telegram_thread_id,
                api_base=cfg.telegram_api_base,
                proxies=cfg.get_proxies(),
            )
            return notifier.test_connection(send_test_message=bool(cfg.get_chat_ids()))

        self._active_worker = SingleActionWorker(_do)
        self._active_worker.finished_result.connect(self._on_tg_test_done)
        self._active_worker.start()

    def _on_tg_test_done(self, ok: bool, payload: object) -> None:
        self.test_tg_btn.setEnabled(True)
        if ok and isinstance(payload, tuple):
            success, msg = payload
            self.tg_test_label.setText(msg)
            self.tg_test_label.setStyleSheet("color: #10b981;" if success else "color: #fb7185;")
        else:
            self.tg_test_label.setText(f"测试异常: {payload}")
            self.tg_test_label.setStyleSheet("color: #fb7185;")

    def _test_tmdb(self) -> None:
        cfg = self.build_config()
        self.test_tmdb_btn.setEnabled(False)
        self.tmdb_test_label.setText("正在测试 TMDB 连接...")
        self.tmdb_test_label.setStyleSheet("color: #94a3b8;")

        def _do():
            client = TMDBClient(
                api_key=cfg.tmdb_api_key,
                language=cfg.tmdb_language,
                api_base=cfg.tmdb_api_base,
                image_base=cfg.tmdb_image_base,
                poster_size=cfg.tmdb_poster_size,
                proxies=cfg.get_proxies(),
            )
            return client.test_connection()

        self._active_worker = SingleActionWorker(_do)
        self._active_worker.finished_result.connect(self._on_tmdb_test_done)
        self._active_worker.start()

    def _on_tmdb_test_done(self, ok: bool, payload: object) -> None:
        self.test_tmdb_btn.setEnabled(True)
        if ok and isinstance(payload, tuple):
            success, msg = payload
            self.tmdb_test_label.setText(msg)
            self.tmdb_test_label.setStyleSheet("color: #10b981;" if success else "color: #fb7185;")
        else:
            self.tmdb_test_label.setText(f"测试异常: {payload}")
            self.tmdb_test_label.setStyleSheet("color: #fb7185;")


class TMDBSearchDialog(QDialog):
    """Manual TMDB search and candidate picker dialog."""

    def __init__(
        self,
        config: AppConfig,
        initial_query: str = "",
        initial_type: str = "multi",
        initial_candidates: Optional[list[TMDBMatch]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("搜索 / 更换 TMDB 影视匹配")
        self.resize(580, 450)
        self.config = config
        self.selected_match: Optional[TMDBMatch] = None
        self._results: list[TMDBMatch] = list(initial_candidates or [])
        self._worker: Optional[SingleActionWorker] = None
        apply_windows_titlebar_theme(self, dark=(config.theme != "light"))

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.query_edit = QLineEdit(initial_query)
        self.query_edit.setPlaceholderText("输入电影/剧集名称，或直接输入 TMDB 数字 ID")
        self.query_edit.returnPressed.connect(self._run_search)

        self.type_combo = QComboBox()
        self.type_combo.addItem("全部 (电影+剧集)", "multi")
        self.type_combo.addItem("电影", "movie")
        self.type_combo.addItem("电视剧", "tv")
        idx = self.type_combo.findData(initial_type)
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)

        self.search_btn = QPushButton("搜索")
        self.search_btn.setObjectName("primaryBtn")
        self.search_btn.setIcon(make_icon("search", "#ffffff", 14))
        self.search_btn.clicked.connect(self._run_search)

        bar.addWidget(self.query_edit, 1)
        bar.addWidget(self.type_combo)
        bar.addWidget(self.search_btn)
        layout.addLayout(bar)

        self.hint_label = QLabel("双击列表条目或点击「确定选择」即可应用该影视信息与海报。")
        self.hint_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(self.hint_label)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda _: self._confirm_selection())
        layout.addWidget(self.list_widget, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("确定选择")
        ok_btn.setObjectName("primaryBtn")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._confirm_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_list(self._results)

    def _populate_list(self, matches: list[TMDBMatch]) -> None:
        self._results = matches
        self.list_widget.clear()
        for m in matches:
            orig = f" / {m.original_title}" if m.original_title and m.original_title != m.title else ""
            rating = f"{m.vote_average:.1f}分" if m.vote_average > 0 else "暂无评分"
            poster_flag = "有海报" if m.poster_path else "无海报"
            text = f"[{m.type_label_zh}] {m.display_title}{orig}  —  {rating} · {poster_flag} · ID:{m.tmdb_id}"
            self.list_widget.addItem(QListWidgetItem(text))
        if matches:
            self.list_widget.setCurrentRow(0)

    def _run_search(self) -> None:
        q = self.query_edit.text().strip()
        if not q:
            return
        if not self.config.tmdb_api_key.strip():
            QMessageBox.warning(self, "未配置 TMDB", "请先在设置中填写 TMDB API Key 或 Read Access Token。")
            return

        media_type = self.type_combo.currentData() or "multi"
        self.search_btn.setEnabled(False)
        self.hint_label.setText("正在搜索 TMDB...")

        def _do():
            client = TMDBClient(
                api_key=self.config.tmdb_api_key,
                language=self.config.tmdb_language,
                api_base=self.config.tmdb_api_base,
                image_base=self.config.tmdb_image_base,
                poster_size=self.config.tmdb_poster_size,
                proxies=self.config.get_proxies(),
            )
            if q.isdigit():
                found: list[TMDBMatch] = []
                types_to_try = [media_type] if media_type in ("movie", "tv") else ["movie", "tv"]
                for t in types_to_try:
                    try:
                        found.append(client.fetch_by_id(int(q), media_type=t))
                    except TMDBError:
                        pass
                return found
            return client.search(q, media_type=media_type, limit=12)

        self._worker = SingleActionWorker(_do)
        self._worker.finished_result.connect(self._on_search_done)
        self._worker.start()

    def _on_search_done(self, ok: bool, payload: object) -> None:
        self.search_btn.setEnabled(True)
        if ok and isinstance(payload, list):
            self._populate_list(payload)
            self.hint_label.setText(f"找到 {len(payload)} 条 TMDB 结果。")
        else:
            self.hint_label.setText(f"搜索失败: {payload}")

    def _confirm_selection(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._results):
            QMessageBox.information(self, "提示", "请先选择一个 TMDB 条目。")
            return
        self.selected_match = self._results[row]
        self.accept()


class HistoryDialog(QDialog):
    """Searchable SQLite history browser with copy, txt export, and re-push actions."""

    def __init__(
        self,
        history: HistoryStore,
        config: AppConfig,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("历史记录与秒算缓存 — ED2K Notifier")
        self.resize(1000, 600)
        self.history = history
        self.config = config
        self._dark = config.theme != "light"
        self._records: list[HistoryRecord] = []
        self._worker: Optional[SingleActionWorker] = None
        apply_windows_titlebar_theme(self, dark=self._dark)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("按电影/剧集名称、文件名或 ED2K Hash 搜索...")
        self.search_edit.textChanged.connect(self._reload)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #94a3b8;")
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.count_label)
        layout.addLayout(search_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["TMDB 名称", "集数", "文件名", "大小", "推送状态", "更新时间"])
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)

        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 74)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 92)
        layout.addWidget(self.table, 1)

        icon_fg = "#cbd5e1" if self._dark else "#334155"
        btn_row = QHBoxLayout()
        self.copy_btn = QPushButton("复制选中 ED2K")
        self.copy_btn.setIcon(make_icon("copy", icon_fg, 14))
        self.copy_btn.clicked.connect(self._copy_selected)

        self.export_btn = QPushButton("导出选中 TXT")
        self.export_btn.setIcon(make_icon("file_text", icon_fg, 14))
        self.export_btn.clicked.connect(self._export_selected)

        self.push_btn = QPushButton("打包推送选中到 Telegram")
        self.push_btn.setObjectName("successBtn")
        self.push_btn.setIcon(make_icon("send", "#ffffff", 14))
        self.push_btn.clicked.connect(self._push_selected)

        self.del_btn = QPushButton("删除选中")
        self.del_btn.setIcon(make_icon("trash", icon_fg, 14))
        self.del_btn.clicked.connect(self._delete_selected)

        self.clear_btn = QPushButton("清空全部")
        self.clear_btn.setObjectName("dangerBtn")
        self.clear_btn.clicked.connect(self._clear_all)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(self.copy_btn)
        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.push_btn)
        btn_row.addWidget(self.del_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._reload()

    def _reload(self) -> None:
        self._records = self.history.list_records(query=self.search_edit.text(), limit=500)
        self.table.setRowCount(0)
        for rec in self._records:
            r = self.table.rowCount()
            self.table.insertRow(r)
            title_txt = (
                f"{rec.tmdb_title} ({rec.tmdb_year})"
                if rec.tmdb_title and rec.tmdb_year
                else (rec.tmdb_title or "未匹配")
            )
            ep_txt = rec.episode_label or ("剧集" if rec.media_type == "tv" else "电影")
            fname_item = QTableWidgetItem(rec.file_name)
            fname_item.setToolTip(f"{rec.file_path}\n{rec.ed2k_link}")

            self.table.setItem(r, 0, QTableWidgetItem(title_txt))
            self.table.setCellWidget(
                r, 1, PillBadgeCell(ep_txt, tone="done" if rec.media_type == "tv" else "idle", dark=self._dark)
            )
            self.table.setItem(r, 2, fname_item)
            self.table.setItem(r, 3, QTableWidgetItem(format_file_size(rec.file_size)))
            self.table.setCellWidget(
                r,
                4,
                PillBadgeCell("已推送" if rec.notified else "未推送", tone="success" if rec.notified else "idle", dark=self._dark),
            )
            self.table.setItem(r, 5, QTableWidgetItem(rec.updated_at))
        self.count_label.setText(f"共 {len(self._records)} 条记录")

    def _selected_records(self) -> list[HistoryRecord]:
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})
        return [self._records[r] for r in rows if 0 <= r < len(self._records)]

    def _copy_selected(self) -> None:
        recs = self._selected_records()
        if not recs:
            QMessageBox.information(self, "提示", "请先选择至少一条记录（支持 Ctrl / Shift 多选）。")
            return
        QApplication.clipboard().setText("\n".join(r.ed2k_link for r in recs))
        QMessageBox.information(self, "已复制", f"已复制 {len(recs)} 条 ED2K 链接到剪贴板。")

    def _export_selected(self) -> None:
        recs = self._selected_records()
        if not recs:
            QMessageBox.information(self, "提示", "请先选择要导出的记录。")
            return
        default_name = (recs[0].tmdb_title or "ED2K_历史导出") + "_ED2K.txt"
        save_path, _ = QFileDialog.getSaveFileName(self, "导出 ED2K 链接为 TXT", default_name, "文本文件 (*.txt)")
        if not save_path:
            return
        Path(save_path).write_text("\n".join(r.ed2k_link for r in recs) + "\n", encoding="utf-8")
        QMessageBox.information(self, "导出成功", f"已导出 {len(recs)} 条 ED2K 链接到:\n{save_path}")

    def _push_selected(self) -> None:
        recs = self._selected_records()
        if not recs:
            QMessageBox.information(self, "提示", "请先选择要推送的历史记录。")
            return
        if not self.config.telegram_bot_token.strip() or not self.config.get_chat_ids():
            QMessageBox.warning(self, "未配置 Telegram", "请先在主窗口「设置」中配置 Telegram Bot Token 和 Chat ID。")
            return

        self.push_btn.setEnabled(False)
        items = [_history_record_to_batch_item(rec, self.config) for rec in recs]
        groups = group_batch_items(items)

        def _do():
            tmdb_client = (
                TMDBClient(
                    api_key=self.config.tmdb_api_key,
                    language=self.config.tmdb_language,
                    api_base=self.config.tmdb_api_base,
                    image_base=self.config.tmdb_image_base,
                    poster_size=self.config.tmdb_poster_size,
                    proxies=self.config.get_proxies(),
                )
                if self.config.tmdb_api_key.strip()
                else None
            )
            notifier = TelegramNotifier(
                bot_token=self.config.telegram_bot_token,
                chat_id=self.config.get_chat_ids(),
                thread_id=self.config.telegram_thread_id,
                api_base=self.config.telegram_api_base,
                proxies=self.config.get_proxies(),
            )
            telegraph_client = _make_telegraph_client(self.config)
            last_url = ""
            for grp in groups:
                poster_bytes = None
                if tmdb_client and grp[0].tmdb_match and grp[0].tmdb_match.poster_url:
                    poster_bytes = tmdb_client.download_poster(grp[0].tmdb_match.poster_url)
                resp = notifier.send_batch_notification(
                    grp,
                    poster_bytes=poster_bytes,
                    include_overview=self.config.include_overview,
                    include_specs=self.config.include_specs,
                    include_hashtags=self.config.include_hashtags,
                    attach_txt=self.config.attach_txt_for_batch,
                    telegraph_client=telegraph_client,
                    notify_link_mode=self.config.notify_link_mode,
                )
                if resp.get("telegraph_url"):
                    last_url = resp["telegraph_url"]
            _persist_telegraph_token_if_created(self.config, telegraph_client)
            self.history.mark_notified([r.file_path for r in recs], ed2k_mode=self.config.ed2k_mode)
            return (len(groups), last_url)

        def _done(ok: bool, payload: object) -> None:
            self.push_btn.setEnabled(True)
            if ok:
                self._reload()
                cnt, url = payload if isinstance(payload, tuple) else (payload, "")
                extra = f"\n\nTelegraph 笔记：{url}" if url else ""
                QMessageBox.information(self, "推送成功", f"已成功向 Telegram 推送 {cnt} 组通知。{extra}")
            else:
                QMessageBox.critical(self, "推送失败", f"发送到 Telegram 失败：\n{payload}")

        self._worker = SingleActionWorker(_do)
        self._worker.finished_result.connect(_done)
        self._worker.start()

    def _delete_selected(self) -> None:
        recs = self._selected_records()
        if not recs:
            return
        self.history.delete_records([r.id for r in recs])
        self._reload()

    def _clear_all(self) -> None:
        reply = QMessageBox.question(
            self,
            "确认清空",
            "确定要清空全部本地历史记录与秒算缓存吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.history.clear_all()
            self._reload()


class MainWindow(QMainWindow):
    """Main desktop window for ED2K Notifier."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ED2K Notifier")
        self.setWindowIcon(get_app_icon())
        self.setAcceptDrops(True)

        self.config = load_config()
        self._dark = self.config.theme != "light"
        self.history = HistoryStore()
        self.tasks: list[TaskItem] = []
        self._worker: Optional[ProcessWorker] = None
        self._action_worker: Optional[SingleActionWorker] = None

        self.resize(max(960, self.config.window_width), max(600, self.config.window_height))
        self._build_ui()
        self._apply_theme(self._dark)
        self._restore_layout_prefs()
        self._refresh_config_banner()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(14, 12, 14, 10)
        root_layout.setSpacing(10)

        # --- Top Command Bar ---
        top_frame = QFrame()
        top_frame.setObjectName("topBarFrame")
        top_bar = QHBoxLayout(top_frame)
        top_bar.setContentsMargins(10, 8, 10, 8)
        top_bar.setSpacing(8)

        brand_icon = QLabel()
        brand_icon.setPixmap(get_app_icon().pixmap(26, 26))
        brand_icon.setFixedSize(28, 28)
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setToolTip("ED2K Notifier")

        self.add_files_btn = QPushButton("添加文件")
        self.add_files_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_files_btn.clicked.connect(self._add_files_dialog)

        self.add_folder_btn = QPushButton("添加文件夹")
        self.add_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_folder_btn.clicked.connect(self._add_folder_dialog)

        self.run_toggle_btn = QPushButton("开始计算")
        self.run_toggle_btn.setObjectName("primaryBtn")
        self.run_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_toggle_btn.clicked.connect(self._on_run_toggle_clicked)

        self.clear_done_btn = QPushButton("清空已完成")
        self.clear_done_btn.setObjectName("ghostBtn")
        self.clear_done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_done_btn.clicked.connect(self._clear_completed)

        self.history_btn = QPushButton("历史记录")
        self.history_btn.setObjectName("ghostBtn")
        self.history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_btn.clicked.connect(self._open_history)

        self.auto_tg_cb = ToggleSwitch("自动推送")
        self.auto_tg_cb.setChecked(self.config.auto_notify_telegram)
        self.auto_tg_cb.toggled.connect(self._on_auto_tg_toggled)

        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("ghostBtn")
        self.theme_btn.setToolTip("切换深色 / 浅色主题")
        self.theme_btn.setFixedWidth(34)
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.clicked.connect(self._toggle_theme)

        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghostBtn")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self._open_settings)

        top_bar.addWidget(brand_icon)
        top_bar.addSpacing(2)
        top_bar.addWidget(self.add_files_btn)
        top_bar.addWidget(self.add_folder_btn)
        top_bar.addWidget(self.run_toggle_btn)
        top_bar.addSpacing(4)
        top_bar.addWidget(self.clear_done_btn)
        top_bar.addWidget(self.history_btn)
        top_bar.addStretch()
        top_bar.addWidget(self.auto_tg_cb)
        top_bar.addSpacing(4)
        top_bar.addWidget(self.theme_btn)
        top_bar.addWidget(self.settings_btn)
        root_layout.addWidget(top_frame)

        # --- Config notice banner ---
        self.banner_label = QLabel("")
        self.banner_label.setStyleSheet(
            "background-color: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.35);"
            "border-radius: 8px; padding: 7px 12px; color: #f59e0b; font-size: 12px;"
        )
        self.banner_label.setVisible(False)
        root_layout.addWidget(self.banner_label)

        # --- Main Splitter ---
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(8)

        # Left pane: Stack (EmptyDropZone <-> Queue Table) + Total ETA Bar + Queue Actions
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 2, 0)
        left_layout.setSpacing(8)

        self.queue_stack = QStackedWidget()

        self.drop_zone = EmptyDropZone()
        self.drop_zone.files_dropped.connect(self._add_paths)
        self.drop_zone.add_files_clicked.connect(self._add_files_dialog)
        self.drop_zone.add_folder_clicked.connect(self._add_folder_dialog)
        self.queue_stack.addWidget(self.drop_zone)

        self.table = DropTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["文件名", "集数", "大小", "TMDB 匹配", "进度", "状态"])
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)

        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(56)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.table.files_dropped.connect(self._add_paths)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.queue_stack.addWidget(self.table)
        left_layout.addWidget(self.queue_stack, 1)

        # Total batch progress + ETA strip
        total_frame = QFrame()
        total_frame.setObjectName("totalBarFrame")
        total_layout = QHBoxLayout(total_frame)
        total_layout.setContentsMargins(12, 8, 12, 8)
        total_layout.setSpacing(12)

        self.total_info_label = QLabel("总进度: 0/0 文件 · 0 B")
        self.total_info_label.setStyleSheet("color: #94a3b8; font-size: 12px;")

        self.total_progress_bar = QProgressBar()
        self.total_progress_bar.setObjectName("totalBar")
        self.total_progress_bar.setRange(0, 100)
        self.total_progress_bar.setValue(0)
        self.total_progress_bar.setFixedWidth(180)

        total_layout.addWidget(self.total_info_label, 1)
        total_layout.addWidget(self.total_progress_bar)
        left_layout.addWidget(total_frame)

        # Bottom queue actions
        queue_bar = QHBoxLayout()
        queue_bar.setSpacing(8)
        self.copy_all_btn = QPushButton("复制全部 ED2K")
        self.copy_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_all_btn.clicked.connect(self._copy_all_ed2k)

        self.export_txt_btn = QPushButton("导出 TXT")
        self.export_txt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_txt_btn.clicked.connect(self._export_all_txt)

        self.send_all_bundle_btn = QPushButton("打包推送全部到 Telegram")
        self.send_all_bundle_btn.setObjectName("successBtn")
        self.send_all_bundle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_all_bundle_btn.clicked.connect(self._send_all_bundled_telegram)

        self.remove_sel_btn = QPushButton("移除选中")
        self.remove_sel_btn.setObjectName("ghostBtn")
        self.remove_sel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_sel_btn.clicked.connect(self._remove_selected)

        queue_bar.addWidget(self.copy_all_btn)
        queue_bar.addWidget(self.export_txt_btn)
        queue_bar.addWidget(self.send_all_bundle_btn)
        queue_bar.addStretch()
        queue_bar.addWidget(self.remove_sel_btn)
        left_layout.addLayout(queue_bar)

        # Right pane: Media Detail Hero Card
        right_card = QFrame()
        right_card.setObjectName("cardFrame")
        right_card.setMinimumWidth(360)
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(16, 16, 16, 16)
        right_layout.setSpacing(10)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(14)

        self.poster_label = QLabel()
        self.poster_label.setObjectName("posterLabel")
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster_label.setFixedSize(136, 204)
        meta_row.addWidget(self.poster_label, 0, Qt.AlignmentFlag.AlignTop)

        info_col = QVBoxLayout()
        info_col.setSpacing(6)

        self.title_label = QLabel("请选择一个影视文件")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(10)

        self.orig_title_label = QLabel("")
        self.orig_title_label.setStyleSheet("color: #64748b; font-style: italic; font-size: 12px;")
        self.orig_title_label.setWordWrap(True)
        self.orig_title_label.setMinimumWidth(10)

        self.meta_badges_label = QLabel("")
        self.meta_badges_label.setStyleSheet("color: #38bdf8; font-size: 12px;")
        self.meta_badges_label.setWordWrap(True)
        self.meta_badges_label.setMinimumWidth(10)
        self.meta_badges_label.setOpenExternalLinks(True)

        self.spec_chips = SpecChipsWidget()
        self.spec_chips.setVisible(False)
        self.spec_chips.setMinimumWidth(10)

        self.parsed_hint_label = QLabel("")
        self.parsed_hint_label.setStyleSheet("color: #64748b; font-size: 11px;")
        self.parsed_hint_label.setWordWrap(True)
        self.parsed_hint_label.setMinimumWidth(10)

        self.change_tmdb_btn = QPushButton("手动匹配 / 更换 TMDB")
        self.change_tmdb_btn.setEnabled(False)
        self.change_tmdb_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_tmdb_btn.clicked.connect(self._manual_tmdb_match)

        info_col.addWidget(self.title_label)
        info_col.addWidget(self.orig_title_label)
        info_col.addWidget(self.meta_badges_label)
        info_col.addWidget(self.spec_chips)
        info_col.addWidget(self.parsed_hint_label)
        info_col.addStretch()
        info_col.addWidget(self.change_tmdb_btn)
        meta_row.addLayout(info_col, 1)
        right_layout.addLayout(meta_row)

        self.overview_label = QLabel("")
        self.overview_label.setWordWrap(True)
        self.overview_label.setMinimumWidth(10)
        self.overview_label.setStyleSheet("color: #94a3b8; font-size: 12px; line-height: 1.4;")
        self.overview_label.setMaximumHeight(68)
        right_layout.addWidget(self.overview_label)

        # Telegraph Note Card (shown when a Telegraph link has been generated)
        self.telegraph_card = QFrame()
        self.telegraph_card.setObjectName("telegraphCard")
        self.telegraph_card.setVisible(False)
        tg_card_layout = QHBoxLayout(self.telegraph_card)
        tg_card_layout.setContentsMargins(10, 7, 10, 7)
        tg_card_layout.setSpacing(8)

        self.telegraph_link_label = QLabel("")
        self.telegraph_link_label.setOpenExternalLinks(True)
        self.telegraph_link_label.setStyleSheet("font-size: 12px; font-weight: 500;")
        self.telegraph_link_label.setWordWrap(True)
        self.telegraph_link_label.setMinimumWidth(10)

        self.open_tg_note_btn = QPushButton("打开")
        self.open_tg_note_btn.setObjectName("miniBtn")
        self.open_tg_note_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_tg_note_btn.clicked.connect(self._open_selected_telegraph)

        self.copy_tg_note_btn = QPushButton("复制")
        self.copy_tg_note_btn.setObjectName("miniBtn")
        self.copy_tg_note_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_tg_note_btn.clicked.connect(self._copy_selected_telegraph)

        tg_card_layout.addWidget(self.telegraph_link_label, 1)
        tg_card_layout.addWidget(self.open_tg_note_btn)
        tg_card_layout.addWidget(self.copy_tg_note_btn)
        right_layout.addWidget(self.telegraph_card)

        # ED2K header + inline copy button
        ed2k_hdr_row = QHBoxLayout()
        ed2k_header = QLabel("ED2K 链接")
        ed2k_header.setStyleSheet("font-weight: 600; font-size: 12px; color: #94a3b8;")
        self.copy_one_btn = QPushButton("复制链接")
        self.copy_one_btn.setObjectName("miniBtn")
        self.copy_one_btn.setEnabled(False)
        self.copy_one_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_one_btn.clicked.connect(self._copy_selected_ed2k)
        ed2k_hdr_row.addWidget(ed2k_header)
        ed2k_hdr_row.addStretch()
        ed2k_hdr_row.addWidget(self.copy_one_btn)
        right_layout.addLayout(ed2k_hdr_row)

        self.ed2k_text = QTextEdit()
        self.ed2k_text.setObjectName("ed2kBox")
        self.ed2k_text.setReadOnly(True)
        self.ed2k_text.setPlaceholderText("计算完成后将在此显示完整 ED2K 链接...")
        self.ed2k_text.setMinimumHeight(82)
        right_layout.addWidget(self.ed2k_text, 1)

        self.detail_error_label = QLabel("")
        self.detail_error_label.setStyleSheet("color: #fb7185; font-size: 12px;")
        self.detail_error_label.setWordWrap(True)
        right_layout.addWidget(self.detail_error_label)

        self.send_tg_btn = QPushButton("单推此文件到 Telegram")
        self.send_tg_btn.setObjectName("successBtn")
        self.send_tg_btn.setEnabled(False)
        self.send_tg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_tg_btn.clicked.connect(self._send_selected_telegram)
        right_layout.addWidget(self.send_tg_btn)

        self.splitter.addWidget(left_widget)
        self.splitter.addWidget(right_card)
        self.splitter.setStretchFactor(0, 66)
        self.splitter.setStretchFactor(1, 34)
        root_layout.addWidget(self.splitter, 1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 — 请拖入电影或电视剧文件")
        self._clear_detail()

    def _apply_theme(self, dark: bool) -> None:
        self._dark = dark
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(get_stylesheet("dark" if dark else "light"))
        apply_windows_titlebar_theme(self, dark=dark)

        fg = "#cbd5e1" if dark else "#334155"
        self.add_files_btn.setIcon(make_icon("file_plus", fg, 15))
        self.add_folder_btn.setIcon(make_icon("folder_plus", fg, 15))
        self._update_run_button_visual(running=bool(self._worker and self._worker.isRunning()))
        self.clear_done_btn.setIcon(make_icon("clean", fg, 15))
        self.history_btn.setIcon(make_icon("history", fg, 15))
        self.settings_btn.setIcon(make_icon("settings", fg, 15))
        self.theme_btn.setIcon(make_icon("sun" if dark else "moon", "#fbbf24" if dark else "#475569", 16))
        self.auto_tg_cb.set_dark_mode(dark)
        self.drop_zone.set_dark_mode(dark)

        self.copy_all_btn.setIcon(make_icon("copy", fg, 14))
        self.export_txt_btn.setIcon(make_icon("file_text", fg, 14))
        self.send_all_bundle_btn.setIcon(make_icon("send", "#ffffff", 14))
        self.remove_sel_btn.setIcon(make_icon("trash", fg, 14))
        self.change_tmdb_btn.setIcon(make_icon("search", fg, 14))
        self.copy_one_btn.setIcon(make_icon("copy", fg, 13))
        self.send_tg_btn.setIcon(make_icon("send", "#ffffff", 14))
        self.open_tg_note_btn.setIcon(make_icon("external", fg, 13))
        self.copy_tg_note_btn.setIcon(make_icon("copy", fg, 13))

        # Re-theme live table cell widgets
        for row in range(len(self.tasks)):
            self._refresh_table_row(row)
        row = self.table.currentRow()
        if 0 <= row < len(self.tasks):
            self._render_detail(self.tasks[row])
        else:
            self._clear_detail()

    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        self.config.theme = "dark" if self._dark else "light"
        self._apply_theme(self._dark)
        save_config(self.config)

    def _update_run_button_visual(self, running: bool) -> None:
        if running:
            self.run_toggle_btn.setText("停止计算")
            self.run_toggle_btn.setObjectName("dangerBtn")
            self.run_toggle_btn.setIcon(make_icon("stop", "#ffffff", 14))
        else:
            self.run_toggle_btn.setText("开始计算")
            self.run_toggle_btn.setObjectName("primaryBtn")
            self.run_toggle_btn.setIcon(make_icon("play", "#ffffff", 14))
        self.run_toggle_btn.style().unpolish(self.run_toggle_btn)
        self.run_toggle_btn.style().polish(self.run_toggle_btn)

    def _restore_layout_prefs(self) -> None:
        if len(self.config.splitter_sizes) == 2 and sum(self.config.splitter_sizes) > 200:
            self.splitter.setSizes(self.config.splitter_sizes)
        else:
            self.splitter.setSizes([840, 410])

        default_widths = [350, 78, 74, 140, 88, 110]
        widths = self.config.column_widths if len(self.config.column_widths) == 6 else default_widths
        for col in range(1, 6):
            self.table.setColumnWidth(col, max(56, int(widths[col])))

    def closeEvent(self, event: QCloseEvent) -> None:
        self.config.window_width = self.width()
        self.config.window_height = self.height()
        self.config.splitter_sizes = list(self.splitter.sizes())
        self.config.column_widths = [self.table.columnWidth(c) for c in range(6)]
        try:
            save_config(self.config)
        except OSError:
            pass
        super().closeEvent(event)

    def _refresh_config_banner(self) -> None:
        missing: list[str] = []
        if not self.config.tmdb_api_key.strip():
            missing.append("TMDB API Key")
        if not self.config.telegram_bot_token.strip() or not self.config.get_chat_ids():
            missing.append("Telegram Bot Token / Chat ID")
        if missing:
            self.banner_label.setText(
                f"尚未配置：{'、'.join(missing)}。点击右上角「设置」完成配置后即可自动匹配海报并推送到 Telegram。"
            )
            self.banner_label.setVisible(True)
        else:
            self.banner_label.setVisible(False)

    def _sync_queue_view(self) -> None:
        self.queue_stack.setCurrentIndex(1 if self.tasks else 0)
        self._update_idle_total_summary()

    def _update_idle_total_summary(self) -> None:
        total = len(self.tasks)
        done = sum(1 for t in self.tasks if t.ed2k_result is not None)
        total_b = sum(t.file_size for t in self.tasks)
        done_b = sum(t.file_size for t in self.tasks if t.ed2k_result is not None)
        pct = int((done_b * 100) / total_b) if total_b > 0 else (100 if total > 0 and done == total else 0)
        self.total_info_label.setText(
            f"总进度: {done}/{total} 文件 · {format_file_size(done_b)} / {format_file_size(total_b)}"
        )
        self.total_progress_bar.setValue(pct)

    # --- Drag & Drop on main window ---
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self._add_paths(paths)
            event.acceptProposedAction()

    # --- Queue management ---
    def _add_files_dialog(self) -> None:
        video_filter = "视频文件 (" + " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS)) + ");;所有文件 (*.*)"
        files, _ = QFileDialog.getOpenFileNames(self, "选择电影或电视剧文件", "", video_filter)
        if files:
            self._add_paths(files)

    def _add_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择包含影视文件的文件夹")
        if folder:
            self._add_paths([folder])

    def _add_paths(self, raw_paths: list[str]) -> None:
        existing = {os.path.normcase(os.path.abspath(t.file_path)) for t in self.tasks}
        added = 0

        for raw in raw_paths:
            p = Path(raw)
            if p.is_file():
                norm = os.path.normcase(os.path.abspath(str(p)))
                if norm not in existing:
                    self._append_file(p)
                    existing.add(norm)
                    added += 1
            elif p.is_dir():
                for child in sorted(p.rglob("*")):
                    if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                        norm = os.path.normcase(os.path.abspath(str(child)))
                        if norm not in existing:
                            self._append_file(child)
                            existing.add(norm)
                            added += 1

        self._sync_queue_view()
        if added:
            self.status_bar.showMessage(f"已添加 {added} 个文件到队列")
            if self.table.currentRow() < 0 and self.tasks:
                self.table.selectRow(0)
        else:
            self.status_bar.showMessage("未发现新的可添加文件")

    def _append_file(self, path: Path) -> None:
        try:
            st = path.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
        except OSError:
            size = 0
            mtime_ns = 0

        parsed = parse_media_path(path)
        task = TaskItem(
            file_path=str(path),
            file_name=path.name,
            file_size=size,
            mtime_ns=mtime_ns,
            parsed=parsed,
        )

        if self.config.enable_hash_cache:
            try:
                cached = self.history.lookup_cache(path, size, mtime_ns, ed2k_mode=self.config.ed2k_mode)
                if cached is not None:
                    task.status = "待秒算" + (" · 已推" if cached.notified else "")
            except Exception:
                pass

        row = len(self.tasks)
        self.tasks.append(task)
        self.table.insertRow(row)

        name_item = QTableWidgetItem(task.file_name)
        name_item.setToolTip(task.file_path)
        ep_txt = parsed.episode_label or ("剧集" if parsed.media_type == "tv" else "电影")

        self.table.setItem(row, 0, name_item)
        self.table.setCellWidget(
            row, 1, PillBadgeCell(ep_txt, tone="done" if parsed.media_type == "tv" else "idle", dark=self._dark)
        )
        self.table.setItem(row, 2, QTableWidgetItem(format_file_size(size)))
        self.table.setItem(row, 3, QTableWidgetItem(parsed.title))

        pbar = SlimProgressWidget()
        pbar.set_dark_mode(self._dark)
        self.table.setCellWidget(row, 4, pbar)
        self.table.setCellWidget(
            row, 5, PillBadgeCell(task.status, tone=classify_status_tone(task.status), dark=self._dark)
        )

    def _selected_rows(self) -> list[int]:
        return sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})

    def _refresh_table_row(self, row: int) -> None:
        if row < 0 or row >= len(self.tasks):
            return
        task = self.tasks[row]
        ep_txt = (
            (task.tmdb_match.episode_label if task.tmdb_match and task.tmdb_match.episode_label else "")
            or task.parsed.episode_label
            or ("剧集" if task.parsed.media_type == "tv" else "电影")
        )
        ep_widget = self.table.cellWidget(row, 1)
        if isinstance(ep_widget, PillBadgeCell):
            ep_widget.set_badge(ep_txt, tone="done" if task.parsed.media_type == "tv" else "idle", dark=self._dark)

        self.table.item(row, 3).setText(task.tmdb_match.display_title if task.tmdb_match else task.parsed.title)

        pbar = self.table.cellWidget(row, 4)
        if isinstance(pbar, SlimProgressWidget):
            pbar.set_dark_mode(self._dark)
            pbar.setValue(task.progress)

        st_widget = self.table.cellWidget(row, 5)
        if isinstance(st_widget, PillBadgeCell):
            st_widget.set_badge(task.status, tone=classify_status_tone(task.status), dark=self._dark)

        if self.table.currentRow() == row:
            self._render_detail(task)

    def _on_selection_changed(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.tasks):
            self._render_detail(self.tasks[row])
        else:
            self._clear_detail()

    def _clear_detail(self) -> None:
        placeholder_color = "#334155" if self._dark else "#cbd5e1"
        self.poster_label.setPixmap(make_icon("film", placeholder_color, 40).pixmap(40, 40))
        self.title_label.setText("请选择一个影视文件")
        self.orig_title_label.setText("")
        self.meta_badges_label.setText("")
        self.spec_chips.set_chips([], dark=self._dark)
        self.parsed_hint_label.setText("")
        self.overview_label.setText("")
        self.telegraph_card.setVisible(False)
        self.ed2k_text.clear()
        self.detail_error_label.setText("")
        self.change_tmdb_btn.setEnabled(False)
        self.copy_one_btn.setEnabled(False)
        self.send_tg_btn.setEnabled(False)

    def _render_detail(self, task: TaskItem) -> None:
        self.change_tmdb_btn.setEnabled(True)

        if task.poster_bytes:
            pixmap = QPixmap()
            if pixmap.loadFromData(task.poster_bytes):
                border_col = "#293548" if self._dark else "#cbd5e1"
                self.poster_label.setPixmap(
                    rounded_pixmap(pixmap, self.poster_label.size(), radius=10, border_color=border_col)
                )
            else:
                self.poster_label.setPixmap(make_icon("film", "#475569", 36).pixmap(36, 36))
        else:
            placeholder_color = "#334155" if self._dark else "#cbd5e1"
            self.poster_label.setPixmap(make_icon("film", placeholder_color, 36).pixmap(36, 36))

        link_color = "#60a5fa" if self._dark else "#2563eb"
        if task.tmdb_match:
            m = task.tmdb_match
            self.title_label.setText(m.display_title)
            self.orig_title_label.setText(m.original_title if m.original_title != m.title else "")
            badges = [m.type_label_zh]
            if m.episode_label:
                ep = f"{m.episode_label} · {m.episode_name}" if m.episode_name else m.episode_label
                badges.append(ep)
            if m.vote_average > 0:
                badges.append(f"TMDB {m.vote_average:.1f}")
            badges.append(f'<a style="color:{link_color}; text-decoration:none;" href="{m.tmdb_url}">TMDB 详情 ↗</a>')
            self.meta_badges_label.setText("  ·  ".join(badges))
            self.overview_label.setText(m.overview[:175] + ("..." if len(m.overview) > 175 else ""))
        else:
            self.title_label.setText(task.parsed.title or task.file_name)
            self.orig_title_label.setText("尚未匹配 TMDB 条目")
            kind = "电视剧" if task.parsed.media_type == "tv" else "电影"
            yr = f" · {task.parsed.year}" if task.parsed.year else ""
            ep = f" · {task.parsed.episode_label}" if task.parsed.episode_label else ""
            self.meta_badges_label.setText(f"文件名解析: {kind}{yr}{ep}")
            self.overview_label.setText("")

        # Spec pills
        specs = task.parsed.specs
        chip_list = [
            specs.resolution,
            specs.hdr,
            specs.source,
            specs.platform,
            specs.video_codec,
            specs.audio,
            specs.subtitles,
        ]
        self.spec_chips.set_chips(chip_list, dark=self._dark)
        self.parsed_hint_label.setText(f"{task.file_name}  ({format_file_size(task.file_size)})")

        # Telegraph link card
        if task.telegraph_url:
            self.telegraph_link_label.setText(
                f'Telegraph 笔记: <a style="color:{link_color}; text-decoration:none;" href="{task.telegraph_url}">{task.telegraph_url}</a>'
            )
            self.telegraph_card.setVisible(True)
        else:
            self.telegraph_card.setVisible(False)

        if task.ed2k_result:
            self.ed2k_text.setPlainText(task.ed2k_result.ed2k_link)
            self.copy_one_btn.setEnabled(True)
            self.send_tg_btn.setEnabled(True)
        else:
            self.ed2k_text.clear()
            self.copy_one_btn.setEnabled(False)
            self.send_tg_btn.setEnabled(False)

        self.detail_error_label.setText(task.error_message)

    # --- Context Menu ---
    def _show_table_context_menu(self, pos: QPoint) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        running = bool(self._worker and self._worker.isRunning())
        has_computed = any(self.tasks[r].ed2k_result is not None for r in rows)

        menu = QMenu(self)

        act_push_sel = menu.addAction(f"打包推送选中项 ({len(rows)} 个) 到 Telegram")
        act_push_sel.setEnabled(has_computed and not running)
        act_push_sel.triggered.connect(lambda: self._send_rows_bundled_telegram(rows))

        act_copy_sel = menu.addAction("复制选中 ED2K 链接")
        act_copy_sel.setEnabled(has_computed)
        act_copy_sel.triggered.connect(lambda: self._copy_rows_ed2k(rows))

        act_export_sel = menu.addAction("导出选中为 .txt 文件")
        act_export_sel.setEnabled(has_computed)
        act_export_sel.triggered.connect(lambda: self._export_rows_txt(rows))

        menu.addSeparator()

        act_tmdb = menu.addAction("手动匹配 / 更换 TMDB")
        act_tmdb.triggered.connect(self._manual_tmdb_match)

        act_recompute = menu.addAction("强制重新计算 (忽略秒算缓存)")
        act_recompute.setEnabled(not running)
        act_recompute.triggered.connect(lambda: self._force_recompute_rows(rows))

        act_locate = menu.addAction("在资源管理器中打开所在位置")
        act_locate.triggered.connect(lambda: self._open_in_explorer(self.tasks[rows[0]].file_path))

        menu.addSeparator()

        act_remove = menu.addAction("移除选中")
        act_remove.setEnabled(not running)
        act_remove.triggered.connect(self._remove_selected)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _open_in_explorer(self, file_path: str) -> None:
        p = Path(file_path)
        if p.exists():
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif p.parent.exists():
            subprocess.Popen(["explorer", str(p.parent)])

    def _force_recompute_rows(self, rows: list[int]) -> None:
        if self._worker and self._worker.isRunning():
            return
        for r in rows:
            if 0 <= r < len(self.tasks):
                t = self.tasks[r]
                t.ed2k_result = None
                t.progress = 0
                t.cached_hit = False
                t.force_recompute = True
                t.status = "等待重算"
                self._refresh_table_row(r)
        self._start_processing(indices=rows)

    # --- Processing ---
    def _on_run_toggle_clicked(self) -> None:
        if self._worker and self._worker.isRunning():
            self._stop_processing()
        else:
            self._start_processing()

    def _start_processing(self, checked: bool = False, indices: Optional[list[int]] = None) -> None:
        if self._worker and self._worker.isRunning():
            return

        pending = (
            indices
            if indices is not None
            else [i for i, t in enumerate(self.tasks) if t.ed2k_result is None]
        )
        if not pending:
            if not self.tasks:
                QMessageBox.information(self, "提示", "队列为空，请先添加电影或电视剧文件。")
            else:
                QMessageBox.information(self, "提示", "队列中所有文件均已计算完成。")
            return

        self._update_run_button_visual(running=True)
        self.add_files_btn.setEnabled(False)
        self.add_folder_btn.setEnabled(False)
        self.send_all_bundle_btn.setEnabled(False)

        self._worker = ProcessWorker(self.tasks, pending, self.config, self.history)
        self._worker.progress_updated.connect(self._on_worker_progress)
        self._worker.batch_progress.connect(self._on_batch_progress)
        self._worker.item_updated.connect(self._refresh_table_row)
        self._worker.log_message.connect(self.status_bar.showMessage)
        self._worker.finished_all.connect(self._on_worker_finished)
        self._worker.start()

    def _stop_processing(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.run_toggle_btn.setEnabled(False)
            self.status_bar.showMessage("正在停止计算...")

    def _on_worker_progress(self, row: int, pct: int, status_text: str) -> None:
        if 0 <= row < len(self.tasks):
            pbar = self.table.cellWidget(row, 4)
            if isinstance(pbar, SlimProgressWidget):
                pbar.setValue(pct)
            st_widget = self.table.cellWidget(row, 5)
            if isinstance(st_widget, PillBadgeCell):
                st_widget.set_badge(status_text, tone=classify_status_tone(status_text), dark=self._dark)

    def _on_batch_progress(
        self,
        done_files: int,
        total_files: int,
        done_bytes: int,
        total_bytes: int,
        speed: float,
        eta: float,
    ) -> None:
        pct = int((done_bytes * 100) / total_bytes) if total_bytes > 0 else 100
        self.total_progress_bar.setValue(min(100, max(0, pct)))
        speed_part = f" · {speed:.1f} MB/s" if speed > 0 else ""
        eta_part = f" · 预计剩余 {_format_eta(eta)}" if (done_files < total_files and speed > 0) else ""
        self.total_info_label.setText(
            f"总进度: {done_files}/{total_files} 文件 · {format_file_size(done_bytes)} / {format_file_size(total_bytes)}{speed_part}{eta_part}"
        )

    def _on_worker_finished(self) -> None:
        self.run_toggle_btn.setEnabled(True)
        self._update_run_button_visual(running=False)
        self.add_files_btn.setEnabled(True)
        self.add_folder_btn.setEnabled(True)
        self.send_all_bundle_btn.setEnabled(True)
        self._update_idle_total_summary()
        self.status_bar.showMessage("队列处理已结束")

    # --- Actions ---
    def _on_auto_tg_toggled(self, checked: bool) -> None:
        self.config.auto_notify_telegram = checked
        save_config(self.config)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.config, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.config = dlg.build_config()
            save_config(self.config)
            self._refresh_config_banner()
            self.status_bar.showMessage("设置已保存")

    def _open_history(self) -> None:
        dlg = HistoryDialog(self.history, self.config, self)
        dlg.exec()

    def _open_selected_telegraph(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.tasks) and self.tasks[row].telegraph_url:
            webbrowser.open(self.tasks[row].telegraph_url)

    def _copy_selected_telegraph(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.tasks) and self.tasks[row].telegraph_url:
            QApplication.clipboard().setText(self.tasks[row].telegraph_url)
            self.status_bar.showMessage("已复制 Telegraph 笔记链接到剪贴板")

    def _manual_tmdb_match(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.tasks):
            return
        task = self.tasks[row]
        dlg = TMDBSearchDialog(
            config=self.config,
            initial_query=task.parsed.title,
            initial_type=task.parsed.media_type,
            initial_candidates=task.tmdb_candidates,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_match is not None:
            match = _clone_tmdb_match(dlg.selected_match)
            if match.media_type == "tv" and task.parsed.episode_label:
                match.episode_label = task.parsed.episode_label
            task.tmdb_match = match
            task.poster_bytes = None
            target_rows = [row]

            siblings = [
                i
                for i, t in enumerate(self.tasks)
                if i != row
                and t.parsed.title.lower() == task.parsed.title.lower()
                and t.parsed.season == task.parsed.season
            ]
            if siblings:
                reply = QMessageBox.question(
                    self,
                    "同步同剧集匹配",
                    f"队列中还有 {len(siblings)} 个同剧集/同季文件，是否同时应用「{match.display_title}」？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    for sib_row in siblings:
                        sib_task = self.tasks[sib_row]
                        sib_match = _clone_tmdb_match(match)
                        if sib_match.media_type == "tv":
                            sib_match.episode_label = sib_task.parsed.episode_label
                            sib_match.episode_name = ""
                        sib_task.tmdb_match = sib_match
                        sib_task.poster_bytes = None
                        target_rows.append(sib_row)

            for r in target_rows:
                self._refresh_table_row(r)

            if self.config.tmdb_api_key.strip():
                self.status_bar.showMessage(f"正在加载海报: {match.display_title}...")

                def _fetch():
                    client = TMDBClient(
                        api_key=self.config.tmdb_api_key,
                        language=self.config.tmdb_language,
                        api_base=self.config.tmdb_api_base,
                        image_base=self.config.tmdb_image_base,
                        poster_size=self.config.tmdb_poster_size,
                        proxies=self.config.get_proxies(),
                    )
                    poster_url = match.poster_url
                    if match.media_type == "tv" and task.parsed.season is not None:
                        season_poster = client.fetch_season_poster_url(match.tmdb_id, task.parsed.season)
                        if season_poster:
                            poster_url = season_poster
                    ep_name = ""
                    if (
                        match.media_type == "tv"
                        and task.parsed.season is not None
                        and task.parsed.episode is not None
                    ):
                        ep_name = client.fetch_episode_name(
                            match.tmdb_id, task.parsed.season, task.parsed.episode
                        )
                    poster = client.download_poster(poster_url) if poster_url else None
                    return (poster_url, ep_name, poster)

                def _done(ok: bool, payload: object, rows: list[int] = target_rows, main_row: int = row) -> None:
                    if ok and isinstance(payload, tuple):
                        poster_url, ep_name, poster = payload
                        for r in rows:
                            if 0 <= r < len(self.tasks) and self.tasks[r].tmdb_match is not None:
                                t = self.tasks[r]
                                t.tmdb_match.poster_url = poster_url
                                t.poster_bytes = poster
                                if r == main_row and ep_name:
                                    t.tmdb_match.episode_name = ep_name
                                if t.ed2k_result is not None:
                                    self.history.upsert_record(
                                        t.ed2k_result,
                                        mtime_ns=t.mtime_ns,
                                        parsed=t.parsed,
                                        tmdb_match=t.tmdb_match,
                                    )
                                self._refresh_table_row(r)
                        self.status_bar.showMessage(f"已更新 TMDB 匹配: {match.display_title}（共 {len(rows)} 个文件）")

                self._action_worker = SingleActionWorker(_fetch)
                self._action_worker.finished_result.connect(_done)
                self._action_worker.start()

    def _send_all_bundled_telegram(self) -> None:
        completed_rows = [i for i, t in enumerate(self.tasks) if t.ed2k_result is not None]
        self._send_rows_bundled_telegram(completed_rows)

    def _send_rows_bundled_telegram(self, rows: list[int]) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "提示", "正在计算中，请等待计算完成后再手动推送。")
            return
        completed_rows = [r for r in rows if 0 <= r < len(self.tasks) and self.tasks[r].ed2k_result is not None]
        if not completed_rows:
            QMessageBox.information(self, "提示", "没有已计算完成的文件可推送。")
            return
        if not self.config.telegram_bot_token.strip() or not self.config.get_chat_ids():
            QMessageBox.warning(self, "未配置 Telegram", "请先在「设置」中配置 Telegram Bot Token 和 Chat ID。")
            return

        self.send_all_bundle_btn.setEnabled(False)
        self.send_tg_btn.setEnabled(False)
        self.status_bar.showMessage(f"正在按影视/剧集季度打包推送 {len(completed_rows)} 个文件...")

        row_by_result_id = {id(self.tasks[r].ed2k_result): r for r in completed_rows}
        batch_items = [_task_to_batch_item(self.tasks[r]) for r in completed_rows]
        groups = group_batch_items(batch_items)

        def _do():
            notifier = TelegramNotifier(
                bot_token=self.config.telegram_bot_token,
                chat_id=self.config.get_chat_ids(),
                thread_id=self.config.telegram_thread_id,
                api_base=self.config.telegram_api_base,
                proxies=self.config.get_proxies(),
            )
            telegraph_client = _make_telegraph_client(self.config)
            group_urls: list[str] = []
            for grp in groups:
                resp = notifier.send_batch_notification(
                    grp,
                    include_overview=self.config.include_overview,
                    include_specs=self.config.include_specs,
                    include_hashtags=self.config.include_hashtags,
                    attach_txt=self.config.attach_txt_for_batch,
                    telegraph_client=telegraph_client,
                    notify_link_mode=self.config.notify_link_mode,
                )
                group_urls.append(resp.get("telegraph_url", ""))
            _persist_telegraph_token_if_created(self.config, telegraph_client)
            return group_urls

        def _done(ok: bool, payload: object) -> None:
            self.send_all_bundle_btn.setEnabled(True)
            cur = self.table.currentRow()
            if 0 <= cur < len(self.tasks) and self.tasks[cur].ed2k_result:
                self.send_tg_btn.setEnabled(True)
            if ok and isinstance(payload, list):
                pushed_paths: list[str] = []
                last_url = ""
                for grp, url in zip(groups, payload):
                    if url:
                        last_url = url
                    status_txt = "已打包推送" if len(grp) > 1 else "已推送"
                    for it in grp:
                        r = row_by_result_id.get(id(it.ed2k_result))
                        if r is not None and 0 <= r < len(self.tasks):
                            self.tasks[r].notified = True
                            if url:
                                self.tasks[r].telegraph_url = url
                            self.tasks[r].status = status_txt
                            self.tasks[r].error_message = ""
                            pushed_paths.append(self.tasks[r].file_path)
                            self._refresh_table_row(r)
                self.history.mark_notified(pushed_paths, ed2k_mode=self.config.ed2k_mode)
                url_part = f" · {last_url}" if last_url else ""
                self.status_bar.showMessage(f"打包推送成功！共发送 {len(groups)} 组合集通知{url_part}")
            else:
                QMessageBox.critical(self, "打包推送失败", f"发送到 Telegram 失败：\n{payload}")
                self.status_bar.showMessage(f"打包推送失败: {payload}")

        self._action_worker = SingleActionWorker(_do)
        self._action_worker.finished_result.connect(_done)
        self._action_worker.start()

    def _send_selected_telegram(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.tasks):
            return
        task = self.tasks[row]
        if not task.ed2k_result:
            return
        if not self.config.telegram_bot_token.strip() or not self.config.get_chat_ids():
            QMessageBox.warning(self, "未配置 Telegram", "请先在「设置」中配置 Telegram Bot Token 和 Chat ID。")
            return

        self.send_tg_btn.setEnabled(False)
        self.status_bar.showMessage(f"正在推送到 Telegram: {task.file_name}...")

        def _do():
            notifier = TelegramNotifier(
                bot_token=self.config.telegram_bot_token,
                chat_id=self.config.get_chat_ids(),
                thread_id=self.config.telegram_thread_id,
                api_base=self.config.telegram_api_base,
                proxies=self.config.get_proxies(),
            )
            telegraph_client = _make_telegraph_client(self.config)
            resp = notifier.send_notification(
                task.ed2k_result,
                tmdb_match=task.tmdb_match,
                poster_bytes=task.poster_bytes,
                include_overview=self.config.include_overview,
                include_specs=self.config.include_specs,
                include_hashtags=self.config.include_hashtags,
                specs=task.parsed.specs,
                telegraph_client=telegraph_client,
                use_telegraph=(self.config.notify_link_mode == "telegraph_all"),
            )
            _persist_telegraph_token_if_created(self.config, telegraph_client)
            return resp

        def _done(ok: bool, payload: object, r: int = row, t: TaskItem = task) -> None:
            self.send_tg_btn.setEnabled(True)
            if ok:
                t.notified = True
                t.status = "已推送"
                t.error_message = ""
                tg_url = payload.get("telegraph_url", "") if isinstance(payload, dict) else ""
                if tg_url:
                    t.telegraph_url = tg_url
                self.history.mark_notified([t.file_path], ed2k_mode=self.config.ed2k_mode)
                self._refresh_table_row(r)
                url_part = f" · {tg_url}" if tg_url else ""
                self.status_bar.showMessage(f"Telegram 推送成功: {t.file_name}{url_part}")
            else:
                t.error_message = f"Telegram 推送失败: {payload}"
                self._refresh_table_row(r)
                QMessageBox.critical(self, "推送失败", f"发送到 Telegram 失败：\n{payload}")

        self._action_worker = SingleActionWorker(_do)
        self._action_worker.finished_result.connect(_done)
        self._action_worker.start()

    def _copy_selected_ed2k(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.tasks) and self.tasks[row].ed2k_result:
            QApplication.clipboard().setText(self.tasks[row].ed2k_result.ed2k_link)
            self.status_bar.showMessage("已复制 ED2K 链接到剪贴板")

    def _copy_rows_ed2k(self, rows: list[int]) -> None:
        links = [self.tasks[r].ed2k_result.ed2k_link for r in rows if 0 <= r < len(self.tasks) and self.tasks[r].ed2k_result]
        if not links:
            QMessageBox.information(self, "提示", "选中项中没有已计算完成的 ED2K 链接。")
            return
        QApplication.clipboard().setText("\n".join(links))
        self.status_bar.showMessage(f"已复制 {len(links)} 条 ED2K 链接到剪贴板")

    def _copy_all_ed2k(self) -> None:
        links = [t.ed2k_result.ed2k_link for t in self.tasks if t.ed2k_result]
        if not links:
            QMessageBox.information(self, "提示", "当前没有已计算完成的 ED2K 链接。")
            return
        QApplication.clipboard().setText("\n".join(links))
        self.status_bar.showMessage(f"已复制 {len(links)} 条 ED2K 链接到剪贴板")

    def _export_all_txt(self) -> None:
        rows = [i for i, t in enumerate(self.tasks) if t.ed2k_result is not None]
        self._export_rows_txt(rows)

    def _export_rows_txt(self, rows: list[int]) -> None:
        computed = [self.tasks[r] for r in rows if 0 <= r < len(self.tasks) and self.tasks[r].ed2k_result is not None]
        if not computed:
            QMessageBox.information(self, "提示", "没有可导出的已计算 ED2K 链接。")
            return
        first = computed[0]
        base = (first.tmdb_match.title if first.tmdb_match else first.parsed.title) or "ED2K_清单"
        season_tag = f"_S{first.parsed.season:02d}" if first.parsed.season is not None else ""
        default_name = f"{base}{season_tag}_ED2K.txt"
        save_path, _ = QFileDialog.getSaveFileName(self, "导出 ED2K 链接为 TXT", default_name, "文本文件 (*.txt)")
        if not save_path:
            return
        links = [t.ed2k_result.ed2k_link for t in computed if t.ed2k_result is not None]
        Path(save_path).write_text("\n".join(links) + "\n", encoding="utf-8")
        self.status_bar.showMessage(f"已导出 {len(links)} 条 ED2K 链接到 {save_path}")

    def _remove_selected(self) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "提示", "计算进行中，请先停止后再移除条目。")
            return
        rows = self._selected_rows()
        for row in reversed(rows):
            if 0 <= row < len(self.tasks):
                self.tasks.pop(row)
                self.table.removeRow(row)
        self._sync_queue_view()
        if not self.tasks:
            self._clear_detail()

    def _clear_completed(self) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "提示", "计算进行中，请先停止后再清空。")
            return
        for row in range(len(self.tasks) - 1, -1, -1):
            if self.tasks[row].ed2k_result is not None:
                self.tasks.pop(row)
                self.table.removeRow(row)
        self._sync_queue_view()
        if not self.tasks:
            self._clear_detail()


def run_gui() -> int:
    """Launch the PySide6 desktop GUI."""
    set_windows_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(get_app_icon())
    window = MainWindow()
    window.show()
    return app.exec()
