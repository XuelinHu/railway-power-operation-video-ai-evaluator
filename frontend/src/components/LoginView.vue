<script setup>
import { ref } from 'vue'
import { api } from '../services/api'

const emit = defineEmits(['success'])

const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

async function submit() {
  if (!username.value || !password.value) {
    error.value = '请填写用户名和密码。'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const result = await api.login(username.value.trim(), password.value)
    password.value = '' // 成功后立刻清掉，别留在内存里
    emit('success', result.user)
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
      <p class="eyebrow">Railway Power Operation AI Evaluator</p>
      <h1>铁道供电作业视频智能评价平台</h1>
      <p class="login-hint">请使用实训室下发的账号登录。首次登录后需要修改密码。</p>

      <label>
        <span>用户名</span>
        <input v-model="username" autocomplete="username" autofocus />
      </label>
      <label>
        <span>密码</span>
        <input v-model="password" type="password" autocomplete="current-password" />
      </label>

      <p v-if="error" class="login-error">{{ error }}</p>

      <button class="primary-button" type="submit" :disabled="busy">
        {{ busy ? '正在登录…' : '登录' }}
      </button>
    </form>
  </main>
</template>
