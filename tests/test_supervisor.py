from fastapi.testclient import TestClient
from types import SimpleNamespace

from app.auth import hash_password
from app.config import Settings
from app.main import create_app
from app.supervisor import SupervisorService
from app.schemas import AskResponse, CitationCheck, Evidence


def test_supervisor_executes_specialists_on_selected_knowledge_base(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    with TestClient(app) as client:
        specialist = {"agent_id": "research", "name": "Research", "knowledge_base_id": "default-kb",
                      "agent_type": "research", "allowed_tools": ["find_files"], "max_tool_calls": 3}
        assert client.post("/api/admin/agents", json=specialist).status_code == 200
        assert client.post("/api/admin/agents", json={**specialist, "agent_id": "knowledge", "name": "Knowledge", "agent_type": "knowledge"}).status_code == 200
        default = next(item for item in client.get("/api/admin/agents").json() if item["is_default"])
        default.update(agent_type="supervisor", member_agent_ids=["research", "knowledge"], allowed_tools=["find_files"], max_tool_calls=2)
        assert client.put("/api/admin/agents/" + default["agent_id"], json=default).status_code == 200
        assert client.post("/api/admin/knowledge-bases", json={"knowledge_base_id": "second-kb", "name": "Second", "dataset_id": "second", "api_key": "key"}).status_code == 200
        runtime = app.state.registry.get("@kb:second-kb")
        assert isinstance(runtime.agent, SupervisorService)
        assert runtime.agent.specialists[0].knowledge_base_id == "second-kb"
        assert runtime.agent.specialists[0].allowed_tools == {"find_files"}
        assert runtime.agent.specialists[0].max_tool_calls == 1
        result = client.post("/api/agents/%40kb%3Asecond-kb/retrieve", json={"question": "查找文件"})
        assert result.status_code == 200
        assert result.json()["knowledge_base_id"] == "second-kb"
        assert result.json()["trace"][0]["title"] == "调用 Research"
        assert any(event["title"] == "调用 Knowledge" for event in result.json()["trace"])
        assert [event["sequence"] for event in result.json()["trace"]] == list(range(1, len(result.json()["trace"]) + 1))
        assert result.json()["tool_calls"] <= 2
        assert client.delete("/api/admin/agents/research").status_code == 409
        specialist["enabled"] = False
        assert client.put("/api/admin/agents/research", json=specialist).status_code == 400
        run_id = result.json()["run_id"]
        assert client.get("/api/agent-runs/" + run_id).json()["trace"][0]["title"] == "调用 Research"


def test_supervisor_rejects_cycles_missing_members_and_disjoint_tools(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    with TestClient(create_app(settings)) as client:
        value = {"agent_id": "supervisor", "name": "Supervisor", "knowledge_base_id": "default-kb",
                 "agent_type": "supervisor", "allowed_tools": ["find_files"], "member_agent_ids": ["missing"]}
        assert client.post("/api/admin/agents", json=value).status_code == 400
        value["member_agent_ids"] = ["supervisor"]
        assert client.post("/api/admin/agents", json=value).status_code == 400
        value["member_agent_ids"] = ["default"]
        value["allowed_tools"] = ["get_adjacent_chunks"]
        default = next(item for item in client.get("/api/admin/agents").json() if item["agent_id"] == "default")
        default["allowed_tools"] = ["find_files"]
        assert client.put("/api/admin/agents/default", json=default).status_code == 200
        assert client.post("/api/admin/agents", json=value).status_code == 400


def test_supervisor_routes_only_to_known_members_and_verifies_synthesis() -> None:
    class Model:
        configured = True
        chosen = ["b"]
        supported = True
        def route_specialists(self, question, candidates):
            return self.chosen
        def answer(self, question, evidence, prompt):
            return "综合结论 [ev_001]"
        def verify_answer(self, question, answer, evidence):
            return CitationCheck(supported=self.supported)

    model = Model()
    invoked = []
    def specialist(agent_id):
        def ask(question, source_base_url, conversation_id, history):
            invoked.append(agent_id)
            return AskResponse(answer=f"原始结果 {agent_id}", evidences=[Evidence(source_id=f"src_{agent_id}", filename=f"{agent_id}.pdf", content="证据", retrieval_type="vector", pdf_url=f"http://example.test/{agent_id}")], process=[], tool_calls=1)
        return SimpleNamespace(agent_id=agent_id, agent_name=agent_id.upper(), ask=ask)
    base = SimpleNamespace(llm=model, answer_llm=model, allowed_tools={"find_files"}, agent_id="supervisor", agent_name="Supervisor", answer_prompt="", knowledge_base_id="kb")
    service = SupervisorService(base, [specialist("a"), specialist("b")], [{"id": "a"}, {"id": "b"}], "auto", "combined")
    result = service.ask("问题")
    assert invoked == ["b"] and "综合结论" in result.answer
    assert [item.source_id for item in result.sources] == ["src_b"]
    assert result.trace[0].title == "已选择专业 Agent"
    model.supported = False
    assert "原始结果 b" in service.ask("问题").answer
    model.chosen = ["unknown"]
    invoked.clear()
    fallback = service.ask("问题")
    assert invoked == ["a", "b"]
    assert fallback.trace[0].status == "warning"
