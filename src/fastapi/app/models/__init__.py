"""GeoRAG domain models package.

Exports all Pydantic models from the geological and RAG sub-modules so that
the rest of the application imports from a single location:

    from app.models import CollarRead, GeoRAGResponse, Citation
"""

from app.models.answer_run import (
    AnswerCitationItemCreate,
    AnswerCitationItemRead,
    AnswerCitationSpanCreate,
    AnswerCitationSpanRead,
    AnswerRetrievalItemCreate,
    AnswerRetrievalItemRead,
    AnswerRunCreate,
    AnswerRunRead,
    AnswerRunUpdate,
    BackendLiteral,
    CitationLifecycleState,
    CitationLifecycleStateLiteral,
    CitationMode,
    CitationModeLiteral,
    FusionMethodLiteral,
    QueryClassLiteral,
    SourceStoreLiteral,
    StageLiteral,
)
from app.models.evidence import (
    DocumentRevisionCreate,
    DocumentRevisionRead,
    EvidenceItemCreate,
    EvidenceItemRead,
    EvidenceTypeLiteral,
    StructuredRecordLineageCreate,
    StructuredRecordLineageRead,
)
from app.models.feedback import (
    FeedbackCategory,
    FeedbackCreate,
    FeedbackPolarity,
    FeedbackRead,
)
from app.models.geological import (
    AlterationCreate,
    AlterationRead,
    CollarCreate,
    CollarRead,
    GeochemistryCreate,
    GeochemistryRead,
    LithologyLogCreate,
    LithologyLogRead,
    ProjectCreate,
    ProjectRead,
    ReportCreate,
    ReportRead,
    SampleCreate,
    SampleRead,
    StructureCreate,
    StructureRead,
    SurveyCreate,
    SurveyRead,
)
from app.models.rag import (
    Citation,
    GeoRAGResponse,
    MapPayload,
    VizPayload,
)

__all__ = [
    # Geological — Project
    "ProjectCreate",
    "ProjectRead",
    # Geological — Collar
    "CollarCreate",
    "CollarRead",
    # Geological — Survey
    "SurveyCreate",
    "SurveyRead",
    # Geological — LithologyLog
    "LithologyLogCreate",
    "LithologyLogRead",
    # Geological — Alteration
    "AlterationCreate",
    "AlterationRead",
    # Geological — Structure
    "StructureCreate",
    "StructureRead",
    # Geological — Sample
    "SampleCreate",
    "SampleRead",
    # Geological — Geochemistry
    "GeochemistryCreate",
    "GeochemistryRead",
    # Geological — Report
    "ReportCreate",
    "ReportRead",
    # Evidence model (§04j stubs — pending senior-reviewer migration approval)
    "EvidenceTypeLiteral",
    "DocumentRevisionCreate",
    "DocumentRevisionRead",
    "EvidenceItemCreate",
    "EvidenceItemRead",
    "StructuredRecordLineageCreate",
    "StructuredRecordLineageRead",
    # Answer runs (Module 4 Phase B — migrations 2026-04-21)
    "QueryClassLiteral",
    "FusionMethodLiteral",
    "BackendLiteral",
    "CitationLifecycleStateLiteral",
    "CitationLifecycleState",
    "CitationModeLiteral",
    "CitationMode",
    "StageLiteral",
    "SourceStoreLiteral",
    "AnswerRunCreate",
    "AnswerRunRead",
    "AnswerRunUpdate",
    "AnswerRetrievalItemCreate",
    "AnswerRetrievalItemRead",
    # Citation tables (Module 6 Phase B Chunk 1 — migrations 150000 + 160000)
    "AnswerCitationItemCreate",
    "AnswerCitationItemRead",
    "AnswerCitationSpanCreate",
    "AnswerCitationSpanRead",
    # RAG pipeline
    "Citation",
    "GeoRAGResponse",
    "MapPayload",
    "VizPayload",
    # Feedback (Module 7 Phase B Chunk 1)
    "FeedbackPolarity",
    "FeedbackCategory",
    "FeedbackCreate",
    "FeedbackRead",
]
