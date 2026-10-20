"""Vector index callers share the configured process lock mount."""



from db.vector.embeddings import VECTOR_SIZE

VECTOR = [1.0] + [0.0] * (VECTOR_SIZE - 1)


def test_compose_vector_callers_share_one_lock_mount():
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    for name in ("api", "ingestion", "rules_engine"):
        service = compose["services"][name]
        assert "EDGAR_INDEX_LOCK_DIR=/index-locks" in service["environment"]
        assert "./data/.index-locks:/index-locks" in service["volumes"]
