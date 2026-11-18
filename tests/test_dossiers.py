"""Parcours du dossier depuis l'API et contrôle des accès aux documents privés."""

import io
import json
import zipfile

import pytest


def make_pdf():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(400, 400)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (Luzerne en prairie. Le rendement reste stable avec du sorgho"
                    b" cultive dans une parcelle profonde pendant une secheresse estivale.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_private_folder_workflow(demo_ctx, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from sourcia.api.app import create_app
    from sourcia.container import Context

    owner_token, guest_token = "a" * 40, "b" * 40
    monkeypatch.setenv("SOURCIA_DOSSIER_ACCOUNTS", json.dumps({
        "enseignant@example.org": owner_token, "collegue@example.org": guest_token,
    }))
    owner = {"Authorization": f"Bearer {owner_token}"}
    guest = {"Authorization": f"Bearer {guest_token}"}
    client = TestClient(create_app(ctx=demo_ctx))
    home = client.get("/")
    assert 'href="/dossiers"' not in home.text
    for page in (home, client.get("/dossiers")):
        assert page.status_code == 200
        assert 'href="/assets/sourcia.css"' in page.text
    assert client.get("/assets/sourcia.css").headers["content-type"].startswith("text/css")
    assert client.get("/api/dossiers").status_code == 401
    created = client.post("/api/dossiers", headers=owner, json={})
    assert created.status_code == 201
    assert created.json()["title"].startswith("Dossier pédagogique")
    folder = "/api/dossiers/" + created.json()["id"]
    assert not client.get(folder, headers=owner).json()["export_disponible"]
    assert client.get(folder, headers=guest).status_code == 404
    assert client.get(folder + "/export.zip", headers=guest).status_code == 404

    pdf = make_pdf()
    rejected = client.post(folder + "/documents", headers=owner, files={"file": ("faux.pdf", b"texte")})
    assert rejected.status_code == 415
    imported = client.post(folder + "/documents", headers=owner, files={"file": ("Cours.pdf", pdf)})
    assert imported.status_code == 202
    doc_id = imported.json()["id"]
    detail = client.get(folder, headers=owner).json()
    assert detail["documents"][0]["status"] == "pret"
    assert detail["notification"]
    assert detail["export_disponible"]
    assert client.get(folder + "?q=parcelle", headers=owner).json()["documents"]
    corpus = client.get(folder + "/corpus", headers=owner).json()["documents"]
    assert "Luzerne" in corpus[0]["pages"][0]["texte"]
    assert corpus[0]["url"] is None
    assert client.get(f"/api/fiche/{doc_id}").status_code == 404

    public = client.get(folder + "/recherche-catalogue?q=sorgho", headers=owner).json()[0]
    payload = {"document_id": public["id"]}
    assert client.post(folder + "/catalogue", headers=owner, json=payload).status_code == 202
    assert client.post(folder + "/catalogue", headers=owner, json=payload).status_code == 409

    share = {"email": "COLLEGUE@example.org", "role": "lecture"}
    assert client.put(folder + "/partages", headers=owner, json=share).status_code == 200
    patch = folder + f"/documents/{doc_id}"
    assert client.patch(patch, headers=guest, json={"selected": False}).status_code == 403
    zipped = client.get(folder + "/export.zip", headers=guest)
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        path = next(name for name in archive.namelist() if name.endswith(".pdf"))
        assert archive.read(path) == pdf

    share["role"] = "modification"
    assert client.put(folder + "/partages", headers=owner, json=share).status_code == 200
    restarted = TestClient(create_app(ctx=Context(demo_ctx.settings)))
    assert restarted.patch(patch, headers=guest, json={"selected": False}).status_code == 200
    assert doc_id not in [doc["id"] for doc in restarted.get(folder + "/corpus", headers=owner).json()["documents"]]
    share["role"] = None
    assert restarted.put(folder + "/partages", headers=owner, json=share).status_code == 200
    assert restarted.get(folder, headers=guest).status_code == 404
