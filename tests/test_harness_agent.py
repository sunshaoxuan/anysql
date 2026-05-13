from anysql.harness.agentic_service import build_evidence_bundles, build_intent_plan
from anysql.storage.rag_repository import _rrf_fuse


def test_intent_plan_preserves_name_and_all_record_conditions():
    plan = build_intent_plan("查出所有姓“丰田”的人的基本信息")
    assert plan.units[0].name == "employee_basic_name"
    assert {"kind": "name", "operator": "LIKE", "value": "丰田"} in plan.conditions
    assert {"kind": "all_records", "operator": "none", "value": None} in plan.conditions


def test_rrf_fusion_promotes_multi_evidence_candidate():
    vector = [
        {"node_id": "a", "source_id": "A", "score": 0.95, "weight": 1.0, "reasons": ["vector"]},
        {"node_id": "b", "source_id": "B", "score": 0.92, "weight": 1.0, "reasons": ["vector"]},
    ]
    text = [
        {"node_id": "b", "source_id": "B", "score": 0.8, "weight": 1.0, "reasons": ["text"]},
        {"node_id": "a", "source_id": "A", "score": 0.2, "weight": 1.0, "reasons": ["text"]},
    ]
    fused = _rrf_fuse([vector, text], top_k=2)
    assert fused[0]["source_id"] == "B"
    assert set(fused[0]["reasons"]) == {"vector", "text"}


def test_transfer_records_blocks_check_log_from_evidence_bundle():
    plan = build_intent_plan("查所有异动记录")
    evidence = [
        {
            "node_id": "1",
            "source_id": "XCIDOCHKLOG",
            "facet": "table_profile",
            "table": "XCIDOCHKLOG",
            "score": 1.0,
            "reasons": ["vector"],
            "meta": {"table": "XCIDOCHKLOG", "domain": "transfer", "role": "log"},
        },
        {
            "node_id": "2",
            "source_id": "DKIDO_R",
            "facet": "table_profile",
            "table": "DKIDO_R",
            "score": 0.9,
            "reasons": ["profile"],
            "meta": {"table": "DKIDO_R", "domain": "transfer", "role": "history_fact"},
        },
        {
            "node_id": "3",
            "source_id": "DKIDO_R.CSHAINNO",
            "facet": "column_semantic",
            "table": "DKIDO_R",
            "column": "CSHAINNO",
            "score": 0.8,
            "reasons": ["field"],
            "meta": {"table": "DKIDO_R", "column": "CSHAINNO", "domain": "transfer", "role": "history_fact"},
        },
    ]
    bundle = build_evidence_bundles(plan, evidence)[0]
    assert bundle.recommended_tables[0]["table"] == "DKIDO_R"
    assert all(item["table"] != "XCIDOCHKLOG" for item in bundle.recommended_tables)
    assert any(item["table"] == "XCIDOCHKLOG" for item in bundle.blocked_candidates)


def test_transfer_check_log_restricts_to_check_log_table():
    plan = build_intent_plan("查异动检查错误日志")
    evidence = [
        {
            "node_id": "1",
            "source_id": "CHANGELOG_DKIDO",
            "facet": "table_profile",
            "table": "CHANGELOG_DKIDO",
            "score": 10.0,
            "reasons": ["vector"],
            "meta": {"table": "CHANGELOG_DKIDO", "domain": "transfer", "role": "log"},
        },
        {
            "node_id": "2",
            "source_id": "XCIDOCHKLOG",
            "facet": "table_profile",
            "table": "XCIDOCHKLOG",
            "score": 0.8,
            "reasons": ["profile"],
            "meta": {"table": "XCIDOCHKLOG", "domain": "transfer", "role": "log"},
        },
        {
            "node_id": "3",
            "source_id": "XCIDOCHKLOG.CMSG",
            "facet": "column_semantic",
            "table": "XCIDOCHKLOG",
            "column": "CMSG",
            "score": 0.7,
            "reasons": ["field"],
            "meta": {"table": "XCIDOCHKLOG", "column": "CMSG", "domain": "transfer", "role": "log"},
        },
    ]
    bundle = build_evidence_bundles(plan, evidence)[0]
    assert bundle.recommended_tables[0]["table"] == "XCIDOCHKLOG"
    assert all(item["table"] == "XCIDOCHKLOG" for item in bundle.recommended_tables)
