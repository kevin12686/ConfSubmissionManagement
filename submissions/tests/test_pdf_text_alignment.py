"""Occurrence accounting and semantic context, independent of PDF encodings."""

from django.test import SimpleTestCase

from submissions.services.pdf_text_alignment import (
    align_tokens, split_confidence_ranges, structure_changes,
)


def tokens(text, **metadata):
    return [{"text": word, **metadata} for word in text.split()]


class PDFTextAlignmentTests(SimpleTestCase):
    def test_duplicate_occurrence_is_not_deduplicated(self):
        a, b = tokens("same word same word"), tokens("same word")
        operations, limited = align_tokens(a, b)
        self.assertFalse(limited)
        self.assertEqual(sum(j - i for op, i, j, _, _ in operations if op == "delete"), 2)

    def test_shared_first_word_does_not_consume_reordered_paragraph_anchor(self):
        first, second = tokens("The first study measured outcomes ."), tokens("The second study measured results .")
        operations, limited = align_tokens(first + second, second + first)
        self.assertFalse(limited)
        self.assertTrue(any(op == "equal" and j - i >= len(first) for op, i, j, _, _ in operations))

    def test_different_table_cells_do_not_cancel_equal_values(self):
        a = tokens("12", context_key=["results", "control"]) + tokens("21", context_key=["results", "treatment"])
        b = tokens("21", context_key=["results", "control"]) + tokens("12", context_key=["results", "treatment"])
        operations, _ = align_tokens(a, b)
        self.assertFalse(any(op == "equal" for op, *_ in operations))

    def test_normalized_context_does_not_depend_on_dictionary_order(self):
        a = tokens("12", context_key={"table": "results", "cell": "control"})
        b = tokens("12", context_key={"cell": "control", "table": "results"})
        self.assertEqual(align_tokens(a, b)[0], [("equal", 0, 1, 0, 1)])

    def test_unreadable_character_cannot_be_equal_merely_by_extracted_text(self):
        a, b = tokens("Q", issues=["Missing mapping"]), tokens("Q")
        self.assertEqual(align_tokens(a, b)[0], [("replace", 0, 1, 0, 1)])
        self.assertEqual(align_tokens(a, a, identical_bytes=True)[0], [("equal", 0, 1, 0, 1)])

    def test_exact_opaque_glyph_identity_is_occurrence_and_context_sensitive(self):
        glyph = tokens("\ufffc", opaque_key="a" * 64, text_identity_supported=True, issues=["Unicode unresolved"])
        self.assertEqual(align_tokens(glyph, glyph)[0], [("equal", 0, 1, 0, 1)])
        different = [{**glyph[0], "opaque_key": "b" * 64}]
        self.assertEqual(align_tokens(glyph, different)[0], [("replace", 0, 1, 0, 1)])
        other_cell = [{**glyph[0], "context_key": "other cell"}]
        self.assertEqual(align_tokens(glyph, other_cell)[0], [("replace", 0, 1, 0, 1)])
        self.assertEqual(align_tokens(glyph + glyph, glyph)[0][-1][0], "delete")

    def test_unproven_opaque_claim_does_not_override_resource_uncertainty(self):
        glyph = tokens("\ufffc", opaque_key="a" * 64, text_identity_supported=False, issues=["Resource conflict"])
        self.assertEqual(align_tokens(glyph, glyph)[0], [("replace", 0, 1, 0, 1)])

    def test_opaque_identity_does_not_hide_supported_script_role_change(self):
        a = tokens("\ufffc", opaque_key="a" * 64, text_identity_supported=True,
                   issues=["Unicode unresolved"], char_meta=[{"script_role": "super"}])
        b = [{**a[0], "char_meta": [{"script_role": "sub"}]}]
        self.assertEqual(list(structure_changes(a, b, [(0, 1, 0, 1)])), [(0, 0, [0])])

    def test_unknown_structure_does_not_invent_a_text_edit(self):
        a = tokens("value", text_identity_supported=True, issues=["Unknown structure"])
        self.assertEqual(align_tokens(a, a)[0], [("equal", 0, 1, 0, 1)])
        self.assertEqual(align_tokens(a, tokens("changed"))[0], [("replace", 0, 1, 0, 1)])

    def test_confident_deletion_is_separate_from_neighboring_unreadable_glyph(self):
        data = tokens("Deleted words") + tokens("?", issues=["Missing mapping"]) + tokens("remain")
        self.assertEqual(split_confidence_ranges(data, 0, 4), [(0, 2), (2, 3), (3, 4)])

    def test_small_change_in_long_repeated_text_is_not_discarded(self):
        a = tokens("word " * 15000)
        b = a[:7500] + tokens("changed") + a[7501:]
        operations, limited = align_tokens(a, b)
        self.assertFalse(limited)
        self.assertIn(("replace", 7500, 7501, 7500, 7501), operations)

    def test_work_limit_keeps_ambiguous_middle_as_unmatched(self):
        a, b = tokens("a b " * 500), tokens("b a " * 500)
        operations, limited = align_tokens(a, b, work_limit=10)
        self.assertTrue(limited)
        self.assertEqual(operations, [("replace", 0, 1000, 0, 1000)])

    def test_script_role_change_is_detected_but_unknown_is_not_invented(self):
        a = tokens("x2", char_meta=[{"script_role": "base"}, {"script_role": "super"}])
        b = tokens("x2", char_meta=[{"script_role": "base"}, {"script_role": "sub"}])
        self.assertEqual(list(structure_changes(a, b, [(0, 1, 0, 1)])), [(0, 0, [1])])
        b[0]["char_meta"][1]["script_role"] = None
        self.assertEqual(list(structure_changes(a, b, [(0, 1, 0, 1)])), [])
