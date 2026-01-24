import os
import sys
import asyncio
import re
import math
from telethon import TelegramClient, utils
from telethon.sessions import StringSession 
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
import googleapiclient.errors

# --- CONFIGURATION ---
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
PARALLEL_CHUNKS = 4  # Number of parallel downloads. 4 is usually optimal.

# Fetching API Keys
# Note: GEMINI and OMDB keys are no longer needed for simple mode

def download_progress_callback(current, total):
    # Simple progress indicator
    if total:
        print(f"⬇️ Download: {current/1024/1024:.2f}MB / {total/1024/1024:.2f}MB ({current*100/total:.1f}%)", end='\r')

async def fast_download(client, message, filename, progress_callback=None):
    """
    Downloads a file in parallel chunks to maximize speed, then stitches them together.
    """
    msg_media = message.media
    if not msg_media:
        return None
        
    # Get file size and attributes
    document = msg_media.document if hasattr(msg_media, 'document') else msg_media
    file_size = document.size
    
    # 10MB chunk size to reduce overhead and improve speed
    part_size = 10 * 1024 * 1024 
    part_count = math.ceil(file_size / part_size)
    
    print(f"🚀 Starting Parallel Download ({PARALLEL_CHUNKS} threads) for {file_size/1024/1024:.2f} MB...")

    # Create a lock for file writing (since we are async)
    file_lock = asyncio.Lock()
    
    # Tracking progress
    downloaded_bytes = 0
    
    with open(filename, 'wb') as f:
        # Pre-allocate file size (optional but good for performance)
        f.seek(file_size - 1)
        f.write(b'\0')
        f.seek(0)
        
        queue = asyncio.Queue()
        for i in range(part_count):
            queue.put_nowait(i)
            
        async def worker():
            nonlocal downloaded_bytes
            while not queue.empty():
                try:
                    part_index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                
                offset = part_index * part_size
                # Calculate limit for this chunk (might be smaller for last chunk)
                current_limit = min(part_size, file_size - offset)
                
                try:
                    # Use iter_download to stream the specific chunk range
                    # This fixes the 'offset' argument error by using the correct method
                    current_file_pos = offset
                    async for chunk in client.iter_download(
                        message.media, 
                        offset=offset, 
                        limit=current_limit,
                        request_size=512*1024 # Request larger blocks (512KB) from Telegram
                    ):
                        async with file_lock:
                            f.seek(current_file_pos)
                            f.write(chunk)
                        
                        chunk_len = len(chunk)
                        current_file_pos += chunk_len
                        downloaded_bytes += chunk_len
                        
                        if progress_callback:
                            progress_callback(downloaded_bytes, file_size)
                            
                except Exception as e:
                    print(f"⚠️ Chunk {part_index} failed, retrying... ({e})")
                    queue.put_nowait(part_index) # Retry
                finally:
                    queue.task_done()

        # Start workers
        tasks = [asyncio.create_task(worker()) for _ in range(PARALLEL_CHUNKS)]
        await asyncio.gather(*tasks)

    print(f"\n✅ Fast Download Complete: {filename}")
    return filename

def get_simple_metadata(message, filename):
    """
    Extracts simple title from filename and description from the Telegram message caption.
    """
    # 1. Title from Filename
    clean_name = os.path.splitext(filename)[0]
    # Replace common separators with spaces and strip
    title = clean_name.replace('_', ' ').replace('.', ' ').strip()
    
    # Ensure title isn't too long for YouTube (max 100 chars)
    if len(title) > 95:
        title = title[:95]

    # 2. Description from Message Caption
    description = message.message if message.message else f"Uploaded from Telegram: {title}"
    
    # 3. Tags (Static)
    tags = ["Telegram", "Video", "Upload"]

    return {
        "title": title,
        "description": description,
        "tags": tags
    }

def upload_to_youtube(video_path, metadata):
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
                'title': metadata['title'],
                'description': metadata['description'],
                'tags': metadata['tags'],
                'categoryId': '22' # 'People & Blogs' as a generic default
            },
            'status': {'privacyStatus': 'private'}
        }
        
        print(f"🚀 Uploading: {body['snippet']['title']}")
        # Resumable upload allows for more stability with large files
        media = MediaFileUpload(video_path, chunksize=1024*1024*2, resumable=True)
        
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"⬆️ Upload: {int(status.progress() * 100)}%", end='\r')
        
        print(f"\n🎉 SUCCESS! https://youtu.be/{response['id']}")
        return True

    except googleapiclient.errors.HttpError as e:
        error_details = e.content.decode()
        if "uploadLimitExceeded" in error_details or "quotaExceeded" in error_details:
            print("\n❌ API LIMIT REACHED!")
            return "LIMIT_REACHED"
        print(f"\n🔴 YouTube HTTP Error: {e}")
        return False
    except Exception as e:
        print(f"\n🔴 Error during upload: {e}")
        return False

def parse_telegram_link(link):
    """
    Parses a Telegram link to extract chat entity and message ID.
    Supports:
    - Public: https://t.me/username/123
    - Private: https://t.me/c/1234567890/123
    - Private with Topic: https://t.me/c/1234567890/184/215
    """
    link = link.strip()
    if '?' in link:
        link = link.split('?')[0]
    
    # Private Link Handling
    if 't.me/c/' in link:
        try:
            # parsing https://t.me/c/CHANNEL_ID/TOPIC_ID/MSG_ID or CHANNEL_ID/MSG_ID
            # Split by 't.me/c/' and take the right side, then split by '/'
            path_parts = link.split('t.me/c/')[1].split('/')
            
            # Filter to keep only numeric parts (ignores empty strings or non-numeric segments)
            numeric_parts = [p for p in path_parts if p.isdigit()]
            
            if len(numeric_parts) >= 2:
                chat_id_str = numeric_parts[0]
                msg_id = int(numeric_parts[-1]) # Always take the LAST number as the message ID
                
                chat_id = int(f"-100{chat_id_str}")
                return chat_id, msg_id
        except Exception:
            pass # Fallback to other regex if this manual parse fails (unlikely)

    # Legacy Regex for Standard Private Links (Just in case)
    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        # This might catch the topic ID if the loop above fails, but the loop above is safer.
        chat_id_str = private_match.group(1)
        msg_id = int(private_match.group(2))
        chat_id = int(f"-100{chat_id_str}")
        return chat_id, msg_id

    # Public Link Handling (username/id)
    public_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if public_match:
        username = public_match.group(1)
        msg_id = int(public_match.group(2))
        return username, msg_id
        
    return None, None

async def process_single_link(client, link):
    try:
        print(f"\n--- Processing: {link} ---")
        
        chat_id, msg_id = parse_telegram_link(link)
        
        if not chat_id or not msg_id:
            print(f"❌ Invalid Link Format: {link}")
            return True

        print(f"🔍 Debug: Fetching Message ID {msg_id} from Chat {chat_id}")

        # Fetch Message
        try:
            message = await client.get_messages(chat_id, ids=msg_id)
        except ValueError:
            print(f"❌ Cannot access chat. Ensure the account is a member of: {chat_id}")
            return True
        except Exception as e:
            print(f"❌ Error fetching message: {e}")
            return True
        
        if not message or not message.media:
            print("❌ No media found in message.")
            return True

        # Determine filename
        if hasattr(message.file, 'name') and message.file.name:
            original_filename = message.file.name
        else:
            ext = message.file.ext if hasattr(message, 'file') else ".mp4"
            original_filename = f"video_{msg_id}{ext}"

        raw_file = f"download_{msg_id}_{original_filename}"
        
        # SAFETY CHECK: Remove file if it exists
        if os.path.exists(raw_file):
            os.remove(raw_file)

        # USE FAST DOWNLOADER
        await fast_download(client, message, raw_file, progress_callback=download_progress_callback)
        
        # Get Metadata
        metadata = get_simple_metadata(message, original_filename)
        
        # Upload
        status = upload_to_youtube(raw_file, metadata)

        # Cleanup
        if os.path.exists(raw_file):
            os.remove(raw_file)
            
        return status

    except Exception as e:
        print(f"🔴 Critical Error processing link {link}: {e}")
        return False

async def run_flow(links_str):
    links = [l.strip() for l in links_str.split(',') if l.strip()]
    
    try:
        client = TelegramClient(
            StringSession(os.environ['TG_SESSION_STRING']), 
            int(os.environ['TG_API_ID']), 
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
    except Exception as e:
        print(f"🔴 Client Error: {e}")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        asyncio.run(run_flow(sys.argv[1]))
