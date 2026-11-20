// src/core/calculator.js
import { INDICATORS, evalStatus } from '../config/indicators.js';

// 通用计算：分子/分母 × 系数
export function calcGeneric(indicator, numerator, denominator) {
  if (!denominator) return null;
  const value = (numerator / denominator) * indicator.multiplier;
  return round(value, indicator.type === 'index' ? 2 : 1);
}

// ★ICU-11 标化病死指数 SMR —— 前端备用计算器（**当前未接线**）
// 线上 ICU-11 数值一律由后端 db.get_icu11_data 计算：预计死亡率直接读评分表
// 已存的 apacheII.calDead.score，不走下面这条回归公式；且分子只认转归=死亡、
// 两率同除「同期ICU患者总数」（统计期内在科记录，人次口径）。
// 下面保留仅作口径参考：ln(R/(1-R)) = -3.517 + APACHEⅡ×0.146 + 主要疾病得分 + 0.603(仅急诊术后)
export function calcDeathRisk(apache2, diseaseScore, isEmergencyPostOp) {
  const logit = -3.517 + apache2 * 0.146 + diseaseScore +
                (isEmergencyPostOp ? 0.603 : 0);
  return 1 / (1 + Math.exp(-logit)); // 单个患者死亡危险性 R
}

export function calcSMR(patients) {
  // patients: [{ apache2, diseaseScore, isEmergencyPostOp, isDead }]
  const total = patients.length;
  if (!total) return null;
  const expectedDeaths = patients.reduce((sum, p) =>
    sum + calcDeathRisk(p.apache2, p.diseaseScore, p.isEmergencyPostOp), 0);
  const actualDeaths = patients.filter(p => p.isDead).length;
  const expectedRate = expectedDeaths / total;
  const actualRate = actualDeaths / total;
  return round(actualRate / expectedRate, 2); // SMR
}

// 统一入口
export function compute(code, rawData) {
  const ind = INDICATORS.find(i => i.code === code);
  if (!ind) throw new Error(`未知指标: ${code}`);

  let value;
  if (ind.customCalc === 'smr') {
    value = calcSMR(rawData.patients);
  } else if (ind.subMetrics) {
    // ICU-05 分时段
    value = {};
    ind.subMetrics.forEach(t => {
      value[t] = calcGeneric(ind, rawData[t].num, rawData[t].den);
    });
    return { code, name: ind.name, value, unit: ind.unit, subMetrics: true };
  } else {
    value = calcGeneric(ind, rawData.numerator, rawData.denominator);
  }

  return {
    code, name: ind.name, value, unit: ind.unit,
    status: value != null ? evalStatus(ind, value) : 'unknown',
    meaning: ind.meaning, chart: ind.chart,
  };
}

function round(n, d) { return Math.round(n * 10 ** d) / 10 ** d; }
