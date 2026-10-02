# How the images are built and kept current

## Repositories

| Repository | Holds | Writes to it |
|---|---|---|
| `ci` | `rc`, the reusable build workflow, `distros.yml`, `packages.yml`, `status/catalog.json`, the reconciler | maintainers; `status/` by the reconcile workflow |
| `yt-dlp`, `ffmpeg`, ... | `package.yml`, `Dockerfile.ubuntu`, `Dockerfile.alpine`, `runtime-deps.<distro>`, lock files, README, caller workflow | maintainers; version bumps and lock updates by the App |
| `yt-dlp-ffmpeg`, ... | files generated from the owner's `package.yml` | the App only |
| `.github` | the organization profile and the contributing and security policies | maintainers |

Each image is pushed by the workflow of the repository that owns it, with that repository's `GITHUB_TOKEN`. GitHub App tokens cannot push to ghcr.io, so the App never touches images. It creates combo repositories, commits files and dispatches builds.

## The build

`build.yml` in a package or combo repository calls `randomcontainers/ci/.github/workflows/build.yml@v1`. The jobs are described in the [README](../README.md#using-the-build-workflow). Everything the reconciler later compares ends up in the annotations of each published index:

| Annotation | Value |
|---|---|
| `org.opencontainers.image.version` | package version; for combos the owner's version |
| `org.opencontainers.image.revision` | commit of the repository that was built |
| `org.opencontainers.image.base.digest` | index digest of the base: the distro image for slim images, the base member's `slim-<distro>` index for default and combo images |
| `org.opencontainers.image.created` | build time |
| `com.randomcontainers.members` | `{"<name>": {"version": ..., "digest": ...}}` for every member built from a published slim image |
| `com.randomcontainers.distro-version` | `26.04`, `3.24`, ... |

A package's default image leaves the package itself out of `members`, since it is built in the same run.

A default or combo image runs the tests of every member. For a member taken from its published `slim-<distro>` image, `rc plan` reads the member's `package.yml` at the commit in that image's `org.opencontainers.image.revision` label, so its tests, environment and entrypoint match the image. A test added to a member applies to these images once the member's slim image is rebuilt.

## A reconcile pass

`tick.yml` dispatches `reconcile.yml` every 15 minutes. Passes never overlap (concurrency group `reconcile`). A pass keeps no state of its own: it reads the repositories, the upstreams and the registry, and a pass that finds everything current does nothing.

### 1. Definitions

`packages.yml` lists the package repositories. For each, the pass reads the head of `main` and `package.yml` at that commit. Files are read from raw.githubusercontent.com at the commit id, which does not count against the API rate limit and cannot return stale content. An invalid `package.yml` is reported and that package is skipped.

### 2. Upstream

The newest upstream version comes from:

| `upstream.source` | Where | Release date |
|---|---|---|
| `pypi` | the PyPI JSON simple index | newest upload time of the version's files |
| `github-release` | the latest 100 releases, without drafts and pre-releases | `published_at` |
| `github-tag` | all tags starting with the literal prefix of `tag-pattern` | tagger date, or the commit date for lightweight tags |
| `gitlab-release` | the latest 100 releases of a GitLab project, without upcoming releases | `released_at`, or `created_at` when that is later, since `released_at` can be set to a past date |
| `forgejo-tag` | tags from the Forgejo API (Codeberg by default), newest first, in pages of 50 up to the first page with a tag no newer than the current version, at most 10 pages | for an annotated tag the later of its tagger date and `commit.created`, else `commit.created` |
| `html-index` | the version group of `pattern`, matched against each line of the page at `url` | the `date` group, or the `Last-Modified` header of `artifact.url` |

Each tag, PyPI version or version read from a page must match `tag-pattern` (ASCII digits only), and the version built from `version-template` must pass the `versioning` scheme. Anything else is ignored, so no upstream string reaches a tag, a file or a commit message without matching a strict pattern. PyPI normalizes `2026.08.19` to `2026.8.19`; for `calver` packages the month and day are padded back before the pattern is applied.

An `html-index` page is read once per pass, at most 4 MiB, and lines longer than 16 KiB are skipped, which bounds the work of the regular expression. A page on which `pattern` matches no line is reported as an error. Dates on a page are read in a few fixed formats; a date without a zone counts as the latest moment it can stand for, so a release is never taken before its cooldown. A version with a `date` group that cannot be read is refused.

The newest candidate above the current version is taken when:

- it is older than `upstream.cooldown` (default `24h`);
- on PyPI, it is not yanked, it has a pure-Python wheel or wheels for glibc and musl on x86_64 and aarch64, and every file has a PEP 740 provenance from the PyPI Integrity API whose publisher is the GitHub repository in `upstream.publisher`;
- for tarball builds, `artifact.url` and `artifact.signature` answer HTTP 200, or are assets of the release; the same holds for each extra artifact without a `version`;
- for git tag builds, the tag that `git.tag` gives is at `git.url`, and with a `github-*`, `gitlab-release` or `forgejo-tag` source it is the tag the version was read from (otherwise the version is refused).

A candidate that is too new is skipped and the next older one is tried. A refused candidate (wrong publisher, no provenance, missing wheels) is listed in the tracking issue. So is a candidate that still waits for its tag or files more than 7 days after its cooldown ended, which usually means a wrong `git.url`, `git.tag` or URL template. An undated `html-index` version is not listed this way, since its release date is only known once its artifact answers.

A timeout, a failed connection or an HTTP 429 or 5xx answer from an upstream, PyPI or a git server is only logged, and the next pass tries again. Other HTTP errors, unreadable replies and an `html-index` page that no longer matches are listed in the tracking issue as errors.

For the bump, the tarball is downloaded once. Its sha256 is written to `artifact.sha256`, after a match against `artifact.checksums` when that is set (GNU, BSD or OpenSSL lines of the configured algorithm), and against the digest GitHub records for release assets. At least one of these checks has to run unless the package has `artifact.signature`, which its Dockerfile verifies. When none can, nothing is downloaded and the tracking issue lists the version under Upstream as needing a hand pin, until someone compares the file with upstream and commits the version and its sha256. Extra artifacts without a `version` are downloaded and pinned the same way; those with a `version` are pinned by hand and left alone. For a git tag build, the pass reads the refs of `git.url` from its `info/refs` advertisement, the same list `git clone` starts from, and writes the commit the tag points to (peeled, for an annotated tag) to `git.commit`. Python packages get their lock files regenerated by `uv pip compile` with the options named in each lock's header (see [adding a package](adding-a-package.md#python-packages)). A new `requirements.lock` that pins a dependency at another version than its directory in `licenses/` is not committed; the tracking issue says so under Upstream. `package.yml` is edited as text, keeping comments, and the edit is refused unless the parsed result differs only in `upstream.version`, `upstream.artifact.sha256`, `upstream.git.commit` and the `sha256` of extra artifacts without a `version`. All changed files go into one commit, "Update yt-dlp to 2026.09.20". The pass only plans it: a later step of the same job makes it with the Git Data API as the App (see [Tokens](#tokens)). The branch is moved without force, so a commit that raced the pass makes the update fail and the next pass tries again.

Once the newest commit to a package's `requirements.lock` is 7 days old, the pass regenerates the locks at the current version and commits "Update yt-dlp dependencies" if anything changed, with the same `licenses/` check.

A commit to a package repository starts its build through the `push` trigger, so the pass does not dispatch one on top.

### 3. Combo repositories

Every combo declared in any `package.yml` gets a repository named after it. The pass renders its files (`combo.yml`, both Dockerfiles, README, `LICENSE`, `.hadolint.yaml`, the caller workflow and `.github/zizmor.yml`), compares their git blob ids with the tree at the head of `main`, and commits the files that differ. The files contain no package versions, so a version bump changes nothing here. A combo repository's workflow has no `push` trigger, so the build of such a commit is dispatched by the next pass.

A missing repository is created with a README, the combo summary as description and no homepage. The next pass mints a token that covers the new repository, commits the generated files and asks for the repository's topics. Until that commit succeeds, a repository that has only the initial README and the description or topics set by the reconciler counts as newly created, so a failed first commit is retried on the following pass. The pass after the commit dispatches the first build. A managed combo repository that lacks one of its topics (`randomcontainers`, `container-image`, `docker` and the member names) gets them back; other topics are kept.

An existing repository is written only when its `combo.yml` names the same owner. The pass never deletes a repository. A combo repository that is no longer declared is listed in the tracking issue, and combos that `rc validate --catalog` rejects, such as two with the same members, are neither created nor built. `rc plan` makes the same checks, so a build started by hand refuses such a combo too; for a package's default combo it builds only the slim images, and the pass lists the default image as blocked.

Creating a repository and setting topics need the App's Administration permission. The pass itself does not have it: it writes those changes to a file, and a later step of the same job mints a token with only Administration and runs `rc setup-repos` on the file. That step reads no upstream data, and it checks each request again (a valid, unreserved name, a description of at most 160 characters, valid topics).

### 4. Published images

For each repository and distro the pass works out what the image should be and reads the annotations of what is published:

| Image | Tag read | Should be |
|---|---|---|
| package slim | `slim-<distro>` | `package.yml` version, head of `main`, base image index digest |
| package default | `<distro>` | the same version and commit, the base member's slim digest, the members' slim versions and digests |
| combo | `<distro>` | the owner's published slim version, head of the combo repository, the base member's slim digest, every member's slim version and digest |

A member counts only when its `slim-<distro>` image can be pulled anonymously, has a valid version and was built on the current distro release, which is the same check `rc plan` makes. Otherwise the default or combo image on that distro is listed as blocked and not built.

An image differs when any value differs, when the tag does not exist, or when it was built more than 7 days ago, which picks up distro security updates. A package whose slim images are current but whose default image differs is rebuilt with `default-only: true`.

An image that cannot be pulled anonymously is never treated as out of date. When one of the last three successful builds of `main` in its repository published an index (a `Publish` job whose `Create index` step ran), it is listed as waiting to be made public; ghcr.io creates every package private and only the web UI can change that. Otherwise it counts as not built yet and is dispatched. An image the registry did not answer for in this pass is not compared at all.

### 5. Dispatch and backoff

A dispatch carries `want`, a hash of the repository, the scope (`all`, `default` or `combo`) and the desired values of every target in that scope. The caller workflow names the run `Build <want>`, so the pass finds earlier attempts in the runs API without storing anything. It counts only runs of `main` started by a push or a dispatch, since a pull request run never publishes and one from a fork can use any branch name and title:

- any queued or running build: no dispatch;
- no earlier dispatched run with that title: dispatch;
- one, two, or three or more earlier runs: dispatch again only 6, 12 or 24 hours after the last one ended.

A run that failed, or finished while the images still differ, is listed in the tracking issue with its next attempt time.

### 6. Health

- When `tick.yml` is not `active`, for example `disabled_inactivity` after 60 days without activity in this repository, the tracking issue says so.
- When `latest` of a package is at another version than `package.yml` and the head of `main` is more than 6 hours old, the package is marked `stale` in the catalog and listed in the tracking issue.

### 7. Catalog and tracking issue

`rc reconcile --output-dir` writes `catalog.json`, `issue.md` and `issue-state`. The `publish-status` job then commits `status/catalog.json` when its content changed and updates the issue titled "Reconciler status": opened when something needs attention, closed when nothing does. That job uses this repository's `GITHUB_TOKEN` and installs nothing.

`status/catalog.json` is the machine-readable list of the published images and their tags. Its format (schema 1):

```
schema, generated, registry {ghcr}, distros [{id, version, default}], platforms
images[]:
  package: name, kind, title, summary, homepage, repository, license, version, versioning,
           examples [{title, command}], source_release,
           default {kind: slim} or {kind: combo, combo, with, base, summary, license},
           combos, public, default_ready, stale, updated, variants
  combo:   name, kind, owner, members, base, title, summary, repository, license, version,
           public, updated, variants
variants[]: flavour (default or slim), distro, version, tags, digest, size {platform: bytes}
```

`version` is the version in `package.yml`; each variant has the published version. `source_release` is true when the build attaches the upstream source to the `v<version>` release of the package repository. `base` names the member whose slim image a combined image is built on; the other members are copied onto it. `public` is true when the image can be pulled anonymously. `generated` and `updated` come from the build times of the published images, not the time of the pass, so a pass that finds nothing new leaves the file unchanged. Sizes are the compressed size of config and layers per platform and are reused from the previous catalog for an unchanged digest. When an image cannot be read in a pass, or its `package.yml` cannot, its previous entry is kept, and so are the entries of the combos and default images it is a member of.

## Tokens

| Token | Used for | Permissions |
|---|---|---|
| package or combo `GITHUB_TOKEN` | building and pushing its own image, the source release, code scanning | set by the caller workflow, narrowed per job |
| App token `RC_GITHUB_TOKEN` | reading repositories and runs, dispatching builds | Actions write, Contents read |
| App token `RC_WRITE_TOKEN` | commits, in the `rc apply-commits` step; minted only when a pass plans commits, for those repositories only | Contents and Workflows write |
| App token `RC_ADMIN_TOKEN` | new combo repositories and their topics, in the `rc setup-repos` step; minted only when a pass asks for such a change | Administration write |
| ci `GITHUB_TOKEN` in `reconcile` | the state of `tick.yml` | Actions read |
| ci `GITHUB_TOKEN` in `publish-status` | `status/catalog.json` and the tracking issue | Contents and Issues write |

The App's private key is a secret of the `bot` environment of this repository, which only `main` can use. The `reconcile` job uses the environment with `deployment: false`: GitHub still applies the branch rule and provides the secret, but records no deployment for each pass. `publish-status` needs no secret and runs without it. The App is installed on the package repositories and the combo repositories it creates, and not on this repository.

The reconciler runs from `main`, while builds run the ci version behind `v1`, so a change to how labels or annotations are written is released right after it is merged.

`rc plan` refuses `package.yml` keys it does not know, so a new key reaches builds only when `v1` moves. After such a change, wait for `test.yml` to pass on `main` and move `v1` to that commit before a package repository uses the key or is added to `packages.yml`. Until then the `plan` job of its builds fails.

## Security rules the workflows follow

- Every third-party action is pinned by commit with its version in a comment, and Dependabot updates them. Callers use `build.yml@v1`, a tag only organization admins can move.
- Workflows start from `permissions: {}` and each job asks for what it uses. Checkouts do not keep credentials. There is no `pull_request_target` and no cache in a job that publishes.
- Images are pushed only after `rc check-image` and the tests pass, only from `main`, and only while the run's commit is still the head of `main`. Floating tags never move to an older version.
- The App key exists only in the `bot` environment of this repository. Apart from each repository's `GITHUB_TOKEN`, it is the only secret the workflows use.
- The reconcile job, which holds the App tokens, installs only hash-pinned Python packages (PyYAML and uv) and runs no upstream code: resolving locks with `--only-binary :all:` never runs a package's build scripts. The `rc reconcile` step, which parses upstream and registry data, gets only the token for reads and dispatches. Commits and repository changes are made by later steps that read nothing but the file the pass wrote.
- Upstream values reach a shell only through `env:`, and only after matching the patterns in `rc`.
- The release job fetches a git tag only over https, with git's system and global configuration ignored, and checks the URL, tag and commit from the plan against the patterns in `rc` again first.
- `actionlint` and `zizmor` run on every change to this repository, and `shellcheck` checks the `run:` steps of the workflows (through actionlint) and of `setup-rc`. `hadolint` checks the generated combo Dockerfiles here, and every package and combo repository's Dockerfiles in the `plan` job of each build.

## Pinned tools

- Actions are pinned by commit and updated by Dependabot once a week, 7 days after a release.
- The images in `build.yml` (`moby/buildkit`, `hadolint/hadolint`) and `test.yml` (`hadolint/hadolint`, `rhysd/actionlint`) are pinned by digest and updated by hand. The two hadolint pins must match; a test checks that. Look up the digest of a new tag with `docker buildx imagetools inspect <image>:<tag>`.
- The Dockerfile frontend, `docker/dockerfile`, is pinned by digest on the `# syntax=` line of every package Dockerfile and in `DOCKERFILE_SYNTAX` in `src/rc/render.py`, which writes the combo Dockerfiles. When `moby/buildkit` is bumped, move all of them to the frontend release that ships with it; a test checks that `render.py` keeps a digest.
- The reconciler's uv is pinned with hashes in `requirements-reconcile.txt`. Change the version in `requirements-reconcile.in` and regenerate the file with the command on its second line.
- `distros.yml` holds the base images. After a change, the next pass rebuilds every slim image; default and combo images are blocked until all their members are on the new release, then rebuilt. Builds read `distros.yml` from the commit behind `v1`, so the change is released right after it is merged. The combo READMEs follow `distros.yml` on their own. Update these in the other repositories at the same time:
  - in every package repository, the `ARG BASE_IMAGE` default of each `Dockerfile.<distro>`, and in `README.md` the tag table and the sentence naming the releases the images are built on;
  - in `ffmpeg` and `ghostscript`, the Alpine aports branch (`3.24-stable`) and distfiles (`v3.24`) links in `README.md`;
  - in `.github`, `CONTRIBUTING.md`, `SECURITY.md` and `profile/README.md`.

  `rc validate --files` reports a `BASE_IMAGE` default or a release named in `README.md` that differs from `distros.yml`, and `rc plan` shows the same as a warning in every package build.
