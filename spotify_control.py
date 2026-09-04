"""
Denisse -- Spotify playback control (play/pause/skip/search), via the
official Web API through the `spotipy` package.

Opt-in like Inworld's voice: with no SPOTIFY_CLIENT_ID/SECRET set, every
function here just returns a clear "not configured" message instead of
touching the network. Requires a free Spotify Developer app (client ID +
secret, see .env.example) and a Spotify Premium account -- the Web API's
playback-control endpoints (play/pause/skip) are Premium-only; a free
account can search but the "no active device"-style errors will show up
for actual playback.

First real call opens a one-time browser login (spotipy's standard OAuth
flow) so you can authorize Denisse against your own Spotify account; the
resulting token is cached at memory/.spotify_cache and silently refreshed
after that -- no repeated logins.

Also needs the Spotify app itself open and playing (or at least active)
on some device -- "Spotify Connect" only controls a device that's already
running, it doesn't launch one. Pair with the existing open_application
tool if Spotify isn't open yet.
"""

import os

import spotipy
from spotipy.oauth2 import SpotifyOAuth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "memory", ".spotify_cache")

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-modify-playback-state user-read-playback-state"

_client = None


def _get_client() -> "spotipy.Spotify | None":
    global _client
    if _client is not None:
        return _client
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_path=CACHE_PATH,
    )
    _client = spotipy.Spotify(auth_manager=auth)
    return _client


def _not_configured() -> str:
    return (
        "Spotify isn't set up yet -- needs SPOTIFY_CLIENT_ID and "
        "SPOTIFY_CLIENT_SECRET in .env (see .env.example for how to get them)."
    )


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    if "NO_ACTIVE_DEVICE" in msg or "No active device" in msg:
        return "Spotify doesn't see an active device -- make sure the Spotify app is open and try again."
    if "PREMIUM_REQUIRED" in msg:
        return "That needs Spotify Premium -- this account doesn't have it."
    return f"Spotify error: {msg}"


def play_song(query: str) -> str:
    """Search for a track and start playing it on whatever device is
    currently active in Spotify."""
    sp = _get_client()
    if sp is None:
        return _not_configured()
    try:
        results = sp.search(q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return f"Couldn't find a track matching {query!r} on Spotify."
        track = items[0]
        name = track["name"]
        artist = ", ".join(a["name"] for a in track["artists"])
        sp.start_playback(uris=[track["uri"]])
        return f"Now playing {name} by {artist}."
    except Exception as e:
        return _friendly_error(e)


def pause_music() -> str:
    sp = _get_client()
    if sp is None:
        return _not_configured()
    try:
        sp.pause_playback()
        return "Paused."
    except Exception as e:
        return _friendly_error(e)


def resume_music() -> str:
    sp = _get_client()
    if sp is None:
        return _not_configured()
    try:
        sp.start_playback()
        return "Resumed."
    except Exception as e:
        return _friendly_error(e)


def skip_song() -> str:
    sp = _get_client()
    if sp is None:
        return _not_configured()
    try:
        sp.next_track()
        return "Skipped to the next track."
    except Exception as e:
        return _friendly_error(e)


def previous_song() -> str:
    sp = _get_client()
    if sp is None:
        return _not_configured()
    try:
        sp.previous_track()
        return "Went back to the previous track."
    except Exception as e:
        return _friendly_error(e)


def current_track() -> str:
    sp = _get_client()
    if sp is None:
        return _not_configured()
    try:
        playback = sp.current_playback()
        if not playback or not playback.get("item"):
            return "Nothing is currently playing."
        item = playback["item"]
        name = item["name"]
        artist = ", ".join(a["name"] for a in item["artists"])
        state = "playing" if playback.get("is_playing") else "paused"
        return f"{name} by {artist} -- {state}."
    except Exception as e:
        return _friendly_error(e)
