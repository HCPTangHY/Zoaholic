import asyncio

import pytest

from core.channels.gemini_channel import get_gemini_payload
from core.channels.vertex_channel import get_vertex_gemini_payload
from core.models import RequestModel


@pytest.mark.parametrize(
    "payload_builder, engine, base_url",
    [
        (get_gemini_payload, "gemini", "https://generativelanguage.googleapis.com/v1beta"),
        (get_vertex_gemini_payload, "vertex-gemini", "https://aiplatform.googleapis.com/v1beta"),
    ],
    ids=["gemini", "vertex-gemini"],
)
def test_nullable_tool_type_arrays_in_payload(payload_builder, engine, base_url):
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "skip": {"type": ["number", "null"], "description": "Rows to skip"},
            "reverse": {"type": ["null", "number"]},
            "label": {"type": ["string", "null"], "description": "Label", "enum": ["a", "b"]},
            "nested": {
                "type": "object",
                "properties": {"flag": {"type": ["boolean", "null"]}},
            },
            "values": {"type": "array", "items": {"type": ["null", "string"]}},
            "legacy": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "union": {"type": ["string", "number", "null"]},
        },
        "required": ["pattern"],
    }
    request = RequestModel(
        model="gemini-2.0-flash",
        messages=[{"role": "user", "content": "OK"}],
        stream=False,
        tools=[{
            "type": "function",
            "function": {"name": "grep", "parameters": parameters},
        }],
    )
    original_request = request.model_dump(exclude_unset=True)
    provider = {
        "base_url": base_url,
        "model": [request.model],
        "project_id": "test-project",
    }

    _, _, payload = asyncio.run(payload_builder(request, engine, provider))
    function = payload["tools"][0]["function_declarations"][0]

    assert function["name"] == "grep"
    assert function["parameters"] == {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "skip": {"type": "number", "nullable": True, "description": "Rows to skip"},
            "reverse": {"type": "number", "nullable": True},
            "label": {"type": "string", "nullable": True, "description": "Label", "enum": ["a", "b"]},
            "nested": {
                "type": "object",
                "properties": {"flag": {"type": "boolean", "nullable": True}},
            },
            "values": {"type": "array", "items": {"type": "string", "nullable": True}},
            "legacy": {"type": "number", "nullable": True},
            "union": {"anyOf": [{"type": "string"}, {"type": "number"}], "nullable": True},
        },
        "required": ["pattern"],
    }
    assert request.model_dump(exclude_unset=True) == original_request


def _convert(schema):
    import copy
    from core.channels.gemini_channel import process_tool_parameters

    data = copy.deepcopy(schema)
    process_tool_parameters(data)
    return data


def test_multi_type_union_moves_type_specific_fields_into_branches():
    assert _convert({"type": ["array", "string", "null"], "items": {"type": ["integer", "null"]}, "description": "ids"}) == {
        "description": "ids",
        "nullable": True,
        "anyOf": [
            {"type": "array", "items": {"type": "integer", "nullable": True}},
            {"type": "string"},
        ],
    }


def test_typed_any_of_branches_are_kept():
    schema = {"anyOf": [
        {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        {"type": "string"},
        {"type": "null"},
    ]}
    assert _convert(schema) == {
        "nullable": True,
        "anyOf": [
            {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            {"type": "string"},
        ],
    }


def test_constraint_only_any_of_falls_back_to_first_branch():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "anyOf": [{"required": ["a"]}, {"required": ["b"]}],
    }
    converted = _convert(schema)
    assert "anyOf" not in converted
    assert converted["type"] == "object"
