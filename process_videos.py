import os
import json
import asyncio
import time
from google import genai
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm

# --- Configuration ---
TG_API_ID = os.environ.get('TG_API_ID')
TG_API_HASH = os.environ.get('TG_API_HASH')
TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')

YOUTUBE_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
VIDEO_LINKS = os.environ.get('VIDEO_LINKS', '').split(',')

# --- Helpers ---

def get_youtube_service():
    """Authenticates with YouTube using a Refresh Token."""
    creds = Credentials(
        None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET
    )
    return build('youtube', 'v3', credentials=creds)

async def analyze_content_with_retry(text_content):
    """Uses Gemini API with mandatory exponential backoff for retries."""
    if not text_content:
        text_content = "No description provided. Analyze the context of a generic tuition class."
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are an intelligent assistant organizing tuition videos.
    Analyze this raw text from a Telegram message: "{text_content}"

    Extract/Generate:
    1. A clear, professional Video Title.
    2. A short Description.
    3. A general Subject Category (e.g., Mechanics, Calculus, Organic Chemistry). 

    Return ONLY a JSON object with keys: "title", "description", "category".
    """
    
    retries = 5
    for i in range(retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash-preview-09-2025',
                contents=prompt
            )
            clean_json = response.text.replace('```json', '').replace('```', '').strip()
            return json.loads(clean_json)
        except Exception as e:
            if i < retries - 1:
                wait_time = (2 ** i) # 1s, 2s, 4s, 8s, 16s
                await asyncio.sleep(wait_time)
                continue
            print(f"Gemini final error after retries: {e}")
            return {
                "title": "Tuition Class Video", 
                "description": f"Original text: {text_content}", 
                "category": "General Tuition"
            }

def progress_callback(current, total):
    # Print progress every 1% to avoid flooding logs but keep connection alive
    percent = (current * 100 / total)
    if int(current) % (1024 * 1024) == 0 or current == total: # Log every MB
        print(f"\rDownloading: {percent:.1f}% ({current}/{total} bytes)", end="")

async def parse_telegram_link(client, link):
    clean_link = link.strip().replace('https://', '').replace('http://', '').replace('t.me/', '')
    parts = clean_link.split('/')
    if len(parts) < 2:
        raise ValueError(f"Invalid link format: {link}")

    msg_id = int(parts[-1])
    entity_id = parts[1] if parts[0] == 'c' else parts[0]
    
    if parts[0] == 'c':
        entity_id = int(f"-100{entity_id}")
    
    try:
        entity = await client.get_entity(entity_id)
        return entity, msg_id
    except Exception:
        async for dialog in client.iter_dialogs():
            if str(dialog.id) == str(entity_id) or str(dialog.id) == f"-100{entity_id}":
                return dialog.entity, msg_id
    raise ValueError(f"Could not find entity for: {link}")

# --- Main Logic ---

async def main():
    if not VIDEO_LINKS or VIDEO_LINKS == ['']:
        return

    print("Connecting to Telegram...")
    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    # Configure client for better stability on large files
    client.flood_sleep_threshold = 60 
    await client.connect()

    if not await client.is_user_authorized():
        print("Telegram Auth Failed.")
        return

    youtube = get_youtube_service()

    for link in VIDEO_LINKS:
        link = link.strip()
        if not link: continue
        print(f"\n--- Processing: {link} ---")

        try:
            entity, msg_id = await parse_telegram_link(client, link)
            message = await client.get_messages(entity, ids=msg_id)

            if not message or not message.media:
                print("No media found.")
                continue

            print("Analyzing content with Gemini (with backoff)...")
            metadata = await analyze_content_with_retry(message.text or message.caption)
            
            # 3. Download Video with simple method but higher stability settings
            print("Downloading from Telegram...")
            if not os.path.exists("downloads"):
                os.makedirs("downloads")
                
            file_path = await client.download_media(
                message, 
                file="downloads/", 
                progress_callback=progress_callback
            )
            print(f"\nDownloaded: {file_path}")

            # 4. Upload to YouTube
            print("Uploading to YouTube...")
            body = {
                'snippet': {
                    'title': metadata['title'],
                    'description': metadata['description'],
                    'categoryId': '27'
                },
                'status': {'privacyStatus': 'private'}
            }

            media = MediaFileUpload(file_path, chunksize=5*1024*1024, resumable=True)
            upload_request = youtube.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )

            response = None
            while response is None:
                status, response = upload_request.next_chunk()
                if status:
                    print(f"\rUpload: {int(status.progress() * 100)}%", end='')

            print(f"\nSuccess! Video ID: {response.get('id')}")

            if os.path.exists(file_path):
                os.remove(file_path)

        except Exception as e:
            print(f"Error: {e}")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
