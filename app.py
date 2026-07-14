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
# 📌 2. BOSS 設定 (日後要新增 BOSS 改這裡即可)
# =========================================================
BOSS_COOLDOWN = {
    "巴風特": 120,    # 2 小時
    "惡魔宰相": 60,   # 1 小時
    "地獄犬": 45,     # 45 分鐘
}

BOSS_ALIASES = {
    "山羊": "巴風特", "小巴": "巴風特", "baphomet": "巴風特",
    "宰相": "惡魔宰相", "三頭犬": "地獄犬", "dog": "地獄犬"
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
            # 將群組 ID 存入 Redis 的集合 (Set) 中
            redis.sadd('allowed_groups', current_group_id)
            reply_text = f"🔓 【系統通知】此群組已成功解鎖！\nID: {current_group_id}"
        else:
            reply_text = f"❌ 認證失敗：密碼錯誤。\n您輸入的密碼是：[{input_pwd}]"
        
        send_reply(event, reply_text)
        return  # 攔截所有解鎖訊息

    # ---------------------------------------------------------
    # 防護機制 C：檢查目前群組是否已解鎖
    # ---------------------------------------------------------
    # 檢查目前的 group_id 是否存在於 Redis 的 'allowed_groups' 集合中
    is_allowed = redis.sismember('allowed_groups', current_group_id)
    
    # 處理 Redis 回傳值可能是數字 1/0 或 布林值 的相容性檢查
    if is_allowed == 1 or is_allowed is True:
        is_allowed = True
    else:
        is_allowed = False

    if not is_allowed:
        # ⚠️ 若群組未解鎖，當收到關鍵字指令時，提醒使用者要解鎖
        if user_message.lower().startswith('/z ') or user_message.lower() == 'kb':
            reply_text = f"🔒 本群組尚未授權啟用。\n請聯繫管理員輸入解鎖指令。\n當前群組ID: {current_group_id}"
            send_reply(event, reply_text)
        return  # 只要沒解鎖一律在此攔截切斷

    # =========================================================
    # ➔ 以下為原本的王墓功能 (只有通過檢查的群組才能執行到這)
    # =========================================================
    
    # 功能 A: 輸入 /z 紀錄死亡時間
    if user_message.lower().startswith('/z '):
        raw_content = user_message[3:].strip()
        if not raw_content:
            reply_text = "❌ 請輸入正確格式，例如：\n/z 巴風特\n/z 巴風特 1051"
        else:
            now_taiwan = datetime.now(TAIWAN_TZ)
            parts = raw_content.rsplit(maxsplit=1)
            death_time = now_taiwan
            input_name = raw_content
            is_backfill = False
            
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
                time_str = parts[1]
                hour, minute = int(time_str[:2]), int(time_str[2:])
                if hour < 24 and minute < 60:
                    input_name = parts[0].strip()
                    death_time = now_taiwan.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    is_backfill = True
                    if (now_taiwan - death_time).total_seconds() > 43200:
                        if hour < 12 and now_taiwan.hour >= 12:
                            death_time = death_time + timedelta(days=1)
                else:
                    reply_text = "❌ 時間格式錯誤！"
            
            if not reply_text:
                real_name = BOSS_ALIASES.get(input_name.lower(), input_name)
                cooldown_min = BOSS_COOLDOWN.get(real_name, DEFAULT_RESPAWN_MINUTES)
                next_spawn_time = death_time + timedelta(minutes=cooldown_min)
                
                # Redis 鍵名加上群組 ID，實現多群組數據獨立隔離
                redis_key = f"boss_timer:{current_group_id}"
                redis.hset(redis_key, real_name, next_spawn_time.isoformat())
                
                backfill_tag = " (補登)" if is_backfill else ""
                reply_text = (
                    f"📝 【BOSS 死亡紀錄】\n"
                    f"👾 怪物名稱：{real_name}\n"
                    f"💀 死亡時間：{death_time.strftime('%Y-%m-%d %H:%M:%S')}{backfill_tag}\n"
                    f"⏳ 重生冷卻：{cooldown_min} 分鐘\n"
                    f"✨ 下次出生：{next_spawn_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )

    # 功能 B: 輸入 kb 顯示王墓看板 (新增：逾時自動加算 CD 且統計輪空次數機制)
    elif user_message.lower() == 'kb':
        redis_key = f"boss_timer:{current_group_id}"
        all_records = redis.hgetall(redis_key)
        
        if not all_records:
            reply_text = "📋 目前沒有任何 BOSS 的死亡紀錄喔！"
        else:
            date_groups = defaultdict(list)
            boss_list_to_sort = []
            now_taiwan = datetime.now(TAIWAN_TZ)
            has_updates = False  # 標記是否有王被自動順延
            
            for boss_name, time_iso_str in all_records.items():
                spawn_time = datetime.fromisoformat(time_iso_str)
                
                # 1. 安全防護：如果是超過 24 小時的古老紀錄，直接清除
                if now_taiwan - spawn_time > timedelta(hours=24):
                    redis.hdel(redis_key, boss_name)
                    continue
                
                # 💡 2. 核心機制：計算輪空次數
                real_name = BOSS_ALIASES.get(boss_name.lower(), boss_name)
                cooldown_min = BOSS_COOLDOWN.get(real_name, DEFAULT_RESPAWN_MINUTES)
                
                skip_count = 0  # 每一隻王獨立計算輪空次數
                while now_taiwan > spawn_time:
                    spawn_time = spawn_time + timedelta(minutes=cooldown_min)
                    skip_count += 1
                    has_updates = True
                
                # 如果有發生時間變更，把新時間存回 Redis 覆蓋
                if skip_count > 0:
                    redis.hset(redis_key, boss_name, spawn_time.isoformat())
                    # 把這隻王的顯示名稱後面加上 (輪空+次數)
                    display_name = f"{boss_name} (輪空+{skip_count})"
                else:
                    display_name = boss_name
                
                boss_list_to_sort.append((display_name, spawn_time))
            
           if not boss_list_to_sort:
                reply_text = "📋 目前沒有任何 BOSS 的死亡紀錄喔！"
            else:
                # 依據更新後的時間由近到遠排序
                sorted_records = sorted(boss_list_to_sort, key=lambda x: x[1])
                for name, spawn_time in sorted_records:
                    date_key = spawn_time.strftime('%m/%d')
                    
                    # 💡 1. 破解超連結：一樣加入零寬度隱形字元
                    time_str = spawn_time.strftime('%H:\u200b%M:\u200b%S')
                    
                    # 💡 2. 緊湊排版優化：
                    # 原本是：➔ {name} ({time_str}) 
                    # 改成：➔ [{time_str}] {name}  (把時間拉到前面，且拿掉中間多餘空格)
                    # 這樣就算名字太長換行，時間也絕對會在第一眼看清楚！
                    date_groups[date_key].append(f"➔ [{time_str}]{name}")
                
                lines = ["📋 【BOSS 下次出生時間表】\n"]
                for date_key, boss_list in date_groups.items():
                    lines.append(f"📅 {date_key}")
                    lines.append("\n".join(boss_list))
                    lines.append("")
                reply_text = "\n".join(lines).strip()

    if reply_text:
        send_reply(event, reply_text)

# 💡 抽出來的發送訊息副程式
def send_reply(event, text):
    reply_content = TextMessage(text=text)
    with ApiClient(configuration) as api_client:
        line_messaging_api = MessagingApi(api_client)
        line_messaging_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_content])
        )

if __name__ == "__main__":
    app.run(port=5000)