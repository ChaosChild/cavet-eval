#!/usr/bin/env python3
"""cavet benchmark harness.

Subcommands:
  selftest   Validate the token normaliser, cost model and stream/scan
             parsers. No network, no spend.
  probe      Run a trivial prompt through each harness and dump the raw usage
             record, so the extractors are wired from observed data rather than
             from documentation. Costs a few cents.
  prepare    Clone the corpus at pinned SHAs, de-identify, re-init git, manifest.
  run        Execute the matrix. --pilot writes verification records to
             runs/pilot/ (outside the duplicate guard and report aggregates).
             --arm armc --from-a <baseline_T2_run_id> runs the arm C
             traditional-CI comparator (plan §3.3).
  rotate     Phase-1 scheduler: serial round-robin across pairs, one cell per
             pair per cycle, defer-on-quota-error, checkpointed to
             runs/scheduler-state.json so an interrupted matrix resumes.
  report     Aggregate runs/ into tables (--phase0 for the archived set).
  recost     Recompute record costs from recorded tokens at current rates.

Design notes that are easy to get wrong, see README:
  - Harnesses disagree on whether input_tokens includes cache reads (5.2).
  - No harness-reported cost is trusted; cost is always computed (5.3).
  - Corpus prep must re-init git or cavet cannot run at all (3.4).
  - Repo owner/name exists only in .env. Everything written - run ids,
    records, prompts, the manifest - uses corpus-<n> ids, and each record is
    scrubbed of the names before it lands (Ethics).
  - Reasoning effort: REASONING_EFFORT (default medium) drives claude/agy
    flags and the intended tier for qwen's model config; zcode/opencode have
    no headless control and record harness-default (5.9). Effort is part of
    the cell key.
  - cavet is invoked ONLY through CAVET_BIN (the pinned .tools binary); on
    cavet-arm cells its directory is prepended to the harness subprocess PATH
    so the agent's own `cavet` resolves to the same build. Binary path,
    --version and the engine image digest are recorded at cell start AND end.
"""
from __future__ import annotations

import argparse
import contextlib
import re
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
PILOT_RUNS = RUNS / "pilot"    # --pilot records: outside the duplicate guard,
                               # report aggregates, and phase-1 cell keys
PHASE0_RUNS = RUNS / "phase0"  # archived phase-0 records (relocated pre-phase-1)
INVALID_RUNS = RUNS / "invalid"
LOGS = RUNS / "logs"
WORK = ROOT / "work"          # scratch: probe dirs
REPOS = ROOT / "repositories"  # prepared corpus clones
RATES = json.loads((ROOT / "rates.json").read_text(encoding="utf-8"))

CAVET_ENGINE_IMAGE = "ghcr.io/chaoschild/cavet-engine:0.2-core"

# Phase-1 rotation (plan §4). One cell per pair per cycle; the order keeps the
# constrained pairs (positions 1/3/5/7) spaced widest. Override with the
# ROTATION env var (comma-separated pair shorthands, matched against the
# models configured in .env; `muse-spark-zen` is the documented alias for the
# opencode-hosted free route).
DEFAULT_ROTATION = ("qwen3.7-plus,GLM-5.3,qwen3.8-max,claude-sonnet-5,"
                    "gemini-3.8-flash,GLM-5.3-Flash,muse-spark-zen,claude-opus-5")

# Per-pair repo order within a task (plan §4: 1, 2, 4, 3, 5).
REPO_ORDER = ["corpus-1", "corpus-2", "corpus-4", "corpus-3", "corpus-5"]

# Provider quota/allocation exhaustion markers. A hit defers a rotation pair
# to a later cycle instead of poisoning the whole matrix with more of the same.
QUOTA_MARKERS = ("session limit", "rate limit", "quota", "not authenticated",
                 "incorrect api key", "unauthorized")

# --------------------------------------------------------------------------
# env
# --------------------------------------------------------------------------

def load_env() -> dict:
    env = {}
    f = ROOT / ".env"
    if not f.exists():
        sys.exit(".env not found. Copy .env.example to .env and fill it in.")
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


# --------------------------------------------------------------------------
# corpus registry.  Owner and repo names exist ONLY in .env (gitignored).
# Every public artefact - run records, run ids, prompts, the manifest -
# addresses a repository as corpus-<n>, and the runner scrubs the names from
# each record before writing it.  See README "Ethics".
# --------------------------------------------------------------------------

def corpus_from_env(env) -> dict:
    out = {}
    for i in range(1, 9):
        repo = env.get(f"CORPUS_{i}_REPO")
        if not repo:
            continue
        out[f"corpus-{i}"] = {
            "repo": repo,
            "ref": env.get(f"CORPUS_{i}_REF", ""),
            "lang": env.get(f"CORPUS_{i}_LANG", ""),
            "ratio": env.get(f"CORPUS_{i}_RATIO", ""),
        }
    return out


def name_variants(repo: str) -> list:
    """Every substring that could identify the repo, longest first."""
    bare = repo.split("/")[-1]
    owner = repo.split("/")[0]
    seen = []
    for v in (repo, bare, owner):
        if v and v not in seen:
            seen.append(v)
    return seen


def scrub(obj, mapping: dict):
    """Replace every identifying substring throughout a record's strings."""
    if isinstance(obj, dict):
        return {k: scrub(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, mapping) for v in obj]
    if isinstance(obj, str):
        for name, cid in mapping.items():
            if name in obj:
                obj = obj.replace(name, cid)
    return obj


# --------------------------------------------------------------------------
# pinned cavet tooling (phase 1, plan §2.1 and §5)
# --------------------------------------------------------------------------

def cavet_bin(env) -> str:
    """The pinned cavet binary. bench.py invokes cavet ONLY through this.
    On cavet-arm and arm-C cells its directory is also prepended to the
    harness subprocess PATH, so the agent's own `cavet` calls resolve to the
    same pinned build rather than to whatever is first on the operator PATH."""
    return env.get("CAVET_BIN", "cavet")


def cavet_cell_env(env, arm: str) -> dict:
    """Subprocess env for one cell. On cavet-ARM cells the pinned binary's
    directory leads PATH so the agent's own `cavet` calls resolve to the same
    pinned build. NOT applied to the arm C fix session: that session models
    the traditional CI loop with no cavet in the agent's environment, and
    bench.py's own headless scans invoke the pinned binary by absolute path
    regardless. Other arms inherit the environment untouched."""
    e = os.environ.copy()
    if arm == "cavet":
        d = str(Path(cavet_bin(env)).resolve().parent)
        e["PATH"] = d + os.pathsep + e.get("PATH", "")
    return e


def cavet_provenance(env) -> dict:
    """Snapshot of the cavet toolchain: binary path, --version output and the
    engine image digest. Captured at cell start AND again at cell end, so a
    mid-run binary swap is detectable from the record alone (plan §2.1).
    The digest is provenance, not a gate: v0.2.x shares the 0.2-core engine."""
    b = cavet_bin(env)
    prov = {"binary": b, "version": "", "engine_digest": ""}
    try:
        v = _run_cmd([b, "--version"], timeout=60)
        prov["version"] = (v["out"] + v["err"]).strip() or f"rc={v['rc']}"
    except Exception as e:
        prov["version"] = f"error: {type(e).__name__}: {e}"
    try:
        img = _run_cmd(
            ["docker", "image", "inspect", "--format",
             "{{index .RepoDigests 0}}", CAVET_ENGINE_IMAGE], timeout=120)
        prov["engine_digest"] = img["out"].strip() \
            or f"inspect rc={img['rc']}"
    except Exception as e:
        prov["engine_digest"] = f"error: {type(e).__name__}: {e}"
    return prov


def cavet_init(env, d: Path) -> dict:
    return _run_cmd([cavet_bin(env), "init"], cwd=d, timeout=600)


def our_cavet_containers(work_root: Path) -> list:
    """Running cavet-* containers bound to OUR work root — and only those.

    Docker Desktop rewrites the summary's Mounts sources to in-VM paths, but
    HostConfig.Binds keeps the host path as created (the same source of truth
    cavet's own prune uses), so the bind list is the safe identity check.
    Containers belonging to other sessions' repositories never match and
    are never touched: the operator runs other cavet sessions on this host.
    """
    ids = _run_cmd(["docker", "ps", "-q", "--filter", "name=cavet-"],
                   timeout=120)["out"].split()
    markers = [str(work_root).lower(),
               str(work_root).replace("\\", "/").lower()]
    ours = []
    for cid_ in ids:
        try:
            binds = _run_cmd(
                ["docker", "inspect", "--format", "{{.HostConfig.Binds}}",
                 cid_], timeout=60)["out"].lower()
        except Exception:
            continue
        if any(m in binds for m in markers):
            ours.append(cid_)
    return ours


def stop_engine_scoped(env, d: Path) -> dict:
    """`cavet engine stop` from inside the repo (cavet identifies its own
    container via .cavet/), then force-remove ONLY survivors bound to our
    work tree. Never a global prune, never a broad name filter (plan §5):
    cavet's own `engine prune` would also sweep pre-existing orphans from
    OTHER sessions, which the standing docker-safety rule forbids touching;
    the operator may run it by hand if wanted."""
    stop = _run_cmd([cavet_bin(env), "engine", "stop"], cwd=d, timeout=180)
    rec = {"rc": stop["rc"], "out": stop["out"], "err": stop["err"]}
    if stop["timed_out"]:
        rec["stop_timed_out"] = True
    left = our_cavet_containers(WORK)
    if left:
        rm = _run_cmd(["docker", "rm", "-f", *left], timeout=180)
        rec["forced"] = len(left)
        rec["forced_ids"] = left
        if rm["timed_out"]:
            rec["rm_timed_out"] = True
    return rec


# --------------------------------------------------------------------------
# usage normalisation.  The whole benchmark's correctness sits here.
# --------------------------------------------------------------------------

@dataclass
class Usage:
    """Normalised token counts.

    uncached_input is ALWAYS tokens billed at the full input rate, with cache
    reads and cache writes excluded. Harnesses disagree about this, which is
    exactly why this type exists.
    """
    uncached_input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0        # 5-minute TTL
    cache_write_1h: int = 0     # 1-hour TTL, billed at 2x input on Anthropic
    reasoning: int = 0
    wall_s: float = 0.0
    requests: int = 0
    tool_calls: int = 0
    source: str = ""          # where the numbers came from
    raw: dict = field(default_factory=dict)

    @property
    def total_prompt(self) -> int:
        return (self.uncached_input + self.cache_read
                + self.cache_write + self.cache_write_1h)


def normalise(kind: str, input_tokens: int, cache_read: int, cache_write: int,
              output: int, **kw) -> Usage:
    """Convert a harness's native counts into Usage.

    kind:
      "exclusive" - input_tokens excludes cache (Claude Code, OpenCode)
      "inclusive" - input_tokens includes cache reads (ZCode)
    """
    w1h = kw.pop("cache_write_1h", 0)
    if kind == "exclusive":
        uncached = input_tokens
    elif kind == "inclusive":
        uncached = input_tokens - cache_read - cache_write - w1h
    else:
        raise ValueError(f"unknown accounting kind: {kind}")
    if uncached < 0:
        raise ValueError(
            f"negative uncached input ({uncached}). accounting kind '{kind}' is "
            f"probably wrong for this harness: input={input_tokens} "
            f"cache_read={cache_read} cache_write={cache_write}")
    return Usage(uncached_input=uncached, output=output, cache_read=cache_read,
                 cache_write=cache_write, cache_write_1h=w1h, **kw)


def resolve_model(name: str) -> str:
    name = (name or "").strip()
    return RATES["aliases"].get(name, name)


def cost_usd(model: str, u: Usage) -> float:
    m = resolve_model(model)
    r = RATES["models"].get(m)
    if r is None:
        raise KeyError(f"no rate for model '{model}' (resolved '{m}'). "
                       f"Add it to rates.json rather than guessing.")
    def rate(key, tokens, fallback_key=None):
        """A missing rate is UNKNOWN, never zero and never the input rate.

        Silently charging cache reads at the full input rate overstated one
        Gemini run by 6x before this was caught. If tokens were actually spent
        in a class with no published rate, fail rather than invent a number.
        """
        v = r.get(key)
        if v is None and fallback_key:
            v = r.get(fallback_key)
        if v is None:
            if tokens:
                raise KeyError(
                    f"model '{m}' has no published '{key}' rate but the run used "
                    f"{tokens} such tokens. Add the real rate to rates.json.")
            return 0.0
        return v
    per = 1_000_000.0
    total = (u.uncached_input * rate("input", u.uncached_input)
             + u.output * rate("output", u.output)
             + u.cache_read * rate("cache_read", u.cache_read)
             + u.cache_write * rate("cache_write", u.cache_write)
             + u.cache_write_1h * rate("cache_write_1h", u.cache_write_1h,
                                       "cache_write")) / per
    return round(total, 6)


# --------------------------------------------------------------------------
# harness adapters
# --------------------------------------------------------------------------

@dataclass
class Harness:
    name: str
    accounting: str                     # "exclusive" | "inclusive" | "unknown"
    build_cmd: object                   # (bin, model, prompt, cwd) -> list[str]
    read_usage: object                  # (ctx) -> Usage
    setup: object = None                # optional (env, model) -> contextmanager
    shell: bool = False
    stream: bool = False                # stdout is an incremental event stream:
                                        # parse JSONL per line so a timed-out
                                        # run still yields its usage events


def _sqlite_ro(path: Path):
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def usage_opencode(ctx) -> Usage:
    """OpenCode: session table, keyed by directory. input excludes cache."""
    db = Path(os.path.expanduser("~/.local/share/opencode/opencode.db"))
    c = _sqlite_ro(db)
    # Attribution is by time, not directory: OpenCode resolves session.directory
    # to a project root it picks itself, which under subprocess does not reliably
    # equal the cwd we passed. The runner is serial (required for ZCode anyway),
    # so the newest session created after t0 is this run.
    # SUM every session in the window, not just the newest. One run spawns a
    # main session plus subagent sessions; reading `limit 1` captured only the
    # subagent and silently dropped the larger parent, undercounting a measured
    # run by 70%. The runner is serial, so every session after t0 is this run.
    row = c.execute(
        "select count(*), coalesce(sum(tokens_input),0), coalesce(sum(tokens_output),0),"
        " coalesce(sum(tokens_reasoning),0), coalesce(sum(tokens_cache_read),0),"
        " coalesce(sum(tokens_cache_write),0), min(time_created), max(time_updated),"
        " coalesce(sum(cost),0)"
        " from session where time_created >= ?", (ctx["t0_ms"],)).fetchone()
    n, ti, to, tr, cr, cw, t_created, t_updated, reported_cost = row
    if not n:
        raise LookupError("no OpenCode session found in this run's time window")
    model = f"{n} session(s)"
    u = normalise("exclusive", ti or 0, cr or 0, cw or 0, to or 0,
                  reasoning=tr or 0,
                  wall_s=((t_updated or t_created) - t_created) / 1000.0,
                  source="opencode.db:session")
    u.raw = {"sessions": n, "provider_reported_cost": reported_cost}
    return u


def usage_zcode(ctx) -> Usage:
    """ZCode: session joined to model_usage. input INCLUDES cache reads."""
    db = Path(os.path.expanduser("~/.zcode/cli/db/db.sqlite"))
    c = _sqlite_ro(db)
    # Time-ordered attribution; see the note in usage_opencode.
    sess = c.execute(
        "select id from session where time_created >= ?"
        " order by time_created desc limit 1",
        (ctx["t0_ms"],)).fetchone()
    if not sess:
        raise LookupError("no ZCode session found for this run directory")
    sid = sess[0]
    row = c.execute(
        "select coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0),"
        " coalesce(sum(reasoning_tokens),0), coalesce(sum(cache_read_input_tokens),0),"
        " coalesce(sum(cache_creation_input_tokens),0), coalesce(sum(duration_ms),0),"
        " count(*), coalesce(sum(tool_call_count),0)"
        " from model_usage where session_id = ? or session_id like ?",
        (sid, f"%{sid}%")).fetchone()
    ti, to, tr, cr, cw, dur, n, tools = row
    return normalise("inclusive", ti, cr, cw, to, reasoning=tr,
                     wall_s=dur / 1000.0, requests=n, tool_calls=tools,
                     source="zcode db.sqlite:model_usage")


def usage_from_result_json(ctx) -> Usage:
    """Claude Code / qwen / agy: parse the --output-format json result blob.

    Shapes differ; `probe` dumps them so this can be pinned to observed keys
    rather than assumed. Until probe has run for a harness, this raises.
    """
    blob = ctx.get("stdout_json")
    if not blob:
        raise LookupError(
            f"{ctx['harness']}: no JSON on stdout. Run `bench.py probe` and wire "
            f"the extractor from the observed shape before running the matrix.")
    if isinstance(blob.get("status"), str) and blob["status"].upper() == "ERROR":
        raise RuntimeError(f"{ctx['harness']} returned ERROR: {blob.get('error')}")
    usage = None
    for key in ("usage", "tokenUsage", "stats", "metrics"):
        if isinstance(blob.get(key), dict):
            usage = blob[key]
            break
    if usage is None:
        raise LookupError(
            f"{ctx['harness']}: no usage object in result JSON. Observed keys: "
            f"{sorted(blob.keys())[:20]}")
    def g(*names):
        for n in names:
            if n in usage and isinstance(usage[n], (int, float)):
                return int(usage[n])
        return 0
    cc = usage.get("cache_creation") or {}
    w1 = int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
    w5 = int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
    # Claude Code reports usage twice and the two disagree. `usage` is a
    # snapshot; `modelUsage` carries the session totals and the costUSD that
    # Claude itself bills. Prefer the totals, and take the TTL split from
    # `usage.cache_creation`, treating any remainder as 5-minute. Verified: this
    # reproduces total_cost_usd exactly on an Opus run where reading `usage`
    # alone was 2.6% low.
    mu = (blob.get("modelUsage") or {}) if isinstance(blob, dict) else {}
    if isinstance(mu, dict) and mu:
        tot = next(iter(mu.values()))
        if isinstance(tot, dict):
            usage = dict(usage)
            usage["input_tokens"] = tot.get("inputTokens", usage.get("input_tokens", 0))
            usage["output_tokens"] = tot.get("outputTokens", usage.get("output_tokens", 0))
            usage["cache_read_input_tokens"] = tot.get(
                "cacheReadInputTokens", usage.get("cache_read_input_tokens", 0))
            total_cc = tot.get("cacheCreationInputTokens")
            if total_cc is not None:
                w5 = max(0, int(total_cc) - w1)
    total_write = g("cache_creation_input_tokens", "cacheWriteTokens", "cache_creation_tokens")
    if not (w5 or w1):
        w5 = total_write          # no TTL breakdown: assume the cheaper bucket
    return normalise(
        ctx["accounting"],
        g("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
        g("cache_read_input_tokens", "cacheReadTokens", "cache_read_tokens", "cachedInputTokens"),
        w5,
        g("output_tokens", "outputTokens", "completion_tokens", "completionTokens"),
        cache_write_1h=w1,
        reasoning=g("reasoning_tokens", "reasoningTokens", "thinking_tokens"),
        source=f"{ctx['harness']}:result-json")


def parse_stream_events(text: str) -> list:
    """Parse a harness event stream into dicts.

    Handles `-o stream-json` (one JSON object per line, flushed as it is
    produced) and, for compatibility, the old `-o json` whole-array shape.
    A run killed by the wall-clock timeout leaves a truncated FINAL line;
    every complete line before it still parses. That is exactly the forensic
    recovery the buffered `-o json` array made impossible - a timed-out qwen
    run used to leave an empty log and $0.00 of recorded spend (README 5.7,
    FIRST-EVALUATION §5 defect 5).
    """
    out = []
    text = (text or "").strip()
    if not text:
        return out
    if text.startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                return [e for e in arr if isinstance(e, dict)]
        except Exception:
            pass  # truncated array: fall through to per-line parsing
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue  # truncated tail line, or a non-JSON banner line
        if isinstance(ev, dict):
            out.append(ev)
    return out


USAGE_KEYS = ("usage", "tokenUsage", "stats", "metrics")


def _event_usage(ev: dict):
    """The usage dict on a stream event, wherever the harness hides it:
    top-level on result-style events, under message.usage on assistant
    events."""
    for k in USAGE_KEYS:
        if isinstance(ev.get(k), dict):
            return ev[k], ev
    msg = ev.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
        return msg["usage"], ev
    return None, ev


def last_usage_event(events: list) -> dict:
    """The terminal usage-bearing event, lifted into result-json shape so the
    shared extractor and actual_model keep working unchanged. On a completed
    qwen run that is the terminal `result` event with SESSION-TOTAL usage.

    On a timed-out run that event never arrives, and each assistant message
    carries only ITS OWN turn's usage — so the recovery SUMS usage over unique
    assistant messages and returns a synthetic result event. The previous
    last-event lift reported one turn out of 108 as the whole session on a
    real 1800s qwen3.8-max timeout ($2.05 of spend read as $0.00).

    Costing rule preserved (README 5.2): qwen's input_tokens INCLUDE cached
    tokens, so `normalise("inclusive", ...)` subtracts cache reads - never
    double-count. The fixture selftest pins this.
    """
    for ev in reversed(events):
        if isinstance(ev.get("usage"), dict):
            return ev
    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "total_tokens": 0}
    seen = set()
    model = None
    found = False
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        mid = msg.get("id")
        if mid is not None:
            if mid in seen:
                continue
            seen.add(mid)
        found = True
        model = msg.get("model") or model
        for k in total:
            total[k] += int(u.get(k) or 0)
    if found:
        return {"type": "result", "usage": total, "model": model,
                "recovered_from_stream": True}
    # last resort: the newest usage-bearing event of any shape
    for ev in reversed(events):
        usage, ev2 = _event_usage(ev)
        if usage is not None:
            if "usage" not in ev2:
                ev2 = dict(ev2)
                ev2["usage"] = usage
            return ev2
    return {}


@contextlib.contextmanager
def zcode_model_config(env, model):
    """Pin ZCode's model by rewriting its global CLI config, then restore it.

    ZCode has no --model flag and its documented --settings flag is not
    implemented (README 5.5), so the model is global state. That is why the
    runner is serial. The original file is always restored, including on error,
    and a rolling disk backup (config.json.bak-precell) is written before each
    rewrite: the in-memory restore dies with the process, so a kill mid-cell
    (operator stops the detached PID, power loss) is recovered by copying the
    backup back over config.json. The API key lives only in this file (and the
    backup, same 0o600 mode) and never in a run record.
    """
    path = Path(env.get("ZCODE_CONFIG_PATH")
                or os.path.expanduser("~/.zcode/cli/config.json"))
    for required in ("ZCODE_BASE_URL", "ZCODE_API_KEY"):
        if not env.get(required):
            raise RuntimeError(f"{required} is not set in .env")
    original = path.read_text(encoding="utf-8") if path.exists() else None
    backup = path.with_name(path.name + ".bak-precell")
    try:
        # Schema, decoded from the bundle and confirmed by experiment:
        #   provider: record<string, {kind, api, apiKey, options{baseURL}, ...}>
        #   model:    "provider/model"  |  {main?, lite?}   <- .strict(), no extras
        # The base URL must appear at options.baseURL. A top-level "baseURL" or
        # "api" alone both fail with "Model provider <id> is missing baseURL".
        pid = env.get("ZCODE_PROVIDER", "zai")
        base, key = env["ZCODE_BASE_URL"], env["ZCODE_API_KEY"]
        cfg = json.loads(original) if original else {}
        cfg["provider"] = {pid: {"kind": "openai-compatible", "api": base,
                                 "apiKey": key, "options": {"baseURL": base}}}
        cfg["model"] = {"main": f"{pid}/{model}"}
        path.parent.mkdir(parents=True, exist_ok=True)
        if original is not None:
            backup.write_text(original, encoding="utf-8")
            os.chmod(backup, 0o600)
        path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)  # holds a plaintext API key for the run window
        yield
    finally:
        if original is not None:
            path.write_text(original, encoding="utf-8")
        elif path.exists():
            path.unlink()


HARNESSES = {
    "claude": Harness(
        name="claude", accounting="exclusive",
        # --effort is real in 2.1.267 (low/medium/high/xhigh/max). Phase 0 ran
        # without it (model default); REASONING_EFFORT=medium is the phase-1
        # methodology (README 5.9), so effort is part of the cell key.
        build_cmd=lambda b, m, p, d: [b, "-p", p, "--model", m,
                                      "--effort", os.environ.get("REASONING_EFFORT", "medium"),
                                      "--output-format", "json",
                                      "--permission-mode", "acceptEdits"],
        read_usage=usage_from_result_json),
    "opencode": Harness(
        name="opencode", accounting="exclusive",
        # --dir is REQUIRED: without it OpenCode resolves the project root by
        # walking up and reviews an ancestor directory instead of the run copy.
        # It was removed once for "hanging"; the hang was actually inherited
        # stdin, fixed separately. Removing it silently produced runs that
        # audited the benchmark harness rather than the corpus repo.
        # --variant carries provider-specific reasoning effort; supported by
        # the opencode-hosted (Zen) muse-spark, not by the openrouter route.
        build_cmd=lambda b, m, p, d: [b, "run", p, "-m", m, "--dir", str(d),
                                      "--variant",
                                      os.environ.get("REASONING_EFFORT", "medium")],
        read_usage=usage_opencode),
    "zcode": Harness(
        name="zcode", accounting="inclusive",
        build_cmd=lambda b, m, p, d: [*b.split(), "--prompt", p,
                                      "--cwd", str(d), "--json"],
        read_usage=usage_zcode, setup=zcode_model_config),
    "qwen": Harness(
        # INCLUSIVE, verified against ~/.qwen/usage_record.jsonl:
        #   inputTokens 2,850,188 = uncached 117,742 + cached 2,732,446
        # Misclassifying this as exclusive charged the cached tokens twice,
        # once at the full input rate and once at the cache rate, overstating
        # one run by 6x. Same failure class as the Gemini cache-rate default.
        # stream-json replaces -o json (README 5.7): the JSON array buffered
        # until completion, so all three timed-out qwen baselines left empty
        # logs and zero forensic data. stream-json flushes each event as it
        # arrives; parse_stream_events recovers usage from a killed run.
        name="qwen", accounting="inclusive", stream=True,
        build_cmd=lambda b, m, p, d: [b, p, "-m", m, "-o", "stream-json",
                                      "--approval-mode", "yolo"],
        read_usage=usage_from_result_json),
    "agy": Harness(
        name="agy", accounting="exclusive",
        # agy has its own print-mode timeout, default 5m0s, which fires long
        # before the runner's. Keep it just under RUN_TIMEOUT_SECONDS so the
        # harness reports a clean ERROR rather than the runner killing it blind.
        build_cmd=lambda b, m, p, d: [b, "-p", p, "--model", m,
                                      "--effort", os.environ.get("REASONING_EFFORT",
                                                                os.environ.get("AGY_EFFORT", "medium")),
                                      "--print-timeout",
                                      os.environ.get("AGY_PRINT_TIMEOUT", "25m"),
                                      "--output-format", "json",
                                      "--dangerously-skip-permissions"],
        read_usage=usage_from_result_json),
}

# How each harness exposes reasoning effort, and whether it can be set at all.
#   flag         - CLI flag, tier taken from REASONING_EFFORT (default medium)
#   model-config - tier lives in the provider's model config, operator-side
#                  (Qwen Code: generationConfig.reasoning in settings.json)
#   None         - no headless control exists; the pair runs at harness default
# Verified 2026-09-10/11: zcode --effort returns "Unknown option" (the /effort
# command is TUI-only, and the strict config schema has no key); GLM's ladder
# is low/high/max with no medium tier. OpenCode has no flag and muse-spark is
# not a reasoning model on OpenRouter.
REASONING_MECHANISM = {
    "claude": "flag",
    "agy": "flag",
    "qwen": "model-config",
    "opencode": "variant",
    "zcode": None,
}


def reasoning_effort_for(hname: str) -> dict:
    """The per-run reasoning-effort record field. Part of the cell key."""
    mech = REASONING_MECHANISM.get(hname)
    if mech == "flag":
        return {"tier": os.environ.get("REASONING_EFFORT", "medium"),
                "mechanism": "cli --effort"}
    if mech == "variant":
        return {"tier": os.environ.get("REASONING_EFFORT", "medium"),
                "mechanism": "cli --variant (opencode zen models)"}
    if mech == "model-config":
        return {"tier": os.environ.get("REASONING_EFFORT", "medium"),
                "mechanism": "model config generationConfig.reasoning "
                             "(~/.qwen/settings.json, operator-side)"}
    return {"tier": "harness-default", "mechanism":
            "unsupported: no headless effort control in this harness"}


def _event_text(ev: dict) -> str:
    """The agent-visible text on one stream event, if any."""
    for k in ("result", "response", "text"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v
    msg = ev.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            parts = [p.get("text") for p in c
                     if isinstance(p, dict) and isinstance(p.get("text"), str)]
            if parts:
                return parts[-1]
    return ""


def final_response(ctx) -> str:
    """The agent's final answer text, which is what T1 is scored on.

    Raw stdout is a stream of events for most harnesses; the findings live on
    the terminal result. Keyed off observed shapes (see probe/*.json):
      claude -> result   agy -> response   qwen -> stream/array, terminal text
      opencode -> plain stdout            zcode -> result/response if present
    For a stream harness the last text-bearing event wins (the terminal
    `result` event on a completed qwen run; the last complete assistant text
    on a timed-out one).
    """
    if ctx.get("stream") and ctx.get("stdout_events"):
        for ev in reversed(ctx["stdout_events"]):
            t = _event_text(ev)
            if t:
                return t
    blob = ctx.get("stdout_json")
    if isinstance(blob, dict):
        for k in ("result", "response", "text", "output", "message"):
            v = blob.get(k)
            if isinstance(v, str) and v.strip():
                return v
    out = (ctx.get("stdout") or "").strip()
    if out.startswith("["):
        try:
            events = json.loads(out)
            texts = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                for k in ("result", "response", "text"):
                    v = ev.get(k)
                    if isinstance(v, str) and v.strip():
                        texts.append(v)
                msg = ev.get("message")
                if isinstance(msg, dict):
                    c = msg.get("content")
                    if isinstance(c, str) and c.strip():
                        texts.append(c)
                    elif isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                texts.append(part["text"])
            if texts:
                return texts[-1]
        except Exception:
            pass
    return out


def _read_tail(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if size > limit:
            f.seek(size - limit)
        return f.read()


def _kill_tree(pid: int):
    """Windows subprocess.kill leaves grandchildren alive; taskkill /T does not."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=60)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass


def _run_cmd(cmd, cwd=None, timeout: int = 180) -> dict:
    """Pipe-safe subprocess.run for cavet/docker teardown calls.

    run(timeout=...) kills the direct child on expiry but then waits for the
    stdout/stderr pipes to close — and on Windows a grandchild (docker.exe
    spawned by a cavet CLI, inheriting the handles) keeps them open forever,
    so the read blocks past the timeout. That froze one cell's teardown for
    30 minutes on 2026-09-18. Redirecting to files has no EOF to wait on, and
    a tree-kill on expiry cannot deadlock. Returns {rc, out, err, timed_out};
    rc is None when the call had to be killed.
    """
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None,
                                stdout=fo, stderr=fe,
                                stdin=subprocess.DEVNULL)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            rc = None
        fo.seek(0)
        fe.seek(0)
        out = fo.read().decode("utf-8", errors="replace")[-400:]
        err = fe.read().decode("utf-8", errors="replace")[-400:]
    return {"rc": rc, "out": out, "err": err, "timed_out": rc is None}


def actual_model(ctx) -> str:
    """The model the harness really used.

    `qwen -m qwen3.8-flash` silently ran qwen3.7-plus because that id is absent
    from ~/.qwen/settings.json and it falls back to the first configured entry.
    No error, no warning. Three records were costed against the wrong model
    before this was caught, so every run now asserts what actually answered.
    """
    blob = ctx.get("stdout_json")
    if isinstance(blob, dict):
        st = blob.get("stats") or {}
        if isinstance(st.get("models"), dict) and st["models"]:
            return next(iter(st["models"]))
        mu = blob.get("modelUsage")
        if isinstance(mu, dict) and mu:
            return next(iter(mu))
        if isinstance(blob.get("model"), str):
            return blob["model"]
    out = (ctx.get("stdout") or "")
    m = re.search(r'"model"\s*:\s*"([^"]+)"', out)
    return m.group(1) if m else ""


def invoke(h: Harness, binary: str, model: str, prompt: str, cwd: Path,
           timeout: int, log_dir: Path = None, env_extra: dict = None) -> dict:
    """Run one harness turn.

    stdout goes to a FILE, never to a pipe held in memory. An agent auditing a
    repository emits a continuous event stream; buffering it with
    capture_output=True is what exhausted memory and got the matrix killed twice.
    env_extra: the subprocess environment. cavet-arm cells pass a PATH that
    leads with the pinned cavet binary's directory, so the agent's own `cavet`
    calls resolve to the pinned build.
    """
    cmd = h.build_cmd(binary, model, prompt, cwd)
    log_dir = log_dir or cwd
    out_path, err_path = log_dir / "_stdout.log", log_dir / "_stderr.log"
    t0 = time.time()
    t0_ms = int(t0 * 1000)
    timed_out = False
    # shell=True is deliberately NOT used: on Windows it routes through cmd.exe
    # and the child loses `cwd`, which breaks directory-based run attribution.
    # Point HARNESS_*_BIN at the .cmd shim instead.
    # stdin must be closed: a harness that waits on it hangs the whole matrix.
    with open(out_path, "w", encoding="utf-8", errors="replace") as fo,          open(err_path, "w", encoding="utf-8", errors="replace") as fe:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=fo, stderr=fe,
                                stdin=subprocess.DEVNULL, shell=h.shell,
                                env=env_extra)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            rc = None
    wall = time.time() - t0

    size = out_path.stat().st_size if out_path.exists() else 0
    # Only the tail is ever needed: the terminal result event carries the usage
    # and the final answer. Reading a multi-hundred-MB stream defeats the point.
    tail = _read_tail(out_path, 4_000_000)
    blob = None
    stripped = tail.strip()
    if stripped.startswith("["):
        try:
            events = json.loads(stripped)
            for ev in reversed(events):
                if isinstance(ev, dict) and any(
                        isinstance(ev.get(k), dict) for k in
                        ("usage", "tokenUsage", "stats", "metrics")):
                    blob = ev
                    break
            if blob is None and events:
                blob = events[-1] if isinstance(events[-1], dict) else None
        except Exception:
            pass
    elif stripped.startswith("{"):
        try:
            blob = json.loads(stripped)
        except Exception:
            for line in reversed(stripped.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        blob = json.loads(line)
                        break
                    except Exception:
                        continue
    # Stream harnesses (qwen stream-json): parse the JSONL incrementally. The
    # terminal event of a completed run becomes the result blob; on a timed-out
    # run the last COMPLETE event still yields usage - the recovery that
    # `-o json`'s buffered array made impossible.
    stream_events = parse_stream_events(stripped) if h.stream else []
    if h.stream:
        if not blob and stream_events:
            blob = last_usage_event(stream_events)
        elif blob is not None and h.name == "qwen":
            # A completed run must cost on the terminal event's SESSION-TOTAL
            # usage, not on a mid-stream assistant snapshot.
            better = last_usage_event(stream_events)
            if better.get("type") == "result":
                blob = better
    return {"harness": h.name, "accounting": h.accounting, "cwd": cwd,
            "stream": h.stream, "stdout_events": stream_events,
            "t0_ms": t0_ms, "wall_s": wall, "rc": rc, "timed_out": timed_out,
            "stdout": tail, "stdout_bytes": size,
            "stderr": _read_tail(err_path, 200_000), "stdout_json": blob,
            "stdout_path": str(out_path), "cmd": cmd}


# --------------------------------------------------------------------------
# cavet scan-result parsing (arm C, plan §3.3)
# --------------------------------------------------------------------------

SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def parse_scan_result(text: str) -> dict:
    """Parse cavet's rendered scan result (a markdown table with an aggregate
    line, cli-spec §4.1) into structured findings.

    Row shape: | id | sev | rule | location | description |, where the
    severity cell carries verdict confidence as `high*` / `high^` for triaged
    rows (cli-spec §16.22). A description containing '|' is rejoined from the
    split tail; the header and separator rows are skipped.
    """
    header, aggregate_line = "", ""
    findings = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("scan:"):
            header = s
        elif (not s.startswith("|") and " confirmed " in f" {s} "
              and " dismissed" in s):
            aggregate_line = s
        elif s.startswith("|") and not s.startswith("|-"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 5 or cells[0] in ("id", ""):
                continue
            fid, sev, rule, loc = cells[0], cells[1], cells[2], cells[3]
            desc = "|".join(cells[4:])
            conf = ""
            if sev.endswith("*"):
                conf, sev = "high", sev[:-1]
            elif sev.endswith("^"):
                conf, sev = "low", sev[:-1]
            path, _, ln = loc.rpartition(":")
            try:
                lineno = int(ln)
            except ValueError:
                path, lineno = loc, 0
            findings.append({"id": fid, "sev": sev, "conf": conf,
                             "rule": rule, "path": path, "line": lineno,
                             "desc": desc})
    return {"header": header, "aggregate": aggregate_line,
            "findings": findings}


def dedupe_findings(findings: list) -> list:
    """CI handover granularity: one row per (file, rule) — README 4.2's
    'deduplicated by file and rule' — keeping the highest severity, the first
    description, and every observed location."""
    best = {}
    for f in findings:
        k = (f["path"], f["rule"])
        cur = best.get(k)
        if cur is None:
            best[k] = {**f, "locations": [f["line"]], "ids": [f["id"]]}
        else:
            if SEV_RANK.get(f["sev"], 9) < SEV_RANK.get(cur["sev"], 9):
                cur.update(sev=f["sev"], conf=f["conf"], desc=f["desc"],
                           id=f["id"])
            if f["line"] not in cur["locations"]:
                cur["locations"].append(f["line"])
            if f["id"] not in cur["ids"]:
                cur["ids"].append(f["id"])
    out = sorted(best.values(),
                 key=lambda f: (SEV_RANK.get(f["sev"], 9), f["path"],
                                f["rule"]))
    for f in out:
        f["n_locations"] = len(f["locations"])
    return out


def format_ci_comment(deduped: list, scan_meta: dict) -> str:
    """The CI-style findings comment — what a scanner bot posts on a branch,
    and the entire prompt context the arm C fix session gets (plan §3.3):
    severity, rule, location, detail. No task text, no arm-A response,
    nothing else from the original session."""
    n_raw = scan_meta.get("raw_count", len(deduped))
    head = (f"## Automated security scan: {len(deduped)} finding(s) "
            f"({n_raw} reported, deduplicated by file and rule)\n\n"
            "CI ran security scanners over this branch. The findings below "
            "are untriaged scanner output: review each one, fix what is "
            "real, and note what you judged a false positive.\n")
    if not deduped:
        return head + "\nThe scan reported no findings.\n"
    rows = ["| severity | rule | location | detail |", "|---|---|---|---|"]
    for f in deduped:
        locs = ", ".join(f"{f['path']}:{ln}" for ln in f["locations"][:5])
        if len(f["locations"]) > 5:
            locs += f" (+{len(f['locations']) - 5} more)"
        rows.append(f"| {f['sev']} | {f['rule']} | {locs} | "
                    f"{f['desc'] or '-'} |")
    meta = scan_meta.get("header", "")
    return (head + "\n" + "\n".join(rows)
            + (f"\n\nScan: {meta}" if meta else "\n"))


# --------------------------------------------------------------------------
# corpus preparation
# --------------------------------------------------------------------------

# All prose goes. Markdown and reStructuredText are documentation by definition,
# and AGENTS.md / CLAUDE.md are agent instruction files that would otherwise hand
# the agent a briefing the operator never wrote. LICENSE stays.
STRIP_SUFFIXES = {".md", ".rst", ".adoc"}
STRIP_NAMES = {"readme", "readme.txt", "authors", "codeowners", "notice"}
STRIP_DIRS = {"docs", "doc", "documentation", ".github", "examples", "example"}
KEEP_PREFIXES = ("license", "licence", "copying")


def _rmtree(path: Path):
    """Windows marks git object files read-only; plain rmtree leaves them."""
    def onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass
    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def _safe_component(s: str) -> str:
    # ponytail: naive name check, real containment is enforced by the caller
    # resolving under a fixed base dir; upgrade to resolve()+relative_to if
    # untrusted input ever reaches these paths.
    if not s or s in (".", "..") or "/" in s or "\\" in s or "\x00" in s:
        raise ValueError(f"unsafe path component: {s!r}")
    return s


def _check_repo_ref(repo: str, ref: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"bad CORPUS repo {repo!r}: want 'owner/name'")
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", ref):
        raise ValueError(f"bad CORPUS ref {ref!r}: want a hex commit SHA")


def prepare_repo(repo: str, ref: str, dest: Path) -> dict:
    _check_repo_ref(repo, ref)
    _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    subprocess.run(["git", "clone", "--quiet", "--", url, str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", ref], check=True)

    # 1. de-identify: drop history, prose, CI metadata
    _rmtree(dest / ".git")
    removed = []
    for p in sorted(dest.rglob("*")):
        if not p.exists():
            continue
        rel = p.relative_to(dest)
        if p.is_dir() and p.name.lower() in STRIP_DIRS:
            _rmtree(p); removed.append(str(rel)); continue
        if p.is_file():
            low = p.name.lower()
            if low.startswith(KEEP_PREFIXES):
                continue
            if low in STRIP_NAMES or p.suffix.lower() in STRIP_SUFFIXES:
                p.unlink(missing_ok=True); removed.append(str(rel))

    # 2. re-init: cavet needs a git repo, and scan --staged/--diff need commits.
    #    Without this every arm B run fails. See README 3.4.
    subprocess.run(["git", "-C", str(dest), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(dest), "-c", "user.name=bench",
                    "-c", "user.email=bench@localhost", "commit", "--quiet",
                    "-m", "corpus root"], check=True)
    root = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    files = [p for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts]
    langs = {}
    for p in files:
        langs[p.suffix.lower() or "(none)"] = langs.get(p.suffix.lower() or "(none)", 0) + 1
    return {"repo": repo, "ref": ref, "root_commit": root,
            "files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "ext_histogram": dict(sorted(langs.items(), key=lambda kv: -kv[1])[:12]),
            "removed_count": len(removed)}


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_selftest(_args):
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r} want {want!r}")

    # observed ZCode row: input includes the cache reads
    z = normalise("inclusive", 64383, 63296, 0, 468)
    check("zcode uncached", z.uncached_input, 1087)
    check("zcode total_prompt", z.total_prompt, 64383)

    # observed OpenCode row: input excludes them
    o = normalise("exclusive", 36158, 284928, 0, 592)
    check("opencode uncached", o.uncached_input, 36158)
    check("opencode total_prompt", o.total_prompt, 321086)

    # the trap: applying the wrong convention to the ZCode row
    wrong = normalise("exclusive", 64383, 63296, 0, 468)
    if wrong.total_prompt <= z.total_prompt:
        fails.append("expected the wrong convention to inflate the prompt total")

    # wrong convention the other way must be caught, not silently negative
    try:
        normalise("inclusive", 36158, 284928, 0, 592)
        fails.append("expected negative-uncached guard to raise")
    except ValueError:
        pass

    # cost: 1M uncached input on Opus 5 is $5.00
    check("opus input cost", cost_usd("claude-opus-5",
          Usage(uncached_input=1_000_000)), 5.0)
    # cache read is 0.1x input
    check("opus cache read", cost_usd("claude-opus-5",
          Usage(cache_read=1_000_000)), 0.5)
    # qwen flash: explicit cache create is 12.5x its read rate, must not collapse
    check("qwen flash cache write", cost_usd("qwen3.8-flash",
          Usage(cache_write=1_000_000)), 0.2)
    check("qwen flash cache read", cost_usd("qwen3.8-flash",
          Usage(cache_read=1_000_000)), 0.016)
    # alias resolution: contributor variant prices at standard rates
    check("muse alias", cost_usd("opencode/muse-spark-1.3-contributor-free",
          Usage(uncached_input=1_000_000)), 1.25)
    # REGRESSION: reconcile against Claude Code's own reported cost.
    # Observed probe 2026-09-08, claude-opus-5: input 2, output 5,
    # cache_read 13198, cache_creation 28188 all at 1-hour TTL.
    # Claude reported total_cost_usd = 0.288614. Computing the 1h write at the
    # 5-minute rate gives 0.1829, a 37% undercount, which is how this was found.
    obs = Usage(uncached_input=2, output=5, cache_read=13198, cache_write_1h=28188)
    got = cost_usd("claude-opus-5", obs)
    if abs(got - 0.288614) > 0.000002:
        fails.append(f"claude cost reconciliation: got {got} want 0.288614")

    # unknown model must fail loudly rather than price at zero
    try:
        cost_usd("no-such-model", Usage(uncached_input=1000))
        fails.append("expected unknown model to raise")
    except KeyError:
        pass

    # ---- qwen stream-json fixtures (plan §2.3) ----
    # Shape pinned from the observed phase-0 probe (probe/qwen.stdout.txt,
    # -o json): events system-init / assistant(message.usage per turn) /
    # terminal result with SESSION-TOTAL usage and stats.models. stream-json
    # emits the same events, one per line.
    stream_fixture = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                    "model": "qwen3.7-plus"}),
        json.dumps({"type": "assistant", "session_id": "s1",
                    "message": {"role": "assistant", "model": "qwen3.7-plus",
                                "content": [{"type": "text", "text": "half"}],
                                "usage": {"input_tokens": 1000,
                                          "output_tokens": 10,
                                          "cache_read_input_tokens": 800}}}),
        json.dumps({"type": "result", "subtype": "success",
                    "session_id": "s1", "result": "audit text",
                    "usage": {"input_tokens": 50000, "output_tokens": 900,
                              "cache_read_input_tokens": 40000},
                    "stats": {"models": {"qwen3.7-plus": {"tokens": {
                        "prompt": 50000}}}}}),
    ])
    evs = parse_stream_events(stream_fixture)
    check("stream fixture event count", len(evs), 3)
    blob = last_usage_event(evs)
    check("stream terminal event type", blob.get("type"), "result")
    check("stream final_response", final_response(
        {"stream": True, "stdout_events": evs, "stdout_json": blob}), "audit text")
    check("stream actual_model", actual_model(
        {"stdout_json": blob, "stdout": ""}), "qwen3.7-plus")
    # INCLUSIVE accounting: input_tokens 50000 already contains the 40000
    # cached tokens; treating them as exclusive would double-count.
    u_stream = normalise("inclusive",
                         blob["usage"]["input_tokens"],
                         blob["usage"].get("cache_read_input_tokens", 0),
                         0, blob["usage"]["output_tokens"])
    check("stream uncached (inclusive rule)", u_stream.uncached_input, 10000)
    check("stream cache_read", u_stream.cache_read, 40000)
    # 10000*0.32 + 900*1.28 + 40000*0.064 = 6912 micro-USD-per-token sum / 1M
    check("stream qwen3.7-plus cost",
          cost_usd("qwen3.7-plus", u_stream), 0.006912)

    # Timeout recovery: the run is killed mid-write; the final line is
    # truncated and there is no result event. The last COMPLETE event must
    # still yield usage (lifted from message.usage), costed inclusively.
    truncated_fixture = stream_fixture.splitlines()[0] + "\n" + \
        stream_fixture.splitlines()[1] + "\n" + '{"type":"result","subty'
    evs_t = parse_stream_events(truncated_fixture)
    check("truncated stream complete events", len(evs_t), 2)
    blob_t = last_usage_event(evs_t)
    check("truncated stream lifts message.usage",
          (blob_t.get("usage") or {}).get("input_tokens"), 1000)
    u_t = normalise("inclusive",
                    blob_t["usage"]["input_tokens"],
                    blob_t["usage"].get("cache_read_input_tokens", 0),
                    0, blob_t["usage"]["output_tokens"])
    check("truncated stream uncached", u_t.uncached_input, 200)
    # Multi-turn timeout: each assistant message carries only its own turn's
    # usage, so recovery must SUM unique messages. The previous last-event
    # lift reported one turn out of 108 as the whole session on a real 1800s
    # qwen3.8-max timeout (2026-09-18): $2.05 of spend read as $0.00.
    a1 = json.dumps({"type": "assistant", "message": {"id": "m1",
        "model": "qwen3.8-max",
        "usage": {"input_tokens": 1000, "output_tokens": 50,
                  "cache_read_input_tokens": 800}}})
    a2 = json.dumps({"type": "assistant", "message": {"id": "m2",
        "model": "qwen3.8-max",
        "usage": {"input_tokens": 2000, "output_tokens": 70,
                  "cache_read_input_tokens": 1900}}})
    multi = stream_fixture.splitlines()[0] + "\n" + a1 + "\n" + a2 + \
        "\n" + a1 + "\n" + '{"trun'  # duplicate id must not double-count
    evs_m = parse_stream_events(multi)
    blob_m = last_usage_event(evs_m)
    check("multi-turn timeout sums input", blob_m["usage"]["input_tokens"], 3000)
    check("multi-turn timeout sums output", blob_m["usage"]["output_tokens"], 120)
    check("multi-turn dedupe by message id",
          blob_m["usage"]["cache_read_input_tokens"], 2700)
    check("multi-turn recovery flagged", blob_m.get("recovered_from_stream"), True)
    u_m = normalise("inclusive", blob_m["usage"]["input_tokens"],
                    blob_m["usage"]["cache_read_input_tokens"], 0,
                    blob_m["usage"]["output_tokens"])
    check("multi-turn uncached", u_m.uncached_input, 300)
    # legacy -o json whole-array output must keep parsing
    check("legacy array parses", len(parse_stream_events(
        "[" + stream_fixture.replace("\n", ",") + "]")), 3)

    # ---- cavet scan-result parsing fixtures (arm C, plan §3.3) ----
    # Shape pinned from internal/output/render.go (RenderResult, §16.22
    # confidence glyphs). Description containing '|' must rejoin.
    scan_fixture = (
        "scan: full workspace · scanners: gitleaks,trivy,opengrep · "
        "phase: build · engine: cdaa96cc\n"
        "3 confirmed (high, medium): 1 high confidence, 2 low confidence "
        "· 5 dismissed · 0 new suppressions · baseline 12\n\n"
        "| id     | sev      | rule              | location          | "
        "description              |\n"
        "|--------|----------|-------------------|-------------------|"
        "--------------------------|\n"
        "| a3f9c2 | high*    | py.sql-injection  | api/users.py:88   | "
        "user input concat         |\n"
        "| 7b1e04 | high^    | generic.weak-hash | auth/tokens.py:23 | "
        "MD5 used for tokens       |\n"
        "| 9c2d11 | medium   | py.sql-injection  | api/users.py:90   | "
        "same sink, other line     |\n"
        "| 4d5e22 | medium   | x.dupe            | cfg/a.yml:1       | "
        "desc with | pipe char      |\n"
    )
    sr = parse_scan_result(scan_fixture)
    check("scan header parsed", sr["header"].startswith("scan: full workspace"), True)
    check("scan aggregate parsed", "3 confirmed" in sr["aggregate"], True)
    check("scan findings parsed", len(sr["findings"]), 4)
    f0 = sr["findings"][0]
    check("scan conf glyph high*", (f0["sev"], f0["conf"]), ("high", "high"))
    check("scan path:line split", (f0["path"], f0["line"]), ("api/users.py", 88))
    dd = dedupe_findings(sr["findings"])
    check("scan dedupe by file+rule", len(dd), 3)
    check("scan dedupe keeps highest severity first",
          (dd[0]["sev"], dd[0]["rule"]), ("high", "py.sql-injection"))
    check("scan dedupe merges locations",
          sorted(dd[0]["locations"]), [88, 90])
    ci = format_ci_comment(dd, {"raw_count": 4, "header": sr["header"]})
    check("ci comment counts deduped findings",
          "3 finding(s) (4 reported" in ci, True)
    check("ci comment carries severity+location",
          ("| high | py.sql-injection |" in ci) and ("api/users.py:88" in ci),
          True)

    if fails:
        print("SELFTEST FAILED")
        for f in fails:
            print("  -", f)
        return 1
    print(f"selftest ok ({RATES['checked_on']} rates)")
    return 0


def cmd_probe(args):
    env = load_env()
    WORK.mkdir(parents=True, exist_ok=True)
    out_dir = ROOT / "probe"
    out_dir.mkdir(exist_ok=True)
    targets = args.harness or ["claude", "opencode", "zcode", "qwen", "agy"]
    binmap = {"claude": env.get("HARNESS_CLAUDE_BIN", "claude"),
              "opencode": env.get("HARNESS_OPENCODE_BIN", "opencode"),
              "zcode": env.get("HARNESS_ZCODE_BIN", ""),
              "qwen": env.get("HARNESS_QWENCODE_BIN", "qwen"),
              "agy": env.get("HARNESS_ANTIGRAVITY_BIN", "agy")}
    modelmap = {"claude": env.get("MODELS_CLAUDE", "").split(",")[0],
                "opencode": env.get("MODELS_OPENCODE", "").split(",")[0],
                "zcode": env.get("MODELS_ZCODE", "").split(",")[0],
                "qwen": env.get("MODELS_QWENCODE", "").split(",")[0],
                "agy": env.get("MODELS_ANTIGRAVITY", "").split(",")[0]}
    for name in targets:
        h = HARNESSES[name]
        d = WORK / f"probe-{name}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        # OpenCode resolves session.directory to a project root; give the
        # probe dir the same git marker prepared corpora have (README 3.4).
        if not (d / ".git").exists():
            subprocess.run(["git", "-C", str(d), "init", "--quiet"], check=False)
        binary, model = binmap[name], modelmap[name]
        if not binary:
            print(f"{name:9} SKIP (no binary configured in .env)"); continue
        print(f"{name:9} probing with model={model!r} ...")
        try:
            setup = h.setup(env, model) if h.setup else contextlib.nullcontext()
            with setup:
                ctx = invoke(h, binary, model,
                             "Reply with exactly the word PONG.", d,
                             timeout=int(env.get("RUN_TIMEOUT_SECONDS", 900)))
        except Exception as e:
            print(f"{name:9} INVOKE FAILED: {e}"); continue
        rec = {"rc": ctx["rc"], "wall_s": round(ctx["wall_s"], 2),
               "cmd": ctx["cmd"], "stdout_head": (ctx["stdout"] or "")[:4000],
               "stderr_head": (ctx["stderr"] or "")[:2000],
               "stdout_json_keys": sorted(ctx["stdout_json"].keys())
                                   if ctx["stdout_json"] else None}
        try:
            u = h.read_usage(ctx)
            rec["usage"] = asdict(u)
            rec["cost_usd"] = cost_usd(model, u)
            print(f"{name:9} ok  uncached={u.uncached_input} out={u.output} "
                  f"cache_r={u.cache_read} cost=${rec['cost_usd']:.4f} "
                  f"src={u.source}")
        except Exception as e:
            rec["usage_error"] = str(e)
            print(f"{name:9} ran, but usage extraction failed: {e}")
        (out_dir / f"{name}.json").write_text(
            json.dumps(rec, indent=2, default=str), encoding="utf-8")
        (out_dir / f"{name}.stdout.txt").write_text(
            ctx["stdout"] or "", encoding="utf-8")
    print(f"\nraw probe records in {out_dir}")
    print("Wire any failing extractor from these before running the matrix.")
    return 0


def cmd_prepare(args):
    env = load_env()
    REPOS.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for cid, c in corpus_from_env(env).items():
        if not c["ref"]:
            sys.exit(f"{cid}: CORPUS_{cid.split('-')[1]}_REF empty. "
                     f"Pin a commit SHA; a branch is not reproducible.")
        dest = REPOS / _safe_component(cid)
        print(f"preparing {cid} (lang {c['lang']}, ratio {c['ratio']}) ...")
        m = prepare_repo(c["repo"], c["ref"], dest)
        m["lang"] = c["lang"]
        m["ratio"] = c["ratio"]
        # Upstream identity (owner/name, pinned ref) stays in .env; the
        # published manifest describes the prepared tree only.
        del m["repo"], m["ref"]
        manifest[cid] = m
        print(f"   {m['files']} files, {m['bytes']//1024} KB, "
              f"stripped {m['removed_count']} paths, root {m['root_commit'][:8]}")
    (ROOT / "corpus-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nmanifest: {ROOT / 'corpus-manifest.json'}")
    return 0



# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

DIRECTIVE = (
    "Complete this task end to end. Do not ask clarifying questions. Where a "
    "detail is unspecified, choose a sensible default consistent with the "
    "existing code and note the choice in your final summary. Finish with the "
    "work committed on the current branch."
)

T1 = ("Review this repository for security vulnerabilities. For each issue give "
      "the file and line, a severity, and why it matters. " + DIRECTIVE)


def load_t2(cid: str) -> str:
    """T2 task text per corpus id, from a private file.

    Task text names in-repo file paths and codebase details by design, which
    is exactly what makes a subject identifiable by code search, so the real
    tasks live in t2.local.json (gitignored). t2.example.json shows the schema.
    """
    f = ROOT / "t2.local.json"
    if not f.exists():
        raise FileNotFoundError(
            f"no t2.local.json in {ROOT}. Copy t2.example.json and write one "
            f"task per corpus-<n> id. Task text is kept private: it quotes "
            f"in-repo paths that identify the subject.")
    tasks = json.loads(f.read_text(encoding="utf-8"))
    if cid not in tasks:
        raise FileNotFoundError(f"t2.local.json has no task for {cid}")
    return tasks[cid].strip() + " " + DIRECTIVE


def pairs_from_env(env) -> list:
    out = []
    for hname, key in (("claude", "MODELS_CLAUDE"), ("zcode", "MODELS_ZCODE"),
                       ("qwen", "MODELS_QWENCODE"), ("agy", "MODELS_ANTIGRAVITY"),
                       ("opencode", "MODELS_OPENCODE")):
        for m in [x.strip() for x in env.get(key, "").split(",") if x.strip()]:
            out.append((hname, m))
    return out


def harness_binary(env, name) -> str:
    return env.get({"claude": "HARNESS_CLAUDE_BIN",
                    "opencode": "HARNESS_OPENCODE_BIN",
                    "zcode": "HARNESS_ZCODE_BIN",
                    "qwen": "HARNESS_QWENCODE_BIN",
                    "agy": "HARNESS_ANTIGRAVITY_BIN"}[name], name)


def acquire_run_lock() -> Path:
    """Serial-runner lock. Two concurrent matrices exhaust memory and
    duplicate records; ZCode's model config is global besides (README 5.5)."""
    lock = ROOT / ".run.lock"
    if lock.exists():
        old_pid = lock.read_text(encoding="utf-8").strip()
        if os.name == "nt":
            alive = subprocess.run(["tasklist", "/FI", f"PID eq {old_pid}"],
                                   capture_output=True, text=True).stdout
            running = bool(old_pid) and old_pid in alive
        else:
            try:
                os.kill(int(old_pid), 0)
                running = True
            except (ValueError, OSError):
                running = False
        if running:
            sys.exit(f"another run is active (pid {old_pid}). Two concurrent "
                     f"matrices exhaust memory and duplicate records.")
        print(f"stale lock from dead pid {old_pid}, taking over")
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def cmd_run(args):
    env = load_env()
    if (args.arm and "armc" in args.arm) or args.from_a:
        if args.arm and set(args.arm) - {"armc"}:
            sys.exit("--from-a runs only the arm C fix session; pass --arm "
                     "armc without other arms (plan §3.3)")
        if not args.from_a:
            sys.exit("armc requires --from-a <arm-A baseline T2 run_id>")
        if args.task:
            sys.exit("armc always fixes a T2 outcome; do not pass --task")
        lock = acquire_run_lock()
        try:
            return _run_armc(args, env)
        finally:
            lock.unlink(missing_ok=True)
    lock = acquire_run_lock()
    try:
        return _run_matrix(args, env)
    finally:
        lock.unlink(missing_ok=True)


def load_records(dir_path: Path = None) -> dict:
    """run_id -> record, for every valid record directly in a runs directory.
    Non-recursive by design: runs/phase0/ (archived) and runs/pilot/
    (verification) stay out of the phase-1 duplicate guard unless asked for
    by name."""
    base = dir_path or RUNS
    out = {}
    if base.exists():
        for p in base.glob("*.json"):
            r = json.loads(p.read_text(encoding="utf-8"))
            if "run_id" not in r:
                continue  # scheduler-state.json and other non-record files
            out[r["run_id"]] = r
    return out


def record_key(r: dict) -> tuple:
    c = r.get("corpus", {})
    return (c.get("repo_id", c.get("repo", "")), r["harness"]["name"],
            r["harness"]["model"], r["arm"], r["task"], int(r["rep"]),
            (r.get("reasoning_effort") or {}).get("tier", ""))


def assert_tree_root(cid: str, d: Path, manifest: dict):
    """Tree assert (plan §2.2): a cell's work clone must root exactly at the
    corpus pin in corpus-manifest.json. A drifted tree would make the diff,
    the scan and the SHA pin meaningless, so the cell aborts loudly before
    any spend."""
    want = (manifest.get(cid) or {}).get("root_commit", "")
    if not want:
        sys.exit(f"TREE ASSERT FAILED: corpus-manifest.json has no "
                 f"root_commit for {cid}. Run `bench.py prepare` first.")
    got = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    if got != want:
        sys.exit(f"TREE ASSERT FAILED for {cid}: work clone root is "
                 f"{got[:12] or '(none)'} but the manifest pins "
                 f"{want[:12]}. Refusing to spend on a drifted tree.")


def run_cell(env, cid, hname, model, arm, task, rep, manifest, *, timeout,
             out_dir: Path = None, logs_base: Path = None, pilot: bool = False,
             keep_work: bool = False) -> dict:
    """Execute one cell end to end and write its record.

    One cell = fresh prepared clone -> tree assert -> (cavet init with the
    pinned binary) -> harness session -> usage/cost -> diff vs the root
    commit -> (engine stop + scoped container cleanup). The record lands in
    out_dir (runs/, or runs/pilot/ under --pilot, with logs under
    runs/logs/pilot-<run_id>/). Raises SystemExit on a failed tree assert.
    """
    out_dir = out_dir or RUNS
    logs_base = logs_base or LOGS
    if pilot:
        out_dir = PILOT_RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = _safe_component(model.replace("/", "-"))
    rid = (f"{_safe_component(cid)}_{_safe_component(hname)}_{safe_model}_"
           f"{_safe_component(arm)}_{_safe_component(task)}_r{int(rep)}_"
           f"{uuid.uuid4().hex[:6]}")
    d = WORK / rid
    _rmtree(d)
    shutil.copytree(REPOS / _safe_component(cid), d)
    assert_tree_root(cid, d, manifest)
    # Prep leaves exactly one root commit; the execution directive tells
    # the agent to COMMIT its work. Diffing against HEAD after the run
    # would therefore show a clean tree (observed: every T1 record had
    # diff_bytes 0). Diff against the root commit instead, so committed
    # and uncommitted work are both captured. `cavet init` adds no commits.
    root_sha = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    print(f"[{rid}]")

    prov_start = prov_end = None
    cavet_init_rc = None
    if arm == "cavet":
        prov_start = cavet_provenance(env)
        r = cavet_init(env, d)
        cavet_init_rc = r["rc"]
        if r["rc"] != 0:
            print(f"   cavet init failed rc={r['rc']}: {r['err'][:200]}")

    rec_stop = None
    prompt = T1 if task == "T1" else load_t2(cid)
    h = HARNESSES[hname]
    started = time.time()
    ctx = {}          # never inherit the previous run's context on failure
    try:
        logs = logs_base / (f"pilot-{rid}" if pilot else rid)
        logs.mkdir(parents=True, exist_ok=True)
        setup = h.setup(env, model) if h.setup else contextlib.nullcontext()
        with setup:
            ctx = invoke(h, harness_binary(env, hname), model, prompt, d,
                         timeout, log_dir=logs,
                         env_extra=cavet_cell_env(env, arm))
        if ctx.get("timed_out"):
            raise TimeoutError(f"no result after {timeout}s")
        u = h.read_usage(ctx)
        err = None
    except Exception as e:
        u, err = Usage(), f"{type(e).__name__}: {e}"
    # Costing is separate: a missing rate must not discard a measurement that
    # was read successfully. Tokens are the measurement, dollars are derived,
    # and `bench.py recost` fills them in once the rate card is corrected.
    used_model = actual_model(ctx) if ctx else ""
    if used_model and used_model != model:
        err = (err + " | " if err else "") + (
            f"MODEL MISMATCH: requested {model}, harness used {used_model}")
    usd = 0.0
    try:
        if u.total_prompt or u.output:
            usd = cost_usd(model, u)
    except Exception as e:
        err = (err + " | " if err else "") + f"costing: {e}"
    wall = time.time() - started

    diff = subprocess.run(["git", "-C", str(d), "diff", root_sha or "HEAD"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout
    if arm == "cavet":
        # `cavet init` starts a long-lived engine container. Deleting the
        # repo directory orphans it, and they accumulate. Stop it BEFORE the
        # directory goes, while cavet can still find .cavet/ to identify its
        # own container; scoped fallback + prune come after.
        prov_end = cavet_provenance(env)
        rec_stop = stop_engine_scoped(env, d)

    mf_entry = manifest.get(cid, {})
    rec = {
        "run_id": rid, "started": started, "wall_s": round(wall, 1),
        "corpus": {"repo_id": cid,
                   "repo_hash": mf_entry.get("root_commit", ""),
                   "lang": mf_entry.get("lang", ""),
                   "ratio": mf_entry.get("ratio", ""),
                   "task": task},
        "harness": {"name": hname, "model": model},
        "arm": arm, "task": task, "rep": rep,
        "pilot": pilot,
        "cavet_init_rc": cavet_init_rc,
        "cavet_provenance": ({"start": prov_start, "end": prov_end}
                             if arm == "cavet" else None),
        "cost": {"usd": usd, "wall_s": round(wall, 1),
                 "uncached_input": u.uncached_input, "output_tokens": u.output,
                 "cache_read": u.cache_read, "cache_write": u.cache_write,
                 "cache_write_1h": u.cache_write_1h, "source": u.source},
        "harness_rc": ctx.get("rc"), "error": err,
        "timed_out": bool(ctx.get("timed_out")),
        "reasoning_effort": reasoning_effort_for(hname),
        "model_actual": used_model,
        "model_mismatch": bool(used_model and used_model != model),
        "engine_stop": rec_stop if arm == "cavet" else None,
        "stdout_bytes": ctx.get("stdout_bytes", 0),
        "diff_bytes": len(diff),
        "final_response": final_response(ctx)[:60000] if ctx else "",
        "artifacts": {"stdout": (ctx.get("stdout") or "")[:20000],
                      "diff": diff[:200000]},
    }
    # Provider quota/allocation markers, recorded on the record and used by
    # the rotation scheduler to defer a pair instead of poisoning the matrix.
    low = ((rec["final_response"] or "")
           + (ctx.get("stderr") or "")).lower()
    rec["provider_blocked"] = sorted({b for b in QUOTA_MARKERS if b in low})
    if not keep_work:
        _rmtree(d)
    # Agents quote the tree they were given (package names in manifests,
    # import paths), so the owner/name and bare name are scrubbed from the
    # record's strings before it lands. The name only ever exists in .env.
    rec = scrub(rec, {n: cid for n in name_variants(corpus_from_env(env)[cid]["repo"])})
    # Full diff sidecar for arm C (the record field is deliberately capped).
    try:
        (logs_base / (f"pilot-{rid}" if pilot else rid)
         ).mkdir(parents=True, exist_ok=True)
        (logs_base / (f"pilot-{rid}" if pilot else rid) / "diff.patch"
         ).write_text(diff, encoding="utf-8")
    except OSError:
        pass
    (out_dir / f"{rid}.json").write_text(json.dumps(rec, indent=2,
                                                    default=str),
                                         encoding="utf-8")
    status = err or f"rc={ctx.get('rc')}"
    print(f"   {status} | {wall:.0f}s | ${usd:.4f}")
    return rec


def _run_matrix(args, env):
    out_dir = PILOT_RUNS if args.pilot else RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    cap = float(env.get("BUDGET_USD_CAP", 25))
    timeout = int(env.get("RUN_TIMEOUT_SECONDS", 900))
    if args.pilot:
        print("PILOT MODE: records land in runs/pilot/ - outside the "
              "duplicate guard, report aggregates and phase-1 cell keys.\n")

    corpus = corpus_from_env(env)
    if not corpus:
        sys.exit("no CORPUS_*_REPO entries in .env")
    existing = load_records(out_dir)
    spent = sum(r["cost"]["usd"] for r in existing.values())
    print(f"already spent ${spent:.4f} of ${cap:.2f} cap "
          f"({'runs/pilot/' if args.pilot else 'runs/'})\n")

    ids = args.repo or list(corpus)
    unknown = [i for i in ids if i not in corpus]
    if unknown:
        sys.exit(f"unknown corpus id(s) {unknown}. Pass corpus-<n> as listed "
                 f"in .env; repo names are never used on the command line.")
    pairs = [p for p in pairs_from_env(env)
             if not args.pair or f"{p[0]}/{p[1]}" in args.pair]
    arms = args.arm or ["baseline", "cavet"]
    tasks = args.task or ["T1"]

    if "cavet" in arms:
        d = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if d.returncode != 0:
            sys.exit("arm 'cavet' needs a running Docker daemon; `docker info` failed.")

    plan = [(r, h, m, a, t, k)
            for r in ids for (h, m) in pairs for a in arms
            for t in tasks for k in range(1, args.reps + 1)]
    # A cell is one (repo, pair, arm, task, rep); reruns must replace the old
    # record, never stack a second one next to it. Pilot runs are excluded:
    # verification must never occupy, or be blocked by, phase-1 cell keys.
    if not args.pilot:
        for row in plan:
            key = (row[0], row[1], row[2], row[3], row[4], row[5])
            dups = [r for r in existing.values() if record_key(r) == key]
            for d in dups:
                if not args.force:
                    sys.exit(f"cell {key} already has record {d['run_id']}. "
                             f"Rerunning would create a duplicate. Use --force to "
                             f"quarantine the old record into runs/invalid/ first.")
                q = INVALID_RUNS
                q.mkdir(parents=True, exist_ok=True)
                d["invalid_reason"] = (f"superseded by rerun of cell {key} "
                                       f"on {time.strftime('%Y-%m-%d %H:%M')}")
                (q / f"{d['run_id']}.json").write_text(
                    json.dumps(d, indent=2, default=str), encoding="utf-8")
                (RUNS / f"{d['run_id']}.json").unlink()
                del existing[d["run_id"]]
                print(f"quarantined superseded record {d['run_id']}")

    print(f"{len(plan)} runs planned: {len(ids)} repo(s) x {len(pairs)} pair(s)"
          f" x {len(arms)} arm(s) x {len(tasks)} task(s) x {args.reps} rep(s)")
    if args.dry_run:
        for row in plan:
            print("  ", " | ".join(str(x) for x in row))
        return 0

    manifest = {}
    mf = ROOT / "corpus-manifest.json"
    if mf.exists():
        manifest = json.loads(mf.read_text(encoding="utf-8"))

    for cid, hname, model, arm, task, rep in plan:
        if spent >= cap:
            print(f"\nBUDGET CAP REACHED (${spent:.4f} >= ${cap:.2f}). Stopping.")
            break
        if not (REPOS / _safe_component(cid)).exists():
            sys.exit(f"{REPOS / cid} not prepared. Run `bench.py prepare` first.")
        rec = run_cell(env, cid, hname, model, arm, task, rep, manifest,
                       timeout=timeout, out_dir=out_dir, pilot=args.pilot,
                       keep_work=args.keep_work)
        spent += rec["cost"]["usd"]
        if rec.get("provider_blocked"):
            print(f"   ABORT: {hname} is blocked by the provider "
                  f"({', '.join(rec['provider_blocked'])}). Remaining runs for "
                  f"this pair would produce more of the same. Stopping the matrix.")
            break
        print(f"   running ${spent:.4f}")
    print(f"\ntotal this session: ${spent:.4f}")
    return 0


def find_record(run_id: str):
    """Locate a record by run_id across runs/, runs/pilot/ and runs/phase0/.
    Returns (record, path) or (None, None)."""
    for base in (RUNS, PILOT_RUNS, PHASE0_RUNS):
        p = base / f"{run_id}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")), p
    return None, None


def _run_armc(args, env):
    """Arm C, the traditional-CI comparator (plan §3.3, README 3.1).

    Input: one arm-A (baseline) T2 record via --from-a. Then:
      fresh prepared clone -> tree assert -> pinned `cavet init` ->
      apply arm A's captured diff, commit it ->
      headless pinned `cavet scan --full` (no agent, exactly as CI would) ->
      dedupe by file+rule, attach severities, render a CI-style comment ->
      a FRESH harness session (same pair as arm A) whose prompt is that
      comment plus the T2 commit directive and NOTHING else from arm A's
      context -> record C_fix, wall time, post-fix diff (vs the applied
      commit, .cavet excluded) and a post-fix scan with the same baseline.

    The post-fix scan needs the SAME .cavet state (baseline = pristine corpus
    root) across the fix session, so .cavet/ stays in place; the post-fix
    diff excludes it so audit-trail churn can never enter the captured diff.
    Cost-to-clean C_A + C_fix lands on the record and in `report`.
    """
    out_dir = PILOT_RUNS if args.pilot else RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    cap = float(env.get("BUDGET_USD_CAP", 25))
    timeout = int(env.get("RUN_TIMEOUT_SECONDS", 900))
    scan_timeout = int(env.get("SCAN_TIMEOUT_SECONDS", 1200))
    corpus = corpus_from_env(env)

    a, apath = find_record(args.from_a)
    if not a:
        sys.exit(f"no record '{args.from_a}' in runs/, runs/pilot/ or runs/phase0/")
    if a.get("arm") != "baseline" or a.get("task") != "T2":
        sys.exit(f"{args.from_a} is arm={a.get('arm')} task={a.get('task')}; "
                 f"arm C consumes an arm-A (baseline) T2 record")
    cid = a.get("corpus", {}).get("repo_id", "")
    if cid not in corpus:
        sys.exit(f"arm-A record's corpus id {cid!r} is not in .env")
    hname, model = a["harness"]["name"], a["harness"]["model"]
    rep = int(a.get("rep", 1))
    manifest = {}
    mf = ROOT / "corpus-manifest.json"
    if mf.exists():
        manifest = json.loads(mf.read_text(encoding="utf-8"))

    existing = load_records(out_dir)
    spent = sum(r["cost"]["usd"] for r in existing.values())
    if spent >= cap:
        sys.exit(f"BUDGET CAP REACHED (${spent:.4f} >= ${cap:.2f}) before arm C.")
    if not args.pilot:
        # armc duplicate guard (pilot armc runs never occupy cell keys)
        key_probe = {"corpus": {"repo_id": cid}, "harness": {"name": hname,
                     "model": model}, "arm": "armc", "task": "T2", "rep": rep,
                     "reasoning_effort": a.get("reasoning_effort") or {}}
        if any(record_key(r) == record_key(key_probe) for r in existing.values()):
            sys.exit("an armc record for this (corpus, pair, rep) already "
                     "exists in runs/; use a pilot run or clear it first")

    safe_model = _safe_component(model.replace("/", "-"))
    rid = (f"{cid}_{hname}_{safe_model}_armc_T2_r{rep}_"
           f"{uuid.uuid4().hex[:6]}")
    d = WORK / rid
    _rmtree(d)
    shutil.copytree(REPOS / _safe_component(cid), d)
    assert_tree_root(cid, d, manifest)
    logs = LOGS / (f"pilot-{rid}" if args.pilot else rid)
    logs.mkdir(parents=True, exist_ok=True)
    print(f"[{rid}] arm C on arm-A {a['run_id']}")
    started = time.time()
    prov_start = cavet_provenance(env)

    init = cavet_init(env, d)
    if init["rc"] != 0:
        print(f"   cavet init failed rc={init['rc']}: {init['err'][:200]}")

    # Arm A's diff: prefer the full sidecar (the record field is capped at
    # 200k chars; a T2 diff can exceed that).
    a_logs = LOGS / (f"pilot-{a['run_id']}" if a.get("pilot") else a["run_id"])
    sidecar = a_logs / "diff.patch"
    if sidecar.exists():
        diff_text = sidecar.read_text(encoding="utf-8", errors="replace")
        diff_source = "sidecar"
    else:
        diff_text = (a.get("artifacts") or {}).get("diff", "")
        diff_source = "record"
    if not diff_text.strip():
        _rmtree(d)
        sys.exit(f"arm-A record {a['run_id']} carries no diff; nothing to scan")
    patch = d / "_arm_a.patch"
    patch.write_text(diff_text, encoding="utf-8")
    apply = subprocess.run(["git", "-C", str(d), "apply", "--whitespace=nowarn",
                            "_arm_a.patch"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if apply.returncode != 0:
        (logs / "apply_error.txt").write_text(
            (apply.stdout or "") + (apply.stderr or ""), encoding="utf-8")
        print(f"   git apply FAILED rc={apply.returncode}; work dir kept at {d}")
        sys.exit(f"arm A's diff does not apply to the pinned {cid} root. "
                 f"See {logs / 'apply_error.txt'}")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.name=bench",
                    "-c", "user.email=bench@localhost", "commit", "--quiet",
                    "-m", "branch state under review"], check=True,
                   capture_output=True)
    applied_sha = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                                 capture_output=True,
                                 text=True).stdout.strip()
    patch.unlink(missing_ok=True)

    def headless_scan(tag: str):
        r = _run_cmd([cavet_bin(env), "scan", "--full"], cwd=d,
                     timeout=scan_timeout)
        (logs / f"scan_{tag}.txt").write_text(
            (r["out"] or "") + (r["err"] or ""), encoding="utf-8")

        class _R:
            stdout, returncode = r["out"], r["rc"]
        return _R()

    scan1 = headless_scan("before")
    scan1_res = parse_scan_result(scan1.stdout)
    dedup1 = dedupe_findings(scan1_res["findings"])
    scan_meta = {"raw_count": len(scan1_res["findings"]),
                 "header": scan1_res["header"]}
    comment = format_ci_comment(dedup1, scan_meta)
    (logs / "ci_comment.md").write_text(comment, encoding="utf-8")
    print(f"   scan: rc={scan1.returncode} "
          f"{len(scan1_res['findings'])} raw -> {len(dedup1)} deduped "
          f"finding(s) handed to the fixer")

    # The fresh fix session: findings comment + the T2 commit directive,
    # nothing else from arm A's context.
    prompt = comment.rstrip() + "\n\n" + DIRECTIVE
    h = HARNESSES[hname]
    ctx = {}
    try:
        setup = h.setup(env, model) if h.setup else contextlib.nullcontext()
        with setup:
            # arm "baseline" env on purpose: no cavet on the fix session's
            # PATH - the CI comparator runs WITHOUT cavet in the loop (the
            # pilot showed a fixer that finds the binary will use it).
            ctx = invoke(h, harness_binary(env, hname), model, prompt, d,
                         timeout, log_dir=logs,
                         env_extra=cavet_cell_env(env, "baseline"))
        if ctx.get("timed_out"):
            raise TimeoutError(f"no result after {timeout}s")
        u = h.read_usage(ctx)
        err = None
    except Exception as e:
        u, err = Usage(), f"{type(e).__name__}: {e}"
    used_model = actual_model(ctx) if ctx else ""
    if used_model and used_model != model:
        err = (err + " | " if err else "") + (
            f"MODEL MISMATCH: requested {model}, harness used {used_model}")
    c_fix = 0.0
    try:
        if u.total_prompt or u.output:
            c_fix = cost_usd(model, u)
    except Exception as e:
        err = (err + " | " if err else "") + f"costing: {e}"
    fix_wall = time.time() - started

    post_diff = subprocess.run(
        ["git", "-C", str(d), "diff", applied_sha, "--", ".",
         ":(exclude).cavet"], capture_output=True, text=True,
        encoding="utf-8", errors="replace").stdout

    scan2 = headless_scan("after")
    scan2_res = parse_scan_result(scan2.stdout)
    dedup2 = dedupe_findings(scan2_res["findings"])

    prov_end = cavet_provenance(env)
    rec_stop = stop_engine_scoped(env, d)
    if not args.keep_work:
        _rmtree(d)

    c_a = float(a.get("cost", {}).get("usd", 0.0) or 0.0)
    mf_entry = manifest.get(cid, {})
    rec = {
        "run_id": rid, "started": started, "wall_s": round(fix_wall, 1),
        "corpus": {"repo_id": cid,
                   "repo_hash": mf_entry.get("root_commit", ""),
                   "lang": mf_entry.get("lang", ""),
                   "ratio": mf_entry.get("ratio", ""),
                   "task": "T2"},
        "harness": {"name": hname, "model": model},
        "arm": "armc", "task": "T2", "rep": rep,
        "pilot": args.pilot,
        "from_a": a["run_id"],
        "from_a_wall_s": a.get("wall_s"),
        "cavet_init_rc": init["rc"],
        "cavet_apply": {"rc": apply.returncode, "commit": applied_sha,
                        "diff_source": diff_source,
                        "diff_bytes": len(diff_text)},
        "cavet_provenance": {"start": prov_start, "end": prov_end},
        "cost": {"usd": c_fix, "wall_s": round(fix_wall, 1),
                 "uncached_input": u.uncached_input,
                 "output_tokens": u.output,
                 "cache_read": u.cache_read, "cache_write": u.cache_write,
                 "cache_write_1h": u.cache_write_1h, "source": u.source},
        "cost_to_clean": {"c_a": c_a, "c_fix": c_fix,
                          "total": round(c_a + c_fix, 6)},
        "fix_waste_share": (round(c_fix / (c_a + c_fix), 4)
                            if (c_a + c_fix) > 0 else None),
        "harness_rc": ctx.get("rc"), "error": err,
        "timed_out": bool(ctx.get("timed_out")),
        "reasoning_effort": a.get("reasoning_effort"),
        "model_actual": used_model,
        "model_mismatch": bool(used_model and used_model != model),
        "scan_before": {"rc": scan1.returncode,
                        "raw": len(scan1_res["findings"]),
                        "deduped": len(dedup1),
                        "aggregate": scan1_res["aggregate"],
                        "header": scan1_res["header"],
                        "findings": dedup1[:200]},
        "scan_after": {"rc": scan2.returncode,
                       "raw": len(scan2_res["findings"]),
                       "deduped": len(dedup2),
                       "aggregate": scan2_res["aggregate"]},
        "engine_stop": rec_stop,
        "stdout_bytes": ctx.get("stdout_bytes", 0),
        "diff_bytes": len(post_diff),
        "final_response": final_response(ctx)[:60000] if ctx else "",
        "artifacts": {"stdout": (ctx.get("stdout") or "")[:20000],
                      "diff": post_diff[:200000]},
    }
    low = ((rec["final_response"] or "")
           + (ctx.get("stderr") or "")).lower()
    rec["provider_blocked"] = sorted({b for b in QUOTA_MARKERS if b in low})
    rec = scrub(rec, {n: cid for n in name_variants(corpus[cid]["repo"])})
    try:
        (logs / "diff_after.patch").write_text(post_diff, encoding="utf-8")
    except OSError:
        pass
    (out_dir / f"{rid}.json").write_text(json.dumps(rec, indent=2,
                                                    default=str),
                                         encoding="utf-8")
    status = err or f"rc={ctx.get('rc')}"
    print(f"   fix session: {status} | {fix_wall:.0f}s | C_fix ${c_fix:.4f}")
    print(f"   cost-to-clean: C_A ${c_a:.4f} + C_fix ${c_fix:.4f} = "
          f"${c_a + c_fix:.4f}")
    print(f"   scan after fix: rc={scan2.returncode} "
          f"{len(scan2_res['findings'])} raw -> {len(dedup2)} deduped")
    print(f"   running ${spent + c_fix:.4f}")
    return 0


# --------------------------------------------------------------------------
# phase-1 rotation scheduler (plan §4)
# --------------------------------------------------------------------------

SCHED_STATE = RUNS / "scheduler-state.json"


def rotation_pairs(env) -> list:
    """Resolve the ROTATION order into (harness, model) pairs. Entries match
    a configured pair by exact model id or harness/model; `muse-spark-zen`
    is the documented shorthand for the opencode-hosted free route.

    An explicit ROTATION is the exact set: pairs left out are paused, not
    appended - that is the mechanism for sitting a pair out (allocation
    exhausted, operator at work on that subscription). Only the implicit
    default appends configured-but-unlisted pairs, so a newly configured
    pair can never be silently dropped from the matrix."""
    raw = os.environ.get("ROTATION") or env.get("ROTATION") or ""
    explicit = bool(raw.strip())
    order = raw if explicit else DEFAULT_ROTATION
    pairs = pairs_from_env(env)
    rot = []
    for entry in [x.strip() for x in order.split(",") if x.strip()]:
        match = None
        for p in pairs:
            if entry == p[1] or entry == f"{p[0]}/{p[1]}":
                match = p
                break
        if match is None and entry == "muse-spark-zen":
            for p in pairs:
                if p[0] == "opencode" and "muse-spark" in p[1] and "free" in p[1]:
                    match = p
                    break
        if match is None:
            sys.exit(f"ROTATION entry {entry!r} matches no configured pair. "
                     f"Configured: {[f'{h}/{m}' for h, m in pairs]}")
        if match not in rot:
            rot.append(match)
    if not explicit:
        for p in pairs:
            if p not in rot:
                rot.append(p)
    return rot


def pair_cells(task_filter=None, repo_filter=None, reps: int = 3) -> list:
    """One pair's cell list in the fixed enumeration order (plan §4): task T1
    for ALL cells first, then T2; within a task repos 1,2,4,3,5; within a
    repo baseline arm then cavet arm; reps ascending."""
    cells = []
    for task in (task_filter or ["T1", "T2"]):
        for cid in (repo_filter or REPO_ORDER):
            for arm in ("baseline", "cavet"):
                for rep in range(1, reps + 1):
                    cells.append((cid, arm, task, rep))
    return cells


def _sched_save(state: dict):
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    SCHED_STATE.write_text(json.dumps(state, indent=2, default=str),
                           encoding="utf-8")


def cmd_rotate(args):
    """Serial round-robin across pairs: one cell per pair per cycle, cycling
    in ROTATION order. State checkpoints to runs/scheduler-state.json after
    every cell, so an interrupted matrix resumes exactly where it stopped.
    On a provider quota/allocation error the pair is deferred - it rejoins at
    its next rotation slot; two consecutive quota hits sit the pair out for
    two further cycles so a dead allocation window is not hammered while the
    operator decides fund-vs-wait."""
    env = load_env()
    lock = acquire_run_lock()
    try:
        return _rotate(args, env)
    finally:
        lock.unlink(missing_ok=True)


def _rotate(args, env):
    RUNS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    cap = float(env.get("BUDGET_USD_CAP", 25))
    timeout = int(env.get("RUN_TIMEOUT_SECONDS", 900))
    reps = args.reps or int(env.get("REPS", 3))
    corpus = corpus_from_env(env)
    if not corpus:
        sys.exit("no CORPUS_*_REPO entries in .env")
    for cid in (args.repo or REPO_ORDER):
        if cid not in corpus:
            sys.exit(f"unknown corpus id {cid}")
        if not (REPOS / _safe_component(cid)).exists():
            sys.exit(f"{REPOS / cid} not prepared. Run `bench.py prepare` first.")
    mf_path = ROOT / "corpus-manifest.json"
    if not mf_path.exists():
        sys.exit("corpus-manifest.json missing. Run `bench.py prepare` first.")
    manifest_all = json.loads(mf_path.read_text(encoding="utf-8"))
    missing = [c for c in (args.repo or REPO_ORDER) if c not in manifest_all]
    if missing:
        sys.exit(f"corpus-manifest.json has no entry for {missing}. "
                 f"Run `bench.py prepare` first.")

    d = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if d.returncode != 0:
        sys.exit("the matrix includes cavet arms; `docker info` failed.")

    rot = rotation_pairs(env)
    cells = pair_cells(args.task, args.repo, reps)
    print(f"rotation: {', '.join(f'{h}/{m}' for h, m in rot)}")
    print(f"{len(cells)} cells per pair x {len(rot)} pairs = "
          f"{len(cells) * len(rot)} cells total"
          f" (task {'+'.join(args.task) if args.task else 'T1,T2'}, "
          f"repos {','.join(args.repo or REPO_ORDER)}, {reps} reps)")

    state = {"cycle": 0, "pairs": {}, "deferred_until": {},
             "consecutive_quota": {}, "deferrals": []}
    if SCHED_STATE.exists() and not args.fresh:
        try:
            state.update(json.loads(SCHED_STATE.read_text(encoding="utf-8")))
            print(f"resuming from {SCHED_STATE.name} "
                  f"(cycle {state.get('cycle', 0)})")
        except Exception as e:
            print(f"WARNING: unreadable scheduler state ({e}); starting fresh")

    manifest = manifest_all
    pointers = {f"{h}/{m}": int(state["pairs"].get(f"{h}/{m}", {}).get("next", 0))
                for h, m in rot}

    if args.dry_run:
        for h, m in rot:
            key = f"{h}/{m}"
            i = pointers[key]
            nxt = cells[i:i + 3]
            print(f"  {key}: next {nxt if nxt else 'EXHAUSTED'}")
        return 0

    spent = sum(r["cost"]["usd"] for r in load_records().values())
    print(f"already spent ${spent:.4f} of ${cap:.2f} cap\n")
    cycle = int(state.get("cycle", 0))
    while True:
        remaining = [f"{h}/{m}" for h, m in rot
                     if pointers[f"{h}/{m}"] < len(cells)]
        if not remaining:
            print("matrix complete: every pair has run its cell list.")
            break
        ran_this_cycle = 0
        print(f"--- cycle {cycle} ---")
        for h, m in rot:
            key = f"{h}/{m}"
            i = pointers[key]
            # advance past cells that already have records (manual runs, or a
            # checkpoint written before a record landed)
            while i < len(cells):
                cid, arm, task, rep = cells[i]
                key_probe = {"corpus": {"repo_id": cid},
                             "harness": {"name": h, "model": m},
                             "arm": arm, "task": task, "rep": rep,
                             "reasoning_effort": reasoning_effort_for(h)}
                if not any(record_key(r) == record_key(key_probe)
                           for r in load_records().values()):
                    break
                i += 1
            pointers[key] = i
            state.setdefault("pairs", {})[key] = {"next": i}
            _sched_save(state)
            if i >= len(cells):
                continue
            if spent >= cap:
                print(f"\nBUDGET CAP REACHED (${spent:.4f} >= ${cap:.2f}). "
                      f"Stopping; checkpoint preserved.")
                _sched_save(state)
                return 0
            until = int(state.get("deferred_until", {}).get(key, 0))
            if until > cycle:
                print(f"[{key}] deferred until cycle {until} (quota); skipping")
                continue
            cid, arm, task, rep = cells[i]
            print(f"[cycle {cycle}] {key} cell {i + 1}/{len(cells)}: "
                  f"{cid} {arm} {task} r{rep}")
            rec = run_cell(env, cid, h, m, arm, task, rep, manifest,
                           timeout=timeout, keep_work=args.keep_work)
            spent += rec["cost"]["usd"]
            ran_this_cycle += 1
            state.setdefault("pairs", {})[key] = {"next": i + 1}
            pointers[key] = i + 1
            if rec.get("provider_blocked"):
                cons = int(state.get("consecutive_quota", {}).get(key, 0)) + 1
                state.setdefault("consecutive_quota", {})[key] = cons
                defer_to = cycle + (3 if cons >= 2 else 1)
                state.setdefault("deferred_until", {})[key] = defer_to
                state.setdefault("deferrals", []).append({
                    "cycle": cycle, "pair": key, "run_id": rec["run_id"],
                    "markers": rec["provider_blocked"],
                    "deferred_until_cycle": defer_to,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S")})
                print(f"[{key}] QUOTA/BLOCK hit {rec['provider_blocked']}; "
                      f"deferred to cycle {defer_to}")
            else:
                state.get("consecutive_quota", {}).pop(key, None)
                state.get("deferred_until", {}).pop(key, None)
            _sched_save(state)
        cycle += 1
        state["cycle"] = cycle
        _sched_save(state)
        if ran_this_cycle == 0:
            pend = [k for k in remaining
                    if int(state.get("deferred_until", {}).get(k, 0)) > cycle]
            if pend:
                print(f"all remaining pairs deferred ({', '.join(pend)}). "
                      f"Stopping; operator decides fund-vs-wait, then relaunch "
                      f"(`rotate` resumes from the checkpoint).")
                break
    print(f"\ntotal spend: ${spent:.4f}")
    return 0


def cmd_recost(args):
    """Recompute cost on every stored run from its recorded tokens.

    Token counts are the measurement; dollars are derived. When a rate is
    corrected the records must follow without spending anything again.
    --phase0 retargets the archived phase-0 records.
    """
    base = PHASE0_RUNS if args.phase0 else RUNS
    n = 0
    for p in sorted(base.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        c = d["cost"]
        u = Usage(uncached_input=c.get("uncached_input", 0),
                  output=c.get("output_tokens", 0),
                  cache_read=c.get("cache_read", 0),
                  cache_write=c.get("cache_write", 0),
                  cache_write_1h=c.get("cache_write_1h", 0))
        try:
            new = cost_usd(d["harness"]["model"], u)
        except KeyError as e:
            print(f"  SKIP {d['run_id'][:48]}: {e}")
            continue
        if abs(new - c["usd"]) > 1e-9:
            print(f"  {d['run_id'][:52]}  ${c['usd']:.4f} -> ${new:.4f}")
            c["usd"] = new
            p.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
            n += 1
    scope = "runs/phase0/" if args.phase0 else "runs/"
    print(f"{n} record(s) recosted at {RATES['checked_on']} rates ({scope})")
    return 0


def cmd_report(args):
    """Aggregate a runs directory into tables. Default: runs/ (phase 1).
    --phase0 aggregates the archived calibration set. runs/pilot/ is never
    aggregated - verification runs are not data (plan §2.4)."""
    base = PHASE0_RUNS if args.phase0 else RUNS
    scope = "runs/phase0/" if args.phase0 else "runs/"
    if not base.exists():
        print(f"no {scope} directory yet")
        return 0
    rows = [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(base.glob("*.json"))]
    if not rows:
        print(f"{scope} has no records yet (phase-0 records live in "
              f"runs/phase0/; aggregate them with `report --phase0`).")
        return 0
    by = {}
    for r in rows:
        c = r.get("corpus", {})
        k = (c.get("repo_id", c.get("repo", "?")), r["harness"]["name"],
             r["harness"]["model"], r["arm"], r["task"],
             (r.get("reasoning_effort") or {}).get("tier", "-"))
        b = by.setdefault(k, {"n": 0, "usd": 0.0, "wall": 0.0, "out": 0})
        b["n"] += 1
        b["usd"] += r["cost"]["usd"]
        b["wall"] += r["cost"]["wall_s"]
        b["out"] += r["cost"]["output_tokens"]
    print(f"{'repo':9} {'harness':10} {'model':28} {'arm':8} {'task':5} "
          f"{'effort':8} {'n':>3} {'mean $':>9} {'mean s':>8}")
    print("-" * 101)
    for (cid, hn, mo, arm, task, eff), b in sorted(by.items()):
        print(f"{cid:9} {hn:10} {mo[:28]:28} {arm:8} {task:5} {eff:8} "
              f"{b['n']:>3} {b['usd']/b['n']:>9.4f} {b['wall']/b['n']:>8.1f}")
    total = sum(r["cost"]["usd"] for r in rows)
    print(f"\n{len(rows)} runs, total ${total:.2f} at {RATES['checked_on']} list rates")

    # Arm C cost-to-clean (README 4.3): the number the cost objection lives
    # or dies on. C_fix is the armc record's own session; C_A comes from the
    # linked arm-A record, wherever it lives.
    armc_rows = [r for r in rows if r.get("arm") == "armc"]
    if armc_rows:
        print(f"\narm C - cost to clean (C_A + C_fix vs C_B):")
        print(f"{'run_id':44} {'C_fix':>9} {'C_A':>9} {'C_A+C_fix':>11} "
              f"{'fix waste':>9}")
        print("-" * 86)
        for r in armc_rows:
            ctc = r.get("cost_to_clean") or {}
            if not ctc:
                a_rec, _ = find_record(r.get("from_a", ""))
                c_a = float((a_rec or {}).get("cost", {}).get("usd", 0) or 0)
                ctc = {"c_fix": r["cost"]["usd"], "c_a": c_a,
                       "total": round(c_a + r["cost"]["usd"], 6)}
            print(f"{r['run_id'][:44]:44} {ctc.get('c_fix', 0):>9.4f} "
                  f"{ctc.get('c_a', 0):>9.4f} {ctc.get('total', 0):>11.4f} "
                  f"{(r.get('fix_waste_share') or 0) * 100:>8.1f}%")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    p = sub.add_parser("probe")
    p.add_argument("--harness", action="append",
                   choices=list(HARNESSES), help="repeatable; default all")
    p.set_defaults(fn=cmd_probe)
    sub.add_parser("prepare").set_defaults(fn=cmd_prepare)
    r = sub.add_parser("run")
    r.add_argument("--repo", action="append", metavar="CORPUS_N",
                   help="corpus-<n> from .env, repeatable; default all")
    r.add_argument("--pair", action="append", help="harness/model, repeatable")
    r.add_argument("--arm", action="append", choices=["baseline", "cavet", "armc"])
    r.add_argument("--task", action="append", choices=["T1", "T2"])
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--from-a", metavar="RUN_ID",
                   help="arm C only: run_id of the arm-A baseline T2 record "
                        "whose diff this fix session consumes (plan §3.3)")
    r.add_argument("--pilot", action="store_true",
                   help="verification run: record lands in runs/pilot/ "
                        "(outside the duplicate guard and report aggregates), "
                        "logs under runs/logs/pilot-<run_id>/")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--keep-work", action="store_true",
                   help="keep per-run repo copies for inspection")
    r.add_argument("--force", action="store_true",
                   help="rerun a cell that already has a record: the old "
                        "record is quarantined to runs/invalid/ first")
    r.set_defaults(fn=cmd_run)
    rot = sub.add_parser("rotate")
    rot.add_argument("--task", action="append", choices=["T1", "T2"],
                     help="restrict the matrix task(s); default T1 then T2")
    rot.add_argument("--repo", action="append", metavar="CORPUS_N",
                     help="restrict corpus ids; default 1,2,4,3,5")
    rot.add_argument("--reps", type=int, default=None,
                     help="default REPS from .env (3)")
    rot.add_argument("--fresh", action="store_true",
                     help="ignore runs/scheduler-state.json and start over")
    rot.add_argument("--dry-run", action="store_true")
    rot.add_argument("--keep-work", action="store_true")
    rot.set_defaults(fn=cmd_rotate)
    rec = sub.add_parser("recost")
    rec.add_argument("--phase0", action="store_true",
                     help="recompute the archived runs/phase0/ records")
    rec.set_defaults(fn=cmd_recost)
    rep = sub.add_parser("report")
    rep.add_argument("--phase0", action="store_true",
                     help="aggregate the archived runs/phase0/ records "
                          "instead of runs/")
    rep.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
