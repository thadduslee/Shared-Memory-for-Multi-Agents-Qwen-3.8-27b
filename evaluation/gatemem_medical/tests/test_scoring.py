"""GateMem scoring integration checks, separate from the reusable core suite."""
import unittest
from pathlib import Path

from evaluation.gatemem_medical.scoring import GateMemScorer,verdict_schema,valid_verdict


class JudgeDouble:
    def __init__(self,responses):
        self.responses=iter(responses)
        self.schemas=[]

    def complete(self,system,payload,*,schema=None):
        self.schemas.append(schema)
        return next(self.responses)


def verdict(category,value):
    result={'action_ok':True,'utility_ok':None,'privacy_leak':None,
            'deletion_leak':None,'notes':'Category evaluated.'}
    result[{'utility':'utility_ok','privacy':'privacy_leak','safety':'deletion_leak'}[category]]=value
    return result


@unittest.skipUnless(Path('/home/thaddus/GateMem/bench/eval/scorer.py').is_file(),'GateMem fixture repository required')
class ScoringTests(unittest.TestCase):
    def score(self,category,judge):
        checkpoint={'checkpoint_id':'case-1','episode_id':'episode-1',
                    'query_type':category,'as_of_turn_id':'turn-1',
                    'query_text':'What is available?',
                    'expected_action':'answer','judge_spec':{'include':[]},'leak_targets':[]}
        prediction={'output':{'action':'answer','answer':'Available information.',
                              'answer_structured':{},'retrieved_memory':[]}}
        return GateMemScorer('/home/thaddus/GateMem',judge).score(checkpoint,prediction)

    def test_applicable_null_retried_without_changing_false_verdict(self):
        judge=JudgeDouble([verdict('utility',None),verdict('utility',False)])
        result=self.score('utility',judge)
        self.assertFalse(result['utility_correct'])
        self.assertEqual(len(judge.schemas),2)
        self.assertEqual(judge.schemas[0]['properties']['utility_ok'],{'type':'boolean'})

    def test_invalid_verdict_is_never_a_pass(self):
        judge=JudgeDouble([verdict('privacy','false')]*3)
        with self.assertRaisesRegex(ValueError,'remains unscored'):
            self.score('privacy',judge)
        self.assertEqual(len(judge.schemas),3)

    def test_judge_leak_preserved_for_each_leak_category(self):
        for category,key in [('privacy','privacy_e2e_leak'),('safety','deletion_e2e_leak')]:
            with self.subTest(category=category):
                result=self.score(category,JudgeDouble([verdict(category,True)]))
                self.assertTrue(result[key])
                self.assertFalse(result['passed'])

    def test_inapplicable_fields_cannot_supply_a_verdict(self):
        result=verdict('privacy',False)
        result['utility_ok']=True
        self.assertFalse(valid_verdict(result,verdict_schema('privacy')))

    def test_extra_fields_and_integers_rejected(self):
        result=verdict('utility',True)
        result['action_ok']=1
        self.assertFalse(valid_verdict(result,verdict_schema('utility')))
        result=verdict('utility',True)
        result['unexpected']=True
        self.assertFalse(valid_verdict(result,verdict_schema('utility')))


if __name__=='__main__':
    unittest.main()
