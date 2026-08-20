import threading
import time
from typing import Iterator, Optional, Union
from uuid import UUID

from unshackle.core.vault import Vault
from unshackle.core.vaults import Vaults


class FakeVault(Vault):
    def __init__(self, name: str, key: Optional[str], delay: float = 0.0, local: bool = False):
        super().__init__(name)
        self.key, self.delay, self.local = key, delay, local
        self.calls = 0
        self.threads: set[int] = set()

    def get_key(self, kid: Union[UUID, str], service: str) -> Optional[str]:
        self.calls += 1
        self.threads.add(threading.get_ident())
        time.sleep(self.delay)
        return self.key

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        return iter(())

    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        return False

    def add_keys(self, service: str, kid_keys: dict) -> int:
        return 0

    def get_services(self) -> Iterator[str]:
        return iter(())


def make(*vaults: FakeVault) -> Vaults:
    v = Vaults("SVC")
    v.vaults = list(vaults)
    return v


def test_local_hit_skips_remote() -> None:
    local = FakeVault("local", "aa" * 16, local=True)
    remote = FakeVault("remote", "bb" * 16)
    assert make(remote, local).get_key("kid") == ("aa" * 16, local)
    assert remote.calls == 0


def test_remote_vaults_run_together_and_first_hit_wins() -> None:
    local = FakeVault("local", None, local=True)
    slow = FakeVault("slow", "cc" * 16, delay=1.0)
    fast = FakeVault("fast", "dd" * 16, delay=0.05)
    start = time.monotonic()
    key, vault = make(local, slow, fast).get_key("kid")
    assert (key, vault) == ("dd" * 16, fast)
    assert time.monotonic() - start < 0.9  # did not wait for the slow vault
    assert slow.calls == 1 and local.calls == 1
    assert slow.threads != fast.threads


def test_miss_everywhere() -> None:
    assert make(FakeVault("a", None), FakeVault("b", "0" * 32)).get_key("kid") == (None, None)
