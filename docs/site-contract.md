# 站点流程与错误分类对照

## 1. 登录

```
GET  /mock/{window}/login
POST /mock/{window}/login  (step=credential, account, password)
     → 校验账号密码，向预留手机发验证码，页面出现验证码输入框
GET  /mock/sms/api/latest?phone=&purpose=login&since_id=
     → RPA 轮询取码；since_id 只接受本次请求之后的短信
POST /mock/{window}/login  (step=sms, account, password, code, challenge)
     → 成功：Set-Cookie mock_{window}_sid，303 到首页
     → 失败：400 + #error-box
```

验证码一次性消费，5 分钟有效。会话 10 分钟无操作过期。

## 2. 申报表单（3 步）

| 步骤 | 页面元素 | 动作 |
|---|---|---|
| 1 填写 | `#employee_name` `#id_card` `#social_no`(减员/调基) `#base_after`(增/调) `#effective_month` `#contact_phone` | 点 `#btn-next` → 服务端校验 → 2 |
| 2 确认 | 只读展示 | 点 `#btn-next` → 303 到 step=3 |
| 3 提交 | `#btn-submit-final` | 点提交 → 回执页 `#receipt-no` |

失败时页面有 `#error-box`，带 `data-error-code` 与中文文案；文案前缀是 `【EXXX】`。

第 1 步的校验同时写草稿（`mock_drafts`），第 2 步更新草稿，第 3 步提交时按草稿与表单值再次校验。

## 3. 查询接口

```
GET /mock/{window}/query?fingerprint=...   （需登录 Cookie）
    → {"found": true, "receipt_no": ..., "action": ..., "effective_month": ...}
    → {"found": false}
    → 401 {"error": "session_expired"}
```

只读，供 RPA 在「提交结果未知」时核对。查询参数也支持 `receipt_no`。

## 4. 错误码与归类

| 站点码 | 场景 | 归类 | 处置 |
|---|---|---|---|
| E102 | 生效月份格式错 | `invalid_field` | 待修正 |
| E103/E104 | 基数缺失/非数字 | `invalid_field` | 待修正 |
| E105 | 调基后基数与原基数相同 | `invalid_field` | 待修正 |
| E201 | 证件号格式错 | `id_card_invalid` | 待修正 |
| E203 | 社保编号与证件不匹配 | `account_mismatch` | 待修正 |
| E205 | 姓名与证件不符 | `name_mismatch` | 待修正 |
| E206 | 人员库无此证件 | `not_found` | 待修正（先做信息采集） |
| E301 | 基数超上下限 | `base_out_of_range` | 待修正 |
| E401 | 已在参保状态，重复增员 | `already_exists` | 待修正，可核实后跳过 |
| E402/E403 | 减员/调基对象不在册 | `not_found` | 待修正 |
| E500 | 访问过多（HTTP 429） | `rate_limited` | 自动退避重排 |
| E900 | 站点繁忙（HTTP 500） | `site_error` | 自动退避重排 |
| — | 跳登录页 | `session_expired` | 原地重登重放 |
| — | 提交后无响应/无回执页 | `submit_unknown` | 只读核对 → 采纳或重排 |
| — | 账号密码错 | `auth_failed` | 失败，需运维 |

`app/domain.py` 的 `ERROR_CATEGORIES` 是唯一事实来源，`app/orchestrator.classify_error` 负责把文案映射到分类。

## 5. 故障注入开关（`/mock/admin`）

| 键 | 含义 |
|---|---|
| `session_ttl_seconds` | 会话有效期，默认 600；测试可设 1 |
| `expire_on_step` | 下一次「第 N 步」表单提交后强制使会话失效（然后自动清零） |
| `fail_next_submit` | 下一次提交返回 500，**未落库** |
| `drop_next_submit_response` | 下一次提交**已落库**但返回 504（模拟响应丢失） |
| `rate_limit_per_min` | 窗口内已受理笔数上限，超出返回 429 |
| `rate_limit_window_seconds` | 限流窗口，默认 60 |
| `maintenance` | 维护模式，申报页直接拒绝 |

这几个开关专门用来验证 RPA 的异常处理：`fail_next_submit` 应触发重试并最终成功一次；`drop_next_submit_response` 应触发核对并采纳，**不能**触发第二次提交。

## 6. 选择器约定

驱动（`app/flow.py`）只依赖以下稳定契约：

- 登录：`#account` `#password` `#btn-send` `#code` `#btn-submit`
- 表单：`#employee_name` `#id_card` `#social_no` `#base_after` `#effective_month` `#contact_phone` `#btn-next` `#btn-back` `#btn-submit-final`
- 错误：`#error-box[data-error-code]`
- 回执：`#receipt-no`

对接真实站点时，把这几个选择器与错误文案映射替换掉即可，引擎其余逻辑不变。
