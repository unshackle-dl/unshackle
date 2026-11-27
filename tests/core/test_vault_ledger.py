"""Vaults as the ledger of content keys: where each key in use came from, and who hears that it is bad."""

import logging
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest
import requests

from tests.core.key_resolver import MemoryVault, vaults_of

KID = UUID(int=1)
BAD = "aa" * 16
GOOD = "bb" * 16


def server() -> SimpleNamespace:
    """A source outside the loaded vaults, as the server's vaults are over --remote."""
    return SimpleNamespace(name="server_cdm", local=False, report_now=True)


def test_an_offered_key_comes_after_the_local_vaults_and_is_its_sources() -> None:
    local, source = MemoryVault(), server()
    vaults = vaults_of(local)
    vaults.offer(KID, BAD, source)  # type: ignore[arg-type]

    assert vaults.get_key(KID) == (BAD, source)
    assert local.asked == [KID]
    assert vaults.source_of(KID, BAD) is source
    assert vaults.source_of(KID, GOOD) is None


def test_a_proven_vault_key_goes_to_every_other_vault() -> None:
    remote, local = MemoryVault({KID: GOOD}, local=False), MemoryVault(name="local")
    vaults = vaults_of(local, remote)
    assert vaults.get_key(KID) == (GOOD, remote)

    vaults.proven({KID: GOOD}, {KID: remote})

    assert local.added == {KID: GOOD}
    assert remote.added == {}


def test_a_pair_proved_wrong_before_its_proof_is_stored_goes_nowhere() -> None:
    remote, local = MemoryVault({KID: GOOD}, local=False), MemoryVault(name="local")
    vaults = vaults_of(local, remote)
    vaults.get_key(KID)

    vaults.flag_bad_key(KID, GOOD)
    vaults.proven({KID: GOOD}, {KID: remote})

    assert local.added == {}
    assert remote.added == {}


def test_a_remote_vault_hears_of_its_bad_pair_only_once_another_key_replaces_it() -> None:
    remote = MemoryVault({KID: BAD}, local=False)
    vaults = vaults_of(remote)
    vaults.get_key(KID)

    vaults.flag_bad_key(KID, BAD)
    vaults.proven({KID: BAD}, {})
    assert remote.told == []
    assert vaults.proved_wrong(KID, BAD)

    vaults.proven({KID: GOOD}, {})
    vaults.proven({KID: GOOD}, {})
    assert remote.told == [(KID, BAD)]


def test_a_licence_clears_a_flag_of_this_run_without_a_local_vault() -> None:
    remote = MemoryVault({KID: BAD}, local=False)
    vaults = vaults_of(remote)
    vaults.get_key(KID)
    vaults.flag_bad_key(KID, BAD)

    assert vaults.licensed({KID: BAD}) == 1
    assert not vaults.proved_wrong(KID, BAD)
    assert remote.added == {KID: BAD}
    vaults.proven({KID: GOOD}, {})
    assert remote.told == []


def test_one_source_offered_twice_at_once_is_held_once() -> None:
    """Thread A has checked an offer and not yet stored it when thread B offers the same pair."""
    a_storing, b_moved, release = threading.Event(), threading.Event(), threading.Event()
    vaults = vaults_of()
    source = server()

    class HeldList(list):
        def append(self, item: object) -> None:
            if threading.current_thread().name == "a":
                a_storing.set()
                release.wait(10)
            else:
                b_moved.set()
            super().append(item)

    class Lock:
        """The ledger lock, which tells the test when thread B waits for it."""

        def __init__(self) -> None:
            self.lock = threading.Lock()

        def __enter__(self) -> None:
            if threading.current_thread().name == "b":
                b_moved.set()
            self.lock.acquire()

        def __exit__(self, *exc: object) -> None:
            self.lock.release()

    vaults.candidates[KID] = HeldList()
    vaults.lock = Lock()  # type: ignore[assignment]
    a = threading.Thread(target=vaults.offer, args=(KID, BAD, source), name="a")
    b = threading.Thread(target=vaults.offer, args=(KID, BAD, source), name="b")
    a.start()
    assert a_storing.wait(10)
    b.start()
    assert b_moved.wait(10)
    release.set()
    a.join(10)
    b.join(10)

    assert vaults.candidates[KID] == [(BAD, source)]


def test_two_lookups_that_fail_at_once_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    both_asking = threading.Barrier(2, timeout=10)

    class Down(MemoryVault):
        def get_key(self, kid: UUID | str, service: str) -> str | None:
            both_asking.wait()
            raise requests.ConnectTimeout("no answer")

    vaults = vaults_of(Down(local=False, name="down"))
    with caplog.at_level(logging.WARNING, logger="unshackle.core.vaults"):
        threads = [threading.Thread(target=vaults.get_key, args=(KID,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Failed to get key from Vault 'down': no answer",
        "Skipping Vault 'down' for the rest of this run",
    ]
