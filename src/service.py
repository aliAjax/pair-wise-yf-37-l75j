from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    ValidationError,
)
from .rules import (
    CONFIRMABLE_CASE_STATUSES,
    RuleEngine,
    case_lab_status,
    latest_valid_sample,
    summarize_batch,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        payload["_seq"] = self.repository.next_seq()
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._after_transition(actor, updated, action, patch)
        return updated

    def _after_transition(self, actor, sample, action, patch):
        # 复核阳性才确认病例；仅在调查中/疑似病例上触发，已确认病例保持原诊断。
        if sample["kind"] != "sample" or action != "review_positive":
            return
        case_id = sample["data"].get("case_id")
        case = self.repository.get_entity(case_id) if case_id else None
        if not case or case["status"] not in CONFIRMABLE_CASE_STATUSES:
            return
        next_status, case_patch = self.rules.validate_transition(
            actor, case, "lab_positive",
            {"lab_id": sample["data"].get("lab_id", actor.user_id),
             "sample_id": sample["id"]},
            self._lookup,
        )
        case_updated = self.repository.update_entity(
            case_id, case["version"], next_status,
            {**case["data"], **case_patch},
        )
        self.audit.record(
            case_id, actor, "lab_positive", case["status"], case_updated["status"],
            {"patch": case_patch, "source": "sample_review", "sample_id": sample["id"]},
        )

    def recollect_sample(self, actor, sample_id, data=None):
        """样本失效后重采：原样本保留为失效记录，生成新样本回到初筛队列。"""
        original = self.repository.get_entity(sample_id)
        if not original:
            raise NotFoundError("entity not found: " + sample_id)
        if original["kind"] != "sample":
            raise ValidationError("target is not a sample")
        if original["status"] != "invalid":
            raise InvalidTransition("only invalid samples can be recollected")
        batch = self.repository.get_entity(original["data"]["batch_id"])
        if batch["status"] != "open":
            raise ValidationError("batch %s is closed; start the recollection in a new batch" % batch["id"])
        payload = {
            "case_id": original["data"]["case_id"],
            "batch_id": original["data"]["batch_id"],
            "recollected_from": sample_id,
        }
        payload.update(data or {})
        new_sample = self.create(actor, "sample", payload)
        self.audit.record(
            sample_id, actor, "recollect", "invalid", "invalid",
            {"new_sample_id": new_sample["id"]},
        )
        return new_sample

    def batch_progress(self, batch_id):
        batch = self.repository.get_entity(batch_id)
        if not batch or batch["kind"] != "batch":
            raise NotFoundError("batch not found: " + batch_id)
        return summarize_batch(
            batch,
            [s for s in self.repository.list_entities(kind="sample")
             if s["data"].get("batch_id") == batch_id],
        )

    def lab_board(self):
        """检验室看板：批次进度、待处理样本数，以及病例检验状态。"""
        cases = self.repository.list_entities(kind="case")
        samples = self.repository.list_entities(kind="sample")
        batches = self.repository.list_entities(kind="batch")
        by_case = {}
        for sample in samples:
            by_case.setdefault(sample["data"].get("case_id"), []).append(sample)
        case_items = []
        pending_submission = 0
        for case in cases:
            lab_status = case_lab_status(case, by_case.get(case["id"], []))
            if lab_status["code"] == "pending_submission":
                pending_submission += 1
            latest = latest_valid_sample(by_case.get(case["id"], []))
            case_items.append({
                "case_id": case["id"],
                "status": case["status"],
                "lab_status": lab_status,
                "batch_id": latest["data"].get("batch_id") if latest else None,
            })
        return {
            "pending_samples": sum(
                1 for s in samples if s["status"] in ("received", "review_pending")
            ),
            "pending_review": sum(
                1 for s in samples if s["status"] == "review_pending"
            ),
            "invalid_samples": sum(1 for s in samples if s["status"] == "invalid"),
            "pending_submission_cases": pending_submission,
            "batches": [summarize_batch(batch, samples) for batch in batches],
            "cases": case_items,
        }

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
