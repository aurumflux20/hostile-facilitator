"""The version is a CLAIM: it is stamped into every signed result document.

It lives in three places — pyproject.toml, hostile_facilitator.__version__, and
the git tag the README and action.yml install from. On 10 Sep the pins said
v0.1.2 while pyproject said 0.2.0; on 11 Sep __init__ still said 0.1.2 and put
that string inside a Rekor-anchored artifact describing a 10-mode battery that
0.1.2 never had.

A mismatch here is not cosmetic. It is a false statement about which instrument
produced a result, signed by us, in a public transparency log.
"""
import re
from pathlib import Path

import hostile_facilitator

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def test_dunder_version_matches_pyproject():
    assert hostile_facilitator.__version__ == _pyproject_version(), (
        "hostile_facilitator.__version__ and pyproject.toml disagree — the "
        "result document stamps __version__, so this ships a wrong version "
        "inside a signed artifact"
    )


def test_install_pins_match_pyproject():
    """README and the composite Action install by git tag. A tag that predates
    the current version installs a battery with fewer modes than we describe."""
    expected = f"@v{_pyproject_version()}"
    for name in ("README.md", "action.yml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        pins = re.findall(r"hostile-facilitator(@v[\d.]+)", text)
        assert pins, f"{name} pins no version at all"
        wrong = sorted({p for p in pins if p != expected})
        assert not wrong, f"{name} pins {wrong}, expected {expected!r}"
