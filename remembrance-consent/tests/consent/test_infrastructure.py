"""Configuration, authentication seam, rate limiting and object storage."""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from datetime import timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.auth import ActorKind, JwtBearerAuthenticator, current_actor
from app.config import Settings
from app.consent.cascade import CeleryDispatcher, InlineDispatcher
from app.consent.tokens import private_pem
from app.errors import ApiError
from app.ratelimit import InMemoryGCRA, RedisGCRA
from app.services import build_services
from app.storage import LocalFileStorage, S3Storage, StorageError, UrlSigner, validate_key
from tests.support import AUTH_AUDIENCE, AUTH_ISSUER, AUTH_SECRET

# --- configuration --------------------------------------------------------------


def test_production_refuses_development_fallbacks():
    with pytest.raises(ValueError) as caught:
        Settings(env="production", cascade_dispatch="inline", storage_backend="s3", auth_jwt_key="short")
    message = str(caught.value)
    for problem in ("TOKEN_SIGNING_KEY_PEM", "AUTH_JWT_KEY must be at least", "S3_BUCKET", "PostgreSQL", "inline"):
        assert problem in message
    with pytest.raises(ValueError, match="URL_SIGNING_SECRET"):
        Settings(env="production", database_url="postgresql://x", auth_jwt_key="k" * 40,
                 token_signing_key_pem=private_pem(Ed25519PrivateKey.generate()))
    with pytest.raises(ValueError, match="AUTH_JWT_KEY is required"):
        Settings(env="production")


def test_production_accepts_complete_configuration():
    settings = Settings(env="production", database_url="postgresql+psycopg://u@h/db", auth_jwt_key="k" * 40,
                        token_signing_key_pem=private_pem(Ed25519PrivateKey.generate()), storage_backend="s3", s3_bucket="b")
    assert settings.token_ttl_seconds == 300


def test_settings_validate_key_json_and_ttl_ceiling():
    with pytest.raises(ValueError):
        Settings(token_verification_keys_json='["not", "an", "object"]')
    with pytest.raises(ValueError):
        Settings(token_ttl_seconds=301)


def test_build_services_selects_backends(tmp_path, engine):
    base = dict(env="test", auth_jwt_key=AUTH_SECRET, storage_local_root=str(tmp_path))
    inline = build_services(Settings(**base, cascade_dispatch="inline"), engine=engine)
    assert isinstance(inline.dispatcher, InlineDispatcher) and isinstance(inline.storage, LocalFileStorage)
    celery = build_services(Settings(**base), engine=engine)
    assert isinstance(celery.dispatcher, CeleryDispatcher) and isinstance(celery.rate_limiter, InMemoryGCRA)
    with pytest.raises(ValueError, match="AUTH_JWT_KEY"):
        build_services(Settings(env="test", storage_local_root=str(tmp_path)), engine=engine)
    s3 = build_services(Settings(**base, storage_backend="s3", s3_bucket="b", s3_region="us-east-1"), engine=engine)
    assert isinstance(s3.storage, S3Storage)
    redis_backed = build_services(Settings(**base, rate_limit_backend="redis", redis_url="redis://127.0.0.1:1/0"), engine=engine)
    assert isinstance(redis_backed.rate_limiter, RedisGCRA)


# --- authentication ---------------------------------------------------------------


@pytest.fixture
def authenticator():
    return JwtBearerAuthenticator(key=AUTH_SECRET, algorithm="HS256", issuer=AUTH_ISSUER, audience=AUTH_AUDIENCE)


def assertion(**overrides):
    now = int(time.time())
    claims = {"sub": "u1", "iss": AUTH_ISSUER, "aud": AUTH_AUDIENCE, "iat": now, "exp": now + 60}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return "Bearer " + jwt.encode(claims, AUTH_SECRET, algorithm="HS256")


def test_authenticator_builds_actors(authenticator):
    actor = authenticator.authenticate(assertion(email="Mixed@Case.Test", email_verified=True, roles=["Admin"]))
    assert actor.user_id == "u1" and actor.email == "mixed@case.test" and actor.is_admin and actor.kind is ActorKind.HUMAN
    service = authenticator.authenticate(assertion(actor_kind="service", email="x@y.z"))
    assert service.kind is ActorKind.SERVICE and service.email is None  # unverified email is never trusted


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic dXNlcjpwYXNz",
        "Bearer ",
        "Bearer garbage",
        assertion(exp=int(time.time()) - 3600),
        assertion(aud="someone-else"),
        assertion(iss="someone-else"),
        assertion(sub=None),
        assertion(sub=""),
        assertion(sub="x" * 256),
        assertion(actor_kind="robot"),
        assertion(roles="admin"),
        "Bearer " + jwt.encode({"sub": "u1"}, "another-secret-entirely-0123456789", algorithm="HS256"),
    ],
)
def test_authenticator_rejects(authenticator, header):
    assert authenticator.authenticate(header) is None


def test_current_actor_requires_the_middleware():
    with pytest.raises(ApiError) as caught:
        current_actor(SimpleNamespace(state=SimpleNamespace()))
    assert caught.value.status_code == 401


# --- rate limiting ------------------------------------------------------------------


def test_gcra_burst_then_steady_rate():
    now = [1000.0]
    limiter = InMemoryGCRA(rate_per_minute=60, burst=3, now=lambda: now[0])
    assert [limiter.hit("k").allowed for _ in range(4)] == [True, True, True, False]
    denied = limiter.hit("k")
    assert not denied.allowed and 0 < denied.retry_after_seconds <= 1.0
    now[0] += 1.0
    assert limiter.hit("k").allowed and not limiter.hit("k").allowed
    assert limiter.hit("other").allowed  # keys are independent
    now[0] += 60
    assert [limiter.hit("k").allowed for _ in range(3)] == [True, True, True]  # full burst restored


def test_gcra_prunes_idle_keys():
    now = [0.0]
    limiter = InMemoryGCRA(rate_per_minute=60, burst=1, now=lambda: now[0])
    limiter.MAX_KEYS = 5
    for n in range(5):
        limiter.hit(f"k{n}")
    now[0] += 10
    limiter.hit("fresh")
    assert set(limiter._tat) == {"fresh"}


@pytest.mark.parametrize("rate,burst", [(0, 1), (1, 0)])
def test_gcra_parameters_must_be_positive(rate, burst):
    with pytest.raises(ValueError):
        InMemoryGCRA(rate, burst)


@pytest.fixture(scope="module")
def redis_url():
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server not installed")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([binary, "--port", str(port), "--save", "", "--appendonly", "no"], stdout=subprocess.DEVNULL)
    import redis

    client = redis.Redis(port=port)
    for _ in range(50):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}/0"
    process.terminate()
    process.wait(5)


def test_redis_gcra_is_shared_and_atomic(redis_url):
    import redis

    client = redis.Redis.from_url(redis_url)
    first, second = RedisGCRA(client, 60, 2), RedisGCRA(client, 60, 2)  # two workers, one budget
    assert first.hit("actor").allowed and second.hit("actor").allowed
    denied = first.hit("actor")
    assert not denied.allowed and 0 < denied.retry_after_seconds <= 1.0
    assert 0 < client.pttl("remembrance:rl:actor") <= 3000


# --- storage --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "../x", "spaces here", "a" * 1100])
def test_invalid_keys(key):
    with pytest.raises(StorageError):
        validate_key(key)


def test_local_storage_round_trip(tmp_path, clock):
    storage = LocalFileStorage(tmp_path, UrlSigner(b"s" * 32), "https://consent.example/")
    storage.put("exports/p/1.zip", b"data", "application/zip")
    assert storage.exists("exports/p/1.zip") and storage.get("exports/p/1.zip") == b"data"
    assert oct((tmp_path / "exports/p/1.zip").stat().st_mode & 0o777) == "0o600"
    storage.delete("exports/p/1.zip")
    storage.delete("exports/p/1.zip")  # idempotent
    assert not storage.exists("exports/p/1.zip")
    with pytest.raises(StorageError):
        storage.get("exports/p/1.zip")
    url = storage.signed_url("exports/p/1.zip", 60, clock.now())
    assert url.startswith("https://consent.example/export/download?key=exports%2Fp%2F1.zip&expires=")


def test_url_signer(clock):
    signer = UrlSigner(b"k" * 32)
    expires = int((clock.now() + timedelta(seconds=60)).timestamp())
    signature = signer.signature("exports/a.zip", expires)
    assert signer.verify("exports/a.zip", expires, signature, clock.now())
    assert not signer.verify("exports/b.zip", expires, signature, clock.now())
    assert not signer.verify("exports/a.zip", expires, signature, clock.now() + timedelta(seconds=61))
    with pytest.raises(ValueError):
        UrlSigner(b"short")


def test_local_storage_write_failure_leaves_no_temp_files(tmp_path, monkeypatch):
    storage = LocalFileStorage(tmp_path, UrlSigner(b"s" * 32), "http://x")

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr("app.storage.os.replace", fail)
    with pytest.raises(OSError):
        storage.put("audio/a.wav", b"x", "audio/wav")
    assert [p.name for p in (tmp_path / "audio").iterdir()] == []


@pytest.fixture
def s3():
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="voice")
        yield client


def test_s3_delete_purges_every_version(s3, clock):
    s3.put_bucket_versioning(Bucket="voice", VersioningConfiguration={"Status": "Enabled"})
    storage = S3Storage("voice", client=s3)
    storage.put("models/m.bin", b"v1", "application/octet-stream")
    storage.put("models/m.bin", b"v2", "application/octet-stream")
    storage.put("models/m.bin.keep", b"other", "application/octet-stream")
    assert storage.get("models/m.bin") == b"v2" and storage.exists("models/m.bin")
    storage.delete("models/m.bin")
    listing = s3.list_object_versions(Bucket="voice", Prefix="models/m.bin")
    remaining = [v["Key"] for v in listing.get("Versions", []) + listing.get("DeleteMarkers", [])]
    assert remaining == ["models/m.bin.keep"]  # no versions or delete markers of the deleted key
    assert not storage.exists("models/m.bin")
    storage.delete("models/m.bin")  # idempotent
    with pytest.raises(StorageError):
        storage.get("models/m.bin")
    assert "models/m.bin.keep" in storage.signed_url("models/m.bin.keep", 60, clock.now())


def test_s3_unversioned_and_errors(s3):
    storage = S3Storage("voice", client=s3)
    storage.put("audio/a.wav", b"x", "audio/wav")
    assert s3.head_object(Bucket="voice", Key="audio/a.wav")["ServerSideEncryption"] == "AES256"
    storage.delete("audio/a.wav")
    assert not storage.exists("audio/a.wav")
    broken = S3Storage("no-such-bucket", client=s3)
    with pytest.raises(s3.exceptions.ClientError):
        broken.exists("audio/a.wav")
