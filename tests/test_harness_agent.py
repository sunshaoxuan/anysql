from anysql.core.harness_models import EvidenceBundle
from anysql.harness.agentic_service import HarnessAgentService, build_evidence_bundles, build_intent_plan
from anysql.models.schemas import SQLAnalysis, SQLRecord, SQLStatement
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


def test_part_time_hire_date_restricts_to_djnd3001():
    plan = build_intent_plan('查所有姓“村田”的非常勤職員的入职日')
    assert plan.units[0].name == "part_time_employee_hire_date"
    evidence = [
        {
            "facet": "table_profile",
            "source_id": "DKIDO",
            "table": "DKIDO",
            "score": 0.98,
            "reasons": ["vector"],
            "meta": {"table": "DKIDO", "domain": "transfer", "role": "business_fact"},
        },
        {
            "facet": "table_profile",
            "source_id": "DJND3001",
            "table": "DJND3001",
            "score": 0.62,
            "reasons": ["profile"],
            "meta": {"table": "DJND3001", "domain": "employee", "role": "master"},
        },
        {
            "facet": "column_semantic",
            "source_id": "DJND3001.NINYO_DTE",
            "table": "DJND3001",
            "column": "NINYO_DTE",
            "score": 0.7,
            "meta": {"table": "DJND3001", "column": "NINYO_DTE", "domain": "employee", "role": "master"},
        },
    ]
    bundle = build_evidence_bundles(plan, evidence)[0]
    assert bundle.recommended_tables[0]["table"] == "DJND3001"
    assert all(row["table"] != "DKIDO" for row in bundle.recommended_tables)
    assert bundle.recommended_fields[0]["column"] == "NINYO_DTE"


def test_unknown_intent_blocks_dangerous_tables_from_context():
    plan = build_intent_plan("查询所有员工中有多子女在扶养中的员工")
    evidence = [
        {
            "facet": "table_profile",
            "source_id": "WK_BAD",
            "table": "WK_BAD",
            "score": 0.9,
            "meta": {"table": "WK_BAD", "domain": "employee", "role": "work"},
        },
        {
            "facet": "table_profile",
            "source_id": "EMP_FAMILY",
            "table": "EMP_FAMILY",
            "score": 0.7,
            "meta": {"table": "EMP_FAMILY", "domain": "employee", "role": "master"},
        },
    ]
    bundle = build_evidence_bundles(plan, evidence)[0]
    assert all(row["table"] != "WK_BAD" for row in bundle.recommended_tables)
    assert any(row["table"] == "WK_BAD" for row in bundle.blocked_candidates)


def test_unknown_intent_only_employee_number_is_invalid():
    service = HarnessAgentService.__new__(HarnessAgentService)
    plan = build_intent_plan("查询所有员工中有多子女在扶养中的员工")
    record = SQLRecord(
        statement=SQLStatement(id="draft", product="p", source_file="draft.sql", raw_sql="SELECT CSHAINNO FROM EMP_MASTER;", tables=["EMP_MASTER"]),
        analysis=SQLAnalysis(summary="x"),
    )
    bundle = EvidenceBundle(
        intent="unknown",
        recommended_tables=[{"table": "EMP_MASTER", "role": "master", "domain": "employee"}],
        recommended_fields=[{"table": "EMP_MASTER", "column": "CSHAINNO"}],
    )
    result = service.validate_record(record, plan, [bundle])
    assert not result.valid
    assert any("Dependent-child/support evidence" in error for error in result.errors)


def test_count_function_is_not_unknown_column():
    service = HarnessAgentService.__new__(HarnessAgentService)
    plan = build_intent_plan("查所有异动记录")
    record = SQLRecord(
        statement=SQLStatement(
            id="draft",
            product="p",
            source_file="draft.sql",
            raw_sql="SELECT CSHAINNO, COUNT(*) FROM DKIDO_R GROUP BY CSHAINNO;",
            tables=["DKIDO_R"],
        ),
        analysis=SQLAnalysis(summary="x"),
    )
    bundle = EvidenceBundle(
        intent="transfer_records",
        recommended_tables=[{"table": "DKIDO_R", "role": "history_fact", "domain": "transfer"}],
        recommended_fields=[{"table": "DKIDO_R", "column": "CSHAINNO"}],
    )
    result = service.validate_record(record, plan, [bundle])
    assert not any("COUNT" in error for error in result.errors)
