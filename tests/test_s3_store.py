"""S3 blob store tests against moto's in-process AWS mock: the same write /
reindex / forget / export flows, but with canonical files living in a bucket."""

import tarfile

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from openbrainstore import blobstore, service, store  # noqa: E402

BUCKET = "obs-test"


@pytest.fixture()
def s3_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OBS_USER", "testuser")
    monkeypatch.setenv("OBS_BACKEND", "sqlite")
    monkeypatch.setenv("OBS_BLOB", "s3")
    monkeypatch.setenv("OBS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    blobstore.reset_cache()
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield
    blobstore.reset_cache()


@pytest.fixture()
def s3_backend(s3_env):
    from openbrainstore.backends import get_backend

    b = get_backend()
    yield b
    b.close()


def test_write_read_roundtrip_via_s3(s3_backend):
    res = service.remember(
        s3_backend, "Canonical file lives in a bucket now.", "fact", tags=["s3"]
    )
    mem = store.read_latest("testuser", res["id"])
    assert mem.body == "Canonical file lives in a bucket now."
    # the blob really is in the bucket, at the spec's key layout
    keys = boto3.client("s3").list_objects_v2(
        Bucket=BUCKET, Prefix=f"tenants/testuser/memories/{res['id']}/"
    )
    assert keys["KeyCount"] == 1
    assert keys["Contents"][0]["Key"].endswith(".md")


def test_reindex_from_s3_blobs(s3_backend):
    res = service.remember(s3_backend, "Rebuild me from the bucket.", "fact")
    s3_backend.clear_user("testuser")
    assert service.reindex(s3_backend) == {"reindexed": 1}
    out = service.get_memory_schema(s3_backend)
    assert out["total_memories"] == 1
    assert store.list_memory_ids("testuser") == [res["id"]]


def test_forget_tombstones_in_s3(s3_backend):
    res = service.remember(s3_backend, "Delete me.", "fact")
    service.forget(s3_backend, res["id"])
    assert store.list_memory_ids("testuser") == []
    assert store.is_tombstoned("testuser", res["id"])
    # marker + moved blob both under the tombstone prefix
    keys = boto3.client("s3").list_objects_v2(
        Bucket=BUCKET, Prefix=f"tenants/testuser/tombstones/{res['id']}/"
    )
    names = [o["Key"] for o in keys["Contents"]]
    assert any(k.endswith("_deleted_at") for k in names)
    assert any(k.endswith(".md") for k in names)
    # not yet expired -> survives purge; retain=0 -> reaped
    assert store.purge_tombstones("testuser") == 0
    assert store.purge_tombstones("testuser", retain_days=0) == 1
    assert not store.is_tombstoned("testuser", res["id"])


def test_export_from_s3(s3_backend):
    service.remember(s3_backend, "First.", "fact")
    service.remember(s3_backend, "Second.", "fact")
    res = service.export()
    assert res["memories"] == 2
    with tarfile.open(res["path"]) as tar:
        # OKF bundle built from S3-backed blobs: one memory file each
        assert sum("/memories/" in n and n.endswith(".md") for n in tar.getnames()) == 2
