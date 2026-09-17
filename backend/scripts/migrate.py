"""旧库升级：把 V1 的 app.db 就地升到 V2 结构。

**为什么是脚本而不是一份 .sql**

`SQLModel.metadata.create_all()` 只会建缺失的**表**，绝不会给已存在的表加列。
所以第二次部署必然撞 `no such column`——这是升级时最容易翻车的一步。

手写一份 `v1_to_v2.sql` 能解决这一次，但它从写下的那一刻就开始和
`models.py` 走散：以后每次改模型都得记得同步改 SQL，忘一次就是一次线上故障，
而且不会有任何测试报错。这个项目没有 Alembic（只有几百条数据，不值得引入），
所以更需要一个**从模型自己算出来**的升级方式——它不可能与模型不一致。

    python -m scripts.migrate            # 先看要改什么，不落盘
    python -m scripts.migrate --apply    # 备份后真的改

**任何情况下都会先做一次 VACUUM INTO 备份**，且备份失败就中止。
升级脚本没有"改到一半失败"这种可接受的结果。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import DATABASE_URL  # noqa: E402

# 导入模型模块，让 SQLModel.metadata 里真的有表。
from app import models  # noqa: E402,F401

DB_PATH = Path(DATABASE_URL.removeprefix("sqlite:///"))


class MigrationError(RuntimeError):
    """升级脚本自己发现的问题。消息面向运维，要说清"该怎么办"。"""

# 新增 NOT NULL 列时用什么值填既有行。
# SQLite 的 ALTER TABLE ADD COLUMN 要求 NOT NULL 列必须带默认值，
# 不给会直接报 "Cannot add a NOT NULL column with default value NULL"。
_FALLBACK_BY_TYPE = {
    "INTEGER": 0,
    "FLOAT": 0.0,
    "REAL": 0.0,
    "NUMERIC": 0.0,
    "BOOLEAN": 0,
    "VARCHAR": "",
    "TEXT": "",
    "DATETIME": None,   # 时间列一律允许为空，填一个假时间比留空更糟
    "DATE": None,
    "JSON": "[]",
}


def _sqlite_type(column) -> str:
    return column.type.compile(dialect=sqlite_dialect.dialect())


def _default_literal(column) -> str | None:
    """给新增的 NOT NULL 列算一个填充值；算不出来就返回 None（该列可空）。"""
    if column.nullable:
        return None

    default = column.default
    if default is not None and not callable(default.arg) and default.arg is not None:
        value = default.arg
        if isinstance(value, str):
            escaped = value.replace("'", "''")
            return f"'{escaped}'"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)

    fallback = _FALLBACK_BY_TYPE.get(_sqlite_type(column).split("(")[0].upper(), "")
    if fallback is None:
        return None
    if isinstance(fallback, str):
        return f"'{fallback}'"
    return str(fallback)


def _create_table_sql(table, name: str) -> str:
    """按模型生成建表语句，但用另一个表名。"""
    ddl = str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
    # 只替换表名本身（"CREATE TABLE" 之后那一个），
    # 表内 FOREIGN KEY 引用到的**其它**表名不能被改动。
    return ddl.replace(f"CREATE TABLE {table.name}", f"CREATE TABLE {name}", 1)


def _rebuild(table, current_columns: list[str], reasons: list[str]) -> list[str]:
    """SQLite 的 12 步改表法。

    **什么时候必须整表重建**：SQLite 不支持 `ALTER TABLE ... ALTER COLUMN`，
    所以"把 NOT NULL 去掉""改类型"这类变更只能靠 建新表→搬数据→换名。

    这不是理论问题：V1 的 `analysisjob.score` 是 `NOT NULL DEFAULT 0`，
    而 V2 要求它可空（没有证据时分数必须是空，不能是 0）。
    如果只 ADD COLUMN 而不重建，升级后的库上 `score = NULL` 会直接抛
    IntegrityError —— 也就是**不变量 #1 在升级过的库上根本无法满足**，
    而且只在升级路径上出现，全新安装永远测不到。
    """
    temporary = f"_v2_{table.name}"
    carried = [column.name for column in table.columns if column.name in current_columns]

    # 新增的 NOT NULL 列必须**显式给值**。
    # 模型里的 `Field(default="")` 是 Python 侧的默认值，SQLAlchemy 不会把它
    # 写成建表语句里的 SQL DEFAULT，所以新表那一列是光秃秃的 NOT NULL，
    # 不补值 INSERT 直接 IntegrityError。
    added_not_null: list[tuple[str, str]] = []
    for column in table.columns:
        if column.name in current_columns or column.nullable:
            continue
        literal = _default_literal(column)
        if literal is None:
            # 编一个假时间戳比报错更糟——那会变成一条没人知道是假的记录。
            raise MigrationError(
                f"{table.name}.{column.name} 是新增的 NOT NULL 列，且没有可用的默认值。"
                "无法自动回填，需要人工决定旧数据该填什么。"
            )
        added_not_null.append((column.name, literal))

    target_columns = ", ".join(
        [f'"{name}"' for name in carried] + [f'"{name}"' for name, _ in added_not_null]
    )
    select_columns = ", ".join(
        [f'"{name}"' for name in carried] + [literal for _, literal in added_not_null]
    )

    statements = [
        f"-- {table.name}：{('、'.join(reasons))} → 需要整表重建",
        # foreign_keys 是连接级开关，且**在事务内设置会被忽略**，
        # 所以必须放在 BEGIN 之前。
        "PRAGMA foreign_keys=OFF",
        "BEGIN",
        _create_table_sql(table, temporary),
        f'INSERT INTO "{temporary}" ({target_columns}) SELECT {select_columns} FROM "{table.name}"',
        f'DROP TABLE "{table.name}"',
        f'ALTER TABLE "{temporary}" RENAME TO "{table.name}"',
    ]

    # 索引是独立对象，DROP TABLE 会一并带走，必须重建。
    for index in table.indexes:
        columns = ", ".join(f'"{column.name}"' for column in index.columns)
        unique = "UNIQUE " if index.unique else ""
        statements.append(
            f'CREATE {unique}INDEX IF NOT EXISTS "{index.name}" ON "{table.name}" ({columns})'
        )

    statements += [
        # 重建期间关掉了外键检查，提交前必须自己验一遍，
        # 否则一次搬错就是一次静默的数据损坏。
        "PRAGMA foreign_key_check",
        "COMMIT",
        "PRAGMA foreign_keys=ON",
    ]
    return statements


def plan(connection: sqlite3.Connection) -> list[str]:
    """算出需要执行的语句。**只读**，不改任何东西。"""
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    statements: list[str] = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing:
            statements.append(f"-- 新表 {table.name}：由 create_all 建立")
            continue

        rows = connection.execute(f'PRAGMA table_info("{table.name}")').fetchall()
        current = {row[1]: row for row in rows}

        missing = [column for column in table.columns if column.name not in current]
        # PRAGMA table_info 的元组是 (cid, name, type, notnull, dflt_value, pk)，
        # 下标 3 就是 NOT NULL 标志。
        # 可空性不一致只能靠整表重建改——SQLite 没有 ALTER COLUMN。
        nullable_changed = [
            column.name
            for column in table.columns
            if column.name in current
            and bool(current[column.name][3]) != (not column.nullable)
        ]

        if nullable_changed:
            reasons = []
            if missing:
                reasons.append("新增列 " + "、".join(c.name for c in missing))
            reasons.append("可空性变化 " + "、".join(nullable_changed))
            statements += _rebuild(table, list(current), reasons)
            continue

        for column in missing:
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {_sqlite_type(column)}'
            literal = _default_literal(column)
            if not column.nullable:
                if literal is None:
                    # 不能给既有行编一个假值——留空让调用方显式处理，
                    # 而不是悄悄写入一个看起来正常的数据。
                    statements.append(
                        f"-- 注意：{table.name}.{column.name} 是 NOT NULL 但无默认值，"
                        f"已按可空列添加，需要人工回填"
                    )
                    statements.append(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {_sqlite_type(column)}'
                    )
                    continue
                clause += f" NOT NULL DEFAULT {literal}"
            statements.append(clause)

    return statements


def apply_statements(connection: sqlite3.Connection, statements: list[str]) -> None:
    """执行 plan() 产出的语句。

    单独抽出来是因为 sqlite3 的事务处理很坑：驱动默认会在 DML 前**隐式**
    开启事务，于是重建语句里那个显式的 `BEGIN` 会撞成
    "cannot start a transaction within a transaction"。
    把 `isolation_level` 置空（自动提交）后，事务边界完全由语句自己控制，
    12 步重建法才是它写出来的那个样子。**测试和 main() 必须走同一条路径**，
    否则测过的和跑起来的就不是一回事。
    """
    previous = connection.isolation_level
    connection.isolation_level = None
    try:
        for statement in statements:
            if statement.startswith("--"):
                continue
            connection.execute(statement)
    finally:
        connection.isolation_level = previous


def data_repairs(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """升级顺带要修的历史数据。

    **不变量 #1：失败的任务绝不允许有分数。**
    V1 的 `AnalysisJob.score` 默认是 0，且失败路径不重置，
    所以旧库里凡是 `status='failed'` 的行，score 字段都写着一个 0——
    在前端显示成"0 分"，等于无故指控学生没做。
    这不是显示问题，是数据本身错了，必须在升级时改掉。
    """
    return [
        (
            "把失败任务的分数清空（V1 会把失败留成 0 分）",
            "UPDATE analysisjob SET score = NULL WHERE status = 'failed' AND score IS NOT NULL",
        ),
        (
            "同步清空对应报告的分数",
            "UPDATE evaluationreport SET score = NULL WHERE job_id IN "
            "(SELECT id FROM analysisjob WHERE status = 'failed')",
        ),
        (
            "旧步骤记录没有三态判定，一律标为待人工确认而不是已完成",
            "UPDATE stepevent SET needs_review = 1 WHERE verdict IS NULL OR verdict = ''",
        ),
    ]


def backup() -> Path:
    """VACUUM INTO 做一份一致快照。

    **不能 `cp app.db`**：WAL 模式下有未落盘的 -wal 内容，
    复制出来的副本可能是损坏的，而"备份是坏的"这件事通常要到恢复时才发现。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = settings.backup_dir / f"pre-migrate-{stamp}.db"
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(str(DB_PATH))
    try:
        source.execute("VACUUM INTO ?", (str(target),))
    finally:
        source.close()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧库升级到当前模型结构")
    parser.add_argument("--apply", action="store_true", help="真的执行（默认只预览）")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"数据库不存在：{DB_PATH}")
        print("这是全新部署，直接启动服务即可（init_db 会建好全部表）。")
        return 0

    connection = sqlite3.connect(str(DB_PATH))
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        statements = plan(connection)
        repairs = data_repairs(connection)

        print(f"数据库：{DB_PATH}")
        print(f"\n=== 结构变更（{len(statements)} 项）===")
        for statement in statements:
            print(f"  {statement}")

        print(f"\n=== 数据修复（{len(repairs)} 项）===")
        for description, _ in repairs:
            print(f"  {description}")

        if not args.apply:
            print("\n以上为预览。确认无误后加 --apply 执行。")
            return 0

        if not statements and not repairs:
            print("\n无需变更。")
            return 0

        snapshot = backup()
        print(f"\n已备份到 {snapshot}")

        apply_statements(connection, statements)
        apply_statements(connection, [statement for _, statement in repairs])

        # 新表交给 create_all，和 plan 里写的保持一致。
        from app.database import init_db

        connection.close()
        init_db()
        print("升级完成。")
        return 0
    except Exception:
        print(f"\n升级失败。数据库已备份在 {settings.backup_dir}，可原样恢复。", file=sys.stderr)
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
