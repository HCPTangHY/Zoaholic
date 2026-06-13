"""流式响应和首包错误处理工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
import asyncio
import time as time_module
from typing import Optional

import h2.exceptions
import httpx
from fastapi import HTTPException

from core.json_utils import json_dumps_text, json_loads
from core.log_config import logger
from core.utils import safe_get


async def ensure_string(item, as_sse: bool = True):
    if isinstance(item, (bytes, bytearray)):
        return item.decode("utf-8")
    elif isinstance(item, str):
        return item
    elif isinstance(item, dict):
        # 大 dict（如含 base64 图片的响应）同步序列化会阻塞事件循环，
        # 放到线程池执行，避免高并发生图时 event loop block
        json_str = await asyncio.to_thread(json_dumps_text, item)
        if as_sse:
            return f"data: {json_str}\n\n"
        return json_str
    else:
        return str(item)


def identify_audio_format(file_bytes):
    # 读取开头的字节
    if file_bytes.startswith(b'\xFF\xFB') or file_bytes.startswith(b'\xFF\xF3'):
        return "MP3"
    elif file_bytes.startswith(b'ID3'):
        return "MP3 with ID3"
    elif file_bytes.startswith(b'OpusHead'):
        return "OPUS"
    elif file_bytes.startswith(b'ADIF'):
        return "AAC (ADIF)"
    elif file_bytes.startswith(b'\xFF\xF1') or file_bytes.startswith(b'\xFF\xF9'):
        return "AAC (ADTS)"
    elif file_bytes.startswith(b'fLaC'):
        return "FLAC"
    elif file_bytes.startswith(b'RIFF') and file_bytes[8:12] == b'WAVE':
        return "WAV"
    return "Unknown/PCM"


async def wait_for_timeout(wait_for_thing, timeout = 3, wait_task=None):
    # 创建一个任务来获取第一个响应，但不直接中断生成器
    if wait_task is None:
        try:
            first_response_task = asyncio.create_task(wait_for_thing.__anext__())
        except RuntimeError as e:
            # 保护：避免并发 anext 直接抛异常打断 keepalive 主循环
            if "asynchronous generator is already running" in str(e):
                return None, "reentrant"
            raise
        # 防止 "Task exception was never retrieved"：即使后续调用方中途退出，异常也会被消费
        def _silence_task_exception(task: asyncio.Task):
            try:
                _ = task.exception()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        first_response_task.add_done_callback(_silence_task_exception)
    else:
        first_response_task = wait_task

    # 创建一个超时任务
    timeout_task = asyncio.create_task(asyncio.sleep(timeout))

    # 等待任意一个任务完成
    done, pending = await asyncio.wait(
        [first_response_task, timeout_task],
        return_when=asyncio.FIRST_COMPLETED
    )

    # 成功返回
    if first_response_task in done:
        # 取消超时任务
        timeout_task.cancel()
        try:
            return first_response_task.result(), "success"
        except RuntimeError as e:
            if "asynchronous generator is already running" in str(e):
                return None, "reentrant"
            raise

    # 超时返回
    else:
        return first_response_task, "timeout"


SSE_KEEPALIVE_COMMENT = ": keepalive\n\n"


async def iter_sse_with_keepalive(
    generator,
    interval,
    *,
    wait_task=None,
    emit_initial=False,
    transform=None,
):
    """统一的 SSE keepalive 注入循环，供普通流式与透传流式共用。

    修改原因：error_handling_wrapper 与 core/passthrough 此前各自复制了一份几乎相同的
      keepalive pump（wait_for_timeout -> 超时发注释帧 -> 命中则产出 item），keepalive
      帧样式与重入/取消清理逻辑分散，容易改一处漏一处。
    修改方式：抽出唯一的 pump 实现，把「是否对 item 做协议转换」通过 transform 注入；
      其余 keepalive 固有逻辑（单飞 __anext__、超时/重入退避、上游 EOF 收尾、finally
      清理挂起 wait_task）集中于此。
    目的：两条路径复用同一套 keepalive 帧与保活语义，并顺带修复生成器被关闭时挂起的
      wait_task 泄漏。

    职责边界：只处理 keepalive 循环本身；网络错误、done_message、reset_client、
      stream_end 日志等业务语义不在此处理，相关异常会原样向调用方传播，由各路径自行收尾。

    参数：
    - generator: 上游异步生成器。
    - interval: 心跳间隔秒数（即 wait_for_timeout 的 timeout）。
    - wait_task: 首包阶段已创建、尚未完成的 __anext__ 任务，复用以避免并发拉取上游。
    - emit_initial: 进入循环前是否立即补发一帧（首包尚未到达时为 True）。
    - transform: 可选 async 转换器，仅作用于真实 item（不作用于注释帧）；普通流式传入
      ensure_string 包装，透传流式不传以保持「不解析/不改写协议内容」。
    """
    if emit_initial:
        yield SSE_KEEPALIVE_COMMENT
    try:
        while True:
            try:
                item, status = await wait_for_timeout(generator, timeout=interval, wait_task=wait_task)
            except StopAsyncIteration:
                # 上游 EOF：正常结束循环，交由 finally 统一清理
                return
            except RuntimeError as e:
                # 极端时序仍可能抛重入错误：退避后补一帧，不打断主循环
                if "asynchronous generator is already running" in str(e):
                    wait_task = None
                    await asyncio.sleep(0.2)
                    yield SSE_KEEPALIVE_COMMENT
                    continue
                raise
            if status == "timeout":
                # 复用仍在运行的 __anext__ 任务，避免并发创建导致重入
                wait_task = item
                yield SSE_KEEPALIVE_COMMENT
                continue
            if status == "reentrant":
                # 重入：按心跳周期退避，避免刷屏
                wait_task = None
                await asyncio.sleep(interval)
                yield SSE_KEEPALIVE_COMMENT
                continue
            wait_task = None
            yield (await transform(item)) if transform is not None else item
    finally:
        # 无论因 EOF、异常还是被消费者关闭（GeneratorExit）退出，都取消仍挂起的
        # 单飞 __anext__ 任务，避免连接资源泄漏。
        if wait_task is not None and not wait_task.done():
            wait_task.cancel()


async def error_handling_wrapper(
    generator,
    channel_id,
    engine,
    stream,
    error_triggers,
    keepalive_interval=None,
    last_message_role=None,
    done_message: Optional[str] = None,
    *,
    request_url: Optional[str] = None,
    app: Optional[object] = None,
):

    def _log_stream_end(reason: str, *, level: str = "info", detail: Optional[str] = None):
        msg = f"provider: {channel_id:<11} stream_end reason={reason}"
        if detail:
            msg += f" detail={detail}"
        if level == "debug":
            logger.debug(msg)
        elif level == "warning":
            logger.warning(msg)
        elif level == "error":
            logger.error(msg)
        else:
            logger.info(msg)

    async def new_generator(first_item=None, with_keepalive=False, wait_task=None, timeout=3):
        stream_end_logged = False

        if first_item:
            yield await ensure_string(first_item, as_sse=stream)

        # 如果需要心跳机制但不使用嵌套生成器方式
        if with_keepalive:
            # 修改原因：此前 keepalive pump 在本函数与 core/passthrough 各复制了一份，
            #   keepalive 帧样式、重入退避和挂起任务清理容易改一处漏一处。
            # 修改方式：统一改调 iter_sse_with_keepalive，本分支只保留普通流式特有的业务收尾
            #   （网络错误发 done、reset_client、stream_end 日志）；用 ensure_string 作为 transform
            #   注入协议转换，首包尚未到达时通过 emit_initial 补发首帧。挂起的 __anext__ 任务由
            #   iter_sse_with_keepalive 的 finally 统一清理，本分支不再各自 cancel。
            # 目的：与透传路径共用同一套 keepalive 帧与保活语义，消除重复实现。
            async def _keepalive_transform(item):
                return await ensure_string(item, as_sse=stream)

            try:
                async for chunk in iter_sse_with_keepalive(
                    generator,
                    interval=timeout,
                    wait_task=wait_task,
                    emit_initial=(first_item is None),
                    transform=_keepalive_transform,
                ):
                    yield chunk
                _log_stream_end("upstream_eof")
                stream_end_logged = True
            except asyncio.CancelledError:
                logger.debug(f"provider: {channel_id:<11} Stream cancelled by client in main loop")
                _log_stream_end("client_cancelled", level="debug")
                stream_end_logged = True
            except (
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.ReadTimeout,
                httpx.WriteError,
                httpx.ProtocolError,
                h2.exceptions.ProtocolError,
            ) as e:
                logger.error(f"provider: {channel_id:<11} Network error in keepalive loop: {e}")

                try:
                    err_str = str(e)
                    if request_url and app and ("StreamReset" in err_str or "stream_id" in err_str):
                        from urllib.parse import urlparse
                        host = urlparse(request_url).netloc
                        if host and hasattr(app, "state") and hasattr(app.state, "client_manager"):
                            asyncio.create_task(app.state.client_manager.reset_client(host))
                except Exception:
                    pass

                # 发送 OAI 格式 error event，让客户端知道流异常结束
                try:
                    import json as _json
                    err_payload = _json.dumps({
                        "error": {
                            "message": f"Upstream network error: {type(e).__name__}",
                            "type": "upstream_network_error",
                            "param": None,
                            "code": "upstream_network_error",
                        }
                    })
                    yield f"data: {err_payload}\n\n"
                except Exception:
                    pass

                done = "data: [DONE]\n\n" if done_message is None else done_message
                if done:
                    yield done
                _log_stream_end("upstream_network_error", level="warning", detail=type(e).__name__)
                stream_end_logged = True
            except Exception as e:
                logger.error(f"provider: {channel_id:<11} Error in keepalive loop: {e}")
                done = "data: [DONE]\n\n" if done_message is None else done_message
                if done:
                    yield done
                _log_stream_end("wrapper_exception", level="error", detail=type(e).__name__)
                stream_end_logged = True
        else:
            # 原始逻辑：不需要心跳
            try:
                async for item in generator:
                    yield await ensure_string(item, as_sse=stream)
                _log_stream_end("upstream_eof")
                stream_end_logged = True
            except asyncio.CancelledError:
                logger.debug(f"provider: {channel_id:<11} Stream cancelled by client")
                _log_stream_end("client_cancelled", level="debug")
                stream_end_logged = True
                return
            except (
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.ReadTimeout,
                httpx.WriteError,
                httpx.ProtocolError,
                h2.exceptions.ProtocolError,
            ) as e:
                logger.error(f"provider: {channel_id:<11} Network error in new_generator: {e}")

                try:
                    err_str = str(e)
                    if request_url and app and ("StreamReset" in err_str or "stream_id" in err_str):
                        from urllib.parse import urlparse
                        host = urlparse(request_url).netloc
                        if host and hasattr(app, "state") and hasattr(app.state, "client_manager"):
                            asyncio.create_task(app.state.client_manager.reset_client(host))
                except Exception:
                    pass

                done = "data: [DONE]\n\n" if done_message is None else done_message
                if done:
                    yield done
                _log_stream_end("upstream_network_error", level="warning", detail=type(e).__name__)
                stream_end_logged = True
                return
            finally:
                if not stream_end_logged:
                    _log_stream_end("unknown")

    def _extract_first_json_candidate(text: str) -> Optional[str]:
        """
        从首个 chunk 中提取可用于 json.loads 的字符串。

        兼容：
        - OpenAI/Gemini SSE: "data: {...}"
        - Claude SSE: "event: ...\ndata: {...}"
        - 非 SSE: "{...}" / "[...]"
        """
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        if not stripped:
            return None

        for raw_line in stripped.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                continue
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    return payload
                continue
            if line.startswith("{") or line.startswith("["):
                return line

        if stripped.startswith("data:"):
            payload = stripped[len("data:") :].strip()
            return payload or None
        if stripped.startswith("{") or stripped.startswith("["):
            return stripped
        return None

    start_time = time_module.time()
    try:
        # 创建一个任务来获取第一个响应，但不直接中断生成器
        if keepalive_interval and stream:
            first_item, status = await wait_for_timeout(generator, timeout=keepalive_interval)
            if status == "timeout":
                return new_generator(None, with_keepalive=True, wait_task=first_item, timeout=keepalive_interval), 3.1415
        else:
            first_item = await generator.__anext__()

        first_response_time = time_module.time() - start_time
        # 对第一个响应项进行原有的处理逻辑
        first_item_str = first_item
        # logger.info("first_item_str: %s :%s", type(first_item_str), first_item_str)
        if isinstance(first_item_str, (bytes, bytearray)):
            if identify_audio_format(first_item_str) in ["MP3", "MP3 with ID3", "OPUS", "AAC (ADIF)", "AAC (ADTS)", "FLAC", "WAV"]:
                return first_item, first_response_time
            else:
                first_item_str = first_item_str.decode("utf-8")
        
        # 跳过空行和keepalive消息，获取真正的第一个有效响应
        while isinstance(first_item_str, str) and (not first_item_str.strip() or first_item_str.startswith(": keepalive")):
            first_item = await generator.__anext__()
            first_item_str = first_item
            if isinstance(first_item_str, (bytes, bytearray)):
                first_item_str = first_item_str.decode("utf-8")
        
        if isinstance(first_item_str, str) and not first_item_str.startswith(": keepalive"):
            json_candidate = _extract_first_json_candidate(first_item_str)
            parse_target = (json_candidate if json_candidate is not None else first_item_str).strip()

            if parse_target.startswith("[DONE]"):
                logger.error(f"provider: {channel_id:<11} error_handling_wrapper [DONE]!")
                raise StopAsyncIteration
            try:
                encode_first_item_str = parse_target.encode().decode("unicode-escape")
            except UnicodeDecodeError:
                encode_first_item_str = parse_target
                logger.error(f"provider: {channel_id:<11} error UnicodeDecodeError: %s", parse_target)

            if any(x in encode_first_item_str for x in error_triggers):
                logger.error(f"provider: {channel_id:<11} error const string: %s", encode_first_item_str)
                raise StopAsyncIteration

            # 仅当能提取到 JSON candidate 时才进行 json.loads，避免包含 event: 行的 SSE 首包导致误判
            if json_candidate is not None:
                try:
                    first_item_str = json_loads(json_candidate)
                except json.JSONDecodeError:
                    logger.error(
                        f"provider: {channel_id:<11} error_handling_wrapper JSONDecodeError! {repr(json_candidate)}"
                    )
                    raise StopAsyncIteration

            # minimax
            status_code = safe_get(first_item_str, 'base_resp', 'status_code', default=200)
            if status_code != 200:
                if status_code == 2013:
                    status_code = 400
                if status_code == 1008:
                    status_code = 429
                detail = safe_get(first_item_str, 'base_resp', 'status_msg', default="no error returned")
                raise HTTPException(status_code=status_code, detail=f"{detail}"[:1000])

        # minimax
        if isinstance(first_item_str, dict) and safe_get(first_item_str, "base_resp", "status_msg", default=None) == "success":
            full_audio_hex = safe_get(first_item_str, "data", "audio", default=None)
            if full_audio_hex:
                audio_bytes = bytes.fromhex(full_audio_hex)
                return audio_bytes, first_response_time

        if isinstance(first_item_str, dict) and 'error' in first_item_str and first_item_str.get('error') != {"message": "","type": "","param": "","code": None}:
            # 如果第一个 yield 的项是错误信息，抛出 HTTPException
            status_code = first_item_str.get('status_code')
            detail = first_item_str.get('details')

            error_obj = first_item_str.get('error')

            # 针对 check_response 返回的格式进行深度提取
            if isinstance(detail, dict) and 'error' in detail:
                inner_error = detail.get('error')
                if isinstance(inner_error, dict):
                    detail = inner_error.get('message') or detail
                elif isinstance(inner_error, str):
                    detail = inner_error

            # 针对标准的 OpenAI 错误格式 { "error": { "message": "...", "code": ... } }
            if not detail and isinstance(error_obj, dict):
                detail = error_obj.get('message')
                if not status_code:
                    status_code = error_obj.get('code')

            if not status_code:
                status_code = 400

            # 确保 status_code 是有效的 HTTP 状态码
            try:
                status_code = int(status_code)
                if status_code < 100 or status_code > 599:
                    status_code = 400
            except (TypeError, ValueError):
                status_code = 400

            # 生成可读 message（不向客户端透传 details）
            message = None
            details_payload = detail if detail is not None else first_item_str

            # 这里保持“通用”提取逻辑，不做渠道字段硬编码。
            if isinstance(details_payload, dict):
                message = (
                    safe_get(details_payload, "error", "message", default=None)
                    or safe_get(details_payload, "message", default=None)
                )

            if not message and isinstance(error_obj, dict):
                message = error_obj.get("message")

            if not message:
                message = str(detail) if detail is not None else str(first_item_str)

            raise HTTPException(status_code=status_code, detail=f"{message}"[:5000])

        if isinstance(first_item_str, dict) and safe_get(first_item_str, "choices", 0, "error", default=None):
            # 如果第一个 yield 的项是错误信息，抛出 HTTPException
            status_code = safe_get(first_item_str, "choices", 0, "error", "code", default=500)
            detail = safe_get(first_item_str, "choices", 0, "error", "message", default=f"{first_item_str}")
            raise HTTPException(status_code=status_code, detail=f"{detail}"[:1000])

        finish_reason = safe_get(first_item_str, "choices", 0, "finish_reason", default=None)
        if isinstance(first_item_str, dict) and finish_reason == "PROHIBITED_CONTENT":
            raise HTTPException(status_code=400, detail="PROHIBITED_CONTENT")

        if isinstance(first_item_str, dict) and finish_reason == "stop" and \
        not safe_get(first_item_str, "choices", 0, "message", "content", default=None) and \
        not safe_get(first_item_str, "choices", 0, "delta", "content", default=None) and \
        not safe_get(first_item_str, "choices", 0, "message", "reasoning_content", default=None) and \
        not safe_get(first_item_str, "choices", 0, "delta", "reasoning_content", default=None) and \
        last_message_role != "assistant":
            raise StopAsyncIteration

        if isinstance(first_item_str, dict) and engine not in ["tts", "embedding", "dalle", "moderation", "whisper"] and not stream and isinstance(first_item_str.get("choices"), list):
            if any(x in str(first_item_str) for x in error_triggers):
                logger.error(f"provider: {channel_id:<11} error const string: %s", first_item_str)
                raise StopAsyncIteration
            content = safe_get(first_item_str, "choices", 0, "message", "content", default=None)
            reasoning_content = safe_get(first_item_str, "choices", 0, "message", "reasoning_content", default=None)
            b64_json = safe_get(first_item_str, "data", 0, "b64_json", default=None)
            tool_calls = safe_get(first_item_str, "choices", 0, "message", "tool_calls", default=None)
            if (content == "" or content is None) and (tool_calls == "" or tool_calls is None) and (reasoning_content == "" or reasoning_content is None) and b64_json is None:
                raise StopAsyncIteration

        return new_generator(
            first_item,
            with_keepalive=bool(keepalive_interval and stream),
            timeout=keepalive_interval or 3,
        ), first_response_time

    except StopAsyncIteration:
        # 502 Bad Gateway 是一个更合适的状态码，因为它表明作为代理或网关的服务器从上游服务器收到了无效的响应。
        logger.warning(f"provider: {channel_id:<11} empty response [{type(first_item_str)}]: {first_item_str}")
        raise HTTPException(status_code=502, detail="Upstream server returned an empty response.")
