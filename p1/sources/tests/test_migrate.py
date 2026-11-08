import pytest

from p1.sources.migrate import USERS, discover, new_password, plan, split_sql, user_sql


def test_split_handles_strings_comments_and_dollar_quotes():
    sql = """
    -- 주석 안의 ; 는 무시
    CREATE TABLE a (x TEXT DEFAULT 'a;b');
    /* 블록 ; 주석 */
    DO $$ BEGIN PERFORM 1; PERFORM 2; END $$;
    INSERT INTO t VALUES ('it''s; fine');
    SELECT `weird;name` FROM t
    """
    stmts = split_sql(sql)
    assert len(stmts) == 4
    assert stmts[1].startswith("DO $$") and stmts[1].endswith("END $$")
    assert "'it''s; fine'" in stmts[2]


def test_split_tagged_dollar_quote():
    stmts = split_sql("CREATE FUNCTION f() RETURNS int AS $fn$ SELECT 1; $fn$ LANGUAGE sql; SELECT 2;")
    assert len(stmts) == 2


@pytest.mark.parametrize("engine", ["mysql", "postgres"])
def test_repository_sql_files_parse(engine):
    migs = discover(engine)
    assert migs, "마이그레이션 파일이 있어야 한다"
    assert [m.version for m in migs] == sorted(m.version for m in migs)
    for m in migs:
        assert m.statements


def test_plan_detects_drift():
    migs = discover("mysql")
    applied = {migs[0].version: "0" * 64}
    with pytest.raises(RuntimeError, match="바뀌었다"):
        plan(migs, applied)


def test_plan_skips_applied():
    migs = discover("postgres")
    applied = {migs[0].version: migs[0].checksum}
    assert [m.version for m in plan(migs, applied)] == [m.version for m in migs[1:]]


def test_password_alphabet_is_connection_string_safe():
    pw = new_password()
    assert len(pw) == 32 and not set(pw) & set("'\"\\@/:;")


def test_debezium_mysql_has_no_lock_tables():
    stmts = user_sql("mysql", USERS["mysql"]["debezium"], new_password())
    joined = " ".join(stmts)
    assert "REPLICATION SLAVE" in joined and "LOCK TABLES" not in joined
    assert "REQUIRE SSL" in stmts[0]


def test_postgres_user_sql_rejects_unsafe_password():
    with pytest.raises(ValueError):
        user_sql("postgres", USERS["postgres"]["recon"], "a'; DROP ROLE x; --")
