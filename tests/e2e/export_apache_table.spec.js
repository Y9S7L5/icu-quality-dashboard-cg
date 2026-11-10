const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

// xlsx 解析用：根 node_modules 没有 xlsx，显式引用前端依赖
function loadXlsx() {
  try {
    return require('xlsx');
  } catch (e) {
    return require(path.join(process.cwd(), 'icu-quality-dashboard', 'node_modules', 'xlsx'));
  }
}

test.describe('导出全科APACHE数据表 E2E', () => {

  test('点击导出下载《重症医学科APACHEII评分表》xlsx 并含分段汇总', async ({ page }) => {
    // 选择 2026-08（本机小库有数据的月份）
    await page.goto('/');
    await page.waitForSelector('.filter-bar', { timeout: 10000 });

    const yearSelect = page.locator('.filters select').first();
    await yearSelect.selectOption('2026');
    const startSelect = page.locator('.filters select').nth(1);
    await startSelect.selectOption('8');
    const endSelect = page.locator('.filters select').nth(2);
    await endSelect.selectOption('8');
    await page.waitForSelector('.indi-table tbody tr', { timeout: 30000 });

    // 捕获下载
    const downloadPromise = page.waitForEvent('download', { timeout: 120000 });
    const btn = page.locator('.filter-bar button', { hasText: '导出全科APACHE数据表' });
    await expect(btn).toBeVisible();
    await btn.click();
    const download = await downloadPromise;

    const suggested = download.suggestedFilename();
    console.log('下载文件名:', suggested);
    expect(suggested).toMatch(/^重症医学科APACHEII评分表_2026-08.*\.xlsx$/);

    const tmp = path.join(process.cwd(), '_e2e_download_apache.xlsx');
    await download.saveAs(tmp);
    const stat = fs.statSync(tmp);
    console.log('下载大小:', stat.size);
    expect(stat.size).toBeGreaterThan(1000);

    // xlsx 魔数 (PK zip)
    const head = fs.readFileSync(tmp).subarray(0, 2).toString('latin1');
    expect(head).toBe('PK');

    // 内容校验：标题 / 时间范围 / 表头 / 分段汇总
    const XLSX = loadXlsx();
    const wb = XLSX.read(fs.readFileSync(tmp));
    const ws = wb.Sheets[wb.SheetNames[0]];
    const aoa = XLSX.utils.sheet_to_json(ws, { header: 1 });
    console.log('行数(含表头/汇总):', aoa.length);

    expect(String(aoa[0][0])).toContain('APACHEII评分表');
    expect(String(aoa[1][0])).toBe('时间范围：2026年08月');
    expect(aoa[2].slice(0, 9)).toEqual(
      ['类别', '住院号', '姓名', '性别', '年龄', '入院/转科时间', '医生', '评分', '诊断']);

    // 汇总块在最后 8 行（5 分段 + ＜15/≥15 小计 + 未评分），分段计数之和 = 数据行数
    const texts = aoa.map((r) => String(r[0] ?? ''));
    const num = (prefix) => {
      const hit = texts.find((t) => t.startsWith(prefix));
      expect(hit, `缺少汇总行 ${prefix}`).toBeTruthy();
      return Number(hit.slice(prefix.length));
    };
    const lt10 = num('APACHEII＜10分例数：');
    const b10 = num('10≤APACHEII＜15分例数：');
    const b15 = num('15≤APACHEII＜20分例数：');
    const b20 = num('20≤APACHEII＜25分例数：');
    const ge25 = num('APACHEII≥25分例数：');
    const lt15 = num('APACHEII＜15分例数：');
    const ge15 = num('APACHEII≥15分例数：');
    const unscored = num('未评分例数：');
    const counts = [lt10, b10, b15, b20, ge25, unscored];
    const dataRows = aoa.length - 3 - 8; // 总行 − 标题/时间范围/表头 − 8 汇总行
    console.log('分段汇总:', counts.join('/'), '| ＜15:', lt15, '≥15:', ge15, '| 数据行:', dataRows);
    expect(counts.reduce((a, b) => a + b, 0)).toBe(dataRows);
    expect(lt15).toBe(lt10 + b10);   // 小计自洽
    expect(ge15).toBe(b15 + b20 + ge25);
    expect(dataRows).toBeGreaterThan(0);

    // 表体按入科时间升序
    const timeIdx = 5;
    const times = aoa.slice(3, 3 + dataRows).map((r) => String(r[timeIdx] ?? ''));
    const sorted = [...times].sort();
    expect(times).toEqual(sorted);

    // 按钮恢复可用
    await expect(btn).toHaveText('导出全科APACHE数据表');
    await expect(btn).toBeEnabled();
  });
});
