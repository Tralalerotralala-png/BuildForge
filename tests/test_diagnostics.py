import json
import unittest

from diagnostics import analyze, target_lines


class DiagnosticTests(unittest.TestCase):
    def test_slow_selection(self):
        events = [
            {'event': 'unit', 'name': 'fast.cpp', 'state': 'compiled', 'seconds': 10},
            {
                'event': 'progress',
                'done': 1,
                'total': 3,
                'active': 2,
                'active_units': [
                    {'name': 'slow.cpp', 'seconds': 1800, 'cpu_seconds': 1700},
                    {'name': 'quick.cpp', 'seconds': 20},
                ],
            },
        ]
        r = analyze(map(json.dumps, events), 900)
        self.assertEqual(target_lines(r), 'slow.cpp | 0 | timing')
        self.assertEqual(r['candidates'][0]['relative_to_median'], 180)

    def test_completed_removed(self):
        r = analyze(
            map(
                json.dumps,
                [
                    {'event': 'progress', 'active_units': [{'name': 'a.cpp', 'seconds': 1000}]},
                    {'event': 'unit', 'name': 'a.cpp', 'state': 'compiled'},
                ],
            )
        )
        self.assertEqual(r['candidates'], [])

    def test_invalid_names_and_o0(self):
        r = analyze(
            [
                'not JSON',
                json.dumps(
                    {
                        'event': 'progress',
                        'active_units': [
                            {'name': '../x.cpp', 'seconds': 1000},
                            {'name': 'good.cpp', 'seconds': 1000},
                        ],
                    }
                ),
            ],
            900,
            '0',
        )
        self.assertEqual(target_lines(r), 'good.cpp | same | timing')
        self.assertEqual(r['malformed_lines'], 1)

    def test_legacy_exact_tail_inference(self):
        e = [
            {'event': 'unit', 'name': 'a.cpp', 'state': 'cached'},
            {'event': 'progress', 'done': 1, 'total': 3, 'active': 2, 'queued': 0, 'seconds': 10},
            {'event': 'progress', 'done': 1, 'total': 3, 'active': 2, 'queued': 0, 'seconds': 1010},
        ]
        r = analyze(map(json.dumps, e), 900, expected_names=['a.cpp', 'b.cpp', 'c.cpp'])
        self.assertEqual(target_lines(r), 'b.cpp | 0 | timing\nc.cpp | 0 | timing')
        self.assertIn('lower bound', r['candidates'][0]['evidence'])
        self.assertEqual(analyze(map(json.dumps, e), 900)['candidates'], [])

    def test_legacy_inference_refuses_inconsistent_count(self):
        e = [
            {'event': 'unit', 'name': 'a.cpp', 'state': 'cached'},
            {'event': 'progress', 'done': 2, 'total': 3, 'active': 1, 'queued': 0, 'seconds': 10},
            {'event': 'progress', 'done': 2, 'total': 3, 'active': 1, 'queued': 0, 'seconds': 1010},
        ]
        self.assertEqual(analyze(map(json.dumps, e), 900, expected_names=['a.cpp', 'b.cpp', 'c.cpp'])['candidates'], [])

    def test_legacy_inference_resets_on_new_session(self):
        e = [
            {'event': 'unit', 'name': 'a.cpp', 'state': 'cached'},
            {'event': 'progress', 'done': 1, 'total': 2, 'active': 1, 'queued': 0, 'seconds': 10},
            {'event': 'phase', 'message': 'Checking toolchain and hashing headers'},
            {'event': 'progress', 'done': 1, 'total': 2, 'active': 1, 'queued': 0, 'seconds': 1010},
        ]
        self.assertEqual(analyze(map(json.dumps, e), 900, expected_names=['a.cpp', 'b.cpp'])['candidates'], [])

    def test_slow_then_failed_named_units_retained(self):
        events = [
            {'event': 'unit', 'name': 'fast.cpp', 'state': 'compiled', 'seconds': 12},
            {
                'event': 'progress',
                'done': 1,
                'total': 3,
                'active': 2,
                'queued': 0,
                'active_units': [
                    {'name': 'slow_a.cpp', 'seconds': 1800, 'cpu_seconds': 1750},
                    {'name': 'slow_b.cpp', 'seconds': 1700, 'cpu_seconds': 1650},
                ],
            },
            {'event': 'unit', 'name': 'slow_a.cpp', 'state': 'failed'},
            {
                'event': 'progress',
                'done': 2,
                'total': 3,
                'active': 1,
                'queued': 0,
                'active_units': [{'name': 'slow_b.cpp', 'seconds': 1750, 'cpu_seconds': 1700}],
            },
            {'event': 'unit', 'name': 'slow_b.cpp', 'state': 'failed'},
            {'event': 'progress', 'done': 3, 'total': 3, 'active': 0, 'queued': 0, 'active_units': []},
        ]
        r = analyze(map(json.dumps, events), 900)
        self.assertEqual(target_lines(r), 'slow_a.cpp | 0 | timing\nslow_b.cpp | 0 | timing')
        self.assertTrue(all(c['failed'] for c in r['candidates']))

    def test_slow_then_failed_legacy_tail_retained(self):
        expected = ['u0.cpp', 'u1.cpp', 'u2.cpp', 'u3.cpp', 's0.cpp', 's1.cpp', 's2.cpp']
        events = [
            {'event': 'unit', 'name': 'u0.cpp', 'state': 'cached'},
            {'event': 'unit', 'name': 'u1.cpp', 'state': 'cached'},
            {'event': 'unit', 'name': 'u2.cpp', 'state': 'cached'},
            {'event': 'unit', 'name': 'u3.cpp', 'state': 'compiled', 'seconds': 5},
            {'event': 'progress', 'done': 4, 'total': 7, 'active': 3, 'queued': 0, 'seconds': 10},
            {'event': 'progress', 'done': 4, 'total': 7, 'active': 3, 'queued': 0, 'seconds': 2461},
            {'event': 'unit', 'name': 's2.cpp', 'state': 'failed'},
            {'event': 'progress', 'done': 5, 'total': 7, 'active': 2, 'queued': 0, 'seconds': 2462},
            {'event': 'unit', 'name': 's0.cpp', 'state': 'failed'},
            {'event': 'progress', 'done': 6, 'total': 7, 'active': 1, 'queued': 0, 'seconds': 2464},
            {'event': 'unit', 'name': 's1.cpp', 'state': 'failed'},
            {'event': 'progress', 'done': 7, 'total': 7, 'active': 0, 'queued': 0, 'seconds': 2464},
        ]
        r = analyze(map(json.dumps, events), 900, expected_names=expected)
        self.assertEqual(target_lines(r), 's0.cpp | 0 | timing\ns1.cpp | 0 | timing\ns2.cpp | 0 | timing')
        self.assertEqual(r['candidates'][0]['elapsed_seconds'], 2451)
        self.assertTrue(all(c['failed'] for c in r['candidates']))

    def test_legacy_tail_partial_completion_excluded(self):
        events = [
            {'event': 'unit', 'name': 'a.cpp', 'state': 'cached'},
            {'event': 'progress', 'done': 1, 'total': 3, 'active': 2, 'queued': 0, 'seconds': 10},
            {'event': 'progress', 'done': 1, 'total': 3, 'active': 2, 'queued': 0, 'seconds': 1010},
            {'event': 'unit', 'name': 'b.cpp', 'state': 'compiled', 'seconds': 1012},
            {'event': 'unit', 'name': 'c.cpp', 'state': 'failed'},
        ]
        r = analyze(map(json.dumps, events), 900, expected_names=['a.cpp', 'b.cpp', 'c.cpp'])
        self.assertEqual(target_lines(r), 'c.cpp | 0 | timing')


if __name__ == '__main__':
    unittest.main()
