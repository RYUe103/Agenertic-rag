from app.glossary import GlossaryIndex
from app.schemas import GlossaryDocument
from app.retrieval import RetrievalTools
from app.clients.dify import MockDifyClient
from test_agent import create_named_source
from test_agent import FileIntentLLM
from app.agent import AgentService
from app.schemas import ToolCall


def test_multiple_dictionaries_bidirectional_and_boundaries():
    documents = [GlossaryDocument(name="reactors.json", entries=[{
        "term": "PWR", "aliases": ["pressurized water reactor", "压水堆"], "data": {"专业": "核工程"},
    }]), GlossaryDocument(name="pumps.json", entries=[{"term": "pump", "aliases": ["泵"]}])]
    index = GlossaryIndex(documents)
    assert index.match("pwr泵相关纪要")[0]["term"] == "PWR"
    assert index.match("压水堆是什么")[0]["data"] == {"专业": "核工程"}
    assert index.match("pressurized water reactor")[0]["term"] == "PWR"
    assert not index.match("XPWR12")
    assert len(index.match("压水堆的pump")) == 2
    assert not index.match("核工程")


def test_disabled_and_ambiguous_dictionaries():
    index = GlossaryIndex([
        GlossaryDocument(name="a.json", entries=[{"term": "PWR", "aliases": ["压水堆"]}]),
        GlossaryDocument(name="b.json", entries=[{"term": "PWR", "aliases": ["其他义项"]}]),
        GlossaryDocument(name="off.json", enabled=False, entries=[{"term": "XYZ", "aliases": []}]),
    ])
    assert len(index.match("PWR")) == 2
    assert index.groups(["PWR", "泵"], index.match("PWR")) == [["PWR"], ["泵"]]
    assert not index.match("XYZ")


def test_automaton_handles_empty_overlap_and_duplicate_normalized_aliases():
    assert GlossaryIndex([]).match("PWR") == []
    index = GlossaryIndex([GlossaryDocument(name="test.json", entries=[
        {"term": "管道", "aliases": []},
        {"term": "管道等级表", "aliases": []},
        {"term": "等级表", "aliases": []},
        {"term": "PWR", "aliases": ["pwr", "压水堆"]},
    ])])
    assert index.match("") == []
    assert [item["term"] for item in index.match("请查管道等级表和pwr")] == ["管道", "管道等级表", "等级表", "PWR"]
    assert len(index.match("PWR与压水堆还有pwr")) == 1
    assert not index.match("XPWR和PWR_12")


def test_synonym_or_and_concept_intersection(repository, tmp_path):
    expected = create_named_source(repository, tmp_path, "会议.pdf", "压水堆的泵设计")
    create_named_source(repository, tmp_path, "其他A.pdf", "PWR reactor")
    create_named_source(repository, tmp_path, "其他B.pdf", "泵设计")
    tools = RetrievalTools(repository, MockDifyClient())
    groups = [["PWR", "压水堆"], ["泵"]]
    for results in [
        tools.grep_files(["PWR", "泵"], include_document_fields=True, match_mode="all", keyword_groups=groups),
        tools.keyword_search("PWR 泵", search_terms=["PWR", "泵"], keyword_groups=groups),
    ]:
        assert {item.source_id for item in results} == {expected.source_id}


def test_json_glossary_ask_never_calls_glossary_model(repository, tmp_path):
    expected = create_named_source(repository, tmp_path, "目标会议.pdf", "压水堆的泵设计")
    create_named_source(repository, tmp_path, "其他会议.pdf", "PWR reactor")

    class Planner(FileIntentLLM):
        def expand_query_with_glossary(self, *args):
            raise AssertionError("JSON 字典不得调用 LLM 做词典匹配")

        def plan(self, question, available_tools, planner_prompt=""):
            assert "reactors.json" in question
            return ToolCall(name="grep_files", arguments={
                "query": "PWR 泵", "keywords": ["PWR", "泵"], "intent": "list_documents",
                "match_mode": "all", "recency_requested": False,
            })

    service = AgentService(RetrievalTools(repository, MockDifyClient()), Planner(), glossary_documents=[
        GlossaryDocument(name="reactors.json", entries=[{"term": "PWR", "aliases": ["压水堆"]}]),
    ])
    result = service.ask("PWR泵相关纪要有哪些")
    assert "目标会议.pdf" in result.answer
    assert "其他会议.pdf" not in result.answer
    assert "reactors.json" not in result.answer
    assert service.llm.answer_calls == 0
