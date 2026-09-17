#!/usr/bin/env python3
"""对着**真实运行的服务**跑一遍完整链路 —— 部署验收用。

`pytest` 用的是 TestClient（进程内、无网络），它验证不了这几件事，
而它们恰恰是部署时最容易坏的地方：

- cookie 真的按浏览器规则收发（TestClient 不会因为 SameSite/Secure 拒收）
- worker 是**另一个进程**（进程内测试可以顺手把活干了，掩盖进程间的问题）
- ffmpeg 真的在跑、前端静态文件真的被托管
- systemd 起来的那套环境变量（DATA_DIR、AI_PROVIDER）确实是生效的那套

所以每次部署完、以及升级后，都该在服务器上跑一次。

**它会往库里写数据**（一个任务、两个学生、一段合成视频）。
生产库上跑之前先备份；它造的学号带随机后缀，不会覆盖真实数据。

用法：

    python -m scripts.smoke_live
    python -m scripts.smoke_live --base http://127.0.0.1:8091
    python -m scripts.smoke_live --admin-password '你改过的口令'

退出码 0 表示全部通过。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# 首登强制改密是真实行为，所以第一次跑会把这个引导口令换掉。
# 第二次跑就得用换过之后的那个——照抄 tests/conftest.py 的处理方式。
BOOTSTRAP_PASSWORD = "AdminPass123"
ROTATED_PASSWORD = "AdminPass456"

BASE = "http://127.0.0.1:8090"
TMP = Path(tempfile.gettempdir())
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(label)


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body=None, raw: bool = False):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=60) as response:
                payload = response.read()
                return response.status, (payload if raw else json.loads(payload or b"null"))
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload or b"null")
            except json.JSONDecodeError:
                return exc.code, payload.decode(errors="replace")

    def get(self, path: str, raw: bool = False):
        return self.call("GET", path, raw=raw)

    def post(self, path: str, body=None):
        return self.call("POST", path, body if body is not None else {})

    def upload(self, path: str, fields: dict, file_field: str, file_path: Path, filename: str):
        """multipart 上传。用标准库拼，免得为了一个验收脚本引入 requests。"""
        boundary = "----smokeboundary1234567890"
        parts = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: video/mp4\r\n\r\n'.encode()
        )
        parts.append(file_path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        payload = b"".join(parts)

        request = urllib.request.Request(
            BASE + path,
            data=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=180) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"null")
            except json.JSONDecodeError:
                return exc.code, raw.decode(errors="replace")


# 合成的验收视频时长。
#
# **必须 >= 服务的 MIN_DURATION_SEC**（默认 20 秒），否则上传会被正当地拒掉，
# 而这个脚本会以一个看起来像"上传坏了"的错误失败。取 30 秒留出余量。
# 时长写进文件名，改了参数就会重新生成，不会拿旧的短片子接着跑。
SMOKE_VIDEO_SEC = 30


def make_video(path: Path) -> None:
    if path.exists():
        return
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=size=640x480:rate=15:duration={SMOKE_VIDEO_SEC}",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            str(path),
        ],
        check=True,
        timeout=180,
    )


def login_admin(client: Client, username: str, password: str) -> bool:
    """登录管理员，必要时改密。返回是否成功。"""
    response = client.post("/api/auth/login", {"username": username, "password": password})
    if response[0] != 200 and password == BOOTSTRAP_PASSWORD:
        # 上一次跑已经把引导口令换掉了。
        response = client.post(
            "/api/auth/login", {"username": username, "password": ROTATED_PASSWORD}
        )
        if response[0] == 200:
            check("管理员登录（口令已被上一次运行轮换过）", True)
            return True
    check("管理员登录", response[0] == 200, f"{response[0]} {response[1]}")
    if response[0] != 200:
        return False

    check(
        "服务端下发了 httpOnly 会话 cookie",
        any(c.name == "rpo_session" for c in client.jar),
        str([c.name for c in client.jar]),
    )
    _, me = client.get("/api/auth/me")
    check("凭 cookie 取回当前用户", me.get("username") == username)

    if me.get("must_change_password"):
        status, body = client.post(
            "/api/auth/change-password",
            {"old_password": password, "new_password": ROTATED_PASSWORD},
        )
        check("首次登录强制改密", status == 200, f"{status} {body}")
        # 回归点：改密后如果把自己也踢下线，每一个新用户都会撞上。
        status, _ = client.get("/api/auth/me")
        check("改密后当前会话仍然有效（不被自己踢下线）", status == 200, f"实际 {status}")
    return True


def run(base: str, admin_user: str, admin_password: str) -> int:
    global BASE
    BASE = base.rstrip("/")
    video = TMP / f"rpo-smoke-{SMOKE_VIDEO_SEC}s.mp4"
    make_video(video)

    # 学号带随机后缀：这个脚本要能反复跑，而账号是**全局唯一**的。
    # 用固定学号的话第二次跑会走到"复用已有账号、不重置密码"的分支，
    # 于是拿不到初始密码，脚本会以一个看不懂的 401 失败。
    run_tag = f"{random.randint(0, 999999):06d}"
    student_a, student_b = f"9{run_tag}1", f"9{run_tag}2"

    # --- 0. 未登录必须被挡住 ----------------------------------------------
    anon = Client()
    status, _ = anon.get("/api/tasks")
    check("未登录访问 /api/tasks 返回 401", status == 401, f"实际 {status}")
    status, _ = anon.get("/api/submissions")
    check("未登录访问 /api/submissions 返回 401", status == 401, f"实际 {status}")

    # 登录页本身必须能匿名打开，否则谁也没法登录。
    status, page = anon.get("/", raw=True)
    check("未登录能打开登录页（前端由后端托管）", status == 200 and b'<div id="app">' in page)

    # --- 1. 管理员 ---------------------------------------------------------
    admin = Client()
    if not login_admin(admin, admin_user, admin_password):
        return 1

    # --- 2. 建任务 + 导名册 ------------------------------------------------
    status, task = admin.post(
        "/api/tasks",
        {
            "title": f"冒烟-接触网停电验电接地-{run_tag}",
            "course": "铁道供电安全实训",
            "class_name": "供电2401",
            "description": "由 scripts/smoke_live.py 生成，可随时删除",
        },
    )
    check("教师创建作业任务", status == 200, f"{status} {task}")
    if status != 200:
        return 1
    check("任务归属取自登录账号而非表单", task.get("teacher") not in (None, ""), str(task.get("teacher")))
    task_id = task["id"]

    status, roster = admin.post(
        "/api/roster/import",
        {
            "task_id": task_id,
            "text": f"学号,姓名\n{student_a},张三\n{student_b},李四\n",
            "create_accounts": True,
        },
    )
    check("导入名册并批量开通账号", status == 200 and roster.get("created_entries") == 2, f"{status} {roster}")
    initial_password = roster.get("initial_password")
    check("返回了一次性初始密码", bool(initial_password), str(roster))
    if not initial_password:
        return 1

    # --- 3. 学生登录 + 上传 ------------------------------------------------
    student = Client()
    status, body = student.post(
        "/api/auth/login", {"username": student_a, "password": initial_password}
    )
    check("学生用学号+初始密码登录", status == 200, f"{status} {body}")
    if status != 200:
        return 1
    if body["user"]["must_change_password"]:
        status, _ = student.post(
            "/api/auth/change-password",
            {"old_password": initial_password, "new_password": "StudentPass123"},
        )
        check("学生首次登录强制改密", status == 200, f"{status}")

    status, body = student.upload(
        "/api/submissions", {"task_id": str(task_id)}, "file", video, "张三作业.mp4"
    )
    check("学生上传视频", status == 200, f"{status} {body}")
    if status != 200:
        return 1
    submission_id = body["id"]
    job_id = body["job"]["id"]
    check(
        "姓名学号取自信名册而非表单",
        body["student_no"] == student_a and body["student_name"] == "张三",
        f'{body["student_no"]}/{body["student_name"]}',
    )

    status, _ = student.upload(
        "/api/submissions", {"task_id": str(task_id)}, "file", video, "再传一次.mp4"
    )
    check("重复提交被拒绝", status == 409, f"实际 {status}")

    # --- 4. 等 worker 处理 -------------------------------------------------
    # worker 是**另一个进程**，这里只能轮询。这也是这条断言的价值所在。
    deadline = time.time() + 180
    job = None
    while time.time() < deadline:
        _, job = student.get(f"/api/analysis/jobs/{job_id}")
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(2)

    check(
        "worker 在另一个进程里完成了分析",
        job is not None and job["status"] == "completed",
        f'{job and job["status"]} / {job and job.get("error")}',
    )
    if not job or job["status"] != "completed":
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 1

    # **不变量 #1**：分析失败绝不留分数。这条断言在成功路径上看似多余，
    # 但它是"score 可空"这个改造的现场确认。
    check("分析产出分数", job["score"] is not None, str(job["score"]))
    check("任务已释放 worker 占用", job.get("worker_id", "") == "", str(job.get("worker_id")))

    _, detail = student.get(f"/api/analysis/jobs/{job_id}/detail")
    check(
        "报告声明以教师复核为准",
        "教师复核" in detail["disclaimer"] and "仅供参考" in detail["disclaimer"],
    )
    check(
        "未复核时不是终分",
        detail["report"]["review_status"] != "confirmed",
        detail["report"]["review_status"],
    )
    check("步骤判定覆盖全部标准步骤", len(detail["steps"]) >= 9, str(len(detail["steps"])))

    # **不变量 #4**：判定词表是封闭的。这里断言的是"没有冒出新词"，
    # 而**不是**"必须出现 not_visible"——后者是 fake 提供方那份固定剧本的
    # 性质，不是系统的性质。demo 按文件名猜，正常视频全判 completed 是对的，
    # 要求它凑一个 not_visible 出来反而是让它编。
    # 三态语义真正的护栏在 tests/test_antihallucination.py。
    verdicts = {s["verdict"] for s in detail["steps"]}
    check(
        "判定词表封闭（无自造词）",
        verdicts <= {"completed", "not_completed", "not_visible", "not_applicable"},
        str(sorted(verdicts)),
    )

    with_frames = [s for s in detail["steps"] if s.get("evidence_frames")]
    if with_frames:
        index = with_frames[0]["evidence_frames"][0]
        status, data = student.get(f"/api/submissions/{submission_id}/frames/{index}", raw=True)
        check(
            "证据帧可以按序号取回（JPEG 魔数）",
            status == 200 and isinstance(data, bytes) and data[:2] == b"\xff\xd8",
            f"{status} {data[:8] if isinstance(data, bytes) else data}",
        )
    else:
        print("[---- ] 当前 AI 提供方不产证据帧，跳过")

    status, _ = student.get(f"/api/submissions/{submission_id}/video", raw=True)
    check("视频可以回放", status == 200, f"实际 {status}")

    # --- 5. 越权 -----------------------------------------------------------
    other = Client()
    status, body = other.post(
        "/api/auth/login", {"username": student_b, "password": initial_password}
    )
    if status == 200:
        if body["user"]["must_change_password"]:
            other.post(
                "/api/auth/change-password",
                {"old_password": initial_password, "new_password": "StudentPass456"},
            )
        status, _ = other.get(f"/api/submissions/{submission_id}/video")
        check("学生看不到别人的视频（404 而非 403）", status == 404, f"实际 {status}")
        _, mine = other.get("/api/submissions")
        check("学生的提交列表里只有自己的", mine == [], str(mine))
        status, _ = other.get("/api/reviews/queue")
        check("学生访问教师接口被拒", status == 403, f"实际 {status}")
    else:
        check("第二个学生能登录", False, f"{status} {body}")

    # --- 6. 教师复核 + 终审 + 导出 -----------------------------------------
    _, queue = admin.get("/api/reviews/queue")
    check(
        "教师能看到待复核队列",
        isinstance(queue, list) and any(q["job_id"] == job_id for q in queue),
        f"{status} {len(queue) if isinstance(queue, list) else queue}",
    )

    violations = detail["violations"]
    if violations:
        violation_id = violations[0]["id"]
        status, body = admin.post(
            f"/api/reviews/jobs/{job_id}/violations/{violation_id}",
            {"action": "dismiss", "comment": "冒烟：判为误判"},
        )
        check("驳回扣分项并重算", status == 200, f"{status} {body}")
        if status == 200:
            check(
                "驳回后终分上升",
                body["final_score"] > job["score"],
                f'{body["final_score"]} vs {job["score"]}',
            )
    else:
        print("[---- ] 本次没有扣分项，跳过驳回")

    status, body = admin.post(f"/api/reviews/jobs/{job_id}/finalize", {"comment": "冒烟终审"})
    check("教师终审", status == 200 and body.get("review_status") == "confirmed", f"{status} {body}")

    # 已终审的任务重跑会抹掉复核结果——必须被挡住。
    status, _ = admin.post(f"/api/analysis/jobs/{job_id}/rerun")
    check("已终审的任务不能再重跑", status == 409, f"实际 {status}")

    status, csv = admin.get(f"/api/reviews/export.csv?task_id={task_id}", raw=True)
    check("导出 CSV 带 BOM（Excel 不乱码）", status == 200 and csv.startswith(b"\xef\xbb\xbf"))
    check("导出的 CSV 含学号姓名", student_a.encode() in csv and "张三".encode() in csv)
    check("终审状态出现在导出里", "已终审".encode() in csv)

    # --- 7. 账号管理 -------------------------------------------------------
    _, rows = admin.get("/api/users")
    check(
        "管理员能列出账号",
        isinstance(rows, list) and len(rows) >= 3,
        str(len(rows) if isinstance(rows, list) else rows),
    )

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print(f"全部通过。（本次用的学号：{student_a} / {student_b}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="对运行中的服务跑一遍端到端验收")
    parser.add_argument("--base", default=BASE, help=f"服务地址，默认 {BASE}")
    parser.add_argument("--admin-user", default="admin", help="管理员用户名")
    parser.add_argument(
        "--admin-password",
        default=BOOTSTRAP_PASSWORD,
        help=f"管理员当前口令，默认引导口令 {BOOTSTRAP_PASSWORD}",
    )
    args = parser.parse_args()
    return run(args.base, args.admin_user, args.admin_password)


if __name__ == "__main__":
    sys.exit(main())
