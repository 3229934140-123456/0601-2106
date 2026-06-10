import csv
from pathlib import Path
from typing import List, Tuple, Dict, Any

from .models import Merchant


REQUIRED_COLUMNS = [
    "merchant_id",
    "merchant_name",
    "operation_years",
    "revenue_last_month",
    "revenue_month_before",
    "revenue_3months_ago",
    "legal_person_change_count",
    "legal_person_name",
    "address",
    "address_status",
    "phone",
    "contact_person"
]

COLUMN_ALIASES = {
    "商户编号": "merchant_id",
    "商户名称": "merchant_name",
    "经营年限": "operation_years",
    "上月营收": "revenue_last_month",
    "前月营收": "revenue_month_before",
    "3月前营收": "revenue_3months_ago",
    "法人变更次数": "legal_person_change_count",
    "法人姓名": "legal_person_name",
    "经营地址": "address",
    "地址状态": "address_status",
    "联系电话": "phone",
    "联系人": "contact_person",
    "身份证号": "id_card",
    "所属行业": "industry"
}


class DataImporter:
    @staticmethod
    def read_csv(file_path: str) -> Tuple[List[Merchant], List[Dict[str, Any]]]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        merchants: List[Merchant] = []
        errors: List[Dict[str, Any]] = []

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            normalized_headers = DataImporter._normalize_headers(headers)
            reader.fieldnames = normalized_headers

            missing = [c for c in REQUIRED_COLUMNS if c not in normalized_headers]
            if missing:
                raise ValueError(
                    f"CSV缺少必要列: {', '.join(missing)}\n"
                    f"必要列列表: {', '.join(REQUIRED_COLUMNS)}\n"
                    f"也支持中文列名: {', '.join(COLUMN_ALIASES.keys())}"
                )

            for idx, row in enumerate(reader, start=2):
                try:
                    merchant = DataImporter._parse_row(row, idx)
                    merchants.append(merchant)
                except Exception as e:
                    errors.append({
                        "row_number": idx,
                        "merchant_id": row.get("merchant_id", ""),
                        "merchant_name": row.get("merchant_name", ""),
                        "error": str(e),
                        "raw_data": dict(row)
                    })
        return merchants, errors

    @staticmethod
    def _normalize_headers(headers: List[str]) -> List[str]:
        normalized = []
        for h in headers:
            h = h.strip()
            if h in COLUMN_ALIASES:
                normalized.append(COLUMN_ALIASES[h])
            else:
                normalized.append(h)
        return normalized

    @staticmethod
    def _parse_row(row: Dict[str, str], row_number: int) -> Merchant:
        def _get(key: str, default: str = "") -> str:
            return (row.get(key) or default).strip()

        def _to_float(value: str, field: str) -> float:
            try:
                v = value.strip().replace(",", "")
                return float(v) if v else 0.0
            except (ValueError, TypeError):
                raise ValueError(f"{field} 格式错误: '{value}'，应为数字")

        def _to_int(value: str, field: str) -> int:
            try:
                v = value.strip().replace(",", "")
                return int(float(v)) if v else 0
            except (ValueError, TypeError):
                raise ValueError(f"{field} 格式错误: '{value}'，应为整数")

        mid = _get("merchant_id")
        mname = _get("merchant_name")
        if not mid:
            raise ValueError("商户编号不能为空")
        if not mname:
            raise ValueError("商户名称不能为空")

        return Merchant(
            row_number=row_number,
            merchant_id=mid,
            merchant_name=mname,
            operation_years=_to_float(_get("operation_years", "0"), "经营年限"),
            revenue_last_month=_to_float(_get("revenue_last_month", "0"), "上月营收"),
            revenue_month_before=_to_float(_get("revenue_month_before", "0"), "前月营收"),
            revenue_3months_ago=_to_float(_get("revenue_3months_ago", "0"), "3月前营收"),
            legal_person_change_count=_to_int(_get("legal_person_change_count", "0"), "法人变更次数"),
            legal_person_name=_get("legal_person_name"),
            address=_get("address"),
            address_status=_get("address_status"),
            phone=_get("phone"),
            contact_person=_get("contact_person"),
            id_card=_get("id_card"),
            industry=_get("industry"),
            extra={k: v for k, v in row.items() if k not in REQUIRED_COLUMNS and k not in ["id_card", "industry"]}
        )
