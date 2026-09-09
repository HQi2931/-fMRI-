from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from neuroagent.api.app import create_app
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service
from tests.literature.pdf_factory import make_blank_pdf, make_text_pdf


def test_pdf_upload_persists_paper_sections_chunks_and_source(
    service: NeuroAgentService, work_root: Path
) -> None:
    pdf = make_text_pdf(
        (
            "Traceable rs-fMRI Paper\nAbstract\nA short abstract.\n1 Introduction\nIntro text.",
            "2 Methods\nOverview.\n2.1 Image preprocessing\n"
            "Motion was corrected.\n3 Results\nFindings.",
        ),
        title="Traceable rs-fMRI Paper",
        author="Researcher One",
    )
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/literature/papers",
            files={"file": ("paper.pdf", pdf, "application/pdf")},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        restored = client.get(f"/api/v1/literature/papers/{body['paper']['paper_id']}")
        listed = client.get("/api/v1/literature/papers")

    assert restored.status_code == 200
    assert restored.json() == body
    assert len(listed.json()) == 1
    paper = body["paper"]
    source = work_root.joinpath(*Path(paper["source_file"]).parts)
    assert source.read_bytes() == pdf
    section_ids = {item["section_id"] for item in body["sections"]}
    assert body["chunks"]
    for chunk in body["chunks"]:
        assert chunk["paper_id"] == paper["paper_id"]
        assert chunk["metadata"]["source_section_id"] in section_ids
        assert 1 <= chunk["page_start"] <= chunk["page_end"] <= paper["page_count"]


def test_pdf_upload_rejects_unsupported_corrupt_and_textless_files(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        wrong_extension = client.post(
            "/api/v1/literature/papers",
            files={"file": ("paper.txt", b"plain text", "text/plain")},
        )
        disguised = client.post(
            "/api/v1/literature/papers",
            files={"file": ("paper.pdf", b"plain text", "application/pdf")},
        )
        corrupt = client.post(
            "/api/v1/literature/papers",
            files={"file": ("paper.pdf", b"%PDF-1.4\nbroken", "application/pdf")},
        )
        textless = client.post(
            "/api/v1/literature/papers",
            files={"file": ("scan.pdf", make_blank_pdf(), "application/pdf")},
        )

    assert wrong_extension.status_code == 415
    assert disguised.status_code == 415
    assert corrupt.status_code == 422
    assert corrupt.json()["error"]["code"] == "pdf_parse_failed"
    assert textless.status_code == 422
    assert textless.json()["error"]["code"] == "pdf_text_layer_missing"


def test_pdf_upload_enforces_configured_size_limit(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    settings = Settings(
        rag_db_dir=None,
        database_url=f"sqlite:///{(tmp_path / 'small.sqlite').as_posix()}",
        allowed_source_roots=[tmp_path / "source"],
        allowed_work_root=tmp_path / "work",
        literature_max_pdf_bytes=100,
    )
    service = build_service(settings)
    try:
        with TestClient(create_app(service=service)) as client:
            response = client.post(
                "/api/v1/literature/papers",
                files={"file": ("large.pdf", b"%PDF-" + b"x" * 100, "application/pdf")},
            )
    finally:
        service.close()
    assert response.status_code == 413
    assert response.json()["error"]["details"]["max_bytes"] == 100


def test_missing_paper_uses_standard_error_envelope(service: NeuroAgentService) -> None:
    with TestClient(create_app(service=service)) as client:
        response = client.get("/api/v1/literature/papers/missing-paper")
    assert response.status_code == 404
    assert response.json()["error"]["details"]["resource"] == "paper"


def test_manual_index_retry_filter_and_pdf_source(
    service: NeuroAgentService, work_root: Path
) -> None:
    from typing import Any

    from neuroagent.agent.secrets import ProcessEnvironmentSecretResolver
    from neuroagent.retrieval.uploaded_index import UploadedLiteratureIndex

    class Embeddings:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

        def embed_query(self, text: str) -> list[float]:
            return [1.0]

    class Collection:
        def __init__(self) -> None:
            self.rows: dict[str, tuple[str, dict[str, Any]]] = {}
            self.fail = True

        def upsert(
            self,
            *,
            ids: list[str],
            documents: list[str],
            metadatas: list[dict[str, Any]],
            embeddings: Any,
        ) -> None:
            if self.fail:
                raise RuntimeError("synthetic failure")
            self.rows.update(
                {
                    key: (text, meta)
                    for key, text, meta in zip(ids, documents, metadatas, strict=True)
                }
            )

        def query(
            self, *, query_embeddings: Any, n_results: int, where: Any, include: Any
        ) -> dict[str, Any]:
            matches = [
                (key, text, meta)
                for key, (text, meta) in self.rows.items()
                if meta["paper_id"] in where["paper_id"]["$in"]
            ][:n_results]
            return {
                "ids": [[r[0] for r in matches]],
                "documents": [[r[1] for r in matches]],
                "metadatas": [[r[2] for r in matches]],
            }

    index = UploadedLiteratureIndex(
        work_root=work_root,
        repository=service.repository,
        secret_resolver=ProcessEnvironmentSecretResolver(),
        api_key_env="DASHSCOPE_API_KEY",
        redaction_salt="test-salt-for-literature",
    )
    collection = Collection()
    index._collection = collection
    index._embeddings = Embeddings()
    service.literature._indexer = index
    pdf = make_text_pdf(
        ("Synthetic methods\nAbstract\nConnectivity methods.\n1 Methods\nCorrelation analysis.",)
    )
    with TestClient(create_app(service=service)) as client:
        result = client.post(
            "/api/v1/literature/papers", files={"file": ("a.pdf", pdf, "application/pdf")}
        ).json()
        paper_id = result["paper"]["paper_id"]
        path = f"/api/v1/literature/papers/{paper_id}"
        assert result["paper"]["index_status"] == "not_indexed"
        assert index.candidates("methods", limit=8, paper_ids=[paper_id]) == []
        assert client.post(path + "/index").status_code == 503
        assert client.get(path).json()["paper"]["index_status"] == "failed"
        collection.fail = False
        assert client.post(path + "/index").json()["paper"]["index_status"] == "ready"
        assert client.post(path + "/index").status_code == 200
        assert len(collection.rows) == len(result["chunks"])
        candidates = index.candidates("methods", limit=8, paper_ids=[paper_id])
        assert candidates[0]["page_start"] == 1
        assert candidates[0]["chunk_id"] == result["chunks"][0]["chunk_id"]
        assert client.get(path + "/source").content == pdf
