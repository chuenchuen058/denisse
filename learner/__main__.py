"""python -m learner <command>

  seed                          add the starting question bank (safe to repeat)
  ask [-n 2]                    print the next questions to put to Chuen, marks them asked
  answer Q_ID "text"            file his answer as a belief (source: answered)
  answer Q_ID --unsure          he isn't sure: stop asking, measure it instead
  add "claim" --area A --source stated|measured [--evidence E]
  supersede B_ID "new claim"    replace a belief that changed (old one kept, marked)
  show                          print the model
  render                        regenerate Atlas/Concepts/Learning/Learner-Model.md
Every change also re-renders the note.
"""
import argparse
import sys

from . import core


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m learner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    a = sub.add_parser("ask"); a.add_argument("-n", type=int, default=2)
    an = sub.add_parser("answer"); an.add_argument("qid"); an.add_argument("text", nargs="?", default="")
    an.add_argument("--unsure", action="store_true")
    ad = sub.add_parser("add"); ad.add_argument("claim"); ad.add_argument("--area", required=True, choices=core.AREAS)
    ad.add_argument("--source", required=True, choices=("stated", "measured")); ad.add_argument("--evidence", default="")
    su = sub.add_parser("supersede"); su.add_argument("bid"); su.add_argument("claim")
    sub.add_parser("show"); sub.add_parser("render")
    args = ap.parse_args(argv)

    data = core.load()
    changed = True
    if args.cmd == "seed":
        print(f"added {core.seed(data)} question(s)")
    elif args.cmd == "ask":
        qs = core.next_questions(data, args.n)
        print("\n".join(f"{q['id']} [{q['area']}] {q['text']}" for q in qs) or "no open questions due")
    elif args.cmd == "answer":
        if not args.unsure and not args.text.strip():
            ap.error("give the answer text, or --unsure")
        b = core.answer(data, args.qid, args.text, args.unsure)
        print(f"filed {b['id']}" if b else f"{args.qid} marked: measure, don't re-ask")
    elif args.cmd == "add":
        b = core.add_belief(data, args.claim, args.area, args.source, args.evidence)
        print(f"filed {b['id']}")
    elif args.cmd == "supersede":
        print(f"filed {core.supersede(data, args.bid, args.claim)['id']}")
    else:
        changed = False
        if args.cmd == "show":
            print(core.render(data))
    if changed:
        core.save(data)
    if changed or args.cmd == "render":
        if core.VAULT.is_dir():
            print(f"note -> {core.write_note(data)}")
        else:
            print(f"vault not found at {core.VAULT}; note not written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
