#!/usr/bin/env python3
"""v0 of the agentic orchestration layer (PLAN.md 7.7).

Everything else in re-com is a fixed pipeline: given inputs, a predetermined
sequence of calls runs. The tool *definitions* are good, but nothing decides at
runtime which tools to call, in what order, or replans when an intermediate
result is bad -- `recommend_for_mood` says "only 12 of 20 songs genuinely
matched this mood" in its `notes` and then returns anyway. This script is the
smallest thing that acts on that honesty: one agent, one task, one checkable
verdict, and a log of the plan it actually took.

**The task** (see SPEC): a 20-song run set, no artist more than twice, getting
more energetic toward the end, at least 15 of the 20 genuinely matched rather
than filler. Not one tool call. `arc.sequence` already caps artists at 2
*within* a call, so that constraint only bites once results from more than one
call are stitched together -- and nothing in re-com enforces it across calls.
`recommend.build` caps filler at 25% of the requested limit and reports the
shortfall, so reaching 15 rated usually means reading that report and
re-querying differently.

**The context budget is the design problem, not a detail.** A crawl-scale
session produces tool output far larger than a useful prompt. `_trim` decides
what survives: song lists are compacted to one line each and raw metadata dumps
(`seeds`, `filters`, `slot_target`, `sources`, `graphRef`) are dropped, while
`notes` and `match_quality` are kept verbatim -- trimming the very signals the
agent is supposed to replan on would defeat the experiment. The budget is
deliberately small so the decision binds, and every trim is logged.

**The checker reads tool output, not the agent's claims.** `_Registry` captures
each song exactly as the tool reported it, before trimming; the agent's final
answer only chooses `videoId`s. So a run cannot pass by asserting that its picks
were well-rated -- `rated` and `mood` come from re-com.

Needs live credentials, a configured backend, and the Claude Agent SDK, and it
spends tokens on every run. Like `quality_check.py` and `smoke_all.py` it is a
by-hand tool -- the third layer in PLAN.md 5 -- and is never wired into CI.

    pip install -e ".[agent]"
    python scripts/orchestrate.py --log run.jsonl
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The task, as numbers the checker can read. Which arc to ask for is left to
# the agent: the energy check tests whether the songs it actually got rise,
# which is a stronger claim than whether it passed arc="lift" to a tool.
SPEC = {
    "count": 20,
    "max_per_artist": 2,
    "min_rated": 15,
}

TASK = (
    "Build a {count}-song set to run to. Constraints, all of which are checked "
    "afterwards: exactly {count} distinct songs; no artist appears more than "
    "{max_per_artist} times across the whole set; the set gets more energetic "
    "toward the end; and at least {min_rated} of the {count} are genuine mood "
    "matches rather than filler."
)

# Read-only tools only. `record_feedback` writes to the local store and
# `refresh_library` is a minutes-long rebuild -- neither belongs in a loop that
# may retry, and leaving them out means a bad plan cannot cost anything.
TOOLS = [
    "mcp__recom__recommend_for_mood",
    "mcp__recom__recommend_from_playlist_for_mood",
    "mcp__recom__recommend_from_song",
    "mcp__recom__songs_by_artist",
    "mcp__recom__read_my_mood",
    "mcp__recom__index_status",
    "mcp__recom__explain_recommendation",
]

# The Claude Code harness ships a filesystem and shell agent. None of it is
# relevant here and all of it is reachable by default, so it is named off
# explicitly rather than left to the allowlist alone.
BUILTINS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "NotebookEdit"]

SYSTEM_PROMPT = """You are an orchestrator over re-com, a music recommendation engine exposed as MCP tools.

Your job is to satisfy a set of constraints that no single tool call satisfies. Work by calling tools, reading what they report about themselves, and adjusting.

Rules:
- Only use songs that a tool actually returned. Never invent a song or a videoId.
- Every tool result tells you how well it did. `match_quality.genuine` is how many picks were real mood matches rather than filler, and `notes` says plainly when a result came back short or thin. Read them and act: if a call returns too much filler, try a different route (a different feeling or vector, a genre narrowing, a playlist-seeded call, or song-seeded calls from picks you already trust) rather than accepting it.
- Per-artist limits are enforced inside a single call only. If you combine results from several calls, counting artists across the combined set is your job.
- A song may only appear once in the final set.

When you are done, end your final message with a fenced json block, and nothing after it:

```json
{"songs": [{"videoId": "...", "title": "...", "artist": "..."}]}
```

Include exactly the songs you have chosen, in the order you want them played."""


# --- the checker ------------------------------------------------------------


def _primary(song: dict[str, Any]) -> str:
    """The artist a song is counted under -- the same rule arc.sequence uses
    for its own per-artist cap, so the two cannot disagree."""
    return ((song.get("artists") or [None])[0] or "").lower()


def _energy(song: dict[str, Any]) -> float | None:
    mood = song.get("mood")
    if isinstance(mood, dict) and isinstance(mood.get("energy"), (int, float)):
        return float(mood["energy"])
    return None


def check(songs: list[dict[str, Any]], spec: dict[str, Any] = SPEC) -> dict[str, Any]:
    """Pass/fail per constraint, plus an overall verdict.

    Library exclusion is deliberately not re-checked here: it is re-com's
    central guarantee, enforced in two stages inside `signals`, and a
    client-side re-implementation of it would be a second, weaker copy of the
    rule rather than a test of it.
    """
    checks: dict[str, Any] = {}

    checks["count"] = {
        "want": spec["count"], "got": len(songs),
        "passed": len(songs) == spec["count"],
    }

    ids = [s.get("videoId") for s in songs]
    distinct = len({i for i in ids if i})
    checks["distinct"] = {
        "want": len(songs), "got": distinct, "passed": distinct == len(songs),
    }

    counts = Counter(_primary(s) for s in songs if _primary(s))
    worst = counts.most_common(1)[0] if counts else (None, 0)
    checks["max_per_artist"] = {
        "want": spec["max_per_artist"], "got": worst[1], "worst_artist": worst[0],
        "passed": worst[1] <= spec["max_per_artist"],
    }

    rated = sum(1 for s in songs if s.get("rated"))
    checks["rated"] = {
        "want": spec["min_rated"], "got": rated, "passed": rated >= spec["min_rated"],
    }

    # Only rated songs carry a mood vector, so only they can speak to the arc.
    # Too few of them is reported as its own failure rather than quietly
    # averaging two songs into a trend.
    half = len(songs) // 2
    first = [e for e in (_energy(s) for s in songs[:half]) if e is not None]
    second = [e for e in (_energy(s) for s in songs[half:]) if e is not None]
    if len(first) >= 2 and len(second) >= 2:
        a, b = statistics.mean(first), statistics.mean(second)
        checks["energy_rises"] = {
            "first_half": round(a, 3), "second_half": round(b, 3),
            "n": [len(first), len(second)], "passed": b > a,
        }
    else:
        checks["energy_rises"] = {
            "first_half": None, "second_half": None, "n": [len(first), len(second)],
            "passed": False, "reason": "too few rated songs to judge a trend",
        }

    return {"checks": checks, "passed": all(c["passed"] for c in checks.values())}


# --- the context budget -----------------------------------------------------

# Dropped from every tool result before the model sees it: large, fixed-shape
# metadata that says nothing about whether the result was good.
DROP_TOP = ("seeds", "filters", "target_origin", "seed_report")

# Kept verbatim at any budget. These are how a tool reports its own shortfall,
# which is the whole thing the agent is supposed to replan on.
KEEP = ("notes", "match_quality")


def _line(song: dict[str, Any]) -> str:
    """One song, as one line. The videoId survives because the agent's final
    answer is a list of them; everything else here is what a person would need
    to judge the pick."""
    bits = [song.get("videoId") or "?", f"{song.get('title')} - {', '.join(song.get('artists') or [])}"]
    energy = _energy(song)
    if energy is not None:
        bits.append(f"energy {energy:.2f}")
    bits.append("rated" if song.get("rated") else "unrated")
    if song.get("slot") is not None:
        bits.append(f"slot {song['slot']}")
    return " | ".join(bits)


def trim(payload: Any, budget: int) -> tuple[Any, dict[str, Any]]:
    """Shrink one tool result to fit `budget` bytes. Returns (payload, report).

    Order matters: compact first, truncate last. Compacting is lossless about
    which songs came back and how good they were; truncation loses songs
    outright, so it only happens if compaction was not enough, and the payload
    then says how many were dropped rather than silently coming back short.
    """
    report = {"bytes_before": len(json.dumps(payload, default=str)), "compacted": False, "songs_dropped": 0}

    if not isinstance(payload, dict) or not isinstance(payload.get("songs"), list):
        report["bytes_after"] = report["bytes_before"]
        return payload, report

    out = {k: v for k, v in payload.items() if k not in DROP_TOP}
    out["songs"] = [_line(s) for s in payload["songs"] if isinstance(s, dict)]
    report["compacted"] = True
    report["songs_in"] = len(payload["songs"])

    def _notice(dropped: int) -> str:
        return (
            f"{dropped} more song(s) were returned but dropped to fit the "
            "context budget. Ask for a smaller limit if you need to see them all."
        )

    # The notice counts against the budget like everything else. Adding it after
    # the loop instead put every truncated result over the cap it had just been
    # trimmed to fit -- a 2000-byte budget returning 2091 bytes.
    while out["songs"]:
        candidate = dict(out)
        if report["songs_dropped"]:
            candidate["truncated"] = _notice(report["songs_dropped"])
        if len(json.dumps(candidate, default=str)) <= budget:
            break
        out["songs"].pop()
        report["songs_dropped"] += 1
    if report["songs_dropped"]:
        out["truncated"] = _notice(report["songs_dropped"])

    report["bytes_after"] = len(json.dumps(out, default=str))
    # `notes` and `match_quality` are never trimmed, so a small enough budget
    # cannot be met. Say so rather than reporting a fit that did not happen.
    report["over_budget"] = report["bytes_after"] > budget
    report["songs_kept"] = len(out["songs"])
    return out, report


class Registry:
    """Every song any tool returned, as the tool reported it.

    The agent's final answer is a list of videoIds; the checker reads mood and
    `rated` from here. So the run is judged on what re-com said, never on what
    the agent said about it -- which is the only reason the verdict means
    anything.
    """

    def __init__(self) -> None:
        self.songs: dict[str, dict[str, Any]] = {}

    def capture(self, payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        found = payload.get("songs")
        if not isinstance(found, list):
            return 0
        added = 0
        for song in found:
            vid = isinstance(song, dict) and song.get("videoId")
            if vid and vid not in self.songs:
                self.songs[vid] = song
                added += 1
        return added

    def resolve(self, chosen: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """Chosen ids -> the records re-com actually returned. An id no tool
        ever produced is reported, not filled in: a set that cites songs the
        engine never offered has not met the task, and quietly dropping them
        would hide exactly that."""
        out, unknown = [], []
        for pick in chosen:
            vid = pick.get("videoId") if isinstance(pick, dict) else None
            if vid and vid in self.songs:
                out.append(self.songs[vid])
            else:
                unknown.append(vid or repr(pick))
        return out, unknown


# --- the run ----------------------------------------------------------------


_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_answer(text: str) -> list[dict[str, Any]]:
    """The songs from the agent's final fenced json block. The last block wins
    -- the agent may show its working in earlier ones."""
    blocks = _JSON_BLOCK.findall(text or "")
    for raw in reversed(blocks):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        songs = parsed.get("songs")
        if isinstance(songs, list):
            return songs
    return []


class Log:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        if path:
            path.write_text("")

    def add(self, event: str, **fields: Any) -> None:
        record = {"at": round(time.time(), 3), "event": event, **fields}
        self.records.append(record)
        if self.path:
            with self.path.open("a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")


def _mcp_payload(response: Any) -> Any:
    """The tool's own JSON, out of whatever envelope it arrives in.

    An MCP result reaches a hook wrapped by two layers that each have more than
    one shape in the wild -- a `CallToolResult` (`content` blocks, or
    `structuredContent`), inside the CLI's own tool-response record. Every
    branch here was reached by a real run; guessing one shape and returning the
    envelope on a miss is what made the first run capture nothing at all.
    """
    seen = 0
    while seen < 5:
        seen += 1
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except json.JSONDecodeError:
                return response
            continue
        if isinstance(response, list):
            texts = [b.get("text") for b in response
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            if len(texts) != 1:
                return response
            response = texts[0]
            continue
        if not isinstance(response, dict):
            return response
        if "songs" in response or "notes" in response:
            return response
        for key in ("structuredContent", "content", "toolResult", "result", "response"):
            if key in response:
                response = response[key]
                break
        else:
            return response
    return response


def _shape(value: Any) -> str:
    """A one-line description of an unparsed payload, for the log. A harness
    that cannot say what it failed to read cannot be debugged from its trace."""
    if isinstance(value, dict):
        return f"dict({', '.join(sorted(value)[:8])})"
    if isinstance(value, list):
        return f"list[{len(value)}] of {_shape(value[0]) if value else 'empty'}"
    return type(value).__name__


async def run(args: argparse.Namespace, log: Log) -> dict[str, Any]:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        HookMatcher,
        ResultError,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    registry = Registry()

    async def on_tool_result(payload: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        name = payload.get("tool_name", "?")
        raw = _mcp_payload(payload.get("tool_response"))
        captured = registry.capture(raw)
        trimmed, report = trim(raw, args.tool_budget)
        # A recommendation result that produced no songs is either a genuine
        # empty result or a payload this harness failed to read. Recording the
        # shape is what tells those apart afterwards.
        if not report["compacted"] and name.startswith("mcp__recom__"):
            report["unparsed_shape"] = _shape(payload.get("tool_response"))
        log.add("tool_result", name=name, captured=captured,
                notes=(raw.get("notes") if isinstance(raw, dict) else None),
                match_quality=(raw.get("match_quality") if isinstance(raw, dict) else None),
                **report)
        if not report["compacted"]:
            return {}
        # An MCP tool's output must go back as MCP content blocks, not as the
        # bare object they carry. Returning the object crashes the CLI while it
        # measures the response ("'e.reduce' is undefined"), which reaches the
        # agent as every tool failing at once -- it correctly refused to invent
        # songs and reported the engine as broken.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": [{"type": "text", "text": json.dumps(trimmed, default=str)}],
            }
        }

    spec = {**SPEC, "count": args.count, "min_rated": args.min_rated}
    options = ClaudeAgentOptions(
        model=args.model,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={
            "recom": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(REPO / "server.py")],
                "env": {k: v for k, v in os.environ.items() if k.startswith("RECOM_")},
            }
        },
        allowed_tools=TOOLS,
        disallowed_tools=BUILTINS,
        # Nothing reachable here can write, and every tool is named above --
        # so the run is non-interactive by construction rather than by trust.
        permission_mode="bypassPermissions",
        # The experiment is this script's configuration, not the developer's.
        # Without these two, whatever MCP servers and settings happen to be on
        # this machine would join the run and the log would not describe it.
        strict_mcp_config=True,
        setting_sources=[],
        max_turns=args.max_turns,
        cwd=str(REPO),
        hooks={"PostToolUse": [HookMatcher(hooks=[on_tool_result])]},
    )

    log.add("run_start", task=TASK.format(**spec), spec=spec, model=args.model,
            tool_budget=args.tool_budget, max_turns=args.max_turns)

    final_text, result_meta = "", {}
    try:
        async for message in query(prompt=TASK.format(**spec), options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        log.add("tool_call", name=block.name, input=block.input)
                    elif isinstance(block, TextBlock) and block.text.strip():
                        final_text = block.text
                        log.add("assistant_text", text=block.text)
            elif isinstance(message, ResultMessage):
                result_meta = {
                    "num_turns": message.num_turns, "cost_usd": message.total_cost_usd,
                    "terminal_reason": message.terminal_reason, "is_error": message.is_error,
                }
                if message.result:
                    final_text = message.result
                log.add("result", **result_meta)
    except ResultError as e:
        # Running out of turns is a result, not a crash: the whole point is to
        # find out what the loop did, and an agent that spent its budget
        # without finishing is one of the things worth finding out.
        result_meta = {"error": str(e)}
        log.add("result", **result_meta)

    chosen, unknown = registry.resolve(parse_answer(final_text))
    verdict = check(chosen, spec)
    verdict["unknown_ids"] = unknown
    if unknown:
        verdict["passed"] = False
    verdict["tool_calls"] = sum(1 for r in log.records if r["event"] == "tool_call")
    verdict["songs_seen"] = len(registry.songs)
    verdict.update(result_meta)
    log.add("verdict", **verdict)
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=None, help="write the run trace here as JSONL")
    parser.add_argument("--count", type=int, default=SPEC["count"], help="songs the set must contain")
    parser.add_argument("--min-rated", type=int, default=SPEC["min_rated"],
                        help="how many of them must be genuine mood matches, not filler")
    parser.add_argument("--tool-budget", type=int, default=2000,
                        help="bytes a single tool result may occupy after trimming")
    parser.add_argument("--max-turns", type=int, default=20, help="stop the agent after this many turns")
    parser.add_argument("--model", default="claude-opus-5")
    args = parser.parse_args()

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print('error: the Claude Agent SDK is not installed. Run: pip install -e ".[agent]"', file=sys.stderr)
        return 1

    import asyncio

    log = Log(args.log)
    started = time.time()
    verdict = asyncio.run(run(args, log))

    print(f"=== orchestrator v0 ({verdict.get('tool_calls', 0)} tool calls, "
          f"{len(log.records)} events, {time.time() - started:.0f}s) ===")
    for name, row in verdict["checks"].items():
        mark = "pass" if row["passed"] else "FAIL"
        detail = " ".join(f"{k}={v}" for k, v in row.items() if k != "passed")
        print(f"  {mark}  {name:<16} {detail}")
    if verdict.get("unknown_ids"):
        print(f"  FAIL  {'cited songs':<16} {len(verdict['unknown_ids'])} id(s) no tool ever returned")

    trims = [r for r in log.records if r["event"] == "tool_result"]
    bound = [r for r in trims if r.get("songs_dropped")]
    if trims:
        before = sum(r["bytes_before"] for r in trims)
        after = sum(r["bytes_after"] for r in trims)
        print(f"\ncontext budget: {before:,} -> {after:,} bytes across {len(trims)} results "
              f"({len(bound)} hit the {args.tool_budget}-byte cap)")
    print(f"songs seen: {verdict.get('songs_seen', 0)} | cost: ${verdict.get('cost_usd') or 0:.2f} | "
          f"turns: {verdict.get('num_turns', '?')}")
    if verdict.get("error"):
        print(f"run ended early: {verdict['error']}")
    print(f"\nVERDICT: {'PASS' if verdict['passed'] else 'FAIL'}")
    if args.log:
        print(f"trace: {args.log}")
    else:
        print("(no --log given; pass --log PATH to keep the trace)")
    return 0 if verdict["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
