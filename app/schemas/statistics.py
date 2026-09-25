from typing import Optional, List
from .common import BaseSchema


# 样本规则版本：满意度/留任率口径发生变化时必须递增，
# 冻结报告按此版本标记生成时的口径
STATISTICS_POLICY_VERSION = "2026-09-sample-scope-v1"


class SampleTraceItem(BaseSchema):
    """单个毕业生在报告口径中的采用情况。

    只暴露 graduate_id 与回访记录 id，不包含姓名、学号等个人信息，
    调用方可以据此核对分母，但看不到无权访问的个人字段。
    """

    graduate_id: int
    follow_up_count: int = 0
    # 满意度：该毕业生名下有效/缺失（含越界）评分的回访记录数
    satisfaction_valid_count: int = 0
    satisfaction_missing_count: int = 0
    # 留任率：被选中的最近一次有效回访
    retention_selected_follow_up_id: Optional[int] = None
    retention_selected_follow_up_date: Optional[str] = None
    is_still_employed: Optional[bool] = None
    # 未纳入原因：无回访记录 / 回访缺少留任状态 / 满意度评分缺失 / 满意度评分越界
    exclusion_reasons: List[str] = []


class SampleScope(BaseSchema):
    """某一分组的样本口径说明，可由报告追溯到具体采用记录。"""

    policy_version: str
    cohort_size: int
    follow_up_count: int
    # 满意度：只纳入有效评分（1-5 且非空），按回访记录计数
    satisfaction_sample_count: int
    satisfaction_missing_count: int
    # 留任率：每位毕业生取最近一次有效回访（is_still_employed 非空），按人计数
    retention_sample_count: int
    retention_missing_count: int = 0
    retention_unreachable_count: int = 0
    members: List[SampleTraceItem] = []


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
    # 可追溯样本口径（以上旧字段保持不变）
    satisfaction_sample_count: int = 0
    satisfaction_missing_count: int = 0
    retention_sample_count: int = 0
    retention_missing_count: int = 0
    retention_unreachable_count: int = 0
    policy_version: str = STATISTICS_POLICY_VERSION
    sample_scope: Optional[SampleScope] = None


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
    # 可追溯样本口径（以上旧字段保持不变）
    subject_type: Optional[str] = None
    subject_id: Optional[int] = None
    satisfaction_sample_count: int = 0
    satisfaction_missing_count: int = 0
    retention_sample_count: int = 0
    retention_missing_count: int = 0
    retention_unreachable_count: int = 0
    avg_satisfaction: Optional[float] = None
    retention_rate: Optional[float] = None
    policy_version: str = STATISTICS_POLICY_VERSION
    sample_scope: Optional[SampleScope] = None


class ReportResponse(BaseSchema):
    report_type: str
    data: List[ReportItem]
    generated_at: str
    # 冻结后存在；实时预览时为 None
    policy_version: Optional[str] = None
    snapshot_id: Optional[str] = None
    frozen_at: Optional[str] = None
    digest: Optional[str] = None
    frozen: bool = False


class FrozenReportSummary(BaseSchema):
    snapshot_id: str
    report_type: str
    policy_version: str
    member_count: int
    created_by: str
    frozen_at: str
    digest: str


class FrozenReportListResponse(BaseSchema):
    total: int
    data: List[FrozenReportSummary]


class FreezeReportRequest(BaseSchema):
    created_by: str = "system"
    snapshot_id: Optional[str] = None


class GraduateQueryParams(BaseSchema):
    graduation_year: Optional[int] = None
    college_id: Optional[int] = None
    micro_major_id: Optional[int] = None
    has_micro_major: Optional[bool] = None
    destination_status: Optional[str] = None
