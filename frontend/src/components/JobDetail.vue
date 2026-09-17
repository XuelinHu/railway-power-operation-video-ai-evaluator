<script setup>
/**
 * 分析结果详情。教师与学生共用，靠 `canReview` 决定是否出现改判控件。
 *
 * 两条展示上的硬规矩：
 * 1. **score 为 null 时绝不显示成 0**。0 分读起来是"你做错了"，
 *    而 null 的真实含义是"这段视频不足以判断"。两者在界面上必须长得不一样。
 * 2. **"看不见"和"没做"要分开显示**。VLM 判定里的 not_visible 不扣分，
 *    如果和 not_completed 一样渲染成一条红色扣分，老师会以为学生在偷懒。
 */
import { computed, ref } from 'vue'
import { api } from '../services/api'

const props = defineProps({
  detail: { type: Object, required: true },
  canReview: { type: Boolean, default: false }
})
const emit = defineEmits(['changed'])

const comment = ref('')
const busy = ref(false)
const error = ref('')
const videoEl = ref(null)

const job = computed(() => props.detail.job)
const report = computed(() => props.detail.report)
const submission = computed(() => props.detail.submission)

// 终分优先于机器分：复核过的任务，机器分只是过程量。
const displayScore = computed(() => {
  if (report.value?.final_score !== null && report.value?.final_score !== undefined) {
    return report.value.final_score
  }
  return report.value?.score ?? job.value?.score ?? null
})

const isConfirmed = computed(() => report.value?.review_status === 'confirmed')

const VERDICT_TEXT = {
  completed: '已完成',
  not_completed: '未完成',
  not_visible: '画面中看不到',
  not_applicable: '不适用'
}

const STATUS_TEXT = {
  pending: '排队中',
  running: '分析中',
  completed: '已完成',
  failed: '分析失败'
}

function verdictClass(verdict) {
  if (verdict === 'completed') return 'ok'
  if (verdict === 'not_completed') return 'bad'
  return 'unknown' // not_visible / not_applicable：既不是做对也不是做错
}

function formatTime(seconds) {
  if (seconds === null || seconds === undefined) return '--:--'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function formatDateTime(value) {
  if (!value) return '-'
  // 后端存的是服务器本地时间，这里只做显示截断，不做时区换算——
  // 单机部署下换算只会把时间弄错 8 小时。
  return String(value).replace('T', ' ').slice(0, 16)
}

/** 点证据帧跳到视频对应时间点。 */
function seekTo(seconds) {
  const video = videoEl.value
  if (!video || seconds === null || seconds === undefined) return
  video.currentTime = seconds
  video.play().catch(() => {}) // 用户没交互过时浏览器会拒绝自动播放，忽略即可
}

async function reviewViolation(violationId, action) {
  busy.value = true
  error.value = ''
  try {
    await api.reviewViolation(job.value.id, violationId, action, comment.value)
    comment.value = ''
    emit('changed')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function setStepVerdict(stepId, verdict) {
  busy.value = true
  error.value = ''
  try {
    await api.reviewStep(job.value.id, stepId, verdict, comment.value)
    comment.value = ''
    emit('changed')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function finalize() {
  busy.value = true
  error.value = ''
  try {
    await api.finalize(job.value.id, comment.value)
    comment.value = ''
    emit('changed')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function reopen() {
  busy.value = true
  error.value = ''
  try {
    await api.reopen(job.value.id, comment.value)
    comment.value = ''
    emit('changed')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="detail-grid">
    <!-- 视频 + 分数 ------------------------------------------------------ -->
    <div class="video-pane">
      <video
        v-if="submission"
        ref="videoEl"
        controls
        preload="metadata"
        :src="api.videoUrl(submission.id)"
      ></video>

      <div v-if="job.status === 'failed'" class="score-card failed">
        <div>
          <h3>分析未完成</h3>
          <p>{{ job.error || '系统未能完成本次分析。' }}</p>
          <p class="hint">本次没有产生成绩。可以请老师重新分析，或重新上传视频。</p>
        </div>
      </div>

      <div v-else-if="displayScore === null" class="score-card pending">
        <div>
          <h3>{{ STATUS_TEXT[job.status] || job.status }}</h3>
          <p v-if="job.status === 'pending'">
            前面还有 {{ job.queue_position ?? 0 }} 个视频在排队，预计等待约
            {{ Math.max(1, Math.round((job.estimated_wait_sec ?? 0) / 60)) }} 分钟。
          </p>
          <p v-else>{{ job.stage || '正在处理，请稍候…' }}</p>
          <p class="hint">
            暂无分数：只有从画面中找到足够依据后系统才会给分，
            没有依据时不会用 0 分或满分来充数。
          </p>
        </div>
      </div>

      <div v-else class="report-card">
        <span class="score">{{ displayScore }}</span>
        <div>
          <h3>{{ report?.conclusion || job.summary || '分析完成' }}</h3>
          <p v-if="report?.strengths">{{ report.strengths }}</p>
          <p v-if="report && report.score !== null && report.final_score !== null
                   && report.final_score !== report.score" class="hint">
            机器评分 {{ report.score }} 分，经教师复核后调整为 {{ report.final_score }} 分。
          </p>
          <p v-if="!isConfirmed" class="hint">
            {{ detail.disclaimer }}
          </p>
          <p v-else class="hint">
            本成绩已由教师复核确认{{ report.reviewed_at ? `（${formatDateTime(report.reviewed_at)}）` : '' }}。
          </p>
        </div>
      </div>

      <div v-if="job.status === 'running'" class="progress-bar">
        <div class="progress-fill" :style="{ width: `${job.progress || 0}%` }"></div>
      </div>
      <p v-if="job.status === 'running'" class="hint">{{ job.stage }}</p>
    </div>

    <!-- 步骤与扣分 -------------------------------------------------------- -->
    <div class="evidence-pane">
      <h3>标准流程识别</h3>
      <ol class="timeline">
        <li v-for="step in detail.steps" :key="step.id" :class="verdictClass(step.verdict)">
          <time>{{ formatTime(step.start_sec) }}</time>
          <div>
            <strong>
              {{ step.step_name }}
              <span class="badge" :class="verdictClass(step.verdict)">
                {{ VERDICT_TEXT[step.verdict] || step.verdict }}
              </span>
              <span v-if="step.source === 'human'" class="badge human">教师改判</span>
              <span v-if="step.needs_review" class="badge warn">待人工确认</span>
            </strong>
            <p>{{ step.evidence }}</p>
            <p v-if="step.validation_note" class="hint">{{ step.validation_note }}</p>

            <div v-if="step.evidence_frames?.length" class="frame-strip">
              <button
                v-for="index in step.evidence_frames"
                :key="index"
                class="frame-thumb"
                type="button"
                :title="`跳到视频第 ${formatTime(step.start_sec)}`"
                @click="seekTo(step.start_sec)"
              >
                <img
                  v-if="submission"
                  :src="api.frameUrl(submission.id, index)"
                  :alt="`证据画面 ${index}`"
                  loading="lazy"
                />
              </button>
            </div>

            <div v-if="canReview" class="review-actions">
              <button
                v-for="(label, verdict) in { completed: '判为已完成', not_completed: '判为未完成', not_visible: '判为看不见' }"
                :key="verdict"
                class="ghost-button tiny"
                :disabled="busy || isConfirmed || step.verdict === verdict"
                @click="setStepVerdict(step.id, verdict)"
              >
                {{ label }}
              </button>
            </div>
          </div>
        </li>
      </ol>

      <h3>扣分项</h3>
      <div v-if="detail.violations.length" class="violations">
        <article
          v-for="item in detail.violations"
          :key="item.id"
          class="violation"
          :class="{ dismissed: item.status === 'dismissed' }"
        >
          <div>
            <strong>{{ item.title }}</strong>
            <span>
              扣 {{ item.deduction }} 分 ·
              {{ item.status === 'confirmed' ? '已确认'
                 : item.status === 'dismissed' ? '已驳回（误判）' : '待复核' }}
              <template v-if="item.timestamp_sec !== null">
                · <button class="link-button" type="button" @click="seekTo(item.timestamp_sec)">
                  跳到 {{ formatTime(item.timestamp_sec) }}
                </button>
              </template>
            </span>
          </div>
          <p>{{ item.reason }}</p>
          <small>{{ item.suggestion }}</small>
          <small v-if="item.review_comment">教师意见：{{ item.review_comment }}</small>

          <div v-if="canReview && !isConfirmed && item.status === 'auto'" class="review-actions">
            <button class="ghost-button tiny" :disabled="busy" @click="reviewViolation(item.id, 'confirm')">
              确认扣分
            </button>
            <button class="ghost-button tiny" :disabled="busy" @click="reviewViolation(item.id, 'dismiss')">
              属于误判，撤销
            </button>
          </div>
        </article>
      </div>
      <p v-else class="empty">本次分析没有产生扣分项。</p>

      <!-- 教师终审 -------------------------------------------------------- -->
      <div v-if="canReview" class="finalize-box">
        <h3>教师复核</h3>
        <textarea
          v-model="comment"
          rows="2"
          placeholder="复核意见（会记入审计记录，学生也能看到）"
        ></textarea>
        <p v-if="error" class="login-error">{{ error }}</p>
        <div class="review-actions">
          <button
            v-if="!isConfirmed"
            class="primary-button"
            :disabled="busy || job.status !== 'completed'"
            @click="finalize"
          >
            确认终审
          </button>
          <button v-else class="ghost-button" :disabled="busy" @click="reopen">
            撤回终审，重新复核
          </button>
        </div>
        <p v-if="job.status !== 'completed' && !isConfirmed" class="hint">
          分析完成后才能终审。
        </p>
      </div>
    </div>
  </div>
</template>
