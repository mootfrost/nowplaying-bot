import asyncio
import functools
import io
import aiohttp
from telethon import TelegramClient, events, Button
from telethon.tl.types import (
    UpdateBotInlineSend,
    TypeInputFile,
    InputFile,
    DocumentAttributeAudio,
    InputPeerSelf, InputDocument
)
from telethon import functions
from telethon.utils import get_input_document
import urllib.parse
from mutagen.id3 import ID3, APIC
import logging
from cachetools import LRUCache


from app.MusicProvider import MusicProviderContext, SpotifyStrategy
from app.config import config
from app.dependencies import get_session, get_session_context
from app.models import Track
from app.youtube_api import name_to_youtube, download_youtube

logging.basicConfig(
    format='[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = TelegramClient('nowplaying', config.api_id, config.api_hash)
client.parse_mode = 'html'
dummy_file: TypeInputFile = None
cache = LRUCache(maxsize=100)


def get_link_account_keyboard():
    return [Button.inline('Spotify', 'connect_spotify')]


@client.on(events.NewMessage(pattern='/start'))
async def start(e: events.NewMessage.Event):
    await e.respond("Hello! I'm a bot that lets you download music you listen in inline mode. Press button below to connect your account.",
                    buttons=get_link_account_keyboard())


@client.on(events.CallbackQuery(pattern='connect_spotify'))
async def connect_spotify(e: events.CallbackQuery.Event):
    params = {
        'client_id': config.spotify.client_id,
        'response_type': 'code',
        'redirect_uri': config.spotify.redirect,
        'scope': 'user-read-recently-played',
        'state': f'tg_{e.sender_id}'
    }
    await e.respond('Link your Spotify account',
                    buttons=[Button.url('Link', f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}")])


async def fetch_file(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()


async def update_dummy_file():
    global dummy_file
    dummy_file = await client.upload_file('empty.mp3')


def get_track_links(track_id):
    return f'<a href="https://open.spotify.com/track/{track_id}">Spotify</a> | <a href="https://song.link/s/{track_id}">Other</a>'


# TODO: make faster and somehow fix cover not displaying in response
async def update_dummy_file_cover(cover_url: str):
    cover = await fetch_file(cover_url)
    dummy_name = 'empty.mp3'
    audio = ID3(dummy_name)
    audio.delall('APIC')
    audio.add(APIC(
        encoding=3,
        mime='image/jpeg',
        type=3,
        desc='Cover',
        data=cover
    ))
    res = io.BytesIO()
    audio.save(res)
    global dummy_file
    dummy_file = await client.upload_file(res.getvalue(), file_name='empty.mp3')


async def build_response(e: events.InlineQuery.Event, track: Track):
    if not track.telegram_id:
        await update_dummy_file()
        buttons = [Button.inline('Loading', 'loading')]
    else:
        global dummy_file
        dummy_file = InputDocument(
            id=track.telegram_id,
            access_hash=track.telegram_access_hash,
            file_reference=track.telegram_file_reference
        )
        buttons = None
    return e.builder.document(
        file=dummy_file,
        title=track.name,
        description=track.artist,
        id=track.spotify_id,
        mime_type='audio/mpeg',
        attributes=[
            DocumentAttributeAudio(
                duration=1,
                voice=False,
                title=track.name,
                performer=track.artist,
                waveform=None,
            )
        ],
        text=get_track_links(track.spotify_id),
        buttons=buttons
    )


@client.on(events.InlineQuery())
async def query_list(e: events.InlineQuery.Event):
    context = MusicProviderContext(SpotifyStrategy(e.sender_id))
    tracks = await context.get_tracks()
    result = []

    for track in tracks:
        track = await context.get_cached_track(track)
        cache[track.spotify_id] = track
        result.append(await build_response(e, track))
    await e.answer(result)


async def get_track_file(track):
    yt_id = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(name_to_youtube, f'{track.name} - {track.artist}')
    )
    audio, duration = await download_youtube(yt_id)
    _, media, _ = await client._file_to_media(
        audio,
        attributes=[
            DocumentAttributeAudio(
                duration=duration,
                voice=False,
                title=track.name,
                performer=track.artist,
                waveform=None,
            )]
    )
    uploaded_media = await client(
        functions.messages.UploadMediaRequest(
            InputPeerSelf(), media=media
        )
    )

    return get_input_document(uploaded_media.document)


async def cache_file(track):
    async with get_session_context() as session:
        session.add(track)
        await session.commit()


@client.on(events.Raw([UpdateBotInlineSend]))
async def send_track(e: UpdateBotInlineSend):
    track = cache[e.id]
    if track.telegram_id:
        return

    file = await get_track_file(track)
    track.telegram_id = file.id
    track.telegram_access_hash = file.access_hash
    track.telegram_file_reference = file.file_reference
    await cache_file(track)
    await client.edit_message(e.msg_id, file=file, text=get_track_links(e.id))


async def main():
    await client.start(bot_token=config.bot_token)
    await update_dummy_file()
    logger.info('Bot started')
    await client.run_until_disconnected()


__all__ = ['main']