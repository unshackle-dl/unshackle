"""The vault operations the server asks of Vaults: hold, store local first, flag a served pair, probe."""

from uuid import UUID

import pytest

from tests.core.key_resolver import BrokenVault, MemoryVault, vaults_of
from unshackle.core.vault import Refused, Vault
from unshackle.core.vaults import MODULES, flag_stores

KID = UUID("11111111-2222-3333-4444-555555555555")
KEY = "ab" * 16


def test_a_local_vault_with_the_kid_holds_it_and_a_remote_one_does_not_count() -> None:
    assert vaults_of(MemoryVault({KID: KEY})).holds(KID)
    assert not vaults_of(MemoryVault(), MemoryVault({KID: KEY}, local=False)).holds(KID)


def test_a_local_vault_that_cannot_answer_holds_the_kid_and_one_without_read_access_does_not() -> None:
    assert vaults_of(BrokenVault(OSError("EXAMPLE vault file locked"))).holds(KID)
    assert not vaults_of(BrokenVault(PermissionError("no read access"))).holds(KID)


def test_add_keys_stores_only_in_the_local_or_only_in_the_remote_vaults() -> None:
    local, remote = MemoryVault(), MemoryVault(local=False)
    vaults = vaults_of(local, remote)

    assert vaults.add_keys({KID: KEY}, local=True) == 1
    assert (local.added, remote.added) == ({KID: KEY}, {})
    assert vaults.add_keys({KID: KEY}, local=False) == 1
    assert remote.added == {KID: KEY}


def test_stored_everywhere_counts_an_unreachable_vault_and_skips_a_no_push_one() -> None:
    down = MemoryVault(local=False)
    vaults = vaults_of(MemoryVault(), down, MemoryVault(no_push=True))
    vaults.unreachable.add(down)

    stored = vaults.add_keys({KID: KEY})
    assert stored == 1
    assert not vaults.stored_everywhere(stored)
    assert vaults.stored_everywhere(2)


def test_a_served_pair_is_flagged_in_the_local_vaults_and_its_source_only() -> None:
    local = MemoryVault()
    source = MemoryVault(local=False, name="source")
    bystander = MemoryVault(local=False, name="other")
    broken = BrokenVault(OSError("EXAMPLE vault down"))

    vaults_of(broken, local, bystander, source).flag_served(KID, KEY, "source")

    assert local.told == source.told == [(KID, KEY)]
    assert bystander.told == []


def test_probe_passes_a_vault_that_refuses_the_query_and_raises_any_other_failure() -> None:
    assert vaults_of(BrokenVault(Refused("bad kid"), name="api")).probe(KID) == "api BrokenVault"
    with pytest.raises(OSError):
        vaults_of(BrokenVault(OSError("EXAMPLE vault down"))).probe(KID)


def test_a_vault_type_stores_flags_only_when_it_reads_them_back() -> None:
    for module in MODULES.values():
        assert module.stores_flags == (module.is_bad_key is not Vault.is_bad_key), module


def test_a_vault_type_has_tables_only_when_it_can_make_them() -> None:
    for module in MODULES.values():
        assert module.has_tables == (module.create_table is not Vault.create_table), module


def test_flag_stores_names_the_configs_whose_vault_type_keeps_flags() -> None:
    configs = [
        {"type": "SQLite", "name": "local"},
        {"type": "SQLite"},
        {"type": "API", "name": "shared"},
        {"type": "Unknown", "name": "x"},
    ]
    assert flag_stores(configs) == ["local", "SQLite"]
