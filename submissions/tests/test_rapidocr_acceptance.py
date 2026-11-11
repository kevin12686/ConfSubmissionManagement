"""Method-7 OCR-only contracts; these do not assert glyph or native-text proof."""
import copy
import json
import random
import re
import subprocess
import unittest
from unittest.mock import patch
from submissions.services.rapidocr_comparison import core, engine, locator, runtime, symbols
from scripts.accept_rapidocr_real3pairs import check_accounting, check_provenance, gold_checks
from django.test import SimpleTestCase

TITLE='A reliable scientific measurement and independent verification experiment'
START='The introductory explanation preserves unchanged context for every comparison.'
END='The final discussion retains independent evidence and reproducible observations.'


def page(body,x=40,y=200):
    lines=[]
    for text,xx,yy in [(TITLE,40,60),(START,40,120),(body,x,y),(END,40,360)]:
        lines.append({'text':text,'box':[xx,yy,xx+len(text)*7,yy+18],'words':[],'score':1})
    return {'page':1,'width':1000,'height':1000,'ocr':{'status':'completed','lines':lines}}


class VisualTests(unittest.TestCase):
    def compare(self,left,right,profile='broad'):
        result = engine.compare_pages([page(left)],[page(right)],profile=profile)
        check_accounting(result)
        return result

    def test_every_direct_rule_both_directions_is_accounted(self):
        for left,right,rid in symbols.rules('broad'):
            a=[{'char':c,'owner':None} for c in left];b=[{'char':c,'owner':None} for c in right]
            with self.subTest(left=left,right=right):
                result=engine.visual_alignment(a,b,'broad')
                self.assertFalse(any(r[0]=='change' for r in result))
                self.assertTrue(any(r[0]=='tolerated' for r in result))

    def test_no_transitive_rule_chaining(self):
        # 8/B and B/Greek Beta do not silently create an 8/Greek Beta rule.
        a=[{'char':'8','owner':None}];b=[{'char':'Β','owner':None}]
        self.assertTrue(any(p[0]=='change' for p in engine.visual_alignment(a,b,'broad')))

    def test_owner_cannot_be_cancelled(self):
        a=[{'char':'0','owner':'Dose A'}];b=[{'char':'O','owner':'Dose B'}]
        self.assertTrue(any(p[0]=='change' for p in engine.visual_alignment(a,b,'broad')))

    def test_glyph_substitutions_are_not_insertions_or_deletions(self):
        for left,right in [('The value is -0.05.','The value is 0.05.'),
                           ('The value is 10.5.','The value is 105.'),
                           ('The reference is Figure 2.','The reference is Figure ?.'),
                           ('The result was carefully inspected.','The result was inspected.'),
                           ('The result was stable.','The result was independently stable.')]:
            with self.subTest(left=left):
                r=self.compare(left,right);self.assertTrue(r['findings']);self.assertFalse(r['handoffs'])

    def test_printed_word_reference_error_still_visible(self):
        self.assertTrue(self.compare('The reference was Figure 2.','Error! Reference source not found.')['findings'])

    def test_garbage_still_visible(self):
        self.assertTrue(self.compare('The calibration passed.','The c@l!br@t??n passed.')['findings'])

    def test_nonconfusable_numeric_edit_stays(self):
        r=self.compare('The measured dose was 23 units.','The measured dose was 99 units.')
        self.assertTrue(r['findings']);self.assertFalse(r['handoffs'])

    def test_all_listed_groups_enabled_in_broad(self):
        for left,right in [('O','0'),('I','l'),('rn','m'),('5','S'),('f','t'),('+','十')]:
            r=self.compare('The code reads '+left+' here.','The code reads '+right+' here.')
            self.assertFalse(r['findings']);self.assertTrue(r['ignored_visual'])

    def test_core_does_not_include_weak_pairs(self):
        for left,right in [('5','S'),('f','t'),('u','v')]:
            self.assertTrue(self.compare('The code reads '+left+' here.','The code reads '+right+' here.','core')['findings'])

    def test_safety_false_alarm_and_real_edit_share_the_same_blind_spot(self):
        r=self.compare('The safety analysis was inspected.','The satety analysis was inspected.')
        self.assertFalse(r['findings']);self.assertTrue(r['ignored_visual'])

    def test_adjacent_true_change_is_not_swallowed(self):
        r=self.compare('The safety analysis used 23 trials.','The satety analysis used 99 trials.')
        self.assertTrue(r['findings']);self.assertTrue(r['ignored_visual']);self.assertFalse(r['handoffs'])

    def test_spacing_and_visual_policy_both_preserve_audit(self):
        r=self.compare('The code was O1 andinsufficient.','The code was 0l and insufficient.')
        self.assertFalse(r['findings']);self.assertTrue(r['ignored_visual']);self.assertTrue(r['ignored_spacing'])

    def test_word_order_does_not_become_a_visual_exception(self):
        self.assertTrue(self.compare('The method uses careful teaching.','The method uses teaching careful.')['findings'])

    def test_case_remains_sensitive_except_explicit_groups(self):
        self.assertTrue(self.compare('Safety was checked.','safety was checked.')['findings'])

    def test_superscripts_are_not_flattened(self):
        self.assertTrue(self.compare('The expression was x².','The expression was x2.')['findings'])

    def test_decimal_separator_is_not_mapped_to_a_comma(self):
        self.assertTrue(self.compare('The value is 1.5.','The value is 1,5.')['findings'])

    def test_missing_question_mark_is_not_tolerated(self):
        self.assertTrue(self.compare('The reference is Figure ?.','The reference is Figure .')['findings'])

    def test_repeated_word_deletion_is_not_visual_equivalence(self):
        self.assertTrue(self.compare('The method method was retained.','The method was retained.')['findings'])

    def test_shift_does_not_create_changes(self):
        a=page('The unchanged prose remains available.');b=copy.deepcopy(a)
        for line in b['ocr']['lines']:
            x,y,u,v=line['box'];line['box']=[x+35,y+65,u+35,v+65]
        r=engine.compare_pages([a],[b]);self.assertFalse(r['findings']);self.assertFalse(r['handoffs'])

    def test_input_records_unchanged(self):
        a=page('The code was O1.');b=page('The code was 0l.');original=copy.deepcopy((a,b))
        engine.compare_pages([a],[b]);self.assertEqual((a,b),original)

    def test_empty_ocr_is_not_a_pass(self):
        a=page('');a['ocr']['lines']=[]
        r=engine.compare_pages([a],[a]);self.assertEqual(r['status'],'no_ocr_text');self.assertTrue(r['warnings'])

    def test_failed_ocr_not_suppressed(self):
        a=page('');a['ocr']['status']='failed'
        with self.assertRaises(ValueError):engine.compare_pages([a],[page('text')])

    def test_no_context_real_difference_is_unlocated(self):
        a=page('');a['ocr']['lines']=a['ocr']['lines'][2:3];a['ocr']['lines'][0]['text']='Unrelated original document'
        b=page('');b['ocr']['lines']=b['ocr']['lines'][2:3];b['ocr']['lines'][0]['text']='Entirely different revised text'
        self.assertTrue(engine.compare_pages([a],[b])['handoffs'])

    def test_work_bound(self):
        with self.assertRaises(ValueError):engine.visual_alignment([{'char':'a','owner':None}]*1601,[],'broad')

    def test_partial_similarity_does_not_fragment_a_real_edit(self):
        r=self.compare('The safety analysis was retained.','The satetx analysis was retained.')
        self.assertEqual(len(r['findings']),1);self.assertFalse(r['ignored_visual'])

    def test_randomized_occurrences_and_edits_account_every_character(self):
        rng = random.Random(1947)
        for _ in range(60):
            words = [rng.choice(('method', '23', 'alpha', 'beta', 'result')) for _ in range(12)]
            changed = list(words)
            changed.pop(rng.randrange(len(changed)))
            changed.insert(rng.randrange(len(changed)), 'independent')
            with self.subTest(before=words, after=changed):
                result = self.compare(' '.join(words), ' '.join(changed))
                self.assertTrue(result['findings'] or result['handoffs'])

    def test_large_gap_remains_accounted_for_as_author_handoff(self):
        result = self.compare(' '.join(['old'] * 210), ' '.join(['new'] * 210))
        self.assertTrue(result['handoffs'])
        self.assertFalse(result['findings'])


    def test_token_work_limit_fails_instead_of_losing_remainder(self):
        with patch.dict(core.CONFIG, maximum_document_tokens=1):
            with self.assertRaisesRegex(ValueError, 'work bound'):
                self.compare('one two', 'one three')

    def test_raw_ocr_provenance_survives_tolerance_and_real_edit(self):
        result = self.compare('The safety analysis used 23 trials.', 'The satety analysis used 99 trials.')
        for category in ('findings', 'ignored_visual'):
            self.assertTrue(result[category])
            for finding in result[category]:
                for side in ('before', 'after'):
                    self.assertTrue(finding[side + '_ocr'])
                    for line in finding[side + '_ocr']:
                        self.assertEqual(line['score'], 1)
                        self.assertEqual(line['page'], 1)
                        self.assertEqual(len(line['box']), 4)

    def test_one_empty_side_is_not_complete_even_when_other_side_has_text(self):
        empty = page('')
        empty['ocr']['lines'] = []
        result = engine.compare_pages([empty], [page('Visible text')])
        check_accounting(result)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['warnings'])
        self.assertTrue(result['handoffs'])


def native_hint(ocr_page):
    words = []
    for block, line in enumerate(ocr_page['ocr']['lines']):
        x, y, _, bottom = line['box']
        for number, match in enumerate(re.finditer(r'\S+', line['text'])):
            words.append({'text': match.group(), 'box': [x + match.start()*7, y, x + match.end()*7, bottom],
                          'block': block, 'line': 0, 'word': number})
    return [{'page': ocr_page['page'], 'words': words}]


class NativeOrderingTests(unittest.TestCase):
    def test_stale_native_reference_cannot_hide_visible_question(self):
        before, after = page('The reference to Figure 2 remains visible.'), page('The reference to Figure ? remains visible.')
        result = engine.compare_pages([before], [after], native_hint(before), native_hint(before))
        check_accounting(result)
        self.assertTrue(result['findings'])
        self.assertIn('?', ' '.join(row['after_text'] for row in result['findings']))

    def test_native_spelling_never_changes_ocr_result(self):
        before, after = page('The result was stable.'), page('The result was independently stable.')
        hints = native_hint(before)
        poisoned = copy.deepcopy(hints)
        for word in poisoned[0]['words']:
            word['text'] = 'invented native words must not become OCR'
        expected = engine.compare_pages([before], [after], hints, hints)
        self.assertEqual(engine.compare_pages([before], [after], hints, poisoned), expected)
        self.assertIn('independently', ' '.join(row['after_text'] for row in expected['findings']))

    def test_reversed_stale_word_slots_preserve_each_ocr_occurrence(self):
        source = page('The method method was independently inspected.')
        hints = native_hint(source)
        for word in hints[0]['words']:
            word['block'] = 0
        hints[0]['words'].reverse()
        tokens, _, _ = core.flatten([source])
        ordered, _ = locator.guide(tokens, [source], hints)
        self.assertEqual([(t['text'], t['box']) for t in ordered], [(t['text'], t['box']) for t in tokens])

    def test_native_layer_cannot_fill_empty_or_failed_ocr(self):
        hints = native_hint(page('Native-only text cannot prove visible content.'))
        empty = page('')
        empty['ocr']['lines'] = []
        result = engine.compare_pages([empty], [empty], hints, hints)
        self.assertEqual(result['status'], 'no_ocr_text')
        self.assertTrue(result['warnings'])
        empty['ocr']['status'] = 'failed'
        with self.assertRaises(ValueError):
            engine.compare_pages([empty], [empty], hints, hints)

    def test_rotated_pdf_native_boxes_use_rendered_frame_and_never_copy_text(self):
        import fitz

        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), fitz.open() as document:
                pdf_page = document.new_page(width=300, height=400)
                pdf_page.insert_text((40, 80), 'Native geometry only')
                pdf_page.set_rotation(rotation)
                source_box = fitz.Rect(pdf_page.get_text('words')[0][:4]) * pdf_page.rotation_matrix
                data = document.tobytes()
                with patch.object(runtime, 'recognize_page', return_value={'status': 'completed', 'lines': []}):
                    pages, native = runtime.extract_document(data, object())
                width, height = pages[0]['width'], pages[0]['height']
                expected = [source_box.x0*width/pdf_page.rect.width, source_box.y0*height/pdf_page.rect.height,
                            source_box.x1*width/pdf_page.rect.width, source_box.y1*height/pdf_page.rect.height]
                for actual, coordinate in zip(native[0]['words'][0]['box'], expected):
                    self.assertAlmostEqual(actual, coordinate, places=9)
                self.assertTrue(all('text' not in word for word in native[0]['words']))
                for word in native[0]['words']:
                    x0, y0, x1, y1 = word['box']
                    self.assertTrue(0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)


class OCRWorkerBoundaryTests(SimpleTestCase):
    """Generic fail-closed properties retained from the retired glyph worker."""

    def compare_response(self, payload):
        from submissions.services.pdf_comparison import compare_pdf_bytes

        response = subprocess.CompletedProcess('ocr-worker', 0, stdout=payload)
        with patch('submissions.services.pdf_comparison_execution.subprocess.run', return_value=response):
            return compare_pdf_bytes(b'before PDF bytes', b'after PDF bytes')

    def valid_response(self):
        from submissions.services.rapidocr_comparison.result import build_result

        empty = dict(page(''), page=2, ocr={'status': 'completed', 'lines': []})
        result = build_result([page('The reference was Figure 2.')],
                              [page('Error! Reference source not found.'), empty])
        return {'version': 7, 'result': result}

    def test_worker_timeout_oserror_and_nonzero_are_unavailable(self):
        from submissions.services.pdf_comparison import PDFComparisonError, compare_pdf_bytes

        for failure in (subprocess.TimeoutExpired('ocr-worker', 240), OSError('unavailable'),
                        subprocess.CalledProcessError(1, 'ocr-worker', stderr=b'OCR failed')):
            with self.subTest(failure=type(failure).__name__):
                with patch('submissions.services.pdf_comparison_execution.subprocess.run', side_effect=failure):
                    with self.assertRaises(PDFComparisonError):
                        compare_pdf_bytes(b'before', b'after')

    def test_worker_malformed_incomplete_and_old_schema_are_unavailable(self):
        from submissions.services.pdf_comparison import PDFComparisonError

        for response in (b'', b'not JSON', b'null', b'[]', b'{}', b'{"version":7,"result":{}}',
                         b'{"version":6,"result":{}}', b'\xff'):
            with self.subTest(response=response), self.assertRaises(PDFComparisonError):
                self.compare_response(response)

    def test_worker_structured_failure_never_becomes_empty_success(self):
        from submissions.services.pdf_comparison import PDFComparisonError

        for error in ('configuration', 'ocr_failed', 'unknown_failure'):
            with self.subTest(error=error), self.assertRaises(PDFComparisonError):
                self.compare_response(json.dumps({'version': 7, 'error': error}).encode())

    def test_empty_and_oversized_input_reject_before_worker(self):
        from submissions.services.pdf_comparison import MAX_BYTES, PDFComparisonError, compare_pdf_bytes

        for data in (b'', b'x' * (MAX_BYTES + 1)):
            with self.subTest(size=len(data)), patch('submissions.services.pdf_comparison_execution.subprocess.run') as worker:
                with self.assertRaises(PDFComparisonError):
                    compare_pdf_bytes(data, b'other PDF')
                worker.assert_not_called()

    def test_worker_valid_response_retains_all_findings(self):
        payload = self.valid_response()
        self.assertGreater(len(payload['result']['findings']), 1)
        self.assertEqual(self.compare_response(json.dumps(payload).encode()), payload['result'])

    def test_duplicate_finding_identity_and_inconsistent_totals_are_rejected(self):
        from submissions.services.pdf_comparison import PDFComparisonError

        valid = self.valid_response()
        for kind in ('duplicate', 'total_findings', 'content_count', 'total_attention_count'):
            payload = copy.deepcopy(valid)
            result = payload['result']
            if kind == 'duplicate':
                result['findings'][1]['id'] = result['findings'][0]['id']
            else:
                result[kind] += 1
            with self.subTest(kind=kind), self.assertRaises(PDFComparisonError):
                self.compare_response(json.dumps(payload).encode())


    def test_nonfinite_inverted_outside_and_wrong_page_coordinates_are_rejected(self):
        from submissions.services.pdf_comparison import PDFComparisonError

        for defect in ('nan', 'infinity', 'outside', 'inverted', 'page'):
            payload = self.valid_response()
            location = next(location for row in payload['result']['findings'] for location in row['after_locations'])
            if defect == 'page':
                location['page'] = 999
            else:
                location['box'] = {'nan': [0, float('nan'), 1, 1], 'infinity': [0, 0, float('inf'), 1],
                                   'outside': [-1, 0, 1, 1], 'inverted': [1, 0, 0, 1]}[defect]
            with self.subTest(defect=defect), self.assertRaises(PDFComparisonError):
                self.compare_response(json.dumps(payload).encode())

    def test_oversized_worker_output_is_rejected_before_json_decode(self):
        from submissions.services.pdf_comparison import PDFComparisonError

        with patch('submissions.services.pdf_comparison.json.loads') as decode:
            with self.assertRaises(PDFComparisonError):
                self.compare_response(b' ' * (8 * 1024 * 1024 + 1))
            decode.assert_not_called()

    def test_forged_ocr_accounting_and_provenance_are_rejected(self):
        from submissions.services.pdf_comparison import PDFComparisonError

        for defect in ('accounting', 'summary', 'score', 'raw_page'):
            payload = self.valid_response()
            result = payload['result']
            if defect == 'accounting':
                result['ocr_accounting']['before_characters'] = -1
            elif defect == 'summary':
                result['evidence_summary']['before']['ocr_characters'] += 1
            else:
                raw = next(raw for row in result['findings'] for raw in row['after_ocr'])
                raw['score' if defect == 'score' else 'page'] = float('nan') if defect == 'score' else 999
            with self.subTest(defect=defect), self.assertRaises(PDFComparisonError):
                self.compare_response(json.dumps(payload).encode())





class AcceptanceRunnerChecks(unittest.TestCase):
    def test_changed_raw_ocr_provenance_cannot_pass_cached_parity(self):
        pages = {'before': [page('The result used 23 trials.')], 'after': [page('The result used 99 trials.')]}
        baseline = engine.compare_pages(pages['before'], pages['after'])
        check_provenance(baseline, pages)
        for field, value in (('text', 'fabricated OCR'), ('score', 0.123), ('box', [0, 0, 1, 1])):
            result = copy.deepcopy(baseline)
            result['findings'][0]['after_ocr'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_provenance(result, pages)

    def test_gold_check_requires_affected_location_not_just_finding_count(self):
        case = {'gold_edits': [{'before': [], 'after': [{'page': 2, 'box': [100, 100, 200, 200]}]}]}
        for location in ({'page': 1, 'box': [100, 100, 200, 200]}, {'page': 2, 'box': [0, 0, 50, 50]}):
            result = {'findings': [{'before': [], 'after': [location]}]}
            self.assertFalse(gold_checks(case, result)[0]['after_covered'])
        result = {'findings': [{'before': [], 'after': [{'page': 2, 'box': [100, 100, 200, 200]}]}]}
        self.assertTrue(gold_checks(case, result)[0]['after_covered'])
