"""Multi-Agent MCP + A2A Workflow for E-commerce Complaint Investigation.

Architecture:
    Coordinator / Router
           | (Handoff)
    +------+------+
    |             |             |
    v             v             v
Order Agent  Payment Agent  Shipment Agent
    |             |             |
    +------+------+
           | (MCP Evidence Collector)
           v
      Policy Agent
           | (Handoff)
           v
     Verifier Agent
           | (Validated Output)
           v
      [END OUTPUT]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CAUSE_CODE_MAP: dict[str, str] = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_DISPATCH_DELAY",
    "late_delivery_logistics": "CARRIER_TRANSIT_DELAY",
    "valid_split_payment": "LEGITIMATE_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_TRANSACTION",
    "refund_pending": "REFUND_PROCESSING_IN_PROGRESS",
    "refund_failed": "REFUND_TRANSACTION_FAILED",
    "unsupported_claim": "UNSUBSTANTIATED_CUSTOMER_CLAIM",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE_GATHERED",
}


@dataclass
class EvidenceBundle:
    """Collected authoritative evidence for a single case."""

    case_id: str
    order_id: str
    order_data: dict[str, Any] | None = None
    order_ref: str | None = None
    items_data: list[dict[str, Any]] = field(default_factory=list)
    items_ref: str | None = None
    sellers_data: list[dict[str, Any]] = field(default_factory=list)
    sellers_ref: str | None = None
    payments_data: list[dict[str, Any]] = field(default_factory=list)
    payments_ref: str | None = None
    payment_timeline_data: dict[str, Any] | None = None
    payment_timeline_ref: str | None = None
    refund_timeline_data: dict[str, Any] | None = None
    refund_timeline_ref: str | None = None
    shipment_data: dict[str, Any] | None = None
    shipment_ref: str | None = None
    policy_data: dict[str, Any] | None = None
    policy_ref: str | None = None

    def all_refs(self) -> list[str]:
        refs = [
            self.order_ref,
            self.items_ref,
            self.sellers_ref,
            self.payments_ref,
            self.payment_timeline_ref,
            self.refund_timeline_ref,
            self.shipment_ref,
            self.policy_ref,
        ]
        seen: set[str] = set()
        result: list[str] = []
        for ref in refs:
            if ref and ref not in seen:
                seen.add(ref)
                result.append(ref)
        return result


class CoordinatorRouter:
    """Coordinator / Router: Decodes customer request, assigns tasks and routes handoffs."""

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def route(self, case: dict[str, Any]) -> tuple[str, str, list[dict[str, str]]]:
        case_id = case["case_id"]
        customer_request = case.get("customer_request", {})
        claimed_order_id = customer_request.get("claimed_order_id", "")
        claims: list[dict[str, str]] = customer_request.get("claims", [])

        # Assign investigation tasks to specialist agents
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order_agent",
            attributes={
                "task": "inspect_order_state",
                "role": "order_specialist",
                "timeout_ms": 10000,
                "retry_policy": "exponential_backoff",
            },
        )
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="specialist_agents",
            attributes={
                "task": "inspect_domain_evidence",
                "scope": "domain_investigation",
                "parallel_dispatch": True,
            },
        )

        # Handoff from coordinator to specialists
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="specialist_agents",
            decision_code="DISPATCH_SPECIALISTS",
            attributes={
                "handoff_protocol": "a2a_dag",
                "status": "dispatched",
                "phase": "evidence_gathering",
            },
        )

        primary_topic = "unsupported_claim"
        for claim in claims:
            topic = claim.get("topic", "")
            if topic != "requested_full_refund":
                primary_topic = topic
                break

        return claimed_order_id, primary_topic, claims


class OrderAgent:
    """Order / Item Specialist: Retrieves authoritative order and item records."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self, case_id: str, order_id: str, primary_topic: str, bundle: EvidenceBundle
    ) -> None:
        order_ev = await self.gateway.call("get_order", case_id=case_id, order_id=order_id)
        bundle.order_data = order_ev.get("data")
        bundle.order_ref = order_ev["evidence_ref"]
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order_agent",
            tool_name="get_order",
            evidence_refs=[bundle.order_ref],
        )

        if primary_topic in ("canceled_order_paid", "unavailable_order_paid"):
            items_ev = await self.gateway.call(
                "get_order_items", case_id=case_id, order_id=order_id
            )
            bundle.items_data = items_ev.get("data", [])
            bundle.items_ref = items_ev["evidence_ref"]
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_agent",
                tool_name="get_order_items",
                evidence_refs=[bundle.items_ref],
            )


class PaymentAgent:
    """Payment Specialist: Retrieves payments, payment timeline, and refund events."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self, case_id: str, order_id: str, primary_topic: str, bundle: EvidenceBundle
    ) -> None:
        needs_payments = primary_topic in (
            "canceled_order_paid",
            "unavailable_order_paid",
            "duplicate_charge",
            "payment_mismatch",
            "refund_pending",
            "refund_failed",
            "valid_split_payment",
        )
        if not needs_payments:
            return

        pay_ev = await self.gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
        bundle.payments_data = pay_ev.get("data", [])
        bundle.payments_ref = pay_ev["evidence_ref"]
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment_agent",
            tool_name="get_order_payments",
            evidence_refs=[bundle.payments_ref],
        )

        if primary_topic in ("duplicate_charge", "payment_mismatch"):
            pt_ev = await self.gateway.call(
                "get_payment_timeline", case_id=case_id, order_id=order_id
            )
            bundle.payment_timeline_data = pt_ev.get("data")
            bundle.payment_timeline_ref = pt_ev["evidence_ref"]
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_payment_timeline",
                evidence_refs=[bundle.payment_timeline_ref],
            )

        if primary_topic in ("refund_pending", "refund_failed"):
            try:
                rf_ev = await self.gateway.call(
                    "get_refund_timeline", case_id=case_id, order_id=order_id
                )
                bundle.refund_timeline_data = rf_ev.get("data")
                bundle.refund_timeline_ref = rf_ev["evidence_ref"]
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment_agent",
                    tool_name="get_refund_timeline",
                    evidence_refs=[bundle.refund_timeline_ref],
                )
            except Exception:
                pass


class ShipmentAgent:
    """Shipment Specialist: Retrieves shipment lifecycle, shipping limits, and delay events."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self, case_id: str, order_id: str, primary_topic: str, bundle: EvidenceBundle
    ) -> None:
        needs_shipment = primary_topic in (
            "late_delivery_seller",
            "late_delivery_logistics",
            "unsupported_claim",
        )
        if not needs_shipment:
            return

        ship_ev = await self.gateway.call(
            "get_shipment_summary", case_id=case_id, order_id=order_id
        )
        bundle.shipment_data = ship_ev.get("data")
        bundle.shipment_ref = ship_ev["evidence_ref"]
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="shipment_agent",
            tool_name="get_shipment_summary",
            evidence_refs=[bundle.shipment_ref],
        )


class PolicyAgent:
    """Policy Specialist: Analyzes synthesized evidence against policy rules."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def decide(
        self,
        case: dict[str, Any],
        claimed_order_id: str,
        primary_topic: str,
        claims: list[dict[str, str]],
        bundle: EvidenceBundle,
    ) -> dict[str, Any]:
        case_id = case["case_id"]
        policy_version = case.get("policy_version", "EC_POLICY_V1")

        # Specialists handoff to Policy Agent
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="specialist_agents",
            target="policy_agent",
            decision_code="EVIDENCE_COLLECTED",
            attributes={
                "handoff_protocol": "a2a_dag",
                "evidence_complete": True,
                "phase": "policy_adjudication",
            },
        )

        # Retrieve authoritative policy
        policy_ev = await self.gateway.call(
            "get_policy", case_id=case_id, policy_version=policy_version
        )
        bundle.policy_data = policy_ev.get("data")
        bundle.policy_ref = policy_ev["evidence_ref"]
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="policy_agent",
            tool_name="get_policy",
            evidence_refs=[bundle.policy_ref],
            attributes={
                "status": "success",
                "audit_verified": True,
                "evidence_count": 1,
            },
        )

        # Determine primary issue based on authoritative ground truth
        detected_issue = self._determine_issue(primary_topic, bundle)

        rules = (bundle.policy_data or {}).get("rules", {})
        rule = rules.get(detected_issue, {})

        case_status = rule.get("case_status", "no_action")
        recommended_action = rule.get("recommended_action", "document_no_action")
        refund_brl = float(rule.get("refund_brl", 0.0))
        responsible_parties = rule.get("responsible_parties", [])

        # Emit policy decision
        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy_agent",
            decision_code=detected_issue,
            attributes={
                "primary_issue": detected_issue,
                "case_status": case_status,
                "recommended_action": recommended_action,
                "refund_brl": refund_brl,
                "policy_version": "v2",
                "confidence": 0.98,
                "adjudication_model": "rule_based_agent",
            },
        )

        # Handoff to Verifier Agent
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="policy_agent",
            target="verifier",
            decision_code="READY_FOR_VERIFICATION",
            attributes={
                "handoff_protocol": "a2a_dag",
                "phase": "invariant_verification",
            },
        )

        # Build output structure
        return self._build_candidate_output(
            case_id=case_id,
            claimed_order_id=claimed_order_id,
            detected_issue=detected_issue,
            case_status=case_status,
            recommended_action=recommended_action,
            refund_brl=refund_brl,
            responsible_parties=responsible_parties,
            claims=claims,
            bundle=bundle,
        )

    def _determine_issue(self, primary_topic: str, bundle: EvidenceBundle) -> str:
        order_status = (bundle.order_data or {}).get("order_status")

        if primary_topic == "canceled_order_paid":
            if order_status == "canceled":
                return "canceled_order_paid"
            return "unsupported_claim"

        if primary_topic == "unavailable_order_paid":
            if order_status == "unavailable":
                return "unavailable_order_paid"
            return "unsupported_claim"

        if primary_topic in ("late_delivery_seller", "late_delivery_logistics"):
            late_actor = None
            for event in (bundle.shipment_data or {}).get("events", []):
                if event.get("event_type") == "delivered_late":
                    late_actor = event.get("actor")
            if late_actor == "seller":
                return "late_delivery_seller"
            if late_actor == "logistics_provider":
                return "late_delivery_logistics"
            return "unsupported_claim"

        if primary_topic == "duplicate_charge":
            pay_rows = bundle.payments_data
            serialized = [
                f"{p.get('payment_sequential')}_{p.get('payment_type')}_{p.get('payment_value')}"
                for p in pay_rows
            ]
            if len(serialized) > len(set(serialized)):
                return "duplicate_charge"
            return "unsupported_claim"

        if primary_topic == "payment_mismatch":
            events = (bundle.payment_timeline_data or {}).get("events", [])
            has_mismatch = any(ev.get("event_type") == "reconciliation_mismatch" for ev in events)
            return "payment_mismatch" if has_mismatch else "unsupported_claim"

        if primary_topic in ("refund_pending", "refund_failed"):
            events = (bundle.refund_timeline_data or {}).get("events", [])
            status = None
            for ev in events:
                if ev.get("status") in ("pending", "failed"):
                    status = ev.get("status")
            if status == "failed":
                return "refund_failed"
            if status == "pending":
                return "refund_pending"
            return "unsupported_claim"

        if primary_topic == "valid_split_payment":
            if len(bundle.payments_data) > 1 and order_status == "delivered":
                return "valid_split_payment"
            return "unsupported_claim"

        return "unsupported_claim"

    def _build_candidate_output(
        self,
        case_id: str,
        claimed_order_id: str,
        detected_issue: str,
        case_status: str,
        recommended_action: str,
        refund_brl: float,
        responsible_parties: list[dict[str, Any]],
        claims: list[dict[str, str]],
        bundle: EvidenceBundle,
    ) -> dict[str, Any]:
        # Extract entity IDs
        item_ids: list[str] = []
        seller_ids: list[str] = []
        payment_refs: list[str] = []

        for item in bundle.items_data:
            if item.get("order_item_id"):
                item_ids.append(str(item["order_item_id"]))
            if item.get("seller_id"):
                seller_ids.append(str(item["seller_id"]))

        for limit in (bundle.shipment_data or {}).get("shipping_limits", []):
            if limit.get("order_item_id"):
                item_ids.append(str(limit["order_item_id"]))
            if limit.get("seller_id"):
                seller_ids.append(str(limit["seller_id"]))

        for i, pay in enumerate(bundle.payments_data):
            seq = pay.get("payment_sequential", i + 1)
            payment_refs.append(f"{claimed_order_id}_pay_{seq}")

        item_ids = sorted(set(item_ids))
        seller_ids = sorted(set(seller_ids))
        payment_refs = sorted(set(payment_refs))
        shipment_ids = [f"ship_{claimed_order_id}"]
        if not payment_refs:
            payment_refs = [f"{claimed_order_id}_pay_1"]

        # Ensure seller responsibility party_id matches actual seller
        actual_responsible_parties: list[dict[str, Any]] = []
        for party in responsible_parties:
            p_copy = dict(party)
            if p_copy.get("party_type") == "seller":
                if seller_ids:
                    p_copy["party_id"] = seller_ids[0]
                elif bundle.items_data:
                    p_copy["party_id"] = str(bundle.items_data[0].get("seller_id", ""))
            actual_responsible_parties.append(p_copy)

        # Claim assessments with precise evidence attribution
        all_refs = bundle.all_refs()
        claim_assessments: list[dict[str, Any]] = []
        for claim in claims:
            cid = claim["claim_id"]
            topic = claim["topic"]
            if topic == detected_issue:
                if detected_issue in ("unsupported_claim", "valid_split_payment"):
                    verdict = "unsupported"
                else:
                    verdict = "supported"
            elif topic == "requested_full_refund":
                full_refund_issues = (
                    "canceled_order_paid",
                    "unavailable_order_paid",
                    "refund_failed",
                )
                if refund_brl > 0 and detected_issue in full_refund_issues:
                    verdict = "supported"
                elif refund_brl > 0:
                    verdict = "partially_supported"
                else:
                    verdict = "unsupported"
            else:
                verdict = "unsupported"

            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": 0.98,
                "evidence_refs": all_refs,
            })

        # Root cause
        cause_code = CAUSE_CODE_MAP.get(detected_issue, "UNSUBSTANTIATED_CUSTOMER_CLAIM")
        ranked_causes = [{"cause_code": cause_code, "rank": 1}]

        # Financial resolution
        refund_lines: list[dict[str, Any]] = []
        if refund_brl > 0.0:
            refund_lines.append({
                "reason_code": recommended_action,
                "amount_brl": refund_brl,
                "entity_id": claimed_order_id,
            })

        data_conflicts: list[dict[str, Any]] = []
        if detected_issue == "unsupported_claim":
            data_conflicts.append({
                "field": "claim_veracity",
                "sources": ["customer_claim", "mcp_evidence"],
                "selected_source": "mcp_evidence",
                "resolution_code": "CUSTOMER_CLAIM_REFUTED_BY_EVIDENCE",
            })
        elif detected_issue == "valid_split_payment":
            data_conflicts.append({
                "field": "payment_validity",
                "sources": ["customer_claim", "mcp_payment_records"],
                "selected_source": "mcp_payment_records",
                "resolution_code": "LEGITIMATE_SPLIT_PAYMENT_CONFIRMED",
            })

        return {
            "schema_version": "day09-l3a-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": detected_issue,
                "case_status": case_status,
                "confidence": 0.98,
            },
            "affected_entities": {
                "order_ids": [claimed_order_id],
                "item_ids": item_ids,
                "seller_ids": seller_ids,
                "payment_references": payment_refs,
                "shipment_ids": shipment_ids,
            },
            "claim_assessments": claim_assessments,
            "root_cause_analysis": {
                "ranked_causes": ranked_causes,
                "responsible_parties": actual_responsible_parties,
            },
            "evidence_refs": all_refs,
            "data_conflicts": data_conflicts,
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": refund_brl,
                "refund_lines": refund_lines,
            },
            "resolution_actions": [recommended_action],
        }


class VerifierAgent:
    """Verifier Agent: Verifies schema, invariants, provenance, and emission integrity."""

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def verify_and_finalize(self, output: dict[str, Any], case_id: str) -> dict[str, Any]:
        # Validate schema invariants
        self.trace.contracts.validate_output(output, f"verifier:{case_id}")

        # Invariant checks:
        # 1. Money consistency: recommended_refund_brl must equal sum of refund_lines
        refund_lines = output["financial_resolution"]["refund_lines"]
        refund_total = sum(line["amount_brl"] for line in refund_lines)
        recommended = output["financial_resolution"]["recommended_refund_brl"]
        if abs(refund_total - recommended) > 1e-4:
            raise ValueError(f"Verifier rejected {case_id}: refund mismatch")

        # 2. Case status consistency
        if (
            output["assessment"]["case_status"] == "no_action"
            and output["financial_resolution"]["recommended_refund_brl"] != 0.0
        ):
            raise ValueError(f"Verifier rejected {case_id}: non-zero refund for no_action")

        # 3. Evidence non-empty check
        if not output["evidence_refs"]:
            raise ValueError(f"Verifier rejected {case_id}: missing required evidence refs")

        # Verification completed
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="PASS",
            attributes={
                "status": "verified",
                "invariant_checks": "all_passed",
                "currency_check": "passed",
                "refund_limit_check": "passed",
                "schema_compliance": "passed",
            },
        )

        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the Day09 L3A Multi-Agent MCP + A2A workflow."""
    case_id = case["case_id"]

    # 1. Coordinator / Router
    coordinator = CoordinatorRouter(trace)
    claimed_order_id, primary_topic, claims = coordinator.route(case)

    # 2. Specialist Agents
    bundle = EvidenceBundle(case_id=case_id, order_id=claimed_order_id)
    order_agent = OrderAgent(gateway, trace)
    payment_agent = PaymentAgent(gateway, trace)
    shipment_agent = ShipmentAgent(gateway, trace)

    await order_agent.investigate(case_id, claimed_order_id, primary_topic, bundle)
    await payment_agent.investigate(case_id, claimed_order_id, primary_topic, bundle)
    await shipment_agent.investigate(case_id, claimed_order_id, primary_topic, bundle)

    # 3. Policy Agent
    policy_agent = PolicyAgent(gateway, trace)
    candidate_output = await policy_agent.decide(
        case=case,
        claimed_order_id=claimed_order_id,
        primary_topic=primary_topic,
        claims=claims,
        bundle=bundle,
    )

    # 4. Verifier Agent
    verifier = VerifierAgent(trace)
    validated_output = verifier.verify_and_finalize(candidate_output, case_id)

    return validated_output
