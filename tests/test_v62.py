import os
import sys
import time
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from execution_guard import guarded_prediction, entry_check
from unified_scoring import combined_assessment
from paper_trade import setup, report, observe, METHOD, SHADOW_METHOD

NOW = 1_800_000_000_000

def prediction(side='LONG'):
    return dict(signal=side, candidate_signal=side, confidence=.8, p_long=.8, p_short=.2,
        p_wait=None, candle_time=NOW-1_000_000, entry_reference=100., risk_atr_pct=.01,
        tp_atr=2.5, sl_atr=1.5, target_estimate=102.5, stop_estimate=98.5,
        model_version='test', technical={})

def book(age=1.):
    return dict(bid=99.99, ask=100.01, bid_qty=100., ask_qty=100., updated_at=NOW/1000-age)

def quote(price=100., age=1.):
    return dict(price=price, updated_at=NOW/1000-age)

class EntryTests(unittest.TestCase):
    def test_valid_never_promotes_unapproved_model(self):
        p=prediction()
        self.assertTrue(entry_check(p,quote(),NOW,book())['approved'])
        self.assertEqual(guarded_prediction(p,quote(),NOW,False,book=book())['signal'],'BEKLE')
        self.assertEqual(p['signal'],'LONG')

    def test_stale_future_missing_nonfinite_quotes(self):
        for q in [quote(age=10),quote(age=-1),{},quote(float('nan')),quote(float('inf'))]:
            with self.subTest(q=q):
                result=guarded_prediction(prediction(),q,NOW,True,book=book())
                u=combined_assessment(result)
                self.assertFalse(u['approved'])
                self.assertEqual(u['status'],'BEKLE')
                self.assertIsNone(u['tp_estimate'])
                self.assertTrue(u['reason'])

    def test_departure_and_expired_or_unclosed_candle(self):
        for changes,q in [({},quote(100.4)),({'candle_time':NOW-1_800_001},quote()),({'candle_time':NOW},quote())]:
            p=prediction();p.update(changes)
            self.assertFalse(entry_check(p,q,NOW,book())['approved'])

    def test_cost_and_live_drift_veto_both_sides(self):
        for side in ['LONG','SHORT']:
            p=prediction(side)
            self.assertEqual(guarded_prediction(p,quote(),NOW,True,True,book=book())['signal'],'BEKLE')
            p['risk_atr_pct']=.0001
            self.assertFalse(entry_check(p,quote(),NOW,book())['approved'])

    def test_rest_observation_cannot_receive_approved_blocked_signal(self):
        db=sqlite3.connect(':memory:');setup(db)
        p=guarded_prediction(prediction(),quote(101),NOW,True,book=book())
        self.assertFalse(observe(db,'BTCUSDT',p,101,NOW))
        self.assertEqual(report(db)['summary']['observed'],0)
        db.close()

class EvidenceTests(unittest.TestCase):
    def test_forward_metrics_exclude_shadow_legacy_open_and_gaps_from_wins(self):
        db=sqlite3.connect(':memory:');setup(db)
        samples=[(METHOD,'LONG','CLOSED','TP',.02),(METHOD,'LONG','CLOSED','SL',-.01),
                 (METHOD,'LONG','OPEN',None,None),(METHOD,'LONG','VERI_EKSIK',None,None),
                 (SHADOW_METHOD,'LONG','CLOSED','TP',.1),('legacy_close_bar_60m','LONG','CLOSED','TP',.1)]
        for i,(method,side,state,reason,net) in enumerate(samples):
            db.execute('INSERT INTO paper_observations (symbol,candle_time,model_version,signal,confidence,observed_at,entry_price,settlement_method,state,exit_reason,net_return) VALUES (?,?,?,?,?,?,?,?,?,?,?)',('BTCUSDT',i,'test',side,.8,NOW,100,method,state,reason,net))
        d=report(db)['directional']
        self.assertEqual(d['LONG']['observed'],4)
        self.assertEqual(d['LONG']['settled'],2)
        self.assertEqual(d['LONG']['tp_first_rate'],.5)
        self.assertAlmostEqual(d['LONG']['profit_factor'],2)
        self.assertLess(d['LONG']['tp_wilson_lower_95'],.5)
        self.assertEqual(d['LONG']['unverified'],1)
        self.assertIsNone(d['SHORT']['profit_factor'])
        db.close()

class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=tempfile.TemporaryDirectory()
        os.environ['DATA_DIR']=cls.data.name
        with patch.dict(os.environ, {k:'' for k in ('ALL_PROXY','HTTP_PROXY','HTTPS_PROXY','all_proxy','http_proxy','https_proxy')}):
            import app
        cls.app=app
    @classmethod
    def tearDownClass(cls):
        cls.app.db.close();cls.data.cleanup()

    def test_inference_queue_yields_to_network_between_predictions(self):
        import asyncio
        from contextlib import suppress
        a=self.app
        async def check():
            queue=asyncio.Queue();trace=[]
            for symbol in ('BTCUSDT','ETHUSDT'):queue.put_nowait(symbol)
            def infer(symbol,candles):
                trace.append(symbol);return {'candle_time':NOW,'model_version':'test'}
            async def network():trace.append('network')
            with patch.object(a,'closed_queue',queue),patch.object(a,'universe',{'BTCUSDT':{},'ETHUSDT':{}}),patch.object(a,'signals',{}),\
                 patch.object(a.coin_engine,'ready',return_value=True),patch.object(a.coin_engine,'infer',side_effect=infer),\
                 patch.object(a,'contiguous_recent',return_value=[{}]),patch.object(a,'freshness_ok',return_value=True),patch.object(a,'_save_signal'):
                worker=asyncio.create_task(a.stream_signal_worker());other=asyncio.create_task(network())
                await other
                await queue.join()
                worker.cancel()
                with suppress(asyncio.CancelledError):await worker
            self.assertLess(trace.index('network'),trace.index('ETHUSDT'))
        asyncio.run(check())

    def test_all_views_use_the_same_dynamic_entry_gate(self):
        import asyncio
        a=self.app;now=time.time();p=prediction();p['candle_time']=int(now*1000)-1_000_000
        a.universe.clear();a.signals.clear();a.tickers.clear()
        a.universe['BTCUSDT']={'base':'BTC','quote':'USDT'}
        a.signals['BTCUSDT']=p
        a.tickers['BTCUSDT']={'price':101,'updated_at':now-1,'quote_volume':100}
        a.books['BTCUSDT']=dict(book(),updated_at=now-1)
        a.status['tickers_updated']=now
        with patch.object(a.coin_engine,'version',return_value='test'),patch.object(a.coin_engine,'permissions',return_value={'LONG':{'approved':True}}),patch.object(a,'_live_symbols',return_value=[]),patch.object(a,'_current_drift',return_value=False):
            m=asyncio.run(a.markets(limit=20))
            detail=asyncio.run(a.symbol_detail('BTCUSDT'))
            opp=asyncio.run(a.opportunities(limit=12))
            self.assertEqual(m['rows'][0]['unified']['status'],'BEKLE')
            self.assertEqual(detail['unified']['status'],'BEKLE')
            self.assertEqual(opp['total'],0)
            self.assertEqual(a._unified_counts(now)['islem_sinyali'],0)
            self.assertEqual(p['signal'],'LONG')
            a.tickers['BTCUSDT']['price']=100
            self.assertEqual(asyncio.run(a.markets(limit=20))['rows'][0]['unified']['status'],'İŞLEM SİNYALİ')
            with patch.object(a,'_current_drift',return_value=True):
                self.assertEqual(asyncio.run(a.symbol_detail('BTCUSDT'))['unified']['status'],'BEKLE')

    def test_professional_report_and_rollback_auth(self):
        import asyncio
        import json
        from fastapi import HTTPException
        a=self.app
        report=asyncio.run(a.professional_report())
        self.assertEqual(report['release'],'V7.2')
        self.assertEqual(report['calibration']['resolved_within_24h'],0)
        json.dumps(report,allow_nan=False)
        with self.assertRaises(HTTPException) as exc:
            asyncio.run(a.rollback('BTCUSDT','bad',None))
        self.assertEqual(exc.exception.status_code,403)

if __name__=='__main__':unittest.main()
