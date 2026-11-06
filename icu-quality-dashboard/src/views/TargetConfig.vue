<template>
  <div class="config-page">
    <header class="config-header">
      <div class="header-left">
        <h2>目标值配置</h2>
      </div>
      <div class="actions">
        <button class="btn-ghost" @click="window.dispatchEvent(new CustomEvent('navigate-view', { detail: 'table' }))">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:14px;height:14px;margin-right:4px;vertical-align:-2px"><path d="M2 2h5l1 2h6v10H2V2zM6 8h4M6 11h2"/></svg>
          指标明细表
        </button>
        <button class="btn-primary" @click="save">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:14px;height:14px;margin-right:4px;vertical-align:-2px"><path d="M12 14H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h5l3 3v8a1 1 0 0 1-1 1zM10 2v3h3M6 9h4"/></svg>
          保存配置
        </button>
      </div>
    </header>

    <div class="info-banner">
      <span class="info-icon">ℹ</span>
      <span>为每个指标配置各年度的目标值，查询统计数据时按所查年份在指标表中展示。目标值为自由文本（如 90%、≥90%、&lt;10），不参与状态判定。</span>
    </div>

    <section class="panel">
      <div class="section-header">
        <span class="section-bar"></span>
        <h3>年度目标值</h3>
        <div class="year-picker">
          <label class="year-label">配置年度</label>
          <select v-model="year" @change="loadYear()" class="year-select">
            <option v-for="y in years" :key="y" :value="y">{{ y }} 年</option>
          </select>
        </div>
      </div>
      <table class="config-table">
        <thead>
          <tr>
            <th class="col-code">编号</th>
            <th class="col-name">指标名称</th>
            <th class="col-target">目标值</th>
            <th class="col-action">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="ind in indicators" :key="ind.code">
            <td class="cell-code">{{ displayCode(ind) }}</td>
            <td class="cell-name">{{ ind.name }}</td>
            <td class="cell-target">
              <input v-model="form[ind.code]" class="target-input" placeholder="如 90% / ≥90% / <10" />
            </td>
            <td class="cell-action">
              <button class="reset-btn" @click="form[ind.code] = ''" title="清空">×</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="table-tip">
        <span class="info-icon tip-icon">ℹ</span>
        <span>提示：目标值一年一变，按年度分别保存；留空保存即删除该指标该年度的目标值配置。</span>
      </div>
    </section>

    <transition name="fade">
      <div v-if="saved" class="toast">✓ 配置已保存</div>
    </transition>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue';
import { INDICATORS } from '../config/indicators.js';
import { fetchTargetConfig, saveTargetConfig } from '../api/index.js';

// 使用 Asia/Shanghai 当前年月作为默认值（同 IndicatorTable）
const _now = new Date();
const _shanghaiNow = new Date(_now.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }));
const _curYear = _shanghaiNow.getFullYear();

const indicators = INDICATORS;
// 动态年份：从2024到当前年+1（同 IndicatorTable）
const years = [];
for (let y = 2024; y <= _curYear + 1; y++) years.push(y);

const year = ref(_curYear);
const form = reactive({});
const saved = ref(false);
const saving = ref(false);

function displayCode(ind) { return ind.displayCode || ind.code; }

async function loadYear() {
  for (const ind of indicators) form[ind.code] = '';
  try {
    const data = await fetchTargetConfig(year.value);
    for (const t of (data.targets || [])) {
      if (t.indicator in form) form[t.indicator] = t.target_text;
    }
  } catch { /* 读取失败时保持空白 */ }
}

async function save() {
  if (saving.value) return;
  saving.value = true;
  try {
    const payload = indicators.map(ind => ({
      indicator: ind.code,
      target_text: form[ind.code] || '',
    }));
    await saveTargetConfig(year.value, payload);
    // 通知指标表刷新目标值缓存
    window.dispatchEvent(new CustomEvent('target-config-updated'));
    saved.value = true;
    setTimeout(() => { saved.value = false; }, 1600);
  } finally {
    saving.value = false;
  }
}

onMounted(loadYear);
</script>

<style scoped>
.config-page {
  padding: 24px 32px;
  max-width: 1200px;
  margin: 0 auto;
}

.config-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.header-left h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  color: var(--text-title);
}
.actions { display: flex; gap: 10px; }
.btn-ghost {
  background: #fff;
  color: #2B5EA7;
  border: 1px solid rgba(43,94,167,0.30);
  border-radius: 8px;
  padding: 8px 18px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-ghost:hover { background: rgba(43,94,167,0.06); border-color: #2B5EA7; }
.btn-primary {
  background: #2B5EA7;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 8px 20px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}
.btn-primary:hover { background: #237572; }

.info-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(43,94,167,0.06);
  border: 1px solid rgba(43,94,167,0.15);
  border-radius: 8px;
  padding: 10px 16px;
  margin-bottom: 20px;
  font-size: 13px;
  color: #4b5e6e;
}
.info-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--brand);
  color: #fff;
  font-size: var(--fs-caption);
  font-weight: 700;
  flex-shrink: 0;
}

.panel {
  background: #fff;
  border: 1px solid #e6ebf2;
  border-radius: 12px;
  padding: 20px 24px;
  margin-bottom: 20px;
  box-shadow: 0 1px 3px rgba(17,24,39,0.04);
}
.section-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
}
.section-bar {
  width: 4px;
  height: 20px;
  border-radius: 2px;
  background: #2B5EA7;
}
.section-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: var(--text-title);
}
.year-picker {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.year-label {
  font-size: 12px;
  color: #94a3b8;
}
.year-select {
  border: 1px solid #d9e2ef;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  color: #374151;
  background: #fff;
  cursor: pointer;
  transition: border-color 0.2s;
  appearance: auto;
}
.year-select:focus { outline: none; border-color: #2B5EA7; }

.config-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
.config-table th,
.config-table td {
  padding: 12px 14px;
  text-align: left;
  font-size: 13px;
  border-bottom: 1px solid #f0f2f5;
}
.config-table th {
  background: #f8fafc;
  color: #64748b;
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
}
.config-table tbody tr:hover { background: #f6faf9; }
.col-code { width: 100px; }
.col-name { width: auto; }
.col-target { width: 280px; }
.col-action { width: 80px; text-align: center; }
.cell-code {
  font-family: 'Cascadia Code', 'Consolas', monospace;
  color: #64748b;
  font-weight: 500;
}
.cell-name { color: var(--text-title); }
.target-input {
  width: 100%;
  border: 1px solid #d9e2ef;
  border-radius: 6px;
  padding: 7px 10px;
  font-size: 13px;
  color: #374151;
  background: #fff;
  transition: border-color 0.2s;
}
.target-input:focus { outline: none; border-color: #2B5EA7; }
.cell-action { text-align: center; }
.reset-btn {
  width: 30px; height: 30px;
  border-radius: 50%;
  border: 1px solid #d9e2ef;
  background: #fff;
  color: #2B5EA7;
  font-size: 16px;
  font-weight: 700;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}
.reset-btn:hover { background: rgba(43,94,167,0.08); border-color: #2B5EA7; }

.table-tip {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
  padding: 10px 0 0;
  border-top: 1px solid #f0f2f5;
  font-size: 12px;
  color: #94a3b8;
}
.tip-icon { background: #94a3b8; }

.toast {
  position: fixed;
  top: 24px;
  right: 24px;
  background: #2B5EA7;
  color: #fff;
  padding: 10px 22px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  box-shadow: 0 4px 12px rgba(43,94,167,0.30);
  z-index: 9999;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.3s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

@media (max-width: 600px) {
  .config-page { padding: 16px; }
}
</style>
