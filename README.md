# 治理中非数字合作承诺协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/digital_trade_foundation/governance/：跨境合作承诺治理模块，覆盖谈判、正式承诺、履约核验到退出的全周期；
- tests/：基础规则、事务边界、接口路由、治理规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance
    PYTHONPATH=src python3 -m digital_trade_foundation.governance.acceptance

基础验收在临时 SQLite 数据库中登记合作机构、操作者、业务节点和参考资料，核对幂等回执与审计链；治理验收走完一条完整链路（协议登记、条件组核验、分期拨付、重复回调防护、成果唯一认领、违约整改、范围缩减、合作方替换、监督对账、历史还原和服务重启续跑）。两者成功时都输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## 承诺治理模块

治理模块把跨境合作从谈判一直管到退出：

- 七类承诺分别登记：各方投入、受益群体、属地化目标、知识与数据权属、资金托管、风险保障、退出责任；
- 四类文书相互区分：谈判稿、正式承诺、履约证据、争议结论；
- 互相依赖的条件登记为一组，证据齐备且经独立复核后整组核验，阶段承诺才生效或触发分期拨付；属地能力未核验前资金只能停留在托管；
- 证据迟到会被标记但责任保留；局部违约、合作方替换、范围缩减只影响尚未兑现的部分，已兑现成果与历史责任保持原样；
- 共享成果按成果键全局唯一认领，不能被多个项目重复计入；
- 参与方按职责受限：责任方提交证据与履约，独立复核人（与提交方不同组织）复核，投资方注资，办公室管理结构性变更，监督角色对账；
- 所有写接口幂等，重复回调不会再次改变承诺状态或托管余额；
- 未决复核与整改期限持久化在 SQLite，服务重启后按原顺序继续处理；
- 监督人员可随时对账（托管余额、已拨金额、未履行承诺），并可还原任一历史时点每项成果归谁、哪方仍负有责任。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m digital_trade_foundation.governance.api --database governance.sqlite3 --host 127.0.0.1 --port 8081

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。治理模块的接口位于 /governance 前缀下，与基础接口共用同一套身份、幂等与审计规则；governance.api 启动的服务同时承载基础路由与治理路由。
