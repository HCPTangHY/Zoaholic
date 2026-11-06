import asyncio
import json
from core.models import RequestModel
from core.channels.gemini_channel import (
    get_gemini_payload,
    patch_passthrough_gemini_payload,
    process_tool_parameters,
    inline_schema_refs,
)
from core.channels.vertex_channel import get_vertex_gemini_payload


def test_qi_batch_item_translation_const_anyof_schema():
    """Verify Qi's exact batch_item_translation schema with items.anyOf[{const:...}] converts to enum without const."""
    req = RequestModel(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "translate items"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "batch_item_translation",
                    "description": "Batch translate items",
                    "strict": False,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "item_ids": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 1, "maximum": 9999, "multipleOf": 1},
                                "minItems": 1,
                            },
                            "item_statuses": {
                                "type": "array",
                                "items": {
                                    "anyOf": [
                                        {"const": "untranslated"},
                                        {"const": "translated"},
                                        {"const": "excluded"},
                                    ]
                                },
                                "minItems": 1,
                            },
                            "mode": {"const": "fast"},
                            "nullable_tag": {"type": ["string", "null"], "examples": ["a"]},
                            "int_enum": {"type": "integer", "enum": [1, 2, None]},
                            "bare_array": {"type": "array"},
                            "tuple_array": {"type": "array", "items": [{"const": "x"}, {"type": "number"}]},
                        },
                        "required": ["item_statuses", "non_existent_prop"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "batch_item_translation"}},
        stop="STOP_HERE",
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "translation_result",
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {"anyOf": [{"const": "done"}, {"const": "error"}]},
                    },
                    "additionalProperties": False,
                },
            },
        },
    )
    provider = {
        "provider": "firebaseVertex",
        "engine": "firebaseVertex",
        "base_url": "https://firebasevertexai.googleapis.com/v1beta",
        "model": [{"gemini-4-argon": "gemini-4-argon"}],
        "project_id": "test-proj",
    }

    url, headers, g_payload = asyncio.run(get_gemini_payload(req, "firebaseVertex", provider, api_key="test-key"))
    dumped = json.dumps(g_payload, ensure_ascii=False)
    assert '"const"' not in dumped
    assert '"anyOf"' not in dumped
    assert '"additionalProperties"' not in dumped
    assert '"examples"' not in dumped

    fn_decl = g_payload["tools"][0]["function_declarations"][0]
    props = fn_decl["parameters"]["properties"]
    assert props["item_statuses"]["items"] == {"type": "string", "enum": ["untranslated", "translated", "excluded"]}
    assert props["mode"] == {"type": "string", "enum": ["fast"]}
    assert props["nullable_tag"] == {"type": "string", "nullable": True}
    assert props["int_enum"] == {"type": "string", "enum": ["1", "2"], "nullable": True}
    assert props["bare_array"] == {"type": "array", "items": {"type": "string"}}
    assert props["tuple_array"] == {"type": "array", "items": {"type": "string", "enum": ["x"]}}
    assert fn_decl["parameters"]["required"] == ["item_statuses"]
    assert g_payload["tool_config"]["function_calling_config"] == {
        "mode": "ANY",
        "allowed_function_names": ["batch_item_translation"],
    }
    assert g_payload["generationConfig"]["responseMimeType"] == "application/json"
    assert g_payload["generationConfig"]["responseSchema"]["properties"]["status"] == {
        "type": "string",
        "enum": ["done", "error"],
    }

    # Also test vertex_channel
    v_url, v_headers, v_payload = asyncio.run(get_vertex_gemini_payload(req, "vertex-gemini", provider, api_key="test-token"))
    v_dumped = json.dumps(v_payload, ensure_ascii=False)
    assert '"const"' not in v_dumped
    assert v_payload["tool_config"]["function_calling_config"] == {
        "mode": "ANY",
        "allowed_function_names": ["batch_item_translation"],
    }
    assert v_payload["generation_config"]["stopSequences"] == ["STOP_HERE"]
    assert v_payload["generation_config"]["responseMimeType"] == "application/json"


def test_developer_role_tool_content_list_and_thought_signature_fallback():
    """Verify developer role, list[ContentItem] in tool message, and thoughtSignature fallback."""
    req = RequestModel(
        model="gemini-4-argon",
        messages=[
            {"role": "developer", "content": "System rule 1"},
            {"role": "system", "content": "System rule 2"},
            {"role": "user", "content": "Run tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "my_tool", "arguments": '{"x": 1}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc",
                "content": [
                    {"type": "text", "text": "part 1"},
                    {"type": "text", "text": "part 2"},
                ],
            },
        ],
    )
    provider = {
        "provider": "firebaseVertex",
        "engine": "firebaseVertex",
        "base_url": "https://firebasevertexai.googleapis.com/v1beta",
        "model": [{"gemini-4-argon": "gemini-4-argon"}],
        "project_id": "test-proj",
    }

    _, _, g_payload = asyncio.run(get_gemini_payload(req, "firebaseVertex", provider, api_key="k"))
    # Must serialize cleanly to JSON without ContentItem TypeError
    raw_json = json.dumps(g_payload, ensure_ascii=False)
    assert "System rule 1\n\nSystem rule 2" == g_payload["systemInstruction"]["parts"][0]["text"]
    assert all(c["role"] in ("user", "model") for c in g_payload["contents"])
    model_turn = g_payload["contents"][1]
    assert model_turn["parts"][0]["thoughtSignature"] == "skip_thought_signature_validator"
    tool_turn = g_payload["contents"][2]
    assert tool_turn["parts"][0]["functionResponse"]["name"] == "my_tool"
    assert tool_turn["parts"][0]["functionResponse"]["response"]["result"] == "part 1\npart 2"

    _, _, v_payload = asyncio.run(get_vertex_gemini_payload(req, "vertex-gemini", provider, api_key="k"))
    json.dumps(v_payload, ensure_ascii=False)
    assert v_payload["system_instruction"]["parts"][0]["text"] == "System rule 1\n\nSystem rule 2"
    assert v_payload["contents"][1]["parts"][0]["thoughtSignature"] == "skip_thought_signature_validator"
    assert v_payload["contents"][2]["parts"][0]["functionResponse"]["response"]["content"]["result"] == "part 1\npart 2"


def test_passthrough_cleans_tool_parameters():
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "t1",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "s": {"anyOf": [{"const": "a"}, {"const": "b"}]},
                            },
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        ],
    }
    res = asyncio.run(
        patch_passthrough_gemini_payload(payload, {}, None, "firebaseVertex", {"engine": "firebaseVertex"})
    )
    assert '"const"' not in json.dumps(res)
    assert res["tools"][0]["functionDeclarations"][0]["parameters"]["properties"]["s"] == {
        "type": "string",
        "enum": ["a", "b"],
    }


def test_mid_and_trailing_system_developer_messages_preserved_in_place():
    """Leading system/developer go to systemInstruction; mid/trailing system/developer stay in contents as role='user'."""
    req = RequestModel(
        model="gemini-4-argon",
        messages=[
            {"role": "system", "content": "Leading system 1"},
            {"role": "developer", "content": "Leading developer 2"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "system", "content": "[Depth injection worldbook]"},
            {"role": "developer", "content": "[Dynamic post-history instruction]"},
            {"role": "user", "content": "What next?"},
        ],
    )
    provider = {
        "provider": "firebaseVertex",
        "engine": "firebaseVertex",
        "base_url": "https://firebasevertexai.googleapis.com/v1beta",
        "model": [{"gemini-4-argon": "gemini-4-argon"}],
        "project_id": "test-proj",
    }

    _, _, g_payload = asyncio.run(get_gemini_payload(req, "firebaseVertex", provider, api_key="k"))
    # Only leading system/developer should be in systemInstruction
    assert g_payload["systemInstruction"]["parts"][0]["text"] == "Leading system 1\n\nLeading developer 2"
    # Mid/trailing system/developer must remain in contents at exact index 2 and 3 as role="user"
    assert [c["role"] for c in g_payload["contents"]] == ["user", "model", "user", "user", "user"]
    assert g_payload["contents"][2]["parts"][0]["text"] == "[Depth injection worldbook]"
    assert g_payload["contents"][3]["parts"][0]["text"] == "[Dynamic post-history instruction]"
    assert g_payload["contents"][4]["parts"][0]["text"] == "What next?"

    _, _, v_payload = asyncio.run(get_vertex_gemini_payload(req, "vertex-gemini", provider, api_key="k"))
    assert v_payload["system_instruction"]["parts"][0]["text"] == "Leading system 1\n\nLeading developer 2"
    assert [c["role"] for c in v_payload["contents"]] == ["user", "model", "user", "user", "user"]
    assert v_payload["contents"][2]["parts"][0]["text"] == "[Depth injection worldbook]"
    assert v_payload["contents"][3]["parts"][0]["text"] == "[Dynamic post-history instruction]"
