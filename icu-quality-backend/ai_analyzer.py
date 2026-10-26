# ai_analyzer.py
# 规则引擎判定异常 + LLM 生成质控报告（LLM只做解读，不碰诊疗）
# 打包兼容：注解延迟求值，避免 dict|None 在 Python<3.10 导入即崩
from __future__ import annotations

# ---- 指标元数据（与前端 indicators.js 保持同源，真实项目应抽到共享配置）----
INDICATOR_META = {
    "ICU-01": {"name": "ICU床位使用率", "unit": "%", "good": (75, 90), "warn": (60, 95), "dir": "range"},
    "ICU-02": {"name": "ICU医师床位比", "unit": ":1", "good": (0.8, 99), "warn": (0.5, 99), "dir": "higher"},
    "ICU-03": {"name": "ICU护士床位比", "unit": ":1", "good": (3.0, 99), "warn": (2.5, 99), "dir": "higher"},
    "ICU-04-01": {"name": "APACHEⅡ≥15分收治率", "unit": "%", "good": (50, 100), "warn": (30, 100), "dir": "higher"},
    "ICU-05": {"name": "感染性休克bundle完成率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-06": {"name": "抗菌药物前病原学送检率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-07": {"name": "DVT预防率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-08": {"name": "中重度ARDS俯卧位通气实施率", "unit": "%", "good": (80, 100), "warn": (60, 100), "dir": "higher"},
    "ICU-09": {"name": "ICU镇痛评估率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-10": {"name": "ICU镇静评估率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-11": {"name": "ICU患者标化病死指数(SMR)", "unit": "", "good": (0, 1.0), "warn": (0, 1.2), "dir": "lower"},
    "ICU-12": {"name": "非计划气管插管拔管率", "unit": "%", "good": (0, 5), "warn": (0, 10), "dir": "lower"},
    "ICU-13": {"name": "拔管后48h再插管率", "unit": "%", "good": (0, 5), "warn": (0, 12), "dir": "lower"},
    "ICU-14": {"name": "非计划转入ICU率", "unit": "%", "good": (0, 5), "warn": (0, 10), "dir": "lower"},
    "ICU-15": {"name": "转出ICU后48h重返率", "unit": "%", "good": (0, 3), "warn": (0, 6), "dir": "lower"},
    "ICU-16": {"name": "VAP发病率", "unit": "‰", "good": (0, 8), "warn": (0, 14), "dir": "lower"},
    "ICU-17": {"name": "CRBSI发病率", "unit": "‰", "good": (0, 1), "warn": (0, 3.5), "dir": "lower"},
    "ICU-18": {"name": "急性脑损伤意识评估率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-19": {"name": "48h内肠内营养启动率", "unit": "%", "good": (80, 100), "warn": (60, 100), "dir": "higher"},
}

# 已知的指标关联关系（用于归因提示，不是因果断言）
CORRELATIONS = [
    {
        "trigger": "ICU-16", "related": "ICU-06",
        "hint": "VAP发病率上升常与病原学送检/感控流程依从性下降相关"
    },
    {
        "trigger": "ICU-17", "related": "ICU-06",
        "hint": "CRBSI上升提示导管置入与维护流程可能存在问题"
    },
]


def detect_abnormal(values: dict) -> list:
    """规则引擎：纯确定性逻辑判定每个指标状态，不依赖LLM"""
    results = []
    for code, val in values.items():
        meta = INDICATOR_META.get(code)
        if not meta or val is None:
            continue
        status = _eval_status(meta, val)
        if status != "good":
            results.append({
                "code": code, "name": meta["name"],
                "value": val, "unit": meta["unit"], "level": status,
            })
    return results


def _eval_status(meta, val) -> str:
    g_lo, g_hi = meta["good"]
    w_lo, w_hi = meta["warn"]
    if g_lo <= val <= g_hi:
        return "good"
    if w_lo <= val <= w_hi:
        return "warn"
    return "danger"


def build_attribution(abnormal: list, values: dict) -> list:
    """基于预设关联关系，给异常指标补充归因线索（确定性，不靠LLM瞎猜）"""
    abnormal_codes = {a["code"] for a in abnormal}
    hints = []
    for rel in CORRELATIONS:
        if rel["trigger"] in abnormal_codes:
            related_val = values.get(rel["related"])
            hints.append({
                "trigger": rel["trigger"],
                "related": rel["related"],
                "related_value": related_val,
                "hint": rel["hint"],
            })
    return hints


def build_prompt(period: str, abnormal: list, hints: list, all_good_count: int) -> str:
    """构造给LLM的prompt——把确定性结论喂进去，约束它只做翻译解读"""
    abnormal_txt = "\n".join(
        f"- {a['name']}({a['code']}): {a['value']}{a['unit']}，状态：{'预警' if a['level']=='warn' else '严重异常'}"
        for a in abnormal
    ) or "无异常指标"

    hints_txt = "\n".join(
        f"- {h['trigger']} 与 {h['related']}(当前值{h['related_value']})：{h['hint']}"
        for h in hints
    ) or "无关联线索"

    return f"""你是医院重症医学科的质控数据分析助手。以下是 {period} 的ICU质控指标分析结果，请基于这些【已经判定好的确定性结论】生成一段简洁、专业的中文质控总结报告。

【异常指标（系统已判定，请勿改动判定结论）】
{abnormal_txt}

【指标关联线索（供归因参考，仅为统计关联，非因果结论）】
{hints_txt}

【正常指标数量】{all_good_count} 项

请严格遵守以下要求：
1. 只对上述数据做解读和归因提示，不要编造未提供的数据或指标。
2. 【绝对禁止】给出任何针对具体患者的临床诊疗建议、用药建议或治疗方案。
3. 可以提出"流程层面"的管理改进方向（如建议核查某项流程依从性），但措辞应为"建议核查/关注"，不可下定论。
4. 归因时必须说明这是"统计关联，需临床团队进一步核实"，不可断言因果。
5. 报告控制在150字以内，语气客观专业。

请输出报告："""


# ---- LLM 调用（以 OpenAI 兼容接口为例，可换成你自己的模型）----
def call_llm(prompt: str) -> str:
    import os
    from openai import OpenAI  # pip install openai

    client = OpenAI(
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
    )
    resp = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "你是严谨的医疗质控数据分析助手，绝不提供临床诊疗建议。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,  # 低温度，减少发挥
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()


def analyze(period: str, values: dict) -> dict:
    """对外总入口"""
    abnormal = detect_abnormal(values)
    hints = build_attribution(abnormal, values)
    good_count = sum(1 for c, v in values.items()
                     if c in INDICATOR_META and v is not None
                     and _eval_status(INDICATOR_META[c], v) == "good")
    prompt = build_prompt(period, abnormal, hints, good_count)

    try:
        summary = call_llm(prompt)
    except Exception as e:
        # LLM 挂了也不能让看板崩，降级成规则文本
        summary = _fallback_summary(abnormal, good_count)

    return {"summary": summary, "abnormal": abnormal, "hints": hints}


def _fallback_summary(abnormal: list, good_count: int) -> str:
    """LLM不可用时的兜底（纯规则生成，不依赖AI）"""
    if not abnormal:
        return f"本期共 {good_count} 项指标全部达标，整体平稳。"
    names = "、".join(f"{a['name']}({a['value']}{a['unit']})" for a in abnormal)
    return f"本期 {good_count} 项达标，{len(abnormal)} 项需关注：{names}。建议质控团队核查相关流程依从性。"


# ============================================================
# ICU-06 AI 判定：抗菌药使用目的分类（治疗性 vs 预防性）
# ============================================================

import json
import hashlib
import re
import threading
from datetime import datetime as _dt
from tzutil import fmt_bj  # noqa: E402 — naive UTC → 北京展示

# AI 并发控制
_AI_SEMAPHORE = threading.Semaphore(5)

# AI 提示词模板 — 强制 JSON 输出
ABX_PURPOSE_SYSTEM_PROMPT = """你是ICU临床药师，辅助判断住院患者使用抗菌药物的真实目的（治疗性 vs 预防性）。

【判断规则】
- 治疗性：有明确感染证据（临床诊断含感染关键词、体温≥38.5℃、WBC/CRP/PCT显著升高），或临床场景高度指向活动性感染。
- 预防性：围术期短程用药、无感染相关症状体征、炎症指标正常或仅轻微波动、给药次数少且疗程极短。

【输入上下文】
你将收到患者的诊断、手术史、抗菌药名称、疗程小时数、给药次数、炎症指标摘要。

【输出要求】
严格输出以下JSON格式，不得包含任何其他文字：
{"purpose":"治疗性"|"预防性","confidence":0.0-1.0,"reason":"一句话依据（≤50字）"}

其中：
- purpose: 必须是"治疗性"或"预防性"二选一
- confidence: 0.0~1.0，表示你的判定确信度
- reason: 用一句话说明判定依据，引用输入中的关键信息"""


# ============================================================
# 脓毒症早期预警 AI 判定（Sepsis-3 / qSOFA 辅助识别）
# 定位：质控分诊提示，NOT 临床诊疗决策。只判"是否需要临床团队评估"。
# ============================================================

SEPSIS_ALERT_SYSTEM_PROMPT = """你是ICU质控辅助分析助手，依据 Sepsis-3 国际共识与 qSOFA 标准，辅助判断住院患者当前数据是否提示【疑似脓毒症 / 需临床团队尽快评估】。

【判断依据（Sepsis-3 + qSOFA）】
1. qSOFA 三项（每项1分，≥2分提示预后不良、需警惕脓毒症）：
   - 呼吸频率 ≥ 22 次/分
   - 收缩压 ≤ 100 mmHg
   - 意识改变（GCS < 15 / 新发意识障碍）
2. 感染证据：临床诊断含感染关键词、体温 ≥38.3℃ 或 <36℃、WBC异常、PCT/CRP/乳酸显著升高。
3. 器官功能恶化趋势：乳酸 ≥2 mmol/L、少尿、新发或加重的器官功能指标异常。
4. 脓毒性休克警示：在充分液体复苏后仍需升压药维持 MAP≥65 且乳酸>2 —— 但你【不评估是否已复苏、不建议是否用升压药】，仅标记该数据组合为高危。

【风险分级】
- high：qSOFA≥2 且有感染证据，或乳酸≥4，或已出现休克数据组合 —— 提示尽快床旁评估。
- medium：qSOFA=1 伴感染证据，或炎症指标显著升高但 qSOFA 未达标 —— 提示加强监测。
- low：无明确感染证据且生命体征平稳。

【绝对禁止】
- 禁止给出任何诊断结论（不得说"该患者已是脓毒症"）。
- 禁止给出任何治疗/用药/液体复苏/抗菌药建议。
- 你的输出只能是"是否建议临床团队评估"的分诊提示，最终诊断与处置由临床医师负责。

【输出要求】
严格输出以下JSON，不得包含任何其他文字：
{"risk":"high"|"medium"|"low","qsofa":0-3,"suspect_sepsis":true|false,"reason":"一句话依据（≤60字，引用输入关键数据）","action":"建议措辞，仅限'建议临床团队尽快床旁评估'/'建议加强监测与复查'/'暂无预警，常规监测'"}"""


def parse_llm_json(text: str) -> dict | None:
    """
    容错 JSON 解析：剥 markdown 代码块、正则提取首个 {...}、归一中文标点。
    返回解析后的 dict，失败返回 None。
    """
    if not text:
        return None
    text = text.strip()

    # 1. 剥除 ```json / ``` 代码块标记
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")

    # 2. 归一中文标点（LLM 有时用中文引号/冒号）
    text = text.replace("“", '"').replace("”", '"')  # "  "
    text = text.replace("：", ":").replace("，", ",")  # ： ，

    # 3. 正则提取首个 {...} 片段（支持嵌套）
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# AI 重试次数（可配置常量）
AI_MAX_RETRY = 1


def call_llm_json_with_system(system_prompt: str, prompt: str, max_tokens: int = 200) -> dict | None:
    """
    调用 LLM 返回结构化 JSON。
    复用现有 OpenAI client 配置，强制 JSON 输出。
    返回解析后的 dict，失败返回 None。
    """
    import os
    from openai import OpenAI

    client = OpenAI(
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
    )
    resp = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,       # 极低温度，保证判定一致
        max_tokens=max_tokens,
        response_format={"type": "json_object"},  # 强制 JSON
    )
    raw = resp.choices[0].message.content.strip()
    return parse_llm_json(raw)


def call_llm_json(prompt: str) -> dict | None:
    return call_llm_json_with_system(ABX_PURPOSE_SYSTEM_PROMPT, prompt)


# ============================================================
# ai_decision_cache 缓存表
# ============================================================

AI_CACHE_COLLECTION = "ai_decision_cache"


def ensure_ai_cache_collection():
    """
    创建 ai_decision_cache 集合及唯一索引 (hisPid, task)。
    幂等 — 多次调用安全。
    """
    from db import BED_DB_NAMES, get_client
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            coll = db[AI_CACHE_COLLECTION]
            # 唯一索引: 同一患者同一任务只判一次
            try:
                coll.create_index(
                    [("hisPid", 1), ("task", 1)],
                    unique=True, background=True,
                    name="idx_hispid_task",
                )
            except Exception:
                pass
            # 辅助索引: 按时间查
            try:
                coll.create_index(
                    [("created_at", -1)],
                    background=True,
                    name="idx_created_at",
                )
            except Exception:
                pass
            break
        except Exception:
            continue


def get_ai_cache(hispid: str, task: str = "abx_purpose", key: str = "") -> dict | None:
    """读取 AI 判定缓存。返回 result 子文档或 None。key 用于区分同 task 下的不同实体。"""
    from db import BED_DB_NAMES, get_client
    query = {"hisPid": hispid, "task": task}
    if key:
        query["key"] = key
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            doc = db[AI_CACHE_COLLECTION].find_one(query, {"result": 1, "_id": 0})
            if doc:
                return doc.get("result")
        except Exception:
            continue
    return None


def set_ai_cache(hispid: str, task: str, result: dict, prompt_snapshot: str, key: str = ""):
    """写入 AI 判定缓存。幂等 upsert，缓存命中时直接返回不重调。"""
    from db import BED_DB_NAMES, get_client
    query = {"hisPid": hispid, "task": task}
    if key:
        query["key"] = key
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            doc = {
                "hisPid": hispid,
                "task": task,
                "result": result,
                "prompt_snapshot": prompt_snapshot,
                "created_at": _dt.utcnow(),
            }
            if key:
                doc["key"] = key
            db[AI_CACHE_COLLECTION].update_one(query, {"$set": doc}, upsert=True)
            break
        except Exception:
            continue


def _build_abx_prompt(ctx: dict) -> str:
    """构造抗菌药目的判定提示词"""
    return f"""请判断以下ICU患者使用抗菌药的真实目的。

【患者信息】
诊断：{ctx.get('diagnosis', '未知')}
手术史：{ctx.get('surgery', '无')}
抗菌药：{ctx.get('antibiotics', '未知')}
疗程：{ctx.get('course_hours', '?')} 小时
给药次数：{ctx.get('dose_count', '?')} 次
炎症指标：{ctx.get('inflammation', '未查')}

请按要求输出JSON判定结果。"""


def _build_sepsis_prompt(ctx: dict) -> str:
    """构造脓毒症预警判定提示词（精简版，规则已在 system prompt）"""
    return f"""判断该ICU患者是否疑似脓毒症、是否需评估，按要求输出JSON。

诊断：{ctx.get('diagnosis', '未知')}
T={ctx.get('temperature', '?')}℃ RR={ctx.get('resp_rate', '?')} SBP={ctx.get('sbp', '?')} MAP={ctx.get('map', '?')} 意识={ctx.get('consciousness', '?')}
WBC={ctx.get('wbc', '?')} PCT={ctx.get('pct', '?')} CRP={ctx.get('crp', '?')} 乳酸={ctx.get('lactate', '?')}mmol/L
尿量={ctx.get('urine_output', '?')} 升压药={ctx.get('vasopressor', '?')}"""


def _fallback_sepsis_alert(ctx: dict, reason: str) -> dict:
    return {
        "risk": "unknown",
        "qsofa": None,
        "suspect_sepsis": False,
        "reason": reason[:60],
        "action": "建议加强监测与复查",
        "by": "fallback",
        "evaluated": False,
        "need_review": True,
    }


def classify_sepsis_alert_with_ai(ctx: dict) -> dict | None:
    """
    调用 AI 做脓毒症早期预警分诊提示。

    返回:
      {risk, qsofa, suspect_sepsis, reason, action, by, need_review}

    仅用于质控分诊提示，不输出诊断或治疗建议。
    """
    hispid = ctx.get("hisPid", "")
    if not hispid:
        return None

    cache_task = "sepsis_alert"
    cache_key = f"{hispid}:{ctx.get('sample_time') or ctx.get('time') or ''}"
    cached = get_ai_cache(cache_key, cache_task)
    if cached:
        return cached

    acquired = _AI_SEMAPHORE.acquire(timeout=30)
    if not acquired:
        return _fallback_sepsis_alert(ctx, "AI并发已满，需人工复核预警数据")

    try:
        prompt = _build_sepsis_prompt(ctx)
        llm_result = call_llm_json_with_system(SEPSIS_ALERT_SYSTEM_PROMPT, prompt, max_tokens=220)
        if not llm_result:
            retry_prompt = prompt + (
                "\n\n【重要提醒】严格只输出一个 JSON 对象，不要 markdown 代码块，不要解释。"
                "仅输出: {\"risk\":\"high|medium|low\",\"qsofa\":0,\"suspect_sepsis\":false,"
                "\"reason\":\"...\",\"action\":\"...\"}"
            )
            llm_result = call_llm_json_with_system(SEPSIS_ALERT_SYSTEM_PROMPT, retry_prompt, max_tokens=220)

        if not llm_result:
            result = _fallback_sepsis_alert(ctx, "AI解析失败，需人工复核预警数据")
        else:
            risk = str(llm_result.get("risk", "medium")).lower()
            if risk not in {"high", "medium", "low"}:
                risk = "medium"

            try:
                qsofa = int(llm_result.get("qsofa", 0))
                qsofa = max(0, min(3, qsofa))
            except (ValueError, TypeError):
                qsofa = 0

            action = str(llm_result.get("action", "")).strip()
            allowed_actions = {
                "建议临床团队尽快床旁评估",
                "建议加强监测与复查",
                "暂无预警，常规监测",
            }
            if action not in allowed_actions:
                action = {
                    "high": "建议临床团队尽快床旁评估",
                    "medium": "建议加强监测与复查",
                    "low": "暂无预警，常规监测",
                }[risk]

            result = {
                "risk": risk,
                "qsofa": qsofa,
                "suspect_sepsis": bool(llm_result.get("suspect_sepsis", risk != "low")),
                "reason": str(llm_result.get("reason", ""))[:60],
                "action": action,
                "by": "ai",
                "evaluated": True,
                "need_review": risk in {"high", "medium"},
            }

        set_ai_cache(cache_key, cache_task, result, prompt)
        return result
    except Exception:
        return _fallback_sepsis_alert(ctx, "AI调用异常，需人工复核预警数据")
    finally:
        _AI_SEMAPHORE.release()


def classify_abx_with_ai(ctx: dict) -> dict | None:
    """
    调用 AI 判定抗菌药使用目的（治疗性 vs 预防性）。

    ctx 需包含:
      - hisPid: 患者住院号（缓存键）
      - diagnosis: 临床诊断
      - surgery: 手术史摘要
      - antibiotics: 抗菌药名称
      - course_hours: 疗程小时数
      - dose_count: 给药次数
      - inflammation: 炎症指标摘要

    流程:
      1. 查 ai_decision_cache，命中直接返回
      2. 未命中 → 调 LLM（受并发上限 Semaphore(5) 控制）
      3. 解析失败 → 重试一次（AI_MAX_RETRY=1），重试 prompt 追加强约束
      4. 两次都失败 → fallback 兜底
      5. 结果写缓存

    返回: {purpose, confidence, reason, by: "ai"|"fallback", need_review: bool} 或 None
    """
    hispid = ctx.get("hisPid", "")
    if not hispid:
        return None

    # Step 1: 查缓存
    cached = get_ai_cache(hispid, "abx_purpose")
    if cached:
        return cached

    # Step 2: 调 LLM（并发控制）
    acquired = _AI_SEMAPHORE.acquire(timeout=30)
    if not acquired:
        return {"purpose": "未判定", "confidence": 0.0,
                "reason": "AI并发已满,需人工复核", "by": "fallback",
                "evaluated": False,
                "need_review": True}

    try:
        prompt = _build_abx_prompt(ctx)
        llm_result = call_llm_json(prompt)

        # Step 3: 解析失败 → 重试一次
        if not llm_result:
            retry_prompt = prompt + (
                "\n\n【重要提醒】严格只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。"
                "仅输出: {\"purpose\":\"...\",\"confidence\":...,\"reason\":\"...\"}"
            )
            llm_result = call_llm_json(retry_prompt)

        # Step 4: 判定结果
        course_hours = ctx.get("course_hours", 0)
        dose_count = ctx.get("dose_count", 0)
        antibiotics = ctx.get("antibiotics", "")

        if not llm_result:
            result = {
                "purpose": "未判定", "confidence": 0.0,
                "reason": f"AI解析失败兜底(疗程{course_hours:.0f}h/{dose_count}次/{antibiotics[:30]})",
                "by": "fallback", "evaluated": False, "need_review": True,
            }
        else:
            purpose_raw = str(llm_result.get("purpose", "治疗性"))
            if "预防" in purpose_raw:
                purpose = "预防性"
            else:
                purpose = "治疗性"

            try:
                conf = float(llm_result.get("confidence", 0.5))
                conf = max(0.0, min(1.0, conf))
            except (ValueError, TypeError):
                conf = 0.5

            reason = str(llm_result.get("reason", ""))[:200]
            result = {
                "purpose": purpose,
                "confidence": conf,
                "reason": reason,
                "by": "ai",
                "evaluated": True,
                "need_review": conf < 0.6,
            }

        # Step 5: 写缓存
        set_ai_cache(hispid, "abx_purpose", result, prompt)

        return result

    except Exception:
        course_hours = ctx.get("course_hours", 0)
        dose_count = ctx.get("dose_count", 0)
        antibiotics = ctx.get("antibiotics", "")
        return {"purpose": "未判定", "confidence": 0.0,
                "reason": f"AI调用异常兜底(疗程{course_hours:.0f}h/{dose_count}次/{antibiotics[:30]})",
                "by": "fallback", "evaluated": False, "need_review": True}
    finally:
        _AI_SEMAPHORE.release()


def get_all_ai_decisions(dept_codes: list = None, period_start: str = None,
                         period_end: str = None, min_confidence: float = None,
                         limit: int = 500) -> list:
    """
    查询 ai_decision_cache 中的 AI 判定记录。
    支持按科室、时间范围、置信度阈值筛选。
    用于前端 AI 决策复核界面。
    """
    from db import BED_DB_NAMES, get_client
    results = []
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            query = {"task": "abx_purpose"}
            # created_at 存储为真 UTC → 查询边界北京−8h（tzutil 入口转换）
            from datetime import timedelta as _td
            if period_start:
                query["created_at"] = query.get("created_at", {})
                query["created_at"]["$gte"] = _dt.fromisoformat(period_start) - _td(hours=8)
            if period_end:
                query["created_at"] = query.get("created_at", {})
                query["created_at"]["$lte"] = _dt.fromisoformat(
                    f"{period_end}-31" if len(period_end) == 7 else period_end) - _td(hours=8)
            if min_confidence is not None:
                query["result.confidence"] = {"$lte": float(min_confidence)}

            docs = list(db[AI_CACHE_COLLECTION].find(
                query,
                {"hisPid": 1, "task": 1, "result": 1, "created_at": 1, "_id": 0},
            ).sort("created_at", -1).limit(limit))
            if docs:
                results = docs
                break
        except Exception:
            continue

    # 格式化返回
    return [{
        "hisPid": d["hisPid"],
        "task": d["task"],
        "purpose": d.get("result", {}).get("purpose", ""),
        "confidence": d.get("result", {}).get("confidence", 0),
        "reason": d.get("result", {}).get("reason", ""),
        "decided_by": d.get("result", {}).get("by", "ai"),
        "created_at": fmt_bj(d.get("created_at")) if d.get("created_at") else "",
    } for d in results]


def override_ai_decision(hispid: str, purpose: str = "", reason: str = "",
                         overridden_by: str = "主任",
                         task: str = "abx_purpose", key: str = "",
                         result: dict | None = None) -> dict:
    """
    人工推翻 AI 判定。
    写回 ai_decision_cache，标记 source='override'。
    - abx_purpose 用法（向后兼容）：override_ai_decision(hispid, "治疗性", "理由")
    - 通用用法：override_ai_decision(hispid, task="septic_shock_confirm", key=diseaseId, result={...})
    """
    from db import BED_DB_NAMES, get_client
    if result is None:
        # 向后兼容：abx_purpose 的旧签名
        result = {
            "purpose": purpose,
            "confidence": 1.0,
            "reason": f"[人工推翻({overridden_by})] {reason}",
            "source": "override",
        }
    query = {"hisPid": hispid, "task": task}
    if key:
        query["key"] = key
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            doc = {
                "hisPid": hispid,
                "task": task,
                "result": result,
                "prompt_snapshot": f"manual_override by {overridden_by}",
                "created_at": _dt.utcnow(),
            }
            if key:
                doc["key"] = key
            db[AI_CACHE_COLLECTION].update_one(query, {"$set": doc}, upsert=True)
            return {"success": True, "hisPid": hispid, "result": result}
        except Exception:
            continue
    return {"success": False, "error": "Database not available"}


# ============================================================
# 脓毒性休克确认（Sepsis-3）
# ============================================================

AI_CONF_THRESHOLD = 0.6


def _safe_float(value):
    """安全转 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

SEPTIC_SHOCK_CONFIRM_SYSTEM_PROMPT = """你是重症医学科(ICU)质控 AI。你的任务是：依据 Sepsis-3 标准，判断某例患者是否构成【脓毒症】与【脓毒性休克】，确定该次事件的时间零点 T0，并输出 SOFA 评分明细。你只依据我在用户消息中提供的结构化指标数据进行判断，不得编造或假设任何未提供的数据。

═══════════════ 一、权威定义（Sepsis-3, 2016 JAMA）═══════════════
1. 脓毒症 (Sepsis) = 感染（确诊或高度怀疑）+ 因感染导致的 SOFA 评分较基线急性升高 ≥2 分。
   · 既往无器官功能障碍者，基线 SOFA 记为 0。
2. 脓毒性休克 (Septic Shock) = 在脓毒症基础上，经【充分液体复苏】后仍需【血管活性药物】维持 MAP ≥ 65 mmHg，且【血乳酸 > 2 mmol/L】。三项须同时满足。

═══════════════ 二、SOFA 六系统评分规则（每系统 0–4 分）═══════════════
· 呼吸  PaO2/FiO2 (mmHg)：<400=1；<300=2；<200 且机械通气=3；<100 且机械通气=4
· 凝血  血小板 (×10^9/L)：<150=1；<100=2；<50=3；<20=4
· 肝    总胆红素 (mg/dL)：≥1.2=1；≥2.0=2；≥6.0=3；≥12.0=4
· 中枢  GCS：<15=1；<13=2；<10=3；<6=4
· 肾    肌酐 (mg/dL)：≥1.2=1；≥2.0=2；≥3.5=3；≥5.0=4  或  尿量：<500 ml/d=3；<200 ml/d=4（取较高分）
· 心血管：MAP<70未用升压药=1；多巴胺≤5或多巴酚丁胺=2；多巴胺>5或去甲≤0.1或肾上腺素≤0.1=3；多巴胺>15或去甲>0.1或肾上腺素>0.1=4

═══════════════ 三、T0 判定 ═══════════════
T0 = 在感染背景下，SOFA 相对基线首次急性升高达到 ≥2 分的时间点。依据各指标采样时间，取"促成 SOFA 达标的关键指标最早满足的时间"。

═══════════════ 四、重要判定原则 ═══════════════
· qSOFA 仅作床旁筛查提示，不能作为确认依据。
· "充分液体复苏"若数据未提供，标注 "unknown"。
· 任一指标缺失时须据实标注缺失，不得以默认值凑分。
· 升压药须为"当前正在泵注"。

═══════════════ 五、输出：严格 JSON ═══════════════
{
  "confirm": true/false,
  "is_sepsis": true/false,
  "is_septic_shock": true/false,
  "sofa_baseline": 数字,
  "sofa_current": 数字,
  "sofa_delta": 数字,
  "sofa_breakdown": {"resp":n,"coag":n,"liver":n,"cardio":n,"cns":n,"renal":n},
  "t0": "ISO8601 或 null",
  "t0_basis": "字符串",
  "qsofa": 数字,
  "lactate": 数字或null,
  "on_vasopressor": true/false,
  "adequate_fluid": "yes/no/unknown",
  "confidence": 0.0-1.0,
  "reason": "字符串"
}"""


def _bili_mgdl(val):
    """µmol/L → mg/dL"""
    v = _safe_float(val)
    return round(v / 17.1, 2) if v is not None else None


def _crea_mgdl(val):
    """µmol/L → mg/dL"""
    v = _safe_float(val)
    return round(v / 88.4, 2) if v is not None else None


def _s_resp(pf, on_vent):
    if pf is None:
        return 0
    if pf < 100 and on_vent:
        return 4
    if pf < 200 and on_vent:
        return 3
    if pf < 300:
        return 2
    if pf < 400:
        return 1
    return 0


def _s_coag(plt):
    v = _safe_float(plt)
    if v is None:
        return 0
    if v < 20: return 4
    if v < 50: return 3
    if v < 100: return 2
    if v < 150: return 1
    return 0


def _s_liver(tbil_mgdl):
    v = _safe_float(tbil_mgdl)
    if v is None:
        return 0
    if v >= 12.0: return 4
    if v >= 6.0: return 3
    if v >= 2.0: return 2
    if v >= 1.2: return 1
    return 0


def _s_cns(gcs):
    v = _safe_float(gcs)
    if v is None:
        return 0
    if v < 6: return 4
    if v < 10: return 3
    if v < 13: return 2
    if v < 15: return 1
    return 0


def _s_renal(crea_mgdl, urine_ml_d):
    c = _safe_float(crea_mgdl)
    u = _safe_float(urine_ml_d)
    score = 0
    if c is not None:
        if c >= 5.0: score = 4
        elif c >= 3.5: score = 3
        elif c >= 2.0: score = 2
        elif c >= 1.2: score = 1
    if u is not None:
        if u < 200: score = max(score, 4)
        elif u < 500: score = max(score, 3)
    return score

def _drug_amount_ug(dose, unit):
    """药量 → µg。"""
    v = _safe_float(dose)
    if v is None:
        return None
    u = (unit or "").lower()
    if u == "g":
        return v * 1_000_000
    if u in ("ug", "µg", "mcg"):
        return v
    return v * 1000  # mg 及默认按 mg


def _vaso_rate_ugkgmin(drug, doc, weight_kg, speed_mlh):
    """浓度=药量/总配液量；速率=浓度×泵速(ml/h)/60/体重(kg) → µg/kg/min。

    优先使用 scoring.adapter.ne_ugkgmin (含单位白名单、盐型校验)。
    降级到旧逻辑兜底。
    """
    try:
        from scoring.adapter import ne_ugkgmin
        action = {
            "dose": drug.get("dose"),
            "unit": drug.get("unit"),
            "liquidAmount": drug.get("liquidAmount") or doc.get("liquidAmount"),
            "liquidAmountUnit": drug.get("liquidAmountUnit") or doc.get("liquidAmountUnit") or "ml",
            "speed": speed_mlh,
        }
        result, flags = ne_ugkgmin(action, weight_kg)
        if result is not None:
            return result
    except Exception:
        pass
    # 降级: 旧逻辑
    drug_ug = _drug_amount_ug(drug.get("dose"), drug.get("unit"))
    total_ml = _safe_float(drug.get("liquidAmount")) or _safe_float(doc.get("liquidAmount"))
    if drug_ug and total_ml and weight_kg and speed_mlh and speed_mlh > 0:
        return round((drug_ug / total_ml) * speed_mlh / 60.0 / weight_kg, 4)
    return None


def _effective_action(actions, before=None):
    """
    重建 before 时刻的有效泵注状态：取 before 之前的最后一个动作。
    返回 (action, speed_mlh)。末动作 stop/pause 视为未在泵 → speed 0。
    """
    prior = [a for a in actions if a.get("time") and (before is None or a["time"] <= before)]
    if not prior:
        return (None, 0.0)
    prior.sort(key=lambda a: a.get("time") or _dt.min)
    last = prior[-1]
    if last.get("action") in ("stop", "pause"):
        return (last.get("action"), 0.0)
    speed = _safe_float(last.get("speed")) or _safe_float(last.get("dripSpeed")) or 0.0
    return (last.get("action"), speed)


def _has_infection_evidence(ev):
    """空 dict 也 truthy，必须看实际内容。"""
    if not ev:
        return False
    return bool(ev.get("diagnosis") or ev.get("antibiotics")
                or ev.get("culture") or (ev.get("inflammation") or {}))


def _sofa_cardio(map_val, vasos):
    """心血管 SOFA。vasos = [{drug, rate_ugkgmin}]"""
    if vasos:
        scores = []
        for v in vasos:
            drug = (v.get("drug") or "").lower()
            zh = v.get("drug") or ""
            r = _safe_float(v.get("rate_ugkgmin"))
            if "norepinephrine" in drug or "去甲" in zh:
                scores.append(4 if r is not None and r > 0.1 else 3)
            elif ("epinephrine" in drug or "肾上腺素" in zh) and "去甲" not in zh and "norepinephrine" not in drug:
                scores.append(4 if r is not None and r > 0.1 else 3)
            elif "dopamine" in drug or "多巴胺" in zh:
                if r is None:
                    scores.append(3)
                elif r > 15:
                    scores.append(4)
                elif r > 5:
                    scores.append(3)
                else:
                    scores.append(2)
            elif "dobutamine" in drug or "多巴酚丁胺" in zh:
                scores.append(2)
            else:
                scores.append(3)  # 加压素/去氧肾/间羟胺：在用即≥3
        return max(scores) if scores else 0
    mv = _safe_float(map_val)
    if mv is not None and mv < 70:
        return 1
    return 0


def compute_sofa_t0(items, baseline=0):
    """启发式 T0：按各阳性子项采样时间累计，首次达到 baseline+2 的时间。"""
    contrib = sorted(
        [it for it in items if it.get("score", 0) > 0 and it.get("time")],
        key=lambda x: x["time"]
    )
    running = 0
    hit = []
    for it in contrib:
        running += it["score"]
        hit.append(f'{it["key"]}={it["score"]}@{it["time"]}')
        if running - baseline >= 2:
            return it["time"], "；".join(hit)
    return None, "SOFA 急升未达 2 分或关键指标缺时间"


def extract_sofa_qsofa(pid, his_pid, before=None):
    """
    从 bGATemp + bedside + drugExe + VI_ICU_EXAM_ITEM 提取 SOFA 六组件 + qSOFA。
    缺失项为 None；sofa_current 仅累加已测域（真实值的下界）。
    回顾性调用请传 before=diagnosisTime/T0。
    """
    from db import BED_DB_NAMES, get_client, get_datacenter_db
    from datetime import timedelta
    from bson import ObjectId

    weight = None
    sofa_data = {
        "pid": pid, "his_pid": his_pid, "weight": None,
        "sofa_baseline": 0, "sofa_current": None, "sofa_breakdown": {},
        "sofa_items": {},
        "rr": None, "on_ventilator": False, "sbp": None, "gcs": None, "qsofa": None,
        "map": None, "map_time": None,
        "vasopressors": [], "lactate": None, "lactate_time": None,
        "t0": None, "t0_basis": "",
        "infection_evidence": None, "fluid_resuscitation": "unknown",
    }
    sitems = sofa_data["sofa_items"]
    end_dt = before or _dt.utcnow()
    start_dt = end_dt - timedelta(hours=24)

    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]

            # 体重（patient.weight，kg；doc.weight 常为0不可信）
            pat = db.patient.find_one({"_id": ObjectId(pid)}, {"weight": 1})
            if pat:
                w = pat.get("weight")
                if w and isinstance(w, (int, float)) and w > 0:
                    weight = w
                    sofa_data["weight"] = w

            # bGATemp: P/F, FiO2, Lac, pAO2
            coll = "BGATemp" if "BGATemp" in db.list_collection_names() else "bGATemp"
            if coll in db.list_collection_names():
                for doc in db[coll].find(
                    {"eventExe.pid": pid, "eventExe.startTime": {"$gte": start_dt, "$lte": end_dt}},
                    {"eventExe": 1, "bedsides": 1},
                ).sort("eventExe.startTime", -1).max_time_ms(20000).limit(500):
                    evt_time = (doc.get("eventExe") or {}).get("startTime")
                    for b in doc.get("bedsides") or []:
                        code = b.get("code", "")
                        fval = _safe_float(b.get("strVal") or b.get("fVal"))
                        if fval is None:
                            continue
                        if code == "param_bg_P/Fratio" and "pf" not in sitems:
                            sitems["pf"] = {"val": fval, "time": evt_time}
                        elif code == "param_bg_FiO2" and "fio2" not in sitems:
                            sitems["fio2"] = {"val": fval, "time": evt_time}
                        elif code == "param_bg_pAO2" and "pao2" not in sitems:
                            sitems["pao2"] = {"val": fval, "time": evt_time}
                        elif code == "param_bg_Lac" and sofa_data["lactate"] is None:
                            sofa_data["lactate"] = round(fval, 2)
                            sofa_data["lactate_time"] = evt_time

            # bedside: GCS, MAP, 尿量, RR, SBP
            bedside_codes = {
                "gcs": ["param_score_gcs_obs"],
                "map": ["param_ibp_m", "param_nibp_m"],
                "urine": ["param_niaoLiang"],
                "rr": ["param_resp", "param_vent_resp"],   # 监护仪优先，呼吸机兜底
                "sbp": ["param_ibp_s", "param_nibp_s"],
            }
            for key, codes in bedside_codes.items():
                for doc in db.bedside.find(
                    {"pid": pid, "code": {"$in": codes}, "time": {"$gte": start_dt, "$lte": end_dt}},
                    {"code": 1, "strVal": 1, "time": 1},
                ).sort("time", -1).max_time_ms(10000).limit(100):
                    val = _safe_float(doc.get("strVal"))
                    if val is None:
                        continue
                    t = doc.get("time")
                    if key == "gcs" and sofa_data["gcs"] is None:
                        sofa_data["gcs"] = int(val); sitems["gcs"] = {"val": int(val), "time": t}
                    elif key == "map" and sofa_data["map"] is None:
                        sofa_data["map"] = round(val, 1); sofa_data["map_time"] = t
                        sitems["map"] = {"val": round(val, 1), "time": t}
                    elif key == "urine" and "urine" not in sitems:
                        sitems["urine"] = {"val": round(val, 1), "time": t}
                    elif key == "rr" and sofa_data["rr"] is None:
                        sofa_data["rr"] = int(val)
                    elif key == "sbp" and sofa_data["sbp"] is None:
                        sofa_data["sbp"] = int(val)
                    break  # 每类取最新一条有效值

            # 机械通气判断
            for _doc in db.bedside.find(
                {"pid": pid, "code": {"$in": ["param_vent_resp", "param_vent_peep"]},
                 "time": {"$gte": start_dt, "$lte": end_dt}},
                {"_id": 0, "code": 1}).max_time_ms(5000).limit(1):
                sofa_data["on_ventilator"] = True
                break

            # 升压药：drugList.name 嵌套；按 before 时刻重建在泵状态；换算 µg/kg/min
            VASO_KW = ["去甲", "肾上腺素", "多巴胺", "多巴酚丁胺"]
            if "drugExe" in db.list_collection_names():
                for doc in db.drugExe.find(
                    {"pid": pid, "drugList.name": {"$regex": "去甲|肾上腺素|多巴胺|多巴酚丁胺"}},
                    {"drugList": 1, "drugActionList": 1, "weight": 1}
                ).max_time_ms(10000).limit(200):
                    action, speed_mlh = _effective_action(doc.get("drugActionList") or [], end_dt)
                    if action is None or speed_mlh <= 0:
                        continue  # 该时刻不在泵
                    for drug in doc.get("drugList") or []:
                        name = drug.get("name", "")
                        if not any(kw in name for kw in VASO_KW):
                            continue
                        rate = _vaso_rate_ugkgmin(drug, doc, weight, speed_mlh)
                        sofa_data["vasopressors"].append({
                            "drug": name, "rate_ugkgmin": rate,
                            "speed_mlh": speed_mlh, "action": action,
                        })

            # 检验：VI_ICU_EXAM_ITEM 直接按 hisPid 查
            try:
                db_dc = get_datacenter_db()
                hp = str(his_pid)
                LAB = {"coag_plt": ["PLT"], "liver_tbil": ["TBIL"],
                       "renal_crea": ["sCr", "Cr", "CREA"]}
                for item_key, codes in LAB.items():
                    r = db_dc["VI_ICU_EXAM_ITEM"].find_one(
                        {"hisPid": hp, "itemCode": {"$in": codes}},
                        sort=[("authTime", -1)],
                        projection={"result": 1, "unit": 1, "itemName": 1, "authTime": 1})
                    if not r:
                        continue
                    raw = _safe_float(r.get("result"))
                    if raw is None:
                        continue
                    unit = (r.get("unit") or "").lower().replace("μ", "u")
                    t = r.get("authTime")
                    if item_key == "liver_tbil":
                        val = round(raw / 17.1, 2) if "umol" in unit else round(raw, 2)
                        sitems["liver_tbil"] = {"val": val, "raw": raw, "unit": unit, "time": t}
                    elif item_key == "renal_crea":
                        val = round(raw / 88.4, 2) if "umol" in unit else round(raw, 2)
                        sitems["renal_crea"] = {"val": val, "raw": raw, "unit": unit, "time": t}
                    else:
                        sitems["coag_plt"] = {"val": raw, "unit": unit, "time": t}
            except Exception:
                pass

            break  # 找到数据库就退出
        except Exception:
            continue

    # SOFA 评分
    pf_val = sitems.get("pf", {}).get("val")
    s = {
        "resp": _s_resp(pf_val, sofa_data["on_ventilator"]),
        "coag": _s_coag(sitems.get("coag_plt", {}).get("val")),
        "liver": _s_liver(sitems.get("liver_tbil", {}).get("val")),
        "cns": _s_cns(sofa_data["gcs"]),
        "renal": _s_renal(sitems.get("renal_crea", {}).get("val"), sitems.get("urine", {}).get("val")),
        "cardio": _sofa_cardio(sofa_data["map"], sofa_data["vasopressors"]),
    }

    # 缺失域追踪：缺失域不计分，sofa_current 是真实值的下界
    measured = {
        "resp": pf_val is not None,
        "coag": sitems.get("coag_plt", {}).get("val") is not None,
        "liver": sitems.get("liver_tbil", {}).get("val") is not None,
        "cns": sofa_data["gcs"] is not None,
        "renal": (sitems.get("renal_crea", {}).get("val") is not None
                  or sitems.get("urine", {}).get("val") is not None),
        "cardio": sofa_data["map"] is not None or bool(sofa_data["vasopressors"]),
    }
    missing_domains = [k for k, ok in measured.items() if not ok]
    sofa_data["measured"] = measured
    sofa_data["missing_domains"] = missing_domains
    sofa_data["sofa_is_lower_bound"] = bool(missing_domains)
    sofa_data["sofa_breakdown"] = s
    sofa_data["sofa_current"] = sum(v for k, v in s.items() if measured[k])

    # qSOFA
    q = 0
    if sofa_data["rr"] is not None and sofa_data["rr"] >= 22: q += 1
    if sofa_data["sbp"] is not None and sofa_data["sbp"] <= 100: q += 1
    if sofa_data["gcs"] is not None and sofa_data["gcs"] < 15: q += 1
    sofa_data["qsofa"] = q

    # T0（仅用已测域）
    t0_items = [
        {"key": "resp", "score": s["resp"], "time": sitems.get("pf", {}).get("time")},
        {"key": "coag", "score": s["coag"], "time": sitems.get("coag_plt", {}).get("time")},
        {"key": "liver", "score": s["liver"], "time": sitems.get("liver_tbil", {}).get("time")},
        {"key": "cns", "score": s["cns"], "time": sitems.get("gcs", {}).get("time")},
        {"key": "renal", "score": s["renal"],
         "time": sitems.get("renal_crea", {}).get("time") or sitems.get("urine", {}).get("time")},
        {"key": "cardio", "score": s["cardio"], "time": sofa_data["map_time"]},
    ]
    t0_items = [it for it in t0_items if measured.get(it["key"], True)]
    sofa_data["t0"], sofa_data["t0_basis"] = compute_sofa_t0(t0_items, sofa_data["sofa_baseline"])

    return sofa_data


def compute_sofa_shadow(observations, medications, eval_time, weight_kg=None):
    """
    使用新评分模块计算经典 SOFA + SOFA-2，用于影子比对。

    Args:
        observations: 观测数据 [{code, value_number, unit, observed_at}]
        medications: 用药数据 [{med_name, route, dose_ugkgmin, admin_end}]
        eval_time: 评估时间 (timezone-aware)
        weight_kg: 体重

    Returns:
        {
            "classic": compute_sofa_classic 结果,
            "sofa2": compute_sofa2 结果,
        }
    """
    from scoring.sofa_core import compute_sofa_classic
    from scoring.sofa2_core import compute_sofa2

    classic = compute_sofa_classic(
        observations, medications, eval_time,
        has_advanced_support=False, weight_kg=weight_kg,
    )
    sofa2 = compute_sofa2(
        observations, medications, eval_time,
        has_advanced_support=False, weight_kg=weight_kg,
    )
    return {"classic": classic, "sofa2": sofa2}


def get_infection_evidence(pid, his_pid, before=None):
    """四路合并：诊断 + 抗菌药 + 病原学 + 炎症指标。"""
    from db import BED_DB_NAMES, get_client
    from bson import ObjectId

    evidence = {"diagnosis": [], "antibiotics": [], "culture": [], "inflammation": {}}
    ABX_KW = ("头孢|青霉|培南|万古|利奈唑|替加环素|哌拉西林|舒巴坦|他唑巴坦|"
              "沙星|霉素|唑烷酮|氟康|伏立康|卡泊芬净|阿奇|克林|甲硝唑|抗")

    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]

            pat = db.patient.find_one({"_id": ObjectId(pid)})
            if pat:
                for key in ("clinicalDiagnosis", "diagnosis", "admissionDiagnosis"):
                    val = pat.get(key)
                    if val:
                        evidence["diagnosis"].append(str(val))
                for item in pat.get("diagnosisHistoryList") or []:
                    if isinstance(item, dict):
                        d = item.get("diagnosis") or item.get("name")
                        if d:
                            evidence["diagnosis"].append(str(d))

            if "drugExe" in db.list_collection_names():
                for doc in db.drugExe.find(
                    {"pid": pid, "drugList.name": {"$regex": ABX_KW}},
                    {"drugList": 1, "startTime": 1}
                ).max_time_ms(10000).limit(50):
                    for d in doc.get("drugList") or []:
                        nm = d.get("name", "")
                        if nm:
                            evidence["antibiotics"].append(nm)

            break
        except Exception:
            continue

    return evidence


def _build_septic_shock_prompt(d):
    """d = extract_sofa_qsofa() 的输出"""
    import json
    missing = d.get("missing_domains") or []
    payload = {
        "感染证据": d.get("infection_evidence"),
        "SOFA基线": d.get("sofa_baseline", 0),
        "SOFA各项(含采样时间)": d.get("sofa_items"),
        "SOFA当前总分(仅已测域)": d.get("sofa_current"),
        "SOFA是否下界": d.get("sofa_is_lower_bound", False),
        "缺失域(missing_domains)": missing if missing else "无，六域齐全",
        "qSOFA": {"RR": d.get("rr"), "on_ventilator": d.get("on_ventilator"),
                  "SBP": d.get("sbp"), "GCS": d.get("gcs"), "score": d.get("qsofa")},
        "升压药(当前泵注)": d.get("vasopressors"),
        "乳酸": {"value": d.get("lactate"), "time": str(d.get("lactate_time", ""))},
        "MAP": {"value": d.get("map"), "time": str(d.get("map_time", ""))},
        "液体复苏": d.get("fluid_resuscitation", "unknown"),
    }
    note = ""
    if missing:
        note = (f"\n\n⚠️ 注意：以下域缺失数据，SOFA 总分 {d.get('sofa_current')} 仅为下界，"
                f"真实值可能更高。缺失域：{missing}。请在 confidence 中反映这一不确定性。")
    return ("请依据以下该患者的结构化指标数据判定，并严格按系统提示的 JSON 格式输出：\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, default=str) + note)


def _rule_confirm_septic_shock(data):
    """
    规则初判：决定性场景直接出结论，边界交 AI。
    缺失域守卫：delta<2 且有缺失域时不能判"非脓毒症"（真实值可能更高）。
    """
    infection = _has_infection_evidence(data.get("infection_evidence"))
    base = data.get("sofa_baseline", 0) or 0
    cur = data.get("sofa_current")
    delta = None if cur is None else cur - base
    vasos = data.get("vasopressors") or []
    on_vaso = len(vasos) > 0
    lac = data.get("lactate")
    fluid = data.get("fluid_resuscitation", "unknown")
    missing = data.get("missing_domains") or []
    is_lower_bound = bool(data.get("sofa_is_lower_bound"))

    # is_sepsis：已测域下界≥2 → 脓毒症成立（缺失域只会更高，向上确认安全）
    is_sepsis = bool(infection and delta is not None and delta >= 2)

    res = {
        "source": "rule", "is_sepsis": is_sepsis,
        "sofa_baseline": base, "sofa_current": cur, "sofa_delta": delta,
        "sofa_breakdown": data.get("sofa_breakdown"), "qsofa": data.get("qsofa"),
        "lactate": lac, "on_vasopressor": on_vaso, "adequate_fluid": fluid,
        "t0": data.get("t0"), "t0_basis": data.get("t0_basis"),
        "missing_domains": missing, "sofa_is_lower_bound": is_lower_bound,
    }

    # A. 六域齐全 + delta<2 → 明确非脓毒症
    if infection and delta is not None and delta < 2 and not missing:
        res.update(confirm=False, is_septic_shock=False, decisive=True, confidence=0.9,
                   reason=f"SOFA 六域齐全，急升 {delta} 分(<2)，不构成脓毒症。")
        return res
    # A'. delta<2 但有缺失域 → 不能定，交 AI
    if infection and delta is not None and delta < 2 and missing:
        res.update(decisive=False, confidence=0.3,
                   reason=f"已测域急升 {delta} 分，但 {missing} 缺失，真实值可能更高，交 AI 结合病程判定。")
        return res
    # B. 脓毒症(下界≥2) + 在泵升压药 + 乳酸>2 + 已充分复苏 → 明确脓毒性休克
    if is_sepsis and on_vaso and lac is not None and lac > 2 and fluid == "yes":
        res.update(confirm=True, is_septic_shock=True, decisive=True, confidence=0.95,
                   reason=(f"感染 + SOFA 急升 {delta} 分构成脓毒症；充分复苏后仍需升压药"
                           f"维持 MAP≥65 且乳酸 {lac}>2 mmol/L，符合脓毒性休克。"))
        return res
    # B'. 脓毒症 + 在泵升压药 + 乳酸>2，但复苏状态未知 → 交 AI
    if is_sepsis and on_vaso and lac is not None and lac > 2 and fluid != "yes":
        res.update(decisive=False,
                   reason="疑似脓毒性休克，但液体复苏是否充分未记录，交 AI 结合病程判定。")
        return res
    # C. 脓毒症成立、乳酸明确≤2 且未用升压药 → 明确非休克
    if is_sepsis and (not on_vaso) and lac is not None and lac <= 2:
        res.update(confirm=False, is_septic_shock=False, decisive=True, confidence=0.85,
                   reason=f"构成脓毒症(SOFA急升{delta})，但未用升压药且乳酸{lac}≤2，非脓毒性休克。")
        return res
    # D. 其余(乳酸缺失/复苏未知/剂量边界/缺失域) → 交 AI
    res.update(decisive=False)
    return res


def _fallback_septic_shock(data):
    """AI 不可用时的兜底判定。"""
    base = data.get("sofa_baseline", 0) or 0
    cur = data.get("sofa_current")
    delta = None if cur is None else cur - base
    vasos = data.get("vasopressors") or []
    lac = data.get("lactate")
    is_sepsis = bool(data.get("infection_evidence") and delta is not None and delta >= 2)
    is_shock = bool(is_sepsis and vasos and lac is not None and lac > 2)
    return {
        "source": "fallback", "confirm": is_shock,
        "is_sepsis": is_sepsis, "is_septic_shock": is_shock,
        "sofa_baseline": base, "sofa_current": cur, "sofa_delta": delta,
        "sofa_breakdown": data.get("sofa_breakdown"), "qsofa": data.get("qsofa"),
        "lactate": lac, "on_vasopressor": bool(vasos),
        "adequate_fluid": data.get("fluid_resuscitation", "unknown"),
        "t0": data.get("t0"), "t0_basis": data.get("t0_basis"),
        "confidence": 0.4, "low_confidence": True, "needs_review": True,
        "missing_domains": data.get("missing_domains") or [],
        "sofa_is_lower_bound": bool(data.get("sofa_is_lower_bound")),
        "reason": "AI 判定不可用，按规则兜底，建议人工复核。",
    }


def _normalize_septic_shock_result(res, data):
    """AI 结果校验：客观指标以本地值为准，逻辑一致性兜底。缺失域强制低置信。"""
    if not isinstance(res, dict):
        return _fallback_septic_shock(data)
    base = data.get("sofa_baseline", 0) or 0
    cur = data.get("sofa_current")
    missing = data.get("missing_domains") or []
    res["sofa_baseline"] = base
    res["sofa_current"] = cur
    res["sofa_delta"] = None if cur is None else cur - base
    res["missing_domains"] = missing
    res["sofa_is_lower_bound"] = bool(missing)
    res.setdefault("sofa_breakdown", data.get("sofa_breakdown"))
    res.setdefault("qsofa", data.get("qsofa"))
    res.setdefault("lactate", data.get("lactate"))
    res.setdefault("on_vasopressor", bool(data.get("vasopressors")))
    res.setdefault("t0", data.get("t0"))
    res.setdefault("t0_basis", data.get("t0_basis"))
    if res.get("is_septic_shock"):
        res["is_sepsis"] = True
    res["confirm"] = bool(res.get("is_septic_shock"))
    try:
        res["confidence"] = float(res.get("confidence", 0.5))
    except (TypeError, ValueError):
        res["confidence"] = 0.5
    # 缺失域一律低置信（数据不全不给高置信）
    if missing:
        res["confidence"] = min(res["confidence"], 0.5)
        res["low_confidence"] = True
    else:
        res["low_confidence"] = res["confidence"] < AI_CONF_THRESHOLD
    res.setdefault("source", "ai")
    return res


def classify_septic_shock_with_ai(disease_id, his_pid, data):
    """优先级：人工覆盖/缓存 > 规则决定性 > AI > 兜底。"""
    cached = get_ai_cache(his_pid, task="septic_shock_confirm", key=disease_id)
    if cached:
        return cached  # 覆盖(source=override)与普通缓存都在此命中

    # 合并感染证据（否则规则层 infection 永远 False）
    if data.get("infection_evidence") is None:
        data["infection_evidence"] = get_infection_evidence(
            data["pid"], his_pid, before=data.get("t0"))

    rule = _rule_confirm_septic_shock(data)
    if rule.get("decisive"):
        rule["low_confidence"] = rule.get("confidence", 1.0) < AI_CONF_THRESHOLD
        set_ai_cache(his_pid, "septic_shock_confirm", rule, "rule", key=disease_id)
        return rule

    try:
        with _AI_SEMAPHORE:
            raw = call_llm_json_with_system(
                SEPTIC_SHOCK_CONFIRM_SYSTEM_PROMPT,
                _build_septic_shock_prompt(data),
                max_tokens=1200)
        res = _normalize_septic_shock_result(parse_llm_json(raw), data)
    except Exception as e:
        res = _fallback_septic_shock(data)
        res["error"] = str(e)

    set_ai_cache(his_pid, "septic_shock_confirm", res, "ai", key=disease_id)
    return res


def override_septic_shock_decision(his_pid, disease_id, decision, operator=None, note=None):
    """人工覆盖脓毒性休克判定。"""
    payload = {
        "is_septic_shock": bool(decision.get("is_septic_shock")),
        "is_sepsis": bool(decision.get("is_sepsis", decision.get("is_septic_shock"))),
        "confirm": bool(decision.get("is_septic_shock")),
        "reason": decision.get("reason", "人工复核判定"),
        "operator": operator, "note": note,
        "source": "override", "confidence": 1.0, "low_confidence": False,
    }
    return override_ai_decision(
        his_pid, task="septic_shock_confirm", key=disease_id,
        result=payload, overridden_by=operator or "主任"
    )
