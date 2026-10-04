"""定义承诺治理平台允许的承诺类别、文档类别与调整类别。"""

from __future__ import annotations

# 七类分列的承诺：各方投入、受益群体、属地化目标、知识与数据权属、
# 资金托管、风险保障、退出责任。
COMMITMENT_TYPES = frozenset({
    "party_input",            # 各方投入：技术方平台与培训、当地机构市场与人才等
    "beneficiary_group",      # 受益群体：受益对象、人数与分配口径
    "localization_target",    # 属地化目标：属地能力形成节点
    "knowledge_data_rights",  # 知识与数据权属
    "escrow_funding",         # 资金托管
    "risk_assurance",         # 风险保障
    "exit_responsibility",    # 退出责任
})

# 四类记录严格分离：谈判稿、正式承诺、履约证据、争议结论。
DOCUMENT_KINDS = frozenset({
    "negotiation_draft",  # 谈判稿
    "sealed_terms",       # 正式承诺（封存条款）
})

ADJUSTMENT_KINDS = frozenset({
    "late_evidence",       # 证据迟到
    "partial_breach",      # 局部违约
    "party_replacement",   # 合作方替换
    "scope_reduction",     # 范围缩减
    "dispute_relief",      # 争议减免
})

# 合作方在项目中的分工。
PARTY_ROLES = frozenset({
    "technology_provider",  # 技术方
    "investor",             # 投资方
    "local_agency",         # 当地机构
    "office",               # 合作项目办公室
    "independent",          # 独立复核方
})


def is_commitment_type(value: str) -> bool:
    return value in COMMITMENT_TYPES
