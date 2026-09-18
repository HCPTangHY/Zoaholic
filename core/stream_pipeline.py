"""流式响应和首包错误处理工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
import asyncio
import json
import time as time_module
from typing import Optional

import h2.exceptions
import httpx
from fastapi import HTTPException

from core.json_utils import json_dumps_text, json_loads
from core.log_config import logger
from core.utils import safe_get
from core.stream_utils import close_async_iterator, OwnedAsyncIterator
from core.stream_errors import guard_stream, extract_stream_error, UpstreamStreamError


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

    try:
        # asyncio.wait's timeout doesn't cancel the read and needs no separate sleep task.
        done, _ = await asyncio.wait({first_response_task}, timeout=timeout)
        if not done:
            return first_response_task, "timeout"
        try:
            return first_response_task.result(), "success"
        except RuntimeError as e:
            if "asynchronous generator is already running" in str(e):
                return None, "reentrant"
            raise
    except BaseException:
        # The caller has not yet received ownership of a newly created read task.
        await close_async_iterator(wait_for_thing, first_response_task)
        raise


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
    try:
        if emit_initial:
            yield SSE_KEEPALIVE_COMMENT
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
        await close_async_iterator(generator, wait_task)


async def prepare_stream(generator, *, current_info=None, keepalive_interval=None, error_triggers=(), request_url=None, app=None, engine=None):
    """Inspect before returning to handler; only then allow downstream headers/keepalives."""
    from core.response_context import get_current_request_info

    info = current_info if current_info is not None else (get_current_request_info() or {})
    if not isinstance(info, dict):
        info = {}
    # 每次尝试都是新的首段：清掉可能残留的上一次提交标记，避免共享上下文污染。
    for key in ("_stream_committed", "_stream_error", "_stream_error_handled", "_stream_has_output", "_stream_raw_inspector"):
        info.pop(key, None)
    start = time_module.monotonic()
    # 优先使用渠道注册表声明的事件分类器，未声明时由 guard_stream 内部走通用结构判断。
    classifier = None
    if engine:
        try:
            from core.channels import get_channel
            channel = get_channel(engine)
            classifier = getattr(channel, "stream_event_classifier", None)
        except Exception:
            classifier = None
    guarded = OwnedAsyncIterator(guard_stream(generator, info=info, classifier=classifier, error_triggers=error_triggers), generator)
    pending = None
    first = None
    try:
        # 首包等待沿用旧语义：有 keepalive 配置时等满一个心跳间隔，未配置时无限等待。
        # 上游错误响应头几乎总在心跳间隔内到达，能在此窗口内抛回 handler 换取正确的 HTTP 状态码。
        # 修复：旧实现用 min(3s, keepalive) 且超时后立即标记已提交，导致慢到的 400 退化为 200+流内错误帧。
        if keepalive_interval:
            first, status = await wait_for_timeout(guarded, timeout=keepalive_interval)
            if status == "timeout":
                pending, first = first, None
            elif status == "reentrant":
                raise RuntimeError("stream read reentrant before response")
        else:
            first = await guarded.__anext__()
    except BaseException:
        await close_async_iterator(guarded, pending)
        raise

    async def replay():
        iterator = guarded
        try:
            if first is not None:
                yield first if isinstance(first, (str, bytes)) else await ensure_string(first)
            if keepalive_interval:
                iterator = iter_sse_with_keepalive(
                    guarded, interval=keepalive_interval,
                    wait_task=pending, emit_initial=(pending is not None),
                )
                async for item in iterator:
                    yield item if isinstance(item, (str, bytes)) else await ensure_string(item)
            else:
                # 未配置 keepalive 的渠道保持原有纯转发，不注入注释帧。
                async for item in guarded:
                    yield item if isinstance(item, (str, bytes)) else await ensure_string(item)
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout,
                httpx.WriteError, httpx.ProtocolError, h2.exceptions.ProtocolError) as exc:
            # 保留原有 HTTP/2 StreamReset 自动重建连接行为。
            if request_url and app and ("StreamReset" in str(exc) or "stream_id" in str(exc)):
                from urllib.parse import urlparse
                host = urlparse(request_url).netloc
                if host and hasattr(app, "state") and hasattr(app.state, "client_manager"):
                    asyncio.create_task(app.state.client_manager.reset_client(host))
            raise
        finally:
            try:
                await close_async_iterator(iterator)
            finally:
                await close_async_iterator(guarded, pending)

    return OwnedAsyncIterator(replay(), guarded, pending), time_module.monotonic() - start


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
    current_info=None,
):
    if stream:
        return await prepare_stream(generator, current_info=current_info, keepalive_interval=keepalive_interval,
                                    error_triggers=error_triggers,
                                    request_url=request_url, app=app, engine=engine)

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
        iterator = generator
        try:
            if first_item is not None:
                yield await ensure_string(first_item, as_sse=stream)
                first_item = None
            if with_keepalive:
                async def transform(item):
                    return await ensure_string(item, as_sse=stream)

                iterator = iter_sse_with_keepalive(
                    generator, timeout, wait_task=wait_task,
                    emit_initial=(wait_task is not None), transform=transform,
                )
                async for chunk in iterator:
                    yield chunk
            else:
                async for item in generator:
                    yield await ensure_string(item, as_sse=stream)
            _log_stream_end("upstream_eof")
        except asyncio.CancelledError:
            _log_stream_end("client_cancelled", level="debug")
            raise
        except UpstreamStreamError:
            # 必须回到 handler 的路由重试循环，不能在流式响应中吞掉该异常。
            _log_stream_end("upstream_stream_error", level="warning")
            raise
        except (
            httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout,
            httpx.WriteError, httpx.ProtocolError, h2.exceptions.ProtocolError,
        ) as exc:
            logger.error(f"provider: {channel_id:<11} Network error in stream: {exc}")
            if request_url and app and ("StreamReset" in str(exc) or "stream_id" in str(exc)):
                from urllib.parse import urlparse
                host = urlparse(request_url).netloc
                if host and hasattr(app, "state") and hasattr(app.state, "client_manager"):
                    asyncio.create_task(app.state.client_manager.reset_client(host))
            if with_keepalive:
                yield await ensure_string({"error": {
                    "message": f"Upstream network error: {type(exc).__name__}",
                    "type": "upstream_network_error", "param": None,
                    "code": "upstream_network_error",
                }})
            done = "data: [DONE]\n\n" if done_message is None else done_message
            if done:
                yield done
            _log_stream_end("upstream_network_error", level="warning", detail=type(exc).__name__)
        except Exception as exc:
            if not with_keepalive:
                raise
            logger.error(f"provider: {channel_id:<11} Error in keepalive loop: {exc}")
            done = "data: [DONE]\n\n" if done_message is None else done_message
            if done:
                yield done
            _log_stream_end("wrapper_exception", level="error", detail=type(exc).__name__)
        finally:
            try:
                await close_async_iterator(iterator, wait_task)
            finally:
                if iterator is not generator:
                    await close_async_iterator(generator)

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
    first_item_str = None
    try:
        # 创建一个任务来获取第一个响应，但不直接中断生成器
        if keepalive_interval and stream:
            first_item, status = await wait_for_timeout(generator, timeout=keepalive_interval)
            if status == "timeout":
                return OwnedAsyncIterator(
                    new_generator(None, with_keepalive=True, wait_task=first_item, timeout=keepalive_interval),
                    generator, first_item,
                ), 3.1415
        else:
            first_item = await generator.__anext__()

        first_response_time = time_module.time() - start_time
        # 非流式响应沿用首包校验。
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

            first_error = extract_stream_error(first_item)
            if first_error:
                raise UpstreamStreamError(first_error)
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

        return OwnedAsyncIterator(new_generator(
            first_item,
            with_keepalive=bool(keepalive_interval and stream),
            timeout=keepalive_interval or 3,
        ), generator), first_response_time

    except StopAsyncIteration:
        await close_async_iterator(generator)
        # 502 Bad Gateway 是一个更合适的状态码，因为它表明作为代理或网关的服务器从上游服务器收到了无效的响应。
        logger.warning(f"provider: {channel_id:<11} empty response [{type(first_item_str)}]: {first_item_str}")
        raise HTTPException(status_code=502, detail="Upstream server returned an empty response.")

    except BaseException:
        await close_async_iterator(generator)
        raise
