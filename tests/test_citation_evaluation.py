import pytest
from scripts.evaluate_agent_citations import validate_judgment,aggregate,gold_mention


def test_judge_needs_exact_witness_in_claim_sources():
    claims=[{'text':'Value is 42','source_ids':['S1']}]
    evidence={'S1':'Revenue 42 dollars','S2':'Other 99 dollars'}
    obj={'answer_kind':'answered','claims':[{'index':0,'verdict':'supported',
        'quotes':[{'source_id':'S1','quote':'42 dollars'}]}]}
    assert validate_judgment(obj,claims,evidence)['claims'][0]['verdict']=='supported'
    for quotes in [[],[{'source_id':'S2','quote':'99 dollars'}],[{'source_id':'S1','quote':'42 euros'}]]:
        obj['claims'][0]['quotes']=quotes
        with pytest.raises(ValueError): validate_judgment(obj,claims,evidence)


def test_judge_cannot_skip_or_duplicate_claims():
    claims=[{'text':'a','source_ids':['S1']},{'text':'b','source_ids':['S1']}]
    obj={'answer_kind':'answered','claims':[{'index':0,'verdict':'unclear'}]}
    with pytest.raises(ValueError): validate_judgment(obj,claims,{'S1':'text'})
    obj['claims']*=2
    with pytest.raises(ValueError): validate_judgment(obj,claims,{'S1':'text'})


def test_html_display_quote_normalization_preserves_values():
    claims=[{'text':'Value is 42','source_ids':['S1']}]
    evidence={'S1':'<table><tr><td>Revenue</td><td>42</td></tr></table>'}
    obj={'answer_kind':'answered','claims':[{'index':0,'verdict':'supported',
        'quotes':[{'source_id':'S1','quote':'Revenue\n42'}]}]}
    assert validate_judgment(obj,claims,evidence)['claims'][0]['display_normalized_quote_count']==1
    obj['claims'][0]['quotes'][0]['quote']='Revenue 43'
    with pytest.raises(ValueError): validate_judgment(obj,claims,evidence)


def test_failed_cases_stay_in_denominator_and_no_accuracy_claim():
    rows=[{'status':'failed'}, {'citation_validation':'rejected','repair_attempted':True},
          {'citation_validation':'passed','final_citation_count':2,'gold_document_citation_count':1,
           'gold_document_hit':True,'support_judgment':{'status':'assessed','claims':[
               {'verdict':'partial'}],'answer_kind':'answered'},'agent_seconds':2}]
    r=aggregate(rows)
    assert r['completed_questions']==3 and r['citation_validation']['execution_failed']==1
    assert r['total_citations']==2 and r['gold_source_citations']==1
    assert r['fully_supported_answers']==0 and r['repair_success']==0
    assert 'accuracy' not in r
    assert aggregate([{'status':'failed'}])['agent_p50_seconds'] is None


@pytest.mark.parametrize('text,gold,expected',[
    ('Year 2019','["2019"]',True),('20190','["2019"]',False),
    ('42.5','["42"]',False),('Both 42 and 43','["42","43"]',True),
    ('I cannot answer','["42"]',False)])
def test_gold_mention_is_only_bounded_lexical_match(text,gold,expected):
    assert bool(gold_mention(text,gold))==expected
