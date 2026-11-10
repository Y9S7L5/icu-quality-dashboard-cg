/**
 * apacheTableXlsx.js —《重症医学科APACHEII评分表》全科导出
 *
 * 版式按纸质评分表（用户截图）：标题行 / 时间范围行 / 表头
 * （类别·住院号·姓名·性别·年龄·入院/转科时间·医生·评分·诊断）/
 * 分母明细行（按入科时间升序）/ 分段汇总（APACHEII 分段例数 + 未评分例数）。
 * 不含制表人/打印时间。
 *
 * 数据源 = ICU-04-01 分母明细（与详情弹窗同一份输出，含 enrich 补的
 * gender/age/admission_type/bed_physician/diagnosis），跨月查询逐月成行
 * （与公式条分母逐月累加口径一致）。
 *
 * 评分分段（与上报表 6–10 行一致，半开区间）：
 *   <10 / 10≤·<15 / 15≤·<20 / 20≤·<25 / ≥25，无评分计「未评分」。
 *
 * 样式 xlsx 手写 OOXML（SheetJS 社区版不能写单元格样式），
 * 打包复用 reportXlsx.js 的 zipStore。
 */
import { esc, zipStore, XML_HEAD } from './reportXlsx.js';
import { fmtText, fmtDate } from './detailColumns.js';
import { fetchDetail } from '../api/index.js';

const enc = new TextEncoder();

const MAX_ROWS = 5000;
const PAGE_SIZE = 500;

/* ---------------- 列定义（与截图纸质表一致） ---------------- */

function categoryOf(p) {
  // admissionType：入院 / 转入；纸质表「转入」写作「转科」
  const t = fmtText(p.admission_type);
  return t === '转入' ? '转科' : t;
}

function genderOf(p) {
  const g = fmtText(p.gender);
  if (g === 'Male' || g === 'M') return '男';
  if (g === 'Female' || g === 'F') return '女';
  return g;
}

function ageOf(p) {
  const a = p.age;
  return (a === '' || a == null) ? '' : `${a}岁`;
}

function scoreOf(p) {
  const v = p.value;
  if (v == null || v === '' || v === '—') return null; // null → 空单元格（未评）
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

const COLS = [
  { header: '类别',         width: 8,  get: categoryOf },
  { header: '住院号',       width: 13, get: (p) => fmtText(p.patient_id) },
  { header: '姓名',         width: 10, get: (p) => fmtText(p.name) },
  { header: '性别',         width: 6,  get: genderOf },
  { header: '年龄',         width: 7,  get: ageOf },
  { header: '入院/转科时间', width: 19, get: (p) => fmtDate(p.icu_in_time || p.admit_time) },
  { header: '医生',         width: 10, get: (p) => fmtText(p.bed_physician) },
  { header: '评分',         width: 7,  get: scoreOf },
  { header: '诊断',         width: 30, get: (p) => fmtText(p.diagnosis) },
];

/* ---------------- 汇总 ---------------- */

const SUMMARY_LABELS = [
  ['APACHEII＜10分例数：', 'lt10'],
  ['10≤APACHEII＜15分例数：', 'b10'],
  ['15≤APACHEII＜20分例数：', 'b15'],
  ['20≤APACHEII＜25分例数：', 'b20'],
  ['APACHEII≥25分例数：', 'ge25'],
  ['APACHEII＜15分例数：', 'lt15'],
  ['APACHEII≥15分例数：', 'ge15'],
  ['未评分例数：', 'unscored'],
];

export function summarizeRows(rows) {
  const s = { lt10: 0, b10: 0, b15: 0, b20: 0, ge25: 0, unscored: 0 };
  for (const p of rows) {
    const n = scoreOf(p);
    if (n == null) { s.unscored++; continue; }
    if (n < 10) s.lt10++;
    else if (n < 15) s.b10++;
    else if (n < 20) s.b15++;
    else if (n < 25) s.b20++;
    else s.ge25++;
  }
  // 小计：＜15 = 前两档，≥15 = 后三档（与 ICU-04-02 分子 / ICU-04-01 分子同边界）
  s.lt15 = s.lt10 + s.b10;
  s.ge15 = s.b15 + s.b20 + s.ge25;
  return s;
}

/* ---------------- OOXML 部件 ---------------- */

const CONTENT_TYPES = `${XML_HEAD}
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>`;

const RELS_ROOT = `${XML_HEAD}
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>`;

const WORKBOOK = `${XML_HEAD}
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="APACHEII评分表" sheetId="1" r:id="rId1"/></sheets>
</workbook>`;

const WORKBOOK_RELS = `${XML_HEAD}
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>`;

// 字体 0 = 宋体 11；字体 1 = 宋体 14 加粗（标题）
// cellXfs：0 默认 / 1 细边框居中 / 2 细边框左对齐 / 3 标题（加粗居中带框）
const STYLES = `${XML_HEAD}
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="宋体"/><family val="2"/><charset val="134"/></font>
<font><b/><sz val="14"/><name val="宋体"/><family val="2"/><charset val="134"/></font>
</fonts>
<fills count="2">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border>
<left style="thin"><color auto="1"/></left>
<right style="thin"><color auto="1"/></right>
<top style="thin"><color auto="1"/></top>
<bottom style="thin"><color auto="1"/></bottom>
<diagonal/>
</border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>`;

const COL_LETTERS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I'];

function textCell(ref, s, v) {
  if (v === '' || v == null) return `<c r="${ref}" s="${s}"/>`;
  return `<c r="${ref}" s="${s}" t="inlineStr"><is><t xml:space="preserve">${esc(v)}</t></is></c>`;
}

function numCell(ref, s, v) {
  if (v == null) return `<c r="${ref}" s="${s}"/>`;
  return `<c r="${ref}" s="${s}"><v>${Number.isInteger(v) ? String(v) : String(Math.round(v * 1e10) / 1e10)}</v></c>`;
}

/** 时间范围行文本：2026年06月 / 2026年08月-2026年11月 */
export function rangeLabel(period, endPeriod = '') {
  const fmt = (p) => {
    const [y, m] = String(p).split('-');
    return `${y}年${String(Number(m)).padStart(2, '0')}月`;
  };
  return endPeriod && endPeriod !== period ? `${fmt(period)}-${fmt(endPeriod)}` : fmt(period);
}

function buildSheet(rows, period, endPeriod) {
  const summary = summarizeRows(rows);
  const xmlRows = [];
  const merges = [];
  const lastCol = COL_LETTERS[COLS.length - 1];
  let r = 0;

  // 第1行：标题（A1:I1 合并）
  r = 1;
  xmlRows.push(`<row r="1" ht="26" customHeight="1">${textCell('A1', 3, '重症医学科APACHEII评分表')}</row>`);
  merges.push(`A1:${lastCol}1`);
  // 第2行：时间范围（A2:I2 合并，左对齐）
  r = 2;
  xmlRows.push(`<row r="2">${textCell('A2', 2, `时间范围：${rangeLabel(period, endPeriod)}`)}</row>`);
  merges.push(`A2:${lastCol}2`);
  // 第3行：表头
  r = 3;
  xmlRows.push(`<row r="3">`
    + COLS.map((c, i) => textCell(`${COL_LETTERS[i]}3`, 1, c.header)).join('')
    + `</row>`);
  // 数据行：按入科时间升序（同一格式字符串，字典序 = 时间序）
  const sorted = [...rows].sort((a, b) => {
    const ta = String(a.icu_in_time || a.admit_time || '');
    const tb = String(b.icu_in_time || b.admit_time || '');
    return ta < tb ? -1 : ta > tb ? 1 : 0;
  });
  sorted.forEach((p, idx) => {
    r = 4 + idx;
    xmlRows.push(`<row r="${r}">`
      + COLS.map((c, i) => {
          const v = c.get(p);
          const ref = `${COL_LETTERS[i]}${r}`;
          return typeof v === 'number' ? numCell(ref, 1, v) : textCell(ref, 1, v);
        }).join('')
      + `</row>`);
  });
  // 汇总行：每行整行合并、左对齐（截图纸质表样式）
  SUMMARY_LABELS.forEach(([label, key], i) => {
    r = 4 + sorted.length + i;
    xmlRows.push(`<row r="${r}">${textCell(`A${r}`, 2, `${label}${summary[key]}`)}</row>`);
    merges.push(`A${r}:${lastCol}${r}`);
  });

  const lastRow = 3 + sorted.length + SUMMARY_LABELS.length;
  const colsXml = COLS.map((c, i) =>
    `<col min="${i + 1}" max="${i + 1}" width="${c.width}" customWidth="1"/>`).join('');

  return `${XML_HEAD}
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<dimension ref="A1:${lastCol}${lastRow}"/>
<sheetViews><sheetView workbookViewId="0"/></sheetViews>
<sheetFormatPr defaultRowHeight="15"/>
<cols>${colsXml}</cols>
<sheetData>${xmlRows.join('')}</sheetData>
<mergeCells count="${merges.length}">${merges.map((m) => `<mergeCell ref="${m}"/>`).join('')}</mergeCells>
</worksheet>`;
}

/** 生成《重症医学科APACHEII评分表》xlsx 字节。 */
export function buildApacheWorkbook(rows, period, endPeriod = '') {
  const files = [
    { name: '[Content_Types].xml', data: enc.encode(CONTENT_TYPES) },
    { name: '_rels/.rels', data: enc.encode(RELS_ROOT) },
    { name: 'xl/workbook.xml', data: enc.encode(WORKBOOK) },
    { name: 'xl/_rels/workbook.xml.rels', data: enc.encode(WORKBOOK_RELS) },
    { name: 'xl/styles.xml', data: enc.encode(STYLES) },
    { name: 'xl/worksheets/sheet1.xml', data: enc.encode(buildSheet(rows, period, endPeriod)) },
  ];
  return zipStore(files);
}

/* ---------------- 数据拉取 + 导出 ---------------- */

/** 全量拉取 ICU-04-01 分母明细（跨月端已按「统计月+患者」成行）。 */
async function fetchAllRows({ period, endPeriod = '', unit, onProgress }) {
  const all = [];
  let offset = 0;
  for (;;) {
    const resp = await fetchDetail('ICU-04-01', period, 'denominator', unit, endPeriod, {
      limit: PAGE_SIZE,
      offset,
    });
    const batch = resp.patients || [];
    all.push(...batch);
    if (onProgress) onProgress(all.length, resp.count);
    if (all.length >= MAX_ROWS || !resp.has_more || batch.length === 0) break;
    offset += batch.length;
  }
  return all;
}

function sanitizeFilename(s) {
  return (s || '').replace(/[\\/:*?"<>|]/g, '_').replace(/\s+/g, '_');
}

/**
 * 导出《重症医学科APACHEII评分表》xlsx 并触发下载。
 *
 * @param {Object} opts
 * @param {string} opts.period - 起始统计期 'YYYY-MM'
 * @param {string} [opts.endPeriod] - 结束统计期（跨月；缺省 = 单月）
 * @param {string} opts.unit - 科室编码
 * @param {string} [opts.unitName] - 科室显示名（文件名用）
 * @param {Function} [opts.onProgress] - 进度回调 (loaded, total)
 * @returns {Promise<{rows: number, filename: string, truncated: boolean}>}
 */
export async function exportApacheTable(opts) {
  const { period, endPeriod = '', unit, unitName = '', onProgress } = opts;
  const rows = await fetchAllRows({ period, endPeriod, unit, onProgress });
  if (rows.length === 0) {
    throw new Error('查询范围内无分母数据');
  }

  const bytes = buildApacheWorkbook(rows, period, endPeriod);
  const blob = new Blob([bytes], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
  const rangePart = endPeriod && endPeriod !== period ? `${period}_${endPeriod}` : period;
  const unitPart = unit && unit !== 'all' ? `_${unitName || unit}` : '';
  const filename = sanitizeFilename(`重症医学科APACHEII评分表_${rangePart}${unitPart}.xlsx`);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);

  return { rows: rows.length, filename, truncated: rows.length >= MAX_ROWS };
}
