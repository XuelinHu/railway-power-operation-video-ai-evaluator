"""账号开通：初始密码必须自己就能通过强度校验。

这条用例的存在理由是一次真实缺陷：名册导入时用
`secrets.token_urlsafe(6)` 生成初始密码，而它约 **18.6%** 是纯字母，
会被自家的 `validate_password_strength` 拒掉。症状是"导入名册五分之一次
报『密码不能是纯数字或纯字母』"，教师再点一次就好了——于是没人会报，
但它每次都在发生。

之所以一直没被测出来：原有的端到端用例用的是 `create_accounts=False`，
**生成密码的那条路径整个没被走过**。所以下面除了断言生成器本身，
还必须真的走一遍开通账号的接口。
"""

from __future__ import annotations

import pytest

from app.auth import AuthError, generate_initial_password, validate_password_strength


class TestGenerateInitialPassword:
    def test_always_passes_own_strength_check(self):
        """抽样 2000 次全部合法。

        用 2000 而不是 1 次：原来的 bug 失败率 18.6%，抽 1 次有 81% 的概率
        测不出来；抽 2000 次的话，漏掉的概率是 10^-179。
        """
        for _ in range(2000):
            validate_password_strength(generate_initial_password())

    def test_is_long_enough_for_handwriting(self):
        """长度不能太短。这个口令要在课堂上念/抄，太短会撞熵下限。"""
        assert len(generate_initial_password()) >= 10

    def test_avoids_ambiguous_characters(self):
        """不含 0/O、1/l/I 等形近字符。

        这个口令是要念给一屋子学生、或抄在黑板上、或打印在纸名单上的。
        "第 4 位是 0 还是 O"是一整节课的答疑量。
        """
        for _ in range(500):
            password = generate_initial_password()
            assert not (set(password) & set("0O1lI")), password

    def test_is_actually_random(self):
        """两次生成不相同。防的是有人把它改成常量（例如学号后六位）——
        那等于全校学生的初始密码公开可猜。"""
        assert len({generate_initial_password() for _ in range(200)}) == 200

    def test_strength_check_still_rejects_weak_passwords(self):
        """护栏本身没被削弱。上面几条断言的是"生成的够强"，
        而不是"校验变松了"——后者才是更容易走的捷径。"""
        for weak in ("12345678", "abcdefgh", "password"):
            with pytest.raises(AuthError):
                validate_password_strength(weak)


class TestRosterAccountCreation:
    def _task(self, teacher) -> int:
        response = teacher.post(
            "/api/tasks",
            json={"title": "初始密码用例", "course": "实训", "class_name": "供电2401"},
        )
        assert response.status_code == 200, response.text
        return response.json()["id"]

    def test_import_returns_usable_initial_password(self, app_client, admin, sample_video):
        """开通账号这条路径必须真的走通，且发出去的密码能登录。

        这就是原用例漏掉的那条路：`create_accounts=False` 永远不会生成密码，
        所以生成器坏了也测不出来。
        """
        task_id = self._task(admin)

        response = admin.post(
            "/api/roster/import",
            json={
                "task_id": task_id,
                "text": "学号,姓名\n2026001,张三\n2026002,李四\n",
                "create_accounts": True,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created_entries"] == 2
        assert body["created_users"] == 2

        initial_password = body.get("initial_password")
        assert initial_password, "开通了账号却没有返回初始密码，教师无从告知学生"

        # 服务端发的密码必须真的能登录——这是端到端的闭环断言。
        from tests.conftest import login

        student = app_client
        assert login(student, "2026001", initial_password).status_code == 200
        # 首次登录必须强制改密，否则初始密码会一直用下去。
        assert student.get("/api/auth/me").json()["must_change_password"] is True

    def test_import_is_repeatable(self, app_client, admin):
        """同一份名册导入两次不报错、不重复建号。

        这条挡的是"随机失败"被修成"每次都失败"——如果生成器改成抛异常，
        或者导入逻辑变成对已存在的账号也重置密码，这里会红。
        """
        task_id = self._task(admin)
        payload = {
            "task_id": task_id,
            "text": "学号,姓名\n2026101,王五\n",
            "create_accounts": True,
        }
        first = admin.post("/api/roster/import", json=payload)
        assert first.status_code == 200, first.text
        second = admin.post("/api/roster/import", json=payload)
        assert second.status_code == 200, second.text
        # 第二次没有新行可加，也就不该再发一个新密码。
        assert second.json()["created_entries"] == 0

    def test_reset_issues_a_password_that_satisfies_the_policy(self, app_client, admin):
        """重置密码走的是另一个生成点，同样不能被自家校验拒掉。

        原先这里是 f"rpo{secrets.token_hex(4)}"，当那 8 位十六进制恰好
        全是 a-f 时（约 0.06%）整串是纯字母，会被拒。概率比导入那条低得多，
        正因为低，真出现时更会被当成"系统抽风"。
        """
        task_id = self._task(admin)
        admin.post(
            "/api/roster/import",
            json={
                "task_id": task_id,
                "text": "学号,姓名\n2026201,赵六\n",
                "create_accounts": True,
            },
        )

        response = admin.post(f"/api/roster/{task_id}/reset-student", params={"student_no": "2026201"})
        assert response.status_code == 200, response.text
        new_password = response.json()["new_password"]
        validate_password_strength(new_password)
