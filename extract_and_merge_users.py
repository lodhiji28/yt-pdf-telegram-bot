import json
import re

DATA_PATH = 'data.json'
USERS_PATH = 'users.json'

def extract_users_from_data():
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    messages = data.get('messages', [])
    users = {}
    for msg in messages:
        # Only look for messages with user info
        text = msg.get('text', [])
        if not isinstance(text, list):
            continue
        user_id = None
        username = None
        real_name = None
        # Try to extract user_id and username from text blocks
        for i, part in enumerate(text):
            if isinstance(part, dict):
                if part.get('type') == 'phone':
                    user_id = part.get('text')
                if part.get('type') == 'mention':
                    username = part.get('text')
            elif isinstance(part, str):
                # Try to extract real name from preceding text
                # Hindi/English: 'Name: ...', 'नाम: ...', 'उपयोगकर्ता का नाम: ...'
                name_match = re.search(r'(?:Name|नाम|उपयोगकर्ता का नाम)[:：]?\s*([\w\s\-().:]+)', part)
                if name_match:
                    real_name = name_match.group(1).strip()
        # Fallback: try to extract from text_entities if not found
        if not real_name and 'text_entities' in msg:
            for ent in msg['text_entities']:
                if ent.get('type') == 'plain':
                    name_match = re.search(r'(?:Name|नाम|उपयोगकर्ता का नाम)[:：]?\s*([\w\s\-().:]+)', ent.get('text', ''))
                    if name_match:
                        real_name = name_match.group(1).strip()
        # Compose user if we have user_id
        if user_id:
            users[user_id] = {
                'user_id': user_id,
                'username': username or '',
                'real_name': real_name or ''
            }
    return list(users.values())

def merge_users(new_users):
    try:
        with open(USERS_PATH, 'r', encoding='utf-8') as f:
            existing = json.load(f)
    except Exception:
        existing = []
    existing_ids = {str(u['user_id']) for u in existing}
    merged = existing[:]
    for user in new_users:
        if str(user['user_id']) not in existing_ids:
            merged.append(user)
            existing_ids.add(str(user['user_id']))
    with open(USERS_PATH, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"Merged {len(new_users)} new users. Total users: {len(merged)}")

def main():
    new_users = extract_users_from_data()
    merge_users(new_users)

if __name__ == '__main__':
    main()
