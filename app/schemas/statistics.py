from typing import Optional, List
from datetime import date, datetime
from .common import BaseSchema
from app.services.sample_scope import SAMPLE_RULE_VERSION


class GroupStats(BaseSchema):
    total_count: int
    confirmed_count: int
    confirmed_rate: float
    aligned_count: int
    aligned_rate: float
    avg_salary: Optional[float]
    avg_salary_display: str
    avg_satisfaction: Optional[float]
    avg_satisfaction_display: str
    retention_rate: Optional[float]
    retention_rate_display: str
    follow_up_count: int
    satisfaction_sample_count: int = 0
    satisfaction_excluded_count: int = 0
    retention_sample_count: int = 0
    sample_rule_version: str = SAMPLE_RULE_VERSION


class FollowUpComparisonStats(BaseSchema):
    with_micro: GroupStats
    without_micro: GroupStats


class ComparisonStats(BaseSchema):
    with_micro: GroupStats
    without_micro: GroupStats


class YearlyTrendItem(BaseSchema):
    year: int
    with_micro_rate: float
    without_micro_rate: float
    with_micro_count: int
    without_micro_count: int
    has_warning: bool = False
    warning_types: List[str] = []
    confirmed_rate: float = 0.0
    aligned_rate: float = 0.0


class YearlyTrendResponse(BaseSchema):
    micro_major_name: str
    trend: List[YearlyTrendItem]
    has_active_warnings: bool = False
    active_warning_count: int = 0


class ReportItem(BaseSchema):
    dimension: str
    dimension_value: str
    total_count: int
    confirmed_rate: float
    aligned_rate: float
    avg_salary_display: str
    avg_satisfaction_display: str
    retention_rate_display: str
    follow_up_count: int
    satisfaction_sample_count: int = 0
    retention_sample_count: int = 0
    sample_rule_version: str = SAMPLE_RULE_VERSION


class ReportResponse(BaseSchema):
    report_type: str
    data: List[ReportItem]
    generated_at: str


class SampleScopeRecordItem(BaseSchema):
    """单条回访记录的采用结论，仅含统计证据，不含个人信息。"""

    follow_up_id: int
    graduate_id: int
    follow_up_date: Optional[date]
    satisfaction_score: Optional[float]
    satisfaction_included: bool
    satisfaction_exclusion_reason: Optional[str]
    is_still_employed: Optional[bool]
    retention_selected: bool
    retention_exclusion_reason: Optional[str]


class SampleScopeSummary(BaseSchema):
    sample_rule_version: str
    graduate_count: int
    member_ids: List[int]
    follow_up_count: int
    satisfaction_sample_count: int
    satisfaction_excluded_count: int
    retention_sample_count: int
    avg_satisfaction: Optional[float]
    retention_rate: Optional[float]


class SampleScopeResponse(BaseSchema):
    summary: SampleScopeSummary
    records: List[SampleScopeRecordItem]


class ReportConfirmRequest(BaseSchema):
    confirmed_by: str
    note: Optional[str] = None


class ReportSnapshotRow(BaseSchema):
    dimension: str
    dimension_value: str
    metrics: ReportItem
    member_ids: List[int]
    records: List[SampleScopeRecordItem]


class ReportSnapshotResponse(BaseSchema):
    snapshot_id: str
    report_type: str
    report_name: str
    sample_rule_version: str
    status: str
    confirmed_by: str
    confirmed_at: datetime
    note: Optional[str]
    digest: str
    rows: List[ReportSnapshotRow]


class ReportConfirmResponse(ReportSnapshotResponse):
    created: bool


class ReportSnapshotListItem(BaseSchema):
    snapshot_id: str
    report_type: str
    report_name: str
    sample_rule_version: str
    status: str
    confirmed_by: str
    confirmed_at: datetime
    digest: str
    row_count: int


class ReportSnapshotListResponse(BaseSchema):
    total: int
    data: List[ReportSnapshotListItem]


class GraduateQueryParams(BaseSchema):
    graduation_year: Optional[int] = None
    college_id: Optional[int] = None
    micro_major_id: Optional[int] = None
    has_micro_major: Optional[bool] = None
    destination_status: Optional[str] = None
