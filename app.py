import os
from datetime import datetime, timedelta
from collections import defaultdict
import pytz  # 用於處理台灣時區
from flask import Flask, request, abort
from dotenv import load_dotenv
from upstash_redis import Redis  # 初始化雲端 Redis

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

load_dotenv()

app = Flask(__name__)

# =========================================================
# 📌 1. 環境變數與資料庫初始化
# =========================================================
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET', '')
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN', '')

redis = Redis.from_env()
TAIWAN_TZ = pytz.timezone('Asia/Taipei')

# 💡 【自訂解鎖金鑰】請確認這裡的密碼是你想設定的內容！
UNLOCK_PASSWORD = "龍哥罩虎爺" 

# =========================================================
# 📌 2. BOSS 設定 (25 隻完整清單、週期與綽號)
# =========================================================
BOSS_COOLDOWN = {
    # --- 240 分鐘系列 (4 小時) ---
    "庫約": 240,
    "殺戮者": 240,
    "蜥蜴王": 240,
    "飛龍1": 240,
    "飛龍2": 240,
    "飛龍3": 240,
    "飛龍4": 240,
    "屠殺者莫莉": 240,
    "海賊德雷克": 240,
    "巴爾博薩夫人": 240,
    "卡司特王": 240,
    "黑蛇騎士團麥肯": 240,
    "尼羅德": 240,
    "黑虎": 240,
    "大腳": 240,
    "奧杜亞": 240,
    
    # --- 480 分鐘系列 (8 小時) ---
    "狼王": 480,
    "頭目": 480,
    "蛇女": 480,
    "烏勒庫斯": 480,
    "奈克偌斯": 480,
    "克洛林": 480,
    "巨鱷": 480,
    
    # --- 720 分鐘系列 (12 小時) ---
    "巨人": 720,
    "四色": 720,
}

BOSS_ALIASES = {
    "胖子": "殺戮者",
    "ce": "蜥蜴王",
    "1": "飛龍1",
    "2": "飛龍2",
    "3": "飛龍3",
    "4": "飛龍4",
    "87": "屠殺者莫莉", "茉莉": "屠殺者莫莉",
    "海賊": "海賊德雷克",
    "夫人": "巴爾博薩夫人",
    "卡王": "卡司特王",
    "麥肯": "黑蛇騎士團麥肯",
    "尼": "尼羅德",
    "虎": "黑虎",
    "大腳": "大腳",
    "奧": "奧杜亞",
    "狼": "狼王",
    "頭目": "頭目",
    "蛇": "蛇女",
    "烏": "烏勒庫斯",
    "奈": "奈克偌斯",
    "克": "克洛林",
    "鱷魚": "巨鱷",
    "巨人": "巨人",
    "四色": "四色", "4c": "四色",
}

DEFAULT_RESPAWN_MINUTES = 60  

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/", methods=['GET'])
def home():
    return "LINE Bot is running with Redis & Group Lock!"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    source_type = event.source.type
    user_message = event.message.text.strip()
    reply_text = None
    
    # ---------------------------------------------------------
    # 防護機制 A：如果是個人私訊，直接封殺不回應
    # ---------------------------------------------------------
    if source_type != 'group':
        return

    current_group_id = str(event.source.group_id)  # 強制轉成純字串，避免格式錯亂

    # ---------------------------------------------------------
    # 防護機制 B：解鎖功能指令 (例如輸入: /unlock 龍哥罩虎爺)
    # ---------------------------------------------------------
    if user_message.startswith('/unlock '):
        input_pwd = user_message[8:].strip()
        if input_pwd == UNLOCK_PASSWORD:
            redis.sadd('allowed_groups', current_group_id)
            reply_text = f"🔓 【系統通知】此群組已成功解鎖！\nID: {current_group_id}"
        else:
            reply_text = f"❌ 認證失敗：密碼錯誤。\n您輸入的密碼是：[{input_pwd}]"
        
        send_reply(event, reply_text)
        return  # 攔截所有解鎖訊息

    # ---------------------------------------------------------
    # 防護機制 C：檢查目前群組是否已解鎖
    # ---------------------------------------------------------
    is_allowed = redis.sismember('allowed_groups', current_group_id)
    
    if is_allowed == 1 or is_allowed is True:
        is_allowed = True
    else:
        is_allowed = False

    if not is_allowed:
        if user_message.lower().startswith('z ') or user_message.lower() == 'kb':
            reply_text = f"🔒 本群組尚未授權啟用。\n請聯繫管理員輸入解鎖指令。\n當前群組ID: {current_group_id}"
            send_reply(event, reply_text)
        return  # 只要沒解鎖一律在此攔截切斷

    # =========================================================
    # ➔ 以下為王墓功能 (包含自動輪空累加功能)
    # =========================================================
    
    # 功能 A: 輸入 z 紀錄死亡時間 (新增：未來時間防呆、無此 BOSS、格式錯誤防呆)
    if user_message.lower().startswith('z '):
        raw_content = user_message[2:].strip()
        if not raw_content:
            reply_text = "❌ 請輸入正確格式，例如：\nz 巴風特\nz 巴風特 1051"
        else:
            now_taiwan = datetime.now(TAIWAN_TZ)
            parts = raw_content.rsplit(maxsplit=1)
            death_time = now_taiwan
            input_name = raw_content
            is_backfill = False
            format_error = False
            
            # 1. 嘗試解析補登時間格式 (4碼數字)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
                time_str = parts[1]
                hour, minute = int(time_str[:2]), int(time_str[2:])
                if hour < 24 and minute < 60:
                    input_name = parts[0].strip()
                    death_time = now_taiwan.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    is_backfill = True
                    # 處理跨日邏輯
                    if (now_taiwan - death_time).total_seconds() > 43200:
                        if hour < 12 and now_taiwan.hour >= 12:
                            death_time = death_time + timedelta(days=1)
                else:
                    reply_text = "❌ 時間格式錯誤（時分超出範圍）！"
                    format_error = True
            
            # 💡 2. 未來時間防呆檢查 (死亡時間不能比當下的台灣時間還晚)
            if not format_error and is_backfill:
                # 容許 60 秒內的些微系統時間差
                if death_time > now_taiwan + timedelta(seconds=60):
                    reply_text = f"❌ 登記失敗：死亡時間 [{death_time.strftime('%H:%M')}] 不能超過當前未來時間！"
                    format_error = True
            
            # 3. 嚴格檢查王名是否存在
            if not format_error:
                real_name = BOSS_ALIASES.get(input_name.lower(), input_name)
                
                # 如果查出來的真實名字不在 BOSS_COOLDOWN 字典裡，代表是非名單上的王或打錯字
                if real_name not in BOSS_COOLDOWN:
                    reply_text = f"❌ 找不到怪物【{input_name}】。\n請確認名稱是否正確，或此王非名單內 BOSS！"
                else:
                    # ✅ 只有格式正確、非未來時間、且是名單內的王，才進行計算並寫入 Redis 紀錄
                    cooldown_min = BOSS_COOLDOWN[real_name]
                    next_spawn_time = death_time + timedelta(minutes=cooldown_min)
                    
                    redis_key = f"boss_timer:{current_group_id}"
                    redis.hset(redis_key, real_name, next_spawn_time.isoformat())
                    
                    # 破解時間與日期超連結
                    death_time_str = death_time.strftime('%H:\u200b%M:\u200b%S')
                    next_spawn_str = next_spawn_time.strftime('%H:\u200b%M:\u200b%S')
                    next_spawn_date = next_spawn_time.strftime('%m/\u200b%d')
                    
                    backfill_tag = "(補登)" if is_backfill else ""
                    
                    reply_text = (
                        f"👾[怪物名稱] {real_name}({cooldown_min}分){backfill_tag}\n"
                        f"💀[死亡紀錄] {death_time_str}\n"
                        f"✨[下次出生] {next_spawn_date} [{next_spawn_str}]"
                    )

    # 功能 B: 輸入 kb 顯示王墓看板 (消日期時間超連結)
    elif user_message.lower() == 'kb':
        redis_key = f"boss_timer:{current_group_id}"
        all_records = redis.hgetall(redis_key)
        
        if not all_records:
            reply_text = "📋 目前沒有任何 BOSS 的死亡紀錄喔！"
        else:
            date_groups = defaultdict(list)
            boss_list_to_sort = []
            now_taiwan = datetime.now(TAIWAN_TZ)
            
            for boss_name, time_iso_str in all_records.items():
                spawn_time = datetime.fromisoformat(time_iso_str)
                
                # 1. 移除超過 7 天的舊紀錄
                if now_taiwan - spawn_time > timedelta(days=7):
                    redis.hdel(redis_key, boss_name)
                    continue
                
                # 2. 自動累加輪空次數機制
                real_name = BOSS_ALIASES.get(boss_name.lower(), boss_name)
                cooldown_min = BOSS_COOLDOWN.get(real_name, DEFAULT_RESPAWN_MINUTES)
                
                skip_count = 0
                while now_taiwan > spawn_time:
                    spawn_time = spawn_time + timedelta(minutes=cooldown_min)
                    skip_count += 1
                
                if skip_count > 0:
                    redis.hset(redis_key, boss_name, spawn_time.isoformat())
                    display_name = f"{boss_name}(輪空+{skip_count})"
                else:
                    display_name = boss_name
                
                boss_list_to_sort.append((display_name, spawn_time))
            
            if not boss_list_to_sort:
                reply_text = "📋 目前沒有任何 BOSS 的死亡紀錄喔！"
            else:
                sorted_records = sorted(boss_list_to_sort, key=lambda x: x[1])
                for name, spawn_time in sorted_records:
                    date_key = spawn_time.strftime('%m/\u200b%d')
                    time_str = spawn_time.strftime('%H:\u200b%M:\u200b%S')
                    
                    date_groups[date_key].append(f"➔[{time_str}]{name}")
                
                lines = ["📋 【BOSS 下次出生時間表】\n"]
                for date_key, boss_list in date_groups.items():
                    lines.append(f"📅 {date_key}")
                    lines.append("\n".join(boss_list))
                    lines.append("")
                reply_text = "\n".join(lines).strip()

    if reply_text:
        send_reply(event, reply_text)

# 發送 LINE 訊息的副程式
def send_reply(event, text):
    reply_content = TextMessage(text=text)
    with ApiClient(configuration) as api_client:
        line_messaging_api = MessagingApi(api_client)
        line_messaging_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_content])
        )

if __name__ == "__main__":
    app.run(port=5000)