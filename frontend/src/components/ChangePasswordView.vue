<script setup>
/**
 * 首登强制改密。
 *
 * 这是**独立的一屏而不是一个弹窗**：后端在改密之前会拦掉所有其它接口
 * （`must_change_password` 中间件），所以用户在这一步做不了任何别的事，
 * 给一个可以关掉的弹窗只会让人以为关掉就能用。
 */
import { ref } from 'vue'
import { api } from '../services/api'

const emit = defineEmits(['success'])

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const busy = ref(false)
const error = ref('')

async function submit() {
  error.value = ''
  if (newPassword.value !== confirmPassword.value) {
    error.value = '两次输入的新密码不一致。'
    return
  }
  busy.value = true
  try {
    await api.changePassword(oldPassword.value, newPassword.value)
    emit('success')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-shell">
    <form class="login-card" @submit.prevent="submit">
      <p class="eyebrow">首次登录</p>
      <h1>请修改初始密码</h1>
      <p class="login-hint">
        初始密码由老师统一下发，容易被猜到，请改成只有你自己知道的密码。
        密码至少 8 位，且不能是纯数字或纯字母。
      </p>

      <label>
        <span>当前密码</span>
        <input v-model="oldPassword" type="password" autocomplete="current-password" autofocus />
      </label>
      <label>
        <span>新密码</span>
        <input v-model="newPassword" type="password" autocomplete="new-password" />
      </label>
      <label>
        <span>再输一次新密码</span>
        <input v-model="confirmPassword" type="password" autocomplete="new-password" />
      </label>

      <p v-if="error" class="login-error">{{ error }}</p>

      <button class="primary-button" type="submit" :disabled="busy">
        {{ busy ? '正在提交…' : '修改密码并继续' }}
      </button>
    </form>
  </main>
</template>
