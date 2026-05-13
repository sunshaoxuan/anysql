from anysql.core.table_profiles import infer_table_profile, is_table_allowed, policy_for_intent, resolve_intent
from anysql.core.table_profile_classifier import classify_table_profile
from anysql.storage.repositories import _profile_review_priority


def test_profile_infers_transfer_check_log():
    profile = infer_table_profile("XCIDOCHKLOG", "異動データチェックログ", [{"column_name": "CERRVALUE"}, {"column_name": "CMSG"}])
    assert profile.domain == "transfer"
    assert profile.role == "log"


def test_profile_infers_transfer_fact_and_history():
    assert infer_table_profile("DKIDO", "異動情報").role == "business_fact"
    assert infer_table_profile("DKIDO_R", "異動情報累積").role == "history_fact"


def test_profile_infers_employee_master():
    profile = infer_table_profile("DJND0110", "個人基本情報DB", [{"column_name": "CNAMEKNJ", "comment": "漢字氏名"}])
    assert profile.domain == "employee"
    assert profile.role == "master"


def test_profile_infers_part_time_employee_master_not_transfer():
    profile = infer_table_profile(
        "DJND3001",
        "非常勤職員基本情報DB 20030711 idx_2作成",
        [
            {"column_name": "CNAMEKNJ", "comment": "漢字氏名"},
            {"column_name": "HJKSYK_NNMN_CDE", "comment": "非常勤職員任免コード"},
            {"column_name": "DHTREI_SYKIN_DTE", "comment": "発令年月日（職員区分）"},
        ],
    )
    assert profile.domain == "employee"
    assert profile.role == "master"


def test_profile_classifier_uses_llm_for_conflicting_evidence():
    class FakeLLM:
        async def chat_json(self, prompt, system_prompt="", temperature=None):
            assert "DJND3001" in prompt
            return {
                "domain": "employee",
                "role": "master",
                "confidence": 0.93,
                "reason": "非常勤職員基本情報DB and name fields indicate part-time employee master data.",
            }

    import asyncio

    profile = asyncio.run(
        classify_table_profile(
            "DJND3001",
            "非常勤職員基本情報DB 20030711 idx_2作成",
            [
                {"column_name": "CNAMEKNJ", "comment": "漢字氏名"},
                {"column_name": "HJKSYK_NNMN_CDE", "comment": "非常勤職員任免コード"},
                {"column_name": "DHTREI_SYKIN_DTE", "comment": "発令年月日（職員区分）"},
            ],
            FakeLLM(),
        )
    )
    assert profile.domain == "employee"
    assert profile.role == "master"
    assert profile.source == "auto_llm"


def test_profile_review_prioritizes_part_time_basic_tables():
    table = type("T", (), {"table_name": "DJND3001", "comment": "非常勤職員基本情報DB"})()
    low = type("T", (), {"table_name": "ADIF1050", "comment": "例外予算事項"})()
    assert _profile_review_priority(table) > _profile_review_priority(low)


def test_employee_basic_policy_blocks_dangerous_roles():
    policy = policy_for_intent(resolve_intent("查出所有姓“丰田”的人的基本信息"))
    assert policy is not None
    assert "log" in policy.blocked_roles
    assert "work" in policy.blocked_roles
    assert "if_staging" in policy.blocked_roles
    assert "backup" in policy.blocked_roles


def test_transfer_records_policy_blocks_check_log():
    policy = policy_for_intent(resolve_intent("查所有异动记录"))
    assert policy is not None
    log_profile = type("P", (), {"role": "log"})()
    assert not is_table_allowed(log_profile, policy)
    assert policy.preferred_tables[:2] == ("DKIDO_R", "DKIDO")


def test_transfer_check_log_policy_prefers_xcidochklog():
    policy = policy_for_intent(resolve_intent("查异动检查错误日志"))
    assert policy is not None
    assert policy.allowed_roles == ("log",)
    assert policy.preferred_tables == ("XCIDOCHKLOG",)
