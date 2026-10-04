# 中非数字合作承诺治理平台

本项目在跨境数字贸易合作的共享服务端基础能力之上，提供一套把合作**从谈判一直管到退出**的承诺治理平台：分别保存各方投入、受益群体、属地化目标、知识与数据权属、资金托管、风险保障与退出责任，并把谈判稿、正式承诺、履约证据与争议结论严格分开。

## 治理规则

- **七类承诺分列**：`party_input`（各方投入）、`beneficiary_group`（受益群体）、`localization_target`（属地化目标）、`knowledge_data_rights`（知识与数据权属）、`escrow_funding`（资金托管）、`risk_assurance`（风险保障）、`exit_responsibility`（退出责任）。
- **四类记录分离**：谈判稿（`negotiation_draft`，可多版）、正式承诺（`sealed_terms`，封存后不可改）、履约证据（独立复核）、争议结论（独立成档）。
- **条件组成组核验**：相互依赖的核验条件挂在同一阶段，只有全部证据齐备并经**独立复核机构**接受，阶段才生效；阶段生效后才触发拨付，属地能力形成前不释放资金。
- **责任不可抹除**：证据迟到、局部违约只追加留痕；合作方替换、范围缩减、争议减免只能改变**尚未兑现**的部分，已履行金额与原始责任方始终保留。
- **共享成果唯一认领**：一项成果只能归属一个项目，跨项目重复认领直接拒绝，重复回调幂等。
- **资金托管对账**：上存总额、已拨金额、托管余额与未履行承诺随时对齐；重复支付回调不会再次改变余额。
- **期限与顺序持久化**：复核期限与整改期限以绝对时间戳入库，服务中断重启后按原定期限、原顺序继续。
- **角色与可见范围**：办公室（operator/office）编排与拨付、参与方提交本方材料、独立复核所（reviewer/independent）裁定、监督人员（auditor）只读全部；被替换/退出方保留历史可读。
- **历史时点还原**：关键实体只增快照，可还原任意时点每项成果归谁、哪方仍负责。

## 目录

- src/digital_trade_foundation/：基础模型、SQLite 存储、权限/幂等/审计，以及承诺治理服务、HTTP 路由和离线验收。
- tests/：基础规则与承诺治理的单元、接口、恢复和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

基础服务：

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance

承诺治理全链路：

    PYTHONPATH=src python3 -m digital_trade_foundation.commitment_acceptance

验收命令在临时 SQLite 数据库中演练建档、七类承诺、谈判稿封存、条件组独立复核、
分期资金在能力形成后拨付、重复回调不二次扣款、迟到证据与违约留痕、合作方替换、
成果唯一认领、争议结论、托管对账与历史时点还原，成功时输出 `status` 为 `ok` 的
JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database governance.sqlite3 --host 127.0.0.1 --port 8080

写入与读取接口均通过 `X-Actor-Id` 标识操作者。主要路由：

| 动作 | 方法与路径 |
| --- | --- |
| 建项目 / 查项目 / 关闭项目 | `POST /projects`、`GET /projects/{id}`、`POST /projects/{id}/close` |
| 参与方登记 / 替换 | `POST /projects/{id}/parties`、`POST /projects/{id}/parties/replacements` |
| 承诺起草 / 改稿 / 封存 | `POST /commitments`、`POST /commitments/{id}/drafts`、`POST /commitments/{id}/seal` |
| 阶段与条件 | `POST /commitments/{id}/stages`、`POST /stages/{id}/conditions` |
| 证据与独立复核 | `POST /conditions/{id}/evidences`、`POST /evidences/{id}/review` |
| 阶段生效 / 拨付 | `POST /stages/{id}/effect`、`POST /stages/{id}/disburse` |
| 托管入金 / 余额 / 对账 | `POST /projects/{id}/escrow/deposits`、`GET /projects/{id}/escrow`、`GET /projects/{id}/reconcile` |
| 违约 / 缩范围 / 责任视图 | `POST /commitments/{id}/partial-breach`、`POST /commitments/{id}/scope-reduction`、`GET /commitments/{id}/responsibility` |
| 争议提出 / 结论 | `POST /projects/{id}/disputes`、`POST /disputes/{id}/conclude` |
| 成果登记 / 认领 | `POST /outcomes`、`POST /outcomes/{id}/claims` |
| 成果归属 / 未决复核 / 历史还原 | `GET /projects/{id}/outcome-assignments`、`GET /projects/{id}/pending-reviews`、`GET /history` |

所有写接口要求 `request_id` 以保证幂等；未满足前置条件返回 412 并附带待满足项，
状态不允许推进返回 422，越权返回 403，重复认领返回 409。健康检查使用 `GET /health`。
