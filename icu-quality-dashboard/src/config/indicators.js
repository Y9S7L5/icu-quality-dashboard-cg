// src/config/indicators.js
// 指标类型: ratio(百分率) / permille(千分率) / proportion(配比) / index(指数)
// thresholds: 红黄绿阈值, direction: higher_better / lower_better

export const STATUS_CONFIG_KEY = 'icu-quality-status-config';

export const DEFAULT_STATUS_META = {
  good: { label: '达标', color: '#15966b', background: 'rgba(21,150,107,0.08)' },
  warn: { label: '预警', color: '#c97a16', background: 'rgba(201,122,22,0.08)' },
  danger: { label: '异常', color: '#cf4040', background: 'rgba(207,64,64,0.08)' },
  unknown: { label: '/', color: '#94a3b8', background: 'rgba(148,163,184,0.10)' },
};

export const INDICATORS = [
  {
    code: 'ICU-00', name: 'ICU-患者动态', type: 'ratio', unit: '%',
    numerator: '期初原有患者数', denominator: '同期患者总数（原有+新入）',
    multiplier: 100, direction: 'range',
    thresholds: { good: [0, 100], warn: [0, 100] }, // 结构指标，不做达标判定
    excludeFromAlert: true,                          // 不计入大屏 danger/warn 计数
    excludeFromStatusConfig: true,                   // 不在状态配置页出现
    meaning: '反映 ICU 患者流转结构与分母构成，是各率类指标分母的拆解口径',
    chart: 'stack',
  },  {
    code: 'ICU-01', name: 'ICU床位使用率', type: 'ratio', unit: '%',
    numerator: '实际占用总床日数', denominator: '实际开放总床日数',
    multiplier: 100, direction: 'range', // 床位使用率太高太低都不好
    thresholds: { good: [75, 90], warn: [60, 95], }, // 75-90%为常用目标区间
    meaning: '反映重症床位资源使用效率', chart: 'gauge',
  },
  {
    code: 'ICU-02', name: 'ICU医师床位比', type: 'proportion', unit: ':1',
    numerator: 'ICU医师总数', denominator: '实际开放床位数',
    multiplier: 1, direction: 'higher_better',
    thresholds: { good: [0.8, 99], warn: [0.5, 99] }, // ≥0.8:1
    meaning: '反映ICU人力资源配置', chart: 'bar',
  },
  {
    code: 'ICU-03', name: 'ICU护士床位比', type: 'proportion', unit: ':1',
    numerator: 'ICU护士总数', denominator: '实际开放床位数',
    multiplier: 1, direction: 'higher_better',
    thresholds: { good: [3, 99], warn: [2.5, 99] }, // ≥3:1
    meaning: '反映ICU人力资源配置', chart: 'bar',
  },
  {
    code: 'ICU-04-01', name: 'APACHEⅡ≥15分收治率', type: 'ratio', unit: '%',
    numerator: '首次APACHEⅡ≥15分患者数', denominator: '同期收治患者总数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [50, 100], warn: [30, 100] },
    meaning: '反映收治患者病情危重程度', chart: 'gauge',
  },
  {
    code: 'ICU-04-02', name: 'APACHEⅡ＜15分收治率', type: 'ratio', unit: '%',
    numerator: '首次APACHEⅡ＜15分患者数', denominator: '同期收治患者总数',
    multiplier: 100, direction: 'lower_better',
    thresholds: { good: [0, 50], warn: [0, 70] },
    meaning: '反映收治患者中病情相对较轻的比例', chart: 'gauge',
  },
  {
    code: 'ICU-04-03', name: 'APACHEⅡ评分<10分患者总数', type: 'count', unit: '人',
    numerator: '首次APACHEⅡ<10分患者数', denominator: '同期收治患者总数',
    multiplier: 1, direction: 'range',
    thresholds: { good: [0, 999999], warn: [0, 999999] },
    excludeFromAlert: true, excludeFromStatusConfig: true,
    meaning: 'APACHEⅡ评分结构分布：<10分', chart: 'bar',
  },
  {
    code: 'ICU-04-04', name: '10≤APACHEⅡ评分<15分患者总数', type: 'count', unit: '人',
    numerator: '首次APACHEⅡ 10～14分患者数', denominator: '同期收治患者总数',
    multiplier: 1, direction: 'range',
    thresholds: { good: [0, 999999], warn: [0, 999999] },
    excludeFromAlert: true, excludeFromStatusConfig: true,
    meaning: 'APACHEⅡ评分结构分布：10≤评分<15', chart: 'bar',
  },
  {
    code: 'ICU-04-05', name: '15≤APACHEⅡ评分<20分患者总数', type: 'count', unit: '人',
    numerator: '首次APACHEⅡ 15～19分患者数', denominator: '同期收治患者总数',
    multiplier: 1, direction: 'range',
    thresholds: { good: [0, 999999], warn: [0, 999999] },
    excludeFromAlert: true, excludeFromStatusConfig: true,
    meaning: 'APACHEⅡ评分结构分布：15≤评分<20', chart: 'bar',
  },
  {
    code: 'ICU-04-06', name: '20≤APACHEⅡ评分<25分患者总数', type: 'count', unit: '人',
    numerator: '首次APACHEⅡ 20～24分患者数', denominator: '同期收治患者总数',
    multiplier: 1, direction: 'range',
    thresholds: { good: [0, 999999], warn: [0, 999999] },
    excludeFromAlert: true, excludeFromStatusConfig: true,
    meaning: 'APACHEⅡ评分结构分布：20≤评分<25', chart: 'bar',
  },
  {
    code: 'ICU-04-07', name: 'APACHEⅡ评分≥25分患者总数', type: 'count', unit: '人',
    numerator: '首次APACHEⅡ≥25分患者数', denominator: '同期收治患者总数',
    multiplier: 1, direction: 'range',
    thresholds: { good: [0, 999999], warn: [0, 999999] },
    excludeFromAlert: true, excludeFromStatusConfig: true,
    meaning: 'APACHEⅡ评分结构分布：≥25分', chart: 'bar',
  },
  {
    code: 'ICU-05-1h', name: '感染性休克1h Bundle完成率', type: 'ratio', unit: '%',
    numerator: '1h内乳酸+血培养+液体复苏均达标患者数',
    denominator: '感染+休克+乳酸检测同时满足的患者数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '1h内完成乳酸检测、血培养先于抗生素、液体复苏存在（不要求1500ml）',
    chart: 'gauge', bundleHour: '1h',
  },
  {
    code: 'ICU-05-3h', name: '感染性休克3h Bundle完成率', type: 'ratio', unit: '%',
    numerator: '3h内乳酸+血培养+液体≥1500ml均达标患者数',
    denominator: '感染+休克+乳酸检测同时满足的患者数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '3h内完成乳酸检测、血培养先于抗生素、液体复苏累计≥1500ml',
    chart: 'gauge', bundleHour: '3h',
  },
  {
    code: 'ICU-05-6h', name: '感染性休克6h Bundle完成率', type: 'ratio', unit: '%',
    numerator: '6h内乳酸+血培养+液体≥1500ml+乳酸复测均达标患者数',
    denominator: '感染+休克+乳酸检测同时满足的患者数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '6h内完成乳酸检测、血培养先于抗生素、液体≥1500ml、乳酸复测（T0+1h后）',
    chart: 'gauge', bundleHour: '6h',
  },
  {
    code: 'ICU-06', name: '抗菌药物治疗前病原学送检率', type: 'ratio', unit: '%',
    numerator: '用药前送检患者数', denominator: '治疗性用药患者总数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '反映抗菌药物使用规范性', chart: 'gauge',
  },
  {
    code: 'ICU-07', name: 'DVT预防率', type: 'ratio', unit: '%',
    numerator: '进行DVT预防患者数', denominator: '同期患者总数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '反映DVT预防情况', chart: 'gauge',
  },
  {
    code: 'ICU-08', name: '中重度ARDS俯卧位通气实施率', type: 'ratio', unit: '%',
    numerator: '实施俯卧位通气人数', denominator: '同期ARDS患者数(诊断含呼吸窘迫综合征/ARDS)',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [80, 100], warn: [60, 100] },
    meaning: '反映ARDS规范治疗', chart: 'gauge',
  },
  {
    code: 'ICU-09', name: 'ICU镇痛评估率', type: 'ratio', unit: '%',
    numerator: '镇痛评估患者数(NRS/CPOT/BPS)', denominator: '同期患者总数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '反映镇痛评估情况', chart: 'gauge',
  },
  {
    code: 'ICU-10', name: 'ICU镇静评估率', type: 'ratio', unit: '%',
    numerator: '镇静评估患者数(RASS/SAS)', denominator: '同期患者总数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '反映镇静评估情况', chart: 'gauge',
  },
  {
    code: 'ICU-11', name: 'ICU患者标化病死指数', type: 'index', unit: '',
    numerator: '实际病死率', denominator: '预计病死率',
    multiplier: 1, direction: 'lower_better', // <1说明优于预期
    customCalc: 'smr', // ★走自定义计算器
    thresholds: { good: [0, 1], warn: [0, 1.2] },
    meaning: '反映ICU整体诊疗水平', chart: 'gauge',
    formulaDetail: {
      type: 'special',
      lines: [
        '标化病死指数 = 实际病死率 ÷ 预计病死率',
        '预计病死率 = Σ每位患者首次APACHEⅡ预计病死率 ÷ 收治总数',
        '实际病死率 = 实际死亡人数 ÷ 收治总数',
      ],
    },
  },
  {
    code: 'ICU-12', name: '非计划气管插管拔管率', type: 'ratio', unit: '%',
    numerator: '非计划拔管例数', denominator: '拔管总例数',
    multiplier: 100, direction: 'lower_better',
    thresholds: { good: [0, 5], warn: [0, 10] },
    meaning: '反映ICU管理水平', chart: 'gauge',
  },
  {
    code: 'ICU-13', name: '拔管后48h再插管率', type: 'ratio', unit: '%',
    numerator: '48h内再插管例数', denominator: '拔管总例数',
    multiplier: 100, direction: 'lower_better',
    thresholds: { good: [0, 5], warn: [0, 12] },
    meaning: '反映脱机拔管指征把握能力', chart: 'gauge',
  },
  {
    code: 'ICU-14', name: '非计划转入ICU率', type: 'ratio', unit: '%',
    numerator: '非计划转入手术患者数', denominator: '转入ICU手术患者总数',
    multiplier: 100, direction: 'lower_better',
    thresholds: { good: [0, 5], warn: [0, 10] },
    meaning: '反映医疗质量重要结果指标', chart: 'gauge',
  },
  {
    code: 'ICU-15', name: '转出ICU后48h重返率', type: 'ratio', unit: '%',
    numerator: '48h内重返ICU患者数', denominator: '转出ICU患者总数',
    multiplier: 100, direction: 'lower_better',
    thresholds: { good: [0, 3], warn: [0, 6] },
    meaning: '反映转出指征把握能力', chart: 'gauge',
  },
  {
    code: 'ICU-16', name: 'VAP发病率', type: 'permille', unit: '‰',
    numerator: 'VAP新发例次', denominator: '有创呼吸机累计使用天数',
    multiplier: 1000, direction: 'lower_better',
    thresholds: { good: [0, 8], warn: [0, 14] },
    meaning: '反映感控及机械通气管理能力', chart: 'control',
  },
  {
    code: 'ICU-17', name: 'CRBSI发病率', type: 'permille', unit: '‰',
    numerator: 'CRBSI新发例次', denominator: '血管导管累计使用天数',
    multiplier: 1000, direction: 'lower_better',
    thresholds: { good: [0, 1], warn: [0, 3.5] },
    meaning: '反映感控及导管留置管理能力', chart: 'control',
  },
  {
    code: 'ICU-18', displayCode: 'ICU-19', name: '急性脑损伤意识评估率', type: 'ratio', unit: '%',
    numerator: '完成意识评估患者数(有APACHEⅡ评分即视为已评估GCS)', denominator: 'ICD编码清单判定的急性脑损伤患者总数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [90, 100], warn: [70, 100] },
    meaning: '反映脑损伤患者评估规范性', chart: 'gauge',
  },
  {
    code: 'ICU-19', displayCode: 'ICU-20', name: '48h内肠内营养启动率', type: 'ratio', unit: '%',
    numerator: '48h内启动EN患者数', denominator: '入住>48h患者总数',
    multiplier: 100, direction: 'higher_better',
    thresholds: { good: [80, 100], warn: [60, 100] },
    meaning: '反映营养治疗规范性', chart: 'gauge',
  },
  {
    code: 'CAUTI', displayCode: 'ICU-18', name: 'CAUTI尿管相关感染率', type: 'permille', unit: '‰',
    numerator: 'CAUTI新发例次', denominator: '导尿管累计使用天数',
    multiplier: 1000, direction: 'lower_better',
    thresholds: { good: [0, 2], warn: [0, 5] },
    meaning: '反映感控及导尿管留置管理能力', chart: 'control',
  },
];

export function getStatusConfig() {
  try {
    const saved = JSON.parse(localStorage.getItem(STATUS_CONFIG_KEY) || '{}');
    const thresholds = Object.fromEntries(
      INDICATORS.map(ind => [
        ind.code,
        {
          direction: saved.thresholds?.[ind.code]?.direction || ind.direction,
          thresholds: saved.thresholds?.[ind.code]?.thresholds || ind.thresholds,
        },
      ])
    );
    return {
      meta: { ...DEFAULT_STATUS_META, ...(saved.meta || {}) },
      thresholds,
    };
  } catch {
    return {
      meta: DEFAULT_STATUS_META,
      thresholds: Object.fromEntries(
        INDICATORS.map(ind => [ind.code, { direction: ind.direction, thresholds: ind.thresholds }])
      ),
    };
  }
}

export function saveStatusConfig(config) {
  localStorage.setItem(STATUS_CONFIG_KEY, JSON.stringify(config));
  window.dispatchEvent(new CustomEvent('status-config-updated'));
}

// 阈值判定 -> 返回 'good' | 'warn' | 'danger'
export function evalStatus(indicator, value, statusConfig = getStatusConfig()) {
  if (indicator.excludeFromAlert) return 'unknown';
  if (value == null || Number.isNaN(Number(value))) return 'unknown';
  const cfg = statusConfig.thresholds?.[indicator.code] || indicator;
  const { good, warn } = cfg.thresholds || indicator.thresholds;
  const inRange = (v, [a, b]) => v >= a && v <= b;
  if (inRange(value, good)) return 'good';
  if (inRange(value, warn)) return 'warn';
  return 'danger';
}

export function statusText(status, statusConfig = getStatusConfig()) {
  return statusConfig.meta?.[status]?.label || status;
}
