<script setup>
/**
 * 教师端：建任务 → 导名册 → 复核 → 终审 → 导出成绩。
 *
 * 复核是这套系统可信度的地基，所以"复核"标签页是默认页，
 * 而且 `needs_review` 的任务排在前面——教师的时间应该优先花在
 * 机器自己都觉得没把握的地方。
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../services/api'
import JobDetail from './JobDetail.vue'

const props = defineProps({
  user: { type: Object, required: true }
})
const emit = defineEmits(['notify'])

const tab = ref('review')

const tasks = ref([])
const queue = ref([])
const appeals = ref([])
const users = ref([])
const roster = ref(null)
const submissions = ref([])
const detail = ref(null)
const selectedJobId = ref(null)
const selectedTaskId = ref('')

const busy = ref(false)
const error = ref('')
const notice = ref('')

const taskForm = ref({ title: '', course: '', class_name: '', description: '' })
const rosterText = ref('')
const rosterTaskId = ref('')
const userForm = ref({ username: '', password: '', role: 'student', display_name: '', class_name: '' })

const isAdmin = computed(() => props.user.role === 'admin')

const VISIBLE_STEPS = ['任务与名册', '复核', '成绩导出', '账号']
const tabs = computed(() => (isAdmin.value ? [...VISIBLE_STEPS, '审计'] : VISIBLE_STEPS))

function formatDateTime(value) {
  if (!value) return '-'
  return String(value).replace('T', ' ').slice(0, 16)
}

function report(text, isError = false) {
  if (isError) {
    error.value = text
    notice.value = ''
  } else {
    notice.value = text
    error.value = ''
  }
}

async function guard(fn) {
  busy.value = true
  try {
    await fn()
  } catch (err) {
    report(err.message, true)
  } finally {
    busy.value = false
  }
}

async function loadTasks() {
  tasks.value = await api.listTasks()
  if (!rosterTaskId.value && tasks.value.length) rosterTaskId.value = String(tasks.value[0].id)
  if (!selectedTaskId.value && tasks.value.length) selectedTaskId.value = String(tasks.value[0].id)
}

async function loadQueue() {
  queue.value = await api.reviewQueue()
}

async function loadAppeals() {
  appeals.value = await api.listAppeals()
}

async function loadUsers() {
  if (!isAdmin.value) return
  users.value = await api.listUsers()
}

async function refresh() {
  await guard(async () => {
    await loadTasks()
    await loadQueue()
    await loadAppeals()
    await loadUsers()
    if (selectedJobId.value) await selectJob(selectedJobId.value, { silent: true })
  })
}

async function createTask() {
  await guard(async () => {
    const task = await api.createTask(taskForm.value)
    taskForm.value = { title: '', course: '', class_name: '', description: '' }
    report(`已创建作业「${task.title}」。下一步请导入学生名册。`)
    await loadTasks()
    rosterTaskId.value = String(task.id)
  })
}

async function importRoster() {
  await guard(async () => {
    const result = await api.importRoster({
      task_id: Number(rosterTaskId.value),
      text: rosterText.value,
      create_accounts: true
    })
    let text =
      `名册导入完成：识别 ${result.parsed} 行，新增 ${result.created_entries} 人，` +
      `新建账号 ${result.created_users} 个，复用已有账号 ${result.reused_users} 个。`
    if (result.initial_password) {
      // 初始密码只在这一次响应里存在，服务端不存明文。
      // 教师必须当场记下来——刷新页面就再也拿不到了。
      text +=
        `\n本次新账号的初始密码统一为：${result.initial_password}` +
        `\n请立刻记录并在班上公布；刷新本页后不再显示。学生首次登录会被强制改密。`
    }
    report(text)
    rosterText.value = ''
    await loadRoster()
  })
}

async function loadRoster() {
  if (!rosterTaskId.value) return
  await guard(async () => {
    roster.value = await api.listRoster(Number(rosterTaskId.value))
  })
}

async function openTask(taskId) {
  selectedTaskId.value = String(taskId)
  await guard(async () => {
    submissions.value = await api.listSubmissions(taskId)
  })
}

async function selectJob(jobId, { silent = false } = {}) {
  selectedJobId.value = jobId
  try {
    detail.value = await api.getDetail(jobId)
  } catch (err) {
    if (!silent) report(err.message, true)
  }
}

async function afterReview() {
  await selectJob(selectedJobId.value, { silent: true })
  await loadQueue()
}

async function createUser() {
  await guard(async () => {
    await api.createUser(userForm.value)
    report(`已创建账号 ${userForm.value.username}。`)
    userForm.value = { username: '', password: '', role: 'student', display_name: '', class_name: '' }
    await loadUsers()
  })
}

async function resetPassword(user) {
  const newPassword = window.prompt(`为 ${user.username} 设置新密码（至少 8 位）：`)
  if (!newPassword) return
  await guard(async () => {
    await api.resetPassword(user.id, newPassword)
    report(`已重置 ${user.username} 的密码，该账号需要重新登录。请当面告知学生新密码。`)
    await loadUsers()
  })
}

async function toggleActive(user) {
  await guard(async () => {
    await api.setActive(user.id, !user.is_active)
    report(`${user.username} 已${user.is_active ? '停用' : '启用'}。`)
    await loadUsers()
  })
}

onMounted(refresh)

defineExpose({ refresh })
</script>

<template>
  <section class="workspace">
    <aside class="panel controls">
      <h2>教师工作台</h2>
      <p class="hint">
        任课教师：{{ user.display_name || user.username }}
      </p>
      <nav class="tab-list">
        <button
          v-for="name in tabs"
          :key="name"
          class="tab-button"
          :class="{ active: tab === name }"
          @click="tab = name"
        >
          {{ name }}
          <span v-if="name === '复核' && queue.length" class="badge warn">{{ queue.length }}</span>
        </button>
      </nav>
      <button class="ghost-button" :disabled="busy" @click="refresh">刷新数据</button>
    </aside>

    <section class="panel result-panel">
      <p v-if="notice" class="message">{{ notice }}</p>
      <p v-if="error" class="login-error">{{ error }}</p>

      <!-- 任务与名册 ------------------------------------------------------ -->
      <div v-show="tab === '任务与名册'">
        <div class="section-header"><h2>新建作业任务</h2></div>
        <div class="form-grid">
          <label><span>作业名称</span><input v-model="taskForm.title" placeholder="接触网停电验电接地实训" /></label>
          <label><span>课程</span><input v-model="taskForm.course" placeholder="铁道供电安全实训" /></label>
          <label><span>班级</span><input v-model="taskForm.class_name" placeholder="供电2401" /></label>
          <label class="full"><span>任务说明</span><textarea v-model="taskForm.description" rows="2"></textarea></label>
        </div>
        <button class="primary-button" :disabled="busy" @click="createTask">创建作业</button>

        <div class="divider"></div>

        <div class="section-header"><h2>导入学生名册</h2></div>
        <label>
          <span>选择作业</span>
          <select v-model="rosterTaskId" @change="loadRoster">
            <option v-for="task in tasks" :key="task.id" :value="String(task.id)">
              {{ task.title }}（{{ task.class_name }}）
            </option>
          </select>
        </label>
        <label class="full">
          <span>名册内容（每行"学号,姓名"，可直接从 Excel 或教务系统粘贴）</span>
          <textarea
            v-model="rosterText"
            rows="6"
            placeholder="2023001,张三&#10;2023002,李四"
          ></textarea>
        </label>
        <p class="hint">
          导入时会同时为每个学生开通账号，用户名为学号，初始密码为学号后 6 位。
          学生首次登录后必须修改密码。
        </p>
        <button class="primary-button" :disabled="busy || !rosterText.trim()" @click="importRoster">
          导入名册并开通账号
        </button>

        <div v-if="roster" class="roster-summary">
          <p class="hint">共 {{ roster.total }} 人，已登录过 {{ roster.bound }} 人。</p>
          <ul class="plain-list roster-list">
            <li v-for="entry in roster.entries" :key="entry.id">
              <strong>{{ entry.student_no }} {{ entry.student_name }}</strong>
              <span>{{ entry.bound ? '已激活' : '未登录' }}</span>
            </li>
          </ul>
        </div>

        <div class="divider"></div>

        <div class="section-header">
          <h2>提交情况</h2>
          <a
            v-if="selectedTaskId"
            class="ghost-button"
            :href="api.exportUrl(selectedTaskId)"
          >导出成绩 CSV</a>
        </div>
        <div class="job-list">
          <button
            v-for="task in tasks"
            :key="task.id"
            class="job-item"
            :class="{ active: String(task.id) === selectedTaskId }"
            @click="openTask(task.id)"
          >
            <span>{{ task.title }}<br /><small>{{ task.class_name }}</small></span>
            <strong>{{ task.submitted_count }}/{{ task.roster_total }}</strong>
          </button>
        </div>
        <ul v-if="submissions.length" class="plain-list roster-list">
          <li v-for="row in submissions" :key="row.id">
            <strong>{{ row.student_no }} {{ row.student_name }}</strong>
            <span>
              {{ formatDateTime(row.uploaded_at) }} ·
              {{ row.job?.status || '—' }} ·
              <button
                v-if="row.job"
                class="link-button"
                @click="selectJob(row.job.id)"
              >查看</button>
            </span>
          </li>
        </ul>
        <p v-else class="empty">这个作业还没有学生提交。</p>
      </div>

      <!-- 复核 ------------------------------------------------------------ -->
      <div v-show="tab === '复核'">
        <div class="section-header">
          <h2>待复核（{{ queue.length }}）</h2>
        </div>
        <p class="hint">
          标记「需人工确认」的是机器自己没把握的：画面里看不到、或者证据不足。
          它们**没有自动扣分**，需要你判断是真没做还是没拍到。
        </p>
        <div class="job-list">
          <button
            v-for="item in queue"
            :key="item.job_id"
            class="job-item"
            :class="{ active: item.job_id === selectedJobId }"
            @click="selectJob(item.job_id)"
          >
            <span>
              {{ item.student_no }} {{ item.student_name }}<br />
              <small>
                {{ item.review_status === 'needs_review' ? '需人工确认' : '待复核' }}
                <template v-if="item.invisible_steps">
                  · {{ item.invisible_steps }} 步看不见
                </template>
              </small>
            </span>
            <strong>{{ item.machine_score === null ? '—' : `${item.machine_score} 分` }}</strong>
          </button>
        </div>
        <p v-if="!queue.length" class="empty">没有待复核的任务。</p>

        <div v-if="appeals.length" class="appeal-box">
          <h3>学生申请复核</h3>
          <ul class="plain-list">
            <li v-for="item in appeals" :key="item.id">
              <strong>{{ item.student_no }} {{ item.student_name }}</strong>
              <span>
                {{ item.message }}
                <button class="link-button" @click="selectJob(item.job_id)">查看</button>
              </span>
            </li>
          </ul>
        </div>

        <div v-if="detail" class="divider"></div>
        <JobDetail
          v-if="detail"
          :detail="detail"
          :can-review="true"
          @changed="afterReview"
        />
      </div>

      <!-- 成绩导出 --------------------------------------------------------- -->
      <div v-show="tab === '成绩导出'">
        <div class="section-header"><h2>成绩导出</h2></div>
        <p class="hint">
          导出的 CSV 带 BOM，可以直接用 Excel 打开而不乱码。
          「机器分」和「终分」两列都在，未复核的记录终分为空——
          **不会用机器分冒充终分**。
        </p>
        <label>
          <span>选择作业</span>
          <select v-model="selectedTaskId">
            <option v-for="task in tasks" :key="task.id" :value="String(task.id)">
              {{ task.title }}（{{ task.class_name }}）
            </option>
          </select>
        </label>
        <a
          v-if="selectedTaskId"
          class="primary-button inline-button"
          :href="api.exportUrl(selectedTaskId)"
        >下载成绩 CSV</a>
        <p v-else class="empty">还没有作业任务。</p>
      </div>

      <!-- 账号 ------------------------------------------------------------ -->
      <div v-show="tab === '账号'">
        <div class="section-header"><h2>账号管理</h2></div>
        <p v-if="!isAdmin" class="hint">只有管理员可以创建和停用账号。</p>

        <template v-if="isAdmin">
          <div class="form-grid">
            <label><span>用户名</span><input v-model="userForm.username" /></label>
            <label><span>初始密码</span><input v-model="userForm.password" /></label>
            <label>
              <span>角色</span>
              <select v-model="userForm.role">
                <option value="student">学生</option>
                <option value="teacher">教师</option>
                <option value="admin">管理员</option>
              </select>
            </label>
            <label><span>姓名</span><input v-model="userForm.display_name" /></label>
            <label><span>班级</span><input v-model="userForm.class_name" /></label>
          </div>
          <button class="primary-button" :disabled="busy" @click="createUser">创建账号</button>

          <div class="divider"></div>

          <ul class="plain-list roster-list">
            <li v-for="row in users" :key="row.id">
              <strong>{{ row.username }} {{ row.display_name }}</strong>
              <span>
                {{ row.role }}
                <template v-if="!row.is_active"> · 已停用</template>
                <template v-if="row.must_change_password"> · 待改密</template>
                · <button class="link-button" @click="resetPassword(row)">重置密码</button>
                · <button class="link-button" @click="toggleActive(row)">
                  {{ row.is_active ? '停用' : '启用' }}
                </button>
              </span>
            </li>
          </ul>
        </template>
      </div>

      <!-- 审计 ------------------------------------------------------------ -->
      <div v-show="tab === '审计'">
        <div class="section-header"><h2>审计记录</h2></div>
        <p class="hint">
          评分、改分、停用账号都会留下记录。这是出现成绩争议时唯一能拿出来的依据。
        </p>
        <div class="empty-state">审计明细请在服务器上查看，或联系维护人员导出。</div>
      </div>
    </section>
  </section>
</template>
