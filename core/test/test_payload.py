import json
import asyncio

from ..request import prepare_request_payload

"""
测试脚本: 用于测试core/request.py中的get_payload函数

该测试脚本构造core/models.py中的RequestModel对象，然后调用core/request.py的get_payload函数，
返回url, headers, payload。通过这种方式可以单独测试get_payload模块的功能。

测试用例: 带工具函数的模型调用测试

使用的API配置信息来自api.yaml中的'new-i1-pe'。

python -m core.test_payload
"""

async def test_payload():
    print("===== 开始测试 get_payload 函数 =====")

    # 步骤1: 配置provider
    provider = {
        "provider": "new",
        "base_url": "/v1/chat/completions",
        "api": "",
        "model": [
            "gpt-4"  # 使用支持工具功能的模型名称
        ],
        "tools": True  # 启用工具支持
    }

    request_data = {
        "model": "gpt-4",
        "messages": [
            {
                "role": "system",
                "content": "你是一个有用的AI助手。"
            },
            {
                "role": "user",
                "content": "你好，请介绍一下自己。"
            }
        ],
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 1000,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_current_weather",
                    "description": "获取当前天气信息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "城市名称，例如：北京"
                            },
                            "unit": {
                                "type": "string",
                                "enum": ["celsius", "fahrenheit"],
                                "description": "温度单位"
                            }
                        },
                        "required": ["location"]
                    }
                }
            }
        ],
        "tool_choice": "auto"  # 添加工具选择参数
    }

    # 调用函数处理请求并获取结果
    url, headers, payload, engine = await prepare_request_payload(provider, request_data)

    # 打印结果
    print("\nURL:")
    print(url)

    print("\nHeaders:")
    print(json.dumps(headers, indent=4, ensure_ascii=False))

    print("\nPayload:")
    print(json.dumps(payload, indent=4, ensure_ascii=False))

    print("\n===== 测试完成 =====")

if __name__ == "__main__":
    # 执行异步测试函数
    asyncio.run(test_payload())
