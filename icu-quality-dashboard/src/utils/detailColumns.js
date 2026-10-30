// src/utils/detailColumns.js
// ============================================================
// 指标明细「单一数据源」列定义
// DetailModal.vue（渲染表头/单元格）与 exportExcel.js（导出）共同调用，
// 保证界面展示与 Excel 导出永远一致。
//
// 每列结构：{ header: string, get: (patient) => string }
//   - header：中文表头（表格 <thead> 与 Excel 首行都用它）
//   - get   ：返回「已格式化」的显示文本（日期/布尔/空值均已处理）
// ============================================================

/* ---------------- 统一格式化 ---------------- */

export function fmtDate(v) {
  if (v === null || v === undefined || v === '') return ''
  // 后端已格式化的 "YYYY-MM-DD HH:mm" 字符串直接返回，避免 new Date() 的浏览器差异与时区偏移
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(v)) return v.slice(0, 16)
  const d = v instanceof Date ? v : new Date(v)
  if (isNaN(d.getTime())) return String(v) // 非日期：原样返回，不猜
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function fmtBool(v) {
  if (v === true) return '是'
  if (v === false) return '否'
  return v == null ? '' : String(v)
}

export function fmtText(v) {
  return v == null ? '' : String(v)
}

/* ---------------- 指标分组（与 DetailModal.vue 模板分支一一对应）---------------- */

const AIRWAY_CODES = ['ICU-12', 'ICU-13']
const TRITUBE_CODES = ['ICU-16', 'ICU-17', 'CAUTI']
const ASSESSMENT_CODES = ['ICU-09', 'ICU-10']
const BUNDLE_CODES = ['ICU-05-1h', 'ICU-05-3h', 'ICU-05-6h']

/* ---------------- ICU-05 Bundle 原因码映射 ---------------- */

export const REASON_MAP = {
  'NO_T0': '找不到T0锚点',
  'NOT_SEPTIC_SHOCK': '未通过休克确认(乳酸/升压药)',
  'NO_ORGAN_DYSFUNCTION': '无器官障碍证据',
  'NO_INFECTION': '无感染证据',
  'NO_INFECTION_EVIDENCE': '无感染证据',
  'A1_NOT_MET': '乳酸未测量',
  'B3_NOT_MET': '血培养/抗生素不达标',
  'AB_MISSING': '无抗生素记录',
  'AB_LATE': '抗生素超出时间窗',
  'BC_MISSING': '无血培养记录',
  'BC_LATE': '血培养超出时间窗',
  'BC_AFTER_AB': '先用抗生素后采培养(顺序错)',
  'C3_FLUID_INSUFFICIENT': '液体量不足',
  'FLUID_NONE': '无液体执行',
  'FLUID_INSUFFICIENT': '液体量不足1500ml',
  'MAP_NOT_MET': 'MAP<70(需液体复苏)',
  'MAP_NOT_TRIGGERED': 'MAP未触发(<70mmHg)',
  'LACTATE_NOT_TRIGGERED': '乳酸未触发(<4mmol/L)',
  'LACTATE_RECHECK_MISSING': '未复测乳酸',
  'FINISH_FALSE': 'Bundle未完成',
  'DATA_MISSING_LAC': '乳酸数据缺失',
  'DATA_MISSING_MAP': 'MAP数据缺失',
  'DATA_MISSING_FLUID': '液体数据缺失',
  'SITE_UNCONFIRMED': '感染部位未确认',
  'MANUAL_EXCLUDED': '人工排除',
  /* ICU-05 cg 简化口径（四项证据）原因码 */
  'BLOOD_GAS_MISSING': '血气(ICU血气医嘱)未做',
  'SPUTUM_CULTURE_MISSING': '痰培养(细菌培养)未做',
  'BLOOD_CULTURE_MISSING': '血培养(血培养鉴定)未做',
  'ANTIBIOTIC_MISSING': '抗生素未执行',
  'RECHECK_BGA_MISSING': '6h内血气复查<2次',
}

/* gate 级原因（未进判定，非数据缺失） —— 与 finish=None 的"数据缺失"区分开 */
export const GATE_REASONS = new Set([
  'NOT_SEPTIC_SHOCK', 'NO_ORGAN_DYSFUNCTION',
  'NO_INFECTION', 'NO_INFECTION_EVIDENCE', 'NO_T0',
])

/* 结论三色语义: 红=明确未达标 / 橙=顺序问题 / 灰=数据缺失 / 蓝灰=未确认 */
export function verdictOf(p) {
  const v3 = p.v3 || {}
  if (p.excluded) return { text: '已排除', cls: 'v-excluded' }
  const codes = (v3.reason_codes && v3.reason_codes.length)
    ? v3.reason_codes
    : (v3.reason ? [v3.reason] : [])
  const labels = codes.map(c => REASON_MAP[c] || c)
  if (v3.finish === true) return { text: '达标', cls: 'v-ok' }
  if (v3.finish === false) {
    const cls = codes[0] === 'BC_AFTER_AB' ? 'v-order' : 'v-fail'
    return { text: labels.join(' · ') || '未达标', cls }
  }
  if (codes.some(c => GATE_REASONS.has(c))) {
    return { text: labels.join(' · ') || '未通过门控', cls: 'v-gate' }
  }
  if (codes.length) return { text: labels.join(' · '), cls: 'v-missing' }
  if (v3.t0 || v3.finish === null) return { text: '无法判定(数据缺失)', cls: 'v-missing' }
  return { text: '—', cls: '' }
}

function fmtReason(code) {
  return REASON_MAP[code] || code || ''
}

/* ---------------- 通用列片段 ---------------- */

const COL_PATIENT_ID = { header: '住院号', get: (p) => fmtText(p.patient_id) }
const COL_NAME       = { header: '姓名',   get: (p) => fmtText(p.name) }

/* 出科三件套（后端 _enrich_admission_discharge 统一补数，所有指标明细共用）：
   出科类型=patient.dischargedType；出科时间优先 icu_out_time（部分指标
   discharge_time 槽位被复用为管道/评估时间等语义，不可直接读）；
   主治医生=patient.bedPhysician。 */
const COL_DISCHARGE_TYPE = { header: '出科类型', get: (p) => fmtText(p.discharge_type) || '/' }
const COL_DISCHARGE_TIME = { header: '出科时间', get: (p) => fmtDate(p.icu_out_time) || '/' }
const COL_PHYSICIAN      = { header: '主治医生', get: (p) => fmtText(p.bed_physician) || '/' }

/* ============================================================
 * default 分支 col3/col4/col5 中文表头 —— 按 code + part 决定
 * 来源：DetailModal.vue L227-262 col3/col4/col5 computed
 * ============================================================ */

function isStaffCode(code) { return code === 'ICU-02' || code === 'ICU-03' }

function resolveCol3Header(code, part) {
  if (isStaffCode(code)) return '职称'
  if (code === 'ICU-08' && part === 'denominator') return 'P/F  PEEP'
  if (code === 'ICU-08' && part === 'numerator') return '俯卧次数'
  if (code === 'ICU-06' && part === 'denominator') return '抗菌药 [目的]'
  if (code === 'ICU-06' && part === 'numerator') return '送检项目'
  if (code === 'ICU-07' && part === 'numerator') return '预防措施'
  if (code === 'ICU-11') return '转归'
  if (code === 'ICU-13' && part === 'numerator') return '拔管→再置管'
  if (code === 'ICU-12' || code === 'ICU-13') return '管道类型'
  return '床号'
}

function resolveCol4Header(code, part) {
  if (isStaffCode(code)) return '入职日期'
  if (code === 'ICU-08' && part === 'denominator') return '纳入路径'
  if (code === 'ICU-08' && part === 'numerator') return '首次俯卧时间'
  if (code === 'ICU-06' && part === 'denominator') return '给药 & 判定理由'
  if (code === 'ICU-06' && part === 'numerator') return '送检时间'
  if (code === 'ICU-07' && part === 'numerator') return '医嘱示例'
  if (code === 'ICU-11') return '首次APACHEⅡ时间'
  if (code === 'ICU-13' && part === 'numerator') return '拔管/再置管时间'
  if (code === 'ICU-12' || code === 'ICU-13') return '拔管时间'
  return '入科时间'
}

function resolveCol5Header(code, part) {
  if (isStaffCode(code)) return '人数'
  if (code === 'ICU-08' && part === 'denominator') return 'P/F值'
  if (code === 'ICU-08' && part === 'numerator') return '俯卧总次数'
  if (code === 'ICU-04-01' && part === 'numerator') return 'APACHEⅡ'
  if (code === 'ICU-04-01' && part === 'denominator') return '首次APACHEⅡ'
  if (part === 'denominator' && /^ICU-04-0[2-7]$/.test(code)) return '首次APACHEⅡ'
  if (code === 'ICU-01' && part === 'numerator') return '在床天数'
  if (code === 'ICU-06' && part === 'denominator') return '给药次数'
  if (code === 'ICU-07' && part === 'numerator') return '医嘱条数'
  if (code === 'ICU-11') return '预计死亡率'
  if (code === 'ICU-12' || code === 'ICU-13') return '例次'
  return '数值'
}

// 分母 = 同期ICU患者总数（人次口径）的指标：明细需区分 原有/新入/出科/未评，
// 与 DetailModal.vue 的 TYPE_FILTER_CODES 一一对应
const TYPE_TAG_CODES = [
  'ICU-04-01', 'ICU-04-02', 'ICU-04-03', 'ICU-04-04', 'ICU-04-05',
  'ICU-04-06', 'ICU-04-07', 'ICU-07', 'ICU-09', 'ICU-10',
]

// 分母状态列表头：评估/评分类 → 评估状态(未评/已评)；DVT → 预防状态(未预防/已预防)
function evalStatusHeader(code) {
  return code === 'ICU-07' ? '预防状态' : '评估状态'
}

function resolveDefaultCols(code, part) {
  // ICU-02/03 明细行是医护人员（account 表），只留 职称；入职日期/入科类型/人数不展示
  if (isStaffCode(code)) {
    return [{ header: '职称', get: (p) => fmtText(p.bed_no) }]
  }
  // 患者类指标明细统一出科三件套：出科类型/出科时间/主治医生
  // 出科时间优先 icu_out_time（后端 enrich 从 patient.icuDischargeTime 补），
  // 在科患者为空时回退 discharge_time（ICU-01 分子显示「在科」）
  const base = [
    { header: resolveCol3Header(code, part), get: (p) => fmtText(p.bed_no) },
    { header: resolveCol4Header(code, part), get: (p) => fmtDate(p.admit_time) },
    { header: '入科类型', get: (p) => fmtText(p.admission_type) },
    { header: '出科类型', get: (p) => fmtText(p.discharge_type) },
    { header: '出科时间', get: (p) => fmtDate(p.icu_out_time || p.discharge_time) || '/' },
  ]
  if (TYPE_TAG_CODES.includes(code)) {
    // 患者类型(原有/新入)；分母再加 未评/已评（07 为 未预防/已预防）
    base.push({ header: '患者类型', get: (p) => fmtText(p.patient_type || '/') })
  }
  base.push({ header: '主治医生', get: (p) => fmtText(p.bed_physician) || '/' })
  base.push({ header: resolveCol5Header(code, part), get: (p) => fmtText(p.value ?? '—') })
  if (TYPE_TAG_CODES.includes(code) && part === 'denominator') {
    base.push({ header: evalStatusHeader(code), get: (p) => fmtText(p.eval_status || '/') })
  }
  return base
}

/* ============================================================
 * ICU-12/13 气道动态列
 * 来源：DetailModal.vue L182-193 airwayExtraCol / airwayExtraValue
 * ============================================================ */

function airwayExtraHeader(code, part) {
  if (code === 'ICU-13' && part === 'numerator') return '再置管'
  if (code === 'ICU-12' && part === 'numerator') return '非计划'
  return '说明'
}

function airwayExtraGet(code, part) {
  return (p) => {
    if (code === 'ICU-13' && part === 'numerator') {
      return p.reinsert_start ? `${p.reinsert_type} ${p.reinsert_start}` : ''
    }
    if (code === 'ICU-12' && part === 'numerator') return p.unplanned ? '是' : '否'
    return ''
  }
}

/* ============================================================
 * ICU-19 动态列
 * 来源：DetailModal.vue L219-225 icu19TimeCol/icu19SourceCol/icu19EvidenceCol/icu19Evidence
 * ============================================================ */

function resolveIcu19Cols(part) {
  const timeHeader = part === 'numerator' ? 'EN启动时间' : 'EN启动情况'
  const sourceHeader = part === 'numerator' ? '启动来源' : '判定来源'
  const evidenceHeader = part === 'numerator' ? '命中证据' : '命中/禁忌证'

  return {
    timeHeader,
    timeGet: (p) =>
      part === 'numerator'
        ? fmtText(p.enStartTime || p.discharge_time || '/')
        : fmtText(p.windowResult || p.admission_source || '/'),
    sourceHeader,
    sourceGet: (p) => fmtText(p.enSource || p.bed_no || '/'),
    evidenceHeader,
    evidenceGet: (p) => {
      const hit = p.enHit || p.enRoute || p.enDrug || '/'
      return p.contraindication ? `${hit}；禁忌证：${p.contraindication}` : hit
    },
  }
}

/* ============================================================
 * ICU-16/17/CAUTI 三管卡片→列
 * 来源：DetailModal.vue L194-218 triTubeCol3/4/5 + triTubeValue3/4/5 + triTubeTimeText
 * ============================================================ */

function resolveTriTubeCols(code, part) {
  const col3Header = part === 'numerator' ? '感染类型' : '设备/导管'
  const col4Header = part === 'numerator' ? '诊断时间' : '天数'
  let col5Header = '备注'
  if (part === 'denominator') {
    col5Header = code === 'ICU-16' ? '通气方式 / 记录点数' : '管道类型 / 置管点数'
  }

  return {
    col3Header,
    col3Get: (p) =>
      part === 'numerator'
        ? fmtText(p.diseaseType || p.bed_no || '/')
        : fmtText(p.device_type || p.bed_no || '/'),
    col4Header,
    col4Get: (p) =>
      part === 'numerator'
        ? fmtText(p.diagnosisTime || p.admit_time || '/')
        : (p.device_days != null ? `${p.device_days}天` : '/'),
    col5Header,
    col5Get: (p) => {
      if (part === 'numerator') return fmtText(p.notes || p.dept || '/')
      return `${fmtText(p.tube_type || p.device_value || p.dept || '/')} / ${p.tube_points || 0}`
    },
    timeGet: (p) => {
      if (part === 'numerator') return fmtText(p.diagnosisTime || p.admit_time || '')
      const parts = []
      if (p.device_day) parts.push(`日期：${p.device_day}`)
      if (p.tube_start) parts.push(`开始：${p.tube_start}`)
      if (p.tube_end) parts.push(`结束：${p.tube_end}`)
      return parts.join('  ')
    },
  }
}

/* ============================================================
 * ICU-05 Bundle 列定义
 * ============================================================ */

/* 候选状态中文映射 */
const CANDIDATE_STATUS_MAP = {
  'high_probability': '高概率',
  'probable': '很可能',
  'pending_review': '待复核',
  'not_candidate': '非候选',
}
const CONFIRMATION_STATUS_MAP = {
  'confirmed': '已确诊',
  'pending_review': '待复核',
  'insufficient': '证据不足',
}

function resolveIcu05Cols(part, forExport = false) {
  const full = [
    COL_PATIENT_ID,
    COL_NAME,
    { header: '候选状态', get: (p) => {
      const status = p.candidate_status || 'not_candidate'
      const label = CANDIDATE_STATUS_MAP[status] || status
      // 候选路径: 从 candidate_pathways 列表中取第一个
      const pathways = p.candidate_pathways || p.candidate_info?.candidate_pathways || []
      const pathway = pathways[0] || p.candidate_info?.pathway
      const pathwayLabel = pathway ? ` (${pathway})` : ''
      return `${label}${pathwayLabel}`
    }},
    { header: '临床确认', get: (p) => {
      const status = p.clinical_confirmation_status || 'insufficient'
      return CONFIRMATION_STATUS_MAP[status] || status
    }},
    { header: '入科诊断', get: (p) => {
      const diag = p.diagnose || ''
      return diag.length > 30 ? diag.slice(0, 30) + '...' : diag
    }},
    { header: 'T0时间', get: (p) => fmtDate(p.t0 || p.admit_time) },
    { header: '入科类型', get: (p) => fmtText(p.admission_type || '—') },
    COL_DISCHARGE_TYPE,
    COL_DISCHARGE_TIME,
    COL_PHYSICIAN,
    { header: '乳酸(mmol/L)', get: (p) => {
      const v3 = p.v3 || {}
      if (v3.lactate == null) return '—'
      const time = v3.lactate_time ? ` @${v3.lactate_time.slice(5)}` : ''
      return `${Number(v3.lactate).toFixed(2)}${time}`
    }},
    { header: 'MAP(mmHg)', get: (p) => {
      const v3 = p.v3 || {}
      if (v3.map == null) return '—'
      const time = v3.map_time ? ` @${v3.map_time.slice(5)}` : ''
      return `${v3.map}${time}`
    }},
    { header: '血培养', get: (p) => {
      const v3 = p.v3 || {}
      if (!v3.culture_time) return '—'
      return v3.culture_time.slice(5) // MM-DD HH:mm
    }},
    { header: '抗生素', get: (p) => {
      const v3 = p.v3 || {}
      if (!v3.antibiotic_time) return '—'
      const name = v3.antibiotic_name || ''
      const time = v3.antibiotic_time.slice(5)
      return name ? `${name} ${time}` : time
    }},
    { header: '液体量(ml)', get: (p) => {
      const v3 = p.v3 || {}
      const ml = v3.fluid_ml || 0
      return ml > 0 ? `${ml}` : '—'
    }},
    ...(part === 'numerator' ? [
      { header: '完成时间', get: (p) => {
        const v3 = p.v3 || {}
        return v3.finish ? fmtDate(p.admit_time || p.t0) : '—'
      }},
    ] : [
      { header: 'Bundle', get: (p) => {
        const v3 = p.v3 || {}
        if (v3.finish === true) return '✅完成'
        if (v3.finish === false) return '❌未完成'
        return '—'
      }},
      { header: '状态', get: (p) => {
        const v3 = p.v3 || {}
        if (v3.finish === true) return '达标'
        return fmtReason(v3.reason) || '—'
      }},
    ]),
  ]
  if (forExport) return full
  // 界面精简列（#设计 P2: 结论列保留；出科三件套按需求全指标补齐）
  return [
    COL_PATIENT_ID,
    COL_NAME,
    { header: 'T0', get: (p) => fmtDate(p.t0 || p.admit_time) },
    { header: '乳酸', get: (p) => {
      const v = (p.v3 || {}).lactate
      return v == null ? '—' : Number(v).toFixed(1)
    }},
    { header: '升压药', get: (p) => ((p.v3 || {}).vaso_name ? '有' : '无') },
    COL_DISCHARGE_TYPE,
    COL_DISCHARGE_TIME,
    COL_PHYSICIAN,
    { header: '结论', get: (p) => verdictOf(p).text, cls: (p) => verdictOf(p).cls },
  ]
}

/* ============================================================
 * 主入口：getDetailColumns(code, part, opts)
 * part: 'numerator' | 'denominator'
 * opts.forExport: true 时返回全列（Excel 导出用），默认精简展示列
 * 返回：[{ header, get, cls? }]
 * ============================================================ */

export function getDetailColumns(code, part, opts = {}) {
  const cols = buildColumns(code, part, opts);
  // 跨月明细按「统计月+患者」成行：首列标注归属月份，便于逐行核对公式条
  if (opts.crossMonth) {
    return [
      { header: '统计月', get: (p) => fmtText(p.stat_month || '—') },
      ...cols,
    ];
  }
  return cols;
}

function buildColumns(code, part, opts = {}) {
  // ---- ICU-05 Bundle ----
  if (BUNDLE_CODES.includes(code)) {
    return resolveIcu05Cols(part, !!opts.forExport)
  }
  // ---- ICU-15：转出ICU后48h重返率 ----
  if (code === 'ICU-15') {
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: '出科时间',     get: (p) => fmtText(p.icuDischargeTime || p.admit_time || '/') },
      { header: '重返入科时间', get: (p) => fmtText(p.reIcuAdmissionTime || p.discharge_time || '/') },
      COL_DISCHARGE_TYPE,
      COL_PHYSICIAN,
      { header: '来源',         get: (p) => fmtText(p.event_source || p.admission_source || '/') },
      { header: '依据',         get: (p) => fmtText(p.basis || '/') },
    ]
  }

  // ---- ICU-18：急性脑损伤意识评估率 ----
  if (code === 'ICU-18') {
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: '脑损伤类别', get: (p) => fmtText(p.brain_category || p.bed_no || '/') },
      { header: '入分母来源', get: (p) => fmtText(p.den_source || p.dept || '/') },
      { header: '命中依据',   get: (p) => fmtText(p.evidence || p.basis || '/') },
      { header: '是否评估',   get: (p) => fmtText(p.assessed || '/') },
      { header: '首次评估',   get: (p) => {
        const t = fmtText(p.firstAssessTime || p.discharge_time || '/')
        const s = fmtText(p.assessSource || '')
        return s ? `${t} ${s}` : t
      }},
      COL_DISCHARGE_TYPE,
      COL_DISCHARGE_TIME,
      COL_PHYSICIAN,
    ]
  }

  // ---- ICU-14：非计划转入ICU ----
  if (code === 'ICU-14') {
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: '转入类型',    get: (p) => fmtText(p.admissionType || p.bed_no || '/') },
      { header: '转入计划',    get: (p) => fmtText(p.admissionPlan || p.admission_source || '/') },
      { header: '手术名称',    get: (p) => fmtText(p.operation_name || p.dept || '/') },
      { header: '转入ICU时间', get: (p) => fmtText(p.icuAdmissionTime || p.admit_time) },
      COL_DISCHARGE_TYPE,
      COL_DISCHARGE_TIME,
      COL_PHYSICIAN,
      { header: '依据',        get: (p) => fmtText(p.basis || '/') },
    ]
  }

  // ---- ICU-12/13：气道/管道 ----
  if (AIRWAY_CODES.includes(code)) {
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: '管道类型', get: (p) => fmtText(p.tube_type) },
      { header: '插管时间', get: (p) => fmtDate(p.tube_start) },
      { header: '拔管时间', get: (p) => fmtDate(p.tube_end) },
      { header: airwayExtraHeader(code, part), get: airwayExtraGet(code, part) },
      { header: '例次',     get: () => '1' },
      COL_DISCHARGE_TYPE,
      COL_DISCHARGE_TIME,
      COL_PHYSICIAN,
    ]
  }

  // ---- ICU-19：48h 内 EN 启动率（动态列，按 part）----
  if (code === 'ICU-19') {
    const d = resolveIcu19Cols(part)
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: '入科时间',       get: (p) => fmtText(p.icuAdmissionTime || p.admit_time || '/') },
      { header: d.timeHeader,     get: d.timeGet },
      { header: d.sourceHeader,   get: d.sourceGet },
      { header: d.evidenceHeader, get: d.evidenceGet },
      COL_DISCHARGE_TYPE,
      COL_DISCHARGE_TIME,
      COL_PHYSICIAN,
      { header: '判定说明',       get: (p) => fmtText(p.basis || '/') },
    ]
  }

  // ---- ICU-16/17/CAUTI：三管（卡片→列）----
  if (TRITUBE_CODES.includes(code)) {
    const t = resolveTriTubeCols(code, part)
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: t.col3Header, get: t.col3Get },
      { header: t.col4Header, get: t.col4Get },
      { header: t.col5Header, get: t.col5Get },
      { header: '时间',       get: t.timeGet },
      COL_DISCHARGE_TYPE,
      COL_DISCHARGE_TIME,
      COL_PHYSICIAN,
      { header: '依据',       get: (p) => fmtText(p.basis || p.admission_source || '/') },
    ]
  }

  // ---- ICU-09/10 分子：评估类 ----
  if (ASSESSMENT_CODES.includes(code) && part === 'numerator') {
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: '评估来源',   get: (p) => fmtText(p.bed_no) },
      { header: '评分中文名', get: (p) => fmtText(p.dept) },
      { header: '分值',       get: (p) => fmtText(p.value ?? '—') },
      { header: '评估时间',   get: (p) => fmtDate(p.admit_time) },
      { header: '出科时间',   get: (p) => fmtDate(p.icu_out_time || p.discharge_time) || '/' },
      { header: '患者类型',   get: (p) => fmtText(p.patient_type || '/') },
      COL_DISCHARGE_TYPE,
      COL_PHYSICIAN,
    ]
  }

  // ---- ICU-00：患者动态（原有/新入/出科） ----
  if (code === 'ICU-00') {
    return [
      COL_PATIENT_ID,
      COL_NAME,
      { header: '患者类型', get: (p) => fmtText(p.patient_type) },
      { header: '床号',     get: (p) => fmtText(p.bed_no) },
      { header: '入科时间', get: (p) => fmtDate(p.admit_time) },
      { header: '出科时间', get: (p) => fmtDate(p.icu_out_time || p.discharge_time) },
      { header: '入科类型', get: (p) => fmtText(p.admission_type) },
      { header: '出科类型', get: (p) => fmtText(p.discharge_type) },
      COL_PHYSICIAN,
      { header: '住院天数', get: (p) => fmtText(p.los_days != null ? p.los_days + '天' : '/') },
    ]
  }

  // ---- default（ICU-01~08、11 及 09/10 分母等）----
  const dyn = resolveDefaultCols(code, part)
  return [
    COL_PATIENT_ID,
    COL_NAME,
    ...dyn,
  ]
}
