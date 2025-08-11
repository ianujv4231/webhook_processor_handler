import hashlib
import hmac
import json
import logging
import mimetypes
import os
import uuid
from typing import Optional

import boto3
import requests
from boto3.dynamodb.conditions import Key
from datetime import datetime

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

SEARCH_API_URL = "https://fashion-api-public-ba5xh34p.uc.gateway.dev/search/stream"
APP_SECRET = os.environ.get('APP_SECRET')  # from Meta App Dashboard
DATA_MODELLING_TABLE_NAME = os.environ.get('DATA_MODELLING_TABLE_NAME')
dynamodb = boto3.resource('dynamodb')
data_modelling_table = dynamodb.Table(DATA_MODELLING_TABLE_NAME)
VALIDATION_TABLE_NAME = os.environ.get('VALIDATION_TABLE_NAME')
validation_table = dynamodb.Table(VALIDATION_TABLE_NAME)
GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE = os.environ.get('GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE')
INSTAGRAM_DATA_BUCKET_NAME = os.environ.get('INSTAGRAM_DATA_BUCKET_NAME')
s3 = boto3.client('s3')
REGION = os.environ.get('REGION')
WEBHOOK_COMMENTS_TABLE_NAME = os.environ.get('WEBHOOK_COMMENTS_TABLE_NAME')
webhook_comments_table = dynamodb.Table(WEBHOOK_COMMENTS_TABLE_NAME)
CF_ACCOUNT_ID = os.environ.get('CLOUD_FLARE_ACCOUNT_ID')
CF_API_TOKEN = os.environ.get('CLOUD_FLARE_API_KEY')

FB_GRAPH_HOST = os.environ.get('FB_GRAPH_HOST', 'graph.facebook.com')
ENABLE_DM = os.environ.get('ENABLE_DM', 'True').lower() == 'true'



def upload_to_cloudflare_image(local_path: str) -> Optional[str]:
    try:
        with open(local_path, 'rb') as image_file:
            files = {'file': image_file}
            headers = {'Authorization': f'Bearer {CF_API_TOKEN}'}
            upload_url = f'https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/images/v1'
            response = requests.post(upload_url, files=files, headers=headers)

        if response.status_code != 200:
            logger.error(f'Cloudflare upload failed: {response.text}')
            return None

        return response.json()['result']['variants'][0]
    except Exception as e:
        logger.error(f'Cloudflare upload error: {e}')
        return None


def download_and_upload(media_url: str, file_id: str, media_type: str) -> Optional[str]:
    try:
        ext = get_file_extension(media_type)
        local_path = f'/tmp/{file_id}.{ext}'

        # Save original file
        with open(local_path, 'wb') as f:
            f.write(download(media_url))

        # Upload the original file (no WebP conversion)
        cdn_url = upload_to_cloudflare_image(local_path)

        # Clean up
        if os.path.exists(local_path):
            os.remove(local_path)

        return cdn_url
    except Exception as e:
        logger.error(f'Failed to download and upload {file_id}: {e}')
        return None


def download(url):
    r = requests.get(url)
    r.raise_for_status()
    return r.content


def get_file_extension(media_type):
    if media_type == 'IMAGE':
        return 'jpg'
    elif media_type == 'VIDEO':
        return 'mp4'
    return 'bin'


def persist_media_item(media_id, access_token, user_id):
    # get media item from instagram
    # url = f'https://graph.instagram.com/{media_id}'
    url = f'https://graph.facebook.com/v23.0/{media_id}'
    params = {'fields': 'id,media_type,media_url,caption,username,timestamp,permalink,like_count,comments_count', 'access_token': access_token}
    response = requests.get(url, params=params)
    response.raise_for_status()
    media_data = response.json()

    post_urls = []
    look_ids = []
    media_type = media_data['media_type']
    if media_type == 'CAROUSEL_ALBUM':
        children_url = f'https://graph.instagram.com/{media_id}/children'
        child_params = {'fields': 'id,media_url,media_type', 'access_token': access_token}
        child_resp = requests.get(children_url, params=child_params)
        child_resp.raise_for_status()
        for item in child_resp.json().get('data', []):
            ext = get_file_extension(item['media_type'])
            s3_key = f'{user_id}/{item["id"]}.{ext}'
            content = download(item['media_url'])
            content_type = mimetypes.guess_type(s3_key)[0] or 'application/octet-stream'
            s3.put_object(Bucket=INSTAGRAM_DATA_BUCKET_NAME, Key=s3_key, Body=content, ContentType=content_type)
            # public_url = f"https://{INSTAGRAM_DATA_BUCKET_NAME}.s3.{REGION}.amazonaws.com/{s3_key}"
            # post_urls.append(public_url)
            cdn_url = download_and_upload(item['media_url'], f'{user_id}_{item["id"]}', item['media_type'])
            if cdn_url:
                post_urls.append(cdn_url)
                look_ids.append(str(uuid.uuid4()))
    else:
        ext = get_file_extension(media_type)
        s3_key = f'{user_id}/{media_id}.{ext}'
        content = download(media_data['media_url'])
        content_type = mimetypes.guess_type(s3_key)[0] or 'application/octet-stream'
        s3.put_object(Bucket=INSTAGRAM_DATA_BUCKET_NAME, Key=s3_key, Body=content, ContentType=content_type)
        # public_url = f"https://{INSTAGRAM_DATA_BUCKET_NAME}.s3.{REGION}.amazonaws.com/{s3_key}"
        # post_urls.append(public_url)
        cdn_url = download_and_upload(media_data['media_url'], f'{user_id}_{media_id}', media_type)
        if cdn_url:
            post_urls.append(cdn_url)
            look_ids.append(str(uuid.uuid4()))
    post = {
        'id': media_id,
        'poster_id': media_data.get('username', ''),
        'post_id': media_id,
        'post_urls': post_urls,
        'timestamp': media_data.get('timestamp', ''),
        'caption': media_data.get('caption', ''),  # <-- FIXED LINE
        'like_count': media_data.get('like_count', 0),
        'comments_count': media_data.get('comments_count', 0),
        'platform': 'Instagram',
        'type': media_type,
        'user_id': user_id,
        'look_id': look_ids,
    }
    data_modelling_table.put_item(Item=post)


# def handle_comment(creator_id, change):
#     # get access token from validation table using creator_id from GSI
#     secondary_response = validation_table.query(
#         IndexName=GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE, KeyConditionExpression=Key('instagram_user_id').eq(creator_id)
#     )
#     if 'Items' in secondary_response:
#         print('Access token found in validation table')
#         access_token = secondary_response['Items'][0]['access_token']
#         user_id = secondary_response['Items'][0]['user_id']
#     else:
#         print('Access token not found in validation table')
#         return

#     # check the db if the comment is already been replied to
#     # comment_id = change['value']['id']
#     # comment_text = change['value']['text']
#     print('Handling comment')
#     print(change)
#     media_id = change['value']['media']['id']

#     # check if media_id is already in the table
#     data_modelling_response = data_modelling_table.get_item(Key={'id': media_id})
#     if 'Item' in data_modelling_response:
#         print('Media already exists in the table')
#         media_item = data_modelling_response['Item']
#         print(media_item)
#     else:
#         print('Media does not exist in the table')
#         # get access token from validation table using creator_id from GSI
#         # persist media_item in data_modelling_table
#         persist_media_item(media_id, access_token, user_id)

#     # remove the comment for instagram feature

#     # #add the comment to the webhook_comments_table
#     # webhook_comments_table.put_item(Item={'id': comment_id, 'media_id': media_id, 'user_id': user_id,'instagram_user_id': creator_id, 'comment': comment_text})

#     # commented_user_id=change['value']['from']['id']

#     # if commented_user_id == creator_id:
#     #     return

#     # #reply to comment using the instagram graphapi
#     # reply_url = f"https://graph.instagram.com/v22.0/{comment_id}/replies"

#     # reply_params = {
#     #     "message": "Thank you for your comment",
#     #     "access_token": access_token
#     # }
#     # reply_response = requests.post(reply_url, json=reply_params)
#     # reply_response.raise_for_status()
#     # print("reply to comment successful")

#     # #send message to the user using the instagram graphapi
#     # send_message_url = f"https://graph.instagram.com/v22.0/{creator_id}/messages"
#     # send_message_body = {
#     #     "recipient": {"comment_id": comment_id},
#     #     "message":{
#     #         "text": "Thank you for your comment"
#     #     }
#     # }
#     # headers = {
#     #     "Authorization": f"Bearer {access_token}",
#     #     "Content-Type": "application/json"
#     # }
#     # send_message_response = requests.post(send_message_url, json=send_message_body, headers=headers)
#     # send_message_response.raise_for_status()
#     # print("message sent to the user")


# works duplicated
def handle_comment(creator_id, change):
    # Fetch access token from validation table
    secondary_response = validation_table.query(
        IndexName=GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE,
        KeyConditionExpression=Key('instagram_user_id').eq(creator_id)
    )

    if 'Items' not in secondary_response or not secondary_response['Items']:
        logger.warning('❌ Access token not found for creator_id')
        return

    access_token = secondary_response['Items'][0]['access_token']
    user_id = secondary_response['Items'][0]['user_id']

    comment_id = change['value'].get('id')
    comment_text = change['value'].get('text')
    media_id = change['value']['media']['id']
    commented_user_id = change['value']['from']['id']

    # Skip replying to own comment
    if commented_user_id == creator_id:
        logger.info('📌 Skipping self-comment.')
        return

    # 1. Persist media if needed
    media_exists = data_modelling_table.get_item(Key={'id': media_id})
    if 'Item' not in media_exists:
        persist_media_item(media_id, access_token, user_id)

    # 2. Save comment in webhook_comments_table
    webhook_comments_table.put_item(Item={
        'id': comment_id,
        'media_id': media_id,
        'user_id': user_id,
        'instagram_user_id': creator_id,
        'comment': comment_text
    })

    # 3. Reply to the comment
    try:
        reply_url = f"https://graph.facebook.com/v23.0/{comment_id}/replies"
        reply_params = {
            "message": "Thank you for your comment! 💬",
            "access_token": access_token
        }

        # 💬 Log the outgoing request before sending
        logger.debug(f"📤 Replying to comment {comment_id} at URL: {reply_url}")
        logger.debug(f"📦 Payload being sent: {json.dumps(reply_params)}")

        response = requests.post(reply_url, json=reply_params)
        status_code = response.status_code  # ✅ capture status code

        response.raise_for_status()  # Raises an error only if status code is not 2xx

        logger.info(f"✅ Replied to comment {comment_id} successfully with statusCode={status_code}")

    except requests.exceptions.HTTPError as http_err:
        logger.error(f"📉 Instagram API error response: {http_err.response.text}")
        logger.exception("❌ Failed to reply to the comment due to HTTP error.")

    except Exception as e:
        logger.exception("❌ Unexpected error while replying to the comment.")



# from datetime import datetime

# def handle_comment(creator_id, change):
#     # Fetch access token from validation table
#     secondary_response = validation_table.query(
#         IndexName=GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE,
#         KeyConditionExpression=Key('instagram_user_id').eq(creator_id)
#     )

#     if 'Items' not in secondary_response or not secondary_response['Items']:
#         logger.warning('❌ Access token not found for creator_id')
#         return

#     access_token = secondary_response['Items'][0]['access_token']
#     user_id = secondary_response['Items'][0]['user_id']

#     comment_id = change['value'].get('id')
#     comment_text = change['value'].get('text', '')
#     media_id = change['value']['media']['id']
#     commented_user_id = change['value']['from']['id']

#     # 🛑 Skip replying to own comment
#     if commented_user_id == creator_id:
#         logger.info('📌 Skipping self-comment.')
#         return

#     # 🔁 Prevent duplicate replies
#     existing_comment = webhook_comments_table.get_item(Key={'id': comment_id})
#     if 'Item' in existing_comment:
#         logger.info(f"🔁 Comment {comment_id} already processed. Skipping duplicate reply.")
#         return

#     # 🧠 Persist media if not already stored
#     media_exists = data_modelling_table.get_item(Key={'id': media_id})
#     if 'Item' not in media_exists:
#         logger.info(f"🆕 Media {media_id} not found in DB. Persisting...")
#         persist_media_item(media_id, access_token, user_id)
#     else:
#         logger.debug(f"✅ Media {media_id} already exists. Skipping persist.")

#     # 💾 Save the comment to prevent duplicates
#     webhook_comments_table.put_item(Item={
#         'id': comment_id,
#         'media_id': media_id,
#         'user_id': user_id,
#         'instagram_user_id': creator_id,
#         'comment': comment_text,
#         'created_at': datetime.utcnow().isoformat()  # ✅ Timestamp added
#     })

#     # 💬 Reply to the comment
#     try:
#         reply_url = f"https://graph.facebook.com/v17.0/{comment_id}/replies"
#         reply_params = {
#             "message": "Thank you for your comment! 💬",
#             "access_token": access_token
#         }

#         logger.debug(f"📤 Replying to comment {comment_id} at URL: {reply_url}")
#         logger.debug(f"📦 Payload being sent: {json.dumps(reply_params)}")

#         response = requests.post(reply_url, json=reply_params)
#         response.raise_for_status()

#         logger.info(f"✅ Replied to comment {comment_id} successfully.")

#     except requests.exceptions.HTTPError as http_err:
#         logger.error(f"📉 Instagram API HTTP error: {http_err.response.text}")
#         logger.exception("❌ Failed to reply to the comment due to HTTP error.")
#     except Exception as e:
#         logger.exception("❌ Unexpected error while replying to the comment.")




def handle_reaction(creator_id, change):
    print('Handling reaction')
    print(change)



def format_dm_reply(results):
    if not results:
        return "Hmm, I couldn’t find anything matching that. Want to try another style or keyword? 💡"

    # Show up to 3 item titles
    bullets = "\n".join([f"• {item.get('title', 'Untitled')}" for item in results[:3]])

    return f"""✨ I found these for you:\n\n{bullets}\n\nWant more like these? Just reply with a vibe, color, or occasion! 💬"""



DM_TEMPLATE = "I am a bot. Thank you for your message"

# def run_query_search(text):
#     """
#     Function to generate a dynamic reply. Replace this with your actual logic.
#     """
#     return DM_TEMPLATE # Simplified for this example




# def run_query_search(text, session_id="test_session", user_id="demo_user"):
#     try:
#         params = {
#             "session_id": session_id,
#             "user_id": user_id
#         }

#         payload = {
#             "query": text
#         }

#         response = requests.post(SEARCH_API_URL, params=params, json=payload, timeout=30)
#         response.raise_for_status()
#         logger.debug(f"Search API raw response: {response.text}")

#         # 🔍 Debug the raw response content
#         if not response.content:
#             logger.error("Search API response was empty.")
#             return "The search engine didn’t return any results."

#         try:
#             results = response.json()
#         except json.JSONDecodeError as e:
#             logger.error(f"Invalid JSON from Search API: {response.text}")
#             return f"Search failed: API returned invalid response."

#         return json.dumps(results, indent=2)

#     except Exception as e:
#         logger.error(f"Search API failed: {e}")
#         return "Sorry, I'm having trouble searching right now."



# def handle_messages(creator_id, message):
#     print('Handling messages')
#     sender_id = message['sender']['id']
#     if sender_id == creator_id:
#         return

#     text = message['message']['text']
#     print(text)

#     # get access token from validation table using creator_id from GSI
#     secondary_response = validation_table.query(
#         IndexName=GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE, KeyConditionExpression=Key('instagram_user_id').eq(creator_id)
#     )
#     access_token = secondary_response['Items'][0]['access_token']

#     if access_token is None:
#         print('Access token not found in validation table')
#         return

#     # send message to the user using the instagram graphapi
#     send_message_url = f'https://graph.instagram.com/v22.0/{creator_id}/messages'
#     send_message_params = {'recipient': {'id': sender_id}, 'message': {'text': 'I am a bot. Thank you for your message'}}
#     headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
#     send_message_response = requests.post(send_message_url, json=send_message_params, headers=headers)
#     send_message_response.raise_for_status()
#     print('message sent to the user')



def format_dm_reply(results_json):
    """
    Decorates the raw product JSON into a user-friendly format for a DM.
    Handles the nested {"product": {...}} shape and missing brand_name.
    """
    try:
        items = results_json.get("data", {}).get("products", [])
        if not items:
            return "I couldn't find any specific items, but I'm still looking!"

        reply_parts = ["I found a few things you might like! ✨\n"]

        # helper to guess a brand if brand_name is missing
        def guess_brand(p):
            # prefer explicit name if ever added later
            bn = p.get("brand_name")
            if bn: 
                return bn
            # fallback: sometimes APIs include {"brand": {"name": ...}}
            brand = p.get("brand") or {}
            if isinstance(brand, dict) and brand.get("name"):
                return brand["name"]
            # fallback to brand_id or domain from URL
            if p.get("brand_id"):
                return f"Brand {str(p['brand_id'])[:8]}…"  # short id
            url = p.get("product_url") or ""
            try:
                from urllib.parse import urlparse
                netloc = urlparse(url).netloc
                return netloc.replace("www.", "") if netloc else "Unknown brand"
            except Exception:
                return "Unknown brand"

        # only show genuinely relevant nail-polish-ish items
        shown = 0
        for item in items:
            p = item.get("product", {}) if isinstance(item, dict) else {}
            name = p.get("name") or "N/A"
            url  = p.get("product_url") or "#"

            # optional: filter to nail polish results
            name_l = name.lower()
            subcat = (p.get("subcategory") or "").lower()
            item_type = (p.get("item_type") or "").lower()
            if not (
                "nail" in name_l
                or "nail" in subcat
                or "nail" in item_type
            ):
                continue

            brand = guess_brand(p)
            price = p.get("price")
            price_str = f" (${price})" if isinstance(price, (int, float)) else ""

            reply_parts.append(f"- {name} from {brand}{price_str}\n  check it out: {url}")
            shown += 1
            if shown == 3:
                break

        if shown == 0:
            # fallback: no nail-only matches—show top 3 of anything returned
            for item in items[:3]:
                p = item.get("product", {}) if isinstance(item, dict) else {}
                name = p.get("name") or "N/A"
                url  = p.get("product_url") or "#"
                brand = guess_brand(p)
                price = p.get("price")
                price_str = f" (${price})" if isinstance(price, (int, float)) else ""
                reply_parts.append(f"- {name} from {brand}{price_str}\n  check it out: {url}")

        return "\n".join(reply_parts)

    except Exception as e:
        logger.exception(f"Failed to format/decorate the reply: {e}")
        return "I found some results, but had trouble formatting them. Please try again."


def run_query_search(text, session_id="test_session", user_id="demo_user"):
    try:
        params = {"session_id": session_id, "user_id": user_id}
        payload = {"query": text}

        response = requests.post(
            SEARCH_API_URL, 
            params=params, 
            json=payload, 
            timeout=30, 
            stream=True
        )
        response.raise_for_status()

        for line in response.iter_lines():
            if not line:
                continue

            decoded_line = line.decode('utf-8')
            logger.debug("🔄 Received stream chunk: %s", decoded_line)

            if decoded_line.startswith('data: '):
                json_str = decoded_line[len('data: '):]

                if not json_str:
                    continue

                try:
                    data_chunk = json.loads(json_str)

                    # 🔍 NEW: Log full raw chunk for debugging
                    logger.debug("📦 RAW DATA CHUNK:\n%s", json.dumps(data_chunk, indent=2))

                    if data_chunk.get("type") == "product":
                        logger.debug("Found the 'product' data chunk. Formatting for DM.")
                        return format_dm_reply(data_chunk)

                except json.JSONDecodeError:
                    logger.warning(f"Skipping a line that wasn't valid JSON: {json_str}")
                    continue
        
        logger.error("Stream ended without finding any 'product' type data.")
        return "The search finished, but no products were returned."

    except requests.exceptions.RequestException as e:
        logger.error(f"Search API request failed: {e}")
        return "Sorry, I'm having trouble connecting to the search service right now."
    except Exception as e:
        logger.error(f"An unexpected error occurred in run_query_search: {e}")
        return "Something went wrong while searching. Please try again later!"


### recent commented one 
def handle_messages(creator_id, sender_id, message_text):
    logger.info('⏳ Handling incoming Instagram DM')

    # Avoid responding to messages from yourself (business account)
    if sender_id == creator_id:
        logger.info('📌 Skipped self-message.')
        return

    logger.info(f"✅ Creator ID (IG Business Account): {creator_id}")
    logger.info(f"✅ Sender ID (User messaging): {sender_id}")
    logger.info(f"📩 Message text received: {message_text}")

    # --- DynamoDB Lookup ---
    try:
        response = validation_table.query(
            IndexName=GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE,
            KeyConditionExpression=Key('instagram_user_id').eq(creator_id)
        )

        items = response.get('Items')
        if not items:
            logger.error(f"❌ No access token found in DB for creator_id={creator_id}.")
            return

        page_access_token = items[0].get('access_token')
        fb_page_id = items[0].get('fb_page_id')

        if not page_access_token or not fb_page_id:
            logger.error(f"❌ Missing access_token or fb_page_id in DB item for creator_id={creator_id}.")
            return

    except Exception as e:
        logger.exception(f"❌ DynamoDB lookup failed: {e}")
        return

    # --- Send DM via Instagram Graph API ---
    send_message_url = f"https://{FB_GRAPH_HOST}/v17.0/{fb_page_id}/messages"
    formatted_reply = run_query_search(message_text, session_id=str(uuid.uuid4()), user_id=creator_id)
    logger.info(f"✅ Generated DM Reply: {formatted_reply}")

    headers = {
        "Authorization": f"Bearer {page_access_token}",
        "Content-Type": "application/json"
    }
    
    body = {
        "recipient": {"id": sender_id},
        "message": {"text": f"👋 {formatted_reply}"}
    }

    try:
        response = requests.post(send_message_url, headers=headers, json=body, timeout=10)
        response.raise_for_status() 
        logger.info(f"🚀 DM successfully sent to User {sender_id}")
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"❌ HTTP error from Instagram API: {http_err.response.status_code} - {http_err.response.text}")
    except Exception as e:
        logger.exception(f"❌ Unexpected error sending DM: {e}")



# ## carousel
# def handle_messages(creator_id, sender_id, message_text):
#     logger.info('⏳ Handling incoming Instagram DM')

#     # Avoid responding to messages from yourself (business account)
#     if sender_id == creator_id:
#         logger.info('📌 Skipped self-message.')
#         return

#     logger.info(f"✅ Creator ID (IG Business Account): {creator_id}")
#     logger.info(f"✅ Sender ID (User messaging): {sender_id}")
#     logger.info(f"📩 Message text received: {message_text}")

#     # --- DynamoDB Lookup ---
#     try:
#         response = validation_table.query(
#             IndexName=GLOBAL_SECONDARY_INDEX_FOR_VALIDATION_TABLE,
#             KeyConditionExpression=Key('instagram_user_id').eq(creator_id)
#         )

#         items = response.get('Items')
#         if not items:
#             logger.error(f"❌ No access token found in DB for creator_id={creator_id}.")
#             return

#         page_access_token = items[0].get('access_token')
#         fb_page_id = items[0].get('fb_page_id')

#         if not page_access_token or not fb_page_id:
#             logger.error(f"❌ Missing access_token or fb_page_id in DB item for creator_id={creator_id}.")
#             return

#     except Exception as e:
#         logger.exception(f"❌ DynamoDB lookup failed: {e}")
#         return

#     # --- Run the fashion product search ---
#     results_json = run_query_search(message_text, session_id=str(uuid.uuid4()), user_id=creator_id)
#     products = results_json.get("data", {}).get("products", []) if isinstance(results_json, dict) else []
#     logger.debug(f"📦 {len(products)} products returned.")
#     logger.debug(f"📦 First product: {json.dumps(products[0], indent=2)}")

#     # --- Build Instagram message payload ---
#     if not products:
#         logger.warning("🔍 No products found for this query.")
#         fallback_reply = {
#             "recipient": {"id": sender_id},
#             "message": {"text": "Hmm, I couldn’t find anything matching that. Want to try another style or keyword? 💡"}
#         }
#         try:
#             logger.debug("🧪 Carousel payload being sent:\n%s", json.dumps(message_payload, indent=2))  # <--- INSERT THIS
#             response = requests.post(
#                 f"https://{FB_GRAPH_HOST}/v17.0/{fb_page_id}/messages",
#                 headers={
#                     "Authorization": f"Bearer {page_access_token}",
#                     "Content-Type": "application/json"
#                 },
#                 json=fallback_reply,
#                 timeout=10
#             )
#             response.raise_for_status()
#             logger.info("✅ Sent fallback message for no results.")
#         except Exception as e:
#             logger.exception("❌ Failed to send fallback message.")
#         return


#     elements = []
#     for p in products[:10]:
#         try:
#             elements.append({
#                 "title": p.get("name", "Untitled"),
#                 "image_url": p.get("image_urls", [""])[0],
#                 "subtitle": f"{p.get('brand_name', 'Unknown')} · {p.get('category', '')}",
#                 "default_action": {
#                     "type": "web_url",
#                     "url": p.get("product_url", "#"),
#                     "webview_height_ratio": "tall"
#                 },
#                 "buttons": [
#                     {
#                         "type": "web_url",
#                         "url": p.get("product_url", "#"),
#                         "title": f"Shop on {p.get('brand_name', 'Brand')}"
#                     }
#                 ]
#             })
#         except Exception as e:
#             logger.warning(f"⚠️ Skipping malformed product: {e}")



#     message_payload = {
#         "recipient": {"id": sender_id},
#         "message": {
#             "attachment": {
#                 "type": "template",
#                 "payload": {
#                     "template_type": "generic",
#                     "elements": elements
#                 }
#             }
#         }
#     }

#     logger.debug("🧪 Carousel payload being sent:\n%s", json.dumps(message_payload, indent=2))

#     # --- Send the carousel message ---
#     try:
#         response = requests.post(
#             f"https://{FB_GRAPH_HOST}/v17.0/{fb_page_id}/messages",
#             headers={
#                 "Authorization": f"Bearer {page_access_token}",
#                 "Content-Type": "application/json"
#             },
#             json=message_payload,
#             timeout=10 
#         )
#         response.raise_for_status()
#         logger.info(f"🚀 Carousel DM successfully sent to User {sender_id}")
#     except requests.exceptions.HTTPError as http_err:
#         logger.error(f"❌ HTTP error from Instagram API: {http_err.response.status_code} - {http_err.response.text}")
#     except Exception as e:
#         logger.exception(f"❌ Unexpected error sending DM carousel: {e}")





def verify_signature(body: str, header_signature: str) -> bool:
    if header_signature is None:
        return False
    expected_signature = 'sha256=' + hmac.new(APP_SECRET.encode(), msg=body.encode(), digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_signature, header_signature)


# def lambda_handler(event, context):
#     try:
#         for record in event.get('Records', []):
#             body = json.loads(record['body'])
#             print(f"Received body: {body}")
            
#             for entry in body.get('entry', []):
#                 creator_id = entry.get('id')
                
#                 if not creator_id:
#                     logger.warning("No creator_id found in entry; skipping.")
#                     continue
                
#                 messaging_events = entry.get('messaging', [])
#                 for msg_event in messaging_events:
#                     sender_id = msg_event.get('sender', {}).get('id')
#                     message = msg_event.get('message', {})
#                     message_text = message.get('text', '')
#                     is_echo = message.get('is_echo', False)
                    
#                     if not sender_id or not message:
#                         logger.warning("Sender ID or message object missing; skipping.")
#                         continue
                    
#                     if is_echo:
#                         logger.info(f"📌 Skipping echo message from sender_id={sender_id}.")
#                         continue
                    
#                     # Call the message handling function
#                     handle_messages(creator_id, sender_id, message_text)


#             for entry in body.get('entry', []):
#                 creator_id = entry.get('id')
                
#                 for change in entry.get('changes', []):
#                     field = change.get('field')
#                     if field == 'comments':
#                         handle_comment(creator_id, change)





#         return {'statusCode': 200, 'body': 'ok'}

#     except Exception as e:
#         logger.exception(f"Unhandled exception in lambda_handler: {e}")
#         return {'statusCode': 500, 'body': 'Internal server error'}




def lambda_handler(event, context):
    try:
        for record in event.get('Records', []):
            body = json.loads(record['body'])
            logger.info(f"📦 Received body: {json.dumps(body)}")

            for entry in body.get('entry', []):
                creator_id = entry.get('id')

                if not creator_id:
                    logger.warning("No creator_id found in entry; skipping.")
                    continue

                # --- Handle DMs ---
                for msg_event in entry.get('messaging', []):
                    sender_id = msg_event.get('sender', {}).get('id')
                    message = msg_event.get('message', {})
                    message_text = message.get('text', '')
                    is_echo = message.get('is_echo', False)

                    if not sender_id or not message:
                        logger.warning("Sender ID or message object missing; skipping.")
                        continue

                    if is_echo:
                        logger.info(f"📌 Skipping echo message from sender_id={sender_id}.")
                        continue

                    # Handle user message (DM)
                    handle_messages(creator_id, sender_id, message_text)

                # --- Handle Comments ---
                for change in entry.get('changes', []):
                    field = change.get('field')
                    if field == 'comments':
                        handle_comment(creator_id, change)

        return {'statusCode': 200, 'body': 'ok'}

    except Exception as e:
        logger.exception(f"❌ Unhandled exception in lambda_handler: {e}")
        return {'statusCode': 500, 'body': 'Internal server error'}




