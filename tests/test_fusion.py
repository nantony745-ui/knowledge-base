from app.rag.fusion import fuse_rrf
import pytest

def test_rrf_hand_calculation_and_deduplication():
    hits = fuse_rrf([[{'chunk_id': 'a'}, {'chunk_id': 'a'}, {'chunk_id': 'b'}],
                     [{'chunk_id': 'b'}, {'chunk_id': 'c'}]])
    assert [h['chunk_id'] for h in hits] == ['b', 'a', 'c']
    assert hits[0]['rrf_score'] == pytest.approx(1/62+1/61)
    assert hits[1]['rrf_score'] == pytest.approx(1/61)
    assert hits[2]['rrf_score'] == pytest.approx(1/62)
    assert hits[0]['bm25_rank'] == 2 and hits[0]['vector_rank'] == 1
    assert hits[1]['vector_rank'] is None and hits[1]['vector_contribution'] == 0

def test_rrf_ties_use_chunk_id_not_raw_scores():
    result = fuse_rrf([[{'chunk_id':'b','score':100000}], [{'chunk_id':'a','score':-1}]])
    assert [h['chunk_id'] for h in result] == ['a', 'b']
    assert fuse_rrf([[],[]]) == []
