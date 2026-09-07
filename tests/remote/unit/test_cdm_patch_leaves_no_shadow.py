"""No test may patch ``load_cdm`` on the CDM package with ``setattr``.

``unshackle.core.cdm`` resolves ``load_cdm`` through a lazy ``__getattr__`` (PEP 562), so the
name never sits in the package dict. ``monkeypatch.setattr`` on the package reads the old value
through ``__getattr__`` and, on undo, writes the real function into the package dict. That key
shadows ``__getattr__`` for the rest of the process, and every later test that patches
``unshackle.core.cdm.loader`` loses its stub. ``tests/commands/test_cdm_cache.py`` ran the real
loader and raised ``ValueError: device_b does not exist``.

``monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", ...)`` leaves no residue: it deletes the key
it added. This guard names the file at fault instead of leaving the damage to surface in an
unrelated test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.unit

TESTS_ROOT = Path(__file__).resolve().parents[2]

# A setattr of load_cdm on anything but the loader module, which holds the name for real.
PACKAGE_SETATTR = re.compile(r"""setattr\(\s*(?!loader\b)[\w.]+\s*,\s*["']load_cdm["']""")


def test_no_test_patches_load_cdm_on_the_package() -> None:
    offenders: List[str] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        if path == Path(__file__).resolve():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if PACKAGE_SETATTR.search(line):
                offenders.append(f"{path.relative_to(TESTS_ROOT)}:{number}: {line.strip()}")

    assert not offenders, "patch the package dict with monkeypatch.setitem instead:\n" + "\n".join(offenders)
