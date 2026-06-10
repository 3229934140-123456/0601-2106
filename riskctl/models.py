from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime


class RiskLevel(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    REJECT = "REJECT"

    @property
    def display_name(self) -> str:
        mapping = {
            RiskLevel.PASS: "通过",
            RiskLevel.REVIEW: "需复核",
            RiskLevel.REJECT: "拒绝"
        }
        return mapping[self]

    @property
    def color(self) -> str:
        mapping = {
            RiskLevel.PASS: "green",
            RiskLevel.REVIEW: "yellow",
            RiskLevel.REJECT: "red"
        }
        return mapping[self]


@dataclass
class RuleHit:
    rule_code: str
    rule_name: str
    severity: int
    message: str
    suggested_action: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Merchant:
    row_number: int
    merchant_id: str
    merchant_name: str
    operation_years: float
    revenue_last_month: float
    revenue_month_before: float
    revenue_3months_ago: float
    legal_person_change_count: int
    legal_person_name: str
    address: str
    address_status: str
    phone: str
    contact_person: str
    id_card: str = ""
    industry: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MerchantRiskResult:
    merchant: Merchant
    risk_score: int = 0
    risk_level: RiskLevel = RiskLevel.PASS
    rule_hits: List[RuleHit] = field(default_factory=list)
    is_whitelisted: bool = False
    final_decision: RiskLevel = RiskLevel.PASS
    review_reason: str = ""
    processed_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "row_number": self.merchant.row_number,
            "merchant_id": self.merchant.merchant_id,
            "merchant_name": self.merchant.merchant_name,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level.value,
            "final_decision": self.final_decision.value,
            "final_decision_display": self.final_decision.display_name,
            "is_whitelisted": self.is_whitelisted,
            "review_reason": self.review_reason,
            "rule_hits": [
                {
                    "rule_code": h.rule_code,
                    "rule_name": h.rule_name,
                    "severity": h.severity,
                    "message": h.message,
                    "suggested_action": h.suggested_action,
                    "details": h.details
                }
                for h in self.rule_hits
            ],
            "processed_at": self.processed_at.isoformat()
        }


@dataclass
class BatchResult:
    batch_id: str
    input_file: str
    total_count: int
    valid_count: int = 0
    pass_count: int = 0
    review_count: int = 0
    reject_count: int = 0
    error_count: int = 0
    error_rows: List[Dict[str, Any]] = field(default_factory=list)
    results: List[MerchantRiskResult] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)

    def summary(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "input_file": self.input_file,
            "total_count": self.total_count,
            "valid_count": self.valid_count,
            "pass_count": self.pass_count,
            "review_count": self.review_count,
            "reject_count": self.reject_count,
            "error_count": self.error_count,
            "pass_rate": f"{(self.pass_count / self.total_count * 100):.1f}%" if self.total_count > 0 else "0%",
            "review_rate": f"{(self.review_count / self.total_count * 100):.1f}%" if self.total_count > 0 else "0%",
            "reject_rate": f"{(self.reject_count / self.total_count * 100):.1f}%" if self.total_count > 0 else "0%",
            "valid_pass_rate": f"{(self.pass_count / self.valid_count * 100):.1f}%" if self.valid_count > 0 else "0%",
            "valid_review_rate": f"{(self.review_count / self.valid_count * 100):.1f}%" if self.valid_count > 0 else "0%",
            "valid_reject_rate": f"{(self.reject_count / self.valid_count * 100):.1f}%" if self.valid_count > 0 else "0%",
            "error_rate": f"{(self.error_count / self.total_count * 100):.1f}%" if self.total_count > 0 else "0%",
            "created_at": self.created_at.isoformat()
        }

    def get_pass_list(self) -> List[MerchantRiskResult]:
        return [r for r in self.results if r.final_decision == RiskLevel.PASS]

    def get_review_list(self) -> List[MerchantRiskResult]:
        return [r for r in self.results if r.final_decision == RiskLevel.REVIEW]

    def get_reject_list(self) -> List[MerchantRiskResult]:
        return [r for r in self.results if r.final_decision == RiskLevel.REJECT]
