import json
import os
import copy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
BATCHES_DIR = DATA_DIR / "batches"
SAMPLES_DIR = DATA_DIR / "samples"
VERSIONS_DIR = DATA_DIR / "config_versions"


DEFAULT_CONFIG = {
    "thresholds": {
        "min_operation_years": 1,
        "max_volatility_ratio": 0.5,
        "max_legal_person_change_count": 2,
        "duplicate_contact_threshold": 3,
        "pass_score_min": 0,
        "pass_score_max": 30,
        "review_score_min": 31,
        "review_score_max": 70,
        "reject_score_min": 71
    },
    "review_reasons": [
        "经营年限接近阈值",
        "交易波动较大需人工确认",
        "法人变更次数偏多",
        "地址信息存在异常标记",
        "联系方式出现重复",
        "风险分值处于灰区"
    ],
    "reject_reasons": [
        "经营年限不足",
        "交易波动异常剧烈",
        "法人变更过于频繁",
        "黑名单命中",
        "地址严重异常",
        "联系方式严重重复"
    ],
    "suggested_actions": {
        "operation_years_low": "要求补充经营证明材料或缩短授信期限",
        "volatility_high": "要求提供近6个月银行流水并核实交易背景",
        "legal_person_change": "核实股权变更背景，要求实际控制人担保",
        "blacklist_hit": "直接拒绝，记录黑名单原因",
        "address_abnormal": "上门核实经营地址，要求提供租赁合同",
        "contact_duplicate": "排查关联商户，要求联系人书面说明关系"
    },
    "blacklist": [],
    "whitelist": []
}


class ConfigManager:
    def __init__(self, config_path: Optional[Path] = None, versions_dir: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.versions_dir = Path(versions_dir) if versions_dir else VERSIONS_DIR
        self._ensure_config_exists()
        self._config = self._load_config()

    def _ensure_config_exists(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self._save_config(DEFAULT_CONFIG)

    def _load_config(self) -> Dict[str, Any]:
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_config(self, config: Dict[str, Any]):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    THRESHOLD_SCHEMA = {
        "min_operation_years": {
            "type": float,
            "min": 0.0,
            "max": 50.0,
            "integer_only": False,
            "label": "最低经营年限",
            "desc": "应在 0~50 年之间"
        },
        "max_volatility_ratio": {
            "type": float,
            "min": 0.01,
            "max": 2.0,
            "integer_only": False,
            "label": "交易波动阈值",
            "desc": "应在 0.01~2.0 之间（1.0表示波动100%）"
        },
        "max_legal_person_change_count": {
            "type": int,
            "min": 0,
            "max": 20,
            "integer_only": True,
            "label": "法人变更阈值",
            "desc": "应为 0~20 之间的整数"
        },
        "duplicate_contact_threshold": {
            "type": int,
            "min": 2,
            "max": 100,
            "integer_only": True,
            "label": "联系方式重复阈值",
            "desc": "应为 2~100 之间的整数"
        },
        "pass_score_min": {
            "type": int,
            "min": 0,
            "max": 200,
            "integer_only": True,
            "label": "通过分数下限",
            "desc": "应为 0~200 之间的整数"
        },
        "pass_score_max": {
            "type": int,
            "min": 0,
            "max": 200,
            "integer_only": True,
            "label": "通过分数上限",
            "desc": "应为 0~200 之间的整数"
        },
        "review_score_min": {
            "type": int,
            "min": 0,
            "max": 200,
            "integer_only": True,
            "label": "复核分数下限",
            "desc": "应为 0~200 之间的整数"
        },
        "review_score_max": {
            "type": int,
            "min": 0,
            "max": 200,
            "integer_only": True,
            "label": "复核分数上限",
            "desc": "应为 0~200 之间的整数"
        },
        "reject_score_min": {
            "type": int,
            "min": 0,
            "max": 200,
            "integer_only": True,
            "label": "拒绝分数下限",
            "desc": "应为 0~200 之间的整数"
        },
    }

    SCORE_RANGE_KEYS = [
        "pass_score_min", "pass_score_max",
        "review_score_min", "review_score_max",
        "reject_score_min"
    ]

    def validate_threshold_value(self, key: str, raw_value: str) -> Any:
        if key not in self.THRESHOLD_SCHEMA:
            raise ValueError(
                f"未知的阈值参数: '{key}'\n"
                f"可用参数: {', '.join(self.THRESHOLD_SCHEMA.keys())}"
            )

        schema = self.THRESHOLD_SCHEMA[key]
        label = schema["label"]

        cleaned = str(raw_value).strip()

        if schema["integer_only"]:
            if not self._is_integer_like(cleaned):
                raise ValueError(
                    f"[{label}] '{key}' 必须是整数\n"
                    f"  你输入的是: '{raw_value}'\n"
                    f"  说明: {schema['desc']}"
                )
            parsed_val = int(cleaned)
        else:
            try:
                parsed_val = float(cleaned)
            except (ValueError, TypeError):
                raise ValueError(
                    f"[{label}] '{key}' 必须是数字\n"
                    f"  你输入的是: '{raw_value}'\n"
                    f"  说明: {schema['desc']}"
                )

        if parsed_val < schema["min"] or parsed_val > schema["max"]:
            raise ValueError(
                f"[{label}] '{key}' 超出合理范围\n"
                f"  你输入的是: {parsed_val}\n"
                f"  允许范围: {schema['min']} ~ {schema['max']}\n"
                f"  说明: {schema['desc']}"
            )

        return parsed_val

    def validate_score_consistency(
        self, updated_key: Optional[str] = None,
        temp_value: Optional[Any] = None
    ) -> List[str]:
        issues = []
        t = dict(self._config.get("thresholds", {}))
        if updated_key and temp_value is not None:
            t[updated_key] = temp_value

        for k in self.SCORE_RANGE_KEYS:
            if k not in t or t[k] is None:
                issues.append(f"分数区间参数缺失: {k}")

        if issues:
            return issues

        p_min, p_max = t["pass_score_min"], t["pass_score_max"]
        r_min, r_max = t["review_score_min"], t["review_score_max"]
        j_min = t["reject_score_min"]

        if p_min > p_max:
            issues.append(
                f"[通过区间] 下限({p_min}) > 上限({p_max})，上下限颠倒"
            )

        if r_min > r_max:
            issues.append(
                f"[复核区间] 下限({r_min}) > 上限({r_max})，上下限颠倒"
            )

        if p_max + 1 != r_min:
            issues.append(
                f"[区间衔接] 通过上限({p_max}) + 1 应 = 复核下限({r_min})，"
                f"差值为 {r_min - p_max - 1}"
            )

        if r_max + 1 != j_min:
            issues.append(
                f"[区间衔接] 复核上限({r_max}) + 1 应 = 拒绝下限({j_min})，"
                f"差值为 {j_min - r_max - 1}"
            )

        if not (p_max < r_min <= r_max < j_min):
            issues.append(
                f"[区间交叉] 顺序应为: 通过(0~{p_max}) < 复核({r_min}~{r_max}) < 拒绝({j_min}~)，"
                f"当前存在重叠或顺序混乱"
            )

        return issues

    @staticmethod
    def _is_integer_like(s: str) -> bool:
        s = s.strip()
        if not s:
            return False
        if s.startswith("-"):
            s = s[1:]
        return s.isdigit()

    def set_threshold(self, key: str, value: Any):
        if "thresholds" not in self._config:
            self._config["thresholds"] = {}
        self._config["thresholds"][key] = value
        self._save_config(self._config)

    def add_blacklist(self, item: str):
        if item not in self._config["blacklist"]:
            self._config["blacklist"].append(item)
            self._save_config(self._config)
            return True
        return False

    def remove_blacklist(self, item: str):
        if item in self._config["blacklist"]:
            self._config["blacklist"].remove(item)
            self._save_config(self._config)
            return True
        return False

    def add_whitelist(self, item: str):
        if item not in self._config["whitelist"]:
            self._config["whitelist"].append(item)
            self._save_config(self._config)
            return True
        return False

    def remove_whitelist(self, item: str):
        if item in self._config["whitelist"]:
            self._config["whitelist"].remove(item)
            self._save_config(self._config)
            return True
        return False

    def add_review_reason(self, reason: str):
        if reason not in self._config["review_reasons"]:
            self._config["review_reasons"].append(reason)
            self._save_config(self._config)
            return True
        return False

    def remove_review_reason(self, reason: str):
        if reason in self._config["review_reasons"]:
            self._config["review_reasons"].remove(reason)
            self._save_config(self._config)
            return True
        return False

    def reset(self):
        self._config = DEFAULT_CONFIG.copy()
        self._save_config(self._config)

    def get_all(self) -> Dict[str, Any]:
        return self._config

    def _ensure_versions_dir(self):
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    def save_version(self, name: str, description: str = "") -> bool:
        self._ensure_versions_dir()
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        if not safe_name:
            raise ValueError("版本名不能为空或全为非法字符")
        version_file = self.versions_dir / f"{safe_name}.json"
        if version_file.exists():
            return False
        version_data = {
            "name": safe_name,
            "description": description,
            "created_at": datetime.now().isoformat(),
            "config": copy.deepcopy(self._config)
        }
        with open(version_file, "w", encoding="utf-8") as f:
            json.dump(version_data, f, ensure_ascii=False, indent=2)
        return True

    def list_versions(self) -> List[Dict[str, Any]]:
        self._ensure_versions_dir()
        versions = []
        for f in sorted(self.versions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                versions.append({
                    "name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "created_at": data.get("created_at", ""),
                    "file_path": str(f)
                })
            except Exception:
                continue
        return versions

    def get_version(self, name: str) -> Optional[Dict[str, Any]]:
        self._ensure_versions_dir()
        version_file = self.versions_dir / f"{name}.json"
        if not version_file.exists():
            alt_files = list(self.versions_dir.glob(f"*{name}*.json"))
            if alt_files:
                version_file = alt_files[0]
            else:
                return None
        with open(version_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_version(self, name: str) -> bool:
        version_data = self.get_version(name)
        if not version_data:
            return False
        self._config = copy.deepcopy(version_data["config"])
        self._save_config(self._config)
        return True

    def diff_versions(self, name_a: str, name_b: str) -> List[Dict[str, Any]]:
        va = self.get_version(name_a)
        vb = self.get_version(name_b)
        if not va or not vb:
            missing = []
            if not va:
                missing.append(name_a)
            if not vb:
                missing.append(name_b)
            raise ValueError(f"版本不存在: {', '.join(missing)}")
        ca = va["config"]
        cb = vb["config"]
        diffs = []
        all_keys = set(list(ca.keys()) + list(cb.keys()))
        for top_key in sorted(all_keys):
            va_val = ca.get(top_key)
            vb_val = cb.get(top_key)
            if isinstance(va_val, dict) and isinstance(vb_val, dict):
                sub_keys = set(list(va_val.keys()) + list(vb_val.keys()))
                for sk in sorted(sub_keys):
                    v1 = va_val.get(sk)
                    v2 = vb_val.get(sk)
                    if v1 != v2:
                        diffs.append({
                            "path": f"{top_key}.{sk}",
                            "value_a": v1,
                            "value_b": v2,
                            "change": self._describe_change(v1, v2)
                        })
            elif isinstance(va_val, list) and isinstance(vb_val, list):
                added = [x for x in vb_val if x not in va_val]
                removed = [x for x in va_val if x not in vb_val]
                if added or removed:
                    diffs.append({
                        "path": top_key,
                        "value_a": va_val,
                        "value_b": vb_val,
                        "added": added,
                        "removed": removed,
                        "change": f"新增 {len(added)} 项，移除 {len(removed)} 项"
                    })
            else:
                if va_val != vb_val:
                    diffs.append({
                        "path": top_key,
                        "value_a": va_val,
                        "value_b": vb_val,
                        "change": self._describe_change(va_val, vb_val)
                    })
        return diffs

    @staticmethod
    def _describe_change(v1: Any, v2: Any) -> str:
        try:
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                diff = v2 - v1
                pct = f"({diff:+.1f})" if isinstance(diff, float) else f"({diff:+d})"
                return f"{v1} → {v2} {pct}"
        except Exception:
            pass
        return f"{v1} → {v2}"

    def delete_version(self, name: str) -> bool:
        version_file = self.versions_dir / f"{name}.json"
        if version_file.exists():
            version_file.unlink()
            return True
        return False
