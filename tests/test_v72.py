import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch,AsyncMock
import numpy as np
from test_v62 import prediction,book,NOW,quote
import execution_paper as paper
import quality_report
from professional import professional_audit,market_regimes,correlation_groups,POLICY
from coin_models import CoinModelRegistry
from ml_engine import features,COLS
from execution_guard import guarded_prediction
from quality_guard import live_drift_block


def synthetic_bars(n=9000):
    end=int(time.time()*1000)//900000*900000
    rng=np.random.default_rng(42);closes=100*np.exp(np.cumsum(rng.normal(0,.005,n)))
    rows=[]
    for i,c in enumerate(closes):
        op=float(closes[i-1]) if i else 100.;t=end-(n-i)*900000
        rows.append(dict(time=t,close_time=t+899999,open=op,close=float(c),
            high=max(op,float(c))*(1+float(rng.uniform(.001,.006))),
            low=min(op,float(c))*(1-float(rng.uniform(.001,.006))),volume=float(rng.uniform(1000,10000))))
    return rows


class ExecutableTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');paper.setup(self.db);quality_report.setup(self.db)
    def tearDown(self):self.db.close()
    def enter(self,side='LONG',shadow=False):
        p=prediction(side);p['p_short']=.8 if side=='SHORT' else .2
        p['execution_quality']={'approved':not shadow,'policy_version':'V7.2'}
        self.assertTrue(paper.observe(self.db,'BTCUSDT',p,book(),NOW,shadow=shadow))
        return p
    def touch(self,side='LONG'):
        target=self.db.execute("SELECT target_price FROM executable_paper WHERE shadow=0").fetchone()[0]
        b=dict(book(),bid=target+.5 if side=='LONG' else target-.51,
               ask=target+.51 if side=='LONG' else target-.5,updated_at=(NOW+5000)/1000)
        self.assertEqual(paper.reconcile_books(self.db,{'BTCUSDT':b},NOW+5500),1)
    def test_entry_uses_executable_side_and_adverse_slippage(self):
        self.enter('LONG');entry=self.db.execute('SELECT entry_price FROM executable_paper').fetchone()[0]
        self.assertAlmostEqual(entry,100.01*1.0002)
        self.assertFalse(paper.observe(self.db,'BTCUSDT',prediction(),book(),NOW))
    def test_no_net_result_until_funding_coverage_and_correct_long_cost(self):
        self.enter();self.touch()
        r=self.db.execute('SELECT state,entry_price,result_price,net_return FROM executable_paper').fetchone()
        self.assertEqual(r[0],'AWAITING_FUNDING');self.assertIsNone(r[3])
        e={'symbol':'BTCUSDT','fundingTime':NOW+2500,'fundingRate':'.001','markPrice':'102','rateType':'Regular'}
        self.assertTrue(paper.apply_funding(self.db,'BTCUSDT',NOW+1,NOW+5000,[e],NOW+10000))
        gross,fees,funding,net=self.db.execute('SELECT gross_return,fee_return,funding_return,net_return FROM executable_paper').fetchone()
        self.assertAlmostEqual(funding,-.001*102/r[1])
        self.assertAlmostEqual(fees,.0006*(1+r[2]/r[1]))
        self.assertAlmostEqual(net,gross-fees+funding)
        self.assertEqual(paper.report(self.db)['summary']['settled'],1)
    def test_short_receives_positive_funding_and_uses_ask_exit(self):
        self.enter('SHORT');self.touch('SHORT')
        e={'symbol':'BTCUSDT','fundingTime':NOW+2500,'fundingRate':'.001','markPrice':'98'}
        paper.apply_funding(self.db,'BTCUSDT',NOW+1,NOW+5000,[e],NOW+10000)
        self.assertGreater(self.db.execute('SELECT funding_return FROM executable_paper').fetchone()[0],0)
    def test_partial_coverage_does_not_bridge_gap_or_duplicate_funding(self):
        self.enter();self.touch()
        e={'symbol':'BTCUSDT','fundingTime':NOW+1000,'fundingRate':'.001','markPrice':'101'}
        paper.apply_funding(self.db,'BTCUSDT',NOW+1,NOW+2000,[e],NOW+10000)
        self.assertEqual(self.db.execute('SELECT state FROM executable_paper').fetchone()[0],'AWAITING_FUNDING')
        paper.apply_funding(self.db,'BTCUSDT',NOW+4000,NOW+5000,[],NOW+10000)
        self.assertEqual(self.db.execute('SELECT state FROM executable_paper').fetchone()[0],'AWAITING_FUNDING')
        paper.apply_funding(self.db,'BTCUSDT',NOW+1,NOW+5000,[e],NOW+10000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM paper_funding').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT state FROM executable_paper').fetchone()[0],'CLOSED')
    def test_missing_mark_price_bad_symbol_full_page_or_future_not_zero_cost(self):
        self.enter();self.touch()
        good={'symbol':'BTCUSDT','fundingTime':NOW+2500,'fundingRate':'.001','markPrice':'102'}
        for events in [[dict(good,markPrice='nan')],[dict(good,symbol='ETHUSDT')],[{k:v for k,v in good.items() if k!='markPrice'}],[good]*1000]:
            self.assertFalse(paper.apply_funding(self.db,'BTCUSDT',NOW+1,NOW+5000,events,NOW+10000))
        self.assertFalse(paper.apply_funding(self.db,'BTCUSDT',NOW+1,NOW+20000,[],NOW+10000))
        self.assertIsNone(self.db.execute('SELECT net_return FROM executable_paper').fetchone()[0])
    def test_book_replay_gap_and_exit_size_cannot_create_fake_tp(self):
        self.enter();self.assertEqual(paper.reconcile_books(self.db,{'BTCUSDT':book()},NOW),0)
        paper.reconcile_books(self.db,{'BTCUSDT':dict(book(),bid=105,ask=105.01,updated_at=(NOW+180001)/1000)},NOW+180002)
        self.assertEqual(self.db.execute('SELECT state FROM executable_paper').fetchone()[0],'VERI_EKSIK')
        self.assertIsNone(self.db.execute('SELECT net_return FROM executable_paper').fetchone()[0])
    def test_exit_without_enough_displayed_size_is_unverified(self):
        self.enter()
        b=dict(book(),bid=105,ask=105.01,bid_qty=.01,updated_at=(NOW+5000)/1000)
        paper.reconcile_books(self.db,{'BTCUSDT':b},NOW+5500)
        state,reason,net=self.db.execute('SELECT state,exit_reason,net_return FROM executable_paper').fetchone()
        self.assertEqual((state,reason),('VERI_EKSIK','EXIT_SIZE_UNKNOWN'));self.assertIsNone(net)
    def test_shadow_never_counts_as_approved_or_masks_bad_drift(self):
        self.enter(shadow=True);self.enter()
        self.assertEqual(paper.report(self.db)['summary']['observed'],1)
        self.assertEqual(paper.report(self.db)['summary']['shadow_observations'],1)
        for i in range(15):
            self.db.execute('INSERT INTO executable_paper(symbol,candle_time,model_version,signal,confidence,observed_at,entry_price,stop_price,target_price,state,last_quote_ms,result_at,net_return,funding_covered_until,fee_rate,slippage_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('BADUSDT',i,'v72','LONG',.8,NOW,100,99,102,'CLOSED',NOW,NOW,-.01,NOW,.0006,.0002))
        self.assertTrue(live_drift_block(self.db,'LONG',NOW,'BADUSDT','v72')[0])
    def test_forward_calibration_excludes_late_closes_shadow_and_unknown(self):
        self.enter();self.touch()
        c=quality_report.calibration(self.db,NOW+10000)
        self.assertEqual(c['resolved_within_24h'],1)
        self.db.execute("UPDATE executable_paper SET result_at=?,state='CLOSED',net_return=.01",(NOW+86400001,))
        c=quality_report.calibration(self.db,NOW+86400002)
        self.assertEqual(c['resolved_within_24h'],0);self.assertEqual(c['censored_24h'],1)
    def test_daily_decision_identity_and_istanbul_day(self):
        p=prediction();p['execution_quality']={'approved':False,'reasons':['x','x']}
        quality_report.record(self.db,'BTCUSDT',p,NOW)
        p['signal']='BEKLE';quality_report.record(self.db,'BTCUSDT',p,NOW+1)
        d=quality_report.snapshot(self.db,NOW+1000)
        self.assertEqual(d['decisions'],1);self.assertEqual(d['blocked_candidates'],1)
        self.assertEqual(d['veto_reasons'][0]['count'],1)
        self.assertEqual(d['timezone'],'Europe/Istanbul')
        json.dumps(quality_report.report(self.db,NOW+1000),allow_nan=False)
    def test_midnight_snapshot_refreshes_previous_day_after_late_funding(self):
        midnight=int(datetime(2026,10,11,21,tzinfo=timezone.utc).timestamp()*1000)
        p=prediction();p['execution_quality']={'approved':False,'reasons':['late-day']}
        quality_report.record(self.db,'BTCUSDT',p,midnight-1000)
        self.enter();self.touch()
        self.db.execute('UPDATE executable_paper SET observed_at=?,result_at=?,funding_covered_until=?',
                        (midnight-10000,midnight-500,midnight-10000))
        quality_report.snapshot(self.db,midnight+1000)
        row=self.db.execute("SELECT report FROM daily_quality WHERE day='2026-10-11'").fetchone()
        d=json.loads(row[0]);self.assertEqual(d['decisions'],1);self.assertEqual(d['funding_pending_exits_today'],1)
        paper.apply_funding(self.db,'BTCUSDT',midnight-9999,midnight-500,[],midnight+2000)
        quality_report.snapshot(self.db,midnight+3000)
        d=json.loads(self.db.execute("SELECT report FROM daily_quality WHERE day='2026-10-11'").fetchone()[0])
        self.assertEqual(d['closed_today'],1);self.assertEqual(d['funding_pending_exits_today'],0)
        self.assertEqual(quality_report.daily(self.db,midnight+3000)['decisions'],0)


class EvidenceTests(unittest.TestCase):
    def test_many_overlapping_signals_do_not_replace_independent_events(self):
        a=professional_audit([1]*100,[1]*100,[.01]*100,np.arange(100)*900000,1,2.5,1.5,['RANGE']*100,18000)
        self.assertLess(a['independent_resolved'],12);self.assertFalse(a['passed'])
    def test_frozen_family_screen_and_three_periods_can_pass_strong_fixture(self):
        a=professional_audit([1]*30,[1]*30,[.01]*30,np.arange(30)*86400000,1,2.5,1.5,['RANGE']*30,18000)
        self.assertTrue(a['passed']);self.assertEqual(a['family_tests'],1400)
        self.assertTrue(all(s['independent_resolved']==10 for s in a['chronological_periods']))
        self.assertFalse(a['regimes']['EXTREME']['allowed'])
    def test_numpy_training_outcomes_with_selected_candidates_are_json_safe(self):
        a=professional_audit(np.ones(30,dtype=int),np.ones(30,dtype=int),
            np.full(30,.01),np.arange(30)*86400000,1,2.5,1.5,np.full(30,'RANGE'),18000)
        self.assertTrue(a['passed']);json.dumps(a,allow_nan=False)
    def test_unresolved_spacing_and_negative_period_not_cherry_picked(self):
        a=professional_audit([1]*30,[1]*20+[-1]*10,[.01]*30,np.arange(30)*86400000,1,2.5,1.5,['RANGE']*30,18000)
        self.assertFalse(a['checks']['period_consistency'])
        b=professional_audit([1,1,1],[0,1,-1],[.01]*3,[0,900000,86400000],1,2.5,1.5,['RANGE']*3,18000)
        self.assertEqual(b['independent_candidates'],2);self.assertEqual(b['independent_tp'],0)
    def test_extreme_or_unproven_regime_vetoes_otherwise_valid_entry(self):
        p=prediction();p['market_condition']={'name':'EXTREME'};p['regime_evidence']={'allowed':True}
        self.assertEqual(guarded_prediction(p,quote(),NOW,True,book=book())['signal'],'BEKLE')
        p['market_condition']['name']='RANGE';p['regime_evidence']['allowed']=False
        self.assertFalse(guarded_prediction(p,quote(),NOW,True,book=book())['execution_quality']['approved'])
    def test_exposure_signs_and_missing_timestamps(self):
        returns=np.sin(np.arange(100))*.01;up=100*np.exp(np.cumsum(returns));down=100*np.exp(-np.cumsum(returns))
        histories={s:[{'time':i*900000,'close':float(x)} for i,x in enumerate(cs)] for s,cs in [('AUSDT',up),('BUSDT',down)]}
        self.assertFalse(correlation_groups(histories,{'AUSDT':'LONG','BUSDT':'LONG'})['groups'])
        self.assertEqual(len(correlation_groups(histories,{'AUSDT':'LONG','BUSDT':'SHORT'})['groups']),1)
        histories['BUSDT']=histories['BUSDT'][:20]
        self.assertEqual(correlation_groups(histories,{'AUSDT':'LONG','BUSDT':'SHORT'})['unknown_pairs'],1)
    def test_future_mutations_do_not_change_past_58_features_or_regimes(self):
        bars=synthetic_bars(600);before=features(bars[:480]);changed=[dict(b) for b in bars]
        for b in changed[480:]:
            for k in ('open','high','low','close'):b[k]*=10
            b['volume']*=20
        after=features(changed);common=after[after['time'].isin(before['time'])]
        self.assertTrue(np.allclose(before[COLS].to_numpy(),common[COLS].to_numpy(),rtol=1e-10,atol=1e-12))
        self.assertEqual(list(market_regimes(before)),list(market_regimes(common)))
    def test_fit_reload_version_archive_and_incumbent_preservation(self):
        bars=synthetic_bars();now=int(time.time()*1000)
        with tempfile.TemporaryDirectory() as tmp:
            r=CoinModelRegistry(tmp);info=r.fit('BTCUSDT',bars);version=info['version']
            self.assertEqual(info['professional_policy'],POLICY)
            self.assertEqual(info['historical_candles_used'],9000)
            self.assertEqual(len(r.version_history('BTCUSDT')),1)
            reloaded=CoinModelRegistry(tmp);pred=reloaded.infer('BTCUSDT',bars,now)
            self.assertIsNotNone(pred);self.assertEqual(pred['model_version'],version)
            original=r._artifact_path('BTCUSDT').read_bytes()
            approved={'LONG':{'approved':True},'SHORT':{'approved':False}}
            blocked={s:{'approved':False} for s in ('LONG','SHORT')}
            with patch.object(r,'permissions',return_value=approved),patch('coin_models.model_permissions',return_value=blocked):
                challenger=r.fit('BTCUSDT',bars)
            self.assertFalse(challenger['promotion']['accepted']);self.assertEqual(r.version('BTCUSDT'),version)
            self.assertEqual(original,r._artifact_path('BTCUSDT').read_bytes())
            self.assertEqual(len(r.version_history('BTCUSDT')),2)
            with self.assertRaises(ValueError):r.restore_version('BTCUSDT','../../bad')
            with patch('coin_models.model_permissions',return_value=approved):
                restored=r.restore_version('BTCUSDT',challenger['version'])
            self.assertEqual(restored['version'],challenger['version'])
            self.assertEqual(r.version('BTCUSDT'),challenger['version'])
            json.dumps(info,allow_nan=False);json.dumps(pred,allow_nan=False)
    def test_bad_history_gap_or_ohlc_never_train(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=CoinModelRegistry(tmp);bars=synthetic_bars(600)
            with self.assertRaises(ValueError):r.fit('BTCUSDT',bars[:100]+bars[101:])
            bars[20]['high']=bars[20]['low']*.5
            with self.assertRaises(ValueError):r.fit('BTCUSDT',bars)
    def test_successful_history_pages_survive_a_later_request_failure(self):
        import asyncio,app
        bars=synthetic_bars(9000)
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp);path=cache/'BTCUSDT.json'
            path.write_text(json.dumps({'symbol':'BTCUSDT','fetched_at':int(time.time()),'candles':bars[-1500:]}))
            fetch=AsyncMock(side_effect=[bars[-3000:-1500],RuntimeError('interrupted')])
            with patch.object(app,'TRAINING_CACHE',cache),patch.object(app.client,'klines',fetch):
                with self.assertRaises(RuntimeError):asyncio.run(app._training_bars('BTCUSDT'))
                saved=json.loads(path.read_text());self.assertEqual(len(saved['candles']),3000)
                self.assertEqual(len(asyncio.run(app._training_bars('BTCUSDT'))),3000)
                self.assertEqual(fetch.await_count,2)


if __name__=='__main__':unittest.main()
