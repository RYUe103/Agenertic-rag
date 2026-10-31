from collections import Counter


def fuse(channels, config):
    """A candidate contributes at most once per channel, irrespective of occurrences."""
    unique = {}
    raw_count = 0
    for channel, hits in sorted(channels.items()):
        seen = set()
        weight = getattr(config, channel+'_weight')
        for hit in hits:
            raw_count += 1
            key = hit.key()
            if key in seen:
                continue
            seen.add(key)
            if key not in unique:
                unique[key] = hit.model_copy(deep=True)
                unique[key].fused_score = 0
                unique[key].provenance = []
            item = unique[key]
            item.fused_score += weight/(config.rrf_k+hit.source_rank)
            item.provenance.append(dict(adapter=channel,rank=hit.source_rank,score=hit.source_score,
                                        generation=hit.index_generation))
            for match in hit.matched_ranges:
                if match not in item.matched_ranges:
                    item.matched_ranges.append(match)
    ranked = sorted(unique.values(),key=lambda e:(-e.fused_score,e.key()))
    docs = {}
    for item in ranked:
        key = item.knowledge_base_id,item.document_id,item.version_id
        doc = docs.setdefault(key,dict(knowledge_base_id=key[0],document_id=key[1],version_id=key[2],
            score=item.fused_score,chunk_ids=[],table_ids=[],section_ids=[],candidate_count=0))
        doc['candidate_count'] += 1
        if item.chunk_id:
            doc['chunk_ids'].append(item.chunk_id)
        doc['table_ids']=sorted(set(doc['table_ids']+item.table_ids))
        doc['section_ids']=sorted(set(doc['section_ids']+item.section_ids))
    return ranked, list(docs.values()), raw_count-len(unique)


def diversify(ranked, maximum, top_k):
    counts, result = Counter(), []
    for item in ranked:
        key = item.knowledge_base_id,item.document_id
        if counts[key] < maximum:
            result.append(item)
            counts[key] += 1
        if len(result)>=top_k:
            break
    return result
