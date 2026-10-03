"""Las llamadas a Claude no se hacen con una transacción abierta.

process_document confirma su estado (fase A), cierra las lecturas (fase B) y llama a Claude sin
transacción (fase C); guarda el resultado en una transacción nueva (fases D y E). En SQLite, una
transacción con escrituras pendientes bloquearía cualquier otra escritura mientras Claude responde.
Claude está simulado: no se llama a la API real.
"""
from __future__ import annotations

import threading
import time

LLM_WAIT_SECONDS = 6  # lo que «tarda» la IA simulada como mucho (espera a que la otra escritura termine)


def make_document(client, sample_pdfs):
    import shutil
    import uuid

    from app.config import settings
    from app.database import SessionLocal
    from app.models import Document

    source = next(iter(sample_pdfs.values()))
    stored = f"{uuid.uuid4().hex}.pdf"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, settings.upload_dir / stored)
    with SessionLocal() as database:
        document = Document(original_filename="lenta.pdf", stored_filename=stored, sha256=uuid.uuid4().hex * 2, extension=".pdf",
                            size_bytes=source.stat().st_size, source="manual_upload", status="RECEIVED", extraction_status="PENDING")
        database.add(document)
        database.commit()
        return document.id, settings.upload_dir / stored


def test_a_slow_llm_call_does_not_block_another_write(client, sample_pdfs, monkeypatch):
    from app.agents import llm
    from app.database import SessionLocal
    from app.invoice_service import process_document
    from app.models import AuditEvent
    from app.models import Document
    import app.interpretation as interpretation

    llm_started, other_write_done = threading.Event(), threading.Event()

    def slow_llm(**kwargs):
        llm_started.set()
        other_write_done.wait(LLM_WAIT_SECONDS)  # la IA «piensa» mientras otra petición escribe
        return None, {"model": "simulado", "fallback": "simulado", "input_tokens": None, "output_tokens": None, "cost_usd": None}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_invoice", slow_llm)
    monkeypatch.setattr(interpretation, "needs_help", lambda *args, **kwargs: ["motivo de prueba"])
    monkeypatch.setattr("app.routing.allowed_reasons", lambda reasons: True)

    document_id, path = make_document(client, sample_pdfs)
    errors: list[BaseException] = []

    def work():
        with SessionLocal() as database:
            try:
                process_document(database, document=database.get(Document, document_id), file_path=path, actor="prueba")
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

    worker = threading.Thread(target=work)
    worker.start()
    assert llm_started.wait(10), "la IA simulada no llegó a llamarse"

    started = time.perf_counter()
    with SessionLocal() as other:  # otra petición escribe mientras Claude «piensa»
        other.add(AuditEvent(action="prueba.escritura_concurrente", entity_type="test", entity_id="1", actor="prueba", event_data={}))
        other.commit()
    elapsed = time.perf_counter() - started
    other_write_done.set()
    worker.join(30)

    assert not errors, errors
    assert elapsed < 2, f"la escritura esperó {elapsed:.1f} s: había una transacción abierta durante la llamada a la IA"
    with SessionLocal() as database:
        document = database.get(Document, document_id)
        assert document.extraction_status != "RUNNING" and document.status != "FAILED"
        assert database.query(AuditEvent).filter(AuditEvent.action == "prueba.escritura_concurrente").count() == 1


def test_llm_failure_keeps_the_rules_result_and_the_document_states(client, sample_pdfs, monkeypatch):
    """Si la IA falla (error de API), se vuelve a las reglas: el documento termina como con reglas solas."""
    from app.agents import llm
    from app.database import SessionLocal
    from app.invoice_service import process_document
    from app.models import Document
    import app.interpretation as interpretation

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_invoice", lambda **kwargs: (None, {"model": "simulado", "fallback": "servicio saturado (529)", "outcome": "error"}))
    monkeypatch.setattr(interpretation, "needs_help", lambda *args, **kwargs: ["motivo de prueba"])
    monkeypatch.setattr("app.routing.allowed_reasons", lambda reasons: True)

    document_id, path = make_document(client, sample_pdfs)
    with SessionLocal() as database:
        invoice = process_document(database, document=database.get(Document, document_id), file_path=path, actor="prueba")
        document = database.get(Document, document_id)
        assert invoice is not None and document.status != "FAILED" and document.extraction_status != "RUNNING"
        assert document.extraction_runs[-1].status not in {"RUNNING", "FAILED"}


def test_a_failure_while_saving_marks_the_document_failed_cleanly(client, sample_pdfs, monkeypatch):
    """Un error al guardar deja FAILED con el motivo, sin dejar la sesión rota ni datos a medias."""
    from app.database import SessionLocal
    from app.invoice_service import process_document
    from app.models import Document

    def broken(*args, **kwargs):
        raise RuntimeError("fallo simulado al guardar")

    monkeypatch.setattr("app.invoice_service.persist_extraction_result", broken)
    document_id, path = make_document(client, sample_pdfs)
    with SessionLocal() as database:
        try:
            process_document(database, document=database.get(Document, document_id), file_path=path, actor="prueba")
        except RuntimeError:
            pass
    with SessionLocal() as database:
        document = database.get(Document, document_id)
        assert document.status == "FAILED" and "fallo simulado" in document.failure_reason
        assert document.extraction_runs[-1].status == "FAILED"
