"""
FastApi endpoint translating Danswer API to OpenAI Chat API to fit ChatWoot.
And vice-versa.
"""
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from pydantic import ValidationError

from danswer.utils.logger import setup_logger
from onyx.onyxbot.openai_compliant_adapter.pipeline import get_danswer_response
from onyx.onyxbot.openai_compliant_adapter.translator import extract_last_user_query
from onyx.onyxbot.openai_compliant_adapter.translator import Message
from onyx.onyxbot.openai_compliant_adapter.translator import translate_danswer_to_openai

logger = setup_logger(name="opeai_compliant_adapter")
app = FastAPI()


@app.post("/translate")
async def translate(request: Request):
    try:
        openai_request = await request.json()
        user_query, prompt_token_count = extract_last_user_query(openai_request)

        danswer_response = get_danswer_response(user_query)
        try:
            danswer_message = Message(**danswer_response)
        except ValidationError as e:
            logger.error(e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        openai_response = translate_danswer_to_openai(
            danswer_message, prormpt_token_count=prompt_token_count
        )

        return openai_response

    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
