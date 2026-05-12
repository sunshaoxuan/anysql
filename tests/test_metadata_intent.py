from anysql.storage.pgvector_engine import _employee_basic_table_score, _metadata_intent


def test_employee_basic_name_intent_is_detected_without_llm():
    assert _metadata_intent("查出所有姓“丰田”的人的基本信息") == "employee_basic_name"


def test_employee_basic_table_score_prefers_personal_master_over_payroll_work_tables():
    personal = _employee_basic_table_score("DJND0110", "個人基本情報DB")
    if_table = _employee_basic_table_score("DT_DJND0110", "就業IF用個人基本情報DB")
    payroll_history = _employee_basic_table_score("XKYYTKIHON_R", "給与基本情報累積")
    history = _employee_basic_table_score("DJND0110_IDO", "個人基本情報DB")
    work = _employee_basic_table_score("UWZAIKIHON", "財務会計用基本情報マスタワーク")
    savings = _employee_basic_table_score("DJND2001", "財形貯蓄基本情報DB")
    assert personal > if_table
    assert personal > payroll_history
    assert personal > history
    assert personal > work
    assert personal > savings
