import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from decision_policy import decide, select_policy
from quality_guard import model_permissions, validation_checks
from coin_models import CoinModelRegistry, MODEL_FAMILY, INFERENCE_HISTORY_BARS, ENSEMBLE_POLICY
from professional import POLICY
from ml_engine import features, COLS
from execution_guard import guarded_prediction
from test_v62 import prediction, quote, book, NOW


def candles(n=4500):
    rng = np.random.default_rng(71)
    end = NOW//900000*900000
    returns = rng.normal(0, .006, n) + .002*np.sin(np.arange(n)/60)
    close = 100*np.exp(np.cumsum(returns))
    opens = np.r_[close[0], close[:-1]]
    return [dict(time=end-(n-i)*900000, close_time=end-(n-i-1)*900000-1,
                 open=float(opens[i]), close=float(close[i]),
                 high=float(max(opens[i], close[i])*1.003),
                 low=float(min(opens[i], close[i])*.997),
                 volume=float(rng.uniform(100, 300))) for i in range(n)]


class ThresholdTests(unittest.TestCase):
    def policy(self):
        times = np.arange(600)*900000
        long = np.where((np.arange(600)//48)%2 == 0, .60, .20)
        short = np.where(long == .60, .20, .60)
        outcomes = np.tile(np.where(np.arange(600)%10 < 8, 1, -1)[:, None], (1, 2))
        plans = {s: dict(tp_atr=2.5, sl_atr=1.5) for s in ('LONG', 'SHORT')}
        return select_policy(long, short, outcomes, np.full(600, .012), times, plans)

    def test_calibrated_scores_can_qualify_only_with_frozen_validation_policy(self):
        policy = self.policy()
        self.assertTrue(policy['search_succeeded'])
        self.assertEqual(policy['threshold'], .60)
        self.assertEqual(decide(.60, .20)[0], 'BEKLE')
        self.assertEqual(decide(.60, .20, policy)[0], 'LONG')
        self.assertEqual(decide(.20, .60, policy)[0], 'SHORT')
        self.assertFalse(policy['holdout_used_for_selection'])

    def test_bad_validation_does_not_create_trade_permission(self):
        plans = {s: dict(tp_atr=2.5, sl_atr=1.5) for s in ('LONG', 'SHORT')}
        policy = select_policy(np.full(600, .6), np.full(600, .2),
                               np.full((600, 2), -1), np.full(600, .01), np.arange(600)*900000, plans)
        self.assertFalse(policy['search_succeeded'])
        self.assertEqual(policy['threshold'], .65)
        self.assertFalse(any(policy['validation_passed'].values()))

    def test_validation_search_includes_existing_consensus_veto(self):
        plans = {s: dict(tp_atr=2.5, sl_atr=1.5) for s in ('LONG', 'SHORT')}
        policy = select_policy(np.full(600, .8), np.full(600, .2),
                               np.ones((600, 2)), np.full(600, .01), np.arange(600)*900000, plans,
                               {s: np.zeros(600, dtype=bool) for s in ('LONG', 'SHORT')})
        self.assertFalse(policy['search_succeeded'])
        self.assertEqual(policy['validation_summary']['LONG']['signals'], 0)

    def test_policy_corruption_or_nonfinite_score_waits(self):
        policy = self.policy()
        for changes in ({'chosen_on': 'holdout'}, {'threshold': .1}, {'frozen_before_holdout': False}):
            self.assertEqual(decide(.8, .1, dict(policy, **changes))[0], 'BEKLE')
        self.assertEqual(decide(float('nan'), .2, policy)[0], 'BEKLE')
        self.assertEqual(decide(.8, .79, policy)[0], 'BEKLE')

    def test_candidate_still_cannot_bypass_live_permission(self):
        p = prediction(); p.update(p_long=.60, confidence=.60)
        self.assertEqual(guarded_prediction(p, quote(), NOW, False, book=book())['signal'], 'BEKLE')

    def test_validation_selection_alone_cannot_override_failed_holdout(self):
        metrics = dict(signals=80, resolved=80, resolved_fraction=1,
            distinct_test_candles=80, hit_rate=.8, wilson_lower_95=.7,
            mean_assumed_net_return=.01, validation_outperformed_baseline=True,
            holdout_outperformed_baseline=True, independent_signal_windows=4,
            mean_stressed_net_return=.008, calibration_alpha=.5, calibration_sample_count=60)
        info = dict(model_family=MODEL_FAMILY, artifact_schema=3,
            evaluation_method='tp_sl_first_touch_adaptive_bracket_24h_v5', decision_policy=self.policy(),
            directional_validation={'LONG': metrics, 'SHORT': dict(metrics)},
            risk_plan={side: dict(tp_atr=2.5, sl_atr=1.5, chosen_on='predeclared_fallback') for side in ('LONG', 'SHORT')})
        self.assertTrue(model_permissions(info)['LONG']['approved'])
        info['directional_validation']['LONG']['holdout_outperformed_baseline'] = False
        result = model_permissions(info)
        self.assertFalse(result['LONG']['approved'])
        self.assertIn('holdout_outperformed_baseline', result['LONG']['failed_checks'])
        self.assertTrue(result['SHORT']['approved'])
        info['decision_policy']['validation_passed']['SHORT'] = False
        self.assertFalse(model_permissions(info)['SHORT']['approved'])

    def test_diagnostics_identify_all_rejections_and_nan(self):
        checks = validation_checks(dict(signals=50, resolved=50, resolved_fraction=1,
            distinct_test_candles=50, hit_rate=.8, wilson_lower_95=.7,
            mean_assumed_net_return=float('nan')))
        self.assertEqual([c['id'] for c in checks if not c['passed']], ['mean_assumed_net_return'])
        p = model_permissions(dict(model_family=MODEL_FAMILY, evaluation_method='tp_sl_first_touch_adaptive_bracket_24h_v5'))
        self.assertFalse(p['LONG']['approved'])
        self.assertIn('calibration_alpha', p['LONG']['failed_checks'])
        self.assertIn('signals', p['SHORT']['failed_checks'])


class ModelTests(unittest.TestCase):
    def test_uninterrupted_rally_does_not_drop_rsi_features(self):
        bars = candles(1500)
        for i, bar in enumerate(bars):
            previous = 100 + i + i*i*.0001
            price = 100 + i+1 + (i+1)*(i+1)*.0001
            bar.update(open=previous, close=price, high=price*1.001, low=previous*.999)
        frame = features(bars)
        self.assertFalse(frame.empty)
        self.assertEqual(frame.iloc[-1]['time'], bars[-1]['time'])
        self.assertEqual(frame.iloc[-1]['rsi14'], 1.)
        self.assertEqual(frame.iloc[-1]['h1_rsi'], 1.)
        self.assertEqual(frame.iloc[-1]['h4_rsi'], 1.)

    def test_feature_calculation_is_causal(self):
        bars = candles(1800)
        full = features(bars)
        prefix = features(bars[:1500])
        row = full[full['time'] == prefix.iloc[-1]['time']].iloc[0]
        np.testing.assert_allclose(row[COLS].to_numpy(dtype=float), prefix.iloc[-1][COLS].to_numpy(dtype=float))

    def test_short_history_rejected_and_new_policy_pairing_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = CoinModelRegistry(tmp)
            r.metadata['BTCUSDT'] = dict(version='new', model_family=MODEL_FAMILY, artifact_schema=3,
                                        decision_policy=ThresholdTests().policy(), ensemble_policy=ENSEMBLE_POLICY, professional_policy=POLICY)
            with patch.object(r, '_load', return_value={'models': {}, 'calibration': {}, 'ensemble_policy': ENSEMBLE_POLICY, 'professional_policy': POLICY, 'version': 'new'}):
                self.assertIsNone(r.infer('BTCUSDT', candles(110), NOW))
            payload = dict(version='new', model_family=MODEL_FAMILY, decision_policy={'threshold': .1})
            with patch.object(r, 'ready', return_value=True), patch('coin_models.joblib.load', return_value=payload):
                self.assertIsNone(r._load('BTCUSDT'))

    def test_training_reload_and_inference_use_frozen_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            bars = candles()
            r = CoinModelRegistry(tmp)
            info = r.fit('BTCUSDT', bars)
            self.assertEqual(info['artifact_schema'], 3)
            self.assertEqual(info['training_pipeline_version'], 'v73')
            self.assertEqual(info['decision_policy']['chosen_on'], 'chronological_validation_only')
            self.assertFalse(info['decision_policy']['holdout_used_for_selection'])
            # Synthetic data only verifies plumbing; no market performance claim.
            fresh = CoinModelRegistry(tmp)
            p = fresh.infer('BTCUSDT', bars[-INFERENCE_HISTORY_BARS:], NOW)
            self.assertIsNotNone(p)
            self.assertEqual(p['decision_policy'], info['decision_policy'])
            self.assertEqual(p['inference_history_bars'], INFERENCE_HISTORY_BARS)
            self.assertFalse(fresh.training_due('BTCUSDT', now=__import__('time').time()))
            fresh.metadata['BTCUSDT'].pop('training_pipeline_version')
            self.assertTrue(fresh.training_due('BTCUSDT'))


if __name__ == '__main__':
    unittest.main()
