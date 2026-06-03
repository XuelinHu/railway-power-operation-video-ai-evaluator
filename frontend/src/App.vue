<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './services/api'

const loading = ref(false)
const message = ref('')
const stats = ref({ total_jobs: 0, completed_jobs: 0, average_score: 0, excellent_count: 0, risk_count: 0 })
const tasks = ref([])
const jobs = ref([])
const steps = ref([])
const rules = ref([])
const knowledge = ref([])
const selectedJobId = ref(null)
const detail = ref(null)
const fileInput = ref(null)

const taskForm = ref({
  title: '接触网停电验电接地实训评价',
  course: '铁道供电安全实训',
  class_name: '供电 2401 班',
  teacher: '张老师',
  description: '围绕防护用品、工作票确认、验电、挂接地线、开关操作和复核状态进行视频智能评价。'
})

const uploadForm = ref({
  task_id: '',
  student_name: '李明',
  student_no: '20260001'
})

const selectedTask = computed(() => tasks.value.find((task) => task.id === Number(uploadForm.value.task_id)))
const completedJobs = computed(() => jobs.value.filter((job) => job.status === 'completed'))

function formatTime(seconds) {
  if (seconds === null || seconds === undefined) return '-'
  const minute = Math.floor(seconds / 60)
  const second = Math.floor(seconds % 60)
  return `${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}`
}

async function refreshAll() {
  const [nextStats, nextTasks, nextJobs, nextSteps, nextRules, nextKnowledge] = await Promise.all([
    api.stats(),
    api.listTasks(),
    api.listJobs(),
    api.steps(),
    api.rules(),
    api.knowledge()
  ])
  stats.value = nextStats
  tasks.value = nextTasks
  jobs.value = nextJobs
  steps.value = nextSteps
  rules.value = nextRules
  knowledge.value = nextKnowledge
  if (!uploadForm.value.task_id && nextTasks.length) {
    uploadForm.value.task_id = String(nextTasks[0].id)
  }
  if (!selectedJobId.value && nextJobs.length) {
    await selectJob(nextJobs[0].id)
  } else if (selectedJobId.value) {
    await selectJob(selectedJobId.value)
  }
}

async function createTask() {
  loading.value = true
  message.value = ''
  try {
    const task = await api.createTask(taskForm.value)
    uploadForm.value.task_id = String(task.id)
    message.value = '评价任务已创建'
    await refreshAll()
  } catch (error) {
    message.value = error.message
  } finally {
    loading.value = false
  }
}

async function uploadVideo() {
  const file = fileInput.value?.files?.[0]
  if (!file) {
    message.value = '请先选择视频文件'
    return
  }
  if (!uploadForm.value.task_id) {
    message.value = '请先创建或选择评价任务'
    return
  }
  loading.value = true
  message.value = ''
  try {
    const formData = new FormData()
    formData.append('task_id', uploadForm.value.task_id)
    formData.append('student_name', uploadForm.value.student_name)
    formData.append('student_no', uploadForm.value.student_no)
    formData.append('file', file)
    const result = await api.uploadSubmission(formData)
    selectedJobId.value = result.job.id
    message.value = '视频已上传，分析任务已创建'
    await new Promise((resolve) => setTimeout(resolve, 600))
    await refreshAll()
  } catch (error) {
    message.value = error.message
  } finally {
    loading.value = false
  }
}

async function selectJob(jobId) {
  selectedJobId.value = jobId
  detail.value = await api.getDetail(jobId)
}

async function rerunJob() {
  if (!selectedJobId.value) return
  loading.value = true
  try {
    await api.rerun(selectedJobId.value)
    message.value = '已重新分析'
    await refreshAll()
  } catch (error) {
    message.value = error.message
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  loading.value = true
  try {
    await refreshAll()
  } catch (error) {
    message.value = `后端连接失败：${error.message}`
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main class="app-shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">Railway Power Operation AI Evaluator</p>
        <h1>铁道供电作业视频智能评价平台</h1>
      </div>
      <button class="ghost-button" :disabled="loading" @click="refreshAll">刷新</button>
    </header>

    <section class="metrics">
      <div class="metric">
        <span>分析任务</span>
        <strong>{{ stats.total_jobs }}</strong>
      </div>
      <div class="metric">
        <span>已完成</span>
        <strong>{{ stats.completed_jobs }}</strong>
      </div>
      <div class="metric">
        <span>平均分</span>
        <strong>{{ stats.average_score }}</strong>
      </div>
      <div class="metric">
        <span>风险作业</span>
        <strong>{{ stats.risk_count }}</strong>
      </div>
    </section>

    <p v-if="message" class="message">{{ message }}</p>

    <section class="workspace">
      <aside class="panel controls">
        <h2>基本操作</h2>
        <div class="form-grid">
          <label>
            <span>任务名称</span>
            <input v-model="taskForm.title" />
          </label>
          <label>
            <span>课程</span>
            <input v-model="taskForm.course" />
          </label>
          <label>
            <span>班级</span>
            <input v-model="taskForm.class_name" />
          </label>
          <label>
            <span>教师</span>
            <input v-model="taskForm.teacher" />
          </label>
          <label class="full">
            <span>任务说明</span>
            <textarea v-model="taskForm.description" rows="3"></textarea>
          </label>
        </div>
        <button class="primary-button" :disabled="loading" @click="createTask">创建评价任务</button>

        <div class="divider"></div>

        <label>
          <span>选择任务</span>
          <select v-model="uploadForm.task_id">
            <option v-for="task in tasks" :key="task.id" :value="String(task.id)">
              {{ task.title }}
            </option>
          </select>
        </label>
        <div class="form-grid compact">
          <label>
            <span>学生姓名</span>
            <input v-model="uploadForm.student_name" />
          </label>
          <label>
            <span>学号</span>
            <input v-model="uploadForm.student_no" />
          </label>
        </div>
        <label>
          <span>上传视频</span>
          <input ref="fileInput" type="file" accept="video/*,.txt,.mp4,.mov,.avi" />
        </label>
        <button class="primary-button" :disabled="loading" @click="uploadVideo">上传并分析</button>
        <p v-if="selectedTask" class="hint">当前任务：{{ selectedTask.class_name }} / {{ selectedTask.teacher }}</p>
      </aside>

      <section class="panel result-panel">
        <div class="section-header">
          <h2>评分结果</h2>
          <button class="ghost-button" :disabled="!selectedJobId || loading" @click="rerunJob">重新分析</button>
        </div>

        <div class="job-list">
          <button
            v-for="job in jobs"
            :key="job.id"
            class="job-item"
            :class="{ active: job.id === selectedJobId }"
            @click="selectJob(job.id)"
          >
            <span>#{{ job.id }} {{ job.status }}</span>
            <strong>{{ job.score }} 分</strong>
          </button>
        </div>

        <div v-if="detail" class="detail-grid">
          <div class="video-pane">
            <video
              v-if="detail.submission"
              controls
              :src="api.videoUrl(detail.submission.id)"
            ></video>
            <div class="report-card">
              <span class="score">{{ detail.job.score }}</span>
              <div>
                <h3>{{ detail.report?.conclusion || detail.job.summary || '等待分析完成' }}</h3>
                <p>{{ detail.report?.strengths }}</p>
              </div>
            </div>
          </div>

          <div class="evidence-pane">
            <h3>标准流程识别</h3>
            <ol class="timeline">
              <li v-for="step in detail.steps" :key="step.id">
                <time>{{ formatTime(step.start_sec) }}</time>
                <div>
                  <strong>{{ step.step_name }}</strong>
                  <p>{{ step.evidence }}</p>
                </div>
              </li>
            </ol>

            <h3>扣分项</h3>
            <div v-if="detail.violations.length" class="violations">
              <article v-for="item in detail.violations" :key="item.id" class="violation">
                <div>
                  <strong>{{ item.title }}</strong>
                  <span>{{ item.severity }} / 扣 {{ item.deduction }} 分</span>
                </div>
                <p>{{ item.reason }}</p>
                <small>{{ item.suggestion }}</small>
              </article>
            </div>
            <p v-else class="empty">暂无硬性规则扣分项</p>
          </div>
        </div>

        <div v-else class="empty-state">
          创建任务并上传视频后，这里会显示 AI 证据、扣分项和评价报告。
        </div>
      </section>
    </section>

    <section class="reference-grid">
      <div class="panel">
        <h2>评分规则</h2>
        <ul class="plain-list">
          <li v-for="rule in rules" :key="rule.id">
            <strong>{{ rule.title }}</strong>
            <span>扣 {{ rule.deduction }} 分</span>
          </li>
        </ul>
      </div>
      <div class="panel">
        <h2>标准步骤</h2>
        <ul class="plain-list">
          <li v-for="step in steps" :key="step.id">
            <strong>{{ step.order_index }}. {{ step.name }}</strong>
            <span>{{ step.required ? '必需' : '可选' }}</span>
          </li>
        </ul>
      </div>
      <div class="panel">
        <h2>知识库</h2>
        <ul class="plain-list docs">
          <li v-for="doc in knowledge" :key="doc.id">
            <strong>{{ doc.title }}</strong>
            <span>{{ doc.category }}</span>
          </li>
        </ul>
      </div>
    </section>
  </main>
</template>
