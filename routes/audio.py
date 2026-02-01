"""
Audio 路由
"""

import io
from typing import Optional

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Request, UploadFile, File, Form

from core.env import env_bool

from core.models import TextToSpeechRequest, AudioTranscriptionRequest
from routes.deps import rate_limit_dependency, verify_api_key, get_model_handler

router = APIRouter()


@router.post("/v1/audio/speech", dependencies=[Depends(rate_limit_dependency)])
async def audio_speech(
    request: TextToSpeechRequest,
    background_tasks: BackgroundTasks,
    api_index: str = Depends(verify_api_key)
):
    """
    文本转语音
    
    兼容 OpenAI TTS API 格式
    """
    model_handler = get_model_handler()
    return await model_handler.request_model(
        request, api_index, background_tasks, endpoint="/v1/audio/speech"
    )


@router.post("/v1/audio/transcriptions", dependencies=[Depends(rate_limit_dependency)])
async def audio_transcriptions(
    http_request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    model: str = Form(...),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
    temperature: Optional[float] = Form(None),
    api_index: int = Depends(verify_api_key)
):
    """
    音频转文字
    
    兼容 OpenAI Whisper API 格式
    """
    is_debug = env_bool("DEBUG", False)
    
    try:
        # 手动解析表单数据
        form_data = await http_request.form()
        # 使用 getlist 处理同一键的多个值
        timestamp_granularities = form_data.getlist("timestamp_granularities[]")
        if not timestamp_granularities:
            timestamp_granularities = None

        # 读取上传的文件内容
        content = await file.read()
        file_obj = io.BytesIO(content)

        # 创建 AudioTranscriptionRequest 对象
        request_obj = AudioTranscriptionRequest(
            file=(file.filename, file_obj, file.content_type),
            model=model,
            language=language,
            prompt=prompt,
            response_format=response_format,
            temperature=temperature,
            timestamp_granularities=timestamp_granularities
        )

        model_handler = get_model_handler()
        return await model_handler.request_model(
            request_obj, api_index, background_tasks, endpoint="/v1/audio/transcriptions"
        )
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid audio file encoding")
    except Exception as e:
        if is_debug:
            import traceback
            traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error processing audio file: {str(e)}")