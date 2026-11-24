"""Unit tests for TMDB client and Telegram Bot notifier."""

from __future__ import annotations

from unittest.mock import MagicMock

from ed2k_notifier.ed2k import ED2KResult
from ed2k_notifier.media_parser import parse_media_path
from ed2k_notifier.telegram_bot import (
    TELEGRAM_MESSAGE_LIMIT,
    TELEGRAM_PHOTO_CAPTION_LIMIT,
    BatchItem,
    TelegramNotifier,
    _visible_length,
    build_batch_notification_html,
    build_notification_html,
    group_batch_items,
)
from ed2k_notifier.tmdb import TMDBClient, TMDBMatch


def _mock_response(json_data: dict, status_code: int = 200, content: bytes = b""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.content = content
    return resp


def test_tmdb_auth_detection_v3_and_v4() -> None:
    session = MagicMock()
    session.get.return_value = _mock_response({"results": []})

    client_v3 = TMDBClient(api_key="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", session=session)
    client_v3.search("Dune", media_type="movie")
    _, kwargs = session.get.call_args
    assert kwargs["params"]["api_key"] == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    assert "Authorization" not in kwargs["headers"]

    session.get.reset_mock()
    token = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiIxMjM0NTY3ODkwIiwidGVzdCI6dHJ1ZX0.signature"
    client_v4 = TMDBClient(api_key=token, session=session)
    client_v4.search("Dune", media_type="movie")
    _, kwargs = session.get.call_args
    assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
    assert "api_key" not in kwargs["params"]


def test_tmdb_match_ranking_and_episode_lookup() -> None:
    session = MagicMock()

    def fake_get(url, **kwargs):
        if url.endswith("/search/tv"):
            return _mock_response(
                {
                    "results": [
                        {
                            "id": 999,
                            "name": "绝命毒师衍生剧",
                            "original_name": "Other Show",
                            "first_air_date": "2018-01-01",
                            "poster_path": "/other.jpg",
                            "vote_average": 6.5,
                            "popularity": 10.0,
                        },
                        {
                            "id": 1396,
                            "name": "绝命毒师",
                            "original_name": "Breaking Bad",
                            "first_air_date": "2008-01-20",
                            "poster_path": "/bb.jpg",
                            "vote_average": 8.9,
                            "popularity": 300.0,
                        },
                    ]
                }
            )
        if url.endswith("/tv/1396/season/1/episode/1"):
            return _mock_response({"name": "试播集"})
        return _mock_response({"results": []})

    session.get.side_effect = fake_get
    client = TMDBClient(api_key="0" * 32, session=session)
    parsed = parse_media_path("Breaking.Bad.S01E01.1080p.BluRay.mkv")
    matches = client.match_parsed_media(parsed)

    assert len(matches) == 2
    assert matches[0].tmdb_id == 1396
    assert matches[0].title == "绝命毒师"
    assert matches[0].poster_url == "https://image.tmdb.org/t/p/w780/bb.jpg"
    assert matches[0].episode_label == "S01E01"
    assert matches[0].episode_name == "试播集"


def test_telegram_notification_html_and_caption_split() -> None:
    match = TMDBMatch(
        tmdb_id=842675,
        media_type="movie",
        title="流浪地球2 <特别版>",
        original_title="The Wandering Earth II",
        release_date="2023-01-22",
        vote_average=7.6,
        poster_path="/poster.jpg",
        poster_url="https://image.tmdb.org/t/p/w780/poster.jpg",
    )
    short_res = ED2KResult(
        file_path="D:/流浪地球2.mkv",
        file_name="流浪地球2.2023.2160p.mkv",
        file_size=13_368_084_480,
        ed2k_hash="A" * 32,
        ed2k_link=f"ed2k://|file|流浪地球2.2023.2160p.mkv|13368084480|{'A' * 32}|/",
        elapsed_seconds=10.0,
    )
    primary, followup = build_notification_html(short_res, tmdb_match=match)
    assert followup is None
    assert "流浪地球2 &lt;特别版&gt;" in primary
    assert "ed2k://|file|" in primary
    assert _visible_length(primary) <= TELEGRAM_PHOTO_CAPTION_LIMIT

    # Very long filename should split photo caption and ED2K reply cleanly
    long_name = "超长电影文件名_" * 80 + ".mkv"
    long_res = ED2KResult(
        file_path=f"D:/{long_name}",
        file_name=long_name,
        file_size=1024,
        ed2k_hash="B" * 32,
        ed2k_link=f"ed2k://|file|{long_name}|1024|{'B' * 32}|/",
        elapsed_seconds=1.0,
    )
    primary2, followup2 = build_notification_html(long_res, tmdb_match=match)
    assert followup2 is not None
    assert "ed2k://|file|" in followup2
    assert _visible_length(primary2) <= TELEGRAM_PHOTO_CAPTION_LIMIT


def test_telegram_send_photo_with_bytes() -> None:
    session = MagicMock()
    session.post.return_value = _mock_response({"ok": True, "result": {"message_id": 42}})

    notifier = TelegramNotifier(bot_token="123:TOKEN", chat_id="-100123456", session=session)
    match = TMDBMatch(
        tmdb_id=842675,
        media_type="movie",
        title="流浪地球2",
        poster_path="/p.jpg",
        poster_url="https://image.tmdb.org/t/p/w780/p.jpg",
    )
    res = ED2KResult(
        file_path="D:/movie.mkv",
        file_name="movie.mkv",
        file_size=100,
        ed2k_hash="C" * 32,
        ed2k_link=f"ed2k://|file|movie.mkv|100|{'C' * 32}|/",
        elapsed_seconds=0.1,
    )

    notifier.send_notification(res, tmdb_match=match, poster_bytes=b"\xff\xd8\xffFAKEJPG")
    assert session.post.call_count == 1
    url = session.post.call_args[0][0]
    kwargs = session.post.call_args[1]
    assert url.endswith("/bot123:TOKEN/sendPhoto")
    assert kwargs["data"]["chat_id"] == "-100123456"
    assert kwargs["data"]["parse_mode"] == "HTML"
    assert "photo" in kwargs["files"]


def test_group_and_batch_season_notification() -> None:
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
    # Deliberately supply episodes out of order + a Season 1 episode
    items = []
    for ep in [3, 1, 2, 10, 4, 5, 6, 7, 8, 9]:
        fname = f"Dear.White.People.2017.S02E{ep:02d}.2160p.NF.WEB-DL.mkv"
        res = ED2KResult(
            file_path=f"H:/DWP/S02/{fname}",
            file_name=fname,
            file_size=3_000_000_000,
            ed2k_hash=f"{ep:032X}",
            ed2k_link=f"ed2k://|file|{fname}|3000000000|{ep:032X}|/",
            elapsed_seconds=5.0,
        )
        items.append(
            BatchItem(
                ed2k_result=res,
                tmdb_match=show,
                poster_bytes=b"\xff\xd8\xffJPG",
                season=2,
                episode=ep,
                episode_label=f"S02E{ep:02d}",
                parsed_title="Dear White People",
                media_type="tv",
            )
        )
    # Add one Season 1 episode to verify seasons are separated
    s1_fname = "Dear.White.People.2017.S01E01.2160p.mkv"
    items.append(
        BatchItem(
            ed2k_result=ED2KResult(
                file_path=f"H:/DWP/S01/{s1_fname}",
                file_name=s1_fname,
                file_size=2_500_000_000,
                ed2k_hash="F" * 32,
                ed2k_link=f"ed2k://|file|{s1_fname}|2500000000|{'F' * 32}|/",
                elapsed_seconds=4.0,
            ),
            tmdb_match=show,
            poster_bytes=b"\xff\xd8\xffJPG",
            season=1,
            episode=1,
            episode_label="S01E01",
            parsed_title="Dear White People",
            media_type="tv",
        )
    )

    groups = group_batch_items(items)
    assert len(groups) == 2
    s2_group = groups[0]
    assert len(s2_group) == 10
    assert [it.episode for it in s2_group] == list(range(1, 11))

    primary, followups = build_batch_notification_html(s2_group)
    assert "亲爱的白种人" in primary
    assert "第 2 季" in primary
    assert "共 10 集 (S02E01 - S02E10)" in primary
    assert _visible_length(primary) <= TELEGRAM_PHOTO_CAPTION_LIMIT
    assert len(followups) == 1
    assert "<pre>" in followups[0]
    assert _visible_length(followups[0]) <= TELEGRAM_MESSAGE_LIMIT
    for ep in range(1, 11):
        assert f"S02E{ep:02d}" in followups[0]

    # Verify TelegramNotifier sends 1 photo + 1 reply message (NOT 10 photos!)
    session = MagicMock()
    session.post.return_value = _mock_response({"ok": True, "result": {"message_id": 100}})
    notifier = TelegramNotifier(bot_token="123:TOKEN", chat_id="-100123", session=session)
    notifier.send_batch_notification(s2_group)
    assert session.post.call_count == 2
    assert session.post.call_args_list[0][0][0].endswith("/sendPhoto")
    assert session.post.call_args_list[1][0][0].endswith("/sendMessage")
    assert session.post.call_args_list[1][1]["data"]["reply_to_message_id"] == 100


def test_large_season_pagination_over_4096_chars() -> None:
    show = TMDBMatch(tmdb_id=100, media_type="tv", title="长篇剧集", release_date="2024-01-01")
    items = []
    for ep in range(1, 55):
        fname = f"Long.Drama.Name.2024.S01E{ep:02d}.2160p.WEB-DL.DDP5.1.Atmos.H265-Group.mkv"
        res = ED2KResult(
            file_path=f"H:/Drama/{fname}",
            file_name=fname,
            file_size=2_000_000_000,
            ed2k_hash=f"{ep:032X}",
            ed2k_link=f"ed2k://|file|{fname}|2000000000|{ep:032X}|/",
            elapsed_seconds=1.0,
        )
        items.append(
            BatchItem(
                ed2k_result=res,
                tmdb_match=show,
                season=1,
                episode=ep,
                episode_label=f"S01E{ep:02d}",
                media_type="tv",
            )
        )

    primary, followups = build_batch_notification_html(items)
    assert _visible_length(primary) <= TELEGRAM_PHOTO_CAPTION_LIMIT
    assert len(followups) >= 2
    joined = "\n".join(followups)
    for part in followups:
        assert _visible_length(part) <= TELEGRAM_MESSAGE_LIMIT
    for ep in range(1, 55):
        assert f"S01E{ep:02d}" in joined
