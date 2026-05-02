"""Sequential alias name generator.

The bot creates Proton addresses in batches following a deterministic pattern
based on a *base* (e.g. ``vielz``), an alphabetic *suffix* that grows when the
numeric counter wraps, and a zero-padded *number*::

    vielz001, vielz002, ..., vielz999, vielza001, ..., vielzz999, vielzaa001, ...

State is stored per ``(chat_id, primary_id, base)`` so consecutive ``/genaddr``
calls keep producing fresh names without collisions.

Functions in this module are pure — they don't touch the database. The DB
layer in :mod:`proton_telegram_bot.db` is responsible for loading & saving the
``(suffix, number)`` cursor.
"""
from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass

# Alphabetic suffix uses lowercase a..z. We never inject digits or symbols
# into the suffix to keep generated local-parts predictable & valid.
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)

# Hard ceiling for the numeric counter before we roll over to the next suffix.
# 999 keeps every name three digits wide which is what the user asked for.
DEFAULT_MAX_NUMBER = 999

# A small ceiling on how many names a single call may emit. Without this an
# accidental ``/genaddr 999999`` would happily generate 26^N entries until it
# OOMs the server. Higher than this should be split into multiple calls.
MAX_BATCH = 500


class AliasGenError(ValueError):
    """Raised when the caller asks for an impossible/invalid generation."""


@dataclass(frozen=True, slots=True)
class GenState:
    """Cursor describing which name the generator should emit next.

    ``suffix`` is ``""`` for the first 999 names and grows ``a`` -> ``b`` ->
    ... -> ``z`` -> ``aa`` -> ... once the numeric counter wraps. ``number``
    is 1-indexed; ``GenState("", 1)`` means the next name is ``base001``.
    """

    suffix: str
    number: int

    def __post_init__(self) -> None:
        if any(c not in _ALPHABET for c in self.suffix):
            raise AliasGenError(
                f"suffix must be lowercase a-z only, got {self.suffix!r}"
            )
        if self.number < 1:
            raise AliasGenError(
                f"number must be >= 1, got {self.number}"
            )


def _validate_base(base: str) -> str:
    """Lowercase + sanity-check the user-supplied base.

    Local-parts in email addresses can technically contain a wider set of
    characters but Proton's signup form rejects most non-alphanumeric ones.
    We accept ``[a-z0-9._-]`` to stay on the safe side.
    """
    cleaned = base.strip().lower()
    if not cleaned:
        raise AliasGenError("base must not be empty")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if any(c not in allowed for c in cleaned):
        raise AliasGenError(
            f"base contains unsupported characters: {base!r} "
            "(allowed: a-z, 0-9, '.', '_', '-')"
        )
    if len(cleaned) > 32:
        raise AliasGenError("base too long (max 32 chars)")
    return cleaned


def _validate_domain(domain: str) -> str:
    cleaned = domain.strip().lower().lstrip("@")
    if not cleaned or "." not in cleaned:
        raise AliasGenError(f"domain {domain!r} is not a valid hostname")
    return cleaned


def increment_suffix(suffix: str) -> str:
    """Return the next suffix in the sequence ``""`` < ``a`` < ``b`` < ... < ``z`` < ``aa`` < ...

    This is a pure odometer: ``""`` rolls forward to ``a`` (the very first
    overflow), then ``a..z``, then ``aa``, then ``ab``, and so on. ``zz`` rolls
    to ``aaa``.
    """
    if suffix == "":
        return "a"
    chars = list(suffix)
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "z":
            chars[i] = _ALPHABET[_ALPHABET.index(chars[i]) + 1]
            return "".join(chars)
        chars[i] = "a"
        i -= 1
    # Carried past the most-significant digit — extend the length.
    return "a" * (len(suffix) + 1)


def advance(state: GenState, max_number: int = DEFAULT_MAX_NUMBER) -> GenState:
    """Compute the cursor *after* emitting one name from ``state``."""
    if state.number < max_number:
        return GenState(state.suffix, state.number + 1)
    return GenState(increment_suffix(state.suffix), 1)


def name_at(base: str, state: GenState, digits: int = 3) -> str:
    """Render the local-part for the cursor without advancing it."""
    base_clean = _validate_base(base)
    return f"{base_clean}{state.suffix}{state.number:0{digits}d}"


def iter_names(
    base: str,
    state: GenState,
    digits: int = 3,
    max_number: int = DEFAULT_MAX_NUMBER,
) -> Iterator[tuple[str, GenState]]:
    """Infinite iterator yielding ``(name, state_after_emitting_this_name)``.

    Use :func:`generate_batch` for a bounded batch in normal code paths.
    """
    base_clean = _validate_base(base)
    cur = state
    while True:
        name = f"{base_clean}{cur.suffix}{cur.number:0{digits}d}"
        nxt = advance(cur, max_number=max_number)
        yield name, nxt
        cur = nxt


def random_suffix_names(
    base: str,
    count: int,
    *,
    rng: random.Random | None = None,
) -> list[str]:
    """Return ``count`` distinct ``<base><NN>`` local-parts with random
    zero-padded numeric suffixes.

    The suffix width is the smallest digit count that still lets the
    pool fit ``count`` distinct values — i.e. 2 digits for ``count
    <= 100``, 3 digits for ``count <= 1000``, and so on. The minimum is
    2 digits even for tiny ``count``, so the user always sees the
    "vielz88311 / vielz88347 / …" shape requested ("auto generate nya
    ada di vielz88311 yang 11 randomized").

    Names are sampled WITHOUT REPLACEMENT, so the caller can iterate
    through all of them safely without producing duplicates inside the
    same batch. (Proton-side ``ALREADY_EXISTS`` is still possible and
    handled by the orchestrator.)
    """
    base_clean = _validate_base(base)
    if count < 1:
        raise AliasGenError("count must be >= 1")
    if count > MAX_BATCH:
        raise AliasGenError(f"count exceeds MAX_BATCH={MAX_BATCH}")
    digits = max(2, len(str(count - 1)))
    pool = 10 ** digits
    if count > pool:
        raise AliasGenError(
            f"count {count} exceeds {digits}-digit random pool {pool}"
        )
    sampler = rng if rng is not None else random
    suffixes = sampler.sample(range(pool), count)
    return [f"{base_clean}{s:0{digits}d}" for s in suffixes]


def generate_batch(
    base: str,
    state: GenState,
    count: int,
    domain: str | None = None,
    digits: int = 3,
    max_number: int = DEFAULT_MAX_NUMBER,
) -> tuple[list[str], GenState]:
    """Produce ``count`` consecutive names starting from ``state``.

    Returns ``(names, next_state)`` where ``next_state`` is the cursor to
    persist for the next batch. Names are returned as plain local-parts when
    ``domain`` is ``None``, otherwise as ``local@domain`` strings ready to be
    inserted into the ``aliases`` table.
    """
    if count < 1:
        raise AliasGenError("count must be >= 1")
    if count > MAX_BATCH:
        raise AliasGenError(f"count exceeds MAX_BATCH={MAX_BATCH}")

    domain_clean = _validate_domain(domain) if domain is not None else None

    names: list[str] = []
    next_state = state
    iterator = iter_names(base, state, digits=digits, max_number=max_number)
    for _ in range(count):
        local, next_state = next(iterator)
        names.append(f"{local}@{domain_clean}" if domain_clean else local)
    return names, next_state
