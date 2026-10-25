"""get_boxes must give each thread its own box when tracks read their init segments at the same time."""

from __future__ import annotations

import struct
import threading
from uuid import UUID, uuid4

from unshackle.core.utilities import get_boxes


def tenc_init(kid: UUID, version: int, crypt_skip: int) -> bytes:
    body = struct.pack(">B3s", version, b"\0\0\0") + bytes([0, crypt_skip if version else 0]) + b"\x01\x10" + kid.bytes
    return b"\x00" * 8 + struct.pack(">I", 8 + len(body)) + b"tenc" + body


def test_get_boxes_finds_the_tenc() -> None:
    kid = uuid4()
    (box,) = get_boxes(tenc_init(kid, 1, 0x19), b"tenc")
    assert box.key_ID == kid


def test_get_boxes_is_right_when_tracks_parse_at_the_same_time() -> None:
    cases = [(uuid4(), version, crypt_skip) for version, crypt_skip in ((0, 0), (1, 0x19), (1, 0x91), (1, 0x55))] * 2
    failures: list[str] = []

    def parse(kid: UUID, version: int, crypt_skip: int) -> None:
        data = tenc_init(kid, version, crypt_skip)
        for _ in range(1500):
            try:
                boxes = list(get_boxes(data, b"tenc"))
            except Exception as e:
                failures.append(repr(e))
                continue
            if [box.key_ID for box in boxes] != [kid]:
                failures.append("wrong or missing tenc")

    threads = [threading.Thread(target=parse, args=case) for case in cases]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not failures, failures[:3]
    assert not any(thread.is_alive() for thread in threads)
