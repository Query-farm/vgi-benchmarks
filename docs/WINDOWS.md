# Running the VGI benchmarks on Windows

The harness runs in-process via `haybarn`, so the suite itself is portable. The
moving parts on Windows are (a) which **transport** works and (b) building the
per-language **example workers**, which aren't published as runnable packages.

## Transport on Windows

| Transport | Windows | Notes |
|-----------|:-------:|-------|
| `subprocess` (stdio pipe) | ✅ | **Use this.** Same wire path, all four languages. |
| `tcp://` | ✅ | Works (VGI ≥ the `feat/windows-tcp-transport` merge) but ~2–3× slower than the pipe — TCP-loopback overhead, not the process model. |
| `http` | ✅ | Works; HTTP framing makes it much slower for this workload. |
| `launch:` / `unix://` / shared-memory (`launch-shm`) | ❌ | POSIX-only (AF_UNIX / `flock` launcher / `shm_open`). |

So on Windows every language is benchmarked over **`subprocess`**, whereas the
macOS runs use `launch-shm` (shared memory) for the compiled languages — keep
that in mind when comparing: the Windows numbers carry no shared-memory path and
are a conservative floor.

## Prerequisites

- **Python** with `haybarn` (the suite's venv: `uv sync`).
- **Go** (`winget install GoLang.Go`) **+ a C compiler for cgo** — the Go worker
  embeds the DuckDB Go bindings. Install mingw, e.g.
  `winget install BrechtSanders.WinLibs.POSIX.UCRT`, and build with
  `CGO_ENABLED=1` and `gcc` on `PATH`.
- **Rust** (`rustup`, `winget install Rustlang.Rustup`).
- **JDK 21** (`winget install Microsoft.OpenJDK.21`).

## Build the example workers from source

The published packages don't ship runnable example workers (`go install` is
blocked by `replace` directives; the Rust worker binary isn't on crates.io), so
clone and build. Put them where the adapters expect (`~/Development/...`, `~/vgi-java`).

**Python** — `uv sync` in `~/Development/vgi-python` provides `vgi-fixture-worker`.

**Go**
```sh
git clone https://github.com/Query-farm/vgi-go ~/Development/vgi-go
cd ~/Development/vgi-go
set CGO_ENABLED=1            # PATH must include the mingw gcc
go build -o vgi-example-worker-go.exe ./cmd/vgi-example-worker
```

**Rust**
```sh
git clone https://github.com/Query-farm/vgi-rust ~/Development/vgi-rust
cargo build --release --bin vgi-example-worker --manifest-path ~/Development/vgi-rust/Cargo.toml
```

**Java** — build the app, then the tiny native launcher (see below):
```sh
git clone https://github.com/Query-farm/vgi-java ~/vgi-java
cd ~/vgi-java
gradlew.bat :vgi-example-worker:installDist
gcc scripts/windows/vgi-java-launcher.c -o ~/vgi-java/vgi-java-worker.exe
```
The Java worker needs a native launcher (`scripts/windows/vgi-java-launcher.c`)
because VGI's LOCATION is whitespace-tokenized (the space in the JDK path breaks
a direct `java …` command), the app jar has no `Main-Class`, and `_execl` would
orphan the JVM. The launcher reads its config from the environment, so set:
```sh
set VGI_JAVA_EXE=C:\Program Files\Microsoft\jdk-21.x\bin\java.exe
set VGI_JAVA_CP=C:\Users\you\vgi-java\vgi-example-worker\build\install\vgi-example-worker\lib\*
```

## Run

```sh
uv run vgi-bench run --cases scalar_multiply --transports subprocess \
    --languages python,go,rust,java --threads 1,4,8
```
The adapters carry a `subprocess` transport for each language; `adapter.py`
appends `.exe` to the worker path on Windows automatically.

### Benchmarking an unreleased VGI build

To load a locally-built `vgi.duckdb_extension` (e.g. an unmerged Windows fix)
instead of the community release, set `VGI_BENCH_LOCAL_EXT` to its path. The
harness then `LOAD`s it by path (allowing the unsigned + version-mismatched
extension), skipping `FORCE INSTALL`:
```sh
set VGI_BENCH_LOCAL_EXT=C:\Users\you\Development\vgi\build\release\extension\vgi\vgi.duckdb_extension
```

## Reference numbers (Windows 11, Intel Core Ultra 7 265, subprocess)

`sum(multiply(n, 2))` over 10M BIGINT rows, median rows/s, threads 1 / 4 / 8:

| Language | 1 | 4 | 8 |
|----------|---:|---:|---:|
| Python | 25M | 81M | 120M |
| Go     | 28M | 100M | 194M |
| Rust   | 30M | 111M | 185M |
| Java   | 33M | 109M | 96M* |

\* Java's subprocess pool spawns one JVM per worker; at 8 JVMs heap/GC pressure
caps it. A single-JVM `tcp://` server scales worse, not better (loopback
overhead) — so subprocess remains the fastest Java path on Windows.
