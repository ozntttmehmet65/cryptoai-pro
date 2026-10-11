import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_v62 import prediction, quote, book, NOW
from execution_guard import entry_check, guarded_prediction
from market_stream import parse_books, parse_tickers, merge_ordered
from paper_trade import setup, observe, report, METHOD
from quality_guard import live_drift_block
from holdout_audit import audit
from coin_models import CoinModelRegistry, MODEL_FAMILY


class MarketTests(unittest.TestCase):
    def payload(self):
        return {'e':'bookTicker','s':'BTCUSDT','st':1,'b':'99.99','a':'100.01',
                'B':'100','A':'100','E':NOW-1000,'u':5}

    def test_raw_combined_and_array_book_payloads(self):
        for payload in [self.payload(),{'data':self.payload()},[self.payload()],json.dumps(self.payload())]:
            self.assertEqual(parse_books(payload,{'BTCUSDT'},NOW/1000)['BTCUSDT']['bid'],99.99)

    def test_bad_book_never_becomes_live(self):
        for changes in [{'st':2},{'a':'99'},{'B':'0'},{'b':'nan'},{'E':NOW+1},{'E':NOW-30000},{'s':'OTHER'}]:
            self.assertFalse(parse_books(dict(self.payload(),**changes),{'BTCUSDT'},NOW/1000))
        self.assertFalse(parse_books('invalid',{'BTCUSDT'}))

    def test_replay_cannot_refresh_quotes(self):
        current={'BTCUSDT':book()}
        self.assertFalse(merge_ordered(current,{'BTCUSDT':dict(book(),bid=1)}))
        self.assertEqual(current['BTCUSDT']['bid'],99.99)
        current['BTCUSDT']['update_id']=8
        self.assertFalse(merge_ordered(current,{'BTCUSDT':dict(book(age=0),update_id=7)}))

    def test_ticker_requires_real_event_timestamp(self):
        p={'s':'BTCUSDT','c':'100','P':'1','q':'200'}
        self.assertFalse(parse_tickers([p],{'BTCUSDT'},NOW/1000))
        self.assertFalse(parse_tickers([dict(p,E=NOW+1)],{'BTCUSDT'},NOW/1000))


class PolicyTests(unittest.TestCase):
    def test_valid_both_directions_and_veto_never_promotes(self):
        for side in ('LONG','SHORT'):
            p=prediction(side);p['p_short']=.8 if side=='SHORT' else .2
            self.assertTrue(entry_check(p,quote(),NOW,book())['approved'])
            self.assertEqual(guarded_prediction(p,quote(),NOW,False,book=book())['signal'],'BEKLE')

    def test_book_gap_spread_depth_and_stress_veto(self):
        for b in [None,book(12),book(-1),dict(book(),bid=99.9,ask=100.1),dict(book(),bid_qty=1),dict(book(),ask=float('nan'))]:
            self.assertFalse(entry_check(prediction(),quote(),NOW,b)['approved'])
        p=prediction();p.update(p_long=.4)
        self.assertFalse(entry_check(p,quote(),NOW,book())['approved'])

    def test_model_veto_is_visible_when_inference_already_waits(self):
        p=prediction();p['signal']='BEKLE'
        check=guarded_prediction(p,quote(),NOW,False,book=book())['execution_quality']
        self.assertFalse(check['approved'])
        self.assertIn('model',[c['id'] for c in check['checks']])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');setup(self.db)
    def tearDown(self):self.db.close()

    def test_shadow_does_not_block_approved_forward_observation(self):
        p=prediction();p['execution_quality']={'policy_version':'V7.0'}
        self.assertTrue(observe(self.db,'BTCUSDT',p,100,NOW,shadow=True))
        self.assertTrue(observe(self.db,'BTCUSDT',p,100,NOW))
        self.assertFalse(observe(self.db,'BTCUSDT',p,100,NOW+1))
        r=report(self.db)
        self.assertEqual(r['summary']['observed'],1)
        self.assertEqual(r['summary']['shadow_observations'],1)
        self.assertEqual(r['entry_policy_summary']['V7.0']['observed'],1)

    def test_bad_coin_model_not_hidden_by_other_winners(self):
        for i in range(35):
            bad=i<15
            self.db.execute('INSERT INTO paper_observations(symbol,candle_time,model_version,signal,confidence,observed_at,entry_price,state,settlement_method,net_return,result_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('BADUSDT' if bad else 'GOODUSDT',i,'v1','LONG',.8,NOW,100,'CLOSED',METHOD,-.01 if bad else .02,NOW-1000+i))
        self.assertFalse(live_drift_block(self.db,'LONG',NOW)[0])
        self.assertTrue(live_drift_block(self.db,'LONG',NOW,'BADUSDT','v1')[0])
        self.assertFalse(live_drift_block(self.db,'LONG',NOW,'BADUSDT','v2')[0])

    def test_unresolved_independent_candidate_not_cherry_picked(self):
        a=audit([1,1,1],[0,1,-1],[.01]*3,[0,900000,86400000],1,2.5,1.5,[.8]*3)
        self.assertEqual(a['independent_24h']['signals'],2)
        self.assertEqual(a['independent_24h']['resolved'],1)
        self.assertEqual(a['independent_24h']['tp'],0)
        self.assertFalse(a['used_for_tuning'])

    def test_new_artifact_metadata_mismatch_rejected_old_artifact_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=CoinModelRegistry(tmp)
            r.metadata['BTCUSDT']={'version':'new','artifact_schema':2,'model_family':MODEL_FAMILY}
            with patch.object(r,'ready',return_value=True),patch('coin_models.joblib.load',return_value={'version':'old','model_family':MODEL_FAMILY}):
                self.assertIsNone(r._load('BTCUSDT'))
            r.metadata['BTCUSDT'].pop('artifact_schema')
            with patch.object(r,'ready',return_value=True),patch('coin_models.joblib.load',return_value={'models':{},'calibration':{}}):
                self.assertIsNotNone(r._load('BTCUSDT'))


if __name__=='__main__':unittest.main()
