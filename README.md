# Hugging Face network benchmark

Run the same model-download and network tests on login and compute nodes across clusters, then compare their saved results. The script runs on the current host; it does not SSH to other nodes or submit jobs automatically.

## What it measures

- Interface link speeds and drivers, IPv4/IPv6 routes, addresses, and route lookups for Hugging Face.
- A complete, fresh Hugging Face model snapshot, pinned to its resolved commit, with file-size verification and elapsed time.
- A separate identity-encoded HTTP range transfer, streamed and discarded, to distinguish ordinary transfer speed from Xet's effective file throughput.
- Host-wide network-counter changes, software versions, and visible cgroup memory limits.
- Markdown and JSON reports, plus a comparison table with speedup relative to the first report.

No root privileges are required. Missing `ip`/`ethtool`, unreadable sysfs entries, or denied network inspection are recorded as unavailable rather than stopping a download. Network benchmarks need outbound HTTPS access. Linux provides the most complete interface and cgroup information; other platforms retain download timings with less network detail.

## Install

Use Python 3.10 or newer. To reproduce the original tests, use the pinned Hugging Face versions in `requirements.txt` on every host:

```bash
git clone git@github.com:davidecarlson/hf-network-benchmark.git
cd hf-network-benchmark
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On clusters that provide a Python module, load it before creating the environment. If the repository/environment resides on shared storage, it can be reused on compatible nodes of that cluster. On separate clusters, clone and install separately.

## Inspect without downloading

```bash
python hf_network_benchmark.py inspect --output network.json
```

This resolves Hugging Face's current addresses and records routes, but downloads no model data. `--host` selects another destination; `--no-ethtool` skips that optional tool. Unavailable fields remain null or include a command error.

If proxy environment variables are set, the report records their host/port and route lookups without proxy credentials. Proxy and `NO_PROXY` settings can change the actual request path, so a direct route lookup alone does not prove which path HTTP used.

## Run identical tests on each host

For the exact model revision used in the original conversation:

```bash
python hf_network_benchmark.py run \
  --label cluster-a-login \
  --model Qwen/Qwen3.8-27B \
  --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
  --output /path/to/scratch/hf-benchmarks \
  --workers 2 --range-gets 4 --http-test-mib 1024
```

Run the same command **inside your compute-node allocation**, changing the label to `cluster-a-compute`. Repeat on another cluster with labels such as `cluster-b-login` and `cluster-b-compute`. Use the same model revision, selected files, versions, concurrency, and HTTP test size for useful comparisons. Node allocation commands differ by cluster; an example [Slurm script](examples/slurm-benchmark.sbatch) is provided for adaptation.

The model is about **55.6 GB**, and the HTTP test transfers an additional **1 GiB**. Each invocation generates a unique run directory and keeps its downloaded model and isolated cache. Choose scratch storage with enough free space for every retained run. The filesystem free-space check cannot detect all user/project quotas. Use `--model-dir /new/path` to store the model elsewhere; an existing directory is rejected so cached files cannot inflate the benchmark. There is no resume mode.

For another model, change `--model` and `--revision`. If no revision is supplied, the script resolves the repository's current commit and records it; copy that exact commit into subsequent runs. For private or gated models, export `HF_TOKEN` in your shell before running. A fresh cache means tokens stored only in your usual Hugging Face cache are not reused. Tokens and signed download URLs are not written to the JSON reports.

## Smaller tests

A network-only test avoids another full model download:

```bash
python hf_network_benchmark.py run \
  --label cluster-a-compute-http-only \
  --model Qwen/Qwen3.8-27B \
  --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
  --output /path/to/scratch/hf-benchmarks \
  --skip-download --http-test-mib 1024
```

A quick download smoke test fetches only the model configuration:

```bash
python hf_network_benchmark.py run \
  --label config-smoke-test \
  --model Qwen/Qwen3.8-27B \
  --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
  --include config.json --http-test-mib 0
```

`--include` accepts repository globs and can be repeated. A subset measures that subset's bytes, not the full model. If no selected file is large enough for the requested HTTP transfer, that test is marked skipped. Set `--http-test-mib 0` to disable it explicitly.

## Results and comparison

Each run creates:

```text
<output>/<UTC timestamp>-<unique suffix>/
  report.json             # Complete record and status; updated after each stage
  report.md               # Human-readable summary
  download-job.json        # Selected files and worker settings
  download-result.json     # Successful timing or failure information
  model/                  # Fresh model copy, unless --model-dir was supplied
  cache/                  # Isolated Hugging Face and Xet state
```

The job/result files are only created for full or subset downloads. Copy the small `report.json` files from separate clusters to one location to compare them:

```bash
python hf_network_benchmark.py compare \
  /path/to/cluster-a-compute/report.json \
  /path/to/cluster-a-login/report.json \
  /path/to/cluster-b-compute/report.json \
  /path/to/cluster-b-login/report.json \
  --output comparison.md
```

The first report is the speedup baseline. Failed downloads never supply a speedup. The comparison flags differing models, revisions, selected files, or concurrency settings; it is not a claim that network link speed alone caused a difference.

## Interpreting rates

- **Snapshot MB/s:** completed, size-verified model bytes divided by the wall-clock duration of `snapshot_download`. Metadata inside that call is included; worker startup, preflight metadata, and verification are excluded. Xet compression can make this rate exceed actual wire throughput.
- **HTTP MB/s:** identity-encoded application bytes divided by request elapsed time. A separate streaming rate excludes request/response setup. Data is discarded rather than written to the model directory. Short responses and encoded responses are not reported as successful tests.
- **Interface counters:** all traffic on the host, including other users and storage traffic; counters can lag. They cannot establish exact per-process bandwidth.
- **Link speed:** the operating speed reported by an interface. It is an upper bound for that link, not guaranteed internet or filesystem throughput. InfiniBand is often used for internal traffic; recorded destination routes determine whether it carries an internet request.

The supervisor uses a separate download process. If the worker is killed, the report records failure and its return code instead of a successful rate. A SIGKILL does not by itself prove OOM: inspect the recorded memory counters and scheduler logs. If the entire supervisor is killed, the last `report.json` remains `running`, not complete. Incomplete model files are retained in that run's directory.

The defaults are two file workers and four Xet range requests per file. The original login-node run hit a 5 GiB user memory limit with eight workers. Higher values and `--high-performance` may help on faster, less constrained hosts, but compare identical settings across hosts and account for memory limits.

Only explicitly selected small reports should be added to source control. The default benchmark-results directory, model weights, caches, and environment files are ignored by Git. Reports include hostnames, IP addresses, routes, and filesystem paths; choose repository visibility appropriate for that data.

## Original measurements

The [original-results note](examples/original-results.md) documents the prior 136.0 MB/s compute-node and 828.0 MB/s login-node results. The detailed conversation and evidence remain in [codex-conversations](https://github.com/davidecarlson/codex-conversations).

## Validation

```bash
python -m unittest discover -s tests -v
```

Tests cover range-transfer validation, interrupted workers, file-size verification, path validation, and comparison handling of mismatched or failed runs. See [VALIDATION.md](VALIDATION.md) for recorded smoke tests.

## References

- [Hugging Face snapshot downloads](https://huggingface.co/docs/huggingface_hub/en/package_reference/file_download)
- [Hugging Face cache and Xet environment variables](https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables)
- [Xet chunk compression](https://huggingface.co/docs/xet/xorb)
