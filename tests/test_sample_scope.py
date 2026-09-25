"""通过本地接口验证统计样本口径、报告冻结与追溯可见性。"""

import unittest
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.core import get_db
from app.models import (
    Base,
    College,
    MicroMajor,
    Graduate,
    EmployerFollowUp,
    DestinationStatus,
    DestinationType,
)

API = "/api/v1"


def make_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), testing_session


class SampleScopeInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.client, self.session_factory = make_client()
        self.db = self.session_factory()
        self.college = College(name="计算机学院", code="CS001")
        self.db.add(self.college)
        self.db.flush()
        self.micro_major = MicroMajor(
            name="数据分析", code="MM001", college_id=self.college.id
        )
        self.db.add(self.micro_major)
        self.db.flush()
        self._student_seq = 0

    def tearDown(self):
        self.db.close()
        app.dependency_overrides.clear()

    def add_graduate(self, has_micro_major=False, name="毕业生", year=2026):
        self._student_seq += 1
        graduate = Graduate(
            student_id=f"SID{self._student_seq:04d}",
            name=name,
            major="软件工程",
            graduation_year=year,
            college_id=self.college.id,
            has_micro_major=has_micro_major,
            micro_major_id=self.micro_major.id if has_micro_major else None,
            destination_status=DestinationStatus.VERIFIED,
            destination_type=DestinationType.EMPLOYMENT,
        )
        self.db.add(graduate)
        self.db.flush()
        return graduate

    def add_follow_up(self, graduate_id, follow_up_date, score=None, still_employed=True,
                      employer_name=None, visited_by=None, job_title=None, remark=None):
        follow_up = EmployerFollowUp(
            graduate_id=graduate_id,
            follow_up_date=follow_up_date,
            satisfaction_score=score,
            is_still_employed=still_employed,
            employer_name=employer_name,
            visited_by=visited_by,
            job_title=job_title,
            remark=remark,
        )
        self.db.add(follow_up)
        self.db.flush()
        return follow_up

    def comparison_without_micro(self):
        resp = self.client.get(f"{API}/statistics/comparison")
        self.assertEqual(resp.status_code, 200)
        return resp.json()["without_micro"]

    def test_all_empty_group_returns_no_ratio_but_rule_version(self):
        self.add_graduate()
        self.add_graduate()
        self.db.commit()

        stats = self.comparison_without_micro()
        self.assertIsNone(stats["avg_satisfaction"])
        self.assertEqual(stats["avg_satisfaction_display"], "暂无数据")
        self.assertIsNone(stats["retention_rate"])
        self.assertEqual(stats["retention_rate_display"], "暂无数据")
        self.assertEqual(stats["follow_up_count"], 0)
        self.assertEqual(stats["satisfaction_sample_count"], 0)
        self.assertEqual(stats["satisfaction_excluded_count"], 0)
        self.assertEqual(stats["retention_sample_count"], 0)
        self.assertEqual(stats["sample_rule_version"], "sample-v1")

        resp = self.client.get(f"{API}/statistics/sample-scope")
        self.assertEqual(resp.status_code, 200)
        scope = resp.json()
        self.assertEqual(scope["summary"]["graduate_count"], 2)
        self.assertEqual(scope["summary"]["follow_up_count"], 0)
        self.assertEqual(scope["summary"]["satisfaction_sample_count"], 0)
        self.assertEqual(scope["summary"]["retention_sample_count"], 0)
        self.assertIsNone(scope["summary"]["avg_satisfaction"])
        self.assertIsNone(scope["summary"]["retention_rate"])
        self.assertEqual(scope["records"], [])
        self.assertEqual(len(scope["summary"]["member_ids"]), 2)

    def test_all_scores_missing_keeps_retention_but_not_satisfaction(self):
        g1 = self.add_graduate()
        g2 = self.add_graduate()
        self.add_follow_up(g1.id, date(2026, 1, 10), score=None, still_employed=True)
        self.add_follow_up(g2.id, date(2026, 1, 12), score=None, still_employed=False)
        self.db.commit()

        stats = self.comparison_without_micro()
        self.assertIsNone(stats["avg_satisfaction"])
        self.assertEqual(stats["avg_satisfaction_display"], "暂无数据")
        self.assertEqual(stats["satisfaction_sample_count"], 0)
        self.assertEqual(stats["satisfaction_excluded_count"], 2)
        # 缺失评分不能把留任状态排除
        self.assertEqual(stats["retention_sample_count"], 2)
        self.assertEqual(stats["retention_rate"], 50.0)

    def test_partially_missing_scores_only_valid_ones_count(self):
        g1 = self.add_graduate()
        g2 = self.add_graduate()
        g3 = self.add_graduate()
        self.add_follow_up(g1.id, date(2026, 1, 10), score=4.0, still_employed=True)
        self.add_follow_up(g2.id, date(2026, 1, 11), score=None, still_employed=True)
        # 越界评分通过直接写库模拟历史脏数据
        self.add_follow_up(g3.id, date(2026, 1, 12), score=7.0, still_employed=False)
        self.db.commit()

        stats = self.comparison_without_micro()
        self.assertEqual(stats["avg_satisfaction"], 4.0)
        self.assertEqual(stats["satisfaction_sample_count"], 1)
        self.assertEqual(stats["satisfaction_excluded_count"], 2)
        self.assertEqual(stats["retention_sample_count"], 3)
        self.assertEqual(stats["retention_rate"], round(2 / 3 * 100, 2))

        resp = self.client.get(f"{API}/statistics/sample-scope")
        records = {r["follow_up_id"]: r for r in resp.json()["records"]}
        self.assertEqual(len(records), 3)
        reasons = sorted(
            r["satisfaction_exclusion_reason"]
            for r in records.values()
            if not r["satisfaction_included"]
        )
        self.assertEqual(reasons, ["评分缺失", "评分超出1-5有效范围"])
        self.assertTrue(all(r["retention_selected"] for r in records.values()))

    def test_latest_valid_follow_up_wins_per_graduate(self):
        g1 = self.add_graduate()
        self.add_follow_up(g1.id, date(2026, 1, 5), score=5.0, still_employed=True)
        latest = self.add_follow_up(g1.id, date(2026, 3, 1), score=None, still_employed=False)

        g2 = self.add_graduate()
        # 同日两次回访，以记录ID较大者（后录入）为准
        self.add_follow_up(g2.id, date(2026, 2, 1), score=3.0, still_employed=True)
        self.add_follow_up(g2.id, date(2026, 2, 1), score=4.0, still_employed=False)
        self.db.commit()

        stats = self.comparison_without_micro()
        # 两人最近一次有效回访均为离职
        self.assertEqual(stats["retention_sample_count"], 2)
        self.assertEqual(stats["retention_rate"], 0.0)
        # 满意度按记录计：5.0、3.0、4.0 三条有效
        self.assertEqual(stats["avg_satisfaction"], 4.0)
        self.assertEqual(stats["satisfaction_sample_count"], 3)
        self.assertEqual(stats["satisfaction_excluded_count"], 1)

        resp = self.client.get(f"{API}/statistics/sample-scope")
        records = resp.json()["records"]
        latest_record = next(r for r in records if r["follow_up_id"] == latest.id)
        self.assertTrue(latest_record["retention_selected"])
        self.assertFalse(latest_record["satisfaction_included"])
        self.assertEqual(latest_record["satisfaction_exclusion_reason"], "评分缺失")

        superseded = [r for r in records if not r["retention_selected"]]
        self.assertEqual(len(superseded), 2)
        self.assertTrue(
            all(r["retention_exclusion_reason"] == "存在更新的有效回访" for r in superseded)
        )

    def test_satisfaction_score_boundaries(self):
        scores = [1.0, 5.0, 0.9, 5.1, 0.0, 6.0]
        for index, score in enumerate(scores):
            graduate = self.add_graduate()
            self.add_follow_up(
                graduate.id, date(2026, 1, 10 + index), score=score, still_employed=True
            )
        self.db.commit()

        stats = self.comparison_without_micro()
        # 仅 1.0 与 5.0 为有效边界值
        self.assertEqual(stats["avg_satisfaction"], 3.0)
        self.assertEqual(stats["satisfaction_sample_count"], 2)
        self.assertEqual(stats["satisfaction_excluded_count"], 4)
        # 评分越界不影响留任口径
        self.assertEqual(stats["retention_sample_count"], 6)
        self.assertEqual(stats["retention_rate"], 100.0)

    def test_report_items_keep_existing_fields_and_add_sample_scope(self):
        graduate = self.add_graduate()
        self.add_follow_up(graduate.id, date(2026, 1, 10), score=4.5, still_employed=True)
        self.db.commit()

        resp = self.client.get(f"{API}/statistics/reports/by-college")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["data"]
        self.assertEqual(len(items), 1)
        item = items[0]
        for field in (
            "dimension", "dimension_value", "total_count", "confirmed_rate",
            "aligned_rate", "avg_salary_display", "avg_satisfaction_display",
            "retention_rate_display", "follow_up_count",
        ):
            self.assertIn(field, item)
        self.assertEqual(item["satisfaction_sample_count"], 1)
        self.assertEqual(item["retention_sample_count"], 1)
        self.assertEqual(item["sample_rule_version"], "sample-v1")


class ReportSnapshotInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.client, self.session_factory = make_client()
        self.db = self.session_factory()
        self.college = College(name="计算机学院", code="CS001")
        self.db.add(self.college)
        self.db.flush()
        self.micro_major = MicroMajor(
            name="数据分析", code="MM001", college_id=self.college.id
        )
        self.db.add(self.micro_major)
        self.db.flush()
        self.graduate = Graduate(
            student_id="SID0001",
            name="张隐私",
            major="软件工程",
            graduation_year=2026,
            college_id=self.college.id,
            has_micro_major=True,
            micro_major_id=self.micro_major.id,
            destination_status=DestinationStatus.VERIFIED,
            destination_type=DestinationType.EMPLOYMENT,
        )
        self.db.add(self.graduate)
        self.db.flush()
        self.follow_up = EmployerFollowUp(
            graduate_id=self.graduate.id,
            follow_up_date=date(2026, 1, 10),
            satisfaction_score=4.0,
            is_still_employed=True,
            employer_name="秘密公司",
            visited_by="李老师",
            job_title="机密岗位",
            remark="敏感备注",
        )
        self.db.add(self.follow_up)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        app.dependency_overrides.clear()

    def confirm_by_college(self):
        return self.client.post(
            f"{API}/statistics/reports/by-college/confirm",
            json={"confirmed_by": "教务处"},
        )

    def test_confirm_freezes_members_and_rule_version(self):
        resp = self.confirm_by_college()
        self.assertEqual(resp.status_code, 200)
        snapshot = resp.json()
        self.assertTrue(snapshot["created"])
        self.assertEqual(snapshot["report_type"], "by-college")
        self.assertEqual(snapshot["report_name"], "按学院统计")
        self.assertEqual(snapshot["sample_rule_version"], "sample-v1")
        self.assertEqual(snapshot["status"], "confirmed")
        self.assertEqual(snapshot["confirmed_by"], "教务处")
        self.assertTrue(snapshot["digest"])

        self.assertEqual(len(snapshot["rows"]), 1)
        row = snapshot["rows"][0]
        self.assertEqual(row["member_ids"], [self.graduate.id])
        self.assertEqual(row["metrics"]["satisfaction_sample_count"], 1)
        self.assertEqual(row["metrics"]["retention_sample_count"], 1)
        self.assertEqual(len(row["records"]), 1)
        record = row["records"][0]
        self.assertEqual(record["follow_up_id"], self.follow_up.id)
        self.assertTrue(record["satisfaction_included"])
        self.assertTrue(record["retention_selected"])

        # 数据未变时重复确认幂等，不产生新快照
        again = self.confirm_by_college()
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["created"])
        self.assertEqual(again.json()["snapshot_id"], snapshot["snapshot_id"])

        listed = self.client.get(f"{API}/statistics/report-snapshots")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(
            listed.json()["data"][0]["snapshot_id"], snapshot["snapshot_id"]
        )

    def test_new_follow_up_does_not_rewrite_confirmed_report(self):
        snapshot = self.confirm_by_college().json()
        snapshot_id = snapshot["snapshot_id"]
        frozen = self.client.get(f"{API}/statistics/report-snapshots/{snapshot_id}")
        self.assertEqual(frozen.status_code, 200)
        frozen_before = frozen.json()
        row_before = frozen_before["rows"][0]
        self.assertEqual(row_before["metrics"]["retention_rate_display"], "100.0%")

        # 确认后新增回访：该毕业生离职且评分很低
        resp = self.client.post(
            f"{API}/follow-ups",
            json={
                "graduate_id": self.graduate.id,
                "follow_up_date": "2026-06-01",
                "satisfaction_score": 1,
                "is_still_employed": False,
            },
        )
        self.assertEqual(resp.status_code, 200)

        # 实时报表反映新数据
        live = self.client.get(f"{API}/statistics/reports/by-college").json()
        live_row = live["data"][0]
        self.assertEqual(live_row["retention_rate_display"], "0.0%")
        self.assertEqual(live_row["follow_up_count"], 2)

        # 旧快照保持冻结
        frozen_after = self.client.get(
            f"{API}/statistics/report-snapshots/{snapshot_id}"
        ).json()
        self.assertEqual(frozen_after, frozen_before)
        self.assertEqual(
            frozen_after["rows"][0]["metrics"]["retention_rate_display"], "100.0%"
        )
        self.assertEqual(frozen_after["rows"][0]["member_ids"], [self.graduate.id])
        self.assertEqual(len(frozen_after["rows"][0]["records"]), 1)

        # 数据变化后再次确认生成新快照，旧快照仍可追溯
        newer = self.confirm_by_college().json()
        self.assertTrue(newer["created"])
        self.assertNotEqual(newer["snapshot_id"], snapshot_id)
        self.assertEqual(
            newer["rows"][0]["metrics"]["retention_rate_display"], "0.0%"
        )
        still_frozen = self.client.get(
            f"{API}/statistics/report-snapshots/{snapshot_id}"
        ).json()
        self.assertEqual(still_frozen, frozen_before)

        listed = self.client.get(f"{API}/statistics/report-snapshots").json()
        self.assertEqual(listed["total"], 2)

    def test_redetection_does_not_rewrite_confirmed_report(self):
        snapshot = self.client.post(
            f"{API}/statistics/reports/by-micro-major/confirm",
            json={"confirmed_by": "教务处"},
        ).json()
        snapshot_id = snapshot["snapshot_id"]
        before = self.client.get(
            f"{API}/statistics/report-snapshots/{snapshot_id}"
        ).json()

        trend = self.client.get(
            f"{API}/statistics/trend/{self.micro_major.id}",
            params={"run_detection": "true"},
        )
        self.assertEqual(trend.status_code, 200)

        after = self.client.get(
            f"{API}/statistics/report-snapshots/{snapshot_id}"
        ).json()
        self.assertEqual(after, before)

    def test_trace_responses_hide_personal_information(self):
        scope_resp = self.client.get(f"{API}/statistics/sample-scope")
        self.assertEqual(scope_resp.status_code, 200)
        for secret in ("张隐私", "SID0001", "秘密公司", "李老师", "机密岗位", "敏感备注"):
            self.assertNotIn(secret, scope_resp.text)
        allowed_keys = {
            "follow_up_id", "graduate_id", "follow_up_date",
            "satisfaction_score", "satisfaction_included",
            "satisfaction_exclusion_reason", "is_still_employed",
            "retention_selected", "retention_exclusion_reason",
        }
        for record in scope_resp.json()["records"]:
            self.assertEqual(set(record.keys()), allowed_keys)

        snapshot = self.confirm_by_college().json()
        snapshot_resp = self.client.get(
            f"{API}/statistics/report-snapshots/{snapshot['snapshot_id']}"
        )
        self.assertEqual(snapshot_resp.status_code, 200)
        for secret in ("张隐私", "SID0001", "秘密公司", "李老师", "机密岗位", "敏感备注"):
            self.assertNotIn(secret, snapshot_resp.text)
        for row in snapshot_resp.json()["rows"]:
            for record in row["records"]:
                self.assertEqual(set(record.keys()), allowed_keys)

    def test_confirm_validation_and_unknown_routes(self):
        resp = self.client.post(
            f"{API}/statistics/reports/not-a-report/confirm",
            json={"confirmed_by": "教务处"},
        )
        self.assertEqual(resp.status_code, 404)

        resp = self.client.post(
            f"{API}/statistics/reports/by-college/confirm",
            json={"confirmed_by": "   "},
        )
        self.assertEqual(resp.status_code, 400)

        resp = self.client.get(f"{API}/statistics/report-snapshots/by-college:missing")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
