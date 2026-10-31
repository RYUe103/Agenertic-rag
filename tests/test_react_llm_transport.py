from langchain_core.messages import HumanMessage,ToolMessage,AIMessage
from app.clients.llm import LLMClient


def test_native_chat_tool_turns_and_malformed_args(monkeypatch):
    model=LLMClient('https://unused','fake','test')
    seen=[]
    def post(path,body):
        seen.append(body)
        return {'choices':[{'message':{'content':'','tool_calls':[
            {'id':'valid','function':{'name':'v2__read_document','arguments':'{"document_id":"a"}'}},
            {'id':'invalid','function':{'name':'v2__read_document','arguments':'{'}}]}}],'usage':{'total_tokens':10}}
    monkeypatch.setattr(model,'_post',post)
    result=model.react([HumanMessage(content='q'),AIMessage(content='',tool_calls=[dict(id='c',name='t',args={})]),
                       ToolMessage(content='observation',tool_call_id='c')],[])
    assert result.tool_calls[0]['id']=='valid' and result.invalid_tool_calls[0]['id']=='invalid'
    assert seen[0]['messages'][-1]['tool_call_id']=='c'
    assert result.response_metadata['usage']['total_tokens']==10


def test_responses_function_call_observation(monkeypatch):
    model=LLMClient('https://unused','fake','test','responses')
    seen=[]
    def post(path,body):
        seen.append((path,body));return {'output':[{'type':'function_call','call_id':'next','name':'v2__read_document','arguments':'{}'}]}
    monkeypatch.setattr(model,'_post',post)
    result=model.react([HumanMessage(content='q'),AIMessage(content='',tool_calls=[dict(id='c',name='t',args={})]),
                       ToolMessage(content='observation',tool_call_id='c')],[])
    assert seen[0][0]=='/v1/responses'
    assert seen[0][1]['input'][-1]=={'type':'function_call_output','call_id':'c','output':'observation'}
    assert result.tool_calls[0]['id']=='next'
