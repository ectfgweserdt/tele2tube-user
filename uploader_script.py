import os
import sys
import argparse
import time
import asyncio
import subprocess
import json
import base64
import re
import requests
from telethon import TelegramClient, errors
from telethon.sessions import StringSession 
from telethon.tl.types import MessageMediaDocument
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import googleapiclient.errors

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025"

# Fetching API Keys
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
OMDB_API_KEY = os.environ.get('OMDB_API_KEY', '').strip() 

def run_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()
    return output.decode(), error.decode(), process.returncode

def download_progress_callback(current, total):
    print(f"⏳ Telegram Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.2f}%)", end='\r', flush=True)

def parse_filename(filename):
    """Extracts title, season, and episode for better IMDb searching."""
    clean_name = os.path.splitext(filename)[0].replace('_', ' ').replace('.', ' ')
    match = re.search(r'S(\d+)E(\d+)', clean_name, re.IGNORECASE)
    season, episode = None, None
    if match:
        season = match.group(1)
        episode = match.group(2)
        search_title = clean_name[:match.start()].strip()
    else:
        tags = [r'\d{3,4}p', 'HD', 'NF', 'WEB-DL', 'Dual Audio', 'ES', 'x264', 'x265', 'HEVC']
        search_title = clean_name
        for tag in tags:
            search_title = re.sub(tag, '', search_title, flags=re.IGNORECASE)
        search_title = ' '.join(search_title.split()).strip()
    return search_title, season, episode

async def get_metadata(filename):
    search_title, season, episode = parse_filename(filename)
    print(f"\n🔍 Searching IMDb for: {search_title} " + (f"(S{season}E{episode})" if season else ""))
    omdb_data = None
    if OMDB_API_KEY:
        try:
            url = f"http://www.omdbapi.com/?t={search_title}&apikey={OMDB_API_KEY}"
            if season: url += f"&Season={season}&Episode={episode}"
            res = requests.get(url, timeout=10)
            data = res.json()
            if data.get("Response") == "True":
                print(f"✅ IMDb Match: {data['Title']}")
                omdb_data = data
        except: pass

    if GEMINI_API_KEY:
        print("🤖 AI is formatting neat description...")
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        prompt = (
            f"Context: Filename is '{filename}'. IMDb Data: {json.dumps(omdb_data) if omdb_data else 'None found'}.\n\n"
            "Task: Create NEAT YouTube metadata. Return JSON with keys: 'title', 'description', 'tags'.\n"
            "1. TITLE: Formal (e.g. 'Show - S01E01 - Title')\n"
            "2. DESCRIPTION: Sections with emojis (Synopsis, Cast, Details).\n"
            "3. TAGS: 10 comma-separated keywords."
        )
        try:
            payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}}
            res = requests.post(gemini_url, json=payload, timeout=30)
            if res.status_code == 200:
                return json.loads(res.json()['candidates'][0]['content']['parts'][0]['text'])
        except: pass
    return {"title": search_title, "description": "High quality upload.", "tags": "video"}

def generate_thumbnail_from_video(video_path):
    print("🖼️ Extracting thumbnail...")
    output_thumb = "thumbnail.jpg"
    try:
        duration_cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 '{video_path}'"
        duration_out, _, _ = run_command(duration_cmd)
        seek_time = float(duration_out.strip()) / 4 if duration_out.strip() else 15
        run_command(f"ffmpeg -ss {seek_time} -i '{video_path}' -vframes 1 -q:v 2 -y {output_thumb}")
        return output_thumb if os.path.exists(output_thumb) else None
    except: return None

def process_video(input_path):
    output_path = "processed_video.mp4"
    print(f"🔍 Optimizing video & audio...")
    cmd_ffmpeg = (
        f"ffmpeg -i '{input_path}' -map 0:v:0 -map 0:a:m:language:eng? -map 0:a:0? "
        f"-c:v copy -c:a aac -b:a 192k -y '{output_path}'"
    )
    _, _, code = run_command(cmd_ffmpeg)
    return output_path if code == 0 and os.path.exists(output_path) else input_path

def upload_to_youtube(video_path, metadata, thumb_path):
    try:
        creds = Credentials(
            token=None, refresh_token=os.environ.get('YOUTUBE_REFRESH_TOKEN'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.environ.get('YOUTUBE_CLIENT_ID'),
            client_secret=os.environ.get('YOUTUBE_CLIENT_SECRET'),
            scopes=YOUTUBE_SCOPES
        )
        creds.refresh(Request())
        youtube = build('youtube', 'v3', credentials=creds)
        body = {
            'snippet': {
                'title': metadata.get('title', 'Video')[:95],
                'description': metadata.get('description', ''),
                'tags': metadata.get('tags', '').split(','),
                'categoryId': '24'
            },
            'status': {'privacyStatus': 'private'}
        }
        print(f"🚀 Uploading: {body['snippet']['title']}")
        media = MediaFileUpload(video_path, chunksize=1024*1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status: print(f"Uploaded {int(status.progress() * 100)}%")
        
        if thumb_path:
            # Shortened delay to reduce process time
            time.sleep(5)
            try: youtube.thumbnails().set(videoId=response['id'], media_body=MediaFileUpload(thumb_path)).execute()
            except: pass
        print(f"🎉 SUCCESS! https://youtu.be/{response['id']}")
        return True
    except googleapiclient.errors.HttpError as e:
        error_details = e.content.decode()
        # Handle both global quota and per-user daily limits
        if "uploadLimitExceeded" in error_details or "quotaExceeded" in error_details:
            print("\n❌ API LIMIT REACHED!")
            print("YouTube allows ~6 uploads per day via API for free projects.")
            print("The limit will reset in 24 hours.")
            return "LIMIT_REACHED"
        print(f"🔴 YouTube Error: {e}")
        return False
    except Exception as e:
        print(f"🔴 Error: {e}")
        return False

async def process_single_link(client, link):
    try:
        print(f"\n--- Processing: {link} ---")
        parts = [p for p in link.strip('/').split('/') if p]
        msg_id, chat_id = int(parts[-1]), int(f"-100{parts[parts.index('c')+1]}")
    except: return True

    message = await client.get_messages(chat_id, ids=msg_id)
    if not message or not message.media: return True

    raw_file = f"download_{msg_id}" + (message.file.ext if hasattr(message, 'file') else ".mp4")
    print(f"⬇️ Downloading...")
    await client.download_media(message, raw_file, progress_callback=download_progress_callback)
    
    metadata = await get_metadata(message.file.name or raw_file)
    final_video = process_video(raw_file)
    thumb = generate_thumbnail_from_video(final_video)
    
    status = upload_to_youtube(final_video, metadata, thumb)

    for f in [raw_file, "processed_video.mp4", "thumbnail.jpg"]:
        if os.path.exists(f): os.remove(f)
    return status

async def run_flow(links_str):
    links = [l.strip() for l in links_str.split(',') if l.strip()]
    client = TelegramClient(
        StringSession(os.environ['TG_SESSION_STRING']), 
        os.environ['TG_API_ID'], 
        os.environ['TG_API_HASH'],
        connection_retries=None,
        retry_delay=5
    )
    await client.start()
    for link in links:
        result = await process_single_link(client, link)
        if result == "LIMIT_REACHED":
            break
    await client.disconnect()

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
