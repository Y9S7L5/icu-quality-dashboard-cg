<template>
  <div class="dashboard" :class="{ compact: compactMode }">
    <!-- 顶栏：标题 + 范围 + 期间选择 + 状态 -->
    <header class="db-topbar">
      <div class="tt-left">
        <h1 class="tt-title">ICU 质控总览</h1>
        <span class="tt-sub">{{ hospitalName }} · {{ scopeLabel }}</span>
      </div>
      <div class="tt-right">
        <div class="period-selector">
          <select v-model.number="year" @change="loadData()" aria-label="年份">
            <option v-for="y in years" :key="y" :value="y">{{ y }}年</option>
          </select>
          <select v-model.number="sMonth" @change="loadData()" aria-label="起始月">
            <option v-for="m in 12" :key="m" :value="m">{{ m }}月</option>
          </select>
          <span class="period-to">至</span>
          <select v-model.number="eMonth" @change="loadData()" aria-label="结束月">
            <option v-for="m in 12" :key="m" :value="m">{{ m }}月</option>
          </select>
        </div>
        <span class="status-text" :class="risk.overall_status">
          <i class="dot"></i>{{ overallText }}
        </span>
        <span v-if="updatedAt" class="meta-update">更新 {{ updatedAt }}</span>
        <button class="density-btn" @click="compactMode = !compactMode"
                :title="compactMode ? '切换舒适模式' : '切换紧凑模式'">
          <svg v-if="compactMode" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h12M2 8h12M2 12h12"/></svg>
          <svg v-else viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 5h10M3 8h10M3 11h10"/></svg>
        </button>
      </div>
    </header>

    <div v-if="error" class="state error">{{ error }}</div>
    <div v-else-if="loading" class="state loading">
      <svg class="spin-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1v4M8 11v4M1 8h4M11 8h4M3.05 3.05l2.83 2.83M10.12 10.12l2.83 2.83M3.05 12.95l2.83-2.83M10.12 5.88l2.83-2.83"/></svg>
      正在读取预聚合质控数据...
    </div>
    <div v-else-if="rebuilding" class="state rebuilding">
      <svg class="spin-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1v4M8 11v4M1 8h4M11 8h4M3.05 3.05l2.83 2.83M10.12 10.12l2.83 2.83M3.05 12.95l2.83-2.83M10.12 5.88l2.83-2.83"/></svg>
      后台补算中，正在处理缺失月份: {{ missingPeriods.join(', ') }}。页面将在完成后自动刷新。
    </div>
    <div v-else-if="rebuildError" class="state error">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1L15 14H1L8 1zM8 6v4M8 12h.01"/></svg>
      补算失败: {{ rebuildError }}
    </div>
    <div v-else-if="!dataComplete && missingPeriods.length" class="state warning">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1L15 14H1L8 1zM8 6v4M8 12h.01"/></svg>
      部分月份数据缺失 ({{ missingPeriods.join(', ') }})，显示为「/」。点击刷新可触发补算。
    </div>

    <!-- KPI 六宫格 -->
    <section class="kpi-row">
      <div v-for="kpi in kpiCards" :key="kpi.code" class="kpi-tile" @click="openDetail(kpi)">
        <div class="kpi-name">
          <i class="dot" :class="kpi.status"></i>
          <span class="kpi-label">{{ kpi.name }}</span>
          <em class="kpi-code">{{ displayCode(kpi.code) }}</em>
        </div>
        <div class="kpi-value">
          <strong class="tabular-nums">{{ kpi.valueText }}</strong>
          <span class="kpi-unit">{{ kpi.unit }}</span>
        </div>
        <div class="kpi-deltas">
          <DeltaBadge :text="kpi.momText" :tooltip="kpi.momTooltip" :css-class="kpi.momClass"
                      :arrow="kpi.momArrow" label="环比" :small-sample="kpi.momSmall" />
          <DeltaBadge :text="kpi.yoyText" :tooltip="kpi.yoyTooltip" :css-class="kpi.yoyClass"
                      :arrow="kpi.yoyArrow" label="同比" :small-sample="kpi.yoySmall" />
        </div>
      </div>
    </section>

    <!-- 焦点横幅：达标环 + 异常摘要 + 质控提示 -->
    <section class="focus-banner">
      <div class="fb-donut">
        <svg viewBox="0 0 80 80" class="donut-svg" aria-label="总体达标分布">
          <circle cx="40" cy="40" r="30" fill="none" stroke="var(--border)" stroke-width="9" />
          <circle v-for="(s, i) in donutSegs" :key="i" cx="40" cy="40" r="30" fill="none"
                  :stroke="s.color" stroke-width="9" stroke-linecap="butt"
                  :stroke-dasharray="s.len + ' ' + (188.5 - s.len)"
                  :stroke-dashoffset="s.offset"
                  transform="rotate(-90 40 40)" />
          <text x="40" y="39" class="donut-num">{{ riskGood }}</text>
          <text x="40" y="52" class="donut-cap">/ {{ donutTotal }} 达标</text>
        </svg>
        <div class="fb-legend">
          <span><i class="dot good"></i>达标 {{ riskCounts.good }}</span>
          <span><i class="dot warn"></i>预警 {{ riskCounts.warn }}</span>
          <span><i class="dot danger"></i>异常 {{ riskCounts.danger }}</span>
          <span><i class="dot unknown"></i>待定 {{ riskCounts.unknown }}</span>
        </div>
      </div>
      <div class="fb-main">
        <div class="fb-headline" :class="headlineClass">{{ headlineText }}</div>
        <div v-if="abnormalList.length" class="fb-chips">
          <button v-for="a in abnormalList.slice(0, 3)" :key="a.code" class="fb-chip" @click="openDetail(a)">
            <i class="dot" :class="a.status"></i>
            <em>{{ displayCode(a.code) }}</em>
            <span>{{ a.name }}</span>
            <b class="tabular-nums">{{ fmtValue(a.value) }}{{ a.unit }}</b>
          </button>
        </div>
        <button class="link-btn" @click="activeTab = 'abnormal'">
          查看全部异常清单（{{ abnormalList.length }}）
        </button>
      </div>
      <div class="fb-ai">
        <div class="fb-ai-label">质控提示</div>
        <p class="fb-ai-text">{{ ai.summary || '暂无质控提示' }}</p>
        <button class="link-btn" @click="aiVisible = true">展开完整提示</button>
      </div>
    </section>

    <!-- Tab 导航 -->
    <nav class="tab-bar">
      <button v-for="t in tabs" :key="t.key" class="tab-btn" :class="{ active: activeTab === t.key }"
              @click="activeTab = t.key">
        {{ t.label }}
        <span v-if="t.key === 'abnormal' && abnormalList.length" class="tab-badge">{{ abnormalList.length }}</span>
      </button>
    </nav>

    <!-- Tab · 异常清单 -->
    <section v-show="activeTab === 'abnormal'" class="tab-panel">
      <div class="panel-head">
        <h2>异常与预警</h2>
        <span class="head-note">按严重程度排序，点击行查看患者明细</span>
      </div>
      <div v-if="abnormalList.length" class="ab-table">
        <div class="ab-row ab-head">
          <span>状态</span><span>编码</span><span>指标名称</span><span>当前值</span>
          <span>分子 / 分母</span><span>环比</span><span>提示</span>
        </div>
        <div v-for="a in abnormalList" :key="a.code" class="ab-row" @click="openDetail(a)">
          <span class="c-status"><i class="dot" :class="a.status"></i>{{ statusText(a.status) }}</span>
          <span class="c-code">{{ displayCode(a.code) }}</span>
          <span class="c-name">{{ a.name }}</span>
          <span class="c-value tabular-nums">{{ fmtValue(a.value) }}{{ a.unit }}</span>
          <span class="c-nd tabular-nums">{{ a.numerator ?? '/' }} / {{ a.denominator ?? '/' }}</span>
          <span class="c-delta">{{ a.delta ?? '—' }}</span>
          <span class="c-hint">{{ a.hint || '' }}</span>
        </div>
      </div>
      <div v-else class="empty">
        当前范围内暂无异常或预警指标，可切换上方标签查看各项指标明细。
      </div>
    </section>

    <!-- Tab · 结构 -->
    <section v-show="activeTab === 'structure'" class="tab-panel">
      <div class="panel-head">
        <h2>结构与资源</h2>
        <span class="zone-stats-inline">
          <em v-if="zoneStats.structure.good" class="s-good">{{ zoneStats.structure.good }} 项达标</em>
          <em v-if="zoneStats.structure.warn" class="s-warn">{{ zoneStats.structure.warn }} 项预警</em>
          <em v-if="zoneStats.structure.danger" class="s-danger">{{ zoneStats.structure.danger }} 项异常</em>
        </span>
      </div>
      <div class="ring-grid">
        <div v-for="code in structureCodes" :key="code" class="ring-cell" @click="openDetail(rowsByCode[code])">
          <RingProgress
            :name="rowsByCode[code]?.name || code"
            :value="rowsByCode[code]?.value ?? null"
            :unit="rowsByCode[code]?.unit || '%'"
            :status="rowsByCode[code]?.status || 'unknown'"
            :numerator-label="getIndicatorProp(code, 'numerator')"
            :numerator="rowsByCode[code]?.numerator"
            :denominator-label="getIndicatorProp(code, 'denominator')"
            :denominator="rowsByCode[code]?.denominator"
          />
        </div>
      </div>
    </section>

    <!-- Tab · 过程 -->
    <section v-show="activeTab === 'process'" class="tab-panel">
      <div class="panel-head">
        <h2>过程质量</h2>
        <span class="zone-stats-inline">
          <em v-if="zoneStats.process.good" class="s-good">{{ zoneStats.process.good }} 项达标</em>
          <em v-if="zoneStats.process.warn" class="s-warn">{{ zoneStats.process.warn }} 项预警</em>
          <em v-if="zoneStats.process.danger" class="s-danger">{{ zoneStats.process.danger }} 项异常</em>
        </span>
      </div>
      <div class="proc-grid">
        <div class="proc-tiles">
          <div v-for="t in processTiles" :key="t.code" class="metric-tile" @click="openDetail(rowsByCode[t.code])">
            <div class="mt-top">
              <span class="mt-name">{{ t.name }}</span>
              <i class="dot" :class="t.status"></i>
            </div>
            <div class="mt-value tabular-nums">
              {{ t.valueText }}<small>{{ t.unit }}</small>
            </div>
            <div class="mt-bar">
              <i class="mt-fill" :class="t.status" :style="{ width: t.fill + '%' }"></i>
              <i class="mt-tick" :style="{ left: t.tick + '%' }"></i>
            </div>
            <div class="mt-foot tabular-nums">目标 {{ t.targetText }} · {{ t.numerator ?? '/' }}/{{ t.denominator ?? '/' }}</div>
          </div>
        </div>
        <div class="proc-chart">
          <div class="sub-head">达标排行（竖线为目标值）</div>
          <HBarRankChart :items="processHbarItems" unit="%" :height="260" @click="handleChartClick" />
        </div>
      </div>
    </section>

    <!-- Tab · 结果 -->
    <section v-show="activeTab === 'outcome'" class="tab-panel">
      <div class="panel-head">
        <h2>结果质量</h2>
        <span class="zone-stats-inline">
          <em v-if="zoneStats.outcome.good" class="s-good">{{ zoneStats.outcome.good }} 项达标</em>
          <em v-if="zoneStats.outcome.warn" class="s-warn">{{ zoneStats.outcome.warn }} 项预警</em>
          <em v-if="zoneStats.outcome.danger" class="s-danger">{{ zoneStats.outcome.danger }} 项异常</em>
        </span>
      </div>
      <div class="outcome-grid">
        <div v-for="t in outcomeTiles" :key="t.code" class="metric-tile" @click="openDetail(rowsByCode[t.code])">
          <div class="mt-top">
            <span class="mt-name">{{ t.name }}</span>
            <i class="dot" :class="t.status"></i>
          </div>
          <div class="mt-value tabular-nums">
            {{ t.valueText }}<small>{{ t.unit }}</small>
          </div>
          <div class="mt-bar">
            <i class="mt-fill" :class="t.status" :style="{ width: t.fill + '%' }"></i>
            <i class="mt-tick" :style="{ left: t.tick + '%' }"></i>
          </div>
          <div class="mt-foot tabular-nums">目标 {{ t.targetText }} · {{ t.numerator ?? '/' }}/{{ t.denominator ?? '/' }}</div>
        </div>
      </div>
      <div class="smr-row">
        <SmrChart :current="smrCurrent" :history="smrHistory" :months="months" />
      </div>
    </section>

    <!-- Tab · 组合分析 -->
    <section v-show="activeTab === 'combo'" class="tab-panel">
      <div class="panel-head"><h2>组合分析</h2></div>
      <div class="combo-grid">
        <!-- 感染防控 -->
        <div class="panel combo-panel">
          <div class="panel-title">感染防控</div>
          <GroupedBarChart :categories="infectionCategories" :series="infectionSeries" unit="‰" :height="240" />
        </div>
        <!-- Sepsis Bundle -->
        <div class="panel combo-panel">
          <div class="panel-title">Sepsis Bundle</div>
          <FunnelBundleChart :data="bundleFunnelData" :height="240" />
          <div v-if="icu05CandidateStats" class="cand-strip">
            <span>原始候选 <b>{{ icu05CandidateStats.raw_candidate_count }}</b></span>
            <span>高概率 <b>{{ icu05CandidateStats.high_probability_count }}</b></span>
            <span>很可能 <b>{{ icu05CandidateStats.probable_count }}</b></span>
            <span>待复核 <b>{{ icu05CandidateStats.pending_review_count }}</b></span>
            <span class="cand-divider"></span>
            <span>新口径确诊 <b>{{ icu05CandidateStats.new_shock_count }}</b></span>
            <span>旧口径确诊 <b>{{ icu05CandidateStats.old_shock_count }}</b></span>
            <span :class="icu05CandidateStats.shock_diff > 0 ? 'diff-positive' : (icu05CandidateStats.shock_diff < 0 ? 'diff-negative' : '')">
              差异 <b>{{ icu05CandidateStats.shock_diff > 0 ? '+' : '' }}{{ icu05CandidateStats.shock_diff }}</b>
            </span>
          </div>
        </div>
        <!-- 镇痛镇静雷达 -->
        <div class="panel combo-panel">
          <div class="panel-title">镇痛镇静意识评估</div>
          <RadarAssessChart :data="sedationRadarData" :height="240" />
        </div>
        <!-- 人力与负荷 -->
        <div class="panel combo-panel">
          <div class="panel-title">人力与负荷</div>
          <BarTargetChart :items="ratioItems" />
        </div>
        <!-- 患者流转 -->
        <div class="panel combo-panel">
          <div class="panel-title">患者流转</div>
          <CensusStackChart :trend="censusTrend" />
        </div>
        <!-- 气道安全 -->
        <div class="panel combo-panel">
          <div class="panel-title">气道安全</div>
          <GroupedBarChart :categories="airwayCategories" :series="airwaySeries" unit="%" :height="240" />
        </div>
      </div>
    </section>

    <!-- Tab · 环比同比表 -->
    <section v-show="activeTab === 'compare'" class="tab-panel">
      <ComparisonTable
        :rows="rows"
        :trend="trendData"
        :months="months"
        :yoy-trend="yoyTrend"
        :yoy-months="yoyMonths"
        :rows-by-code="rowsByCode"
        @row-click="openDetail"
      />
    </section>

    <!-- 底部 -->
    <footer class="db-footer">
      <span>数据来源：医院信息系统（HIS）| ICU质量管理系统（ICU-QMS）</span>
      <span>本看板数据仅供医疗质量管理参考，不作为临床决策依据</span>
    </footer>

    <!-- 弹窗 -->
    <Modal v-if="aiVisible" title="质控提示" @close="aiVisible = false">
      <AiPanel :analysis="ai" />
    </Modal>
    <Modal v-if="detailVisible" :title="detailTitle" @close="detailVisible = false">
      <DetailModal :data="detailData" :summary="detailData.summary" :parts="detailData.parts"
                   :funnel="detailData.funnel"
                   :period="ps" :end-period="endPeriodParam" :unit="detailUnit" :unit-name="detailUnitName"
                   @exclusion-changed="onDetailExclusionChanged" />
    </Modal>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted, onUnmounted, watch } from 'vue';
import { INDICATORS, getStatusConfig, statusText as getStatusLabel } from '../config/indicators.js';
import { INDICATOR_GROUPS, getGroupStats } from '../config/indicatorGroups.js';
import { fetchCommandCenter, fetchDetail, fetchRebuildStatus } from '../api/index.js';
import { getMoM, getYoY, formatDelta, formatValue } from '../utils/compare.js';

// 组件
import DeltaBadge from '../components/DeltaBadge.vue';
import RingProgress from '../components/RingProgress.vue';
import HBarRankChart from '../components/HBarRankChart.vue';
import GroupedBarChart from '../components/GroupedBarChart.vue';
import FunnelBundleChart from '../components/FunnelBundleChart.vue';
import RadarAssessChart from '../components/RadarAssessChart.vue';
import BarTargetChart from '../components/BarTargetChart.vue';
import CensusStackChart from '../components/CensusStackChart.vue';
import SmrChart from '../components/SmrChart.vue';
import ComparisonTable from '../components/ComparisonTable.vue';
import AiPanel from '../components/AiPanel.vue';
import Modal from '../components/Modal.vue';
import DetailModal from '../components/DetailModal.vue';

const hospitalName = '重庆医科大学附属第二医院';

// ---- 状态 ----
const year = ref(new Date().getFullYear());
const sMonth = ref(new Date().getMonth() + 1);
const eMonth = ref(new Date().getMonth() + 1);
const hostDeptCode = inject('hostDeptCode', ref('all'));
const dept = computed(() => hostDeptCode.value || 'all');
const years = computed(() => {
  const arr = [];
  for (let y = 2024; y <= new Date().getFullYear() + 1; y++) arr.push(y);
  return arr;
});
const compactMode = ref(false);

const rows = ref([]);
const rowsByCode = ref({});
const values = ref({});
const trendData = ref({});
const months = ref([]);
const risk = ref({ overall_status: 'unknown', counts: {} });
const abnormal = ref([]);
const ai = ref({ summary: '', hints: [], todos: [], tri_tube: {}, low_confidence: {} });
const loading = ref(false);
const error = ref('');
const updatedAt = ref('');
const statusConfig = ref(getStatusConfig());
const censusTrend = ref([]);

// 补算状态
const rebuilding = ref(false);
const rebuildError = ref(null);
const missingPeriods = ref([]);
const dataComplete = ref(true);
let rebuildPollTimer = null;

// 同比数据
const yoyTrend = ref(null);
const yoyMonths = ref(null);

// 弹窗
const detailVisible = ref(false);
const detailData = ref(null);
const detailTitle = ref('');
const detailUnit = ref('');
const detailUnitName = ref('');
const aiVisible = ref(false);

// Tab
const tabs = [
  { key: 'abnormal', label: '异常清单' },
  { key: 'structure', label: '结构' },
  { key: 'process', label: '过程' },
  { key: 'outcome', label: '结果' },
  { key: 'combo', label: '组合分析' },
  { key: 'compare', label: '环比同比表' },
];
const activeTab = ref('abnormal');

const ps = computed(() => `${year.value}-${String(sMonth.value).padStart(2, '0')}`);
const pe = computed(() => `${year.value}-${String(eMonth.value).padStart(2, '0')}`);
const endPeriodParam = computed(() => sMonth.value === eMonth.value ? '' : pe.value);

// ---- 工具 ----
function displayCode(code) {
  return INDICATORS.find(i => i.code === code)?.displayCode || code;
}
function statusText(status) {
  return getStatusLabel(status, statusConfig.value);
}
function fmtValue(v) {
  return v == null || Number.isNaN(Number(v)) ? '/' : v;
}
function getIndicatorProp(code, prop) {
  return INDICATORS.find(i => i.code === code)?.[prop] || '';
}
const scopeLabel = computed(() => (dept.value === 'all' ? '全部科室' : `科室 ${dept.value}`));

// ---- 分组 ----
const structureCodes = computed(() => {
  const group = INDICATOR_GROUPS.find(g => g.id === 'structure');
  return (group?.codes || []).filter(c => rowsByCode.value[c]);
});
const processCodes = computed(() => {
  const group = INDICATOR_GROUPS.find(g => g.id === 'process');
  return (group?.codes || []).filter(c => rowsByCode.value[c]);
});
const outcomeCodes = computed(() => {
  const group = INDICATOR_GROUPS.find(g => g.id === 'outcome');
  return (group?.codes || []).filter(c => rowsByCode.value[c]);
});

const zoneStats = computed(() => {
  const stats = {};
  for (const g of INDICATOR_GROUPS) {
    if (['structure', 'process', 'outcome'].includes(g.id)) {
      stats[g.id] = getGroupStats(g, rowsByCode.value);
    }
  }
  return stats;
});

// ---- KPI 六宫格 ----
const kpiCardCodes = ['ICU-01', 'ICU-04-01', 'ICU-06', 'ICU-11', 'ICU-16', 'ICU-17'];
const kpiCards = computed(() => {
  return kpiCardCodes.map(code => {
    const row = rowsByCode.value[code];
    if (!row) return null;
    const mom = getMoM(code, trendData.value, months.value, row.denominator);
    const yoy = getYoY(code, trendData.value, months.value, yoyTrend.value, yoyMonths.value, row.denominator);
    const momFmt = formatDelta(mom, 'mom');
    const yoyFmt = formatDelta(yoy, 'yoy');
    return {
      code,
      name: row.name,
      value: row.value,
      valueText: row.value == null ? '—' : formatValue(row.value, code),
      unit: row.value == null ? '' : (row.unit || ''),
      status: row.status || 'unknown',
      numerator: row.numerator,
      denominator: row.denominator,
      momText: momFmt.text,
      momTooltip: momFmt.tooltip,
      momClass: momFmt.cssClass,
      momArrow: momFmt.arrow,
      momSmall: momFmt.smallSample,
      yoyText: yoyFmt.text,
      yoyTooltip: yoyFmt.tooltip,
      yoyClass: yoyFmt.cssClass,
      yoyArrow: yoyFmt.arrow,
      yoySmall: yoyFmt.smallSample,
    };
  }).filter(Boolean);
});

// ---- 异常清单 ----
const abnormalList = computed(() => (abnormal.value || []).filter(i => {
  if (i.status === 'unknown') return false;
  const ind = INDICATORS.find(x => x.code === i.code);
  return !ind?.excludeFromAlert;
}));

// ---- 达标环 ----
const riskCounts = computed(() => {
  const c = risk.value.counts || {};
  return {
    good: c.good || 0,
    warn: c.warn || 0,
    danger: c.danger || 0,
    unknown: c.unknown || 0,
  };
});
const donutTotal = computed(() => {
  const c = riskCounts.value;
  return c.good + c.warn + c.danger + c.unknown;
});
const riskGood = computed(() => riskCounts.value.good);
const donutSegs = computed(() => {
  const c = riskCounts.value;
  const total = donutTotal.value;
  if (!total) return [];
  const C = 188.5;
  const colorMap = {
    good: 'var(--good)',
    warn: 'var(--warn)',
    danger: 'var(--danger)',
    unknown: 'var(--text-faint)',
  };
  let acc = 0;
  return ['good', 'warn', 'danger', 'unknown'].filter(k => c[k] > 0).map(k => {
    const len = (c[k] / total) * C;
    const seg = { color: colorMap[k], len, offset: -acc };
    acc += len;
    return seg;
  });
});
const headlineText = computed(() => {
  const c = riskCounts.value;
  if (c.danger || c.warn) return `${c.danger} 项异常 · ${c.warn} 项预警需关注`;
  if (donutTotal.value) return '本期全部指标平稳';
  return '暂无数据';
});
const headlineClass = computed(() => {
  const c = riskCounts.value;
  if (c.danger) return 'is-danger';
  if (c.warn) return 'is-warn';
  return 'is-good';
});

// ---- 指标格（过程 / 结果） ----
function buildTile(code, better) {
  const row = rowsByCode.value[code];
  if (!row) return null;
  const ind = INDICATORS.find(i => i.code === code);
  const th = ind?.thresholds?.good || [];
  const target = better === 'high' ? (th[0] ?? 80) : (th[1] ?? 5);
  const val = Number(row.value) || 0;
  let fill, tick;
  if (better === 'high') {
    // 越高越好：刻度 0-100，目标线在 target%
    fill = Math.max(0, Math.min(val, 100));
    tick = Math.max(0, Math.min(target, 100));
  } else {
    // 越低越好：量程 = max(2×目标, 当前值)
    const scale = Math.max(target * 2, val, 1);
    fill = Math.max(0, Math.min((val / scale) * 100, 100));
    tick = Math.max(0, Math.min((target / scale) * 100, 100));
  }
  return {
    code,
    name: row.name,
    status: row.status || 'unknown',
    valueText: formatValue(row.value, code),
    unit: row.value == null ? '' : (row.unit || ''),
    fill,
    tick,
    targetText: `${target}${row.unit || '%'}`,
    numerator: row.numerator,
    denominator: row.denominator,
  };
}
const processTiles = computed(() => processCodes.value.map(c => buildTile(c, 'high')).filter(Boolean));
const outcomeTiles = computed(() => outcomeCodes.value.filter(c => c !== 'ICU-11').map(c => buildTile(c, 'low')).filter(Boolean));

// ---- 过程 / 结果 hbar ----
const processHbarItems = computed(() => {
  return processCodes.value.map(code => {
    const row = rowsByCode.value[code];
    if (!row) return null;
    const ind = INDICATORS.find(i => i.code === code);
    const target = ind?.thresholds?.good?.[0] || 80;
    return { name: row.name, value: row.value, target, status: row.status };
  }).filter(Boolean);
});

// ---- SMR ----
const smrCurrent = computed(() => values.value['ICU-11'] ?? 1);
const smrHistory = computed(() => trendData.value['ICU-11'] ?? []);

// ---- 感染防控 ----
const infectionCategories = computed(() => {
  return ['VAP', 'CRBSI', 'CAUTI'].filter((_, i) => {
    const codes = ['ICU-16', 'ICU-17', 'CAUTI'];
    return rowsByCode.value[codes[i]]?.value != null;
  });
});
const infectionSeries = computed(() => {
  const codes = ['ICU-16', 'ICU-17', 'CAUTI'];
  return [{
    name: '本期',
    data: codes.map(c => rowsByCode.value[c]?.value ?? 0),
    color: '#1e5eb8',
  }];
});

// ---- Sepsis 漏斗 ----
const bundleFunnelData = computed(() => {
  const codes = ['ICU-05-1h', 'ICU-05-3h', 'ICU-05-6h'];
  return codes.map(code => {
    const row = rowsByCode.value[code];
    return {
      code,
      name: row?.name?.replace('感染性休克', '').replace('完成率', '') || code,
      value: row?.value || 0,
    };
  });
});
const icu05CandidateStats = computed(() => {
  const row = rowsByCode.value['ICU-05-1h'] || rowsByCode.value['ICU-05-3h'];
  if (!row) return null;
  return {
    raw_candidate_count: row.raw_candidate_count || 0,
    high_probability_count: row.high_probability_count || 0,
    probable_count: row.probable_count || 0,
    pending_review_count: row.pending_review_count || 0,
    new_shock_count: row.new_shock_count || 0,
    old_shock_count: row.old_shock_count || 0,
    shock_diff: row.shock_diff || 0,
  };
});

// ---- 镇痛镇静雷达 ----
const sedationRadarData = computed(() => {
  const codes = ['ICU-09', 'ICU-10', 'ICU-18'];
  return codes.map(code => {
    const row = rowsByCode.value[code];
    const ind = INDICATORS.find(i => i.code === code);
    return {
      code,
      name: row?.name || code,
      value: row?.value || 0,
      target: ind?.thresholds?.good?.[0] || 90,
    };
  });
});

// ---- 人力配置 ----
const ratioItems = computed(() => [
  { name: '医生床位比', value: values.value['ICU-02'] ?? 0, target: 0.8 },
  { name: '护士床位比', value: values.value['ICU-03'] ?? 0, target: 2.5 },
]);

// ---- 气道安全 ----
const airwayCategories = computed(() => {
  return ['非计划拔管', '48h再插管'].filter((_, i) => {
    const codes = ['ICU-12', 'ICU-13'];
    return rowsByCode.value[codes[i]]?.value != null;
  });
});
const airwaySeries = computed(() => {
  const codes = ['ICU-12', 'ICU-13'];
  return [{
    name: '本期',
    data: codes.map(c => rowsByCode.value[c]?.value ?? 0),
    color: '#c62828',
  }];
});

// ---- 总体 ----
const overallText = computed(() => {
  if (risk.value.overall_status === 'danger') return '异常';
  if (risk.value.overall_status === 'warn') return '预警';
  if (risk.value.overall_status === 'good') return '平稳';
  return '待刷新';
});

// ---- 交互 ----
let detailSeq = 0; // 明细请求序号：快速切换指标时丢弃过期响应

async function openDetail(row) {
  // 加载中 rows 尚未切换到当前期间：禁止下钻，防止「公式条(旧行) ≠ 明细(新期间)」
  if (loading.value || rebuilding.value) return;
  if (!row || !row.code) return;
  const seq = ++detailSeq;
  detailTitle.value = `${displayCode(row.code)} ${row.name} — 患者明细`;
  detailUnit.value = row.unit || '';
  detailUnitName.value = row.name;
  detailVisible.value = true;

  const base = { name: row.name, code: row.code, part: 'numerator', loading: true, patients: [], source_desc: '加载中...', count: 0 };
  const summary = { value: row.value, unit: row.unit || '' };
  if (row.numerator != null) summary.numerator = row.numerator;
  if (row.denominator != null) summary.denominator = row.denominator;
  const funnel = String(row.code).startsWith('ICU-05') ? {
    candidate: row.candidate_den ?? row.denominator ?? null,
    shock: row.old_shock_count ?? null,
    completed: row.numerator ?? null,
  } : null;
  detailData.value = { ...base, summary, funnel };
  try {
    const [numR, denR] = await Promise.all([
      fetchDetail(row.code, ps.value, 'numerator', dept.value, endPeriodParam.value, { limit: 200, offset: 0 }),
      fetchDetail(row.code, ps.value, 'denominator', dept.value, endPeriodParam.value, { limit: 200, offset: 0 }),
    ]);
    if (seq !== detailSeq) return;
    detailData.value = {
      ...numR,
      summary,
      parts: { numerator: numR, denominator: denR },
      funnel,
    };
  } catch (e) {
    if (seq !== detailSeq) return;
    detailData.value = { ...base, loading: false, error: e.message || '明细加载失败', source_desc: '明细加载失败', summary, funnel };
  }
}

async function onDetailExclusionChanged() {
  if (!detailData.value) return;
  const seq = ++detailSeq;
  const cur = detailData.value;
  const base = { ...cur, loading: true, patients: [], source_desc: '重新加载中...' };
  detailData.value = base;
  try {
    const [numR, denR] = await Promise.all([
      fetchDetail(cur.code, ps.value, 'numerator', dept.value, endPeriodParam.value, { limit: 200, offset: 0 }),
      fetchDetail(cur.code, ps.value, 'denominator', dept.value, endPeriodParam.value, { limit: 200, offset: 0 }),
    ]);
    if (seq !== detailSeq) return;
    const summary = { ...(cur.summary || {}) };
    if (summary.numerator != null) summary.numerator = numR.count;
    if (summary.denominator != null) summary.denominator = denR.count;
    const funnel = cur.funnel ? { ...cur.funnel, candidate: denR.count, completed: numR.count } : null;
    detailData.value = { ...numR, summary, parts: { numerator: numR, denominator: denR }, funnel };
  } catch (e) {
    if (seq !== detailSeq) return;
    detailData.value = { ...base, loading: false, error: e.message || '明细加载失败' };
  }
}

function handleChartClick(params) {
  if (params?.name) {
    const row = rows.value.find(r => r.name === params.name);
    if (row) openDetail(row);
  }
}

// ---- 数据加载 ----
let yoyCache = {};
let loadSeq = 0;   // 请求序号：期间快速切换时，丢弃过期响应，防止旧行覆盖新数据

async function loadData(nocache = false) {
  if (eMonth.value < sMonth.value) eMonth.value = sMonth.value;
  const seq = ++loadSeq;
  loading.value = true;
  error.value = '';
  try {
    const res = await fetchCommandCenter(ps.value, endPeriodParam.value, dept.value, nocache);
    if (seq !== loadSeq) return; // 已有更新的请求，本响应作废
    rows.value = res.rows || [];
    rowsByCode.value = Object.fromEntries(rows.value.map(r => [r.code, r]));
    values.value = res.values || {};
    trendData.value = res.trend || {};
    months.value = res.months || [];
    risk.value = res.risk || { overall_status: 'unknown', counts: {} };
    abnormal.value = res.abnormal || [];
    ai.value = res.ai || { summary: '', hints: [], todos: [] };
    updatedAt.value = res.updated_at ? res.updated_at.replace('T', ' ') : '';
    censusTrend.value = res.census_trend || [];

    dataComplete.value = res.data_complete !== false;
    missingPeriods.value = res.missing_periods || [];
    if (res.rebuilding) {
      rebuilding.value = true;
      startRebuildPolling();
    } else {
      rebuilding.value = false;
      stopRebuildPolling();
    }

    loadYoYData(seq);
  } catch (e) {
    if (seq !== loadSeq) return;
    error.value = e.message || '大屏数据读取失败';
    rows.value = [];
    rowsByCode.value = {};
    values.value = {};
    trendData.value = {};
    abnormal.value = [];
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

async function loadYoYData(seq) {
  const yoyYear = year.value - 1;
  const yoyPeriod = `${yoyYear}-${String(sMonth.value).padStart(2, '0')}`;
  const yoyEnd = sMonth.value === eMonth.value ? '' : `${yoyYear}-${String(eMonth.value).padStart(2, '0')}`;
  const cacheKey = `${yoyPeriod}-${yoyEnd}-${dept.value}`;

  if (yoyCache[cacheKey]) {
    if (seq === loadSeq) {
      yoyTrend.value = yoyCache[cacheKey].trend;
      yoyMonths.value = yoyCache[cacheKey].months;
    }
    return;
  }

  try {
    const res = await fetchCommandCenter(yoyPeriod, yoyEnd, dept.value);
    if (seq !== loadSeq) return; // 期间又变了，丢弃
    if (res.trend && res.months) {
      yoyTrend.value = res.trend;
      yoyMonths.value = res.months;
      yoyCache[cacheKey] = { trend: res.trend, months: res.months };
    } else {
      yoyTrend.value = null;
      yoyMonths.value = null;
    }
  } catch {
    if (seq !== loadSeq) return;
    yoyTrend.value = null;
    yoyMonths.value = null;
  }
}

// ---- 补算轮询 ----
const MAX_POLL_FAILURES = 6;
let pollFailureCount = 0;

function startRebuildPolling() {
  if (rebuildPollTimer) return;
  pollFailureCount = 0;
  rebuildPollTimer = setInterval(async () => {
    try {
      const status = await fetchRebuildStatus();
      pollFailureCount = 0;
      if (!status.rebuilding) {
        rebuilding.value = false;
        rebuildError.value = null;
        stopRebuildPolling();
        loadData(true);
      }
    } catch (e) {
      pollFailureCount++;
      if (pollFailureCount >= MAX_POLL_FAILURES) {
        rebuilding.value = false;
        rebuildError.value = `轮询失败: ${e.message || '网络错误'}`;
        stopRebuildPolling();
      }
    }
  }, 5000);
}

function stopRebuildPolling() {
  if (rebuildPollTimer) {
    clearInterval(rebuildPollTimer);
    rebuildPollTimer = null;
  }
}

// ---- URL 同步 ----
function syncFromURL() {
  const p = new URLSearchParams(window.location.search);
  const y = p.get('year'); if (y) year.value = +y;
  const sm = p.get('sMonth'); if (sm) sMonth.value = +sm;
  const em = p.get('eMonth'); if (em) eMonth.value = +em;
  const tab = p.get('tab'); if (tab && tabs.some(t => t.key === tab)) activeTab.value = tab;
}
function syncToURL() {
  const u = new URL(window.location);
  u.searchParams.set('year', year.value);
  u.searchParams.set('sMonth', sMonth.value);
  u.searchParams.set('eMonth', eMonth.value);
  u.searchParams.set('tab', activeTab.value);
  window.history.replaceState({}, '', u);
}
watch([year, sMonth, eMonth], syncToURL);
watch(activeTab, syncToURL);
watch(hostDeptCode, () => { loadData(true); });

onMounted(() => {
  window.addEventListener('status-config-updated', () => {
    statusConfig.value = getStatusConfig();
  });
  syncFromURL();
  loadData();
});

onUnmounted(() => {
  stopRebuildPolling();
});
</script>

<style scoped>
/* ================= 基础 ================= */
.dashboard {
  padding: 14px 24px 0;
  min-height: 100vh;
  background: var(--bg-app);
  color: var(--text-body);
}
.dashboard.compact { padding: 8px 16px 0; }
.dashboard.compact .kpi-tile { padding: 9px 12px; }
.dashboard.compact .focus-banner { padding: 12px 16px; }
.dashboard.compact .tab-panel { padding-top: 10px; }

.dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-faint); flex-shrink: 0;
}
.dot.good { background: var(--good); }
.dot.warn { background: var(--warn); }
.dot.danger { background: var(--danger); }
.dot.unknown { background: var(--text-faint); }

/* ================= 顶栏 ================= */
.db-topbar {
  position: sticky; top: 0; z-index: 40;
  display: flex; justify-content: space-between; align-items: center;
  gap: 16px; flex-wrap: wrap;
  padding: 4px 0 12px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-app);
}
.tt-left { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
.tt-title {
  font-size: 20px; font-weight: 700; color: var(--text-title);
  line-height: 1.3; margin: 0; letter-spacing: 0.01em;
}
.tt-sub { font-size: 12px; color: var(--text-sub); white-space: nowrap; }
.tt-right { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

.period-selector { display: flex; align-items: center; gap: 5px; }
.period-to { font-size: 12px; color: var(--text-sub); }
.period-selector select {
  background: var(--bg-surface); color: var(--text-body);
  border: 1px solid var(--border); border-radius: 6px;
  padding: 4px 8px; font-size: 12px; height: 28px;
  transition: border-color .15s;
}
.period-selector select:focus { border-color: var(--brand); outline: none; }

.status-text {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; font-weight: 600; color: var(--text-sub);
}
.status-text.good { color: var(--good); }
.status-text.warn { color: var(--warn); }
.status-text.danger { color: var(--danger); }

.meta-update { color: var(--text-faint); font-size: 12px; white-space: nowrap; }

.density-btn {
  display: flex; align-items: center; justify-content: center;
  width: 28px; height: 28px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-surface); color: var(--text-sub); cursor: pointer; transition: all .15s;
}
.density-btn:hover { border-color: var(--brand); color: var(--brand); }
.density-btn svg { width: 14px; height: 14px; }

/* ================= 状态提示 ================= */
.state {
  margin-bottom: 10px; padding: 8px 12px; border-radius: 6px;
  background: var(--brand-weak); color: var(--text-sub); font-size: 13px;
  border: 1px solid rgba(30,94,184,0.15); display: flex; align-items: center; gap: 8px;
}
.state.error { background: var(--danger-weak); color: var(--danger); border-color: rgba(198,40,40,0.15); }
.state.rebuilding, .state.warning { background: var(--warn-weak); color: var(--warn); border-color: rgba(178,106,0,0.15); }
.spin-icon { width: 15px; height: 15px; animation: spin 1s linear infinite; flex-shrink: 0; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ================= KPI 六宫格 ================= */
.kpi-row {
  display: grid; grid-template-columns: repeat(6, 1fr);
  gap: 10px; margin-bottom: 10px;
}
.kpi-tile {
  background: var(--bg-surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 11px 13px; cursor: pointer;
  transition: border-color .15s;
  min-width: 0; display: flex; flex-direction: column; gap: 5px;
}
.kpi-tile:hover { border-color: var(--border-strong); }
.kpi-name { display: flex; align-items: center; gap: 6px; min-width: 0; }
.kpi-label {
  font-size: 12px; color: var(--text-sub); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0;
}
.kpi-code { font-size: 10px; font-style: normal; color: var(--text-faint); font-weight: 600; flex-shrink: 0; }
.kpi-value { display: flex; align-items: baseline; gap: 4px; }
.kpi-value strong { font-size: 25px; font-weight: 700; color: var(--text-title); line-height: 1.1; }
.kpi-unit { font-size: 11px; color: var(--text-sub); }
.kpi-deltas { display: flex; gap: 4px; flex-wrap: wrap; }

/* ================= 焦点横幅 ================= */
.focus-banner {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) minmax(220px, 290px);
  gap: 20px; align-items: center;
  background: var(--brand-weak);
  border: 1px solid rgba(30,94,184,0.16);
  border-radius: 8px;
  padding: 14px 18px;
  margin-bottom: 12px;
}
.fb-donut { display: flex; align-items: center; gap: 14px; }
.donut-svg { width: 84px; height: 84px; flex-shrink: 0; }
.donut-num { font-size: 17px; font-weight: 700; fill: var(--text-title); text-anchor: middle; }
.donut-cap { font-size: 8.5px; fill: var(--text-sub); text-anchor: middle; }
.fb-legend { display: flex; flex-direction: column; gap: 5px; }
.fb-legend span {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--text-sub); white-space: nowrap;
}

.fb-main { display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.fb-headline { font-size: 16px; font-weight: 700; color: var(--text-title); }
.fb-headline.is-danger { color: var(--danger); }
.fb-headline.is-warn { color: var(--warn); }
.fb-headline.is-good { color: var(--good); }
.fb-chips { display: flex; flex-direction: column; gap: 4px; }
.fb-chip {
  display: inline-flex; align-items: center; gap: 8px;
  background: none; border: none; padding: 2px 0; cursor: pointer;
  font-size: 12px; color: var(--text-body); text-align: left;
  max-width: 100%; min-width: 0;
}
.fb-chip:hover span { color: var(--brand); }
.fb-chip em { font-style: normal; color: var(--brand); font-weight: 700; font-size: 11px; flex-shrink: 0; }
.fb-chip span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.fb-chip b { font-weight: 700; color: var(--text-title); flex-shrink: 0; }

.link-btn {
  align-self: flex-start;
  background: none; border: none; padding: 0;
  font-size: 12px; font-weight: 600; color: var(--brand);
  cursor: pointer; text-decoration: underline; text-underline-offset: 3px;
  text-decoration-color: rgba(30,94,184,0.35);
}
.link-btn:hover { text-decoration-color: var(--brand); }

.fb-ai {
  background: var(--bg-surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 12px;
  display: flex; flex-direction: column; gap: 6px; min-width: 0;
}
.fb-ai-label { font-size: 11px; font-weight: 700; color: var(--text-sub); letter-spacing: 0.04em; }
.fb-ai-text {
  font-size: 12px; line-height: 1.55; color: var(--text-body); margin: 0;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}

/* ================= Tab ================= */
.tab-bar {
  display: flex; gap: 2px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 0;
}
.tab-btn {
  background: none; border: none; cursor: pointer;
  padding: 9px 14px; font-size: 13px; color: var(--text-sub);
  border-bottom: 2px solid transparent; margin-bottom: -1px;
  display: inline-flex; align-items: center; gap: 6px;
  transition: color .15s;
}
.tab-btn:hover { color: var(--text-title); }
.tab-btn.active { color: var(--brand); font-weight: 700; border-bottom-color: var(--brand); }
.tab-badge {
  min-width: 17px; height: 17px; padding: 0 5px; border-radius: 9px;
  background: var(--danger-weak); color: var(--danger);
  font-size: 10px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
}

.tab-panel { padding-top: 14px; padding-bottom: 8px; }
.panel-head {
  display: flex; align-items: baseline; gap: 12px;
  margin-bottom: 10px;
}
.panel-head h2 { font-size: 14px; font-weight: 700; color: var(--text-title); margin: 0; }
.head-note { font-size: 11px; color: var(--text-faint); }
.zone-stats-inline { display: inline-flex; gap: 10px; font-size: 11px; font-style: normal; }
.zone-stats-inline em { font-style: normal; font-weight: 600; }
.s-good { color: var(--good); }
.s-warn { color: var(--warn); }
.s-danger { color: var(--danger); }

/* ================= 异常清单表 ================= */
.ab-table {
  background: var(--bg-surface); border: 1px solid var(--border);
  border-radius: 8px; overflow: hidden;
}
.ab-row {
  display: grid;
  grid-template-columns: 76px 88px minmax(140px, 1.3fr) 84px 110px 84px minmax(120px, 1fr);
  gap: 10px; align-items: center;
  padding: 9px 14px;
  border-top: 1px solid var(--bg-subtle);
  font-size: 12.5px;
}
.ab-row:not(.ab-head) { cursor: pointer; transition: background .12s; }
.ab-row:not(.ab-head):hover { background: var(--bg-hover); }
.ab-head {
  border-top: none; background: var(--bg-subtle);
  font-size: 11px; color: var(--text-faint); font-weight: 600;
  padding-top: 7px; padding-bottom: 7px;
}
.c-status { display: inline-flex; align-items: center; gap: 6px; color: var(--text-sub); }
.c-code { color: var(--brand); font-weight: 700; font-size: 11.5px; }
.c-name { color: var(--text-title); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-value { font-weight: 700; color: var(--text-title); }
.c-nd, .c-delta { color: var(--text-sub); }
.c-hint { color: var(--text-faint); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.empty {
  color: var(--text-faint); font-size: 13px; padding: 32px; text-align: center;
  background: var(--bg-surface); border: 1px dashed var(--border-strong); border-radius: 8px;
}

/* ================= 结构（环形） ================= */
.ring-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
}
.ring-cell {
  background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 12px; cursor: pointer; transition: border-color .15s;
}
.ring-cell:hover { border-color: var(--border-strong); }

/* ================= 过程 / 结果 指标格 ================= */
.proc-grid {
  display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
  gap: 10px; align-items: start;
}
.proc-tiles, .outcome-grid {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;
}
.outcome-grid { grid-template-columns: repeat(3, 1fr); margin-bottom: 10px; }
.metric-tile {
  background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; cursor: pointer; transition: border-color .15s;
  display: flex; flex-direction: column; gap: 6px; min-width: 0;
}
.metric-tile:hover { border-color: var(--border-strong); }
.mt-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.mt-name {
  font-size: 12px; color: var(--text-sub); font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.mt-value { font-size: 19px; font-weight: 700; color: var(--text-title); line-height: 1.1; }
.mt-value small { font-size: 11px; font-weight: 400; color: var(--text-sub); margin-left: 3px; }
.mt-bar {
  position: relative; height: 5px; border-radius: 3px;
  background: var(--bg-subtle); border: 1px solid var(--border); overflow: visible;
}
.mt-fill {
  position: absolute; left: 0; top: 0; bottom: 0;
  border-radius: 3px; background: var(--text-faint);
}
.mt-fill.good { background: var(--good); }
.mt-fill.warn { background: var(--warn); }
.mt-fill.danger { background: var(--danger); }
.mt-tick {
  position: absolute; top: -3px; bottom: -3px; width: 2px;
  background: var(--text-title); opacity: 0.55; border-radius: 1px;
}
.mt-foot { font-size: 10.5px; color: var(--text-faint); }

.proc-chart {
  background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px;
}
.sub-head { font-size: 11px; color: var(--text-faint); margin-bottom: 6px; }

.smr-row {
  background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px;
}

/* ================= 组合分析 ================= */
.combo-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.panel {
  background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 13px 15px; min-width: 0;
}
.panel-title {
  font-size: 13px; color: var(--text-title); font-weight: 700;
  margin-bottom: 10px; padding-left: 9px; border-left: 3px solid var(--brand);
}
.combo-panel { min-height: 0; }

.cand-strip {
  display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px;
  margin-top: 10px; padding: 9px 11px;
  background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 6px;
  font-size: 11.5px; color: var(--text-sub);
}
.cand-strip b { color: var(--text-title); font-weight: 700; }
.cand-divider { width: 1px; height: 14px; background: var(--border-strong); padding: 0; }
.diff-positive b { color: var(--good); }
.diff-negative b { color: var(--danger); }

/* ================= 页脚 ================= */
.db-footer {
  display: flex; justify-content: space-between; align-items: center;
  margin-top: 14px; padding: 11px 2px;
  border-top: 1px solid var(--border);
  font-size: 11.5px; color: var(--text-faint);
}

/* ================= 响应式 ================= */
@media (max-width: 1400px) {
  .kpi-row { grid-template-columns: repeat(3, 1fr); }
  .outcome-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 1200px) {
  .focus-banner { grid-template-columns: auto minmax(0, 1fr); }
  .fb-ai { grid-column: 1 / -1; }
  .proc-grid { grid-template-columns: 1fr; }
  .combo-grid { grid-template-columns: 1fr; }
  .ab-row { grid-template-columns: 70px 80px minmax(120px, 1fr) 76px 96px; }
  .c-delta, .c-hint { display: none; }
}
@media (max-width: 960px) {
  .kpi-row { grid-template-columns: repeat(2, 1fr); }
  .ring-grid { grid-template-columns: repeat(2, 1fr); }
  .proc-tiles, .outcome-grid { grid-template-columns: 1fr; }
  .db-topbar { flex-direction: column; align-items: flex-start; }
  .ab-row { grid-template-columns: 64px 72px minmax(100px, 1fr) 70px; }
  .c-nd { display: none; }
}
</style>
