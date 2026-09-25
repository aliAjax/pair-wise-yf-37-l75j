from datetime import datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 样本仍需检验人员处理的状态：待初筛、待复核
PENDING_SAMPLE_STATUSES = ("received", "review_pending")

# 病例派生检验状态（无样本记录的旧病例按待送检处理）
LAB_STATUS_PENDING_SUBMISSION = ("pending_submission", "待送检")
LAB_STATUS_RECOLLECTION = ("recollection_needed", "待重采")
LAB_STATUS_BY_SAMPLE = {
    "received": ("screening", "初筛中"),
    "review_pending": ("pending_review", "待复核"),
    "screened_negative": ("screen_negative", "初筛阴性"),
    "reviewed_negative": ("review_negative", "复核阴性"),
    "reviewed_positive": ("review_positive", "复核阳性"),
}

CONFIRMABLE_CASE_STATUSES = ("investigating", "probable")


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_batch(actor, data, lookup):
    if not data.get("batch_no"):
        raise ValidationError("missing required field: batch_no")
    rows = lookup("batch", "batch_no", data.get("batch_no")) or [] if lookup else []
    if rows:
        raise ConflictError("duplicate batch_no: " + str(data.get("batch_no")))


def _validate_sample(actor, data, lookup):
    case = _find_one(lookup, "case", "id", data.get("case_id"))
    if not case:
        raise ValidationError("sample must reference an existing case")
    batch = _find_one(lookup, "batch", "id", data.get("batch_id"))
    if not batch:
        raise ValidationError("sample must reference an existing batch")
    if batch["status"] != "open":
        raise ValidationError("batch %s is not open for new samples" % batch["id"])
    recollected_from = data.get("recollected_from")
    if recollected_from:
        original = _find_one(lookup, "sample", "id", recollected_from)
        if not original or original["data"].get("case_id") != data.get("case_id"):
            raise ValidationError("recollection must reference a sample of the same case")
        if original["status"] != "invalid":
            raise ValidationError("only invalid samples can be recollected")


def _validate_lab_positive(actor, entity, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["data"].get("case_id") != entity["id"]:
        raise ValidationError("sample_id must reference a sample of this case")
    if sample["status"] != "reviewed_positive":
        raise ValidationError("only a review-positive sample can confirm a case")
    latest = latest_valid_sample(lookup("sample", "case_id", entity["id"]) or [])
    if latest and latest["id"] != sample["id"]:
        raise ValidationError("a newer valid sample result exists for this case")
    return {"confirmed_by": actor.user_id, "confirming_sample_id": sample["id"]}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _validate_batch_close(actor, entity, data, lookup):
    samples = lookup("sample", "batch_id", entity["id"]) or []
    pending = [s for s in samples if s["status"] in PENDING_SAMPLE_STATUSES]
    if pending:
        raise ValidationError("batch still has %d pending sample(s)" % len(pending))


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


def latest_valid_sample(samples):
    """同一病例重复送检时，失效样本不算有效结果，取最新一条有效样本。"""
    valid = [s for s in samples if s.get("status") != "invalid"]
    if not valid:
        return None
    return sorted(
        valid,
        key=lambda s: (s.get("data", {}).get("_seq", 0), s.get("created_at", ""), s.get("id", "")),
    )[-1]


def case_lab_status(case, samples):
    """派生病例检验状态；旧病例没有任何样本记录时按待送检处理。"""
    if not samples:
        code, label = LAB_STATUS_PENDING_SUBMISSION
        return {"code": code, "label": label, "sample_id": None}
    latest = latest_valid_sample(samples)
    if latest is None:
        code, label = LAB_STATUS_RECOLLECTION
        return {"code": code, "label": label, "sample_id": None}
    code, label = LAB_STATUS_BY_SAMPLE[latest["status"]]
    return {"code": code, "label": label, "sample_id": latest["id"]}


def summarize_batch(batch, samples):
    members = [s for s in samples if s.get("data", {}).get("batch_id") == batch["id"]]
    counts = {}
    for sample in members:
        counts[sample["status"]] = counts.get(sample["status"], 0) + 1
    return {
        "id": batch["id"],
        "batch_no": batch["data"].get("batch_no"),
        "status": batch["status"],
        "total": len(members),
        "counts": counts,
        "pending": sum(counts.get(status, 0) for status in PENDING_SAMPLE_STATUSES),
        "confirmed": counts.get("reviewed_positive", 0),
    }


CUSTOM_CREATE = {"batch": _validate_batch, "case": _validate_case, "sample": _validate_sample}
CUSTOM_TRANSITIONS = {
    ("batch", "close"): _validate_batch_close,
    ("case", "lab_positive"): _validate_lab_positive,
    ("case", "mark_probable"): _validate_probable,
}


class RuleEngine:
    ALIASES = {"batches": "batch", "cases": "case", "contacts": "contact", "samples": "sample"}
    INITIAL_STATUS = {"batch": "open", "case": "reported", "contact": "identified", "sample": "received"}
    TRANSITIONS = {
        "batch": {
            "close": (("open",), "closed"),
        },
        "case": {
            "triage": (("reported",), "investigating"),
            "lab_positive": ((CONFIRMABLE_CASE_STATUSES), "confirmed"),
            "mark_probable": (("investigating",), "probable"),
            "recover": (("confirmed", "probable"), "recovered"),
            "close": (("recovered",), "closed"),
        },
        "contact": {
            "begin_followup": (("identified",), "following"),
            "complete_followup": (("following",), "completed"),
        },
        "sample": {
            "screen_positive": (("received",), "review_pending"),
            "screen_negative": (("received",), "screened_negative"),
            "mark_invalid": (("received", "review_pending"), "invalid"),
            "review_positive": (("review_pending",), "reviewed_positive"),
            "review_negative": (("review_pending",), "reviewed_negative"),
        },
    }
    CREATE_REQUIRED = {
        "batch": ("batch_no",),
        "case": ("person_id", "onset_date", "location", "symptoms"),
        "contact": ("case_id", "person_id", "exposure_start"),
        "sample": ("case_id", "batch_id"),
    }
    ACTION_REQUIRED = {
        ("batch", "close"): (),
        ("case", "triage"): ("clinician",),
        ("case", "lab_positive"): ("lab_id", "sample_id"),
        ("case", "mark_probable"): ("epi_link",),
        ("case", "recover"): ("recovered_at",),
        ("case", "close"): ("outcome",),
        ("contact", "begin_followup"): ("followup_start", "due_at"),
        ("contact", "complete_followup"): ("outcome",),
        ("sample", "screen_positive"): ("lab_id",),
        ("sample", "screen_negative"): ("lab_id",),
        ("sample", "mark_invalid"): ("lab_id", "reason"),
        ("sample", "review_positive"): ("lab_id",),
        ("sample", "review_negative"): ("lab_id",),
    }
    CREATE_ROLES = {
        "batch": ("admin", "lab"),
        "case": ("admin", "clinician"),
        "contact": ("admin", "investigator"),
        "sample": ("admin", "clinician", "investigator", "lab"),
    }
    ROLE_ACTIONS = {
        "triage": ("admin", "clinician"),
        "lab_positive": ("admin", "lab"),
        "mark_probable": ("admin", "investigator"),
        "recover": ("admin", "clinician"),
        "close": ("admin", "lab", "investigator"),
        "begin_followup": ("admin", "investigator"),
        "complete_followup": ("admin", "investigator"),
        ("batch", "close"): ("admin", "lab"),
        ("case", "close"): ("admin", "investigator"),
        "screen_positive": ("admin", "lab"),
        "screen_negative": ("admin", "lab"),
        "mark_invalid": ("admin", "lab"),
        "review_positive": ("admin", "lab"),
        "review_negative": ("admin", "lab"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
