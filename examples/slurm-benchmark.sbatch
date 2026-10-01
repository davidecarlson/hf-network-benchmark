#!/bin/bash
#SBATCH --job-name=hf-network-bench
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=hf-network-bench-%j.out

# Adapt partition/account, memory, Python environment, and paths for your cluster.
# This benchmark does not require a GPU.
set -euo pipefail

benchmark_repo="${BENCHMARK_REPO:?Set BENCHMARK_REPO to the absolute repository path}"
benchmark_output="${BENCHMARK_OUTPUT:?Set BENCHMARK_OUTPUT to a writable scratch directory}"
benchmark_python="${BENCHMARK_PYTHON:-python3}"

"$benchmark_python" "$benchmark_repo/hf_network_benchmark.py" run \
  --label "${BENCHMARK_LABEL:-compute}-${SLURM_JOB_ID}" \
  --model Qwen/Qwen3.8-27B \
  --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
  --output "$benchmark_output" \
  --workers 2 --range-gets 4 --http-test-mib 1024
