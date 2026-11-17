from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
from rapidfuzz import fuzz
import requests
import json
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True
}

BASE_BAD_KEYWORDS = [
    "cover", "karaoke", "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore", "bass boosted", "reaction", "review"
]

OFFICIAL_LABELS = [
    "vevo", "topic", "official", "sony", "t-series", "zee", 
    "warner", "universal", "monstercat", "record", "music", 
    "entertainment", "audio", "anirudh", "think music", "saregama", "lahari"
]

def fetch_spotify_embed_playlist(playlist_url: str) -> dict:
    """Fetches public playlist metadata and tracks via Spotify's embed endpoint without API keys."""
    match = re.search(r"playlist/([a-zA-Z0-9]+)", playlist_url)
    if not match:
        raise ValueError("Invalid Spotify playlist URL")
    
    playlist_id = match.group(1)
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    resp = requests.get(embed_url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise ValueError(f"Failed to fetch playlist (HTTP {resp.status_code})")
        
    next_data = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text,
        re.DOTALL
    )
    if not next_data:
        raise ValueError("Could not parse playlist data from Spotify embed page")
        
    data = json.loads(next_data.group(1))
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    track_list = entity.get("trackList", [])
    
    parsed_tracks = []
    for t in track_list:
        # 'subtitle' holds the artist(s), sometimes separated by non-breaking spaces
        artist_str = (t.get("subtitle") or "").replace("\xa0", " ").strip()
        primary_artist = artist_str.split(",")[0].strip() if artist_str else ""
        parsed_tracks.append({
            "name": (t.get("title") or "").strip(),
            "artist": primary_artist,
            "album": "",  # Embed payload doesn't include album name, handled gracefully in best_youtube
            "duration_ms": t.get("duration", 0)
        })
        
    return {
        "id": playlist_id,
        "name": entity.get("name") or entity.get("title", "Spotify Playlist"),
        "owner": entity.get("subtitle", "Spotify"),
        "tracks": parsed_tracks
    }

def clean_text(text: str) -> str:
    """Normalize text for comparison."""
    if not text: return ""
    text = text.lower()

    if not any(x in text for x in ["remix", "slowed", "reverb", "live"]):
        text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def get_channel_trust_score(channel_name: str, artist_name: str) -> int:
    """Calculates how 'official' a channel looks."""
    channel_norm = channel_name.lower()
    artist_norm = artist_name.lower()
    score = 0

    if fuzz.partial_ratio(artist_norm, channel_norm) > 85:
        score += 50
    if "topic" in channel_norm:
        score += 40

    if any(label in channel_norm for label in OFFICIAL_LABELS):
        score += 25
        
    return score

def best_youtube(title, artist, album, target_duration):
    search_query_parts = [title, artist]
    
    is_generic_album = any(x in album.lower() for x in ["greatest hits", "best of", "single", "compilation"])
    if album and not is_generic_album:
        search_query_parts.append(album)
    
    search_query_parts.append("Official Audio")
    query = " ".join(search_query_parts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            search = ydl.extract_info(f"ytsearch15:{query}", download=False)
        except Exception:
            return None

    candidates = []
    
    target_title_norm = clean_text(title)
    target_artist_norm = clean_text(artist)
    target_album_norm = clean_text(album)
    
    current_bad_keywords = [
        bad for bad in BASE_BAD_KEYWORDS if bad not in target_title_norm
    ]

    for e in search.get("entries", []):
        if not e: continue

        yt_id = e.get("id")
        yt_title = e.get("title") or ""
        yt_channel = e.get("uploader") or ""
        yt_duration = e.get("duration") or 0
        yt_views = e.get("view_count") or 0
        
        yt_title_norm = clean_text(yt_title)

        if any(bad in yt_title_norm for bad in current_bad_keywords):
            continue

        diff = abs(yt_duration - target_duration) if target_duration else 0
        if target_duration and diff > 60: 
            continue

        score = 0
        score += fuzz.token_set_ratio(target_title_norm, yt_title_norm)
        
        if target_album_norm and fuzz.partial_ratio(target_album_norm, yt_title_norm) > 80:
            score += 20

        score += get_channel_trust_score(yt_channel, artist)
        if target_duration:
            if diff <= 5: score += 25
            elif diff <= 15: score += 15

        if yt_views > 1000000: score += 15
        elif yt_views > 100000: score += 10
        elif yt_views > 10000: score += 5

        if fuzz.partial_ratio(target_artist_norm, yt_title_norm) < 50 and \
           fuzz.partial_ratio(target_artist_norm, clean_text(yt_channel)) < 50:
            score -= 40
            
        candidates.append({"data": e, "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)

    if candidates and candidates[0]["score"] > 50:
        best = candidates[0]["data"]
        return {
            "id": best["id"],
            "title": title,
            "artist": best.get("uploader") or artist,
            "sourceArtist": artist,
            "duration": best.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
        }
    
    return None

class ImportReq(BaseModel):
    playlistUrl: str
    limit: int | None = None

@app.post("/preview-spotify")
def preview_spotify(data: ImportReq):
    try:
        playlist = fetch_spotify_embed_playlist(data.playlistUrl)
    except Exception as e:
        return {"error": str(e)}

    total_duration_ms = sum(t["duration_ms"] for t in playlist["tracks"])
    mins = total_duration_ms // 60000

    return {
        "playlist": {
            "id": playlist["id"],
            "name": playlist["name"],
            "owner": playlist["owner"],
            "total_tracks": len(playlist["tracks"]),
            "duration": f"{mins // 60} hr {mins % 60} min"
        }
    }

@app.post("/import-spotify")
def import_spotify(data: ImportReq):
    try:
        playlist = fetch_spotify_embed_playlist(data.playlistUrl)
    except Exception as e:
        return {"error": str(e)}

    tracks = playlist["tracks"]
    if data.limit:
        tracks = tracks[:data.limit]

    songs = []
    skipped = {"spotify_unavailable": 0, "no_youtube_match": 0}

    for track in tracks:
        if not track["name"]:
            skipped["spotify_unavailable"] += 1
            continue

        yt = best_youtube(
            track["name"],
            track["artist"],
            track["album"],
            track["duration_ms"] // 1000
        )

        if yt:
            songs.append(yt)
        else:
            skipped["no_youtube_match"] += 1

    return {
        "playlist": {
            "id": playlist["id"],
            "name": playlist["name"],
            "total_tracks": len(playlist["tracks"])
        },
        "stats": {
            "processed": len(tracks),
            "imported": len(songs),
            "skipped_spotify": skipped["spotify_unavailable"],
            "skipped_youtube": skipped["no_youtube_match"]
        },
        "songs": songs
    }
