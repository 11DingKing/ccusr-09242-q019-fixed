"""统计报告的生成、冻结与读取。

- 预览（build_report）：按当前数据即时计算，不写入任何内容
- 冻结（freeze_report）：报告确认后，把生成时的成员集合（各分组采用的
  graduate_id / follow_up_id）与规则版本一起写入 report_snapshots 表；
  后续回访、重新检测或成员变动都不会改写旧快照
- 读取（load_frozen_report）：从存储正文重建响应，并重新计算摘要校验完整性
"""

from datetime import datetime, timezone
import json
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models import ReportSnapshotRecord
from app.schemas import (
    ReportResponse,
    FrozenReportSummary,
    FrozenReportListResponse,
    STATISTICS_POLICY_VERSION,
)
from app.services.report_snapshot import canonical_payload, payload_digest


# 各报告口径的规则说明，随快照冻结，解释分母采用了哪些毕业生
POLICY_RULES: dict[str, dict[str, object]] = {
    "by-college": {
        "cohort": "各学院全体毕业生",
        "satisfaction": "只纳入有效评分(1-5且非空)的回访记录，按记录计数",
        "retention": "每位毕业生取最近一次is_still_employed非空的回访，按人计数；评分缺失不排除留任状态",
    },
    "by-micro-major": {
        "cohort": "未修读微专业全体毕业生 + 各微专业修读毕业生，互不重叠",
        "satisfaction": "只纳入有效评分(1-5且非空)的回访记录，按记录计数",
        "retention": "每位毕业生取最近一次is_still_employed非空的回访，按人计数；评分缺失不排除留任状态",
    },
    "by-year": {
        "cohort": "各毕业届次全体毕业生",
        "satisfaction": "只纳入有效评分(1-5且非空)的回访记录，按记录计数",
        "retention": "每位毕业生取最近一次is_still_employed非空的回访，按人计数；评分缺失不排除留任状态",
    },
    "by-employer-follow-up": {
        "cohort": "去向类型为就业的毕业生，按是否修读微专业拆分为两组",
        "satisfaction": "只纳入有效评分(1-5且非空)的回访记录，按记录计数",
        "retention": "每位毕业生取最近一次is_still_employed非空的回访，按人计数；评分缺失不排除留任状态",
    },
}

SUPPORTED_REPORT_TYPES = tuple(POLICY_RULES)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def build_report(report_type: str, builder: Callable[[], ReportResponse]) -> ReportResponse:
    """按当前数据生成实时预览（不冻结）。

    report_type 为内部口径标识（by-college 等），不覆盖响应中的中文 report_type。
    """
    if report_type not in POLICY_RULES:
        raise ValueError(f"不支持的报告口径: {report_type}")
    report = builder()
    report.policy_version = STATISTICS_POLICY_VERSION
    report.frozen = False
    return report


def _report_to_payload(report: ReportResponse) -> dict:
    """规范化为可冻结/可摘要的普通字典（剔除冻结状态字段本身）。"""
    payload = report.model_dump(mode="json")
    payload["frozen"] = True
    return payload


def freeze_report(
    db: Session,
    report_type: str,
    builder: Callable[[], ReportResponse],
    *,
    created_by: str = "system",
    snapshot_id: Optional[str] = None,
) -> ReportSnapshotRecord:
    """确认报告后冻结：固化成员集合与规则版本，只新增、不覆盖。"""
    if report_type not in POLICY_RULES:
        raise ValueError(f"不支持的报告口径: {report_type}")

    report = build_report(report_type, builder)
    frozen_at = _now_utc()
    snapshot_id = snapshot_id or f"{report_type}-{frozen_at.strftime('%Y%m%d%H%M%S%f')}"
    frozen_at_text = frozen_at.strftime("%Y-%m-%d %H:%M:%S")

    payload = _report_to_payload(report)
    # 冻结标识与冻结时刻固化进正文；摘要本身不进正文（自引用）
    payload["snapshot_id"] = snapshot_id
    payload["frozen_at"] = frozen_at_text
    canonical = canonical_payload(payload)
    digest = payload_digest(payload)

    existing = (
        db.query(ReportSnapshotRecord)
        .filter(ReportSnapshotRecord.snapshot_id == snapshot_id)
        .first()
    )
    if existing is not None:
        raise ValueError(f"快照标识已存在: {snapshot_id}")

    member_count = sum(item.total_count for item in report.data)

    record = ReportSnapshotRecord(
        snapshot_id=snapshot_id,
        report_type=report_type,
        policy_version=STATISTICS_POLICY_VERSION,
        policy_rules_json=json.dumps(
            {"version": STATISTICS_POLICY_VERSION, "rules": POLICY_RULES[report_type]},
            ensure_ascii=False,
        ),
        member_count=member_count,
        payload_json=canonical,
        digest=digest,
        created_by=created_by,
        frozen_at=frozen_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _record_to_report(record: ReportSnapshotRecord, *, verify: bool = True) -> ReportResponse:
    payload = json.loads(record.payload_json)
    if verify and payload_digest(payload) != record.digest:
        raise ValueError(f"报告快照 {record.snapshot_id} 摘要校验失败，正文可能已被篡改")
    report = ReportResponse.model_validate(payload)
    report.frozen = True
    report.snapshot_id = record.snapshot_id
    report.policy_version = record.policy_version
    report.frozen_at = record.frozen_at.strftime("%Y-%m-%d %H:%M:%S")
    report.digest = record.digest
    return report


def load_frozen_report(db: Session, snapshot_id: str) -> ReportResponse:
    """读取冻结报告；数据与生成时一致，不受后续回访/重新检测影响。"""
    record = (
        db.query(ReportSnapshotRecord)
        .filter(ReportSnapshotRecord.snapshot_id == snapshot_id)
        .first()
    )
    if record is None:
        raise KeyError(f"报告快照不存在: {snapshot_id}")
    return _record_to_report(record)


def list_frozen_reports(
    db: Session,
    report_type: Optional[str] = None,
) -> FrozenReportListResponse:
    query = db.query(ReportSnapshotRecord)
    if report_type:
        query = query.filter(ReportSnapshotRecord.report_type == report_type)
    records = query.order_by(ReportSnapshotRecord.frozen_at.desc()).all()
    return FrozenReportListResponse(
        total=len(records),
        data=[
            FrozenReportSummary(
                snapshot_id=record.snapshot_id,
                report_type=record.report_type,
                policy_version=record.policy_version,
                member_count=record.member_count,
                created_by=record.created_by,
                frozen_at=record.frozen_at.strftime("%Y-%m-%d %H:%M:%S"),
                digest=record.digest,
            )
            for record in records
        ],
    )
