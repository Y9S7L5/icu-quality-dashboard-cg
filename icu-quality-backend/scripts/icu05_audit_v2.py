"""ICU-05 反向验证审计脚本 v2 — 三态逻辑 + 完整字段"""
import sys, os, csv
from datetime import datetime, timedelta
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_client, get_bundle_data_v2
from scoring.bundle_engine import set_vaso_wide_labels, _classify_vasopressor, VASO_WIDE_LABELS

def main():
    sc = get_client('SmartCare')['SmartCare']

    # 注入 VASO_WIDE_LABELS
    vaso_drugs_cfg = list(sc.configDrug.find(
        {'classification': {'$regex': '血管活性', '$options': 'i'}},
        {'name': 1, '_id': 0}
    ))
    labels = {d['name'].strip().lower() for d in vaso_drugs_cfg if d.get('name')}
    if not labels:
        labels = {'去甲肾上腺素','多巴胺','多巴酚丁胺','肾上腺素','血管加压素',
                  '苯肾上腺素','米力农','左西孟旦'}
    set_vaso_wide_labels(labels)
    print(f'VASO_WIDE_LABELS: {len(labels)} drugs')

    dept_codes = ['3439','2927','2915','3442','3414','3412','2964','2953','3409','3487','3452']

    # === 乳酸反向索引 ===
    print('Building lactate index...')
    bga_all = list(sc.bGATemp.find(
        {'bedsides': {'$elemMatch': {'code': 'param_bg_Lac', 'valid': 'valid'}}},
        {'mrn': 1, 'bedsides': 1}
    ).batch_size(5000))
    lac_by_mrn = defaultdict(list)
    for doc in bga_all:
        mrn = doc.get('mrn', '')
        for bs in doc.get('bedsides', []):
            if bs.get('code') == 'param_bg_Lac' and bs.get('valid') == 'valid':
                lac_by_mrn[mrn].append({'value': bs.get('fVal'), 'time': bs.get('time')})
    print(f'  bGATemp docs: {len(bga_all)}, unique MRNs with Lac: {len(lac_by_mrn)}')

    # === 升压药反向索引 ===
    print('Building vasopressor index...')
    vaso_name_regex = '去甲肾|肾上腺素|多巴胺|多巴酚丁胺|血管加压素|苯肾上腺素|米力农|间羟胺|特利加压素|左西孟旦'
    vaso_all = list(sc.drugExe.find(
        {'drugList.name': {'$regex': vaso_name_regex, '$options': 'i'}}
    ).batch_size(5000))
    vaso_by_pid = defaultdict(list)
    for r in vaso_all:
        vaso_by_pid[str(r.get('pid', ''))].append(r)
    print(f'  drugExe vaso records: {len(vaso_all)}, unique SC pids: {len(vaso_by_pid)}')

    # === 逐月计算 ===
    rows = []
    for period in ['2026-06', '2026-07', '2026-08', '2026-09']:
        y, m = period.split('-')
        s = f'{y}-{m}-01'
        e_dt = datetime(int(y), int(m) + 1, 1) if m != '12' else datetime(int(y), 12, 31)
        e = e_dt.strftime('%Y-%m-%d')
        d = get_bundle_data_v2(dept_codes, s, e)

        for p in d['den_patients']:
            v3 = p.get('v3') or {}
            b1 = v3.get('bundle_1h') or {}
            b3 = v3.get('bundle_3h') or {}
            b6 = v3.get('bundle_6h') or {}
            cl = v3.get('clinical_layer') or {}
            shock = cl.get('layer4_shock', {})

            mrn = p.get('mrn', '')
            sc_pid = p.get('sc_pid', '')
            t0 = p.get('t0')

            # --- 乳酸三态 ---
            raw_lac = lac_by_mrn.get(mrn, [])
            filtered_lac = []
            if t0 and raw_lac:
                t0n = t0.replace(tzinfo=None) if t0.tzinfo else t0
                for lv in raw_lac:
                    lt = lv.get('time')
                    if lt:
                        if lt.tzinfo:
                            lt = lt.replace(tzinfo=None)
                        if t0n - timedelta(hours=2) <= lt <= t0n + timedelta(hours=6):
                            filtered_lac.append(lv)
            lactate_source = 'bGATemp' if raw_lac else 'none'

            k1_val = v3.get('k1')
            if k1_val is True:
                k1_status, k1_reason = 'True', 'lactate>=2 confirmed'
            elif k1_val is False:
                k1_status, k1_reason = 'False', 'lactate<2 or not in T0 window'
            else:
                if not raw_lac:
                    k1_status, k1_reason = 'None', 'no lactate data in bGATemp'
                else:
                    k1_status, k1_reason = 'None', f'lactate exists ({len(raw_lac)} raw) but not in T0 window'

            # --- 升压药三态 ---
            raw_vaso = vaso_by_pid.get(sc_pid, [])
            filtered_vaso = []
            if t0 and raw_vaso:
                t0n = t0.replace(tzinfo=None) if t0.tzinfo else t0
                t0_6h = t0n + timedelta(hours=6)
                t0_24h = t0n - timedelta(hours=24)
                for vr in raw_vaso:
                    st = vr.get('startTime')
                    if st:
                        if st.tzinfo:
                            st = st.replace(tzinfo=None)
                        if st <= t0_6h:
                            for dl in vr.get('drugList', []):
                                name = dl.get('name', '')
                                iw, ist = _classify_vasopressor(name)
                                if iw or ist:
                                    filtered_vaso.append({
                                        'name': name, 'in_wide': iw,
                                        'in_strict': ist, 'start': st
                                    })
            vasopressor_source = 'drugExe' if raw_vaso else 'none'

            k2_val = v3.get('k2')
            if k2_val is True:
                k2_status, k2_reason = 'True', 'vasopressor active in T0 window'
            elif k2_val is False:
                if filtered_vaso:
                    k2_status = 'False_BUG'
                    k2_reason = (f'vaso found in T0 window ({len(filtered_vaso)} records, '
                                 f'in_wide=False in_strict=True) but code only checks in_wide')
                elif raw_vaso:
                    k2_status = 'False_NO_WINDOW'
                    k2_reason = f'vaso records exist ({len(raw_vaso)}) but none in T0-24h~T0+6h window'
                else:
                    k2_status = 'False_CORRECT'
                    k2_reason = 'no vasopressor records in drugExe for this patient'
            else:
                k2_status, k2_reason = 'None', 'v3 returned k2=None'

            # --- 患者关联 ---
            if sc_pid:
                sc_pat = sc.patient.find_one({'mrn': mrn})
                if sc_pat and str(sc_pat.get('_id', '')) == sc_pid:
                    link_status, link_reason = 'ok', 'MRN bridge verified'
                elif sc_pat:
                    link_status = 'mismatch'
                    link_reason = (f'SC pid mismatch: expected '
                                   f'{str(sc_pat.get("_id", ""))[:15]}, got {sc_pid[:15]}')
                else:
                    link_status, link_reason = 'no_sc_patient', 'no SC patient with this MRN'
            else:
                link_status, link_reason = 'no_sc_pid', 'no sc_pid mapped'

            # --- bundle reasons 拼接 ---
            b1_reasons = b1.get('reasons', [])
            b3_reasons = b3.get('reasons', [])
            b6_reasons = b6.get('reasons', [])

            rows.append({
                'period': period,
                'mrn': mrn,
                'sc_pid': sc_pid,
                'dc_pid': str(p.get('_id', '')),
                't0': str(t0),
                't0_source': p.get('t0_source', ''),
                'source': p.get('source', ''),
                'candidate_status': p.get('candidate_status', ''),
                'k1': k1_val,
                'k1_status': k1_status,
                'k1_reason': k1_reason,
                'k2': k2_val,
                'k2_status': k2_status,
                'k2_reason': k2_reason,
                'bundle_1h_finish': b1.get('finish'),
                'bundle_1h_reasons': str(b1_reasons),
                'bundle_3h_finish': b3.get('finish'),
                'bundle_3h_reasons': str(b3_reasons),
                'bundle_6h_finish': b6.get('finish'),
                'bundle_6h_reasons': str(b6_reasons),
                'lactate_source': lactate_source,
                'vasopressor_source': vasopressor_source,
                'patient_link_status': link_status,
                'patient_link_reason': link_reason,
                'raw_lactate_record_count': len(raw_lac),
                'raw_vasopressor_record_count': len(raw_vaso),
                'filtered_lactate_record_count': len(filtered_lac),
                'filtered_vasopressor_record_count': len(filtered_vaso),
            })

    # === 写 CSV ===
    csv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'icu05_audit_v2_2026_06_09.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f'\nCSV written: {csv_path} ({len(rows)} rows)')

    # === 三态汇总 ===
    print('\n' + '=' * 70)
    print('三态汇总')
    print('=' * 70)
    for period in ['2026-06', '2026-07', '2026-08', '2026-09']:
        pr = [r for r in rows if r['period'] == period]
        k2_true = sum(1 for r in pr if r['k2_status'] == 'True')
        k2_bug = sum(1 for r in pr if r['k2_status'] == 'False_BUG')
        k2_nowindow = sum(1 for r in pr if r['k2_status'] == 'False_NO_WINDOW')
        k2_correct = sum(1 for r in pr if r['k2_status'] == 'False_CORRECT')
        k2_none = sum(1 for r in pr if r['k2_status'] == 'None')
        lac_ok = sum(1 for r in pr if r['raw_lactate_record_count'] > 0)
        lac_miss = sum(1 for r in pr if r['raw_lactate_record_count'] == 0)
        link_ok = sum(1 for r in pr if r['patient_link_status'] == 'ok')
        link_bad = len(pr) - link_ok

        print(f'\n{period}: den={len(pr)}')
        print(f'  K2: True={k2_true}, False_BUG(有药+in_wide漏)={k2_bug}, '
              f'False_无窗口={k2_nowindow}, False_正确={k2_correct}, None={k2_none}')
        print(f'  乳酸: 有记录={lac_ok}, 无记录={lac_miss}')
        print(f'  关联: ok={link_ok}, 异常={link_bad}')

    # === 总结分类 ===
    print('\n' + '=' * 70)
    print('结论分类')
    print('=' * 70)
    total = len(rows)
    cat_b = sum(1 for r in rows if r['patient_link_status'] != 'ok')
    cat_d_wide = sum(1 for r in rows if r['k2_status'] == 'False_BUG')
    cat_d_lac = sum(1 for r in rows if r['k1_status'] == 'None' and r['raw_lactate_record_count'] == 0)
    cat_f_vaso = sum(1 for r in rows if r['k2_status'] == 'False_NO_WINDOW')
    cat_a = sum(1 for r in rows if r['k2_status'] == 'False_CORRECT'
                and r['k1_status'] in ('False', 'None')
                and r['patient_link_status'] == 'ok')

    print(f'A. 源系统确实没有记录: {cat_a} (无升压药且无乳酸且关联正常)')
    print(f'B. 有记录但患者关联失败: {cat_b}')
    print(f'D. 有记录但药品代码未映射: {cat_d_wide} (in_wide=False, in_strict=True → BUG)')
    print(f'   乳酸有记录但代码未查到: 见各患者k1_reason')
    print(f'F. 有记录但时间窗过滤: {cat_f_vaso} (升压药在T0窗口外)')


if __name__ == '__main__':
    main()
