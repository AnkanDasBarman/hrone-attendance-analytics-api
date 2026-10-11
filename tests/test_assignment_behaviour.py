import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

from fastapi import HTTPException
from pydantic import ValidationError
from pymongo import MongoClient

from app import main


TEST_MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")


def utc(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
        tzinfo=timezone.utc,
    )


class AttendanceRuleTests(unittest.TestCase):
    def test_late_grace_boundary_and_whole_minute_floor(self):
        self.assertEqual(
            main.compute_late_minutes(utc(2026, 7, 6, 4, 10), "09:30", "2026-07-06"),
            0,
        )
        self.assertEqual(
            main.compute_late_minutes(
                utc(2026, 7, 6, 4, 10, 1), "09:30", "2026-07-06"
            ),
            10,
        )
        self.assertEqual(
            main.compute_late_minutes(
                utc(2026, 7, 6, 4, 45, 59), "09:30", "2026-07-06"
            ),
            45,
        )

    def test_overnight_shift_assigns_attendance_date_and_overtime(self):
        early_punch = utc(2026, 7, 6, 19, 30)
        self.assertEqual(
            main.attendance_date_for(early_punch, "22:00", "06:00"),
            "2026-07-06",
        )
        punch_out = utc(2026, 7, 7, 1, 0)
        self.assertEqual(
            main.compute_overtime(
                punch_out, "22:00", "06:00", "2026-07-06"
            ),
            30,
        )

    def test_instants_truncate_to_whole_seconds(self):
        value = utc(2026, 7, 6, 4, 10, 0, 900000)
        self.assertEqual(main.as_utc(value), utc(2026, 7, 6, 4, 10))

    def test_work_hours_use_half_up_rounding_and_half_day_uses_rounded_value(self):
        start = utc(2026, 7, 6, 0)
        just_below_half_day = start + timedelta(hours=4, minutes=29, seconds=59)
        shorter_shift = start + timedelta(hours=4, minutes=29)
        self.assertEqual(main.compute_work_hours(start, just_below_half_day), 4.5)
        self.assertFalse(main.compute_half_day(4.5))
        self.assertEqual(main.compute_work_hours(start, shorter_shift), 4.48)
        self.assertTrue(main.compute_half_day(4.48))

    def test_epoch_milliseconds_reject_seconds_and_coercible_types(self):
        with self.assertRaises(HTTPException) as raised:
            main.validate_epoch_ms(1_783_312_500)
        self.assertEqual(raised.exception.status_code, 422)

        with self.assertRaises(ValidationError):
            main.PunchInIn.model_validate(
                {"emp_code": "EMP1001", "punched_at": "1783312500000"}
            )
        with self.assertRaises(ValidationError):
            main.PunchInIn.model_validate(
                {"emp_code": "EMP1001", "punched_at": 1783312500000.0}
            )

    def test_employee_rejects_invalid_calendar_date_and_equal_shift_times(self):
        employee = {
            "emp_code": "EMP1001",
            "name": "Test Employee",
            "email": "test@example.com",
            "department": "Engineering",
            "joined_on": "2026-02-30",
        }
        with self.assertRaises(ValidationError):
            main.EmployeeIn(**employee)

        employee["joined_on"] = "2026-02-28"
        employee["shift_start"] = "09:30"
        employee["shift_end"] = "09:30"
        with self.assertRaises(ValidationError):
            main.EmployeeIn(**employee)


class MongoAttendanceTests(unittest.TestCase):
    def setUp(self):
        self.client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            self.client.admin.command("ping")
        except Exception as exc:
            self.client.close()
            self.skipTest(f"MongoDB test server unavailable: {exc}")

        hosts = {host for host, _ in self.client.nodes}
        if not hosts or not hosts.issubset({"localhost", "127.0.0.1", "::1"}):
            self.client.close()
            self.skipTest(
                "Tests require a loopback MongoDB server; refusing to use a remote database"
            )

        self.db_name = f"attendance_assignment_test_{uuid.uuid4().hex}"
        self.db = self.client[self.db_name]
        self.original_db = main.db
        main.db = self.db
        main.create_indexes()

    def tearDown(self):
        if hasattr(self, "db_name"):
            main.db = self.original_db
            self.client.drop_database(self.db_name)
            self.client.close()

    def add_employee(
        self,
        emp_code,
        department="Engineering",
        joined_on="2026-07-06",
        shift_start="09:30",
        shift_end="18:30",
    ):
        self.db.employees.insert_one(
            {
                "emp_code": emp_code,
                "name": f"Employee {emp_code}",
                "email": f"{emp_code.lower()}@example.com",
                "department": department,
                "shift_start": shift_start,
                "shift_end": shift_end,
                "joined_on": joined_on,
                "created_at": utc(2026, 1, 1),
            }
        )

    def add_attendance(
        self,
        emp_code,
        day,
        status,
        *,
        half_day=False,
        work_hours=None,
        late_minutes=0,
        overtime_minutes=0,
    ):
        self.db.attendance_logs.insert_one(
            {
                "emp_code": emp_code,
                "date": day,
                "status": status,
                "punch_in": utc(2026, 7, 6, 4),
                "punch_out": None,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
                "history": [],
            }
        )

    @staticmethod
    def status_of(callable_, *args, **kwargs):
        try:
            callable_(*args, **kwargs)
            return 200
        except HTTPException as exc:
            return exc.status_code

    def test_startup_index_definitions_are_created_idempotently(self):
        main.create_indexes()
        employee_indexes = {index["name"] for index in self.db.employees.list_indexes()}
        attendance_indexes = {
            index["name"] for index in self.db.attendance_logs.list_indexes()
        }
        self.assertTrue(
            {
                "uq_employee_code",
                "ix_employee_department_joined",
                "ix_employee_joined_on",
            }.issubset(employee_indexes)
        )
        self.assertTrue(
            {
                "uq_attendance_employee_date",
                "ix_attendance_employee_punchin",
                "ix_attendance_date_employee",
            }.issubset(attendance_indexes)
        )

    def test_concurrent_punch_ins_have_one_success_and_conflicts(self):
        self.add_employee("EMP1001")
        body = main.PunchInIn(emp_code="EMP1001", punched_at=1783310400000)
        workers = 8
        barrier = Barrier(workers)

        def request():
            barrier.wait()
            try:
                main.punch_in(body)
                return 201
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=workers) as executor:
            statuses = list(executor.map(lambda _: request(), range(workers)))

        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), workers - 1)
        self.assertEqual(
            self.db.attendance_logs.count_documents({"emp_code": "EMP1001"}),
            1,
        )

    def test_concurrent_punch_outs_close_a_record_once(self):
        self.add_employee("EMP1001")
        main.punch_in(
            main.PunchInIn(emp_code="EMP1001", punched_at=1783310400000)
        )
        body = main.PunchOutIn(emp_code="EMP1001", punched_at=1783342800000)
        workers = 8
        barrier = Barrier(workers)

        def request():
            barrier.wait()
            return self.status_of(main.punch_out, body)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            statuses = list(executor.map(lambda _: request(), range(workers)))

        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), workers - 1)
        record = self.db.attendance_logs.find_one({"emp_code": "EMP1001"})
        if record is None:
            self.fail("punch-out did not leave an attendance record")
        self.assertIsNotNone(record["punch_out"])
        self.assertEqual(record["history"], [])

    def test_concurrent_regularizations_do_not_lose_successful_audit_entries(self):
        self.add_employee("EMP1001")
        self.add_attendance(
            "EMP1001",
            "2026-07-06",
            "PRESENT",
            work_hours=None,
        )
        bodies = [
            main.RegularizeIn(
                status="WFH",
                reason="Remote work correction",
                regularized_by="test-a",
            ),
            main.RegularizeIn(
                punch_in=1783311000000,
                reason="Biometric time correction",
                regularized_by="test-b",
            ),
        ]
        workers = len(bodies)
        barrier = Barrier(workers)

        def request(body):
            barrier.wait()
            try:
                main.regularize_attendance("EMP1001", "2026-07-06", body)
                return 200
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=workers) as executor:
            statuses = list(executor.map(request, bodies))

        self.assertTrue(all(status in {200, 409} for status in statuses))
        successful = statuses.count(200)
        record = self.db.attendance_logs.find_one({"emp_code": "EMP1001"})
        if record is None:
            self.fail("regularization removed the attendance record")
        self.assertEqual(len(record["history"]), successful)
        self.assertTrue(all(entry["changes"] for entry in record["history"]))

    def test_attendance_list_validates_filter_and_range(self):
        for status in ("", "INVALID"):
            with self.assertRaises(HTTPException) as raised:
                main.list_attendance(
                    status=status,
                    page=1,
                    page_size=20,
                )
            self.assertEqual(raised.exception.status_code, 422)

        with self.assertRaises(HTTPException) as raised:
            main.list_attendance(
                date_from="2026-07-10",
                date_to="2026-07-09",
                page=1,
                page_size=20,
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_regularization_rejects_explicit_null_and_preserves_valid_omission(self):
        self.add_employee("EMP1001")
        self.add_attendance("EMP1001", "2026-07-06", "PRESENT")

        with self.assertRaises(HTTPException) as raised:
            main.regularize_attendance(
                "EMP1001",
                "2026-07-06",
                main.RegularizeIn(
                    status=None,
                    reason="Correct status",
                    regularized_by="test",
                ),
            )
        self.assertEqual(raised.exception.status_code, 422)

        with self.assertRaises(HTTPException) as raised:
            main.regularize_attendance(
                "EMP1001",
                "2026-07-06",
                main.RegularizeIn(
                    punch_in=None,
                    reason="Correct time",
                    regularized_by="test",
                ),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def seed_analytics_case(self):
        self.add_employee("EMP1001")
        self.add_employee("EMP1002")
        self.add_employee("EMP1003", joined_on="2026-07-08")
        self.add_employee("EMP1004", joined_on="2026-08-01")

        self.add_attendance(
            "EMP1001",
            "2026-07-06",
            "PRESENT",
            half_day=True,
            work_hours=4.5,
            late_minutes=30,
        )
        self.add_attendance(
            "EMP1001",
            "2026-07-07",
            "WFH",
            work_hours=8.0,
        )
        self.add_attendance("EMP1001", "2026-07-09", "LEAVE")
        self.add_attendance(
            "EMP1001",
            "2026-07-10",
            "PRESENT",
            work_hours=9.0,
            late_minutes=30,
        )
        self.add_attendance(
            "EMP1001",
            "2026-07-11",
            "PRESENT",
            half_day=True,
            work_hours=4.0,
            late_minutes=10,
            overtime_minutes=45,
        )
        self.add_attendance(
            "EMP1003",
            "2026-07-08",
            "ON_DUTY",
            work_hours=8.0,
            late_minutes=70,
        )

    def test_monthly_and_department_analytics_cover_joiners_weekends_and_no_logs(self):
        self.seed_analytics_case()

        monthly = main.employee_monthly("EMP1001", "2026-07")
        self.assertEqual(monthly["working_days"], 20)
        self.assertEqual(monthly["present_days"], 2.5)
        self.assertEqual(monthly["leave_days"], 1)
        self.assertEqual(monthly["late_count"], 3)
        self.assertEqual(monthly["total_late_minutes"], 70)
        self.assertEqual(monthly["total_overtime_minutes"], 45)
        self.assertEqual(monthly["attendance_pct"], 12.5)

        summary = main.department_summary("2026-07")["items"]
        self.assertEqual(len(summary), 1)
        engineering = summary[0]
        self.assertEqual(engineering["department"], "Engineering")
        self.assertEqual(engineering["headcount"], 3)
        self.assertEqual(engineering["present_days"], 3.5)
        self.assertEqual(engineering["avg_work_hours"], 6.7)
        self.assertEqual(engineering["late_count"], 4)
        self.assertEqual(engineering["total_late_minutes"], 140)
        self.assertEqual(engineering["leave_count"], 1)
        self.assertEqual(engineering["on_duty_count"], 1)

    def test_leaderboard_returns_cutoff_ties_in_employee_code_order(self):
        self.seed_analytics_case()
        result = main.late_leaderboard("2026-07", limit=1)
        items = result["items"]
        self.assertEqual([item["emp_code"] for item in items], ["EMP1001", "EMP1003"])
        self.assertEqual([item["rank"] for item in items], [1, 1])
        self.assertEqual(
            [item["total_late_minutes"] for item in items],
            [70, 70],
        )

    def test_department_trend_fills_missing_dates_and_excludes_weekends_from_rate(self):
        self.seed_analytics_case()
        result = main.department_trend(
            "Engineering",
            date(2026, 7, 6),
            date(2026, 7, 12),
        )
        items = result["items"]
        self.assertEqual(len(items), 7)
        self.assertEqual(
            [item["date"] for item in items],
            [f"2026-07-{day:02}" for day in range(6, 13)],
        )
        by_date = {item["date"]: item for item in items}
        self.assertEqual(by_date["2026-07-06"]["headcount"], 2)
        self.assertEqual(by_date["2026-07-08"]["headcount"], 3)
        self.assertEqual(by_date["2026-07-09"]["attendance_rate"], 0.0)
        self.assertEqual(by_date["2026-07-11"]["present_count"], 0.5)
        self.assertIsNone(by_date["2026-07-11"]["attendance_rate"])
        self.assertEqual(by_date["2026-07-10"]["moving_avg_7d"], 0.2833)


if __name__ == "__main__":
    unittest.main()
