import json
from collections.abc import Generator
from typing import Any
from typing import cast

from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.chat.chat_utils import llm_doc_from_inference_section
from onyx.chat.llm_response_handler import LLMCall
from onyx.chat.models import AnswerStyleConfig
from onyx.chat.models import ContextualPruningConfig
from onyx.chat.models import DocumentPruningConfig
from onyx.chat.models import LlmDoc
from onyx.chat.models import OnyxContext
from onyx.chat.models import OnyxContexts
from onyx.chat.models import PromptConfig
from onyx.chat.models import SectionRelevancePiece
from onyx.chat.prompt_builder.build import AnswerPromptBuilder
from onyx.chat.prompt_builder.citations_prompt import compute_max_llm_input_tokens
from onyx.chat.prune_and_merge import prune_and_merge_sections
from onyx.chat.prune_and_merge import prune_sections
from onyx.configs.chat_configs import CONTEXT_CHUNKS_ABOVE
from onyx.configs.chat_configs import CONTEXT_CHUNKS_BELOW
from onyx.configs.model_configs import GEN_AI_MODEL_FALLBACK_MAX_TOKENS
from onyx.context.search.enums import LLMEvaluationType
from onyx.context.search.enums import QueryFlow
from onyx.context.search.enums import SearchType
from onyx.context.search.models import BaseFilters
from onyx.context.search.models import IndexFilters
from onyx.context.search.models import InferenceSection
from onyx.context.search.models import RerankingDetails
from onyx.context.search.models import RetrievalDetails
from onyx.context.search.models import SearchRequest
from onyx.context.search.models import Tag
from onyx.context.search.pipeline import SearchPipeline
from onyx.db.models import Persona
from onyx.db.models import User
from onyx.llm.factory import get_default_llms
from onyx.llm.interfaces import LLM
from onyx.llm.models import PreviousMessage
from onyx.secondary_llm_flows.choose_search import check_if_need_search
from onyx.secondary_llm_flows.query_expansion import history_based_query_rephrase
from onyx.tools.message import ToolCallSummary
from onyx.tools.models import ToolResponse
from onyx.tools.tool import Tool
from onyx.tools.tool_implementations.search.search_utils import llm_doc_to_dict
from onyx.tools.tool_implementations.search_like_tool_utils import (
    build_next_prompt_for_search_like_tool,
)
from onyx.tools.tool_implementations.search_like_tool_utils import (
    FINAL_CONTEXT_DOCUMENTS_ID,
)
from onyx.tools.tool_implementations.search_like_tool_utils import (
    ORIGINAL_CONTEXT_DOCUMENTS_ID,
)
from onyx.utils.logger import setup_logger
from onyx.utils.special_types import JSON_ro

logger = setup_logger()

SEARCH_RESPONSE_SUMMARY_ID = "search_response_summary"
SEARCH_DOC_CONTENT_ID = "search_doc_content"
SECTION_RELEVANCE_LIST_ID = "section_relevance_list"
SEARCH_EVALUATION_ID = "llm_doc_eval"


class SearchResponseSummary(BaseModel):
    top_sections: list[InferenceSection]
    rephrased_query: str | None = None
    predicted_flow: QueryFlow | None
    predicted_search: SearchType | None
    final_filters: IndexFilters
    recency_bias_multiplier: float


SEARCH_TOOL_DESCRIPTION = """
Runs a semantic search over the user's knowledge base. The default behavior is to use this tool. \
The only scenario where you should not use this tool is if:

- There is sufficient information in chat history to FULLY and ACCURATELY answer the query AND \
additional information or details would provide little or no value.
- The query is some form of request that does not require additional information to handle.

HINT: if you are unfamiliar with the user input OR think the user input is a typo, use this tool.
"""

HARDCODED_PRE_CHOICE_PROMPT = """
You will be given user query and list of all possible tags that sysyem can use to give user docs relevant for the query.
Think out loud in short and concise way what things might be relevant for the user query.
If there's strong connection to some of the mentioned tags, make sure to mention them.
Here are all possible tag values: `{all_tag_values}` , here's the user query : `{query}`
"""

HARDCODED_DTAGS_PROMPT = """
You will be given a report on what tags might be relevant to the user query. Choose tags, from the list of scoped values.
The format should be a simple list of tags separated by commas in brackets, for example: `[tag1, tag2, tag3]`.
It should always be the ONLY thing you write in the message. If not tags are relevant, write `[]`. No yapping.
Here are all scoped possible tag values: `{tag_values}` , here's the user query : `{query}`, here's the report: {report}.
"""


class SingleTagConfig(BaseModel):
    tag_key: str
    all_possible_tag_values: list[str]


class DtagsConfig(BaseModel):
    dtags: list[SingleTagConfig]


logger = setup_logger(__name__)


def generate_context_dependent_filters(
    query: str, dtags_config_str: str
) -> tuple[BaseFilters | None, str]:
    """
    This function is used to generate filters based on the query and the persona's description.
    We use the dtags_config_str to parse it into expected format.
    Expected format:
    {
        "dtags": [
            {
            "tag_key": "Tags",
            "all_possible_tag_values": ["равенство", "дискриминация", "преступление"],
            },
            {
            "tag_key": "Select",
            "all_possible_tag_values": ["Уже во Франции", "Хочу во Францию"],
            },
            ],
    }
    Args:
    query: str: The query that the user has asked.
    dtags_config_str: str: The persona's description that contains the dtags_config and can be loaded into a dict with json.loads.

    Returns:
    BaseFilters: The filters that are generated based on the query and the dtags_config_str.
    """

    # define pydantic model and validators using our doc
    # copilot, write it and define validators
    (
        main_llm,
        fast_llm,
    ) = get_default_llms()  # TODO:  maybe singleton pattern and out of function scope?
    llm = main_llm
    try:
        dtags_config = DtagsConfig(**json.loads(dtags_config_str))
    except Exception as e:
        logger.error(
            f"Error parsing dtags config. Will use empty filters, error is: {e}"
        )
        filters = None
        # filters = BaseFilters(
        #     tags=[Tag(tag_key="Tags", tag_value="равенство")]
        # )  # эксперимент
    all_tags = []
    tag_report = ""
    all_possible_tag_values = [
        single_dtag
        for single_tag_config in dtags_config.dtags
        for single_dtag in single_tag_config.all_possible_tag_values
    ]
    pre_report_prompt = HARDCODED_PRE_CHOICE_PROMPT.format(
        all_tag_values=all_possible_tag_values, query=query
    )
    pre_report_msg = llm.invoke(prompt=pre_report_prompt, tools=None, tool_choice=None)
    logger.info(f"Pre report msg: {pre_report_msg.content}")
    for single_dtag_config in dtags_config.dtags:
        try:
            scoped_tag_values = str(single_dtag_config.all_possible_tag_values)
            choose_tags_prompt = HARDCODED_DTAGS_PROMPT.format(
                tag_values=scoped_tag_values,
                query=query,
                report=str(pre_report_msg.content),
            )
            chosen_tags_msg = llm.invoke(
                prompt=choose_tags_prompt, tools=None, tool_choice=None
            )
            logger.info(f"Chosen tags msg: {chosen_tags_msg.content}")
            raw_tags = (
                str(chosen_tags_msg.content).split("[")[-1].split("]")[0].split(",")
            )
            tags = [
                Tag(tag_key=single_dtag_config.tag_key, tag_value=tag.strip())
                for tag in raw_tags
            ]
            all_tags.extend(tags)
            tag_report += f"{single_dtag_config.tag_key} : {raw_tags}\n"
        except (ValueError, TypeError) as e:
            logger.error(
                f"Error parsing dtags config. Will skip this tag, error is: {e}"
            )
    filters = BaseFilters(tags=all_tags)
    return filters, tag_report


class SearchTool(Tool):
    _NAME = "run_search"
    _DISPLAY_NAME = "Search Tool"
    _DESCRIPTION = SEARCH_TOOL_DESCRIPTION

    def __init__(
        self,
        db_session: Session,
        user: User | None,
        persona: Persona,
        retrieval_options: RetrievalDetails | None,
        prompt_config: PromptConfig,
        llm: LLM,
        fast_llm: LLM,
        pruning_config: DocumentPruningConfig,
        answer_style_config: AnswerStyleConfig,
        evaluation_type: LLMEvaluationType,
        # if specified, will not actually run a search and will instead return these
        # sections. Used when the user selects specific docs to talk to
        selected_sections: list[InferenceSection] | None = None,
        chunks_above: int | None = None,
        chunks_below: int | None = None,
        full_doc: bool = False,
        bypass_acl: bool = False,
        rerank_settings: RerankingDetails | None = None,
    ) -> None:
        self.user = user
        self.persona = persona
        self.retrieval_options = retrieval_options
        self.prompt_config = prompt_config
        self.llm = llm
        self.fast_llm = fast_llm
        self.evaluation_type = evaluation_type

        self.selected_sections = selected_sections

        self.full_doc = full_doc
        self.bypass_acl = bypass_acl
        self.db_session = db_session

        # Only used via API
        self.rerank_settings = rerank_settings

        self.chunks_above = (
            chunks_above
            if chunks_above is not None
            else (
                persona.chunks_above
                if persona.chunks_above is not None
                else CONTEXT_CHUNKS_ABOVE
            )
        )
        self.chunks_below = (
            chunks_below
            if chunks_below is not None
            else (
                persona.chunks_below
                if persona.chunks_below is not None
                else CONTEXT_CHUNKS_BELOW
            )
        )

        # For small context models, don't include additional surrounding context
        # The 3 here for at least minimum 1 above, 1 below and 1 for the middle chunk
        max_llm_tokens = compute_max_llm_input_tokens(self.llm.config)
        if max_llm_tokens < 3 * GEN_AI_MODEL_FALLBACK_MAX_TOKENS:
            self.chunks_above = 0
            self.chunks_below = 0

        num_chunk_multiple = self.chunks_above + self.chunks_below + 1

        self.answer_style_config = answer_style_config
        self.contextual_pruning_config = (
            ContextualPruningConfig.from_doc_pruning_config(
                num_chunk_multiple=num_chunk_multiple, doc_pruning_config=pruning_config
            )
        )

    @property
    def name(self) -> str:
        return self._NAME

    @property
    def description(self) -> str:
        return self._DESCRIPTION

    @property
    def display_name(self) -> str:
        return self._DISPLAY_NAME

    """For explicit tool calling"""

    def tool_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def build_tool_message_content(
        self, *args: ToolResponse
    ) -> str | list[str | dict[str, Any]]:
        final_context_docs_response = next(
            response for response in args if response.id == FINAL_CONTEXT_DOCUMENTS_ID
        )
        final_context_docs = cast(list[LlmDoc], final_context_docs_response.response)

        return json.dumps(
            {
                "search_results": [
                    llm_doc_to_dict(doc, ind)
                    for ind, doc in enumerate(final_context_docs)
                ]
            }
        )

    """For LLMs that don't support tool calling"""

    def get_args_for_non_tool_calling_llm(
        self,
        query: str,
        history: list[PreviousMessage],
        llm: LLM,
        force_run: bool = False,
    ) -> dict[str, Any] | None:
        if not force_run and not check_if_need_search(
            query=query, history=history, llm=llm
        ):
            return None

        rephrased_query = history_based_query_rephrase(
            query=query, history=history, llm=llm
        )
        return {"query": rephrased_query}

    """Actual tool execution"""

    def _build_response_for_specified_sections(
        self, query: str
    ) -> Generator[ToolResponse, None, None]:
        if self.selected_sections is None:
            raise ValueError("Sections must be specified")

        yield ToolResponse(
            id=SEARCH_RESPONSE_SUMMARY_ID,
            response=SearchResponseSummary(
                rephrased_query=None,
                top_sections=[],
                predicted_flow=None,
                predicted_search=None,
                final_filters=IndexFilters(access_control_list=None),  # dummy filters
                recency_bias_multiplier=1.0,
            ),
        )

        # Build selected sections for specified documents
        selected_sections = [
            SectionRelevancePiece(
                relevant=True,
                document_id=section.center_chunk.document_id,
                chunk_id=section.center_chunk.chunk_id,
            )
            for section in self.selected_sections
        ]

        yield ToolResponse(
            id=SECTION_RELEVANCE_LIST_ID,
            response=selected_sections,
        )

        final_context_sections = prune_and_merge_sections(
            sections=self.selected_sections,
            section_relevance_list=None,
            prompt_config=self.prompt_config,
            llm_config=self.llm.config,
            question=query,
            contextual_pruning_config=self.contextual_pruning_config,
        )

        llm_docs = [
            llm_doc_from_inference_section(section)
            for section in final_context_sections
        ]

        yield ToolResponse(id=FINAL_CONTEXT_DOCUMENTS_ID, response=llm_docs)

    def run(self, **kwargs: str) -> Generator[ToolResponse, None, None]:
        query = cast(str, kwargs["query"])

        if self.selected_sections:
            yield from self._build_response_for_specified_sections(query)
            return
        tag_report = ""
        if "[DTAGS]" in self.persona.description:
            dtags_config_str = self.persona.description.split("[DTAGS]")[-1].split(
                "[/DTAGS]"
            )[0]
            filters, tag_report = generate_context_dependent_filters(
                query, dtags_config_str
            )
            if not self.retrieval_options:
                self.retrieval_options = RetrievalDetails(filters=filters)
            else:
                self.retrieval_options.filters = filters
        search_pipeline = SearchPipeline(
            search_request=SearchRequest(
                query=query,
                evaluation_type=self.evaluation_type,
                human_selected_filters=(
                    self.retrieval_options.filters if self.retrieval_options else None
                ),
                persona=self.persona,
                offset=(
                    self.retrieval_options.offset if self.retrieval_options else None
                ),
                limit=self.retrieval_options.limit if self.retrieval_options else None,
                rerank_settings=self.rerank_settings,
                chunks_above=self.chunks_above,
                chunks_below=self.chunks_below,
                full_doc=self.full_doc,
                enable_auto_detect_filters=(
                    self.retrieval_options.enable_auto_detect_filters
                    if self.retrieval_options
                    else None
                ),
            ),
            user=self.user,
            llm=self.llm,
            fast_llm=self.fast_llm,
            bypass_acl=self.bypass_acl,
            db_session=self.db_session,
            prompt_config=self.prompt_config,
        )

        yield ToolResponse(
            id=SEARCH_RESPONSE_SUMMARY_ID,
            response=SearchResponseSummary(
                rephrased_query=query + tag_report,
                top_sections=search_pipeline.final_context_sections,
                predicted_flow=search_pipeline.predicted_flow,
                predicted_search=search_pipeline.predicted_search_type,
                final_filters=search_pipeline.search_query.filters,
                recency_bias_multiplier=search_pipeline.search_query.recency_bias_multiplier,
            ),
        )

        yield ToolResponse(
            id=SEARCH_DOC_CONTENT_ID,
            response=OnyxContexts(
                contexts=[
                    OnyxContext(
                        content=section.combined_content,
                        document_id=section.center_chunk.document_id,
                        semantic_identifier=section.center_chunk.semantic_identifier,
                        blurb=section.center_chunk.blurb,
                    )
                    for section in search_pipeline.reranked_sections
                ]
            ),
        )

        yield ToolResponse(
            id=SECTION_RELEVANCE_LIST_ID,
            response=search_pipeline.section_relevance,
        )

        pruned_sections = prune_sections(
            sections=search_pipeline.final_context_sections,
            section_relevance_list=search_pipeline.section_relevance_list,
            prompt_config=self.prompt_config,
            llm_config=self.llm.config,
            question=query,
            contextual_pruning_config=self.contextual_pruning_config,
        )

        llm_docs = [
            llm_doc_from_inference_section(section) for section in pruned_sections
        ]

        yield ToolResponse(id=FINAL_CONTEXT_DOCUMENTS_ID, response=llm_docs)

    def final_result(self, *args: ToolResponse) -> JSON_ro:
        final_docs = cast(
            list[LlmDoc],
            next(arg.response for arg in args if arg.id == FINAL_CONTEXT_DOCUMENTS_ID),
        )
        # NOTE: need to do this json.loads(doc.json()) stuff because there are some
        # subfields that are not serializable by default (datetime)
        # this forces pydantic to make them JSON serializable for us
        return [json.loads(doc.model_dump_json()) for doc in final_docs]

    def build_next_prompt(
        self,
        prompt_builder: AnswerPromptBuilder,
        tool_call_summary: ToolCallSummary,
        tool_responses: list[ToolResponse],
        using_tool_calling_llm: bool,
    ) -> AnswerPromptBuilder:
        return build_next_prompt_for_search_like_tool(
            prompt_builder=prompt_builder,
            tool_call_summary=tool_call_summary,
            tool_responses=tool_responses,
            using_tool_calling_llm=using_tool_calling_llm,
            answer_style_config=self.answer_style_config,
            prompt_config=self.prompt_config,
        )

    """Other utility functions"""

    @classmethod
    def get_search_result(
        cls, llm_call: LLMCall
    ) -> tuple[list[LlmDoc], dict[str, int]] | None:
        """
        Returns the final search results and a map of docs to their original search rank (which is what is displayed to user)
        """
        if not llm_call.tool_call_info:
            return None

        final_search_results = []
        doc_id_to_original_search_rank_map = {}

        for yield_item in llm_call.tool_call_info:
            if (
                isinstance(yield_item, ToolResponse)
                and yield_item.id == FINAL_CONTEXT_DOCUMENTS_ID
            ):
                final_search_results = cast(list[LlmDoc], yield_item.response)
            elif (
                isinstance(yield_item, ToolResponse)
                and yield_item.id == ORIGINAL_CONTEXT_DOCUMENTS_ID
            ):
                search_contexts = yield_item.response.contexts
                original_doc_search_rank = 1
                for idx, doc in enumerate(search_contexts):
                    if doc.document_id not in doc_id_to_original_search_rank_map:
                        doc_id_to_original_search_rank_map[
                            doc.document_id
                        ] = original_doc_search_rank
                        original_doc_search_rank += 1

        return final_search_results, doc_id_to_original_search_rank_map
