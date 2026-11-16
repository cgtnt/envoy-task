# Envoy in-memory cache

An HTTP response cache registered implemented as an Envoy extension and registerd as `envoy.filters.http.simple_cache`. The task writeup is in [WRITEUP.md](WRITEUP.md).

## Directory layout

| Directory | Contents |
|---|---|
| [envoy/](envoy/) | Envoy fork, included as a Git submodule, containing the extension and its unit tests. |
| [experiments/](experiments/) | Configurations, scripts, and benchmark results: [01](experiments/01/README.md) tests warm hits, [02](experiments/02/README.md) tests unique misses without coalescing, and [profiling](experiments/profiling/README.md) contains hit/miss workloads for perf. |
| [integration/](integration/) | Python HTTP integration tests and their Envoy configuration. |

## Extension source (in envoy subdirectory)

- [Implementation](envoy/source/extensions/filters/http/simple_cache/)
- [Protobuf configuration](envoy/api/envoy/extensions/filters/http/simple_cache/v3/cache.proto)
- [Unit tests](envoy/test/extensions/filters/http/simple_cache/)


## Build

The extension is already registered in Envoy's build configuration, so building this fork includes it in the binary.

The following commands use the `fastbuild` configuration with two build jobs.

From the project root, initialize the submodule, then build and copy the executable:

```bash
git submodule update --init --recursive
cd envoy
```

```bash
SOURCE_DIR="$PWD/.." ENVOY_DOCKER_SOURCE_DIR=/source/envoy ./ci/run_envoy_docker.sh 'bazel build -c fastbuild --jobs=2 //source/exe:envoy-static && mkdir -p /source/bin && cp -L bazel-bin/source/exe/envoy-static /source/bin/envoy-coalescing'
```

## Run tests and experiments

Run the extension's unit tests from the `envoy/` directory on the host. This opens a
new container with the same source mount and build cache:

```bash
SOURCE_DIR="$PWD/.." ENVOY_DOCKER_SOURCE_DIR=/source/envoy ./ci/run_envoy_docker.sh 'bazel test -c fastbuild --jobs=2 --test_output=errors //test/extensions/filters/http/simple_cache/...'
```

Run the HTTP integration tests from the repository root on the host 

```bash
python3 integration/harness.py --envoy "./bin/envoy-coalescing"
```

For benchmarks and profiling, follow the `README.md` in the corresponding experiment directory.
