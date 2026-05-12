from anysql.core.table_profiles import infer_table_profile, is_table_allowed, policy_for_intent, resolve_intent


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
