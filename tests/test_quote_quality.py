import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from quote_quality import quote_quality, quote_coverage
from execution_guard import entry_check
from test_v62 import quote, book, prediction, NOW


class QuoteQualityTests(unittest.TestCase):
    def test_execution_boundary_is_unchanged(self):
        for age, expected in [(0,'fresh'),(9.999,'fresh'),(10,'stale'),(119,'stale'),(121,'stale'),(-.001,'future')]:
            q=quote_quality(quote(age=age),NOW/1000)
            self.assertEqual(q['status'],expected)
            check=entry_check(prediction(),quote(age=age),NOW,book())
            self.assertEqual(check['quote_quality'],q)
            self.assertEqual(next(x['passed'] for x in check['checks'] if x['id']=='quote'),expected=='fresh')

    def test_missing_corrupt_and_future_are_not_stale(self):
        for value, expected in [(None,'missing'),({},'missing'),({'price':1},'invalid'),
                                (quote(age=-1),'future'),(dict(quote(),updated_at=float('nan')),'invalid'),
                                (dict(quote(),price=float('inf')),'invalid')]:
            check=entry_check(prediction(),value,NOW,book())
            self.assertEqual(check['quote_quality']['status'],expected)
            self.assertFalse(check['approved'])
            json.dumps(check,allow_nan=False)

    def test_coverage_uses_same_ten_second_limit(self):
        values={'fresh':quote(age=1),'stale':quote(age=15),'old':quote(age=150),'future':quote(age=-1)}
        result=quote_coverage([*values,'missing'],values,NOW/1000)
        self.assertEqual(result['fresh'],1)
        self.assertEqual(result['stale'],2)
        self.assertEqual(result['missing'],['missing'])
        self.assertEqual(result['invalid'],['future'])

    def test_no_retimestamping_or_sticky_failure(self):
        value=quote(age=20);original=dict(value)
        self.assertFalse(quote_quality(value,NOW/1000)['fresh'])
        self.assertEqual(value,original)
        self.assertTrue(quote_quality(quote(age=1),NOW/1000)['fresh'])
        stale=entry_check(prediction(),value,NOW,book())
        future=entry_check(prediction(),quote(age=-1),NOW,book())
        self.assertNotEqual(stale['checks'][0]['label'],future['checks'][0]['label'])
