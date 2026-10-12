import asyncio
import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import numpy as np
from test_v62 import prediction,book,quote,NOW
from test_v72 import synthetic_bars
from decision_policy import select_policy,decide
from coin_models import CoinModelRegistry,ENSEMBLE_POLICY
from execution_guard import entry_check,guarded_prediction
from unified_scoring import combined_assessment
from order_depth import parse_depth,execution_book,visible_fill,selected_symbols,MAX_SYMBOLS
import execution_paper as paper


class DepthTests(unittest.TestCase):
    def snapshot(self,bids=None,asks=None,age=.5):
        payload={'stream':'btcusdt@depth20@500ms','data':{'e':'depthUpdate','s':'BTCUSDT','st':1,
            'E':NOW-age*1000,'u':10,'b':bids or [[99.99,.2],[99.98,2.]],
            'a':asks or [[100.01,.2],[100.02,2.]]}}
        return parse_depth(payload,{'BTCUSDT'},NOW/1000).get('BTCUSDT')

    def test_small_best_level_full_depth_passes_guard_and_same_paper_entry(self):
        b=self.snapshot();self.assertLess(b['ask']*b['ask_qty'],100)
        effective=execution_book(b,NOW)
        self.assertGreater(effective['ask'],b['ask'])
        audit=entry_check(prediction(),quote(),NOW,b)
        self.assertTrue(audit['approved']);self.assertEqual(audit['liquidity_method'],'depth20_vwap')
        db=sqlite3.connect(':memory:')
        try:
            paper.setup(db);p=prediction();p['execution_quality']=audit
            self.assertTrue(paper.observe(db,'BTCUSDT',p,b,NOW))
            entry=db.execute('SELECT entry_price FROM executable_paper').fetchone()[0]
            self.assertAlmostEqual(entry,effective['ask']*(1+paper.SLIPPAGE_PER_LEG))
        finally:db.close()

    def test_visible_size_impact_stale_and_invalid_snapshots_do_not_pass(self):
        thin=self.snapshot(bids=[[99.99,.1],[99.98,.1]],asks=[[100.01,.1],[100.02,.1]])
        result=entry_check(prediction(),quote(),NOW,thin)
        self.assertFalse(result['approved'])
        self.assertIn('liquidity',[c['id'] for c in result['checks'] if not c['passed']])
        costly=self.snapshot(asks=[[100.01,.2],[100.1,2.]])
        result=entry_check(prediction(),quote(),NOW,costly)
        self.assertFalse(result['approved'])
        self.assertIn('depth_impact',[c['id'] for c in result['checks'] if not c['passed']])
        self.assertIsNone(self.snapshot(age=3.))
        self.assertIsNone(self.snapshot(age=-1.))
        self.assertIsNone(self.snapshot(bids=[[99.98,1],[99.99,1]]))
        self.assertIsNone(self.snapshot(asks=[[100.01,1],[100.01,1]]))
        self.assertIsNone(self.snapshot(asks=[[float('nan'),1]]))
        b=self.snapshot();b['updated_at']=NOW/1000-4
        self.assertFalse(entry_check(prediction(),quote(),NOW,b)['approved'])
        self.assertIsNone(paper.valid_book(costly,NOW))

    def test_fill_requires_full_quantity_and_caps_tracking_universe(self):
        self.assertIsNone(visible_fill([(100,1.)],quantity=1.1))
        self.assertAlmostEqual(visible_fill([(100,.5),(101,1.)],quantity=1.)['price'],100.5)
        universe=[f'C{i}USDT' for i in range(60)]
        quotes={s:{'quote_volume':60-i} for i,s in enumerate(universe)}
        signals={'C58USDT':{'candidate_signal':'LONG'},'NOTLISTED':{'candidate_signal':'LONG'}}
        selected=selected_symbols(universe,quotes,signals,['C59USDT'])
        self.assertEqual(selected[:2],['C59USDT','C58USDT'])
        self.assertEqual(len(selected),MAX_SYMBOLS);self.assertNotIn('NOTLISTED',selected)

    def test_short_entry_uses_sell_vwap_and_exit_walks_actual_position(self):
        db=sqlite3.connect(':memory:')
        try:
            paper.setup(db);p=prediction('SHORT');p['p_short']=.8
            p['execution_quality']=entry_check(p,quote(),NOW,self.snapshot())
            self.assertTrue(paper.observe(db,'BTCUSDT',p,self.snapshot(),NOW))
            entry,target=db.execute('SELECT entry_price,target_price FROM executable_paper').fetchone()
            self.assertLess(entry,99.99)
            # Construct the independently timed snapshot at the later event.
            exit_book=dict(self.snapshot(),bid=target-.1,ask=target-.09,
                bids=[[target-.1,.2],[target-.11,2]],asks=[[target-.09,.2],[target-.08,2]],updated_at=(NOW+5000)/1000)
            self.assertEqual(paper.reconcile_books(db,{'BTCUSDT':exit_book},NOW+5500),1)
            state,price=db.execute('SELECT state,result_price FROM executable_paper').fetchone()
            self.assertEqual(state,'AWAITING_FUNDING');self.assertIsNotNone(price)
        finally:db.close()


class PipelineTests(unittest.TestCase):
    def test_long_short_thresholds_are_selected_independently(self):
        n=1200;times=np.arange(n)*900000
        l=np.r_[np.full(600,.6),np.full(600,.2)]
        s=np.r_[np.full(600,.2),np.full(600,.75)]
        outcomes=np.tile(np.where(np.arange(n)%10<9,1,-1)[:,None],(1,2))
        plans={side:{'tp_atr':2.5,'sl_atr':1.5} for side in ('LONG','SHORT')}
        policy=select_policy(l,s,outcomes,np.full(n,.012),times,plans)
        self.assertEqual(policy['thresholds'],{'LONG':.6,'SHORT':.75})
        self.assertEqual(decide(.6,.2,policy)[0],'LONG')
        self.assertEqual(decide(.2,.6,policy)[0],'BEKLE')
        self.assertEqual(decide(.2,.75,policy)[0],'SHORT')
        self.assertFalse(policy['holdout_used_for_selection'])

    def test_member_votes_use_member_calibrations_and_legacy_is_compatible(self):
        class Fixed:
            classes_=np.array([0,1])
            def predict_proba(self,x):return np.tile([.2,.8],(len(x),1))
        model={'kind':ENSEMBLE_POLICY,'members':[{'name':n,'model':Fixed()} for n in ('A','B','C')], 'weights':[1/3]*3}
        legacy={'validation_prior':.4,'alpha':1.}
        raw,agreement,details=CoinModelRegistry.vote_details(model,legacy,np.zeros((1,2)))
        self.assertTrue(agreement[0])
        current=dict(legacy,members={n:dict(legacy,alpha=0. if n=='A' else 1.) for n in ('A','B','C')})
        raw,agreement,details=CoinModelRegistry.vote_details(model,current,np.zeros((1,2)))
        self.assertAlmostEqual(details[0]['members'][0]['tp_score'],.4)
        self.assertFalse(agreement[0]);self.assertAlmostEqual(raw[0],.8)

    def test_entry_failure_does_not_erase_unapproved_technical_watch(self):
        p=prediction();p.update(signal='BEKLE',candidate_signal=None,confidence=None,p_long=.6)
        p['technical']={'direction':'LONG','score':12.5,'timeframes':{'1h':'YUKARI','4h':'YUKARI'},'patterns':[]}
        result=guarded_prediction(p,quote(),NOW,False,book=dict(book(),bid_qty=.1,ask_qty=.1))
        self.assertFalse(result['execution_quality']['approved'])
        unified=combined_assessment(result)
        self.assertEqual(unified['status'],'TAKİP ET');self.assertFalse(unified['approved'])
        p.update(candidate_signal='LONG')
        self.assertEqual(combined_assessment(guarded_prediction(p,quote(),NOW,False,book=book()))['status'],'BEKLE')

    def test_successful_budget_limited_backfill_continues_without_daily_lock(self):
        bars=synthetic_bars(9000)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ,dict(DATA_DIR=tmp,**{k:'' for k in ('ALL_PROXY','HTTP_PROXY','HTTPS_PROXY','all_proxy','http_proxy','https_proxy')})):
                import app
            path=Path(tmp)/'BTCUSDT.json'
            path.write_text(json.dumps({'symbol':'BTCUSDT','fetched_at':int(__import__('time').time()),
                'older_backfill_attempted_at':int(__import__('time').time()),'older_backfill_target_count':18000,
                'candles':bars[-1500:]}))
            fetch=AsyncMock(side_effect=[bars[-3000:-1500],bars[-4500:-3000],bars[-6000:-4500],bars[-7500:-6000]])
            with patch.object(app,'TRAINING_CACHE',Path(tmp)),patch.object(app.client,'klines',fetch):
                result=asyncio.run(app._training_bars('BTCUSDT'))
            self.assertEqual(len(result),7500);self.assertEqual(fetch.await_count,4)
            self.assertFalse(json.loads(path.read_text())['older_backfill_exhausted'])


if __name__=='__main__':unittest.main()
