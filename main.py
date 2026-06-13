from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Body
from fastapi.middleware.cors import CORSMiddleware
import anthropic
import base64
import os
import json
import traceback
import httpx
from typing import Optional

app = FastAPI(title="PassportFill API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

PASSPORT_PROMPT = """Ты эксперт по чтению паспортов. Извлеки все данные из этого паспорта и верни ТОЛЬКО JSON без лишнего текста.

Верни JSON в таком формате:
{
  "last_name": "ФАМИЛИЯ латиницей",
  "first_name": "ИМЯ латиницей",
  "middle_name": "ОТЧЕСТВО если есть",
  "birth_date": "ДД.ММ.ГГГГ",
  "passport_series": "серия (обычно одна буква N для казахских)",
  "passport_number": "номер паспорта (только цифры)",
  "expire_date": "ДД.ММ.ГГГГ дата окончания",
  "issue_date": "ДД.ММ.ГГГГ дата выдачи паспорта",
  "issued_by": "кем выдан паспорт латинскими буквами (например MVDKAZ или MIA RK)",
  "iin": "ИИН 12 цифр если есть",
  "gender": "M или F",
  "citizenship": "KAZ или RUS и т.д.",
  "tourist_type": "MR для мужчины взрослого, MRS для женщины взрослой, CHD для ребёнка до 12 лет",
  "nationality": "национальность"
}

Если какого-то поля нет — поставь null. Верни ТОЛЬКО JSON, без объяснений."""


async def record_history(profile_id: str, count: int, operator: str = None):
    """Record processing in history table"""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/processing_history",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json"
                },
                json={"profile_id": profile_id, "tourists_count": count, "operator": operator},
                timeout=5.0
            )
    except:
        pass


async def check_and_use_credit(api_key: str) -> dict:
    """Check API key in Supabase and deduct 1 credit"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        # If Supabase not configured, allow all requests (dev mode)
        print("WARNING: Supabase not configured, skipping credit check")
        return {"success": True, "credits_left": 999}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/use_credit",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json"
            },
            json={"p_api_key": api_key},
            timeout=10.0
        )

        if response.status_code != 200:
            print(f"Supabase error: {response.status_code} {response.text}")
            return {"success": False, "error": "Ошибка проверки ключа"}

        return response.json()


@app.get("/")
def root():
    return {"status": "PassportFill API работает", "version": "1.0"}


@app.post("/extract")
async def extract_passport(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API ключ не передан")

    api_key = authorization.replace("Bearer ", "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Неверный API ключ")

    # Check credits in Supabase
    credit_result = await check_and_use_credit(api_key)
    if not credit_result.get("success"):
        error = credit_result.get("error", "Ошибка")
        if "кредит" in error.lower() or "недостаточно" in error.lower():
            raise HTTPException(status_code=402, detail="Недостаточно кредитов. Пополните баланс на passportfill.kz")
        raise HTTPException(status_code=401, detail=f"Неверный API ключ: {error}")

    print(f"Processing file: {file.filename}, type: {file.content_type}, credits_left: {credit_result.get('credits_left')}")

    file_bytes = await file.read()

    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой (макс 10МБ)")

    content_type = file.content_type or "image/jpeg"
    if content_type not in ["image/jpeg", "image/png", "image/webp", "application/pdf"]:
        print(f"Unsupported type: {content_type}, forcing image/jpeg")
        content_type = "image/jpeg"

    file_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print("Calling Claude API...")

        if content_type == "application/pdf":
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64}},
                    {"type": "text", "text": PASSPORT_PROMPT}
                ]}]
            )
        else:
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": file_b64}},
                    {"type": "text", "text": PASSPORT_PROMPT}
                ]}]
            )

        response_text = message.content[0].text.strip()
        print(f"Claude response: {response_text[:200]}")

        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]

        passport_data = json.loads(response_text.strip())
        del file_bytes
        del file_b64

        return {
            "success": True,
            "data": passport_data,
            "credits_left": credit_result.get("credits_left"),
            "total_processed": credit_result.get("total_processed")
        }

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        raise HTTPException(status_code=422, detail="Не удалось прочитать паспорт. Попробуйте более чёткое фото.")
    except anthropic.APIError as e:
        print(f"Anthropic API error: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка AI: {str(e)}")
    except Exception as e:
        print(f"Unexpected error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")


@app.post("/record_history")
async def record_history_endpoint(
    body: dict = Body(...),
    authorization: Optional[str] = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API ключ не передан")
    
    api_key = authorization.replace("Bearer ", "").strip()
    count = body.get("count", 1)
    operator = body.get("operator", "Тур оператор")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"success": True}

    try:
        # Get profile_id by api_key
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/profiles?api_key=eq.{api_key}&select=id",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"
                },
                timeout=5.0
            )
            profiles = resp.json()
            if not profiles:
                return {"success": False}
            
            profile_id = profiles[0]["id"]
            
            # Insert history record
            await client.post(
                f"{SUPABASE_URL}/rest/v1/processing_history",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json"
                },
                json={"profile_id": profile_id, "tourists_count": count, "operator": operator},
                timeout=5.0
            )
        return {"success": True}
    except Exception as e:
        print(f"History error: {e}")
        return {"success": False}


@app.get("/health")
def health():
    return {"ok": True}


# ── Google API Setup ──────────────────────────────────────────
import re
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SPREADSHEET_ID = "1tbQXU_gQwiY_drbO4cWt0-Jg4gK0PY47wGB_1TEUleU"
TEMPLATE_DOC_ID = "1XSjOVbCFwykLVi5AhRV_HtfDK-zo4XMB"

def get_google_services():
    if not GOOGLE_CREDENTIALS_JSON:
        return None, None
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    docs = build("docs", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return docs, sheets, drive


@app.post("/create_contract")
async def create_contract(
    body: dict = Body(...),
    authorization: Optional[str] = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API ключ не передан")

    api_key = authorization.replace("Bearer ", "").strip()
    tour_data = body.get("tour", {})
    tourists = body.get("tourists", [])
    contract_num = body.get("contract_num", "")
    agency = body.get("agency", {})

    if not tourists:
        raise HTTPException(status_code=400, detail="Нет данных туристов")

    try:
        docs_svc, sheets_svc, drive_svc = get_google_services()
        if not docs_svc:
            raise HTTPException(status_code=500, detail="Google API не настроен")

        # 1. Copy template doc
        today = datetime.now().strftime("%d.%m.%Y")
        copy_title = f"Договор №{contract_num} от {today}"
        copied = drive_svc.files().copy(
            fileId=TEMPLATE_DOC_ID,
            body={
                "name": copy_title,
                "parents": ["1_42pvO1Bqh0Gh1lLpCBGDfNzWnnwhI3h"]
            }
        ).execute()
        new_doc_id = copied["id"]

        # Make it accessible
        drive_svc.permissions().create(
            fileId=new_doc_id,
            body={"type": "anyone", "role": "reader"}
        ).execute()

        # 2. Build replacements
        main_tourist = tourists[0]
        tourist_fio = f"{main_tourist.get('last_name', '')} {main_tourist.get('first_name', '')}".strip()
        tourist_iin = main_tourist.get("iin", "")

        # Passport table rows
        passport_rows = ""
        for t in tourists:
            passport_rows += f"{t.get('last_name','')} {t.get('first_name','')} | {t.get('birth_date','')} | {t.get('passport_series','')}{t.get('passport_number','')} | {t.get('expire_date','')} | {t.get('citizenship','KAZ')}\n"

        price_kzt = tour_data.get("price_kzt", "")
        price_currency = tour_data.get("price_currency", "")
        price_currency_val = tour_data.get("price_currency_val", "")

        replacements = {
            "{{НОМЕР_ДОГОВОРА}}": str(contract_num),
            "____": str(contract_num),
            "____________имя_фамилия__ИИН______": f"{tourist_fio}, ИИН {tourist_iin}",
            "___ИМЯ___ФАМИЛИЯ___": tourist_fio,
            "___НОМЕР ДОГОВОРА___": str(contract_num),
            "ДАТА ТЕКУЩАЯ": today,
            "______СУММА____": str(price_kzt),
            "___СУММА ПРОПИСЬЮ___": tour_data.get("price_words", ""),
            "______СУММА_В_ЕВРО/USD___": f"{price_currency_val} {price_currency}",
            "___СТРАНА___ГОРОД___": f"{tour_data.get('country','')} / {tour_data.get('city','')}",
            "___ДАТА НАЧАЛА___": tour_data.get("date_start", ""),
            "___ДАТА ОКОНЧАНИЕ___": tour_data.get("date_end", ""),
            "НАЗВАНИЕ ОТЕЛЯ": tour_data.get("hotel_name", ""),
            "КОЛИЧЕСТВО ЗВЕЗД": tour_data.get("hotel_stars", ""),
            "СТРАНА": tour_data.get("country", ""),
            "ТИП НОМЕРА": tour_data.get("room_type", ""),
            "ТИП ПИТАНИЯ": tour_data.get("meal_type", ""),
            "ДАТА НАЧАЛА ДАТА ОКОНЧАНИЯ": f"{tour_data.get('date_start','')} - {tour_data.get('date_end','')}",
            "КОЛИЧЕСВТО": str(len(tourists)),
            "Количество гостей": str(len(tourists)),
            "ИНФОРМАЦИЯ О СТРАХОВКЕ, СУММА ПОКРЫТИЕ ДАТЫ": tour_data.get("insurance", ""),
            "ИМЯ ФАМИЛИЯ": tourist_fio,
        }

        # Build batch update requests
        requests = []
        for old_text, new_text in replacements.items():
            requests.append({
                "replaceAllText": {
                    "containsText": {"text": old_text, "matchCase": False},
                    "replaceText": new_text
                }
            })

        # Add flight info
        flights = tour_data.get("flights", [])
        for i, flight in enumerate(flights[:2]):
            flight_str = f"{flight.get('route','')} | {flight.get('airline','')} | {flight.get('number','')} | {flight.get('time','')} | {flight.get('date','')} | {flight.get('class','Эконом')}"
            if i == 0:
                requests.append({
                    "replaceAllText": {
                        "containsText": {"text": "ВЫЛЕТ ГОРОД ПРИЛЕТ ГОРОД", "matchCase": False},
                        "replaceText": flight.get("route", "")
                    }
                })
                requests.append({
                    "replaceAllText": {
                        "containsText": {"text": "НАЗВАНИЕ АВИАЛИНИИ", "matchCase": False},
                        "replaceText": flight.get("airline", "")
                    }
                })
                requests.append({
                    "replaceAllText": {
                        "containsText": {"text": "НОМЕР РЕЙСА", "matchCase": False},
                        "replaceText": flight.get("number", "")
                    }
                })

        # Apply replacements
        docs_svc.documents().batchUpdate(
            documentId=new_doc_id,
            body={"requests": requests}
        ).execute()

        # 3. Add row to Google Sheets
        row_data = [
            "",  # # - auto
            today,  # Дата создания
            tour_data.get("date_start", ""),
            tour_data.get("date_end", ""),
            body.get("trip_type", "пакетный тур"),
            "",  # Deadline оплаты
            tour_data.get("country", ""),
            tourist_fio,
            str(len(tourists)),
            agency.get("manager", ""),
            "в работе",
            tourist_iin,
            tour_data.get("operator", ""),
            tour_data.get("booking_num", ""),
            str(price_kzt),
            f"{price_currency_val} {price_currency}",
            "",  # цена туроператора
            "",  # цена туроператора в валюте
            "",  # Доход
            f"N{contract_num} от {today}",
            tour_data.get("hotel_name", ""),
            "",  # чек
            body.get("tourist_phone", ""),
        ]

        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="2025!A:W",
            valueInputOption="USER_ENTERED",
            body={"values": [row_data]}
        ).execute()

        # 4. Return doc link
        doc_link = f"https://docs.google.com/document/d/{new_doc_id}/export?format=docx"
        view_link = f"https://docs.google.com/document/d/{new_doc_id}/edit"

        return {
            "success": True,
            "doc_id": new_doc_id,
            "download_link": doc_link,
            "view_link": view_link,
            "title": copy_title
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Ошибка создания договора: {str(e)}")
