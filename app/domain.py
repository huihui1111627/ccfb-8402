"""领域常量：变更类型、状态、错误分类、指纹。

指纹是幂等的核心：同一企业、同一险种、同一人、同一变更类型、同一生效月份，
在业务上只应申报一次。指纹被用作数据库唯一 claim 键。
"""
from __future__ import annotations

import hashlib

WINDOWS = {
    "si": {"name": "社会保险", "short": "社保"},
    "hf": {"name": "住房公积金", "short": "公积金"},
}

ACTIONS = {
    "add": "增员",
    "remove": "减员",
    "adjust": "基数调整",
}

# 任务状态机
STATUS_LABELS = {
    "pending": "待执行",
    "queued": "排队中",
    "running": "执行中",
    "retry": "待重试",
    "needs_fix": "待修正",
    "success": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "duplicate": "重复（已存在提交）",
    "unknown": "状态未知",
}

ACTIVE_STATUSES = ("pending", "queued", "running", "retry", "unknown")

# 站点校验报错归类。真实站点文案千变万化，归类后只影响「怎么修」，
# 不影响幂等：任何需要重新提交的修正都复用同一 claim。
ERROR_CATEGORIES = {
    "id_card_invalid": {
        "label": "证件号校验不通过",
        "hint": "核对证件号/姓名/出生日期是否一致，修正后重跑。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "name_mismatch": {
        "label": "姓名与证件不符",
        "hint": "核对姓名是否有生僻字或空格，修正花名册。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "base_out_of_range": {
        "label": "缴费基数超出上下限",
        "hint": "按当年社平工资上下限调整基数，或确认生效月份。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "already_exists": {
        "label": "该人员已在册",
        "hint": "重复增员。确认是否历史已申报成功；若属实，标记跳过。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "not_found": {
        "label": "在册人员不存在",
        "hint": "减员/调基对象不在册，核对社保编号或先完成增员。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "account_mismatch": {
        "label": "单位账号与人员不匹配",
        "hint": "企业账号配置或人员归属有误。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "session_expired": {
        "label": "会话过期",
        "hint": "系统自动重新登录并续跑，无需人工处理。",
        "retryable_after_fix": True,
        "auto_retry": True,
    },
    "invalid_field": {
        "label": "表单字段校验失败",
        "hint": "查看截图定位字段。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "rate_limited": {
        "label": "站点限流",
        "hint": "自动退避重排，无需人工处理。",
        "retryable_after_fix": True,
        "auto_retry": True,
    },
    "submit_unknown": {
        "label": "提交结果未知",
        "hint": "站点超时/网络中断。系统会用幂等键查询确认后决定是否重提。",
        "retryable_after_fix": True,
        "auto_retry": True,
    },
    "auth_failed": {
        "label": "登录失败",
        "hint": "账号密码或短信验证码异常，检查企业账号配置。",
        "retryable_after_fix": False,
        "auto_retry": False,
    },
    "quota_exceeded": {
        "label": "超出企业月配额",
        "hint": "在「企业账号」调整该客户月配额，或等待下月执行。",
        "retryable_after_fix": True,
        "auto_retry": False,
    },
    "site_error": {
        "label": "站点内部错误",
        "hint": "站点 5xx，稍后自动重试。",
        "retryable_after_fix": True,
        "auto_retry": True,
    },
}

# 花名册列名别名（兼容各种 HR 导出模板）
COLUMN_ALIASES = {
    "enterprise_code": ["企业编码", "企业编号", "客户编码", "单位编号", "enterprise_code"],
    "enterprise_name": ["企业名称", "客户名称", "单位名称", "enterprise_name"],
    "employee_name": ["姓名", "员工姓名", "employee_name", "name"],
    "id_card": ["证件号码", "身份证号", "身份证号码", "证件号", "id_card"],
    "social_no": ["社保编号", "个人编号", "社保号", "social_no"],
    "action": ["变更类型", "业务类型", "申报类型", "action"],
    "window": ["险种", "业务", "申报业务", "window"],
    "base_before": ["原基数", "调整前基数", "base_before"],
    "base_after": ["新基数", "调整后基数", "申报基数", "base_after"],
    "effective_month": ["生效月份", "所属期", "缴费月份", "effective_month"],
    "phone": ["手机号", "联系电话", "phone"],
    "remark": ["备注", "remark"],
}

ACTION_SYNONYMS = {
    "增员": "add", "新增": "add", "参保": "add", "新增参保": "add", "add": "add",
    "减员": "remove", "停保": "remove", "减少": "remove", "remove": "remove",
    "基数调整": "adjust", "调基": "adjust", "基数变更": "adjust",
    "adjust": "adjust", "调整": "adjust",
}

WINDOW_SYNONYMS = {
    "社保": "si", "社会保险": "si", "养老": "si", "si": "si",
    "公积金": "hf", "住房公积金": "hf", "hf": "hf",
}


def normalize_action(raw: str) -> str | None:
    if not raw:
        return None
    return ACTION_SYNONYMS.get(str(raw).strip())


def normalize_window(raw: str) -> str | None:
    if not raw:
        return None
    return WINDOW_SYNONYMS.get(str(raw).strip())


def fingerprint(enterprise_code: str, window: str, id_card: str, action: str,
                effective_month: str) -> str:
    """业务幂等键。

    注意：刻意不含基数金额。同一人同月对同一险种的「基数调整」如果先后提交两次
    不同金额，也属于重复申报事故——第二次必须人工确认后走更正流程，而不是自动再提交。
    """
    norm = "|".join([
        enterprise_code.strip().upper(),
        window.lower(),
        id_card.strip().upper(),
        action.lower(),
        effective_month.strip(),
    ])
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]
