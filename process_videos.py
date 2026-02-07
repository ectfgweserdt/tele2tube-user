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

# List of links (handles mixed formats: t.me/chat/123 or t.me/c/id/123)
VIDEO_LINKS = [l.strip() for l in os.environ.get('VIDEO_LINKS', '').split(',') if l.strip()]

# --- AI & Translation Logic ---

async def analyze_with_ai(text_content):
    """Uses GitHub GPT-4o-mini for Physics context analysis."""
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
        })
        data = response.json()
        raw = data['choices'][0]['message']['content']
        return json.loads(re.search(r'\{.*\}', raw, re.DOTALL).group())
    except:
        return {"title": text_content[:50], "category": "General"}

# --- Telegram Link Resolver ---

async def resolve_telegram_message(client, link):
    """
    Robustly resolves a Telegram link to a message object.
    Handles public links, private 'c' links, and topic links.
    """
    try:
        # Regex to extract components from various link styles
        # Format 1: https://t.me/c/123456789/580
        # Format 2: https://t.me/username/580
        # Format 3: https://t.me/c/123456789/100/580 (Topics)
        
        parts = link.strip('/').split('/')
        msg_id = int(parts[-1])
        
        if 't.me/c/' in link:
            # Private link. ID is usually the second to last or third to last part.
            raw_id = parts[-2] if len(parts) == 5 else parts[-3]
            # Convert to Telegram's internal 'marked' ID format
            chat_id = int(f"-100{raw_id}")
            
            # CRITICAL: If the ID isn't in cache, we MUST fetch dialogs to find it
            try:
                entity = await client.get_entity(chat_id)
            except ValueError:
                print(f"Chat {chat_id} not in cache. Refreshing dialogs...")
                await client.get_dialogs()
                entity = await client.get_entity(chat_id)
        else:
            # Public link (e.g., t.me/my_channel/580)
            chat_username = parts[-2]
            entity = await client.get_entity(chat_username)

        message = await client.get_messages(entity, ids=msg_id)
        return message, entity
    except Exception as e:
        print(f"Failed to resolve link {link}: {e}")
        return None, None

# --- Main Logic ---

async def main():
    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    await client.connect()
    
    # Pre-fetch dialogs once to populate cache
    print("Initializing Telegram cache...")
    await client.get_dialogs()
    
    # Initialize YouTube
    creds = Credentials(None, refresh_token=YOUTUBE_REFRESH_TOKEN, 
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET)
    youtube = build('youtube', 'v3', credentials=creds)

    for link in VIDEO_LINKS:
        print(f"\n--- Processing Link: {link} ---")
        message, entity = await resolve_telegram_message(client, link)
        
        if not message or not message.media:
            print("Skipping: Message not found or has no video.")
            continue

        # 1. AI Analysis
        text = message.text or message.caption or "Untitled Video"
        metadata = await analyze_with_ai(text)
        print(f"Target: {metadata['title']} | Unit: {metadata['category']}")

        # 2. Download
        path = await client.download_media(message, file="downloads/")
        
        # 3. Upload to YouTube
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
            video_response = youtube.videos().insert(part="snippet,status", body=request_body, media_body=media).execute()
            print(f"Uploaded! ID: {video_response['id']}")
            
            # Clean up
            os.remove(path)
        except Exception as e:
            print(f"YouTube Error: {e}")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
