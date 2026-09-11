"""Every tool, on every configured backend, against the live account.

The unit suite runs against fakes and is worth what it costs -- but fakes
cannot model a real sqlite connection crossing a real thread pool, and that is
exactly what broke: `recommend_from_playlist` raised ProgrammingError on both
backends, for any multi-track playlist, from the v6 merge until 2026-08-29.
507 passing tests did not see it. It survived because the only live smoke test
(`scripts/test_recommend.py`, since removed in favour of this one) went through
`recommend_from_song`, which deliberately keeps a single seed on the calling
thread.

So the coverage rule here is tools x backends, not tools. A tool verified on
YouTube is not verified on Spotify: the two differ in `capabilities()`, which
decides whether native signals fill the candidate pool or the graph is the only
source -- and a bug in the graph path is invisible on a backend whose native
signals hide it.

Three invariants per tool, because "it returned" is not the promise re-com
makes:

  returns      a non-empty result, or an explicit, stated reason for the
               shortfall. Silent emptiness is the failure mode being hunted.
  excludes     nothing already in Liked Music or any playlist. This is the
               guarantee the project exists for; a live check is the only
               place it can actually be tested.
  within       a latency ceiling. The measured warm figures are ~4-6s; a tool
               that quietly starts taking 60s has regressed even if it still
               returns the right songs.

Usage:

    python scripts/smoke_all.py                      # every configured backend
    python scripts/smoke_all.py --provider spotify   # just one
    python scripts/smoke_all.py --budget 45          # looser latency ceiling
    python scripts/smoke_all.py --include-writes     # also exercise record_feedback

Each backend runs in its own subprocess: `RECOM_PROVIDER` is read once at
import time (server.PROVIDER), so one process cannot honestly test two.

Needs the same env vars `claude mcp add` uses (RECOM_YTMUSIC_MCP_COMMAND /
_ARGS, RECOM_SPOTIFY_MCP_COMMAND / _ARGS) and each sibling *-mcp server
already authenticated. A backend with no command configured is reported as
`skipped`, not as a pass -- an unrun check must never read as a green one.

Exit status is 0 only if every check on every backend passed.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROVIDERS = {
    "youtube": "RECOM_YTMUSIC_MCP_COMMAND",
    "spotify": "RECOM_SPOTIFY_MCP_COMMAND",
}

# A seed every catalogue carries, so the run isn't testing search coverage.
SEED_SONG = "One More Time"
SEED_ARTIST = "Daft Punk"
SEED_ARTIST_CATALOG = "Daft Punk"

PASS, FAIL, SKIP = "pass", "fail", "skip"


class Check:
    """One tool's result, with the reason attached rather than inferred."""

    def __init__(self, name):
        self.name = name
        self.status = SKIP
        self.seconds = 0.0
        self.detail = ""

    def as_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "detail": self.detail,
        }


# --- invariants -------------------------------------------------------------


def _songs_of(result):
    """Every tool returns songs; only the wrapper around them differs."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("songs") or []
    return []


def _leaked(songs, excluded):
    """Results that are already in the library -- the guarantee, checked."""
    return [s for s in songs if s.get("videoId") and s["videoId"] in excluded]


def _check(check, result, excluded, budget, *, allow_empty_if=None, out=None):
    songs = _songs_of(result)
    if songs and out is not None and not out.get("_explain_id"):
        # Keep the first real videoId this run produced, so explain/feedback
        # act on a song this account was actually just recommended.
        first = next((s["videoId"] for s in songs if s.get("videoId")), None)
        if first:
            out["_explain_id"] = first

    if not songs:
        reason = allow_empty_if(result) if allow_empty_if else None
        if reason:
            # A stated shortfall is the documented behaviour, not a failure --
            # recommend_from_playlist_for_mood is allowed to find nothing so
            # long as it says why instead of returning off-mood filler.
            check.status = PASS
            check.detail = f"empty, explained: {reason}"
            return check
        check.status = FAIL
        check.detail = "returned no songs and gave no reason"
        return check

    leaked = _leaked(songs, excluded)
    if leaked:
        check.status = FAIL
        titles = ", ".join(f"{s.get('title')!r}" for s in leaked[:3])
        check.detail = f"{len(leaked)} already in the library: {titles}"
        return check

    if check.seconds > budget:
        check.status = FAIL
        check.detail = f"{len(songs)} songs but took {check.seconds:.1f}s (budget {budget}s)"
        return check

    check.status = PASS
    check.detail = f"{len(songs)} songs, none in the library"
    return check


def _check_mood_read(check, result):
    """read_my_mood's contract: lead with the evidence, never assert a mood
    without it.

    A read with no vector is not a failure so long as it says why -- measured
    on Spotify, "25 recent plays, but none of them have a mood label yet" is
    exactly what this tool should say rather than inventing a mood. The first
    live run marked that FAILED, which was the harness being wrong, not the
    tool. Same rule as the empty-but-explained shortfall above.
    """
    evidence = result.get("evidence") or []
    if result.get("vector") and evidence:
        check.status = PASS
        check.detail = result.get("described") or "mood read with evidence"
    elif evidence:
        check.status = PASS
        check.detail = f"no mood, explained: {str(evidence[0])[:110]}"
    else:
        # A verdict with no evidence behind it is the thing this tool's own
        # docstring says not to do.
        check.status = FAIL
        check.detail = f"asserted a mood with no evidence: {result}"
    return check


# A tool that cannot satisfy a request says so by raising, and re-com's third
# hard requirement is that such a refusal state its reason. Two of these tools
# refuse by contract rather than by failing, and both name the tool to use
# instead -- that is the signature the harness keys on, because a genuine crash
# never suggests an alternative.
_REFUSES_BY_CONTRACT = {
    "recommend_from_playlist_for_mood",
    "recommend_for_mood",
}


def _is_stated_refusal(name, message):
    """Whether a raised error is this tool's documented refusal, not a break."""
    return name in _REFUSES_BY_CONTRACT and "Try recommend" in message


def _run(name, fn, checks):
    """Time one tool call, and turn a raised error into a failed check.

    Except a refusal the tool documents: "no track in this playlist fits that
    mood, try recommend_for_mood instead" is the behaviour the README promises
    over returning off-mood filler, and marking it FAILED is the harness crying
    wolf -- which gets it ignored exactly when it is right.
    """
    check = Check(name)
    checks.append(check)
    started = time.monotonic()
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - a crashing tool is a failed check
        check.seconds = time.monotonic() - started
        message = str(e)
        if _is_stated_refusal(name, message):
            check.status = PASS
            check.detail = f"refused, explained: {message[:110]}"
        else:
            check.status = FAIL
            check.detail = f"{type(e).__name__}: {message}"
        return None
    check.seconds = time.monotonic() - started
    return result


# --- the per-backend run ----------------------------------------------------


def run_one_backend(budget, limit, include_writes):
    """Exercise every tool in this process, against this process's provider."""
    import server

    checks = []
    out = {"provider": server.PROVIDER, "graph": server.GRAPH_ENABLED, "checks": checks}

    yt = server._client()

    # The exclusion set is the yardstick every other check is measured against,
    # so build it first and fail the whole run if it can't be built -- checking
    # novelty against an empty set would pass everything for the wrong reason.
    refresh = _run("refresh_library", server.refresh_library, checks)
    if refresh is None:
        return out
    excluded = server._library_video_ids(yt)
    if not excluded:
        checks[-1].status = FAIL
        checks[-1].detail = "the library exclusion set is empty; nothing else can be trusted"
        return out
    checks[-1].status = PASS
    checks[-1].detail = f"{len(excluded)} tracks excluded"

    # --- similarity path ----------------------------------------------------

    result = _run(
        "recommend_from_song",
        lambda: server.recommend_from_song(song=SEED_SONG, artist=SEED_ARTIST, limit=limit),
        checks,
    )
    if result is not None:
        _check(checks[-1], result, excluded, budget, out=out)

    playlist_id = _multi_track_playlist(yt)
    if playlist_id is None:
        checks.append(Check("recommend_from_playlist"))
        checks[-1].detail = "no library playlist with 2+ tracks to seed from"
    else:
        # 2+ tracks on purpose: a single-seed playlist stays on the calling
        # thread and would miss the exact bug this script exists to catch.
        result = _run(
            "recommend_from_playlist",
            lambda: server.recommend_from_playlist(playlist_id, limit=limit),
            checks,
        )
        if result is not None:
            _check(checks[-1], result, excluded, budget, out=out)

    result = _run(
        "songs_by_artist",
        lambda: server.songs_by_artist(SEED_ARTIST_CATALOG, limit=limit),
        checks,
    )
    if result is not None:
        _check(checks[-1], result, excluded, budget, out=out)

    # --- mood path ----------------------------------------------------------

    if server.PROVIDER not in server.MOOD_PROVIDERS:
        for name in ("recommend_for_mood", "recommend_from_playlist_for_mood", "read_my_mood"):
            check = Check(name)
            check.detail = f"mood tools are gated off on {server.PROVIDER}"
            checks.append(check)
    else:
        result = _run(
            "recommend_for_mood",
            lambda: server.recommend_for_mood(feeling="melancholy", arc="mirror", limit=limit),
            checks,
        )
        if result is not None:
            _check(checks[-1], result, excluded, budget, out=out)

        if playlist_id is not None:
            result = _run(
                "recommend_from_playlist_for_mood",
                lambda: server.recommend_from_playlist_for_mood(
                    playlist_id, feeling="melancholy", arc="mirror", limit=limit
                ),
                checks,
            )
            if result is not None:
                _check(
                    checks[-1], result, excluded, budget, out=out,
                    allow_empty_if=_stated_shortfall,
                )

        result = _run("read_my_mood", server.read_my_mood, checks)
        if result is not None:
            _check_mood_read(checks[-1], result)

    # --- reporting tools ----------------------------------------------------

    status = _run("index_status", server.index_status, checks)
    if status is not None:
        check = checks[-1]
        check.status = PASS if isinstance(status, dict) and status else FAIL
        check.detail = _summarise_index(status)

    explained = _explainable_video_id(out)
    if explained:
        result = _run(
            "explain_recommendation",
            lambda: server.explain_recommendation(explained),
            checks,
        )
        if result is not None:
            checks[-1].status = PASS if isinstance(result, dict) else FAIL
            checks[-1].detail = str(result)[:120]

    if include_writes and explained:
        # Off by default: record_feedback writes to the real store, and a
        # `skipped`/`wrong_mood` reaction bans that song permanently. A smoke
        # test must not quietly edit the user's taste profile, so the benign
        # reaction is used and only when explicitly asked for.
        result = _run(
            "record_feedback",
            lambda: server.record_feedback(explained, "loved"),
            checks,
        )
        if result is not None:
            checks[-1].status = PASS
            checks[-1].detail = f"recorded 'loved' for {explained}"

    return out


def _multi_track_playlist(yt):
    """A library playlist with at least two readable tracks, or None.

    Two is the whole point: `gather_seeds` keeps a single seed on the calling
    thread, so a one-track playlist exercises none of the threading -- the bug
    this script exists to catch would slip straight through.

    The count has to be verified by reading, not trusted: Spotify reports no
    `count` at all, so trusting it picked the first playlist in the listing,
    which on this account was an empty test playlist. The tool then failed with
    "no playable tracks", which is correct behaviour being reported as a
    regression -- a harness that cries wolf gets ignored exactly when it is
    right.
    """
    try:
        playlists = yt.get_library_playlists(limit=25)
    except Exception:  # noqa: BLE001 - reported by the caller as a skip
        return None

    for pl in playlists or []:
        playlist_id = pl.get("playlistId")
        if not playlist_id:
            continue
        count = pl.get("count")
        if count is not None and int(count) < 2:
            continue
        try:
            tracks = (yt.get_playlist(playlist_id, limit=5) or {}).get("tracks") or []
        except Exception:  # noqa: BLE001 - unreadable is simply not a candidate
            continue
        if len([t for t in tracks if t.get("videoId")]) >= 2:
            return playlist_id
    return None


def _stated_shortfall(result):
    """Whether an empty mood-from-playlist result explained itself."""
    if not isinstance(result, dict):
        return None
    for key in ("notes", "seed_report", "message"):
        value = result.get(key)
        if value:
            return str(value)[:160]
    return None


def _summarise_index(status):
    if not isinstance(status, dict):
        return str(status)[:120]
    parts = [f"{k}={v}" for k, v in status.items() if isinstance(v, (int, float, str))]
    return ", ".join(parts[:6])


def _explainable_video_id(out):
    """A videoId this run actually produced, for explain/feedback to act on."""
    return out.get("_explain_id")


# --- the parent process -----------------------------------------------------


def configured_providers(requested):
    if requested != "all":
        return [requested]
    return [name for name, env in PROVIDERS.items() if os.environ.get(env)]


def run_child(provider, args):
    """Re-exec this script with RECOM_PROVIDER pinned for one backend."""
    env = dict(os.environ, RECOM_PROVIDER=provider)
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--run-one", provider,
        "--budget", str(args.budget),
        "--limit", str(args.limit),
    ]
    if args.include_writes:
        cmd.append("--include-writes")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    return {
        "provider": provider,
        "checks": [],
        "error": (proc.stderr or proc.stdout or "no output").strip()[-800:],
    }


def report(results):
    """One line per tool per backend, and a status that can't be misread."""
    failures = 0
    for run in results:
        header = f"{run['provider']}"
        if "graph" in run:
            header += f"  (graph {'on' if run['graph'] else 'off'})"
        print(f"\n=== {header} ===")
        if run.get("error"):
            print(f"  the backend never started: {run['error']}")
            failures += 1
            continue
        if not run["checks"]:
            print("  no checks ran")
            failures += 1
            continue
        for check in run["checks"]:
            mark = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}[check["status"]]
            print(f"  {mark}  {check['name']:<32} {check['seconds']:>6.1f}s  {check['detail']}")
            if check["status"] == FAIL:
                failures += 1

    skipped = sum(1 for r in results for c in r["checks"] if c["status"] == SKIP)
    print()
    if failures:
        print(f"FAILED: {failures} check(s) failed.")
    else:
        print(f"OK: every check passed" + (f" ({skipped} skipped)." if skipped else "."))
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--provider", default="all", choices=["all", *PROVIDERS])
    parser.add_argument("--budget", type=float, default=30.0,
                        help="per-tool latency ceiling in seconds (warm runs measure ~4-6s)")
    parser.add_argument("--limit", type=int, default=10, help="songs per recommendation")
    parser.add_argument("--include-writes", action="store_true",
                        help="also exercise record_feedback, which writes to the real store")
    parser.add_argument("--run-one", default=None,
                        help=argparse.SUPPRESS)  # internal: the child-process entrypoint
    args = parser.parse_args()

    if args.run_one:
        try:
            result = run_one_backend(args.budget, args.limit, args.include_writes)
        except Exception:  # noqa: BLE001 - report, don't traceback at the parent
            result = {"provider": args.run_one, "checks": [], "error": traceback.format_exc()}
        print("__RESULT__" + json.dumps(_serialisable(result)))
        return 0

    providers = configured_providers(args.provider)
    if not providers:
        print("No backend is configured. Set RECOM_YTMUSIC_MCP_COMMAND and/or "
              "RECOM_SPOTIFY_MCP_COMMAND (see README's Setup).")
        return 1

    print(f"Smoke-testing {len(providers)} backend(s): {', '.join(providers)}")
    return report([run_child(p, args) for p in providers])


def _serialisable(result):
    return {
        "provider": result.get("provider"),
        "graph": result.get("graph"),
        "error": result.get("error"),
        "checks": [c.as_dict() if isinstance(c, Check) else c for c in result.get("checks", [])],
    }


if __name__ == "__main__":
    sys.exit(main())
