"""Unit tests for the sequential alias generator."""
from __future__ import annotations

import pytest

from proton_telegram_bot.alias_gen import (
    DEFAULT_MAX_NUMBER,
    MAX_BATCH,
    AliasGenError,
    GenState,
    advance,
    generate_batch,
    increment_suffix,
    name_at,
)

# ---------------------------------------------------------------- increment_suffix


@pytest.mark.parametrize(
    "given,expected",
    [
        ("", "a"),
        ("a", "b"),
        ("y", "z"),
        ("z", "aa"),
        ("aa", "ab"),
        ("az", "ba"),
        ("zz", "aaa"),
        ("zzz", "aaaa"),
    ],
)
def test_increment_suffix(given: str, expected: str) -> None:
    assert increment_suffix(given) == expected


# ---------------------------------------------------------------- advance


def test_advance_within_number_block() -> None:
    assert advance(GenState("", 1)) == GenState("", 2)


def test_advance_rolls_suffix_at_max() -> None:
    assert advance(GenState("", DEFAULT_MAX_NUMBER)) == GenState("a", 1)


def test_advance_rolls_suffix_zz_to_aaa() -> None:
    assert advance(GenState("zz", DEFAULT_MAX_NUMBER)) == GenState("aaa", 1)


def test_advance_respects_custom_max_number() -> None:
    assert advance(GenState("", 5), max_number=5) == GenState("a", 1)


# ---------------------------------------------------------------- name_at


def test_name_at_first() -> None:
    assert name_at("vielz", GenState("", 1)) == "vielz001"


def test_name_at_with_suffix() -> None:
    assert name_at("vielz", GenState("a", 7)) == "vielza007"


def test_name_at_double_letter() -> None:
    assert name_at("vielz", GenState("aa", 999)) == "vielzaa999"


def test_name_at_custom_digits() -> None:
    assert name_at("foo", GenState("", 1), digits=4) == "foo0001"


# ---------------------------------------------------------------- generate_batch


def test_generate_batch_simple() -> None:
    names, nxt = generate_batch("vielz", GenState("", 1), count=3)
    assert names == ["vielz001", "vielz002", "vielz003"]
    assert nxt == GenState("", 4)


def test_generate_batch_with_domain() -> None:
    names, nxt = generate_batch(
        "vielz", GenState("", 22), count=3, domain="@proton.me"
    )
    assert names == [
        "vielz022@proton.me",
        "vielz023@proton.me",
        "vielz024@proton.me",
    ]
    assert nxt == GenState("", 25)


def test_generate_batch_domain_normalization() -> None:
    names, _ = generate_batch(
        "vielz", GenState("", 1), count=1, domain="PROTON.ME"
    )
    assert names == ["vielz001@proton.me"]


def test_generate_batch_crosses_suffix_boundary() -> None:
    # max_number=2 forces a roll after every 2 names so we can verify the
    # cursor steps through the full odometer.
    names, nxt = generate_batch(
        "x", GenState("", 1), count=5, max_number=2
    )
    assert names == ["x001", "x002", "xa001", "xa002", "xb001"]
    assert nxt == GenState("b", 2)


def test_generate_batch_resumes_from_state() -> None:
    """Persisting state across calls must not drop or duplicate names."""
    state = GenState("", 1)
    first, state = generate_batch("v", state, count=10)
    second, state = generate_batch("v", state, count=10)
    # Concatenating the two batches must equal a single 20-batch.
    expected, _ = generate_batch("v", GenState("", 1), count=20)
    assert first + second == expected


# ---------------------------------------------------------------- error paths


def test_generate_batch_rejects_zero_count() -> None:
    with pytest.raises(AliasGenError):
        generate_batch("v", GenState("", 1), count=0)


def test_generate_batch_rejects_huge_count() -> None:
    with pytest.raises(AliasGenError):
        generate_batch("v", GenState("", 1), count=MAX_BATCH + 1)


def test_generate_batch_rejects_invalid_domain() -> None:
    with pytest.raises(AliasGenError):
        generate_batch("v", GenState("", 1), count=1, domain="not-a-domain")


def test_genstate_rejects_uppercase_suffix() -> None:
    with pytest.raises(AliasGenError):
        GenState("A", 1)


def test_genstate_rejects_zero_number() -> None:
    with pytest.raises(AliasGenError):
        GenState("", 0)


@pytest.mark.parametrize("base", ["", "vielz!", "viel z", "VIELZ" * 10])
def test_invalid_base(base: str) -> None:
    """Empty / illegal-character / overlong bases must be rejected."""
    with pytest.raises(AliasGenError):
        generate_batch(base, GenState("", 1), count=1)


def test_uppercase_base_normalized_to_lower() -> None:
    """Uppercase is OK as input but normalized so generated names stay lowercase."""
    names, _ = generate_batch("VIELZ", GenState("", 1), count=1)
    assert names == ["vielz001"]
