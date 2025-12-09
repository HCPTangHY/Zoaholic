"""
Claude Thinking 模式插件

功能：
1. 识别以 -thinking 结尾的 Claude 模型名称
2. 自动添加 <thinking> 预填充消息
3. 调整 reasoning 和 completion token 预算
4. 在响应流中将 <thinking>...</thinking> 内容映射到 reasoning_content

使用方式：
- 请求模型名为 claude-3-5-sonnet-20241022-thinking 时自动启用
- 会自动去掉 -thinking 后缀，添加预填充，并转换响应流

关键设计：
- 使用 request_info 在请求和响应拦截器之间共享状态（避免 contextvars 跨异步边界丢失）
- 预填充 <thinking> 后，上游响应不会再返回 <thinking> 标签
- 转换器初始状态为 inside_thinking=True，等待 </thinking> 标签
"""

import re
import json
import asyncio
from typing import Any, Dict, Optional, Tuple

from core.log_config import logger
from core.middleware import request_info
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
    register_response_interceptor,
    unregister_response_interceptor,
)


# 插件元信息
PLUGIN_INFO = {
    "name": "claude_thinking",
    "version": "1.1.0",
    "description": "Claude Thinking 模式插件 - 支持 -thinking 后缀模型的思考链处理",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["claude", "thinking", "reasoning"],
    },
}

# 声明提供的扩展
EXTENSIONS = [
    "interceptors:claude_thinking_request",
    "interceptors:claude_thinking_response",
]

# 常量
THINK_OPEN = "<thinking>"
THINK_CLOSE = "</thinking>"


def is_thinking_claude_model(model: Any) -> bool:
    """
    检查是否为 thinking 模式的 Claude 模型
    
    Args:
        model: 模型名称
        
    Returns:
        是否为 thinking 模式
    """
    if not isinstance(model, str):
        return False
    
    return (
        ("claude" in model.lower() or "codewise" in model.lower())
        and model.endswith("-thinking")
    )


def add_prefill_thinking_message(payload: Dict[str, Any]) -> None:
    """
    添加 <thinking> 预填充消息
    
    Args:
        payload: 请求 payload
    """
    if "messages" not in payload or not isinstance(payload["messages"], list):
        payload["messages"] = []
    
    messages = payload["messages"]
    
    # 检查最后一条消息是否已经是预填充
    if messages:
        last = messages[-1]
        if (
            isinstance(last, dict)
            and last.get("role") == "assistant"
            and last.get("content") == THINK_OPEN
        ):
            return
    
    # 添加预填充消息
    messages.append({
        "role": "assistant",
        "content": THINK_OPEN
    })


def adjust_reasoning_and_completion_tokens(payload: Dict[str, Any]) -> None:
    """
    调整 reasoning 和 completion token 预算
    
    Args:
        payload: 请求 payload
    """
    # 确保 reasoning 对象存在
    if "reasoning" not in payload or not isinstance(payload.get("reasoning"), dict):
        payload["reasoning"] = {}
    
    reasoning = payload["reasoning"]
    
    # 设置 reasoning max_tokens
    reasoning_max_tokens = reasoning.get("max_tokens")
    if not isinstance(reasoning_max_tokens, (int, float)) or reasoning_max_tokens <= 0:
        reasoning_max_tokens = 32768
        reasoning["max_tokens"] = reasoning_max_tokens
    
    # 调整 max_completion_tokens
    user_max = payload.get("max_completion_tokens")
    min_answer_budget = 8192
    default_answer_budget = 16384
    min_required = reasoning_max_tokens + min_answer_budget
    
    if not isinstance(user_max, (int, float)) or user_max <= reasoning_max_tokens:
        payload["max_completion_tokens"] = reasoning_max_tokens + default_answer_budget
    elif user_max < min_required:
        payload["max_completion_tokens"] = min_required


class ThinkingStreamTransformer:
    """
    SSE 流转换器
    
    将 </thinking> 前的内容映射到 reasoning_content，
    之后的内容映射到 content。
    """
    
    def __init__(self):
        self.close_tag = THINK_CLOSE
        self.close_tag_lower = self.close_tag.lower()
        self.keep_tail = len(self.close_tag_lower) - 1
        self.pending = ""
        self.inside_thinking = True  # 预填充后，初始就在 thinking 模式中
    
    def build_patched_data(self, parsed: Dict[str, Any], patch_delta: Dict[str, Any]) -> Dict[str, Any]:
        """构建修补后的数据"""
        choices = parsed.get("choices", [])
        ch0 = choices[0] if choices else {}
        
        # 复制 delta，移除 content 和 reasoning_content
        delta = ch0.get("delta", {})
        copy_delta = {k: v for k, v in delta.items() if k not in ("content", "reasoning_content")}
        
        return {
            **parsed,
            "choices": [
                {
                    **ch0,
                    "delta": {**copy_delta, **patch_delta}
                }
            ]
        }
    
    def emit_reasoning(self, parsed: Dict[str, Any], text: str) -> Optional[str]:
        """生成 reasoning_content 输出（单条 SSE 事件，带空行分隔）"""
        if not text:
            return None
        out = self.build_patched_data(parsed, {"reasoning_content": text})
        return f"data: {json.dumps(out)}\n\n"
    
    def emit_content(self, parsed: Dict[str, Any], text: str) -> Optional[str]:
        """生成 content 输出（单条 SSE 事件，带空行分隔）"""
        if not text:
            return None
        out = self.build_patched_data(parsed, {"content": text})
        return f"data: {json.dumps(out)}\n\n"
    
    def handle_text_chunk(self, parsed: Dict[str, Any], text: str) -> list:
        """
        处理文本块
        
        Returns:
            输出行列表
        """
        outputs = []
        combined = self.pending + text
        self.pending = ""
        
        if self.inside_thinking:
            # 查找 </thinking> 标签
            idx = combined.lower().find(self.close_tag_lower)
            if idx != -1:
                # 找到结束标签
                before = combined[:idx]
                after = combined[idx + len(self.close_tag_lower):]
                
                if before:
                    out = self.emit_reasoning(parsed, before)
                    if out:
                        outputs.append(out)
                
                self.inside_thinking = False
                
                if after:
                    out = self.emit_content(parsed, after)
                    if out:
                        outputs.append(out)
            else:
                # 未找到结束标签，保留尾部以防标签被截断
                if len(combined) > self.keep_tail:
                    emit = combined[:-self.keep_tail]
                    tail = combined[-self.keep_tail:]
                    if emit:
                        out = self.emit_reasoning(parsed, emit)
                        if out:
                            outputs.append(out)
                    self.pending = tail
                else:
                    self.pending = combined
        else:
            # 已经在 thinking 之外
            if combined:
                out = self.emit_content(parsed, combined)
                if out:
                    outputs.append(out)
        
        return outputs

    def _sanitize_and_forward_tool_calls(self, parsed: Dict[str, Any]) -> Optional[list]:
        """将 function_call 统一转为 tool_calls，如存在工具调用则单独透传，后续仍继续处理文本。"""
        choices = parsed.get("choices", [])
        if not choices:
            return None
        
        ch0 = choices[0] or {}
        delta = ch0.get("delta")
        if not isinstance(delta, dict):
            return None
        
        # 统一将 function_call 转为 tool_calls，便于下游统一处理
        if "function_call" in delta and "tool_calls" not in delta:
            fn_call = delta.get("function_call") or {}
            tc = [{
                "index": 0,
                "id": fn_call.get("id"),
                "type": "function",
                "function": {
                    "name": fn_call.get("name", ""),
                    "arguments": fn_call.get("arguments", ""),
                }
            }]
            delta.pop("function_call", None)
            delta["tool_calls"] = tc
        
        # 如有 tool_calls，先透传工具事件（单条 SSE），再移除以免干扰思维链/正文处理
        if "tool_calls" in delta:
            tool_calls = delta.get("tool_calls")
            out = self.build_patched_data(parsed, {"tool_calls": tool_calls})
            delta.pop("tool_calls", None)
            return [f"data: {json.dumps(out)}\n\n"]
        
        return None
    
    async def transform_line(self, line: str) -> list:
        """
        转换单行 SSE 数据
        
        Args:
            line: SSE 行
            
        Returns:
            转换后的输出行列表
        """
        trimmed = line.strip()
        
        # 过滤空行和keepalive消息（必须在最前面，避免后续JSON解析失败）
        if not trimmed or trimmed.startswith(":"):
            return [line + "\n"]
        
        # 处理 [DONE] 标记
        if trimmed == "data: [DONE]":
            outputs = []
            if self.pending:
                dummy_parsed = {"choices": [{"delta": {}}]}
                if self.inside_thinking:
                    out = self.emit_reasoning(dummy_parsed, self.pending)
                else:
                    out = self.emit_content(dummy_parsed, self.pending)
                if out:
                    outputs.append(out)
                self.pending = ""
            outputs.append(line + "\n")
            return outputs
        
        # 支持既有 SSE 前缀，也支持纯 JSON 行（上游可能已经去掉 data:）
        # 先尝试提取 payload，再解析 JSON；解析失败则透传原始行
        if line.startswith("data: "):
            json_str = line[6:]
        elif line.startswith("data:"):
            json_str = line[5:]
        else:
            json_str = line
        
        json_str = json_str.strip()
        # 某些上游会在行尾追加逗号，便于 JSON.parse 批量处理，这里兼容去尾逗号
        if json_str.endswith(","):
            json_str = json_str[:-1].rstrip()
        if not json_str:
            return [line + "\n"]
        try:
            parsed = await asyncio.to_thread(json.loads, json_str)
        except json.JSONDecodeError:
            return [line + "\n"]

        outputs = []
        # 先处理工具调用：如存在则单独透传，再继续处理思维链/正文
        tool_call_out = self._sanitize_and_forward_tool_calls(parsed)
        if tool_call_out:
            outputs.extend(tool_call_out)
        
        # 获取 delta
        choices = parsed.get("choices", [])
        if not choices:
            return outputs if outputs else [line + "\n"]
        
        delta = choices[0].get("delta", {})
        if not isinstance(delta, dict):
            return outputs if outputs else [line + "\n"]
        
        # 检查是否有 usage 信息需要透传（重要：用于 token 计费）
        usage = parsed.get("usage")
        if usage:
            if outputs:
                outputs.append(line + "\n")
                return outputs
            return [line + "\n"]
        
        # 仅当存在文本字段时才进行思维链处理，保持与反代逻辑一致
        rc = delta.get("reasoning_content")
        ct = delta.get("content")
        has_rc = isinstance(rc, str)
        has_ct = isinstance(ct, str)
        
        if has_rc:
            outputs.extend(self.handle_text_chunk(parsed, rc))
        if has_ct:
            outputs.extend(self.handle_text_chunk(parsed, ct))
        
        return outputs or [line + "\n"]
    
    def flush(self) -> list:
        """刷新剩余的 pending 内容"""
        outputs = []
        if self.pending:
            dummy_parsed = {"choices": [{"delta": {}}]}
            if self.inside_thinking:
                out = self.emit_reasoning(dummy_parsed, self.pending)
            else:
                out = self.emit_content(dummy_parsed, self.pending)
            if out:
                outputs.append(out)
            self.pending = ""
        return outputs


# ==================== 请求拦截器 ====================

async def claude_thinking_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Claude Thinking 请求拦截器
    
    处理 -thinking 后缀的模型请求
    """
    model = payload.get("model", "")
    
    if not is_thinking_claude_model(model):
        return url, headers, payload
    
    logger.info(f"[claude_thinking] Processing thinking model: {model}")
    
    # 去掉 -thinking 后缀
    original_model = model
    payload["model"] = model.replace("-thinking", "")
    
    # 添加 <thinking> 预填充
    add_prefill_thinking_message(payload)
    
    # 调整 token 预算
    adjust_reasoning_and_completion_tokens(payload)
    
    # 使用 request_info 存储状态（代替 contextvars，避免跨异步边界丢失）
    try:
        current_info = request_info.get()
        current_info["_claude_thinking_enabled"] = True
        current_info["_claude_thinking_transformer"] = ThinkingStreamTransformer()
    except Exception as e:
        logger.error(f"[claude_thinking] Failed to set request_info: {e}")
    
    return url, headers, payload


# ==================== 响应拦截器 ====================

async def claude_thinking_response_interceptor(
    response_chunk: Any,
    engine: str,
    model: str,
    is_stream: bool,
) -> Any:
    """
    Claude Thinking 响应拦截器
    
    转换 SSE 流中的 </thinking> 前后的内容
    """
    # 从 request_info 读取状态
    try:
        current_info = request_info.get()
        thinking_enabled = current_info.get("_claude_thinking_enabled", False)
        transformer = current_info.get("_claude_thinking_transformer")
    except Exception as e:
        logger.error(f"[claude_thinking_response] Failed to get request_info: {e}")
        return response_chunk
    
    # 快速退出：检查是否为 thinking 模式
    if not thinking_enabled or not transformer or not is_stream:
        return response_chunk
    
    if not isinstance(response_chunk, str):
        return response_chunk
    
    # 处理响应块（异步处理每一行）
    # 特殊处理：如果响应只是空行、keepalive或不包含data:，直接返回
    stripped = response_chunk.strip()
    if not stripped or stripped.startswith(":"):
        return response_chunk
    
    try:
        lines = response_chunk.split("\n")
        output_lines = []
        
        for line in lines:
            # 保留空行，它们是SSE协议的一部分
            if not line:
                output_lines.append("\n")
            else:
                transformed = await transformer.transform_line(line)
                output_lines.extend(transformed)
        
        result = "".join(output_lines)
            
    except Exception as e:
        # 如果处理过程中出现任何错误，返回原始响应
        logger.error(f"[claude_thinking_response] Error processing response chunk: {e}", exc_info=True)
        return response_chunk
    
    # 如果收到 [DONE]，刷新状态
    if "data: [DONE]" in response_chunk:
        try:
            flush_outputs = transformer.flush()
            if flush_outputs:
                # 在 [DONE] 前插入刷新的内容
                done_idx = result.find("data: [DONE]")
                if done_idx != -1:
                    result = result[:done_idx] + "".join(flush_outputs) + result[done_idx:]
        except Exception as e:
            logger.error(f"[claude_thinking] Error flushing transformer: {e}")
    
    return result


# ==================== 插件生命周期 ====================

def setup(manager):
    """
    插件初始化
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")
    
    # 注册请求拦截器
    register_request_interceptor(
        interceptor_id="claude_thinking_request",
        callback=claude_thinking_request_interceptor,
        priority=50,  # 较高优先级，在其他拦截器之前处理
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "Claude Thinking 请求处理"},
    )
    
    # 注册响应拦截器
    register_response_interceptor(
        interceptor_id="claude_thinking_response",
        callback=claude_thinking_response_interceptor,
        priority=50,
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "Claude Thinking 响应流转换"},
    )
    
    logger.info(f"[{PLUGIN_INFO['name']}] 已注册请求和响应拦截器")


def teardown(manager):
    """
    插件清理
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")
    
    # 注销拦截器
    unregister_request_interceptor("claude_thinking_request")
    unregister_response_interceptor("claude_thinking_response")
    
    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")


def unload():
    """
    插件卸载回调
    """
    logger.debug(f"[{PLUGIN_INFO['name']}] 模块即将卸载")