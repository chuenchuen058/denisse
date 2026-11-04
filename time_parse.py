"""
Denisse -- turn a spoken time phrase into a datetime, deterministically.

The model decides *what* to remind Chuen about and copies his words for
*when* ("Thursday at 3", "in 20 mins", "tomorrow morning", "on the 12th").
This module turns those words into a time. It is plain code so a wrong
answer is reproducible and testable (scripts/test_time_parse.py), and so the
8B model never has to do calendar arithmetic it can't see a clock for.

The timezone, every word list, the day-part times and the ambiguity limits
are in time_parse.json (Rule 0); the values quoted below are its shipped
defaults. Day-part times can be overridden at runtime by set_part_source()
-- the hook for what the learner model knows about Chuen. `now()` is the
one clock; tests and the eval pin it with `fixed_clock(...)`.

parse(text) returns a Parsed:
    when       aware datetime, or None
    question   set instead of `when` when the phrase is genuinely ambiguous
               -- ask Chuen, don't guess
    has_time   False when only a day was given (when = that day 23:59)
    leftover   words that weren't understood (shown, not silently dropped)
when=None and question=None means "not a time phrase I can read"; callers
fall back to whatever they did before (tasks.py: dateparser).

Documented defaults -- every one of these is a guess, which is why every
reply echoes the resolved time in words (describe()):
    morning 09:00 . lunchtime 12:00 . afternoon 14:00 . evening 18:00
    tonight / night 20:00 . a day with no time 23:59 (end of that day,
    the convention tasks.py already used for "friday")
    a bare hour with no am/pm: 1-6 -> pm, 7-11 -> am, 12 -> noon; with no
    day given, if that has passed today but the other half of the day
    hasn't, the other half ("at 8" said at 18:00 -> 20:00)
    "this Monday"  -> the Monday of this week still to come (today if today)
    "next Monday"  -> the first Monday after today (tasks.py's existing
                      reading; "the week after" is not assumed)
    "before Friday"  -> by the end of Friday (tasks.py, tested)
    "before 12 Nov"  -> by the end of 11 Nov (tasks.py, tested)
    "12/11" is 12 November (UK order)
Ambiguous, so parse() asks instead:
    the weekday or day-of-month that is today, when the time hasn't passed
    ("Thursday at 3" said Thursday morning: today or next week?)
    "tomorrow" said between midnight and 05:00 (today's daytime, or the next?)
    -- ambiguity.small_hours_end in the JSON
    a time today that has already passed ("today at 9" said at 10:00)
    a weekday that contradicts the date ("Friday the 12th" when the 12th
    is a Thursday), or a part of day that contradicts am/pm
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

# Every word list, default time, the timezone and the ambiguity limits live
# in time_parse.json beside this file (Rule 0); this module only says how
# they fit together. Loaded once, at import.
with open(os.path.splitext(os.path.abspath(__file__))[0] + ".json", encoding="utf-8") as _f:
    CONFIG = json.load(_f)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _alt(words) -> str:
    """A regex alternation of literal words, longest first."""
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


TZ = ZoneInfo(CONFIG["timezone"])
LONDON = TZ  # the name tasks.py and the tests already use

PART_DEFAULTS = {p: _hhmm(t) for p, t in CONFIG["day_parts"].items()}
_PART_ALIASES = dict(CONFIG["day_part_aliases"])
END_OF_DAY = _hhmm(CONFIG["end_of_day"])
_AM_PARTS = set(CONFIG["am_parts"])
_PM_PARTS = set(CONFIG["pm_parts"])
_PM_LEANING = _PM_PARTS | set(CONFIG["pm_leaning_parts"])
_TODAY_PARTS = set(CONFIG["parts_meaning_today"])

WEEKDAYS = list(CONFIG["weekdays"])
_WD_ALT = dict(CONFIG["weekday_abbreviations"])
MONTHS = list(CONFIG["months"])
_MONTH_ALT = dict(CONFIG["month_abbreviations"])
_NUMBER_WORDS = dict(CONFIG["number_words"])
_DAY_WORDS = dict(CONFIG["day_words"])
_AMB = CONFIG["ambiguity"]
_BARE_PM_LO, _BARE_PM_HI = _AMB["bare_hour_pm"]
_SMALL_HOURS_END = _AMB["small_hours_end"]

_fixed: datetime | None = None

# ---------------------------------------------------------------- day parts
#
# "tomorrow morning" means 09:00 only until something knows better. The hook
# below is where what the learner model knows about Chuen ("he's never up
# before 10") gets to say so: a callable returning {part: "HH:MM" | time}.
# Nothing here wires the learner itself; a source that raises, or returns a
# part or time that doesn't make sense, leaves the JSON default in place.

_part_source: Callable[[], dict] | None = None


def set_part_source(source: Callable[[], dict] | None) -> None:
    """Install (or, with None, remove) the override for day-part times."""
    global _part_source
    _part_source = source


def part_time(part: str) -> time:
    """The time a part of day means: the override if it has one, else the
    default from time_parse.json."""
    if _part_source is not None:
        try:
            val = (_part_source() or {}).get(part)
            if isinstance(val, time):
                return val.replace(tzinfo=None)
            if isinstance(val, str):
                return _hhmm(val.strip())
        except Exception:
            pass
    return PART_DEFAULTS[part]


def _aware(dt: datetime) -> datetime:
    """A naive datetime is wall-clock time in the configured timezone."""
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def now() -> datetime:
    """The clock everything here (and tasks.add_task) reads."""
    return _fixed if _fixed is not None else datetime.now(TZ)


def set_clock(dt: datetime | None) -> None:
    global _fixed
    _fixed = None if dt is None else _aware(dt)


@contextmanager
def fixed_clock(dt: datetime):
    """Pin now() for a test or an eval run."""
    global _fixed
    before = _fixed
    set_clock(dt)
    try:
        yield _fixed
    finally:
        _fixed = before


@dataclass
class Parsed:
    when: datetime | None = None
    question: str | None = None
    has_time: bool = False
    leftover: str = ""

    @property
    def ok(self) -> bool:
        return self.when is not None


# ---------------------------------------------------------------- durations

_UNIT = dict(CONFIG["duration_units"])
_DUR_PHRASES = dict(CONFIG["duration_phrases"])
_DUR_LEAD = re.compile(r"^(?:" + _alt(CONFIG["duration_lead_words"]) + r")\s+")
_DUR_TAIL = re.compile(r"\s+(?:" + _alt(CONFIG["duration_tail_words"]) + r")$")
_SPAN_LEAD = re.compile(r"^(?:" + _alt(CONFIG["span_lead_words"]) + r")\s+")
_SPAN_TAIL = re.compile(r"\b(?:" + _alt(CONFIG["span_tail_words"]) + r")$")
_NUM = r"(?:\d+(?:\.\d+)?|" + _alt(_NUMBER_WORDS) + r")"
_UNIT_RE = r"(?:" + _alt(_UNIT) + r")"
_DUR_PART = re.compile(r"(" + _NUM + r")\s*(" + _UNIT_RE + r")\b(\s+and\s+a\s+half)?")


def _num(word: str) -> float:
    word = word.strip()
    return float(word) if word[0].isdigit() else float(_NUMBER_WORDS[word])


def parse_duration(text: str) -> timedelta | None:
    """'20 mins', 'an hour and a half', '1h30', 'half an hour', '90 seconds',
    'a quarter of an hour', '2 hours 15 minutes'. None if any of it isn't a
    duration -- a half-understood length is worse than a question."""
    s = (text or "").lower().strip()
    s = _DUR_LEAD.sub("", s)
    s = _DUR_TAIL.sub("", s).strip(" .")
    if not s:
        return None
    if s in _DUR_PHRASES:
        return timedelta(seconds=_DUR_PHRASES[s])
    m = re.fullmatch(r"(\d+)\s*h\s*(\d+)\s*(?:m|mins?)?", s)  # 1h30, 1h 30m
    if m:
        return timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))
    m = re.fullmatch(r"(\d+):(\d{2})", s)  # 1:30 as a length
    if m:
        return timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))
    total, pos = 0.0, 0
    for part in _DUR_PART.finditer(s):
        gap = s[pos:part.start()].strip(" ,")
        if gap not in ("", "and"):
            return None
        secs = _UNIT[part.group(2)]
        total += _num(part.group(1)) * secs + (secs / 2 if part.group(3) else 0)
        pos = part.end()
    if pos == 0 or s[pos:].strip(" ,"):
        return None
    return timedelta(seconds=total) if total > 0 else None


def _plus(base: datetime, span: timedelta) -> datetime:
    """Hours and minutes are elapsed time (correct across a clock change);
    whole days keep the wall-clock time."""
    days, rest = divmod(span.total_seconds(), 86400)
    if days:
        d = base.date() + timedelta(days=int(days))
        base = datetime.combine(d, base.time().replace(tzinfo=None), TZ)
    out = (base.astimezone(timezone.utc) + timedelta(seconds=rest)).astimezone(TZ)
    return out.replace(microsecond=0)


# ---------------------------------------------------------------- phrases

_WORD_HOUR = {w: n for w, n in _NUMBER_WORDS.items() if n <= 12 and w.isalpha()
              and w not in set(CONFIG["not_hour_words"])}
_HOUR_TOKEN = r"(\d{1,2}|" + _alt(_WORD_HOUR) + r")"
_MINS_VAL = dict(CONFIG["minute_words"])
_MINS_TOKEN = r"(" + _alt(_MINS_VAL) + r"|\d{1,2})"
_ORD = r"(\d{1,2})(?:st|nd|rd|th)?"
# Each month by its full name or its first three letters, plus the extra
# abbreviations in the JSON ("sept").
_MONTH_RE = r"(" + "|".join(
    [re.escape(a) for a in sorted(_MONTH_ALT, key=len, reverse=True)]
    + [re.escape(m[:3]) + r"(?:" + re.escape(m[3:]) + r")?" for m in MONTHS]) + r")\.?"
_WD_RE = r"(" + _alt(WEEKDAYS) + "|" + _alt(_WD_ALT) + r")"
_WD_NEXT = set(CONFIG["next_weekday_modifiers"])
_WD_MODS = _WD_NEXT | set(CONFIG["this_weekday_modifiers"])
_PART_RE = r"\b(" + _alt(set(PART_DEFAULTS) - _TODAY_PARTS | set(_PART_ALIASES)) + r")\b"
_TODAY_PART_RE = r"\b(" + _alt(_TODAY_PARTS) + r")\b" if _TODAY_PARTS else None
_NAMED_TIMES = {w: _hhmm(t) for w, t in CONFIG["named_times"].items()}
_BEFORE_RE = r"^(?:" + _alt(CONFIG["before_words"]) + r")\b"


def _month(word: str) -> int:
    if word in _MONTH_ALT:
        return _MONTH_ALT[word]
    return [m[:3] for m in MONTHS].index(word[:3]) + 1


def _hour_val(tok: str) -> int:
    return int(tok) if tok.isdigit() else _WORD_HOUR[tok]


# Words that carry no meaning of their own here.
_FILLER = set(CONFIG["filler"])
# A leftover word that looks like timing we failed to read: the whole parse
# is distrusted rather than half-used ("3 days before friday" is not Friday).
_SUSPECT = re.compile(r"\d|\b(?:" + _alt(CONFIG["suspect_words"]) + r")\b")
_MISSPELT = [(re.compile(r"\b(?:" + _alt(bad) + r")\b"), right)
             for right, bad in CONFIG["misspellings"].items()]


def _clean(text: str) -> str:
    s = (text or "").lower().strip()
    s = s.replace("’", "'").replace(",", " ").replace("!", " ").replace("?", " ")
    for pattern, right in _MISSPELT:
        s = pattern.sub(right, s)
    s = re.sub(r"\bo'?clock\b", " ", s)
    s = re.sub(r"\s*\.\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class _Bits:
    before: bool = False
    day_word: str | None = None   # the words he used: "tomorrow", "tonight", ...
    day_offset: int = 0           # days after today they mean
    weekday: int | None = None
    wd_mod: str | None = None     # this / next / None
    dom: int | None = None
    month: int | None = None
    year: int | None = None
    part: str | None = None
    hour: int | None = None
    minute: int = 0
    meridiem: str | None = None   # am / pm / None (bare)
    explicit_24h: bool = False


def _take(pattern: str, s: str):
    m = re.search(pattern, s)
    if not m:
        return None, s
    return m, (s[:m.start()] + " " + s[m.end():])


def _extract(s: str) -> tuple[_Bits, str]:
    b = _Bits()
    if re.match(_BEFORE_RE, s):
        b.before = True

    # ---- day (the JSON lists day words in priority order)
    for phrase, offset in _DAY_WORDS.items():
        m, s = _take(r"\b" + re.escape(phrase) + r"\b", s)
        if m:
            b.day_word, b.day_offset = phrase, offset
            break
    if _TODAY_PART_RE:
        m, s = _take(_TODAY_PART_RE, s)
        if m:
            b.part = m.group(1)
            if b.day_word is None:
                b.day_word, b.day_offset = m.group(1), 0

    mods = _alt(_WD_MODS)
    m, s = _take(r"\b(" + mods + r")?\s*" + _WD_RE + r"\b", s)
    if m:
        w = m.group(2)
        b.weekday = WEEKDAYS.index(w) if w in WEEKDAYS else _WD_ALT[w]
        mod = m.group(1) or ""
        b.wd_mod = "next" if mod in _WD_NEXT else ("this" if mod else None)

    # ---- explicit dates
    m, s = _take(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        b.year, b.month, b.dom = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if b.dom is None:
        m, s = _take(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s)
        if m:
            b.dom, b.month = int(m.group(1)), int(m.group(2))
            if m.group(3):
                y = int(m.group(3))
                b.year = y + 2000 if y < 100 else y
    if b.dom is None:
        m, s = _take(r"\b" + _ORD + r"\s+(?:of\s+)?" + _MONTH_RE + r"(?:\s+(\d{4}))?\b", s)
        if m:
            b.dom, b.month = int(m.group(1)), _month(m.group(2))
            b.year = int(m.group(3)) if m.group(3) else None
    if b.dom is None:
        m, s = _take(r"\b" + _MONTH_RE + r"\s+(?:the\s+)?" + _ORD + r"\b(?:\s+(\d{4}))?(?!\s*(?:am|pm|:))", s)
        if m:
            b.month, b.dom = _month(m.group(1)), int(m.group(2))
            b.year = int(m.group(3)) if m.group(3) else None
    if b.dom is None:
        m, s = _take(r"\b(\d{1,2})(?:st|nd|rd|th)\b", s)
        if m:
            b.dom = int(m.group(1))

    # ---- part of day
    if b.part is None:
        m, s = _take(_PART_RE, s)
        if m:
            b.part = _PART_ALIASES.get(m.group(1), m.group(1))
            s = re.sub(r"\bin the\s*$|\bin the\b(?=\s*$)", " ", s)

    # ---- clock time
    m, s = _take(r"\b(" + _alt(_NAMED_TIMES) + r")\b", s)
    if m:
        t = _NAMED_TIMES[m.group(1)]
        b.hour, b.minute = t.hour, t.minute
        b.meridiem = "am" if t.hour < 12 else "pm"
    if b.hour is None:
        m, s = _take(r"\b" + _MINS_TOKEN + r"\s+(?:minutes\s+)?(past|after|to)\s+" + _HOUR_TOKEN
                     + r"\b(?:\s*(am|pm|a\.m\.|p\.m\.))?", s)
        if m:
            mins = _MINS_VAL.get(m.group(1)) or int(m.group(1))
            hour = _hour_val(m.group(3))
            if m.group(2) == "to":
                hour, mins = (hour - 1) or 12, 60 - mins
            b.hour, b.minute = hour, mins
            b.meridiem = (m.group(4) or "").replace(".", "") or None
    if b.hour is None:
        m, s = _take(r"\b(\d{1,2})[:.h](\d{2})\s*(am|pm|a\.m\.|p\.m\.)?(?=\s|$)", s)
        if m:
            b.hour, b.minute = int(m.group(1)), int(m.group(2))
            b.meridiem = (m.group(3) or "").replace(".", "") or None
            b.explicit_24h = b.meridiem is None and (b.hour >= 13 or m.group(1).startswith("0")
                                                     or b.hour == 0)
    if b.hour is None:
        m, s = _take(r"\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)(?=\s|$)", s)
        if m:
            b.hour, b.meridiem = int(m.group(1)), m.group(2).replace(".", "")
    if b.hour is None:
        m, s = _take(r"\b(?:at|for|by|around|about)\s+" + _HOUR_TOKEN + r"\b(?!\s*(?:st|nd|rd|th))", s)
        if m:
            b.hour = _hour_val(m.group(1))
            if b.hour >= 13:
                b.explicit_24h = True
    if b.hour is None and b.part is None and b.day_word is None and b.weekday is None and b.dom is None:
        m, s = _take(r"^\s*" + _HOUR_TOKEN + r"\s*$", s)  # a bare "3"
        if m:
            b.hour = _hour_val(m.group(1))
    if b.hour is None:
        # "half four" -- British for 16:30
        m, s = _take(r"\bhalf\s+" + _HOUR_TOKEN + r"\b", s)
        if m:
            b.hour, b.minute = _hour_val(m.group(1)), 30

    left = " ".join(w for w in s.split() if w not in _FILLER)
    return b, left


def _wall(d: date, t: time) -> datetime:
    return datetime.combine(d, t, TZ)


def _hour24(b: _Bits) -> tuple[int, str | None]:
    """24h hour, and a question if am/pm contradicts the part of day."""
    h, mer = b.hour, b.meridiem
    if b.explicit_24h:
        return h, None
    if mer == "am":
        if b.part in _PM_PARTS:
            return h, f"'{b.part}' and 'am' don't agree -- which did you mean?"
        return (0 if h == 12 else h), None
    if mer == "pm":
        if b.part in _AM_PARTS:
            return h, f"'{b.part}' and 'pm' don't agree -- which did you mean?"
        return (h if h == 12 else h + 12), None
    if b.part in _AM_PARTS:
        return (0 if h == 12 else h), None
    if b.part in _PM_LEANING:
        return (h if h == 12 else h + 12) if h < 12 else h, None
    if h == 12 or h >= 13:
        return h, None
    return (h + 12 if _BARE_PM_LO <= h <= _BARE_PM_HI else h), None


def _fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def describe(when: datetime, now_: datetime | None = None) -> str:
    """'Thursday 6 Nov, 15:00' -- the words a reply echoes back so a wrong
    parse is visible. The year is added only when it isn't this one."""
    when = _aware(when)
    ref = _aware(now_) if now_ is not None else now()
    s = f"{WEEKDAYS[when.weekday()].title()} {when.day} {when:%b}"
    if when.year != ref.year:
        s += f" {when.year}"
    return f"{s}, {_fmt_time(when.time())}"


def parse(text: str, now_: datetime | None = None) -> Parsed:
    ref = _aware(now_) if now_ is not None else now()
    s = _clean(text)
    if not s:
        return Parsed()

    # A pure span from now: "in 20 mins", "2 hours from now".
    if _SPAN_LEAD.match(s) or _SPAN_TAIL.search(s):
        span = parse_duration(s)
        if span is not None:
            return Parsed(when=_plus(ref, span), has_time=True)

    b, left = _extract(s)
    if left and _SUSPECT.search(left):
        return Parsed(leftover=left)
    have_day = any(x is not None for x in (b.day_word, b.weekday, b.dom))
    if not have_day and b.hour is None and b.part is None:
        return Parsed(leftover=left)

    today = ref.date()

    # ---- the time of day
    question = None
    if b.hour is not None:
        if b.hour > 23 or b.minute > 59:
            return Parsed(leftover=left)
        h, question = _hour24(b)
        if question:
            return Parsed(question=question, leftover=left)
        clock = time(h, b.minute)
        has_time = True
    elif b.part:
        clock, has_time = part_time(b.part), True
    else:
        clock, has_time = END_OF_DAY, False

    def ask(q):
        return Parsed(question=q, has_time=has_time, leftover=left)

    # ---- the day
    day = None
    if b.dom is not None:
        year = b.year or today.year
        month = b.month
        try:
            if month is None:
                # "on the 12th": the next 12th, skipping months without one.
                month, year = today.month, today.year
                for _ in range(13):
                    try:
                        cand = date(year, month, b.dom)
                    except ValueError:
                        cand = None
                    if cand is not None and cand >= today:
                        break
                    month, year = (1, year + 1) if month == 12 else (month + 1, year)
                if cand is None:
                    return Parsed(leftover=left)
                day = cand
                if day == today and _wall(day, clock) > ref:
                    nxt = "next month"
                    return ask(f"Today is the {b.dom}{_suffix(b.dom)} -- do you mean today "
                               f"({_fmt_time(clock)}) or the {b.dom}{_suffix(b.dom)} {nxt}?")
                if day == today:
                    day = None  # passed today: the next one
                    month, year = (1, year + 1) if month == 12 else (month + 1, year)
                    for _ in range(12):
                        try:
                            day = date(year, month, b.dom)
                            break
                        except ValueError:
                            month, year = (1, year + 1) if month == 12 else (month + 1, year)
            else:
                day = date(year, month, b.dom)
                if b.year is None and day < today:
                    day = date(year + 1, month, b.dom)
        except ValueError:
            return Parsed(leftover=left)
        if b.weekday is not None and day.weekday() != b.weekday:
            return ask(f"The {day.day}{_suffix(day.day)} of {day:%B} is a "
                       f"{WEEKDAYS[day.weekday()].title()}, not a {WEEKDAYS[b.weekday].title()} "
                       "-- which did you mean?")
        if b.before and not has_time:
            day -= timedelta(days=1)
    elif b.weekday is not None:
        ahead = (b.weekday - today.weekday()) % 7
        name = WEEKDAYS[b.weekday].title()
        if b.wd_mod == "next" and ahead == 0:
            ahead = 7
        elif b.wd_mod is None and ahead == 0:
            if _wall(today, clock) > ref:
                when_s = _fmt_time(clock) if has_time else "end of day"
                return ask(f"It's {name} today -- do you mean today ({when_s}) or "
                           f"next {name}, {describe(_wall(today + timedelta(days=7), clock), ref)}?")
            ahead = 7
        day = today + timedelta(days=ahead)
    elif b.day_word is not None:
        if b.day_offset == 0:
            day = today
        else:
            if ref.hour < _SMALL_HOURS_END:
                return ask(f"It's after midnight -- by '{b.day_word}' do you mean later "
                           f"today ({describe(_wall(today, clock), ref)}) or the day after?")
            day = today + timedelta(days=b.day_offset)
    else:
        # A time with no day: the next time it comes round.
        day = today
        if _wall(today, clock) <= ref:
            bare = (b.hour is not None and b.meridiem is None and not b.explicit_24h
                    and b.part is None)
            other = time(clock.hour + 12, clock.minute) if 0 < clock.hour < 12 else None
            if bare and other and _wall(today, other) > ref:
                clock = other  # "at 8" said at 18:00 is 20:00
            else:
                day = today + timedelta(days=1)

    when = _wall(day, clock)
    if when <= ref:
        # "today at 9" at 10:00, "this Tuesday at 9" on Tuesday at 10:00, a
        # dated day already gone: never quietly set a reminder in the past.
        return ask(f"{describe(when, ref)} has already passed -- when should it be?")
    return Parsed(when=when, has_time=has_time, leftover=left)


def only_a_time(text: str) -> bool:
    """Whether `text` says WHEN and nothing else: 'Thursday', 'tomorrow at
    5', 'tonight'. A part of day on its own ('lunch') is not counted -- it
    can be the thing itself. For the task policy: a reminder whose text is
    only its time has no subject (2026-11-04, "remind me thursday")."""
    s = _clean(text)
    if not s:
        return False
    if _SPAN_LEAD.match(s) or _SPAN_TAIL.search(s):
        return parse_duration(s) is not None
    b, left = _extract(s)
    pinned = b.day_word is not None or b.weekday is not None or b.dom is not None or b.hour is not None
    return pinned and not left.strip()


def _suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
