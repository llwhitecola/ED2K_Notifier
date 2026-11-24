"""Tests for MediaSpecs, SQLite HistoryStore cache, multi-chat, txt attachments, and retries."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ed2k_notifier.ed2k import ED2KResult
from ed2k_notifier.history import HistoryStore
from ed2k_notifier.media_parser import extract_media_specs, parse_media_path
from ed2k_notifier.telegram_bot import (
    BatchItem,
    TelegramNotifier,
    build_batch_notification_html,
    build_batch_txt_attachment,
    parse_chat_ids,
)
from ed2k_notifier.telegraph import TelegraphClient, TelegraphError
from ed2k_notifier.tmdb import TMDBMatch


def _mock_response(json_data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {}
    return resp


def test_media_specs_and_hashtags() -> None:
    fname = "Dear.White.People.2017.S02E01.Chapter.I.2160p.NF.WEB-DL.DDP5.1.Atmos.DV.HDR10+.H.265.简繁内封-FGTWeb.mkv"
    specs = extract_media_specs(fname)
    assert specs.resolution == "2160p"
    assert specs.source == "WEB-DL"
    assert specs.platform == "Netflix"
    assert specs.video_codec == "H.265"
    assert "杜比视界" in specs.hdr
    assert "HDR10+" in specs.hdr
    assert "DDP5.1" in specs.audio
    assert "Atmos" in specs.audio
    assert specs.subtitles

    tags = specs.to_hashtags(media_type="tv")
    assert "#电视剧" in tags
    assert "#4K" in tags
    assert "#WEBDL" in tags
    assert "#Netflix" in tags
    assert "#杜比视界" in tags
    assert "#中字" in tags


def test_history_store_instant_cache_and_search(tmp_path: Path) -> None:
    db = HistoryStore(tmp_path / "history.db")
    fake_file = tmp_path / "Dear.White.People.2017.S02E01.2160p.mkv"
    fake_file.write_bytes(b"sample")
    st = fake_file.stat()

    parsed = parse_media_path(fake_file)
    res = ED2KResult(
        file_path=str(fake_file),
        file_name=fake_file.name,
        file_size=st.st_size,
        ed2k_hash="A" * 32,
        ed2k_link=f"ed2k://|file|{fake_file.name}|{st.st_size}|{'A' * 32}|/",
        elapsed_seconds=0.2,
    )
    match = TMDBMatch(
        tmdb_id=69629,
        media_type="tv",
        title="亲爱的白种人",
        original_title="Dear White People",
        release_date="2017-04-28",
        vote_average=6.0,
        poster_path="/p.jpg",
        poster_url="https://image.tmdb.org/t/p/w780/p.jpg",
        episode_label="S02E01",
    )

    assert db.lookup_cache(fake_file, st.st_size, st.st_mtime_ns) is None
    db.upsert_record(res, mtime_ns=st.st_mtime_ns, parsed=parsed, tmdb_match=match)

    hit = db.lookup_cache(fake_file, st.st_size, st.st_mtime_ns)
    assert hit is not None
    assert hit.ed2k_hash == "A" * 32
    assert hit.tmdb_title == "亲爱的白种人"
    assert hit.notified is False

    # Size mismatch must miss cache
    assert db.lookup_cache(fake_file, st.st_size + 1, st.st_mtime_ns) is None

    db.mark_notified([str(fake_file)])
    hit2 = db.lookup_cache(fake_file, st.st_size, st.st_mtime_ns)
    assert hit2 is not None and hit2.notified is True

    found = db.list_records(query="白种人")
    assert len(found) == 1
    db.delete_records([found[0].id])
    assert db.list_records() == []


def test_multi_chat_and_txt_attachment_and_retry() -> None:
    assert parse_chat_ids("12345, -100999; @mychan\n555") == ["12345", "-100999", "@mychan", "555"]

    show = TMDBMatch(
        tmdb_id=69629,
        media_type="tv",
        title="亲爱的白种人",
        original_title="Dear White People",
        release_date="2017-04-28",
        poster_path="/dwp.jpg",
        poster_url="https://image.tmdb.org/t/p/w780/dwp.jpg",
    )
    items = []
    for ep in range(1, 11):
        fn = f"Dear.White.People.2017.S02E{ep:02d}.2160p.NF.WEB-DL.H265.mkv"
        items.append(
            BatchItem(
                ed2k_result=ED2KResult(
                    file_path=f"H:/DWP/{fn}",
                    file_name=fn,
                    file_size=2_000_000_000,
                    ed2k_hash=f"{ep:032X}",
                    ed2k_link=f"ed2k://|file|{fn}|2000000000|{ep:032X}|/",
                    elapsed_seconds=1.0,
                ),
                tmdb_match=show,
                poster_bytes=b"\xff\xd8\xffJPG",
                season=2,
                episode=ep,
                episode_label=f"S02E{ep:02d}",
                media_type="tv",
            )
        )

    primary, followups = build_batch_notification_html(items, include_specs=True, include_hashtags=True)
    assert "规格:" in primary
    assert "#电视剧" in primary
    assert "#4K" in primary
    assert "#Netflix" in primary

    txt_name, txt_bytes = build_batch_txt_attachment(items)
    assert txt_name == "亲爱的白种人_S02_ED2K.txt"
    assert len(txt_bytes.decode("utf-8").strip().splitlines()) == 10

    # Simulate transient 429 on first call, then success on all subsequent calls across 2 chats
    session = MagicMock()
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _mock_response({"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 0}}, status_code=429)
        return _mock_response({"ok": True, "result": {"message_id": 77}})

    session.post.side_effect = fake_post
    notifier = TelegramNotifier(
        bot_token="123:TOKEN",
        chat_id="11111, -10022222",
        retry_backoff=0.0,
        session=session,
    )
    notifier.send_batch_notification(items, attach_txt=True)

    # For 2 chats: each chat gets sendPhoto (+1 retry on first chat) + sendMessage (followup) + sendDocument (.txt)
    methods = [c[0][0].rsplit("/", 1)[-1] for c in session.post.call_args_list]
    assert methods.count("sendPhoto") == 3  # 1 failed 429 + 1 succeeded chat1 + 1 succeeded chat2
    assert methods.count("sendDocument") == 2
    assert len(followups) == 1
    assert methods.count("sendMessage") == 2


def test_telegraph_publish_and_single_message_telegram_push() -> None:
    import json

    tg_ph_session = MagicMock()

    def fake_telegraph_post(url, data=None, **kwargs):
        if url.endswith("/createAccount"):
            return _mock_response({"ok": True, "result": {"access_token": "tok_auto_123"}})
        if url.endswith("/createPage"):
            nodes = json.loads(data["content"])
            assert len(nodes) == 2
            assert "10 条 ED2K 链接" in nodes[0]["children"][0]
            # 10 links interleaved with 9 <br> tags = 19 children
            assert len(nodes[1]["children"]) == 19
            return _mock_response(
                {"ok": True, "result": {"url": "https://telegra.ph/Dear-White-People-11-24"}}
            )
        return _mock_response({"ok": False, "error": "UNKNOWN"}, status_code=400)

    tg_ph_session.post.side_effect = fake_telegraph_post
    telegraph = TelegraphClient(retry_backoff=0.0, session=tg_ph_session)

    show = TMDBMatch(
        tmdb_id=69629,
        media_type="tv",
        title="亲爱的白种人",
        original_title="Dear White People",
        release_date="2017-04-28",
        vote_average=6.0,
        poster_path="/dwp.jpg",
        poster_url="https://image.tmdb.org/t/p/w780/dwp.jpg",
    )
    items = []
    for ep in range(1, 11):
        fn = f"Dear.White.People.2017.S02E{ep:02d}.2160p.NF.WEB-DL.H265.mkv"
        items.append(
            BatchItem(
                ed2k_result=ED2KResult(
                    file_path=f"H:/DWP/{fn}",
                    file_name=fn,
                    file_size=3_000_000_000,
                    ed2k_hash=f"{ep:032X}",
                    ed2k_link=f"ed2k://|file|{fn}|3000000000|{ep:032X}|/",
                    elapsed_seconds=1.0,
                ),
                tmdb_match=show,
                poster_bytes=b"\xff\xd8\xffJPG",
                season=2,
                episode=ep,
                episode_label=f"S02E{ep:02d}",
                media_type="tv",
            )
        )

    bot_session = MagicMock()
    bot_session.post.return_value = _mock_response({"ok": True, "result": {"message_id": 500}})
    notifier = TelegramNotifier(bot_token="123:TOKEN", chat_id="-100999", session=bot_session)

    resp = notifier.send_batch_notification(
        items,
        telegraph_client=telegraph,
        notify_link_mode="telegraph_all",
        attach_txt=False,
    )
    assert telegraph.access_token == "tok_auto_123"
    assert resp["telegraph_url"] == "https://telegra.ph/Dear-White-People-11-24"
    # Only ONE Telegram API call (single poster card with inline Telegraph button!)
    assert bot_session.post.call_count == 1
    assert bot_session.post.call_args[0][0].endswith("/sendPhoto")
    sent_data = bot_session.post.call_args[1]["data"]
    assert "https://telegra.ph/Dear-White-People-11-24" in sent_data["caption"]
    markup = json.loads(sent_data["reply_markup"])
    assert markup["inline_keyboard"][0][0]["url"] == "https://telegra.ph/Dear-White-People-11-24"


def test_telegraph_failure_falls_back_to_inline_block() -> None:
    broken_tg_session = MagicMock()
    broken_tg_session.post.return_value = _mock_response({"ok": False, "error": "FLOOD_WAIT"}, status_code=400)
    telegraph = TelegraphClient(access_token="tok", retry_backoff=0.0, session=broken_tg_session)

    show = TMDBMatch(tmdb_id=69629, media_type="tv", title="亲爱的白种人", release_date="2017-04-28")
    items = [
        BatchItem(
            ed2k_result=ED2KResult(
                file_path=f"H:/DWP/S02E{ep:02d}.mkv",
                file_name=f"Dear.White.People.2017.S02E{ep:02d}.2160p.WEB-DL.mkv",
                file_size=2_000_000_000,
                ed2k_hash=f"{ep:032X}",
                ed2k_link=f"ed2k://|file|Dear.White.People.2017.S02E{ep:02d}.2160p.WEB-DL.mkv|2000000000|{ep:032X}|/",
                elapsed_seconds=1.0,
            ),
            tmdb_match=show,
            poster_bytes=b"\xff\xd8\xffJPG",
            season=2,
            episode=ep,
            episode_label=f"S02E{ep:02d}",
            media_type="tv",
        )
        for ep in range(1, 11)
    ]

    bot_session = MagicMock()
    bot_session.post.return_value = _mock_response({"ok": True, "result": {"message_id": 600}})
    notifier = TelegramNotifier(bot_token="123:TOKEN", chat_id="-100999", session=bot_session)

    resp = notifier.send_batch_notification(
        items,
        telegraph_client=telegraph,
        notify_link_mode="telegraph_all",
    )
    assert "telegraph_url" not in resp
    # Fallback sends 1 photo + 1 reply message with <pre> ED2K links
    assert bot_session.post.call_count == 2
    assert bot_session.post.call_args_list[1][0][0].endswith("/sendMessage")
    assert "<pre>" in bot_session.post.call_args_list[1][1]["data"]["text"]
