<template>
  <div class="table-page">
    <!-- 顶部筛选条 -->
    <div class="filter-bar">
      <span class="page-title">{{ deptName }}</span>
      <div class="filters">
        <select v-model="year" @change="reload()"><option v-for="y in years" :key="y" :value="y">{{ y }}年</option></select>
        <select v-model="startMonth" @change="reload()"><option v-for="m in 12" :key="m" :value="m">{{ m }}月</option></select>
        <span class="dash">—</span>
        <select v-model="endMonth" @change="reload()"><option v-for="m in 12" :key="m" :value="m">{{ m }}月</option></select>

        <button class="refresh-btn" :disabled="refreshing" @click="triggerRefresh">
          <span v-if="refreshing" class="spinner"></span>
          {{ refreshing ? '后台刷新中' : '刷新数据' }}
        </button>
        <button class="guide-btn" @click="guideVisible=true">指标说明</button>
        <button class="export-btn" @click="exportCsv">导出CSV</button>
        <button class="export-btn" :disabled="exporting" @click="exportReport">{{ exporting ? exportLabel : '导出上报表' }}</button>
      </div>
    </div>

    <!-- 患者动态摘要条 -->
    <div v-if="census" class="census-bar">
      <span class="census-label">患者动态</span>
      <span class="census-item clickable" @click="drillCensus('carry_in', '原有患者')">
        <small>原有</small> <b>{{ census.carry_in }}</b>
      </span>
      <span class="census-item clickable" @click="drillCensus('new_admit', '新入患者')">
        <small>新入</small> <b>{{ census.new_admit }}</b>
      </span>
      <span class="census-item clickable" @click="drillCensus('discharge', '出科患者')">
        <small>出科</small> <b>{{ census.discharge }}</b>
      </span>
      <span class="census-item clickable" @click="drillCensus('carry_out', '期末在科')">
        <small>期末在科</small> <b>{{ census.carry_out }}</b>
      </span>
      <span class="census-item total">
        <small>同期总数</small> <b>{{ census.total }}</b>
      </span>
    </div>

    <div class="table-wrap" :class="{ 'multi-month': isMultiMonth }">
      <table class="indi-table">
        <colgroup>
          <col class="c-code" />
          <col class="c-name" />
          <col class="c-num" />
          <col class="c-num" />
          <col class="c-val" />
          <col v-for="m in monthCols" :key="'col'+m" class="c-month" />
          <col class="c-status" />
          <col class="c-trend" />
        </colgroup>
        <thead>
          <tr>
            <th class="t-left">编码</th>
            <th class="t-left">指标名称</th>
            <th class="t-right">分子</th>
            <th class="t-right">分母</th>
            <th class="t-right sep">比值</th>
            <th v-for="m in monthCols" :key="'h'+m" class="t-center">{{ m }}月</th>
            <th class="t-center sep">状态</th>
            <th class="t-center">趋势</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.code" :class="{ selected: selectedRow === row.code }" @click="selectedRow = selectedRow === row.code ? null : row.code">
            <td class="code t-left">{{ displayCode(row.code) }}</td>
            <td class="name t-left" @mouseenter="showTip($event, row.code)" @mouseleave="hideTip">
              <span class="name-txt" tabindex="0" :title="row.name">{{ row.name }}</span>
              <span v-if="row.excluded_den > 0" class="excl-badge" :title="`原始分母  例，人工排除  例，实际计入  例`">口径已调整</span>
              <span class="formula-icon">ƒ</span>
            </td>
            <td class="num t-right link" @click="drillDetail(row,'numerator')">{{ fmtCell(row.numerator) }}</td>
            <td class="num t-right link" @click="drillDetail(row,'denominator')" :title="census ? '= 原有 ' + census.carry_in + ' + 新入 ' + census.new_admit : ''">{{ fmtCell(row.denominator) }}</td>
            <td class="t-right sep link" @click="drillTrend(row)"><b class="val">{{ fmtValue(row) }}</b></td>
            <td v-for="m in monthCols" :key="row.code+m" class="t-center month-cell link" :class="cellLevel(row, m)"
                @click="drillMonthDetail(row, m)">
              {{ fmtMonth(row, m) }}
            </td>
            <td class="t-center sep">
              <span class="badge" :class="row.status" :style="statusStyle(row.status)">
                <i class="badge-dot" :style="{ background: statusConfig.meta?.[row.status]?.color }"></i>
                {{ statusText(row.status) }}
              </span>
            </td>
            <td class="t-center"><span class="mini" @click="drillTrend(row)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:16px;height:16px;vertical-align:-2px"><polyline points="1,12 4,6 7,9 10,3 15,8"/><line x1="1" y1="14" x2="15" y2="14"/></svg></span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 公式悬浮 -->
    <div v-if="tip.show" class="formula-tip" :style="{ left: tip.x+'px', top: tip.y+'px' }">
      <div class="tip-title">{{ tip.data.name }}</div>
      <div v-if="tip.data.mode==='fraction'" class="tip-formula">
        <span class="fraction">
          <span class="numerator">{{ tip.data.numerator }}</span>
          <span class="fraction-line"></span>
          <span class="denominator">{{ tip.data.denominator }}</span>
        </span>
        <span class="multiplier">{{ tip.data.sign }}</span>
      </div>
      <div v-else-if="tip.data.mode==='special'" class="tip-special">
        <div v-for="(l,i) in tip.data.lines" :key="i" class="special-line">{{ l }}</div>
      </div>
      <div v-else class="tip-note">{{ tip.data.note }}</div>
      <div class="tip-meaning">{{ tip.data.meaning }}</div>
    </div>

    <Modal v-if="trendData" :title="`${trendData.name} · ${year}年趋势`" @close="trendData=null"><TrendModal :data="trendData" /></Modal>
    <Modal v-if="detailData" :title="detailTitle" @close="detailData=null">
      <DetailModal :data="detailData" :summary="detailData.summary" :parts="detailData.parts"
                   :funnel="detailData.funnel"
                   :period="period" :end-period="isMultiMonth ? periodEnd : ''" :unit="unit" :unit-name="deptName"
                   @exclusion-changed="onDetailExclusionChanged" />
    </Modal>
    <!-- 月份单元格下钻弹窗：独立查询上下文（分子/分母切换由 DetailModal 内部处理） -->
    <Modal v-if="monthDetailData" :title="monthDetailTitle" @close="closeMonthDetail">
      <DetailModal :data="monthDetailData" :summary="monthDetailData.summary" :parts="monthDetailData.parts"
                   :funnel="monthDetailData.funnel"
                   :period="monthDetailPeriod" :end-period="''" :unit="unit" :unit-name="deptName"
                   @exclusion-changed="onMonthExclusionChanged" />
    </Modal>
    <Modal v-if="guideVisible" title="指标口径说明" @close="guideVisible=false"><IndicatorGuideModal /></Modal>

    <!-- Toast 通知 -->
    <Transition name="toast-fade">
      <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.message }}</div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted, watch } from 'vue';
import { INDICATORS, evalStatus, getStatusConfig, statusText as getStatusText } from './config/indicators.js';
import Modal from './components/Modal.vue';
import TrendModal from './components/TrendModal.vue';
import DetailModal from './components/DetailModal.vue';
import IndicatorGuideModal from './components/IndicatorGuideModal.vue';
import { fetchIndicatorList, fetchTrend as apiFetchTrend, fetchDetail as apiFetchDetail, triggerRefresh as apiTriggerRefresh, getRefreshStatus, fetchReportValues } from './api/index.js';
import { downloadReportWorkbook } from './utils/reportXlsx.js';

// 使用 Asia/Shanghai 当前年月作为默认值
const _now = new Date();
const _shanghaiNow = new Date(_now.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }));
const _curYear = _shanghaiNow.getFullYear();
const _curMonth = _shanghaiNow.getMonth() + 1;

const year = ref(_curYear), startMonth = ref(_curMonth), endMonth = ref(_curMonth);
const hostDeptCode = inject('hostDeptCode', ref('all'));
const unit = computed(() => hostDeptCode.value || 'all');
// 动态年份：从2024到当前年+1
const years = computed(() => {
  const arr = [];
  for (let y = 2024; y <= _curYear + 1; y++) arr.push(y);
  return arr;
});
const rows = ref([]); const trendData = ref(null); const detailData = ref(null);
const rowsLoading = ref(false); // 期间切换加载中：禁止下钻，防止公式条(旧行)≠明细(新期间)
let drillSeq = 0;               // 明细请求序号：快速切换指标时丢弃过期响应
const census = ref(null); const selectedRow = ref(null);
// 月份单元格下钻
const monthDetailData = ref(null);
const monthDetailPeriod = ref('');
const monthDetailNumerator = ref(null);   // 分子数据缓存
const monthDetailDenominator = ref(null); // 分母数据缓存
const monthDetailAbort = ref(null); // 用于取消过期请求
const guideVisible = ref(false);
const deptName = computed(() => { if (!hostDeptCode.value || hostDeptCode.value === 'all') return '全部ICU'; return hostDeptCode.value; });
const refreshing = ref(false);
const toast = ref({ show: false, message: '', type: 'success' });
const statusConfig = ref(getStatusConfig());
let _refreshTimer = null;
let _refreshPollTimer = null;
const activeRefreshTask = ref(null);

const monthCols = computed(() => {
  if (startMonth.value === endMonth.value) return [];
  const arr = []; for (let m = startMonth.value; m <= endMonth.value; m++) arr.push(m);
  return arr;
});
const isMultiMonth = computed(() => monthCols.value.length > 0);
const period = computed(() => `${year.value}-${String(startMonth.value).padStart(2,'0')}`);
const periodEnd = computed(() => `${year.value}-${String(endMonth.value).padStart(2,'0')}`);

function fmtMonth(row, m) {
  const v = row.monthly?.[m];
  return v == null ? '/' : `${v}${row.unit}`;
}
function fmtCell(v) { return v == null ? '/' : v; }
function fmtValue(row) { return row.value == null ? '/' : `${row.value}${row.unit}`; }
function displayCode(code) {
  return INDICATORS.find(i => i.code === code)?.displayCode || code;
}
function codeOrder(code) {
  const shown = displayCode(code);
  // 支持 ICU-03 / ICU-04-01 / ICU-05-1h：主号×100+后缀序号
  // ICU-04-01~07 → 401~407，落在 ICU-03(300) 与 ICU-05-1h(501) 之间
  const match = shown.match(/^ICU-(\d+)(?:-(\d+)h?)?$/);
  if (!match) return Number.MAX_SAFE_INTEGER;
  return Number(match[1]) * 100 + Number(match[2] || 0);
}
function cellLevel(row, m) {
  const v = row.monthly?.[m]; if (v == null) return '';
  const base = row.value;
  if (base && Math.abs(v - base) / base > 0.15) return 'alert';
  return '';
}
function statusStyle(status) {
  const meta = statusConfig.value.meta?.[status];
  if (!meta) return {};
  return { color: meta.color };
}

// 公式悬浮
const tip = ref({ show:false, x:0, y:0, data:null });
function buildFormula(code) {
  const ind = INDICATORS.find(i => i.code === code); if (!ind) return null;
  const base = { name: ind.name, meaning: ind.meaning };
  if (ind.formulaDetail?.type === 'special') return { ...base, mode:'special', lines: ind.formulaDetail.lines };
  if (ind.formulaDetail?.type === 'note') return { ...base, mode:'note', note: ind.formulaDetail.note };
  const sign = ind.multiplier===100?'× 100%':ind.multiplier===1000?'× 1000‰':'';
  return { ...base, mode:'fraction', numerator: ind.numerator, denominator: ind.denominator, sign };
}
function showTip(e, code){ const d=buildFormula(code); if(!d)return; tip.value={show:true,x:e.clientX+16,y:e.clientY+12,data:d}; }
function moveTip(e){ if(tip.value.show){ tip.value.x=e.clientX+16; tip.value.y=e.clientY+12; } }
function hideTip(){ tip.value.show=false; }

// ===== 真实 API 数据加载 =====
async function reload(nocache = false) {
  // 校验：开始月份不能晚于结束月份
  if (startMonth.value > endMonth.value) {
    endMonth.value = startMonth.value;
  }
  return reloadRange(period.value, isMultiMonth.value ? periodEnd.value : '', nocache);
}

let rangeSeq = 0; // 列表请求序号：期间快速切换时丢弃过期响应

async function reloadRange(startPeriod, endPeriod = '', nocache = false) {
  const seq = ++rangeSeq;
  rowsLoading.value = true;
  try {
    const data = await fetchIndicatorList(startPeriod, unit.value, endPeriod, nocache);
    if (seq !== rangeSeq) return; // 已有更新的请求，本响应作废
    // 后端返回 monthly 是数组，转换为 {月份: 值} 对象
    rows.value = data.sort((a, b) => codeOrder(a.code) - codeOrder(b.code)).map(r => {
      if (r.months && r.monthly) {
        const map = {};
        r.months.forEach((mon, i) => { map[parseInt(mon.split('-')[1])] = r.monthly[i]; });
        r.monthly = map;
      }
      return r;
    });
    rows.value = rows.value.map(r => {
      const ind = INDICATORS.find(i => i.code === r.code);
      return ind && r.value != null ? { ...r, status: evalStatus(ind, r.value, statusConfig.value) } : r;
    });
    const icu00 = data.find(r => r.code === 'ICU-00'); census.value = icu00?.census || null;
  } catch { if (seq === rangeSeq) { rows.value = []; census.value = null; } }
  finally { if (seq === rangeSeq) rowsLoading.value = false; }
}

async function drillTrend(row) {
  if (row.value == null) return;
  try { trendData.value = await apiFetchTrend(row.code, year.value, unit.value, startMonth.value, endMonth.value); } catch { /* */ }
}
async function drillCensus(part, label) {
  if (rowsLoading.value) return;
  const endP = isMultiMonth.value ? periodEnd.value : '';
  const base = { code: 'ICU-00', name: label, part, count: 0, source_desc: '明细加载中...', patients: [], loading: true };
  detailData.value = base;
  try {
    detailData.value = await apiFetchDetail('ICU-00', period.value, part, unit.value, endP, { limit: 200, offset: 0 });
  } catch (e) {
    detailData.value = { ...base, loading: false, error: e.message || '明细加载失败', source_desc: '明细加载失败' };
  }
}
async function drillDetail(row, part) {
  if (rowsLoading.value) return;
  if (row[part] == null) return;
  const seq = ++drillSeq;
  const endP = isMultiMonth.value ? periodEnd.value : '';
  const base = { code: row.code, name: row.name, part, count: 0, source_desc: '明细加载中...', patients: [], loading: true };
  // 公式条/漏斗在加载期间即显示
  detailData.value = { ...base, summary: buildSummary(row), funnel: buildFunnel(row) };
  try {
    const [numR, denR] = await Promise.all([
      apiFetchDetail(row.code, period.value, 'numerator', unit.value, endP, { limit: 200, offset: 0 }),
      apiFetchDetail(row.code, period.value, 'denominator', unit.value, endP, { limit: 200, offset: 0 }),
    ]);
    if (seq !== drillSeq) return;
    const clicked = part === 'numerator' ? numR : denR;
    detailData.value = {
      ...clicked,
      summary: buildSummary(row),
      parts: { numerator: numR, denominator: denR },
      funnel: buildFunnel(row),
    };
  } catch (e) {
    if (seq !== drillSeq) return;
    detailData.value = { ...base, loading: false, error: e.message || '明细加载失败', source_desc: '明细加载失败',
                         summary: buildSummary(row), funnel: buildFunnel(row) };
  }
}

// 公式条数据：单期/跨期用行上的真实分子分母；月度下钻 fromCounts 用明细条数近似
const MONTH_COUNT_SKIP = ['ICU-01', 'ICU-02', 'ICU-03', 'ICU-11']; // 分子/分母值≠明细条数的指标
function buildSummary(row, opts = {}) {
  const s = { value: opts.value ?? row.value, unit: row.unit || '' };
  if (opts.fromCounts) {
    if (!MONTH_COUNT_SKIP.includes(row.code)) {
      s.numerator = opts.numCount;
      s.denominator = opts.denCount;
    }
  } else {
    if (row.numerator != null) s.numerator = row.numerator;
    if (row.denominator != null) s.denominator = row.denominator;
  }
  return s;
}
// ICU-05 人群漏斗：候选池 → 休克确认(K1∧K2) → Bundle达标
function buildFunnel(row, opts = {}) {
  if (!row.code || !String(row.code).startsWith('ICU-05')) return null;
  return {
    candidate: opts.candidate ?? row.candidate_den ?? row.denominator ?? null,
    shock: opts.shock ?? row.old_shock_count ?? null,
    completed: opts.completed ?? row.numerator ?? null,
  };
}

// 月份单元格下钻：查询被点击月份的分子和分母
async function drillMonthDetail(row, m) {
  if (rowsLoading.value) return;
  const monthPeriod = `${year.value}-${String(m).padStart(2, '0')}`;
  // 0 和 null 严格区分：0 可以下钻，null 显示空态
  const val = row.monthly?.[m];
  if (val == null && val !== 0) return;

  // 取消上一次未完成的请求
  if (monthDetailAbort.value) {
    monthDetailAbort.value.abort();
  }
  const controller = new AbortController();
  monthDetailAbort.value = controller;

  monthDetailPeriod.value = monthPeriod;

  // 先显示加载状态（含公式条）
  const base = { code: row.code, name: row.name, part: 'numerator', count: 0, source_desc: '明细加载中...', patients: [], loading: true };
  monthDetailData.value = {
    ...base,
    summary: buildSummary(row, { fromCounts: true, value: row.monthly?.[m] }),
    // 月度口径: 候选/达标=当月明细条数；休克确认无当月数据 → 显示 '—'
  };

  // 并行获取分子和分母
  try {
    const [numResult, denResult] = await Promise.all([
      apiFetchDetail(row.code, monthPeriod, 'numerator', unit.value, '', { limit: 200, offset: 0 }),
      apiFetchDetail(row.code, monthPeriod, 'denominator', unit.value, '', { limit: 200, offset: 0 })
    ]);
    if (!controller.signal.aborted) {
      monthDetailNumerator.value = numResult;
      monthDetailDenominator.value = denResult;
      monthDetailData.value = {
        ...numResult,
        summary: buildSummary(row, { fromCounts: true, value: row.monthly?.[m], numCount: numResult.count, denCount: denResult.count }),
        parts: { numerator: numResult, denominator: denResult },
        // 月度漏斗: 候选/达标为当月值；休克确认无当月口径 → null 显示 '—'
        funnel: String(row.code).startsWith('ICU-05')
          ? { candidate: denResult.count, shock: null, completed: numResult.count }
          : null,
      }; // 默认显示分子
    }
  } catch (e) {
    if (!controller.signal.aborted) {
      monthDetailData.value = { ...base, loading: false, error: e.message || '明细加载失败', source_desc: '明细加载失败' };
    }
  }
}

// 分子/分母切换已内置于 DetailModal（parts prop），无需外部维护当前 part

const detailTitle = computed(()=> detailData.value ? `${detailData.value.name} · 明细` : '');

const monthDetailTitle = computed(() => {
  if (!monthDetailData.value) return '';
  return `${monthDetailData.value.name} · ${monthDetailPeriod.value} 明细`;
});

function closeMonthDetail() {
  if (monthDetailAbort.value) {
    monthDetailAbort.value.abort();
    monthDetailAbort.value = null;
  }
  monthDetailData.value = null;
  monthDetailPeriod.value = '';
  monthDetailNumerator.value = null;
  monthDetailDenominator.value = null;
}

function onMonthExclusionChanged(part) {
  // 排除变更后重新加载分子+分母（公式条计数同步更新）
  if (!monthDetailData.value || !monthDetailPeriod.value) return;
  const code = monthDetailData.value.code;
  reloadBothParts(
    (p) => apiFetchDetail(code, monthDetailPeriod.value, p, unit.value, '', { limit: 200, offset: 0 }),
    part || monthDetailData.value.part,
    monthDetailData.value,
    (next) => { monthDetailData.value = next; },
    (numR, denR) => { monthDetailNumerator.value = numR; monthDetailDenominator.value = denR; }
  );
}

function onDetailExclusionChanged(part) {
  if (!detailData.value) return;
  const endP = isMultiMonth.value ? periodEnd.value : '';
  const cur = detailData.value;
  reloadBothParts(
    (p) => apiFetchDetail(cur.code, period.value, p, unit.value, endP, { limit: 200, offset: 0 }),
    part || cur.part,
    cur,
    (next) => { detailData.value = next; },
    () => {}
  );
}

// 排除变更后统一重载两份明细：更新 parts + 公式条计数，并保持当前 tab
function reloadBothParts(fetcher, stayPart, current, apply, onLoaded) {
  const base = { ...current, loading: true, patients: [], source_desc: '重新加载中...' };
  apply(base);
  Promise.all([fetcher('numerator'), fetcher('denominator')])
    .then(([numR, denR]) => {
      onLoaded(numR, denR);
      const summary = { ...(current.summary || {}) };
      if (summary.numerator != null) summary.numerator = numR.count;
      if (summary.denominator != null) summary.denominator = denR.count;
      const funnel = current.funnel
        ? { ...current.funnel, candidate: denR.count, completed: numR.count }
        : null;
      apply({
        ...(stayPart === 'numerator' ? numR : denR),
        summary,
        parts: { numerator: numR, denominator: denR },
        funnel,
      });
    })
    .catch(e => { apply({ ...base, loading: false, error: e.message }); });
}
function showToast(message, type = 'success', duration = 4000) {
  toast.value = { show: true, message, type };
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(() => { toast.value.show = false; }, duration);
}

async function triggerRefresh() {
  if (refreshing.value) {
    if (activeRefreshTask.value) pollRefreshTask(activeRefreshTask.value, true);
    return;
  }  // 防重复点击
  refreshing.value = true;
  const refreshYear = year.value;
  const refreshStartMonth = startMonth.value;
  const refreshEndMonth = endMonth.value;
  const refreshStartPeriod = `${refreshYear}-${String(refreshStartMonth).padStart(2, '0')}`;
  const refreshEndPeriod = `${refreshYear}-${String(refreshEndMonth).padStart(2, '0')}`;

  try {
    const refreshTask = await apiTriggerRefresh(unit.value, refreshYear, refreshStartMonth, refreshEndMonth);
    const task = {
      ...refreshTask,
      refreshYear,
      refreshUnit: unit.value,
      refreshStartPeriod,
      refreshEndPeriod,
      refreshStartMonth,
      refreshEndMonth,
    };

    if (refreshTask.immediate || refreshTask.status === 'completed') {
      activeRefreshTask.value = task;
      await finishRefreshTask(task);
      return;
    }

    activeRefreshTask.value = task;
    const reused = refreshTask.reused ? '已有同范围任务在后台执行' : '已开始后台重算';
    showToast(`${reused} · 已完成 ${refreshTask.done ?? 0}/${refreshTask.total ?? '?'}`, 'info', 5000);
    pollRefreshTask(task, true);
  } catch (e) {
    refreshing.value = false;
    showToast(e.message || '刷新失败', 'error', 6000);
  }
}

async function pollRefreshTask(task, immediate = false) {
  clearTimeout(_refreshPollTimer);
  if (!task?.task_id) {
    refreshing.value = false;
    return;
  }

  const run = async () => {
    try {
      const latest = await getRefreshStatus(task.task_id);
      if (latest.status === 'completed') {
        await finishRefreshTask({ ...task, ...latest });
        return;
      }
      if (latest.status === 'error') {
        refreshing.value = false;
        activeRefreshTask.value = null;
        showToast(latest.error || '刷新失败', 'error', 6000);
        return;
      }
      if (latest.status === 'not_found') {
        refreshing.value = false;
        activeRefreshTask.value = null;
        showToast('后台刷新任务已过期，请重新刷新', 'error', 6000);
        return;
      }

      activeRefreshTask.value = { ...task, ...latest };
      const done = latest.done ?? 0;
      const total = latest.total ?? task.total ?? '?';
      const started = latest.started ? `，已启动 ${latest.started}` : '';
      const current = latest.current_period && latest.current_indicator
        ? `，当前 ${latest.current_period} ${latest.current_indicator}`
        : '';
      showToast(`后台重算中 · ${done}/${total}${started}${current}`, 'info', 5000);
      _refreshPollTimer = setTimeout(() => pollRefreshTask(activeRefreshTask.value), 3000);
    } catch (e) {
      showToast(e.message || '刷新进度获取失败，后台任务仍可能在执行', 'error', 6000);
      _refreshPollTimer = setTimeout(() => pollRefreshTask(activeRefreshTask.value || task), 8000);
    }
  };

  if (immediate) run();
  else _refreshPollTimer = setTimeout(run, 3000);
}

async function finishRefreshTask(task) {
  clearTimeout(_refreshPollTimer);
  const stillOnRefreshRange =
    year.value === task.refreshYear &&
    unit.value === task.refreshUnit &&
    startMonth.value === task.refreshStartMonth &&
    endMonth.value === task.refreshEndMonth;
  if (stillOnRefreshRange) {
    await reloadRange(
      task.refreshStartPeriod ?? period.value,
      (task.refreshStartMonth ?? startMonth.value) === (task.refreshEndMonth ?? endMonth.value)
        ? ''
        : (task.refreshEndPeriod ?? periodEnd.value),
      true,
    );
  }
  refreshing.value = false;
  activeRefreshTask.value = null;
  showToast(
    `预聚合刷新完成 · ${task.stats?.success ?? task.success ?? '?'}/${task.stats?.total ?? task.total ?? '?'} 项成功`,
    'success',
  );
}

function statusText(s){ return getStatusText(s, statusConfig.value); }

function exportCsv() {
  const header = '编码,指标名称,分子,分母,比值,状态\n';
  const body = rows.value.map(r =>
    `${displayCode(r.code)},${r.name},${r.numerator??'/'},${r.denominator??'/'},${r.value == null ? '/' : `${r.value}${r.unit}`},${statusText(r.status)}`).join('\n');
  const blob = new Blob(['﻿'+header+body], { type:'text/csv;charset=utf-8' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `ICU质控_${period.value}.csv`; a.click();
}

// =====《重症质控指标上报（2024年版）》按月导出 =====
const exporting = ref(false);
const exportLabel = ref('');

function reportFilename(p) {
  const unitPart = unit.value && unit.value !== 'all' ? `_${deptName.value}` : '';
  return `重症质控指标上报_${p}${unitPart}.xlsx`.replace(/[\\/:*?"<>|]/g, '_');
}

async function exportReport() {
  if (exporting.value) return;
  // 导出所选起止月份范围内的每一个月（每月一个独立文件）
  const months = [];
  for (let m = startMonth.value; m <= endMonth.value; m++) months.push(m);
  exporting.value = true;
  const exported = [], skipped = [], failed = [];
  try {
    for (let i = 0; i < months.length; i++) {
      const p = `${year.value}-${String(months[i]).padStart(2, '0')}`;
      exportLabel.value = months.length > 1 ? `导出中 ${i + 1}/${months.length}` : '导出中…';
      try {
        const data = await fetchReportValues(p, unit.value);
        if (!data.has_data) { skipped.push(p); continue; }
        downloadReportWorkbook(data.values, reportFilename(p));
        exported.push(p);
        // 多文件下载间隔，避免浏览器合并/拦截
        if (i < months.length - 1) await new Promise(r => setTimeout(r, 350));
      } catch (e) {
        failed.push(`${p}（${e.message || e}）`);
      }
    }
    const parts = [];
    if (exported.length) parts.push(`已导出 ${exported.length} 个：${exported.join('、')}`);
    if (skipped.length) parts.push(`无数据跳过：${skipped.join('、')}`);
    if (failed.length) parts.push(`失败：${failed.join('、')}`);
    if (parts.length) {
      showToast(parts.join('；'), failed.length ? 'error' : (exported.length ? 'success' : 'info'), 6000);
    }
  } finally {
    exporting.value = false;
    exportLabel.value = '';
  }
}


watch(hostDeptCode, () => { reload(true); });
onMounted(async () => { await reload(true); });
window.addEventListener('status-config-updated', () => {
  statusConfig.value = getStatusConfig();
  rows.value = rows.value.map(r => {
    const ind = INDICATORS.find(i => i.code === r.code);
    return ind && r.value != null ? { ...r, status: evalStatus(ind, r.value, statusConfig.value) } : r;
  });
});
</script>

<style scoped>
.table-page { padding: 20px 28px; }
.filter-bar { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
.page-title { font-size:15px; font-weight:600; color:var(--text-main); }
.filters { display:flex; gap:8px; align-items:center; }
.filters select { background:#fff; color:var(--text-main); border:1px solid var(--border);
  border-radius:7px; padding:6px 10px; font-size:13px; cursor:pointer; }
.dash { color:var(--text-faint); }
.refresh-btn { background:#fff; color:var(--brand); border:1px solid var(--brand);
  border-radius:7px; padding:7px 16px; cursor:pointer; font-size:13px; font-weight:500;
  display:inline-flex; align-items:center; gap:6px; transition:all .2s; }
.refresh-btn:hover:not(:disabled) { background:rgba(44,123,229,0.06); }
.refresh-btn:disabled { opacity:.65; cursor:not-allowed; }
.spinner { display:inline-block; width:14px; height:14px; border:2px solid var(--brand);
  border-top-color:transparent; border-radius:50%; animation:spin .7s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.export-btn { background:var(--brand); color:#fff; border:none; border-radius:7px;
  padding:7px 16px; cursor:pointer; font-size:13px; font-weight:500; }
.export-btn:hover:not(:disabled) { opacity:.85; }
.export-btn:disabled { opacity:.6; cursor:not-allowed; }
.guide-btn { background:var(--bg-subtle); color:var(--brand); border:1px solid var(--border); border-radius:7px;
  padding:7px 14px; cursor:pointer; font-size:13px; font-weight:500; }
.guide-btn:hover { background:#eff6ff; border-color:#93c5fd; color:var(--brand); }

/* Toast 通知 */
.toast { position:fixed; bottom:32px; right:32px; z-index:999;
  padding:12px 22px; border-radius:9px; font-size:14px; font-weight:500;
  box-shadow:0 4px 20px rgba(16,30,54,0.15);
  max-width:420px; line-height:1.5; }
.toast.success { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; }
.toast.error { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; }
.toast.info { background:#eff6ff; color:#1e3a5f; border:1px solid #bfdbfe; }
.toast-fade-enter-active { transition:all .3s ease-out; }
.toast-fade-leave-active { transition:all .25s ease-in; }
.toast-fade-enter-from, .toast-fade-leave-to { opacity:0; transform: translateY(12px); }

.table-wrap { width:fit-content; max-width:100%; margin:0 auto; background:#fff;
  border:1px solid #b0c4f0; border-radius:4px; overflow:hidden; box-shadow:none; }
.table-wrap.multi-month { width:fit-content; max-width:100%; margin:0 auto; overflow-x:auto; }

.indi-table { width:auto; table-layout:fixed; border-collapse:collapse; border-spacing:0; }
.table-wrap.multi-month .indi-table { width:auto; min-width:unset; }

.c-code, .indi-table th:nth-child(1), .indi-table td.code { width:90px !important; min-width:90px !important; max-width:90px !important; }
.c-name, .indi-table th:nth-child(2), .indi-table td.name { width:230px !important; min-width:230px !important; max-width:230px !important; }
.c-num, .indi-table th:nth-child(3), .indi-table th:nth-child(4), .indi-table td.num { width:76px !important; min-width:76px !important; max-width:76px !important; }
.c-val, .indi-table th:nth-child(5), .indi-table td:nth-child(5) { width:86px !important; min-width:86px !important; max-width:86px !important; }
.c-month, .indi-table td.month-cell { width:88px !important; min-width:88px !important; max-width:88px !important; }
.c-status { width:86px !important; min-width:86px !important; max-width:86px !important; }
.c-trend { width:50px !important; min-width:50px !important; max-width:50px !important; }

.indi-table th, .indi-table td {
  padding:0 6px; font-size:13px; height:42px; line-height:42px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; box-sizing:border-box;
}
.indi-table tr { height:42px; }
.indi-table th {
  background:#CBD7F5; color:#1f2a44;
  font-size:13px; font-weight:600; letter-spacing:0.02em;
  border:1px solid #b0c4f0; border-width:0 1px 1px 0;
  position:sticky; top:0; z-index:2;
}
.indi-table th:last-child { border-right:none; }
.indi-table td { color:#1f2a44; border:1px solid #e5eaf2; border-width:0 1px 1px 0; background:#fff; }
.indi-table td:last-child { border-right:none; }
.indi-table tbody tr:last-child td { border-bottom:none; }
.indi-table tbody tr:hover td { background:#f0f4fd; }

/* 选中行样式：背景采用图一所示的明亮活力蓝 #5F8EF1，所有文字和操作图标均为白色，hover 不能覆盖 */
.indi-table tbody tr.selected td { background:#5F8EF1 !important; color:#ffffff !important; }
.indi-table tbody tr.selected:hover td { background:#5F8EF1 !important; color:#ffffff !important; }
.indi-table tbody tr.selected .code,
.indi-table tbody tr.selected .num,
.indi-table tbody tr.selected .val,
.indi-table tbody tr.selected .name-txt,
.indi-table tbody tr.selected .month-cell { color:#ffffff !important; }
.indi-table tbody tr.selected .link:hover,
.indi-table tbody tr.selected .month-cell:hover { color:#ffffff !important; text-decoration:underline; }
.indi-table tbody tr.selected .mini { color:#ffffff !important; opacity:1 !important; }
.indi-table tbody tr.selected .mini svg { stroke:#ffffff !important; }
.indi-table tbody tr.selected .formula-icon { background:rgba(255,255,255,0.25) !important; color:#ffffff !important; opacity:1 !important; }
.indi-table tbody tr.selected .excl-badge { background:rgba(255,255,255,0.2) !important; color:#ffffff !important; border-color:rgba(255,255,255,0.4) !important; }

/* 选中行下的状态：文字纯白，圆点加纯白微光描边，不使用杂乱的大圈圈底色 */
.indi-table tbody tr.selected .badge {
  background:transparent !important;
  color:#ffffff !important;
  border:none !important;
}
.indi-table tbody tr.selected .badge-dot {
  box-shadow:0 0 0 1.5px #ffffff;
}

/* 多月时允许横向滚动，表头及必要的左侧列保持固定易读 */
.table-wrap.multi-month .indi-table th.t-left:nth-child(1),
.table-wrap.multi-month .indi-table td.code {
  position:sticky; left:0; z-index:3;
}
.table-wrap.multi-month .indi-table th.t-left:nth-child(1) {
  z-index:12; background:#CBD7F5;
}
.table-wrap.multi-month .indi-table th.t-left:nth-child(2),
.table-wrap.multi-month .indi-table td.name {
  position:sticky; left:90px; z-index:3;
}
.table-wrap.multi-month .indi-table th.t-left:nth-child(2) {
  z-index:12; background:#CBD7F5;
}
.table-wrap.multi-month .indi-table tbody tr:hover td.code,
.table-wrap.multi-month .indi-table tbody tr:hover td.name {
  background:#f0f4fd;
}
.table-wrap.multi-month .indi-table tbody tr.selected td.code,
.table-wrap.multi-month .indi-table tbody tr.selected td.name,
.table-wrap.multi-month .indi-table tbody tr.selected:hover td.code,
.table-wrap.multi-month .indi-table tbody tr.selected:hover td.name {
  background:#5F8EF1 !important;
}

.t-left{text-align:left} .t-right{text-align:right} .t-center{text-align:center}

/* 数字专用等宽字体 — Windows 优先 Consolas/Cascadia */
.num, .month-cell, .t-right, .code, .val {
  font-family: 'Cascadia Code', 'Consolas', 'SF Mono', 'JetBrains Mono', ui-monospace, monospace;
  font-variant-numeric:tabular-nums;
}
.sep { border-right:1px solid #b0c4f0; }

.code { color:#6b7a94; font-size:12px; }
.name { width:230px !important; min-width:230px !important; max-width:230px !important; }
.name-txt {
  vertical-align:middle; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  display:inline-block; max-width:170px; outline:none;
}
.name:hover .name-txt,
.name-txt:focus-visible {
  overflow:visible; position:relative; z-index:15; background:#fff; color:#1f2a44;
  box-shadow:0 2px 8px rgba(0,0,0,.15); padding:2px 6px; border-radius:3px; white-space:normal; max-width:360px;
}
.indi-table tbody tr.selected .name:hover .name-txt,
.indi-table tbody tr.selected .name-txt:focus-visible {
  background:#5F8EF1 !important; color:#ffffff !important;
  border:1px solid rgba(255,255,255,0.4) !important;
  box-shadow:0 2px 8px rgba(0,0,0,0.2) !important;
}

/* 分子分母:中性深灰；比值:品牌蓝突出 */
.num { color: #1f2a44; }
.val { color: #1e5eb8; font-weight:600; }
.link { cursor:pointer; }
.link:hover { color:#1e5eb8; }

/* 月份列:默认安静灰色,仅异常点亮 */
.month-cell { color: #6b7a94; font-weight:400; font-size:12px; cursor:pointer; }
.month-cell:hover { color:#1e5eb8; text-decoration:underline; }
.month-cell.alert { color: #b26a00; font-weight:500; }

.formula-icon { margin-left:5px; font-style:italic; font-size:11px; color:#1e5eb8;
  background:#eaf1fb; border-radius:3px; padding:0 4px; opacity:.6; cursor:help; }
.name:hover .formula-icon { opacity:1; }

/* 状态展示：彻底去除臃肿的大椭圆圈圈底色，纯净点标 + 文字，紧凑清爽 */
.badge {
  padding:0;
  border-radius:0;
  font-size:12px;
  font-weight:500;
  display:inline-flex;
  align-items:center;
  gap:5px;
  background:transparent !important;
  border:none !important;
}
.badge-dot {
  width:6px;
  height:6px;
  border-radius:50%;
  flex-shrink:0;
  display:inline-block;
}
.badge.good { color:#0e7a52; }
.badge.warn { color:#b26a00; }
.badge.danger { color:#c62828; }
.mini { cursor:pointer; opacity:.35; } .mini:hover { opacity:.6; }

.formula-tip { position:fixed; z-index:200; pointer-events:none; background:#fff;
  border:1px solid var(--border); border-radius:9px; box-shadow:var(--shadow-md);
  padding:12px 16px; min-width:230px; max-width:360px; }
.tip-title { font-size:12px; font-weight:600; color:var(--text-main); margin-bottom:10px; }
.tip-formula { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.fraction { display:inline-flex; flex-direction:column; text-align:center; }
.numerator, .denominator { padding:2px 8px; font-size:12px; white-space:nowrap; }
.fraction-line { height:1px; background:var(--text-main); }
.multiplier { font-size:12px; color:var(--brand); }
.tip-special .special-line { font-size:12px; line-height:1.8; color:var(--text-main); }
.tip-note { font-size:12px; line-height:1.6; color:var(--text-main); margin-bottom:8px; }
.tip-meaning { font-size:var(--fs-caption); color:var(--text-sub); padding-top:8px; border-top:1px dashed var(--border); }
.census-bar { display:flex; align-items:center; gap:18px; padding:10px 16px; margin-bottom:10px;
  background:var(--bg-subtle); border:1px solid var(--border); border-radius:8px; }
.census-label { font-size:12px; font-weight:700; color:var(--brand); white-space:nowrap;
  padding-right:12px; border-right:1px solid var(--border); }
.census-item { display:flex; flex-direction:column; align-items:center; gap:2px; }
.census-item.clickable { cursor:pointer; padding:4px 8px; border-radius:6px; transition:background .15s; }
.census-item.clickable:hover { background:rgba(44,142,137,0.1); }
.census-item small { font-size:var(--fs-caption); color:var(--text-sub); }
.census-item b { font-size:15px; color:#0f172a; font-family:'Cascadia Code','Consolas',monospace; }
.census-item.total b { color:var(--brand); }
.excl-badge { display: inline-block; margin-left: 6px; font-size: 10px; color: #E8A53D; background: rgba(232,165,61,0.1); border: 1px solid rgba(232,165,61,0.3); border-radius: 4px; padding: 1px 6px; font-weight: 600; vertical-align: middle; }

/* 分子/分母切换标签 */
.part-tabs {
  display: flex;
  gap: 0;
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--bg-subtle);
}
.part-tab {
  flex: 1;
  padding: 10px 16px;
  border: none;
  background: transparent;
  color: var(--text-sub);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  border-right: 1px solid var(--border);
}
.part-tab:last-child {
  border-right: none;
}
.part-tab:hover {
  background: var(--bg-hover);
  color: var(--brand);
}
.part-tab.active {
  background: var(--brand);
  color: #fff;
}
</style>