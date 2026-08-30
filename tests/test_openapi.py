"""Automates checks that were previously one-off manual audits (see notes.txt's
2026-08-19/20 entries): the live OpenAPI document is structurally valid, and every
schema field / every operation carries the documentation this project holds itself to.
"""

from fastapi.testclient import TestClient
from openapi_spec_validator import validate


def test_openapi_schema_is_structurally_valid(client: TestClient):
    spec = client.get("/openapi.json").json()
    validate(spec)  # raises OpenAPISpecValidatorError on any structural violation


def test_every_schema_field_has_a_description(client: TestClient):
    spec = client.get("/openapi.json").json()
    missing = []
    for name, schema in spec["components"]["schemas"].items():
        for prop_name, prop in schema.get("properties", {}).items():
            if "description" not in prop and "$ref" not in prop and "allOf" not in prop:
                missing.append(f"{name}.{prop_name}")
    assert not missing, f"schema fields missing a description: {missing}"


def test_every_operation_has_summary_tags_and_operation_id(client: TestClient):
    spec = client.get("/openapi.json").json()
    problems = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            label = f"{method.upper()} {path}"
            if not op.get("operationId"):
                problems.append(f"{label}: missing operationId")
            if not op.get("tags"):
                problems.append(f"{label}: missing tags")
            if not op.get("summary"):
                problems.append(f"{label}: missing summary")
    assert not problems, problems
