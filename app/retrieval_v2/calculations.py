"""Bounded Decimal operations, with exact operand and unit provenance."""
import re
from decimal import Decimal, localcontext, InvalidOperation
from typing import Literal
from pydantic import Field
from .models import StrictModel


class Operand(StrictModel):
    value: str = Field(min_length=1,max_length=64)
    unit: str = Field(default='',max_length=32)
    source: Literal['user','evidence']
    evidence_id: str | None = None
    source_id: str | None = Field(default=None,pattern=r'^S[1-9][0-9]*$',description='当前 Run 可见来源编号，由 Agent 安全转换；REST 调用仍提供 evidence_id')
    start: int | None = Field(default=None,ge=0,le=200000,description='仅当值在来源中重复出现时填写准确偏移；唯一值请省略，服务器精确定位')
    end: int | None = Field(default=None,gt=0,le=200000)
    unit_start: int | None = Field(default=None,ge=0,le=200000)
    unit_end: int | None = Field(default=None,ge=0,le=200000)


class CalculationInput(StrictModel):
    knowledge_base_id: str = Field(min_length=1,max_length=200)
    operation: Literal['add','subtract','multiply','divide','percentage_change','ratio','average','min','max','convert']
    operands: list[Operand] = Field(min_length=1,max_length=20)
    target_unit: str | None = Field(default=None,max_length=32)


UNITS = {'m':('length','1'),'cm':('length','.01'),'mm':('length','.001'),'km':('length','1000'),
         'kg':('mass','1'),'g':('mass','.001'),'mg':('mass','.000001'),
         's':('time','1'),'min':('time','60'),'h':('time','3600'),
         'Pa':('pressure','1'),'kPa':('pressure','1000'),'MPa':('pressure','1000000')}


def calculate(value, context):
    if context is None:
        raise PermissionError('calculation_source_context_required')
    values, units, sources = [], [], []
    for item in value.operands:
        item=item.model_copy()
        text = context(item.source, item.evidence_id)
        if item.start is None and item.end is None:
            positions=list(re.finditer(r'(?<![0-9,.eE+−\-])'+re.escape(item.value)+r'(?![0-9eE]|[.,][0-9])',text))
            if len(positions)!=1: raise ValueError('operand_not_unique_supply_offsets')
            item.start,item.end=positions[0].span()
        if item.start is None or item.end is None: raise ValueError('incomplete_range')
        if item.end <= item.start or text[item.start:item.end] != item.value:
            raise ValueError('operand_source_mismatch')
        # Prevent extracting 12 from 125.6, signs or scientific notation.
        if (item.start and re.match(r'[0-9,.eE+−\-]',text[item.start-1]) or
            item.end<len(text) and re.match(r'(?:[0-9eE]|[.,][0-9])',text[item.end:])):
            raise ValueError('partial_number')
        if item.unit:
            if item.unit_start is None and item.unit_end is None:
                positions=list(re.finditer(re.escape(item.unit),text))
                if len(positions)!=1: raise ValueError('unit_not_unique_supply_offsets')
                item.unit_start,item.unit_end=positions[0].span()
            if item.unit_start is None or item.unit_end is None or text[item.unit_start:item.unit_end] != item.unit:
                raise ValueError('unit_source_mismatch')
        # Preserve the exact source spelling and offsets; normalize only for Decimal.
        numeric=item.value.replace('−','-')
        if ',' in numeric:
            if not re.fullmatch(r'[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?',numeric):
                raise ValueError('invalid_number')
            numeric=numeric.replace(',','')
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?', numeric):
            raise ValueError('invalid_number')
        number = Decimal(numeric)
        if not number.is_finite() or abs(number.adjusted())>100:
            raise ValueError('numeric_budget')
        values.append(number);units.append(item.unit);sources.append(item.model_dump())
    op = value.operation
    if op in {'subtract','divide','ratio','percentage_change'} and len(values)!=2:
        raise ValueError('two_operands_required')
    unit = units[0]
    if op != 'convert' and len(set(units))!=1:
        raise ValueError('incompatible_units_convert_explicitly')
    if op == 'multiply' and any(units):
        raise ValueError('compound_units_not_supported')
    with localcontext() as ctx:
        ctx.prec = 34
        try:
            if op == 'add': result = sum(values)
            elif op == 'subtract': result = values[0]-values[1]
            elif op == 'multiply':
                result = Decimal(1)
                for number in values: result *= number
            elif op in {'divide','ratio'}: result = values[0]/values[1];unit=''
            elif op == 'percentage_change': result=(values[1]-values[0])/values[0]*100;unit='%'
            elif op == 'average': result=sum(values)/len(values)
            elif op == 'min': result=min(values)
            elif op == 'max': result=max(values)
            else:
                if len(values)!=1 or unit not in UNITS or value.target_unit not in UNITS:
                    raise ValueError('unsupported_unit_conversion')
                a,b=UNITS[unit],UNITS[value.target_unit]
                if a[0]!=b[0]: raise ValueError('incompatible_dimensions')
                result=values[0]*Decimal(a[1])/Decimal(b[1]);unit=value.target_unit
        except (ArithmeticError,InvalidOperation):
            raise ValueError('invalid_arithmetic') from None
    return dict(result=str(result),unit=unit,operation=op,operands=sources,
                precision=34,rounding='ROUND_HALF_EVEN', applicability='not_assessed',
                warning='来源位置经过核对；统计口径、型号和测试条件仍须根据原文确认。')
