"""Regression tests for precise evidence paths, with no network or writes."""

import unittest

from scholarship_intelligence.evidence_validation import _path_exists


class EvidencePathTests(unittest.TestCase):
    def test_reloaded_model_is_schema_validated_and_other_inputs_are_rejected(self):
        from pydantic import ValidationError, create_model
        from scholarship_intelligence.models import ScholarshipExtraction
        from scholarship_intelligence.evidence_validation import validate_evidence
        replacement = create_model("ScholarshipExtraction", __module__=ScholarshipExtraction.__module__)
        # A matching reload identity must still pass the complete schema.
        with self.assertRaises(ValidationError):
            validate_evidence(replacement(), "unused.json")
        for candidate in ({}, object()):
            with self.assertRaises(TypeError):
                validate_evidence(candidate, "unused.json")

    def setUp(self):
        self.payload = {"benefits": {"value": [
            {"description": "TEST benefit", "amount_missing_reason": None},
            {"description": "TEST support", "amount_missing_reason": "not_specified"},
        ]}, "status_statements": {"value": [{"statement": "TEST status", "stage": "TEST"}]},
            "matrix": [[{"name": "TEST nested array"}]], "mapping": {"0": "TEST key"}}

    def test_dotted_and_bracketed_array_paths_identify_existing_fields(self):
        for path in (
            "benefits", "benefits.value", "benefits.value.0.description",
            "benefits.value[0].description", "benefits.value[0].amount_missing_reason",
            "benefits.value[1].description", "benefits.value[1].amount_missing_reason",
            "status_statements.value[0].statement", "status_statements.value[0].stage",
            "matrix[0][0].name", "mapping.0",
        ):
            with self.subTest(path=path):
                self.assertTrue(_path_exists(self.payload, path))

    def test_missing_fields_out_of_range_and_wrong_containers_are_rejected(self):
        for path in (
            "benefits.value[2].description", "benefits.value.2.description",
            "benefits.value[0].invented", "benefits[0].value", "mapping[0]",
            "benefits.value[0].description[0]", "benefits.value[0].amount_missing_reason.child",
        ):
            with self.subTest(path=path):
                self.assertFalse(_path_exists(self.payload, path))

    def test_malformed_or_executable_notation_is_rejected(self):
        for path in (
            "", ".benefits", "benefits.", "benefits..value", " benefits.value",
            "benefits.value[-1]", "benefits.value[+0]", "benefits.value[0:1]",
            "benefits.value[*]", "benefits.value['0']", "benefits.value[0]garbage",
            "benefits.value[0].description()", "benefits.value[0][", "benefits.value[]",
            "benefits.value[__import__('os')]", "benefits.value[0]..description",
        ):
            with self.subTest(path=path):
                self.assertFalse(_path_exists(self.payload, path))

    def test_null_leaf_exists_without_allowing_traversal_through_null(self):
        self.assertTrue(_path_exists(self.payload, "benefits.value[0].amount_missing_reason"))
        self.assertFalse(_path_exists(self.payload, "benefits.value[0].amount_missing_reason.value"))


if __name__ == "__main__":
    unittest.main()
