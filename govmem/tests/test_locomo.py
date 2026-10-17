"""LoCoMo adapter: annotations stay out of the conversation; F1 behaves as in the LoCoMo paper (unstemmed)."""

from govmem.adapters.locomo.answer import f1, f1_single
from govmem.adapters.locomo.data import QUESTIONER, to_conversation

SAMPLE = {"sample_id": "conv-x", "conversation": {
    "speaker_a": "Ona Vale", "speaker_b": "Tam Rook",
    "session_1_date_time": "1:00 pm on 2 May, 2023",
    "session_1": [{"speaker": "Ona Vale", "dia_id": "D1:1", "text": "I adopted a cat."},
                  {"speaker": "Tam Rook", "dia_id": "D1:2", "text": "Nice!", "blip_caption": "a photo of a cat"}]},
    "qa": [{"question": "What did Ona adopt?", "answer": "a cat", "evidence": ["D1:1"], "category": 4},
           {"question": "Why?", "evidence": ["D1:1"], "category": 5, "adversarial_answer": "x"}]}


def test_mapping_hides_annotations_and_keeps_dates_and_captions():
    conv, gold = to_conversation(SAMPLE)
    assert [p["id"] for p in conv.principals][-1] == QUESTIONER
    assert conv.turns[1]["text"].endswith("[shares a photo: a photo of a cat]")
    assert conv.turns[0]["timestamp"] == "1:00 pm on 2 May, 2023"
    assert len(conv.questions) == 1 and set(conv.questions[0]) == {"qid", "question"}
    assert gold[conv.questions[0]["qid"]]["answer"] == "a cat"


def test_f1():
    assert f1_single("A cat", "a cat") == 1.0
    assert f1_single("dog", "a cat") == 0.0
    assert 0 < f1("painting, hiking", "hiking, swimming", 1) < 1
