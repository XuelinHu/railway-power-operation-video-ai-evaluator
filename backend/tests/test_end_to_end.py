"""端到端：建任务 → 导名册 → 学生上传 → 分析 → 复核 → 导出。

这条链路每断一处，症状都是"系统看起来在正常工作但结果是错的"，
所以断言写得比较死——尤其是涉及**分数**和**可见性**的地方。

分析用 `AI_PROVIDER=fake`，它刻意产出"部分完成、部分不可见"的形状，
所以这里测到的是三态语义，而不只是"全对"这一条路径。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.models import AnalysisJob, AnalysisStatus, AuditLog, StepEvent, UserRole, Violation, ViolationStatus

from .conftest import login, make_user


def _task(teacher: TestClient, title: str = "停电检修作业") -> int:
    response = teacher.post(
        "/api/tasks",
        json={"title": title, "course": "铁道供电", "class_name": "供电2301", "description": ""},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _upload(student: TestClient, video: Path, task_id: int):
    with video.open("rb") as handle:
        return student.post(
            "/api/submissions",
            data={"task_id": str(task_id)},
            files={"file": ("作业录像.mp4", handle, "video/mp4")},
        )


def _run_worker_once() -> None:
    """跑一次 worker 的处理逻辑（不启动常驻循环）。

    直接调 `claim_next` + `process` 而不是起子进程：测试要断言的是
    **认领与处理的语义**，而"systemd 能不能拉起进程"是部署验收的事。
    """
    from app.services import jobs
    from app.worker import claim_next, release

    job_id = claim_next("test-worker")
    if job_id is None:
        return
    try:
        jobs.process(job_id, "test-worker")
    finally:
        release(job_id)


def _run_worker_until(target_job_id: int) -> None:
    """跑到指定任务被处理过为止。

    worker 取的是**最老的** pending，而同一个库里还留着别的测试没跑完的任务，
    所以"第一次认领就是我要的那个"是不成立的。断言认领顺序等于让用例
    依赖测试的执行顺序——这类偶发失败最难查。
    """
    from app.services import jobs
    from app.worker import claim_next, release

    for _ in range(50):
        claimed = claim_next("failure-test")
        if claimed is None:
            return
        try:
            jobs.process(claimed, "failure-test")
        finally:
            release(claimed)
        if claimed == target_job_id:
            return
    raise AssertionError("队列里积压的任务过多，目标任务始终没被认领到")


class TestFullFlow:
    def test_student_upload_and_review_flow(self, app_client, admin, sample_video):
        teacher = admin
        make_user("s2023001", "Student123", UserRole.student,
                  display_name="张三", student_no="2023001", class_name="供电2301")
        student = TestClient(app_client.app)
        assert login(student, "s2023001", "Student123").status_code == 200

        task_id = _task(teacher)

        # --- 名册 ---------------------------------------------------------
        response = teacher.post(
            "/api/roster/import",
            json={
                "task_id": task_id,
                "text": "学号,姓名\n2023001,张三\n2023002,李四\n",
                "create_accounts": False,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["created_entries"] == 2

        # --- 上传 ---------------------------------------------------------
        response = _upload(student, sample_video, task_id)
        assert response.status_code == 200, response.text
        body = response.json()
        # 姓名学号取自信名册，**不采信表单**——这是防冒名提交的关键。
        assert body["student_no"] == "2023001"
        assert body["student_name"] == "张三"
        assert body["job"]["status"] == "pending"

        # --- 分析 ---------------------------------------------------------
        _run_worker_once()

        with Session(engine) as session:
            job = session.exec(select(AnalysisJob)).first()
            assert job.status == AnalysisStatus.completed
            # fake 提供方：voltage_test 判为 not_completed（扣 20 分），
            # ticket 和 review 判为 not_visible（不扣分）。
            assert job.score == 80.0
            assert job.provider == "fake"
            assert job.worker_id == ""

            steps = {
                step.step_code: step.verdict
                for step in session.exec(select(StepEvent).where(StepEvent.job_id == job.id)).all()
            }
            assert steps["voltage_test"] == "not_completed"
            assert steps["ticket"] == "not_visible"
            assert len(steps) == 9  # 缺的步骤会被补成 not_visible，不能少

            job_id = job.id

        # --- 学生能看到自己的报告 -----------------------------------------
        detail = student.get(f"/api/analysis/jobs/{job_id}/detail").json()
        assert detail["report"]["score"] == 80.0
        # 未复核时对学生的口径必须是"待复核"，不能直接给机器分当终分。
        assert detail["report"]["review_status"] in {"pending", "needs_review"}
        # 未复核时学生看到的每一个数字都必须带"这不是终分"的口径。
        assert "仅供参考" in detail["disclaimer"]
        assert "教师复核" in detail["disclaimer"]

        # --- 证据帧必须真的存在、且能取回来 -------------------------------
        # 这条断言挡的是一个真实出现过的缺陷：提供方在报告里挂上帧号，
        # 磁盘上却一帧都没有，教师点开缩略图就是 404——而"每个判断都能
        # 点开看"正是这套系统相对"黑箱给个分"的全部卖点。
        cited = {
            index
            for step in detail["steps"]
            for index in step.get("evidence_frames", [])
        }
        assert cited, "报告里没有任何证据帧，证据链是空的"

        submission_id = body["id"]
        for index in sorted(cited):
            response = student.get(f"/api/submissions/{submission_id}/frames/{index}")
            assert response.status_code == 200, f"证据帧 {index} 取不回来：{response.text}"
            # 必须是真 JPEG，而不是一个恰好存在的空文件。
            assert response.content[:2] == b"\xff\xd8", f"帧 {index} 不是 JPEG"

        # 看不见的步骤不许挂证据——挂了就等于在编造"我看到了"。
        for step in detail["steps"]:
            if step["verdict"] == "not_visible":
                assert not step.get("evidence_frames"), f'{step["step_code"]} 不可见却有证据帧'

        # --- 教师复核：驳回那条唯一的扣分 ---------------------------------
        with Session(engine) as session:
            violation = session.exec(select(Violation).where(Violation.job_id == job_id)).first()
            assert violation is not None
            violation_id = violation.id
            assert violation.status == ViolationStatus.auto

        response = teacher.post(
            f"/api/reviews/jobs/{job_id}/violations/{violation_id}",
            json={"action": "dismiss", "comment": "验电动作在画面外，属误判"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["final_score"] == 100.0

        # --- 终审 ---------------------------------------------------------
        response = teacher.post(
            f"/api/reviews/jobs/{job_id}/finalize", json={"comment": "已核对视频"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["final_score"] == 100.0
        assert response.json()["review_status"] == "confirmed"

        # --- 终审后不能再重跑 ---------------------------------------------
        assert teacher.post(f"/api/analysis/jobs/{job_id}/rerun").status_code == 409

        # --- 审计留痕 -----------------------------------------------------
        with Session(engine) as session:
            actions = {
                row.action
                for row in session.exec(select(AuditLog)).all()
            }
        assert {"create_task", "import_roster", "upload_submission",
                "review_violation", "finalize_review"} <= actions

        # --- 导出 ---------------------------------------------------------
        response = teacher.get(f"/api/reviews/export.csv?task_id={task_id}")
        assert response.status_code == 200
        # utf-8-sig：不带 BOM 的话，教务老师用 Excel 打开全是乱码。
        assert response.content.startswith(b"\xef\xbb\xbf")
        text = response.content.decode("utf-8-sig")
        assert "2023001" in text and "张三" in text
        assert "100" in text and "已终审" in text

    def test_second_upload_is_rejected(self, app_client, admin, sample_video):
        """重复提交必须被拒。放着不管的话，学生连点三次就是三份 API 费用。"""
        teacher = admin
        make_user("s2023010", "Student123", UserRole.student,
                  display_name="王五", student_no="2023010")
        student = TestClient(app_client.app)
        login(student, "s2023010", "Student123")

        task_id = _task(teacher, "重复提交测试")
        teacher.post("/api/roster/import", json={
            "task_id": task_id, "text": "2023010,王五", "create_accounts": False,
        })

        assert _upload(student, sample_video, task_id).status_code == 200
        duplicate = _upload(student, sample_video, task_id)
        assert duplicate.status_code == 409
        assert "已经提交过" in duplicate.json()["detail"]

    def test_student_outside_roster_cannot_upload(self, app_client, admin, sample_video):
        teacher = admin
        make_user("s2023099", "Student123", UserRole.student,
                  display_name="赵六", student_no="2023099")
        student = TestClient(app_client.app)
        login(student, "s2023099", "Student123")

        task_id = _task(teacher, "名单外学生")
        teacher.post("/api/roster/import", json={
            "task_id": task_id, "text": "2023001,张三", "create_accounts": False,
        })

        response = _upload(student, sample_video, task_id)
        assert response.status_code == 403
        assert "名单" in response.json()["detail"]


class TestVisibility:
    """越权访问是 V1 最严重的问题：任何登录用户都能遍历全班数据。"""

    def test_unauthenticated_requests_are_rejected(self, app_client):
        for path in [
            "/api/tasks",
            "/api/submissions",
            "/api/analysis/jobs",
            "/api/catalog/steps",
            "/api/catalog/rules",
            "/api/catalog/knowledge",
            "/api/users",
            "/api/reviews/queue",
            "/api/roster/1",
        ]:
            assert app_client.get(path).status_code == 401, path

    def test_students_cannot_see_each_other(self, app_client, admin, sample_video):
        teacher = admin
        make_user("s001", "Student123", UserRole.student, display_name="甲", student_no="001")
        make_user("s002", "Student123", UserRole.student, display_name="乙", student_no="002")

        task_id = _task(teacher, "可见性测试")
        teacher.post("/api/roster/import", json={
            "task_id": task_id, "text": "001,甲\n002,乙", "create_accounts": False,
        })

        first = TestClient(app_client.app)
        login(first, "s001", "Student123")
        assert _upload(first, sample_video, task_id).status_code == 200

        second = TestClient(app_client.app)
        login(second, "s002", "Student123")

        # 乙在列表里看不到甲
        assert second.get("/api/submissions").json() == []

        # 乙直接按 id 访问甲的视频：必须 404 而不是 403。
        # 403 等于确认"这个 id 存在但不属于你"，遍历 id 就能统计出提交量。
        assert second.get("/api/submissions/1/video").status_code == 404
        assert second.get("/api/submissions/1/frames").status_code == 404

    def test_students_cannot_reach_teacher_endpoints(self, app_client, admin):
        make_user("s003", "Student123", UserRole.student, display_name="丙", student_no="003")
        student = TestClient(app_client.app)
        login(student, "s003", "Student123")

        assert student.get("/api/reviews/queue").status_code == 403
        assert student.get("/api/users").status_code == 403
        assert student.get("/api/analysis/stats/overview").status_code == 403
        assert student.post("/api/tasks", json={
            "title": "越权建任务", "course": "x", "class_name": "y",
        }).status_code == 403


class TestUploadValidation:
    def test_disguised_text_file_is_rejected(self, app_client, admin, tmp_path):
        """把 .txt 改名成 .mp4 必须被拒。

        判据是 ffprobe 能不能解出时长，不是看扩展名——
        看扩展名的校验，改个名就绕过去了。
        """
        teacher = admin
        make_user("s004", "Student123", UserRole.student, display_name="丁", student_no="004")
        student = TestClient(app_client.app)
        login(student, "s004", "Student123")
        task_id = _task(teacher, "伪装文件")
        teacher.post("/api/roster/import", json={
            "task_id": task_id, "text": "004,丁", "create_accounts": False,
        })

        fake = tmp_path / "fake.mp4"
        fake.write_bytes("这不是视频，只是一段文本。".encode() * 100)

        response = student.post(
            "/api/submissions",
            data={"task_id": str(task_id)},
            files={"file": ("fake.mp4", fake.open("rb"), "video/mp4")},
        )
        assert response.status_code == 400
        assert "视频" in response.json()["detail"]

    def test_unsupported_extension_is_rejected_early(self, app_client, admin, tmp_path):
        teacher = admin
        make_user("s005", "Student123", UserRole.student, display_name="戊", student_no="005")
        student = TestClient(app_client.app)
        login(student, "s005", "Student123")
        task_id = _task(teacher, "扩展名校验")
        teacher.post("/api/roster/import", json={
            "task_id": task_id, "text": "005,戊", "create_accounts": False,
        })

        bogus = tmp_path / "payload.exe"
        bogus.write_bytes(b"MZ" + b"\x00" * 1000)
        response = student.post(
            "/api/submissions",
            data={"task_id": str(task_id)},
            files={"file": ("payload.exe", bogus.open("rb"), "application/octet-stream")},
        )
        assert response.status_code == 400
        assert "格式" in response.json()["detail"]


class TestFailurePaths:
    def test_failed_job_never_has_a_score(self, app_client, admin, db):
        """不变量 #1：失败绝不产生分数。

        V1 里 score 默认 0 且失败路径不重置，于是分析失败的作业
        在前端显示"0 分"——等于无故指控学生。
        """
        from app.clock import now
        from app.models import VideoSubmission

        # 存储文件不存在 → worker 的转码阶段会失败。
        # 这模拟的是"磁盘被清理掉、或上传目录挂了"这类真实故障。
        task_id = _task(admin, "文件缺失")
        with Session(engine) as session:
            submission = VideoSubmission(
                task_id=task_id, student_name="测试", student_no="999",
                original_filename="missing.mp4", stored_filename="does-not-exist.mp4",
            )
            session.add(submission)
            session.commit()
            session.refresh(submission)
            job = AnalysisJob(submission_id=submission.id)
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id
            session.expunge(job)

        _run_worker_until(job_id)

        with Session(engine) as session:
            job = session.get(AnalysisJob, job_id)
            assert job.status == AnalysisStatus.failed
            assert job.score is None
            # 失败原因必须是脱敏的教师可读中文，不能带路径。
            assert job.error_code
            assert "/" not in job.error_code
            assert "Traceback" not in job.error_code
        assert now() is not None

    def test_health_reports_degraded_when_worker_is_gone(self, app_client):
        from app.worker import HEARTBEAT_FILE

        HEARTBEAT_FILE.unlink(missing_ok=True)
        response = app_client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["checks"]["worker"] == "不可用"

    def test_health_is_ok_when_worker_is_alive(self, app_client):
        from app.clock import now
        from app.worker import HEARTBEAT_FILE

        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(now().isoformat(), encoding="utf-8")

        response = app_client.get("/api/health")
        assert response.status_code == 200, response.text
        assert response.json()["checks"]["ffmpeg"] == "ok"
        assert response.json()["checks"]["ai_provider"] == "fake"
