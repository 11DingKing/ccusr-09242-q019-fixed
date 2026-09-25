from typing import Optional, List, Dict, Tuple
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from collections import Counter

from app.core import get_db
from app.models import (
    Graduate,
    College,
    MicroMajor,
    EmployerFollowUp,
    DestinationStatus,
    DestinationType,
    Warning,
    WarningStatus,
    WarningLevel,
    WarningType,
    AttributionRecord,
    AttributionCategory,
    ProvinceReferenceLine,
    ReportSnapshotRecord,
)
from app.schemas import (
    ComparisonStats,
    FollowUpComparisonStats,
    YearlyTrendResponse,
    YearlyTrendItem,
    ReportResponse,
    ReportItem,
    WarningListResponse,
    WarningListItem,
    AttributionDistributionResponse,
    AttributionDistributionItem,
    SampleScopeResponse,
    SampleScopeSummary,
    SampleScopeRecordItem,
    ReportConfirmRequest,
    ReportConfirmResponse,
    ReportSnapshotResponse,
    ReportSnapshotRow,
    ReportSnapshotListItem,
    ReportSnapshotListResponse,
)
from app.services.report_snapshot import payload_digest
from app.services.sample_scope import (
    SAMPLE_RULE_VERSION,
    evaluate_follow_ups,
    summarize_decisions,
)
from app.utils import (
    get_comparison_stats,
    get_follow_up_comparison,
    calculate_group_stats,
    format_salary_display,
    _format_satisfaction,
    _format_retention,
    run_warning_detection_for_target,
    calculate_yearly_indicators,
)
from app.utils.stats_calculator import _eager_load_follow_ups

router = APIRouter(prefix="/statistics", tags=["统计分析"])


def _report_item_from_stats(dimension: str, dimension_value: str, stats) -> ReportItem:
    return ReportItem(
        dimension=dimension,
        dimension_value=dimension_value,
        total_count=stats.total_count,
        confirmed_rate=stats.confirmed_rate,
        aligned_rate=stats.aligned_rate,
        avg_salary_display=stats.avg_salary_display,
        avg_satisfaction_display=stats.avg_satisfaction_display,
        retention_rate_display=stats.retention_rate_display,
        follow_up_count=stats.follow_up_count,
        satisfaction_sample_count=stats.satisfaction_sample_count,
        retention_sample_count=stats.retention_sample_count,
        sample_rule_version=stats.sample_rule_version,
    )


def _select_graduates(
    db: Session,
    graduation_year: Optional[int] = None,
    college_id: Optional[int] = None,
    micro_major_id: Optional[int] = None,
    has_micro_major: Optional[bool] = None,
) -> List[Graduate]:
    query = db.query(Graduate)

    filters = []
    if graduation_year:
        filters.append(Graduate.graduation_year == graduation_year)
    if college_id:
        filters.append(Graduate.college_id == college_id)
    if micro_major_id:
        filters.append(Graduate.has_micro_major == True)
        filters.append(Graduate.micro_major_id == micro_major_id)
    elif has_micro_major is not None:
        filters.append(Graduate.has_micro_major == has_micro_major)

    if filters:
        query = query.filter(and_(*filters))

    return query.all()


def _collect_follow_ups(graduates: List[Graduate]) -> List[EmployerFollowUp]:
    follow_ups = []
    for g in graduates:
        if hasattr(g, 'follow_ups') and g.follow_ups:
            follow_ups.extend(g.follow_ups)
    return follow_ups


def _build_sample_scope_response(graduates: List[Graduate]) -> SampleScopeResponse:
    decisions = evaluate_follow_ups(_collect_follow_ups(graduates))
    summary = summarize_decisions(decisions)
    return SampleScopeResponse(
        summary=SampleScopeSummary(
            sample_rule_version=SAMPLE_RULE_VERSION,
            graduate_count=len(graduates),
            member_ids=sorted(g.id for g in graduates),
            follow_up_count=summary["follow_up_count"],
            satisfaction_sample_count=summary["satisfaction_sample_count"],
            satisfaction_excluded_count=summary["satisfaction_excluded_count"],
            retention_sample_count=summary["retention_sample_count"],
            avg_satisfaction=summary["avg_satisfaction"],
            retention_rate=summary["retention_rate"],
        ),
        records=[SampleScopeRecordItem(**decision.to_dict()) for decision in decisions],
    )


@router.get("/comparison", response_model=ComparisonStats)
def get_group_comparison(
    graduation_year: Optional[int] = Query(None, description="毕业届次"),
    college_id: Optional[int] = Query(None, description="学院ID"),
    micro_major_id: Optional[int] = Query(None, description="微专业ID"),
    db: Session = Depends(get_db),
):
    return get_comparison_stats(
        db=db,
        graduation_year=graduation_year,
        college_id=college_id,
        micro_major_id=micro_major_id
    )


@router.get("/follow-up-comparison", response_model=FollowUpComparisonStats)
def get_follow_up_group_comparison(
    graduation_year: Optional[int] = Query(None, description="毕业届次"),
    college_id: Optional[int] = Query(None, description="学院ID"),
    micro_major_id: Optional[int] = Query(None, description="微专业ID"),
    db: Session = Depends(get_db),
):
    return get_follow_up_comparison(
        db=db,
        graduation_year=graduation_year,
        college_id=college_id,
        micro_major_id=micro_major_id,
    )


@router.get("/sample-scope", response_model=SampleScopeResponse)
def get_sample_scope(
    graduation_year: Optional[int] = Query(None, description="毕业届次"),
    college_id: Optional[int] = Query(None, description="学院ID"),
    micro_major_id: Optional[int] = Query(None, description="微专业ID"),
    has_micro_major: Optional[bool] = Query(None, description="是否修读微专业"),
    db: Session = Depends(get_db),
):
    """查看当前统计口径下的成员集合与每条回访记录的采用结论。

    返回的记录仅含内部标识与统计取值，不包含姓名、学号等个人信息。
    """
    graduates = _select_graduates(
        db,
        graduation_year=graduation_year,
        college_id=college_id,
        micro_major_id=micro_major_id,
        has_micro_major=has_micro_major,
    )
    _eager_load_follow_ups(db, graduates)
    return _build_sample_scope_response(graduates)


@router.get("/trend/{micro_major_id}", response_model=YearlyTrendResponse)
def get_yearly_trend(
    micro_major_id: int,
    run_detection: bool = Query(False, description="是否先运行预警检测"),
    db: Session = Depends(get_db),
):
    micro_major = db.query(MicroMajor).filter(MicroMajor.id == micro_major_id).first()
    if not micro_major:
        raise HTTPException(status_code=404, detail="微专业不存在")

    if run_detection:
        run_warning_detection_for_target(db, "micro_major", micro_major_id)

    active_warnings = db.query(Warning).filter(
        Warning.target_type == "micro_major",
        Warning.target_id == micro_major_id,
        Warning.status == WarningStatus.ACTIVE,
    ).all()

    years = db.query(Graduate.graduation_year).distinct().order_by(
        Graduate.graduation_year
    ).all()
    years = [y[0] for y in years]

    yearly_indicators = calculate_yearly_indicators(db, "micro_major", micro_major_id)
    indicator_map = {d["year"]: d for d in yearly_indicators}

    trend = []
    for year in years:
        all_graduates = db.query(Graduate).filter(
            Graduate.graduation_year == year
        ).all()

        with_micro = [
            g for g in all_graduates
            if g.has_micro_major and g.micro_major_id == micro_major_id
        ]
        without_micro = [
            g for g in all_graduates
            if not g.has_micro_major
        ]

        with_micro_count = len(with_micro)
        without_micro_count = len(without_micro)

        def calc_confirmed_rate(grads):
            if not grads:
                return 0.0
            confirmed = [
                g for g in grads
                if g.destination_status in (DestinationStatus.CONFIRMED, DestinationStatus.VERIFIED)
            ]
            return round((len(confirmed) / len(grads)) * 100, 2)

        year_warnings = [
            w for w in active_warnings
            if w.start_year <= year <= w.end_year
        ]
        has_warning = len(year_warnings) > 0
        warning_types = [w.warning_type.value for w in year_warnings]

        indicator = indicator_map.get(year, {})

        trend.append(YearlyTrendItem(
            year=year,
            with_micro_rate=calc_confirmed_rate(with_micro),
            without_micro_rate=calc_confirmed_rate(without_micro),
            with_micro_count=with_micro_count,
            without_micro_count=without_micro_count,
            has_warning=has_warning,
            warning_types=warning_types,
            confirmed_rate=indicator.get("confirmed_rate", 0.0),
            aligned_rate=indicator.get("aligned_rate", 0.0),
        ))

    return YearlyTrendResponse(
        micro_major_name=micro_major.name,
        trend=trend,
        has_active_warnings=len(active_warnings) > 0,
        active_warning_count=len(active_warnings),
    )


def _build_college_rows(db: Session) -> List[Tuple[ReportItem, List[Graduate]]]:
    rows = []
    for college in db.query(College).all():
        graduates = db.query(Graduate).filter(
            Graduate.college_id == college.id
        ).all()
        _eager_load_follow_ups(db, graduates)
        stats = calculate_group_stats(graduates)
        rows.append((_report_item_from_stats("学院", college.name, stats), graduates))
    return rows


def _build_micro_major_rows(db: Session) -> List[Tuple[ReportItem, List[Graduate]]]:
    rows = []

    all_without_micro = db.query(Graduate).filter(
        Graduate.has_micro_major == False
    ).all()
    _eager_load_follow_ups(db, all_without_micro)
    stats_all = calculate_group_stats(all_without_micro)
    rows.append((_report_item_from_stats("微专业", "未修读微专业", stats_all), all_without_micro))

    for mm in db.query(MicroMajor).all():
        graduates = db.query(Graduate).filter(
            Graduate.micro_major_id == mm.id,
            Graduate.has_micro_major == True
        ).all()
        _eager_load_follow_ups(db, graduates)
        stats = calculate_group_stats(graduates)
        rows.append((_report_item_from_stats("微专业", mm.name, stats), graduates))
    return rows


def _build_year_rows(db: Session) -> List[Tuple[ReportItem, List[Graduate]]]:
    years = db.query(Graduate.graduation_year).distinct().order_by(
        Graduate.graduation_year
    ).all()
    years = [y[0] for y in years]
    rows = []

    for year in years:
        graduates = db.query(Graduate).filter(
            Graduate.graduation_year == year
        ).all()
        _eager_load_follow_ups(db, graduates)
        stats = calculate_group_stats(graduates)
        rows.append((_report_item_from_stats("届次", f"{year}届", stats), graduates))
    return rows


def _build_employer_follow_up_rows(db: Session) -> List[Tuple[ReportItem, List[Graduate]]]:
    employed_graduates = db.query(Graduate).filter(
        Graduate.destination_type == DestinationType.EMPLOYMENT
    ).all()
    _eager_load_follow_ups(db, employed_graduates)

    with_micro = [g for g in employed_graduates if g.has_micro_major]
    without_micro = [g for g in employed_graduates if not g.has_micro_major]

    stats_with = calculate_group_stats(with_micro)
    stats_without = calculate_group_stats(without_micro)

    return [
        (_report_item_from_stats("用人单位回访", "修读微专业", stats_with), with_micro),
        (_report_item_from_stats("用人单位回访", "未修读微专业", stats_without), without_micro),
    ]


REPORT_BUILDERS = {
    "by-college": ("按学院统计", _build_college_rows),
    "by-micro-major": ("按微专业统计", _build_micro_major_rows),
    "by-year": ("按届次统计", _build_year_rows),
    "by-employer-follow-up": ("用人单位回访对照统计", _build_employer_follow_up_rows),
}


def _report_response(report_type_key: str, db: Session) -> ReportResponse:
    report_name, builder = REPORT_BUILDERS[report_type_key]
    rows = builder(db)
    return ReportResponse(
        report_type=report_name,
        data=[item for item, _ in rows],
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@router.get("/reports/by-college", response_model=ReportResponse)
def get_report_by_college(db: Session = Depends(get_db)):
    return _report_response("by-college", db)


@router.get("/reports/by-micro-major", response_model=ReportResponse)
def get_report_by_micro_major(db: Session = Depends(get_db)):
    return _report_response("by-micro-major", db)


@router.get("/reports/by-year", response_model=ReportResponse)
def get_report_by_year(db: Session = Depends(get_db)):
    return _report_response("by-year", db)


@router.get("/reports/by-employer-follow-up", response_model=ReportResponse)
def get_report_by_employer_follow_up(db: Session = Depends(get_db)):
    return _report_response("by-employer-follow-up", db)


def _snapshot_to_response(record: ReportSnapshotRecord) -> ReportSnapshotResponse:
    payload = record.payload or {}
    return ReportSnapshotResponse(
        snapshot_id=record.snapshot_id,
        report_type=record.report_type,
        report_name=payload.get("report_name", record.report_type),
        sample_rule_version=record.sample_rule_version,
        status=record.status,
        confirmed_by=record.confirmed_by,
        confirmed_at=record.confirmed_at,
        note=record.note,
        digest=record.digest,
        rows=[ReportSnapshotRow(**row) for row in payload.get("rows", [])],
    )


@router.post("/reports/{report_type}/confirm", response_model=ReportConfirmResponse)
def confirm_report(
    report_type: str,
    request: ReportConfirmRequest,
    db: Session = Depends(get_db),
):
    """确认报表并冻结生成时的成员集合、采用记录与口径版本。

    快照按内容寻址：数据未变时重复确认返回同一快照；数据变化后再次确认
    会生成新快照，旧快照保持原样，后续回访或重新检测都不会改写它。
    """
    if report_type not in REPORT_BUILDERS:
        raise HTTPException(status_code=404, detail="未知的报表类型")
    if not request.confirmed_by or not request.confirmed_by.strip():
        raise HTTPException(status_code=400, detail="确认人不能为空")

    report_name, builder = REPORT_BUILDERS[report_type]
    rows = builder(db)

    payload_rows = []
    for item, graduates in rows:
        decisions = evaluate_follow_ups(_collect_follow_ups(graduates))
        payload_rows.append({
            "dimension": item.dimension,
            "dimension_value": item.dimension_value,
            "metrics": item.model_dump(),
            "member_ids": sorted(g.id for g in graduates),
            "records": [decision.to_dict() for decision in decisions],
        })

    payload = {
        "report_type": report_type,
        "report_name": report_name,
        "sample_rule_version": SAMPLE_RULE_VERSION,
        "rows": payload_rows,
    }
    digest = payload_digest(payload)
    snapshot_id = f"{report_type}:{digest[:16]}"

    existing = db.query(ReportSnapshotRecord).filter(
        ReportSnapshotRecord.snapshot_id == snapshot_id
    ).first()
    if existing:
        response = _snapshot_to_response(existing)
        return ReportConfirmResponse(**response.model_dump(), created=False)

    record = ReportSnapshotRecord(
        snapshot_id=snapshot_id,
        report_type=report_type,
        sample_rule_version=SAMPLE_RULE_VERSION,
        status="confirmed",
        confirmed_by=request.confirmed_by.strip(),
        confirmed_at=datetime.now(),
        note=request.note,
        digest=digest,
        payload=payload,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    response = _snapshot_to_response(record)
    return ReportConfirmResponse(**response.model_dump(), created=True)


@router.get("/report-snapshots", response_model=ReportSnapshotListResponse)
def list_report_snapshots(
    report_type: Optional[str] = Query(None, description="报表类型"),
    db: Session = Depends(get_db),
):
    query = db.query(ReportSnapshotRecord)
    if report_type:
        query = query.filter(ReportSnapshotRecord.report_type == report_type)
    records = query.order_by(
        ReportSnapshotRecord.confirmed_at.desc(),
        ReportSnapshotRecord.id.desc(),
    ).all()

    data = []
    for record in records:
        payload = record.payload or {}
        data.append(ReportSnapshotListItem(
            snapshot_id=record.snapshot_id,
            report_type=record.report_type,
            report_name=payload.get("report_name", record.report_type),
            sample_rule_version=record.sample_rule_version,
            status=record.status,
            confirmed_by=record.confirmed_by,
            confirmed_at=record.confirmed_at,
            digest=record.digest,
            row_count=len(payload.get("rows", [])),
        ))
    return ReportSnapshotListResponse(total=len(data), data=data)


@router.get("/report-snapshots/{snapshot_id}", response_model=ReportSnapshotResponse)
def get_report_snapshot(snapshot_id: str, db: Session = Depends(get_db)):
    """读取已冻结的报告快照，始终返回确认时的成员集合与采用记录。"""
    record = db.query(ReportSnapshotRecord).filter(
        ReportSnapshotRecord.snapshot_id == snapshot_id
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="报告快照不存在")
    return _snapshot_to_response(record)


@router.get("/reports/warnings", response_model=WarningListResponse)
def get_warning_report(
    status: Optional[str] = Query(None, description="预警状态：预警中/已解决/已忽略"),
    warning_level: Optional[str] = Query(None, description="预警级别"),
    target_type: Optional[str] = Query(None, description="预警对象类型：micro_major/college"),
    run_detection: bool = Query(False, description="是否先运行全量预警检测"),
    db: Session = Depends(get_db),
):
    if run_detection:
        from app.utils import run_full_warning_detection
        run_full_warning_detection(db)

    query = db.query(Warning)

    if status:
        query = query.filter(Warning.status == status)
    if warning_level:
        query = query.filter(Warning.warning_level == warning_level)
    if target_type:
        query = query.filter(Warning.target_type == target_type)

    warnings = query.order_by(
        Warning.warning_level.desc(),
        Warning.created_at.desc(),
    ).all()

    active_count = db.query(Warning).filter(Warning.status == WarningStatus.ACTIVE).count()
    resolved_count = db.query(Warning).filter(Warning.status == WarningStatus.RESOLVED).count()

    data = []
    for w in warnings:
        attribution_count = db.query(AttributionRecord).filter(
            AttributionRecord.warning_id == w.id
        ).count()
        data.append(WarningListItem(
            id=w.id,
            warning_type=w.warning_type.value,
            warning_level=w.warning_level.value,
            status=w.status.value,
            target_type=w.target_type,
            target_id=w.target_id,
            target_name=w.target_name,
            indicator=w.indicator,
            current_value=w.current_value,
            province_value=w.province_value,
            gap=w.gap,
            start_year=w.start_year,
            end_year=w.end_year,
            decline_count=w.decline_count,
            description=w.description,
            attribution_count=attribution_count,
            created_at=w.created_at,
        ))

    return WarningListResponse(
        total=len(warnings),
        active_count=active_count,
        resolved_count=resolved_count,
        data=data,
    )


@router.get("/reports/attribution-distribution", response_model=AttributionDistributionResponse)
def get_attribution_distribution_report(
    target_type: Optional[str] = Query(None, description="对象类型：micro_major/college"),
    db: Session = Depends(get_db),
):
    query = db.query(AttributionRecord)

    if target_type:
        warning_query = db.query(Warning.id).filter(Warning.target_type == target_type)
        warning_ids = [w[0] for w in warning_query.all()]
        if warning_ids:
            query = query.filter(AttributionRecord.warning_id.in_(warning_ids))
        else:
            query = query.filter(False)

    all_records = query.all()
    total_records = len(all_records)

    category_counter = Counter()
    examples_by_category = {}
    for record in all_records:
        cat_value = record.category.value if hasattr(record.category, 'value') else str(record.category)
        category_counter[cat_value] += 1
        if cat_value not in examples_by_category:
            examples_by_category[cat_value] = []
        if len(examples_by_category[cat_value]) < 3:
            warning = db.query(Warning).filter(Warning.id == record.warning_id).first()
            examples_by_category[cat_value].append({
                "record_id": record.id,
                "target_name": warning.target_name if warning else "未知",
                "target_type": warning.target_type if warning else "unknown",
                "description": record.description[:100] + "..." if len(record.description) > 100 else record.description,
            })

    distribution = []
    for cat in AttributionCategory:
        count = category_counter.get(cat.value, 0)
        percentage = round((count / total_records * 100), 2) if total_records > 0 else 0
        distribution.append(AttributionDistributionItem(
            category=cat.value,
            count=count,
            percentage=percentage,
            examples=examples_by_category.get(cat.value, []),
        ))

    target_counter = Counter()
    for record in all_records:
        warning = db.query(Warning).filter(Warning.id == record.warning_id).first()
        if warning:
            key = f"{warning.target_type}:{warning.target_id}:{warning.target_name}"
            target_counter[key] += 1

    top_targets = []
    for key, count in target_counter.most_common(5):
        t_type, t_id, t_name = key.split(":", 2)
        top_targets.append({
            "target_type": t_type,
            "target_id": int(t_id),
            "target_name": t_name,
            "attribution_count": count,
        })

    return AttributionDistributionResponse(
        total_records=total_records,
        distribution=distribution,
        top_targets=top_targets,
    )
