from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import (
    RuleEngine,
    SAMPLE_VALID_RESULTS,
    adopted_lab_result,
    batch_progress as batch_progress_view,
    derive_case_lab_status,
    lab_pending_summary,
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
        if updated["kind"] == "sample" and updated["status"] in SAMPLE_VALID_RESULTS:
            self._apply_sample_result(actor, updated)
        return updated

    def _apply_sample_result(self, actor, sample):
        """样本产生有效结果时回写病例：复核阳性确认病例；病例始终采纳最新
        有效结果；复核阴性或样本失效时保持原诊断不变。"""
        case_id = sample["data"].get("case_id")
        if not case_id:
            return
        case = self.repository.get_entity(case_id)
        if not case:
            return
        adopted = adopted_lab_result(
            self.repository.find_entities("sample", "case_id", case_id)
        )
        patch = {}
        status = case["status"]
        if sample["status"] == "confirmed_positive" and status in ("reported", "investigating"):
            status = "confirmed"
            patch.update({
                "confirmed_by": actor.user_id,
                "confirmed_via": sample["id"],
                "confirmed_at": utcnow(),
            })
        if adopted:
            patch.update({
                "lab_result": adopted["result"],
                "lab_result_sample_id": adopted["sample_id"],
                "lab_result_at": adopted["result_at"],
            })
        if not patch:
            return
        merged = dict(case["data"])
        merged.update(patch)
        self.repository.update_entity(case_id, None, status, merged)
        self.audit.record(
            case_id,
            actor,
            "confirm" if status != case["status"] else "lab_result",
            case["status"],
            status,
            {"patch": patch, "sample_id": sample["id"]},
        )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def case_lab_status(self, case_id):
        case = self.get(case_id)
        if case["kind"] != "case":
            raise ValidationError("entity is not a case: " + case_id)
        samples = self.repository.find_entities("sample", "case_id", case_id)
        return derive_case_lab_status(case, samples)

    def batch_progress(self, batch_id):
        batch = self.get(batch_id)
        if batch["kind"] != "batch":
            raise ValidationError("entity is not a batch: " + batch_id)
        samples = self.repository.find_entities("sample", "batch_id", batch_id)
        return batch_progress_view(batch, samples)

    def lab_pending(self):
        return lab_pending_summary(self.repository.list_entities(kind="sample"))

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
