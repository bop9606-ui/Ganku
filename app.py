import os
from datetime import datetime, timedelta
from collections import defaultdict
import pytz  # 用於處理台灣時區
from flask import Flask, request, abort
from dotenv import load_dotenv
from upstash_redis import Redis  # 💡 引入 Upstash Redis 套件

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
# 📌 1. 環境變數讀取 (已加上空字串防錯)
# =========================================================
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET', '')
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN', '')

# 💡 初始化雲端 Redis 資料庫（會自動讀取環境變數中的 URL 與 TOKEN）
redis = Redis.from_env()

# 設定台灣時區
TAIWAN_TZ = pytz.timezone('Asia/Taipei')

# =========================================================
# 📌 2. 自訂怪物重生時間表
# =========================================================
BOSS_COOLDOWN = {
    "巴風特": 120,    # 2 小時
    "惡魔宰相": 60,   # 1 小時
    "地獄犬": 45,     # 45 分鐘
}

# =========================================================
# 📌 3. 怪物綽號對照表
# =========================================================
BOSS_ALIASES = {
    "山羊": "巴風特",
    "小巴": "巴風特",
    "baphomet": "巴風特",
    "宰相": "惡魔宰相",
    "三頭犬": "地獄犬",
    "dog": "地獄犬"
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
    return "LINE Bot is running with Redis!"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    reply_text = None
    
    # ---------------------------------------------------------
    # 功能 A: 輸入 /z 紀錄死亡時間 (資料寫入 Redis)
    # ---------------------------------------------------------
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
                hour = int(time_str[:2])
                minute = int(time_str[2:])
                
                if hour < 24 and minute < 60:
                    input_name = parts[0].strip()
                    death_time = now_taiwan.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    is_backfill = True
                    
                    if (now_taiwan - death_time).total_seconds() > 43200:
                        if hour < 12 and now_taiwan.hour >= 12:
                            death_time = death_time + timedelta(days=1)
                else:
                    reply_text = "❌ 時間格式錯誤！小時須小於24，分鐘須小於60。例如：1051"
            
            if not reply_text:
                real_name = BOSS_ALIASES.get(input_name.lower(), input_name)
                
                death_time_str = death_time.strftime('%Y-%m-%d %H:%M:%S')
                cooldown_min = BOSS_COOLDOWN.get(real_name, DEFAULT_RESPAWN_MINUTES)
                
                next_spawn_time = death_time + timedelta(minutes=cooldown_min)
                next_spawn_str = next_spawn_time.strftime('%Y-%m-%d %H:%M:%S')
                
                # 💡 核心修改：將下次出生時間（時間戳記）存入 Redis 雜湊表
                # 欄位名稱固定為 'boss_timer'，Key 是怪物本名，Value 是 ISO 時間字串
                redis.hset('boss_timer', real_name, next_spawn_time.isoformat())
                
                backfill_tag = " (補登)" if is_backfill else ""
                
                reply_text = (
                    f"📝 【BOSS 死亡紀錄】\n"
                    f"👾 怪物名稱：{real_name}\n"
                    f"💀 死亡時間：{death_time_str}{backfill_tag}\n"
                    f"⏳ 重生冷卻：{cooldown_min} 分鐘\n"
                    f"✨ 下次出生：{next_spawn_str}"
                )

    # ---------------------------------------------------------
    # 功能 B: 輸入 kb 顯示王墓看板列表 (從 Redis 讀取資料)
    # ---------------------------------------------------------
    elif user_message.lower() == 'kb':
        # 💡 核心修改：自 Redis 撈出所有 Boss 紀錄
        all_records = redis.hgetall('boss_timer')
        
        if not all_records:
            reply_text = "📋 目前沒有任何 BOSS 的死亡紀錄喔！"
        else:
            date_groups = defaultdict(list)
            boss_list_to_sort = []
            
            # 解析從 Redis 拿到的資料
            for boss_name, time_iso_str in all_records.items():
                # 從 ISO 字串還原成 datetime 物件
                spawn_time = datetime.fromisoformat(time_iso_str)
                
                # 💡 自動過期機制：如果這隻王已經出生超過 24 小時，就不要顯示在看板上（選用，可不加）
                if datetime.now(TAIWAN_TZ) - spawn_time > timedelta(hours=24):
                    redis.hdel('boss_timer', boss_name) # 順便從資料庫刪除舊資料
                    continue
                    
                boss_list_to_sort.append((boss_name, spawn_time))
            
            if not boss_list_to_sort:
                reply_text = "📋 目前沒有任何 BOSS 的死亡紀錄喔！"
            else:
                # 對紀錄按時間先後排序
                sorted_records = sorted(boss_list_to_sort, key=lambda x: x[1])
                
                for name, spawn_time in sorted_records:
                    date_key = spawn_time.strftime('%m/%d')
                    time_str = spawn_time.strftime('%H:%M:%S')
                    date_groups[date_key].append(f"➔ {name} ({time_str})")
                
                lines = ["📋 【BOSS 下次出生時間表】\n"]
                for date_key, boss_list in date_groups.items():
                    lines.append(f"📅 {date_key}")
                    lines.append("\n".join(boss_list))
                    lines.append("")
                
                reply_text = "\n".join(lines).strip()

    # 發送回覆訊息給 LINE
    if reply_text:
        reply_content = TextMessage(text=reply_text)
        with ApiClient(configuration) as api_client:
            line_messaging_api = MessagingApi(api_client)
            line_messaging_api.reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_content])
            )

if __name__ == "__main__":
    app.run(port=5000)