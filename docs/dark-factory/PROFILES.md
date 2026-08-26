# Dark Factory — language & stack support (no profiles)

> **Decision:** the Dark Factory does **not** use per-language "profiles" in platform config.
> Language support is decoupled into the **coder image** (toolchains) and the **target repo**
> (build/test discovered from marker files) — devs control it, the platform stays generic.
> An earlier design added a `stackProfiles` map + a `dark-factory-<name>` label; it was removed as
> redundant (the coder already auto-detects, and `detect-deployable` already classifies verification).

## How a stack is supported — three decoupled layers

1. **Toolchains → the coder image.** `examples/dark-factory/coder/Dockerfile` is a generic image that
   carries `git`, `node`, `python3`, `go`. To support Java/Rust/etc., add the toolchain **to the
   image** (or maintain a variant image) — not to platform config. This is the dev/image concern.
   *(The trusted `deploy-test` image separately carries `kubectl` + `terraform`.)*

2. **Build/test → discovered from the repo.** The coder picks the build/test command from the repo's
   own marker files — devs control it by their repo layout, with a `Makefile` as the explicit override:

   | Marker in the repo | Coder runs |
   |---|---|
   | `Makefile` with a `test:` target | `make test` *(explicit dev override — checked first)* |
   | `package.json` | `npm install` + `npm test` |
   | `go.mod` | `go test ./...` |
   | `pyproject.toml` / `setup.py` / `requirements.txt` | `pytest -q` |
   | `Cargo.toml` | `cargo test` |
   | `pom.xml` | `mvn -q test` |
   | `build.gradle[.kts]` | `./gradlew test` |
   | *(none — e.g. Terraform/config change)* | skipped (no unit suite; `deploy-test` still validates) |

3. **Verification kind → auto-detected.** `detect-deployable` classifies the changed files and
   `deploy-test` runs the right tool — `*.tf` → `terraform validate`; `Chart.yaml`/`k8s/`/`Dockerfile`
   → deploy into an ephemeral namespace. No label, no config.

## Why no profiles (the reasoning)

- The one thing a profile added over auto-detection was a `scaffoldHint` string — and the **issue text
  already states the stack** ("Terraform for an S3 bucket", "a Spring Boot service"), which the coder
  reads directly. The build/test commands and the verify kind were **already** covered by marker-file
  detection and `detect-deployable`.
- Keeping per-language build/test in Helm values meant the platform had to know every language — the
  opposite of generic. Pushing it to the **image** (toolchains) and the **repo** (marker files) keeps
  the platform language-agnostic and puts control where it belongs: with the devs/repo.

## Adding a new language

1. Add its toolchain to the coder image (`coder/Dockerfile`) — e.g. `apk add openjdk maven`.
2. Ensure `buildAndTest()`'s marker list covers it (most already are; add a marker if exotic).
3. That's it — no values, no label, no pipeline change. A repo `Makefile test` target works for
   anything without even a marker.

## Greenfield note

For a brand-new/empty repo with no marker files yet, the coder relies on the **issue text** to know
the stack (usually sufficient) and creates the idiomatic project (which then has the marker files a
re-run/iterate would detect). If a repo wants to be explicit, a committed `Makefile` (`build`/`test`
targets) is the clean, dev-owned contract.
