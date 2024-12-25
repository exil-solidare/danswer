"""
Represent onyx pipeline Question to Answer generation.
"""
import json

from sqlalchemy.orm import Session

from onyx.chat.process_message import stream_chat_message
from onyx.context.search.models import OptionalSearchSetting
from onyx.context.search.models import RetrievalDetails
from onyx.db.chat import create_chat_session
from onyx.db.chat import get_or_create_root_message
from onyx.db.engine import get_sqlalchemy_engine
from onyx.server.query_and_chat.models import CreateChatMessageRequest

# from onyx.llm.utils import get_default_llm_tokenizer


# print(build_connection_string(db_api=SYNC_DB_API))
def get_onyx_response(message: str):
    """Get onyx response to the question message."""
    with Session(get_sqlalchemy_engine()) as db_session:
        description = "Openai compliant adapter"
        persona_id = 0
        prompt_id = 0
        llm_override = None
        prompt_override = None

        new_session = create_chat_session(
            db_session=db_session,
            description=description,
            user_id=None,
            persona_id=persona_id,
            llm_override=llm_override,
            prompt_override=prompt_override,
        )

        root_message = get_or_create_root_message(
            chat_session_id=new_session.id, db_session=db_session
        )

        parent_id = root_message.id
        options = RetrievalDetails(run_search=OptionalSearchSetting.AUTO)

        req = CreateChatMessageRequest(
            chat_session_id=new_session.id,
            parent_message_id=parent_id,
            message=message,
            file_descriptors=[],
            prompt_id=prompt_id,
            search_doc_ids=None,
            retrieval_options=options,
            query_override=None,
        )

        result = "\n".join(stream_chat_message(new_msg_req=req, user=None)).split("\n")

        res_obj = json.loads(result[-2])
        # print(res_obj["message"])
        # search_doc_ids = res_obj["context_docs"]["top_documents"]
        # print("context_docs", search_doc_ids)
        # print("citations", res_obj["citations"]
        return res_obj
