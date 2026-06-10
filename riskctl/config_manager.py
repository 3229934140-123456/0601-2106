import json
import os
from pathlib import Path
from typing import Dict, List, Any, Optional


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
BATCHES_DIR = DATA_DIR / "batches"
SAMPLES_DIR = DATA_DIR / "samples"


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
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
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
