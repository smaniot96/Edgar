#!/usr/bin/env python3
"""Solo-play harness for Edgar.

Drives the sync /turn endpoint as a real player would, prints a readable
transcript, and appends every turn (input + narration + adjudication +
character + scene) to claude-report/logs/transcript.jsonl for the report.

Usage:
  python3 play_harness.py "<message>"            # one turn
  python3 play_harness.py --session N "<msg>"    # explicit session
  python3 play_harness.py --state                # just print current character/scene
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://localhost:8000"
LOG = Path(__file__).parent / "logs" / "transcript.jsonl"
LOG.parent.mkdir(parents=True, exist_ok=True)


def _post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def turn(session, message):
    t0 = time.time()
    try:
        res = _post(f"/api/sessions/{session}/turn", {"message": message})
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        print(f"\n!!! HTTP {e.code}: {detail}\n")
        rec = {"ts": time.time(), "input": message, "error": f"{e.code}: {detail}"}
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        return None
    dt = time.time() - t0
    ch = res.get("character") or {}
    adj = res.get("adjudication") or {}
    cs = res.get("combat_state")
    print("=" * 70)
    print(f"> YOU: {message}")
    print("-" * 70)
    print(res.get("narration", "(no narration)"))
    print("-" * 70)
    meta = []
    if ch:
        meta.append(f"{ch.get('name')} HP {ch.get('hp_current')}/{ch.get('hp_max')}")
        if ch.get("conditions"):
            meta.append(f"conditions={ch.get('conditions')}")
    if res.get("current_scene_id"):
        meta.append(f"scene={res['current_scene_id']}")
    if adj:
        meta.append(f"adj.success={adj.get('success')}")
        if adj.get("dice_result") is not None:
            meta.append(f"dice={adj.get('dice_result')}")
        if adj.get("flags_set"):
            meta.append(f"flags_set={adj.get('flags_set')}")
    if cs:
        living = [c for c in (cs.get('combatants') or []) if not c.get('is_player') and c.get('alive')]
        enemy_hp = ", ".join(f"{c.get('name')} {c.get('hp_current')}/{c.get('hp_max')}" for c in (cs.get('combatants') or []) if not c.get('is_player'))
        meta.append(f"COMBAT round={cs.get('round')} ended={cs.get('ended')} outcome={cs.get('outcome')} enemies=[{enemy_hp}]")
    if res.get("campaign_complete"):
        meta.append("*** CAMPAIGN COMPLETE ***")
    meta.append(f"{dt:.1f}s")
    print(" | ".join(meta))
    print()
    rec = {"ts": time.time(), "input": message, "narration": res.get("narration"),
           "adjudication": adj, "character": ch, "scene": res.get("current_scene_id"),
           "combat_state": cs, "latency_s": round(dt, 1)}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return res


def main():
    args = sys.argv[1:]
    session = 1
    if args and args[0] == "--state":
        print(json.dumps(_get("/api/campaigns/1/characters"), indent=2))
        return
    if args and args[0] == "--session":
        session = int(args[1])
        args = args[2:]
    msg = " ".join(args)
    turn(session, msg)


if __name__ == "__main__":
    main()
