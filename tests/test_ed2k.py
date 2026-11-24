"""Unit tests for MD4 and ED2K calculation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ed2k_notifier.ed2k import (
    ED2K_CHUNK_SIZE,
    EMPTY_MD4_DIGEST,
    HAS_FAST_MD4,
    HashCancelledError,
    _PurePythonMD4,
    build_ed2k_link,
    calculate_ed2k_bytes,
    calculate_ed2k_file,
    new_md4,
)

# Standard RFC 1320 MD4 test vectors
RFC1320_VECTORS = [
    (b"", "31d6cfe0d16ae931b73c59d7e0c089c0"),
    (b"a", "bde52cb31de33e46245e05fbdbd6fb24"),
    (b"abc", "a448017aaf21d8525fc10ae87aa6729d"),
    (b"message digest", "d9130a8164549fe818874806e1c7014b"),
    (b"abcdefghijklmnopqrstuvwxyz", "d79e1c308aa5bbcdeea8ed63df412da9"),
    (
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        "043f8582f241db351ce627e153e7f0e4",
    ),
    (
        b"12345678901234567890123456789012345678901234567890123456789012345678901234567890",
        "e33b4ddc9c38f2199c3e7b164fcc0536",
    ),
]


@pytest.mark.parametrize("payload,expected", RFC1320_VECTORS)
def test_md4_rfc1320_vectors(payload: bytes, expected: str) -> None:
    assert new_md4(payload).hexdigest() == expected
    assert _PurePythonMD4(payload).hexdigest() == expected


def test_fast_md4_available() -> None:
    assert HAS_FAST_MD4 is True


def test_ed2k_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.mkv"
    p.write_bytes(b"")
    res = calculate_ed2k_file(p)
    assert res.file_size == 0
    assert res.ed2k_hash == EMPTY_MD4_DIGEST.hex().upper()
    assert res.ed2k_link == f"ed2k://|file|empty.mkv|0|{EMPTY_MD4_DIGEST.hex().upper()}|/"


def test_ed2k_small_file_matches_md4(tmp_path: Path) -> None:
    data = b"The quick brown fox jumps over the lazy dog" * 100
    p = tmp_path / "sample.mkv"
    p.write_bytes(data)

    expected = new_md4(data).hexdigest()
    assert calculate_ed2k_bytes(data) == expected
    res = calculate_ed2k_file(p, uppercase_hash=False)
    assert res.ed2k_hash == expected


def test_ed2k_exact_single_chunk_blue_vs_red(tmp_path: Path) -> None:
    chunk = b"\x00" * ED2K_CHUNK_SIZE
    p = tmp_path / "exact_one_chunk.bin"
    p.write_bytes(chunk)

    chunk_digest = new_md4(chunk).digest()
    expected_red = chunk_digest.hex()
    expected_blue = new_md4(chunk_digest + EMPTY_MD4_DIGEST).hexdigest()

    assert expected_red != expected_blue
    assert calculate_ed2k_bytes(chunk, mode="blue") == expected_blue
    assert calculate_ed2k_bytes(chunk, mode="red") == expected_red

    res_blue = calculate_ed2k_file(p, mode="blue", uppercase_hash=False)
    res_red = calculate_ed2k_file(p, mode="red", uppercase_hash=False)
    assert res_blue.ed2k_hash == expected_blue
    assert res_red.ed2k_hash == expected_red


def test_ed2k_multi_chunk_non_multiple(tmp_path: Path) -> None:
    chunk1 = b"A" * ED2K_CHUNK_SIZE
    chunk2 = b"B" * 12345
    data = chunk1 + chunk2
    p = tmp_path / "multi.mkv"
    p.write_bytes(data)

    expected = new_md4(new_md4(chunk1).digest() + new_md4(chunk2).digest()).hexdigest()
    assert calculate_ed2k_bytes(data, mode="blue") == expected
    assert calculate_ed2k_bytes(data, mode="red") == expected
    res = calculate_ed2k_file(p, uppercase_hash=False)
    assert res.ed2k_hash == expected


def test_ed2k_link_encoding() -> None:
    link = build_ed2k_link("流浪地球2 | 4K版.mkv", 12345, "abcdef0123456789abcdef0123456789")
    assert link == "ed2k://|file|流浪地球2%20%7C%204K版.mkv|12345|ABCDEF0123456789ABCDEF0123456789|/"

    ascii_link = build_ed2k_link(
        "流浪地球2.mkv", 12345, "abcdef0123456789abcdef0123456789", urlencode_non_ascii=True
    )
    assert "%E6%B5%81%E6%B5%AA" in ascii_link


def test_ed2k_cancellation(tmp_path: Path) -> None:
    p = tmp_path / "cancel.bin"
    p.write_bytes(b"X" * (2 * 1024 * 1024))
    with pytest.raises(HashCancelledError):
        calculate_ed2k_file(p, cancel_check=lambda: True)
