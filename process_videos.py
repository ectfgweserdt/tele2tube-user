import os
import json
import asyncio
import time
import re
import requests
from google import genai
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Configuration ---
GH_TOKEN = os.environ.get('GH_MODELS_TOKEN', '') 

TG_API_ID = os.environ.get('TG_API_ID')
TG_API_HASH = os.environ.get('TG_API_HASH')
TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')

YOUTUBE_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

VIDEO_LINKS = [l.strip() for l in os.environ.get('VIDEO_LINKS', '').split(',') if l.strip()]

# Fail-safe mapping for Physics Units
UNIT_MAPPING = {
    "තාපය": "Heat",
    "යාන්ත්‍ර": "Mechanics",
    "ආලෝකය": "Light",
    "තරංග": "Waves",
    "විද්‍යුත්": "Electricity",
    "පදාර්ථ": "Properties of Matter"
}

# --- UI Helpers ---

def log_header(text):
    print(f"\n{'='*60}\n{text.center(60)}\n{'='*60}")

def log_status(step, status):
    print(f"[{step.upper():<12}] {status}")

def format_size(bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unit}"
        bytes /= 1024.0

# --- AI & Translation Logic ---

async def analyze_with_ai(text_content):
    if not GH_TOKEN or not text_content:
        return {"title": text_content or "Untitled", "category": "General"}
        
    endpoint = "https://models.inference.ai.azure.com/chat/completions"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
    
    prompt = f"""
    Analyze this Physics tuition post: '{text_content}'
    1. Translate Sinhala words to English (e.g. 'තාපය' to 'Heat').
    2. Provide a professional English title.
    3. Identify the Physics Unit (Heat, Mechanics, etc).
    
    Return ONLY a JSON object: {{"title":"English Title", "category":"Unit Name"}}
    """
    
    try:
        response = requests.post(endpoint, headers=headers, json={
            "messages": [{"role": "user", "content": prompt}],
            "model": "gpt-4o-mini",
            "temperature": 0.1
        }, timeout=30)
        
        data = response.json()
        raw_content = data['choices'][0]['message']['content']
        
        # Robust JSON extraction
        match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        result = json.loads(match.group()) if match else json.loads(raw_content)
        
        # Fail-safe: If title still contains Sinhala, use manual mapping
        for sin, eng in UNIT_MAPPING.items():
            if sin in result['title']:
                result['title'] = result['title'].replace(sin, eng)
                if result['category'] == 'General':
                    result['category'] = eng
                    
        return result
    except Exception as e:
        log_status("AI ERROR", f"Falling back to mapping: {e}")
        # Manual fallback logic
        cat = "General"
        title = text_content.replace('*', '').strip()
        for sin, eng in UNIT_MAPPING.items():
            if sin in text_content:
                cat = eng
                title = title.replace(sin, eng)
                break
        return {"title": title, "category": cat}

# --- YouTube Helpers ---

def get_or_create_playlist(youtube, title):
    try:
        request = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
        response = request.execute()
        for item in response.get('items', []):
            if item['snippet']['title'].lower() == title.lower():
                return item['id']
        
        log_status("PLAYLIST", f"Creating new playlist: {title}")
        res = youtube.playlists().insert(part="snippet,status", body={
            "snippet": {"title": title, "description": "Automatically categorized tuition videos."},
            "status": {"privacyStatus": "private"}
        }).execute()
        return res['id']
    except Exception as e:
        log_status("PL ERROR", str(e))
        return None

# --- Telegram Link Resolver ---

async def resolve_telegram_message(client, link):
    try:
        clean_link = link.strip().replace('https://t.me/', '').replace('http://t.me/', '')
        parts = [p for p in clean_link.split('/') if p]
        
        msg_id = int(parts[-1])
        target_entity = parts[0]

        if target_entity == 'c':
            raw_id = parts[1]
            chat_id = int(f"-100{raw_id}")
            try:
                entity = await client.get_entity(chat_id)
            except ValueError:
                log_status("TELEGRAM", f"Chat {chat_id} not in cache. Refreshing...")
                await client.get_dialogs()
                entity = await client.get_entity(chat_id)
        else:
            entity = await client.get_entity(target_entity)

        message = await client.get_messages(entity, ids=msg_id)
        return message, entity
    except Exception as e:
        log_status("RESOLVE ERR", f"Failed for {link}: {e}")
        return None, None

# --- Progress Callbacks ---

def download_progress(current, total):
    percent = (current / total) * 100
    print(f"\r[DOWNLOAD    ] Progress: {percent:>5.1f}% | {format_size(current)} / {format_size(total)}", end="")

# --- Main Logic ---

async def main():
    log_header("TELE2TUBE: TUITION VIDEO PROCESSOR")
    
    if not VIDEO_LINKS:
        log_status("ERROR", "No video links provided.")
        return

    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    log_status("TELEGRAM", "Connecting...")
    await client.connect()
    
    log_status("CACHE", "Refreshing dialogs...")
    await client.get_dialogs()
    
    log_status("YOUTUBE", "Initializing API...")
    creds = Credentials(None, refresh_token=YOUTUBE_REFRESH_TOKEN, 
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET)
    youtube = build('youtube', 'v3', credentials=creds)

    for i, link in enumerate(VIDEO_LINKS, 1):
        log_header(f"ITEM {i} OF {len(VIDEO_LINKS)}")
        log_status("LINK", link)
        
        message, entity = await resolve_telegram_message(client, link)
        
        if not message or not message.media:
            log_status("SKIP", "No media found.")
            continue

        # 1. AI Analysis
        log_status("AI", "Translating and Categorizing...")
        text = message.text or message.caption or "Untitled Video"
        metadata = await analyze_with_ai(text)
        
        print(f"\n  > FINAL TITLE: {metadata['title']}")
        print(f"  > PLAYLIST:    {metadata['category']}\n")

        # 2. Download
        if not os.path.exists("downloads"): os.makedirs("downloads")
        path = await client.download_media(message, file="downloads/", progress_callback=download_progress)
        print() 
        
        if not path: continue

        # 3. Upload to YouTube
        log_status("UPLOAD", f"Uploading {format_size(os.path.getsize(path))}...")
        try:
            request_body = {
                'snippet': {
                    'title': metadata['title'],
                    'description': f"Original Text:\n{text}",
                    'categoryId': '27'
                },
                'status': {'privacyStatus': 'private'}
            }
            
            media = MediaFileUpload(path, chunksize=10*1024*1024, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=request_body, media_body=media)
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"\r[UPLOAD      ] Progress: {int(status.progress() * 100):>3}% uploaded...", end="")
            
            video_id = response['id']
            print(f"\n[UPLOAD      ] SUCCESS! ID: {video_id}")
            
            # 4. Playlist Assignment
            playlist_id = get_or_create_playlist(youtube, metadata['category'])
            if playlist_id:
                youtube.playlistItems().insert(part="snippet", body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}
                    }
                }).execute()
                log_status("PLAYLIST", f"Added to '{metadata['category']}'")
            
            # 5. Cleanup
            os.remove(path)
            
        except Exception as e:
            log_status("YT ERROR", f"Upload failed: {e}")

    log_header("ALL PROCESSES COMPLETE")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
