from sqlalchemy import Column, Integer, String, DateTime, Text, JSON
from .base import Base, TimestampMixin


class ReportSnapshotRecord(Base, TimestampMixin):
    """确认后冻结的统计报告快照。

    快照正文（成员集合、采用记录、指标与口径版本）写入后不再修改，
    后续新增回访或重新检测只会产生新的快照，不会改写旧报告。
    """

    __tablename__ = "report_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    snapshot_id = Column(String(80), unique=True, nullable=False, index=True, comment="快照业务标识（按内容寻址）")
    report_type = Column(String(50), nullable=False, comment="报表类型")
    sample_rule_version = Column(String(50), nullable=False, comment="样本口径版本")
    status = Column(String(20), nullable=False, default="confirmed", comment="状态：confirmed=已冻结")
    confirmed_by = Column(String(50), nullable=False, comment="确认人")
    confirmed_at = Column(DateTime, nullable=False, comment="确认时间")
    note = Column(Text, comment="确认备注")
    digest = Column(String(64), nullable=False, comment="正文SHA-256摘要")
    payload = Column(JSON, nullable=False, comment="冻结的报告正文（成员集合与采用记录）")
