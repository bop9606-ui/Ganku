import os
from datetime import datetime, timedelta
from collections import defaultdict
import pytz  # 用於處理台灣時區
from flask import Flask, request, abort
from dotenv import load_dotenv  # 💡 引入讀取 .env 的套件

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

# 💡 在本地開發時，這行會自動尋找同資料夾底下的 .env 檔案並載入變數
# 部署到 Vercel 後，這行不會影響 Vercel 後台設定好的 Environment Variables
load_dotenv()

app = Flask(__name__)

# =========================================================
# 📌 1. 改由 os.environ 讀取環境變數
#    請確保你的 .env 檔案內名稱為：
#    LINE_CHANNEL_SECRET=你的Secret
#    LINE_CHANNEL_ACCESS_TOKEN=你的Token
# =========================================================
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')

# 設定台灣時區
TAIWAN_TZ = pytz.timezone('Asia/Taipei')

# =========================================================
# 📌 2. 自訂怪物重生時間表（主資料庫：一律用「本名」當 Key）
# =========================================================
BOSS_COOLDOWN = {
    "巴風特": 120,    # 2 小時
    "惡魔宰相": 60,   # 1 小時
    "地獄犬": 45,     # 45 分鐘
}

# =========================================================
# 📌 3. 怪物綽號對照表（綽號一律指向本名）
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

# 📌 4. 用于儲存 Boss 下次出生時間的紀錄 (記憶體儲存，統一存本名)
BOSS_RECORDS = {}


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
    return "LINE Bot is running!"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    reply_text = None
    
    # ---------------------------------------------------------
    # 功能 A: 輸入 /z 紀錄死亡時間 (支援補登時間、支援綽號轉換)
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
            
            # 檢查最後一部分是否為 4 位數純數字 (時間補登)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
                time_str = parts[1]
                hour = int(time_str[:2])
                minute = int(time_str[2:])
                
                if hour < 24 and minute < 60:
                    input_name = parts[0].strip()
                    death_time = now_taiwan.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    is_backfill = True
                    
                    # 跨日智慧判斷
                    if (now_taiwan - death_time).total_seconds() > 43200:
                        if hour < 12 and now_taiwan.hour >= 12:
                            death_time = death_time + timedelta(days=1)
                else:
                    reply_text = "❌ 時間格式錯誤！小時須小於24，分鐘須小於60。例如：1051"
            
            if not reply_text:
                # 綽號轉換：查不到別名就用輸入的原名
                real_name = BOSS_ALIASES.get(input_name.lower(), input_name)
                
                death_time_str = death_time.strftime('%Y-%m-%d %H:%M:%S')
                cooldown_min = BOSS_COOLDOWN.get(real_name, DEFAULT_RESPAWN_MINUTES)
                
                next_spawn_time = death_time + timedelta(minutes=cooldown_min)
                next_spawn_str = next_spawn_time.strftime('%Y-%m-%d %H:%M:%S')
                
                # 統一以本名存入紀錄
                BOSS_RECORDS[real_name] = next_spawn_time
                
                backfill_tag = " (補登)" if is_backfill else ""
                
                reply_text = (
                    f"📝 【BOSS 死亡紀錄】\n"
                    f"👾 怪物名稱：{real_name}\n"
                    f"💀 死亡時間：{death_time_str}{backfill_tag}\n"
                    f"⏳ 重生冷卻：{cooldown_min} 分鐘\n"
                    f"✨ 下次出生：{next_spawn_str}"
                )

    # ---------------------------------------------------------
    # 功能 B: 輸入 kb 顯示王墓看板列表 (新版日期下方條列格式)
    # ---------------------------------------------------------
    elif user_message.lower() == 'kb':
        if not BOSS_RECORDS:
            reply_text = "📋 目前沒有任何 BOSS 的死亡紀錄喔！"
        else:
            date_groups = defaultdict(list)
            
            # 對紀錄按時間先後排序
            sorted_records = sorted(BOSS_RECORDS.items(), key=lambda x: x[1])
            
            for name, spawn_time in sorted_records:
                date_key = spawn_time.strftime('%m/%d')
                time_str = spawn_time.strftime('%H:%M:%S')
                date_groups[date_key].append(f"➔ {name} ({time_str})")
            
            lines = ["📋 【BOSS 下次出生時間表】\n"]
            for date_key, boss_list in date_groups.items():
                lines.append(f"📅 {date_key}")
                lines.append("\n".join(boss_list))
                lines.append("") # 天與天之間空一行
            
            reply_text = "\n".join(lines).strip()

    # ---------------------------------------------------------
    # 發送回覆訊息給 LINE
    # ---------------------------------------------------------
    if reply_text:
        reply_content = TextMessage(text=reply_text)
        with ApiClient(configuration) as api_client:
            line_messaging_api = MessagingApi(api_client)
            line_messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[reply_content]
                )
            )

if __name__ == "__main__":
    app.run(port=5000)