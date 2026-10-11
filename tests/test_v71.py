import json
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from test_v62 import prediction, quote, book, NOW
from coin_models import CoinModelRegistry, ENSEMBLE_POLICY, MODEL_FAMILY
from execution_guard import guarded_prediction


class ConstantModel:
    classes_ = np.array([0, 1])
    def __init__(self, p): self.p = p
    def predict_proba(self, x):
        return np.tile([1-self.p, self.p], (len(x), 1))


def ensemble(scores=(.82, .74, .66), weights=(.5, .3, .2)):
    return {"kind": ENSEMBLE_POLICY,
            "members": [{"name":str(i), "model":ConstantModel(p)} for i,p in enumerate(scores)],
            "weights":list(weights)}


class VotingTests(unittest.TestCase):
    def test_weighted_probabilities_and_persistable_member_audit(self):
        p, agree, detail = CoinModelRegistry.vote_details(ensemble(),
            {"validation_prior":.5, "alpha":1}, np.zeros((2, 58)))
        self.assertTrue(np.allclose(p, .764))
        self.assertTrue(agree.all())
        self.assertEqual(len(detail[0]['members']), 3)
        json.dumps(detail, allow_nan=False)

    def test_high_average_does_not_override_disagreement_or_min_two_votes(self):
        for model in [ensemble((.9,.68,.4)), ensemble((.81,.59,.59),(.8,.1,.1))]:
            p, agree, _ = CoinModelRegistry.vote_details(model,
                {"validation_prior":.5, "alpha":1}, np.zeros((1,58)))
            self.assertGreater(p[0], .65)
            self.assertFalse(agree[0])

    def test_bad_payload_cannot_fall_back_to_single_model(self):
        for model in [ensemble((.8,), (1,)), ensemble(weights=(.5,.3,-.2)),
                      ensemble(weights=(.5,.3,.3))]:
            with self.assertRaises(ValueError):
                CoinModelRegistry._predict_tp(model, np.zeros((1,58)))
        _, agreement, detail = CoinModelRegistry.vote_details(ConstantModel(.9),
            {"validation_prior":.5,"alpha":1},np.zeros((1,58)))
        self.assertFalse(agreement[0]); self.assertEqual(detail, [])

    def test_consensus_veto_survives_fresh_quote_and_permission(self):
        p=prediction();p['ensemble']={'policy':ENSEMBLE_POLICY,'LONG':{'agreement':False}}
        result=guarded_prediction(p,quote(),NOW,True,book=book())
        self.assertEqual(result['signal'],'BEKLE')
        self.assertFalse(result['execution_quality']['approved'])
        self.assertIn('consensus',[c['id'] for c in result['execution_quality']['checks'] if not c['passed']])
        p['ensemble']['LONG']['agreement']=True
        self.assertEqual(guarded_prediction(p,quote(),NOW,True,book=book())['signal'],'LONG')

    def test_old_models_migrate_immediately_without_old_permission_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=CoinModelRegistry(tmp)
            registry.metadata['BTCUSDT']={'model_family':MODEL_FAMILY,
                'trained_at':'2099-01-01T00:00:00+00:00'}
            with patch.object(registry,'ready',return_value=True):
                self.assertTrue(registry.training_due('BTCUSDT'))
                self.assertIsNone(registry.infer('BTCUSDT',[]))
                with patch('coin_models.model_permissions',return_value={s:{'approved':True} for s in ('LONG','SHORT')}):
                    self.assertFalse(registry.permissions('BTCUSDT')['LONG']['approved'])
                stats=registry.stats(['BTCUSDT'])
                self.assertEqual(stats['ensemble_trained'],0)
                self.assertEqual(stats['ensemble_pending'],1)

    def test_metadata_policy_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=CoinModelRegistry(tmp)
            r.metadata['BTCUSDT']={'version':'v71','artifact_schema':2,
                'model_family':MODEL_FAMILY,'ensemble_policy':ENSEMBLE_POLICY}
            with patch.object(r,'ready',return_value=True), patch('coin_models.joblib.load',
                return_value={'version':'v71','model_family':MODEL_FAMILY}):
                self.assertIsNone(r._load('BTCUSDT'))


if __name__=='__main__': unittest.main()
