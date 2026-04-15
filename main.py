import cv2
import os
import tempfile
import re
import string
import time
import asyncio
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from fpdf import FPDF
from PIL import Image
import yt_dlp
from skimage.metrics import structural_similarity as ssim
from threading import Semaphore
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
import logging
import io
import json
import http.server
import socketserver
import datetime

# Logging setup - Clean console output
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('telegram.ext').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Your Telegram Bot Token
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('TELEGRAM_TOKEN', '')

# Channel settings
CHANNEL_USERNAME = '@alluserpdf'

# Required group for bot access
REQUIRED_GROUP = '@topperview'
REQUIRED_GROUP_LINK = 'https://t.me/topperview'

# Dashboard admin token
ADMIN_TOKEN = os.getenv('DASHBOARD_ADMIN_TOKEN', 'changeme_admin_token')

# SSIM settings
SSIM_THRESHOLD = 1
SSIM_RESIZE_DIM = (128, 72)
FRAME_SKIP_FOR_SSIM_CHECK = 400

# PDF settings
PDF_FRAME_WIDTH_TARGET = 1280
WATERMARK_TEXT = "Created by @youpdf_bot"
MAX_PDF_PAGES = 5000

# Multi-user processing settings
MAX_CONCURRENT_TOTAL_REQUESTS = 50
MAX_REQUESTS_PER_USER = 10
CHUNK_DURATION_MINUTES = 30
MAX_VIDEO_DURATION_HOURS = 2
ADMIN_MAX_VIDEO_DURATION_HOURS = 50

# Admin/Owner ID
OWNER_ID = 2141959380

# Global tracking for concurrent processing
processing_requests = {}  # {request_id: {user_id, video_id, start_time, title, task}}
user_request_counts = {}  # {user_id: count}
thread_pool = ThreadPoolExecutor(max_workers=50)

USERS_DB_PATH = 'users.json'
PDF_CACHE_PATH = 'pdf_cache.json'
GRANTED_USERS_PATH = 'granted_users.json'
QUEUE_PATH = 'queue.json'

# Thread-safe lock for queue file
queue_file_lock = threading.Lock()


# ─── PDF Cache functions ───────────────────────────────────────────────────────

def load_pdf_cache():
    if not os.path.exists(PDF_CACHE_PATH):
        return {}
    with open(PDF_CACHE_PATH, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_pdf_cache(cache):
    with open(PDF_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def get_cached_pdf(video_id):
    cache = load_pdf_cache()
    entry = cache.get(video_id)
    if entry and entry.get('parts'):
        return entry
    return None

def add_to_pdf_cache(video_id, title, part_num, total_parts, file_id, pages):
    cache = load_pdf_cache()
    if video_id not in cache:
        cache[video_id] = {'title': title, 'parts': []}
    existing_parts = [p['part_num'] for p in cache[video_id]['parts']]
    if part_num not in existing_parts:
        cache[video_id]['parts'].append({
            'part_num': part_num,
            'total_parts': total_parts,
            'file_id': file_id,
            'pages': pages,
            'cached_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        })
        cache[video_id]['parts'].sort(key=lambda x: x['part_num'])
    save_pdf_cache(cache)


# ─── Queue functions ───────────────────────────────────────────────────────────

def load_queue():
    with queue_file_lock:
        if not os.path.exists(QUEUE_PATH):
            return []
        with open(QUEUE_PATH, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except Exception:
                return []

def save_queue(q):
    with queue_file_lock:
        with open(QUEUE_PATH, 'w', encoding='utf-8') as f:
            json.dump(q, f, ensure_ascii=False, indent=2)

def add_to_queue(user_id, chat_id, username, user_name, url, video_id):
    """Add a request to the persistent queue. Returns queue_id."""
    q = load_queue()
    queue_id = str(uuid.uuid4())
    q.append({
        'queue_id': queue_id,
        'user_id': user_id,
        'chat_id': chat_id,
        'username': username,
        'user_name': user_name,
        'url': url,
        'video_id': video_id,
        'queued_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    })
    save_queue(q)
    print(f"📋 Queued request {queue_id[:8]} for user {user_name} | Queue size: {len(q)}")
    return queue_id

def remove_from_queue(queue_id):
    """Remove a processed item from the queue."""
    q = load_queue()
    q = [item for item in q if item['queue_id'] != queue_id]
    save_queue(q)

def get_queue_size():
    return len(load_queue())

def get_queue_position(queue_id):
    q = load_queue()
    for i, item in enumerate(q):
        if item['queue_id'] == queue_id:
            return i + 1
    return None


# ─── Granted Users functions ────────────────────────────────────────────────────

def load_granted_users():
    if not os.path.exists(GRANTED_USERS_PATH):
        return {}
    with open(GRANTED_USERS_PATH, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_granted_users(data):
    with open(GRANTED_USERS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def grant_user_limit(user_id, max_hours):
    data = load_granted_users()
    data[str(user_id)] = {'max_hours': max_hours, 'granted_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    save_granted_users(data)

def revoke_user_limit(user_id):
    data = load_granted_users()
    data.pop(str(user_id), None)
    save_granted_users(data)

def get_user_max_hours(user_id):
    if user_id == OWNER_ID:
        return ADMIN_MAX_VIDEO_DURATION_HOURS
    data = load_granted_users()
    entry = data.get(str(user_id))
    if entry:
        return entry['max_hours']
    return MAX_VIDEO_DURATION_HOURS


# ─── Users DB functions ────────────────────────────────────────────────────────

def load_users():
    if not os.path.exists(USERS_DB_PATH):
        return []
    with open(USERS_DB_PATH, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except Exception:
            return []

def save_users(users):
    with open(USERS_DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def add_user(user_id, username, real_name):
    users = load_users()
    existing = next((u for u in users if u['user_id'] == user_id), None)
    if existing is None:
        users.append({
            'user_id': user_id,
            'username': username,
            'real_name': real_name,
            'joined_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'videos_converted': 0,
            'total_duration_seconds': 0,
            'pdfs_generated': 0,
        })
        save_users(users)
        return True
    else:
        changed = False
        for field, default in [('joined_at', ''), ('videos_converted', 0),
                                ('total_duration_seconds', 0), ('pdfs_generated', 0)]:
            if field not in existing:
                existing[field] = default
                changed = True
        if changed:
            save_users(users)
        return False

def delete_user(user_id):
    """Remove a user from the database by user_id."""
    users = load_users()
    before = len(users)
    users = [u for u in users if u['user_id'] != user_id]
    save_users(users)
    return before - len(users)  # returns 1 if deleted, 0 if not found

def update_user_stats(user_id, duration_seconds=0, pdfs_delta=0):
    users = load_users()
    for u in users:
        if u['user_id'] == user_id:
            u['videos_converted'] = u.get('videos_converted', 0) + (1 if duration_seconds > 0 else 0)
            u['total_duration_seconds'] = u.get('total_duration_seconds', 0) + duration_seconds
            u['pdfs_generated'] = u.get('pdfs_generated', 0) + pdfs_delta
            break
    save_users(users)

def get_global_stats():
    users = load_users()
    cache = load_pdf_cache()
    total_users = len(users)
    total_videos = sum(u.get('videos_converted', 0) for u in users)
    total_duration = sum(u.get('total_duration_seconds', 0) for u in users)
    total_pdfs = sum(u.get('pdfs_generated', 0) for u in users)
    cached_videos = len(cache)
    return {
        'total_users': total_users,
        'total_videos_converted': total_videos,
        'total_duration_seconds': total_duration,
        'total_pdfs_generated': total_pdfs,
        'cached_videos': cached_videos,
        'active_requests': len(processing_requests),
        'queue_size': get_queue_size(),
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }

def is_admin(user_id):
    return user_id == OWNER_ID


# ─── Group membership verification ────────────────────────────────────────────

async def check_membership(bot, user_id):
    """Returns True if user is a member of the required group."""
    if user_id == OWNER_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_GROUP, user_id=user_id)
        return member.status in ('member', 'administrator', 'creator')
    except Exception:
        return False

async def send_join_prompt(bot, chat_id, user_name):
    """Send join group + verify buttons to user."""
    keyboard = [
        [InlineKeyboardButton("🔗 Group Join करें", url=REQUIRED_GROUP_LINK)],
        [InlineKeyboardButton("✅ Verify करें (Join के बाद)", callback_data="verify_membership")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔐 {user_name}, Bot Access Required!\n\n"
            f"इस bot को use करने के लिए आपको हमारे group में join करना होगा।\n\n"
            f"👇 Step 1: नीचे दिए button से group join करें\n"
            f"👇 Step 2: Join के बाद ✅ Verify button दबाएं\n\n"
            f"Group: {REQUIRED_GROUP_LINK}"
        ),
        reply_markup=reply_markup
    )

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Verify Membership inline button."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_name = query.from_user.first_name

    is_member = await check_membership(context.bot, user_id)
    if is_member:
        await query.edit_message_text(
            f"✅ {user_name}, Verification Successful!\n\n"
            f"आप group के member हैं। 🎉\n"
            f"अब आप bot use कर सकते हैं!\n\n"
            f"बस YouTube link भेजिए! 🚀"
        )
    else:
        keyboard = [
            [InlineKeyboardButton("🔗 Group Join करें", url=REQUIRED_GROUP_LINK)],
            [InlineKeyboardButton("🔄 Verify Again", callback_data="verify_membership")]
        ]
        await query.edit_message_text(
            f"❌ {user_name}, आप अभी group में join नहीं हैं!\n\n"
            f"Please पहले group join करें, फिर Verify करें।\n"
            f"Group: {REQUIRED_GROUP_LINK}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ─── Request tracking ──────────────────────────────────────────────────────────

def can_process_request(user_id):
    """Check if user can start a new request right now."""
    current_user_requests = user_request_counts.get(user_id, 0)
    total_requests = len(processing_requests)

    if total_requests >= MAX_CONCURRENT_TOTAL_REQUESTS:
        return False, "server_full"

    if current_user_requests >= MAX_REQUESTS_PER_USER:
        return False, "user_limit"

    return True, "ok"

def start_request(user_id, video_id, title="Processing...", task=None):
    """Start tracking a new request."""
    request_id = str(uuid.uuid4())
    processing_requests[request_id] = {
        'user_id': user_id,
        'video_id': video_id,
        'start_time': time.time(),
        'title': title,
        'task': task
    }

    if user_id not in user_request_counts:
        user_request_counts[user_id] = 0
    user_request_counts[user_id] += 1

    return request_id

def finish_request(request_id):
    """Finish tracking a request and free the slot."""
    if request_id in processing_requests:
        req = processing_requests[request_id]
        user_id = req['user_id']

        task = req.get('task')
        if task and not task.done():
            task.cancel()

        del processing_requests[request_id]

        if user_id in user_request_counts:
            user_request_counts[user_id] -= 1
            if user_request_counts[user_id] <= 0:
                del user_request_counts[user_id]

        print(f"✅ Request {request_id[:8]} finished. Active: {len(processing_requests)} | User {user_id} slots: {user_request_counts.get(user_id, 0)}")


# ─── Video processing helpers ──────────────────────────────────────────────────

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text('❌ Only admin can use this command.')
        return
    if not context.args:
        await update.message.reply_text('Usage: /broadcast <message>')
        return
    message = ' '.join(context.args)
    users = load_users()
    count = 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user['user_id'], text=message)
            count += 1
        except Exception:
            pass
    await update.message.reply_text(f'✅ Broadcast sent to {count} users.')

def get_video_id(url):
    video_id_match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", url)
    if video_id_match:
        return video_id_match.group(1)
    return None

def sanitize_filename(title):
    return ''.join(c for c in title if c in (string.ascii_letters + string.digits + ' -_')).rstrip()

def format_duration(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

def get_video_duration(video_id):
    try:
        from pytubefix import YouTube
        yt = YouTube(f"https://www.youtube.com/watch?v={video_id}")
        return yt.length or 0
    except Exception as e:
        print(f"⚠️  Duration check error for {video_id}: {e}")
        return 0

async def download_video_async(video_id, progress_callback=None):
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    output_file = f"video_{video_id}_{int(time.time())}.mp4"

    def progress_hook(d):
        if progress_callback and d['status'] == 'downloading':
            try:
                percent = d.get('_percent_str', 'N/A').strip()
                speed = d.get('_speed_str', 'N/A').strip()
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(progress_callback(percent, speed))
                except:
                    pass
            except Exception:
                pass

    def download_sync():
        try:
            from pytubefix import YouTube
            yt = YouTube(video_url)
            title = yt.title or 'Unknown Title'
            duration = yt.length or 0

            stream = (
                yt.streams.filter(progressive=True, file_extension='mp4')
                .order_by('resolution').desc().first()
            )
            if not stream:
                stream = yt.streams.filter(file_extension='mp4').first()
            if not stream:
                raise Exception("No downloadable stream found")

            out_dir = os.path.dirname(os.path.abspath(output_file)) or '.'
            final_path = stream.download(output_path=out_dir, filename=os.path.basename(output_file))
            if not os.path.exists(final_path):
                raise Exception("Video file download failed")

            return title, final_path, duration

        except Exception as e:
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except:
                    pass
            raise Exception(f"Download failed: {str(e)}")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, download_sync)

def extract_unique_frames_for_chunk(video_file, output_folder, start_time, end_time, chunk_num, n=3, ssim_threshold=0.8):
    cap = cv2.VideoCapture(video_file)
    fps = int(cap.get(cv2.CAP_PROP_FPS))

    start_frame = int(start_time * fps)
    end_frame = int(end_time * fps)

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    last_frame = None
    saved_frame = None
    frame_number = start_frame
    last_saved_frame_number = -1
    timestamps = []

    while frame_number < end_frame and cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if (frame_number - start_frame) % n == 0:
            frame = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_CUBIC)
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray_frame = cv2.resize(gray_frame, (128, 72))

            if last_frame is not None:
                try:
                    data_range = gray_frame.max() - gray_frame.min()
                    if data_range > 0:
                        similarity = ssim(gray_frame, last_frame, data_range=data_range)
                    else:
                        similarity = 1.0
                except Exception:
                    similarity = 0.0

                if similarity < ssim_threshold:
                    if saved_frame is not None and frame_number - last_saved_frame_number > fps:
                        frame_path = os.path.join(output_folder, f'chunk{chunk_num}_frame{frame_number:04d}_{frame_number // fps}.png')
                        cv2.imwrite(frame_path, saved_frame, [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
                        timestamps.append((frame_number, frame_number // fps))

                    saved_frame = frame
                    last_saved_frame_number = frame_number
                else:
                    saved_frame = frame
            else:
                frame_path = os.path.join(output_folder, f'chunk{chunk_num}_frame{frame_number:04d}_{frame_number // fps}.png')
                cv2.imwrite(frame_path, frame, [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
                timestamps.append((frame_number, frame_number // fps))
                last_saved_frame_number = frame_number

            last_frame = gray_frame

        frame_number += 1

    cap.release()
    return timestamps

def convert_frames_to_pdf_chunk(input_folder, output_file, timestamps, chunk_num):
    frame_files = [f for f in os.listdir(input_folder) if f.startswith(f'chunk{chunk_num}_')]
    frame_files = sorted(frame_files, key=lambda x: int(x.split('_')[1].split('frame')[-1]))

    pdf = FPDF("L")
    pdf.set_auto_page_break(False)
    total_pages = 0

    for i, (frame_file, (frame_number, timestamp_seconds)) in enumerate(zip(frame_files, timestamps)):
        frame_path = os.path.join(input_folder, frame_file)
        if not os.path.exists(frame_path):
            continue

        image = Image.open(frame_path)
        pdf.add_page()
        total_pages += 1

        width, height = image.size
        pdf_width = pdf.w
        pdf_height = pdf.h

        aspect_ratio = width / height
        new_width = pdf_width
        new_height = pdf_width / aspect_ratio

        if new_height > pdf_height:
            new_height = pdf_height
            new_width = pdf_height * aspect_ratio

        x = (pdf_width - new_width) / 2
        y = (pdf_height - new_height) / 2

        pdf.image(frame_path, x=x, y=y, w=new_width, h=new_height)

        timestamp = f"{timestamp_seconds // 3600:02d}:{(timestamp_seconds % 3600) // 60:02d}:{timestamp_seconds % 60:02d}"
        watermark_text = "Created by @youpdf_bot"
        combined_text = f"{timestamp} - {watermark_text}"

        pdf.set_xy(5, 5)
        pdf.set_font("Arial", size=18)
        pdf.cell(0, 0, combined_text)

    if total_pages > 0:
        pdf.output(output_file)
    return total_pages


# ─── Core video processing (works for both live and queued requests) ──────────

async def process_video_chunks(bot, chat_id, video_id, title, video_path,
                                user_name, user_id, username, url,
                                duration_seconds, request_id):
    """Process video chunks and send PDFs. Uses bot+chat_id directly."""
    start_time = time.time()

    try:
        chunk_duration_seconds = CHUNK_DURATION_MINUTES * 60
        total_chunks = int(np.ceil(duration_seconds / chunk_duration_seconds))

        if request_id in processing_requests:
            processing_requests[request_id]['title'] = title

        analysis_msg = await bot.send_message(
            chat_id=chat_id,
            text=(
                f"📊 Video Analysis:\n"
                f"🎬 Title: {title}\n"
                f"⏱️ कुल समय: {format_duration(duration_seconds)}\n"
                f"📦 Total Chunks: {total_chunks}\n"
                f"🆔 Request ID: {request_id[:8]}...\n\n"
                f"🔄 Starting to process {total_chunks} chunks..."
            )
        )

        try:
            await bot.forward_message(
                chat_id=CHANNEL_USERNAME,
                from_chat_id=chat_id,
                message_id=analysis_msg.message_id
            )
        except:
            pass

        total_pages_all = 0

        with tempfile.TemporaryDirectory() as temp_folder:
            for chunk_num in range(total_chunks):
                if request_id not in processing_requests:
                    break

                start_time_chunk = chunk_num * chunk_duration_seconds
                end_time_chunk = min((chunk_num + 1) * chunk_duration_seconds, duration_seconds)

                processing_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔄 Processing Part {chunk_num + 1}/{total_chunks}\n"
                        f"📍 Time: {format_duration(start_time_chunk)} - {format_duration(end_time_chunk)}\n"
                        f"🆔 Request: {request_id[:8]}...\n"
                        f"⚙️ Extracting frames for chunk..."
                    )
                )

                try:
                    await bot.forward_message(
                        chat_id=CHANNEL_USERNAME,
                        from_chat_id=chat_id,
                        message_id=processing_msg.message_id
                    )
                except:
                    pass

                def process_chunk():
                    return extract_unique_frames_for_chunk(
                        video_path, temp_folder, start_time_chunk, end_time_chunk, chunk_num,
                        n=FRAME_SKIP_FOR_SSIM_CHECK, ssim_threshold=SSIM_THRESHOLD
                    )

                loop = asyncio.get_event_loop()
                timestamps = await loop.run_in_executor(thread_pool, process_chunk)

                if not timestamps:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=processing_msg.message_id,
                        text=f"⚠️ Part {chunk_num + 1}: कोई unique frames नहीं मिले"
                    )
                    continue

                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=processing_msg.message_id,
                        text=(
                            f"✅ Part {chunk_num + 1}/{total_chunks} - Frames Extracted!\n"
                            f"📍 Time: {format_duration(start_time_chunk)} - {format_duration(end_time_chunk)}\n"
                            f"🆔 Request: {request_id[:8]}...\n"
                            f"📄 Creating PDF... ({len(timestamps)} frames)"
                        )
                    )
                except:
                    pass

                safe_title = sanitize_filename(title)[:50]
                chunk_filename = f"{safe_title}_Part{chunk_num + 1}_of_{total_chunks}_{request_id[:8]}.pdf"
                chunk_pdf_path = os.path.join(temp_folder, chunk_filename)

                def create_pdf():
                    return convert_frames_to_pdf_chunk(temp_folder, chunk_pdf_path, timestamps, chunk_num)

                pages_in_chunk = await loop.run_in_executor(thread_pool, create_pdf)
                total_pages_all += pages_in_chunk

                if pages_in_chunk > 0 and os.path.exists(chunk_pdf_path):
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=processing_msg.message_id,
                            text=(
                                f"✅ Part {chunk_num + 1}/{total_chunks} - PDF Created!\n"
                                f"📄 Pages: {pages_in_chunk}\n"
                                f"📍 Time: {format_duration(start_time_chunk)} - {format_duration(end_time_chunk)}\n"
                                f"🆔 Request: {request_id[:8]}...\n"
                                f"📤 Preparing to send..."
                            )
                        )
                    except:
                        pass

                    chunk_caption = (
                        f"✅ Part {chunk_num + 1}/{total_chunks} Complete!\n\n"
                        f"🎬 Title: {title}\n"
                        f"📄 Pages: {pages_in_chunk}\n"
                        f"⏱️ Time Range: {format_duration(start_time_chunk)} - {format_duration(end_time_chunk)}\n"
                        f"🆔 Request: {request_id[:8]}..."
                    )

                    with open(chunk_pdf_path, 'rb') as pdf_file:
                        pdf_content = pdf_file.read()

                    # Send to channel first
                    try:
                        channel_update = (
                            f"📤 PDF Part Ready!\n\n"
                            f"👤 User: {user_name} (@{username})\n"
                            f"🆔 ID: {user_id}\n"
                            f"🎬 Video: {title}\n"
                            f"📄 Part {chunk_num + 1}/{total_chunks} - {pages_in_chunk} pages\n"
                            f"⏱️ Time: {format_duration(start_time_chunk)}-{format_duration(end_time_chunk)}\n"
                            f"🆔 Request: {request_id[:8]}...\n"
                            f"🔗 URL: {url}"
                        )
                        await bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_update)

                        pdf_stream = io.BytesIO(pdf_content)
                        pdf_stream.name = chunk_filename
                        channel_doc_msg = await bot.send_document(
                            chat_id=CHANNEL_USERNAME,
                            document=pdf_stream,
                            filename=chunk_filename,
                            caption=f"📤 {user_name} का Part {chunk_num + 1}/{total_chunks}"
                        )

                        try:
                            cached_file_id = channel_doc_msg.document.file_id
                            add_to_pdf_cache(video_id, title, chunk_num + 1, total_chunks,
                                             cached_file_id, pages_in_chunk)
                        except Exception as ce:
                            print(f"⚠️ Cache save error: {ce}")

                        print(f"📤 Part {chunk_num + 1}/{total_chunks} sent to channel & user: {user_name}")
                    except Exception as e:
                        print(f"⚠️ Channel send error: {e}")

                    # Send to user
                    try:
                        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                        user_pdf_stream = io.BytesIO(pdf_content)
                        user_pdf_stream.name = chunk_filename
                        await bot.send_document(
                            chat_id=chat_id,
                            document=user_pdf_stream,
                            filename=chunk_filename,
                            caption=chunk_caption
                        )
                        print(f"✅ PDF Part {chunk_num + 1} delivered to user: {user_name}")
                    except Exception as e:
                        print(f"⚠️ User send error: {e}")

                # Cleanup chunk frames
                for frame_file in os.listdir(temp_folder):
                    if frame_file.startswith(f'chunk{chunk_num}_'):
                        try:
                            os.remove(os.path.join(temp_folder, frame_file))
                        except:
                            pass

                # Delete processing message
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=processing_msg.message_id)
                except:
                    pass

        # Final completion message
        total_processing_time = time.time() - start_time
        completion_msg = (
            f"🎉 सभी Parts Complete!\n\n"
            f"🎬 Title: {title}\n"
            f"📊 Total Pages: {total_pages_all}\n"
            f"📦 Total Parts: {total_chunks}\n"
            f"⏱️ Processing Time: {format_duration(total_processing_time)}\n"
            f"🆔 Request: {request_id[:8]}...\n\n"
            f"📞 Contact Owner @LODHIJI27"
        )
        await bot.send_message(chat_id=chat_id, text=completion_msg)

        try:
            channel_completion = (
                f"✅ Complete Video Processing!\n\n"
                f"👤 User: {user_name} (@{username})\n"
                f"🆔 ID: {user_id}\n"
                f"🎬 Video: {title}\n"
                f"📊 Total: {total_pages_all} pages, {total_chunks} parts\n"
                f"⏱️ Time: {format_duration(total_processing_time)}\n"
                f"🆔 Request: {request_id[:8]}...\n"
                f"🔗 URL: {url}"
            )
            asyncio.create_task(bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_completion))
        except:
            pass

        try:
            update_user_stats(user_id, duration_seconds=duration_seconds, pdfs_delta=total_chunks)
        except Exception as se:
            print(f"⚠️ Stats update error: {se}")

    except Exception as e:
        error_msg = f"❌ Processing Error: {str(e)}"
        try:
            await bot.send_message(chat_id=chat_id, text=error_msg)
        except:
            pass
        print(f"❌ Processing error for {user_name}: {e}")

    finally:
        try:
            if os.path.exists(video_path):
                os.remove(video_path)
                print(f"🗑️ Deleted video file: {video_path}")
        except:
            pass


# ─── Queue Worker ──────────────────────────────────────────────────────────────

async def process_queue_item(app, item):
    """Process one item from the queue."""
    user_id = item['user_id']
    chat_id = item['chat_id']
    username = item['username']
    user_name = item['user_name']
    url = item['url']
    video_id = item['video_id']
    queue_id = item['queue_id']

    request_id = None
    try:
        # Remove from queue first so it won't be picked up again
        remove_from_queue(queue_id)

        request_id = start_request(user_id, video_id)
        if request_id in processing_requests:
            processing_requests[request_id]['task'] = asyncio.current_task()

        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚀 {user_name}, आपकी queued request process होना शुरू हो गई!\n"
                f"🔗 URL: {url}\n"
                f"🆔 Request: {request_id[:8]}..."
            )
        )

        # Check duration
        duration_seconds = get_video_duration(video_id)
        max_hours = get_user_max_hours(user_id)
        max_duration_seconds = max_hours * 3600

        if duration_seconds == 0:
            await app.bot.send_message(
                chat_id=chat_id,
                text="❌ Queue से process करते समय video की जानकारी नहीं मिली। कृपया link दोबारा भेजें।"
            )
            return

        if duration_seconds > max_duration_seconds:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Video की limit ({format_duration(duration_seconds)}) से अधिक है। Queue item skip किया।"
            )
            return

        # Download
        title, video_path, actual_duration = await download_video_async(video_id)

        if request_id in processing_requests:
            processing_requests[request_id]['title'] = title

        try:
            channel_msg = (
                f"🔥 Queue से Video Processing Start!\n\n"
                f"👤 User: {user_name} (@{username})\n"
                f"🆔 ID: {user_id}\n"
                f"🎬 Title: {title}\n"
                f"⏱️ Duration: {format_duration(actual_duration)}\n"
                f"🆔 Request: {request_id[:8]}...\n"
                f"🔗 URL: {url}\n"
                f"📋 Queued at: {item.get('queued_at', 'N/A')}"
            )
            asyncio.create_task(app.bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_msg))
        except:
            pass

        await process_video_chunks(
            app.bot, chat_id, video_id, title, video_path,
            user_name, user_id, username, url, actual_duration, request_id
        )

    except Exception as e:
        print(f"❌ Queue processing error for {user_name}: {e}")
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Queue से processing में error: {str(e)}\nकृपया link दोबारा भेजें।"
            )
        except:
            pass
    finally:
        if request_id:
            finish_request(request_id)


async def queue_worker(app):
    """Background worker: processes queued items as server capacity frees up."""
    print("📋 Queue worker started")
    while True:
        try:
            await asyncio.sleep(5)
            q = load_queue()
            if not q:
                continue

            active_count = len(processing_requests)
            if active_count >= MAX_CONCURRENT_TOTAL_REQUESTS:
                continue

            # Process as many queued items as capacity allows
            slots_free = MAX_CONCURRENT_TOTAL_REQUESTS - active_count
            items_to_process = []

            for item in q:
                if slots_free <= 0:
                    break
                user_id = item['user_id']
                can_process, _ = can_process_request(user_id)
                if can_process:
                    items_to_process.append(item)
                    slots_free -= 1

            for item in items_to_process:
                asyncio.create_task(process_queue_item(app, item))
                # Small delay between spawning queue tasks
                await asyncio.sleep(1)

        except Exception as e:
            print(f"⚠️ Queue worker error: {e}")
            await asyncio.sleep(10)


# ─── Telegram Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    user_id = update.effective_user.id
    username = update.effective_user.username or "No username"

    add_user(user_id, username, user_name)

    # ── Group membership check ────────────────────────────────────────────────
    is_member = await check_membership(context.bot, user_id)
    if not is_member:
        await send_join_prompt(context.bot, update.effective_chat.id, user_name)
        return

    welcome_message = (
        f"👋 नमस्ते {user_name}!\n\n"
        f"🎬 YouTube to PDF Bot में आपका स्वागत है!\n\n"
        f"📋 कैसे काम करता है:\n"
        f"1. YouTube video का link भेजें\n"
        f"2. Bot video को 30-30 मिनट के भागों में बांटेगा\n"
        f"3. हर भाग की PDF बनकर तुरंत भेजी जाएगी\n\n"
        f"🚀 सुविधाएं:\n"
        f"• आप एक साथ {MAX_REQUESTS_PER_USER} videos process कर सकते हैं\n"
        f"• Multiple users एक साथ bot use कर सकते हैं\n"
        f"• Automatic queue system — Bot बंद हो तो भी link save होगा!\n"
        f"• Real-time parallel processing\n\n"
        f"🚨 Bot को लिंक के अलावा कोई और मैसेज न करें\n"
        f"📞 Contact Owner - @LODHIJI27\n\n"
        f"बस YouTube link भेजिए! 🚀\n\n"
        f"⚠️ नोट: केवल {MAX_VIDEO_DURATION_HOURS} घंटे तक की videos ही process होंगी"
    )
    await update.message.reply_text(welcome_message)

    try:
        fwd = await update.message.forward(chat_id=CHANNEL_USERNAME)
        try:
            await context.bot.pin_chat_message(
                chat_id=CHANNEL_USERNAME,
                message_id=fwd.message_id,
                disable_notification=True
            )
        except Exception as pe:
            print(f"⚠️ Pin message error: {pe}")
    except Exception as e:
        print(f"⚠️ Start message forward error: {e}")

    try:
        channel_message = (
            f"🆕 नया User Bot को Start किया!\n\n"
            f"👤 Name: {user_name}\n"
            f"🆔 User ID: {user_id}\n"
            f"📝 Username: @{username}\n"
            f"⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await context.bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_message)
    except Exception as e:
        print(f"⚠️ Channel info message error: {e}")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """YouTube URL handle करता है with parallel processing and queue."""
    url = update.message.text.strip()
    user_name = update.effective_user.first_name
    user_id = update.effective_user.id
    username = update.effective_user.username or "No username"
    chat_id = update.effective_chat.id

    add_user(user_id, username, user_name)

    # ── Group membership check ────────────────────────────────────────────────
    is_member = await check_membership(context.bot, user_id)
    if not is_member:
        await send_join_prompt(context.bot, chat_id, user_name)
        return

    # Forward URL to channel
    try:
        await update.message.forward(chat_id=CHANNEL_USERNAME)
        channel_url_info = (
            f"📨 नया Video Link Request!\n\n"
            f"👤 User: {user_name} (@{username})\n"
            f"🆔 User ID: {user_id}\n"
            f"🔗 URL: {url}\n"
            f"⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await context.bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_url_info)
    except Exception as e:
        print(f"⚠️ URL message forward error: {e}")

    # Extract video ID
    video_id = get_video_id(url)
    if not video_id:
        await update.message.reply_text("❌ Invalid YouTube URL! Please send a valid YouTube link.")
        return

    # ── PDF Cache check — serve instantly if already converted ────────────────
    cached_entry = get_cached_pdf(video_id)
    if cached_entry:
        cached_parts = cached_entry.get('parts', [])
        cached_title = cached_entry.get('title', 'Video')
        total_cached = len(cached_parts)
        if total_cached > 0:
            await update.message.reply_text(
                f"⚡ {user_name}, यह video पहले से convert हो चुकी है!\n"
                f"🎬 Title: {cached_title}\n"
                f"📄 Parts: {total_cached}\n"
                f"🚀 Cache से directly भेज रहे हैं..."
            )
            all_sent = True
            for part in cached_parts:
                try:
                    part_caption = (
                        f"✅ Part {part['part_num']}/{part['total_parts']} (Cached)\n"
                        f"🎬 {cached_title}\n"
                        f"📄 Pages: {part.get('pages', '?')}\n"
                        f"⚡ Instantly served from cache!"
                    )
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=part['file_id'],
                        caption=part_caption
                    )
                except Exception as ce:
                    print(f"⚠️ Cache serve error part {part.get('part_num')}: {ce}")
                    all_sent = False
            if all_sent:
                return

    # ── Check if we can process or need to queue ──────────────────────────────
    can_process, reason = can_process_request(user_id)

    if not can_process:
        # Add to queue instead of rejecting
        queue_id = add_to_queue(user_id, chat_id, username, user_name, url, video_id)
        queue_pos = get_queue_position(queue_id)
        queue_size = get_queue_size()

        if reason == "server_full":
            await update.message.reply_text(
                f"📋 {user_name}, server अभी busy है!\n\n"
                f"✅ आपका link queue में add कर दिया गया है।\n"
                f"📊 Queue Position: {queue_pos}/{queue_size}\n"
                f"🔄 Active Requests: {len(processing_requests)}/{MAX_CONCURRENT_TOTAL_REQUESTS}\n\n"
                f"जैसे ही server free होगा, आपकी video automatically process होगी।\n"
                f"🔔 PDF बनने पर आपको यहीं भेज दी जाएगी।"
            )
        elif reason == "user_limit":
            await update.message.reply_text(
                f"📋 {user_name}, आपकी {MAX_REQUESTS_PER_USER} requests पहले से active हैं!\n\n"
                f"✅ आपका link queue में add कर दिया गया है।\n"
                f"📊 Queue Position: {queue_pos}/{queue_size}\n"
                f"🔄 आपकी Active Requests: {user_request_counts.get(user_id, 0)}/{MAX_REQUESTS_PER_USER}\n\n"
                f"जैसे ही कोई request complete होगी, यह automatically process होगी।"
            )
        return

    # ── Normal immediate processing ───────────────────────────────────────────
    await update.message.reply_text(
        f"📥 {user_name}, आपका link receive हो गया!\n"
        f"🔄 Processing शुरू हो रही है...\n"
        f"⚡ Parallel processing enabled!"
    )

    # Check video duration
    duration_seconds = get_video_duration(video_id)

    max_hours = get_user_max_hours(user_id)
    max_duration_seconds = max_hours * 3600
    if user_id == OWNER_ID:
        user_status = "🔑 ADMIN"
    elif max_hours > MAX_VIDEO_DURATION_HOURS:
        user_status = "⭐ GRANTED USER"
    else:
        user_status = "👤 USER"

    if duration_seconds == 0:
        await update.message.reply_text(
            f"❌ Video की जानकारी नहीं मिल सकी!\n\n"
            f"🔍 Possible reasons:\n"
            f"• Video private या deleted हो सकती है\n"
            f"• URL गलत हो सकता है\n"
            f"• Network issue हो सकता है\n\n"
            f"कृपया valid YouTube URL भेजें।"
        )
        return

    if duration_seconds > max_duration_seconds:
        await update.message.reply_text(
            f"❌ Video बहुत लंबी है!\n\n"
            f"⏱️ Video Duration: {format_duration(duration_seconds)}\n"
            f"📏 Your Limit ({user_status}): {max_hours} hours\n\n"
            f"कृपया {max_hours} घंटे से कम की video भेजें।\n"
            f"🔑 Limit बढ़ाने के लिए @LODHIJI27 से contact करें।"
        )
        return

    # Create processing task
    async def process_video_task():
        request_id = None
        try:
            request_id = start_request(user_id, video_id)

            if request_id in processing_requests:
                processing_requests[request_id]['task'] = asyncio.current_task()

            status_msg = await update.message.reply_text(
                f"🔄 Processing शुरू हो रही है...\n"
                f"{user_status} Status: {user_name}\n"
                f"⏱️ Video Duration: {format_duration(duration_seconds)}\n"
                f"📊 Your Active Requests: {user_request_counts.get(user_id, 0)}/{MAX_REQUESTS_PER_USER}\n"
                f"📊 Total Server Load: {len(processing_requests)}/{MAX_CONCURRENT_TOTAL_REQUESTS}\n"
                f"🆔 Request ID: {request_id[:8]}..."
            )

            async def update_progress(percent, speed):
                try:
                    percent_value = float(percent.replace('%', '').strip()) if 'N/A' not in percent else 0
                    bar_length = 20
                    filled_length = int(bar_length * percent_value / 100)
                    bar = '▓' * filled_length + '░' * (bar_length - filled_length)
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text=(
                            f"⬇️ Downloading Video... ✨\n"
                            f"[{bar}] {percent.strip()} - {speed.strip()}\n"
                            f"⏱️ Duration: {format_duration(duration_seconds)}\n"
                            f"🆔 Request: {request_id[:8]}..."
                        )
                    )
                except Exception as e:
                    logger.debug(f"Progress update error: {e}")

            title, video_path, actual_duration = await download_video_async(video_id, update_progress)

            if request_id in processing_requests:
                processing_requests[request_id]['title'] = title

            try:
                channel_msg = (
                    f"🔥 नई Video Processing Start!\n\n"
                    f"👤 User: {user_name} (@{username})\n"
                    f"🆔 ID: {user_id}\n"
                    f"🎬 Title: {title}\n"
                    f"⏱️ Duration: {format_duration(actual_duration)}\n"
                    f"🆔 Request: {request_id[:8]}...\n"
                    f"🔗 URL: {url}\n"
                    f"⏰ Start Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📊 Server Load: {len(processing_requests)}/{MAX_CONCURRENT_TOTAL_REQUESTS}"
                )
                asyncio.create_task(context.bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_msg))
            except:
                pass

            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            except:
                pass

            await process_video_chunks(
                context.bot, chat_id, video_id, title, video_path,
                user_name, user_id, username, url, actual_duration, request_id
            )

        except Exception as e:
            error_message = f"❌ Download Error: {str(e)}"
            await update.message.reply_text(error_message)
            print(f"❌ Download error for {user_name}: {e}")

        finally:
            if request_id:
                finish_request(request_id)

    asyncio.create_task(process_video_task())


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    user_id = update.effective_user.id
    username = update.effective_user.username or "No username"
    message_text = update.message.text or "No text"

    add_user(user_id, username, user_name)

    try:
        await update.message.forward(chat_id=CHANNEL_USERNAME)
        channel_other_info = (
            f"📝 Non-URL Message Received!\n\n"
            f"👤 User: {user_name} (@{username})\n"
            f"🆔 User ID: {user_id}\n"
            f"💬 Message: {message_text[:100]}...\n"
            f"⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await context.bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_other_info)
    except Exception as e:
        print(f"⚠️ Other message forward error: {e}")

    user_requests = user_request_counts.get(user_id, 0)
    queue_size = get_queue_size()

    await update.message.reply_text(
        f"🚨 {user_name}, कृपया केवल YouTube link भेजें!\n\n"
        f"📝 Example:\n"
        f"https://www.youtube.com/watch?v=VIDEO_ID\n"
        f"https://youtu.be/VIDEO_ID\n\n"
        f"📊 Your Status:\n"
        f"• Active Requests: {user_requests}/{MAX_REQUESTS_PER_USER}\n"
        f"• Server Load: {len(processing_requests)}/{MAX_CONCURRENT_TOTAL_REQUESTS}\n"
        f"• Queue Size: {queue_size}\n\n"
        f"⚡ Parallel processing active - आप एक साथ multiple videos भेज सकते हैं!\n\n"
        f"बाकी messages का reply नहीं दिया जाता।"
    )


async def usercount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_users()
    count = len(users)
    await update.message.reply_text(f"👥 Total unique users: {count}")


async def sendexcel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text('❌ Only admin can use this command.')
        return
    try:
        with open('users.xlsx', 'rb') as f:
            await update.message.reply_document(
                document=f,
                filename='users.xlsx',
                caption='👤 All users Excel file (admin only)'
            )
    except Exception as e:
        await update.message.reply_text(f'❌ Error sending file: {e}')


async def queueinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show queue status (admin only)."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text('❌ Only owner can use this command.')
        return
    q = load_queue()
    if not q:
        await update.message.reply_text(
            f"📋 Queue Status\n"
            f"{'═'*28}\n"
            f"✅ Queue खाली है!\n"
            f"🔄 Active Requests: {len(processing_requests)}/{MAX_CONCURRENT_TOTAL_REQUESTS}"
        )
        return

    lines = [
        f"📋 Queue Status",
        f"{'═'*28}",
        f"📦 Queue Size: {len(q)}",
        f"🔄 Active: {len(processing_requests)}/{MAX_CONCURRENT_TOTAL_REQUESTS}",
        f"{'─'*28}",
    ]
    for i, item in enumerate(q[:10]):
        lines.append(
            f"{i+1}. {item['user_name']} (@{item['username']})\n"
            f"   🔗 {item['url'][:50]}...\n"
            f"   ⏰ {item.get('queued_at', 'N/A')}"
        )
    if len(q) > 10:
        lines.append(f"...और {len(q) - 10} requests")

    await update.message.reply_text('\n'.join(lines))


async def clearcache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear the queue (admin only)."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text('❌ Only owner can use this command.')
        return
    old_size = get_queue_size()
    save_queue([])
    await update.message.reply_text(f"✅ Queue clear कर दी गई। {old_size} items remove किए गए।")


# ─── Owner-only: Grant extended video limit ────────────────────────────────────

async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text('❌ Only owner can use this command.')
        return
    if len(context.args) < 2:
        await update.message.reply_text('Usage: /grant <user_id> <hours>\nExample: /grant 123456789 10')
        return
    try:
        target_id = int(context.args[0])
        max_hours = float(context.args[1])
    except ValueError:
        await update.message.reply_text('❌ Invalid arguments. Usage: /grant <user_id> <hours>')
        return
    grant_user_limit(target_id, max_hours)
    await update.message.reply_text(
        f"✅ User {target_id} को {max_hours} घंटे की video limit grant की गई!\n"
        f"🕐 अब वे {max_hours} घंटे तक की videos convert कर सकते हैं।"
    )


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text('❌ Only owner can use this command.')
        return
    if not context.args:
        await update.message.reply_text('Usage: /revoke <user_id>')
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text('❌ Invalid user_id.')
        return
    revoke_user_limit(target_id)
    await update.message.reply_text(f"✅ User {target_id} की extended limit revoke कर दी गई।")


async def userstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller_id = update.effective_user.id
    if context.args:
        if caller_id != OWNER_ID:
            await update.message.reply_text('❌ Only owner can view other users\' stats.')
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text('❌ Invalid user_id.')
            return
    else:
        target_id = caller_id

    users = load_users()
    user = next((u for u in users if u['user_id'] == target_id), None)
    if not user:
        await update.message.reply_text(f'❌ User {target_id} not found in database.')
        return

    granted = load_granted_users().get(str(target_id), {})
    limit_info = f"{granted.get('max_hours', MAX_VIDEO_DURATION_HOURS)} hours (granted)" if granted else f"{MAX_VIDEO_DURATION_HOURS} hours (default)"
    if target_id == OWNER_ID:
        limit_info = f"{ADMIN_MAX_VIDEO_DURATION_HOURS} hours (admin)"

    total_sec = user.get('total_duration_seconds', 0)
    h, rem = divmod(int(total_sec), 3600)
    m, s = divmod(rem, 60)
    total_dur_str = f"{h}h {m}m {s}s"

    msg = (
        f"📊 User Stats\n"
        f"{'═'*30}\n"
        f"👤 Name: {user.get('real_name', 'N/A')}\n"
        f"📝 Username: @{user.get('username', 'N/A')}\n"
        f"🆔 User ID: {target_id}\n"
        f"📅 Joined: {user.get('joined_at', 'N/A')}\n"
        f"{'─'*30}\n"
        f"🎬 Videos Converted: {user.get('videos_converted', 0)}\n"
        f"⏱️ Total Duration: {total_dur_str}\n"
        f"📄 PDFs Generated: {user.get('pdfs_generated', 0)}\n"
        f"📏 Video Limit: {limit_info}\n"
    )
    await update.message.reply_text(msg)


async def allstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text('❌ Only owner can use this command.')
        return
    users = load_users()
    stats = get_global_stats()
    cache = load_pdf_cache()

    total_dur_sec = stats['total_duration_seconds']
    h, rem = divmod(int(total_dur_sec), 3600)
    m = rem // 60
    dur_str = f"{h}h {m}m"

    top_users = sorted(users, key=lambda u: u.get('videos_converted', 0), reverse=True)[:5]
    top_list = '\n'.join(
        f"  {i+1}. {u.get('real_name','?')} (@{u.get('username','?')}) — {u.get('videos_converted',0)} videos"
        for i, u in enumerate(top_users)
    ) or '  (कोई data नहीं)'

    msg = (
        f"📊 Bot Global Stats\n"
        f"{'═'*32}\n"
        f"👥 Total Users: {stats['total_users']}\n"
        f"🎬 Total Videos Converted: {stats['total_videos_converted']}\n"
        f"⏱️ Total Duration Processed: {dur_str}\n"
        f"📄 Total PDFs Generated: {stats['total_pdfs_generated']}\n"
        f"⚡ Cached Videos: {stats['cached_videos']}\n"
        f"🔄 Active Requests Now: {stats['active_requests']}\n"
        f"📋 Queue Size: {stats['queue_size']}\n"
        f"{'─'*32}\n"
        f"🏆 Top 5 Users:\n{top_list}\n"
        f"{'─'*32}\n"
        f"🕐 {stats['generated_at']}"
    )
    await update.message.reply_text(msg)


# ─── HTTP API server (health + JSON data for dashboard) ───────────────────────

def _build_html_dashboard():
    stats = get_global_stats()
    users = load_users()
    cache = load_pdf_cache()
    queue = load_queue()

    top_users = sorted(users, key=lambda u: u.get('videos_converted', 0), reverse=True)[:10]
    users_rows = ''.join(
        f"<tr><td>{i+1}</td><td>{u.get('real_name','?')}</td>"
        f"<td>@{u.get('username','?')}</td>"
        f"<td>{u.get('videos_converted',0)}</td>"
        f"<td>{u.get('pdfs_generated',0)}</td>"
        f"<td>{int(u.get('total_duration_seconds',0)//3600)}h {int((u.get('total_duration_seconds',0)%3600)//60)}m</td>"
        f"<td>{u.get('joined_at','?')}</td></tr>"
        for i, u in enumerate(top_users)
    )

    total_dur_h = int(stats['total_duration_seconds'] // 3600)
    total_dur_m = int((stats['total_duration_seconds'] % 3600) // 60)

    cached_list = ''.join(
        f"<li><b>{v.get('title','?')}</b> — {len(v.get('parts',[]))} part(s), video_id: {vid}</li>"
        for vid, v in list(cache.items())[:20]
    ) or '<li>No cached videos yet</li>'

    queue_rows = ''.join(
        f"<tr><td>{i+1}</td><td>{item.get('user_name','?')}</td>"
        f"<td>@{item.get('username','?')}</td>"
        f"<td style='max-width:200px;overflow:hidden;text-overflow:ellipsis'>{item.get('url','?')}</td>"
        f"<td>{item.get('queued_at','?')}</td></tr>"
        for i, item in enumerate(queue[:20])
    ) or '<tr><td colspan="5" style="text-align:center;color:#6ee7b7">Queue is empty ✅</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YouTube-to-PDF Bot Dashboard</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}}
  header{{background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:2rem;text-align:center}}
  header h1{{font-size:2rem;font-weight:700}}
  header p{{opacity:.8;margin-top:.5rem}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1.5rem;padding:2rem}}
  .card{{background:#1e293b;border-radius:1rem;padding:1.5rem;text-align:center;border:1px solid #334155;transition:transform .2s}}
  .card:hover{{transform:translateY(-4px)}}
  .card .icon{{font-size:2.5rem;margin-bottom:.5rem}}
  .card .value{{font-size:2rem;font-weight:700;color:#818cf8}}
  .card .label{{font-size:.85rem;color:#94a3b8;margin-top:.3rem}}
  .section{{padding:0 2rem 2rem}}
  .section h2{{font-size:1.3rem;font-weight:600;margin-bottom:1rem;padding-bottom:.5rem;border-bottom:2px solid #334155}}
  table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:1rem;overflow:hidden}}
  th{{background:#312e81;padding:.8rem 1rem;text-align:left;font-weight:600;font-size:.85rem;color:#c7d2fe}}
  td{{padding:.7rem 1rem;border-bottom:1px solid #334155;font-size:.85rem}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#263148}}
  ul.cache-list{{list-style:none;background:#1e293b;border-radius:1rem;padding:1rem 1.5rem}}
  ul.cache-list li{{padding:.5rem 0;border-bottom:1px solid #334155;font-size:.85rem}}
  ul.cache-list li:last-child{{border-bottom:none}}
  footer{{text-align:center;padding:1.5rem;color:#475569;font-size:.8rem}}
</style>
</head>
<body>
<header>
  <h1>📊 YouTube-to-PDF Bot Dashboard</h1>
  <p>Real-time analytics • Generated at {stats['generated_at']}</p>
</header>
<div class="grid">
  <div class="card"><div class="icon">👥</div><div class="value">{stats['total_users']}</div><div class="label">Total Users</div></div>
  <div class="card"><div class="icon">🎬</div><div class="value">{stats['total_videos_converted']}</div><div class="label">Videos Converted</div></div>
  <div class="card"><div class="icon">📄</div><div class="value">{stats['total_pdfs_generated']}</div><div class="label">PDFs Generated</div></div>
  <div class="card"><div class="icon">⏱️</div><div class="value">{total_dur_h}h {total_dur_m}m</div><div class="label">Total Duration</div></div>
  <div class="card"><div class="icon">⚡</div><div class="value">{stats['cached_videos']}</div><div class="label">Cached Videos</div></div>
  <div class="card"><div class="icon">🔄</div><div class="value">{stats['active_requests']}</div><div class="label">Active Requests</div></div>
  <div class="card"><div class="icon">📋</div><div class="value">{stats['queue_size']}</div><div class="label">Queued Requests</div></div>
</div>
<div class="section">
  <h2>🏆 Top 10 Users by Videos Converted</h2>
  <table>
    <thead><tr><th>#</th><th>Name</th><th>Username</th><th>Videos</th><th>PDFs</th><th>Duration</th><th>Joined</th></tr></thead>
    <tbody>{users_rows}</tbody>
  </table>
</div>
<div class="section">
  <h2>📋 Current Queue (First 20)</h2>
  <table>
    <thead><tr><th>#</th><th>Name</th><th>Username</th><th>URL</th><th>Queued At</th></tr></thead>
    <tbody>{queue_rows}</tbody>
  </table>
</div>
<div class="section">
  <h2>⚡ Cached Videos (Last 20)</h2>
  <ul class="cache-list">{cached_list}</ul>
</div>
<footer>YouTube-to-PDF Bot • Channel: @alluserpdf • Owner: @LODHIJI27</footer>
</body>
</html>"""
    return html


class _APIHandler(http.server.BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Token")

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self):
        """Returns True if request carries valid admin token."""
        token = self.headers.get("X-Admin-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        return token == ADMIN_TOKEN

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/api/stats':
            self._json_response(get_global_stats())

        elif path == '/api/users':
            self._json_response(load_users())

        elif path == '/api/cache':
            cache = load_pdf_cache()
            summary = {vid: {'title': v.get('title'), 'parts': len(v.get('parts', []))} for vid, v in cache.items()}
            self._json_response(summary)

        elif path == '/api/queue':
            self._json_response(load_queue())

        elif path == '/api/granted':
            self._json_response(load_granted_users())

        elif path == '/dashboard':
            html = _build_html_dashboard()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(html.encode())

        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is running! Visit /dashboard for analytics.")

    def do_POST(self):
        path = self.path.split('?')[0]

        # Auth check for all admin endpoints
        if not self._check_token():
            self._json_response({"ok": False, "error": "Unauthorized"}, status=401)
            return

        # Read request body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            payload = json.loads(body)
        except Exception:
            payload = {}

        if path == '/api/admin/delete_user':
            uid = payload.get("user_id")
            if not uid:
                self._json_response({"ok": False, "error": "user_id required"}); return
            removed = delete_user(int(uid))
            self._json_response({"ok": True, "removed": removed})

        elif path == '/api/admin/grant_user':
            uid = payload.get("user_id")
            hours = payload.get("max_hours")
            if not uid or hours is None:
                self._json_response({"ok": False, "error": "user_id and max_hours required"}); return
            grant_user_limit(int(uid), float(hours))
            self._json_response({"ok": True})

        elif path == '/api/admin/revoke_user':
            uid = payload.get("user_id")
            if not uid:
                self._json_response({"ok": False, "error": "user_id required"}); return
            revoke_user_limit(int(uid))
            self._json_response({"ok": True})

        elif path == '/api/admin/clear_queue':
            old_size = get_queue_size()
            save_queue([])
            self._json_response({"ok": True, "cleared": old_size})

        elif path == '/api/admin/remove_queue_item':
            qid = payload.get("queue_id")
            if not qid:
                self._json_response({"ok": False, "error": "queue_id required"}); return
            remove_from_queue(qid)
            self._json_response({"ok": True})

        elif path == '/api/admin/update_user':
            uid = payload.get("user_id")
            if not uid:
                self._json_response({"ok": False, "error": "user_id required"}); return
            users = load_users()
            updated = False
            for u in users:
                if u['user_id'] == int(uid):
                    for k, v in payload.items():
                        if k not in ('user_id',):
                            u[k] = v
                    updated = True
                    break
            if updated:
                save_users(users)
            self._json_response({"ok": updated})

        else:
            self._json_response({"ok": False, "error": "Unknown endpoint"}, status=404)

    def log_message(self, format, *args):
        pass


def _start_health_server():
    port = int(os.environ.get("PORT", os.environ.get("BOT_HEALTH_PORT", 8082)))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", port), _APIHandler) as httpd:
        print(f"🌐 API server listening on port {port}")
        httpd.serve_forever()


def main():
    try:
        print("=" * 60)
        print("🤖 YOUTUBE TO PDF TELEGRAM BOT")
        print("=" * 60)
        print(f"📅 Started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📺 Channel: {CHANNEL_USERNAME}")
        print(f"👥 Max concurrent requests: {MAX_CONCURRENT_TOTAL_REQUESTS}")
        print(f"👤 Max requests per user: {MAX_REQUESTS_PER_USER}")
        print(f"⏱️ Max video duration: {MAX_VIDEO_DURATION_HOURS} hours")
        print(f"📦 Chunk duration: {CHUNK_DURATION_MINUTES} minutes")
        print(f"⚡ Parallel processing: ENABLED")
        print(f"📋 Queue system: ENABLED")
        print(f"🔧 Thread pool workers: {thread_pool._max_workers}")
        queue_size = get_queue_size()
        if queue_size > 0:
            print(f"📋 Pending queue items on startup: {queue_size}")
        print("=" * 60)

        health_thread = threading.Thread(target=_start_health_server, daemon=True)
        health_thread.start()

        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # Command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("broadcast", broadcast))
        application.add_handler(CommandHandler("usercount", usercount))
        application.add_handler(CommandHandler("sendexcel", sendexcel))
        application.add_handler(CommandHandler("grant", grant_command))
        application.add_handler(CommandHandler("revoke", revoke_command))
        application.add_handler(CommandHandler("userstats", userstats_command))
        application.add_handler(CommandHandler("allstats", allstats_command))
        application.add_handler(CommandHandler("queueinfo", queueinfo))
        application.add_handler(CommandHandler("clearqueue", clearcache_command))

        # Inline button callback handlers
        application.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_membership$"))

        # URL handler
        url_handler = MessageHandler(
            filters.TEXT & (filters.Regex(r'youtube\.com|youtu\.be') | filters.Regex(r'https?://')),
            handle_url
        )
        application.add_handler(url_handler)

        # Other messages handler
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_other_messages))

        # Start queue worker and register bot commands on startup
        async def post_init(app):
            asyncio.create_task(queue_worker(app))
            if get_queue_size() > 0:
                print(f"📋 Queue worker started — will process {get_queue_size()} pending items")

            # Commands visible to all users
            from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
            user_commands = [
                BotCommand("start",     "🤖 Bot शुरू करें / Welcome message"),
                BotCommand("userstats", "📊 अपनी stats देखें"),
                BotCommand("usercount", "👥 Total users count"),
            ]

            # All commands (including admin) for the owner
            all_commands = user_commands + [
                BotCommand("broadcast", "📢 सभी users को message भेजें (Admin)"),
                BotCommand("sendexcel", "📁 Users Excel file download करें (Admin)"),
                BotCommand("grant",     "⭐ User को extended limit दें (Admin)"),
                BotCommand("revoke",    "❌ User की extended limit हटाएं (Admin)"),
                BotCommand("allstats",  "📈 Bot की complete stats (Admin)"),
                BotCommand("queueinfo", "📋 Queue की current status देखें (Admin)"),
                BotCommand("clearqueue","🗑️ Queue खाली करें (Admin)"),
            ]

            try:
                # Set default commands for all users
                await app.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
                # Set full command list for owner's private chat
                await app.bot.set_my_commands(
                    all_commands,
                    scope=BotCommandScopeChat(chat_id=OWNER_ID)
                )
                print("✅ Bot commands registered with Telegram")
            except Exception as e:
                print(f"⚠️ Could not set bot commands: {e}")

        application.post_init = post_init

        print("🚀 Bot initialization complete!")
        print("📱 Waiting for messages...")
        print("=" * 60)

        # drop_pending_updates=False so offline messages are delivered and queued
        application.run_polling(drop_pending_updates=False)

    except KeyboardInterrupt:
        print("\n" + "=" * 60)
        print("⏹️  Bot stopped by user")
        print("=" * 60)
    except Exception as e:
        print(f"❌ Bot startup error: {e}")

if __name__ == '__main__':
    main()
