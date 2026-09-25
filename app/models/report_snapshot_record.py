from sqlalchemy import Column, Integer, String, Text, DateTime
from .base import Base, TimestampMixin


class ReportSnapshotRecord(Base, TimestampMixin):
    """确认后冻结的统计报告，只增不改。

    - payload_json 为规范化（canonical）后的报告正文
    - digest 为正文的 sha256 摘要，读取时重新计算以校验报告未被改写
    - 不提供更新/删除入口：后续回访或重新检测只能产生新快照
    """

    __tablename__ = "report_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    snapshot_id = Column(String(80), unique=True, nullable=False, index=True, comment="快照业务标识")
    report_type = Column(String(50), nullable=False, index=True, comment="报告口径: by-college/by-micro-major/by-year/by-employer-follow-up")
    policy_version = Column(String(40), nullable=False, comment="冻结时的样本规则版本")
    policy_rules_json = Column(Text, nullable=False, comment="规则版本对应的口径说明JSON")
    member_count = Column(Integer, nullable=False, default=0, comment="冻结成员总数（各行之和）")
    payload_json = Column(Text, nullable=False, comment="规范化报告正文JSON")
    digest = Column(String(64), nullable=False, comment="正文sha256摘要")
    created_by = Column(String(50), nullable=False, default="system", comment="确认人")
    frozen_at = Column(DateTime, nullable=False, comment="冻结时间(UTC)")
