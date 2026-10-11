import os
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone

from pymongo import MongoClient

from app import main


@unittest.skipUnless(
    os.getenv("RUN_SCALE_TESTS") == "1",
    "set RUN_SCALE_TESTS=1 to build the disposable 100k-record dataset",
)
class ScaleExplainTests(unittest.TestCase):
    def setUp(self):
        uri = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")
        self.client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        self.client.admin.command("ping")
        hosts = {host for host, _ in self.client.nodes}
        if not hosts or not hosts.issubset({"localhost", "127.0.0.1", "::1"}):
            self.client.close()
            self.skipTest("Scale tests require a loopback MongoDB server")

        self.db_name = f"attendance_scale_test_{uuid.uuid4().hex}"
        self.db = self.client[self.db_name]
        self.original_db = main.db
        main.db = self.db
        self._seed_dataset()
        main.create_indexes()

    def tearDown(self):
        if hasattr(self, "db_name"):
            main.db = self.original_db
            self.client.drop_database(self.db_name)
            self.client.close()

    def _seed_dataset(self):
        employee_count = 1000
        days_per_employee = 100
        employees = [
            {
                "emp_code": f"EMP{number:06d}",
                "name": f"Employee {number}",
                "email": f"employee{number}@example.com",
                "department": f"Department {number % 10:02d}",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
            for number in range(employee_count)
        ]
        self.db.employees.insert_many(employees, ordered=False)

        first_day = date(2026, 6, 1)
        batch = []
        for employee_number in range(employee_count):
            emp_code = f"EMP{employee_number:06d}"
            for offset in range(days_per_employee):
                attendance_day = (first_day + timedelta(days=offset)).isoformat()
                batch.append(
                    {
                        "emp_code": emp_code,
                        "date": attendance_day,
                        "status": "PRESENT",
                        "punch_in": datetime(2026, 6, 1, 4, tzinfo=timezone.utc),
                        "punch_out": datetime(2026, 6, 1, 13, tzinfo=timezone.utc),
                        "work_hours": 9.0,
                        "late_minutes": (employee_number + offset) % 61,
                        "overtime_minutes": 0,
                        "half_day": False,
                    }
                )
                if len(batch) == 2000:
                    self.db.attendance_logs.insert_many(batch, ordered=False)
                    batch.clear()
        if batch:
            self.db.attendance_logs.insert_many(batch, ordered=False)

        self.assertEqual(self.db.attendance_logs.count_documents({}), 100_000)

    @staticmethod
    def _plan_stages(value):
        if isinstance(value, dict):
            stage = value.get("stage")
            if isinstance(stage, str):
                yield stage
            for child in value.values():
                yield from ScaleExplainTests._plan_stages(child)
        elif isinstance(value, list):
            for child in value:
                yield from ScaleExplainTests._plan_stages(child)

    def test_five_explain_cases_have_index_scans_without_collection_scans(self):
        month = "2026-07"
        explanations = {
            "attendance_list": main.explain_endpoint(
                "attendance_list",
                page=1,
                page_size=20,
            ),
            "employee_monthly": main.explain_endpoint(
                "employee_monthly",
                emp_code="EMP000001",
                month=month,
            ),
            "department_summary": main.explain_endpoint(
                "department_summary",
                month=month,
            ),
            "late_leaderboard": main.explain_endpoint(
                "late_leaderboard",
                month=month,
                limit=10,
            ),
            "department_trend": main.explain_endpoint(
                "department_trend",
                department="Department 01",
                from_date=date(2026, 7, 6),
                to_date=date(2026, 7, 12),
            ),
        }

        for name, result in explanations.items():
            with self.subTest(endpoint=name):
                stages = list(self._plan_stages(result["explain"]))
                self.assertTrue(
                    any("IXSCAN" in stage for stage in stages),
                    f"{name} explain had no index scan: {stages}",
                )
                self.assertNotIn(
                    "COLLSCAN",
                    stages,
                    f"{name} explain had a collection scan: {stages}",
                )


if __name__ == "__main__":
    unittest.main()
