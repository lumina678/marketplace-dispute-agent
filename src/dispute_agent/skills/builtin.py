from __future__ import annotations

from dispute_agent.dispute_types import DisputeType
from dispute_agent.skills.base import (
    ClaimTypeDefinition,
    InvestigationStepDefinition,
    ManifestDisputeSkill,
    QuestionRuleDefinition,
    SkillEvidenceRequirement,
    SkillGuardProfile,
    SkillManifest,
    SkillPolicyScope,
)


DESCRIPTION_MISMATCH_CONTEXT = (
    "case_state",
    "transaction",
    "listing_snapshot",
    "conversation_snapshot",
    "shipment_timeline",
    "claims",
    "evidence",
    "policy_version",
    "open_questions",
)

MISSING_PARTS_CONTEXT = DESCRIPTION_MISMATCH_CONTEXT

EMPTY_PACKAGE_CONTEXT = (
    "case_state",
    "transaction",
    "conversation_snapshot",
    "shipment_timeline",
    "claims",
    "evidence",
    "policy_version",
    "open_questions",
)

SHIPPING_DAMAGE_CONTEXT = EMPTY_PACKAGE_CONTEXT

DESCRIPTION_MISMATCH_TOOLS = (
    "case.get_state",
    "transaction.get",
    "listing.get_snapshot",
    "conversation.search",
    "shipment.get_timeline",
    "evidence.list",
    "evidence.inspect",
    "policy.search",
    "case.add_open_question",
    "resolution.create_draft",
)

MISSING_PARTS_TOOLS = DESCRIPTION_MISMATCH_TOOLS

EMPTY_PACKAGE_TOOLS = tuple(
    tool_name for tool_name in DESCRIPTION_MISMATCH_TOOLS if tool_name != "listing.get_snapshot"
)

SHIPPING_DAMAGE_TOOLS = EMPTY_PACKAGE_TOOLS

COMMON_OUTCOMES = (
    "RETURN_AND_FULL_REFUND",
    "PARTIAL_REFUND",
    "REJECT_CLAIM",
    "REQUEST_MORE_EVIDENCE",
    "RELEASE_FUNDS",
    "ESCALATE_TO_HUMAN",
)


def _guard(*conditions: str) -> SkillGuardProfile:
    return SkillGuardProfile(
        allowed_outcomes=COMMON_OUTCOMES,
        mandatory_escalation_conditions=conditions,
    )


DESCRIPTION_MISMATCH_MANIFEST = SkillManifest(
    name="description-mismatch",
    version="1.0.0",
    title="商品描述不符调查 Skill",
    description="核对交易前承诺、收到商品的实际状态、商品身份以及调包或维修争议。",
    dispute_types=(DisputeType.DESCRIPTION_MISMATCH,),
    claim_types=(
        ClaimTypeDefinition(code="CONFIG_MISMATCH", title="配置不符", description="配置参数与商品页或聊天承诺不一致。"),
        ClaimTypeDefinition(code="SERIAL_MISMATCH", title="序列号不符", description="收到商品或核心部件的序列号与交易前材料不一致。"),
        ClaimTypeDefinition(code="FUNCTIONAL_DEFECT", title="功能缺陷", description="卖家承诺功能正常但签收后发现功能异常。"),
        ClaimTypeDefinition(code="REPAIR_HISTORY_MISMATCH", title="维修历史不符", description="商品维修或拆修历史与交易前描述不一致。"),
        ClaimTypeDefinition(code="HARDWARE_SWAP_ALLEGATION", title="硬件调包指控", description="一方主张对方在交易后更换了设备或硬件。"),
        ClaimTypeDefinition(code="OTHER_DESCRIPTION_MISMATCH", title="其他描述不符", description="其他可明确关联到交易前描述的属性争议。"),
    ),
    required_context=DESCRIPTION_MISMATCH_CONTEXT,
    allowed_tools=DESCRIPTION_MISMATCH_TOOLS,
    evidence_requirements=(
        SkillEvidenceRequirement(
            requirement_id="DM-LISTING-PROMISE",
            description="证明卖方对争议属性作出明确描述或承诺。",
            applies_to_claim_types=("CONFIG_MISMATCH", "FUNCTIONAL_DEFECT", "REPAIR_HISTORY_MISMATCH", "OTHER_DESCRIPTION_MISMATCH"),
            accepted_evidence_types=("LISTING_SNAPSHOT", "CHAT_SNAPSHOT"),
        ),
        SkillEvidenceRequirement(
            requirement_id="DM-RECEIVED-CONDITION",
            description="证明签收商品的实际状态并关联到涉案交易。",
            applies_to_claim_types=("CONFIG_MISMATCH", "FUNCTIONAL_DEFECT", "REPAIR_HISTORY_MISMATCH", "SERIAL_MISMATCH"),
            accepted_evidence_types=("DEVICE_REPORT", "DOCUMENT", "PHOTO", "VIDEO"),
        ),
        SkillEvidenceRequirement(
            requirement_id="DM-DEVICE-IDENTITY",
            description="将配置或检测结果与涉案整机、主板或核心部件身份关联。",
            applies_to_claim_types=("CONFIG_MISMATCH", "SERIAL_MISMATCH", "HARDWARE_SWAP_ALLEGATION"),
            accepted_evidence_types=("LISTING_SNAPSHOT", "DEVICE_REPORT", "DOCUMENT", "PHOTO", "VIDEO"),
        ),
    ),
    policy_scope=SkillPolicyScope(
        policy_ids=("marketplace.description_mismatch",),
        dispute_types=(DisputeType.DESCRIPTION_MISMATCH,),
    ),
    investigation_steps=(
        InvestigationStepDefinition(step_id="DM-LOCK-PROMISE", description="读取交易前商品和聊天快照，确定卖方承诺。", tool_names=("listing.get_snapshot", "conversation.search")),
        InvestigationStepDefinition(step_id="DM-VERIFY-CONDITION", description="检查收到商品的状态、时间和设备身份。", tool_names=("evidence.list", "evidence.inspect", "shipment.get_timeline")),
        InvestigationStepDefinition(step_id="DM-APPLY-POLICY", description="检索交易时间锁定的描述不符规则。", tool_names=("policy.search",)),
    ),
    question_rules=(
        QuestionRuleDefinition(
            rule_id="DM-Q-IDENTITY",
            target="CLAIMANT",
            trigger="现有材料无法把检测结果或配置信息关联到涉案商品。",
            question_template="请补充能够同时显示设备身份标识和争议属性的原始检测记录或文字导出。",
            applies_to_claim_types=("CONFIG_MISMATCH", "SERIAL_MISMATCH"),
            acceptable_evidence_types=("DEVICE_REPORT", "DOCUMENT", "PHOTO", "VIDEO"),
        ),
        QuestionRuleDefinition(
            rule_id="DM-Q-SWAP",
            target="EITHER",
            trigger="存在调包或更换硬件指控，但缺少交易前后身份链。",
            question_template="请补充能够证明发货前或签收后设备及核心部件身份的连续记录。",
            applies_to_claim_types=("HARDWARE_SWAP_ALLEGATION",),
            acceptable_evidence_types=("DEVICE_REPORT", "DOCUMENT", "PHOTO", "VIDEO"),
        ),
    ),
    allowed_outcomes=COMMON_OUTCOMES,
    guard_profile=_guard("SERIAL_OR_HARDWARE_SWAP_DISPUTE", "EVIDENCE_AUTHENTICITY_CONTESTED"),
)


MISSING_PARTS_MANIFEST = SkillManifest(
    name="missing-parts",
    version="1.0.0",
    title="商品缺件调查 Skill",
    description="核对交易范围、应包含的部件、签收时实际内容和卖家发货前记录。",
    dispute_types=(DisputeType.MISSING_PARTS,),
    claim_types=(
        ClaimTypeDefinition(code="MISSING_ACCESSORY", title="配件缺失", description="约定随商品交付的充电器、线材、包装等配件缺失。"),
        ClaimTypeDefinition(code="MISSING_COMPONENT", title="部件缺失", description="商品本体的可识别组成部件缺失。"),
        ClaimTypeDefinition(code="QUANTITY_SHORTAGE", title="数量短缺", description="实际收到数量少于商品页或聊天约定数量。"),
    ),
    required_context=MISSING_PARTS_CONTEXT,
    allowed_tools=MISSING_PARTS_TOOLS,
    evidence_requirements=(
        SkillEvidenceRequirement(
            requirement_id="MP-PROMISED-CONTENTS",
            description="证明交易约定包含争议部件或数量。",
            applies_to_claim_types=("MISSING_ACCESSORY", "MISSING_COMPONENT", "QUANTITY_SHORTAGE"),
            accepted_evidence_types=("LISTING_SNAPSHOT", "CHAT_SNAPSHOT"),
        ),
        SkillEvidenceRequirement(
            requirement_id="MP-RECEIVED-CONTENTS",
            description="证明签收时包裹内实际内容和记录形成时间。",
            applies_to_claim_types=("MISSING_ACCESSORY", "MISSING_COMPONENT", "QUANTITY_SHORTAGE"),
            accepted_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
    ),
    policy_scope=SkillPolicyScope(policy_ids=("marketplace.missing_parts",), dispute_types=(DisputeType.MISSING_PARTS,)),
    investigation_steps=(
        InvestigationStepDefinition(step_id="MP-DEFINE-SCOPE", description="确定交易承诺包含的配件、部件和数量。", tool_names=("listing.get_snapshot", "conversation.search")),
        InvestigationStepDefinition(step_id="MP-COMPARE-CONTENTS", description="比较卖家发货记录与买家签收内容。", tool_names=("evidence.list", "evidence.inspect", "shipment.get_timeline")),
        InvestigationStepDefinition(step_id="MP-APPLY-POLICY", description="检索缺件争议规则。", tool_names=("policy.search",)),
    ),
    question_rules=(
        QuestionRuleDefinition(
            rule_id="MP-Q-CONTENTS",
            target="CLAIMANT",
            trigger="缺少能够显示签收时包裹实际内容的材料。",
            question_template="请说明签收时包裹内实际包含哪些物品，并补充形成时间可核实的清单或记录。",
            applies_to_claim_types=("MISSING_ACCESSORY", "MISSING_COMPONENT", "QUANTITY_SHORTAGE"),
            acceptable_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
        QuestionRuleDefinition(
            rule_id="MP-Q-PACKING",
            target="RESPONDENT",
            trigger="卖方主张已完整发货，但缺少发货前内容记录。",
            question_template="请补充发货前包裹内容、数量及封装过程的可核实记录。",
            applies_to_claim_types=("MISSING_ACCESSORY", "MISSING_COMPONENT", "QUANTITY_SHORTAGE"),
            acceptable_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
    ),
    allowed_outcomes=COMMON_OUTCOMES,
    guard_profile=_guard("HIGH_VALUE_COMPONENT_MISSING", "PACKAGE_CONTENTS_MATERIALLY_CONTESTED"),
)


EMPTY_PACKAGE_MANIFEST = SkillManifest(
    name="empty-package",
    version="1.0.0",
    title="空包争议调查 Skill",
    description="核对包裹重量链、物流异常、签收时间以及发货和开包记录。",
    dispute_types=(DisputeType.EMPTY_PACKAGE,),
    claim_types=(
        ClaimTypeDefinition(code="PACKAGE_EMPTY", title="包裹为空", description="买方主张签收包裹中没有交易商品。"),
        ClaimTypeDefinition(code="CONTENT_NOT_RECEIVED", title="未收到商品内容", description="包裹存在但核心交易商品未随包裹交付。"),
        ClaimTypeDefinition(code="WEIGHT_ANOMALY", title="物流重量异常", description="物流节点重量变化与商品是否装入包裹直接相关。"),
    ),
    required_context=EMPTY_PACKAGE_CONTEXT,
    allowed_tools=EMPTY_PACKAGE_TOOLS,
    evidence_requirements=(
        SkillEvidenceRequirement(
            requirement_id="EP-WEIGHT-CHAIN",
            description="核对揽收、中转和签收相关的包裹重量或物流异常记录。",
            applies_to_claim_types=("PACKAGE_EMPTY", "CONTENT_NOT_RECEIVED", "WEIGHT_ANOMALY"),
            accepted_evidence_types=("SHIPMENT_EVENT", "DOCUMENT"),
        ),
        SkillEvidenceRequirement(
            requirement_id="EP-PACKING-OR-OPENING",
            description="核对卖家打包和买家开包记录是否连续、及时并关联涉案包裹。",
            applies_to_claim_types=("PACKAGE_EMPTY", "CONTENT_NOT_RECEIVED"),
            accepted_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
    ),
    policy_scope=SkillPolicyScope(policy_ids=("marketplace.empty_package",), dispute_types=(DisputeType.EMPTY_PACKAGE,)),
    investigation_steps=(
        InvestigationStepDefinition(step_id="EP-TRACE-WEIGHT", description="建立揽收至签收的包裹重量和异常时间线。", tool_names=("shipment.get_timeline", "evidence.list", "evidence.inspect")),
        InvestigationStepDefinition(step_id="EP-COMPARE-RECORDS", description="比较卖家打包主张与买家开包主张。", tool_names=("conversation.search", "evidence.inspect")),
        InvestigationStepDefinition(step_id="EP-APPLY-POLICY", description="检索空包和举证责任规则。", tool_names=("policy.search",)),
    ),
    question_rules=(
        QuestionRuleDefinition(
            rule_id="EP-Q-WEIGHT",
            target="CLAIMANT",
            trigger="物流时间线缺少能够判断包裹内容变化的重量或异常信息。",
            question_template="请补充揽收、运输或签收环节可核实的包裹重量及异常记录。",
            applies_to_claim_types=("PACKAGE_EMPTY", "CONTENT_NOT_RECEIVED", "WEIGHT_ANOMALY"),
            acceptable_evidence_types=("SHIPMENT_EVENT", "DOCUMENT"),
        ),
        QuestionRuleDefinition(
            rule_id="EP-Q-PACKING",
            target="RESPONDENT",
            trigger="缺少卖方打包时商品已装入涉案包裹的记录。",
            question_template="请补充能关联涉案运单的打包内容记录，并说明记录形成时间。",
            applies_to_claim_types=("PACKAGE_EMPTY", "CONTENT_NOT_RECEIVED"),
            acceptable_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
        QuestionRuleDefinition(
            rule_id="EP-Q-OPENING",
            target="CLAIMANT",
            trigger="缺少买方首次开包时包裹内容和封装状态的记录。",
            question_template="请补充能关联涉案运单的首次开包内容记录，并说明签收和开包时间。",
            applies_to_claim_types=("PACKAGE_EMPTY", "CONTENT_NOT_RECEIVED"),
            acceptable_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
    ),
    allowed_outcomes=COMMON_OUTCOMES,
    guard_profile=_guard("WEIGHT_CHAIN_CONFLICT", "PACKING_OR_OPENING_RECORD_CONTESTED", "FRAUD_ALLEGATION"),
)


SHIPPING_DAMAGE_MANIFEST = SkillManifest(
    name="shipping-damage",
    version="1.0.0",
    title="运输损坏调查 Skill",
    description="核对发货前状态、包装充分性、物流异常、签收状态和报损时效。",
    dispute_types=(DisputeType.SHIPPING_DAMAGE,),
    claim_types=(
        ClaimTypeDefinition(code="ITEM_DAMAGED_IN_TRANSIT", title="商品运输损坏", description="商品损坏可能发生在承运过程。"),
        ClaimTypeDefinition(code="PACKAGING_DAMAGE", title="外包装损坏", description="包裹外包装在运输或签收时存在破损、挤压或浸水。"),
        ClaimTypeDefinition(code="INSUFFICIENT_PACKAGING", title="包装不足", description="卖家包装措施可能不足以保护交易商品。"),
        ClaimTypeDefinition(code="LATE_DAMAGE_REPORT", title="延迟报损", description="买方在签收后较长时间才报告损坏。"),
    ),
    required_context=SHIPPING_DAMAGE_CONTEXT,
    allowed_tools=SHIPPING_DAMAGE_TOOLS,
    evidence_requirements=(
        SkillEvidenceRequirement(
            requirement_id="SD-PRE-SHIPMENT-CONDITION",
            description="证明商品发货前状态以及包装方式。",
            applies_to_claim_types=("ITEM_DAMAGED_IN_TRANSIT", "INSUFFICIENT_PACKAGING"),
            accepted_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
        SkillEvidenceRequirement(
            requirement_id="SD-DELIVERY-CONDITION",
            description="证明签收时或合理时间内的商品和外包装状态。",
            applies_to_claim_types=("ITEM_DAMAGED_IN_TRANSIT", "PACKAGING_DAMAGE", "LATE_DAMAGE_REPORT"),
            accepted_evidence_types=("SHIPMENT_EVENT", "DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
    ),
    policy_scope=SkillPolicyScope(policy_ids=("marketplace.shipping_damage",), dispute_types=(DisputeType.SHIPPING_DAMAGE,)),
    investigation_steps=(
        InvestigationStepDefinition(step_id="SD-ESTABLISH-PRECONDITION", description="核对发货前商品状态和包装措施。", tool_names=("conversation.search", "evidence.list", "evidence.inspect")),
        InvestigationStepDefinition(step_id="SD-TRACE-DELIVERY", description="建立物流异常、签收和报损时间线。", tool_names=("shipment.get_timeline", "evidence.inspect")),
        InvestigationStepDefinition(step_id="SD-APPLY-POLICY", description="检索运输损坏、包装责任和报损时限规则。", tool_names=("policy.search",)),
    ),
    question_rules=(
        QuestionRuleDefinition(
            rule_id="SD-Q-PRECONDITION",
            target="RESPONDENT",
            trigger="缺少发货前商品状态或包装措施记录。",
            question_template="请补充发货前商品状态、包装材料和封装方式的可核实记录。",
            applies_to_claim_types=("ITEM_DAMAGED_IN_TRANSIT", "INSUFFICIENT_PACKAGING"),
            acceptable_evidence_types=("DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
        QuestionRuleDefinition(
            rule_id="SD-Q-DELIVERY",
            target="CLAIMANT",
            trigger="缺少签收时商品、外包装状态或首次报损时间。",
            question_template="请补充签收时外包装和商品状态，并说明首次发现及报告损坏的时间。",
            applies_to_claim_types=("ITEM_DAMAGED_IN_TRANSIT", "PACKAGING_DAMAGE", "LATE_DAMAGE_REPORT"),
            acceptable_evidence_types=("SHIPMENT_EVENT", "DOCUMENT", "PARTY_STATEMENT", "PHOTO", "VIDEO"),
        ),
    ),
    allowed_outcomes=COMMON_OUTCOMES,
    guard_profile=_guard("DAMAGE_CAUSATION_CONTESTED", "LATE_DAMAGE_REPORT", "CARRIER_LIABILITY_REQUIRED"),
)


BUILTIN_SKILLS = (
    ManifestDisputeSkill(DESCRIPTION_MISMATCH_MANIFEST),
    ManifestDisputeSkill(MISSING_PARTS_MANIFEST),
    ManifestDisputeSkill(EMPTY_PACKAGE_MANIFEST),
    ManifestDisputeSkill(SHIPPING_DAMAGE_MANIFEST),
)
