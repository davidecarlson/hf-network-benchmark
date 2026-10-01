# Validation record

Validated on 2026-10-01 on `login1`, using Python 3.13, Hugging Face Hub 1.31.0, and hf-xet 1.6.0.

- Eight unit tests passed, covering file sizes, unsafe paths, exact file selection, missing metadata, HTTP ranges, encoded/short responses, killed workers, comparison handling of failures and mismatched workloads, cgroup ancestor limits, and proxy credential omission.
- The Slurm example passed `bash -n`.
- A real fresh `config.json` download from `Qwen/Qwen3.8-27B` revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` completed and passed size verification. The supervisor, isolated cache, worker timing, and report generation ran successfully.
- A real network-only 16 MiB HTTP range request against that revision completed with HTTP 206, read exactly 16,777,216 bytes, and generated route and counter data.
- The comparison command successfully read both saved reports and generated a Markdown table.

The configuration and 16 MiB runs are smoke tests, not representative sustained model-download bandwidth measurements. The portable script has not yet performed a full 55.6 GB download or been exercised on another cluster. The [historical full-model results](examples/original-results.md) used earlier one-off scripts.

Live validation outputs were stored outside this repository under `/tmp/hf-benchmark-validation`. No model weights or live host-inventory files are committed here.
