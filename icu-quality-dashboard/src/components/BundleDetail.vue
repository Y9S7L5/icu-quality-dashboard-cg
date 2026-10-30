<template>
  <div class="bundle-detail">
    <!-- 患者基本信息 -->
    <div class="info-card">
      <div class="info-header">
        <span class="info-icon">👤</span>
        <span class="info-name">{{ patient.name || '—' }}</span>
        <span class="info-mrn">住院号: {{ patient.patient_id || '—' }}</span>
        <span class="info-type" v-if="patient.admission_type">入科类型: {{ patient.admission_type }}</span>
        <span class="info-type" v-if="patient.discharge_type">出科类型: {{ patient.discharge_type }}</span>
        <span class="info-type" v-if="patient.icu_out_time">出科时间: {{ patient.icu_out_time }}</span>
        <span class="info-type" v-if="patient.bed_physician">主治医生: {{ patient.bed_physician }}</span>
      </div>
      <div class="info-row">
        <span class="info-label">📋 入科诊断:</span>
        <span class="info-value">{{ patient.diagnose || '—' }}</span>
      </div>
      <div class="info-row">
        <span class="info-label">⏱️ T0:</span>
        <span class="info-value">{{ data.t0 || patient.t0 || patient.admit_time || '—' }}</span>
        <span class="info-hint">(首条医嘱时间)</span>
      </div>
    </div>

    <!-- P3: 结论横幅 —— 一眼看到卡在哪 -->
    <div class="verdict-banner" :class="verdict.cls">
      <span class="vb-text">{{ verdict.text }}</span>
      <span class="vb-window" v-if="windowLabel">窗口 {{ windowLabel }}</span>
    </div>

    <!-- P3: 门控徽章行（点击展开/收起 K/I/S 明细） -->
    <div class="gate-row" @click="showGates = !showGates" title="点击展开门控判定明细">
      <span class="gate-label">门控</span>
      <span class="gate-badge" :class="gateCls(gateInfection)">{{ gateText(gateInfection, '感染') }}</span>
      <span class="gate-badge" :class="gateCls(gateOrgan)">{{ gateText(gateOrgan, '器官') }}</span>
      <span class="gate-badge" :class="gateCls(gateShock)">{{ gateText(gateShock, '休克') }}</span>
      <span class="gate-toggle">{{ showGates ? '▼' : '▶' }}</span>
    </div>

    <!-- 候选引擎状态 -->
    <div class="candidate-card" v-if="patient.candidate_status && patient.candidate_status !== 'not_candidate'">
      <div class="card-title" @click="showCandidate = !showCandidate">
        <span>🎯 候选引擎判定
          <span class="candidate-badge" :class="candidateBadgeClass">{{ candidateStatusLabel }}</span>
          <span class="candidate-pathway" v-if="candidatePathways.length">路径 {{ candidatePathways.join(', ') }}</span>
        </span>
        <span class="guide-toggle">{{ showCandidate ? '▼' : '▶' }}</span>
      </div>
      <div v-if="showCandidate" class="candidate-detail">
        <div class="candidate-row">
          <span class="candidate-label">候选状态:</span>
          <span class="candidate-value" :class="candidateBadgeClass">{{ candidateStatusLabel }}</span>
        </div>
        <div class="candidate-row">
          <span class="candidate-label">临床确认:</span>
          <span class="candidate-value">{{ confirmationStatusLabel }}</span>
        </div>
        <div class="candidate-row" v-if="candidatePathways.length">
          <span class="candidate-label">纳入路径:</span>
          <span class="candidate-value">{{ pathwayDescription }}</span>
        </div>
        <div class="candidate-row" v-if="candidateReasons.length">
          <span class="candidate-label">判定原因:</span>
          <div class="candidate-details">
            <div v-for="(reason, idx) in candidateReasons" :key="idx" class="detail-item">
              <span class="detail-icon positive">✓</span>
              <span class="detail-text">{{ reason }}</span>
            </div>
          </div>
        </div>
        <div class="candidate-row" v-if="supportingEvidence.length">
          <span class="candidate-label">支持证据:</span>
          <div class="candidate-details">
            <div v-for="(ev, idx) in supportingEvidence" :key="idx" class="detail-item">
              <span class="detail-icon positive">✓</span>
              <span class="detail-text">{{ ev }}</span>
            </div>
          </div>
        </div>
        <div class="candidate-row" v-if="missingEvidence.length">
          <span class="candidate-label">缺失证据:</span>
          <div class="candidate-details">
            <div v-for="(ev, idx) in missingEvidence" :key="idx" class="detail-item">
              <span class="detail-icon neutral">?</span>
              <span class="detail-text">{{ ev }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 感染部位选择 -->
    <div class="infection-site-card">
      <div class="card-title" @click="showInfectionSite = !showInfectionSite">
        <span>🦠 感染部位确认</span>
        <span class="guide-toggle">{{ showInfectionSite ? '▼' : '▶' }}</span>
      </div>
      <InfectionSiteSelector v-if="showInfectionSite" :pid="patient.sc_pid || patient.patient_id || ''" />
    </div>

    <!-- SOFA 评分卡片 -->
    <div class="sofa-card" v-if="data.sofa">
      <div class="card-title" @click="showSofa = !showSofa">
        <span>📊 SOFA 评分
          <span class="sofa-summary" v-if="data.sofa.sofa2_score != null">
            (SOFA-2: {{ data.sofa.sofa2_score }}分)
          </span>
          <span class="sofa-summary" v-else-if="data.sofa.classic_score != null">
            (经典SOFA: {{ data.sofa.classic_score }}分)
          </span>
        </span>
        <span class="guide-toggle">{{ showSofa ? '▼' : '▶' }}</span>
      </div>
      <SofaScorePanel
        v-if="showSofa"
        :classicScore="data.sofa.classic_components ? { sofa_score: data.sofa.classic_score, components: data.sofa.classic_components, result_status: data.sofa.classic_result_status, completeness: data.sofa.classic_completeness, data_quality_flags: [] } : null"
        :sofa2Score="data.sofa.sofa2_components ? { sofa2_score: data.sofa.sofa2_score, components: data.sofa.sofa2_components, result_status: data.sofa.sofa2_result_status, completeness: data.sofa.sofa2_completeness, data_quality_flags: data.sofa.sofa2_data_quality_flags || [] } : null"
        :sofaData="data.sofa"
        :clinicalLayer="data.clinical_layer"
      />
    </div>

    <!-- ====== 门控判定明细（分子/分母通用，默认折叠，点徽章行展开） ====== -->
    <template v-if="showGates">
      <!-- K组 - 脓毒性休克确认 -->
      <div class="group-card">
        <div class="card-title">🩺 脓毒性休克确认 (K1 AND K2)</div>
        <div class="group-items">
          <div class="group-item">
            <span class="item-label">K1 血乳酸 ≥2 mmol/L</span>
            <StatusBadge :value="data.k1" />
            <span class="item-detail" v-if="data.lactate != null">{{ fmtNum(data.lactate) }} mmol/L {{ data.lactate_time ? `@${data.lactate_time}` : '' }}</span>
            <span class="item-detail" v-else>未测量</span>
          </div>
          <div class="group-item">
            <span class="item-label">K2 升压药使用</span>
            <StatusBadge :value="data.k2" />
            <span class="item-detail" v-if="data.vaso_name">{{ data.vaso_name }} @{{ data.vaso_start_time || '—' }}</span>
            <span class="item-detail" v-else>未使用</span>
          </div>
        </div>
        <div class="group-result">
          <span class="result-label">→ 脓毒性休克确认</span>
          <StatusBadge :value="data.is_septic_shock" />
        </div>
      </div>

      <!-- I组 - 感染证据 -->
      <div class="group-card">
        <div class="card-title">🦠 感染证据 (I1 ∨ I2 ∨ I3 任一成立)</div>
        <div class="group-items">
          <div class="group-item">
            <span class="item-label">I1 诊断含感染关键词</span>
            <StatusBadge :value="data.i1" />
            <span class="item-detail">{{ truncate(patient.diagnose, 30) || '—' }}</span>
          </div>
          <div class="group-item">
            <span class="item-label">I2 抗感染治疗执行</span>
            <StatusBadge :value="data.i2" />
            <span class="item-detail" v-if="data.antibiotic_name">{{ data.antibiotic_name }} @{{ data.antibiotic_time || '—' }}</span>
            <span class="item-detail" v-else>未执行</span>
          </div>
          <div class="group-item">
            <span class="item-label">I3 病原学送检</span>
            <StatusBadge :value="data.i3" />
            <span class="item-detail" v-if="data.culture_time">{{ data.culture_name }} @{{ data.culture_time }}</span>
            <span class="item-detail" v-else>未送检</span>
          </div>
        </div>
        <div class="group-result">
          <span class="result-label">→ 感染证据确认</span>
          <StatusBadge :value="data.i1 || data.i2 || data.i3" />
        </div>
      </div>

      <!-- S组 - 器官功能障碍 (辅助信号，非门控依据) -->
      <div class="group-card">
        <div class="card-title">🫀 器官功能障碍 — 辅助信号
          <span class="aux-hint">(S1-S4 为原始数据信号，SOFA 评分见上方卡片)</span>
        </div>
        <div class="group-items">
          <div class="group-item">
            <span class="item-label">S1 氧合指数 &lt;300</span>
            <StatusBadge :value="data.s1" />
            <span class="item-detail" v-if="data.pf_ratio != null">{{ data.pf_ratio }} {{ data.pf_ratio_time ? `@${data.pf_ratio_time}` : '' }}</span>
            <span class="item-detail" v-else>未测量</span>
          </div>
          <div class="group-item">
            <span class="item-label">S2 GCS &lt;13</span>
            <StatusBadge :value="data.s2" />
            <span class="item-detail" v-if="data.gcs != null">{{ data.gcs }}分 {{ data.gcs_time ? `@${data.gcs_time}` : '' }}</span>
            <span class="item-detail" v-else>未测量</span>
          </div>
          <div class="group-item">
            <span class="item-label">S3 MAP &lt;70 mmHg</span>
            <StatusBadge :value="data.s3" />
            <span class="item-detail" v-if="data.map != null">{{ data.map }} mmHg {{ data.map_time ? `@${data.map_time}` : '' }}</span>
            <span class="item-detail" v-else>未测量</span>
          </div>
          <div class="group-item">
            <span class="item-label">S4 血管活性药 (VASO_WIDE)</span>
            <StatusBadge :value="data.s4" />
            <span class="item-detail" v-if="data.vaso_name">{{ data.vaso_name }}</span>
            <span class="item-detail" v-else>未使用</span>
          </div>
        </div>
        <div class="group-result">
          <span class="result-label">→ S1-S4 辅助信号</span>
          <StatusBadge :value="data.s1 || data.s2 || data.s3 || data.s4" />
          <span class="result-path">注意: S4 ≠ K2，门控使用 SOFA 评分</span>
        </div>
      </div>
    </template>

    <!-- ====== Bundle 执行情况（分子/分母通用） ====== -->
    <template>
      <!-- Bundle 时间线 -->
      <div class="timeline-card">
        <div class="card-title">
          ⏱️ Bundle 时间线
          <span class="tl-window" v-if="windowLabel">判定窗口 {{ windowLabel }}</span>
        </div>
        <div class="timeline">
          <div class="timeline-item active">
            <div class="timeline-dot"></div>
            <div class="timeline-content">
              <div class="timeline-label">T0</div>
              <div class="timeline-value">{{ formatTime(data.t0 || patient.t0) }}</div>
            </div>
          </div>
          <div class="timeline-line"></div>
          <div class="timeline-item" :class="{ active: data.lactate != null }">
            <div class="timeline-dot" :class="statusClass(data.a1)"></div>
            <div class="timeline-content">
              <div class="timeline-label">乳酸</div>
              <div class="timeline-value">{{ data.lactate != null ? `${fmtNum(data.lactate)} mmol/L` : '—' }}</div>
              <div class="timeline-time" v-if="data.lactate_time">{{ data.lactate_time }}</div>
            </div>
          </div>
          <div class="timeline-line"></div>
          <div class="timeline-item" :class="{ active: lactate1h3hCount > 0 }" @click="showLactateTable = !showLactateTable" style="cursor:pointer">
            <div class="timeline-dot" :class="lactate1h3hCount > 0 ? 'status-ok' : 'status-na'"></div>
            <div class="timeline-content">
              <div class="timeline-label">1h—3h乳酸</div>
              <div class="timeline-value">{{ lactate1h3hCount > 0 ? `${lactate1h3hCount}次` : '—' }}</div>
              <div class="timeline-hint" v-if="lactate1h3hCount > 0">点击展开</div>
            </div>
          </div>
          <div class="timeline-line"></div>
          <div class="timeline-item" :class="{ active: !!data.culture_time }">
            <div class="timeline-dot" :class="statusClass(data.step2)"></div>
            <div class="timeline-content">
              <div class="timeline-label">血培养</div>
              <div class="timeline-value">{{ data.culture_name || '—' }}</div>
              <div class="timeline-time" v-if="data.culture_time">{{ data.culture_time }}</div>
            </div>
          </div>
          <div class="timeline-line"></div>
          <div class="timeline-item" :class="{ active: !!data.antibiotic_time }">
            <div class="timeline-dot" :class="statusClass(data.antibiotic_time)"></div>
            <div class="timeline-content">
              <div class="timeline-label">抗生素</div>
              <div class="timeline-value">{{ data.antibiotic_name || '—' }}</div>
              <div class="timeline-time" v-if="data.antibiotic_time">{{ data.antibiotic_time }}</div>
            </div>
          </div>
          <div class="timeline-line"></div>
          <div class="timeline-item" :class="{ active: (data.fluid_ml || 0) > 0 }">
            <div class="timeline-dot" :class="statusClass(data.step3)"></div>
            <div class="timeline-content">
              <div class="timeline-label">液体</div>
              <div class="timeline-value">{{ data.fluid_ml ? `${data.fluid_ml} ml` : '—' }}</div>
              <div class="timeline-hint" v-if="data.fluid_ml && data.fluid_ml < 1500 && data.step3 === false">不足1500ml</div>
            </div>
          </div>
          <div class="timeline-line"></div>
          <div class="timeline-item" :class="{ active: data.finish != null }">
            <div class="timeline-dot" :class="statusClass(data.finish)"></div>
            <div class="timeline-content">
              <div class="timeline-label">完成判定</div>
              <div class="timeline-value">{{ finishLabel }}</div>
              <div class="timeline-hint" v-if="data.finish === true && data.finish_path">
                {{ data.finish_path === 'triggered' ? '触发→液体达标' : '未触发→视为达标' }}
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 乳酸完整记录表格 -->
      <div v-if="showLactateTable && data.lactate_all?.length" class="lactate-card">
        <div class="card-title">🔬 乳酸完整记录 · {{ data.lactate_all.length }}次</div>
        <div class="lactate-table-wrap">
          <table class="lactate-table">
            <thead>
              <tr>
                <th>采样时间</th>
                <th>距T0</th>
                <th>乳酸(mmol/L)</th>
                <th>所属时段</th>
                <th>数据来源</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(lac, idx) in data.lactate_all" :key="idx" :class="lacRowClass(lac)">
                <td>{{ lac.sample_time ? formatFullTime(lac.sample_time) : '—' }}</td>
                <td>{{ lac.minutes_from_t0 != null ? fmtMinutes(lac.minutes_from_t0) : '—' }}</td>
                <td class="lac-value">{{ fmtNum(lac.value) }}</td>
                <td><span class="lac-period-tag" :class="lac.period_label">{{ lac.period_label }}</span></td>
                <td class="lac-source">{{ lac.source || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="lactate-note">
          采样时间为血气分析仪采集时刻；距T0分钟数仅供参考；1h—3h分组使用 (T0+1h, T0+3h]。
        </div>
      </div>
      <div v-else-if="showLactateTable && lactateAllCount === 0" class="lactate-card">
        <div class="card-title">🔬 乳酸完整记录</div>
        <div class="lactate-empty">窗口内无乳酸测量记录</div>
      </div>

      <!-- Bundle 完成情况 -->
      <div class="group-card">
        <div class="card-title">📦 Bundle 完成判定</div>
        <div class="group-items">
          <div class="group-item">
            <span class="item-label">第一步 A1 乳酸测定</span>
            <StatusBadge :value="data.a1" />
            <span class="item-detail" v-if="data.lactate != null">{{ fmtNum(data.lactate) }} mmol/L {{ data.lactate_time ? `@${data.lactate_time}` : '' }}</span>
            <span class="item-detail" v-else>未测量</span>
          </div>
          <div class="group-item">
            <span class="item-label">第二步 B3 血培养先于抗生素</span>
            <StatusBadge :value="data.b3" />
            <span class="item-detail" v-if="data.culture_time && data.antibiotic_time">
              培养@{{ data.culture_time }} &lt; 抗生素@{{ data.antibiotic_time }}
            </span>
            <span class="item-detail" v-else>数据不完整</span>
          </div>
          <div class="group-item">
            <span class="item-label">第三步 C1 MAP&lt;70 触发</span>
            <StatusBadge :value="data.c1" />
            <span class="item-detail" v-if="data.map != null">{{ data.map }} mmHg</span>
            <span class="item-detail" v-else>未测量</span>
          </div>
          <div class="group-item">
            <span class="item-label">第三步 C2 乳酸≥4 触发</span>
            <StatusBadge :value="data.c2" />
            <span class="item-detail" v-if="data.lactate_max != null">{{ fmtNum(data.lactate_max) }} mmol/L</span>
            <span class="item-detail" v-else>未测量</span>
          </div>
          <div class="group-item">
            <span class="item-label">第三步 C3 液体达标</span>
            <StatusBadge :value="data.c3" />
            <span class="item-detail" v-if="data.fluid_ml">{{ data.fluid_ml }} ml</span>
            <span class="item-detail" v-else>无液体</span>
          </div>
        </div>
        <div class="group-result">
          <span class="result-label">→ Bundle 完成</span>
          <StatusBadge :value="data.finish" />
          <span class="result-path" v-if="data.finish">完成路径: {{ data.finish_path || '—' }}</span>
        </div>
      </div>
    </template>

    <!-- 原因码展示（全部原因码，不再只显示第一条） -->
    <div class="reason-card" v-if="reasonCodes.length">
      <div class="card-title">ℹ️ 判定说明</div>
      <div class="reason-list">
        <div class="reason-item" v-for="code in reasonCodes" :key="code">
          <span class="reason-code">{{ code }}</span>
          <span class="reason-text">{{ REASON_MAP[code] || code }}</span>
        </div>
      </div>
    </div>

    <!-- 数据质量标记 -->
    <div class="quality-card" v-if="data.data_quality_flags && data.data_quality_flags.length > 0">
      <div class="card-title">⚠️ 数据质量标记</div>
      <div class="quality-list">
        <div class="quality-item" v-for="flag in data.data_quality_flags" :key="flag">
          {{ FLAG_LABELS[flag] || flag }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import StatusBadge from './StatusBadge.vue'
import InfectionSiteSelector from './InfectionSiteSelector.vue'
import SofaScorePanel from './SofaScorePanel.vue'
import { verdictOf, REASON_MAP } from '../utils/detailColumns.js'

const props = defineProps({
  data: { type: Object, default: () => ({}) },
  patient: { type: Object, default: () => ({}) },
  part: { type: String, default: 'denominator' }, // 'denominator' | 'numerator'
  hour: { type: String, default: '' }, // '1h' | '3h' | '6h'
})

const showInfectionSite = ref(false)
const showLactateTable = ref(false)
const showSofa = ref(false)
const showCandidate = ref(false)
const showGates = ref(false)

// ── P3: 结论横幅 ──
const verdict = computed(() => verdictOf({ v3: props.data, excluded: props.patient.excluded }))

// ── P3: 判定窗口标签 ──
const windowLabel = computed(() => (props.hour ? `T0 ~ T0+${props.hour}` : ''))

const finishLabel = computed(() => {
  if (props.data.finish === true) return '达标'
  if (props.data.finish === false) return '未达标'
  if (props.data.finish === null) return '无法判定'
  return '—'
})

// ── P3: 门控三态徽章 ──
function tri(vals) {
  if (vals.some(v => v === true)) return true
  if (vals.some(v => v === false)) return false
  return null
}
const gateInfection = computed(() => tri([props.data.i1, props.data.i2, props.data.i3]))
const gateOrgan = computed(() => tri([props.data.s1, props.data.s2, props.data.s3, props.data.s4]))
const gateShock = computed(() => {
  const d = props.data
  if (d.k1 === true && d.k2 === true) return true
  if (d.k1 === false || d.k2 === false) return false
  return null
})
const gateCls = (v) => (v === true ? 'gb-ok' : v === false ? 'gb-fail' : 'gb-na')
const gateText = (v, label) => (v === true ? `${label} ✓` : v === false ? `${label} ✗` : `${label} ?`)

// ── 原因码列表（全部） ──
const reasonCodes = computed(() => {
  const d = props.data
  if (d.reason_codes && d.reason_codes.length) return d.reason_codes
  return d.reason ? [d.reason] : []
})

// 候选引擎状态映射
const CANDIDATE_STATUS_MAP = {
  'high_probability': '高概率脓毒性休克',
  'probable': '很可能脓毒性休克',
  'pending_review': '待人工复核',
  'not_candidate': '非候选',
}
const CONFIRMATION_STATUS_MAP = {
  'confirmed': '符合电子确认规则',
  'pending_review': '待人工复核',
  'insufficient': '证据不足',
}
const FLAG_LABELS = {
  'respiratory_missing': '呼吸数据缺失',
  'hemostasis_missing': '止血数据缺失',
  'liver_missing': '肝脏数据缺失',
  'brain_missing': '脑功能数据缺失',
  'kidney_missing': '肾脏数据缺失',
  'cardiovascular_missing': '心血管数据缺失',
  'respiratory_stale': '呼吸数据过期',
  'hemostasis_stale': '止血数据过期',
  'liver_stale': '肝脏数据过期',
  'kidney_creatinine_stale': '肌酐数据过期',
  'kidney_urine_stale': '尿量数据过期',
  'hemostasis_out_of_range': '止血数值超范围',
  'kidney_creatinine_out_of_range': '肌酐数值超范围',
  'urine_unit_unknown': '尿量单位未知',
  'route_unknown': '氧疗途径未知',
}
const PATHWAY_MAP = {
  'diagnosis': '通道A - 明确诊断',
  'strong_shock': '通道B - 强休克证据',
  'composite': '通道C - 组合证据',
  'pending_incomplete': '通道D - 待复核',
  'sofa2_supplement': 'SOFA-2补充',
  'engine_error': '引擎异常-待复核',
  // 兼容旧格式
  'combined_evidence': '通道C - 组合证据',
  'A': '通道A - 明确诊断',
  'B': '通道B - 强休克证据',
  'C': '通道C - 组合证据',
  'D': '通道D - 待复核',
}

const candidateStatusLabel = computed(() => {
  return CANDIDATE_STATUS_MAP[props.patient.candidate_status] || props.patient.candidate_status || '—'
})

const confirmationStatusLabel = computed(() => {
  return CONFIRMATION_STATUS_MAP[props.patient.clinical_confirmation_status] || props.patient.clinical_confirmation_status || '—'
})

const candidateBadgeClass = computed(() => {
  const status = props.patient.candidate_status
  if (status === 'high_probability') return 'candidate-high'
  if (status === 'probable') return 'candidate-probable'
  if (status === 'pending_review') return 'candidate-pending'
  return 'candidate-none'
})

const pathwayDescription = computed(() => {
  // 从 candidate_pathways 列表中取第一个
  const pathways = props.patient.candidate_pathways || props.patient.candidate_info?.candidate_pathways || []
  const pathway = pathways[0] || props.patient.candidate_info?.pathway
  return PATHWAY_MAP[pathway] || pathway || '—'
})

const candidatePathways = computed(() => {
  return props.patient.candidate_pathways || props.patient.candidate_info?.candidate_pathways || []
})

const candidateReasons = computed(() => {
  return props.patient.candidate_reasons || props.patient.candidate_info?.candidate_reasons || []
})

const supportingEvidence = computed(() => {
  return props.patient.supporting_evidence || props.patient.candidate_info?.supporting_evidence || []
})

const missingEvidence = computed(() => {
  return props.patient.missing_evidence || props.patient.candidate_info?.missing_evidence || []
})

const lactate1h3hCount = computed(() => {
  return (props.data.lactate_all || []).filter(l => l.period_label === '1h—3h').length
})
const lactateAllCount = computed(() => (props.data.lactate_all || []).length)

function fmtNum(val) {
  if (val == null) return '—'
  return Number(val).toFixed(2)
}

function formatTime(t) {
  if (!t) return '—'
  if (typeof t === 'string') return t.slice(5)
  return String(t)
}

function truncate(str, len) {
  if (!str) return ''
  return str.length > len ? str.slice(0, len) + '...' : str
}

function statusClass(value) {
  if (value === true) return 'status-ok'
  if (value === false) return 'status-fail'
  return 'status-na'
}

function formatFullTime(t) {
  if (!t) return '—'
  // ISO string → "MM-DD HH:mm"
  const s = String(t)
  if (s.length >= 16) return s.slice(5, 16).replace('T', ' ')
  return s
}

function fmtMinutes(mins) {
  if (mins == null) return '—'
  const abs = Math.abs(mins)
  if (abs < 60) return `${Math.round(mins)}min`
  const h = Math.floor(abs / 60)
  const m = Math.round(abs % 60)
  const sign = mins < 0 ? '-' : '+'
  return m > 0 ? `${sign}${h}h${m}m` : `${sign}${h}h`
}

function lacRowClass(lac) {
  if (lac.period_label === '1h—3h') return 'lac-row-1h3h'
  if (lac.period_label === '3h—6h') return 'lac-row-3h6h'
  return ''
}
</script>

<style scoped>
.bundle-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;
  background: var(--bg-subtle);
  border-radius: 8px;
}

.info-card, .timeline-card, .group-card, .reason-card, .quality-card, .infection-site-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}

/* ── P3: 结论横幅 ── */
.verdict-banner {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 10px 16px; border-radius: 8px; font-weight: 600; font-size: 14px;
  border: 1px solid transparent;
}
.verdict-banner.v-ok { background: #e6f6ec; color: #0a7d33; border-color: #b7e4c8; }
.verdict-banner.v-fail { background: #fdecea; color: #c62828; border-color: #f5c6c2; }
.verdict-banner.v-order { background: #fff3e0; color: #e65100; border-color: #ffcc80; }
.verdict-banner.v-missing { background: #eef1f6; color: #5f6b7f; border-color: #d8dee9; }
.verdict-banner.v-gate { background: #e8eaf6; color: #3949ab; border-color: #c5cae9; }
.verdict-banner.v-excluded { background: #f0f0f0; color: #7a8699; }
.vb-window { font-size: 12px; font-weight: 500; opacity: 0.85; white-space: nowrap; }

/* ── P3: 门控徽章行 ── */
.gate-row {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  background: var(--bg-surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 16px; user-select: none;
}
.gate-row:hover { background: var(--bg-hover); }
.gate-label { font-size: 12px; color: var(--text-sub); font-weight: 600; margin-right: 2px; }
.gate-badge {
  font-size: 12px; padding: 2px 10px; border-radius: 12px; font-weight: 600;
}
.gate-badge.gb-ok { background: #e6f6ec; color: #0a7d33; }
.gate-badge.gb-fail { background: #fdecea; color: #c62828; }
.gate-badge.gb-na { background: #eef1f6; color: #7a8699; }
.gate-toggle { margin-left: auto; color: var(--text-sub); font-size: 10px; }

/* ── P3: 时间线窗口标签 ── */
.tl-window {
  font-size: 12px; font-weight: 500; color: var(--brand);
  background: var(--brand-weak); border-radius: 10px; padding: 2px 10px; margin-left: 8px;
}

.info-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.info-icon { font-size: 1.2em; }
.info-name { font-weight: 600; color: var(--text-title); font-size: 1.1em; }
.info-mrn { color: var(--text-sub); font-family: monospace; }
.info-type { background: var(--brand-weak); color: var(--brand); padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }

.info-row { display: flex; align-items: baseline; gap: 8px; margin-top: 4px; font-size: 0.9em; }
.info-label { color: var(--text-sub); white-space: nowrap; }
.info-value { color: var(--text-body); }
.info-hint { color: var(--text-sub); font-size: 0.85em; }

.card-title { font-weight: 600; color: var(--text-title); margin-bottom: 12px; font-size: 0.95em; }

/* Timeline */
.timeline { display: flex; align-items: flex-start; gap: 0; overflow-x: auto; padding: 8px 0; }
.timeline-item { display: flex; flex-direction: column; align-items: center; min-width: 100px; opacity: 0.4; }
.timeline-item.active { opacity: 1; }
.timeline-dot { width: 12px; height: 12px; border-radius: 50%; background: var(--border); margin-bottom: 8px; }
.timeline-dot.status-ok { background: var(--success, #10b981); }
.timeline-dot.status-fail { background: var(--danger, #ef4444); }
.timeline-dot.status-na { background: var(--border); }
.timeline-line { flex: 1; height: 2px; background: var(--border); margin-top: 5px; min-width: 20px; }
.timeline-content { text-align: center; }
.timeline-label { font-weight: 600; color: var(--text-sub); font-size: 0.8em; margin-bottom: 4px; }
.timeline-value { color: var(--text-body); font-size: 0.85em; white-space: nowrap; }
.timeline-time { color: var(--text-sub); font-size: 0.75em; }

/* Group items */
.group-items { display: flex; flex-direction: column; gap: 8px; }
.group-item { display: flex; align-items: center; gap: 12px; padding: 6px 0; border-bottom: 1px solid var(--border-light); }
.group-item:last-child { border-bottom: none; }
.item-label { min-width: 180px; color: var(--text-sub); font-size: 0.9em; }
.item-detail { color: var(--text-body); font-size: 0.85em; flex: 1; }

.group-result { display: flex; align-items: center; gap: 12px; margin-top: 12px; padding-top: 12px; border-top: 2px solid var(--border); }
.result-label { font-weight: 600; color: var(--text-title); }
.result-path { color: var(--text-sub); font-size: 0.85em; margin-left: 8px; }

/* Reason */
.reason-list { display: flex; flex-direction: column; gap: 8px; }
.reason-item { display: flex; align-items: center; gap: 12px; }
.reason-code { background: var(--bg-subtle); color: var(--text-sub); padding: 2px 8px; border-radius: 4px; font-family: monospace; font-size: 0.85em; }
.reason-text { color: var(--text-body); font-size: 0.9em; }

/* Quality */
.quality-list { display: flex; flex-direction: column; gap: 4px; }
.quality-item { color: var(--warn, #f59e0b); font-size: 0.85em; }

.infection-site-card .card-title { cursor: pointer; display: flex; justify-content: space-between; align-items: center; margin-bottom: 0; }
.guide-toggle { color: var(--text-sub); font-size: 0.85em; }

/* 乳酸完整记录 */
.lactate-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}
.lactate-table-wrap { overflow: auto; max-height: 320px; }
.lactate-table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
.lactate-table th {
  background: #CBD7F5; color: #1f2a44; font-weight: 600;
  padding: 8px 10px; text-align: left; font-size: 12px;
  border-bottom: 1px solid #b0c4f0;
}
.lactate-table td {
  padding: 7px 10px; border-bottom: 1px solid var(--border-light); color: var(--text-body);
}
.lactate-table tbody tr:hover td { background: #f0f4fd; }
.lac-value { font-family: 'Cascadia Code', 'Consolas', monospace; font-weight: 600; color: var(--text-title); }
.lac-period-tag {
  display: inline-block; padding: 1px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
  background: #eaf1fb; color: #1e5eb8;
}
.lactate-table tr.lac-row-1h3h td { background: rgba(52, 78, 132, 0.04); }
.lactate-table tr.lac-row-3h6h td { background: rgba(178, 106, 0, 0.04); }
.lac-source { font-size: 11px; color: var(--text-sub); }
.lactate-note { margin-top: 8px; font-size: 11px; color: var(--text-sub); line-height: 1.5; }
.lactate-empty { text-align: center; padding: 20px; color: var(--text-sub); font-size: 0.9em; }
.timeline-hint { font-size: 10px; color: var(--text-sub); opacity: 0.7; }

/* SOFA 卡片 */
.sofa-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}
.sofa-card .card-title { cursor: pointer; display: flex; justify-content: space-between; align-items: center; margin-bottom: 0; }
.sofa-summary { font-weight: 400; color: var(--text-sub); font-size: 0.9em; }
.aux-hint { font-weight: 400; color: var(--text-sub); font-size: 0.8em; margin-left: 8px; }

/* 候选引擎卡片 */
.candidate-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}
.candidate-card .card-title { cursor: pointer; display: flex; justify-content: space-between; align-items: center; margin-bottom: 0; }
.candidate-badge {
  display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-size: 12px; font-weight: 600; margin-left: 8px;
}
.candidate-high { background: #e8f5e9; color: #2e7d32; }
.candidate-probable { background: #e3f2fd; color: #1565c0; }
.candidate-pending { background: #fff3e0; color: #e65100; }
.candidate-none { background: #f5f5f5; color: #757575; }
.candidate-pathway {
  font-size: 12px; color: var(--text-sub); margin-left: 8px;
  padding: 2px 8px; background: var(--bg-subtle); border-radius: 4px;
}
.candidate-detail {
  margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border-light);
}
.candidate-row {
  display: flex; align-items: flex-start; margin-bottom: 8px; font-size: 0.9em;
}
.candidate-label {
  min-width: 100px; color: var(--text-sub); font-weight: 500; flex-shrink: 0;
}
.candidate-value { color: var(--text-body); }
.candidate-details {
  display: flex; flex-direction: column; gap: 4px;
}
.detail-item {
  display: flex; align-items: center; gap: 6px; font-size: 0.85em;
}
.detail-icon {
  width: 18px; height: 18px; display: flex; align-items: center; justify-content: center;
  border-radius: 50%; font-size: 11px; font-weight: 700; flex-shrink: 0;
}
.detail-icon.positive { background: #e8f5e9; color: #2e7d32; }
.detail-icon.negative { background: #ffebee; color: #c62828; }
.detail-icon.neutral { background: #f5f5f5; color: #757575; }
.detail-text { color: var(--text-body); }
</style>
