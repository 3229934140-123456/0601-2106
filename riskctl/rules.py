from typing import List, Dict, Tuple
from collections import Counter

from .config_manager import ConfigManager
from .models import Merchant, MerchantRiskResult, RuleHit, RiskLevel


class RiskRuleEngine:
    def __init__(self, config: ConfigManager):
        self.config = config

    def evaluate_all(
        self,
        merchants: List[Merchant]
    ) -> Tuple[List[MerchantRiskResult], List[Dict[str, str]]]:
        contact_counter = self._build_contact_counters(merchants)
        results = []
        errors = []

        for merchant in merchants:
            try:
                result = self._evaluate_single(merchant, contact_counter)
                results.append(result)
            except Exception as e:
                errors.append({
                    "row_number": merchant.row_number,
                    "merchant_id": merchant.merchant_id,
                    "merchant_name": merchant.merchant_name,
                    "error": str(e)
                })

        return results, errors

    def _build_contact_counters(
        self, merchants: List[Merchant]
    ) -> Dict[str, Counter]:
        phone_counter = Counter()
        id_card_counter = Counter()
        contact_person_counter = Counter()

        for m in merchants:
            if m.phone:
                phone_counter[m.phone] += 1
            if m.id_card:
                id_card_counter[m.id_card] += 1
            if m.contact_person:
                contact_person_counter[m.contact_person] += 1

        return {
            "phone": phone_counter,
            "id_card": id_card_counter,
            "contact_person": contact_person_counter
        }

    def _evaluate_single(
        self,
        merchant: Merchant,
        contact_counter: Dict[str, Counter]
    ) -> MerchantRiskResult:
        result = MerchantRiskResult(merchant=merchant)
        rule_hits: List[RuleHit] = []

        whitelist = self.config.get("whitelist", [])
        if merchant.merchant_id in whitelist or merchant.merchant_name in whitelist:
            result.is_whitelisted = True
            result.final_decision = RiskLevel.PASS
            result.review_reason = "白名单豁免"
            return result

        rule_hits.extend(self._check_operation_years(merchant))
        rule_hits.extend(self._check_volatility(merchant))
        rule_hits.extend(self._check_legal_person_change(merchant))
        rule_hits.extend(self._check_blacklist(merchant))
        rule_hits.extend(self._check_address(merchant))
        rule_hits.extend(self._check_contact_duplicate(merchant, contact_counter))

        result.rule_hits = rule_hits
        result.risk_score = sum(h.severity for h in rule_hits)
        result.risk_level = self._score_to_level(result.risk_score)

        has_blacklist = any(h.rule_code == "RULE_BLACKLIST" for h in rule_hits)
        if has_blacklist:
            result.final_decision = RiskLevel.REJECT
            result.review_reason = "黑名单命中"
        else:
            result.final_decision = result.risk_level
            if result.risk_level == RiskLevel.REVIEW:
                result.review_reason = self._generate_review_reason(rule_hits)
            elif result.risk_level == RiskLevel.REJECT:
                result.review_reason = self._generate_reject_reason(rule_hits)

        return result

    def _check_operation_years(self, merchant: Merchant) -> List[RuleHit]:
        hits = []
        min_years = self.config.get("thresholds.min_operation_years", 1)
        suggested = self.config.get("suggested_actions.operation_years_low", "")

        if merchant.operation_years < min_years:
            hits.append(RuleHit(
                rule_code="RULE_YEARS_001",
                rule_name="经营年限不足",
                severity=50,
                message=f"经营年限 {merchant.operation_years} 年，低于最低要求 {min_years} 年",
                suggested_action=suggested,
                details={"current": merchant.operation_years, "threshold": min_years}
            ))
        elif merchant.operation_years < min_years + 0.5:
            hits.append(RuleHit(
                rule_code="RULE_YEARS_002",
                rule_name="经营年限接近阈值",
                severity=15,
                message=f"经营年限 {merchant.operation_years} 年，接近阈值 {min_years} 年",
                suggested_action=suggested,
                details={"current": merchant.operation_years, "threshold": min_years}
            ))
        return hits

    def _check_volatility(self, merchant: Merchant) -> List[RuleHit]:
        hits = []
        max_ratio = self.config.get("thresholds.max_volatility_ratio", 0.5)
        suggested = self.config.get("suggested_actions.volatility_high", "")

        r1 = merchant.revenue_last_month
        r2 = merchant.revenue_month_before
        r3 = merchant.revenue_3months_ago
        revenues = [r for r in [r1, r2, r3] if r and r > 0]

        if len(revenues) >= 2:
            max_r = max(revenues)
            min_r = min(revenues)
            if max_r > 0:
                volatility = (max_r - min_r) / max_r
                if volatility > max_ratio * 1.5:
                    hits.append(RuleHit(
                        rule_code="RULE_VOL_001",
                        rule_name="交易波动异常剧烈",
                        severity=55,
                        message=f"近3月交易波动率 {volatility:.2%}，远超阈值 {max_ratio:.0%}",
                        suggested_action=suggested,
                        details={"volatility": volatility, "threshold": max_ratio * 1.5, "revenues": revenues}
                    ))
                elif volatility > max_ratio:
                    hits.append(RuleHit(
                        rule_code="RULE_VOL_002",
                        rule_name="交易波动较大",
                        severity=25,
                        message=f"近3月交易波动率 {volatility:.2%}，超过阈值 {max_ratio:.0%}",
                        suggested_action=suggested,
                        details={"volatility": volatility, "threshold": max_ratio, "revenues": revenues}
                    ))
        return hits

    def _check_legal_person_change(self, merchant: Merchant) -> List[RuleHit]:
        hits = []
        max_changes = self.config.get("thresholds.max_legal_person_change_count", 2)
        suggested = self.config.get("suggested_actions.legal_person_change", "")

        changes = merchant.legal_person_change_count
        if changes >= max_changes + 2:
            hits.append(RuleHit(
                rule_code="RULE_LEGAL_001",
                rule_name="法人变更过于频繁",
                severity=50,
                message=f"近1年法人变更 {changes} 次，远超阈值 {max_changes} 次",
                suggested_action=suggested,
                details={"changes": changes, "threshold": max_changes}
            ))
        elif changes > max_changes:
            hits.append(RuleHit(
                rule_code="RULE_LEGAL_002",
                rule_name="法人变更次数偏多",
                severity=20,
                message=f"近1年法人变更 {changes} 次，超过阈值 {max_changes} 次",
                suggested_action=suggested,
                details={"changes": changes, "threshold": max_changes}
            ))
        return hits

    def _check_blacklist(self, merchant: Merchant) -> List[RuleHit]:
        hits = []
        blacklist = self.config.get("blacklist", [])
        suggested = self.config.get("suggested_actions.blacklist_hit", "")

        check_items = [
            merchant.merchant_id,
            merchant.merchant_name,
            merchant.id_card,
            merchant.legal_person_name,
            merchant.phone
        ]

        for item in check_items:
            if item and item in blacklist:
                hits.append(RuleHit(
                    rule_code="RULE_BLACKLIST",
                    rule_name="黑名单命中",
                    severity=100,
                    message=f"命中黑名单关键字：{item}",
                    suggested_action=suggested,
                    details={"hit_item": item}
                ))
                break
        return hits

    def _check_address(self, merchant: Merchant) -> List[RuleHit]:
        hits = []
        suggested = self.config.get("suggested_actions.address_abnormal", "")

        status = (merchant.address_status or "").lower()
        address = merchant.address or ""

        if status in ["异常", "abnormal", "error", "查无此址"]:
            hits.append(RuleHit(
                rule_code="RULE_ADDR_001",
                rule_name="地址严重异常",
                severity=45,
                message=f"经营地址状态异常：{merchant.address_status}",
                suggested_action=suggested,
                details={"status": merchant.address_status}
            ))
        elif status in ["待核实", "pending", "疑似"]:
            hits.append(RuleHit(
                rule_code="RULE_ADDR_002",
                rule_name="地址存在异常标记",
                severity=18,
                message=f"经营地址状态待核实：{merchant.address_status}",
                suggested_action=suggested,
                details={"status": merchant.address_status}
            ))

        if not address or len(address.strip()) < 5:
            hits.append(RuleHit(
                rule_code="RULE_ADDR_003",
                rule_name="地址信息不完整",
                severity=10,
                message="经营地址信息过短或为空",
                suggested_action=suggested,
                details={"address": address}
            ))
        return hits

    def _check_contact_duplicate(
        self,
        merchant: Merchant,
        contact_counter: Dict[str, Counter]
    ) -> List[RuleHit]:
        hits = []
        threshold = self.config.get("thresholds.duplicate_contact_threshold", 3)
        suggested = self.config.get("suggested_actions.contact_duplicate", "")

        phone = merchant.phone
        if phone and contact_counter["phone"].get(phone, 0) >= threshold + 2:
            hits.append(RuleHit(
                rule_code="RULE_CONTACT_001",
                rule_name="联系方式严重重复",
                severity=50,
                message=f"手机号 {phone} 在本批次出现 {contact_counter['phone'][phone]} 次",
                suggested_action=suggested,
                details={"phone": phone, "count": contact_counter["phone"][phone]}
            ))
        elif phone and contact_counter["phone"].get(phone, 0) >= threshold:
            hits.append(RuleHit(
                rule_code="RULE_CONTACT_002",
                rule_name="联系方式重复",
                severity=20,
                message=f"手机号 {phone} 在本批次出现 {contact_counter['phone'][phone]} 次",
                suggested_action=suggested,
                details={"phone": phone, "count": contact_counter["phone"][phone]}
            ))

        id_card = merchant.id_card
        if id_card and contact_counter["id_card"].get(id_card, 0) >= threshold:
            hits.append(RuleHit(
                rule_code="RULE_CONTACT_003",
                rule_name="身份证号重复",
                severity=30,
                message=f"法人身份证 {id_card} 在本批次出现 {contact_counter['id_card'][id_card]} 次",
                suggested_action=suggested,
                details={"id_card": id_card, "count": contact_counter["id_card"][id_card]}
            ))

        cp = merchant.contact_person
        if cp and contact_counter["contact_person"].get(cp, 0) >= threshold + 1:
            hits.append(RuleHit(
                rule_code="RULE_CONTACT_004",
                rule_name="联系人重复",
                severity=15,
                message=f"联系人 {cp} 在本批次出现 {contact_counter['contact_person'][cp]} 次",
                suggested_action=suggested,
                details={"contact_person": cp, "count": contact_counter["contact_person"][cp]}
            ))

        return hits

    def _score_to_level(self, score: int) -> RiskLevel:
        t = self.config.get("thresholds", {})
        p_min = t.get("pass_score_min", 0)
        p_max = t.get("pass_score_max", 30)
        r_min = t.get("review_score_min", 31)
        r_max = t.get("review_score_max", 70)

        if p_min <= score <= p_max:
            return RiskLevel.PASS
        elif r_min <= score <= r_max:
            return RiskLevel.REVIEW
        else:
            return RiskLevel.REJECT

    def _generate_review_reason(self, rule_hits: List[RuleHit]) -> str:
        configured = self.config.get("review_reasons", [])
        reasons = []
        code_reason_map = {
            "RULE_YEARS_002": configured[0] if len(configured) > 0 else "经营年限接近阈值",
            "RULE_VOL_002": configured[1] if len(configured) > 1 else "交易波动较大需人工确认",
            "RULE_LEGAL_002": configured[2] if len(configured) > 2 else "法人变更次数偏多",
            "RULE_ADDR_002": configured[3] if len(configured) > 3 else "地址信息存在异常标记",
            "RULE_CONTACT_002": configured[4] if len(configured) > 4 else "联系方式出现重复",
            "RULE_CONTACT_004": configured[4] if len(configured) > 4 else "联系方式出现重复",
            "RULE_ADDR_003": configured[3] if len(configured) > 3 else "地址信息存在异常标记",
        }
        for h in rule_hits:
            reason = code_reason_map.get(h.rule_code)
            if reason and reason not in reasons:
                reasons.append(reason)
        if not reasons:
            reasons.append(configured[5] if len(configured) > 5 else "风险分值处于灰区")
        return "；".join(reasons)

    def _generate_reject_reason(self, rule_hits: List[RuleHit]) -> str:
        configured = self.config.get("reject_reasons", [])
        reasons = []
        code_reason_map = {
            "RULE_YEARS_001": configured[0] if len(configured) > 0 else "经营年限不足",
            "RULE_VOL_001": configured[1] if len(configured) > 1 else "交易波动异常剧烈",
            "RULE_LEGAL_001": configured[2] if len(configured) > 2 else "法人变更过于频繁",
            "RULE_BLACKLIST": configured[3] if len(configured) > 3 else "黑名单命中",
            "RULE_ADDR_001": configured[4] if len(configured) > 4 else "地址严重异常",
            "RULE_CONTACT_001": configured[5] if len(configured) > 5 else "联系方式严重重复",
            "RULE_CONTACT_003": configured[5] if len(configured) > 5 else "联系方式严重重复",
        }
        for h in rule_hits:
            reason = code_reason_map.get(h.rule_code)
            if reason and reason not in reasons:
                reasons.append(reason)
        return "；".join(reasons) if reasons else "综合风险过高"
