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
    return bool(re.search(r'[\u0D80-\u0DFF]', text))

def sanitize_title(text):
    """Aggressively translates and cleans text."""
    temp_text = text
    found_unit = "General Physics"
    
    for sin, eng in TRANSLATION_DICT.items():
        if sin in temp_text:
            temp_text = temp_text.replace(sin, eng)
            found_unit = eng
            
    # Remove Sinhala characters if any remain
    temp_text = re.sub(r'[\u0D80-\u0DFF]+', '', temp_text)
    # Remove markdown stars and extra spaces
    temp_text = temp_text.replace('*', '').strip()
    
    return temp_text if len(temp_text) > 3 else "Physics Lesson", found_unit

# --- AI Logic ---

async def analyze_with_ai(text_content):
    if not GH_TOKEN or not text_content or len(text_content.strip()) < 2:
        log_status("AI", "No input text provided to AI.")
        return {"title": "Physics Lesson", "category": "General Physics"}
        
    endpoint = "https://models.inference.ai.azure.com/chat/completions"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
    
    prompt = f"""
    Translate this Sinhala Physics tuition caption into a professional English YouTube Title.
    Identify the Physics Unit (e.g., Heat, Mechanics, Waves).
    
    TEXT TO ANALYZE: "{text_content}"
    
    RULES:
    - Respond ONLY with JSON.
    - Title must be English only.
    - If you see 'තාපය', use 'Heat'.
    - Format: {{"title": "English Title", "category": "Unit Name"}}
    """
    
    try:
        response = requests.post(endpoint, headers=headers, json={
            "messages": [
                {"role": "system", "content": "You are a specialized Physics translator. You output valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            "model": "gpt-4o-mini",
            "temperature": 0.0
        }, timeout=25)
        
        raw_content = response.json()['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        result = json.loads(match.group()) if match else json.loads(raw_content)
        
        # If AI returns generic "Physics Lesson", force our manual translation
        if result['title'].lower() == "physics lesson" or has_sinhala(result['title']):
            log_status("AI WARN", "AI failed translation. Forcing manual dictionary...")
            man_title, man_cat = sanitize_title(text_content)
            return {"title": man_title, "category": man_cat}
            
        return result
    except Exception as e:
        log_status("AI FAIL", f"Error: {e}")
        man_title, man_cat = sanitize_title(text_content)
        return {"title": man_title, "category": man_cat}

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

# --- Main Logic ---

async def main():
    log_header("TELE2TUBE: TUITION VIDEO PROCESSOR")
    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    await client.connect()
    
    creds = Credentials(None, refresh_token=YOUTUBE_REFRESH_TOKEN, 
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET)
    youtube = build('youtube', 'v3', credentials=creds)

    for i, link in enumerate(VIDEO_LINKS, 1):
        log_header(f"ITEM {i} OF {len(VIDEO_LINKS)}")
        
        # Robust Link Parsing
        try:
            clean_link = link.strip().split('t.me/')[1]
            parts = [p for p in clean_link.split('/') if p]
            msg_id = int(parts[-1])
            target = parts[0]
            if target == 'c':
                chat_id = int(f"-100{parts[1]}")
                entity = await client.get_entity(chat_id)
            else:
                entity = await client.get_entity(target)
            
            message = await client.get_messages(entity, ids=msg_id)
        except Exception as e:
            log_status("ERROR", f"Could not find message: {e}")
            continue

        if not message or not message.media:
            log_status("SKIP", "No video found in this message.")
            continue

        # Show exactly what we are sending to the AI
        raw_text = message.text or message.caption or ""
        log_status("INPUT", f"Text detected: '{raw_text[:50]}...'")

        log_status("PROCESS", "Analyzing & Translating...")
        metadata = await analyze_with_ai(raw_text)
        
        print(f"  > FINAL TITLE: {metadata['title']}")
        print(f"  > FINAL UNIT:  {metadata['category']}")

        if not os.path.exists("downloads"): os.makedirs("downloads")
        path = await client.download_media(message, file="downloads/")
        
        if path:
            log_status("UPLOAD", "Uploading to YouTube...")
            body = {
                'snippet': {'title': metadata['title'], 'description': raw_text, 'categoryId': '27'},
                'status': {'privacyStatus': 'private'}
            }
            media = MediaFileUpload(path, chunksize=10*1024*1024, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"\r[UPLOAD] {int(status.progress() * 100)}%", end="")
            
            # Add to Playlist
            pid = get_or_create_playlist(youtube, metadata['category'])
            if pid:
                youtube.playlistItems().insert(part="snippet", body={
                    "snippet": {
                        "playlistId": pid,
                        "resourceId": {"kind": "youtube#video", "videoId": response['id']}
                    }
                }).execute()
                log_status("PLAYLIST", f"Added to {metadata['category']}")
            
            os.remove(path)

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
