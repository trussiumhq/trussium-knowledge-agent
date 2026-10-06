"""HTTP question endpoint and response contracts for the browser interface."""

from __future__ import annotations

import os
import sys
from typing import Literal

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from trussium_knowledge_agent.answers import INSUFFICIENT_ANSWER, answer_from_passages
from trussium_knowledge_agent.chat import TrussiumChatClient
from trussium_knowledge_agent.embeddings import TrussiumEmbeddingsClient
from trussium_knowledge_agent.retrieval import search_index

router = APIRouter()


class QuestionRequest(BaseModel):
    """A bounded question and evidence limit submitted by the browser."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=10)


class CitationResponse(BaseModel):
    """Validated citation metadata; paths are display-only, not filesystem URLs."""

    reference_id: str
    relative_path: str
    heading_path: list[str]
    heading_anchor: str
    revision: str | None


class QuestionResponse(BaseModel):
    """Grounded answer response for the browser client."""

    status: Literal["answered", "insufficient_evidence"]
    answer: str
    citations: list[CitationResponse]


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="The question service is unavailable.")


@router.post("/api/ask", response_model=QuestionResponse, tags=["questions"])
def ask_question(request: QuestionRequest) -> QuestionResponse:
    """Retrieve indexed evidence and return a citation-validated answer."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    embedding_model = os.environ.get("TRUSSIUM_EMBEDDING_MODEL", "").strip()
    if (
        not database_url
        or not embedding_model
        or embedding_model == "replace-with-runtime-embedding-model"
    ):
        raise _unavailable()

    base_url = os.environ.get("TRUSSIUM_URL", "http://127.0.0.1:9000")
    api_key = os.environ.get("TRUSSIUM_API_KEY") or None
    try:
        timeout_seconds = float(os.environ.get("TRUSSIUM_TIMEOUT_SECONDS", "30"))
        if not 0 < timeout_seconds <= 120:
            raise ValueError("timeout is outside the supported range")
        with TrussiumEmbeddingsClient(
            base_url, api_key=api_key, timeout_seconds=timeout_seconds
        ) as embeddings_client:
            passages = search_index(
                request.question,
                database_url,
                embeddings_client,
                embedding_model,
                top_k=request.limit,
            )
        if not passages:
            return QuestionResponse(
                status="insufficient_evidence", answer=INSUFFICIENT_ANSWER, citations=[]
            )

        chat_model = os.environ.get("TRUSSIUM_CHAT_MODEL", "").strip()
        if not chat_model or chat_model == "replace-with-runtime-chat-model":
            raise _unavailable()
        with TrussiumChatClient(
            base_url, api_key=api_key, timeout_seconds=timeout_seconds
        ) as chat_client:
            answer = answer_from_passages(request.question, passages, chat_client, chat_model)
        return QuestionResponse(
            status=answer.status,
            answer=answer.answer,
            citations=[
                CitationResponse(
                    reference_id=citation.reference_id,
                    relative_path=citation.source.relative_path,
                    heading_path=list(citation.source.heading_path),
                    heading_anchor=citation.source.heading_anchor,
                    revision=citation.source.revision,
                )
                for citation in answer.citations
            ],
        )
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError, TypeError, ValueError):
        print("Question request failed.", file=sys.stderr)
        raise _unavailable() from None
