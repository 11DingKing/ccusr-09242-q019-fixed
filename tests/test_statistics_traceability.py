"""样本口径可追溯性与报告冻结的端到端测试。

覆盖：
- 全空（无回访）、部分缺失（评分/留任状态分别缺失且互不影响）
- 同一毕业生多次回访时取最近一次有效回访
- 满意度评分边界（1/5 纳入，0/越界不纳入）
- 报告可追溯到采用的毕业生与回访记录，且不暴露姓名/学号
- 报告确认后冻结成员集合与规则版本，后续回访不改写旧报告
"""

import json
import unittest
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.models import (
    Base,
    College,
    Graduate,
    EmployerFollowUp,
    DestinationStatus,
    DestinationType,
)
from app.schemas import STATISTICS_POLICY_VERSION
from main import app


class StatisticsTraceabilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()

    def _add_graduate(self, gid, name, college_id=1, has_micro=False, **kwargs):
        graduate = Graduate(
            id=gid,
            student_id=f"S{gid:03d}",
            name=name,
            major="测试专业",
            graduation_year=2026,
            college_id=college_id,
            has_micro_major=has_micro,
            micro_major_id=kwargs.pop("micro_major_id", 1 if has_micro else None),
            destination_status=kwargs.pop(
                "destination_status", DestinationStatus.VERIFIED
            ),
            destination_type=kwargs.pop(
                "destination_type", DestinationType.EMPLOYMENT
            ),
            **kwargs,
        )
        self.db.add(graduate)
        return graduate

    def _add_follow_up(self, fid, gid, follow_up_date, **kwargs):
        follow_up = EmployerFollowUp(
            id=fid,
            graduate_id=gid,
            follow_up_date=follow_up_date,
            is_aligned=kwargs.pop("is_aligned", True),
            satisfaction_score=kwargs.pop("satisfaction_score", None),
            is_still_employed=kwargs.pop("is_still_employed", True),
            **kwargs,
        )
        self.db.add(follow_up)
        return follow_up

    def _seed(self):
        self.db = self.SessionLocal()
        self.db.add(College(id=1, name="测试学院", code="T"))
        self.db.add(College(id=2, name="第二学院", code="T2"))

        # g1: 全空（无回访）——分母中应计入人数，但没有满意度/留任样本
        self._add_graduate(1, "张三")

        # g2: 部分缺失——有回访但评分缺失；留任状态有效（False），
        #     说明“缺失评分不能把留任状态排除”
        self._add_graduate(2, "李四")
        self._add_follow_up(
            21, 2, date(2026, 3, 1),
            satisfaction_score=None, is_still_employed=False,
        )

        # g3: 多次回访——最近一次(2026-06-01)留任=False 且评分缺失，
        #     更早的回访评分=3；留任取最近一次有效回访，满意度按记录计数
        self._add_graduate(3, "王五")
        self._add_follow_up(
            31, 3, date(2026, 1, 1),
            satisfaction_score=3, is_still_employed=True,
        )
        self._add_follow_up(
            32, 3, date(2026, 6, 1),
            satisfaction_score=None, is_still_employed=False,
        )

        # g7: 有回访且评分有效，但留任状态缺失——计入满意度样本，
        #     不进入留任率分母（与 g2 互为对照：评分缺失不排除留任，反之亦然）
        self._add_graduate(7, "周九")
        self._add_follow_up(
            71, 7, date(2026, 5, 1),
            satisfaction_score=4, is_still_employed=None,
        )

        # g4: 多次回访，最近一次缺留任状态——应回退取上一次有效回访
        self._add_graduate(4, "赵六", college_id=2)
        self._add_follow_up(
            41, 4, date(2026, 2, 1),
            satisfaction_score=4, is_still_employed=False,
        )
        self._add_follow_up(
            42, 4, date(2026, 7, 1),
            satisfaction_score=2, is_still_employed=None,
        )

        # g5: 评分边界——1 与 5 都纳入
        self._add_graduate(5, "钱七", college_id=2)
        self._add_follow_up(
            51, 5, date(2026, 4, 1),
            satisfaction_score=1, is_still_employed=True,
        )
        self._add_follow_up(
            52, 5, date(2026, 5, 1),
            satisfaction_score=5, is_still_employed=True,
        )

        # g6: 越界评分——0 不纳入满意度，但留任状态仍采用
        self._add_graduate(6, "孙八", college_id=2)
        self._add_follow_up(
            61, 6, date(2026, 4, 1),
            satisfaction_score=0, is_still_employed=False,
        )

        self.db.commit()

    def _college_row(self, payload, college_name="测试学院"):
        rows = [r for r in payload["data"] if r["dimension_value"] == college_name]
        self.assertEqual(len(rows), 1)
        return rows[0]

    # -- 口径计算 --------------------------------------------------------

    def test_sample_counts_and_missing_reasons(self):
        row = self._college_row(
            self.client.get("/api/v1/statistics/reports/by-college").json()
        )
        # 测试学院: g1, g2, g3, g7
        self.assertEqual(row["total_count"], 4)

        # 满意度有效记录：g3 的 1 月评分 3 + g7 的评分 4
        self.assertEqual(row["satisfaction_sample_count"], 2)
        # 缺失记录：g2 的 1 条 + g3 的 6 月 1 条 = 2
        self.assertEqual(row["satisfaction_missing_count"], 2)
        self.assertEqual(row["avg_satisfaction"], 3.5)

        # 留任：g2/g3 有有效留任状态 -> 样本 2；g7 状态缺失不进入分母；g1 无回访
        self.assertEqual(row["retention_sample_count"], 2)
        self.assertEqual(row["retention_unreachable_count"], 1)  # g1
        self.assertEqual(row["retention_missing_count"], 1)  # g7
        # g2=False, g3 最近一次=False -> 0/2 = 0.0
        self.assertEqual(row["retention_rate"], 0.0)

    def test_missing_satisfaction_does_not_exclude_retention(self):
        # 第二学院: g4, g5, g6
        row = self._college_row(
            self.client.get("/api/v1/statistics/reports/by-college").json(),
            college_name="第二学院",
        )
        # g6 评分越界缺失，但留任状态 False 仍计入分母
        self.assertEqual(row["retention_sample_count"], 3)
        self.assertEqual(row["retention_rate"], round(1 / 3 * 100, 2))
        # 满意度有效记录：g4 的两条(4,2) + g5 的两条(1,5) = 4，g6 的 0 越界
        self.assertEqual(row["satisfaction_sample_count"], 4)
        self.assertEqual(row["satisfaction_missing_count"], 1)
        self.assertEqual(row["avg_satisfaction"], (4 + 2 + 1 + 5) / 4)

    def test_latest_valid_follow_up_selection_and_bounds(self):
        payload = self.client.get(
            "/api/v1/statistics/reports/by-college"
        ).json()
        row = self._college_row(payload, college_name="第二学院")
        members = {m["graduate_id"]: m for m in row["sample_scope"]["members"]}

        # g4: 最近一次缺留任状态，回退选中 follow_up 41（is_still_employed=False）
        self.assertEqual(members[4]["retention_selected_follow_up_id"], 41)
        self.assertIs(members[4]["is_still_employed"], False)

        # g5: 边界评分 1/5 均纳入，最近一条是 52
        self.assertEqual(members[5]["retention_selected_follow_up_id"], 52)
        self.assertEqual(members[5]["satisfaction_valid_count"], 2)
        self.assertEqual(members[5]["satisfaction_missing_count"], 0)
        self.assertNotIn("满意度评分缺失", members[5]["exclusion_reasons"])

        # g6: 评分 0 越界 -> 标记越界；留任仍采用 61
        self.assertEqual(members[6]["retention_selected_follow_up_id"], 61)
        self.assertEqual(members[6]["satisfaction_valid_count"], 0)
        self.assertEqual(members[6]["satisfaction_missing_count"], 1)
        self.assertIn("满意度评分越界", members[6]["exclusion_reasons"])

        # g3: 多次回访取最近一次有效（32），即使它评分缺失
        row1 = self._college_row(payload)
        members1 = {m["graduate_id"]: m for m in row1["sample_scope"]["members"]}
        self.assertEqual(members1[3]["retention_selected_follow_up_id"], 32)
        self.assertEqual(members1[3]["retention_selected_follow_up_date"], "2026-06-01")
        self.assertIs(members1[3]["is_still_employed"], False)
        self.assertEqual(members1[3]["satisfaction_valid_count"], 1)

        # g1: 全空
        self.assertIsNone(members1[1]["retention_selected_follow_up_id"])
        self.assertIn("无回访记录", members1[1]["exclusion_reasons"])

        # g7: 有评分有效回访但留任状态缺失 -> 满意度计入、留任不选记录
        self.assertIsNone(members1[7]["retention_selected_follow_up_id"])
        self.assertIn("回访缺少留任状态", members1[7]["exclusion_reasons"])
        self.assertEqual(members1[7]["satisfaction_valid_count"], 1)

    def test_trace_contains_no_personal_info(self):
        payload = self.client.get(
            "/api/v1/statistics/reports/by-college"
        ).json()
        raw = json.dumps(payload, ensure_ascii=False)
        # 追溯明细可以看到采用的记录 id，但看不到姓名/学号等个人信息
        self.assertNotIn("张三", raw)
        self.assertNotIn("李四", raw)
        self.assertNotIn("S001", raw)
        self.assertNotIn("S002", raw)
        row = self._college_row(payload)
        ids = {m["graduate_id"] for m in row["sample_scope"]["members"]}
        self.assertEqual(ids, {1, 2, 3, 7})

    def test_policy_version_present_and_backward_compatible_fields(self):
        payload = self.client.get(
            "/api/v1/statistics/reports/by-college"
        ).json()
        self.assertEqual(payload["policy_version"], STATISTICS_POLICY_VERSION)
        self.assertFalse(payload["frozen"])
        row = self._college_row(payload)
        # 旧字段仍在
        for field in (
            "dimension", "dimension_value", "total_count", "confirmed_rate",
            "aligned_rate", "avg_salary_display", "avg_satisfaction_display",
            "retention_rate_display", "follow_up_count",
        ):
            self.assertIn(field, row)

    # -- 全空场景 --------------------------------------------------------

    def test_all_empty_cohort(self):
        self.db.add(College(id=9, name="空学院", code="E"))
        self._add_graduate(9, "空九", college_id=9)
        self.db.commit()

        payload = self.client.get(
            "/api/v1/statistics/reports/by-college"
        ).json()
        row = self._college_row(payload, college_name="空学院")
        self.assertEqual(row["total_count"], 1)
        self.assertEqual(row["satisfaction_sample_count"], 0)
        self.assertEqual(row["retention_sample_count"], 0)
        self.assertEqual(row["retention_unreachable_count"], 1)
        self.assertIsNone(row["retention_rate"])
        self.assertEqual(row["retention_rate_display"], "暂无数据")
        self.assertEqual(len(row["sample_scope"]["members"]), 1)

    # -- 冻结 ------------------------------------------------------------

    def test_freeze_then_follow_up_changes_do_not_rewrite_report(self):
        freeze_resp = self.client.post(
            "/api/v1/statistics/reports/by-college/freeze",
            json={"created_by": "dean-wang", "snapshot_id": "snap-college-1"},
        )
        self.assertEqual(freeze_resp.status_code, 200)
        frozen = freeze_resp.json()
        self.assertTrue(frozen["frozen"])
        self.assertEqual(frozen["snapshot_id"], "snap-college-1")
        self.assertEqual(frozen["policy_version"], STATISTICS_POLICY_VERSION)
        self.assertIsNotNone(frozen["digest"])

        frozen_before = self.client.get(
            "/api/v1/statistics/reports/frozen/snap-college-1"
        ).json()
        row_before = self._college_row(frozen_before)
        self.assertEqual(row_before["retention_sample_count"], 2)
        self.assertEqual(row_before["retention_rate"], 0.0)

        # 冻结后新增/修改回访：g1 补一条在职回访，并改变 g3 的状态
        self._add_follow_up(
            11, 1, date(2026, 9, 1),
            satisfaction_score=5, is_still_employed=True,
        )
        self._add_follow_up(
            33, 3, date(2026, 9, 1),
            satisfaction_score=2, is_still_employed=True,
        )
        self.db.commit()

        # 旧报告不变
        frozen_after = self.client.get(
            "/api/v1/statistics/reports/frozen/snap-college-1"
        ).json()
        self.assertEqual(frozen_after["digest"], frozen_before["digest"])
        row_after = self._college_row(frozen_after)
        self.assertEqual(row_after["retention_sample_count"], 2)
        self.assertEqual(row_after["retention_rate"], 0.0)
        members = {m["graduate_id"]: m for m in row_after["sample_scope"]["members"]}
        self.assertNotIn(11, [m["retention_selected_follow_up_id"] for m in members.values()])

        # 实时预览反映新数据：g1 新增在职回访进入样本，g3 最新状态变为在职
        live = self.client.get("/api/v1/statistics/reports/by-college").json()
        live_row = self._college_row(live)
        self.assertEqual(live_row["retention_sample_count"], 3)
        self.assertEqual(live_row["retention_rate"], round(2 / 3 * 100, 2))

    def test_freeze_listing_and_duplicate_rejected(self):
        r1 = self.client.post(
            "/api/v1/statistics/reports/by-year/freeze",
            json={"snapshot_id": "snap-year-1"},
        )
        self.assertEqual(r1.status_code, 200)

        dup = self.client.post(
            "/api/v1/statistics/reports/by-year/freeze",
            json={"snapshot_id": "snap-year-1"},
        )
        self.assertEqual(dup.status_code, 400)

        listing = self.client.get(
            "/api/v1/statistics/reports/frozen?report_type=by-year"
        ).json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["data"][0]["snapshot_id"], "snap-year-1")
        self.assertEqual(listing["data"][0]["member_count"], 7)

        missing = self.client.get(
            "/api/v1/statistics/reports/frozen/not-exist"
        )
        self.assertEqual(missing.status_code, 404)

    def test_frozen_payload_digest_verifies_integrity(self):
        from app.models import ReportSnapshotRecord

        self.client.post(
            "/api/v1/statistics/reports/by-college/freeze",
            json={"snapshot_id": "snap-tamper-1"},
        )
        # 直接在存储层篡改正文
        db = self.SessionLocal()
        record = db.query(ReportSnapshotRecord).filter(
            ReportSnapshotRecord.snapshot_id == "snap-tamper-1"
        ).first()
        payload = json.loads(record.payload_json)
        payload["data"][0]["retention_rate"] = 99.99
        record.payload_json = json.dumps(payload, ensure_ascii=False)
        db.commit()
        db.close()

        resp = self.client.get(
            "/api/v1/statistics/reports/frozen/snap-tamper-1"
        )
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
