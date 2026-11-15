"""Add a small "Related" block to auto-generated logs, linking the real notes they talk about.

Only adds links (inside marked comments at the end of each log); never deletes or moves text.
Settings live in related_links.json next to this script.

  python scripts/link_related_logs.py VAULT --dry-run OUT.json   # report only
  python scripts/link_related_logs.py VAULT --apply               # write + manifest
  python scripts/link_related_logs.py VAULT --undo MANIFEST.json  # remove the blocks again
"""
import argparse, json, math, pathlib, re, datetime
from collections import Counter

CFG = json.loads(pathlib.Path(__file__).with_name("related_links.json").read_text(encoding="utf-8"))


def rel(p, root):
    return p.relative_to(root).as_posix()


def under(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def frontmatter(text):
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    return m.group(1) if m else ""


def fm_list(fm, key):
    m = re.search(rf"^{key}:\s*\[(.*?)\]\s*$", fm, re.M)
    if m:
        return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
    m = re.search(rf"^{key}:\s*\r?\n((?:\s*-\s*.*\r?\n?)+)", fm, re.M)
    if m:
        return [x.strip()[1:].strip().strip("'\"") for x in m.group(1).splitlines() if x.strip()]
    return []


def phrase_re(phrase):
    parts = [re.escape(w) for w in re.split(r"[\s\-_]+", phrase.strip()) if w]
    return re.compile(r"(?<![\w])" + r"[\s\-_]+".join(parts) + r"(?![\w])", re.I)


def strip_block(text):
    s, e = re.escape(CFG["block_start"]), re.escape(CFG["block_end"])
    return re.sub(rf"\r?\n*{s}.*?{e}\r?\n?", "\n", text, flags=re.S)


def load(vault):
    notes = {}
    for p in vault.rglob("*.md"):
        r = rel(p, vault)
        if r.split("/")[0].startswith("."):
            continue
        try:
            raw = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        notes[r] = raw
    return notes


def targets_and_phrases(notes):
    code_re = re.compile(CFG["code_prefix_regex"])
    targets = {}
    for r, text in notes.items():
        if under(r, CFG["skip_roots"]) or under(r, CFG["log_roots"]):
            continue
        fm = frontmatter(text)
        if set(fm_list(fm, "tags")) & set(CFG["skip_tags"]):
            continue
        stem = r.rsplit("/", 1)[-1][:-3]
        if re.match(CFG.get("skip_stem_regex", "(?!)"), stem):
            continue
        targets[r] = {stem, *fm_list(fm, "aliases")}
    # A folder's name (e.g. a module) points at that folder's main note.
    by_dir = {}
    for r in targets:
        by_dir.setdefault(r.rsplit("/", 1)[0] if "/" in r else "", []).append(r)
    prefs = CFG.get("folder_note_suffixes", [])
    for d, members in by_dir.items():
        if not d:
            continue
        folder = d.rsplit("/", 1)[-1]
        name = code_re.sub("", folder)
        def rank(r):
            stem = r.rsplit("/", 1)[-1][:-3].lower()
            if stem in (folder.lower(), name.lower()):
                return (0, 0, len(stem))
            for i, suf in enumerate(prefs):
                if stem == f"{name.lower()} {suf}":
                    return (1, i, len(stem))
            return (2, 0, len(stem)) if stem.startswith(name.lower()) else (9, 0, len(stem))
        best = min(members, key=rank)
        if rank(best)[0] < 9:
            targets[best] |= {folder, name, *code_re.findall(folder + " ")}
    for r in targets:
        targets[r] = {p.strip() for p in targets[r] if len(p.strip()) >= CFG["min_phrase_len"]}
    return targets


def topic_of(path):
    for t in CFG["topics"]:
        if path.startswith(t["prefix"]):
            return t["name"]
    return CFG["topics"][-1]["name"]


def similar(bodies):
    """Each log's nearest other logs by shared distinctive words (tf-idf cosine)."""
    import numpy as np
    k = CFG["similar_logs"]
    if not k:
        return {}
    word = re.compile(CFG["similar_word_regex"])
    names = list(bodies)
    tfs = [Counter(w.lower() for w in word.findall(bodies[n])) for n in names]
    df = Counter(w for tf in tfs for w in tf)
    N = len(names)
    vocab = {w: i for i, w in enumerate(w for w, c in df.items() if 2 <= c <= CFG["similar_max_df"] * N)}
    M = np.zeros((N, len(vocab)), dtype=np.float32)
    for i, tf in enumerate(tfs):
        scored = sorted(((1 + math.log(c)) * math.log(N / df[w]), w) for w, c in tf.items() if w in vocab)
        for v, w in scored[-CFG["similar_top_terms"]:]:
            M[i, vocab[w]] = v
    M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
    S = M @ M.T
    np.fill_diagonal(S, 0)
    out = {}
    for i, n in enumerate(names):
        idx = np.argsort(-S[i])[:k]
        out[n] = [names[j] for j in idx if S[i, j] >= CFG["similar_min"]]
    return out


def plan(vault):
    notes = load(vault)
    targets = targets_and_phrases(notes)
    logs = {r: strip_block(t) for r, t in notes.items() if under(r, CFG["log_roots"])}
    bodies = {r: t[len(frontmatter(t)):] for r, t in logs.items()}
    phrase_hits, df = {}, Counter()
    regs = {p: phrase_re(p) for ps in targets.values() for p in ps}
    for r, body in bodies.items():
        hits = {}
        for p, rx in regs.items():
            n = len(rx.findall(body))
            if n:
                hits[p] = n
                df[p] += 1
        phrase_hits[r] = hits
    N = max(len(logs), 1)
    idf = {p: math.log(N / c) for p, c in df.items() if c / N <= CFG["max_doc_freq"]}
    pairs = []
    for r, hits in phrase_hits.items():
        for t, ps in targets.items():
            s = sum(math.log1p(hits[p]) * idf[p] for p in ps if p in hits and p in idf)
            if s >= CFG["min_score"]:
                pairs.append((s, r, t))
    # Best matches first; cap links per log and per target so no new star forms.
    out, incoming = {}, Counter()
    for s, r, t in sorted(pairs, reverse=True):
        links = out.setdefault(r, {"links": []})["links"]
        if len(links) < CFG["max_links"] and incoming[t] < CFG["max_in_links"]:
            links.append(t)
            incoming[t] += 1
    out = {r: {"links": i["links"], "topic": topic_of(i["links"][0])} for r, i in out.items() if i["links"]}
    sims = similar(bodies)
    for r, near in sims.items():
        if near:
            out.setdefault(r, {"links": [], "topic": None})["similar"] = near
    # Logs with no note match take the topic of their nearest log that has one.
    # Spread over similar-log links in both directions until it settles.
    nbrs = {}
    for r, i in out.items():
        for n in i.get("similar", []):
            nbrs.setdefault(r, set()).add(n)
            nbrs.setdefault(n, set()).add(r)
    for _ in range(CFG["topic_spread_passes"]):
        got = {}
        for r, i in out.items():
            if not i["topic"]:
                votes = Counter(out[n]["topic"] for n in nbrs.get(r, ()) if out.get(n, {}).get("topic"))
                if votes:
                    got[r] = votes.most_common(1)[0][0]
        if not got:
            break
        for r, t in got.items():
            out[r]["topic"] = t
    for i in out.values():
        i["topic"] = i["topic"] or CFG["topics"][-1]["name"]
    return notes, out


def block(info, nl):
    lines = [CFG["block_start"], CFG["heading"], f"{CFG['topic_field']}:: {info['topic']}"]
    lines += [f"- [[{t[:-3]}|{t.rsplit('/', 1)[-1][:-3]}]]" for t in info["links"]]
    if info.get("similar"):
        lines.append(CFG["similar_heading"] + " " + ", ".join(
            f"[[{t[:-3]}|{t.rsplit('/', 1)[-1][:-3].replace('[', '').replace(']', '')}]]" for t in info["similar"]))
    lines.append(CFG["block_end"])
    return nl.join(lines) + nl


def write_with(vault, r, text, info):
    nl = "\r\n" if "\r\n" in text else "\n"
    base = strip_block(text).rstrip("\r\n") + nl
    new = base + (nl + block(info, nl) if info else "")
    if new != text:
        (vault / r).write_bytes(new.encode("utf-8"))
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--dry-run", metavar="OUT")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", metavar="MANIFEST")
    a = ap.parse_args()
    vault = pathlib.Path(a.vault)
    backups = pathlib.Path(CFG.get("backup_dir", str(pathlib.Path.home() / "git" / "vault-backups")))

    if a.undo:
        man = json.loads(pathlib.Path(a.undo).read_text(encoding="utf-8"))
        bdir, restored, skipped = pathlib.Path(man["backup"]), 0, 0
        for r in man["files"]:
            cur = (vault / r).read_bytes().decode("utf-8")
            if cur.count(CFG["block_start"]) == 1 and strip_block(cur).rstrip() == (bdir / r).read_bytes().decode("utf-8").rstrip():
                (vault / r).write_bytes((bdir / r).read_bytes()); restored += 1
            else:
                skipped += 1
        print(f"restored {restored}, skipped {skipped} (changed since)")
        return

    notes, out = plan(vault)
    logs = [r for r in notes if under(r, CFG["log_roots"])]
    topics = Counter(i["topic"] for i in out.values())
    print(f"logs {len(logs)}, linked {len(out)}, links {sum(len(i['links']) for i in out.values())}, topics {dict(topics)}")
    if a.dry_run:
        pathlib.Path(a.dry_run).write_text(json.dumps(out, indent=1), encoding="utf-8")
        return
    if a.apply:
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
        bdir = backups / f"related-links-{stamp}"
        changed = []
        for r in logs:
            text = notes[r]
            info = out.get(r)
            if not info and CFG["block_start"] not in text:
                continue
            (bdir / r).parent.mkdir(parents=True, exist_ok=True)
            (bdir / r).write_bytes(text.encode("utf-8"))
            if write_with(vault, r, text, info):
                changed.append(r)
        man = bdir / "manifest.json"
        man.write_text(json.dumps({"backup": str(bdir), "files": changed}, indent=1), encoding="utf-8")
        print(f"changed {len(changed)} files; undo with --undo \"{man}\"")


if __name__ == "__main__":
    main()
