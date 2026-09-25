from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import Graduate, EmployerFollowUp, DestinationStatus, DestinationType
from app.schemas import (
    GroupStats,
    ComparisonStats,
    FollowUpComparisonStats,
    SampleScope,
    SampleTraceItem,
    STATISTICS_POLICY_VERSION,
)
from .salary_utils import get_salary_midpoint, format_salary_display

# 满意度有效评分区间（含边界）
SATISFACTION_MIN = 1
SATISFACTION_MAX = 5

# 未纳入原因（写入追溯条目，供报告解释分母）
REASON_NO_FOLLOW_UP = "无回访记录"
REASON_NO_RETENTION_STATUS = "回访缺少留任状态"
REASON_NO_SATISFACTION = "满意度评分缺失"
REASON_SATISFACTION_OUT_OF_RANGE = "满意度评分越界"


def _is_valid_satisfaction(score) -> bool:
    """有效评分：非空且落在 [1, 5] 区间（含边界）。"""
    if score is None:
        return False
    return SATISFACTION_MIN <= score <= SATISFACTION_MAX


def _select_latest_valid_follow_up(follow_ups) -> Optional[EmployerFollowUp]:
    """取最近一次有效回访：is_still_employed 非空，按回访日期倒序，同日取 id 较大者。

    满意度评分缺失不影响留任状态的采用。
    """
    valid = [fu for fu in follow_ups if fu.is_still_employed is not None]
    if not valid:
        return None
    return max(valid, key=lambda fu: (fu.follow_up_date, fu.id or 0))


def _build_member_trace(graduate: Graduate, follow_ups) -> SampleTraceItem:
    """汇总单个毕业生在满意度/留任率口径中的采用情况（不含个人信息）。"""
    valid_scores = [fu for fu in follow_ups if _is_valid_satisfaction(fu.satisfaction_score)]
    missing_scores = [fu for fu in follow_ups if not _is_valid_satisfaction(fu.satisfaction_score)]
    selected = _select_latest_valid_follow_up(follow_ups)

    reasons = []
    if not follow_ups:
        reasons.append(REASON_NO_FOLLOW_UP)
    else:
        if selected is None:
            reasons.append(REASON_NO_RETENTION_STATUS)
        if not valid_scores:
            if any(fu.satisfaction_score is None for fu in missing_scores):
                reasons.append(REASON_NO_SATISFACTION)
            if any(fu.satisfaction_score is not None for fu in missing_scores):
                reasons.append(REASON_SATISFACTION_OUT_OF_RANGE)

    return SampleTraceItem(
        graduate_id=graduate.id,
        follow_up_count=len(follow_ups),
        satisfaction_valid_count=len(valid_scores),
        satisfaction_missing_count=len(missing_scores),
        retention_selected_follow_up_id=selected.id if selected else None,
        retention_selected_follow_up_date=(
            selected.follow_up_date.isoformat() if selected else None
        ),
        is_still_employed=selected.is_still_employed if selected else None,
        exclusion_reasons=reasons,
    )


def _build_sample_scope(
    graduates: List[Graduate],
    follow_ups_by_graduate,
    include_members: bool = True,
) -> SampleScope:
    traces = [
        _build_member_trace(g, follow_ups_by_graduate.get(g.id, []))
        for g in sorted(graduates, key=lambda item: item.id)
    ]
    return SampleScope(
        policy_version=STATISTICS_POLICY_VERSION,
        cohort_size=len(graduates),
        follow_up_count=sum(m.follow_up_count for m in traces),
        satisfaction_sample_count=sum(m.satisfaction_valid_count for m in traces),
        satisfaction_missing_count=sum(m.satisfaction_missing_count for m in traces),
        retention_sample_count=sum(
            1 for m in traces if m.retention_selected_follow_up_id is not None
        ),
        retention_missing_count=sum(
            1 for m in traces if REASON_NO_RETENTION_STATUS in m.exclusion_reasons
        ),
        retention_unreachable_count=sum(
            1 for m in traces if REASON_NO_FOLLOW_UP in m.exclusion_reasons
        ),
        members=traces if include_members else [],
    )


def calculate_group_stats(
    graduates: List[Graduate],
    include_sample_scope: bool = True,
) -> GroupStats:
    total_count = len(graduates)

    follow_ups_by_graduate = {}
    follow_ups = []
    for g in graduates:
        g_follow_ups = list(g.follow_ups) if hasattr(g, 'follow_ups') and g.follow_ups else []
        follow_ups_by_graduate[g.id] = g_follow_ups
        follow_ups.extend(g_follow_ups)

    scope = _build_sample_scope(
        graduates, follow_ups_by_graduate, include_members=include_sample_scope
    )

    if total_count == 0:
        return GroupStats(
            total_count=0,
            confirmed_count=0,
            confirmed_rate=0.0,
            aligned_count=0,
            aligned_rate=0.0,
            avg_salary=None,
            avg_salary_display="暂无数据",
            avg_satisfaction=None,
            avg_satisfaction_display="暂无数据",
            retention_rate=None,
            retention_rate_display="暂无数据",
            follow_up_count=0,
            sample_scope=scope if include_sample_scope else None,
        )

    confirmed_graduates = [
        g for g in graduates
        if g.destination_status in (DestinationStatus.CONFIRMED, DestinationStatus.VERIFIED)
    ]
    confirmed_count = len(confirmed_graduates)
    confirmed_rate = round((confirmed_count / total_count) * 100, 2)

    employed_graduates = [
        g for g in graduates
        if g.destination_type == DestinationType.EMPLOYMENT
    ]
    employed_count = len(employed_graduates)

    aligned_count = sum(1 for g in employed_graduates if g.is_aligned)
    aligned_rate = round((aligned_count / employed_count) * 100, 2) if employed_count > 0 else 0.0

    salaries = [
        get_salary_midpoint(g.salary_range)
        for g in graduates
        if g.salary_range is not None
    ]
    if salaries:
        avg_salary = round(sum(salaries) / len(salaries), 1)
    else:
        avg_salary = None

    # 满意度：只纳入有效评分（非空且在 1-5 之间），按回访记录计数
    satisfaction_scores = [
        fu.satisfaction_score for fu in follow_ups
        if _is_valid_satisfaction(fu.satisfaction_score)
    ]
    avg_satisfaction = (
        round(sum(satisfaction_scores) / len(satisfaction_scores), 2)
        if satisfaction_scores else None
    )

    # 留任率：每位毕业生取最近一次有效回访；缺失评分不排除留任状态
    latest_follow_ups = []
    for g in graduates:
        selected = _select_latest_valid_follow_up(follow_ups_by_graduate.get(g.id, []))
        if selected is not None:
            latest_follow_ups.append(selected)

    if latest_follow_ups:
        still_employed = sum(1 for fu in latest_follow_ups if fu.is_still_employed)
        retention_rate = round((still_employed / len(latest_follow_ups)) * 100, 2)
    else:
        retention_rate = None

    return GroupStats(
        total_count=total_count,
        confirmed_count=confirmed_count,
        confirmed_rate=confirmed_rate,
        aligned_count=aligned_count,
        aligned_rate=aligned_rate,
        avg_salary=avg_salary,
        avg_salary_display=format_salary_display(avg_salary),
        avg_satisfaction=avg_satisfaction,
        avg_satisfaction_display=_format_satisfaction(avg_satisfaction),
        retention_rate=retention_rate,
        retention_rate_display=_format_retention(retention_rate),
        follow_up_count=len(follow_ups),
        satisfaction_sample_count=scope.satisfaction_sample_count,
        satisfaction_missing_count=scope.satisfaction_missing_count,
        retention_sample_count=scope.retention_sample_count,
        retention_missing_count=scope.retention_missing_count,
        retention_unreachable_count=scope.retention_unreachable_count,
        policy_version=scope.policy_version,
        sample_scope=scope if include_sample_scope else None,
    )


def get_comparison_stats(
    db: Session,
    graduation_year: int = None,
    college_id: int = None,
    micro_major_id: int = None
) -> ComparisonStats:
    query = db.query(Graduate)

    filters = []
    if graduation_year:
        filters.append(Graduate.graduation_year == graduation_year)
    if college_id:
        filters.append(Graduate.college_id == college_id)

    if filters:
        query = query.filter(and_(*filters))

    all_graduates = query.all()

    _eager_load_follow_ups(db, all_graduates)

    if micro_major_id:
        with_micro = [
            g for g in all_graduates
            if g.has_micro_major and g.micro_major_id == micro_major_id
        ]
        without_micro = [
            g for g in all_graduates
            if not g.has_micro_major
        ]
    else:
        with_micro = [g for g in all_graduates if g.has_micro_major]
        without_micro = [g for g in all_graduates if not g.has_micro_major]

    return ComparisonStats(
        with_micro=calculate_group_stats(with_micro),
        without_micro=calculate_group_stats(without_micro)
    )


def get_follow_up_comparison(
    db: Session,
    graduation_year: int = None,
    college_id: int = None,
    micro_major_id: int = None
) -> FollowUpComparisonStats:
    query = db.query(Graduate)

    filters = []
    if graduation_year:
        filters.append(Graduate.graduation_year == graduation_year)
    if college_id:
        filters.append(Graduate.college_id == college_id)

    if filters:
        query = query.filter(and_(*filters))

    all_graduates = query.all()

    _eager_load_follow_ups(db, all_graduates)

    if micro_major_id:
        with_micro = [
            g for g in all_graduates
            if g.has_micro_major and g.micro_major_id == micro_major_id
        ]
        without_micro = [
            g for g in all_graduates
            if not g.has_micro_major
        ]
    else:
        with_micro = [g for g in all_graduates if g.has_micro_major]
        without_micro = [g for g in all_graduates if not g.has_micro_major]

    return FollowUpComparisonStats(
        with_micro=calculate_group_stats(with_micro),
        without_micro=calculate_group_stats(without_micro),
    )


def _eager_load_follow_ups(db: Session, graduates: List[Graduate]):
    if not graduates:
        return
    gids = [g.id for g in graduates]
    all_follow_ups = db.query(EmployerFollowUp).filter(
        EmployerFollowUp.graduate_id.in_(gids)
    ).all()

    fu_by_grad = {}
    for fu in all_follow_ups:
        fu_by_grad.setdefault(fu.graduate_id, []).append(fu)

    for g in graduates:
        g.follow_ups = fu_by_grad.get(g.id, [])


def _format_satisfaction(avg: float = None) -> str:
    if avg is None:
        return "暂无数据"
    return f"{avg:.1f}/5"


def _format_retention(rate: float = None) -> str:
    if rate is None:
        return "暂无数据"
    return f"{rate:.1f}%"
