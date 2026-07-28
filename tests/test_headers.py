"""Forwarded-header container and wire parsing (SPEC.md §3.2)."""

from __future__ import annotations

from absl.testing import absltest, parameterized

from jetty.headers import DuplicateHeaderError, Headers


def parse(raw: object) -> Headers:
    return Headers.from_wire(raw)


class DuplicateHeaderTest(absltest.TestCase):

    def test_duplicates_are_preserved_not_collapsed(self):
        """The reason `headers` is an array of pairs and not an object."""
        h = parse([["x-corp-user", "alice"], ["x-corp-user", "mallory"]])
        self.assertLen(h, 2)
        self.assertEqual(h.get_all("x-corp-user"), ["alice", "mallory"])
        self.assertEqual(h.count("x-corp-user"), 2)

    def test_sole_raises_on_a_duplicated_header(self):
        """A driver must not be able to silently pick a winner (SPEC.md §3.2)."""
        h = parse([["x-corp-user", "alice"], ["x-corp-user", "mallory"]])
        with self.assertRaises(DuplicateHeaderError) as ctx:
            h.sole("x-corp-user")
        self.assertEqual(ctx.exception.name, "x-corp-user")
        self.assertEqual(ctx.exception.count, 2)

    def test_sole_returns_the_value_when_unique_and_none_when_absent(self):
        h = parse([["x-corp-user", "alice"]])
        self.assertEqual(h.sole("x-corp-user"), "alice")
        self.assertIsNone(h.sole("x-corp-token"))

    def test_there_is_no_getitem(self):
        """The dangerous 'just give me one' accessor is deliberately absent."""
        h = parse([["a", "1"]])
        self.assertFalse(hasattr(h, "__getitem__"))


class NormalisationTest(absltest.TestCase):

    def test_names_are_lowercased_and_lookup_is_case_insensitive(self):
        h = parse([["X-Corp-User", "alice"]])
        self.assertEqual(h.names(), {"x-corp-user"})
        self.assertEqual(h.sole("X-CORP-USER"), "alice")

    def test_order_is_preserved(self):
        h = parse([["b", "1"], ["a", "2"], ["c", "3"]])
        self.assertEqual([n for n, _ in h], ["b", "a", "c"])

    def test_values_are_verbatim(self):
        """No trimming or decoding — a signature is whitespace-sensitive."""
        h = parse([["x-sig", "  padded value  "]])
        self.assertEqual(h.sole("x-sig"), "  padded value  ")

    def test_empty_is_valid(self):
        """'I received no headers' is well-formed; auth simply fails."""
        h = parse([])
        self.assertEmpty(h)
        self.assertIsNone(h.sole("anything"))


class ShapeTest(parameterized.TestCase):

    def test_rejects_an_object_instead_of_pairs(self):
        with self.assertRaisesRegex(ValueError, "array of"):
            parse({"x-corp-user": "alice"})

    def test_rejects_a_bare_string(self):
        with self.assertRaisesRegex(ValueError, "array of"):
            parse("x-corp-user: alice")

    @parameterized.named_parameters(
        ("one_element_pair", [["only-one"]]),
        ("three_element_pair", [["a", "b", "c"]]),
        ("non_string_value", [["a", 1]]),
        ("non_string_name", [[1, "a"]]),
        ("entry_is_not_a_pair", ["not-a-pair"]),
        ("empty_name", [["", "empty name"]]),
    )
    def test_rejects_malformed_entries(self, bad):
        with self.assertRaises(ValueError):
            parse(bad)


class ReprTest(absltest.TestCase):

    def test_repr_never_contains_values(self):
        """reprs end up in logs and tracebacks by accident."""
        h = parse([["x-corp-token", "super-secret"]])
        self.assertNotIn("super-secret", repr(h))
        self.assertIn("x-corp-token", repr(h))

    def test_wire_roundtrip(self):
        raw = [["x-corp-user", "alice"], ["x-corp-user", "bob"]]
        self.assertEqual(parse(raw).to_wire(), raw)


if __name__ == "__main__":
    absltest.main()
