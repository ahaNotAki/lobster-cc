"""WeCom relay Lambda — pass-through for message storage.

Routes:
  GET  /callback                — WeCom URL verification (legacy single-agent)
  GET  /callback/{agent_id}     — WeCom URL verification (multi-agent)
  POST /callback                — Store raw encrypted WeCom message (legacy)
  POST /callback/{agent_id}     — Store raw encrypted WeCom message (multi-agent)
  POST /messages/fetch          — Return stored messages (cursor-based pagination)
"""

import base64
import hashlib
import json
import os
import struct
import time
import uuid
from xml.etree import ElementTree

import boto3
from boto3.dynamodb.conditions import Key
from Crypto.Cipher import AES

# --- Config from environment variables ---
TABLE_NAME = os.environ.get("TABLE_NAME", "wecom_relay_messages")
TTL_DAYS = int(os.environ.get("TTL_DAYS", "7"))

# Legacy single-agent config (backwards compatible)
WECOM_TOKEN = os.environ.get("WECOM_TOKEN", "")
WECOM_AES_KEY = os.environ.get("WECOM_AES_KEY", "")

# Multi-agent config: JSON dict of agent_id -> {token, aes_key}
# Example: {"1000002": {"token": "xxx", "aes_key": "yyy"}, "1000003": {...}}
AGENT_CONFIGS = {}
_raw = os.environ.get("AGENT_CONFIGS", "")
if _raw:
    try:
        AGENT_CONFIGS = json.loads(_raw)
    except json.JSONDecodeError:
        pass

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def _get_agent_config(agent_id: str) -> tuple[str, str]:
    """Get (token, aes_key) for an agent. Falls back to legacy env vars."""
    if agent_id and agent_id in AGENT_CONFIGS:
        cfg = AGENT_CONFIGS[agent_id]
        return cfg.get("token", ""), cfg.get("aes_key", "")
    return WECOM_TOKEN, WECOM_AES_KEY


# --- WeCom Crypto (minimal, for GET verification only) ---

def _decode_aes_key(encoding_aes_key: str) -> bytes:
    return base64.b64decode(encoding_aes_key + "=")


def _verify_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    parts = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(parts).encode()).hexdigest()


def _decrypt(aes_key: bytes, encrypted: str) -> bytes:
    cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
    plain = cipher.decrypt(base64.b64decode(encrypted))
    # Remove PKCS#7 padding
    pad_len = plain[-1]
    plain = plain[:-pad_len]
    # Skip 16 random bytes + 4 bytes content length
    content_len = struct.unpack(">I", plain[16:20])[0]
    return plain[20 : 20 + content_len]


# --- Handlers ---

def handle_verify(event, agent_id=""):
    """Handle WeCom callback URL verification (GET)."""
    params = event.get("queryStringParameters") or {}
    msg_signature = params.get("msg_signature", "")
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    echostr = params.get("echostr", "")

    token, aes_key_str = _get_agent_config(agent_id)
    if not token or not aes_key_str:
        return {"statusCode": 500, "body": f"No config for agent {agent_id}"}

    # Verify signature
    expected = _verify_signature(token, timestamp, nonce, echostr)
    if expected != msg_signature:
        return {"statusCode": 403, "body": "Invalid signature"}

    # Decrypt echostr
    aes_key = _decode_aes_key(aes_key_str)
    decrypted = _decrypt(aes_key, echostr)
    return {"statusCode": 200, "body": decrypted.decode("utf-8")}


def handle_callback(event, agent_id=""):
    """Store raw WeCom callback data to DynamoDB (POST)."""
    params = event.get("queryStringParameters") or {}
    body = event.get("body", "")

    # Generate atomic sequence number
    counter_resp = table.update_item(
        Key={"msg_id": "__counter__"},
        UpdateExpression="ADD seq :inc",
        ExpressionAttributeValues={":inc": 1},
        ReturnValues="UPDATED_NEW",
    )
    seq = int(counter_resp["Attributes"]["seq"])

    msg_id = str(uuid.uuid4())
    ttl = int(time.time()) + TTL_DAYS * 86400

    table.put_item(Item={
        "msg_id": msg_id,
        "seq": seq,
        "gsi_pk": "msg",  # Partition key for seq-index GSI
        "agent_id": agent_id,  # Tag with agent for future filtering
        "query_params": {
            "msg_signature": params.get("msg_signature", ""),
            "timestamp": params.get("timestamp", ""),
            "nonce": params.get("nonce", ""),
        },
        "body": body,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": ttl,
    })

    return {"statusCode": 200, "body": "success"}


def handle_fetch(event):
    """Return stored messages after the given cursor (POST)."""
    try:
        payload = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        payload = {}

    cursor = int(payload.get("cursor") or 0)
    limit = min(int(payload.get("limit", 100)), 100)

    # Query messages with seq > cursor using the GSI
    resp = table.query(
        IndexName="seq-index",
        KeyConditionExpression=Key("gsi_pk").eq("msg") & Key("seq").gt(cursor),
        Limit=limit,
        ScanIndexForward=True,
    )

    messages = []
    next_cursor = cursor
    for item in resp.get("Items", []):
        if item["msg_id"] == "__counter__":
            continue
        messages.append({
            "msg_id": item["msg_id"],
            "seq": int(item["seq"]),
            "query_params": item.get("query_params", {}),
            "body": item.get("body", ""),
            "agent_id": item.get("agent_id", ""),
        })
        next_cursor = int(item["seq"])

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "messages": messages,
            "next_cursor": str(next_cursor),
        }),
    }


# --- Lambda entry point ---

def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")

    # Extract agent_id from path: /callback/{agent_id}
    agent_id = ""
    if path.startswith("/callback/"):
        agent_id = path.split("/callback/", 1)[1].split("/")[0]
        # Normalize path for routing
        path = "/callback"

    if path == "/callback" and method == "GET":
        return handle_verify(event, agent_id)
    elif path == "/callback" and method == "POST":
        return handle_callback(event, agent_id)
    elif path == "/messages/fetch" and method == "POST":
        return handle_fetch(event)
    else:
        return {"statusCode": 404, "body": "Not found"}
