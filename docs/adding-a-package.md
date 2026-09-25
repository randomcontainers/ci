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

- `ARG BASE_IMAGE` and `ARG VERSION`, plus `ARG SOURCE_SHA256` when `upstream.artifact` is set. The build passes the base image pinned by digest, the version and the pinned sha256; the Dockerfile verifies the download against it.
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
  source: pypi                   # pypi, github-release or github-tag
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

### Upstream patterns

`tag-pattern` is matched against the whole tag or PyPI version, with ASCII digits only. Its groups go into `version-template`, and the result must pass `versioning`:

| Upstream | tag-pattern | version-template | Result |
|---|---|---|---|
| FFmpeg tags `n9.0.2`, not `n9.1-dev` | `'^n(\d+\.\d+(?:\.\d+)?)$'` | `'{1}'` | `9.0.2` (semver) |
| Ghostscript releases `gs10080` | `'^gs(\d+)(\d{2})(\d)$'` | `'{1}.{2}.{3}'` | `10.08.0` (loose) |
| ImageMagick releases `7.1.2-31` | `'^(7\.\d+\.\d+-\d+)$'` | `'{1}'` | `7.1.2-31` (loose) |
| yt-dlp on PyPI `2026.8.19` | `'^(\d{4}\.\d{2}\.\d{2})$'` | `'{1}'` | `2026.08.19` (calver) |

For a `github-tag` source, start the pattern with a literal prefix (`^n`) so the pass lists only matching tags.

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

URLs take `{version}`, `{nodots}`, `{major}` and `{minor}`. Set `sha256` by hand for the first version, and check it against upstream's signature or checksums file, or against the digest GitHub shows for the release asset (`gh release view <tag> --repo <owner>/<repo> --json assets`). For later versions the reconciler pins the hash only when it can check it the same way: `checksums` is set, GitHub recorded a digest for the asset, or `signature` is set and the Dockerfile verifies it.

`source-release: true` is required when `license` contains a GPL, LGPL or AGPL term: the release holds the corresponding source of the compiled binaries. `rc validate` reports an error when it is missing.

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
