import json
from pathlib import Path
import tempfile
import unittest

from evaluation.gatemem_medical.evolution_ablation import compare,paired_counts


class PairedComparisonTests(unittest.TestCase):
    def test_wins_losses_and_exact_test(self):
        result=paired_counts([(False,True),(False,True),(True,False),(True,True)])
        self.assertEqual((result['wins'],result['losses'],result['ties']),(2,1,1))
        self.assertEqual(result['mcnemar_exact_two_sided_p'],1.)
        self.assertEqual(paired_counts([(False,True)]*6)['mcnemar_exact_two_sided_p'],.03125)

    def test_empty_cohort(self):
        self.assertEqual(paired_counts([])['n'],0)
        self.assertEqual(paired_counts([])['mcnemar_exact_two_sided_p'],1.)

    def test_different_model_arms_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for index,arm in enumerate(('baseline','evolved')):
                directory=root/arm;directory.mkdir()
                manifest={k:'same' for k in ('dataset_digest','code_digest','embedding_fingerprint','offline','schedule')}
                manifest['models']={'judge':str(index)}
                (directory/'manifest.json').write_text(json.dumps(manifest))
                (directory/'summary.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'differ in models'):
                compare(root,Path('/unused'))


if __name__=='__main__':unittest.main()
