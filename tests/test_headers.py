"""Forwarded-header container and wire parsing (SPEC.md §3.5)."""

from __future__ import annotations

import pytest

from jetty.headers import DuplicateHeaderError, Headers

LIMITS = {"max_entries": 128, "max_bytes": 64 * 1024}


def parse(raw: object, **over: int) -> Headers:
    return Headers.from_wire(raw, **{**LIMITS, **over})


# ------------------------------------------------------------ duplicates


def test_duplicates_are_preserved_not_collapsed():
    """The reason `headers` is an array of pairs and not an object."""
    h = parse([["x-corp-user", "alice"], ["x-corp-user", "mallory"]])
    assert len(h) == 2
    assert h.get_all("x-corp-user") == ["alice", "mallory"]
    assert h.count("x-corp-user") == 2


def test_sole_raises_on_a_duplicated_header():
    """A driver must not be able to silently pick a winner (SPEC.md §3.5)."""
    h = parse([["x-corp-user", "alice"], ["x-corp-user", "mallory"]])
    with pytest.raises(DuplicateHeaderError) as e:
        h.sole("x-corp-user")
    assert e.value.name == "x-corp-user"
    assert e.value.count == 2


def test_sole_returns_the_value_when_unique_and_none_when_absent():
    h = parse([["x-corp-user", "alice"]])
    assert h.sole("x-corp-user") == "alice"
    assert h.sole("x-corp-token") is None


def test_there_is_no_getitem():
    """The dangerous 'just give me one' accessor is deliberately absent."""
    h = parse([["a", "1"]])
    assert not hasattr(h, "__getitem__")


# ------------------------------------------------------------ normalisation


def test_names_are_lowercased_and_lookup_is_case_insensitive():
    h = parse([["X-Corp-User", "alice"]])
    assert h.names() == {"x-corp-user"}
    assert h.sole("X-CORP-USER") == "alice"


def test_order_is_preserved():
    h = parse([["b", "1"], ["a", "2"], ["c", "3"]])
    assert [n for n, _ in h] == ["b", "a", "c"]


def test_values_are_verbatim():
    """No trimming or decoding — a signature is whitespace-sensitive."""
    h = parse([["x-sig", "  padded value  "]])
    assert h.sole("x-sig") == "  padded value  "


def test_empty_is_valid():
    """'I received no headers' is well-formed; auth simply fails."""
    h = parse([])
    assert len(h) == 0
    assert h.sole("anything") is None


# ------------------------------------------------------------ limits & shape


def test_rejects_an_object_instead_of_pairs():
    with pytest.raises(ValueError, match="array of"):
        parse({"x-corp-user": "alice"})


def test_rejects_a_bare_string():
    with pytest.raises(ValueError, match="array of"):
        parse("x-corp-user: alice")


@pytest.mark.parametrize(
    "bad",
    [
        [["only-one"]],
        [["a", "b", "c"]],
        [["a", 1]],
        [[1, "a"]],
        ["not-a-pair"],
        [["", "empty name"]],
    ],
)
def test_rejects_malformed_entries(bad):
    with pytest.raises(ValueError):
        parse(bad)


def test_rejects_too_many_headers():
    raw = [[f"x-{i}", "v"] for i in range(200)]
    with pytest.raises(ValueError, match="too many headers"):
        parse(raw, max_entries=128)
    assert len(parse(raw[:128], max_entries=128)) == 128


def test_rejects_oversized_headers():
    """A multi-KiB Cookie is normal once every header is forwarded."""
    raw = [["cookie", "x" * 100_000]]
    with pytest.raises(ValueError, match="too large"):
        parse(raw)


def test_limit_errors_do_not_leak_header_values():
    """SPEC.md §1.4 — any forwarded header may be a credential."""
    secret = "super-secret-token-value"
    with pytest.raises(ValueError) as e:
        parse([["x-token", secret]], max_bytes=4)
    assert secret not in str(e.value)


def test_repr_never_contains_values():
    """reprs end up in logs and tracebacks by accident."""
    h = parse([["x-corp-token", "super-secret"]])
    assert "super-secret" not in repr(h)
    assert "x-corp-token" in repr(h)


def test_byte_size_counts_names_and_values():
    assert parse([["ab", "cde"]]).byte_size() == 5


def test_wire_roundtrip():
    raw = [["x-corp-user", "alice"], ["x-corp-user", "bob"]]
    assert parse(raw).to_wire() == raw
