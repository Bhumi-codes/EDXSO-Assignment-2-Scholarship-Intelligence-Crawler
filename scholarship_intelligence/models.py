"""Extraction structures and structural checks, not factual verification."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
NonnegativeNumber = Annotated[
    Decimal, Field(ge=0, allow_inf_nan=False)
]

MissingReason = Literal[
    "not_specified_in_inspected_material",
    "extraction_failed",
    "inaccessible_material",
    "ambiguous_or_conflicting",
    "not_applicable",
]

ApplicationStage = Literal[
    "student_application",
    "renewal_application",
    "institution_verification",
    "nomination",
    "other",
    "unspecified",
]


class Model(BaseModel):
    # Reject unexpected fields instead of silently discarding them.
    model_config = ConfigDict(extra="forbid")


class SourceLocation(Model):
    block_ids: list[Text] = Field(min_length=1)
    page_number: Annotated[int, Field(ge=1, strict=True)] | None = None
    section: Text | None = None


class Evidence(Model):
    # Local extraction identifiers, not database row identifiers.
    evidence_id: Text
    snapshot_id: Text
    quote: Text
    location: SourceLocation
    field_paths: list[Text] = Field(min_length=1)


T = TypeVar("T")


class Claim(Model, Generic[T]):
    """A proposed value, or an explicit explanation of why it is unknown."""

    value: T | None
    missing_reason: MissingReason | None
    explanation: Text | None = None
    evidence_ids: list[Text] = Field(default_factory=list)
    inspected_snapshot_ids: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_value_and_missing_reason(self):
        if self.value is None:
            if self.missing_reason is None or self.explanation is None:
                raise ValueError(
                    "An unknown value requires a missing reason and explanation."
                )
            if (
                self.missing_reason == "not_specified_in_inspected_material"
                and not self.inspected_snapshot_ids
            ):
                raise ValueError(
                    "'Not specified' requires inspected snapshot references."
                )
        else:
            if self.missing_reason is not None:
                raise ValueError("A present value cannot have a missing reason.")
            if not self.evidence_ids:
                raise ValueError("A proposed value requires evidence references.")

        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("Duplicate evidence references.")

        if len(self.inspected_snapshot_ids) != len(
            set(self.inspected_snapshot_ids)
        ):
            raise ValueError("Duplicate inspected snapshot references.")

        return self


class Benefit(Model):
    description: Text
    amount_kind: Literal[
        "fixed", "maximum", "minimum", "range", "variable", "unspecified"
    ]
    amount: NonnegativeNumber | None = None
    minimum_amount: NonnegativeNumber | None = None
    maximum_amount: NonnegativeNumber | None = None
    amount_missing_reason: MissingReason | None = None
    currency: Text | None = None
    period: Literal[
        "one_time", "monthly", "annual", "whole_course", "other", "unspecified"
    ]
    period_details: Text | None = None
    qualifiers: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_amount_shape(self):
        if self.amount_kind == "range":
            if self.amount is not None:
                raise ValueError("A range uses minimum_amount and maximum_amount.")
            if self.minimum_amount is None or self.maximum_amount is None:
                raise ValueError("A range requires both amount boundaries.")
            if self.minimum_amount > self.maximum_amount:
                raise ValueError("Amount range boundaries are reversed.")
        else:
            if self.minimum_amount is not None or self.maximum_amount is not None:
                raise ValueError("Range boundaries require amount_kind='range'.")
            if (
                self.amount_kind in {"fixed", "maximum", "minimum"}
                and self.amount is None
            ):
                raise ValueError("This amount kind requires a numeric amount.")

        has_amount = self.amount is not None or self.minimum_amount is not None
        if has_amount:
            if self.currency is None:
                raise ValueError("A monetary amount requires a currency.")
            if self.amount_missing_reason is not None:
                raise ValueError("A supplied amount cannot have a missing reason.")
        elif self.amount_missing_reason is None:
            raise ValueError("An absent amount requires a missing reason.")

        return self


class NumericConstraint(Model):
    operator: Literal["lt", "lte", "eq", "gte", "gt", "range"]
    value: Annotated[Decimal, Field(allow_inf_nan=False)]
    upper_value: Annotated[Decimal, Field(allow_inf_nan=False)] | None = None
    unit: Text
    qualifiers: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_range(self):
        if self.operator == "range":
            if self.upper_value is None or self.upper_value < self.value:
                raise ValueError("A range requires ordered lower/upper values.")
        elif self.upper_value is not None:
            raise ValueError("upper_value is only valid for a range.")
        return self


class EligibilityCondition(Model):
    kind: Literal["condition"]
    category: Literal[
        "academic",
        "course_level",
        "income",
        "age",
        "gender",
        "category",
        "domicile",
        "institution",
        "disability",
        "defence",
        "sports",
        "research",
        "citizenship",
        "other",
    ]
    statement: Text
    numeric: NumericConstraint | None = None
    qualifiers: list[Text] = Field(default_factory=list)


class EligibilityGroup(Model):
    kind: Literal["group"]
    relation: Literal["all", "any", "not"]
    qualifiers: list[Text] = Field(default_factory=list)
    children: list[
        Annotated[
            EligibilityCondition | EligibilityGroup,
            Field(discriminator="kind"),
        ]
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def check_negation(self):
        if self.relation == "not" and len(self.children) != 1:
            raise ValueError("A 'not' group requires exactly one child.")
        return self


EligibilityGroup.model_rebuild()

EligibilityNode = Annotated[
    EligibilityCondition | EligibilityGroup,
    Field(discriminator="kind"),
]


class Eligibility(Model):
    summary: Text
    conditions: EligibilityNode
    exceptions: list[Text] = Field(default_factory=list)


class ApplicationDate(Model):
    calendar_date: date
    stage: ApplicationStage
    route: Text | None = None
    # Preserve a stated cutoff qualifier without inventing a time or timezone.
    time_qualifier: Text | None = None


class RequiredDocument(Model):
    name: Text
    condition: Text | None = None


class RenewalRequirements(Model):
    summary: Text
    conditions: list[Text] = Field(default_factory=list)


class StatusStatement(Model):
    statement: Text
    stage: ApplicationStage
    route: Text | None = None


class ScholarshipExtraction(Model):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: Text
    snapshot_ids: list[Text] = Field(min_length=1)
    evidence: list[Evidence]

    scholarship_name: Claim[Text]
    provider: Claim[Text]
    official_source: Claim[HttpUrl]
    application_url: Claim[HttpUrl]
    cycle: Claim[Text]
    benefits: Claim[list[Benefit]]
    eligibility: Claim[Eligibility]
    opening_date: Claim[ApplicationDate]
    closing_date: Claim[ApplicationDate]
    documents: Claim[list[RequiredDocument]]
    selection_process: Claim[Text]
    renewal_requirements: Claim[RenewalRequirements]
    status_statements: Claim[list[StatusStatement]]

    @model_validator(mode="after")
    def check_references(self):
        if len(self.snapshot_ids) != len(set(self.snapshot_ids)):
            raise ValueError("Duplicate bundle snapshot references.")

        snapshots = set(self.snapshot_ids)
        evidence_by_id = {}
        for item in self.evidence:
            if item.evidence_id in evidence_by_id:
                raise ValueError("Duplicate evidence identifiers.")
            if item.snapshot_id not in snapshots:
                raise ValueError("Evidence references a snapshot outside the bundle.")
            evidence_by_id[item.evidence_id] = item

        for field_name in type(self).model_fields:
            claim = getattr(self, field_name)
            if not isinstance(claim, Claim):
                continue

            if not set(claim.inspected_snapshot_ids).issubset(snapshots):
                raise ValueError(
                    f"{field_name}: inspected snapshot is outside the bundle."
                )

            for evidence_id in claim.evidence_ids:
                item = evidence_by_id.get(evidence_id)
                if item is None:
                    raise ValueError(
                        f"{field_name}: unknown evidence ID {evidence_id}."
                    )
                if not any(
                    path == field_name or path.startswith(field_name + ".")
                    for path in item.field_paths
                ):
                    raise ValueError(
                        f"{field_name}: evidence is attributed to another field."
                    )

        return self

OFFERING_CLASSIFICATION_VERSION = "offering-types-v2"


def classify_offering(name, claims=None):
    """Conservative source-based type; unknown and combined types need review.

    An explicitly named award stays an award even when it funds education.
    The shared listing URL, cash amount and general word 'awarded' do not
    establish that a programme is a scholarship.
    """
    title = name if isinstance(name, str) else ""
    markers = {
        "scholarship": bool(re.search(r"\bscholarships?\b", title, re.I)),
        "award": bool(re.search(r"\b(?:awards?|medals?|prizes?)\b", title, re.I)),
        "fellowship": bool(re.search(r"\bfellowships?\b", title, re.I)),
    }
    present = [kind for kind, found in markers.items() if found]
    basis = "scholarship_name"
    if not present:
        name_claim = (claims or {}).get("scholarship_name", {})
        designation = name_claim.get("explanation", "") if isinstance(name_claim, dict) else ""
        if isinstance(designation, str) and designation.startswith("Source programme designation: ") and name_claim.get("evidence_ids"):
            present = [kind for kind, marker in (("scholarship", "scholarship code"), ("scholarship", "scholarship initiative"), ("award", "award id")) if marker in designation.lower()]
            present = list(dict.fromkeys(present))
            basis = "source_programme_designation"
    if not present:
        # Only explicit programme terminology in claim text supplies a type.
        # Generic financial support and eligibility alone cannot supply it.
        excerpts = []
        for field in ("benefits", "eligibility", "selection_process", "renewal_requirements"):
            claim = (claims or {}).get(field, {})
            value = claim.get("value") if isinstance(claim, dict) else None
            if isinstance(value, str):
                excerpts.append(value)
            elif isinstance(value, dict):
                excerpts.append(str(value.get("summary", "")))
            elif isinstance(value, list):
                excerpts.extend(str(item.get("description", "")) for item in value if isinstance(item, dict))
        text = "\n".join(excerpts)
        # Positive programme descriptions only, rather than exclusions such
        # as 'not eligible for another scholarship'.
        patterns = {
            "scholarship": r"\b(?:this|the|annual|monthly)\s+scholarship\b|\bscholarship\s+(?:covers?|provides?|supports?|is payable)\b",
            "award": r"\b(?:this|the)\s+(?:award|prize|medal)\b|\b(?:cash|thesis|lecture)\s+award\b",
            "fellowship": r"\b(?:this|the|annual|monthly)\s+fellowship\b|\bfellowship\s+(?:covers?|provides?|supports?)\b",
        }
        present = [kind for kind, pattern in patterns.items() if re.search(pattern, text, re.I)]
        basis = "programme_description"
    kind = present[0] if len(present) == 1 else "mixed" if present else "unknown"
    reason = ("Explicit " + present[0] + " programme terminology in " + basis.replace("_", " ") + "."
              if len(present) == 1 else "Combined programme types need separate identities: " + ", ".join(present) + "."
              if present else "No explicit programme type established; retain for classification review.")
    return {"classification_version": OFFERING_CLASSIFICATION_VERSION, "offering_type": kind,
            "basis": basis, "reason": reason, "main_dashboard_eligible": kind == "scholarship"}
