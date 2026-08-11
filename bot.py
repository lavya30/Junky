import os
import json
import redis
import time
import base64
import urllib.parse
import asyncio
import discord
import aiohttp
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
UPSTASH_REDIS_URL = os.getenv("UPSTASH_REDIS_URL")

SPOTIFY_GREEN = 0x1DB954
HISTORY_EXPIRY_SECONDS = 48 * 3600  # Keep history for 48 hours (2 days)

# --- Redis Connection ---
if not UPSTASH_REDIS_URL:
    raise RuntimeError("UPSTASH_REDIS_URL environment variable is not set! Add it to your .env file.")
redis_client = redis.from_url(UPSTASH_REDIS_URL, decode_responses=True)

# --- Gemini AI client (async) ---
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# --- App-level Spotify Client Credentials Token Cache ---
_app_spotify_token: str | None = None
_app_spotify_token_expires: float = 0


# ==========================================
# 1. User Token Storage & Management
# ==========================================
def load_user_tokens() -> dict:
    """Load connected user tokens from Redis."""
    try:
        data = redis_client.get("user_tokens")
        if data:
            return json.loads(data)
    except Exception as e:
        print(f"Error reading user_tokens from Redis: {e}")
    return {}


def save_user_tokens(tokens: dict):
    """Save user tokens to Redis."""
    try:
        redis_client.set("user_tokens", json.dumps(tokens))
    except Exception as e:
        print(f"Error saving user_tokens to Redis: {e}")


def load_song_history() -> dict:
    """Load recent song recommendation history from Redis."""
    try:
        data = redis_client.get("song_history")
        if data:
            return json.loads(data)
    except Exception as e:
        print(f"Error reading song_history from Redis: {e}")
    return {}


def save_song_history(history: dict):
    """Save recent song recommendation history to Redis."""
    try:
        redis_client.set("song_history", json.dumps(history))
    except Exception as e:
        print(f"Error saving song_history to Redis: {e}")


def get_user_recent_songs(discord_user_id: int) -> list[str]:
    """Return a list of track names/artists recommended to this user within the last 48 hours."""
    history = load_song_history()
    user_str = str(discord_user_id)
    records = history.get(user_str, [])
    now = time.time()

    valid_records = [
        r for r in records
        if isinstance(r, dict) and (now - r.get("timestamp", 0) < HISTORY_EXPIRY_SECONDS)
    ]
    return [r.get("song") for r in valid_records if r.get("song")]


def add_user_recent_song(discord_user_id: int, song_entry: str):
    """Record a recommended song for a user with the current timestamp."""
    history = load_song_history()
    user_str = str(discord_user_id)
    records = history.get(user_str, [])
    now = time.time()

    valid_records = [
        r for r in records
        if isinstance(r, dict) and (now - r.get("timestamp", 0) < HISTORY_EXPIRY_SECONDS)
    ]
    valid_records.append({
        "song": song_entry,
        "timestamp": now,
    })
    # Keep up to last 30 entries
    history[user_str] = valid_records[-30:]
    save_song_history(history)


async def get_valid_user_token(discord_user_id: int) -> str | None:
    """Get a valid access token for a connected user, refreshing if expired."""
    tokens = load_user_tokens()
    user_str = str(discord_user_id)
    user_data = tokens.get(user_str)

    if not user_data:
        return None

    # Check if token is still valid (with 60s buffer)
    if time.time() < user_data.get("expires_at", 0) - 60:
        return user_data.get("access_token")

    # Refresh the token
    refresh_token = user_data.get("refresh_token")
    if not refresh_token:
        return None

    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={
                    "Authorization": f"Basic {auth_b64}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            ) as resp:
                if resp.status != 200:
                    print(f"Failed to refresh token for user {discord_user_id}: {await resp.text()}")
                    return None
                data = await resp.json()

                user_data["access_token"] = data["access_token"]
                user_data["expires_at"] = time.time() + data.get("expires_in", 3600)
                if "refresh_token" in data:
                    user_data["refresh_token"] = data["refresh_token"]

                tokens[user_str] = user_data
                save_user_tokens(tokens)
                return user_data["access_token"]
    except Exception as e:
        print(f"Error refreshing token for user {discord_user_id}: {e}")
        return None


# ==========================================
# 2. Spotify API Methods
# ==========================================
async def get_app_spotify_token() -> str:
    """Get Spotify access token for public search (Client Credentials)."""
    global _app_spotify_token, _app_spotify_token_expires

    if _app_spotify_token and time.time() < _app_spotify_token_expires:
        return _app_spotify_token

    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {auth_b64}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        ) as resp:
            data = await resp.json()
            _app_spotify_token = data["access_token"]
            _app_spotify_token_expires = time.time() + data.get("expires_in", 3600) - 60
            return _app_spotify_token


async def search_spotify(query: str, exclude_songs: list[str] | None = None) -> dict | None:
    """Search Spotify for a track. Returns {url, title, artist, thumbnail} or None."""
    token = await get_app_spotify_token()
    if not token:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": query, "type": "track", "limit": 5, "market": "IN"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"Error querying Spotify search: {e}")
        return None

    tracks = data.get("tracks", {}).get("items", [])
    if not tracks:
        return None

    exclude_lower = [s.lower() for s in (exclude_songs or [])]
    chosen_track = None

    if exclude_lower:
        for t in tracks:
            t_name = t["name"].lower()
            t_artists = " ".join(a["name"].lower() for a in t.get("artists", []))
            combined = f"{t_name} by {t_artists}"
            if not any(ex in combined or ex in t_name or t_name in ex for ex in exclude_lower):
                chosen_track = t
                break

    if not chosen_track:
        chosen_track = tracks[0]

    artists = ", ".join(a["name"] for a in chosen_track.get("artists", []))
    thumbnail = ""
    if chosen_track.get("album", {}).get("images"):
        thumbnail = chosen_track["album"]["images"][0]["url"]

    return {
        "url": chosen_track["external_urls"]["spotify"],
        "title": chosen_track["name"],
        "artist": artists,
        "thumbnail": thumbnail,
    }


async def get_user_top_music(discord_user_id: int) -> dict | None:
    """Fetch user's top artists, top tracks, and musical genres from Spotify."""
    token = await get_valid_user_token(discord_user_id)
    if not token:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}"}

            # Fetch top artists and extract their genres
            artists = []
            genres = []
            async with session.get(
                "https://api.spotify.com/v1/me/top/artists",
                headers=headers,
                params={"limit": 8, "time_range": "medium_term"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for a in data.get("items", []):
                        artists.append(a["name"])
                        genres.extend(a.get("genres", []))

            # Deduplicate genres (take top 6 most common)
            top_genres = list(dict.fromkeys(genres))[:6]

            # Fetch top tracks
            tracks = []
            async with session.get(
                "https://api.spotify.com/v1/me/top/tracks",
                headers=headers,
                params={"limit": 8, "time_range": "medium_term"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for t in data.get("items", []):
                        track_artists = ", ".join(a["name"] for a in t.get("artists", []))
                        tracks.append(f"{t['name']} by {track_artists}")

            if not artists and not tracks:
                return None

            return {
                "top_artists": artists,
                "top_tracks": tracks,
                "top_genres": top_genres,
            }
    except Exception as e:
        print(f"Error fetching user music for {discord_user_id}: {e}")
        return None


# ==========================================
# 3. Gemini AI Recommendation Engine
# ==========================================
GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
]


async def ask_ai_for_song(
    display_name: str,
    username: str,
    user_music: dict | None = None,
    recent_songs: list[str] | None = None,
) -> dict:
    """Ask Gemini AI to deeply analyze Spotify history & taste to pick a tailored song."""
    recent_str = "; ".join(recent_songs[-15:]) if recent_songs else "None"

    if user_music and (user_music.get("top_artists") or user_music.get("top_tracks")):
        artists_str = ", ".join(user_music.get("top_artists", [])) or "None listed"
        tracks_str = "; ".join(user_music.get("top_tracks", [])) or "None listed"
        genres_str = ", ".join(user_music.get("top_genres", [])) or "Diverse genres"

        prompt = f"""You are Junky, a music expert and witty Discord bot.
You are generating a personalized song recommendation for {display_name} (@{username}) based on their REAL Spotify listening history:

User's Spotify History:
- Favorite Genres: {genres_str}
- Top Artists: {artists_str}
- Most Played Tracks: {tracks_str}
- Recently Recommended Tracks (DO NOT REPEAT): {recent_str}

Your Mission:
1. Deeply analyze their music taste, favorite genres, and musical vibes from the list above.
2. Recommend a FRESH, fantastic song that perfectly fits their musical taste (like an artist they'd love, a related banger, or a hidden gem).
3. CRITICAL ANTI-REPETITION RULES:
   - Do NOT pick any song already listed in their Top Tracks!
   - Do NOT pick any song or artist listed in Recently Recommended Tracks (strictly forbidden)!
   - Explore fresh variety: try different related artists, underground gems, or adjacent subgenres that match the aesthetic.
4. Write a witty, clever 10-15 word comment connecting the song to their favorite artists/genres.

Respond ONLY with valid JSON:
{{"search_query": "Song Title Artist Name", "message": "your witty commentary here"}}"""
    else:
        prompt = f"""You are a witty Discord music bot. A user tagged {display_name} (@{username}).
Pick a funny, ironic, or fitting song for this person based on their name, personality vibe, or clever wordplay.

Recently Recommended Songs for this user (DO NOT REPEAT): {recent_str}

Rules:
- Pick a REAL song that exists on Spotify
- Avoid generic songs with the user's name as the title — explore diverse genres (Bollywood, Punjabi, Hip-Hop, Pop, Rock, Memes, Anime).
- CRITICAL: Do NOT repeat any song from the Recently Recommended list above! Pick something completely new and surprising.
- Write a short witty one-liner (max 15 words) explaining why this song fits them

Respond ONLY with valid JSON:
{{"search_query": "Song Title Artist Name", "message": "your witty one-liner here"}}"""

    for model_name in GEMINI_MODELS:
        try:
            response = await gemini_client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=1.15,
                    response_mime_type="application/json",
                ),
            )
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except Exception as e:
            print(f"Gemini error on model '{model_name}': {e}")
            continue

    print("All Gemini models failed. Falling back to default search.")
    return {
        "search_query": display_name,
        "message": "AI took a nap, but here's what Spotify found! 🤷",
    }


# ==========================================
# 4. OAuth Local Web Server (aiohttp)
# ==========================================
async def handle_spotify_callback(request: web.Request) -> web.Response:
    """Handle the OAuth callback from Spotify."""
    code = request.query.get("code")
    discord_user_id = request.query.get("state")
    error = request.query.get("error")

    if error or not code or not discord_user_id:
        html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Connection Failed</title></head>
        <body style="background:#121212;color:#fff;font-family:sans-serif;text-align:center;padding:50px;">
            <h1 style="color:#e22134;">❌ Connection Failed</h1>
            <p>Error: {error or 'Missing authorization code or user ID.'}</p>
            <p>Please try the <code>/connect</code> command in Discord again.</p>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html", status=400)

    # Exchange authorization code for user access token & refresh token
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={
                    "Authorization": f"Basic {auth_b64}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": SPOTIFY_REDIRECT_URI,
                },
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    return web.Response(text=f"Token exchange failed: {err_text}", status=400)
                token_data = await resp.json()

        # Save tokens
        tokens = load_user_tokens()
        tokens[str(discord_user_id)] = {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", ""),
            "expires_at": time.time() + token_data.get("expires_in", 3600),
        }
        save_user_tokens(tokens)

        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Spotify Connected!</title></head>
        <body style="background:#121212;color:#fff;font-family:sans-serif;text-align:center;padding:50px;">
            <h1 style="color:#1DB954;">🎉 Spotify Connected!</h1>
            <p>Your Spotify account has been successfully linked to Junky bot.</p>
            <p>You can close this tab now and try <code>/song</code> on Discord!</p>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        print(f"Error during Spotify callback: {e}")
        return web.Response(text=f"Internal error: {e}", status=500)


# ==========================================
# 5. Discord Bot & Slash Commands
# ==========================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        # Copy global commands to each server the bot is in for INSTANT Discord command updates
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            synced_guild = await bot.tree.sync(guild=guild)
            print(f"✅ Instantly synced {len(synced_guild)} commands to server: {guild.name}")
        
        # Also sync globally
        synced = await bot.tree.sync()
        print(f"✅ Globally synced {len(synced)} slash commands!")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")


@bot.tree.command(name="sync", description="Sync slash commands (bot owner only)")
async def sync_commands(interaction: discord.Interaction):
    """Run /sync once after adding/changing commands. Not needed every restart."""
    if interaction.user.id != (await bot.application_info()).owner.id:
        await interaction.response.send_message("Only the bot owner can sync.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    synced = await bot.tree.sync()
    await interaction.followup.send(f"Synced {len(synced)} commands.", ephemeral=True)


@bot.tree.command(name="connect", description="Connect your Spotify account for personalized AI music suggestions")
async def connect(interaction: discord.Interaction):
    """Send an ephemeral link for user to authorize their Spotify account."""
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": "user-top-read",
        "state": str(interaction.user.id),
        "show_dialog": "true",
    }
    auth_url = f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"

    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="🔗 Authorize Spotify",
        style=discord.ButtonStyle.link,
        url=auth_url,
        emoji="🎵",
    ))

    embed = discord.Embed(
        title="Connect your Spotify",
        description=(
            "Click the button below to connect your Spotify account.\n"
            "This lets **Junky** read your top artists & tracks to give you personalized song suggestions!"
        ),
        color=SPOTIFY_GREEN,
    )
    embed.set_footer(text="🔒 Only you can see this message.")

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="disconnect", description="Disconnect your Spotify account from the bot")
async def disconnect(interaction: discord.Interaction):
    """Disconnect user's Spotify account."""
    tokens = load_user_tokens()
    user_str = str(interaction.user.id)

    if user_str in tokens:
        del tokens[user_str]
        save_user_tokens(tokens)
        await interaction.response.send_message(
            "✅ Your Spotify account has been disconnected.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "You don't have a Spotify account connected.", ephemeral=True
        )


@bot.tree.command(name="song", description="AI picks a personalized song for the tagged user")
@app_commands.describe(user="The person to get music for")
async def song(interaction: discord.Interaction, user: discord.Member):
    # Defer immediately — AI + Spotify API calls take a moment
    await interaction.response.defer()

    try:
        # Step 1: Check if tagged user has connected their Spotify
        user_music = await get_user_top_music(user.id)
        is_personalized = user_music is not None
        recent_songs = get_user_recent_songs(user.id)

        # Step 2: Ask Gemini AI to pick a song
        ai_result = await ask_ai_for_song(
            display_name=user.display_name,
            username=user.name,
            user_music=user_music,
            recent_songs=recent_songs,
        )

        search_query = ai_result.get("search_query", user.display_name)
        ai_message = ai_result.get("message", "")

        # Step 3: Search Spotify for the AI's recommended song
        track = await search_spotify(search_query, exclude_songs=recent_songs)

        if not track:
            await interaction.followup.send(
                f"🤖 AI picked \"{search_query}\" for {user.mention} but couldn't find it on Spotify! 😅",
            )
            return

        # Save to user's recent recommendation history
        add_user_recent_song(user.id, f"{track['title']} by {track['artist']}")

        # Step 4: Build the embed
        footer_text = (
            "✨ Personalized based on Spotify taste • Spotify"
            if is_personalized
            else "🤖 Based on username (use /connect for personalized) • Spotify"
        )

        embed = discord.Embed(
            title=f"{track['title']} — {track['artist']}",
            url=track["url"],
            description=f"*{ai_message}*" if ai_message else f"For {user.mention}",
            color=SPOTIFY_GREEN,
        )
        if track.get("thumbnail"):
            embed.set_thumbnail(url=track["thumbnail"])
        embed.set_footer(
            text=footer_text,
            icon_url="https://storage.googleapis.com/pr-newsroom-wp/1/2023/05/Spotify_Primary_Logo_RGB_Green.png",
        )

        # Add a clickable "Play on Spotify" button
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="▶ Play on Spotify",
            style=discord.ButtonStyle.link,
            url=track["url"],
            emoji="🎧",
        ))

        await interaction.followup.send(
            content=f"🎵 For {user.mention}:",
            embed=embed,
            view=view,
        )

    except Exception as e:
        print(f"Error in /song: {e}")
        await interaction.followup.send(
            f"Something went wrong picking a song for {user.mention}! 😅 Try again.",
        )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    """Gracefully handle interaction errors."""
    if isinstance(error.__cause__, discord.NotFound):
        print(f"Interaction expired for /{interaction.command.name} — Discord took too long.")
    else:
        print(f"Error in /{interaction.command.name}: {error}")


async def handle_home(request: web.Request) -> web.Response:
    """Friendly landing page if user visits http://127.0.0.1:8888 directly."""
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Junky Bot - Spotify Server</title></head>
    <body style="background:#121212;color:#fff;font-family:sans-serif;text-align:center;padding:50px;">
        <h1 style="color:#1DB954;">🎵 Junky Music Bot Server</h1>
        <p>This server handles Spotify account authorization.</p>
        <p>To connect your Spotify, go to Discord and use the <code>/connect</code> command!</p>
    </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")


async def main():
    # Setup OAuth Callback Web Server
    app = web.Application()
    app.router.add_get("/", handle_home)
    app.router.add_get("/callback", handle_spotify_callback)

    port = int(os.getenv("PORT", 8888))
    host = "0.0.0.0"

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Spotify OAuth server running on http://{host}:{port}")

    # Start Discord Bot
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
