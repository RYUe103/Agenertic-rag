"""Optional real reranking. None is the default and leaves RRF order unchanged."""
from typing import Protocol
import math
import httpx


class Reranker(Protocol):
    name: str
    def rank(self, query: str, texts: list[str], timeout: float) -> list[tuple[int,float]]: ...


class HTTPReranker:
    """Explicitly configured Cohere-compatible /rerank endpoint. No model download."""
    name = 'http-rerank'

    def __init__(self, endpoint, api_key, model):
        self.endpoint,self.api_key,self.model = endpoint,api_key,model

    def rank(self, query, texts, timeout):
        with httpx.Client(timeout=timeout) as client:
            with client.stream('POST',self.endpoint,headers={'Authorization':f'Bearer {self.api_key}'},
                json={'model':self.model,'query':query,'documents':texts,'top_n':len(texts)}) as response:
                response.raise_for_status()
                data=bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data)>1_000_000:
                        raise ValueError('Reranker response exceeds budget')
                import json
                results=json.loads(data)['results']
        return [(row['index'],row['relevance_score']) for row in results]


def apply_ranking(ranked, results):
    if len(results)!=len(ranked) or {i for i,_ in results}!=set(range(len(ranked))):
        raise ValueError('Reranker must rank each candidate exactly once')
    if any(not isinstance(i,int) or not math.isfinite(score) for i,score in results):
        raise ValueError('Invalid reranker result')
    output=[]
    for i,score in sorted(results,key=lambda pair:(-pair[1],pair[0])):
        item=ranked[i].model_copy(deep=True)
        item.rerank_score=score
        output.append(item)
    return output
