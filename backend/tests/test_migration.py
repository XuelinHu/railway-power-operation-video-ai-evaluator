"""旧库升级。

这条链路的失败方式很特别：**它在测试环境永远不出现**。
新装的环境跑 `create_all` 一次就把表建对了，只有"在旧库上原地升级"
才会撞上缺列。所以必须专门造一个 V1 形状的库来测。
"""

from __future__ import annotations

import sqlite3

from scripts.migrate import apply_statements, data_repairs, plan

# V1 的真实建表结构（取自初次提交的 models.py）。
# **必须忠实**：凭印象少写一列，升级脚本就会正确地拒绝执行，
# 而那个报错看起来像是脚本有 bug——实际是这份 DDL 不真实。
# 下面的 `test_schema_subset_of_models` 就是防这个的。
#
# 关键点：`score` 是 NOT NULL DEFAULT 0 —— 这正是"分析失败显示 0 分"的根源，
# 也是升级时必须整表重建（而不只是加列）的原因。
_V1_SCHEMA = """
CREATE TABLE analysisjob (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    score FLOAT NOT NULL DEFAULT 0,
    summary VARCHAR NOT NULL DEFAULT '',
    error_message VARCHAR NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    completed_at DATETIME
);
CREATE TABLE evaluationreport (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    score FLOAT NOT NULL DEFAULT 0,
    conclusion VARCHAR NOT NULL DEFAULT '',
    strengths VARCHAR NOT NULL DEFAULT '',
    problems VARCHAR NOT NULL DEFAULT '',
    suggestions VARCHAR NOT NULL DEFAULT '',
    generated_at DATETIME NOT NULL
);
CREATE TABLE stepevent (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    step_code VARCHAR NOT NULL DEFAULT '',
    step_name VARCHAR NOT NULL,
    start_sec FLOAT NOT NULL DEFAULT 0,
    end_sec FLOAT NOT NULL DEFAULT 0,
    confidence FLOAT NOT NULL DEFAULT 0,
    evidence VARCHAR NOT NULL DEFAULT ''
);
"""


def _old_db(tmp_path):
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(_V1_SCHEMA)
    # 一条失败的任务，V1 给它留了个 0 分。
    connection.execute(
        "INSERT INTO analysisjob (id, submission_id, status, score, created_at) "
        "VALUES (1, 1, 'failed', 0, '2026-01-01 10:00:00')"
    )
    # 一条正常完成的任务，分数必须原样保留。
    connection.execute(
        "INSERT INTO analysisjob (id, submission_id, status, score, created_at) "
        "VALUES (2, 2, 'completed', 80, '2026-01-01 11:00:00')"
    )
    connection.execute(
        "INSERT INTO evaluationreport (id, job_id, score, conclusion, generated_at) "
        "VALUES (1, 1, 0, '', '2026-01-01 10:00:05')"
    )
    connection.execute("INSERT INTO stepevent (id, job_id, step_name) VALUES (1, 2, '验电')")
    connection.commit()
    return connection


def test_schema_subset_of_models():
    """上面那份 V1 DDL 必须是当前模型的**真子集**。

    这是防"凭印象写 DDL"的护栏。少写一列时，升级脚本会因为
    "新增的 NOT NULL 列无法回填"而正当地拒绝执行——那个报错会被
    当成脚本的 bug 去查，而真正的问题在这份测试夹具里。
    多写一列则说明 DDL 记错了版本。
    """
    from sqlmodel import SQLModel

    from app import models  # noqa: F401

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_V1_SCHEMA)
        for table in SQLModel.metadata.sorted_tables:
            rows = connection.execute(f'PRAGMA table_info("{table.name}")').fetchall()
            if not rows:
                continue  # V1 没有的表（例如 user/rosterentry）不在此列
            v1_columns = {row[1] for row in rows}
            model_columns = {column.name for column in table.columns}
            assert v1_columns <= model_columns, (
                f"{table.name} 的 V1 DDL 里有当前模型没有的列："
                f"{sorted(v1_columns - model_columns)}"
            )
    finally:
        connection.close()


def _upgraded_db(tmp_path):
    """结构升级**之后**的连接。

    数据修复必须跑在结构升级之后：修复语句会碰 `verdict`/`needs_review`
    这些 V2 才有的列，顺序反了就是 "no such column"。
    这里保持和 `main()` 完全相同的顺序，测的才是真会跑的那条路径。
    """
    connection = _old_db(tmp_path)
    apply_statements(connection, plan(connection))
    return connection


class TestSchemaUpgrade:
    def test_missing_columns_are_added(self, tmp_path):
        """`create_all` 不会给已存在的表加列——这是升级最容易翻车的地方。"""
        connection = _old_db(tmp_path)
        try:
            apply_statements(connection, plan(connection))

            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(analysisjob)").fetchall()
            }
            # 挑几个 V2 新增且对运行至关重要的列。
            assert {"progress", "stage", "worker_id", "heartbeat_at", "progress_at",
                    "attempts", "provider", "error_code"} <= columns
        finally:
            connection.close()

    def test_plan_is_read_only(self, tmp_path):
        """预览模式绝不能改库——否则"先看看要改什么"本身就是一次事故。"""
        connection = _old_db(tmp_path)
        try:
            before = {
                row[1]
                for row in connection.execute("PRAGMA table_info(analysisjob)").fetchall()
            }
            plan(connection)
            plan(connection)
            after = {
                row[1]
                for row in connection.execute("PRAGMA table_info(analysisjob)").fetchall()
            }
            assert before == after
        finally:
            connection.close()

    def test_plan_is_idempotent(self, tmp_path):
        """跑第二遍不能报错，也不能重复加列。"""
        connection = _old_db(tmp_path)
        try:
            apply_statements(connection, plan(connection))
            second = [s for s in plan(connection) if not s.startswith("--")]
            assert second == []
        finally:
            connection.close()

    def test_not_null_column_gets_a_default(self, tmp_path):
        """SQLite 拒绝"给已有行加一个无默认值的 NOT NULL 列"。

        没处理这条会得到一个只说 "Cannot add a NOT NULL column" 的报错，
        而那时升级脚本通常已经改了一半。
        """
        connection = _old_db(tmp_path)
        try:
            apply_statements(connection, plan(connection))  # 不抛异常即为通过
        finally:
            connection.close()


class TestDataRepair:
    def test_failed_job_score_is_cleared(self, tmp_path):
        """**不变量 #1**：失败的任务不允许有分数。

        V1 把失败任务的 score 留成 0，前端显示"0 分"——
        学生看到的是"系统判我没做"，实际上系统根本没跑成功。
        这是数据错误，不是显示问题，必须在升级时清掉。
        """
        connection = _upgraded_db(tmp_path)
        try:
            apply_statements(connection, [stmt for _, stmt in data_repairs(connection)])

            failed_score = connection.execute(
                "SELECT score FROM analysisjob WHERE id = 1"
            ).fetchone()[0]
            assert failed_score is None
        finally:
            connection.close()

    def test_successful_job_score_is_preserved(self, tmp_path):
        """修复不能误伤正常数据。"""
        connection = _upgraded_db(tmp_path)
        try:
            apply_statements(connection, [stmt for _, stmt in data_repairs(connection)])

            assert connection.execute(
                "SELECT score FROM analysisjob WHERE id = 2"
            ).fetchone()[0] == 80
        finally:
            connection.close()

    def test_report_score_follows_its_job(self, tmp_path):
        """报告上的分数要和任务一起清，否则报告页仍显示 0 分。"""
        connection = _upgraded_db(tmp_path)
        try:
            apply_statements(connection, [stmt for _, stmt in data_repairs(connection)])

            assert connection.execute(
                "SELECT score FROM evaluationreport WHERE job_id = 1"
            ).fetchone()[0] is None
        finally:
            connection.close()

    def test_old_steps_are_flagged_for_review(self, tmp_path):
        """V1 没有三态判定。旧步骤一律标为待人工确认，
        而不是默认成"已完成"——那等于凭空发满分。"""
        connection = _upgraded_db(tmp_path)
        try:
            apply_statements(connection, [stmt for _, stmt in data_repairs(connection)])

            row = connection.execute(
                "SELECT needs_review FROM stepevent WHERE id = 1"
            ).fetchone()[0]
            assert row == 1
        finally:
            connection.close()
