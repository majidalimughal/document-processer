
from models import Document


def test_create_document(test_client,db_session):
    response = test_client.post(
        "/documents",
        json={
            "filename": "report.pdf",
            "content": "This is a test document.",
        },
    )

    # 1. Verify the HTTP response
    assert response.status_code == 200

    data = response.json()

    assert data["filename"] == "report.pdf"
    assert data["status"] == "pending"

    # 2. Verify actual database persistence
    db = db_session

    try:
        saved_document = (
            db.query(Document)
            .filter(Document.filename == "report.pdf")
            .first()
        )

        assert saved_document is not None
        assert saved_document.filename == "report.pdf"
        assert saved_document.content == "This is a test document."
        assert saved_document.status == "pending"

    finally:
        db.close()
