"""满意度与留任率的可追溯样本口径。

口径版本 SAMPLE_RULE_VERSION 定义了：
- 满意度：只纳入 1-5 的有效评分，缺失或越界评分不进入分母；
- 留任率：每位毕业生取最近一次有效回访（有回访日期且在职状态非空），
  评分缺失不影响留任状态的纳入；
- 每条回访记录都给出纳入/排除原因，报告可追溯到具体采用记录。
"""

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional, Sequence

SAMPLE_RULE_VERSION = "sample-v1"

MIN_SATISFACTION_SCORE = 1.0
MAX_SATISFACTION_SCORE = 5.0

REASON_SCORE_MISSING = "评分缺失"
REASON_SCORE_OUT_OF_RANGE = "评分超出1-5有效范围"
REASON_SUPERSEDED = "存在更新的有效回访"
REASON_EMPLOYMENT_UNKNOWN = "在职状态缺失"


def is_valid_satisfaction(score: Optional[float]) -> bool:
    """评分是否可纳入满意度：非空且落在 1-5 区间（含边界）。"""

    if score is None:
        return False
    return MIN_SATISFACTION_SCORE <= score <= MAX_SATISFACTION_SCORE


def satisfaction_exclusion_reason(score: Optional[float]) -> Optional[str]:
    """评分未纳入满意度时的原因；纳入时返回 None。"""

    if is_valid_satisfaction(score):
        return None
    if score is None:
        return REASON_SCORE_MISSING
    return REASON_SCORE_OUT_OF_RANGE


def is_valid_retention_record(record: object) -> bool:
    """回访是否可用于留任判定：有回访日期且在职状态非空。

    评分是否缺失与留任无关，缺失评分不能把留任状态排除。
    """

    return (
        getattr(record, "follow_up_date", None) is not None
        and getattr(record, "is_still_employed", None) is not None
    )


def _record_sort_key(record: object) -> tuple:
    follow_up_date = getattr(record, "follow_up_date", None) or date.min
    return (follow_up_date, getattr(record, "id", 0) or 0)


def select_retention_follow_up(records: Sequence[object]) -> Optional[object]:
    """在同一毕业生的回访中选出最近一次有效回访。

    按（回访日期, 记录ID）取最大，保证同日多条时结果确定。
    """

    valid = [record for record in records if is_valid_retention_record(record)]
    if not valid:
        return None
    return max(valid, key=_record_sort_key)


@dataclass(frozen=True)
class FollowUpDecision:
    """单条回访记录在统计口径下的采用结论。"""

    follow_up_id: int
    graduate_id: int
    follow_up_date: Optional[date]
    satisfaction_score: Optional[float]
    satisfaction_included: bool
    satisfaction_exclusion_reason: Optional[str]
    is_still_employed: Optional[bool]
    retention_selected: bool
    retention_exclusion_reason: Optional[str]

    def to_dict(self) -> dict:
        return {
            "follow_up_id": self.follow_up_id,
            "graduate_id": self.graduate_id,
            "follow_up_date": self.follow_up_date.isoformat() if self.follow_up_date else None,
            "satisfaction_score": self.satisfaction_score,
            "satisfaction_included": self.satisfaction_included,
            "satisfaction_exclusion_reason": self.satisfaction_exclusion_reason,
            "is_still_employed": self.is_still_employed,
            "retention_selected": self.retention_selected,
            "retention_exclusion_reason": self.retention_exclusion_reason,
        }


def evaluate_follow_ups(records: Iterable[object]) -> tuple[FollowUpDecision, ...]:
    """对一组回访记录逐条给出采用结论，按毕业生与回访时间稳定排序。"""

    materialized = list(records)
    by_graduate: dict[int, list[object]] = {}
    for record in materialized:
        by_graduate.setdefault(record.graduate_id, []).append(record)

    selected_ids = set()
    for graduate_records in by_graduate.values():
        selected = select_retention_follow_up(graduate_records)
        if selected is not None and getattr(selected, "id", None) is not None:
            selected_ids.add(selected.id)

    decisions = []
    for record in materialized:
        score = getattr(record, "satisfaction_score", None)
        satisfaction_reason = satisfaction_exclusion_reason(score)
        record_id = getattr(record, "id", None)
        if record_id is not None and record_id in selected_ids:
            retention_selected = True
            retention_reason = None
        elif not is_valid_retention_record(record):
            retention_selected = False
            retention_reason = REASON_EMPLOYMENT_UNKNOWN
        else:
            retention_selected = False
            retention_reason = REASON_SUPERSEDED
        decisions.append(FollowUpDecision(
            follow_up_id=record.id,
            graduate_id=record.graduate_id,
            follow_up_date=getattr(record, "follow_up_date", None),
            satisfaction_score=score,
            satisfaction_included=satisfaction_reason is None,
            satisfaction_exclusion_reason=satisfaction_reason,
            is_still_employed=getattr(record, "is_still_employed", None),
            retention_selected=retention_selected,
            retention_exclusion_reason=retention_reason,
        ))

    decisions.sort(key=lambda d: (
        d.graduate_id,
        d.follow_up_date or date.min,
        d.follow_up_id,
    ))
    return tuple(decisions)


def summarize_decisions(decisions: Sequence[FollowUpDecision]) -> dict:
    """汇总采用结论，给出满意度与留任率的分子分母。"""

    scores = [d.satisfaction_score for d in decisions if d.satisfaction_included]
    avg_satisfaction = round(sum(scores) / len(scores), 2) if scores else None

    retention_selected = [d for d in decisions if d.retention_selected]
    still_employed = sum(1 for d in retention_selected if d.is_still_employed)
    retention_rate = None
    if retention_selected:
        retention_rate = round(still_employed / len(retention_selected) * 100, 2)

    return {
        "graduate_ids": tuple(sorted({d.graduate_id for d in decisions})),
        "follow_up_count": len(decisions),
        "satisfaction_sample_count": len(scores),
        "satisfaction_excluded_count": len(decisions) - len(scores),
        "avg_satisfaction": avg_satisfaction,
        "retention_sample_count": len(retention_selected),
        "retention_still_employed_count": still_employed,
        "retention_rate": retention_rate,
    }
