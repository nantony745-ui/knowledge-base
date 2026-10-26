def fuse_rrf(two_ranked_lists: list[list[dict]], k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    for route, hits in zip(('bm25', 'vector'), two_ranked_lists):
        seen = set()
        for hit in hits:
            chunk_id = hit['chunk_id']
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank = len(seen)
            row = fused.setdefault(chunk_id, {**hit, 'rrf_score': 0.0, 'bm25_rank': None,
                                             'vector_rank': None, 'bm25_contribution': 0.0,
                                             'vector_contribution': 0.0})
            contribution = 1 / (k + rank)
            row[route+'_rank'] = rank
            row[route+'_contribution'] = contribution
            row['rrf_score'] += contribution
    return sorted(fused.values(), key=lambda h: (-h['rrf_score'], h['chunk_id']))
