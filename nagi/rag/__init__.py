"""Local, engine-independent retrieval support."""

from .index import (
    apply_keyword_index_plan,
    build_keyword_index_plan,
    load_keyword_index,
    render_index_plan_text,
    render_index_result_text,
)
from .context import KeywordContextRetriever
from .evaluation import (
    evaluate_retrieval_benchmark,
    load_retrieval_benchmark,
    render_retrieval_evaluation_text,
)
from .models import KeywordDocument, KeywordQuery, KeywordSearchResponse, KeywordSearchResult
from .hybrid import (
    HYBRID_FUSION_ID,
    RRF_K,
    HybridSearchResponse,
    HybridSearchResult,
    hybrid_index_id,
    search_hybrid_index,
)
from .retriever import matches_query_filters, search_keyword_index
from .service import query_keyword_index, render_search_response_text
from .strategies import (
    HYBRID_BACKEND_ID,
    VECTOR_BACKEND_ID,
    RetrievalStrategyResult,
    RetrievalStrategySpec,
    default_retrieval_strategy_specs,
    evaluate_retrieval_strategies,
    evaluate_retrieval_strategy,
)
from .vector import (
    VECTOR_DIMENSIONS,
    VECTOR_MODEL_ID,
    VECTOR_NORMALIZATION,
    LoadedVectorIndex,
    VectorSearchResponse,
    VectorSearchResult,
    build_vector_index,
    search_vector_index,
)

__all__ = [
    "KeywordDocument",
    "KeywordContextRetriever",
    "KeywordQuery",
    "KeywordSearchResponse",
    "KeywordSearchResult",
    "HybridSearchResponse",
    "HybridSearchResult",
    "LoadedVectorIndex",
    "RetrievalStrategyResult",
    "RetrievalStrategySpec",
    "VectorSearchResponse",
    "VectorSearchResult",
    "HYBRID_BACKEND_ID",
    "HYBRID_FUSION_ID",
    "RRF_K",
    "VECTOR_BACKEND_ID",
    "VECTOR_DIMENSIONS",
    "VECTOR_MODEL_ID",
    "VECTOR_NORMALIZATION",
    "apply_keyword_index_plan",
    "build_keyword_index_plan",
    "build_vector_index",
    "evaluate_retrieval_benchmark",
    "default_retrieval_strategy_specs",
    "evaluate_retrieval_strategies",
    "evaluate_retrieval_strategy",
    "load_keyword_index",
    "load_retrieval_benchmark",
    "hybrid_index_id",
    "matches_query_filters",
    "query_keyword_index",
    "render_index_plan_text",
    "render_index_result_text",
    "render_retrieval_evaluation_text",
    "render_search_response_text",
    "search_keyword_index",
    "search_hybrid_index",
    "search_vector_index",
]
