from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def main() -> None:
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200, health.text

        steps = client.get("/api/catalog/steps")
        assert steps.status_code == 200, steps.text
        assert len(steps.json()) >= 9

        task_payload = {
            "title": "冒烟测试-接触网停电验电接地",
            "course": "铁道供电安全实训",
            "class_name": "冒烟测试班",
            "teacher": "系统测试",
            "description": "用于验证创建任务、上传视频、自动分析和报告生成。",
        }
        task = client.post("/api/tasks", json=task_payload)
        assert task.status_code == 200, task.text
        task_id = task.json()["id"]

        sample = Path("data/uploads/smoke-no-glove.txt")
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("fake video bytes for smoke test", encoding="utf-8")

        with sample.open("rb") as file:
            upload = client.post(
                "/api/submissions",
                data={"task_id": str(task_id), "student_name": "冒烟学生", "student_no": "SMOKE001"},
                files={"file": ("smoke-no-glove.mp4", file, "video/mp4")},
            )
        assert upload.status_code == 200, upload.text
        job_id = upload.json()["job"]["id"]

        detail = client.get(f"/api/analysis/jobs/{job_id}/detail")
        assert detail.status_code == 200, detail.text
        payload = detail.json()
        assert payload["job"]["status"] == "completed", payload["job"]
        assert payload["report"]["score"] < 100
        assert payload["violations"], "Expected no-glove violation"
        print(
            {
                "health": health.json()["status"],
                "steps": len(steps.json()),
                "task_id": task_id,
                "job_id": job_id,
                "status": payload["job"]["status"],
                "score": payload["report"]["score"],
                "violations": [item["title"] for item in payload["violations"]],
            }
        )


if __name__ == "__main__":
    main()
