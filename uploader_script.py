import os
import sys
import time
import asyncio
import hashlib
import math
from telethon import TelegramClient, errors, utils
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# --- CONFIGURATION ---
PARALLEL_CONNECTIONS = 16  # High-speed sweet spot
CHUNK_SIZE = 1024 * 1024   # 1MB chunks

class FastDownloader:
    """Bypasses Telegram speed limits using parallel connections."""
    def __init__(self, client, message, file_path):
        self.client = client
        self.message = message
        self.file_path = file_path
        self.total_size = message.file.size
        self.downloaded = 0

    async def download_part(self, offset, limit, part_index):
        # We use the document directly for parallel downloading
        async for chunk in self.client.iter_download(self.message.document, offset=offset, limit=limit):
            # Using a temporary file for each part
            part_path = f"{self.file_path}.part{part_index}"
            with open(part_path, "ab") as f:
                f.write(chunk)
                self.downloaded += len(chunk)
                percent = (self.downloaded / self.total_size) * 100
                if time.time() - getattr(self, '_last_print', 0) > 2: # Print every 2 seconds
                    print(f"🚀 Downloading: {percent:.2f}% ({self.downloaded // 1024 // 1024} MB / {self.total_size // 1024 // 1024} MB)")
                    self._last_print = time.time()

    async def download(self):
        part_size = math.ceil(self.total_size / PARALLEL_CONNECTIONS)
        tasks = []
        for i in range(PARALLEL_CONNECTIONS):
            offset = i * part_size
            limit = min(part_size, self.total_size - offset)
            if limit <= 0: break
            tasks.append(self.download_part(offset, limit, i))
        
        await asyncio.gather(*tasks)
        
        # Combine parts
        with open(self.file_path, "wb") as final_file:
            for i in range(PARALLEL_CONNECTIONS):
                part_name = f"{self.file_path}.part{i}"
                if os.path.exists(part_name):
                    with open(part_name, "rb") as pf:
                        final_file.write(pf.read())
                    os.remove(part_name)
        print(f"✅ Download Complete: {self.file_path}")

def get_lecture_title(filename):
    """Cleans file name for a professional YouTube title."""
    name = os.path.splitext(filename)[0]
    name = name.replace("_", " ").replace(".", " ")
    return name.title()

def upload_to_youtube(file_path, title):
    """Simple upload logic for lectures."""
    try:
        creds = Credentials(
            None,
            refresh_token=os.environ['YOUTUBE_REFRESH_TOKEN'],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ['YOUTUBE_CLIENT_ID'],
            client_secret=os.environ['YOUTUBE_CLIENT_SECRET']
        )
        
        youtube = build("youtube", "v3", credentials=creds)
        
        body = {
            "snippet": {
                "title": title[:100],
                "description": f"Uploaded lecture: {title}\nAutomated Archive.",
                "categoryId": "27" # Education
            },
            "status": {"privacyStatus": "private"}
        }
        
        media = MediaFileUpload(file_path, chunksize=1024*1024*5, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        print(f"📤 Uploading to YouTube: {title}...")
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"📈 Upload Progress: {int(status.progress() * 100)}%")
        
        print(f"🎉 SUCCESS: https://youtu.be/{response['id']}")
    except Exception as e:
        print(f"❌ YouTube Upload Error: {e}")

async def process_link(client, link):
    try:
        print(f"🔗 Processing: {link}")
        parts = [p for p in link.strip('/').split('/') if p]
        msg_id = int(parts[-1])
        
        # Handle different Telegram link formats
        if 'c' in parts:
            # Private channel link: /c/CHANNEL_ID/MSG_ID
            chat_id = int(f"-100{parts[parts.index('c')+1]}")
        else:
            # Public channel link: /CHANNEL_NAME/MSG_ID
            chat_id = parts[-2]
        
        message = await client.get_messages(chat_id, ids=msg_id)
        if not message or not message.file:
            print(f"❌ No video found in link: {link}")
            return

        filename = message.file.name or f"lecture_{msg_id}.mp4"
        title = get_lecture_title(filename)
        
        # High Speed Download
        downloader = FastDownloader(client, message, filename)
        await downloader.download()
        
        # Upload
        upload_to_youtube(filename, title)
        
        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        print(f"❌ Error processing {link}: {e}")

async def main():
    if len(sys.argv) < 2: 
        print("❌ No links provided.")
        return
    
    links = sys.argv[1].split(',')
    
    # FETCH SECRETS
    api_id = os.environ.get('TG_API_ID')
    api_hash = os.environ.get('TG_API_HASH')
    bot_token = os.environ.get('TG_BOT_TOKEN')

    if not all([api_id, api_hash, bot_token]):
        print("❌ Missing TG_API_ID, TG_API_HASH, or TG_BOT_TOKEN in GitHub Secrets.")
        return

    # START CLIENT AS BOT
    # By using start(bot_token=...), it bypasses the phone number request
    client = TelegramClient('bot_session', api_id, api_hash)
    
    try:
        await client.start(bot_token=bot_token)
        print("🤖 Bot logged in successfully.")
        
        for link in links:
            await process_link(client, link)
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
