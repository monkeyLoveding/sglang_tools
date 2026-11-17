#!/usr/bin/env python3
"""Collect GitHub Actions data for sgl-project/sglang `nightly-test-npu.yml`
(schedule-triggered runs) into JSON files under ``data/`` for the dashboard.

Data sources (GitHub REST API):
  * workflow runs: /repos/{repo}/actions/workflows/{wf}/runs?event=schedule
  * run jobs:      /repos/{repo}/actions/runs/{run_id}/jobs
  * job logs:      /repos/{repo}/actions/jobs/{job_id}/logs   (needs a token)

For single-node "suite" jobs one job runs many test files; the per-file result
is only available inside the job log, in the machine-readable block that
``test/run_suite.py`` emits:

    ========== TIMINGS BEGIN ==========
    {"file": "test/registered/npu/...", "passed": true, "elapsed": 123}
    ...
    ========== TIMINGS END ==========

For multi-node jobs one job runs exactly one test file, so the job conclusion
is the test-case conclusion.

Output layout (``--out`` dir):
    data/meta.json                 index / coverage / counters
    data/runs.json                 run-level records
    data/jobs/<YYYY-MM>.json       job-level records (sharded by Beijing month)
    data/cases/<YYYY-MM>.json      per-test-file records
    data/_cache.json               incremental cache (not used by the frontend)

Only stdlib is used, so the collector runs in Actions without pip install.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO = "sgl-project/sglang"
WORKFLOW = "nightly-test-npu.yml"
# Bump when parsing/persistence logic changes so cached runs are re-processed.
CACHE_VERSION = 5
API = "https://api.github.com"
BJ = timezone(timedelta(hours=8))  # schedule cron fires 16:00 UTC == 00:00 Beijing

TIMINGS_BEGIN = "TIMINGS BEGIN"
TIMINGS_END = "TIMINGS END"
LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s?")

# Multi-node jobs run exactly one test file chosen by the matrix `name`.
# GitHub truncates long job names, so the path is taken from this map instead
# of the job name. Keep in sync with the matrix in nightly-test-npu.yml.
MULTINODE_CONFIGS = {
    # nightly-poc-multi-node-tests
    "deepseek_v4_flash_w8a8_1p1d_16p_in128k_out1k_prefix90_20ms":
        "test/registered/npu/performance/deepseek_v4_flash/"
        "test_npu_deepseek_v4_flash_w8a8_1p1d_16p_in128k_out1k_prefix90_20ms.py",
    "deepseek_v4_flash_0731_w8a8_1p1d_16p_gpqa":
        "test/registered/npu/accuracy/deepseek_v4_flash/"
        "test_npu_deepseek_v4_flash_0731_w8a8_1p1d_16p_gpqa.py",
    "deepseek_v4_pro_0813_w4a8_1p1d_32p_gpqa":
        "test/registered/npu/accuracy/deepseek_v4_pro/"
        "test_npu_deepseek_v4_pro_0813_w4a8_1p1d_32p_gpqa.py",
    "glm_5_3_w8a8_1p1d_32p_pcp_gpqa":
        "test/registered/npu/accuracy/glm5_3/test_npu_glm_5_3_w8a8_1p1d_32p_pcp_gpqa.py",
    "glm_5_3_w8a8_1p1d_32p_dcp_gpqa":
        "test/registered/npu/accuracy/glm5_3/test_npu_glm_5_3_w8a8_1p1d_32p_dcp_gpqa.py",
    "glm_5_3_w8a8_1p1d_32p_in128k_out1k_prefix90_20ms":
        "test/registered/npu/performance/glm5_3/"
        "test_npu_glm_5_3_w8a8_1p1d_32p_in128k_out1k_prefix90_20ms.py",
    # nightly-poc-multi-node-mix-tests
    "kimi_k3_w4a8_32p_gpqa":
        "test/registered/npu/accuracy/kimi_k3/test_npu_kimi_k3_w4a8_32p_gpqa.py",
    "deepseek_v4_pro_16p_in128k_out1k_prefix90_20ms":
        "test/registered/npu/performance/deepseek_v4_pro/"
        "test_npu_deepseek_v4_pro_w4a8_16p_in128k_out1k_prefix90_20ms.py",
    "kimi_k3_w4a8_32p_mix_in128k_out1_prefill":
        "test/registered/npu/performance/kimi_k3/"
        "test_npu_kimi_k3_w4a8_32p_mix_in128k_out1_prefill.py",
    "kimi_k3_w4a8_32p_mix_in128k_out1k_decode_prefix":
        "test/registered/npu/performance/kimi_k3/"
        "test_npu_kimi_k3_w4a8_32p_mix_in128k_out1k_decode_prefix.py",
}

# `needs` graph of nightly-test-npu.yml, keyed by the *caller* job id.
# Jobs whose group is missing fall back to the run start time.
NEEDS = {
    "set-image-config": [],
    "nightly-1-npu-a3": ["set-image-config"],
    "nightly-2-npu-a3": ["set-image-config"],
    "nightly-4-npu-a3": ["set-image-config", "sgl-router-build-aarch64"],
    "nightly-8-npu-a3": ["set-image-config"],
    "nightly-16-npu-a3": ["set-image-config"],
    "nightly-perf-2-npu-a3": ["set-image-config"],
    "nightly-perf-4-npu-a3": ["set-image-config"],
    "nightly-perf-16-npu-a3": ["set-image-config"],
    "nightly-acc-2-npu-a3": ["set-image-config"],
    "nightly-acc-16-npu-a3": ["set-image-config"],
    "nightly-acc-4-npu-a5": ["set-image-config"],
    "sgl-router-build-aarch64": ["set-image-config"],
    "nightly-poc-multi-node-tests": [
        "set-image-config",
        "nightly-perf-2-npu-a3",
        "nightly-perf-4-npu-a3",
        "nightly-perf-16-npu-a3",
        "nightly-acc-2-npu-a3",
        "nightly-acc-16-npu-a3",
        "sgl-router-build-aarch64",
    ],
    "nightly-poc-multi-node-mix-tests": [
        "set-image-config",
        "nightly-perf-2-npu-a3",
        "nightly-perf-4-npu-a3",
        "nightly-perf-16-npu-a3",
        "nightly-acc-2-npu-a3",
        "nightly-acc-16-npu-a3",
        "nightly-poc-multi-node-tests",
    ],
}
# Display name of a reusable-workflow job -> caller job id.
GROUP_ALIAS = {
    "multi-node-poc": "nightly-poc-multi-node-tests",
    "multi-node-mix-poc": "nightly-poc-multi-node-mix-tests",
}

SUITE_NAME_RE = re.compile(r"^nightly-(?:\d+|perf-\d+|acc-\d+)-npu-a\d+$")


# --------------------------------------------------------------------------- #
# GitHub API helpers
# --------------------------------------------------------------------------- #
class NoAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when following a redirect to another host.

    The job-log endpoint 302-redirects to a pre-signed blob URL; sending the
    bearer token there makes Azure reject the request.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            for attr in ("headers", "unredirected_hdrs"):
                hdrs = getattr(new, attr, None)
                if hdrs:
                    setattr(
                        new,
                        attr,
                        {k: v for k, v in hdrs.items() if k.lower() != "authorization"},
                    )
        return new


class Api:
    def __init__(self, token: str | None, verbose: bool = True):
        self.token = token
        self.verbose = verbose
        self.logs_forbidden = False  # set on the first 401/403 from the log endpoint
        self.opener = urllib.request.build_opener(
            NoAuthOnRedirect, urllib.request.HTTPSHandler()
        )

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _open(self, url: str, accept: str | None = None):
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "sglang-nightly-dashboard")
        req.add_header("Accept", accept or "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("Accept-Encoding", "identity")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return self.opener.open(req, timeout=120)

    def _maybe_sleep_for_rate_limit(self, headers) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is None:
            return
        try:
            remaining_i = int(remaining)
            reset_i = int(reset) if reset else 0
        except (TypeError, ValueError):
            return
        if remaining_i <= 2 and reset_i:
            wait = reset_i - int(time.time()) + 5
            if wait > 0:
                self._log(f"[rate-limit] remaining={remaining_i}; sleeping {wait}s")
                time.sleep(wait)

    def get_json(self, url: str, retries: int = 4):
        """GET a JSON document, retrying on transient/rate-limit errors."""
        last_err = None
        for attempt in range(retries):
            try:
                with self._open(url) as resp:
                    body = resp.read()
                    self._maybe_sleep_for_rate_limit(resp.headers)
                return json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                retry_after = e.headers.get("retry-after") if e.headers else None
                if e.code in (403, 429) and retry_after:
                    self._log(f"[http {e.code}] retry-after={retry_after}s")
                    time.sleep(int(retry_after) + 2)
                    continue
                if e.code >= 500:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:  # noqa: PERF203
                last_err = e
                time.sleep(3 * (attempt + 1))
        raise last_err  # type: ignore[misc]

    def get_job_log(self, job_id: int, retries: int = 3):
        """Download a job log; returns text, or None when unavailable.

        The endpoint 302-redirects to a blob host that occasionally times out,
        so transient network errors and 5xx are retried before giving up.
        """
        url = f"{API}/repos/{REPO}/actions/jobs/{job_id}/logs"
        for attempt in range(retries):
            try:
                with self._open(url, accept="application/vnd.github+json") as resp:
                    return _decode_log(resp.read())
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                if e.code in (401, 403):
                    self.logs_forbidden = True
                    self._log(f"[log {job_id}] HTTP {e.code}: {body.strip()}")
                    return None
                if e.code == 404:
                    self._log(f"[log {job_id}] HTTP 404 (log expired?)")
                    return None
                self._log(f"[log {job_id}] HTTP {e.code}, retry {attempt + 1}/{retries}")
            except (OSError, http.client.HTTPException) as e:
                # URLError/TimeoutError/ConnectionError plus truncated reads
                # (http.client.IncompleteRead) are all retried.
                self._log(f"[log {job_id}] net error, retry {attempt + 1}/{retries}: {e}")
            time.sleep(3 * (attempt + 1))
        return None


def _decode_log(raw: bytes) -> str:
    if raw[:2] == b"PK":  # zip archive of one/more log files
        out = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                out.append(zf.read(name).decode("utf-8", "replace"))
        return "\n".join(out)
    if raw[:2] == b"\x1f\x8b":  # gzip
        return gzip.decompress(raw).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_dt(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def bj_date(value: str | None) -> str | None:
    dt = parse_dt(value)
    return dt.astimezone(BJ).strftime("%Y-%m-%d") if dt else None


def parse_timings(log_text: str):
    """Return a list of {file, passed, elapsed} from the TIMINGS block."""
    records = []
    inside = False
    for line in log_text.splitlines():
        line = LOG_LINE_RE.sub("", line.strip())
        if TIMINGS_BEGIN in line:
            inside = True
            continue
        if TIMINGS_END in line:
            inside = False
            continue
        if not inside or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "file" in obj:
            records.append(
                {
                    "file": obj.get("file"),
                    "passed": bool(obj.get("passed")),
                    "elapsed": obj.get("elapsed"),
                }
            )
    return records


# Directory names that group tests by platform/kind instead of naming a model.
PLATFORM_DIRS = {
    "npu",
    "ascend",
    "cuda",
    "gpu",
    "models",
    "llm_models",
    "vlm_models",
    "reward_models",
    "embedding_models",
    "multimodal_models",
    "llm",
    "vlm",
    "common",
    "utils",
}


def classify_case(file_path: str):
    """Return (category, model) derived from a test file path.

    The `test/registered` and optional leading `npu` prefixes are stripped, then
    the first segment is the category. The model is taken from the directory
    that directly contains the file when that directory names something
    specific, otherwise it is derived from the file name:

      <category>/<model>/<file>.py          -> the directory
      <category>/<platform>/<model>/<file>  -> the directory
      <category>/<models>/<file>.py         -> from the file name
      <category>/<file>.py                  -> from the file name
    """
    parts = (file_path or "").replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] == "test" and parts[1] == "registered":
        rel = parts[2:]
    else:
        rel = parts
    if rel and rel[0] == "npu":
        rel = rel[1:]
    if not rel:
        return "unknown", "unknown"
    category = rel[0]
    segs = rel[1:]
    if len(segs) >= 2:
        parent = segs[-2]
        if parent not in PLATFORM_DIRS:
            return category, parent
    filename = segs[-1] if segs else rel[-1]
    return category, derive_model(filename)


def derive_model(filename: str) -> str:
    name = filename[:-3] if filename.endswith(".py") else filename
    for pref in ("test_npu_", "test_ascend_", "test_"):
        if name.startswith(pref):
            return name[len(pref) :]
    return name


def classify_job(name: str):
    """Return (group, kind, partition) for a job display name."""
    if name.startswith("multi-node-mix-poc"):
        return GROUP_ALIAS["multi-node-mix-poc"], "multi-node", None
    if name.startswith("multi-node-poc"):
        return GROUP_ALIAS["multi-node-poc"], "multi-node", None
    left = name.split(" / ", 1)[0].strip()
    partition = None
    m = re.search(r"\((\d+)\)\s*$", name)
    if m:
        partition = int(m.group(1))
    if SUITE_NAME_RE.match(left):
        return left, "suite", partition
    return left, "infra", partition


def extract_multinode_config(name: str):
    """Return the matrix config name of a multi-node job.

    The display name is "<caller job> (<matrix values>) / <test_config_name>";
    the test file path inside the matrix values is truncated by GitHub, so only
    the config name is usable and the path is resolved via MULTINODE_CONFIGS.
    """
    parts = name.split(" / ")
    if len(parts) >= 2:
        return parts[-1].strip() or None
    return None


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def _read_token_file(path: str) -> str | None:
    """Read a token from a text file. Accepts a bare token, `KEY=value`, or
    `KEY: value`; blank lines and `#` comments are ignored."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    line = line.split("=", 1)[1]
                elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*:", line):
                    line = line.split(":", 1)[1]
                line = line.strip().strip('"').strip("'").strip()
                if line:
                    return line
    except OSError as e:
        print(f"WARNING: cannot read token file {path}: {e}", file=sys.stderr)
    return None


def resolve_token(args):
    """Return (token, source). Order: --token > env > --token-file."""
    if args.token:
        return args.token.strip(), "--token"
    for env_name in ("GH_API_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip(), f"env:{env_name}"
    file_token = _read_token_file(args.token_file)
    if file_token:
        return file_token, f"file:{args.token_file}"
    return None, "none"


def collect(args) -> int:
    token, source = resolve_token(args)
    if token:
        print(f"Using token from {source}")
    else:
        print(
            "WARNING: no token found (--token / GH_API_TOKEN / GITHUB_TOKEN / "
            f"--token-file {args.token_file}). Job logs cannot be downloaded and "
            "the API is limited to 60 requests/hour.",
            file=sys.stderr,
        )
    api = Api(token, verbose=args.verbose)

    out_dir = args.out
    runs_path = os.path.join(out_dir, "runs.json")
    meta_path = os.path.join(out_dir, "meta.json")
    cache_path = os.path.join(out_dir, "_cache.json")
    jobs_dir = os.path.join(out_dir, "jobs")
    cases_dir = os.path.join(out_dir, "cases")

    cache = _load_json(cache_path, default={"version": CACHE_VERSION, "runs": {}})
    if cache.get("version") != CACHE_VERSION:
        print(f"Cache version mismatch -> re-processing all runs")
        cache = {"version": CACHE_VERSION, "runs": {}}
    existing_runs = {r["run_id"]: r for r in _load_json(runs_path, default=[])}
    existing_jobs = _load_records(jobs_dir)
    existing_cases = _load_records(cases_dir)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"Window: runs created after {cutoff.isoformat()}")

    runs = _fetch_runs(api, cutoff, args.limit_runs)
    print(f"Fetched {len(runs)} schedule runs in window")

    model_map = _load_json(args.model_map, default={}) if args.model_map else {}

    processed = 0
    for run in runs:
        run_id = run["id"]
        attempt = run.get("run_attempt", 1)
        run_key = f"{run_id}:{attempt}"
        cached = cache["runs"].get(run_key)
        final = run.get("status") == "completed"
        # Skip work for runs we already captured in a final state.
        if cached and cached.get("done") and final:
            continue

        processed += 1
        print(f"  run {run_id} (#{run.get('run_number')}) attempt={attempt} "
              f"conclusion={run.get('conclusion')}")

        jobs = _fetch_jobs(api, run_id)
        run_date = bj_date(run.get("run_started_at") or run.get("created_at"))

        # Fresh attempt supersedes any previously stored records for this run.
        existing_runs.pop(run_id, None)
        existing_jobs = {k: v for k, v in existing_jobs.items() if v["run_id"] != run_id}
        existing_cases = {k: v for k, v in existing_cases.items() if v["run_id"] != run_id}

        existing_runs[run_id] = _build_run(run, run_date)

        group_done = _group_completion(jobs)
        run_start = parse_dt(run.get("run_started_at") or run.get("created_at"))
        log_parsed = set(cached.get("logs", [])) if cached else set()

        for job in jobs:
            group, kind, partition = classify_job(job.get("name", ""))
            if kind == "infra":
                # Still store infra jobs (useful for queue analysis) but no cases.
                pass
            job_rec = _build_job(job, run, run_date, group, kind, partition, run_start, group_done)
            existing_jobs[(run_id, job["id"])] = job_rec

            if kind == "suite" and args.skip_logs is False and not api.logs_forbidden:
                if job["id"] in log_parsed:
                    continue
                if job.get("status") != "completed":
                    continue
                log_text = api.get_job_log(job["id"])
                if log_text is None:
                    continue
                log_parsed.add(job["id"])
                for rec in parse_timings(log_text):
                    case = _build_case_from_timings(rec, run, job, run_date, group, model_map)
                    if case:
                        existing_cases[(run_id, job["id"], case["case_id"])] = case
            elif kind == "multi-node":
                case = _build_case_from_job(job, run, run_date, group, model_map)
                if case:
                    existing_cases[(run_id, job["id"], case["case_id"])] = case

        cache["runs"][run_key] = {"done": bool(final), "logs": sorted(log_parsed)}

    # Retention: drop records whose run date falls outside the window.
    min_date = (cutoff.astimezone(BJ)).strftime("%Y-%m-%d")
    existing_runs = {k: v for k, v in existing_runs.items() if (v.get("date") or "") >= min_date}
    existing_jobs = {k: v for k, v in existing_jobs.items() if (v.get("date") or "") >= min_date}
    existing_cases = {k: v for k, v in existing_cases.items() if (v.get("date") or "") >= min_date}

    # Prune cache entries that no longer have any data.
    live_keys = {f"{v['run_id']}:{v.get('run_attempt', 1)}" for v in existing_runs.values()}
    cache["runs"] = {k: v for k, v in cache["runs"].items() if k in live_keys}

    _write_outputs(out_dir, existing_runs, existing_jobs, existing_cases, cache, args)
    print(f"Done. processed={processed} runs, "
          f"runs={len(existing_runs)} jobs={len(existing_jobs)} cases={len(existing_cases)}")
    return 0


def _fetch_runs(api: Api, cutoff: datetime, limit: int | None):
    base = (
        f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs"
        f"?event=schedule&per_page=100"
    )
    runs = []
    page = 1
    while True:
        data = api.get_json(f"{base}&page={page}")
        items = data.get("workflow_runs", [])
        if not items:
            break
        for run in items:
            created = parse_dt(run.get("created_at"))
            if created and created < cutoff:
                return runs
            runs.append(run)
            if limit and len(runs) >= limit:
                return runs
        if len(items) < 100:
            break
        page += 1
    return runs


def _fetch_jobs(api: Api, run_id: int):
    base = f"{API}/repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100"
    jobs = []
    page = 1
    while True:
        data = api.get_json(f"{base}&page={page}")
        items = data.get("jobs", [])
        if not items:
            break
        jobs.extend(items)
        if len(items) < 100:
            break
        page += 1
    return jobs


def _group_completion(jobs):
    """group -> latest completed_at among its jobs."""
    done = {}
    for job in jobs:
        group, _, _ = classify_job(job.get("name", ""))
        dt = _job_completed(job)
        if dt and (group not in done or dt > done[group]):
            done[group] = dt
    return done


def _job_completed(job):
    return parse_dt(job.get("completed_at"))


def _seconds_between(a: datetime | None, b: datetime | None):
    if not a or not b:
        return None
    return round((b - a).total_seconds(), 1)


def _nonneg(value):
    """Clamp negative durations/queues to 0; keep None as-is."""
    if value is None:
        return None
    return max(0.0, float(value))


def _build_run(run, run_date):
    return {
        "run_id": run["id"],
        "run_number": run.get("run_number"),
        "run_attempt": run.get("run_attempt", 1),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "run_started_at": run.get("run_started_at"),
        "updated_at": run.get("updated_at"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "html_url": run.get("html_url"),
        "date": run_date,
        "queue_seconds": _seconds_between(
            parse_dt(run.get("created_at")), parse_dt(run.get("run_started_at"))
        ),
    }


def _build_job(job, run, run_date, group, kind, partition, run_start, group_done):
    started = parse_dt(job.get("started_at"))
    completed = parse_dt(job.get("completed_at"))
    deps = NEEDS.get(group, [])
    dep_ready = run_start
    for d in deps:
        dt = group_done.get(d)
        if dt and (dep_ready is None or dt > dep_ready):
            dep_ready = dt
    return {
        "job_id": job["id"],
        "run_id": run["id"],
        "run_attempt": run.get("run_attempt", 1),
        "name": job.get("name"),
        "job_group": group,
        "kind": kind,
        "partition": partition,
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "runner_name": job.get("runner_name"),
        "labels": job.get("labels") or [],
        "html_url": job.get("html_url"),
        "date": run_date,
        "duration_seconds": _nonneg(_seconds_between(started, completed)),
        # dependency-aware queue: time the job waited after it became runnable
        "queue_seconds": _nonneg(_seconds_between(dep_ready, started)),
        # raw queue: from the moment the workflow run started
        "queue_from_run_start_seconds": _nonneg(_seconds_between(run_start, started)),
        "run_test_seconds": _nonneg(_step_seconds(job, "Run test")),
    }


def _step_seconds(job, step_name):
    for step in job.get("steps", []) or []:
        if step.get("name") == step_name:
            return _seconds_between(parse_dt(step.get("started_at")), parse_dt(step.get("completed_at")))
    return None


def _build_case_from_timings(rec, run, job, run_date, group, model_map):
    file_path = rec["file"]
    category, model = classify_case(file_path)
    basename = os.path.basename(file_path or "")
    model = _apply_model_map(model_map, file_path, basename, model, category)
    return {
        "run_id": run["id"],
        "job_id": job["id"],
        "job_group": group,
        "source": "timings",
        "case_id": file_path,
        "case": basename,
        "file": file_path,
        "category": category,
        "model": model,
        "passed": rec["passed"],
        "duration_seconds": rec.get("elapsed"),
        "date": run_date,
    }


def _build_case_from_job(job, run, run_date, group, model_map):
    config = extract_multinode_config(job.get("name", "")) or job.get("name", "")
    test_case = MULTINODE_CONFIGS.get(config)
    if test_case:
        category, model = classify_case(test_case)
        basename = os.path.basename(test_case)
    else:
        category, model, basename = "multi-node", config, config
    model = _apply_model_map(model_map, test_case or config, basename, model, category)
    duration = _step_seconds(job, "Run test")
    if duration is None:
        duration = _seconds_between(parse_dt(job.get("started_at")), parse_dt(job.get("completed_at")))
    return {
        "run_id": run["id"],
        "job_id": job["id"],
        "job_group": group,
        "source": "job",
        "case_id": test_case or config,
        "case": basename,
        "file": test_case,
        "category": category,
        "model": model,
        "config_name": config,
        "passed": job.get("conclusion") == "success",
        "duration_seconds": _nonneg(duration),
        "date": run_date,
    }


def _apply_model_map(model_map, file_path, basename, model, category):
    if not model_map:
        return model
    for key in (file_path, basename, f"{category}/{model}"):
        if key in model_map:
            return model_map[key]
    return model


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _load_records(directory):
    records = {}
    if not os.path.isdir(directory):
        return records
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        data = _load_json(os.path.join(directory, name), default=[])
        for rec in data:
            if "job_id" in rec and "case_id" not in rec:
                records[(rec["run_id"], rec["job_id"])] = rec
            elif "case_id" in rec:
                records[(rec["run_id"], rec["job_id"], rec["case_id"])] = rec
    return records


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def _write_outputs(out_dir, runs, jobs, cases, cache, args):
    jobs_dir = os.path.join(out_dir, "jobs")
    cases_dir = os.path.join(out_dir, "cases")
    # Clear stale shards before rewriting.
    for d in (jobs_dir, cases_dir):
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".json"):
                    os.remove(os.path.join(d, name))

    runs_by_month = defaultdict(list)
    for rec in jobs.values():
        runs_by_month[(rec.get("date") or "unknown")[:7]].append(rec)
    for month, recs in runs_by_month.items():
        recs.sort(key=lambda r: (r.get("started_at") or "", r["job_id"]))
        _write_json(os.path.join(jobs_dir, f"{month}.json"), recs)

    cases_by_month = defaultdict(list)
    for rec in cases.values():
        cases_by_month[(rec.get("date") or "unknown")[:7]].append(rec)
    for month, recs in cases_by_month.items():
        recs.sort(key=lambda r: (r.get("date") or "", r.get("case_id") or ""))
        _write_json(os.path.join(cases_dir, f"{month}.json"), recs)

    run_list = sorted(runs.values(), key=lambda r: (r.get("run_started_at") or ""))
    _write_json(os.path.join(out_dir, "runs.json"), run_list)
    _write_json(os.path.join(out_dir, "_cache.json"), cache)

    months = sorted(set(runs_by_month) | set(cases_by_month))
    meta = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "repo": REPO,
        "workflow": WORKFLOW,
        "window_days": args.days,
        "generated_by": "collector/collect.py",
        "counts": {
            "runs": len(run_list),
            "jobs": len(jobs),
            "cases": len(cases),
        },
        "months": months,
        "date_range": {
            "from": min((r.get("date") for r in run_list if r.get("date")), default=None),
            "to": max((r.get("date") for r in run_list if r.get("date")), default=None),
        },
    }
    _write_json(os.path.join(out_dir, "meta.json"), meta)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days.")
    parser.add_argument("--out", default="data", help="Output data directory.")
    parser.add_argument("--token", default=None, help="GitHub token (else env/file).")
    parser.add_argument("--token-file", default="token.txt",
                        help="Text file to read the token from when no --token/env "
                             "is set (default: token.txt).")
    parser.add_argument("--model-map", default="config/model_map.json",
                        help="Optional JSON file overriding model names.")
    parser.add_argument("--limit-runs", type=int, default=None,
                        help="Only process the N most recent runs (debug).")
    parser.add_argument("--skip-logs", action="store_true",
                        help="Skip job-log downloads (job-level data only).")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    args = parser.parse_args()
    # Allow an empty/missing model-map file to mean "no overrides".
    if args.model_map and not os.path.exists(args.model_map):
        args.model_map = None
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
