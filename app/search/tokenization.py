import re
import unicodedata
import jieba

TOKENIZER_VERSION = 'jieba-0.42.1-hmm0-nfkc-tech-v1'
_segmenter = jieba.Tokenizer()
_segmenter.initialize()
TECH = re.compile(r'[a-z0-9]+(?:[./×*+\-][a-z0-9]+)+|[a-z]+[0-9]+[a-z0-9]*|\d+(?:\.\d+)?(?:kv|mm2|mm|kg|%|元)', re.I)


def normalize(text):
    return unicodedata.normalize('NFKC', text).casefold().replace('−','-').replace('–','-')


def tokens(text):
    text = normalize(text)
    result = [t for t in _segmenter.cut_for_search(text, HMM=False) if re.search(r'\w',t)]
    # Exact technical token supplements word segmentation, retaining separators.
    result.extend(m[0] for m in TECH.finditer(text))
    return result


def identifiers(text):
    return ' '.join(m[0] for m in TECH.finditer(normalize(text)))


def positions(text, query, limit=20):
    # Original offsets: normalization can change length, so never report normalized offsets.
    terms = sorted(set(tokens(query)), key=len, reverse=True)
    found = []
    for term in terms[:64]:
        for match in re.finditer(re.escape(term),text,re.I):
            found.append({'start':match.start(),'end':match.end(),'text':match[0]})
            if len(found) >= limit:
                return found
    return found
