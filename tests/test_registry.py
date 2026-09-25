import pytest

from fakes import DOCKER_LIST, FakeRegistry, digest_of
from rc import http
from rc.errors import RcError
from rc.http import Response
from rc.registry import Denied, NotFound, Registry, RegistryError, parse_reference


@pytest.mark.parametrize(
    "ref,registry,repo,tag,digest",
    [
        ("ubuntu:26.04", "docker.io", "library/ubuntu", "26.04", None),
        ("alpine", "docker.io", "library/alpine", None, None),
        ("denoland/deno:bin-2.9.7", "docker.io", "denoland/deno", "bin-2.9.7", None),
        ("ghcr.io/randomcontainers/ffmpeg:slim-ubuntu", "ghcr.io", "randomcontainers/ffmpeg", "slim-ubuntu", None),
        ("ghcr.io/randomcontainers/ffmpeg@sha256:" + "a" * 64, "ghcr.io", "randomcontainers/ffmpeg", None, "sha256:" + "a" * 64),
    ],
)
def test_parse_reference(ref, registry, repo, tag, digest):
    r = parse_reference(ref)
    assert (r.registry, r.repository, r.tag, r.digest) == (registry, repo, tag, digest)


@pytest.mark.parametrize("ref", ["quay.io/x/y:1", "ghcr.io/Upper/x:1", "ubuntu:bad tag", "x@sha256:zz"])
def test_parse_reference_rejects(ref):
    with pytest.raises(RcError):
        parse_reference(ref)


def test_index_digest_with_token_flow(fake):
    expected = fake.add_image("docker.io", "library/ubuntu", "26.04")
    reg = Registry(fake)
    assert reg.index_digest(parse_reference("ubuntu:26.04")) == expected
    methods = [(m, u.split("?")[0]) for m, u, _ in fake.requests]
    assert methods[0] == ("HEAD", "https://registry-1.docker.io/v2/library/ubuntu/manifests/26.04")
    assert methods[1] == ("GET", "https://auth.docker.io/token")
    # the token is cached for the next request
    reg.index_digest(parse_reference("ubuntu:26.04"))
    assert sum(1 for _, u, _ in fake.requests if "auth.docker.io" in u) == 1


def test_docker_manifest_list_counts_as_index(fake):
    expected = fake.add_image("docker.io", "library/alpine", "3.24", index_type=DOCKER_LIST)
    assert Registry(fake).index_digest(parse_reference("alpine:3.24")) == expected


def test_single_platform_base_is_refused(fake):
    fake.add_single("docker.io", "library/busybox", "1")
    with pytest.raises(RegistryError, match="multi-platform"):
        Registry(fake).index_digest(parse_reference("busybox:1"))


def test_config_labels_follow_blob_redirect_without_token(fake):
    fake.add_image("ghcr.io", "randomcontainers/ffmpeg", "slim-ubuntu", labels={"org.opencontainers.image.version": "9.0.2"})
    labels = Registry(fake).config_labels(parse_reference("ghcr.io/randomcontainers/ffmpeg:slim-ubuntu"))
    assert labels["org.opencontainers.image.version"] == "9.0.2"
    storage = [h for m, u, h in fake.requests if u.startswith("https://pkg-containers")]
    assert storage and all("Authorization" not in h for h in storage)


def test_annotations(fake):
    fake.add_image("ghcr.io", "randomcontainers/x", "latest", annotations={"org.opencontainers.image.version": "1.0.0"})
    assert Registry(fake).annotations(parse_reference("ghcr.io/randomcontainers/x:latest")) == {
        "org.opencontainers.image.version": "1.0.0"
    }


def test_probe(fake):
    fake.add_image("ghcr.io", "randomcontainers/ffmpeg", "slim-ubuntu")
    fake.add_image("ghcr.io", "randomcontainers/yt-dlp", "slim-ubuntu")
    fake.private.add(("ghcr.io", "randomcontainers/yt-dlp"))
    reg = Registry(fake)
    assert reg.probe(parse_reference("ghcr.io/randomcontainers/ffmpeg:slim-ubuntu")).ready
    assert reg.probe(parse_reference("ghcr.io/randomcontainers/ffmpeg:slim-alpine")).status == "unbuilt"
    assert reg.probe(parse_reference("ghcr.io/randomcontainers/yt-dlp:slim-ubuntu")).status == "unavailable"
    assert reg.probe(parse_reference("ghcr.io/randomcontainers/nothing:slim-ubuntu")).status == "unavailable"


def test_credentials_open_private_packages(fake):
    fake.add_image("ghcr.io", "randomcontainers/yt-dlp", "slim-ubuntu")
    fake.private.add(("ghcr.io", "randomcontainers/yt-dlp"))
    ref = parse_reference("ghcr.io/randomcontainers/yt-dlp:slim-ubuntu")
    with pytest.raises(Denied):
        Registry(fake).head(ref)
    Registry(fake, {"ghcr.io": ("bot", "secret")}).head(ref)
    token_requests = [h for m, u, h in fake.requests if u.startswith("https://ghcr.io/token")]
    assert token_requests[-1]["Authorization"].startswith("Basic ")
    with pytest.raises(NotFound):
        Registry(fake, {"ghcr.io": ("bot", "secret")}).head(parse_reference("ghcr.io/randomcontainers/yt-dlp:slim-alpine"))


def test_manifest_digest_mismatch_is_detected(fake):
    fake.add_image("ghcr.io", "randomcontainers/x", "1")
    wrong = "sha256:" + "b" * 64
    repo = fake.repos[("ghcr.io", "randomcontainers/x")]
    body, media_type = next(iter(repo["manifests"].values()))
    repo["manifests"][wrong] = (body, media_type)
    with pytest.raises(RegistryError, match="mismatch"):
        Registry(fake).manifest(parse_reference(f"ghcr.io/randomcontainers/x@{wrong}"))


class Hostile:
    def request(self, method, url, headers):
        if "/v2/" in url and "Authorization" not in headers:
            return Response(401, {"www-authenticate": 'Bearer realm="https://evil.example/token",service="x"'})
        return Response(200, {}, b'{"token":"t"}')


def test_token_realm_must_be_the_registry():
    with pytest.raises(RegistryError, match="unexpected token realm"):
        Registry(Hostile()).head(parse_reference("ghcr.io/randomcontainers/x:1"))


def test_http_follow_drops_headers():
    seen = []

    class T:
        def request(self, method, url, headers):
            seen.append((url, dict(headers)))
            if url == "https://a.example/x":
                return Response(302, {"location": "https://b.example/y"})
            return Response(200, {}, b"ok")

    assert http.get(T(), "https://a.example/x", {"Authorization": "Bearer t"}) == b"ok"
    assert seen[1] == ("https://b.example/y", {})


def test_digest_of_matches_fake():
    assert digest_of(b"") == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_fake_registry_is_isolated():
    assert FakeRegistry().repos == {}


def test_tags_follow_pagination(fake):
    fake.page_size = 2
    for tag in ("a", "b", "c", "d", "e"):
        fake.add_image("ghcr.io", "randomcontainers/x", tag)
    reg = Registry(fake)
    assert reg.tags(parse_reference("ghcr.io/randomcontainers/x")) == ["a", "b", "c", "d", "e"]


def test_tags_of_a_private_package(fake):
    fake.add_image("ghcr.io", "randomcontainers/x", "slim")
    fake.private.add(("ghcr.io", "randomcontainers/x"))
    with pytest.raises(Denied):
        Registry(fake).tags(parse_reference("ghcr.io/randomcontainers/x"))


def test_platform_sizes_skip_attestations(fake):
    digest = fake.add_image("ghcr.io", "randomcontainers/x", "slim")
    sizes = Registry(fake).platform_sizes(parse_reference(f"ghcr.io/randomcontainers/x@{digest}"))
    assert sorted(sizes) == ["linux/amd64", "linux/arm64"]


class Unreachable:
    """Answers the registry with a token challenge and times out everywhere else."""

    def __init__(self, fail_token: bool):
        self.fail_token = fail_token

    def request(self, method, url, headers, body=None):
        if self.fail_token and url.startswith("https://ghcr.io/v2/"):
            return Response(401, {"www-authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
        raise RcError(f"{method} {url}: timed out")


@pytest.mark.parametrize("fail_token", [False, True])
def test_network_failures_are_registry_errors(fail_token):
    with pytest.raises(RegistryError, match="timed out"):
        Registry(Unreachable(fail_token)).manifest(parse_reference("ghcr.io/randomcontainers/ffmpeg:slim-alpine"))
