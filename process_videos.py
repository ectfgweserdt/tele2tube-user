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
    
    prompt = f"Analyze this Physics tuition post: '{text_content}'. Provide a professional English title and the Physics Unit (Heat, Mechanics, etc). Return ONLY JSON: {{\"title\":\"...\", \"category\":\"...\"}}"
    
    try:
        response = requests.post(endpoint, headers=headers, json={
            "messages": [{"role": "user", "content": prompt}],
            "model": "gpt-4o-mini",
            "temperature": 0.1
        }, timeout=30)
        data = response.json()
        raw = data['choices'][0]['message']['content']
        return json.loads(re.search(r'\{.*\}', raw, re.DOTALL).group())
    except Exception as e:
        log_status("AI ERROR", str(e))
        return {"title": text_content[:50], "category": "General"}

# --- Telegram Link Resolver ---

async def resolve_telegram_message(client, link):
    """
    Improved resolver to handle topic-based links like /channel/topic/msg_id
    """
    try:
        # Clean the link and split into segments
        clean_link = link.strip().replace('https://t.me/', '').replace('http://t.me/', '')
        parts = [p for p in clean_link.split('/') if p]
        
        # In a link like sr25theoryeduzone/580/584
        # parts[0] = sr25theoryeduzone (The actual entity)
        # parts[-1] = 584 (The actual message ID)
        
        msg_id = int(parts[-1])
        target_entity = parts[0]

        if target_entity == 'c':
            # Private link format: t.me/c/ID/MSG_ID or t.me/c/ID/TOPIC/MSG_ID
            raw_id = parts[1]
            chat_id = int(f"-100{raw_id}")
            try:
                entity = await client.get_entity(chat_id)
            except ValueError:
                log_status("TELEGRAM", f"Chat {chat_id} not in cache. Refreshing...")
                await client.get_dialogs()
                entity = await client.get_entity(chat_id)
        else:
            # Public link format: t.me/username/MSG_ID or t.me/username/TOPIC/MSG_ID
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
        log_status("ERROR", "No video links provided in environment variables.")
        return

    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    log_status("TELEGRAM", "Connecting to client...")
    await client.connect()
    
    log_status("CACHE", "Pre-fetching dialogs (this may take a moment)...")
    await client.get_dialogs()
    
    log_status("YOUTUBE", "Initializing YouTube API...")
    creds = Credentials(None, refresh_token=YOUTUBE_REFRESH_TOKEN, 
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET)
    youtube = build('youtube', 'v3', credentials=creds)

    for i, link in enumerate(VIDEO_LINKS, 1):
        log_header(f"ITEM {i} OF {len(VIDEO_LINKS)}")
        log_status("LINK", link)
        
        message, entity = await resolve_telegram_message(client, link)
        
        if not message or not message.media:
            log_status("SKIP", "No media or message found.")
            continue

        # 1. AI Analysis
        log_status("AI", "Analyzing text and translating...")
        text = message.text or message.caption or "Untitled Video"
        metadata = await analyze_with_ai(text)
        
        print(f"\n  > Translated Title: {metadata['title']}")
        print(f"  > Target Unit:     {metadata['category']}\n")

        # 2. Download
        log_status("DOWNLOAD", "Starting media download...")
        if not os.path.exists("downloads"):
            os.makedirs("downloads")
            
        path = await client.download_media(message, file="downloads/", progress_callback=download_progress)
        print() # Move to next line after progress bar
        
        if not path:
            log_status("ERROR", "Download failed.")
            continue

        # 3. Upload to YouTube
        log_status("UPLOAD", f"Preparing YouTube upload ({format_size(os.path.getsize(path))})...")
        try:
            request_body = {
                'snippet': {
                    'title': metadata['title'],
                    'description': f"Automated upload from Telegram.\n\nOriginal Text:\n{text}",
                    'categoryId': '27' # Education
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
            
            print(f"\n[UPLOAD      ] SUCCESS! Video ID: {response['id']}")
            
            # 4. Cleanup
            log_status("CLEANUP", "Deleting local file...")
            os.remove(path)
            
        except Exception as e:
            log_status("YT ERROR", f"Upload failed: {e}")

    log_header("ALL PROCESSES COMPLETE")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
