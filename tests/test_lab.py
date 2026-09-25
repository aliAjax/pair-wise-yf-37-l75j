import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LabWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.lab = Actor("lab-1", "lab")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id="P-1", triage=True):
        case = self.service.create(
            self.admin,
            "case",
            {"person_id": person_id, "onset_date": "2026-03-01", "location": "A", "symptoms": ["fever"]},
        )
        if triage:
            self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        return case

    def _batch(self, name="B-1"):
        return self.service.create(self.lab, "batch", {"name": name})

    def _sample(self, case_id, batch_id, **extra):
        data = {"case_id": case_id, "batch_id": batch_id}
        data.update(extra)
        return self.service.create(self.lab, "sample", data)

    def test_review_positive_confirms_case(self):
        case = self._case()
        batch = self._batch()
        sample = self._sample(case["id"], batch["id"])
        sample = self.service.transition(self.lab, sample["id"], "screen", {"result": "positive"})
        self.assertEqual(sample["status"], "pending_review")
        # 初筛阳性只是待复核，病例维持原诊断
        self.assertEqual(self.service.get(case["id"])["status"], "investigating")
        sample = self.service.transition(self.lab, sample["id"], "review", {"result": "positive"})
        self.assertEqual(sample["status"], "confirmed_positive")
        updated = self.service.get(case["id"])
        self.assertEqual(updated["status"], "confirmed")
        self.assertEqual(updated["data"]["confirmed_via"], sample["id"])
        self.assertEqual(updated["data"]["confirmed_by"], "lab-1")
        self.assertEqual(updated["data"]["lab_result"], "positive")
        self.assertEqual(updated["data"]["lab_result_sample_id"], sample["id"])
        actions = [row["action"] for row in self.service.audit_log(case["id"])]
        self.assertIn("confirm", actions)

    def test_review_positive_confirms_reported_case(self):
        case = self._case(triage=False)
        batch = self._batch()
        sample = self._sample(case["id"], batch["id"])
        self.service.transition(self.lab, sample["id"], "screen", {"result": "detected"})
        self.service.transition(self.lab, sample["id"], "review", {"result": "positive"})
        self.assertEqual(self.service.get(case["id"])["status"], "confirmed")

    def test_review_negative_keeps_diagnosis(self):
        case = self._case()
        batch = self._batch()
        sample = self._sample(case["id"], batch["id"])
        self.service.transition(self.lab, sample["id"], "screen", {"result": "positive"})
        sample = self.service.transition(self.lab, sample["id"], "review", {"result": "negative"})
        self.assertEqual(sample["status"], "review_negative")
        updated = self.service.get(case["id"])
        self.assertEqual(updated["status"], "investigating")
        self.assertEqual(updated["data"]["lab_result"], "negative")

    def test_screen_negative_keeps_diagnosis(self):
        case = self._case()
        batch = self._batch()
        sample = self._sample(case["id"], batch["id"])
        sample = self.service.transition(self.lab, sample["id"], "screen", {"result": "negative"})
        self.assertEqual(sample["status"], "screened_negative")
        updated = self.service.get(case["id"])
        self.assertEqual(updated["status"], "investigating")
        self.assertEqual(updated["data"]["lab_result"], "negative")

    def test_invalid_sample_keeps_diagnosis_then_resample(self):
        case = self._case()
        batch = self._batch()
        sample = self._sample(case["id"], batch["id"])
        self.service.transition(self.lab, sample["id"], "screen", {"result": "positive"})
        sample = self.service.transition(self.lab, sample["id"], "invalidate", {"reason": "clotted"})
        self.assertEqual(sample["status"], "invalid")
        updated = self.service.get(case["id"])
        self.assertEqual(updated["status"], "investigating")
        self.assertNotIn("lab_result", updated["data"])
        # 重采样本关联原失效样本，复检阳性后确认病例
        resample = self._sample(case["id"], batch["id"], resample_of=sample["id"])
        self.assertEqual(resample["data"]["resample_of"], sample["id"])
        self.service.transition(self.lab, resample["id"], "screen", {"result": "positive"})
        self.service.transition(self.lab, resample["id"], "review", {"result": "positive"})
        self.assertEqual(self.service.get(case["id"])["status"], "confirmed")

    def test_resample_requires_invalid_sample_of_same_case(self):
        case = self._case()
        other = self._case(person_id="P-2")
        batch = self._batch()
        active = self._sample(case["id"], batch["id"])
        with self.assertRaises(ConflictError):
            self._sample(case["id"], batch["id"], resample_of=active["id"])
        self.service.transition(self.lab, active["id"], "invalidate", {"reason": "leaked"})
        with self.assertRaises(ValidationError):
            self._sample(other["id"], batch["id"], resample_of=active["id"])
        with self.assertRaises(ValidationError):
            self._sample(case["id"], batch["id"], resample_of="missing")

    def test_latest_valid_result_is_adopted(self):
        case = self._case()
        batch = self._batch()
        first = self._sample(case["id"], batch["id"])
        self.service.transition(self.lab, first["id"], "screen", {"result": "positive"})
        self.service.transition(self.lab, first["id"], "review", {"result": "positive"})
        self.assertEqual(self.service.get(case["id"])["status"], "confirmed")
        # 重复送检的更新样本复核阴性：病例保持已确诊，但采纳最新有效结果
        second = self._sample(case["id"], batch["id"])
        self.service.transition(self.lab, second["id"], "screen", {"result": "positive"})
        self.service.transition(self.lab, second["id"], "review", {"result": "negative"})
        updated = self.service.get(case["id"])
        self.assertEqual(updated["status"], "confirmed")
        self.assertEqual(updated["data"]["lab_result"], "negative")
        self.assertEqual(updated["data"]["lab_result_sample_id"], second["id"])

    def test_batch_progress_and_close(self):
        case = self._case()
        batch = self._batch()
        s1 = self._sample(case["id"], batch["id"])
        s2 = self._sample(case["id"], batch["id"])
        s3 = self._sample(case["id"], batch["id"])
        self.service.transition(self.lab, s1["id"], "screen", {"result": "negative"})
        self.service.transition(self.lab, s2["id"], "screen", {"result": "positive"})
        progress = self.service.batch_progress(batch["id"])
        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["pending"], 2)
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(progress["by_status"]["pending_review"], 1)
        self.assertEqual(progress["by_status"]["collected"], 1)
        with self.assertRaises(ConflictError):
            self.service.transition(self.lab, batch["id"], "close_batch")
        self.service.transition(self.lab, s2["id"], "review", {"result": "negative"})
        self.service.transition(self.lab, s3["id"], "screen", {"result": "negative"})
        batch = self.service.transition(self.lab, batch["id"], "close_batch")
        self.assertEqual(batch["status"], "closed")
        self.assertEqual(self.service.batch_progress(batch["id"])["pending"], 0)
        with self.assertRaises(ConflictError):
            self._sample(case["id"], batch["id"])

    def test_case_lab_status_derivation(self):
        case = self._case()
        # 旧病例没有样本记录，按待送检处理
        view = self.service.case_lab_status(case["id"])
        self.assertEqual(view["lab_status"], "pending_submission")
        self.assertEqual(view["sample_count"], 0)
        batch = self._batch()
        sample = self._sample(case["id"], batch["id"])
        self.assertEqual(self.service.case_lab_status(case["id"])["lab_status"], "awaiting_screening")
        self.service.transition(self.lab, sample["id"], "screen", {"result": "positive"})
        self.assertEqual(self.service.case_lab_status(case["id"])["lab_status"], "awaiting_review")
        self.service.transition(self.lab, sample["id"], "invalidate", {"reason": "expired"})
        self.assertEqual(self.service.case_lab_status(case["id"])["lab_status"], "needs_resample")
        resample = self._sample(case["id"], batch["id"], resample_of=sample["id"])
        self.service.transition(self.lab, resample["id"], "screen", {"result": "negative"})
        view = self.service.case_lab_status(case["id"])
        self.assertEqual(view["lab_status"], "negative")
        self.assertEqual(view["adopted"]["sample_id"], resample["id"])

    def test_lab_pending_summary(self):
        case = self._case()
        b1 = self._batch("B-1")
        b2 = self._batch("B-2")
        self._sample(case["id"], b1["id"])
        s2 = self._sample(case["id"], b1["id"])
        self.service.transition(self.lab, s2["id"], "screen", {"result": "positive"})
        self._sample(case["id"], b2["id"])
        done = self._sample(case["id"], b2["id"])
        self.service.transition(self.lab, done["id"], "screen", {"result": "negative"})
        summary = self.service.lab_pending()
        self.assertEqual(summary["pending_total"], 3)
        self.assertEqual(summary["awaiting_screening"], 2)
        self.assertEqual(summary["awaiting_review"], 1)
        by_batch = {item["batch_id"]: item for item in summary["batches"]}
        self.assertEqual(by_batch[b1["id"]]["pending"], 2)
        self.assertEqual(by_batch[b2["id"]]["pending"], 1)

    def test_sample_requires_existing_case_and_batch(self):
        case = self._case()
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self._sample("missing-case", batch["id"])
        with self.assertRaises(ValidationError):
            self._sample(case["id"], "missing-batch")

    def test_sample_transition_guards(self):
        case = self._case()
        batch = self._batch()
        sample = self._sample(case["id"], batch["id"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.lab, sample["id"], "review", {"result": "positive"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, sample["id"], "screen", {"result": "unclear"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, sample["id"], "screen", {})
        self.service.transition(self.lab, sample["id"], "screen", {"result": "positive"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.lab, sample["id"], "screen", {"result": "negative"})

    def test_lab_permissions(self):
        case = self._case()
        viewer = Actor("v-1", "viewer")
        clinician = Actor("c-1", "clinician")
        with self.assertRaises(PermissionDenied):
            self.service.create(viewer, "batch", {"name": "B-x"})
        with self.assertRaises(PermissionDenied):
            self.service.create(clinician, "batch", {"name": "B-x"})
        batch = self._batch()
        with self.assertRaises(PermissionDenied):
            self.service.create(viewer, "sample", {"case_id": case["id"], "batch_id": batch["id"]})
        sample = self._sample(case["id"], batch["id"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(clinician, sample["id"], "screen", {"result": "positive"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(viewer, sample["id"], "invalidate", {"reason": "x"})


if __name__ == "__main__":
    unittest.main()
