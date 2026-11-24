"""Unit tests for movie and TV filename/folder parsing."""

from __future__ import annotations

from ed2k_notifier.media_parser import parse_media_path


def test_bilingual_movie() -> None:
    p = parse_media_path("D:/Movies/流浪地球2.The.Wandering.Earth.II.2023.2160p.WEB-DL.DDP5.1.H265.mkv")
    assert p.media_type == "movie"
    assert p.year == 2023
    assert "流浪地球2" in p.search_queries
    assert "The Wandering Earth II" in p.search_queries


def test_english_movie() -> None:
    p = parse_media_path("Dune.Part.Two.2024.IMAX.2160p.BluRay.REMUX.mkv")
    assert p.media_type == "movie"
    assert p.year == 2024
    assert p.title == "Dune Part Two"


def test_numeric_movie_title_not_eaten_as_year() -> None:
    p = parse_media_path("1917.2019.1080p.BluRay.x265.10bit.mkv")
    assert p.media_type == "movie"
    assert p.title == "1917"
    assert p.year == 2019


def test_english_tv_show() -> None:
    p = parse_media_path("Breaking.Bad.S01E01.Pilot.1080p.BluRay.x264.mkv")
    assert p.media_type == "tv"
    assert p.title == "Breaking Bad"
    assert p.season == 1
    assert p.episode == 1
    assert p.episode_label == "S01E01"


def test_bilingual_tv_show() -> None:
    p = parse_media_path("权力的游戏.Game.of.Thrones.S08E06.1080p.WEB-DL.mkv")
    assert p.media_type == "tv"
    assert p.season == 8
    assert p.episode == 6
    assert "权力的游戏" in p.search_queries
    assert "Game of Thrones" in p.search_queries


def test_chinese_season_and_episode() -> None:
    p = parse_media_path("庆余年.第二季.S02E01.2024.2160p.mkv")
    assert p.media_type == "tv"
    assert p.title == "庆余年"
    assert p.season == 2
    assert p.episode == 1


def test_fansub_anime_filename() -> None:
    p = parse_media_path("[织梦字幕组] 进击的巨人 - 01 [1080p][简繁内封].mkv")
    assert p.media_type == "tv"
    assert p.title == "进击的巨人"
    assert p.episode == 1


def test_parent_folder_fallback() -> None:
    p = parse_media_path(r"D:\TV\漫长的季节 (2023)\Season 1\S01E01.mkv")
    assert p.media_type == "tv"
    assert p.title == "漫长的季节"
    assert p.year == 2023
    assert p.season == 1
    assert p.episode == 1
