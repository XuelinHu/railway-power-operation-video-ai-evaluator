<script setup>
/**
 * 根组件：只做三件事——**登录门、强制改密门、按角色分流**。
 *
 * 为什么不引 vue-router：对 100 人、三个角色、四个页面的系统，
 * 路由带来的收益（可分享的 URL、前进后退）几乎为零，而成本是
 * 多一个依赖、多一层"路由守卫没拦住导致闪现未授权页面"的出错面。
 * 角色分流用一个 v-if 就够了，而且不会出现"URL 直接访问绕过守卫"。
 */
import { onMounted, ref } from 'vue'
import { api, setUnauthorizedHandler } from './services/api'
import LoginView from './components/LoginView.vue'
import ChangePasswordView from './components/ChangePasswordView.vue'
import StudentView from './components/StudentView.vue'
import TeacherView from './components/TeacherView.vue'

const user = ref(null)
const checking = ref(true)
const notice = ref('')
const demoMode = ref(false)

function notify(text) {
  notice.value = text
  // 提示看完就该消失；留太久会让下一条提示显得像是上一条还没处理。
  window.setTimeout(() => {
    if (notice.value === text) notice.value = ''
  }, 8000)
}

async function logout() {
  try {
    await api.logout()
  } catch {
    /* 登出失败也要把本地状态清掉，否则会卡在"看起来已登录但不能用" */
  }
  user.value = null
  notice.value = ''
}

// 任何请求收到 401（会话过期、被管理员停用、被改密踢下线）都回到登录页。
setUnauthorizedHandler(() => {
  if (user.value) {
    user.value = null
    notify('登录已失效，请重新登录。')
  }
})

async function restore() {
  checking.value = true
  try {
    // 直接问后端"我是谁"，而不是把用户信息存 localStorage：
    // 存本地的话，账号被停用后前端仍会显示已登录，直到下一次请求才报错。
    user.value = await api.me()
  } catch {
    user.value = null
  } finally {
    checking.value = false
  }
}

async function loadMode() {
  // 演示横幅的判据来自**后端实际在跑什么**，不是构建时的环境变量——
  // 后者在部署时会和真实配置走散，那时候横幅要么该显示而没显示
  // （把演示结果当成真实评分），要么反过来。
  try {
    const health = await api.health()
    demoMode.value = health?.checks?.ai_provider === 'demo'
  } catch {
    demoMode.value = false // 健康检查失败不该影响登录
  }
}

onMounted(() => {
  loadMode()
  restore()
})
</script>

<template>
  <div v-if="checking" class="boot-screen">正在加载…</div>

  <LoginView v-else-if="!user" @success="user = $event" />

  <ChangePasswordView v-else-if="user.must_change_password" @success="restore" />

  <main v-else class="app-shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">Railway Power Operation AI Evaluator</p>
        <h1>铁道供电作业视频智能评价平台</h1>
      </div>
      <div class="topbar-right">
        <span class="whoami">
          {{ user.display_name || user.username }}
          <small>{{ user.role === 'student' ? '学生' : user.role === 'admin' ? '管理员' : '教师' }}</small>
        </span>
        <button class="ghost-button" @click="logout">退出登录</button>
      </div>
    </header>

    <!-- 演示数据横幅。演示模式的产出**不是对视频内容的真实分析**，
         不标出来的话，看的人会把它当成系统真的判断对了。 -->
    <p v-if="demoMode" class="demo-banner">
      当前是演示模式：分析结果由固定样本生成，**不是**对视频内容的真实判断，不可用于评分。
    </p>

    <p v-if="notice" class="message">{{ notice }}</p>

    <StudentView
      v-if="user.role === 'student'"
      :user="user"
      @notify="notify"
    />
    <TeacherView
      v-else
      :user="user"
      @notify="notify"
    />
  </main>
</template>
