from anysql.core.sql_cleaner import add_sql_header_comment, clean_generated_sql, preserve_requirement_literal


def test_clean_generated_sql_decodes_entities_and_removes_escaped_quotes():
    sql = "SELECT * FROM MAST_EMPLOYEES WHERE ME_CKANJINAME LIKE &amp;#39;松下%&amp;#39;"
    assert clean_generated_sql(sql) == "SELECT * FROM MAST_EMPLOYEES WHERE ME_CKANJINAME LIKE '松下%';"


def test_clean_generated_sql_removes_non_ascii_column_aliases():
    sql = "SELECT ME_CEMPLOYEEID_CK AS 社员番号, ME_CKANJINAME AS kanji_name FROM MAST_EMPLOYEES"
    assert clean_generated_sql(sql) == "SELECT ME_CEMPLOYEEID_CK, ME_CKANJINAME AS kanji_name FROM MAST_EMPLOYEES;"


def test_add_sql_header_comment_uses_japanese_multiline_template():
    commented = add_sql_header_comment("-- 中文注释\nSELECT * FROM MAST_EMPLOYEES", "中文概要", "中文需求")
    lines = commented.splitlines()
    assert lines[0] == "-- AnySQL: Metadata と既存知識をもとに生成したSQLです。"
    assert lines[1] == "-- 条件: 必要に応じてWHERE句の値を調整してください。"
    assert "中文" not in commented


def test_preserve_requirement_literal_replaces_broken_placeholder_like():
    sql = "SELECT TSYAMEI FROM XKSAGAKU WHERE TSYAMEI LIKE '[:1]%'"
    assert preserve_requirement_literal(sql, "查一下所有姓“松下”的员工") == (
        "SELECT TSYAMEI FROM XKSAGAKU WHERE TSYAMEI LIKE '松下%';"
    )


def test_preserve_requirement_literal_removes_name_filter_from_employee_number():
    sql = "SELECT TSYAKKANMEI FROM XKSAGAKU\nWHERE TSYAKKANMEI LIKE '%%'\n  OR CSHAINNO LIKE '%%'"
    assert preserve_requirement_literal(sql, "查一下所有姓“松下”的员工") == (
        "SELECT TSYAKKANMEI FROM XKSAGAKU\nWHERE TSYAKKANMEI LIKE '松下%';"
    )


def test_preserve_requirement_literal_inlines_name_parameter_and_removes_employee_number_parameter():
    sql = """SELECT CSHAINNO, CNAMEKNA_MEI
FROM WKDJNS6500_01
WHERE (:name IS NULL
       OR CNAMEKNA_MEI LIKE :name || '%')
  AND (:shainno IS NULL
       OR CSHAINNO = :shainno)"""
    assert preserve_requirement_literal(sql, "查一下所有姓“松下”的员工") == (
        "SELECT CSHAINNO, CNAMEKNA_MEI\nFROM WKDJNS6500_01\nWHERE CNAMEKNA_MEI LIKE '松下%';"
    )


def test_preserve_requirement_literal_removes_unrequested_generated_params():
    sql = """SELECT CSHAINNO, CNAMEKNJ, DBIRTH_DTE
FROM UWZAIKIHON
WHERE CNAMEKNJ LIKE '$_PARAM_NAME%'
  AND CMNCOMP = '$_PARAM_COMPANY'
  AND DMNDATE >= TO_DATE('$_PARAM_DATE', 'YYYY/MM/DD')"""
    assert preserve_requirement_literal(sql, "查一下所有姓“松下”的员工") == (
        "SELECT CSHAINNO, CNAMEKNJ, DBIRTH_DTE\nFROM UWZAIKIHON\nWHERE CNAMEKNJ LIKE '松下%';"
    )
