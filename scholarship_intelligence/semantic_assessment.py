"""Versioned review outcomes consumed by scoring; no API calls or scores."""

from datetime import datetime
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scholarship_intelligence.models import Claim, ScholarshipExtraction, Text


ASSESSMENT_VERSION = "semantic-assessment-v1"

# Each entry is (metric group, critical, permits not_applicable).
# These definitions belong to the application, not the extracting model.
CHECK_DEFINITIONS = {
    "primary_source_ownership": ("authenticity", True, False),
    "source_integrity": ("authenticity", True, False),
    "evidence_integrity": ("information_support", True, False),
    "critical_claim_support": ("information_support", True, False),
    "completeness": ("information_support", True, False),
    "numbers_dates_units": ("information_support", True, True),
    "application_route_support": ("information_support", True, True),
    "offering_cycle_context": ("currency_consistency", True, False),
    "conflict_resolution": ("currency_consistency", True, False),
    "availability_support": ("currency_consistency", True, True),
}


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    check_id: Text
    status: Literal["pass", "fail", "pending", "not_applicable"]
    reason: Text
    evidence_ids: tuple[Text, ...] = ()


class SemanticAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    assessment_version: Literal["semantic-assessment-v1"] = ASSESSMENT_VERSION
    bundle_id: Text
    snapshot_ids: tuple[Text, ...] = Field(min_length=1)
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessed_at: datetime
    evaluator: Text
    checks: tuple[CheckResult, ...]
    withheld_fields: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def check_assessment(self):
        if self.assessed_at.tzinfo is None or self.assessed_at.utcoffset() is None:
            raise ValueError("Assessment time must include a timezone.")
        if len(self.snapshot_ids) != len(set(self.snapshot_ids)):
            raise ValueError("Duplicate assessment snapshots.")
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate review checks.")
        if set(ids) != set(CHECK_DEFINITIONS):
            raise ValueError("Review must contain exactly the configured checks.")
        for check in self.checks:
            if check.status == "not_applicable" and not CHECK_DEFINITIONS[check.check_id][2]:
                raise ValueError("This critical check cannot be marked not applicable.")
            if len(check.evidence_ids) != len(set(check.evidence_ids)):
                raise ValueError("Duplicate review evidence references.")
        if len(self.withheld_fields) != len(set(self.withheld_fields)):
            raise ValueError("Duplicate withheld fields.")
        if self.withheld_fields and all(
            check.status in ("pass", "not_applicable") for check in self.checks
        ):
            raise ValueError("Withheld claims require an outstanding review check.")
        return self

    @property
    def critical_checks_passed(self) -> bool:
        return not self.withheld_fields and all(
            check.status in ("pass", "not_applicable")
            for check in self.checks
            if CHECK_DEFINITIONS[check.check_id][1]
        )

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            f"{check.check_id}: {check.reason}"
            for check in self.checks
            if CHECK_DEFINITIONS[check.check_id][1] and check.status in ("fail", "pending")
        )


def candidate_fingerprint(candidate: ScholarshipExtraction) -> str:
    content = json.dumps(
        candidate.model_dump(mode="json"), sort_keys=True,
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return sha256(content).hexdigest()


def validate_assessment(
    candidate: ScholarshipExtraction,
    assessment: SemanticAssessment,
) -> SemanticAssessment:
    """Bind review outcomes to the exact candidate and known evidence."""
    candidate = ScholarshipExtraction.model_validate(candidate.model_dump())
    assessment = SemanticAssessment.model_validate(assessment.model_dump())
    if assessment.bundle_id != candidate.bundle_id:
        raise ValueError("Assessment bundle differs from candidate.")
    if assessment.snapshot_ids != tuple(candidate.snapshot_ids):
        raise ValueError("Assessment snapshots differ from candidate.")
    if assessment.candidate_fingerprint != candidate_fingerprint(candidate):
        raise ValueError("Candidate changed after assessment.")
    evidence_ids = {item.evidence_id for item in candidate.evidence}
    for check in assessment.checks:
        if not set(check.evidence_ids).issubset(evidence_ids):
            raise ValueError("Review references unknown evidence.")
    claim_fields = {
        name for name in type(candidate).model_fields
        if isinstance(getattr(candidate, name), Claim)
    }
    if not set(assessment.withheld_fields).issubset(claim_fields):
        raise ValueError("Review withholds an unknown claim field.")
    checks = {check.check_id: check for check in assessment.checks}
    if candidate.application_url.value is not None and checks["application_route_support"].status == "not_applicable":
        raise ValueError("An asserted application URL requires route review.")
    return assessment
