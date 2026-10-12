import json
import sqlite3
import unittest
from test_v62 import prediction, quote, book, NOW
from execution_guard import entry_check, guarded_prediction
from execution_policy import POLICY_VERSION
import execution_paper as paper


class EntryPolicyTests(unittest.TestCase):
    def market(self, price=100., notional=500.):
        bid, ask = price-.01, price+.01
        return dict(book(), bid=bid, ask=ask,
                    bid_qty=notional/bid, ask_qty=notional/ask)

    def test_sub_1000_book_accepts_same_size_as_paper_execution(self):
        for side in ('LONG', 'SHORT'):
            p=prediction(side);p['p_short']=.8
            for size in (100., 500., 999.):
                with self.subTest(side=side,size=size):
                    b=self.market(notional=size)
                    result=guarded_prediction(p,quote(),NOW,True,book=b)
                    self.assertEqual(result['signal'],side)
                    audit=result['execution_quality']
                    self.assertEqual(audit['assumptions']['min_top_notional_usdt'],paper.NOTIONAL)
                    db=sqlite3.connect(':memory:')
                    try:
                        paper.setup(db)
                        self.assertTrue(paper.observe(db,'BTCUSDT',result,b,NOW))
                    finally:db.close()

    def test_each_book_side_must_still_cover_sanity_size(self):
        for side in ('bid','ask'):
            b=self.market();b[side+'_qty']=99./b[side]
            check=entry_check(prediction(),quote(),NOW,b)
            self.assertFalse(check['approved'])
            self.assertIn('liquidity',[c['id'] for c in check['checks'] if not c['passed']])
            self.assertIn('100 USDT',' '.join(check['reasons']))

    def test_reference_tolerance_both_sides_and_price_directions(self):
        for side in ('LONG','SHORT'):
            for movement in (-.5,.5):
                p=prediction(side);p['p_short']=.8
                price=100.+movement
                result=guarded_prediction(p,quote(price),NOW,True,book=self.market(price))
                self.assertEqual(result['signal'],side)
                self.assertEqual(result['execution_quality']['reference_departure_limit_atr'],.75)
                self.assertFalse(guarded_prediction(p,quote(price),NOW,False,book=self.market(price))['execution_quality']['approved'])
                self.assertFalse(guarded_prediction(p,quote(price,age=10),NOW,True,book=self.market(price))['execution_quality']['approved'])

    def test_narrow_bracket_and_executable_ask_cannot_bypass_limit(self):
        p=prediction();p['sl_atr']=1.25
        check=entry_check(p,quote(100.63),NOW,self.market(100.63))
        self.assertFalse(check['approved']);self.assertEqual(check['reference_departure_limit_atr'],.625)
        p=prediction()
        # Last trade at the limit, but the buy ask is already past it.
        check=entry_check(p,quote(100.75),NOW,self.market(100.75))
        failed=[c['id'] for c in check['checks'] if not c['passed']]
        self.assertNotIn('departure',failed);self.assertIn('executable',failed)
        for move in (-.8,.8):
            self.assertFalse(entry_check(p,quote(100.+move),NOW,self.market(100.+move))['approved'])

    def test_invalid_risk_stale_book_and_stress_still_block(self):
        for changes,b in [({'sl_atr':float('nan')},self.market()),
                          ({'risk_atr_pct':0.},self.market()),
                          ({'p_long':.4},self.market()),
                          ({},dict(self.market(),updated_at=NOW/1000-12))]:
            p=prediction();p.update(changes)
            result=entry_check(p,quote(),NOW,b)
            self.assertFalse(result['approved'])
            json.dumps(result,allow_nan=False)

    def test_old_and_new_paper_policies_are_counted_separately(self):
        db=sqlite3.connect(':memory:')
        try:
            paper.setup(db)
            for symbol,policy in [('OLDUSDT','V7.2'),('NEWUSDT',POLICY_VERSION)]:
                p=prediction();p['execution_quality']={'approved':True,'policy_version':policy}
                self.assertTrue(paper.observe(db,symbol,p,self.market(),NOW))
            shadow=prediction();shadow['execution_quality']={'policy_version':POLICY_VERSION}
            self.assertTrue(paper.observe(db,'SHADOWUSDT',shadow,self.market(),NOW,shadow=True))
            db.execute("UPDATE executable_paper SET state='CLOSED',exit_reason='TP',net_return=.01 WHERE symbol='OLDUSDT'")
            r=paper.report(db)
            self.assertEqual(r['current_entry_policy'],POLICY_VERSION)
            self.assertEqual(r['entry_policy_summary']['V7.2']['closed'],1)
            self.assertEqual(r['entry_policy_summary'][POLICY_VERSION]['observed'],1)
            self.assertEqual(r['entry_policy_summary'][POLICY_VERSION]['closed'],0)
            self.assertEqual(r['summary']['observed'],2)
        finally:db.close()


if __name__=='__main__':unittest.main()
