import unittest

from fastapi import HTTPException

from app.main import explain_endpoint, list_attendance


class AttendanceStatusValidationTests(unittest.TestCase):
    def test_attendance_list_rejects_empty_status(self):
        with self.assertRaises(HTTPException) as raised:
            list_attendance(status="")

        self.assertEqual(raised.exception.status_code, 422)

    def test_attendance_explain_rejects_empty_status(self):
        with self.assertRaises(HTTPException) as raised:
            explain_endpoint(endpoint="attendance_list", status="")

        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
