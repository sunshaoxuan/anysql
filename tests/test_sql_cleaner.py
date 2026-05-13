from anysql.core.sql_cleaner import add_sql_header_comment, clean_generated_sql, preserve_requirement_literal


def test_clean_generated_sql_decodes_entities_and_removes_escaped_quotes():
    sql = "SELECT * FROM MAST_EMPLOYEES WHERE ME_CKANJINAME LIKE &amp;#39;田中%&amp;#39;"
    assert clean_generated_sql(sql) == "SELECT * FROM MAST_EMPLOYEES WHERE ME_CKANJINAME LIKE '田中%';"


def test_clean_generated_sql_removes_non_ascii_column_aliases():
    sql = "SELECT ME_CEMPLOYEEID_CK AS 社员番号, ME_CKANJINAME AS kanji_name FROM MAST_EMPLOYEES"
    assert clean_generated_sql(sql) == "SELECT ME_CEMPLOYEEID_CK, ME_CKANJINAME FROM MAST_EMPLOYEES;"


def test_clean_generated_sql_keeps_ascii_aliases_when_enabled():
    sql = "SELECT ME_CEMPLOYEEID_CK AS emp_no, ME_CKANJINAME AS kanji_name FROM MAST_EMPLOYEES"
    assert clean_generated_sql(sql, allow_aliases=True) == (
        "SELECT ME_CEMPLOYEEID_CK AS emp_no, ME_CKANJINAME AS kanji_name FROM MAST_EMPLOYEES;"
    )


def test_clean_generated_sql_removes_implicit_non_ascii_column_aliases():
    sql = "SELECT CSHAINNO 職員番号,\n       CNAMEKNJ 漢字氏名\nFROM DJND0110"
    assert clean_generated_sql(sql) == "SELECT CSHAINNO,\n       CNAMEKNJ\nFROM DJND0110;"


def test_add_sql_header_comment_uses_japanese_multiline_template():
    commented = add_sql_header_comment("-- 中文注释\nSELECT * FROM MAST_EMPLOYEES", "中文概要", "中文需求")
    lines = commented.splitlines()
    assert lines[0] == "-- AnySQL: Metadata と既存知識をもとに生成したSQLです。"
    assert lines[1] == "-- 条件: 必要に応じてWHERE句の値を調整してください。"
    assert "中文" not in commented


def test_preserve_requirement_literal_replaces_broken_placeholder_like():
    sql = "SELECT TSYAMEI FROM XKSAGAKU WHERE TSYAMEI LIKE '[:1]%'"
    assert preserve_requirement_literal(sql, "查一下所有姓“丰田”的员工") == (
        "SELECT TSYAMEI FROM XKSAGAKU WHERE TSYAMEI LIKE '丰田%';"
    )


def test_preserve_requirement_literal_removes_name_filter_from_employee_number():
    sql = "SELECT TSYAKKANMEI FROM XKSAGAKU\nWHERE TSYAKKANMEI LIKE '%%'\n  OR CSHAINNO LIKE '%%'"
    assert preserve_requirement_literal(sql, "查一下所有姓“佐藤”的员工") == (
        "SELECT TSYAKKANMEI FROM XKSAGAKU\nWHERE TSYAKKANMEI LIKE '佐藤%';"
    )


def test_preserve_requirement_literal_inlines_name_parameter_and_removes_employee_number_parameter():
    sql = """SELECT CSHAINNO, CNAMEKNA_MEI
FROM WKDJNS6500_01
WHERE (:name IS NULL
       OR CNAMEKNA_MEI LIKE :name || '%')
  AND (:shainno IS NULL
       OR CSHAINNO = :shainno)"""
    assert preserve_requirement_literal(sql, "查一下所有姓“山田”的员工") == (
        "SELECT CSHAINNO, CNAMEKNA_MEI\nFROM WKDJNS6500_01\nWHERE CNAMEKNA_MEI LIKE '山田%';"
    )


def test_preserve_requirement_literal_removes_unrequested_generated_params():
    sql = """SELECT CSHAINNO, CNAMEKNJ, DBIRTH_DTE
FROM UWZAIKIHON
WHERE CNAMEKNJ LIKE '$_PARAM_NAME%'
  AND CMNCOMP = '$_PARAM_COMPANY'
  AND DMNDATE >= TO_DATE('$_PARAM_DATE', 'YYYY/MM/DD')"""
    assert preserve_requirement_literal(sql, "查一下所有姓“豊田”的员工") == (
        "SELECT CSHAINNO, CNAMEKNJ, DBIRTH_DTE\nFROM UWZAIKIHON\nWHERE CNAMEKNJ LIKE '豊田%';"
    )


def test_preserve_requirement_literal_overrides_llm_example_name():
    sql = "SELECT DISTINCT CSHAINNO, CNAMEKNJ FROM XKYTSIKYU WHERE CNAMEKNJ LIKE '山田%'"
    assert preserve_requirement_literal(sql, "查一下所有姓“丰田”的员工") == (
        "SELECT DISTINCT CSHAINNO, CNAMEKNJ FROM XKYTSIKYU WHERE CNAMEKNJ LIKE '丰田%';"
    )


def test_preserve_requirement_literal_removes_mixed_literal_and_parameter_like():
    sql = "SELECT CNAMEKNA_MEI FROM WKDJNS6500_01 WHERE CNAMEKNA_MEI LIKE '丰田%' || :name || '%'"
    assert preserve_requirement_literal(sql, "查一下所有姓“丰田”的员工") == (
        "SELECT CNAMEKNA_MEI FROM WKDJNS6500_01 WHERE CNAMEKNA_MEI LIKE '丰田%';"
    )


def test_preserve_requirement_literal_removes_leading_employee_number_or_condition():
    sql = """SELECT x.CSHAINNO, w.CNAMEKNJ
FROM XKYTSIKYU x
JOIN WPT_DJNP54106M_01 w ON x.CSHAINNO = w.CSHAINNO
WHERE x.CSHAINNO LIKE '123456%'
  OR w.CNAMEKNJ LIKE '丰田%'"""
    assert preserve_requirement_literal(sql, "查一下所有姓“丰田”的员工") == (
        "SELECT x.CSHAINNO, w.CNAMEKNJ\nFROM XKYTSIKYU x\nJOIN WPT_DJNP54106M_01 w ON x.CSHAINNO = w.CSHAINNO\nWHERE w.CNAMEKNJ LIKE '丰田%';"
    )


def test_preserve_requirement_literal_removes_trailing_employee_number_filter():
    sql = """SELECT CSHAINNO, CNAMEKNJ, CNAMEKNA
FROM XKYYTKIHON_R
WHERE (CNAMEKNJ LIKE '丰田%' OR CNAMEKNA LIKE '丰田%')
  AND CSHAINNO = '123456'"""
    assert preserve_requirement_literal(sql, "查出所有姓“丰田”的人的基本信息") == (
        "SELECT CSHAINNO, CNAMEKNJ, CNAMEKNA\nFROM XKYYTKIHON_R\nWHERE (CNAMEKNJ LIKE '丰田%' OR CNAMEKNA LIKE '丰田%');"
    )


def test_preserve_requirement_literal_replaces_quoted_name_parameter_pattern():
    sql = "SELECT CSHAINNO, CNAMEKNJ FROM DJND0110 WHERE CNAMEKNJ LIKE '':name_pattern'%'"
    assert preserve_requirement_literal(sql, "查出所有姓“丰田”的人的基本信息") == (
        "SELECT CSHAINNO, CNAMEKNJ FROM DJND0110 WHERE CNAMEKNJ LIKE '丰田%';"
    )


def test_preserve_requirement_literal_replaces_bare_name_parameter():
    sql = "SELECT CSHAINNO, CNAMEKNJ FROM DJND0110 WHERE CNAMEKNJ LIKE :name_pattern"
    assert preserve_requirement_literal(sql, "查出所有姓“丰田”的人的基本信息") == (
        "SELECT CSHAINNO, CNAMEKNJ FROM DJND0110 WHERE CNAMEKNJ LIKE '丰田%';"
    )


def test_preserve_requirement_literal_turns_surname_like_into_prefix_match():
    sql = "SELECT CSHAINNO, CNAMEKNJ, DNINYO_DTE FROM DJND3001 WHERE CNAMEKNJ LIKE '村田'"
    assert preserve_requirement_literal(sql, "查所有姓“村田”的非常勤職員的入职日") == (
        "SELECT CSHAINNO, CNAMEKNJ, DNINYO_DTE FROM DJND3001 WHERE CNAMEKNJ LIKE '村田%';"
    )


def test_preserve_requirement_literal_removes_unrequested_parameter_filter_for_all_records():
    sql = "SELECT CSHAINNO, HTREINGB_DTE FROM DKIDO_R WHERE CSHAINNO LIKE :1"
    assert preserve_requirement_literal(sql, "查所有的异动记录") == "SELECT CSHAINNO, HTREINGB_DTE FROM DKIDO_R;"


def test_preserve_requirement_literal_removes_generated_literal_filters_for_all_records():
    sql = "SELECT CSHAINNO, HTREINGB_DTE FROM DKIDO_R WHERE CSHAINNO = '123456' AND HTREINGB_DTE BETWEEN '202001' AND '202312'"
    assert preserve_requirement_literal(sql, "查所有的异动记录") == "SELECT CSHAINNO, HTREINGB_DTE FROM DKIDO_R;"
