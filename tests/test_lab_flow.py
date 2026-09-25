import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LabFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")
        self.clinician = Actor("doc-1", "clinician")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person="P-1"):
        return self.service.create(
            self.admin, "case",
            {"person_id": person, "onset_date": "2026-03-01",
             "location": "District-A", "symptoms": ["fever"]},
        )["id"]

    def _batch(self, no="B-1"):
        return self.service.create(self.lab, "batch", {"batch_no": no})["id"]

    def _sample(self, case_id, batch_id, **extra):
        data = {"case_id": case_id, "batch_id": batch_id}
        data.update(extra)
        return self.service.create(self.lab, "sample", data)

    def test_screen_positive_requires_review_before_confirmation(self):
        case = self._case()
        self.service.transition(self.clinician, case, "triage", {"clinician": "doc-1"})
        batch = self._batch()
        sample = self._sample(case, batch)

        pending = self.service.transition(self.lab, sample["id"], "screen_positive", {"lab_id": "lab-1"})
        self.assertEqual(pending["status"], "review_pending")
        # 初筛阳性不得确认病例
        self.assertEqual(self.service.get(case)["status"], "investigating")

        reviewed = self.service.transition(self.lab, sample["id"], "review_positive", {"lab_id": "lab-1"})
        self.assertEqual(reviewed["status"], "reviewed_positive")
        confirmed = self.service.get(case)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["data"]["confirming_sample_id"], sample["id"])

        progress = self.service.batch_progress(batch)
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["confirmed"], 1)
        self.assertEqual(progress["pending"], 0)

    def test_screen_negative_never_touches_case(self):
        case = self._case()
        self.service.transition(self.clinician, case, "triage", {"clinician": "doc-1"})
        batch = self._batch()
        sample = self._sample(case, batch)

        self.service.transition(self.lab, sample["id"], "screen_negative", {"lab_id": "lab-1"})
        self.assertEqual(self.service.get(case)["status"], "investigating")

    def test_review_negative_keeps_original_diagnosis(self):
        case = self._case()
        self.service.transition(self.clinician, case, "triage", {"clinician": "doc-1"})
        batch = self._batch()
        sample = self._sample(case, batch)
        self.service.transition(self.lab, sample["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, sample["id"], "review_negative", {"lab_id": "lab-1"})
        self.assertEqual(self.service.get(case)["status"], "investigating")

        board = self.service.lab_board()
        case_row = next(item for item in board["cases"] if item["case_id"] == case)
        self.assertEqual(case_row["lab_status"]["code"], "review_negative")

    def test_invalid_sample_needs_recollection_and_board_tracks_it(self):
        case = self._case()
        self.service.transition(self.clinician, case, "triage", {"clinician": "doc-1"})
        batch = self._batch()
        sample = self._sample(case, batch)

        invalid = self.service.transition(
            self.lab, sample["id"], "mark_invalid",
            {"lab_id": "lab-1", "reason": "hemolyzed"},
        )
        self.assertEqual(invalid["status"], "invalid")
        # 失效样本不改变病例诊断
        self.assertEqual(self.service.get(case)["status"], "investigating")

        board = self.service.lab_board()
        case_row = next(item for item in board["cases"] if item["case_id"] == case)
        self.assertEqual(case_row["lab_status"]["code"], "recollection_needed")
        self.assertEqual(board["invalid_samples"], 1)

        # 失效样本不能直接再初筛，必须重采
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.lab, sample["id"], "screen_positive", {"lab_id": "lab-1"})

        retry = self.service.recollect_sample(self.lab, sample["id"])
        self.assertEqual(retry["status"], "received")
        self.assertEqual(retry["data"]["recollected_from"], sample["id"])
        self.service.transition(self.lab, retry["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, retry["id"], "review_positive", {"lab_id": "lab-1"})
        self.assertEqual(self.service.get(case)["status"], "confirmed")

    def test_duplicate_submission_uses_latest_valid_result(self):
        case = self._case()
        self.service.transition(self.clinician, case, "triage", {"clinician": "doc-1"})
        batch = self._batch()

        first = self._sample(case, batch)
        self.service.transition(self.lab, first["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, first["id"], "review_positive", {"lab_id": "lab-1"})
        self.assertEqual(self.service.get(case)["status"], "confirmed")

        # 同病例再次送检，复核阴性 -> 病例保持原诊断（确认）
        second = self._sample(case, batch)
        self.service.transition(self.lab, second["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, second["id"], "review_negative", {"lab_id": "lab-1"})
        self.assertEqual(self.service.get(case)["status"], "confirmed")

        # 已确认病例不允许重复确认，即使引用的是复核阳性样本
        third = self._sample(case, batch)
        self.service.transition(self.lab, third["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, third["id"], "review_positive", {"lab_id": "lab-1"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.lab, case, "lab_positive",
                {"lab_id": "lab-1", "sample_id": third["id"]},
            )

    def test_latest_valid_guard_blocks_stale_sample_on_open_case(self):
        # 病例分流前检验已完成，复核阳性不会自动确认 reported 病例
        case = self._case("P-2")
        batch = self._batch("B-2")
        first = self._sample(case, batch)
        self.service.transition(self.lab, first["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, first["id"], "review_positive", {"lab_id": "lab-1"})
        self.assertEqual(self.service.get(case)["status"], "reported")

        # 分流后再次送检，复核阴性 -> 最新有效结果为阴性
        self.service.transition(self.clinician, case, "triage", {"clinician": "doc-1"})
        second = self._sample(case, batch)
        self.service.transition(self.lab, second["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, second["id"], "review_negative", {"lab_id": "lab-1"})

        # 引用旧的复核阳性样本写回应被"最新有效结果"规则拒绝，病例保持原状态
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab, case, "lab_positive",
                {"lab_id": "lab-1", "sample_id": first["id"]},
            )
        self.assertEqual(self.service.get(case)["status"], "investigating")

        # 失效样本不参与有效结果判定：重采后复核阳性可以确认
        case3 = self._case("P-3")
        self.service.transition(self.clinician, case3, "triage", {"clinician": "doc-1"})
        bad = self._sample(case3, batch)
        self.service.transition(
            self.lab, bad["id"], "mark_invalid",
            {"lab_id": "lab-1", "reason": "hemolyzed"},
        )
        good = self._sample(case3, batch)
        self.service.transition(self.lab, good["id"], "screen_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, good["id"], "review_positive", {"lab_id": "lab-1"})
        self.assertEqual(self.service.get(case3)["status"], "confirmed")

    def test_legacy_case_without_sample_is_pending_submission(self):
        case = self._case("P-legacy")
        board = self.service.lab_board()
        case_row = next(item for item in board["cases"] if item["case_id"] == case)
        self.assertEqual(case_row["lab_status"]["code"], "pending_submission")
        self.assertEqual(board["pending_submission_cases"], 1)

    def test_batch_progress_pending_counts_and_close_guard(self):
        c1, c2 = self._case("P-1"), self._case("P-2")
        self.service.transition(self.clinician, c1, "triage", {"clinician": "doc-1"})
        self.service.transition(self.clinician, c2, "triage", {"clinician": "doc-1"})
        batch = self._batch()
        s1 = self._sample(c1, batch)
        s2 = self._sample(c2, batch)
        self.service.transition(self.lab, s1["id"], "screen_positive", {"lab_id": "lab-1"})

        progress = self.service.batch_progress(batch)
        self.assertEqual(progress["total"], 2)
        self.assertEqual(progress["pending"], 2)  # 一个待复核 + 一个待初筛

        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, batch, "close", {})

        self.service.transition(self.lab, s1["id"], "review_positive", {"lab_id": "lab-1"})
        self.service.transition(self.lab, s2["id"], "screen_negative", {"lab_id": "lab-1"})
        closed = self.service.transition(self.lab, batch, "close", {})
        self.assertEqual(closed["status"], "closed")

        board = self.service.lab_board()
        self.assertEqual(board["pending_samples"], 0)
        self.assertEqual(board["pending_review"], 0)

    def test_sample_requires_existing_case_and_open_batch(self):
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self.service.create(self.lab, "sample", {"case_id": "nope", "batch_id": batch})

        case = self._case("P-x")
        self.service.transition(self.lab, batch, "close", {})
        with self.assertRaises(ValidationError):
            self.service.create(self.lab, "sample", {"case_id": case, "batch_id": batch})

    def test_viewer_cannot_record_results(self):
        case = self._case("P-v")
        batch = self._batch()
        sample = self._sample(case, batch)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"), sample["id"], "screen_positive", {"lab_id": "x"}
            )

    def test_duplicate_batch_no_rejected(self):
        self._batch("B-DUP")
        with self.assertRaises(Exception):
            self.service.create(self.lab, "batch", {"batch_no": "B-DUP"})


if __name__ == "__main__":
    unittest.main()
