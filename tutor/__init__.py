"""Denisse as study tutor (Chuen, 5 Nov 2026) -- part 1 of 4: the foundation.

    python -m tutor ingest [--dry]     KEATS inbox -> tutor notes + topic maps
    python -m tutor topics <module>    print a module's topic map
    python -m tutor cards <module>     vault notes -> Anki cards (part 2, tutor/cards.py)
    python -m tutor anki-sync [module] cards -> Anki (TSV / AnkiConnect), review stats back

KEATS material arrives in a folder he downloads into (nothing here signs in
to KEATS). Each lecture's text is read by the local model and written up as
a tutor note in the module's Week folder: learning outcomes, key methods and
equations, worked examples, a check-yourself list. A per-module topic map
in the vault (tutor.json "don") says which lecture covers what, what it assumes, and
which coursework question needs it. Later tasks plug into those two things:
teaching (2), questions to Anki (3), progress and tutoring (4).

Everything configurable is in tutor/tutor.json; docs/tutor.md explains it.
"""
