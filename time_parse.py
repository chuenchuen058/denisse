"""
Denisse -- turn a spoken time phrase into a datetime, deterministically.

The model decides *what* to remind Chuen about and copies his words for
*when* ("Thursday at 3", "in 20 mins", "tomorrow morning", "on the 12th").
This module turns those words into a time. It is plain code so a wrong
answer is reproducible and testable (scripts/test_time_parse.py), and so the
8B model never has to do calendar arithmetic it can't see a clock for.

Everything is Europe/London. `now()` is the one clock; tests and the eval
pin it with `fixed_clock(...)`.

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
    a time today that has already passed ("today at 9" said at 10:00)
    a weekday that contradicts the date ("Friday the 12th" when the 12th
    is a Thursday), or a part of day that contradicts am/pm
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

PART_DEFAULTS = {
    "morning": time(9, 0),
    "lunchtime": time(12, 0),
    "afternoon": time(14, 0),
    "evening": time(18, 0),
    "tonight": time(20, 0),
    "night": time(20, 0),
}
END_OF_DAY = time(23, 59)
_PM_PARTS = {"afternoon", "evening", "tonight", "night"}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]
_WD_ALT = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3,
           "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
    "ninety": 90, "a": 1, "an": 1, "a couple of": 2, "couple of": 2,
    "a few": 3, "few": 3,
}

_fixed: datetime | None = None


def _aware(dt: datetime) -> datetime:
    """A naive datetime is London wall-clock time."""
    return dt.replace(tzinfo=LONDON) if dt.tzinfo is None else dt.astimezone(LONDON)


def now() -> datetime:
    """The clock everything here (and tasks.add_task) reads."""
    return _fixed if _fixed is not None else datetime.now(LONDON)


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

_UNIT = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800, "wk": 604800, "wks": 604800,
}
_NUM = r"(?:\d+(?:\.\d+)?|" + "|".join(
    sorted((re.escape(k) for k in _NUMBER_WORDS), key=len, reverse=True)) + r")"
_UNIT_RE = r"(?:" + "|".join(sorted(_UNIT, key=len, reverse=True)) + r")"
_DUR_PART = re.compile(r"(" + _NUM + r")\s*(" + _UNIT_RE + r")\b(\s+and\s+a\s+half)?")


def _num(word: str) -> float:
    word = word.strip()
    return float(word) if word[0].isdigit() else float(_NUMBER_WORDS[word])


def parse_duration(text: str) -> timedelta | None:
    """'20 mins', 'an hour and a half', '1h30', 'half an hour', '90 seconds',
    'a quarter of an hour', '2 hours 15 minutes'. None if any of it isn't a
    duration -- a half-understood length is worse than a question."""
    s = (text or "").lower().strip()
    s = re.sub(r"^(?:in|for|within|after)\s+", "", s)
    s = re.sub(r"\s+(?:from now|later|time)$", "", s).strip(" .")
    if not s:
        return None
    fixed = {"half an hour": 1800, "half a hour": 1800, "a half hour": 1800,
             "half hour": 1800, "a quarter of an hour": 900,
             "quarter of an hour": 900, "a quarter hour": 900,
             "three quarters of an hour": 2700}
    if s in fixed:
        return timedelta(seconds=fixed[s])
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
        base = datetime.combine(d, base.time().replace(tzinfo=None), LONDON)
    out = (base.astimezone(timezone.utc) + timedelta(seconds=rest)).astimezone(LONDON)
    return out.replace(microsecond=0)


# ---------------------------------------------------------------- phrases

_WORD_HOUR = {w: n for w, n in _NUMBER_WORDS.items() if n <= 12 and w.isalpha()
              and w not in ("a", "an", "few")}
_HOUR_TOKEN = r"(\d{1,2}|" + "|".join(_WORD_HOUR) + r")"
_MINS_TOKEN = r"(half|quarter|\d{1,2}|five|ten|twenty|twenty-five|twenty five)"
_MINS_VAL = {"half": 30, "quarter": 15, "five": 5, "ten": 10, "twenty": 20,
             "twenty-five": 25, "twenty five": 25}
_ORD = r"(\d{1,2})(?:st|nd|rd|th)?"
_MONTH_RE = r"(" + "|".join(m[:3] + r"(?:" + m[3:] + r")?" for m in MONTHS) + r"|sept)\.?"
_WD_RE = r"(" + "|".join(WEEKDAYS) + "|" + "|".join(
    sorted(_WD_ALT, key=len, reverse=True)) + r")"


def _month(word: str) -> int:
    return 9 if word.startswith("sep") else [m[:3] for m in MONTHS].index(word[:3]) + 1


def _hour_val(tok: str) -> int:
    return int(tok) if tok.isdigit() else _WORD_HOUR[tok]


# Words that carry no meaning of their own here.
_FILLER = {"by", "before", "due", "on", "at", "until", "till", "the", "of",
           "around", "about", "ish", "no", "later", "than", "nlt", "for",
           "in", "this", "and", "please", "remind", "me", "o'clock", "oclock",
           "sharp", "time", "-", "to", "a"}
# A leftover word that looks like timing we failed to read: the whole parse
# is distrusted rather than half-used ("3 days before friday" is not Friday).
_SUSPECT = re.compile(r"\d|\b(?:days?|weeks?|months?|years?|hours?|hrs?|mins?|"
                      r"minutes?|ago|end|eod|weekend|fortnight|midday|noon|"
                      r"am|pm|past|next|last)\b")


def _clean(text: str) -> str:
    s = (text or "").lower().strip()
    s = s.replace("’", "'").replace(",", " ").replace("!", " ").replace("?", " ")
    s = re.sub(r"\btmrw?\b|\btmw\b|\btomorow\b|\btmorrow\b", "tomorrow", s)
    s = re.sub(r"\bo'?clock\b", " ", s)
    s = re.sub(r"\s*\.\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class _Bits:
    before: bool = False
    day_word: str | None = None   # today / tomorrow / overmorrow
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
    if re.match(r"^(?:before|no later than|nlt)\b", s):
        b.before = True

    # ---- day
    m, s = _take(r"\bthe day after tomorrow\b|\bday after tomorrow\b", s)
    if m:
        b.day_word = "overmorrow"
    else:
        m, s = _take(r"\btomorrow\b", s)
        if m:
            b.day_word = "tomorrow"
        else:
            m, s = _take(r"\btoday\b", s)
            if m:
                b.day_word = "today"
    m, s = _take(r"\btonight\b", s)
    if m:
        b.part = "tonight"
        b.day_word = b.day_word or "today"

    m, s = _take(r"\b(this|next|coming|this coming)?\s*" + _WD_RE + r"\b", s)
    if m:
        w = m.group(2)
        b.weekday = WEEKDAYS.index(w) if w in WEEKDAYS else _WD_ALT[w]
        mod = m.group(1) or ""
        b.wd_mod = "next" if mod in ("next", "coming", "this coming") else (mod or None)

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
        m, s = _take(r"\b(morning|lunchtime|lunch|afternoon|evening|night)\b", s)
        if m:
            b.part = "lunchtime" if m.group(1) == "lunch" else m.group(1)
            s = re.sub(r"\bin the\s*$|\bin the\b(?=\s*$)", " ", s)

    # ---- clock time
    m, s = _take(r"\b(noon|midday|midnight)\b", s)
    if m:
        b.hour = 0 if m.group(1) == "midnight" else 12
        b.meridiem = "am" if m.group(1) == "midnight" else "pm"
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
    return datetime.combine(d, t, LONDON)


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
        if b.part == "morning":
            return h, "'morning' and 'pm' don't agree -- which did you mean?"
        return (h if h == 12 else h + 12), None
    if b.part == "morning":
        return (0 if h == 12 else h), None
    if b.part in _PM_PARTS or b.part == "lunchtime":
        return (h if h == 12 else h + 12) if h < 12 else h, None
    if h == 12 or h >= 13:
        return h, None
    return (h + 12 if 1 <= h <= 6 else h), None


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
    if re.match(r"^(?:in|within|after)\s+", s) or re.search(r"\b(?:from now|later)$", s):
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
        clock, has_time = PART_DEFAULTS[b.part], True
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
        if b.day_word == "today":
            day = today
        else:
            if ref.hour < 5:
                return ask(f"It's after midnight -- by '{b.day_word}' do you mean later "
                           f"today ({describe(_wall(today, clock), ref)}) or the day after?")
            day = today + timedelta(days=1 if b.day_word == "tomorrow" else 2)
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


def _suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
