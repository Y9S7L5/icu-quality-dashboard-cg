/**
 * reportXlsx.js — 生成《重症质控指标上报（2024年版）》两列模板 xlsx
 *
 * SheetJS 社区版无法写入单元格样式（居中对齐/字体），因此手写 OOXML：
 * styles.xml 还原 Calibri 居中样式，sheet1.xml 还原列宽 58.64/6.54、
 * 行高 25、A1:B1 合并，并以 STORE（不压缩）方式打包 ZIP，零第三方依赖。
 */
import { REPORT_TITLE, REPORT_VALUE_HEADER, REPORT_ROWS } from './reportTemplate.js';

const XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
const enc = new TextEncoder();

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ---- CRC32（ZIP 校验） ----
let CRC_TABLE = null;
function crcTable() {
  if (CRC_TABLE) return CRC_TABLE;
  CRC_TABLE = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    CRC_TABLE[n] = c >>> 0;
  }
  return CRC_TABLE;
}
function crc32(u8) {
  const t = crcTable();
  let c = 0xFFFFFFFF;
  for (let i = 0; i < u8.length; i++) c = t[(c ^ u8[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// ---- ZIP（STORE 方式，条目不压缩） ----
function dosDateTime(d) {
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
  const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  return { time, date };
}

function zipStore(files) {
  const now = new Date();
  const { time, date } = dosDateTime(now);
  const local = [];
  const central = [];
  let offset = 0;
  for (const f of files) {
    const nameBytes = enc.encode(f.name);
    const crc = crc32(f.data);
    const lh = new Uint8Array(30 + nameBytes.length);
    const dv = new DataView(lh.buffer);
    dv.setUint32(0, 0x04034b50, true); // local file header
    dv.setUint16(4, 20, true);         // version needed
    dv.setUint16(6, 0, true);          // flags（文件名纯 ASCII）
    dv.setUint16(8, 0, true);          // method = store
    dv.setUint16(10, time, true);
    dv.setUint16(12, date, true);
    dv.setUint32(14, crc, true);
    dv.setUint32(18, f.data.length, true);
    dv.setUint32(22, f.data.length, true);
    dv.setUint16(26, nameBytes.length, true);
    dv.setUint16(28, 0, true);         // extra len
    lh.set(nameBytes, 30);
    local.push(lh, f.data);

    const ch = new Uint8Array(46 + nameBytes.length);
    const cv = new DataView(ch.buffer);
    cv.setUint32(0, 0x02014b50, true); // central directory header
    cv.setUint16(4, 20, true);         // version made by
    cv.setUint16(6, 20, true);         // version needed
    cv.setUint16(8, 0, true);          // flags
    cv.setUint16(10, 0, true);         // method
    cv.setUint16(12, time, true);
    cv.setUint16(14, date, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, f.data.length, true);
    cv.setUint32(24, f.data.length, true);
    cv.setUint16(28, nameBytes.length, true);
    cv.setUint16(30, 0, true);         // extra
    cv.setUint16(32, 0, true);         // comment
    cv.setUint16(34, 0, true);         // disk number
    cv.setUint16(36, 0, true);         // internal attrs
    cv.setUint32(38, 0, true);         // external attrs
    cv.setUint32(42, offset, true);    // local header offset
    ch.set(nameBytes, 46);
    central.push(ch);
    offset += lh.length + f.data.length;
  }
  const centralSize = central.reduce((s, c) => s + c.length, 0);
  const eocd = new Uint8Array(22);
  const ev = new DataView(eocd.buffer);
  ev.setUint32(0, 0x06054b50, true);   // EOCD
  ev.setUint16(4, 0, true);
  ev.setUint16(6, 0, true);
  ev.setUint16(8, files.length, true);
  ev.setUint16(10, files.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, offset, true);      // central dir 起始偏移 = 本地条目总长
  ev.setUint16(20, 0, true);
  const parts = [...local, ...central, eocd];
  const total = parts.reduce((s, p) => s + p.length, 0);
  const out = new Uint8Array(total);
  let p = 0;
  for (const part of parts) { out.set(part, p); p += part.length; }
  return out;
}

// ---- OOXML 部件 ----
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
<sheets><sheet name="sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>`;

const WORKBOOK_RELS = `${XML_HEAD}
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>`;

// 字体 0 = Calibri 11，字体 1 = Calibri 12（A1 标题行，同模板）
// cellXfs：0 默认 / 1 指标名列（居中，文本格式）/ 2 表头（居中+自动换行）
//          3 A1（Calibri 12 居中）/ 4 上报值列（居中，常规数值）
const STYLES = `${XML_HEAD}
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>
<font><sz val="12"/><name val="Calibri"/><family val="2"/></font>
</fonts>
<fills count="2">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>`;

function cellNum(v) {
  // 数值序列化：整数原样，小数去掉浮点尾差
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 1e10) / 1e10);
}

function buildSheet(values) {
  const rows = [];
  // 第1行：空标题行，A1:B1 合并（同模板）
  rows.push('<row r="1" ht="25" customHeight="1">'
    + '<c r="A1" s="3" t="inlineStr"><is><t></t></is></c><c r="B1" s="3"/></row>');
  // 第2行：模板标题 + 上报值
  rows.push(`<row r="2" ht="25" customHeight="1">`
    + `<c r="A2" s="2" t="inlineStr"><is><t>${esc(REPORT_TITLE)}</t></is></c>`
    + `<c r="B2" s="2" t="inlineStr"><is><t>${esc(REPORT_VALUE_HEADER)}</t></is></c></row>`);
  // 第3..55行：指标名称 + 上报值（null 留空由人工填报）
  REPORT_ROWS.forEach((row, i) => {
    const r = i + 3;
    const v = values ? values[row.key] : null;
    const b = (typeof v === 'number' && Number.isFinite(v))
      ? `<c r="B${r}" s="4"><v>${cellNum(v)}</v></c>`
      : `<c r="B${r}" s="4"/>`;
    rows.push(`<row r="${r}" ht="25" customHeight="1">`
      + `<c r="A${r}" s="1" t="inlineStr"><is><t>${esc(row.label)}</t></is></c>${b}</row>`);
  });
  const lastRow = REPORT_ROWS.length + 2; // 动态行数（当前 56 项 → 第58行）
  return `${XML_HEAD}
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<dimension ref="A1:B${lastRow}"/>
<sheetViews><sheetView workbookViewId="0"/></sheetViews>
<sheetFormatPr defaultRowHeight="15"/>
<cols>
<col min="1" max="1" width="58.640625" customWidth="1"/>
<col min="2" max="2" width="6.54296875" customWidth="1"/>
</cols>
<sheetData>${rows.join('')}</sheetData>
<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>
</worksheet>`;
}

/** 生成 xlsx 字节（无样式依赖，可直接落盘/打包 Blob）。 */
export function buildReportWorkbook(values) {
  const files = [
    { name: '[Content_Types].xml', data: enc.encode(CONTENT_TYPES) },
    { name: '_rels/.rels', data: enc.encode(RELS_ROOT) },
    { name: 'xl/workbook.xml', data: enc.encode(WORKBOOK) },
    { name: 'xl/_rels/workbook.xml.rels', data: enc.encode(WORKBOOK_RELS) },
    { name: 'xl/styles.xml', data: enc.encode(STYLES) },
    { name: 'xl/worksheets/sheet1.xml', data: enc.encode(buildSheet(values)) },
  ];
  return zipStore(files);
}

/** 触发浏览器下载《重症质控指标上报》xlsx。 */
export function downloadReportWorkbook(values, filename) {
  const bytes = buildReportWorkbook(values);
  const blob = new Blob([bytes], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}
