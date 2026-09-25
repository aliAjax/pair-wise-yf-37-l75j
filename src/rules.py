from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _now():
    # 微秒精度，保证同一次送检流程中 result_at 的先后次序稳定
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


POSITIVE_RESULTS = ("positive", "detected")
NEGATIVE_RESULTS = ("negative", "not_detected", "undetected")

# 样本到达这些状态时产生有效结果，可被病例采纳
SAMPLE_VALID_RESULTS = {
    "screened_negative": "negative",
    "review_negative": "negative",
    "confirmed_positive": "positive",
}
# 检验人员还需处理的样本状态
SAMPLE_PENDING_STATUSES = ("collected", "pending_review")


def _normalize_result(value):
    text = str(value or "").strip().lower()
    if text in POSITIVE_RESULTS:
        return "positive"
    if text in NEGATIVE_RESULTS:
        return "negative"
    raise ValidationError("result must be positive or negative")


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_batch(actor, data, lookup):
    if not str(data.get("name", "")).strip():
        raise ValidationError("batch name is required")


def _validate_sample(actor, data, lookup):
    case = _find_one(lookup, "case", "id", data.get("case_id"))
    if not case:
        raise ValidationError("case not found: " + str(data.get("case_id")))
    batch = _find_one(lookup, "batch", "id", data.get("batch_id"))
    if not batch:
        raise ValidationError("batch not found: " + str(data.get("batch_id")))
    if batch["status"] != "open":
        raise ConflictError("batch is not open: " + batch["id"])
    resample_of = data.get("resample_of")
    if resample_of:
        origin = _find_one(lookup, "sample", "id", resample_of)
        if not origin:
            raise ValidationError("resample_of not found: " + str(resample_of))
        if origin["data"].get("case_id") != data.get("case_id"):
            raise ValidationError("resample must belong to the same case")
        if origin["status"] != "invalid":
            raise ConflictError("only an invalid sample can be resampled")


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _validate_screen(actor, entity, data, lookup):
    result = _normalize_result(data.get("result"))
    now = _now()
    patch = {"screen_result": result, "screened_by": actor.user_id, "screened_at": now}
    if result == "positive":
        # 初筛阳性先进入复核，不产生有效结果
        return "pending_review", patch
    patch["result_at"] = now
    return "screened_negative", patch


def _validate_review(actor, entity, data, lookup):
    result = _normalize_result(data.get("result"))
    now = _now()
    patch = {
        "review_result": result,
        "reviewed_by": actor.user_id,
        "reviewed_at": now,
        "result_at": now,
    }
    return ("confirmed_positive" if result == "positive" else "review_negative"), patch


def _validate_invalidate(actor, entity, data, lookup):
    return {"invalidated_by": actor.user_id, "invalidated_at": _now()}


def _validate_close_batch(actor, entity, data, lookup):
    samples = lookup("sample", "batch_id", entity["id"]) if lookup else []
    pending = [row for row in samples or [] if row["status"] in SAMPLE_PENDING_STATUSES]
    if pending:
        raise ConflictError("batch still has %d pending sample(s)" % len(pending))


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


CUSTOM_CREATE = {'case': _validate_case, 'batch': _validate_batch, 'sample': _validate_sample}
CUSTOM_TRANSITIONS = {('case', 'mark_probable'): _validate_probable, ('batch', 'close_batch'): _validate_close_batch, ('sample', 'screen'): _validate_screen, ('sample', 'review'): _validate_review, ('sample', 'invalidate'): _validate_invalidate}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact', 'batches': 'batch', 'samples': 'sample'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified', 'batch': 'open', 'sample': 'collected'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}, 'batch': {'close_batch': (('open',), 'closed')}, 'sample': {'screen': (('collected',), None), 'review': (('pending_review',), None), 'invalidate': (('collected', 'pending_review'), 'invalid')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start'), 'batch': ('name',), 'sample': ('case_id', 'batch_id')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('sample', 'screen'): ('result',), ('sample', 'review'): ('result',), ('sample', 'invalidate'): ('reason',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator'), 'batch': ('admin', 'lab'), 'sample': ('admin', 'lab')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), 'screen': ('admin', 'lab'), 'review': ('admin', 'lab'), 'invalidate': ('admin', 'lab'), 'close_batch': ('admin', 'lab')}

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
        if isinstance(extra, tuple):
            # 自定义校验可决定目标状态（如初筛按结果分流）
            next_status, extra = extra
        if not next_status:
            raise InvalidTransition("action %s for %s has no target status" % (action, kind))
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _valid_result_key(sample):
    data = sample["data"]
    return (
        str(data.get("result_at") or sample["updated_at"]),
        str(sample["created_at"]),
        str(sample["id"]),
    )


def adopted_lab_result(samples):
    """同一病例重复送检时采用最新有效结果；失效样本不参与。"""
    valid = [sample for sample in samples if sample["status"] in SAMPLE_VALID_RESULTS]
    if not valid:
        return None
    latest = max(valid, key=_valid_result_key)
    return {
        "result": SAMPLE_VALID_RESULTS[latest["status"]],
        "sample_id": latest["id"],
        "batch_id": latest["data"].get("batch_id"),
        "result_at": latest["data"].get("result_at") or latest["updated_at"],
    }


def derive_case_lab_status(case, samples):
    """病例的检验视图；没有样本记录的旧病例按待送检处理。"""
    pending = [sample for sample in samples if sample["status"] in SAMPLE_PENDING_STATUSES]
    view = {
        "case_id": case["id"],
        "case_status": case["status"],
        "lab_status": None,
        "sample_count": len(samples),
        "pending_count": len(pending),
        "adopted": None,
    }
    if not samples:
        view["lab_status"] = "pending_submission"
        return view
    adopted = adopted_lab_result(samples)
    if adopted:
        view["adopted"] = adopted
        view["lab_status"] = adopted["result"]
        return view
    if pending:
        latest = max(pending, key=lambda sample: (str(sample["created_at"]), str(sample["id"])))
        view["lab_status"] = "awaiting_review" if latest["status"] == "pending_review" else "awaiting_screening"
        return view
    view["lab_status"] = "needs_resample"
    return view


def batch_progress(batch, samples):
    """批次进度：按状态计数并给出待处理样本数。"""
    by_status = {}
    for sample in samples:
        by_status[sample["status"]] = by_status.get(sample["status"], 0) + 1
    pending = sum(by_status.get(status, 0) for status in SAMPLE_PENDING_STATUSES)
    return {
        "batch_id": batch["id"],
        "batch_status": batch["status"],
        "name": batch["data"].get("name"),
        "total": len(samples),
        "by_status": by_status,
        "pending": pending,
        "completed": len(samples) - pending,
    }


def lab_pending_summary(samples):
    """检验人员待办汇总：待初筛/待复核数量，按批次分组。"""
    summary = {"pending_total": 0, "awaiting_screening": 0, "awaiting_review": 0, "batches": {}}
    for sample in samples:
        status = sample["status"]
        if status not in SAMPLE_PENDING_STATUSES:
            continue
        key = "awaiting_review" if status == "pending_review" else "awaiting_screening"
        summary["pending_total"] += 1
        summary[key] += 1
        batch_id = sample["data"].get("batch_id")
        bucket = summary["batches"].setdefault(
            batch_id,
            {"batch_id": batch_id, "awaiting_screening": 0, "awaiting_review": 0, "pending": 0},
        )
        bucket[key] += 1
        bucket["pending"] += 1
    summary["batches"] = sorted(summary["batches"].values(), key=lambda item: str(item["batch_id"]))
    return summary
