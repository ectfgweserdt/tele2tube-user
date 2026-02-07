import os
import json
import asyncio
import time
import re
import requests
from google import genai
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- Configuration ---
# Updated secret name to avoid the "GITHUB_" prefix restriction
GH_TOKEN = os.environ.get('GH_MODELS_TOKEN', '') 

TG_API_ID = os.environ.get('TG_API_ID')
TG_API_HASH = os.environ.get('TG_API_HASH')
TG_SESSION_STRING = os.environ.get('TG_SESSION_STRING')

YOUTUBE_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
VIDEO_LINKS = os.environ.get('VIDEO_LINKS', '').split(',')

# Guaranteed mappings to ensure proper playlists even if AI fails
SINHALA_UNIT_MAPPING = {
    "තාපය": "Heat",
    "යාන්ත්‍ර": "Mechanics",
    "ආලෝකය": "Light",
    "තරංග": "Waves",
    "විද්‍යුත්": "Electricity",
    "පදාර්ථ": "Properties of Matter"
}

# --- AI Logic ---

async def analyze_with_github(text_content):
    """Uses GitHub Models (GPT-4o mini) for stable translation and categorization."""
    if not GH_TOKEN:
        return None
        
    endpoint = "https://models.inference.ai.azure.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Content-Type": "application/json"
    }
    
    prompt = f"""
    You are a Physics Tuition Assistant. 
    Analyze this text: "{text_content}"
    
    1. Identify the Physics Unit (e.g., Heat, Mechanics, Waves).
    2. Translate the title to English professionally.
    
    Return ONLY JSON:
    {{"title": "English Title", "category": "Unit Name"}}
    """
    
    try:
        response = requests.post(endpoint, headers=headers, json={
            "messages": [{"role": "user", "content": prompt}],
            "model": "gpt-4o-mini",
            "temperature": 0.1
        })
        data = response.json()
        raw_content = data['choices'][0]['message']['content']
        match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        return json.loads(match.group())
    except Exception as e:
        print(f"GitHub AI Error: {e}")
        return None

async def analyze_content_with_retry(text_content):
    """Hybrid approach: GitHub -> Gemini -> Local Mapping."""
    if not text_content: text_content = "Untitled Video"
    
    # 1. Try GitHub Models (Most stable for this task)
    result = await analyze_with_github(text_content)
    if result:
        return result

    # 2. Try Gemini (Fallback)
    # ... (Gemini logic from previous versions would go here)

    # 3. Last Resort: Local Mapping
    category = "General Tuition"
    for s, e in SINHALA_UNIT_MAPPING.items():
        if s in text_content:
            category = e
            break
            
    return {
        "title": text_content.replace('*', '').strip(),
        "category": category
    }

# --- YouTube & Telegram Helpers ---

def get_youtube_service():
    creds = Credentials(None, refresh_token=YOUTUBE_REFRESH_TOKEN, 
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET)
    return build('youtube', 'v3', credentials=creds)

def get_or_create_playlist(youtube, title):
    try:
        request = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
        response = request.execute()
        for item in response.get('items', []):
            if item['snippet']['title'].lower() == title.lower():
                return item['id']
        
        print(f"Creating Playlist: {title}")
        res = youtube.playlists().insert(part="snippet,status", body={
            "snippet": {"title": title},
            "status": {"privacyStatus": "private"}
        }).execute()
        return res['id']
    except: return None

# --- Main Logic ---

async def main():
    client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
    await client.connect()
    youtube = get_youtube_service()

    for link in VIDEO_LINKS:
        if not link.strip(): continue
        try:
            # Simple regex to get msg_id and chat
            msg_id = int(link.split('/')[-1])
            entity = await client.get_entity(link.split('/')[-2] if 't.me/c/' not in link else int(f"-100{link.split('/')[-2]}"))
            
            message = await client.get_messages(entity, ids=msg_id)
            print(f"\nProcessing: {message.text[:30]}...")

            metadata = await analyze_content_with_retry(message.text or message.caption)
            print(f"AI Result: {metadata['title']} -> Category: {metadata['category']}")

            # Download and Upload logic...
            path = await client.download_media(message, file="downloads/")
            
            body = {
                'snippet': {'title': metadata['title'], 'description': message.text, 'categoryId': '27'},
                'status': {'privacyStatus': 'private'}
            }
            media = MediaFileUpload(path, chunksize=10*1024*1024, resumable=True)
            video = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
            
            # Add to Playlist
            pid = get_or_create_playlist(youtube, metadata['category'])
            if pid:
                youtube.playlistItems().insert(part="snippet", body={
                    "snippet": {
                        "playlistId": pid,
                        "resourceId": {"kind": "youtube#video", "videoId": video['id']}
                    }
                }).execute()
                print(f"Successfully added to {metadata['category']} playlist!")

            os.remove(path)
        except Exception as e:
            print(f"Error: {e}")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
