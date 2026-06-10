import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

from .config_manager import BATCHES_DIR
from .models import BatchResult, MerchantRiskResult, RiskLevel, Merchant


class BatchManager:
    def __init__(self, batches_dir: Optional[Path] = None):
        self.batches_dir = Path(batches_dir) if batches_dir else BATCHES_DIR
        self.batches_dir.mkdir(parents=True, exist_ok=True)

    def generate_batch_id(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def save_batch(self, batch: BatchResult) -> Path:
        file_path = self.batches_dir / f"{batch.batch_id}.json"
        data = {
            "batch_id": batch.batch_id,
            "input_file": batch.input_file,
            "total_count": batch.total_count,
            "pass_count": batch.pass_count,
            "review_count": batch.review_count,
            "reject_count": batch.reject_count,
            "error_count": batch.error_count,
            "error_rows": batch.error_rows,
            "results": [r.to_dict() for r in batch.results],
            "created_at": batch.created_at.isoformat()
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return file_path

    def load_batch(self, batch_id: str) -> Optional[BatchResult]:
        file_path = self.batches_dir / f"{batch_id}.json"
        if not file_path.exists():
            alt_files = list(self.batches_dir.glob(f"*{batch_id}*.json"))
            if alt_files:
                file_path = alt_files[0]
            else:
                return None

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        batch = BatchResult(
            batch_id=data["batch_id"],
            input_file=data["input_file"],
            total_count=data["total_count"],
            pass_count=data["pass_count"],
            review_count=data["review_count"],
            reject_count=data["reject_count"],
            error_count=data["error_count"],
            error_rows=data.get("error_rows", []),
            created_at=datetime.fromisoformat(data["created_at"])
        )

        for rd in data["results"]:
            m = Merchant(
                row_number=rd["row_number"],
                merchant_id=rd["merchant_id"],
                merchant_name=rd["merchant_name"],
                operation_years=0,
                revenue_last_month=0,
                revenue_month_before=0,
                revenue_3months_ago=0,
                legal_person_change_count=0,
                legal_person_name="",
                address="",
                address_status="",
                phone="",
                contact_person=""
            )
            from .models import RuleHit
            rule_hits = []
            for hd in rd["rule_hits"]:
                rule_hits.append(RuleHit(
                    rule_code=hd["rule_code"],
                    rule_name=hd["rule_name"],
                    severity=hd["severity"],
                    message=hd["message"],
                    suggested_action=hd.get("suggested_action", ""),
                    details=hd.get("details", {})
                ))
            result = MerchantRiskResult(
                merchant=m,
                risk_score=rd["risk_score"],
                risk_level=RiskLevel(rd["risk_level"]),
                rule_hits=rule_hits,
                is_whitelisted=rd.get("is_whitelisted", False),
                final_decision=RiskLevel(rd["final_decision"]),
                review_reason=rd.get("review_reason", ""),
                processed_at=datetime.fromisoformat(rd["processed_at"])
            )
            batch.results.append(result)
        return batch

    def list_batches(self, limit: int = 20) -> List[Dict[str, Any]]:
        files = sorted(
            self.batches_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        batches = []
        for f in files[:limit]:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                batches.append({
                    "batch_id": data["batch_id"],
                    "input_file": data["input_file"],
                    "total": data["total_count"],
                    "pass": data["pass_count"],
                    "review": data["review_count"],
                    "reject": data["reject_count"],
                    "error": data["error_count"],
                    "created_at": data["created_at"],
                    "file_path": str(f)
                })
            except Exception:
                continue
        return batches

    def find_batch_partial(self, partial_id: str) -> Optional[str]:
        files = self.batches_dir.glob(f"*{partial_id}*.json")
        for f in files:
            return f.stem
        return None

    def export_csv(
        self,
        batch: BatchResult,
        output_dir: Optional[str] = None,
        categories: Optional[List[str]] = None
    ) -> Dict[str, Path]:
        out_dir = Path(output_dir) if output_dir else self.batches_dir / batch.batch_id
        out_dir.mkdir(parents=True, exist_ok=True)

        exported = {}
        category_map = {
            "pass": (batch.get_pass_list(), "通过名单"),
            "review": (batch.get_review_list(), "复核名单"),
            "reject": (batch.get_reject_list(), "拒绝名单")
        }

        if categories is None:
            categories = ["pass", "review", "reject"]

        for cat in categories:
            if cat not in category_map:
                continue
            results, label = category_map[cat]
            file_path = out_dir / f"{batch.batch_id}_{cat}.csv"
            self._write_results_csv(file_path, results, label)
            exported[cat] = file_path

        if batch.error_rows:
            file_path = out_dir / f"{batch.batch_id}_errors.csv"
            self._write_errors_csv(file_path, batch.error_rows)
            exported["errors"] = file_path

        return exported

    def _write_results_csv(self, file_path: Path, results: List[MerchantRiskResult], label: str):
        import csv
        with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "行号", "商户编号", "商户名称", "风险分值",
                "风险等级", "最终结论", "白名单", "复核/拒绝原因",
                "命中规则数", "命中规则明细", "建议动作汇总",
                "处理时间"
            ])
            for r in results:
                rules_str = " | ".join(
                    f"[{h.rule_code}] {h.rule_name}: {h.message}"
                    for h in r.rule_hits
                ) if r.rule_hits else "-"
                actions = list(set(
                    h.suggested_action for h in r.rule_hits if h.suggested_action
                ))
                actions_str = " | ".join(actions) if actions else "-"
                writer.writerow([
                    r.merchant.row_number,
                    r.merchant.merchant_id,
                    r.merchant.merchant_name,
                    r.risk_score,
                    r.risk_level.display_name,
                    r.final_decision.display_name,
                    "是" if r.is_whitelisted else "否",
                    r.review_reason or "-",
                    len(r.rule_hits),
                    rules_str,
                    actions_str,
                    r.processed_at.strftime("%Y-%m-%d %H:%M:%S")
                ])

    def _write_errors_csv(self, file_path: Path, errors: List[Dict[str, Any]]):
        import csv
        with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["行号", "商户编号", "商户名称", "错误信息", "原始数据"])
            for e in errors:
                writer.writerow([
                    e["row_number"],
                    e.get("merchant_id", ""),
                    e.get("merchant_name", ""),
                    e["error"],
                    json.dumps(e.get("raw_data", {}), ensure_ascii=False)
                ])
