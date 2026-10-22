/**
 * indicatorGroups.js — 指标分组配置
 * 驱动 Dashboard 分区渲染，视图层不写死指标码。
 *
 * kind: 'structure' | 'process' | 'outcome' | 'flow' | 'compare'
 * chart: 'ring' | 'bullet' | 'hbar' | 'grouped-bar' | 'funnel' | 'radar' | 'donut' | 'stacked-bar'
 *
 * 指标码不存在时自动跳过，不报错。
 */

export const INDICATOR_GROUPS = [
  {
    id: 'structure',
    title: '结构与资源',
    kind: 'structure',
    codes: ['ICU-01', 'ICU-02', 'ICU-03', 'ICU-04', 'ICU-04F',
            'ICU-04A', 'ICU-04B', 'ICU-04C', 'ICU-04D', 'ICU-04E'],
    chart: 'ring',
    note: '反映 ICU 资源配置与收治结构',
  },
  {
    id: 'process',
    title: '过程质量',
    kind: 'process',
    codes: [
      'ICU-05-1h', 'ICU-05-3h', 'ICU-05-6h',
      'ICU-06', 'ICU-07', 'ICU-08',
      'ICU-09', 'ICU-10', 'ICU-18', 'ICU-19',
    ],
    chart: 'ring',
    note: '反映诊疗过程规范性',
  },
  {
    id: 'outcome',
    title: '结果质量',
    kind: 'outcome',
    codes: ['ICU-11', 'ICU-12', 'ICU-13', 'ICU-14', 'ICU-15'],
    chart: 'hbar',
    note: '反映诊疗结果与安全',
  },
  {
    id: 'infection',
    title: '感染防控',
    kind: 'outcome',
    codes: ['ICU-16', 'ICU-17', 'CAUTI', 'ICU-06'],
    chart: 'grouped-bar',
    unit: '‰',
    axis: 'dual',
    note: '感染三项（‰）+ 送检率（%）双轴对比',
  },
  {
    id: 'sedation',
    title: '镇痛镇静意识评估',
    kind: 'process',
    codes: ['ICU-09', 'ICU-10', 'ICU-18'],
    chart: 'radar',
    note: '评估类指标雷达图',
  },
  {
    id: 'bundle',
    title: 'Sepsis Bundle',
    kind: 'process',
    codes: ['ICU-05-1h', 'ICU-05-3h', 'ICU-05-6h'],
    chart: 'funnel',
    note: '感染性休克集束化治疗漏斗',
  },
  {
    id: 'workload',
    title: '人力与负荷',
    kind: 'structure',
    codes: ['ICU-01', 'ICU-02', 'ICU-03', 'ICU-04'],
    chart: 'grouped-bar',
    axis: 'dual',
    note: '负荷指标 vs 人力配置双轴',
  },
  {
    id: 'airway',
    title: '气道安全',
    kind: 'outcome',
    codes: ['ICU-12', 'ICU-13'],
    chart: 'grouped-bar',
    note: '非计划拔管 + 48h 再插管',
  },
  {
    id: 'flow',
    title: '患者流转',
    kind: 'flow',
    codes: ['ICU-00'],
    chart: 'stacked-bar',
    note: '原有/新入/出科堆叠柱 + 期末在科折线',
  },
  {
    id: 'compare',
    title: '环比同比总表',
    kind: 'compare',
    codes: [], // 动态取所有启用指标
    chart: 'table',
    note: '所有指标的环比同比对比表',
  },
];

/**
 * 根据 rows 过滤掉不存在的指标码
 * @param {Object} group 分组配置
 * @param {string[]} availableCodes rows 中实际存在的 code 列表
 * @returns {string[]}
 */
export function filterCodes(group, availableCodes) {
  if (!group.codes || group.codes.length === 0) {
    // compare 分组：返回所有可用码
    return availableCodes;
  }
  return group.codes.filter(c => availableCodes.includes(c));
}

/**
 * 获取分区达标统计
 * @param {Object} group
 * @param {Object} rowsByCode { code: { status, ... } }
 * @returns {{ good: number, warn: number, danger: number, total: number }}
 */
export function getGroupStats(group, rowsByCode) {
  const codes = group.codes.length > 0 ? group.codes : Object.keys(rowsByCode);
  let good = 0, warn = 0, danger = 0;
  for (const code of codes) {
    const row = rowsByCode[code];
    if (!row) continue;
    if (row.status === 'good') good++;
    else if (row.status === 'warn') warn++;
    else if (row.status === 'danger') danger++;
  }
  return { good, warn, danger, total: good + warn + danger };
}
