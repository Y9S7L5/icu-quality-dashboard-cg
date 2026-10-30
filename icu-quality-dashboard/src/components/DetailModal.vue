<template>
  <div class="dm-root">
    <!-- P2: 公式条 + 人群漏斗（点击可切换列表/筛选） -->
    <div v-if="summary || funnel" class="formula-row">
      <div class="formula-bar" v-if="summary">
        <span class="fb-label">指标公式</span>
        <template v-if="summary.numerator != null && summary.denominator != null">
          <button class="fb-part" :class="{ active: curPart === 'numerator' }"
                  @click="setPart('numerator')" title="点击查看分子列表">
            <b>{{ summary.numerator }}</b><small>分子</small>
          </button>
          <span class="fb-op">/</span>
          <button class="fb-part" :class="{ active: curPart === 'denominator' }"
                  @click="setPart('denominator')" title="点击查看分母列表">
            <b>{{ summary.denominator }}</b><small>分母</small>
          </button>
          <span class="fb-op">=</span>
        </template>
        <span class="fb-val">{{ summary.value ?? '—' }}{{ summary.unit || '' }}</span>
      </div>
      <div class="funnel" v-if="funnel">
        <div class="fn-node" :class="{ clickable: funnel.candidate != null }"
             @click="setVerdictFilter(null)" title="候选池（全部分母）">
          <b>{{ funnel.candidate ?? '—' }}</b><span>候选池</span>
        </div>
        <span class="fn-arrow">→</span>
        <div class="fn-node fn-warn" :class="{ clickable: funnel.shock != null }"
             @click="setVerdictFilter('shock')" title="K1乳酸≥2 且 K2在用升压药">
          <b>{{ funnel.shock ?? '—' }}</b><span>休克确认</span>
        </div>
        <span class="fn-arrow">→</span>
        <div class="fn-node fn-bad" :class="{ clickable: funnel.completed != null }"
             @click="setVerdictFilter('done')" title="完成 Bundle（分子）">
          <b>{{ funnel.completed ?? '—' }}</b><span>Bundle达标</span>
        </div>
      </div>
    </div>

    <div class="source">
      <div class="source-left">
        <span class="tag">口径</span>
        <span class="source-desc">{{ activeData.source_desc }}</span>
        <span v-if="canExclude && excludedCount > 0" class="excl-count">
          ，已人工排除 {{ excludedCount }} 例
        </span>
      </div>
      <div class="source-right">
        <span class="count" v-if="activeData.count > 0">
          共 {{ activeData.count }} 例<span v-if="activeData.has_more">，先显示 {{ activeData.patients?.length || 0 }} 例</span>
        </span>
        <button class="export-btn" :disabled="exporting || !activeData.patients?.length || isSummary"
                @click="handleExport" :title="isSummary ? '汇总数据无需导出' : !activeData.patients?.length ? '无可导出数据' : ''">
          {{ exporting ? '导出中...' : '导出 Excel' }}
        </button>
      </div>
    </div>
    <div v-if="exportProgress || exportError" class="export-status">
      <span v-if="exportProgress" class="export-progress">{{ exportProgress }}</span>
      <span v-if="exportError" class="export-error">{{ exportError }}</span>
    </div>

    <!-- P2: 三色原因分布（红=明确未达标 / 橙=顺序 / 灰=数据缺失·未确认），点击筛选 -->
    <div v-if="isIcu05 && reasonSummary" class="reason-bar">
      <div class="rb-head">
        <span class="rb-title">未达标原因分布</span>
        <span class="rb-legend">
          <i class="dot d-fail"></i>明确未达标
          <i class="dot d-order"></i>顺序问题
          <i class="dot d-missing"></i>数据缺失/未确认
        </span>
      </div>
      <div class="rb-chips">
        <button v-for="r in reasonSummary.failed_reasons" :key="'f'+r.code"
                class="rb-chip" :class="[r.code === 'BC_AFTER_AB' ? 'order' : 'fail', { on: reasonFilter === r.code }]"
                @click="toggleReason(r.code)">
          {{ reasonLabel(r.code) }} ×{{ r.count }}
        </button>
        <button v-for="r in reasonSummary.uncertain_reasons" :key="'u'+r.code"
                class="rb-chip" :class="[isGateReason(r.code) ? 'gate' : 'missing', { on: reasonFilter === r.code }]"
                @click="toggleReason(r.code)">
          {{ reasonLabel(r.code) }} ×{{ r.count }}
        </button>
        <span v-if="!(reasonSummary.failed_reasons || []).length && !(reasonSummary.uncertain_reasons || []).length"
              class="rb-empty">✅ 全部达标</span>
      </div>
      <div class="rb-note" v-if="(reasonSummary.uncertain || 0) > 0">
        灰/蓝灰色为数据缺失或未通过门控（无法判定），<b>不等同于医疗未达标</b>；共 {{ reasonSummary.uncertain }} 例
      </div>
    </div>

    <!-- P2: 分子/分母 切换 tab（两份数据都已加载时） -->
    <div v-if="hasBothParts" class="part-tabs2">
      <button :class="['pt2-btn', { active: curPart === 'numerator' }]" @click="setPart('numerator')">
        分子 ({{ parts.numerator.count }})
      </button>
      <button :class="['pt2-btn', { active: curPart === 'denominator' }]" @click="setPart('denominator')">
        分母 ({{ parts.denominator.count }})
      </button>
      <span v-if="verdictFilter || reasonFilter" class="pt2-reset" @click="clearFilters">清除筛选 ✕</span>
    </div>

    <!-- P2: 结论筛选 chips（ICU-05） -->
    <div v-if="isIcu05 && activeData.patients?.length" class="chip-bar">
      <button :class="['chip', { on: !verdictFilter }]" @click="setVerdictFilter(null)">全部 {{ chipCounts.all }}</button>
      <button v-if="chipCounts.failed" :class="['chip', 'chip-fail', { on: verdictFilter === 'failed' }]"
              @click="setVerdictFilter('failed')">未达标 {{ chipCounts.failed }}</button>
      <button v-if="chipCounts.uncertain" :class="['chip', 'chip-miss', { on: verdictFilter === 'uncertain' }]"
              @click="setVerdictFilter('uncertain')">无法判定 {{ chipCounts.uncertain }}</button>
      <button v-if="chipCounts.pending" :class="['chip', 'chip-pending', { on: verdictFilter === 'pending' }]"
              @click="setVerdictFilter('pending')">待复核 {{ chipCounts.pending }}</button>
      <button v-if="chipCounts.excluded" :class="['chip', 'chip-excl', { on: verdictFilter === 'excluded' }]"
              @click="setVerdictFilter('excluded')">已排除 {{ chipCounts.excluded }}</button>
    </div>

    <div v-if="activeData.loading" class="loading">明细加载中...</div>
    <div v-else-if="activeData.error" class="empty">{{ activeData.error }}</div>
    <div v-else-if="!activeData.patients?.length" class="empty">暂无明细</div>
    <!-- 分母汇总 -->
    <div v-else-if="isSummary" class="den-summary">{{ activeData.patients[0].name }}</div>
    <!-- 三管卡片布局（ICU-16/17/CAUTI） -->
    <div v-else-if="isTriTube" class="tri-list">
      <article v-for="p in activeData.patients" :key="p.detail_id || p.patient_id" class="tri-card">
        <div class="tri-head">
          <div class="tri-person">
            <span class="mono">{{ p.patient_id }}</span>
            <strong>{{ p.name || '—' }}</strong>
          </div>
          <div class="tri-metrics">
            <span v-for="c in columns.slice(2)" :key="c.header">{{ c.get(p) }}</span>
          </div>
        </div>
        <p class="tri-basis">{{ columns[columns.length - 1]?.get(p) }}</p>
      </article>
    </div>
    <!-- 同期总数类指标：原有/新入/出科/(未评) 筛选 -->
    <div v-if="hasPatientType && activeData.patients?.length" class="census-filter">
      <span class="filter-label">筛选：</span>
      <button :class="['filter-btn', { active: !patientTypeFilter }]" @click="patientTypeFilter = ''">全部</button>
      <button :class="['filter-btn', { active: patientTypeFilter === '原有' }]" @click="patientTypeFilter = '原有'">原有</button>
      <button :class="['filter-btn', { active: patientTypeFilter === '新入' }]" @click="patientTypeFilter = '新入'">新入</button>
      <button :class="['filter-btn', { active: patientTypeFilter === '出科' }]" @click="patientTypeFilter = '出科'">出科</button>
      <button v-if="showEvalFilter"
              :class="['filter-btn', { active: patientTypeFilter === evalFilterLabel }]"
              @click="patientTypeFilter = evalFilterLabel">{{ evalFilterLabel }}</button>
      <span class="filter-count">共 {{ filteredPatients.length }} 例</span>
    </div>
    <!-- 通用表格（共享列定义） -->
    <div v-if="activeData.patients?.length && !isSummary && !isTriTube" class="detail-table-wrap">
      <table class="detail-table">
        <thead>
          <tr>
            <th v-if="isIcu05" style="width:30px"></th>
            <th v-for="c in columns" :key="c.header">{{ c.header }}</th>
            <th v-if="canExclude">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="p in filteredPatients" :key="p.detail_id || p.patient_id">
            <tr :class="[
                  rowClass(p),
                  p.excluded ? 'excluded-row' : '',
                  'detail-row',
                  selectedPatientKey === (p.detail_id || p.patient_id) ? 'selected' : ''
                ]"
                :title="p.admission_source === 'low_confidence' ? 'AI判定置信度<0.6，待人工复核' : ''"
                @click="handleRowClick(p)">
              <td v-if="isIcu05" class="expand-cell">
                <span :class="['expand-icon', { expanded: isExpanded(p.patient_id) }]">▶</span>
              </td>
              <td v-for="c in columns" :key="c.header"
                  :class="[{ mono: c.header === '住院号' || c.header === '账号' }, c.cls ? c.cls(p) : '']">
                {{ c.get(p) }}
              </td>
              <td v-if="canExclude" class="action-cell">
                <template v-if="p.excluded">
                  <button class="btn-restore" @click.stop="handleRestore(p)">恢复</button>
                  <span class="reason-tag">{{ getReasonLabel(p.reason_code) }}</span>
                </template>
                <template v-else>
                  <button class="btn-exclude" @click.stop="handleExclude(p)">排除</button>
                </template>
              </td>
            </tr>
            <!-- ICU-05 Bundle详情展开行 -->
            <tr v-if="isIcu05 && isExpanded(p.patient_id)" class="bundle-detail-row">
              <td :colspan="columns.length + 1 + (canExclude ? 1 : 0)">
                <BundleDetail :data="p.v3 || {}" :patient="p" :part="curPart" :hour="windowHour" />
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <div v-if="!filteredPatients.length && activeData.patients?.length" class="filter-empty">
        当前筛选下无匹配患者，<span class="linklike" @click="clearFilters">清除筛选</span>
      </div>
    </div>
  </div>
    <!-- 排除原因弹窗 -->
    <Teleport to="body">
      <div v-if="showExclForm" class="excl-overlay" @click.self="showExclForm=false">
        <div class="excl-dialog">
          <h3>排除患者</h3>
          <div class="excl-field">
            <label>患者：{{ exclForm.name }} ({{ exclForm.patient_id }})</label>
          </div>
          <div class="excl-field">
            <label>排除原因 <span class="required">*</span></label>
            <select v-model="exclForm.reason_code">
              <option value="">请选择</option>
              <option v-for="r in EXCL_REASONS" :key="r.code" :value="r.code">{{ r.label }}</option>
            </select>
          </div>
          <div v-if="exclForm.reason_code === 'other'" class="excl-field">
            <label>补充说明 <span class="required">*</span></label>
            <textarea v-model="exclForm.reason_text" rows="2" placeholder="请填写排除说明"></textarea>
          </div>
          <div class="excl-field">
            <label>操作人</label>
            <input v-model="exclForm.operator" placeholder="姓名或工号" />
          </div>
          <div class="excl-actions">
            <button class="btn-cancel" @click="showExclForm=false">取消</button>
            <button class="btn-confirm" @click="submitExclusion" :disabled="!exclForm.reason_code || (exclForm.reason_code==='other' && !exclForm.reason_text.trim())">确认排除</button>
          </div>
        </div>
      </div>
    </Teleport>
</template>
<script setup>
import { computed, ref, watch } from 'vue';
import { getDetailColumns, REASON_MAP, GATE_REASONS } from '../utils/detailColumns.js';
import { exportDetailExcel } from '../utils/exportExcel.js';
import { addExclusion, removeExclusion } from '../api/index.js';
import BundleDetail from './BundleDetail.vue';

const props = defineProps({
  data: Object,
  // P2: 公式条（分子/分母/值）
  summary: { type: Object, default: null },
  // P2: 两份明细 { numerator, denominator }，提供时内部渲染切换 tab
  parts: { type: Object, default: null },
  // P2: ICU-05 漏斗 { candidate, shock, completed }
  funnel: { type: Object, default: null },
  period: { type: String, default: '' },
  endPeriod: { type: String, default: '' },
  unit: { type: String, default: 'all' },
  unitName: { type: String, default: '' },
});

const emit = defineEmits(['exclusion-changed']);

// ── P2: 活动分部（分子/分母）──
const activePart = ref(null); // null = 跟随 data.part（未提供 parts 时）
const curPart = computed(() => activePart.value || props.data?.part || 'numerator');
const activeData = computed(() => {
  if (props.parts && props.parts[curPart.value]) return props.parts[curPart.value];
  return props.data || {};
});
const hasBothParts = computed(() =>
  !!(props.parts && props.parts.numerator && props.parts.denominator));
function setPart(part) {
  if (props.parts && props.parts[part]) activePart.value = part;
}

// ── P2: 原因分布（来自分母 payload）──
const reasonSummary = computed(() => {
  if (props.parts?.denominator?.reason_summary) return props.parts.denominator.reason_summary;
  if (curPart.value === 'denominator') return props.data?.reason_summary || null;
  return null;
});
const isGateReason = (code) => GATE_REASONS.has(code);
const reasonLabel = (code) => REASON_MAP[code] || code || '';

// ── P2: 结论筛选（chips + 漏斗点击 + 原因点击）──
const verdictFilter = ref(null); // null | failed | uncertain | pending | excluded | shock | done
const reasonFilter = ref(null);  // 原因码字符串

function setVerdictFilter(f) {
  verdictFilter.value = (verdictFilter.value === f && f) ? null : f;
}
function toggleReason(code) {
  reasonFilter.value = reasonFilter.value === code ? null : code;
}
function clearFilters() {
  verdictFilter.value = null;
  reasonFilter.value = null;
}

const chipCounts = computed(() => {
  const list = activeData.value?.patients || [];
  const c = { all: list.length, failed: 0, uncertain: 0, pending: 0, excluded: 0 };
  for (const p of list) {
    if (p.excluded) { c.excluded++; continue; }
    const v3 = p.v3 || {};
    if (v3.finish === false) c.failed++;
    else if (v3.finish == null && (v3.t0 || v3.reason)) c.uncertain++;
    if (p.candidate_status === 'pending_review') c.pending++;
  }
  return c;
});

function matchVerdict(p) {
  const f = verdictFilter.value;
  if (!f) return true;
  const v3 = p.v3 || {};
  if (f === 'excluded') return !!p.excluded;
  if (f === 'shock') return v3.k1 === true && v3.k2 === true;
  if (f === 'done') return v3.finish === true;
  if (f === 'failed') return v3.finish === false;
  if (f === 'uncertain') return v3.finish == null && (v3.t0 || v3.reason);
  if (f === 'pending') return p.candidate_status === 'pending_review';
  return true;
}
function matchReason(p) {
  if (!reasonFilter.value) return true;
  const v3 = p.v3 || {};
  const codes = v3.reason_codes || (v3.reason ? [v3.reason] : []);
  return codes.includes(reasonFilter.value);
}

// ---- 人工排除 ----
const showExclForm = ref(false);
const exclForm = ref({ exclusion_key: '', patient_id: '', name: '', reason_code: '', reason_text: '', operator: '' });
const EXCL_REASONS_MAP = {
  'ICU-08': [
    { code: 'non_ards', label: '非ARDS原因低氧' },
    { code: 'unstable_gas', label: '氧合数据非稳定状态' },
    { code: 'contraindic', label: '存在俯卧位禁忌症' },
    { code: 'terminal', label: '终末期或家属放弃积极治疗' },
    { code: 'data_error', label: 'PEEP或氧疗途径记录错误' },
    { code: 'other', label: '其他' },
  ],
  'ICU-05': [
    { code: 'not_septic_shock', label: '非脓毒性休克' },
    { code: 'outside_treated', label: '外院已治疗' },
    { code: 'dnr', label: '放弃积极治疗' },
    { code: 't0_wrong', label: 'T0锚点错误' },
    { code: 'data_error', label: '数据错误' },
    { code: 'other', label: '其他' },
  ],
};
const EXCL_REASONS = computed(() => {
  const code = props.data?.code || ''
  if (code.startsWith('ICU-05')) return EXCL_REASONS_MAP['ICU-05']
  return EXCL_REASONS_MAP[code] || EXCL_REASONS_MAP['ICU-08']
});
const EXCLUSION_SUPPORTED_CODES = ['ICU-08', 'ICU-05-1h', 'ICU-05-3h', 'ICU-05-6h'];
const canExclude = computed(() => EXCLUSION_SUPPORTED_CODES.includes(props.data?.code));
const excludedCount = computed(() => (activeData.value?.patients || []).filter(p => p.excluded).length);
const patientTypeFilter = ref('');
// 分母 = 同期ICU患者总数（人次口径）的指标：分子/分母明细都提供 原有/新入/出科/未评 筛选
const TYPE_FILTER_CODES = ['ICU-00', 'ICU-04-01', 'ICU-04-02', 'ICU-04-03', 'ICU-04-04', 'ICU-04-05', 'ICU-04-06', 'ICU-04-07', 'ICU-07', 'ICU-09', 'ICU-10'];
const hasPatientType = computed(() => TYPE_FILTER_CODES.includes(activeData.value?.code));
// 「未评」仅分母维度（ICU-07 是 DVT，标签 未预防；ICU-00 无评估语义）
const evalFilterLabel = computed(() => activeData.value?.code === 'ICU-07' ? '未预防' : '未评');
const showEvalFilter = computed(() =>
  hasPatientType.value && curPart.value === 'denominator' && activeData.value?.code !== 'ICU-00');
const hasDischarged = (v) => !!v && v !== '/' && v !== '-';
function matchTypeFilter(p, f) {
  if (!f) return true;
  if (f === '出科') return hasDischarged(p.discharge_time);
  if (f === '未评' || f === '未预防') return p.eval_status === f;
  return p.patient_type === f;
}
const filteredPatients = computed(() => {
  const list = activeData.value?.patients || [];
  return list.filter(p =>
    (!hasPatientType.value || matchTypeFilter(p, patientTypeFilter.value))
    && matchVerdict(p)
    && matchReason(p));
});
// 切换指标 / 分子分母时清空类型筛选，避免带着「未评」筛出空分子表
watch(() => [activeData.value?.code, curPart.value], () => { patientTypeFilter.value = ''; });

const selectedPatientKey = ref(null);
function handleRowClick(p) {
  const key = p.detail_id || p.patient_id;
  selectedPatientKey.value = selectedPatientKey.value === key ? null : key;
  if (isIcu05.value) {
    toggleExpand(p.patient_id);
  }
}

function getReasonLabel(code) {
  return EXCL_REASONS.value.find(r => r.code === code)?.label || code;
}

function handleExclude(p) {
  exclForm.value = {
    exclusion_key: p.exclusion_key || '',
    patient_id: p.patient_id || '',
    name: p.name || '',
    reason_code: '',
    reason_text: '',
    operator: '',
  };
  showExclForm.value = true;
}

async function submitExclusion() {
  if (!exclForm.value.reason_code) return;
  if (exclForm.value.reason_code === 'other' && !exclForm.value.reason_text.trim()) return;
  try {
    await addExclusion(props.data.code, {
      period: props.period,
      icu_unit: props.unit,
      ...exclForm.value,
    });
    showExclForm.value = false;
    emit('exclusion-changed', curPart.value);
  } catch (e) {
    console.error('Exclude failed:', e);
  }
}

async function handleRestore(p) {
  try {
    await removeExclusion(props.data.code, p.exclusion_key, props.period, props.unit);
    emit('exclusion-changed', curPart.value);
  } catch (e) {
    console.error('Restore failed:', e);
  }
}

// ── ICU-05 Bundle详情 ──
const isIcu05 = computed(() => (activeData.value?.code || props.data?.code || '').startsWith('ICU-05'));
const expandedRows = ref(new Set());

function toggleExpand(patientId) {
  if (expandedRows.value.has(patientId)) {
    expandedRows.value.delete(patientId);
  } else {
    expandedRows.value.add(patientId);
  }
}

function isExpanded(patientId) {
  return expandedRows.value.has(patientId);
}

// ── 共享列定义 ──
const columns = computed(() => getDetailColumns(activeData.value?.code, curPart.value, {
  crossMonth: !!(props.endPeriod && props.endPeriod !== props.period),
}));
// ICU-05 窗口小时数（'1h'|'3h'|'6h'），传给 BundleDetail 显示判定窗口
const windowHour = computed(() => {
  const code = activeData.value?.code || '';
  return code.startsWith('ICU-05') ? (code.split('-')[2] || '') : '';
});

// ── 导出逻辑 ──
const exporting = ref(false);
const exportError = ref('');
const exportProgress = ref('');

async function handleExport() {
  const d = activeData.value;
  if (exporting.value || !d?.patients?.length || isSummary.value) return;
  exporting.value = true;
  exportError.value = '';
  exportProgress.value = '';
  try {
    const { rows, filename, truncated } = await exportDetailExcel({
      code: d.code,
      name: d.name,
      part: d.part,
      period: props.period,
      endPeriod: props.endPeriod,
      unit: props.unit,
      unitName: props.unitName,
      sourceDesc: d.source_desc,
      patients: d.patients,
      hasMore: d.has_more,
      onProgress: (loaded) => { exportProgress.value = `已加载 ${loaded} 条...`; },
    });
    exportProgress.value = '';
    if (truncated) {
      exportError.value = `数据超上限，仅导出前 ${rows} 条`;
    }
    console.log(`[export] 导出完成: ${filename}, ${rows} 行${truncated ? ' (截断)' : ''}`);
  } catch (e) {
    exportProgress.value = '';
    exportError.value = e.message || '导出失败';
    console.error('[export]', e);
  } finally {
    exporting.value = false;
  }
}

// ── 辅助判断 ──
const isSummary = computed(() =>
  activeData.value?.part === 'denominator' &&
  activeData.value?.patients?.length === 1 &&
  activeData.value?.patients[0]?.patient_id === '—'
);
const isTriTube = computed(() => ['ICU-16', 'ICU-17', 'CAUTI'].includes(props.data?.code));

// ICU-06 分母：低置信度 AI 判定行 → 标黄提示人工复核
const rowClass = (p) => {
  if (activeData.value?.code === 'ICU-06' && curPart.value === 'denominator'
      && p.admission_source === 'low_confidence') {
    return 'low-confidence';
  }
  return '';
};
</script>
<style scoped>
/* 根容器：作为 modal-body 的弹性子项，把弹窗高度约束传递给下方表格区 */
.dm-root { flex:1 1 auto; min-height:0; display:flex; flex-direction:column; }
.source {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  font-size:13px; color:var(--text-sub); margin-bottom:14px; padding:10px 12px;
  background:var(--brand-weak); border-radius:6px; border:1px solid rgba(30,94,184,0.08);
  min-height:40px; flex-wrap:wrap;
}
.source-left { display:flex; align-items:center; gap:6px; flex:1; min-width:0; overflow:hidden; }
/* 口径文案换行显示（最多2行），不再单行截断 */
.source-desc { white-space:normal; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.source-right { display:flex; align-items:center; gap:10px; flex-shrink:0; white-space:nowrap; }
.tag { background:var(--brand); color:#fff; padding:1px 8px; border-radius:4px; font-size:12px; flex-shrink:0; }
.count { color:var(--brand); font-weight:600; font-size:13px; }
.export-btn {
  padding:4px 14px; font-size:12px; height:30px; line-height:22px;
  background:var(--brand); color:#fff; border:none; border-radius:4px; cursor:pointer;
  display:inline-flex; align-items:center; justify-content:center; flex-shrink:0;
}
.export-btn:hover:not(:disabled) { opacity:.85; }
.export-btn:disabled { background:var(--text-faint); cursor:not-allowed; }
.export-status { margin-bottom:10px; font-size:12px; }
.export-progress { color:var(--text-sub); }
.export-error { color:var(--danger); }
.excl-count { color:var(--warn); font-weight:600; font-size:13px; }

/* ── P2: 公式条 ── */
.formula-row { display:flex; gap:12px; margin-bottom:10px; flex-wrap:wrap; }
.formula-bar {
  flex: 1 1 300px; display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  padding:12px 14px; background:var(--brand-weak); border:1px solid rgba(30,94,184,0.15); border-radius:8px;
}
.fb-label { font-size:12px; color:var(--text-sub); font-weight:600; }
.fb-part {
  display:flex; flex-direction:column; align-items:center; gap:0;
  background:#fff; border:1.5px solid var(--border); border-radius:8px;
  padding:4px 16px; cursor:pointer; min-width:64px;
}
.fb-part b { font-size:22px; line-height:1.1; color:var(--brand); }
.fb-part small { font-size:11px; color:var(--text-sub); }
.fb-part.active { border-color:var(--brand); box-shadow:0 0 0 2px rgba(30,94,184,0.18); }
.fb-part:hover { border-color:var(--brand); }
.fb-op { font-size:18px; color:var(--text-sub); font-weight:600; }
.fb-val { font-size:24px; font-weight:700; color:var(--danger); }

/* ── P2: 漏斗 ── */
.funnel {
  flex: 1 1 300px; display:flex; align-items:center; gap:6px;
  padding:12px 14px; background:#fff; border:1px solid var(--border); border-radius:8px;
}
.fn-node { flex:1; text-align:center; background:#e8f0fe; border-radius:6px; padding:6px 4px; }
.fn-node b { display:block; font-size:18px; color:var(--brand); line-height:1.2; }
.fn-node span { font-size:11px; color:var(--text-sub); }
.fn-node.fn-warn { background:#fff4e5; } .fn-node.fn-warn b { color:#b26a00; }
.fn-node.fn-bad { background:#fdecea; } .fn-node.fn-bad b { color:var(--danger); }
.fn-node.clickable { cursor:pointer; }
.fn-node.clickable:hover { outline:1.5px solid var(--brand); }
.fn-arrow { color:var(--text-faint); font-size:14px; }

/* ── P2: 三色原因分布 ── */
.reason-bar {
  background:var(--bg-surface); border:1px solid var(--border); border-radius:8px;
  padding:10px 14px; margin-bottom:10px;
}
.rb-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; flex-wrap:wrap; gap:6px; }
.rb-title { font-size:13px; font-weight:600; color:var(--text-title); }
.rb-legend { display:flex; gap:12px; font-size:11px; color:var(--text-sub); align-items:center; }
.rb-legend .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:4px; vertical-align:-1px; }
.d-fail { background:var(--danger); } .d-order { background:#e65100; } .d-missing { background:#9aa6b8; }
.rb-chips { display:flex; flex-wrap:wrap; gap:8px; }
.rb-chip {
  border:1px solid transparent; border-radius:14px; padding:3px 12px;
  font-size:12px; cursor:pointer; font-weight:500;
}
.rb-chip.fail { background:#fdecea; color:#c62828; border-color:#f5c6c2; }
.rb-chip.order { background:#fff3e0; color:#e65100; border-color:#ffcc80; }
.rb-chip.missing { background:#eef1f6; color:#5f6b7f; border-color:#d8dee9; }
.rb-chip.gate { background:#e8eaf6; color:#3949ab; border-color:#c5cae9; }
.rb-chip.on { outline:2px solid var(--brand); outline-offset:1px; }
.rb-chip:hover { filter:brightness(0.96); }
.rb-empty { font-size:12px; color:#0a7d33; }
.rb-note { margin-top:8px; font-size:11px; color:var(--text-sub); }

/* ── P2: 分子/分母 tab ── */
.part-tabs2 { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.pt2-btn {
  padding:6px 18px; font-size:13px; border-radius:6px; cursor:pointer;
  background:var(--bg-subtle); border:1px solid var(--border); color:var(--text-sub);
}
.pt2-btn.active { background:var(--brand); border-color:var(--brand); color:#fff; font-weight:600; }
.pt2-reset { font-size:12px; color:var(--brand); cursor:pointer; margin-left:auto; }
.pt2-reset:hover { text-decoration:underline; }

/* ── P2: 结论筛选 chips ── */
.chip-bar { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px; }
.chip {
  padding:4px 14px; font-size:12px; border-radius:14px; cursor:pointer;
  background:var(--bg-subtle); border:1px solid var(--border); color:var(--text-sub);
}
.chip.on { background:var(--brand); border-color:var(--brand); color:#fff; font-weight:600; }
.chip-fail.on { background:var(--danger); border-color:var(--danger); }
.chip-miss.on { background:#7a8699; border-color:#7a8699; }
.chip-pending.on { background:#b26a00; border-color:#b26a00; }
.chip-excl.on { background:#5f6b7f; border-color:#5f6b7f; }

/* ── P2: 结论列三色 ── */
.detail-table td.v-ok { color:#0a7d33; font-weight:600; }
.detail-table td.v-fail { color:#c62828; font-weight:600; }
.detail-table td.v-order { color:#e65100; font-weight:600; }
.detail-table td.v-missing { color:#7a8699; }
.detail-table td.v-gate { color:#3949ab; }
.detail-table td.v-excluded { color:#9aa6b8; }
.detail-table tbody tr.selected td.v-ok,
.detail-table tbody tr.selected td.v-fail,
.detail-table tbody tr.selected td.v-order,
.detail-table tbody tr.selected td.v-missing,
.detail-table tbody tr.selected td.v-gate,
.detail-table tbody tr.selected td.v-excluded { color:#fff !important; }

.filter-empty { padding:20px; text-align:center; font-size:13px; color:var(--text-sub); }
.linklike { color:var(--brand); cursor:pointer; }
.linklike:hover { text-decoration:underline; }
.den-summary { font-size:16px; font-weight:600; color:var(--text-title); text-align:center;
  padding:32px 20px; background:#f8fafc; border-radius:8px;
  border: 1px solid var(--border); }
.detail-table-wrap {
  border: 1px solid #b0c4f0;
  border-radius: 4px;
  background: #fff;
  box-shadow: none;
  margin-top: 6px;
  /* 弹性占满弹窗剩余高度：行多时表格区内部纵向滚动，
     横向滚动条固定在弹窗可视区底部，无需拉到表格最下方 */
  flex: 1 1 auto;
  min-height: 120px;
  overflow: auto;
}
.detail-table { width:100%; border-collapse:collapse; border-spacing:0; }
.detail-table th {
  color:#1f2a44; font-size:13px; padding:0 12px; text-align:left;
  background:#CBD7F5; border:1px solid #b0c4f0; border-width:0 1px 1px 0; font-weight:600;
  height:42px; line-height:42px; white-space:nowrap; position:sticky; top:0; z-index:2;
}
.detail-table th:last-child { border-right:none; }
.detail-table td {
  padding:0 12px; font-size:13px; color:#1f2a44;
  border:1px solid #e5eaf2; border-width:0 1px 1px 0;
  height:42px; line-height:42px; background:#fff; white-space:nowrap;
}
.detail-table td:last-child { border-right:none; }
.detail-table tbody tr.detail-row { cursor:pointer; }
.detail-table tbody tr.detail-row:hover td { background:#f0f4fd; }

/* 选中行样式：背景统一为图一中的明亮活力蓝 #5F8EF1，所有文字与图标纯白，hover 不能覆盖 */
.detail-table tbody tr.selected td { background:#5F8EF1 !important; color:#ffffff !important; }
.detail-table tbody tr.selected:hover td { background:#5F8EF1 !important; color:#ffffff !important; }
.detail-table tbody tr.selected .mono { color:#ffffff !important; }
.detail-table tbody tr.selected .expand-icon { color:#ffffff !important; }
.detail-table tbody tr.selected .btn-exclude {
  background:rgba(255,255,255,0.2) !important; color:#ffffff !important;
  border:1px solid rgba(255,255,255,0.4) !important;
}
.detail-table tbody tr.selected .btn-restore {
  background:#ffffff !important; color:#5F8EF1 !important; font-weight:600; border:none;
}
.detail-table tbody tr.selected .reason-tag {
  background:rgba(255,255,255,0.2) !important; color:#ffffff !important;
}

.mono { font-family:monospace; color:var(--text-sub); }
.tri-list { display:flex; flex-direction:column; gap:10px; }
.tri-card:hover { border-color:var(--border-strong); background:var(--bg-hover); }
.tri-card { border:1px solid var(--border); border-radius:8px; background:var(--bg-surface); padding:12px 14px; }
.tri-card:hover { border-color:#bfdbfe; background:#f8fbff; }
.tri-head { display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }
.tri-person { display:flex; gap:10px; align-items:center; min-width:180px; }
.tri-person strong { color:var(--text-title); font-size:var(--fs-body); }
.tri-metrics { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }
.tri-metrics span { background:var(--brand-weak); border:1px solid rgba(30,94,184,0.15); color:var(--brand);
  border-radius:6px; padding:3px 8px; font-size:var(--fs-caption); font-weight:600; }
.tri-basis { margin:8px 0 0; color:var(--text-body); font-size:var(--fs-label); line-height:1.65; }
/* ICU-06 低置信度行：黄色背景 + 左侧警告条 */
tr.low-confidence { background: var(--warn-weak); }
tr.low-confidence:hover td { background: rgba(178,106,0,0.12); }
tr.low-confidence td:first-child::before {
  content: ''; display: inline-block; width: 12px; height: 12px; margin-right: 4px;
  background: var(--warn); border-radius: 50%; vertical-align: -1px;
}

/* Exclusion styles */
.action-cell { text-align: center; white-space: normal; }
.btn-exclude { background: var(--warn); color: #fff; border: none; border-radius: 4px; padding: 3px 10px; font-size: var(--fs-caption); cursor: pointer; }
.btn-exclude:hover { opacity:.85; }
.btn-restore { background: var(--brand); color: #fff; border: none; border-radius: 4px; padding: 3px 10px; font-size: var(--fs-caption); cursor: pointer; }
.btn-restore:hover { opacity:.85; }
.reason-tag { display: inline-block; margin-left: 4px; font-size: var(--fs-caption); color: var(--text-sub); background: var(--bg-subtle); border-radius: 3px; padding: 1px 6px; }
tr.excluded-row { opacity: 0.5; }
tr.excluded-row td { text-decoration: line-through; }
.excl-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; }
.excl-dialog { background: var(--bg-surface); border-radius: 12px; padding: 24px; width: 420px; max-width: 90vw; box-shadow: var(--shadow-card); }
.excl-dialog h3 { margin: 0 0 16px; font-size: 16px; color: var(--text-title); }
.excl-field { margin-bottom: 12px; }
.excl-field label { display: block; font-size: var(--fs-label); color: var(--text-sub); margin-bottom: 4px; font-weight: 500; }
.excl-field .required { color: var(--danger); }
.excl-field select, .excl-field input, .excl-field textarea {
  width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: var(--fs-label); }
.excl-field textarea { resize: vertical; }
.excl-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.btn-cancel { padding: 7px 16px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-surface); font-size: var(--fs-label); cursor: pointer; }
.btn-confirm { padding: 7px 16px; border: none; border-radius: 6px; background: var(--danger); color: #fff; font-size: var(--fs-label); cursor: pointer; }
.btn-confirm:disabled { opacity: 0.5; cursor: not-allowed; }
.census-filter { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; padding: 8px 12px; background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 6px; }
.filter-label { font-size: var(--fs-caption); color: var(--text-sub); font-weight: 600; }
.filter-btn { padding: 4px 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-surface); font-size: var(--fs-caption); cursor: pointer; color: var(--text-sub); }
.filter-btn:hover { border-color: var(--brand); color: var(--brand); }
.filter-btn.active { background: var(--brand); color: #fff; border-color: var(--brand); }
.filter-count { margin-left: auto; font-size: var(--fs-caption); color: var(--text-sub); }

/* ICU-05 Bundle详情展开行样式 */
.clickable-row { cursor: pointer; }
.clickable-row:hover { background: var(--bg-hover); }
.expand-cell { width: 30px; text-align: center; padding: 9px 6px !important; }
.expand-icon {
  display: inline-block;
  font-size: 10px;
  color: var(--text-sub);
  transition: transform 0.2s ease;
}
.expand-icon.expanded { transform: rotate(90deg); }
.bundle-detail-row { background: var(--bg-subtle); }
.bundle-detail-row td { padding: 0 !important; border-bottom: 2px solid var(--border); }

/* ICU-05 指标说明面板 */
.bundle-guide {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 12px;
  overflow: hidden;
}

.guide-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
  font-weight: 600;
  color: var(--text-title);
  background: var(--bg-subtle);
}

.guide-title:hover {
  background: var(--bg-hover);
}

.guide-toggle {
  color: var(--text-sub);
  font-size: 0.85em;
}

.guide-content {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.guide-section {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.guide-label {
  font-weight: 600;
  color: var(--text-sub);
  min-width: 80px;
  white-space: nowrap;
}

.guide-text {
  color: var(--text-body);
  font-size: 0.9em;
}

.guide-flow {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.flow-step {
  background: var(--brand-weak);
  color: var(--brand);
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 0.85em;
  font-weight: 500;
}

.flow-arrow {
  color: var(--text-sub);
  font-size: 0.85em;
}
</style>
