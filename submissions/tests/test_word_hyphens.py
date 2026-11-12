"""Word hyphen tolerance must not become a dash/minus deletion rule."""
import copy
import random
import unittest

from scripts.accept_rapidocr_real3pairs import check_accounting, check_provenance
from submissions.services.rapidocr_comparison import core, engine, hyphens
from submissions.services.rapidocr_comparison.result import build_result, validate_result
from submissions.tests.test_rapidocr_acceptance import page


class WordHyphenTests(unittest.TestCase):
    def compare(self, before, after, **kwargs):
        left, right = [page(before)], [page(after)]
        result = engine.compare_pages(left, right, **kwargs)
        check_accounting(result)
        check_provenance(result, {'before': left, 'after': right})
        return result

    def test_only_word_hyphens_in_both_directions_are_info(self):
        for before, after in (
            ('real-time', 'realtime'), ('real-time', 'real time'),
            ('re-enter', 'reenter'), ('well-known', 'wellknown'),
            ('state-of-the-art', 'stateoftheart'), ('state-of-the-art', 'state of the art'),
            ('long-term-study', 'longterm-study'), ('co\u2010operate', 'cooperate'),
            ('co\u2011operate', 'cooperate'), ('r\u00e9-enter', 'r\u00e9enter'),
        ):
            for left, right in ((before, after), (after, before)):
                with self.subTest(left=left, right=right):
                    result = self.compare(f'The {left} method was checked.', f'The {right} method was checked.')
                    self.assertFalse(result['findings'])
                    self.assertFalse(result['handoffs'])
                    self.assertTrue(any(item['kind'] == 'word_hyphen' for item in result['info']))

    def test_unchanged_compounds_are_not_new_findings(self):
        result = self.compare('The real-time method is state-of-the-art.', 'The real-time method is state-of-the-art.')
        self.assertFalse(result['findings'])
        self.assertFalse(result['info'])

    def test_minus_ranges_formulas_identifiers_and_ambiguous_dashes_stay_warnings(self):
        for before, after in (
            ('-0.05', '0.05'), ('\u22120.05', '0.05'), ('10-20', '1020'),
            ('10\u201320', '1020'), ('x-y', 'xy'), ('x - y', 'xy'),
            ('a-b', 'ab'), ('\u03b1\u03b2-\u03b3\u03b4', '\u03b1\u03b2\u03b3\u03b4'),
            ('real\u2212time', 'realtime'), ('real\u2013time', 'realtime'),
            ('real\u2014time', 'realtime'), ('real--time', 'realtime'),
            ('real - time', 'realtime'), ('real- time', 'realtime'),
            ('alpha-beta=gamma', 'alphabeta=gamma'),
            ('(alpha-beta)=gamma', '(alphabeta)=gamma'),
            ('value=alpha-beta', 'value=alphabeta'),
            ('foo-bar2', 'foobar2'), ('foo_bar-baz', 'foo_barbaz'),
            ('foo-bar-b', 'foobarb'), ('real-time/other', 'realtime/other'),
        ):
            for left, right in ((before, after), (after, before)):
                with self.subTest(left=left, right=right):
                    result = self.compare(f'The expression {left} was checked.', f'The expression {right} was checked.')
                    self.assertTrue(result['findings'] or result['handoffs'])
                    self.assertFalse(any(item['kind'] == 'word_hyphen' for item in result['info']))

    def test_math_context_outside_an_anchor_cannot_be_lost_in_the_local_slice(self):
        before = 'This retained explanatory context defines the equation = alpha-beta and keeps its surrounding mathematical meaning.'
        after = before.replace('alpha-beta', 'alphabeta')
        result = self.compare(before, after)
        self.assertTrue(result['findings'])
        self.assertFalse(result['info'])

    def test_adjacent_real_changes_stay_separate_and_detected(self):
        result = self.compare('The real-time method used 23 trials.', 'The realtime method used 99 trials.')
        self.assertTrue(any(item['kind'] == 'word_hyphen' for item in result['info']))
        self.assertTrue(any('23' in item['before_text'] and '99' in item['after_text'] for item in result['findings']))
        result = self.compare('The real-time 23 result was retained.', 'The realtime 99 result was retained.')
        self.assertTrue(result['findings'])
        self.assertTrue(result['info'])

    def test_letters_case_questions_decimals_and_deletions_are_not_hyphen_only(self):
        for before, after in (
            ('real-time', 'real-times'), ('real-time', 'realtme'),
            ('re-enter', 'reentry'), ('re-enter', 'Reenter'),
            ('real-time', 'real?time'), ('Figure 2', 'Figure ?'),
            ('10.5', '105'), ('retained word', 'retained'),
        ):
            with self.subTest(before=before, after=after):
                result = self.compare(f'The result {before} was checked.', f'The result {after} was checked.')
                self.assertTrue(result['findings'] or result['handoffs'])

    def test_repeated_words_cannot_cancel_a_deletion(self):
        result = self.compare('The real-time real-time method was checked.', 'The realtime method was checked.')
        self.assertTrue(result['findings'])

    def test_no_reliable_context_remains_unlocated(self):
        left, right = page('real-time'), page('realtime')
        for item in (left, right): item['ocr']['lines'] = item['ocr']['lines'][2:3]
        result = engine.compare_pages([left], [right])
        check_accounting(result)
        self.assertTrue(result['handoffs'])
        self.assertFalse(result['info'])

    def test_literally_requested_comparison_does_not_enable_word_tolerance(self):
        result = self.compare('The real-time method was checked.', 'The realtime method was checked.', profile='literal')
        self.assertTrue(result['findings'])
        self.assertFalse(result['info'])

    def test_field_ownership_is_not_cancelled_by_word_equivalence(self):
        a, _, _ = core.flatten([page('real-time')])
        b, _, _ = core.flatten([page('realtime')])
        for t in a: t['owner'] = 'Field A'
        for t in b: t['owner'] = 'Field B'
        self.assertFalse(hyphens.alignment_pairs(a, b))

    def test_wrap_and_shift_keep_original_text_and_all_occurrences(self):
        left, right = page('The network method was checked.'), page('The net-')
        right['ocr']['lines'].insert(3, {'text': 'work method was checked.', 'box': [40, 230, 300, 248], 'words': [], 'score': 1})
        for line in right['ocr']['lines']:
            x, y, w, z = line['box'];line['box'] = [x+25, y+40, w+25, z+40]
        original = copy.deepcopy((left, right))
        result = engine.compare_pages([left], [right])
        check_accounting(result)
        check_provenance(result, {'before': [left], 'after': [right]})
        self.assertFalse(result['findings'])
        self.assertTrue(result['info'])
        self.assertEqual((left, right), original)

    def test_wrap_cannot_bypass_math_context_or_missing_anchors(self):
        before, after = page('The equation = alpha-'), page('The equation = alphabeta was checked.')
        before['ocr']['lines'].insert(3, {'text': 'beta was checked.', 'box': [40, 230, 300, 248], 'words': [], 'score': 1})
        result = engine.compare_pages([before], [after])
        check_accounting(result)
        self.assertTrue(result['findings'])
        self.assertFalse(result['info'])
        before['ocr']['lines'] = before['ocr']['lines'][2:4]
        after['ocr']['lines'] = after['ocr']['lines'][2:3]
        result = engine.compare_pages([before], [after])
        check_accounting(result)
        self.assertTrue(result['handoffs'])
        self.assertFalse(result['info'])

    def test_randomized_compounds_and_neighbor_edits_preserve_true_differences(self):
        rng = random.Random(81237)
        for _ in range(35):
            parts = [''.join(rng.choice('abcdefghjkpqrsuwyz') for _ in range(rng.randrange(3, 9))) for _ in range(rng.randrange(2, 5))]
            old, new = '-'.join(parts), ''.join(parts)
            result = self.compare(f'The {old} method used 23 trials.', f'The {new} method used 99 trials.')
            self.assertTrue(result['info'])
            self.assertTrue(result['findings'])

    def test_public_result_retains_info_labels_raw_boxes_counts_and_validation(self):
        result = build_result([page('The real-time method used 23 trials.')], [page('The realtime method used 99 trials.')])
        validate_result(result)
        info = next(item for item in result['findings'] if item['kind'] == 'word_hyphen')
        self.assertEqual(info['category'], 'formatting')
        self.assertEqual(info['label'], 'Hyphen-only word difference')
        self.assertEqual(result['info_kind_counts']['word_hyphen'], 1)
        self.assertGreater(result['content_count'], 0)
        for side in ('before', 'after'):
            self.assertTrue(info[side+'_locations'])
            self.assertTrue(info[side+'_ocr'])

    def test_worker_cannot_relabel_a_minus_or_real_letter_change_as_word_info(self):
        result = build_result([page('The real-time method was checked.')], [page('The realtime method was checked.')])
        for kind in ('word_hyphen', 'wrap_hyphen'):
            for left, right in (('-0.05', '0.05'), ('10-20', '1020'), ('real-time', 'realtimes'), ('x-y', 'xy')):
                forged = copy.deepcopy(result)
                finding = next(item for item in forged['findings'] if item['kind'] == 'word_hyphen')
                finding.update(kind=kind, before_text=left, after_text=right)
                with self.subTest(kind=kind, left=left), self.assertRaisesRegex(ValueError, 'hyphen-only'):
                    validate_result(forged)

    def test_large_rewrites_and_word_work_bounds_do_not_become_clean(self):
        result = self.compare(' '.join(['old']*210), ' '.join(['new']*210))
        self.assertTrue(result['handoffs'])
        before = 'alpha-'+'b'*1600
        result = self.compare(before, before.replace('-', ''))
        self.assertTrue(result['findings'] or result['handoffs'])
