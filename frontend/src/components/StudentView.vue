<script setup>
/**
 * 学生端：选任务 → 上传 → 看结果 → 申请复核。
 *
 * 学生**看不到全班**：列表、报告、证据帧三个接口都在后端按 student_id 过滤，
 * 前端这里只是不做无谓的展示。数据边界不在这一层。
 */
import { computed, onMounted, ref } from 'vue'
import { api, uploadSubmission } from '../services/api'
import JobDetail from './JobDetail.vue'

const props = defineProps({
  user: { type: Object, required: true }
})
const emit = defineEmits(['notify', 'unauthorized'])

const tasks = ref([])
const submissions = ref([])
const selectedJobId = ref(null)
const detail = ref(null)
const loading = ref(false)

const fileInput = ref(null)
const chosenFile = ref(null)
const taskId = ref('')
const progress = ref(0)
const uploading = ref(false)
const localError = ref('')

const STATUS_TEXT = {
  pending: '排队中',
  running: '分析中',
  completed: '已完成',
  failed: '分析失败'
}

const openTasks = computed(() => tasks.value.filter((t) => t.status !== 'closed'))

function formatDateTime(value) {
  if (!value) return '-'
  return String(value).replace('T', ' ').slice(0, 16)
}

/** 学生看到的分数：只有终审过才给数字。 */
function scoreText(row) {
  const job = row.job
  if (!job) return '—'
  if (job.status === 'failed') return '未完成'
  if (job.score === null || job.score === undefined) return '—'
  return `${job.score} 分`
}

async function refresh() {
  const [nextTasks, nextSubmissions] = await Promise.all([api.listTasks(), api.listSubmissions()])
  tasks.value = nextTasks
  submissions.value = nextSubmissions
  if (!taskId.value && nextTasks.length) taskId.value = String(nextTasks[0].id)
  if (selectedJobId.value) await selectJob(selectedJobId.value, { silent: true })
}

function onFileChange(event) {
  const file = event.target.files?.[0] || null
  chosenFile.value = file
  localError.value = ''
}

async function upload() {
  localError.value = ''
  if (!chosenFile.value) {
    localError.value = '请先选择要上传的视频文件。'
    return
  }
  if (!taskId.value) {
    localError.value = '请先选择作业任务。'
    return
  }

  uploading.value = true
  progress.value = 0
  try {
    const result = await uploadSubmission({
      taskId: taskId.value,
      file: chosenFile.value,
      onProgress: (percent) => {
        progress.value = percent
      }
    })
    // 传完立刻置 100：最后的服务端校验（ffprobe/时长）还要几百毫秒，
    // 进度条停在 97% 会让人以为卡住了。
    progress.value = 100
    emit('notify', '上传成功，已加入分析队列。')
    fileInput.value.value = ''
    chosenFile.value = null
    selectedJobId.value = result.job.id
    await refresh()
  } catch (err) {
    localError.value = err.message
  } finally {
    uploading.value = false
  }
}

async function selectJob(jobId, { silent = false } = {}) {
  selectedJobId.value = jobId
  try {
    detail.value = await api.getDetail(jobId)
  } catch (err) {
    if (!silent) localError.value = err.message
  }
}

async function appeal() {
  const message = window.prompt('请说明你认为哪里判断有误（会转给老师处理）：')
  if (!message) return
  try {
    await api.appeal(selectedJobId.value, message)
    emit('notify', '已提交复核申请，老师会尽快处理。')
  } catch (err) {
    localError.value = err.message
  }
}

onMounted(async () => {
  loading.value = true
  try {
    await refresh()
  } catch (err) {
    localError.value = err.message
  } finally {
    loading.value = false
  }
})

defineExpose({ refresh })
</script>

<template>
  <section class="workspace">
    <aside class="panel controls">
      <h2>上传作业视频</h2>

      <label>
        <span>作业任务</span>
        <select v-model="taskId">
          <option v-for="task in openTasks" :key="task.id" :value="String(task.id)">
            {{ task.title }}（{{ task.class_name }}）
          </option>
        </select>
      </label>
      <p v-if="!openTasks.length" class="hint">当前没有开放的作业任务，请等老师发布。</p>

      <label>
        <span>视频文件</span>
        <input
          ref="fileInput"
          type="file"
          accept="video/*,.mp4,.mov,.m4v,.avi,.mkv,.webm,.3gp,.flv,.wmv"
          :disabled="uploading"
          @change="onFileChange"
        />
      </label>
      <p class="hint">
        手机拍摄的录像可以直接上传。系统会读出画面来判断作业步骤，
        请保证能看清人和操作动作。每个任务只能提交一次。
      </p>

      <div v-if="uploading" class="progress-bar">
        <div class="progress-fill" :style="{ width: `${progress}%` }"></div>
      </div>
      <p v-if="uploading" class="hint">正在上传：{{ progress }}%（请勿关闭页面）</p>

      <button class="primary-button" :disabled="uploading" @click="upload">
        {{ uploading ? '上传中…' : '上传并开始分析' }}
      </button>

      <p v-if="localError" class="login-error">{{ localError }}</p>

      <div class="divider"></div>

      <h2>我的提交</h2>
      <div class="job-list">
        <button
          v-for="row in submissions"
          :key="row.id"
          class="job-item"
          :class="{ active: row.job?.id === selectedJobId }"
          @click="row.job && selectJob(row.job.id)"
        >
          <span>
            {{ formatDateTime(row.uploaded_at) }}<br />
            <small>{{ STATUS_TEXT[row.job?.status] || '—' }}</small>
          </span>
          <strong>{{ scoreText(row) }}</strong>
        </button>
      </div>
      <p v-if="!submissions.length" class="hint">还没有提交记录。</p>
    </aside>

    <section class="panel result-panel">
      <div class="section-header">
        <h2>分析结果</h2>
        <div class="review-actions">
          <button
            class="ghost-button"
            :disabled="!selectedJobId"
            @click="selectedJobId && selectJob(selectedJobId)"
          >
            刷新
          </button>
          <button
            v-if="selectedJobId"
            class="ghost-button"
            @click="appeal"
          >
            申请复核
          </button>
        </div>
      </div>

      <JobDetail
        v-if="detail"
        :detail="detail"
        :can-review="false"
        @changed="selectJob(selectedJobId)"
      />
      <div v-else class="empty-state">
        上传作业视频后，这里会显示 AI 找到的证据画面、扣分项和评价报告。
      </div>
    </section>
  </section>
</template>
