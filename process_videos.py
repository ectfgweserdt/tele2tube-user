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

# Strict Translation Dictionary for Physics
# This ensures even if AI fails, the core unit is translated.
TRANSLATION_DICT = {
    "තාපය": "Heat",
    "යාන්ත්‍ර": "Mechanics",
    "ආලෝකය": "Light",
    "තරංග": "Waves",
    "විද්‍යුත්": "Electricity",
    "පදාර්ථ": "Properties of Matter",
    "ගුණ": "Properties",
    "ස්ථිති": "Electrostatics",
    "චුම්භක": "Magnetic",
    "නවීන": "Modern Physics"
}

# --- UI Helpers ---

def log_header(text):
    print(f"\n{'='*60}\n{text.center(60)}\n{'='*60}")

def log_status(step, status):
    print(f"[{step.upper():<12}] {status}")

def has_sinhala(text):
    """Detects if string contains Sinhala characters."""
    return bool(re.search(r'[\u0D80-\u0DFF]', text))

def sanitize_title(text):
    """Manually translates keywords if AI leaves Sinhala text."""
    temp_text = text
    for sin, eng in TRANSLATION_DICT.items():
        temp_text = re.sub(sin, eng, temp_text, flags=re.IGNORECASE)
    # Remove emojis and markdown junk
    temp_text = re.sub(r'[^\x00-\x7F]+', '', temp_text).strip()
    return temp_text if temp_text else "Physics Lesson"

# --- AI Logic ---

async def analyze_with_ai(text_content):
    if not GH_TOKEN or not text_content:
        return {"title": "Physics Lesson", "category": "General Physics"}
        
    endpoint = "https://models.inference.ai.azure.com/chat/completions"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
    
    # Highly specific prompt
    prompt = f"""
    Translate the following Sinhala tuition text into a professional English YouTube Title and identify the Physics Unit.
    
    Text: "{text_content}"
    
    Rules:
    1. The 'title' MUST be in English ONLY.
    2. 'category' MUST be the Physics Unit name (e.g., Heat, Mechanics, Waves).
    3. If the unit is unknown, use 'General Physics'.
    4. Respond ONLY with valid JSON.
    """
    
    try:
        response = requests.post(endpoint, headers=headers, json={
            "messages": [
                {"role": "system", "content": "You are a translation bot that only outputs valid JSON. No conversational text."},
                {"role": "user", "content": prompt}
            ],
            "model": "gpt-4o-mini",
            "temperature": 0.0 # Strictness set to maximum
        }, timeout=25)
        
        raw_content = response.json()['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        result = json.loads(match.group()) if match else json.loads(raw_content)
        
        # Double-check if the AI ignored the 'English ONLY' rule
        if has_sinhala(result['title']):
            log_status("AI WARN", "AI returned Sinhala. Forcing manual translation.")
            result['title'] = sanitize_title(result['title'])
            
        return result
    except Exception as e:
        log_status("AI FAIL", f"Using dictionary fallback: {e}")
        # Manual fallback
        category = "General Physics"
        for sin, eng in TRANSLATION_DICT.items():
            if sin in text_content:
                category = eng
                break
        return {"title": sanitize_title(text_content), "category": category}

# --- YouTube Logic ---

def get_or_create_playlist(youtube, title):
    try:
        request = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
        response = request.execute()
        for item in response.get('items', []):
            if item['snippet']['title'].lower() == title.lower():
                return item['id']
        
        log_status("PLAYLIST", f"Creating: {title}")
        res = youtube.playlists().insert(part="snippet,status", body={
            "snippet": {"title": title, "description": "Physics tuition videos."},
            "status": {"privacyStatus": "private"}
        }).execute()
        return res['id']
    except Exception as e:
        log_status("PL ERROR", f"Could not create playlist: {e}")
        return None

# --- Main App ---

async def resolve_telegram_message(client, link):
    try:
        clean_link = link.strip().split('t.me/')[1]
        parts = [p for p in clean_link.split('/') if p]
        msg_id = int(parts[-1])
        target = parts[0]
        
        if target == 'c':
            chat_id = int(f"-100{parts[1]}")
            try:
                entity = await client.get_entity(chat_id)
            except:
                await client.get_dialogs()
                entity = await client.get_entity(chat_id)
        else:
            entity = await client.get_entity(target)
            
        message = await client.get_messages(entity, ids=msg_id)
        return message, entity
    except: return None, None

async def main():
    log_header("TELE2TUBE: TUITION VIDEO PROCESSOR")
    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    await client.connect()
    await client.get_dialogs()
    
    creds = Credentials(None, refresh_token=YOUTUBE_REFRESH_TOKEN, 
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET)
    youtube = build('youtube', 'v3', credentials=creds)

    for i, link in enumerate(VIDEO_LINKS, 1):
        log_header(f"ITEM {i} OF {len(VIDEO_LINKS)}")
        message, _ = await resolve_telegram_message(client, link)
        
        if not message or not message.media:
            log_status("SKIP", "No media found.")
            continue

        log_status("PROCESS", "Analyzing & Translating...")
        text = message.text or message.caption or ""
        metadata = await analyze_with_ai(text)
        
        # FINAL LOGGING FOR USER
        print(f"  > AI TITLE:    {metadata['title']}")
        print(f"  > AI UNIT:     {metadata['category']}")

        if not os.path.exists("downloads"): os.makedirs("downloads")
        path = await client.download_media(message, file="downloads/")
        
        if path:
            log_status("UPLOAD", f"Uploading video to YouTube...")
            body = {
                'snippet': {'title': metadata['title'], 'description': text, 'categoryId': '27'},
                'status': {'privacyStatus': 'private'}
            }
            media = MediaFileUpload(path, chunksize=10*1024*1024, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"\r[UPLOAD] {int(status.progress() * 100)}%", end="")
            
            video_id = response['id']
            print(f"\n[UPLOAD] Success: {video_id}")
            
            # Playlist Logic
            pid = get_or_create_playlist(youtube, metadata['category'])
            if pid:
                youtube.playlistItems().insert(part="snippet", body={
                    "snippet": {
                        "playlistId": pid,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}
                    }
                }).execute()
                log_status("PLAYLIST", f"Added to {metadata['category']}")
            
            os.remove(path)

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
