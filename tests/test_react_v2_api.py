import json
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from app.main import create_app
from app.auth import SessionManager
from tests.test_api import offline_settings
from tests.test_react_v2 import ScriptedModel


def test_opt_in_api_legacy_and_stream(tmp_path):
    app=create_app(offline_settings(tmp_path))
    agent=app.state.registry.default()
    agent.agent.llm=ScriptedModel(AIMessage(content='你好'),AIMessage(content='你好'))
    with TestClient(app) as client:
        response=client.post('/api/ask',json={'question':'你好','runtime_mode':'react_v2'})
        assert response.status_code==200,response.text
        value=response.json();assert value['runtime_mode']=='react_v2' and value['thread_id']
        state=client.get('/api/v2/agent/threads/'+value['thread_id'])
        assert state.status_code==200 and 'messages' not in state.json()
        response=client.post('/api/v2/agent/stream',json={'question':'你好','agent_id':agent.profile.agent_id,'conversation_id':'stream'})
        assert response.status_code==200
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert [e['type'] for e in events]==['run_started','thinking','answer_started','answer_delta','completed','result']
        assert len({e['event_id'] for e in events if 'event_id' in e})==5
        assert events[-1]['data']['response']['answer']=='你好'
        assert client.delete('/api/v2/agent/threads/'+value['thread_id']).status_code==200


def test_agent_configuration_roundtrip(tmp_path):
    app=create_app(offline_settings(tmp_path))
    registry=app.state.registry;profile=registry.default().profile
    configured=profile.model_copy(update={'runtime_mode':'react_v2','react_tools':['v2.read_document']})
    registry.save_agent(configured)
    stored=registry.store.get_agent(profile.agent_id)
    assert stored.runtime_mode=='react_v2' and stored.react_tools==['v2.read_document']
    assert stored.allowed_tools==profile.allowed_tools
    registry.save_agent(stored.model_copy(update={'runtime_mode':'legacy'}))
    assert registry.default().profile.runtime_mode=='legacy'


def test_no_raw_checkpoint_or_foreign_thread(tmp_path):
    settings=offline_settings(tmp_path).model_copy(update={'auth_enabled':True})
    app=create_app(settings)
    agent=app.state.registry.default();agent.agent.llm=ScriptedModel(AIMessage(content='你好'))
    sessions=SessionManager(settings.admin_username,settings.admin_password_hash,settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name,sessions.create(settings.admin_username,True,'local'))
        response=client.post('/api/v2/agent/run',json={'question':'你好','agent_id':agent.profile.agent_id})
        assert response.status_code==200,response.text
        thread=response.json()['thread_id']
        client.cookies.set(sessions.cookie_name,sessions.create('other',False,'ldap'))
        for method,suffix in [('get',''),('post','/resume'),('post','/cancel'),('delete','')]:
            assert getattr(client,method)('/api/v2/agent/threads/'+thread+suffix).status_code==404
