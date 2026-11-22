import sqlite3

from exchange.issue import issue_totals
from handling.operations import load_operation_log, record_operation
from handling.segments import get_segment, open_segment
from intake.batches import receive_batch
from inventory.close import close_from_paper, close_inventory
from inventory.readback import read_inventory, read_paper_evidence
from paper_examples import fail_seal_write, prepare_case
from tests import WorkdayCase


class PaperTests(WorkdayCase):
    def _close(self, name):
        args = prepare_case(self.source, name)
        return args, close_from_paper(self.source, **args)

    def _counts(self):
        return tuple(self.source.execute(statement).fetchone()[0] for statement in (
            'SELECT COUNT(*) FROM operations',
            'SELECT COUNT(*) FROM raw_uses',
            'SELECT COUNT(*) FROM inventories',
            'SELECT COUNT(*) FROM paper_evidence',
        ))

    def test_paper_ambiguous_leaves_state(self):
        args, result = self._close('ambiguous')
        self.assertEqual(result.status, 'ambiguous')
        self.assertIsNone(result.snapshot)
        self.assertIsNone(result.evidence)
        self.assertIsNone(read_inventory(self.source, args['inventory_id']))
        self.assertIsNone(read_paper_evidence(self.source, args['inventory_id']))
        self.assertTrue(get_segment(self.source, args['segment_id']).is_open)
        self.assertEqual(load_operation_log(self.source, args['segment_id']).operations, ())

    def test_paper_unique_evidence_and_issue(self):
        args, result = self._close('unique')
        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.snapshot.bags, (('M', 6), ('N', 6)))
        evidence = read_paper_evidence(self.source, args['inventory_id'])
        self.assertEqual(evidence, result.evidence)
        self.assertEqual(evidence.paper_id, args['paper_id'])
        self.assertEqual([entry.line_no for entry in evidence.rows], [4, 9])
        by_line = {entry.line_no: entry.operation for entry in evidence.rows}
        self.assertEqual(tuple(sorted(by_line[4].inputs)), ('s', 'u'))
        self.assertEqual(tuple(sorted(by_line[9].inputs)), ('q', 'v'))
        self.assertEqual(result.snapshot, read_inventory(self.source, args['inventory_id']))
        self.assertFalse(get_segment(self.source, args['segment_id']).is_open)
        piece = issue_totals(self.source, args['inventory_id'])
        self.assertEqual(piece.bags, (('M', 6), ('N', 6)))

    def test_paper_rerun_after_ambiguous_then_unique(self):
        args = prepare_case(self.source, 'ambiguous')
        self.assertEqual(close_from_paper(self.source, **args).status, 'ambiguous')
        # s is still free, so pinning N to q would force M onto s even before the rerun.
        narrowed = [dict(args['rows'][0]), dict(args['rows'][1], input_candidates=[['q'], ['v']])]
        second = close_from_paper(self.source, **dict(args, rows=narrowed))
        self.assertEqual(second.status, 'unique')
        self.assertEqual(second.snapshot.bags, (('M', 6), ('N', 6)))

    def test_paper_reverse_later_line_production(self):
        args, result = self._close('reverse')
        self.assertEqual(result.status, 'unique')
        self.assertEqual(
            result.snapshot.bags,
            (('M', 7), ('old-zero', 0), ('tail', 2), ('zero', 0)),
        )
        line2, line8 = result.evidence.rows
        self.assertEqual((line2.line_no, line8.line_no), (2, 8))
        self.assertEqual(line8.operation.sequence, 2)
        self.assertEqual(line2.operation.sequence, 3)
        self.assertEqual(tuple(sorted(line2.operation.inputs)), ('carried', 'mid'))

    def test_paper_reverse_missing_zero_bag_is_no_solution(self):
        args = prepare_case(self.source, 'reverse')
        for observations in (
            [('M', 7), ('tail', 2), ('zero', 0)],
            [('M', 7), ('old-zero', 0), ('tail', 2), ('zero', 0), ('phantom', 0)],
        ):
            with self.subTest(observations=observations):
                result = close_from_paper(self.source, **dict(args, observations=observations))
                self.assertEqual(result.status, 'no_solution')
                self.assertTrue(get_segment(self.source, args['segment_id']).is_open)
                self.assertIsNone(read_inventory(self.source, args['inventory_id']))

    def test_paper_cross_segment(self):
        _, result = self._close('cross-segment')
        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.snapshot.bags, (('M', 6),))
        self.assertEqual(tuple(sorted(result.evidence.rows[0].operation.inputs)), ('s', 'u'))

    def test_paper_cycle_has_no_legal_start(self):
        args = prepare_case(self.source, 'cycle')
        result = close_from_paper(self.source, **args)
        self.assertEqual(result.status, 'no_solution')
        self.assertIsNone(result.snapshot)
        self.assertIsNone(result.evidence)
        self.assertTrue(get_segment(self.source, args['segment_id']).is_open)
        self.assertEqual(load_operation_log(self.source, args['segment_id']).operations, ())

    def test_paper_empty_form_confirms_prefix(self):
        receive_batch(self.source, 'prefix-raw', 3, 'ticket-prefix')
        open_segment(self.source, 'day')
        record_operation(self.source, 'day', 'prefix', 'split', ['prefix-raw'],
                         [('carried', 3), ('prefix-zero', 0)])
        result = close_from_paper(
            self.source, 'day', 'inv-empty-paper', 'paper-empty', [],
            [('carried', 3), ('prefix-zero', 0)],
        )
        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.snapshot.bags, (('carried', 3), ('prefix-zero', 0)))
        self.assertEqual(result.evidence.paper_id, 'paper-empty')
        self.assertEqual(result.evidence.rows, ())

    def test_paper_invalid_rows(self):
        args = prepare_case(self.source, 'unique')
        rows = args['rows']
        invalid_rows = (
            'not a list',
            [dict(rows[0], line_no=0)],
            [dict(rows[0], line_no=True)],
            [dict(rows[0], line_no=1), dict(rows[1], line_no=1)],
            [dict(rows[0], operation_id='same'), dict(rows[1], operation_id='same')],
            [dict(rows[0], kind='pour')],
            [dict(rows[0], input_candidates=[])],
            [dict(rows[0], input_candidates=[[]])],
            [dict(rows[0], input_candidates=[['q', 's'], []])],
            [dict(rows[0], outputs=[])],
            [dict(rows[0], outputs=[('M', 6), ('M', 6)])],
            [{key: value for key, value in rows[0].items() if key != 'kind'}],
        )
        for broken in invalid_rows:
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    close_from_paper(self.source, **dict(args, rows=broken))
                self.assertTrue(get_segment(self.source, 'day').is_open)
                self.assertIsNone(read_inventory(self.source, args['inventory_id']))
        for observations in ([('M', 6), ('N', 6), ('M', 6)], [('M', 6), ('N', -6)],
                             [('M', 6), ('N', True)], [('M', 6), ('N',)]):
            with self.subTest(observations=observations):
                with self.assertRaises(ValueError):
                    close_from_paper(self.source, **dict(args, observations=observations))
        with self.assertRaises(LookupError):
            close_from_paper(self.source, **dict(args, segment_id='absent'))
        self.assertIsNone(read_inventory(self.source, args['inventory_id']))

    def test_paper_used_inventory_id_rejected(self):
        args = prepare_case(self.source, 'unique')
        first = close_from_paper(self.source, **args)
        self.assertEqual(first.status, 'unique')
        open_segment(self.source, 'other')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            close_from_paper(
                self.source, **dict(args, segment_id='other', inventory_id=args['inventory_id']),
            )
        self.assertEqual(read_paper_evidence(self.source, args['inventory_id']), first.evidence)

    def test_paper_closed_segment_rejected(self):
        args = prepare_case(self.source, 'unique')
        result = close_from_paper(self.source, **args)
        self.assertEqual(result.status, 'unique')
        with self.assertRaisesRegex(ValueError, 'closed'):
            close_from_paper(self.source, **dict(args, inventory_id='another'))
        self.assertIsNone(read_inventory(self.source, 'another'))

    def test_paper_write_failure_rolls_back_and_retries(self):
        args = prepare_case(self.source, 'cross-segment')
        before_ops = load_operation_log(self.source, 'day')
        before_counts = self._counts()
        with fail_seal_write(self.source):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'paper seal write failed'):
                close_from_paper(self.source, **args)
        self.assertEqual(self._counts(), before_counts)
        self.assertEqual(load_operation_log(self.source, 'day'), before_ops)
        self.assertIsNone(read_inventory(self.source, args['inventory_id']))
        self.assertIsNone(read_paper_evidence(self.source, args['inventory_id']))
        self.assertTrue(get_segment(self.source, 'day').is_open)
        result = close_from_paper(self.source, **args)
        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.snapshot.bags, (('M', 6),))

    def test_paper_reopen_readback(self):
        args, result = self._close('unique')
        piece = issue_totals(self.source, args['inventory_id'])
        self.reopen_source()
        self.assertEqual(read_inventory(self.source, args['inventory_id']), result.snapshot)
        self.assertEqual(read_paper_evidence(self.source, args['inventory_id']), result.evidence)
        self.assertEqual(issue_totals(self.source, args['inventory_id']), piece)

    def test_paper_evidence_none_for_plain_close(self):
        self.split_segment()
        close_inventory(self.source, 'day', [('bag-A', 6), ('bag-B', 4)])
        self.assertIsNone(read_paper_evidence(self.source, 'day'))
        self.assertIsNone(read_paper_evidence(self.source, 'missing'))

    def test_paper_duplicate_candidates_do_not_multiply(self):
        receive_batch(self.source, 'q', 3, 'ticket-q')
        receive_batch(self.source, 'u', 3, 'ticket-u')
        open_segment(self.source, 'day')
        rows = [{
            'line_no': 1, 'operation_id': 'mix-M', 'kind': 'mix',
            'input_candidates': [['q', 'q'], ['u', 'u']], 'outputs': [('M', 6)],
        }]
        result = close_from_paper(self.source, 'day', 'inv-dup', 'paper-dup', rows, [('M', 6)])
        self.assertEqual(result.status, 'unique')
        self.assertEqual(result.snapshot.bags, (('M', 6),))
        self.assertEqual(len(result.evidence.rows), 1)

    def test_paper_swapped_positions_are_one_reading(self):
        receive_batch(self.source, 'a', 3, 'ticket-a')
        receive_batch(self.source, 'b', 3, 'ticket-b')
        open_segment(self.source, 'day')
        rows = [{
            'line_no': 1, 'operation_id': 'mix-M', 'kind': 'mix',
            'input_candidates': [['b'], ['a']], 'outputs': [('M', 6)],
        }]
        result = close_from_paper(self.source, 'day', 'inv-swap', 'paper-swap', rows, [('M', 6)])
        self.assertEqual(result.status, 'unique')
        self.assertEqual(tuple(sorted(result.evidence.rows[0].operation.inputs)), ('a', 'b'))

    def test_paper_registration_order_can_be_non_greedy(self):
        receive_batch(self.source, 'a', 3, 'ticket-a')
        receive_batch(self.source, 'root', 4, 'ticket-root')
        open_segment(self.source, 'day')
        rows = [
            {'line_no': 1, 'operation_id': 'mix-Z', 'kind': 'mix',
             'input_candidates': [['P'], ['a']], 'outputs': [('M', 7)]},
            {'line_no': 2, 'operation_id': 'split-R', 'kind': 'split',
             'input_candidates': [['root']], 'outputs': [('P', 4), ('q', 0)]},
        ]
        result = close_from_paper(self.source, 'day', 'inv-order', 'paper-order', rows, [('M', 7), ('q', 0)])
        self.assertEqual(result.status, 'unique')
        first, second = result.evidence.rows
        self.assertEqual(first.line_no, 1)
        self.assertEqual(second.operation.sequence, 1)
        self.assertEqual(first.operation.sequence, 2)

    def test_paper_cross_segment_consumption_forces_unique(self):
        open_segment(self.source, 'side')
        for batch, grams in (('q', 3), ('s', 3), ('u', 3), ('v', 3)):
            receive_batch(self.source, batch, grams, f'ticket-{batch}')
        from handling.operations import record_operation
        record_operation(self.source, 'side', 'use-q', 'mix', ['q', 'v'], [('sv', 6)])
        open_segment(self.source, 'day')
        rows = [
            {'line_no': 4, 'operation_id': 'mix-M', 'kind': 'mix',
             'input_candidates': [['q', 's'], ['u']], 'outputs': [('M', 6)]},
            {'line_no': 9, 'operation_id': 'mix-N', 'kind': 'mix',
             'input_candidates': [['q', 's'], ['v']], 'outputs': [('N', 6)]},
        ]
        result = close_from_paper(self.source, 'day', 'inv-side', 'paper-side', rows, [('M', 6), ('N', 6)])
        self.assertEqual(result.status, 'no_solution')
