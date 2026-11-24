"""Command-line interface for ED2K Notifier (ideal for scripts & automation)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ed2k_notifier.config import load_config, save_config
from ed2k_notifier.ed2k import calculate_ed2k_file, format_file_size
from ed2k_notifier.history import HistoryStore
from ed2k_notifier.media_parser import VIDEO_EXTENSIONS, parse_media_path
from ed2k_notifier.telegram_bot import BatchItem, TelegramError, TelegramNotifier, group_batch_items
from ed2k_notifier.telegraph import TelegraphClient
from ed2k_notifier.tmdb import TMDBClient, TMDBError, TMDBMatch


def _collect_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                    files.append(child)
        else:
            print(f"[警告] 路径不存在，已跳过: {raw}", file=sys.stderr)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="计算影视文件 ED2K，匹配 TMDB 电影/剧集信息与海报，并推送到 Telegram Bot。"
    )
    parser.add_argument("paths", nargs="+", help="一个或多个影视文件或文件夹路径")
    parser.add_argument("--config", help="自定义 config.json 路径")
    parser.add_argument("--query", help="手动指定 TMDB 搜索关键词（覆盖文件名解析）")
    parser.add_argument("--tmdb-id", type=int, help="直接指定 TMDB ID")
    parser.add_argument("--type", choices=["movie", "tv"], dest="media_type", help="指定影视类型 (movie 或 tv)")
    parser.add_argument("--no-tmdb", action="store_true", help="跳过 TMDB 查询")
    parser.add_argument("--no-notify", action="store_true", help="只计算不推送到 Telegram")
    parser.add_argument("--no-bundle", action="store_true", help="禁用整季打包推送，改为逐集单独推送")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    proxies = cfg.get_proxies()
    files = _collect_files(args.paths)
    if not files:
        print("未找到任何可计算的文件。", file=sys.stderr)
        return 1

    tmdb_client = (
        TMDBClient(
            api_key=cfg.tmdb_api_key,
            language=cfg.tmdb_language,
            api_base=cfg.tmdb_api_base,
            image_base=cfg.tmdb_image_base,
            poster_size=cfg.tmdb_poster_size,
            proxies=proxies,
        )
        if (not args.no_tmdb and cfg.tmdb_api_key.strip())
        else None
    )

    tg_notifier = (
        TelegramNotifier(
            bot_token=cfg.telegram_bot_token,
            chat_id=cfg.get_chat_ids(),
            thread_id=cfg.telegram_thread_id,
            api_base=cfg.telegram_api_base,
            proxies=proxies,
        )
        if (not args.no_notify and cfg.telegram_bot_token.strip() and cfg.get_chat_ids())
        else None
    )

    telegraph_client = (
        TelegraphClient(
            access_token=cfg.telegraph_access_token,
            author_name=cfg.telegraph_author_name,
            api_base=cfg.telegraph_api_base,
            proxies=proxies,
        )
        if cfg.notify_link_mode != "inline"
        else None
    )

    history = HistoryStore()
    bundle_mode = cfg.bundle_season_notify and not args.no_bundle
    batch_items: list[BatchItem] = []
    exit_code = 0

    for idx, file_path in enumerate(files, 1):
        print(f"\n[{idx}/{len(files)}] 正在处理: {file_path.name}")
        st = file_path.stat()
        cached = (
            history.lookup_cache(file_path, st.st_size, st.st_mtime_ns, ed2k_mode=cfg.ed2k_mode)
            if cfg.enable_hash_cache
            else None
        )
        if cached is not None:
            result = cached.to_ed2k_result(
                uppercase_hash=cfg.ed2k_uppercase_hash,
                urlencode_non_ascii=cfg.ed2k_urlencode_non_ascii,
            )
            print("  ⚡ 命中本地秒算缓存")
        else:

            def _progress(done: int, total: int, speed: float) -> None:
                pct = (done * 100.0 / total) if total > 0 else 100.0
                sys.stdout.write(
                    f"\r  进度: {pct:5.1f}%  ({format_file_size(done)} / {format_file_size(total)})  {speed:.1f} MB/s"
                )
                sys.stdout.flush()

            result = calculate_ed2k_file(
                file_path,
                mode=cfg.ed2k_mode,
                uppercase_hash=cfg.ed2k_uppercase_hash,
                urlencode_non_ascii=cfg.ed2k_urlencode_non_ascii,
                progress_callback=_progress,
            )
            print()
        print(f"  ED2K: {result.ed2k_link}")

        parsed = parse_media_path(file_path)
        if args.query:
            parsed.title = args.query
            parsed.search_queries = [args.query]
        if args.media_type:
            parsed.media_type = args.media_type

        tmdb_match: TMDBMatch | None = cached.to_tmdb_match() if cached is not None else None
        poster_bytes: bytes | None = None

        if tmdb_client is not None:
            try:
                if tmdb_match is None or args.tmdb_id is not None or args.query:
                    if args.tmdb_id is not None:
                        tmdb_match = tmdb_client.fetch_by_id(args.tmdb_id, media_type=parsed.media_type)
                        if tmdb_match.media_type == "tv" and parsed.episode_label:
                            tmdb_match.episode_label = parsed.episode_label
                    else:
                        matches = tmdb_client.match_parsed_media(parsed)
                        if matches:
                            tmdb_match = matches[0]
                    if tmdb_match and tmdb_match.media_type == "tv" and parsed.season is not None:
                        s_poster = tmdb_client.fetch_season_poster_url(tmdb_match.tmdb_id, parsed.season)
                        if s_poster:
                            tmdb_match.poster_url = s_poster
                if tmdb_match:
                    print(f"  TMDB: [{tmdb_match.type_label_zh}] {tmdb_match.display_title} ({tmdb_match.tmdb_url})")
                    if tmdb_match.poster_url:
                        poster_bytes = tmdb_client.download_poster(tmdb_match.poster_url)
                else:
                    print(f"  TMDB: 未找到匹配 (解析名: {parsed.title})")
            except TMDBError as exc:
                print(f"  [TMDB 错误] {exc}", file=sys.stderr)

        history.upsert_record(result, mtime_ns=st.st_mtime_ns, parsed=parsed, tmdb_match=tmdb_match)

        item = BatchItem(
            ed2k_result=result,
            tmdb_match=tmdb_match,
            poster_bytes=poster_bytes,
            season=parsed.season,
            episode=parsed.episode,
            episode_label=parsed.episode_label,
            parsed_title=parsed.title,
            media_type=parsed.media_type,
            specs=parsed.specs,
        )
        batch_items.append(item)

        if tg_notifier is not None and not bundle_mode:
            try:
                resp = tg_notifier.send_notification(
                    result,
                    tmdb_match=tmdb_match,
                    poster_bytes=poster_bytes,
                    include_overview=cfg.include_overview,
                    include_specs=cfg.include_specs,
                    include_hashtags=cfg.include_hashtags,
                    specs=parsed.specs,
                    telegraph_client=telegraph_client,
                    use_telegraph=(cfg.notify_link_mode == "telegraph_all"),
                )
                if telegraph_client and telegraph_client.access_token and telegraph_client.access_token != cfg.telegraph_access_token:
                    cfg.telegraph_access_token = telegraph_client.access_token
                    save_config(cfg, args.config)
                history.mark_notified([str(file_path)], ed2k_mode=cfg.ed2k_mode)
                tg_url = resp.get("telegraph_url", "")
                print(f"  Telegram: 推送成功 ✅{(' · ' + tg_url) if tg_url else ''}")
            except TelegramError as exc:
                print(f"  [Telegram 错误] {exc}", file=sys.stderr)
                exit_code = 2
        elif tg_notifier is None and not args.no_notify:
            print("  Telegram: 跳过（尚未配置 Bot Token 或 Chat ID）")

    if tg_notifier is not None and bundle_mode and batch_items:
        groups = group_batch_items(batch_items)
        print(f"\n正在打包推送到 Telegram（共 {len(groups)} 组）...")
        for grp in groups:
            label = grp[0].tmdb_match.display_title if grp[0].tmdb_match else grp[0].parsed_title
            try:
                resp = tg_notifier.send_batch_notification(
                    grp,
                    include_overview=cfg.include_overview,
                    include_specs=cfg.include_specs,
                    include_hashtags=cfg.include_hashtags,
                    attach_txt=cfg.attach_txt_for_batch,
                    telegraph_client=telegraph_client,
                    notify_link_mode=cfg.notify_link_mode,
                )
                if telegraph_client and telegraph_client.access_token and telegraph_client.access_token != cfg.telegraph_access_token:
                    cfg.telegraph_access_token = telegraph_client.access_token
                    save_config(cfg, args.config)
                history.mark_notified([it.ed2k_result.file_path for it in grp], ed2k_mode=cfg.ed2k_mode)
                tg_url = resp.get("telegraph_url", "")
                print(f"  Telegram 打包推送成功: {label}（共 {len(grp)} 个文件）✅{(' · ' + tg_url) if tg_url else ''}")
            except TelegramError as exc:
                print(f"  [Telegram 错误] {label}: {exc}", file=sys.stderr)
                exit_code = 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
