"""소스 DB 스키마 마이그레이션과 애플리케이션 계정 부트스트랩.

    # 운영 EC2 로 포트 포워딩한 뒤 노트북에서
    python -m p1.sources.migrate --engine mysql --host 127.0.0.1 --port 13306 --bootstrap-users
    # 또는 VPC 안(ECS 태스크, 생성기 이미지)에서 엔드포인트를 SSM 에서 읽어
    python -m p1.sources.migrate --engine postgres --bootstrap-users

원칙
- 적용 이력(schema_migrations)과 체크섬을 남긴다. 이미 적용한 파일이 바뀌면 멈춘다(드리프트).
- 애플리케이션 계정 암호는 여기서 만들고 Secrets Manager 에 바로 넣는다. 사람·Terraform state 를 거치지 않는다.
- 마스터 계정은 마이그레이션과 계정 생성에만 쓴다. 커넥터·생성기는 각자 최소 권한 계정을 쓴다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import string
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SQL_DIR = Path(__file__).parent / "sql"


# ---------------------------------------------------------------------------
# SQL 분할: 문자열·주석·달러 인용($$ ... $$) 안의 세미콜론은 문장 끝이 아니다
# ---------------------------------------------------------------------------
def split_sql(text: str) -> list[str]:
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    dollar: str | None = None
    while i < n:
        ch = text[i]
        if dollar:
            if text.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == "\\" and quote != "`" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:  # '' 이스케이프
                    buf.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            buf.append("\n")
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            j = text.find("$", i + 1)
            tag = text[i : j + 1] if j != -1 else ""
            if tag and all(c.isalnum() or c == "_" for c in tag[1:-1]):
                dollar = tag
                buf.append(tag)
                i = j + 1
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str

    @property
    def statements(self) -> list[str]:
        return split_sql(self.path.read_text(encoding="utf-8"))


def discover(engine: str, sql_dir: Path = SQL_DIR) -> list[Migration]:
    out = []
    for p in sorted((sql_dir / engine).glob("*.sql")):
        out.append(Migration(p.stem, p, hashlib.sha256(p.read_bytes()).hexdigest()))
    return out


def plan(migrations: Iterable[Migration], applied: dict[str, str]) -> list[Migration]:
    """적용할 목록. 이미 적용된 파일의 체크섬이 다르면 예외."""
    todo = []
    for m in migrations:
        if m.version in applied:
            if applied[m.version] != m.checksum:
                raise RuntimeError(
                    f"{m.version} 은 이미 적용됐는데 파일이 바뀌었다. 기존 파일을 고치지 말고 새 번호로 추가한다"
                )
            continue
        todo.append(m)
    return todo


# ---------------------------------------------------------------------------
# 애플리케이션 계정
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DbUser:
    name: str
    grants: tuple[str, ...]


USERS: dict[str, dict[str, DbUser]] = {
    "mysql": {
        "debezium": DbUser(
            "debezium",
            (
                # LOCK TABLES 는 주지 않는다: snapshot.locking.mode=none + GTID 로 락 없이 스냅샷
                "GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{u}'@'%'",
                "GRANT INSERT, DELETE ON commerce.debezium_signal TO '{u}'@'%'",
            ),
        ),
        "generator": DbUser(
            "generator", ("GRANT SELECT, INSERT, UPDATE, DELETE ON commerce.* TO '{u}'@'%'",)
        ),
        "recon": DbUser("recon", ("GRANT SELECT ON commerce.* TO '{u}'@'%'",)),
    },
    "postgres": {
        "debezium": DbUser(
            "debezium",
            (
                "GRANT rds_replication TO {u}",
                "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {u}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {u}",
                "GRANT INSERT, UPDATE ON debezium_heartbeat TO {u}",
                "GRANT INSERT, DELETE ON debezium_signal TO {u}",
            ),
        ),
        "generator": DbUser(
            "generator",
            (
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {u}",
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {u}",
            ),
        ),
        "recon": DbUser("recon", ("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {u}",)),
        # 계보 메타데이터(Marquez)는 결제 DB 인스턴스의 별도 데이터베이스에 둔다(dev 비용 절충, ADR P1-08 대체).
        # 결제 테이블 권한은 없고 자기 DB 만 소유한다
        "marquez": DbUser("marquez", ("ALTER DATABASE marquez OWNER TO {u}",)),
    },
}

# 트랜잭션 안에서 실행할 수 없는 문장(CREATE DATABASE)은 존재 여부를 확인하고 따로 실행한다
EXTRA_DATABASES = {"postgres": ["marquez"]}

_PW_ALPHABET = string.ascii_letters + string.digits + "-_"


def new_password(length: int = 32) -> str:
    # 따옴표·백슬래시·@·/ 를 빼서 연결 문자열·JAAS·커넥터 설정에 안전하게 넣을 수 있게 한다
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def user_sql(engine: str, user: DbUser, password: str) -> list[str]:
    if any(c not in _PW_ALPHABET for c in password):
        raise ValueError("암호에 허용되지 않은 문자")
    u = user.name
    if engine == "mysql":
        create = [
            f"CREATE USER IF NOT EXISTS '{u}'@'%' IDENTIFIED BY '{password}' REQUIRE SSL",
            f"ALTER USER '{u}'@'%' IDENTIFIED BY '{password}'",
        ]
    else:
        create = [
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{u}') "
            f"THEN CREATE ROLE {u} LOGIN; END IF; END $$",
            f"ALTER ROLE {u} WITH LOGIN PASSWORD '{password}'",
        ]
    return create + [g.format(u=u) for g in user.grants]


# ---------------------------------------------------------------------------
# 실행부 (DB 드라이버·boto3 는 실행할 때만 import)
# ---------------------------------------------------------------------------
TRACKING_DDL = {
    "mysql": "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(64) PRIMARY KEY, "
    "checksum CHAR(64) NOT NULL, applied_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3))",
    "postgres": "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(64) PRIMARY KEY, "
    "checksum CHAR(64) NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
}


def _connect(engine: str, host: str, port: int, user: str, password: str, dbname: str):
    if engine == "mysql":
        import pymysql

        return pymysql.connect(
            host=host, port=port, user=user, password=password, database=dbname,
            ssl={"ssl": {}}, autocommit=True, charset="utf8mb4",
        )  # fmt: skip
    import psycopg

    return psycopg.connect(
        host=host, port=port, user=user, password=password, dbname=dbname, sslmode="require", autocommit=True
    )


def run(args: argparse.Namespace) -> None:
    import boto3

    from danbi_common.aws import get_parameter

    prefix = f"/{args.project}/{args.env}/p1/sources"
    key = "mysql" if args.engine == "mysql" else "pg"
    endpoint = get_parameter(f"{prefix}/{key}_endpoint")
    host = args.host or endpoint
    real_port = int(get_parameter(f"{prefix}/{key}_port"))
    port = args.port or real_port
    dbname = "commerce" if args.engine == "mysql" else "payment"

    sm = boto3.client("secretsmanager")
    master = json.loads(
        sm.get_secret_value(SecretId=get_parameter(f"{prefix}/{key}_master_secret_arn"))["SecretString"]
    )
    conn = _connect(args.engine, host, port, master["username"], master["password"], dbname)
    cur = conn.cursor()
    cur.execute(TRACKING_DDL[args.engine])
    cur.execute("SELECT version, checksum FROM schema_migrations")
    applied = {v: c for v, c in cur.fetchall()}

    for m in plan(discover(args.engine), applied):
        print(f"apply {args.engine}/{m.version}")
        for stmt in m.statements:
            cur.execute(stmt)
        ph = "%s"
        cur.execute(
            f"INSERT INTO schema_migrations (version, checksum) VALUES ({ph}, {ph})", (m.version, m.checksum)
        )

    for db in EXTRA_DATABASES.get(args.engine, []):
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE DATABASE {db}")
            print(f"create database {db}")

    if args.bootstrap_users:
        secret_prefix = get_parameter(f"{prefix}/db_user_secret_prefix")
        for name, user in USERS[args.engine].items():
            secret_id = f"{secret_prefix}{args.engine}/{name}"
            password = new_password()
            for stmt in user_sql(args.engine, user, password):
                cur.execute(stmt)
            sm.put_secret_value(
                SecretId=secret_id,
                SecretString=json.dumps(
                    {
                        "engine": args.engine,
                        "host": endpoint,  # 포트 포워딩 주소가 아니라 VPC 안에서 쓰는 실제 엔드포인트
                        "port": real_port,
                        "dbname": dbname,
                        "username": user.name,
                        "password": password,
                    }
                ),
            )
            print(f"user {name}: 암호 회전 완료 → {secret_id}")
    conn.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--engine", choices=["mysql", "postgres"], required=True)
    p.add_argument("--project", default="danbi")
    p.add_argument("--env", default="dev")
    p.add_argument("--host", help="포트 포워딩 시 127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--bootstrap-users", action="store_true", help="애플리케이션 계정 생성·암호 회전")
    p.add_argument("--dry-run", action="store_true", help="적용할 파일과 문장 수만 출력")
    args = p.parse_args(argv)
    if args.dry_run:
        for m in discover(args.engine):
            print(m.version, len(m.statements), "statements", m.checksum[:12])
        return
    run(args)


if __name__ == "__main__":
    main()
