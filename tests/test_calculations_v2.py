import pytest
from app.retrieval_v2.calculations import CalculationInput, calculate


def run(op,numbers,units=None,target=None):
    units=units or ['']*len(numbers);text='';items=[]
    for number,unit in zip(numbers,units):
        start=len(text);text+=number+' '
        item=dict(value=number,unit=unit,source='user',start=start,end=start+len(number))
        if unit:
            item.update(unit_start=len(text),unit_end=len(text)+len(unit));text+=unit+' '
        items.append(item)
    model=CalculationInput(knowledge_base_id='kb',operation=op,operands=items,target_unit=target)
    return calculate(model,lambda kind,ident:text)


@pytest.mark.parametrize('op,nums,expected',[
    ('add',['0.1','0.2'],'0.3'),('subtract',['3','2'],'1'),('multiply',['2','3'],'6'),
    ('divide',['6','3'],'2'),('ratio',['3','6'],'0.5'),('percentage_change',['100','125'],'25.00'),
    ('average',['1','2','3'],'2'),('min',['-1','2'],'-1'),('max',['1','2'],'2'),
    ('add',['1e3','0.001'],'1000.001')])
def test_arithmetic(op,nums,expected): assert run(op,nums)['result']==expected


def test_convert():
    assert run('convert',['1.25'],['m'],'mm')['result']=='1.25E+3'
    assert run('convert',['1e3'],['g'],'kg')['result']=='1'


@pytest.mark.parametrize('op,nums,units,target',[
    ('add',['1','2'],['m','mm'],None),('convert',['1'],['m'],'kg'),
    ('convert',['1'],['unknown'],'m'),('divide',['1','0'],None,None),
    ('add',['NaN','1'],None,None),('add',['1e999','1'],None,None),
    ('multiply',['2','3'],['kg','kg'],None),('add',["__import__('os')",'1'],None,None)])
def test_reject_unsafe(op,nums,units,target):
    with pytest.raises((ValueError,ArithmeticError)): run(op,nums,units,target)


def test_missing_or_false_source():
    model=CalculationInput(knowledge_base_id='kb',operation='add',operands=[dict(value='12',source='user',start=0,end=2)])
    with pytest.raises(ValueError): calculate(model,lambda *_:'125.6')
    with pytest.raises(ValueError): calculate(model,lambda *_:'99')
    with pytest.raises(PermissionError): calculate(model,None)


def test_unique_chinese_operand_and_duplicate_rejection():
    value=CalculationInput(knowledge_base_id='kb',operation='percentage_change',operands=[
        dict(value='125.6',source='user'),dict(value='132.4',source='user')])
    result=calculate(value,lambda *_:'2018 年收入 125.6，2019 年收入 132.4，增长率是多少？')
    assert result['result'].startswith('5.414012738853503')
    assert result['operands'][0]['start']==9
    with pytest.raises(ValueError,match='not_unique'):
        calculate(value,lambda *_:'125.6 125.6 132.4')
