# Adding a package

A package is one repository in the organization, named after the tool. Its images are built by the reusable workflow in this repository, and the reconciler keeps it on the newest upstream release once it is listed in `packages.yml`.

## Files

| File | Contents |
|---|---|
| `package.yml` | what the image is, where new versions come from, tests, combos |
| `Dockerfile.ubuntu`, `Dockerfile.alpine` | one per distro in `distros.yml` |
| `runtime-deps.ubuntu`, `runtime-deps.alpine` | distro packages the tool needs at run time that the ELF scan cannot find (fonts, `python3`), one per line; may be empty |
| `.hadolint.yaml` | hadolint settings for the Dockerfiles |
| `.dockerignore` | the files the build may read: `*`, then a `!` line for each file the Dockerfiles copy |
| `requirements.lock` | Python packages only: the hash-locked install set |
| `scripts/metadata.py` | Python packages only: writes `source`, `buildinfo` and the license files of the venv (see [Python packages](#python-packages)) |
| `licenses/<name>-<version>/`, `scripts/bundled-licenses.py` | Python packages whose wheels link libraries without shipping their licenses: those license files for that exact version, and the script that collects them |
| `.github/workflows/build.yml` | the caller workflow from the [README](../README.md#using-the-build-workflow), unchanged |
| `.github/dependabot.yml`, `.github/zizmor.yml`, `LICENSE`, `README.md` | as in the existing package repositories |

Copy these from the package closest to the new one: `yt-dlp` or `streamlink` for a Python tool, `ghostscript` or `imagemagick` for a source tarball, `ffmpeg` for a signed tarball.

## Dockerfiles

`rc plan` and `rc validate --files` check the parts the build depends on:

- `ARG BASE_IMAGE` and `ARG VERSION`, plus `ARG SOURCE_SHA256` when `upstream.artifact` is set, `ARG SOURCE_COMMIT` when `upstream.git` is set, and `ARG <NAME>_SHA256` for each extra artifact the distro uses. The build passes the base image pinned by digest, the version and the pinned values; the Dockerfile verifies the download or checkout against them (see [Git tag builds](#git-tag-builds) and [Extra artifacts](#extra-artifacts)). An `ARG` without a default that the build does not pass is reported when it is used in a `--checksum=` flag or its name ends in `_SHA256`, `_COMMIT` or `_VERSION`.
- The last stage is named `slim`.
- Everything the package adds lives under `/usr/local`. A Python tool gets a venv at `/usr/local/lib/<name>` on the distro's `python3`, and only its entry points are linked into `/usr/local/bin`.
- `/usr/local/share/randomcontainers/<name>/` holds `runtime-deps` (every distro package the tool needs, without `ca-certificates` and `tini`), `version`, `source`, `licenses/` and any file a combo needs.
- The runtime stage installs `ca-certificates`, `tini` and exactly the lines of `runtime-deps`. It sets `LANG=C.UTF-8` and `XDG_CACHE_HOME=/cache`, creates `/work` and `/cache` with mode 1777, and ends with `WORKDIR /work`, `USER 1000:1000`, `ENTRYPOINT ["tini", "--", <entrypoint>]` and the `CMD` from `package.yml`.

`rc check-image` checks the built image against these rules, the image contract, in every build, before anything is pushed. Before that, the `plan` job runs hadolint on every `Dockerfile.*` with the repository's `.hadolint.yaml`, and a finding stops the build. Compiles use `make -j"${JOBS:-$(nproc)}"` with `ARG JOBS`.

Each line of `runtime-deps` is one package name: a Debian package name, all lowercase, on Ubuntu; an apk package name or a `so:` name on Alpine, where both may contain capitals (`libSvtAv1Enc`, `so:libSvtAv1Enc.so.4`). A combo build fails on any other line.

## package.yml

```yaml
name: streamlink                 # the repository name
title: Streamlink
summary: Extracts live streams from Twitch, YouTube and many other sites.   # at most 160 characters
homepage: https://streamlink.github.io/
license: BSD-2-Clause AND MIT    # SPDX, for everything the slim image adds
upstream:
  source: pypi                   # see "Version sources" below
  project: streamlink            # pypi; github-* use repository: owner/repo
  publisher: streamlink/streamlink   # pypi: the repository that publishes with trusted publishing
  tag-pattern: '^(\d+\.\d+\.\d+)$'
  version-template: '{1}'
  versioning: semver             # calver, semver or loose
  version: 8.6.1                 # updated by the reconciler
  cooldown: 24h                  # default 24h
image:
  entrypoint: [streamlink]
  cmd: [--help]
  env: {PYTHONUNBUFFERED: "1"}
test:                            # run with sh -euc in /work as the runner's user; VERSION is set
  - |
    streamlink --version > version.txt
    test "$(cat version.txt)" = "streamlink $VERSION"
examples:                        # every example with -v also needs --user
  - title: List the qualities a stream is available in
    command: docker run --rm ghcr.io/randomcontainers/streamlink "https://www.twitch.tv/<channel>"
combos:
  - with: [ffmpeg]
    default: true                # at most one; its image is the package's latest
    base: ffmpeg                 # member used as FROM; default is the first in with
    summary: Streamlink with FFmpeg, for streams that deliver video and audio separately.
    test:                        # required: exercise what the combo adds
      - ...
```

`rc validate --files` checks the whole file; `rc validate --catalog --packages-dir DIR` also checks it against the other packages (combo names, members, environment conflicts). `rc plan` makes the same checks against every listed package and does not build a combo that fails them. `rc tags --package-file package.yml` prints the tags each image will get.

### Version sources

| `source` | Keys | Versions from | Release date |
|---|---|---|---|
| `pypi` | `project`, `publisher` | the PyPI simple index | newest upload of the version's files |
| `github-release` | `repository` | the newest 100 releases, without drafts and pre-releases | `published_at` |
| `github-tag` | `repository` | tags | tagger date, or commit date for lightweight tags |
| `gitlab-release` | `project`, `server` | the newest 100 releases, without upcoming ones | `released_at`, or `created_at` when that is later |
| `forgejo-tag` | `repository`, `server` | tags, newest first, until a page lists one that is not newer than `version` (at most 500) | the later of the tagger date and the commit's `created`; for a lightweight tag, the commit's `created` |
| `html-index` | `url`, `pattern` | lines of a web page | the `date` group, or the artifact's `Last-Modified` |

`server` is optional and defaults to `https://gitlab.com` for `gitlab-release` and `https://codeberg.org` for `forgejo-tag`. Set it to another GitLab or Forgejo instance as an https URL without a trailing slash.

```yaml
upstream:                        # graphviz
  source: gitlab-release
  project: "4207231"             # the numeric project id, quoted, or its path: graphviz/graphviz
  tag-pattern: '^(\d+\.\d+\.\d+)$'
  versioning: semver
  version: 16.1.0
```

```yaml
upstream:                        # mkvtoolnix
  source: forgejo-tag
  server: https://codeberg.org
  repository: mbunkus/mkvtoolnix
  tag-pattern: '^release-(\d+\.\d+)$'
  versioning: loose
  version: "102.0"
```

`html-index` reads versions from a download page or a release list:

```yaml
upstream:                        # poppler, with the date on the page
  source: html-index
  url: https://poppler.freedesktop.org/releases.html
  pattern: 'href="poppler-(?P<version>\d+\.\d+\.\d+)\.tar\.xz">[^<]*</a> \((?P<date>[A-Za-z]{3} [A-Za-z]{3} \d{1,2}, \d{4})\)'
  versioning: loose
  version: 26.09.0
```

```yaml
upstream:                        # nmap, dated by the tarball's Last-Modified header
  source: html-index
  url: https://nmap.org/dist/
  pattern: 'href="nmap-(\d+\.\d+)\.tar\.bz2"'
  versioning: loose
  version: "7.991"
  artifact:
    url: https://nmap.org/dist/nmap-{version}.tar.bz2
    signature: https://nmap.org/dist/sigs/nmap-{version}.tar.bz2.asc
    sha256: <hex>
```

- `pattern` is a Python regular expression. It is matched against each line of the page on its own, with ASCII digits only. Lines longer than 16 KiB are skipped, and the page may be at most 4 MiB.
- The version is the group named `version`, or else the first group. It then goes through `tag-pattern` and `version-template` when they are set, and it must pass `versioning`. The only other group name allowed is `date`.
- The `date` group is read as `2026-09-03`, `2026-08-05 14:27`, ISO 8601 with a zone, `05-Aug-2026 14:27`, `Thu Sep 3, 2026`, `3 Sep 2026` or an HTTP date. A date without a zone counts as the latest moment it can stand for, in UTC-12 and at the end of the day when there is no time, so the cooldown never ends early. A version whose date cannot be read is refused.
- Without a `date` group the package needs `artifact`, and the `Last-Modified` header of the artifact URL is the release date.
- When a version is on several lines, its latest date counts.
- A pattern that matches no line is reported as an error in the tracking issue, because the page has most likely changed.

### Upstream patterns

`tag-pattern` is matched against the whole tag or PyPI version, with ASCII digits only. Its groups go into `version-template`, and the result must pass `versioning`:

| Upstream | tag-pattern | version-template | Result |
|---|---|---|---|
| FFmpeg tags `n9.0.2`, not `n9.1-dev` | `'^n(\d+\.\d+(?:\.\d+)?)$'` | `'{1}'` | `9.0.2` (semver) |
| Ghostscript releases `gs10080` | `'^gs(\d+)(\d{2})(\d)$'` | `'{1}.{2}.{3}'` | `10.08.0` (loose) |
| ImageMagick releases `7.1.2-31` | `'^(7\.\d+\.\d+-\d+)$'` | `'{1}'` | `7.1.2-31` (loose) |
| yt-dlp on PyPI `2026.8.19` | `'^(\d{4}\.\d{2}\.\d{2})$'` | `'{1}'` | `2026.08.19` (calver) |

For a `github-tag` source, start the pattern with a literal prefix (`^n`) so the pass lists only matching tags. For `html-index`, `tag-pattern` is optional and applies to the text of the version group.

### Tarball builds

```yaml
  artifact:
    url: https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs{nodots}/ghostscript-{version}.tar.xz
    checksums:                   # optional; checked when the version is bumped
      url: https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs{nodots}/SHA512SUMS
      algorithm: sha512
    signature: https://...tar.xz.asc   # optional; the Dockerfile verifies it
    sha256: <hex>                # pinned by the reconciler
source-release: true             # attach the verified tarball to a v<version> release
```

URLs take `{version}`, `{nodots}` (`1.18.0` becomes `1180`), `{underscored}` (`1_18_0`), `{major}` and `{minor}`. The checksums file may use GNU lines (`<hex>  <file>`), BSD lines (`SHA256 (<file>) = <hex>`) or OpenSSL lines (`SHA2-256(<file>)= <hex>`, also `SHA256(<file>)= <hex>`); only lines of `algorithm` count. Set `sha256` by hand for the first version, and check it against upstream's signature or checksums file, or against the digest GitHub shows for the release asset (`gh release view <tag> --repo <owner>/<repo> --json assets`). For later versions the reconciler pins the hash only when it can check it the same way: `checksums` is set, GitHub recorded a digest for the asset, or `signature` is set and the Dockerfile verifies it. Otherwise it downloads nothing and lists the new version under Upstream in the tracking issue, and the version is pinned by hand.

### Git tag builds

A package whose upstream publishes no release tarball is built from a checkout of the release tag:

```yaml
upstream:                        # whisper-cpp
  source: github-tag
  repository: ggml-org/whisper.cpp
  tag-pattern: '^v(\d+\.\d+\.\d+)$'
  versioning: semver
  version: 1.9.4
  git:
    url: https://github.com/ggml-org/whisper.cpp.git
    tag: v{version}              # the tag of each version, with the same placeholders as URLs
    commit: 927cfce34f31707e17f2bff35c349632fb9e2c3a   # pinned by the reconciler
```

A package has either `artifact` or `git`. For a bump, the reconciler reads the refs of `git.url` as `git clone` does and writes the commit the new tag points to (for an annotated tag, the commit it peels to) to `git.commit`, in the same commit as the version. A version whose tag is not at `git.url` yet waits, and is listed in the tracking issue when it still waits 7 days after its cooldown. With a `github-release`, `github-tag`, `gitlab-release` or `forgejo-tag` source, `git.tag` must give the tag the version was read from; otherwise the version is refused and listed in the tracking issue. Set `commit` by hand for the first version: `git ls-remote <url> 'refs/tags/<tag>^{}'` prints it for an annotated tag, `git ls-remote <url> refs/tags/<tag>` for a lightweight one.

The build passes the commit as `SOURCE_COMMIT`. The Dockerfile clones the tag and stops when it is another commit; the build stage needs `git` and `ca-certificates`:

```dockerfile
ARG VERSION
ARG SOURCE_COMMIT
RUN set -eu; \
    case "${SOURCE_COMMIT}" in \
      *[!0-9a-f]*|'') false ;; \
      *) test "${#SOURCE_COMMIT}" -eq 40 ;; \
    esac || { echo "SOURCE_COMMIT must be a full commit id" >&2; exit 1; }; \
    git -c advice.detachedHead=false clone --quiet --depth 1 --branch "v${VERSION}" \
      https://github.com/ggml-org/whisper.cpp.git /src/whisper.cpp; \
    test "$(git -C /src/whisper.cpp rev-parse HEAD)" = "${SOURCE_COMMIT}" \
      || { echo "tag v${VERSION} is not commit ${SOURCE_COMMIT}" >&2; exit 1; }
```

The first line of `source` is `<git.url>@<commit>`, such as `https://github.com/ggml-org/whisper.cpp.git@927cfce34f31707e17f2bff35c349632fb9e2c3a`, and with `source-release: true` the last line is `https://github.com/randomcontainers/<name>/releases/tag/v<version>`.

### Extra artifacts

Source files the build needs besides the main source go in `upstream.extra-artifacts`, keyed by name:

```yaml
upstream:                        # graphviz
  # source, project, artifact ...
  extra-artifacts:
    gts:
      version: 0.7.6             # pinned by hand
      url: https://downloads.sourceforge.net/project/gts/gts/{version}/gts-{version}.tar.gz
      sha256: 059c3e13e3e3b796d775ec9f96abdce8f2b3b5144df8514eda0cc12e13e8b81e
      distros: [alpine]          # optional; default: every distro
    docs:                        # no version: follows the package version
      url: https://example.org/tool-docs-{version}.tar.xz
      checksums: {url: 'https://example.org/{version}/SHA256SUMS', algorithm: sha256}
      signature: https://example.org/tool-docs-{version}.tar.xz.asc
      sha256: <hex>              # pinned by the reconciler
```

The name is lowercase letters and digits joined by `-` or `_`, at most 32 characters, and not `source`. Upper-cased with `_` for `-`, it names three build arguments that the build passes to the Dockerfile of each distro in `distros`: `GTS_VERSION`, `GTS_URL` (with the placeholders filled in) and `GTS_SHA256`. The Dockerfile declares at least `ARG GTS_SHA256` and verifies the download against it. Spell out the URL in `ADD`: hadolint reports `ADD ${GTS_URL}` as DL3020, since it cannot tell that the variable holds a URL.

```dockerfile
ARG GTS_VERSION
ARG GTS_SHA256
ADD --checksum=sha256:${GTS_SHA256} https://downloads.sourceforge.net/project/gts/gts/${GTS_VERSION}/gts-${GTS_VERSION}.tar.gz /src/
```

Each extra artifact adds its download URL to `source` on a line of its own, after the main source and before the release link, as in `imagemagick`.

- With `version`, the artifact is pinned by hand. Its URLs take that version, and the reconciler never changes it. To update it, change `version` and `sha256` together, after checking the new file against upstream's checksums or signature. The reconciler does not look for newer versions of it, and what the build compiles from it is not in the image's SBOM, so image scanners miss its advisories too. Follow its releases and security advisories upstream.
- Without `version`, the artifact follows the package version, so its `url` needs a placeholder. A new version waits until the artifact's URL and `signature` answer, and the reconciler pins its `sha256` in the same commit as the version, with the same checks as `artifact`: `checksums`, the digest GitHub records for a release asset of the same file name, or `signature`. `checksums` is only allowed here.

### Source releases

`source-release: true` attaches the verified sources to the `v<version>` release of the package repository after the images are published: the `artifact` tarball and its signature, or for a git tag build an archive of the pinned commit, and every extra artifact with its signature. The archive is `<repository>-<version>.tar.gz` (`whisper.cpp-1.9.4.tar.gz`), made by `git archive` after the release job fetched the tag again and found the pinned commit. It holds every file of that commit, whatever the repository's `.gitattributes` say, and no submodules. The file names of a release must differ from each other.

A rebuild of the same version uploads only the files the release does not have yet. When it adds one, it rewrites the notes to list the files of that build. Files of earlier builds stay attached: after a new `version` of a hand-pinned extra artifact, the release holds both files. A file whose name is already attached is never replaced.

`source-release: true` is required when `license` contains a GPL, LGPL or AGPL term and the build compiles `artifact`, `git` or `extra-artifacts`: the release holds the corresponding source of the compiled binaries. `rc validate` reports an error when it is missing.

### Python packages

The Dockerfile installs `requirements.lock` with `pip install --require-hashes --no-deps --only-binary=:all:`. The reconciler regenerates the lock on every version bump and once a week, with the command named on the file's second line. Create the first lock with `rc lock`, which needs uv and writes that line:

```sh
rc lock requirements.lock --requirement 'streamlink==8.6.1' --python-version 3.14
```

The second line then reads `#    echo 'streamlink==8.6.1' | uv pip compile --universal ... - -o requirements.lock`. The requirement may name extras (`'yt-dlp[default,curl-cffi]==2026.08.19'`), and `--no-emit NAME` leaves a package out of the file. `--python-version` is the oldest `python3` of the distros. A combo that installs more Python packages names its own lock file in `extras.<distro>.requirements`; create it the same way. The reconciler regenerates it together with `requirements.lock`. The combo image installs it into the package's venv, appends each wheel as `<url>#sha256=<hex>` to the package's `source` file, and copies each wheel's license files to `licenses/<name>/`. A wheel without license files fails the build. To regenerate a lock by hand at the version in `package.yml`, run `rc lock requirements.lock`.

Some wheels link libraries without shipping their license files, such as lxml in `streamlink` and curl_cffi in `yt-dlp`. The package repository then keeps those files in `licenses/<name>-<version>/`, written by `scripts/bundled-licenses.py`, and `scripts/metadata.py` copies them into the image and fails the build when the installed version is another one. The reconciler does not commit a lock that moves such a dependency to another version; the tracking issue lists it under Upstream. Update both by hand in the package repository and commit them together, after which the next pass makes the update it held back:

```sh
rc lock requirements.lock
python3 scripts/bundled-licenses.py
```

Dependencies released in the last 7 days are left out of the lock, but the package itself is not. When the package pins a dependency exactly and releases both on the same day, as yt-dlp does with `yt-dlp-ejs`, a new version cannot be locked until that dependency is a week old. Exempt such a dependency with `--exempt NAME`; it is kept in the header as `--exclude-newer-package NAME=false`, and `rc lock requirements.lock --exempt NAME` adds it to an existing lock.

## Listing the package

1. A maintainer creates the repository. Push the files above to it. Pull requests build and test both distros on both platforms without publishing, and the first build that publishes runs on the push to `main`.
2. Open a pull request here that adds the name to `packages.yml`. Review the new repository's `package.yml`, Dockerfiles and workflow as part of it: from then on the reconciler commits to it and dispatches its builds.

Images on ghcr.io start private, and the tracking issue lists the package as waiting until a maintainer makes it public. Once the package is listed, the reconciler creates the combo repositories it declares, writes their files on the pass after that, and dispatches their first builds once every member can be pulled.
