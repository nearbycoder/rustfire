use axum::{
    Json, Router,
    body::{Body, Bytes, to_bytes},
    extract::{
        ConnectInfo, Form, FromRequest, Multipart, OriginalUri, Path, Query, RawForm, Request,
        State, WebSocketUpgrade,
        ws::{Message as WsMessage, WebSocket},
    },
    http::{HeaderMap, Method, StatusCode, header},
    middleware::Next,
    response::{Html, IntoResponse, Redirect, Response},
    routing::{delete, get, patch, post},
};
use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE, URL_SAFE_NO_PAD},
};
use bcrypt::{DEFAULT_COST, hash, verify};
use chrono::{Duration, Utc};
use futures_util::{SinkExt, StreamExt};
use openssl::{
    bn::BigNumContext,
    ec::{EcGroup, EcKey, PointConversionForm},
    hash::MessageDigest,
    memcmp,
    nid::Nid,
    pkcs5::pbkdf2_hmac,
    pkey::PKey,
    sign::Signer,
};
use qrcodegen::{QrCode, QrCodeEcc};
use r2d2::Pool;
use r2d2_sqlite::SqliteConnectionManager;
use regex::Regex;
use rusqlite::{OptionalExtension, params};
use scraper::{Html as ParsedHtml, Selector};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    env,
    io::SeekFrom,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::io::{AsyncReadExt, AsyncSeekExt};
use tokio::sync::{Semaphore, broadcast, mpsc};
use tokio_util::io::ReaderStream;
use tower_http::services::ServeDir;
use uuid::Uuid;
use web_push::{
    ContentEncoding, SubscriptionInfo, Urgency, VapidSignatureBuilder, WebPushMessageBuilder,
    request_builder,
};

type Db = Pool<SqliteConnectionManager>;
type AppResult = Result<Response, StatusCode>;
static VAPID_PUBLIC: OnceLock<String> = OnceLock::new();

struct AppState {
    db: Db,
    events: RoomHub,
    typing_events: RoomHub,
    unread_events: RoomHub,
    read_events: RoomHub,
    room_list_events: RoomHub,
    revoked_users: broadcast::Sender<i64>,
    trusted_proxies: HashSet<IpAddr>,
    webhook_client: reqwest::Client,
    webhook_slots: Arc<Semaphore>,
    webhooks_enabled: bool,
    vapid_private: Vec<u8>,
    mention_signing_key: Vec<u8>,
    imported_mention_signing_key: Option<Vec<u8>>,
    avatar_signing_key: Vec<u8>,
    imported_avatar_signing_key: Option<Vec<u8>>,
    blob_signing_key: Vec<u8>,
    imported_blob_signing_key: Option<Vec<u8>>,
    turbo_stream_signing_key: Vec<u8>,
    imported_turbo_stream_signing_key: Option<Vec<u8>>,
    push_slots: Arc<Semaphore>,
    has_push_subscriptions: AtomicBool,
    push_delivery_enabled: bool,
    login_attempts: Mutex<HashMap<IpAddr, VecDeque<std::time::Instant>>>,
    variant_slots: Arc<Semaphore>,
}
#[derive(Clone)]
struct Event {
    room_id: i64,
    payload: String,
}
#[derive(Default)]
struct RoomHub {
    rooms: Mutex<HashMap<i64, broadcast::Sender<String>>>,
}
impl RoomHub {
    fn channel(&self, room_id: i64) -> broadcast::Sender<String> {
        let mut rooms = self.rooms.lock().unwrap();
        rooms
            .entry(room_id)
            .or_insert_with(|| broadcast::channel(1024).0)
            .clone()
    }
    fn send(&self, event: Event) {
        if let Some(channel) = self.rooms.lock().unwrap().get(&event.room_id) {
            let _ = channel.send(event.payload);
        }
    }
    fn remove(&self, room_id: i64) {
        self.rooms.lock().unwrap().remove(&room_id);
    }
}
fn notify_room_lists(s: &AppState, ids: impl IntoIterator<Item = i64>) {
    for id in ids.into_iter().collect::<HashSet<_>>() {
        s.room_list_events.send(Event {
            room_id: id,
            payload: json!({"type":"rooms_changed"}).to_string(),
        });
    }
}
fn notify_direct_room(s: &AppState, rid: i64, ids: impl IntoIterator<Item = i64>) {
    for uid in ids.into_iter().collect::<HashSet<_>>() {
        let rendered = (|| -> Result<String, StatusCode> {
            let room = room_for(s, uid, rid)?;
            let unread: bool = pool(s)?
                .query_row(
                    "SELECT unread_at IS NOT NULL FROM memberships WHERE room_id=?1 AND user_id=?2",
                    params![rid, uid],
                    |row| row.get(0),
                )
                .map_err(db_err)?;
            Ok(sidebar_room_link(&room, None, unread))
        })();
        match rendered {
            Ok(html) => s.room_list_events.send(Event {
                room_id: uid,
                payload: json!({"type":"direct_room_added","room_id":rid,"html":html}).to_string(),
            }),
            Err(status) => {
                eprintln!("Rustfire direct-room update could not render for user {uid}: {status}")
            }
        }
    }
}
#[derive(Clone, Serialize)]
struct User {
    id: i64,
    name: String,
    email: String,
    role: i64,
    bot_token: Option<String>,
    #[serde(skip_serializing)]
    updated_at: String,
    #[serde(skip_serializing)]
    csrf_token: Option<String>,
}
#[derive(Clone, Serialize)]
struct Room {
    id: i64,
    name: String,
    kind: String,
    creator_id: i64,
}
#[derive(Clone, Serialize)]
struct ChatMessage {
    id: i64,
    room_id: i64,
    #[serde(skip_serializing)]
    room_kind: Option<String>,
    #[serde(skip_serializing)]
    room_name: String,
    #[serde(skip_serializing)]
    mention_ids: Vec<i64>,
    creator_id: i64,
    creator_name: String,
    creator_role: i64,
    #[serde(skip_serializing)]
    creator_updated_at: String,
    body: String,
    body_html: Option<String>,
    created_at: String,
    #[serde(skip_serializing)]
    updated_at: String,
    client_message_id: String,
    attachment: Option<Attachment>,
    boosts: Vec<BoostSummary>,
}
#[derive(Clone, Serialize, Deserialize)]
struct BoostSummary {
    id: i64,
    booster_id: i64,
    booster_name: String,
    booster_updated_at: String,
    content: String,
}
#[derive(Clone, Serialize)]
struct Attachment {
    id: i64,
    filename: String,
    content_type: String,
}

fn now() -> String {
    Utc::now().to_rfc3339()
}
fn message_timestamp_ns(value: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(value)
        .map(|date| date.timestamp_nanos_opt())
        .or_else(|_| {
            chrono::NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f")
                .map(|date| date.and_utc().timestamp_nanos_opt())
        })
        .ok()
        .flatten()
}
fn touch_room(db: &rusqlite::Connection, rid: i64) -> Result<(), StatusCode> {
    db.execute(
        "UPDATE rooms SET updated_at=?1 WHERE id=?2",
        params![now(), rid],
    )
    .map_err(db_err)?;
    Ok(())
}
fn touch_message(db: &rusqlite::Connection, mid: i64, rid: i64) -> Result<(), StatusCode> {
    let timestamp = now();
    let timestamp_ns = message_timestamp_ns(&timestamp).ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    db.execute(
        "UPDATE messages SET updated_at=?1,updated_at_ns=?2 WHERE id=?3 AND room_id=?4",
        params![timestamp, timestamp_ns, mid, rid],
    )
    .map_err(db_err)?;
    db.execute(
        "UPDATE rooms SET updated_at=?1 WHERE id=?2",
        params![timestamp, rid],
    )
    .map_err(db_err)?;
    Ok(())
}
fn esc(s: &str) -> String {
    html_escape::encode_safe(s).into_owned()
}
fn safe_preview_url(input: &str, request_host: Option<&str>) -> Option<String> {
    let scheme_end = input.find("://")?;
    let authority = input[scheme_end + 3..].split(['/', '?', '#']).next()?;
    if authority.contains('%') || authority.contains('@') {
        return None;
    }
    let url = reqwest::Url::parse(input).ok()?;
    if !matches!(url.scheme(), "http" | "https") || url.username() != "" || url.password().is_some()
    {
        return None;
    }
    let host = url.host_str()?;
    let ending = host.trim_end_matches('.').rsplit('.').next()?;
    if !host.contains('.')
        || host.parse::<IpAddr>().is_ok()
        || ending.to_ascii_lowercase().starts_with("0x")
        || !ending.bytes().any(|byte| byte.is_ascii_alphabetic())
    {
        return None;
    }
    let own_host = request_host
        .and_then(|host| reqwest::Url::parse(&format!("http://{host}")).ok())
        .and_then(|url| {
            url.host_str()
                .map(|host| host.trim_end_matches('.').to_string())
        })
        .or_else(|| {
            env::var("RUSTFIRE_PUBLIC_URL")
                .ok()
                .and_then(|base| reqwest::Url::parse(&base).ok())
                .and_then(|url| {
                    url.host_str()
                        .map(|host| host.trim_end_matches('.').to_string())
                })
        });
    if own_host
        .as_deref()
        .is_some_and(|own| host.trim_end_matches('.').eq_ignore_ascii_case(own))
    {
        return None;
    }
    Some(input.to_string())
}
fn preview_markup(attributes: &HashMap<String, String>, host: Option<&str>) -> String {
    let title = attributes
        .get("filename")
        .map(String::as_str)
        .unwrap_or("")
        .chars()
        .take(280)
        .collect::<String>();
    let description = attributes
        .get("caption")
        .map(String::as_str)
        .unwrap_or("")
        .chars()
        .take(560)
        .collect::<String>();
    let title = esc(&title);
    let description = esc(&description);
    let link = attributes
        .get("href")
        .and_then(|url| safe_preview_url(url, host))
        .map(|url| {
            format!(
                "<a href='{}' rel='noreferrer' target='_blank'>{title}</a>",
                esc(&url)
            )
        })
        .unwrap_or(title);
    let image = attributes
        .get("url")
        .and_then(|url| safe_preview_url(url, host))
        .map(|url| format!("<img src='{}' alt=''>", esc(&url)))
        .unwrap_or_default();
    format!("<div class='og-embed'>{link}<p>{description}</p>{image}</div>")
}
fn replace_preview_attachments(input: &str, host: Option<&str>, display: bool) -> String {
    if !input.contains("action-text-attachment") && !input.contains("data-trix-attachment") {
        return input.to_string();
    }
    static ACTION_TEXT_ATTACHMENT: OnceLock<Regex> = OnceLock::new();
    static TRIX_FIGURE: OnceLock<Regex> = OnceLock::new();
    let action_text = ACTION_TEXT_ATTACHMENT.get_or_init(|| {
        Regex::new(r"(?is)<action-text-attachment\b[^>]*>.*?</action-text-attachment>").unwrap()
    });
    let figure =
        TRIX_FIGURE.get_or_init(|| Regex::new(r"(?is)<figure\b[^>]*>.*?</figure>").unwrap());
    let replaced = action_text.replace_all(input, |capture: &regex::Captures<'_>| {
        let fragment = ParsedHtml::parse_fragment(&capture[0]);
        let selector = Selector::parse("action-text-attachment").unwrap();
        let Some(element) = fragment.select(&selector).next() else {
            return capture[0].to_string();
        };
        if element.value().attr("content-type")
            != Some("application/vnd.actiontext.opengraph-embed")
        {
            return capture[0].to_string();
        }
        if !display {
            return String::new();
        }
        let attributes = ["href", "url", "filename", "caption"]
            .into_iter()
            .filter_map(|name| {
                element
                    .value()
                    .attr(name)
                    .map(|value| (name.to_string(), value.to_string()))
            })
            .collect();
        preview_markup(&attributes, host)
    });
    figure
        .replace_all(&replaced, |capture: &regex::Captures<'_>| {
            let fragment = ParsedHtml::parse_fragment(&capture[0]);
            let selector = Selector::parse("figure").unwrap();
            let Some(element) = fragment.select(&selector).next() else {
                return capture[0].to_string();
            };
            let Some(data) = element.value().attr("data-trix-attachment") else {
                return capture[0].to_string();
            };
            let Ok(value) = serde_json::from_str::<Value>(data) else {
                return capture[0].to_string();
            };
            if value.get("contentType").and_then(Value::as_str)
                != Some("application/vnd.actiontext.opengraph-embed")
            {
                return capture[0].to_string();
            }
            if !display {
                return String::new();
            }
            let attributes = ["href", "url", "filename", "caption"]
                .into_iter()
                .filter_map(|name| {
                    value
                        .get(name)
                        .and_then(Value::as_str)
                        .map(|value| (name.to_string(), value.to_string()))
                })
                .collect();
            preview_markup(&attributes, host)
        })
        .into_owned()
}
fn replace_mention_attachments(
    input: &str,
    db: &rusqlite::Connection,
    signing_key: &[u8],
    imported_key: Option<&[u8]>,
) -> Result<String, StatusCode> {
    if !input.contains("application/vnd.campfire.mention")
        && !input.contains("application/vnd.rustfire.mention")
    {
        return Ok(input.to_string());
    }
    static ATTACHMENTS: OnceLock<Regex> = OnceLock::new();
    let pattern = ATTACHMENTS.get_or_init(|| {
        Regex::new(r"(?is)<figure\b[^>]*>.*?</figure>|<action-text-attachment\b[^>]*>.*?</action-text-attachment>").unwrap()
    });
    let selector = Selector::parse("figure[data-trix-attachment],action-text-attachment").unwrap();
    let mut rendered = String::with_capacity(input.len());
    let mut consumed = 0;
    for found in pattern.find_iter(input) {
        rendered.push_str(&input[consumed..found.start()]);
        let fragment = ParsedHtml::parse_fragment(found.as_str());
        let replacement = if let Some(element) = fragment.select(&selector).next() {
            let (kind, sgid, legacy_id) =
                if let Some(data) = element.value().attr("data-trix-attachment") {
                    if let Ok(value) = serde_json::from_str::<Value>(data) {
                        (
                            value
                                .get("contentType")
                                .and_then(Value::as_str)
                                .map(str::to_owned),
                            value.get("sgid").and_then(Value::as_str).map(str::to_owned),
                            value.get("userId").and_then(|value| {
                                value.as_i64().or_else(|| value.as_str()?.parse().ok())
                            }),
                        )
                    } else {
                        (None, None, None)
                    }
                } else {
                    (
                        element.value().attr("content-type").map(str::to_owned),
                        element.value().attr("sgid").map(str::to_owned),
                        None,
                    )
                };
            match kind.as_deref() {
                Some("application/vnd.campfire.mention")
                | Some("application/vnd.rustfire.mention") => {
                    let id = if kind.as_deref() == Some("application/vnd.campfire.mention") {
                        sgid.as_deref()
                            .and_then(|sgid| verified_mention_id(signing_key, imported_key, sgid))
                    } else {
                        legacy_id.filter(|id| *id > 0)
                    };
                    if let Some(id) = id {
                        let name: Option<String> = db
                            .query_row("SELECT name FROM users WHERE id=?1", [id], |row| row.get(0))
                            .optional()
                            .map_err(db_err)?;
                        if let Some(name) = name {
                            let sgid_attribute = sgid
                                .as_deref()
                                .map(|sgid| format!(" sgid='{}'", esc(sgid)))
                                .unwrap_or_default();
                            format!(
                                "<span class='mention'{sgid_attribute}><img class='avatar' src='/users/{id}/avatar' alt=''>@{}</span>",
                                esc(&name)
                            )
                        } else {
                            "☒".to_string()
                        }
                    } else {
                        "☒".to_string()
                    }
                }
                _ => found.as_str().to_string(),
            }
        } else {
            found.as_str().to_string()
        };
        rendered.push_str(&replacement);
        consumed = found.end();
    }
    rendered.push_str(&input[consumed..]);
    Ok(rendered)
}
fn rich_body(input: &str, request_host: Option<&str>) -> (String, String) {
    let display_input = replace_preview_attachments(input, request_host, true);
    let html = ammonia::Builder::default()
        .add_tag_attributes("span", &["class"])
        .add_tag_attributes("div", &["class"])
        .add_tag_attributes("figure", &["class"])
        .add_tag_attributes("img", &["class"])
        .clean(&display_input)
        .to_string();
    let plain_input =
        if input.contains("action-text-attachment") || input.contains("data-trix-attachment") {
            let without_previews = replace_preview_attachments(input, request_host, false);
            ammonia::Builder::default()
                .clean(&without_previews)
                .to_string()
        } else {
            html.clone()
        };
    let with_breaks = plain_input
        .replace("</div>", "\n")
        .replace("</p>", "\n")
        .replace("<br>", "\n")
        .replace("<br />", "\n");
    let text = ammonia::Builder::default()
        .tags(HashSet::new())
        .clean(&with_breaks)
        .to_string();
    (
        html_escape::decode_html_entities(&text).trim().to_string(),
        html,
    )
}
fn mention_signature(
    key: &[u8],
    payload: &[u8],
    digest: MessageDigest,
) -> Result<Vec<u8>, openssl::error::ErrorStack> {
    let pkey = PKey::hmac(key)?;
    let mut signer = Signer::new(digest, &pkey)?;
    signer.sign_oneshot_to_vec(payload)
}
fn rails_verifier_key(
    secret_key_base: &str,
    salt: &[u8],
) -> Result<Vec<u8>, openssl::error::ErrorStack> {
    let mut key = vec![0u8; 64];
    pbkdf2_hmac(
        secret_key_base.as_bytes(),
        salt,
        1000,
        MessageDigest::sha256(),
        &mut key,
    )?;
    Ok(key)
}
fn rails_sgid_key(secret_key_base: &str) -> Result<Vec<u8>, openssl::error::ErrorStack> {
    rails_verifier_key(secret_key_base, b"signed_global_ids")
}
fn rails_avatar_key(secret_key_base: &str) -> Result<Vec<u8>, openssl::error::ErrorStack> {
    rails_verifier_key(secret_key_base, b"active_record/signed_id")
}
fn rails_blob_key(secret_key_base: &str) -> Result<Vec<u8>, openssl::error::ErrorStack> {
    rails_verifier_key(secret_key_base, b"ActiveStorage")
}
fn rails_turbo_stream_key(secret_key_base: &str) -> Result<Vec<u8>, openssl::error::ErrorStack> {
    rails_verifier_key(secret_key_base, b"turbo/signed_stream_verifier_key")
}
fn room_stream_token(
    key: &[u8],
    room_kind: &str,
    room_id: i64,
) -> Result<String, openssl::error::ErrorStack> {
    let gid = URL_SAFE_NO_PAD.encode(format!("gid://campfire/{room_kind}/{room_id}"));
    let encoded = STANDARD.encode(json!(format!("{gid}:messages")).to_string());
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha256())?
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("{encoded}--{signature}"))
}
fn room_from_stream_token(key: &[u8], token: &str) -> Option<(i64, String)> {
    if token.len() > 4096 {
        return None;
    }
    let (encoded, signature) = token.split_once("--")?;
    if signature.len() != 64 {
        return None;
    }
    let expected = mention_signature(key, encoded.as_bytes(), MessageDigest::sha256())
        .ok()?
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if !memcmp::eq(signature.as_bytes(), expected.as_bytes()) {
        return None;
    }
    let json_bytes = STANDARD.decode(encoded).ok()?;
    let stream: String = serde_json::from_slice(&json_bytes).ok()?;
    let gid = stream.strip_suffix(":messages")?;
    let gid_bytes = URL_SAFE_NO_PAD.decode(gid).ok()?;
    let gid = std::str::from_utf8(&gid_bytes).ok()?;
    let path = gid.strip_prefix("gid://campfire/")?;
    let (kind, id) = path.rsplit_once('/')?;
    if !matches!(kind, "Rooms::Open" | "Rooms::Closed" | "Rooms::Direct") {
        return None;
    }
    let id = id.parse::<i64>().ok().filter(|id| *id > 0)?;
    Some((id, kind.to_string()))
}
fn avatar_token(key: &[u8], id: i64) -> Result<String, openssl::error::ErrorStack> {
    let payload = json!({"_rails":{"data":id,"pur":"user/avatar"}}).to_string();
    let encoded = URL_SAFE_NO_PAD.encode(payload);
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha256())?;
    let digest = signature
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn avatar_id_from_token(key: &[u8], token: &str) -> Option<i64> {
    if token.len() > 1024 {
        return None;
    }
    let (encoded, signature) = token.split_once("--")?;
    if signature.len() != 64 {
        return None;
    }
    let expected = mention_signature(key, encoded.as_bytes(), MessageDigest::sha256())
        .ok()?
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if !memcmp::eq(signature.as_bytes(), expected.as_bytes()) {
        return None;
    }
    let payload = URL_SAFE_NO_PAD.decode(encoded).ok()?;
    let envelope: Value = serde_json::from_slice(&payload).ok()?;
    let metadata = envelope.get("_rails")?;
    if metadata.get("pur").and_then(Value::as_str) != Some("user/avatar") {
        return None;
    }
    if let Some(expiry) = metadata.get("exp").filter(|expiry| !expiry.is_null()) {
        if chrono::DateTime::parse_from_rfc3339(expiry.as_str()?).ok()? <= Utc::now() {
            return None;
        }
    }
    metadata.get("data")?.as_i64().filter(|id| *id > 0)
}
fn blob_token(key: &[u8], id: i64) -> Result<String, openssl::error::ErrorStack> {
    let payload = json!({"_rails":{"data":id,"pur":"blob_id"}}).to_string();
    let encoded = STANDARD.encode(payload);
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1())?;
    let digest = signature
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn blob_id_from_token(key: &[u8], token: &str) -> Option<i64> {
    if token.len() > 1024 {
        return None;
    }
    let (encoded, signature) = token.split_once("--")?;
    if signature.len() != 40 {
        return None;
    }
    let expected = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1())
        .ok()?
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if !memcmp::eq(signature.as_bytes(), expected.as_bytes()) {
        return None;
    }
    let envelope: Value = serde_json::from_slice(&STANDARD.decode(encoded).ok()?).ok()?;
    let metadata = envelope.get("_rails")?;
    if metadata.get("pur").and_then(Value::as_str) != Some("blob_id") {
        return None;
    }
    if let Some(expiry) = metadata.get("exp").filter(|expiry| !expiry.is_null()) {
        if chrono::DateTime::parse_from_rfc3339(expiry.as_str()?).ok()? <= Utc::now() {
            return None;
        }
    }
    metadata.get("data")?.as_i64().filter(|id| *id > 0)
}
fn blob_path(key: &[u8], id: i64, filename: &str) -> Result<String, StatusCode> {
    let encoded_filename = filename
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect::<String>();
    Ok(format!(
        "/rails/active_storage/blobs/redirect/{}/{encoded_filename}",
        blob_token(key, id).map_err(db_err)?
    ))
}
fn avatar_path(key: &[u8], id: i64, updated_at: &str) -> Result<String, StatusCode> {
    let version = chrono::DateTime::parse_from_rfc3339(updated_at)
        .map(|time| time.with_timezone(&Utc).format("%Y%m%d%H%M%S").to_string())
        .or_else(|_| {
            chrono::NaiveDateTime::parse_from_str(updated_at, "%Y-%m-%d %H:%M:%S%.f")
                .map(|time| time.format("%Y%m%d%H%M%S").to_string())
        })
        .unwrap_or_else(|_| {
            updated_at
                .chars()
                .filter(char::is_ascii_digit)
                .take(14)
                .collect()
        });
    Ok(format!(
        "/users/{}/avatar?v={version}",
        avatar_token(key, id).map_err(db_err)?
    ))
}
fn avatar_initials_svg(id: i64, name: &str) -> String {
    const COLORS: [&str; 18] = [
        "#AF2E1B", "#CC6324", "#3B4B59", "#BFA07A", "#ED8008", "#ED3F1C", "#BF1B1B", "#736B1E",
        "#D07B53", "#736356", "#AD1D1D", "#BF7C2A", "#C09C6F", "#698F9C", "#7C956B", "#5D618F",
        "#3B3633", "#67695E",
    ];
    let mut checksum = !0u32;
    for byte in id.to_string().bytes() {
        checksum ^= u32::from(byte);
        for _ in 0..8 {
            checksum = (checksum >> 1) ^ (0xedb8_8320u32 & 0u32.wrapping_sub(checksum & 1));
        }
    }
    let color = COLORS[(!checksum as usize) % COLORS.len()];
    static INITIAL: OnceLock<Regex> = OnceLock::new();
    let initial = INITIAL.get_or_init(|| Regex::new(r"\b\w").unwrap());
    let initials = initial
        .find_iter(name)
        .map(|match_| match_.as_str())
        .collect::<String>();
    let text_length = if initials.chars().count() >= 3 {
        "textLength=\"85%\" lengthAdjust=\"spacingAndGlyphs\""
    } else {
        ""
    };
    format!(
        "<svg version=\"1.1\" xmlns=\"http://www.w3.org/2000/svg\" xmlns:xlink=\"http://www.w3.org/1999/xlink\" viewBox=\"0 0 512 512\" class=\"avatar\" aria-hidden=\"true\"><defs><clipPath id=\"porthole\"><circle cx=\"50%\" cy=\"50%\" r=\"50%\" /></clipPath></defs><g><rect width=\"100%\" height=\"100%\" rx=\"50\" fill=\"{color}\" /><text x=\"50%\" y=\"50%\" fill=\"#FFFFFF\" text-anchor=\"middle\" dy=\"0.35em\" {text_length} font-family=\"-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif\" font-size=\"230\" font-weight=\"800\" letter-spacing=\"-5\">{}</text></g></svg>",
        esc(&initials)
    )
}
fn mention_sgid(key: &[u8], id: i64) -> Result<String, openssl::error::ErrorStack> {
    let payload = json!({"_rails":{"data":format!("gid://campfire/User/{id}?expires_in"),"pur":"attachable"}}).to_string();
    let encoded = URL_SAFE.encode(payload);
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1())?;
    let digest = signature
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn mention_id_from_sgid(key: &[u8], sgid: &str) -> Option<i64> {
    if sgid.len() > 1024 {
        return None;
    }
    let (encoded, signature) = sgid.split_once("--")?;
    if signature.len() == 40 {
        let expected = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1())
            .ok()?
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        if !memcmp::eq(signature.as_bytes(), expected.as_bytes()) {
            return None;
        }
        let payload = URL_SAFE.decode(encoded).ok()?;
        let envelope: Value = serde_json::from_slice(&payload).ok()?;
        let metadata = envelope.get("_rails")?;
        if metadata.get("pur").and_then(Value::as_str) != Some("attachable") {
            return None;
        }
        if let Some(expiry) = metadata.get("exp").filter(|expiry| !expiry.is_null()) {
            let expiry = expiry.as_str()?;
            if chrono::DateTime::parse_from_rfc3339(expiry).ok()? <= Utc::now() {
                return None;
            }
        }
        let id = metadata
            .get("data")?
            .as_str()?
            .strip_prefix("gid://campfire/User/")?
            .strip_suffix("?expires_in")?;
        return id.parse::<i64>().ok().filter(|id| *id > 0);
    }
    let payload = URL_SAFE_NO_PAD.decode(encoded).ok()?;
    let signature = URL_SAFE_NO_PAD.decode(signature).ok()?;
    let expected = mention_signature(key, &payload, MessageDigest::sha256()).ok()?;
    if signature.len() != expected.len() || !memcmp::eq(&signature, &expected) {
        return None;
    }
    let id = std::str::from_utf8(&payload)
        .ok()?
        .strip_prefix("gid://rustfire/User/")?
        .strip_suffix("/attachable")?;
    id.parse::<i64>().ok().filter(|id| *id > 0)
}
fn verified_mention_id(key: &[u8], imported_key: Option<&[u8]>, sgid: &str) -> Option<i64> {
    mention_id_from_sgid(key, sgid)
        .or_else(|| imported_key.and_then(|imported| mention_id_from_sgid(imported, sgid)))
}
fn mention_ids(input: &str, signing_key: &[u8], imported_key: Option<&[u8]>) -> Vec<i64> {
    if !input.contains("application/vnd.campfire.mention")
        && !input.contains("application/vnd.rustfire.mention")
    {
        return Vec::new();
    }
    let fragment = ParsedHtml::parse_fragment(input);
    let figures = Selector::parse("figure[data-trix-attachment]").unwrap();
    let mut ids = Vec::new();
    for figure in fragment.select(&figures) {
        let Some(data) = figure.value().attr("data-trix-attachment") else {
            continue;
        };
        let Ok(value) = serde_json::from_str::<Value>(data) else {
            continue;
        };
        let id = match value.get("contentType").and_then(Value::as_str) {
            Some("application/vnd.campfire.mention") => value
                .get("sgid")
                .and_then(Value::as_str)
                .and_then(|sgid| verified_mention_id(signing_key, imported_key, sgid)),
            Some("application/vnd.rustfire.mention") => value
                .get("userId")
                .and_then(|value| value.as_i64().or_else(|| value.as_str()?.parse().ok())),
            _ => None,
        };
        if let Some(id) = id.filter(|id| *id > 0) {
            if !ids.contains(&id) {
                ids.push(id);
            }
        }
    }
    let attachments = Selector::parse("action-text-attachment[sgid]").unwrap();
    for attachment in fragment.select(&attachments) {
        if attachment.value().attr("content-type") != Some("application/vnd.campfire.mention") {
            continue;
        }
        let id = attachment
            .value()
            .attr("sgid")
            .and_then(|sgid| verified_mention_id(signing_key, imported_key, sgid));
        if let Some(id) = id {
            if !ids.contains(&id) {
                ids.push(id);
            }
        }
    }
    ids
}
fn db_err(error: impl std::fmt::Debug) -> StatusCode {
    eprintln!("Rustfire database error: {error:?}");
    StatusCode::INTERNAL_SERVER_ERROR
}
fn pool(state: &AppState) -> Result<r2d2::PooledConnection<SqliteConnectionManager>, StatusCode> {
    state.db.get().map_err(db_err)
}
fn cookie(headers: &HeaderMap, key: &str) -> Option<String> {
    headers
        .get(header::COOKIE)?
        .to_str()
        .ok()?
        .split(';')
        .find_map(|p| {
            p.trim()
                .split_once('=')
                .filter(|(k, _)| *k == key)
                .map(|(_, v)| v.to_string())
        })
}
fn safe_return_path(encoded: &str) -> Option<String> {
    let bytes = URL_SAFE_NO_PAD.decode(encoded).ok()?;
    let path = String::from_utf8(bytes).ok()?;
    (path.len() <= 2048
        && path.starts_with('/')
        && !path.starts_with("//")
        && !path.contains('\\')
        && !path.bytes().any(|byte| byte.is_ascii_control()))
    .then_some(path)
}
fn user(state: &AppState, headers: &HeaderMap) -> Result<User, StatusCode> {
    let token = cookie(headers, "session_token").ok_or(StatusCode::UNAUTHORIZED)?;
    let db = pool(state)?;
    db.query_row("SELECT u.id,u.name,COALESCE(u.email_address,''),u.role,u.bot_token,s.csrf_token,u.updated_at FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=?1 AND u.status=0", [token], |r| Ok(User { id:r.get(0)?,name:r.get(1)?,email:r.get(2)?,role:r.get(3)?,bot_token:r.get(4)?,csrf_token:r.get(5)?,updated_at:r.get(6)? })).optional().map_err(db_err)?.ok_or(StatusCode::UNAUTHORIZED)
}
fn bot_user(state: &AppState, key: &str) -> Result<User, StatusCode> {
    let db = pool(state)?;
    db.query_row("SELECT id,name,COALESCE(email_address,''),role,bot_token,updated_at FROM users WHERE bot_token=?1 AND status=0", [key], |r| Ok(User{id:r.get(0)?,name:r.get(1)?,email:r.get(2)?,role:r.get(3)?,bot_token:r.get(4)?,updated_at:r.get(5)?,csrf_token:None})).optional().map_err(db_err)?.ok_or(StatusCode::UNAUTHORIZED)
}
fn is_admin(u: &User) -> bool {
    u.role == 1
}
fn ensure_room_creation_allowed(s: &AppState, u: &User) -> Result<(), StatusCode> {
    if is_admin(u) {
        return Ok(());
    }
    let restricted: bool = pool(s)?
        .query_row(
            "SELECT restrict_room_creation FROM account_settings WHERE id=1",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    if restricted {
        Err(StatusCode::FORBIDDEN)
    } else {
        Ok(())
    }
}
fn room_for(state: &AppState, uid: i64, rid: i64) -> Result<Room, StatusCode> {
    let db = pool(state)?;
    db.query_row("SELECT r.id,CASE WHEN r.type='Rooms::Direct' THEN COALESCE((SELECT group_concat(name,', ') FROM (SELECT u2.name FROM users u2 JOIN memberships m2 ON m2.user_id=u2.id WHERE m2.room_id=r.id AND u2.id!=?1 ORDER BY u2.id)),(SELECT name FROM users WHERE id=?1)) ELSE COALESCE(r.name,'') END,r.type,r.creator_id FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE m.user_id=?1 AND r.id=?2",params![uid,rid],|r|Ok(Room{id:r.get(0)?,name:r.get(1)?,kind:r.get(2)?,creator_id:r.get(3)?})).optional().map_err(db_err)?.ok_or(StatusCode::NOT_FOUND)
}
fn can_admin(u: &User, room: &Room) -> bool {
    is_admin(u) || room.creator_id == u.id || room.kind == "Rooms::Direct"
}
fn found_redirect(path: &str) -> Response {
    let mut response = StatusCode::FOUND.into_response();
    response
        .headers_mut()
        .insert(header::LOCATION, path.parse().unwrap());
    response
}
fn csrf_forms(html: &str, token: &str) -> String {
    let mut out = String::with_capacity(html.len() + 512);
    let mut rest = html;
    while let Some(start) = rest.find("<form") {
        out.push_str(&rest[..start]);
        rest = &rest[start..];
        let Some(end) = rest.find('>') else { break };
        let tag = &rest[..=end];
        out.push_str(tag);
        if tag.contains("method='post'") {
            out.push_str(&format!(
                "<input type='hidden' name='authenticity_token' value='{}'>",
                esc(token)
            ));
        }
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    out
}
fn render(title: &str, body: &str, current: Option<&User>) -> Response {
    render_with_csrf(
        title,
        body,
        current,
        current.and_then(|u| u.csrf_token.as_deref()).unwrap_or(""),
    )
}
fn render_unauth(title: &str, body: &str) -> Response {
    let token = Uuid::new_v4().to_string();
    let mut response = render_with_csrf(title, body, None, &token);
    response.headers_mut().insert(
        header::SET_COOKIE,
        format!(
            "preauth_csrf={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=3600{}",
            secure_cookie_suffix()
        )
        .parse()
        .unwrap(),
    );
    response
}
fn render_with_csrf(title: &str, body: &str, current: Option<&User>, token: &str) -> Response {
    let nav = if let Some(u) = current {
        format!(
            "<div class='top-user'><span>{}</span><a href='/users/me/profile'>Settings</a><form method='post' action='/session/logout'><button>Sign out</button></form></div>",
            esc(&u.name)
        )
    } else {
        String::new()
    };
    let user_id = current.map(|u| u.id.to_string()).unwrap_or_default();
    let html = format!(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='csrf-token' content='{}'><meta name='vapid-public-key' content='{}'><meta name='theme-color' content='#f2ede3'><title>{} · Rustfire</title><link rel='icon' href='/account/logo'><link rel='manifest' href='/webmanifest'><link rel='stylesheet' href='/static/app.css'><link rel='stylesheet' href='/static/chat.css'><link rel='stylesheet' href='/static/trix.css'><link rel='stylesheet' href='/account/custom_styles.css'><script defer src='/static/trix.js'></script><script defer src='/static/app.js'></script></head><body data-user-id='{}'><a class='skip' href='#main'>Skip to main content</a><header><a class='brand' href='/'><img src='/account/logo' alt=''>Rustfire</a>{}</header><main id='main'>{}</main></body></html>",
        esc(token),
        VAPID_PUBLIC.get().map(String::as_str).unwrap_or(""),
        esc(title),
        user_id,
        nav,
        body
    );
    Html(if token.is_empty() {
        html
    } else {
        csrf_forms(&html, token)
    })
    .into_response()
}
fn form_field(label: &str, name: &str, ty: &str) -> String {
    format!(
        "<label>{}<input name='{}' type='{}' required></label>",
        esc(label),
        name,
        ty
    )
}
fn public_url(headers: &HeaderMap, path: &str) -> String {
    if let Ok(base) = env::var("RUSTFIRE_PUBLIC_URL") {
        let base = base.trim_end_matches('/');
        if base.starts_with("https://") || base.starts_with("http://") {
            return format!("{base}{path}");
        }
    }
    let request_host = headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .filter(|v| {
            !v.is_empty()
                && v.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b".:-[]".contains(&b))
        });
    let fallback_host = if request_host.is_none() {
        env::var("RUSTFIRE_ADDR")
            .ok()
            .and_then(|value| value.parse::<SocketAddr>().ok())
            .map(|address| {
                let ip = if address.ip().is_unspecified() {
                    match address.ip() {
                        IpAddr::V4(_) => IpAddr::V4(Ipv4Addr::LOCALHOST),
                        IpAddr::V6(_) => IpAddr::V6(Ipv6Addr::LOCALHOST),
                    }
                } else {
                    address.ip()
                };
                SocketAddr::new(ip, address.port()).to_string()
            })
            .unwrap_or_else(|| "127.0.0.1:3000".to_string())
    } else {
        String::new()
    };
    let host = request_host.unwrap_or(&fallback_host);
    let scheme = if secure_cookie_suffix().is_empty() {
        "http"
    } else {
        "https"
    };
    format!("{scheme}://{host}{path}")
}
fn qr_link(url: &str, label: &str) -> String {
    let id = URL_SAFE_NO_PAD.encode(url);
    format!("<a href='/qr_code/{id}'>{}</a>", esc(label))
}
fn valid_webhook_url(url: &str) -> bool {
    if url.is_empty() {
        return true;
    }
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return false;
    };
    url.len() <= 2048
        && (parsed.scheme() == "http" || parsed.scheme() == "https")
        && parsed.host().is_some()
        && parsed.username().is_empty()
        && parsed.password().is_none()
}
fn valid_push_endpoint(endpoint: &str) -> bool {
    let Ok(url) = reqwest::Url::parse(endpoint) else {
        return false;
    };
    let Some(host) = url.host_str() else {
        return false;
    };
    let permitted = [
        "jmt17.google.com",
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
        "notify.windows.com",
    ];
    endpoint.len() <= 2048
        && url.scheme() == "https"
        && url.port_or_known_default() == Some(443)
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
        && permitted.iter().any(|allowed| {
            host.eq_ignore_ascii_case(allowed)
                || host.to_ascii_lowercase().ends_with(&format!(".{allowed}"))
        })
}
fn valid_push_keys(p256dh: &str, auth: &str) -> bool {
    let decode = |value: &str| {
        URL_SAFE_NO_PAD
            .decode(value)
            .or_else(|_| URL_SAFE.decode(value))
    };
    matches!(decode(p256dh),Ok(key) if key.len()==65&&key[0]==4)
        && matches!(decode(auth),Ok(key) if key.len()==16)
}
fn public_web_url(input: &str) -> Option<reqwest::Url> {
    if input.len() > 2048 {
        return None;
    }
    let url = reqwest::Url::parse(input).ok()?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return None;
    }
    Some(url)
}
async fn pinned_web_client(url: &reqwest::Url) -> Option<reqwest::Client> {
    let host = url.host_str()?;
    let port = url.port_or_known_default()?;
    let addresses: Vec<_> = tokio::net::lookup_host((host, port)).await.ok()?.collect();
    if addresses.is_empty()
        || addresses
            .iter()
            .any(|address| !public_network_ip(address.ip()))
    {
        return None;
    }
    reqwest::Client::builder()
        .no_proxy()
        .resolve(host, addresses[0])
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(7))
        .build()
        .ok()
}
async fn fetch_public_url(input: &str, head: bool) -> Option<reqwest::Response> {
    let mut url = public_web_url(input)?;
    for _ in 0..10 {
        let client = pinned_web_client(&url).await?;
        let response = if head {
            client.head(url.clone()).send().await.ok()?
        } else {
            client.get(url.clone()).send().await.ok()?
        };
        if response.status().is_redirection() {
            let location = response.headers().get(header::LOCATION)?.to_str().ok()?;
            url = public_web_url(url.join(location).ok()?.as_str())?;
            continue;
        }
        return Some(response);
    }
    None
}
fn clean_og_text(input: &str) -> String {
    let cleaned = ammonia::Builder::default()
        .tags(HashSet::new())
        .clean(input)
        .to_string();
    html_escape::decode_html_entities(&cleaned)
        .trim()
        .to_string()
}
fn og_attributes(document: &str) -> HashMap<String, String> {
    let html = ParsedHtml::parse_document(document);
    let selector = Selector::parse("meta").unwrap();
    let mut attributes = HashMap::new();
    for meta in html.select(&selector) {
        let property = meta
            .value()
            .attr("property")
            .or_else(|| meta.value().attr("name"));
        let Some(key) = property.and_then(|value| value.strip_prefix("og:")) else {
            continue;
        };
        if matches!(key, "title" | "url" | "image" | "description") {
            if let Some(content) = meta.value().attr("content") {
                attributes.insert(key.to_string(), content.to_string());
            }
        }
    }
    attributes
}
fn media_link(url: &reqwest::Url) -> bool {
    let path = url.path().to_ascii_lowercase();
    let ext = path.rsplit('.').next().unwrap_or("");
    matches!(
        ext,
        "zip"
            | "tar"
            | "gz"
            | "bz2"
            | "xz"
            | "rar"
            | "7z"
            | "dmg"
            | "exe"
            | "msi"
            | "pkg"
            | "deb"
            | "iso"
            | "jpg"
            | "jpeg"
            | "png"
            | "gif"
            | "bmp"
            | "mp4"
            | "mov"
            | "avi"
            | "mkv"
            | "wmv"
            | "flv"
            | "heic"
            | "heif"
            | "mp3"
            | "wav"
            | "ogg"
            | "aac"
            | "wma"
            | "webm"
            | "ogv"
            | "mpg"
            | "mpeg"
    )
}
async fn unfurl_url(input: &str) -> Option<Value> {
    let mut url = public_web_url(input)?;
    if media_link(&url) {
        return None;
    }
    if matches!(
        url.host_str(),
        Some("twitter.com" | "www.twitter.com" | "x.com" | "www.x.com")
    ) && url.path() != "/"
    {
        url.set_host(Some("fxtwitter.com")).ok()?;
    }
    let response = fetch_public_url(url.as_str(), false).await?;
    if !response.status().is_success()
        || response
            .headers()
            .get(header::CONTENT_TYPE)?
            .to_str()
            .ok()?
            .split(';')
            .next()?
            .trim()
            .to_ascii_lowercase()
            != "text/html"
    {
        return None;
    }
    if response
        .content_length()
        .is_some_and(|length| length > 5 * 1024 * 1024)
    {
        return None;
    }
    let mut body = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.ok()?;
        if body.len() + chunk.len() > 5 * 1024 * 1024 {
            return None;
        }
        body.extend_from_slice(&chunk);
    }
    let attributes = og_attributes(&String::from_utf8_lossy(&body));
    let title = clean_og_text(attributes.get("title")?);
    let description = clean_og_text(attributes.get("description")?);
    if title.is_empty() || description.is_empty() {
        return None;
    }
    let canonical = if let Some(candidate) = attributes.get("url") {
        if let Some(parsed) = public_web_url(candidate) {
            if pinned_web_client(&parsed).await.is_some() {
                candidate.clone()
            } else {
                input.to_string()
            }
        } else {
            input.to_string()
        }
    } else {
        input.to_string()
    };
    let image = if let Some(candidate) = attributes.get("image") {
        if public_web_url(candidate).is_some() {
            if let Some(response) = fetch_public_url(candidate, true).await {
                let kind = response
                    .headers()
                    .get(header::CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("")
                    .split(';')
                    .next()
                    .unwrap_or("")
                    .trim()
                    .to_ascii_lowercase();
                if matches!(
                    kind.as_str(),
                    "image/jpeg" | "image/png" | "image/gif" | "image/webp"
                ) {
                    Some(candidate.clone())
                } else {
                    None
                }
            } else {
                None
            }
        } else {
            None
        }
    } else {
        None
    };
    Some(json!({"title":title,"url":canonical,"description":description,"image":image}))
}
#[derive(Deserialize)]
struct UnfurlInput {
    url: String,
}
async fn unfurl_link(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(input): Json<UnfurlInput>,
) -> AppResult {
    let _ = user(&s, &headers)?;
    match unfurl_url(&input.url).await {
        Some(value) => Ok(Json(value).into_response()),
        None => Ok(StatusCode::NO_CONTENT.into_response()),
    }
}
async fn qr_code_show(Path(id): Path<String>) -> AppResult {
    if id.len() > 4096 {
        return Err(StatusCode::URI_TOO_LONG);
    }
    let decoded = URL_SAFE_NO_PAD
        .decode(&id)
        .or_else(|_| URL_SAFE.decode(&id))
        .map_err(|_| StatusCode::BAD_REQUEST)?;
    let url = std::str::from_utf8(&decoded).map_err(|_| StatusCode::BAD_REQUEST)?;
    if url.len() > 2048 || url.is_empty() {
        return Err(StatusCode::BAD_REQUEST);
    }
    let code = QrCode::encode_text(url, QrCodeEcc::Medium).map_err(|_| StatusCode::BAD_REQUEST)?;
    let size = code.size();
    let extent = size + 8;
    let mut svg = format!(
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {extent} {extent}' role='img' aria-label='QR code'><path fill='#fff' d='M0 0h{extent}v{extent}H0z'/><path fill='#000' d='"
    );
    for y in 0..size {
        for x in 0..size {
            if code.get_module(x, y) {
                svg.push_str(&format!("M{} {}h1v1h-1z", x + 4, y + 4));
            }
        }
    }
    svg.push_str("'/></svg>");
    Ok((
        [
            (header::CONTENT_TYPE, "image/svg+xml"),
            (header::CACHE_CONTROL, "no-store"),
        ],
        svg,
    )
        .into_response())
}
fn session_response(token: String, to: &str) -> Response {
    let mut r = Redirect::to(to).into_response();
    r.headers_mut().insert(
        header::SET_COOKIE,
        format!(
            "session_token={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{}",
            secure_cookie_suffix()
        )
        .parse()
        .unwrap(),
    );
    r
}
fn secure_cookie_suffix() -> &'static str {
    if env::var("RUSTFIRE_SECURE_COOKIES")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false)
    {
        "; Secure"
    } else {
        ""
    }
}
fn load_vapid_key(
    db_path: &std::path::Path,
) -> Result<(Vec<u8>, String), Box<dyn std::error::Error>> {
    use std::io::Write;
    let path = env::var("RUSTFIRE_VAPID_KEY_FILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| db_path.with_extension("vapid.der"));
    let der = if path.exists() {
        std::fs::read(&path)?
    } else {
        let group = EcGroup::from_curve_name(Nid::X9_62_PRIME256V1)?;
        let generated = EcKey::generate(&group)?.private_key_to_der()?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&path) {
            Ok(mut file) => {
                file.write_all(&generated)?;
                generated
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                std::fs::read(&path)?
            }
            Err(error) => return Err(error.into()),
        }
    };
    let pair = EcKey::private_key_from_der(&der)?;
    let mut context = BigNumContext::new()?;
    let public = pair.public_key().to_bytes(
        pair.group(),
        PointConversionForm::UNCOMPRESSED,
        &mut context,
    )?;
    Ok((der, URL_SAFE_NO_PAD.encode(public)))
}
fn create_session(state: &AppState, uid: i64, ip: IpAddr) -> Result<Response, StatusCode> {
    create_session_to(state, uid, ip, "/")
}
fn create_session_to(
    state: &AppState,
    uid: i64,
    ip: IpAddr,
    destination: &str,
) -> Result<Response, StatusCode> {
    let token = Uuid::new_v4().to_string();
    let csrf_token = Uuid::new_v4().to_string();
    let db = pool(state)?;
    db.execute(
        "INSERT INTO sessions(user_id,token,csrf_token,ip_address,created_at,last_active_at) VALUES(?1,?2,?3,?4,?5,?5)",
        params![uid, token, csrf_token, ip.to_string(), now()],
    )
    .map_err(db_err)?;
    Ok(session_response(token, destination))
}
fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            !ip.is_private()
                && !ip.is_loopback()
                && !ip.is_link_local()
                && !ip.is_unspecified()
                && !ip.is_multicast()
                && !ip.is_broadcast()
        }
        IpAddr::V6(ip) => {
            !ip.is_loopback()
                && !ip.is_unique_local()
                && !ip.is_unicast_link_local()
                && !ip.is_unspecified()
                && !ip.is_multicast()
        }
    }
}
fn public_network_ip(ip: IpAddr) -> bool {
    if !public_ip(ip) {
        return false;
    }
    match ip {
        IpAddr::V4(value) => {
            let [a, b, c, _] = value.octets();
            !(a == 0
                || (a == 100 && (64..=127).contains(&b))
                || (a == 192 && b == 0 && c == 0)
                || (a == 192 && b == 0 && c == 2)
                || (a == 192 && b == 88 && c == 99)
                || (a == 198 && (b == 18 || b == 19))
                || (a == 198 && b == 51 && c == 100)
                || (a == 203 && b == 0 && c == 113)
                || a >= 224)
        }
        IpAddr::V6(value) => {
            let seg = value.segments();
            (seg[0] & 0xe000) == 0x2000 && !(seg[0] == 0x2001 && seg[1] == 0x0db8)
        }
    }
}
fn client_ip(trusted_proxies: &HashSet<IpAddr>, headers: &HeaderMap, peer: IpAddr) -> IpAddr {
    let mut ip = peer;
    if let Some(chain) = headers
        .get("x-forwarded-for")
        .and_then(|value| value.to_str().ok())
    {
        for raw in chain.split(',').rev() {
            if !trusted_proxies.contains(&ip) {
                break;
            }
            match raw.trim().parse::<IpAddr>() {
                Ok(next) => ip = next,
                Err(_) => break,
            }
        }
    }
    ip
}
fn presence_update(s: &AppState, uid: i64, rid: i64, action: &str) -> Result<(), StatusCode> {
    let db = pool(s)?;
    let cutoff = (Utc::now() - Duration::seconds(60)).to_rfc3339();
    let current = now();
    match action {
        "present" => {
            db.execute("UPDATE memberships SET connections=CASE WHEN connected_at>?1 THEN connections+1 ELSE 1 END,connected_at=?2,unread_at=NULL WHERE room_id=?3 AND user_id=?4",params![cutoff,current,rid,uid]).map_err(db_err)?;
        }
        "refresh" => {
            db.execute("UPDATE memberships SET connections=CASE WHEN connected_at>?1 THEN connections ELSE 1 END,connected_at=?2,unread_at=NULL WHERE room_id=?3 AND user_id=?4",params![cutoff,current,rid,uid]).map_err(db_err)?;
        }
        "absent" => {
            db.execute("UPDATE memberships SET connections=MAX(0,connections-1),connected_at=CASE WHEN connections<=1 THEN NULL ELSE connected_at END WHERE room_id=?1 AND user_id=?2",params![rid,uid]).map_err(db_err)?;
        }
        _ => return Err(StatusCode::BAD_REQUEST),
    }
    if action == "present" || action == "refresh" {
        s.read_events.send(Event {
            room_id: uid,
            payload: json!({"room_id":rid}).to_string(),
        });
    }
    Ok(())
}
async fn reject_banned_ip(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    mut request: Request,
    next: Next,
) -> Response {
    if request.method() != Method::GET && request.method() != Method::HEAD {
        let ip = client_ip(&s.trusted_proxies, request.headers(), addr.ip()).to_string();
        match pool(&s).and_then(|db| {
            db.query_row(
                "SELECT EXISTS(SELECT 1 FROM bans WHERE ip_address=?1)",
                [ip],
                |r| r.get::<_, bool>(0),
            )
            .map_err(db_err)
        }) {
            Ok(true) => return StatusCode::TOO_MANY_REQUESTS.into_response(),
            Err(code) => return code.into_response(),
            _ => {}
        }
        let path = request.uri().path();
        let preauth_route = path == "/first_run"
            || (path == "/session" && request.method() == Method::POST)
            || path.starts_with("/join/")
            || path.starts_with("/session/transfers/");
        let csrf = if preauth_route {
            match cookie(request.headers(), "preauth_csrf") {
                Some(token) => Some(token),
                None => return StatusCode::FORBIDDEN.into_response(),
            }
        } else if let Some(session_token) = cookie(request.headers(), "session_token") {
            match pool(&s).and_then(|db|db.query_row("SELECT s.csrf_token FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?1 AND u.status=0",[session_token],|r|r.get(0)).optional().map_err(db_err)) {
                Ok(token)=>token,
                Err(code)=>return code.into_response(),
            }
        } else {
            None
        };
        if let Some(csrf) = csrf {
            let header_valid = request
                .headers()
                .get("x-csrf-token")
                .and_then(|v| v.to_str().ok())
                == Some(csrf.as_str());
            if !header_valid {
                let content_type = request
                    .headers()
                    .get(header::CONTENT_TYPE)
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("")
                    .to_string();
                let (parts, body) = request.into_parts();
                let bytes = match to_bytes(body, 25 * 1024 * 1024).await {
                    Ok(bytes) => bytes,
                    Err(_) => return StatusCode::PAYLOAD_TOO_LARGE.into_response(),
                };
                let form_valid = if content_type.starts_with("application/x-www-form-urlencoded") {
                    form_urlencoded::parse(&bytes)
                        .any(|(key, value)| key == "authenticity_token" && value == csrf)
                } else if content_type.starts_with("multipart/form-data") {
                    bytes
                        .windows(csrf.len())
                        .any(|window| window == csrf.as_bytes())
                } else {
                    false
                };
                if !form_valid {
                    return StatusCode::FORBIDDEN.into_response();
                }
                request = Request::from_parts(parts, Body::from(bytes));
            }
        }
    }
    let browser_navigation = request.method() == Method::GET
        && request
            .headers()
            .get(header::ACCEPT)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value.contains("text/html"));
    let requested_path = request
        .uri()
        .path_and_query()
        .map(|value| value.as_str().to_string())
        .unwrap_or_else(|| "/".to_string());
    let response = next.run(request).await;
    if browser_navigation && response.status() == StatusCode::UNAUTHORIZED {
        let encoded = URL_SAFE_NO_PAD.encode(requested_path.as_bytes());
        let mut redirect = Redirect::to("/session/new").into_response();
        redirect.headers_mut().append(
            header::SET_COOKIE,
            format!(
                "return_to={encoded}; HttpOnly; SameSite=Lax; Path=/; Max-Age=1800{}",
                secure_cookie_suffix()
            )
            .parse()
            .unwrap(),
        );
        redirect
    } else {
        response
    }
}
fn transfer_link(state: &AppState, uid: i64) -> Result<String, StatusCode> {
    let db = pool(state)?;
    db.execute(
        "DELETE FROM session_transfers WHERE expires_at<=?1",
        [now()],
    )
    .map_err(db_err)?;
    let token = Uuid::new_v4().to_string();
    let expiry = (Utc::now() + Duration::hours(4)).to_rfc3339();
    db.execute(
        "INSERT INTO session_transfers(token,user_id,expires_at) VALUES(?1,?2,?3)",
        params![token, uid, expiry],
    )
    .map_err(db_err)?;
    Ok(format!("/session/transfers/{token}"))
}
async fn transfer_show(Path(token): Path<String>) -> AppResult {
    Ok(render_unauth(
        "Sign in on this device",
        &format!(
            "<section class='form-card'><h1>Sign in on this device</h1><p>Use this private link to sign in.</p><form method='post' action='/session/transfers/{}'><button class='button'>Sign in</button></form></section>",
            esc(&token)
        ),
    ))
}
async fn transfer_update(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Path(token): Path<String>,
) -> AppResult {
    let db = pool(&s)?;
    let uid: Option<i64> = db.query_row(
        "SELECT t.user_id FROM session_transfers t JOIN users u ON u.id=t.user_id WHERE t.token=?1 AND t.expires_at>?2 AND u.status=0",
        params![token, now()], |r| r.get(0),
    ).optional().map_err(db_err)?;
    let uid = uid.ok_or(StatusCode::BAD_REQUEST)?;
    drop(db);
    create_session(&s, uid, client_ip(&s.trusted_proxies, &headers, addr.ip()))
}
fn first_run_needed(state: &AppState) -> Result<bool, StatusCode> {
    let db = pool(state)?;
    let n: i64 = db
        .query_row("SELECT count(*) FROM users", [], |r| r.get(0))
        .map_err(db_err)?;
    Ok(n == 0)
}

async fn root(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    if first_run_needed(&s)? {
        return Ok(Redirect::to("/first_run").into_response());
    }
    let u = match user(&s, &headers) {
        Ok(u) => u,
        Err(_) => return Ok(Redirect::to("/session/new").into_response()),
    };
    let db = pool(&s)?;
    let last_visited = cookie(&headers, "last_room").and_then(|id| id.parse::<i64>().ok());
    let rid: Option<i64> = if let Some(id) = last_visited {
        db.query_row(
            "SELECT r.id FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE m.user_id=?1 AND r.id=?2",
            params![u.id, id],
            |row| row.get(0),
        )
        .optional()
        .map_err(db_err)?
    } else {
        None
    };
    let rid = if rid.is_some() {
        rid
    } else {
        db.query_row(
            "SELECT r.id FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE m.user_id=?1 ORDER BY r.created_at,r.id LIMIT 1",
            [u.id],
            |row| row.get(0),
        )
        .optional()
        .map_err(db_err)?
    };
    Ok(match rid {
        Some(id) => Redirect::to(&format!("/rooms/{id}")).into_response(),
        None => render(
            "Welcome",
            "<div class='empty'><h1>Welcome to Rustfire</h1><p>Start a room or ping someone to begin.</p><a class='button' href='/rooms/opens/new'>Create a room</a></div>",
            Some(&u),
        ),
    })
}
async fn rooms_index(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let rid: Option<i64> = pool(&s)?
        .query_row(
            "SELECT r.id FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE m.user_id=?1 ORDER BY r.id DESC LIMIT 1",
            [u.id],
            |row| row.get(0),
        )
        .optional()
        .map_err(db_err)?;
    Ok(Redirect::to(
        &rid.map(|id| format!("/rooms/{id}"))
            .unwrap_or_else(|| "/".to_string()),
    )
    .into_response())
}
async fn first_run_get(State(s): State<Arc<AppState>>) -> AppResult {
    if !first_run_needed(&s)? {
        return Ok(Redirect::to("/").into_response());
    }
    Ok(render_unauth(
        "Set up Rustfire",
        &format!(
            "<section class='auth-card'><img class='hero-icon' src='/account/logo' alt=''><h1>Set up Rustfire</h1><form method='post' action='/first_run'>{}{}{}<button class='button'>Create account</button></form></section>",
            form_field("Your name", "name", "text"),
            form_field("Email address", "email_address", "email"),
            form_field("Password", "password", "password")
        ),
    ))
}
#[derive(Deserialize)]
struct Signup {
    name: String,
    email_address: String,
    password: String,
}
async fn first_run_post(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Form(f): Form<Signup>,
) -> AppResult {
    if !first_run_needed(&s)? {
        return Err(StatusCode::CONFLICT);
    }
    if f.name.trim().is_empty() || f.password.len() < 8 || !f.email_address.contains('@') {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let pw = hash(&f.password, DEFAULT_COST).map_err(db_err)?;
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    let existing: i64 = tx
        .query_row("SELECT count(*) FROM users", [], |r| r.get(0))
        .map_err(db_err)?;
    if existing > 0 {
        return Err(StatusCode::CONFLICT);
    }
    let t = now();
    tx.execute(
        "INSERT INTO accounts(id,name,join_code,created_at,updated_at) VALUES(1,'Rustfire',?1,?2,?2)",
        params![Uuid::new_v4().to_string(), t],
    )
    .map_err(|_|StatusCode::CONFLICT)?;
    tx.execute("INSERT INTO users(name,email_address,password_digest,role,status,created_at,updated_at) VALUES(?1,?2,?3,1,0,?4,?4)",params![f.name.trim(),f.email_address.trim().to_lowercase(),pw,t]).map_err(db_err)?;
    let uid = tx.last_insert_rowid();
    tx.execute("INSERT INTO rooms(name,type,creator_id,created_at,updated_at) VALUES('Campfire','Rooms::Open',?1,?2,?2)",params![uid,t]).map_err(db_err)?;
    let rid = tx.last_insert_rowid();
    tx.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(?1,?2,'mentions',?3)",params![rid,uid,t]).map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    drop(db);
    create_session(&s, uid, client_ip(&s.trusted_proxies, &headers, addr.ip()))
}
async fn login_get(State(s): State<Arc<AppState>>) -> AppResult {
    if first_run_needed(&s)? {
        return Ok(Redirect::to("/first_run").into_response());
    }
    Ok(render_unauth(
        "Sign in",
        &format!(
            "<section class='auth-card'><img class='hero-icon' src='/account/logo' alt=''><h1>Sign in</h1><form method='post' action='/session'>{}{}<button class='button'>Sign in</button></form></section>",
            form_field("Email address", "email_address", "email"),
            form_field("Password", "password", "password")
        ),
    ))
}
#[derive(Deserialize)]
struct Login {
    email_address: String,
    password: String,
}
async fn login_post(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Form(f): Form<Login>,
) -> AppResult {
    let ip = client_ip(&s.trusted_proxies, &headers, addr.ip());
    let moment = std::time::Instant::now();
    {
        let mut attempts = s.login_attempts.lock().unwrap();
        if attempts.len() > 10_000 {
            attempts.retain(|_, times| {
                times.retain(|time| {
                    moment.duration_since(*time) < std::time::Duration::from_secs(180)
                });
                !times.is_empty()
            });
        }
        let times = attempts.entry(ip).or_default();
        while times
            .front()
            .is_some_and(|time| moment.duration_since(*time) >= std::time::Duration::from_secs(180))
        {
            times.pop_front();
        }
        if times.len() >= 10 {
            return Err(StatusCode::TOO_MANY_REQUESTS);
        }
        times.push_back(moment);
    }
    let db = pool(&s)?;
    let row: Option<(i64, String)> = db
        .query_row(
            "SELECT id,password_digest FROM users WHERE email_address=?1 AND status=0",
            [f.email_address.to_lowercase()],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    if let Some((id, pw)) = row {
        if verify(&f.password, &pw).unwrap_or(false) {
            let destination = cookie(&headers, "return_to")
                .and_then(|encoded| safe_return_path(&encoded))
                .unwrap_or_else(|| "/".to_string());
            let mut response = create_session_to(&s, id, ip, &destination)?;
            response.headers_mut().append(
                header::SET_COOKIE,
                format!(
                    "return_to=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{}",
                    secure_cookie_suffix()
                )
                .parse()
                .unwrap(),
            );
            return Ok(response);
        }
    }
    Ok((
        StatusCode::UNAUTHORIZED,
        Html("<p>Incorrect email or password. <a href='/session/new'>Try again</a>.</p>"),
    )
        .into_response())
}
async fn logout(State(s): State<Arc<AppState>>, headers: HeaderMap, body: Bytes) -> AppResult {
    if let Some(t) = cookie(&headers, "session_token") {
        let db = pool(&s)?;
        let uid: Option<i64> = db
            .query_row("SELECT user_id FROM sessions WHERE token=?1", [&t], |row| {
                row.get(0)
            })
            .optional()
            .map_err(db_err)?;
        if let Some(uid) = uid {
            let endpoint = if headers
                .get(header::CONTENT_TYPE)
                .and_then(|value| value.to_str().ok())
                .unwrap_or("")
                .starts_with("application/x-www-form-urlencoded")
            {
                form_urlencoded::parse(&body).find_map(|(key, value)| {
                    (key == "push_subscription_endpoint").then(|| value.into_owned())
                })
            } else {
                None
            };
            if let Some(endpoint) = endpoint {
                db.execute(
                    "DELETE FROM push_subscriptions WHERE user_id=?1 AND endpoint=?2",
                    params![uid, endpoint],
                )
                .map_err(db_err)?;
                let any: bool = db
                    .query_row(
                        "SELECT EXISTS(SELECT 1 FROM push_subscriptions)",
                        [],
                        |row| row.get(0),
                    )
                    .map_err(db_err)?;
                s.has_push_subscriptions.store(any, Ordering::Relaxed);
            }
            db.execute("DELETE FROM sessions WHERE token=?1", [&t])
                .map_err(db_err)?;
            let _ = s.revoked_users.send(uid);
        }
    }
    let mut r = Redirect::to("/").into_response();
    r.headers_mut().insert(
        header::SET_COOKIE,
        format!(
            "session_token=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{}",
            secure_cookie_suffix()
        )
        .parse()
        .unwrap(),
    );
    Ok(r)
}

fn rooms_for(s: &AppState, uid: i64) -> Result<Vec<Room>, StatusCode> {
    let db = pool(s)?;
    let mut q=db.prepare("SELECT r.id,CASE WHEN r.type='Rooms::Direct' THEN COALESCE((SELECT group_concat(name,', ') FROM (SELECT u2.name FROM users u2 JOIN memberships m2 ON m2.user_id=u2.id WHERE m2.room_id=r.id AND u2.id!=?1 ORDER BY u2.id)),(SELECT name FROM users WHERE id=?1)) ELSE COALESCE(r.name,'') END,r.type,r.creator_id FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE m.user_id=?1 AND m.involvement!='invisible' ORDER BY CASE WHEN r.type='Rooms::Direct' THEN 0 ELSE 1 END,CASE WHEN r.type='Rooms::Direct' THEN r.updated_at END DESC,LOWER(r.name)").map_err(db_err)?;
    let rows = q
        .query_map([uid], |r| {
            Ok(Room {
                id: r.get(0)?,
                name: r.get(1)?,
                kind: r.get(2)?,
                creator_id: r.get(3)?,
            })
        })
        .map_err(db_err)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(db_err)
}
fn sidebar_room_link(room: &Room, active: Option<i64>, unread: bool) -> String {
    format!(
        "<a class='room-link {}' href='/rooms/{}'>{} {}</a>",
        format!(
            "{} {}",
            if active == Some(room.id) {
                "active"
            } else {
                ""
            },
            if unread { "unread" } else { "" }
        ),
        room.id,
        if room.kind == "Rooms::Direct" {
            "↗"
        } else {
            "#"
        },
        esc(&room.name)
    )
}
fn sidebar(s: &AppState, u: &User, active: Option<i64>) -> Result<String, StatusCode> {
    let rooms = rooms_for(s, u.id)?;
    let db = pool(s)?;
    let direct_participants: i64 = db
        .query_row(
            "SELECT COUNT(DISTINCT m2.user_id) FROM memberships m1 JOIN rooms r ON r.id=m1.room_id JOIN memberships m2 ON m2.room_id=r.id WHERE m1.user_id=?1 AND r.type='Rooms::Direct'",
            [u.id],
            |row| row.get(0),
        )
        .map_err(db_err)?;
    let placeholder_limit = (19 - direct_participants).max(0);
    let mut placeholder_query = db
        .prepare("SELECT id,name FROM users WHERE status=0 AND id!=?1 AND id NOT IN (SELECT m2.user_id FROM memberships m1 JOIN rooms r ON r.id=m1.room_id JOIN memberships m2 ON m2.room_id=r.id WHERE m1.user_id=?1 AND r.type='Rooms::Direct') ORDER BY created_at,id LIMIT ?2")
        .map_err(db_err)?;
    let placeholders = placeholder_query
        .query_map(params![u.id, placeholder_limit], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let mut q = db
        .prepare("SELECT room_id FROM memberships WHERE user_id=?1 AND unread_at IS NOT NULL")
        .map_err(db_err)?;
    let unread: HashSet<i64> = q
        .query_map([u.id], |r| r.get(0))
        .map_err(db_err)?
        .collect::<Result<_, _>>()
        .map_err(db_err)?;
    let restricted: bool = db
        .query_row(
            "SELECT restrict_room_creation FROM account_settings WHERE id=1",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let mut html = format!(
        "<aside class='sidebar'><button class='sidebar-close' data-toggle-sidebar aria-label='Close menu'>×</button><div class='sidebar-account'><a class='icon-btn' href='/rooms/directs/new' aria-label='New ping'>✚</a><a class='sidebar-user' href='/users/me/profile'><img src='/users/{}/avatar' alt=''><span>{}</span></a></div><div class='sidebar-title'>Pings</div><nav id='direct-rooms'>",
        u.id,
        esc(u.name.split_whitespace().next().unwrap_or(&u.name))
    );
    for r in rooms.iter().filter(|r| r.kind == "Rooms::Direct") {
        html.push_str(&sidebar_room_link(r, active, unread.contains(&r.id)));
    }
    html.push_str("</nav><div id='direct-placeholders' class='direct-placeholders' aria-label='People you can ping'>");
    for (id, name) in placeholders {
        let first_name = name.split_whitespace().next().unwrap_or(&name);
        html.push_str(&format!(
            "<form method='post' action='/rooms/directs' data-ping-user-id='{id}'><input type='hidden' name='user_ids[]' value='{id}'><input type='hidden' name='authenticity_token' value='{}'><button type='submit' class='direct-placeholder' aria-label='Start a ping with {}'><img src='/users/{id}/avatar' alt=''><span>{}</span></button></form>",
            esc(u.csrf_token.as_deref().unwrap_or("")),
            esc(&name),
            esc(first_name)
        ));
    }
    html.push_str("</div><div class='sidebar-title'>Rooms</div><nav>");
    for r in rooms.iter().filter(|r| r.kind != "Rooms::Direct") {
        html.push_str(&sidebar_room_link(r, active, unread.contains(&r.id)));
    }
    html.push_str("</nav>");
    if is_admin(u) || !restricted {
        html.push_str("<a class='sidebar-new-room' href='/rooms/opens/new' title='New room' aria-label='New room'>+</a>");
    }
    html.push_str("<div class='sidebar-footer'><a href='/searches'>Search</a><a href='/account'>Account</a></div></aside>");
    Ok(html)
}
fn message_list(
    s: &AppState,
    rid: i64,
    limit: i64,
    before: Option<i64>,
    after: Option<i64>,
) -> Result<Vec<ChatMessage>, StatusCode> {
    message_list_with_room_name(s, rid, limit, before, after, true)
}
fn message_list_with_room_name(
    s: &AppState,
    rid: i64,
    limit: i64,
    before: Option<i64>,
    after: Option<i64>,
    include_room_name: bool,
) -> Result<Vec<ChatMessage>, StatusCode> {
    let db = pool(s)?;
    let (predicate, order, cursor) = if let Some(id) = after {
        if id > 0 {
            ("m.created_at_ns>?2", "ASC", Some(id))
        } else {
            ("1=1", "ASC", None)
        }
    } else if let Some(id) = before {
        ("m.created_at_ns<?2", "DESC", Some(id))
    } else {
        ("1=1", "DESC", None)
    };
    let cursor_time = cursor
        .map(|id| {
            db.query_row(
                "SELECT created_at_ns FROM messages WHERE room_id=?1 AND id=?2",
                params![rid, id],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(db_err)?
            .ok_or(StatusCode::NOT_FOUND)
        })
        .transpose()?;
    let sql = format!(
        "SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at FROM messages m INDEXED BY idx_messages_room_created_ns JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND {predicate} ORDER BY m.created_at_ns {order},m.id {order} LIMIT ?3"
    );
    let mut q = db.prepare(&sql).map_err(db_err)?;
    let rows = q
        .query_map(params![rid, cursor_time, limit], chat_message_from_row)
        .map_err(db_err)?;
    let mut v = rows.collect::<Result<Vec<_>, _>>().map_err(db_err)?;
    if after.is_none() {
        v.reverse()
    }
    if include_room_name {
        set_message_room_names(&db, rid, &mut v)?;
    }
    Ok(v)
}
fn message_by_id(s: &AppState, rid: i64, mid: i64) -> Result<ChatMessage, StatusCode> {
    let db = pool(s)?;
    let mut message = db.query_row(
        "SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at FROM messages m JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND m.id=?2",
        params![rid, mid],
        chat_message_from_row,
    )
    .optional()
    .map_err(db_err)?
    .ok_or(StatusCode::NOT_FOUND)?;
    message.room_name = message_room_display_name(&db, rid)?;
    Ok(message)
}
fn messages_after_position(
    s: &AppState,
    rid: i64,
    after: i64,
    limit: i64,
) -> Result<Vec<ChatMessage>, StatusCode> {
    let db = pool(s)?;
    let cursor_time = if after > 0 {
        db.query_row(
            "SELECT created_at_ns FROM messages WHERE room_id=?1 AND id=?2",
            params![rid, after],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map_err(db_err)?
        .ok_or(StatusCode::NOT_FOUND)?
    } else {
        i64::MIN
    };
    let mut query = db.prepare("SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at FROM messages m INDEXED BY idx_messages_room_created_ns JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND (m.created_at_ns,m.id)>(?2,?3) ORDER BY m.created_at_ns,m.id LIMIT ?4").map_err(db_err)?;
    let mut messages = query
        .query_map(
            params![rid, cursor_time, after, limit],
            chat_message_from_row,
        )
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    set_message_room_names(&db, rid, &mut messages)?;
    Ok(messages)
}
fn chat_message_from_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<ChatMessage> {
    Ok(ChatMessage {
        id: r.get(0)?,
        room_id: r.get(1)?,
        room_kind: None,
        room_name: String::new(),
        mention_ids: Vec::new(),
        creator_id: r.get(2)?,
        creator_name: r.get(3)?,
        creator_role: r.get(11)?,
        creator_updated_at: r.get(13)?,
        body: r.get(4)?,
        body_html: r.get(12)?,
        created_at: r.get(5)?,
        updated_at: r.get(14)?,
        client_message_id: r.get(6)?,
        attachment: r.get::<_, Option<i64>>(7)?.map(|id| Attachment {
            id,
            filename: r.get(8).unwrap_or_default(),
            content_type: r.get(9).unwrap_or_default(),
        }),
        boosts: serde_json::from_str(&r.get::<_, String>(10)?).map_err(|error| {
            rusqlite::Error::FromSqlConversionFailure(
                10,
                rusqlite::types::Type::Text,
                Box::new(error),
            )
        })?,
    })
}
fn messages_since(
    s: &AppState,
    rid: i64,
    cutoff_ns: i64,
    updated: bool,
) -> Result<Vec<ChatMessage>, StatusCode> {
    let db = pool(s)?;
    let (predicate, order, index) = if updated {
        (
            "m.created_at_ns<=?2 AND m.updated_at_ns>?2",
            "DESC",
            "idx_messages_room_updated_ns",
        )
    } else {
        ("m.created_at_ns>?2", "ASC", "idx_messages_room_created_ns")
    };
    let sql = format!(
        "SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at FROM messages m INDEXED BY {index} JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND {predicate} ORDER BY m.created_at_ns {order},m.id {order} LIMIT 40"
    );
    let mut query = db.prepare(&sql).map_err(db_err)?;
    let rows = query
        .query_map(params![rid, cutoff_ns], chat_message_from_row)
        .map_err(db_err)?;
    let mut messages = rows.collect::<Result<Vec<_>, _>>().map_err(db_err)?;
    if updated {
        messages.reverse();
    }
    set_message_room_names(&db, rid, &mut messages)?;
    Ok(messages)
}
fn message_room_display_name(db: &rusqlite::Connection, rid: i64) -> Result<String, StatusCode> {
    let (name, kind): (Option<String>, String) = db
        .query_row("SELECT name,type FROM rooms WHERE id=?1", [rid], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .map_err(db_err)?;
    if kind != "Rooms::Direct" {
        return Ok(name.unwrap_or_default());
    }
    let mut query = db
        .prepare("SELECT u.name FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.room_id=?1 ORDER BY m.id")
        .map_err(db_err)?;
    let names = query
        .query_map([rid], |row| row.get::<_, String>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    Ok(match names.as_slice() {
        [] => name.unwrap_or_default(),
        [only] => only.clone(),
        [first, second] => format!("{first} and {second}"),
        _ => format!(
            "{}, and {}",
            names[..names.len() - 1].join(", "),
            names.last().unwrap()
        ),
    })
}
fn set_message_room_names(
    db: &rusqlite::Connection,
    rid: i64,
    messages: &mut [ChatMessage],
) -> Result<(), StatusCode> {
    if !messages.is_empty() {
        let name = message_room_display_name(db, rid)?;
        for message in messages {
            message.room_name = name.clone();
        }
    }
    Ok(())
}
fn boost_html(
    s: &AppState,
    id: i64,
    message_id: i64,
    booster_id: i64,
    booster_name: &str,
    booster_updated_at: &str,
    content: &str,
) -> String {
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let avatar_url = avatar_path(avatar_key, booster_id, booster_updated_at)
        .unwrap_or_else(|_| format!("/users/{booster_id}/avatar"));
    let content_class = if all_emoji(content) {
        "txt-small txt-medium"
    } else {
        "txt-small"
    };
    format!(
        "<div id='boost_{id}' class='boost boost-item flex-inline postion--relative max-width align-center fill-white gap' data-controller='boost-delete' data-boost-delete-perform-class='boost--deleting' data-boost-delete-reveal-class='expanded' data-boost-delete-booster-id-value='{booster_id}'><figure class='avatar boost__avatar flex-item-no-shrink'><a title='{}' class='btn avatar' data-turbo-frame='_top' href='/users/{booster_id}'><img aria-label='{} boosted {}' src='{}' width='48' height='48'></a></figure><span role='button' class='{content_class}' data-action='click-&gt;boost-delete#reveal keydown.enter-&gt;boost-delete#reveal:prevent' data-boost-delete-target='content'>{}</span><form class='button_to' method='post' action='/messages/{message_id}/boosts/{id}'><input type='hidden' name='_method' value='delete'><button data-action='boost-delete#perform' data-boost-delete-target='button' class='btn btn--negative flex-item-justify-end boost__delete' type='submit'><img aria-hidden='true' src='/assets/minus-b31a1093.svg' width='20' height='20'><span class='for-screen-reader'>Delete this boost</span></button></form></div><span id='delete_boost_accessible_label' class='for-screen-reader'>Press enter to delete this boost</span>",
        esc(booster_name),
        esc(booster_name),
        esc(content),
        avatar_url,
        esc(content)
    )
}
fn room_messages_target(kind: &str, room_id: i64) -> Option<String> {
    let class = match kind {
        "Rooms::Open" => "rooms_open",
        "Rooms::Closed" => "rooms_closed",
        "Rooms::Direct" => "rooms_direct",
        _ => return None,
    };
    Some(format!("messages_{class}_{room_id}"))
}
fn attachment_blob_path(s: &AppState, attachment: &Attachment) -> String {
    let key = s
        .imported_blob_signing_key
        .as_deref()
        .unwrap_or(&s.blob_signing_key);
    blob_path(key, attachment.id, &attachment.filename)
        .unwrap_or_else(|_| format!("/attachments/{}", attachment.id))
}
fn message_presentation_html(s: &AppState, m: &ChatMessage) -> String {
    let attachment = m
        .attachment
        .as_ref()
        .map(|a| {
            let filename = esc(&a.filename);
            let blob_url = attachment_blob_path(s, a);
            let download_url = format!("{blob_url}?disposition=attachment");
            if safe_inline_image(&a.content_type) {
                format!("<div class='max-inline-size center overflow-clip'><a class='flex' href='{blob_url}' data-lightbox-target='image' data-action='lightbox#open' data-lightbox-url-value='{download_url}'><img class='message__attachment' src='/attachments/{id}/thumb' alt='{filename}' loading='lazy'></a></div>",id=a.id)
            } else if safe_inline_video(&a.content_type) {
                format!("<div class='max-inline-size center overflow-clip'><video src='{blob_url}' poster='/attachments/{id}/poster' controls preload='none' width='100%' height='100%' class='message__attachment'></video></div>",id=a.id)
            } else {
                format!("<div class='flex-inline align-center gap-half'><img class='colorize--black' aria-hidden='true' src='/assets/common-file-text-9043d980.svg' width='22' height='22'><span>{filename}</span><a class='btn message__action-btn hide-in-ios-pwa' style='--width: auto;' href='{download_url}'><img aria-hidden='true' src='/assets/download-04029899.svg' width='20' height='20'><span class='for-screen-reader'>Download {filename}</span></a><button class='btn message__action-btn' style='--width: auto;' data-controller='web-share' data-action='web-share#share' data-web-share-files-value='{download_url}'><img aria-hidden='true' src='/assets/share-bf28da4f.svg' width='20' height='20'><span class='for-screen-reader'>Share {filename}</span></button></div>")
            }
        })
        .unwrap_or_default();
    let presentation = if m.attachment.is_some() {
        String::new()
    } else {
        sound_presentation(&m.body)
            .or_else(|| {
                m.body_html
                    .as_ref()
                    .map(|html| format!("<div class='trix-content'>{html}</div>"))
            })
            .unwrap_or_else(|| {
                format!(
                    "<div class='trix-content'>{}</div>",
                    esc(&m.body).replace('\n', "<br>")
                )
            })
    };
    format!(
        "<div id='presentation_message_{}' dir='auto' data-reply-target='body' data-messages-target='body'>{presentation}{attachment}</div>",
        esc(&m.client_message_id)
    )
}
fn message_html(s: &AppState, m: &ChatMessage, request_headers: Option<&HeaderMap>) -> String {
    let fallback_headers = HeaderMap::new();
    let request_headers = request_headers.unwrap_or(&fallback_headers);
    let copy_url = html_escape::encode_single_quoted_attribute(&public_url(
        request_headers,
        &format!("/rooms/{}/@{}", m.room_id, m.id),
    ))
    .into_owned();
    let created =
        message_timestamp_ns(&m.created_at).map(chrono::DateTime::<Utc>::from_timestamp_nanos);
    let datetime = created
        .as_ref()
        .map(|date| date.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
        .unwrap_or_else(|| m.created_at.clone());
    let created_ms = message_timestamp_ns(&m.created_at).unwrap_or(0) / 1_000_000;
    let updated_ms = message_timestamp_ns(&m.updated_at).unwrap_or(0) / 1_000_000;
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let creator_avatar = avatar_path(avatar_key, m.creator_id, &m.creator_updated_at)
        .unwrap_or_else(|_| format!("/users/{}/avatar", m.creator_id));
    let presentation = message_presentation_html(s, m);
    let client_id = esc(&m.client_message_id);
    let quick_boosts=[("👍","Thumbs up"),("👏","Clapping"),("👋","Waving hand"),("💪","Muscle"),("❤️","Red heart"),("😂","Face with tears of joy"),("🎉","Party popper"),("🔥","Fire")].iter().map(|(emoji,label)|format!("<form data-turbo-frame='boosting_message_{client_id}' data-action='popup#close' action='/messages/{}/boosts' accept-charset='UTF-8' method='post'><input type='hidden' name='boost[content]' id='boost_content' value='{emoji}'><button name='button' type='submit' title='{label}' class='btn message__action-btn' data-emoji='{emoji}'><figure class='margin-none boost-character'>{emoji}</figure><span class='for-screen-reader'>{label}</span></button></form>",m.id)).collect::<String>();
    let content_action = if m.attachment.is_some() {
        let attachment = m.attachment.as_ref().unwrap();
        let blob_url = attachment_blob_path(s, attachment);
        format!(
            "<a class='btn message__action-btn center full-width hide-in-ios-pwa' href='{blob_url}?disposition=attachment' title='Download' aria-label='Download'><img class='colorize--black' aria-hidden='true' src='/assets/download-04029899.svg' width='20' height='20'></a><button class='btn message__action-btn center full-width' data-controller='web-share' data-action='web-share#share' data-web-share-files-value='{blob_url}' data-web-share-title-value='{filename}' title='Share' aria-label='Share'><img class='colorize--black' aria-hidden='true' src='/assets/share-bf28da4f.svg' width='20' height='20'></button>",
            filename = esc(&attachment.filename)
        )
    } else {
        "<button class='btn message__action-btn center full-width' data-action='reply#reply' title='Reply' aria-label='Reply'><img class='colorize--black' aria-hidden='true' src='/assets/reply-edb77e33.svg' width='20' height='20'></button>".to_string()
    };
    let actions = format!(
        "<details class='position-relative' data-controller='popup' data-action='keydown.esc-&gt;popup#close toggle-&gt;popup#toggle click@document-&gt;popup#closeOnClickOutside' data-popup-orientation-top-class='popup-orientation-top'><summary class='btn message__action-btn message__options-btn'><img class='colorize--black' aria-hidden='true' src='/assets/menu-dots-horizontal-f6a5d793.svg' width='20' height='20'><span class='for-screen-reader'>Message options</span></summary><div class='message__actions-menu border shadow' data-popup-target='menu'><div class='quick-boosts'>{quick_boosts}<a class='btn message__action-btn message__boost-btn' href='/messages/{message_id}/boosts/new' data-turbo-frame='new_boost_message_{client_id}' data-action='soft-keyboard#open popup#close'><img class='colorize--black' aria-hidden='true' src='/assets/boost-4a7bab66.svg' width='20' height='20'><span class='for-screen-reader'>New boost</span></a></div><div class='flex flex-wrap border-top margin-block-start-half pad-block-start-half message__actions-grid'>{content_action}<button class='btn message__action-btn center full-width' title='Copy link' aria-label='Copy link' data-controller='copy-to-clipboard' data-action='copy-to-clipboard#copy' data-copy-to-clipboard-success-class='btn--success' data-copy-to-clipboard-content-value='{copy_url}'><img class='colorize--black' aria-hidden='true' src='/assets/link-e546a5df.svg' width='20' height='20'></button><a class='btn message__action-btn center full-width message__edit-btn' href='/rooms/{room_id}/messages/{message_id}/edit' data-turbo-frame='edit_message_{client_id}' title='Edit' aria-label='Edit'><img class='colorize--black' aria-hidden='true' src='/assets/pencil-cf9d28aa.svg' width='20' height='20'></a></div></div></details>",
        message_id = m.id,
        room_id = m.room_id
    );
    let boosts = m
        .boosts
        .iter()
        .map(|boost| {
            boost_html(
                s,
                boost.id,
                m.id,
                boost.booster_id,
                &boost.booster_name,
                &boost.booster_updated_at,
                &boost.content,
            )
        })
        .collect::<String>();
    let creator_name = esc(&m.creator_name);
    let datetime = esc(&datetime);
    let message_classes = if all_emoji(&m.body) {
        "message message--emoji"
    } else {
        "message "
    };
    let boost_area = format!(
        "<turbo-frame id='boosting_message_{client_id}'><div class='boosts flex flex-wrap align-center gap full-width' style='--column-gap: 0.4ch; --row-gap: 0' data-controller='turbo-streaming' data-action='turbo:submit-start-&gt;turbo-streaming#unsubscribe'><div class='flex-inline flex-wrap gap' id='boosts_message_{client_id}' data-turbo-streaming-target='container'>{boosts}</div><turbo-frame id='new_boost_message_{client_id}'><div class='flex-inline message__boost-inline' data-controller='soft-keyboard'><a class='boost__action txt-small btn' href='/messages/{}/boosts/new' action='soft-keyboard#open'><img aria-hidden='true' src='/assets/boost-4a7bab66.svg' width='20' height='20'><span class='for-screen-reader'>Add a boost</span></a></div></turbo-frame></div></turbo-frame>",
        m.id
    );
    let metadata = format!(
        "<div class='message__meta'><h3 class='message__heading'><span class='message__author' title='{creator_name}'><strong data-reply-target='author'>{creator_name}</strong></span><a class='message__permalink' target='_top' href='/rooms/{room_id}/@{message_id}'><time class='message__timestamp' datetime='{datetime}' data-local-time-target='time'></time></a><span class='message__room'><a href='/rooms/{room_id}/@{message_id}' target='_top' data-reply-target='link'>{room_name}</a></span></h3><div class='message__actions' data-controller='soft-keyboard'>{actions}</div></div>",
        room_id = m.room_id,
        message_id = m.id,
        room_name = esc(&m.room_name),
    );
    format!(
        "<div id='message_{client_id}' class='{message_classes}' data-controller='reply' data-message-id='{message_id}' data-user-id='{creator_id}' data-message-timestamp='{created_ms}' data-message-updated-at='{updated_ms}' data-sort-value='{created_ms}' data-messages-target='message' data-search-results-target='message' data-refresh-room-target='message' data-reply-composer-outlet='#composer'><h2 class='message__day-separator'><time datetime='{datetime}' data-local-time-target='date'></time></h2><figure class='avatar message__avatar'><a title='{creator_name}' class='btn avatar' data-turbo-frame='_top' href='/users/{creator_id}'><img aria-hidden='true' src='{creator_avatar}' width='48' height='48'></a></figure><turbo-frame id='edit_message_{client_id}'><div class='message__body'><div class='message__body-content'>{metadata}{presentation}{boost_area}</div></div></turbo-frame></div>",
        message_id = m.id,
        creator_id = m.creator_id,
    )
}
fn all_emoji(content: &str) -> bool {
    static EMOJI: OnceLock<Regex> = OnceLock::new();
    EMOJI
        .get_or_init(|| {
            Regex::new(r"\A(?:\p{Emoji_Presentation}|\p{Extended_Pictographic}|\u{FE0F})+\z")
                .unwrap()
        })
        .is_match(content)
}
fn sound_presentation(body: &str) -> Option<String> {
    let name = body.strip_prefix("/play ")?;
    if name.is_empty() || !name.bytes().all(|c| c.is_ascii_alphanumeric()) {
        return None;
    }
    const SOUNDS: &str = "56k bell bezos bueller butts clowntown cottoneyejoe crickets curb dadgummit dangerzone danielsan deeper ballmer donotwant drama flawless glados gogogo greatjob greyjoy guarantee heygirl honk horn horror inconceivable letitgo live loggins makeitso noooo nyan ohmy ohyeah pushit rimshot rollout rumble sax secret sexyback story tada tmyk totes trololo trombone unix vuvuzela what whoomp wups yay yeah yodel";
    if !SOUNDS.split_whitespace().any(|sound| sound == name) {
        return None;
    }
    if !std::path::Path::new("static/sounds")
        .join(format!("{name}.mp3"))
        .is_file()
    {
        return None;
    }
    let label = match name {
        "bell" => "🔔",
        "bezos" => "😆💭",
        "bueller" => "anyone?",
        "cottoneyejoe" => "🎶🙉🎶",
        "crickets" => "hears crickets chirping",
        "dadgummit" => "dad gummit!! 🎣",
        "danielsan" => "🎆 🏆 🎆",
        "flawless" => "#flawless",
        "glados" => "🤖💢",
        "gogogo" => "Go, go, go!",
        "greyjoy" => "😖🎺",
        "guarantee" => "guarantees it 👌",
        "heygirl" => "✨💁✨",
        "honk" => "HONK",
        "horn" => "🐶 ✂️ 🐱",
        "horror" => "💀 💀 💀 💀 💀 💀 💀",
        "inconceivable" => "doesn't think it means what you think it means…",
        "letitgo" => "❄️👩❄️⛄️❄️",
        "live" => "is DOING IT LIVE",
        "makeitso" => "make it so 👉",
        "noooo" => "👸💀😒",
        "ohmy" => "raises an eyebrow 😏",
        "ohyeah" => "isn't playing by the rules",
        "rimshot" => "plays a rimshot",
        "rollout" => "is rolling out 🚗",
        "sax" => "🌇🎷🎶",
        "secret" => "found a secret area 🔑",
        "sexyback" => "🔞",
        "story" => "and now you know…",
        "tada" => "plays a fanfare 🎏",
        "tmyk" => "✨ ⭐️ The More You Know ✨ ⭐️",
        "totes" => "😁👍",
        "trololo" => "трололо",
        "trombone" => "plays a sad trombone",
        "unix" => "knows this 💻",
        "vuvuzela" => "======<() ~ ♪ ~♫",
        "whoomp" => "👏‼️😎",
        "wups" => "wups!",
        "yodel" => "📣🗻🙉",
        _ => name,
    };
    let image = if name == "deeper" { "top" } else { name };
    let visual = if std::path::Path::new("static/sound-images")
        .join(format!("{image}.webp"))
        .is_file()
    {
        format!(
            "<img src='/static/sound-images/{image}.webp' alt='{}' loading='lazy'>",
            esc(label)
        )
    } else {
        esc(label)
    };
    Some(format!(
        "<span class='sound'><button type='button' class='sound-play' data-sound='/static/sounds/{name}.mp3' aria-label='Play {name}'>🔊</button> {visual}</span>"
    ))
}
fn safe_inline_image(content_type: &str) -> bool {
    matches!(
        content_type,
        "image/png" | "image/jpeg" | "image/gif" | "image/webp" | "image/avif"
    )
}
fn safe_inline_video(content_type: &str) -> bool {
    matches!(
        content_type,
        "video/mp4" | "video/webm" | "video/quicktime" | "video/ogg"
    )
}
async fn room_show(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    room_show_with_target(s, headers, rid, None).await
}
async fn room_show_at(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, mid)): Path<(i64, i64)>,
) -> AppResult {
    room_show_with_target(s, headers, rid, Some(mid)).await
}
async fn room_show_with_target(
    s: Arc<AppState>,
    headers: HeaderMap,
    rid: i64,
    requested_message: Option<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = match room_for(&s, u.id, rid) {
        Ok(room) => room,
        Err(StatusCode::NOT_FOUND) => return Ok(Redirect::to("/").into_response()),
        Err(error) => return Err(error),
    };
    let refresh_since = Utc::now().timestamp_millis();
    let db = pool(&s)?;
    db.execute(
        "UPDATE memberships SET unread_at=NULL WHERE room_id=?1 AND user_id=?2",
        params![rid, u.id],
    )
    .map_err(db_err)?;
    s.read_events.send(Event {
        room_id: u.id,
        payload: json!({"room_id":rid}).to_string(),
    });
    let target = if let Some(mid) = requested_message {
        let exists: bool = db
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE room_id=?1 AND id=?2)",
                params![rid, mid],
                |row| row.get(0),
            )
            .map_err(db_err)?;
        exists.then_some(mid)
    } else {
        None
    };
    drop(db);
    let messages = if let Some(mid) = target {
        let mut around = message_list(&s, rid, 40, Some(mid), None)?;
        around.push(message_by_id(&s, rid, mid)?);
        around.extend(message_list(&s, rid, 40, None, Some(mid))?);
        around
    } else {
        message_list(&s, rid, 40, None, None)?
    };
    let has_newer: bool = if target.is_some() {
        let last_loaded = messages.last().map(|message| message.id).unwrap_or(0);
        pool(&s)?
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE room_id=?1 AND created_at_ns>(SELECT created_at_ns FROM messages WHERE id=?2 AND room_id=?1))",
                params![rid, last_loaded],
                |row| row.get(0),
            )
            .map_err(db_err)?
    } else {
        false
    };
    let at_message = target.map(|id| id.to_string()).unwrap_or_default();
    let messages_target = room_messages_target(&room.kind, rid).ok_or(StatusCode::NOT_FOUND)?;
    let mut content = format!(
        "<div class='app-shell'>{}<section class='chat' data-room-id='{}' data-at-message='{at_message}' data-history-mode='{has_newer}' data-refresh-since='{refresh_since}'><div class='chat-head'><a class='room-logo' href='/account' aria-label='Account'><img src='/account/logo' alt=''></a><h1 class='room-pill'>{}</h1><div class='room-header-actions'><a class='icon-btn' href='/rooms/{}/edit' aria-label='Room settings'><img src='/assets/menu-dots-horizontal-f6a5d793.svg' alt=''></a><a class='icon-btn' href='/rooms/{}/involvement' aria-label='Notifications'><img src='/static/icons/notification-bell-mentions.svg' alt=''></a><button class='icon-btn menu-toggle' data-toggle-sidebar aria-label='Open menu'><img src='/static/icons/menu.svg' alt=''></button></div></div><div class='messages' id='{}'>",
        sidebar(&s, &u, Some(rid))?,
        rid,
        esc(&room.name),
        rid,
        rid,
        messages_target
    );
    let stream_key = s
        .imported_turbo_stream_signing_key
        .as_deref()
        .unwrap_or(&s.turbo_stream_signing_key);
    let stream_token = room_stream_token(stream_key, &room.kind, rid).map_err(db_err)?;
    content.push_str(&format!(
        "<turbo-cable-stream-source channel='RoomMessagesChannel' signed-stream-name='{}'></turbo-cable-stream-source>",
        esc(&stream_token)
    ));
    for m in messages {
        content.push_str(&message_html(&s, &m, Some(&headers)));
    }
    content.push_str(&format!("</div><div class='typing-indicator' id='typing-indicator' aria-live='polite' hidden></div><form class='composer' id='composer' method='post' enctype='multipart/form-data' action='/rooms/{rid}/messages'><a class='search-round' href='/searches' aria-label='Search'><img src='/static/icons/search.svg' alt=''></a><input type='hidden' id='message-body' name='message[body]'><input type='hidden' name='message[format]' value='html'><trix-editor input='message-body' aria-label='Write a message' aria-controls='mention-suggestions' placeholder='Write a message…'></trix-editor><div class='mention-suggestions' id='mention-suggestions' role='listbox' aria-label='Mention a person' hidden></div><label class='file-btn' title='Attach file'>📎<input type='file' name='message[attachment]'></label><button type='button' id='rich-toggle' class='rich-toggle' aria-label='Rich text toolbar' aria-expanded='false'>A</button><input type='hidden' name='message[client_message_id]' value='{}'><button class='button' aria-label='Send message'>↑</button></form></section></div>",Uuid::new_v4()));
    let mut response = render(&room.name, &content, Some(&u));
    response.headers_mut().append(
        header::SET_COOKIE,
        format!(
            "last_room={rid}; SameSite=Lax; Path=/; Max-Age=630720000{}",
            secure_cookie_suffix()
        )
        .parse()
        .unwrap(),
    );
    Ok(response)
}
#[derive(Deserialize)]
struct RefreshQuery {
    after: Option<i64>,
    since: Option<i64>,
}
async fn room_refresh(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    Query(q): Query<RefreshQuery>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    let checked_at = Utc::now().timestamp_millis();
    let accept = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if let Some(since) = q.since {
        let cutoff_ns = chrono::DateTime::<Utc>::from_timestamp_millis(since)
            .ok_or(StatusCode::BAD_REQUEST)?
            .timestamp_nanos_opt()
            .ok_or(StatusCode::BAD_REQUEST)?;
        let new_messages = messages_since(&s, rid, cutoff_ns, false)?;
        let updated_messages = messages_since(&s, rid, cutoff_ns, true)?;
        if accept.contains("json") {
            let entries = |messages: &[ChatMessage]| {
                messages
                    .iter()
                    .map(|m| json!({"id":m.id,"html":message_html(&s, m, Some(&headers))}))
                    .collect::<Vec<_>>()
            };
            return Ok(Json(json!({
                "messages":entries(&new_messages),
                "updated":entries(&updated_messages),
                "checked_at":checked_at,
                "has_more":new_messages.len() == 40
            }))
            .into_response());
        }
        let mut html = String::new();
        if !new_messages.is_empty() {
            let entries: String = new_messages
                .iter()
                .map(|m| message_html(&s, m, Some(&headers)))
                .collect();
            let target = room_messages_target(&room.kind, rid).ok_or(StatusCode::NOT_FOUND)?;
            html.push_str(&format!("<turbo-stream action='append' target='{target}'><template>{entries}</template></turbo-stream>"));
        }
        for message in &updated_messages {
            html.push_str(&format!("<turbo-stream action='replace' target='message_{}'><template>{}</template></turbo-stream>", esc(&message.client_message_id), message_html(&s, message, Some(&headers))));
        }
        return Ok(([(header::CONTENT_TYPE, "text/vnd.turbo-stream.html")], html).into_response());
    }
    let after = q.after.unwrap_or(0).max(0);
    let mut messages = messages_after_position(&s, rid, after, 101)?;
    let has_more = messages.len() > 100;
    messages.truncate(100);
    let next_after = messages.last().map(|m| m.id).unwrap_or(after);
    if accept.contains("json") {
        let entries: Vec<Value> = messages
            .iter()
            .map(|m| json!({"id":m.id,"html":message_html(&s, m, Some(&headers))}))
            .collect();
        Ok(
            Json(json!({"messages":entries,"next_after":next_after,"has_more":has_more}))
                .into_response(),
        )
    } else {
        let html = if messages.is_empty() {
            String::new()
        } else {
            let entries: String = messages
                .iter()
                .map(|m| message_html(&s, m, Some(&headers)))
                .collect();
            format!(
                "<turbo-stream action='append' target='{}'><template>{entries}</template></turbo-stream>",
                room_messages_target(&room.kind, rid).ok_or(StatusCode::NOT_FOUND)?
            )
        };
        Ok(([(header::CONTENT_TYPE, "text/vnd.turbo-stream.html")], html).into_response())
    }
}
async fn sidebar_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let active = q.get("active").and_then(|value| value.parse().ok());
    let mut response = Html(sidebar(&s, &u, active)?).into_response();
    if let Some(rid) = active {
        response.headers_mut().insert(
            "x-rustfire-active-room-accessible",
            if room_for(&s, u.id, rid).is_ok() {
                "1".parse().unwrap()
            } else {
                "0".parse().unwrap()
            },
        );
    }
    Ok(response)
}
async fn involvement_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    let db = pool(&s)?;
    let current: String = db
        .query_row(
            "SELECT involvement FROM memberships WHERE room_id=?1 AND user_id=?2",
            params![rid, u.id],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let choices = [
        ("everything", "Everything"),
        ("mentions", "Mentions"),
        ("nothing", "Nothing"),
        ("invisible", "Invisible"),
    ]
    .iter()
    .map(|(value, label)| {
        format!(
            "<option value='{value}' {}>{label}</option>",
            if *value == current { "selected" } else { "" }
        )
    })
    .collect::<String>();
    Ok(render(
        "Notifications",
        &format!(
            "<section class='form-card'><h1>{} notifications</h1><form method='post' action='/rooms/{rid}/involvement'><label>Notify me about<select name='involvement'>{choices}</select></label><button class='button'>Save</button></form><p><button type='button' class='button' data-enable-push>Enable browser notifications</button> <a href='/users/me/push_subscriptions'>Manage subscriptions</a></p><p data-push-status role='status'></p></section>",
            esc(&room.name)
        ),
        Some(&u),
    ))
}
async fn push_subscriptions_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let mut query=db.prepare("SELECT id,endpoint,COALESCE(user_agent,'') FROM push_subscriptions WHERE user_id=?1 ORDER BY id DESC").map_err(db_err)?;
    let rows = query
        .query_map([u.id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let mut list = String::new();
    for (id, endpoint, agent) in rows {
        list.push_str(&format!("<li><strong>{}</strong><p class='overflow-ellipsis'>{}</p><form method='post' action='/users/me/push_subscriptions/{id}/test_notifications'><button class='button'>Send test notification</button></form><form method='post' action='/users/me/push_subscriptions/{id}/delete'><button class='button'>Delete subscription</button></form></li>",esc(&agent),esc(&endpoint)));
    }
    Ok(render(
        "Push notification subscriptions",
        &format!(
            "<section class='form-card'><h1>Push notification subscriptions</h1><button type='button' class='button' data-enable-push>Enable browser notifications</button><p data-push-status role='status'></p><ul>{list}</ul></section>"
        ),
        Some(&u),
    ))
}
#[derive(Deserialize)]
struct PushSubscriptionInput {
    endpoint: String,
    p256dh_key: String,
    auth_key: String,
}
#[derive(Deserialize)]
struct PushSubscriptionEnvelope {
    push_subscription: PushSubscriptionInput,
}
async fn push_subscriptions_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    req: Request,
) -> AppResult {
    let u = user(&s, &headers)?;
    let value = if headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .starts_with("application/json")
    {
        Json::<PushSubscriptionEnvelope>::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::UNPROCESSABLE_ENTITY)?
            .0
            .push_subscription
    } else {
        let Form(values) = Form::<HashMap<String, String>>::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::UNPROCESSABLE_ENTITY)?;
        PushSubscriptionInput {
            endpoint: values
                .get("push_subscription[endpoint]")
                .cloned()
                .ok_or(StatusCode::UNPROCESSABLE_ENTITY)?,
            p256dh_key: values
                .get("push_subscription[p256dh_key]")
                .cloned()
                .ok_or(StatusCode::UNPROCESSABLE_ENTITY)?,
            auth_key: values
                .get("push_subscription[auth_key]")
                .cloned()
                .ok_or(StatusCode::UNPROCESSABLE_ENTITY)?,
        }
    };
    if !valid_push_endpoint(&value.endpoint) {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let host = reqwest::Url::parse(&value.endpoint)
        .map_err(|_| StatusCode::UNPROCESSABLE_ENTITY)?
        .host_str()
        .ok_or(StatusCode::UNPROCESSABLE_ENTITY)?
        .to_string();
    let addresses: Vec<_> = tokio::net::lookup_host((host.as_str(), 443))
        .await
        .map_err(|_| StatusCode::UNPROCESSABLE_ENTITY)?
        .collect();
    if addresses.is_empty()
        || addresses
            .iter()
            .any(|address| !public_network_ip(address.ip()))
    {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let agent = headers
        .get(header::USER_AGENT)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let db = pool(&s)?;
    let t = now();
    db.execute("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at) VALUES(?1,?2,?3,?4,?5,?6,?6) ON CONFLICT(user_id,endpoint) DO UPDATE SET p256dh_key=excluded.p256dh_key,auth_key=excluded.auth_key,user_agent=excluded.user_agent,updated_at=excluded.updated_at",params![u.id,value.endpoint,value.p256dh_key,value.auth_key,agent,t]).map_err(db_err)?;
    s.has_push_subscriptions.store(true, Ordering::Relaxed);
    Ok(StatusCode::OK.into_response())
}
async fn push_subscriptions_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    db.execute(
        "DELETE FROM push_subscriptions WHERE id=?1 AND user_id=?2",
        params![id, u.id],
    )
    .map_err(db_err)?;
    let any: bool = db
        .query_row("SELECT EXISTS(SELECT 1 FROM push_subscriptions)", [], |r| {
            r.get(0)
        })
        .map_err(db_err)?;
    s.has_push_subscriptions.store(any, Ordering::Relaxed);
    Ok(Redirect::to("/users/me/push_subscriptions").into_response())
}
async fn push_test_notification(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let subscription=db.query_row("SELECT id,endpoint,p256dh_key,auth_key FROM push_subscriptions WHERE id=?1 AND user_id=?2",params![id,u.id],|r|Ok(PushSubscription{id:r.get(0)?,endpoint:r.get(1)?,p256dh_key:r.get(2)?,auth_key:r.get(3)?})).optional().map_err(db_err)?.ok_or(StatusCode::NOT_FOUND)?;
    drop(db);
    if s.push_delivery_enabled {
        let badge: i64 = pool(&s)?
            .query_row(
                "SELECT count(*) FROM memberships WHERE user_id=?1 AND unread_at IS NOT NULL",
                [u.id],
                |r| r.get(0),
            )
            .map_err(db_err)?;
        let payload=json!({"title":"Campfire Test","options":{"body":Uuid::new_v4().to_string(),"icon":"/account/logo","data":{"path":"/users/me/push_subscriptions","badge":badge}}}).to_string();
        deliver_push(s, subscription, payload)
            .await
            .map_err(|error| {
                eprintln!("Rustfire test push error: {error}");
                StatusCode::BAD_GATEWAY
            })?;
    }
    Ok(Redirect::to("/users/me/push_subscriptions").into_response())
}
#[derive(Deserialize)]
struct InvolvementForm {
    involvement: String,
}
async fn involvement_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    Form(f): Form<InvolvementForm>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    if !["everything", "mentions", "nothing", "invisible"].contains(&f.involvement.as_str()) {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    let previous: String = tx
        .query_row(
            "SELECT involvement FROM memberships WHERE room_id=?1 AND user_id=?2",
            params![rid, u.id],
            |row| row.get(0),
        )
        .map_err(db_err)?;
    tx.execute(
        "UPDATE memberships SET involvement=?1 WHERE room_id=?2 AND user_id=?3",
        params![f.involvement, rid, u.id],
    )
    .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    if room.kind != "Rooms::Direct" && (previous == "invisible") != (f.involvement == "invisible") {
        notify_room_lists(&s, [u.id]);
    }
    Ok(Redirect::to(&format!("/rooms/{rid}/involvement")).into_response())
}
#[derive(Deserialize)]
struct Paging {
    before: Option<i64>,
    after: Option<i64>,
}
async fn messages_index(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    Query(q): Query<Paging>,
) -> AppResult {
    let u = user(&s, &headers)?;
    room_for(&s, u.id, rid)?;
    if let Some(cursor) = q.after.or(q.before) {
        let found: bool = pool(&s)?
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE room_id=?1 AND id=?2)",
                params![rid, cursor],
                |r| r.get(0),
            )
            .map_err(db_err)?;
        if !found {
            return Err(StatusCode::NOT_FOUND);
        }
    }
    let wants_json = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .contains("application/json");
    let messages = message_list_with_room_name(&s, rid, 40, q.before, q.after, !wants_json)?;
    if messages.is_empty() {
        return Ok(StatusCode::NO_CONTENT.into_response());
    }
    if wants_json {
        return Ok(Json(
            messages
                .iter()
                .map(|m| message_json(&s, m, Some(&headers)))
                .collect::<Result<Vec<_>, _>>()?,
        )
        .into_response());
    }
    Ok(Html(
        messages
            .iter()
            .map(|m| message_html(&s, m, Some(&headers)))
            .collect::<String>(),
    )
    .into_response())
}
fn message_json(
    s: &AppState,
    m: &ChatMessage,
    headers: Option<&HeaderMap>,
) -> Result<Value, StatusCode> {
    let message_path = format!("/rooms/{}/messages/{}", m.room_id, m.id);
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let avatar_path = avatar_path(avatar_key, m.creator_id, &m.creator_updated_at)?;
    let (url, avatar_url) = if let Some(headers) = headers {
        (
            public_url(headers, &message_path),
            public_url(headers, &avatar_path),
        )
    } else {
        (message_path, avatar_path)
    };
    let created_at = chrono::DateTime::parse_from_rfc3339(&m.created_at)
        .map(|time| {
            time.with_timezone(&Utc)
                .to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
        })
        .unwrap_or_else(|_| m.created_at.clone());
    let content = m.body_html.clone().unwrap_or_else(|| esc(&m.body));
    let body_html = if content.is_empty() {
        String::new()
    } else {
        format!(
            "<div class=\"trix-content\">\n  {}\n</div>\n",
            content.replace('\n', "\n  ")
        )
    };
    Ok(
        json!({"id":m.id,"created_at":created_at,"body":{"plain_text":if m.body.is_empty(){m.attachment.as_ref().map(|a|a.filename.as_str()).unwrap_or("")}else{&m.body},"html":body_html},"creator":{"id":m.creator_id,"name":m.creator_name,"role":match m.creator_role{1=>"administrator",2=>"bot",_=>"member"},"avatar_url":avatar_url},"room":{"id":m.room_id},"url":url}),
    )
}
#[derive(Deserialize)]
struct PostMessage {
    #[serde(rename = "message[body]")]
    body: Option<String>,
    #[serde(rename = "message[client_message_id]")]
    client_id: Option<String>,
    #[serde(rename = "message[format]")]
    format: Option<String>,
}
struct Upload {
    filename: String,
    content_type: String,
    bytes: Vec<u8>,
}
fn insert_message(
    s: &Arc<AppState>,
    u: &User,
    rid: i64,
    body: &str,
    client_id: Option<String>,
    upload: Option<Upload>,
    rich: bool,
    request_headers: Option<&HeaderMap>,
) -> Result<ChatMessage, StatusCode> {
    let request_host = request_headers
        .and_then(|headers| headers.get(header::HOST))
        .and_then(|value| value.to_str().ok());
    let room = room_for(s, u.id, rid)?;
    let candidate_mentions = if rich {
        mention_ids(
            body,
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
        )
    } else {
        Vec::new()
    };
    let db = pool(s)?;
    let (plain, body_html) = if rich && !body.trim().is_empty() {
        let trusted = replace_mention_attachments(
            body,
            &db,
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
        )?;
        let (plain, html) = rich_body(&trusted, request_host);
        (plain, Some(html))
    } else {
        (body.trim().to_string(), None)
    };
    if plain.is_empty()
        && upload.is_none()
        && !body_html
            .as_ref()
            .is_some_and(|html| html.contains("class=\"og-embed\""))
    {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    };
    let created = Utc::now();
    let t = created.to_rfc3339();
    let created_at_ns = created
        .timestamp_nanos_opt()
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    let cid = client_id.unwrap_or_else(|| Uuid::new_v4().to_string());
    db.execute("INSERT INTO messages(room_id,creator_id,body,body_html,body_source,client_message_id,created_at,created_at_ns,updated_at,updated_at_ns) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?7,?8)",params![rid,u.id,plain,body_html,if rich {Some(body)} else {None},cid,t,created_at_ns]).map_err(db_err)?;
    let id = db.last_insert_rowid();
    touch_room(&db, rid)?;
    let mut valid_mentions = Vec::new();
    for mentioned_id in candidate_mentions {
        let allowed: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.room_id=?1 AND m.user_id=?2 AND u.status=0)",params![rid,mentioned_id],|r|r.get(0)).map_err(db_err)?;
        if allowed {
            db.execute(
                "INSERT OR IGNORE INTO message_mentions(message_id,user_id) VALUES(?1,?2)",
                params![id, mentioned_id],
            )
            .map_err(db_err)?;
            valid_mentions.push(mentioned_id);
        }
    }
    let cutoff = (Utc::now() - Duration::seconds(60)).to_rfc3339();
    let mut unread_stmt = db.prepare("UPDATE memberships SET unread_at=?1 WHERE room_id=?2 AND user_id!=?3 AND involvement!='invisible' AND (connected_at IS NULL OR connected_at<?4) RETURNING user_id").map_err(db_err)?;
    let newly_unread = unread_stmt
        .query_map(params![t, rid, u.id, cutoff], |r| r.get::<_, i64>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(unread_stmt);
    let attachment = if let Some(file) = upload {
        let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
        std::fs::create_dir_all(&dir).map_err(db_err)?;
        let stored = Uuid::new_v4().to_string();
        std::fs::write(std::path::Path::new(&dir).join(&stored), file.bytes).map_err(db_err)?;
        db.execute("INSERT INTO attachments(message_id,filename,content_type,stored_name,created_at) VALUES(?1,?2,?3,?4,?5)",params![id,file.filename,file.content_type,stored,t]).map_err(db_err)?;
        Some(Attachment {
            id: db.last_insert_rowid(),
            filename: file.filename,
            content_type: file.content_type,
        })
    } else {
        None
    };
    let room_name = message_room_display_name(&db, rid)?;
    let m = ChatMessage {
        id,
        room_id: rid,
        room_kind: Some(room.kind),
        room_name,
        mention_ids: valid_mentions,
        creator_id: u.id,
        creator_name: u.name.clone(),
        creator_role: u.role,
        creator_updated_at: u.updated_at.clone(),
        body: plain,
        body_html,
        created_at: t.clone(),
        updated_at: t,
        client_message_id: cid,
        attachment,
        boosts: Vec::new(),
    };
    let _=s.events.send(Event{room_id:rid,payload:json!({"type":"message","room_id":rid,"room_kind":m.room_kind,"message":message_json(s,&m,None)?,"html":message_html(&s, &m, request_headers)}).to_string()});
    for uid in newly_unread {
        s.unread_events.send(Event {
            room_id: uid,
            payload: json!({"roomId":rid}).to_string(),
        });
    }
    if s.push_delivery_enabled && s.has_push_subscriptions.load(Ordering::Relaxed) {
        if let Err(error) = enqueue_push(s, &m) {
            eprintln!("Rustfire push dispatch error: {error}");
        }
    }
    Ok(m)
}
#[derive(Clone)]
struct PushSubscription {
    id: i64,
    endpoint: String,
    p256dh_key: String,
    auth_key: String,
}
struct PushBody(Vec<u8>);
impl From<Vec<u8>> for PushBody {
    fn from(value: Vec<u8>) -> Self {
        Self(value)
    }
}
impl From<&'static str> for PushBody {
    fn from(value: &'static str) -> Self {
        Self(value.as_bytes().to_vec())
    }
}
fn enqueue_push(s: &Arc<AppState>, message: &ChatMessage) -> Result<(), StatusCode> {
    let db = pool(s)?;
    let room_name: String = db
        .query_row(
            "SELECT COALESCE(name,'') FROM rooms WHERE id=?1",
            [message.room_id],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let cutoff = (Utc::now() - Duration::seconds(60)).to_rfc3339();
    let mut query=db.prepare("SELECT p.id,p.endpoint,p.p256dh_key,p.auth_key,m.user_id,(SELECT count(*) FROM memberships um WHERE um.user_id=m.user_id AND um.unread_at IS NOT NULL),m.involvement FROM push_subscriptions p JOIN memberships m ON m.user_id=p.user_id JOIN users u ON u.id=p.user_id WHERE m.room_id=?1 AND m.user_id!=?2 AND u.status=0 AND (m.connected_at IS NULL OR m.connected_at<?3) AND m.involvement IN ('everything','mentions')").map_err(db_err)?;
    let rows = query
        .query_map(params![message.room_id, message.creator_id, cutoff], |r| {
            Ok((
                PushSubscription {
                    id: r.get(0)?,
                    endpoint: r.get(1)?,
                    p256dh_key: r.get(2)?,
                    auth_key: r.get(3)?,
                },
                r.get::<_, i64>(4)?,
                r.get::<_, i64>(5)?,
                r.get::<_, String>(6)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(query);
    drop(db);
    let direct = message.room_kind.as_deref() == Some("Rooms::Direct");
    let title = if direct {
        message.creator_name.clone()
    } else {
        room_name
    };
    let body = if direct {
        message.body.clone()
    } else {
        format!("{}: {}", message.creator_name, message.body)
    };
    let body: String = body.chars().take(2500).collect();
    for (subscription, user_id, badge, involvement) in rows {
        if involvement == "mentions" && !message.mention_ids.contains(&user_id) {
            continue;
        }
        let Ok(permit) = s.push_slots.clone().try_acquire_owned() else {
            break;
        };
        let state = s.clone();
        let payload=json!({"title":title,"options":{"body":body,"icon":"/account/logo","data":{"path":format!("/rooms/{}",message.room_id),"badge":badge}}}).to_string();
        tokio::spawn(async move {
            let _permit = permit;
            if let Err(error) = deliver_push(state, subscription, payload).await {
                eprintln!("Rustfire push delivery error: {error}");
            }
        });
    }
    Ok(())
}
async fn deliver_push(
    s: Arc<AppState>,
    subscription: PushSubscription,
    payload: String,
) -> Result<(), String> {
    if !valid_push_endpoint(&subscription.endpoint)
        || !valid_push_keys(&subscription.p256dh_key, &subscription.auth_key)
    {
        return Err("invalid subscription".into());
    }
    let endpoint = reqwest::Url::parse(&subscription.endpoint).map_err(|e| e.to_string())?;
    let host = endpoint.host_str().ok_or("missing host")?;
    let addresses: Vec<_> = tokio::net::lookup_host((host, 443))
        .await
        .map_err(|e| e.to_string())?
        .collect();
    if addresses.is_empty() || addresses.iter().any(|addr| !public_network_ip(addr.ip())) {
        return Err("push endpoint resolved to a private or invalid address".into());
    }
    let address = addresses[0];
    let info = SubscriptionInfo::new(
        &subscription.endpoint,
        &subscription.p256dh_key,
        &subscription.auth_key,
    );
    let signature = VapidSignatureBuilder::from_der(s.vapid_private.as_slice(), &info)
        .map_err(|e| e.to_string())?
        .build()
        .map_err(|e| e.to_string())?;
    let mut builder = WebPushMessageBuilder::new(&info);
    builder.set_payload(ContentEncoding::Aes128Gcm, payload.as_bytes());
    builder.set_vapid_signature(signature);
    builder.set_urgency(Urgency::High);
    let request =
        request_builder::build_request::<PushBody>(builder.build().map_err(|e| e.to_string())?);
    let client = reqwest::Client::builder()
        .no_proxy()
        .resolve(host, address)
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(7))
        .build()
        .map_err(|e| e.to_string())?;
    let mut outgoing = client.post(&subscription.endpoint);
    for (name, value) in request.headers() {
        outgoing = outgoing.header(name.as_str(), value.to_str().map_err(|e| e.to_string())?);
    }
    let response = outgoing
        .body(request.into_body().0)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if matches!(response.status().as_u16(), 404 | 410) {
        let db = pool(&s).map_err(|e| e.to_string())?;
        db.execute(
            "DELETE FROM push_subscriptions WHERE id=?1",
            [subscription.id],
        )
        .map_err(|e| e.to_string())?;
        let any: bool = db
            .query_row("SELECT EXISTS(SELECT 1 FROM push_subscriptions)", [], |r| {
                r.get(0)
            })
            .map_err(|e| e.to_string())?;
        s.has_push_subscriptions.store(any, Ordering::Relaxed);
    }
    if !response.status().is_success() {
        return Err(format!("push service returned {}", response.status()));
    }
    Ok(())
}
fn enqueue_webhooks(s: &Arc<AppState>, message: &ChatMessage) -> Result<(), StatusCode> {
    let direct = message.room_kind.as_deref() == Some("Rooms::Direct");
    if !direct && message.mention_ids.is_empty() {
        return Ok(());
    }
    let db = pool(s)?;
    let room_name: String = db
        .query_row(
            "SELECT COALESCE(name,'') FROM rooms WHERE id=?1",
            [message.room_id],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let mut q = db.prepare("SELECT u.id,u.name,u.bot_token,w.url FROM memberships m JOIN users u ON u.id=m.user_id JOIN webhooks w ON w.user_id=u.id WHERE m.room_id=?1 AND u.role=2 AND u.status=0 AND u.bot_token IS NOT NULL").map_err(db_err)?;
    let bots = q
        .query_map([message.room_id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(q);
    drop(db);
    for (id, name, key, url) in bots {
        if id == message.creator_id || (!direct && !message.mention_ids.contains(&id)) {
            continue;
        }
        let Ok(permit) = s.webhook_slots.clone().try_acquire_owned() else {
            break;
        };
        let state = s.clone();
        let post = message.clone();
        let room_name = room_name.clone();
        tokio::spawn(async move {
            let _permit = permit;
            deliver_webhook(state, id, name, key, url, room_name, post).await;
        });
    }
    Ok(())
}
async fn deliver_webhook(
    s: Arc<AppState>,
    bot_id: i64,
    bot_name: String,
    key: String,
    url: String,
    room_name: String,
    message: ChatMessage,
) {
    let html = message
        .body_html
        .clone()
        .unwrap_or_else(|| format!("<div>{}</div>", esc(&message.body)));
    let plain = message
        .body
        .replace(&format!("@{bot_name}"), "")
        .trim()
        .to_string();
    let payload = json!({
        "user":{"id":message.creator_id,"name":message.creator_name},
        "room":{"id":message.room_id,"name":room_name,"path":format!("/rooms/{}/{key}/messages",message.room_id)},
        "message":{"id":message.id,"body":{"html":html,"plain":plain},"path":format!("/rooms/{}/@{}",message.room_id,message.id)}
    });
    let reply = match s.webhook_client.post(&url).json(&payload).send().await {
        Ok(response) => response,
        Err(error) => {
            if error.is_timeout() {
                if let Ok(bot) = bot_user(&s, &key) {
                    let _ = insert_message(
                        &s,
                        &bot,
                        message.room_id,
                        "Failed to respond within 7 seconds",
                        None,
                        None,
                        false,
                        None,
                    );
                }
            }
            return;
        }
    };
    if reply.status() != reqwest::StatusCode::OK {
        return;
    }
    let kind = reply
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_string();
    let max = if kind == "text/plain" || kind == "text/html" {
        1024 * 1024
    } else {
        25 * 1024 * 1024
    };
    let mut reply = reply;
    let mut data = Vec::new();
    loop {
        match reply.chunk().await {
            Ok(Some(chunk)) if data.len() + chunk.len() <= max => data.extend_from_slice(&chunk),
            Ok(None) => break,
            _ => return,
        }
    }
    let Ok(bot) = bot_user(&s, &key) else {
        return;
    };
    if bot.id != bot_id {
        return;
    }
    if kind == "text/plain" || kind == "text/html" {
        if let Ok(text) = String::from_utf8(data) {
            if !text.trim().is_empty() {
                let _ = insert_message(&s, &bot, message.room_id, &text, None, None, false, None);
            }
        }
    } else if matches!(
        kind.as_str(),
        "image/png" | "image/jpeg" | "image/gif" | "image/webp" | "application/pdf"
    ) {
        let ext = match kind.as_str() {
            "image/png" => "png",
            "image/jpeg" => "jpg",
            "image/gif" => "gif",
            "image/webp" => "webp",
            _ => "pdf",
        };
        let _ = insert_message(
            &s,
            &bot,
            message.room_id,
            "",
            None,
            Some(Upload {
                filename: format!("attachment.{ext}"),
                content_type: kind,
                bytes: data,
            }),
            false,
            None,
        );
    }
}
async fn message_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    req: Request,
) -> AppResult {
    let u = user(&s, &headers)?;
    let (body, client_id, upload, rich) = if headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .starts_with("multipart/form-data")
    {
        let mut multipart = Multipart::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let mut body = String::new();
        let mut client_id = None;
        let mut upload = None;
        let mut rich = false;
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?
        {
            let name = field.name().unwrap_or("").to_string();
            if name == "message[attachment]" {
                let filename = field.file_name().unwrap_or("attachment").to_string();
                let content_type = field
                    .content_type()
                    .unwrap_or("application/octet-stream")
                    .to_string();
                let bytes = field
                    .bytes()
                    .await
                    .map_err(|_| StatusCode::BAD_REQUEST)?
                    .to_vec();
                if !bytes.is_empty() {
                    upload = Some(Upload {
                        filename,
                        content_type,
                        bytes,
                    })
                }
            } else if name == "message[body]" {
                body = field.text().await.map_err(|_| StatusCode::BAD_REQUEST)?;
            } else if name == "message[client_message_id]" {
                client_id = Some(field.text().await.map_err(|_| StatusCode::BAD_REQUEST)?);
            } else if name == "message[format]" {
                rich = field.text().await.map_err(|_| StatusCode::BAD_REQUEST)? == "html";
            }
        }
        (body, client_id, upload, rich)
    } else {
        let Form(f) = Form::<PostMessage>::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        (
            f.body.unwrap_or_default(),
            f.client_id,
            None,
            f.format.as_deref() == Some("html"),
        )
    };
    let m = insert_message(&s, &u, rid, &body, client_id, upload, rich, Some(&headers))?;
    if s.webhooks_enabled {
        if let Err(error) = enqueue_webhooks(&s, &m) {
            eprintln!("Rustfire webhook dispatch error: {error}");
        }
    }
    let accept = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if accept.contains("json") {
        Ok((
            StatusCode::CREATED,
            Json(message_json(&s, &m, Some(&headers))?),
        )
            .into_response())
    } else if accept.contains("turbo-stream") {
        Ok((
            StatusCode::CREATED,
            Html(format!(
                "<turbo-stream action='append' target='{}'><template>{}</template></turbo-stream>",
                room_messages_target(m.room_kind.as_deref().ok_or(StatusCode::NOT_FOUND)?, rid)
                    .ok_or(StatusCode::NOT_FOUND)?,
                message_html(&s, &m, Some(&headers))
            )),
        )
            .into_response())
    } else {
        Ok(Redirect::to(&format!("/rooms/{rid}")).into_response())
    }
}
async fn message_show(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, mid)): Path<(i64, i64)>,
) -> AppResult {
    let u = user(&s, &headers)?;
    room_for(&s, u.id, rid)?;
    let m = message_by_id(&s, rid, mid)?;
    Ok(if headers.get("x-rustfire-fragment").is_some() {
        Html(message_html(&s, &m, Some(&headers))).into_response()
    } else if headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .contains("json")
    {
        Json(message_json(&s, &m, Some(&headers))?).into_response()
    } else {
        render("Message", &message_html(&s, &m, Some(&headers)), Some(&u))
    })
}
async fn message_edit(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, mid)): Path<(i64, i64)>,
) -> AppResult {
    let u = user(&s, &headers)?;
    room_for(&s, u.id, rid)?;
    let db = pool(&s)?;
    let row: Option<(i64, String, Option<String>)> = db
        .query_row(
            "SELECT creator_id,body,COALESCE(body_source,body_html) FROM messages WHERE id=?1 AND room_id=?2",
            params![mid, rid],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (creator, body, body_html) = row.ok_or(StatusCode::NOT_FOUND)?;
    if !is_admin(&u) && creator != u.id {
        return Err(StatusCode::FORBIDDEN);
    }
    let source = esc(&body_html.unwrap_or_else(|| format!("<div>{}</div>", esc(&body))));
    if headers.get("x-rustfire-inline").is_some() {
        return Ok(Html(format!(
            "<div class='inline-edit'><form method='post' action='/rooms/{rid}/messages/{mid}/update'><input type='hidden' id='edit-body-{mid}' name='message[body]' value='{source}'><input type='hidden' name='message[format]' value='html'><trix-editor input='edit-body-{mid}' aria-label='Edit message'></trix-editor><div class='inline-edit-actions'><button type='submit' class='button'>Save changes</button><button type='button' data-cancel-edit>Cancel</button><button type='button' class='danger' data-delete-message='/rooms/{rid}/messages/{mid}/delete'>Delete message</button></div></form></div>"
        )).into_response());
    }
    Ok(render(
        "Edit message",
        &format!(
            "<section class='form-card'><h1>Edit message</h1><form method='post' action='/rooms/{rid}/messages/{mid}/update'><input type='hidden' id='edit-body' name='message[body]' value='{}'><input type='hidden' name='message[format]' value='html'><trix-editor input='edit-body' aria-label='Edit message'></trix-editor><button class='button'>Save</button></form><form method='post' action='/rooms/{rid}/messages/{mid}/delete'><button class='danger'>Delete</button></form></section>",
            source
        ),
        Some(&u),
    ))
}
async fn message_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, mid)): Path<(i64, i64)>,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    room_for(&s, u.id, rid)?;
    let db = pool(&s)?;
    let creator: Option<i64> = db
        .query_row(
            "SELECT creator_id FROM messages WHERE id=?1 AND room_id=?2",
            params![mid, rid],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    let creator = creator.ok_or(StatusCode::NOT_FOUND)?;
    if !is_admin(&u) && creator != u.id {
        return Err(StatusCode::FORBIDDEN);
    }
    let body = form_value(&f, "body", "message[body]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    let rich = form_value(&f, "format", "message[format]") == Some("html");
    let (plain, body_html) = if rich {
        let trusted = replace_mention_attachments(
            body,
            &db,
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
        )?;
        let (plain, html) = rich_body(
            &trusted,
            headers
                .get(header::HOST)
                .and_then(|value| value.to_str().ok()),
        );
        (plain, Some(html))
    } else {
        (body.to_string(), None)
    };
    if plain.trim().is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let updated_at = now();
    let updated_at_ns =
        message_timestamp_ns(&updated_at).ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    db.execute(
        "UPDATE messages SET body=?1,body_html=?2,body_source=?3,updated_at=?4,updated_at_ns=?5 WHERE id=?6",
        params![
            plain,
            body_html,
            if rich { Some(body) } else { None },
            updated_at,
            updated_at_ns,
            mid
        ],
    )
    .map_err(db_err)?;
    touch_room(&db, rid)?;
    db.execute("DELETE FROM message_mentions WHERE message_id=?1", [mid])
        .map_err(db_err)?;
    if rich {
        for mentioned_id in mention_ids(
            body,
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
        ) {
            let allowed:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.room_id=?1 AND m.user_id=?2 AND u.status=0)",params![rid,mentioned_id],|r|r.get(0)).map_err(db_err)?;
            if allowed {
                db.execute(
                    "INSERT OR IGNORE INTO message_mentions(message_id,user_id) VALUES(?1,?2)",
                    params![mid, mentioned_id],
                )
                .map_err(db_err)?;
            }
        }
    }
    drop(db);
    let updated_message = message_by_id(&s, rid, mid)?;
    let presentation_html = message_presentation_html(&s, &updated_message);
    let _ = s.events.send(Event {
        room_id: rid,
        payload:
            json!({"type":"message_updated","room_id":rid,"id":mid,"client_message_id":updated_message.client_message_id,"body":plain,"html":body_html,"presentation_html":presentation_html})
                .to_string(),
    });
    if headers
        .get(header::ACCEPT)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .contains("json")
    {
        message_show(State(s), headers, Path((rid, mid))).await
    } else {
        Ok(Redirect::to(&format!("/rooms/{rid}/messages/{mid}")).into_response())
    }
}
async fn message_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, mid)): Path<(i64, i64)>,
) -> AppResult {
    let u = user(&s, &headers)?;
    room_for(&s, u.id, rid)?;
    let db = pool(&s)?;
    let target: Option<(i64, String)> = db
        .query_row(
            "SELECT creator_id,client_message_id FROM messages WHERE id=?1 AND room_id=?2",
            params![mid, rid],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (creator, client_message_id) = target.ok_or(StatusCode::NOT_FOUND)?;
    if !is_admin(&u) && creator != u.id {
        return Err(StatusCode::FORBIDDEN);
    }
    let attachment: Option<String> = db
        .query_row(
            "SELECT stored_name FROM attachments WHERE message_id=?1",
            [mid],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    db.execute("DELETE FROM messages WHERE id=?1", [mid])
        .map_err(db_err)?;
    touch_room(&db, rid)?;
    if let Some(stored) = attachment {
        remove_attachment_files(&stored);
    }
    let _ = s.events.send(Event {
        room_id: rid,
        payload: json!({"type":"message_deleted","room_id":rid,"id":mid,"client_message_id":client_message_id}).to_string(),
    });
    if headers
        .get(header::ACCEPT)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .contains("turbo-stream")
    {
        Ok(Html(format!(
            "<turbo-stream action='remove' target='message_{}'></turbo-stream>",
            esc(&client_message_id)
        ))
        .into_response())
    } else {
        Ok(Redirect::to(&format!("/rooms/{rid}")).into_response())
    }
}
async fn new_room(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(kind): Path<String>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let closed = kind == "closeds";
    if !closed && kind != "opens" {
        return Err(StatusCode::NOT_FOUND);
    }
    ensure_room_creation_allowed(&s, &u)?;
    let db = pool(&s)?;
    let mut q = db
        .prepare("SELECT id,name FROM users WHERE status=0 ORDER BY lower(name)")
        .map_err(db_err)?;
    let users = q
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let choices = if closed {
        users.iter().map(|(id,name)|format!("<label class='check'><input type='checkbox' name='user_ids' value='{id}' {}>{}</label>",if *id==u.id{"checked"}else{""},esc(name))).collect::<String>()
    } else {
        String::new()
    };
    let label = if closed { "Private room" } else { "Room" };
    Ok(render(
        &format!("New {label}"),
        &format!(
            "<section class='form-card'><h1>New {label}</h1><form method='post' action='/rooms/{kind}'><label>Name<input name='name' required value='New room'></label>{choices}<button class='button'>Create {label}</button></form></section>"
        ),
        Some(&u),
    ))
}
fn fields(raw: &[u8]) -> (std::collections::HashMap<String, String>, Vec<i64>) {
    let mut values = std::collections::HashMap::new();
    let mut ids = Vec::new();
    for (k, v) in form_urlencoded::parse(raw) {
        if k == "user_ids" || k == "user_ids[]" {
            if let Ok(id) = v.parse() {
                ids.push(id)
            }
        } else {
            values.insert(k.into_owned(), v.into_owned());
        }
    }
    (values, ids)
}
fn form_value<'a>(
    values: &'a HashMap<String, String>,
    flat: &str,
    nested: &str,
) -> Option<&'a str> {
    values
        .get(flat)
        .or_else(|| values.get(nested))
        .map(String::as_str)
}
async fn create_room(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(kind): Path<String>,
    RawForm(raw): RawForm,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !["opens", "closeds"].contains(&kind.as_str()) {
        return Err(StatusCode::NOT_FOUND);
    }
    ensure_room_creation_allowed(&s, &u)?;
    let (values, user_ids) = fields(&raw);
    let name = form_value(&values, "name", "room[name]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    if name.trim().is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let mut db = pool(&s)?;
    let t = now();
    let ty = if kind == "opens" {
        "Rooms::Open"
    } else {
        "Rooms::Closed"
    };
    let tx = db.transaction().map_err(db_err)?;
    tx.execute(
        "INSERT INTO rooms(name,type,creator_id,created_at,updated_at) VALUES(?1,?2,?3,?4,?4)",
        params![name.trim(), ty, u.id, t],
    )
    .map_err(db_err)?;
    let rid = tx.last_insert_rowid();
    let requested: HashSet<i64> = user_ids.into_iter().collect();
    let ids: Vec<i64> = {
        let query = if kind == "opens" {
            "SELECT id FROM users WHERE status=0"
        } else {
            "SELECT id FROM users"
        };
        let mut statement = tx.prepare(query).map_err(db_err)?;
        statement
            .query_map([], |row| row.get::<_, i64>(0))
            .map_err(db_err)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(db_err)?
            .into_iter()
            .filter(|id| kind == "opens" || requested.contains(id))
            .collect()
    };
    for id in &ids {
        tx.execute("INSERT OR IGNORE INTO memberships(room_id,user_id,involvement,created_at) VALUES(?1,?2,'mentions',?3)",params![rid,id,t]).map_err(db_err)?;
    }
    tx.commit().map_err(db_err)?;
    notify_room_lists(&s, ids);
    Ok(Redirect::to(&format!("/rooms/{rid}")).into_response())
}
async fn create_open_room(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    RawForm(raw): RawForm,
) -> AppResult {
    create_room(State(s), headers, Path("opens".to_string()), RawForm(raw)).await
}
async fn create_closed_room(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    RawForm(raw): RawForm,
) -> AppResult {
    create_room(State(s), headers, Path("closeds".to_string()), RawForm(raw)).await
}
async fn room_edit(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let r = room_for(&s, u.id, rid)?;
    if !can_admin(&u, &r) {
        return Err(StatusCode::FORBIDDEN);
    }
    if r.kind == "Rooms::Direct" {
        return Err(StatusCode::FORBIDDEN);
    }
    let db = pool(&s)?;
    let mut q=db.prepare("SELECT u.id,u.name,EXISTS(SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.room_id=?1) FROM users u WHERE u.status=0 ORDER BY lower(u.name)").map_err(db_err)?;
    let choices=q.query_map([rid],|x|Ok((x.get::<_,i64>(0)?,x.get::<_,String>(1)?,x.get::<_,bool>(2)?))).map_err(db_err)?.collect::<Result<Vec<_>,_>>().map_err(db_err)?.iter().map(|(id,name,yes)|format!("<label class='check'><input type='checkbox' name='user_ids' value='{id}' {}>{}</label>",if *yes{"checked"}else{""},esc(name))).collect::<String>();
    let kind = if r.kind == "Rooms::Open" {
        "opens"
    } else {
        "closeds"
    };
    Ok(render(
        "Room settings",
        &format!(
            "<section class='form-card'><h1>Room settings</h1><form method='post' action='/rooms/{rid}/update'><label>Name<input name='name' value='{}' required></label><label>Access<select name='kind'><option value='opens' {}>Everyone</option><option value='closeds' {}>Selected members</option></select></label><div class='member-choices'>{choices}</div><button class='button'>Save</button></form><form method='post' action='/rooms/{rid}/delete'><button class='danger'>Delete room</button></form></section>",
            esc(&r.name),
            if kind == "opens" { "selected" } else { "" },
            if kind == "closeds" { "selected" } else { "" }
        ),
        Some(&u),
    ))
}
async fn room_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    RawForm(raw): RawForm,
) -> AppResult {
    let u = user(&s, &headers)?;
    let (values, user_ids) = fields(&raw);
    let name = form_value(&values, "name", "room[name]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    let kind = form_value(&values, "kind", "room[kind]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    update_room_values(&s, &u, rid, kind, name, user_ids)?;
    Ok(Redirect::to(&format!("/rooms/{rid}")).into_response())
}
fn update_room_values(
    s: &AppState,
    u: &User,
    rid: i64,
    kind: &str,
    name: &str,
    user_ids: Vec<i64>,
) -> Result<(), StatusCode> {
    let r = room_for(s, u.id, rid)?;
    if !can_admin(u, &r) || r.kind == "Rooms::Direct" {
        return Err(StatusCode::FORBIDDEN);
    }
    if name.trim().is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let ty = match kind {
        "opens" => "Rooms::Open",
        "closeds" => "Rooms::Closed",
        _ => return Err(StatusCode::UNPROCESSABLE_ENTITY),
    };
    let mut db = pool(s)?;
    let t = now();
    let tx = db.transaction().map_err(db_err)?;
    let prior_members: HashSet<i64> = {
        let mut statement = tx
            .prepare("SELECT user_id FROM memberships WHERE room_id=?1")
            .map_err(db_err)?;
        statement
            .query_map([rid], |row| row.get(0))
            .map_err(db_err)?
            .collect::<Result<_, _>>()
            .map_err(db_err)?
    };
    tx.execute(
        "UPDATE rooms SET name=?1,type=?2,updated_at=?3 WHERE id=?4",
        params![name.trim(), ty, t, rid],
    )
    .map_err(db_err)?;
    let mut revoked = Vec::new();
    if kind == "closeds" || r.kind != "Rooms::Open" {
        let requested: HashSet<i64> = user_ids.into_iter().collect();
        let desired: HashSet<i64> = {
            let query = if kind == "opens" {
                "SELECT id FROM users WHERE status=0"
            } else {
                "SELECT id FROM users"
            };
            let mut statement = tx.prepare(query).map_err(db_err)?;
            statement
                .query_map([], |row| row.get::<_, i64>(0))
                .map_err(db_err)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(db_err)?
                .into_iter()
                .filter(|id| kind == "opens" || requested.contains(id))
                .collect()
        };
        if kind == "closeds" {
            let current: Vec<i64> = {
                let mut statement = tx
                    .prepare("SELECT user_id FROM memberships WHERE room_id=?1")
                    .map_err(db_err)?;
                statement
                    .query_map([rid], |row| row.get(0))
                    .map_err(db_err)?
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(db_err)?
            };
            for id in current.into_iter().filter(|id| !desired.contains(id)) {
                tx.execute(
                    "DELETE FROM memberships WHERE room_id=?1 AND user_id=?2",
                    params![rid, id],
                )
                .map_err(db_err)?;
                revoked.push(id);
            }
        }
        for id in desired {
            tx.execute("INSERT OR IGNORE INTO memberships(room_id,user_id,involvement,created_at) VALUES(?1,?2,'mentions',?3)",params![rid,id,t]).map_err(db_err)?;
        }
    }
    tx.commit().map_err(db_err)?;
    let current_members: HashSet<i64> = {
        let mut statement = db
            .prepare("SELECT user_id FROM memberships WHERE room_id=?1")
            .map_err(db_err)?;
        statement
            .query_map([rid], |row| row.get(0))
            .map_err(db_err)?
            .collect::<Result<_, _>>()
            .map_err(db_err)?
    };
    notify_room_lists(s, prior_members.union(&current_members).copied());
    for id in revoked {
        let _ = s.revoked_users.send(id);
    }
    Ok(())
}
async fn room_kind_show(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let r = room_for(&s, u.id, rid)?;
    if r.kind == "Rooms::Direct" {
        return Err(StatusCode::NOT_FOUND);
    }
    Ok(Redirect::to(&format!("/rooms/{rid}")).into_response())
}
async fn room_kind_edit(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    room_edit(State(s), headers, Path(rid)).await
}
async fn room_kind_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    Path(rid): Path<i64>,
    RawForm(raw): RawForm,
) -> AppResult {
    let kind = if uri.path().starts_with("/rooms/opens/") {
        "opens"
    } else {
        "closeds"
    };
    let u = user(&s, &headers)?;
    let (values, user_ids) = fields(&raw);
    let name = form_value(&values, "name", "room[name]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    update_room_values(&s, &u, rid, kind, name, user_ids)?;
    Ok(Redirect::to(&format!("/rooms/{rid}")).into_response())
}
async fn room_kind_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if room_for(&s, u.id, rid)?.kind == "Rooms::Direct" {
        return Err(StatusCode::NOT_FOUND);
    }
    room_delete(State(s), headers, Path(rid)).await
}
async fn direct_edit(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    if room.kind != "Rooms::Direct" {
        return Err(StatusCode::NOT_FOUND);
    }
    let db = pool(&s)?;
    let mut stmt = db.prepare("SELECT u.name FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.room_id=?1 AND (u.id!=?2 OR (SELECT count(*) FROM memberships WHERE room_id=?1)=1) ORDER BY lower(u.name)").map_err(db_err)?;
    let people = stmt
        .query_map(params![rid, u.id], |r| r.get::<_, String>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let people = people
        .iter()
        .map(|name| format!("<div class='member'>{}</div>", esc(name)))
        .collect::<String>();
    Ok(render(
        "Ping settings",
        &format!(
            "<section class='form-card'><h1>Ping settings</h1>{people}<form method='post' action='/rooms/directs/{rid}/delete'><button class='danger'>Delete Ping</button></form></section>"
        ),
        Some(&u),
    ))
}
async fn direct_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    if room.kind != "Rooms::Direct" {
        return Err(StatusCode::NOT_FOUND);
    }
    room_delete(State(s), headers, Path(rid)).await
}
async fn direct_show(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    if room.kind != "Rooms::Direct" {
        return Err(StatusCode::NOT_FOUND);
    }
    Ok(Redirect::to(&format!("/rooms/{rid}")).into_response())
}
async fn room_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let r = room_for(&s, u.id, rid)?;
    if !can_admin(&u, &r) {
        return Err(StatusCode::FORBIDDEN);
    }
    let db = pool(&s)?;
    let members: Vec<i64> = {
        let mut statement = db
            .prepare("SELECT user_id FROM memberships WHERE room_id=?1")
            .map_err(db_err)?;
        statement
            .query_map([rid], |row| row.get(0))
            .map_err(db_err)?
            .collect::<Result<_, _>>()
            .map_err(db_err)?
    };
    let mut query = db.prepare("SELECT a.stored_name FROM attachments a JOIN messages m ON m.id=a.message_id WHERE m.room_id=?1").map_err(db_err)?;
    let attachments = query
        .query_map([rid], |row| row.get::<_, String>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(query);
    db.execute("DELETE FROM rooms WHERE id=?1", [rid])
        .map_err(db_err)?;
    for stored in attachments {
        remove_attachment_files(&stored);
    }
    s.events.remove(rid);
    notify_room_lists(&s, members);
    Ok(Redirect::to("/").into_response())
}
async fn direct_new(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    Ok(render(
        "New ping",
        "<section class='form-card'><h1>New ping</h1><form id='ping-form' method='post' action='/rooms/directs'><label for='ping-search'>People to ping</label><div id='ping-selected' class='ping-selected' aria-live='polite'></div><input id='ping-search' name='user_ids_input' role='combobox' aria-autocomplete='list' aria-controls='ping-suggestions' aria-expanded='false' autocomplete='off' autocorrect='off' placeholder='Type a name' required><div id='ping-suggestions' class='ping-suggestions' role='listbox' aria-label='People' hidden></div><button class='button'>Start ping</button></form></section>",
        Some(&u),
    ))
}
async fn direct_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    RawForm(raw): RawForm,
) -> AppResult {
    let u = user(&s, &headers)?;
    let (_, mut ids) = fields(&raw);
    ids.push(u.id);
    ids.sort_unstable();
    ids.dedup();
    let mut db = pool(&s)?;
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(db_err)?;
    let mut names = Vec::new();
    let mut selected = Vec::with_capacity(ids.len());
    for id in ids {
        let name: Option<String> = tx
            .query_row("SELECT name FROM users WHERE id=?1", [id], |r| r.get(0))
            .optional()
            .map_err(db_err)?;
        if let Some(name) = name {
            if id != u.id {
                names.push(name);
            }
            selected.push(id);
        }
    }
    let ids = selected;
    let member_ids = ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",");
    let existing: Option<i64> = tx
        .query_row(
            "SELECT room_id FROM direct_room_sets WHERE member_ids=?1 ORDER BY room_id LIMIT 1",
            [&member_ids],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    if let Some(id) = existing {
        tx.commit().map_err(db_err)?;
        notify_direct_room(&s, id, ids);
        return Ok(found_redirect(&format!("/rooms/{id}")));
    }
    let t = now();
    tx.execute("INSERT INTO rooms(name,type,creator_id,created_at,updated_at) VALUES(?1,'Rooms::Direct',?2,?3,?3)",params![names.join(", "),u.id,t]).map_err(db_err)?;
    let rid = tx.last_insert_rowid();
    for id in &ids {
        tx.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(?1,?2,'everything',?3)",params![rid,id,t]).map_err(db_err)?;
    }
    tx.execute(
        "INSERT INTO direct_room_sets(room_id,member_ids) VALUES(?1,?2)",
        params![rid, member_ids],
    )
    .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    notify_direct_room(&s, rid, ids);
    Ok(found_redirect(&format!("/rooms/{rid}")))
}
async fn search_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(q): Query<std::collections::HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let query = q.get("q").cloned().unwrap_or_default();
    let mut results = String::new();
    let terms = query
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    if !terms.is_empty() {
        let db = pool(&s)?;
        let mut stmt=db.prepare("SELECT m.id,m.room_id,u.name,m.body,m.created_at FROM message_search_index idx JOIN messages m ON m.id=idx.rowid JOIN users u ON u.id=m.creator_id JOIN memberships mem ON mem.room_id=m.room_id WHERE mem.user_id=?1 AND idx.body MATCH ?2 ORDER BY m.id DESC LIMIT 100").map_err(db_err)?;
        let rows = stmt
            .query_map(params![u.id, terms], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                ))
            })
            .map_err(db_err)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(db_err)?;
        for (id, rid, name, body, date) in rows {
            results.push_str(&format!("<a class='search-result' href='/rooms/{rid}/@{id}'><strong>{}</strong><time>{}</time><p>{}</p></a>",esc(&name),esc(&date),esc(&body)));
        }
    }
    let db = pool(&s)?;
    let mut recent_query = db
        .prepare("SELECT query FROM searches WHERE user_id=?1 ORDER BY created_at DESC LIMIT 10")
        .map_err(db_err)?;
    let recent = recent_query
        .query_map([u.id], |r| r.get::<_, String>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?
        .iter()
        .map(|item| {
            let encoded = form_urlencoded::Serializer::new(String::new())
                .append_pair("q", item)
                .finish();
            format!("<li><a href='/searches?{encoded}'>{}</a></li>", esc(item))
        })
        .collect::<String>();
    Ok(render(
        "Search",
        &format!(
            "<section class='search-page'><h1>Search</h1><form method='post' action='/searches'><input name='q' value='{}' placeholder='Search messages' autofocus><button class='button'>Search</button></form>{results}<h2>Recent searches</h2><ul>{recent}</ul><form method='post' action='/searches/clear'><button>Clear searches</button></form></section>",
            esc(&query)
        ),
        Some(&u),
    ))
}
#[derive(Deserialize)]
struct SearchForm {
    q: String,
}
async fn search_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Form(f): Form<SearchForm>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let query = f.q.trim().chars().take(200).collect::<String>();
    if query.is_empty() {
        return Ok(Redirect::to("/searches").into_response());
    }
    pool(&s)?.execute("INSERT INTO searches(user_id,query,created_at) VALUES(?1,?2,?3) ON CONFLICT(user_id,query) DO UPDATE SET created_at=excluded.created_at",params![u.id,query,now()]).map_err(db_err)?;
    let encoded = form_urlencoded::Serializer::new(String::new())
        .append_pair("q", &query)
        .finish();
    Ok(Redirect::to(&format!("/searches?{encoded}")).into_response())
}
async fn search_clear(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    pool(&s)?
        .execute("DELETE FROM searches WHERE user_id=?1", [u.id])
        .map_err(db_err)?;
    Ok(Redirect::to("/searches").into_response())
}
async fn account_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let (name, code): (String, String) = db
        .query_row("SELECT name,join_code FROM accounts LIMIT 1", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .map_err(db_err)?;
    let mut list = String::new();
    let mut q = db
        .prepare("SELECT id,name,email_address,role,status FROM users WHERE status IN (0,2) ORDER BY lower(name)")
        .map_err(db_err)?;
    for row in q
        .query_map([], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, i64>(4)?,
            ))
        })
        .map_err(db_err)?
    {
        let (id, n, email, role, status) = row.map_err(db_err)?;
        if role == 2 {
            continue;
        }
        let controls = if is_admin(&u) && status == 0 {
            format!(
                "<form class='inline-form' method='post' action='/account/users/{id}/role'><select name='role'><option value='member' {}>Member</option><option value='administrator' {}>Administrator</option></select><button>Save</button></form><form class='inline-form' method='post' action='/account/users/{id}/deactivate'><button class='danger'>Deactivate</button></form>",
                if role == 0 { "selected" } else { "" },
                if role == 1 { "selected" } else { "" }
            )
        } else {
            String::new()
        };
        list.push_str(&format!(
            "<li><a href='/users/{id}'>{}</a> · {} {} {controls}</li>",
            esc(&n),
            esc(email.as_deref().unwrap_or("")),
            if status == 2 {
                "(banned)"
            } else if role == 1 {
                "(admin)"
            } else {
                ""
            }
        ));
    }
    let restricted: bool = db
        .query_row(
            "SELECT restrict_room_creation FROM account_settings WHERE id=1",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let account_controls = if is_admin(&u) {
        format!(
            "<form method='post' action='/account/update'><label>Account name<input name='name' value='{}' required></label><input type='hidden' name='restrict_room_creation' value='off'><label class='check'><input type='checkbox' name='restrict_room_creation' value='on' {}>Only administrators can create rooms</label><button class='button'>Save</button></form><p><a href='/account/custom_styles/edit'>Custom styles</a></p><form method='post' action='/account/logo' enctype='multipart/form-data'><label>Account logo<input type='file' name='logo' accept='image/png,image/jpeg,image/gif,image/webp,image/avif' required></label><button>Upload logo</button></form><form method='post' action='/account/logo/delete'><button>Remove logo</button></form><form method='post' action='/account/join_code'><button>Generate a new invite link</button></form>",
            esc(&name),
            if restricted { "checked" } else { "" }
        )
    } else {
        String::new()
    };
    let invite = public_url(&headers, &format!("/join/{code}"));
    let invite_qr = qr_link(&invite, "Show invite QR code");
    Ok(render(
        "Account",
        &format!(
            "<section class='form-card'><h1>{}</h1>{account_controls}<h2>People</h2><ul class='people-list'>{list}</ul><p><a href='/join/{code}'>Invite link</a> · {invite_qr}</p><label>Share this link<input readonly value='{}'></label><p><a href='/account/bots'>Bots</a></p></section>",
            esc(&name),
            esc(&invite)
        ),
        Some(&u),
    ))
}
async fn account_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let name = form_value(&f, "name", "account[name]");
    let restricted = form_value(
        &f,
        "restrict_room_creation",
        "account[settings][restrict_room_creation_to_administrators]",
    );
    if name.is_none() && restricted.is_none() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    if let Some(name) = name {
        if name.trim().is_empty() {
            return Err(StatusCode::UNPROCESSABLE_ENTITY);
        }
        pool(&s)?
            .execute(
                "UPDATE accounts SET name=?1,updated_at=?2",
                params![name.trim(), now()],
            )
            .map_err(db_err)?;
    }
    if let Some(restricted) = restricted {
        let value = matches!(restricted, "1" | "true" | "on");
        pool(&s)?
            .execute(
                "UPDATE account_settings SET restrict_room_creation=?1 WHERE id=1",
                [value],
            )
            .map_err(db_err)?;
    }
    Ok(Redirect::to("/account").into_response())
}
async fn custom_styles_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let styles: String = pool(&s)?
        .query_row(
            "SELECT css FROM account_custom_styles WHERE id=1",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    Ok(render(
        "Custom styles",
        &format!(
            "<section class='form-card'><h1>Custom styles</h1><form method='post' action='/account/custom_styles'><label>CSS<textarea name='account[custom_styles]' rows='20' spellcheck='false'>{}</textarea></label><button class='button'>Save styles</button></form><p><a href='/account'>Back to account</a></p></section>",
            esc(&styles)
        ),
        Some(&u),
    ))
}
async fn custom_styles_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let css = f
        .get("account[custom_styles]")
        .or_else(|| f.get("custom_styles"))
        .ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    if css.len() > 64 * 1024 {
        return Err(StatusCode::PAYLOAD_TOO_LARGE);
    }
    pool(&s)?
        .execute("UPDATE account_custom_styles SET css=?1 WHERE id=1", [css])
        .map_err(db_err)?;
    Ok(Redirect::to("/account/custom_styles/edit").into_response())
}
async fn custom_styles_css(State(s): State<Arc<AppState>>) -> AppResult {
    let css: String = pool(&s)?
        .query_row(
            "SELECT css FROM account_custom_styles WHERE id=1",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let mut response = css.into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        "text/css; charset=utf-8".parse().unwrap(),
    );
    response
        .headers_mut()
        .insert("x-content-type-options", "nosniff".parse().unwrap());
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    Ok(response)
}
async fn logo_get(State(s): State<Arc<AppState>>) -> AppResult {
    let db = pool(&s)?;
    let row: Option<(String, String)> = db
        .query_row(
            "SELECT stored_name,content_type FROM account_logos WHERE id=1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    if let Some((stored, content_type)) = row {
        let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
        let data = tokio::fs::read(std::path::Path::new(&dir).join("logos").join(stored))
            .await
            .map_err(|_| StatusCode::NOT_FOUND)?;
        let mut r = data.into_response();
        r.headers_mut()
            .insert(header::CONTENT_TYPE, content_type.parse().map_err(db_err)?);
        r.headers_mut()
            .insert("x-content-type-options", "nosniff".parse().unwrap());
        r.headers_mut()
            .insert("content-security-policy", "sandbox".parse().unwrap());
        Ok(r)
    } else {
        Ok(Redirect::to("/static/icons/campfire-icon.png").into_response())
    }
}
async fn logo_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let mut upload = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| StatusCode::BAD_REQUEST)?
    {
        if field.name() == Some("logo") {
            let content_type = field
                .content_type()
                .unwrap_or("application/octet-stream")
                .to_string();
            if !safe_inline_image(&content_type) {
                return Err(StatusCode::UNPROCESSABLE_ENTITY);
            }
            let bytes = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
            if bytes.is_empty() || bytes.len() > 5 * 1024 * 1024 {
                return Err(StatusCode::UNPROCESSABLE_ENTITY);
            }
            upload = Some((bytes, content_type));
        }
    }
    let (bytes, content_type) = upload.ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    )
    .join("logos");
    std::fs::create_dir_all(&dir).map_err(db_err)?;
    let stored = Uuid::new_v4().to_string();
    std::fs::write(dir.join(&stored), bytes).map_err(db_err)?;
    let db = pool(&s)?;
    let old: Option<String> = db
        .query_row(
            "SELECT stored_name FROM account_logos WHERE id=1",
            [],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    db.execute("INSERT INTO account_logos(id,stored_name,content_type) VALUES(1,?1,?2) ON CONFLICT(id) DO UPDATE SET stored_name=excluded.stored_name,content_type=excluded.content_type",params![stored,content_type]).map_err(db_err)?;
    if let Some(old) = old {
        let _ = std::fs::remove_file(dir.join(old));
    }
    Ok(Redirect::to("/account").into_response())
}
async fn logo_delete(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let db = pool(&s)?;
    let old: Option<String> = db
        .query_row(
            "SELECT stored_name FROM account_logos WHERE id=1",
            [],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    db.execute("DELETE FROM account_logos WHERE id=1", [])
        .map_err(db_err)?;
    if let Some(old) = old {
        let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
        let _ = std::fs::remove_file(std::path::Path::new(&dir).join("logos").join(old));
    }
    Ok(Redirect::to("/account").into_response())
}
async fn join_code_create(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    pool(&s)?
        .execute(
            "UPDATE accounts SET join_code=?1,updated_at=?2",
            params![Uuid::new_v4().to_string(), now()],
        )
        .map_err(db_err)?;
    Ok(Redirect::to("/account").into_response())
}
async fn user_role_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let role = match form_value(&f, "role", "user[role]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)? {
        "member" => 0,
        "administrator" => 1,
        _ => return Err(StatusCode::UNPROCESSABLE_ENTITY),
    };
    let db = pool(&s)?;
    let prior: Option<i64> = db
        .query_row(
            "SELECT role FROM users WHERE id=?1 AND status=0 AND role!=2",
            [id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    let prior = prior.ok_or(StatusCode::NOT_FOUND)?;
    if prior == 1 && role == 0 {
        let count: i64 = db
            .query_row(
                "SELECT count(*) FROM users WHERE role=1 AND status=0",
                [],
                |r| r.get(0),
            )
            .map_err(db_err)?;
        if count <= 1 {
            return Err(StatusCode::CONFLICT);
        }
    }
    db.execute(
        "UPDATE users SET role=?1,updated_at=?2 WHERE id=?3",
        params![role, now(), id],
    )
    .map_err(db_err)?;
    Ok(Redirect::to("/account").into_response())
}
async fn user_deactivate(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    let account: Option<(i64, Option<String>)> = tx
        .query_row(
            "SELECT role,email_address FROM users WHERE id=?1 AND status=0 AND role!=2",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (role, email) = account.ok_or(StatusCode::NOT_FOUND)?;
    if role == 1 {
        let count: i64 = tx
            .query_row(
                "SELECT count(*) FROM users WHERE role=1 AND status=0",
                [],
                |r| r.get(0),
            )
            .map_err(db_err)?;
        if count <= 1 {
            return Err(StatusCode::CONFLICT);
        }
    }
    tx.execute("DELETE FROM sessions WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM memberships WHERE user_id=?1 AND room_id IN (SELECT id FROM rooms WHERE type!='Rooms::Direct')", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM push_subscriptions WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM searches WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM session_transfers WHERE user_id=?1", [id])
        .map_err(db_err)?;
    let deactivated_email =
        email.map(|value| value.replace('@', &format!("-deactivated-{}@", Uuid::new_v4())));
    tx.execute(
        "UPDATE users SET status=1,email_address=?1,updated_at=?2 WHERE id=?3",
        params![deactivated_email, now(), id],
    )
    .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    let has_push_subscriptions: bool = db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM push_subscriptions)",
            [],
            |row| row.get(0),
        )
        .map_err(db_err)?;
    s.has_push_subscriptions
        .store(has_push_subscriptions, Ordering::Relaxed);
    let _ = s.revoked_users.send(id);
    Ok(Redirect::to("/account").into_response())
}
async fn join_get(State(s): State<Arc<AppState>>, Path(code): Path<String>) -> AppResult {
    let db = pool(&s)?;
    let valid: bool = db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM accounts WHERE join_code=?1)",
            [&code],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    if !valid {
        return Err(StatusCode::NOT_FOUND);
    }
    Ok(render_unauth(
        "Join Rustfire",
        &format!(
            "<section class='auth-card'><h1>Join Rustfire</h1><form method='post' action='/join/{}'>{}{}{}<button class='button'>Join</button></form></section>",
            esc(&code),
            form_field("Your name", "name", "text"),
            form_field("Email address", "email_address", "email"),
            form_field("Password", "password", "password")
        ),
    ))
}
async fn join_post(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Path(code): Path<String>,
    Form(f): Form<Signup>,
) -> AppResult {
    let db = pool(&s)?;
    let valid: bool = db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM accounts WHERE join_code=?1)",
            [code],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    if !valid {
        return Err(StatusCode::NOT_FOUND);
    }
    if f.password.len() < 8 || !f.email_address.contains('@') {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let t = now();
    let pw = hash(&f.password, DEFAULT_COST).map_err(db_err)?;
    db.execute("INSERT INTO users(name,email_address,password_digest,role,status,created_at,updated_at) VALUES(?1,?2,?3,0,0,?4,?4)",params![f.name.trim(),f.email_address.trim().to_lowercase(),pw,t]).map_err(|_|StatusCode::CONFLICT)?;
    let uid = db.last_insert_rowid();
    db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) SELECT id,?1,'mentions',?2 FROM rooms WHERE type='Rooms::Open'",params![uid,t]).map_err(db_err)?;
    create_session(&s, uid, client_ip(&s.trusted_proxies, &headers, addr.ip()))
}
async fn profile(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let transfer = public_url(&headers, &transfer_link(&s, u.id)?);
    let transfer_qr = qr_link(&transfer, "Show private sign-in QR code");
    let db = pool(&s)?;
    let bio: String = db
        .query_row(
            "SELECT COALESCE(bio,'') FROM users WHERE id=?1",
            [u.id],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    Ok(render(
        "Profile",
        &format!(
            "<section class='form-card'><h1>My profile</h1><img class='profile-avatar' src='/users/{}/avatar' alt='Your avatar'><form method='post' action='/users/me/avatar' enctype='multipart/form-data'><label>Avatar<input type='file' name='avatar' accept='image/png,image/jpeg,image/gif,image/webp,image/avif' required></label><button>Update avatar</button></form><form method='post' action='/users/me/avatar/delete'><button>Remove avatar</button></form><form method='post' action='/users/me/profile'><label>Name<input name='name' value='{}' required></label><label>Email<input type='email' name='email_address' value='{}'></label><label>New password<input type='password' name='password' minlength='8' autocomplete='new-password'></label><label>Bio<textarea name='bio' maxlength='200'>{}</textarea></label><button class='button'>Save</button></form><label>Private sign-in link (expires in four hours)<input readonly value='{}'></label><p>{transfer_qr}</p><p><a href='/users/me/push_subscriptions'>Push notification subscriptions</a></p></section>",
            u.id,
            esc(&u.name),
            esc(&u.email),
            esc(&bio),
            esc(&transfer)
        ),
        Some(&u),
    ))
}
async fn profile_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let name = form_value(&f, "name", "user[name]").unwrap_or(&u.name);
    if name.trim().is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let email = match form_value(&f, "email_address", "user[email_address]") {
        Some(e) if e.trim().is_empty() => None,
        Some(e) if e.contains('@') => Some(e.trim().to_lowercase()),
        Some(_) => return Err(StatusCode::UNPROCESSABLE_ENTITY),
        None => {
            if u.email.is_empty() {
                None
            } else {
                Some(u.email.clone())
            }
        }
    };
    let password = match form_value(&f, "password", "user[password]") {
        Some(p) if p.is_empty() => None,
        Some(p) if p.len() >= 8 => Some(hash(p, DEFAULT_COST).map_err(db_err)?),
        Some(_) => return Err(StatusCode::UNPROCESSABLE_ENTITY),
        None => None,
    };
    let bio = form_value(&f, "bio", "user[bio]").map(|s| s.chars().take(200).collect::<String>());
    pool(&s)?
        .execute(
            "UPDATE users SET name=?1,email_address=?2,bio=COALESCE(?3,bio),password_digest=COALESCE(?4,password_digest),updated_at=?5 WHERE id=?6",
            params![name.trim(),email,bio,password,now(),u.id],
        )
        .map_err(|_|StatusCode::CONFLICT)?;
    Ok(Redirect::to("/users/me/profile").into_response())
}
async fn avatar_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(token): Path<String>,
) -> AppResult {
    let _viewer = user(&s, &headers)?;
    let id = token
        .parse::<i64>()
        .ok()
        .filter(|id| *id > 0)
        .or_else(|| {
            avatar_id_from_token(&s.avatar_signing_key, &token).or_else(|| {
                s.imported_avatar_signing_key
                    .as_deref()
                    .and_then(|key| avatar_id_from_token(key, &token))
            })
        })
        .ok_or(StatusCode::NOT_FOUND)?;
    let db = pool(&s)?;
    let account: Option<(String, i64)> = db
        .query_row(
            "SELECT name,role FROM users WHERE id=?1 AND status=0",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (name, role) = account.ok_or(StatusCode::NOT_FOUND)?;
    let row: Option<(String, String)> = db
        .query_row(
            "SELECT stored_name,content_type FROM avatars WHERE user_id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    if let Some((stored, _content_type)) = row {
        if let Some(data) = avatar_webp_variant(&s, &stored).await {
            let mut response = data.into_response();
            response
                .headers_mut()
                .insert(header::CONTENT_TYPE, "image/webp".parse().unwrap());
            response.headers_mut().insert(
                header::CACHE_CONTROL,
                "public, max-age=1800, stale-while-revalidate=604800"
                    .parse()
                    .unwrap(),
            );
            response
                .headers_mut()
                .insert("x-content-type-options", "nosniff".parse().unwrap());
            return Ok(response);
        }
    }
    let body = if role == 2 {
        include_str!("../static/icons/default-bot-avatar.svg").to_string()
    } else {
        avatar_initials_svg(id, &name)
    };
    let mut response = body.into_response();
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, "image/svg+xml".parse().unwrap());
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        "public, max-age=1800, stale-while-revalidate=604800"
            .parse()
            .unwrap(),
    );
    response
        .headers_mut()
        .insert("x-content-type-options", "nosniff".parse().unwrap());
    Ok(response)
}
async fn avatar_webp_variant(s: &AppState, stored: &str) -> Option<Vec<u8>> {
    if Uuid::parse_str(stored).is_err() {
        return None;
    }
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    )
    .join("avatars");
    let input = dir.join(stored);
    let cache = dir.join("variants");
    tokio::fs::create_dir_all(&cache).await.ok()?;
    let output = cache.join(format!("{stored}.webp"));
    if tokio::fs::metadata(&output).await.is_err() {
        let _permit = s.variant_slots.acquire().await.ok()?;
        if tokio::fs::metadata(&output).await.is_err() {
            let temporary = cache.join(format!("{stored}-{}.webp", Uuid::new_v4()));
            let result = tokio::time::timeout(
                std::time::Duration::from_secs(10),
                tokio::process::Command::new("vips")
                    .arg("thumbnail")
                    .arg(&input)
                    .arg(&temporary)
                    .args(["512", "--height", "512", "--size", "down"])
                    .kill_on_drop(true)
                    .output(),
            )
            .await
            .ok()
            .and_then(Result::ok);
            if result
                .as_ref()
                .is_some_and(|result| result.status.success())
            {
                if tokio::fs::rename(&temporary, &output).await.is_err() {
                    let _ = tokio::fs::remove_file(&temporary).await;
                    return None;
                }
            } else {
                let _ = tokio::fs::remove_file(&temporary).await;
                return None;
            }
        }
    }
    tokio::fs::read(output).await.ok()
}
async fn read_avatar(mut multipart: Multipart) -> Result<(Vec<u8>, String), StatusCode> {
    let mut upload = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| StatusCode::BAD_REQUEST)?
    {
        if field.name() == Some("avatar") {
            let content_type = field
                .content_type()
                .unwrap_or("application/octet-stream")
                .to_string();
            if !safe_inline_image(&content_type) {
                return Err(StatusCode::UNPROCESSABLE_ENTITY);
            }
            let bytes = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
            if bytes.is_empty() || bytes.len() > 5 * 1024 * 1024 {
                return Err(StatusCode::UNPROCESSABLE_ENTITY);
            }
            upload = Some((bytes.to_vec(), content_type));
        }
    }
    upload.ok_or(StatusCode::UNPROCESSABLE_ENTITY)
}
fn save_avatar(
    s: &AppState,
    uid: i64,
    bytes: Vec<u8>,
    content_type: String,
) -> Result<(), StatusCode> {
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    )
    .join("avatars");
    std::fs::create_dir_all(&dir).map_err(db_err)?;
    let stored = Uuid::new_v4().to_string();
    std::fs::write(dir.join(&stored), bytes).map_err(db_err)?;
    let db = pool(s)?;
    let old: Option<String> = db
        .query_row(
            "SELECT stored_name FROM avatars WHERE user_id=?1",
            [uid],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    if let Err(error) = db.execute("INSERT INTO avatars(user_id,stored_name,content_type) VALUES(?1,?2,?3) ON CONFLICT(user_id) DO UPDATE SET stored_name=excluded.stored_name,content_type=excluded.content_type",params![uid,stored,content_type]) {
        let _ = std::fs::remove_file(dir.join(&stored));
        return Err(db_err(error));
    }
    db.execute(
        "UPDATE users SET updated_at=?1 WHERE id=?2",
        params![now(), uid],
    )
    .map_err(db_err)?;
    if let Some(old) = old {
        remove_avatar_files(&old);
    }
    Ok(())
}
fn remove_avatar_files(stored: &str) {
    if Uuid::parse_str(stored).is_err() {
        return;
    }
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    )
    .join("avatars");
    let _ = std::fs::remove_file(dir.join(stored));
    let _ = std::fs::remove_file(dir.join("variants").join(format!("{stored}.webp")));
}
async fn avatar_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    multipart: Multipart,
) -> AppResult {
    let u = user(&s, &headers)?;
    let (bytes, content_type) = read_avatar(multipart).await?;
    save_avatar(&s, u.id, bytes, content_type)?;
    Ok(Redirect::to("/users/me/profile").into_response())
}
async fn avatar_delete(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let old: Option<String> = db
        .query_row(
            "SELECT stored_name FROM avatars WHERE user_id=?1",
            [u.id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    db.execute("DELETE FROM avatars WHERE user_id=?1", [u.id])
        .map_err(db_err)?;
    db.execute(
        "UPDATE users SET updated_at=?1 WHERE id=?2",
        params![now(), u.id],
    )
    .map_err(db_err)?;
    if let Some(old) = old {
        remove_avatar_files(&old);
    }
    Ok(Redirect::to("/users/me/profile").into_response())
}
async fn user_show(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let row = db
        .query_row(
            "SELECT name,COALESCE(bio,''),status FROM users WHERE id=?1 AND status!=1",
            [id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                ))
            },
        )
        .optional()
        .map_err(db_err)?
        .ok_or(StatusCode::NOT_FOUND)?;
    let controls = if is_admin(&u) && u.id != id {
        let action = if row.2 == 2 { "unban" } else { "ban" };
        let action_path = if row.2 == 2 {
            format!("/users/{id}/ban/delete")
        } else {
            format!("/users/{id}/ban")
        };
        let transfer = if row.2 == 0 {
            format!(
                "<label>Private sign-in link (expires in four hours)<input readonly value='{}'></label>",
                esc(&transfer_link(&s, id)?)
            )
        } else {
            String::new()
        };
        format!(
            "{transfer}<form method='post' action='{action_path}'><button class='button'>{action}</button></form>"
        )
    } else {
        String::new()
    };
    Ok(render(
        &row.0,
        &format!(
            "<section class='form-card'><h1>{}</h1><p>{}</p><form method='post' action='/rooms/directs'><input type='hidden' name='user_ids' value='{id}'><button class='button'>Ping</button></form>{controls}</section>",
            esc(&row.0),
            esc(&row.1)
        ),
        Some(&u),
    ))
}
async fn user_ban(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let admin = user(&s, &headers)?;
    if !is_admin(&admin) {
        return Err(StatusCode::FORBIDDEN);
    }
    if admin.id == id {
        return Err(StatusCode::CONFLICT);
    }
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    let status: Option<i64> = tx
        .query_row(
            "SELECT status FROM users WHERE id=?1 AND role!=2",
            [id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    if status != Some(0) {
        return Err(StatusCode::NOT_FOUND);
    }
    let mut st = tx
        .prepare("SELECT id,room_id,client_message_id FROM messages WHERE creator_id=?1")
        .map_err(db_err)?;
    let removed = st
        .query_map([id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(st);
    let mut ip_stmt = tx
        .prepare(
            "SELECT DISTINCT ip_address FROM sessions WHERE user_id=?1 AND ip_address IS NOT NULL",
        )
        .map_err(db_err)?;
    let ips = ip_stmt
        .query_map([id], |r| r.get::<_, String>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(ip_stmt);
    for ip in ips {
        if ip.parse::<IpAddr>().map(public_ip).unwrap_or(false) {
            tx.execute(
                "INSERT INTO bans(user_id,ip_address) VALUES(?1,?2)",
                params![id, ip],
            )
            .map_err(db_err)?;
        }
    }
    tx.execute("DELETE FROM messages WHERE creator_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM sessions WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM session_transfers WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute(
        "UPDATE users SET status=2,updated_at=?1 WHERE id=?2",
        params![now(), id],
    )
    .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    let _ = s.revoked_users.send(id);
    for (mid, rid, client_message_id) in removed {
        s.events.send(Event {
            room_id: rid,
            payload: json!({"type":"message_deleted","room_id":rid,"id":mid,"client_message_id":client_message_id}).to_string(),
        });
    }
    Ok(Redirect::to(&format!("/users/{id}")).into_response())
}
async fn user_unban(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let admin = user(&s, &headers)?;
    if !is_admin(&admin) {
        return Err(StatusCode::FORBIDDEN);
    }
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    let changed = tx
        .execute(
            "UPDATE users SET status=0,updated_at=?1 WHERE id=?2 AND status=2 AND role!=2",
            params![now(), id],
        )
        .map_err(db_err)?;
    if changed == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    tx.execute("DELETE FROM bans WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    Ok(Redirect::to(&format!("/users/{id}")).into_response())
}
async fn autocomplete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let room_id = q
        .get("room_id")
        .map(|id| id.parse::<i64>().map_err(|_| StatusCode::BAD_REQUEST))
        .transpose()?;
    if let Some(rid) = room_id {
        room_for(&s, u.id, rid)?;
    }
    let query = q
        .get("q")
        .or_else(|| q.get("query"))
        .cloned()
        .unwrap_or_default();
    let mut st=db.prepare("SELECT u.id,u.name,u.updated_at FROM users u WHERE u.status=0 AND u.name LIKE ?1 AND (?2 IS NULL OR EXISTS(SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.room_id=?2)) ORDER BY lower(u.name) LIMIT 20").map_err(db_err)?;
    let users = st
        .query_map(params![format!("%{query}%"), room_id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let signing_key = s
        .imported_mention_signing_key
        .as_deref()
        .unwrap_or(&s.mention_signing_key);
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let rows = users
        .into_iter()
        .map(|(id, name, updated_at)| {
            Ok(json!({
                "value": id,
                "name": esc(&name),
                "avatar_url": public_url(&headers, &avatar_path(avatar_key, id, &updated_at)?),
                "sgid": mention_sgid(signing_key, id).map_err(db_err)?,
            }))
        })
        .collect::<Result<Vec<_>, StatusCode>>()?;
    Ok(Json(rows).into_response())
}
async fn bots_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let db = pool(&s)?;
    let mut q = db
        .prepare("SELECT id,name,bot_token FROM users WHERE bot_token IS NOT NULL AND status=0")
        .map_err(db_err)?;
    let rows = q
        .query_map([], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let mut list = String::new();
    for (id, name, key) in rows {
        list.push_str(&format!("<li>{} · key: <code>{}</code> <a href='/account/bots/{id}/edit'>Edit</a> <form method='post' action='/account/bots/{id}/delete'><button>Delete</button></form></li>",esc(&name),esc(&key)));
    }
    Ok(render(
        "Bots",
        &format!(
            "<section class='form-card'><h1>Bots</h1><ul>{list}</ul><form method='post' action='/account/bots'><label>Name<input name='name' required></label><label>Webhook URL<input type='url' name='webhook_url'></label><button class='button'>Create bot</button></form></section>"
        ),
        Some(&u),
    ))
}
async fn bot_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let name = form_value(&f, "name", "user[name]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    if name.trim().is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let webhook_url = form_value(&f, "webhook_url", "user[webhook_url]")
        .unwrap_or("")
        .trim();
    if !valid_webhook_url(webhook_url) {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let db = pool(&s)?;
    let t = now();
    db.execute("INSERT INTO users(name,role,status,bot_token,created_at,updated_at) VALUES(?1,2,0,NULL,?2,?2)",params![name.trim(),t]).map_err(db_err)?;
    let id = db.last_insert_rowid();
    let bot_key = format!("{id}-{}", &Uuid::new_v4().simple().to_string()[..12]);
    db.execute(
        "UPDATE users SET bot_token=?1 WHERE id=?2",
        params![bot_key, id],
    )
    .map_err(db_err)?;
    if !webhook_url.is_empty() {
        db.execute(
            "INSERT INTO webhooks(user_id,url) VALUES(?1,?2)",
            params![id, webhook_url],
        )
        .map_err(db_err)?;
    }
    db.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) SELECT id,?1,'mentions',?2 FROM rooms WHERE type='Rooms::Open'",params![id,t]).map_err(db_err)?;
    Ok(Redirect::to("/account/bots").into_response())
}
async fn bot_edit(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let db = pool(&s)?;
    let bot: Option<(String, String, String)> = db.query_row(
        "SELECT u.name,u.bot_token,COALESCE(w.url,'') FROM users u LEFT JOIN webhooks w ON w.user_id=u.id WHERE u.id=?1 AND u.role=2 AND u.status=0 AND u.bot_token IS NOT NULL",
        [id],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    ).optional().map_err(db_err)?;
    let (name, key, webhook_url) = bot.ok_or(StatusCode::NOT_FOUND)?;
    Ok(render(
        "Edit bot",
        &format!(
            "<section class='form-card'><h1>Edit bot</h1><img class='avatar-large' src='/users/{id}/avatar' alt='Bot avatar'><form method='post' action='/account/bots/{id}/update'><label>Name<input name='name' value='{}' required></label><label>Webhook URL<input type='url' name='webhook_url' value='{}'></label><button class='button'>Save</button></form><form method='post' action='/account/bots/{id}/avatar' enctype='multipart/form-data'><label>Bot avatar<input type='file' name='avatar' accept='image/png,image/jpeg,image/gif,image/webp,image/avif' required></label><button>Upload avatar</button></form><form method='post' action='/account/bots/{id}/avatar/delete'><button>Remove avatar</button></form><p>Key: <code>{}</code></p><form method='post' action='/account/bots/{id}/key'><button>Generate a new key</button></form><form method='post' action='/account/bots/{id}/delete'><button class='danger'>Delete bot</button></form><p><a href='/account/bots'>Back to bots</a></p></section>",
            esc(&name),
            esc(&webhook_url),
            esc(&key)
        ),
        Some(&u),
    ))
}
async fn bot_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let name = form_value(&f, "name", "user[name]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    if name.trim().is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let webhook_url = form_value(&f, "webhook_url", "user[webhook_url]").map(str::trim);
    if webhook_url.is_some_and(|url| !valid_webhook_url(url)) {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let db = pool(&s)?;
    let changed = db.execute(
        "UPDATE users SET name=?1,updated_at=?2 WHERE id=?3 AND role=2 AND status=0 AND bot_token IS NOT NULL",
        params![name.trim(),now(),id],
    ).map_err(db_err)?;
    if changed == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    if let Some(url) = webhook_url {
        if url.is_empty() {
            db.execute("DELETE FROM webhooks WHERE user_id=?1", [id])
                .map_err(db_err)?;
        } else {
            db.execute("INSERT INTO webhooks(user_id,url) VALUES(?1,?2) ON CONFLICT(user_id) DO UPDATE SET url=excluded.url",params![id,url]).map_err(db_err)?;
        }
    }
    Ok(Redirect::to("/account/bots").into_response())
}
fn active_bot(s: &AppState, id: i64) -> Result<(), StatusCode> {
    let found: bool = pool(s)?.query_row(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id=?1 AND role=2 AND status=0 AND bot_token IS NOT NULL)",
        [id], |r| r.get(0),
    ).map_err(db_err)?;
    if found {
        Ok(())
    } else {
        Err(StatusCode::NOT_FOUND)
    }
}
async fn bot_avatar_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    multipart: Multipart,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    active_bot(&s, id)?;
    let (bytes, content_type) = read_avatar(multipart).await?;
    save_avatar(&s, id, bytes, content_type)?;
    Ok(Redirect::to(&format!("/account/bots/{id}/edit")).into_response())
}
async fn bot_avatar_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    active_bot(&s, id)?;
    let db = pool(&s)?;
    let old: Option<String> = db
        .query_row(
            "SELECT stored_name FROM avatars WHERE user_id=?1",
            [id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    db.execute("DELETE FROM avatars WHERE user_id=?1", [id])
        .map_err(db_err)?;
    db.execute(
        "UPDATE users SET updated_at=?1 WHERE id=?2",
        params![now(), id],
    )
    .map_err(db_err)?;
    if let Some(old) = old {
        remove_avatar_files(&old);
    }
    Ok(Redirect::to(&format!("/account/bots/{id}/edit")).into_response())
}
async fn bot_key_rotate(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let new_key = format!("{id}-{}", &Uuid::new_v4().simple().to_string()[..12]);
    let changed = pool(&s)?.execute(
        "UPDATE users SET bot_token=?1,updated_at=?2 WHERE id=?3 AND role=2 AND status=0 AND bot_token IS NOT NULL",
        params![new_key,now(),id],
    ).map_err(db_err)?;
    if changed == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    Ok(Redirect::to("/account/bots").into_response())
}
async fn bot_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    pool(&s)?
        .execute("UPDATE users SET status=1,bot_token=NULL,updated_at=?1 WHERE id=?2 AND bot_token IS NOT NULL",params![now(),id])
        .map_err(db_err)?;
    Ok(Redirect::to("/account/bots").into_response())
}
async fn bot_messages_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, key)): Path<(i64, String)>,
    Query(q): Query<Paging>,
) -> AppResult {
    let u = bot_user(&s, &key)?;
    room_for(&s, u.id, rid)?;
    if let Some(cursor) = q.after.or(q.before) {
        let found: bool = pool(&s)?
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE room_id=?1 AND id=?2)",
                params![rid, cursor],
                |r| r.get(0),
            )
            .map_err(db_err)?;
        if !found {
            return Err(StatusCode::NOT_FOUND);
        }
    }
    let messages = message_list_with_room_name(&s, rid, 40, q.before, q.after, false)?;
    let db = pool(&s)?;
    let count: i64 = db
        .query_row(
            "SELECT count(*) FROM messages WHERE room_id=?1",
            [rid],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let mut r = Json(
        messages
            .iter()
            .map(|m| message_json(&s, m, Some(&headers)))
            .collect::<Result<Vec<_>, _>>()?,
    )
    .into_response();
    r.headers_mut()
        .insert("x-total-count", count.to_string().parse().unwrap());
    if let (Some(first), Some(last)) = (messages.first(), messages.last()) {
        let (direction, cursor) = if q.after.is_some() {
            ("after", last.id)
        } else {
            ("before", first.id)
        };
        let comparison = if direction == "after" { ">" } else { "<" };
        let exists: bool = db
            .query_row(
                &format!(
                    "SELECT EXISTS(SELECT 1 FROM messages WHERE room_id=?1 AND created_at_ns{comparison}(SELECT created_at_ns FROM messages WHERE room_id=?1 AND id=?2))"
                ),
                params![rid, cursor],
                |r| r.get(0),
            )
            .map_err(db_err)?;
        if exists {
            let next = public_url(
                &headers,
                &format!("/rooms/{rid}/{key}/messages?{direction}={cursor}"),
            );
            let link = format!("<{next}>; rel=\"next\"");
            r.headers_mut()
                .insert(header::LINK, link.parse().map_err(db_err)?);
        }
    }
    Ok(r)
}
async fn bot_messages_post(
    State(s): State<Arc<AppState>>,
    Path((rid, key)): Path<(i64, String)>,
    headers: HeaderMap,
    req: Request,
) -> AppResult {
    let u = bot_user(&s, &key)?;
    let (body, attachment) = if headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .starts_with("multipart/form-data")
    {
        let mut multipart = Multipart::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let mut attachment = None;
        let mut body = String::new();
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?
        {
            match field.name() {
                Some("attachment") => {
                    let filename = field.file_name().unwrap_or("attachment").to_string();
                    let content_type = field
                        .content_type()
                        .unwrap_or("application/octet-stream")
                        .to_string();
                    let bytes = field
                        .bytes()
                        .await
                        .map_err(|_| StatusCode::BAD_REQUEST)?
                        .to_vec();
                    if !bytes.is_empty() {
                        attachment = Some(Upload {
                            filename,
                            content_type,
                            bytes,
                        });
                    }
                }
                Some("body") => {
                    body = field.text().await.map_err(|_| StatusCode::BAD_REQUEST)?;
                }
                _ => {}
            }
        }
        (body, attachment)
    } else {
        let bytes = axum::body::to_bytes(req.into_body(), 25 * 1024 * 1024)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        (
            String::from_utf8(bytes.to_vec()).map_err(|_| StatusCode::UNPROCESSABLE_ENTITY)?,
            None,
        )
    };
    let m = insert_message(&s, &u, rid, &body, None, attachment, false, Some(&headers))?;
    let mut r = StatusCode::CREATED.into_response();
    r.headers_mut().insert(
        header::LOCATION,
        public_url(&headers, &format!("/messages/{}", m.id))
            .parse()
            .unwrap(),
    );
    Ok(r)
}
async fn bot_message_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, key, mid)): Path<(i64, String, i64)>,
    body: String,
) -> AppResult {
    let u = bot_user(&s, &key)?;
    room_for(&s, u.id, rid)?;
    let db = pool(&s)?;
    let creator: Option<i64> = db
        .query_row(
            "SELECT creator_id FROM messages WHERE id=?1 AND room_id=?2",
            params![mid, rid],
            |row| row.get(0),
        )
        .optional()
        .map_err(db_err)?;
    let creator = creator.ok_or(StatusCode::NOT_FOUND)?;
    if creator != u.id {
        return Err(StatusCode::FORBIDDEN);
    }
    let updated_at = now();
    let updated_at_ns =
        message_timestamp_ns(&updated_at).ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    let found=db.execute("UPDATE messages SET body=?1,body_html=NULL,body_source=NULL,updated_at=?2,updated_at_ns=?3 WHERE id=?4 AND room_id=?5 AND creator_id=?6",params![body,updated_at,updated_at_ns,mid,rid,u.id]).map_err(db_err)?;
    if found == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    touch_room(&db, rid)?;
    db.execute("DELETE FROM message_mentions WHERE message_id=?1", [mid])
        .map_err(db_err)?;
    drop(db);
    let m = message_by_id(&s, rid, mid)?;
    let presentation_html = message_presentation_html(&s, &m);
    let _ = s.events.send(Event {
        room_id: rid,
        payload: json!({"type":"message_updated","room_id":rid,"id":mid,"client_message_id":m.client_message_id,"body":body,"presentation_html":presentation_html}).to_string(),
    });
    Ok(Json(message_json(&s, &m, Some(&headers))?).into_response())
}
async fn bot_message_delete(
    State(s): State<Arc<AppState>>,
    Path((rid, key, mid)): Path<(i64, String, i64)>,
) -> AppResult {
    let u = bot_user(&s, &key)?;
    room_for(&s, u.id, rid)?;
    let db = pool(&s)?;
    let target: Option<(i64, Option<String>, String)> = db.query_row("SELECT m.creator_id,a.stored_name,m.client_message_id FROM messages m LEFT JOIN attachments a ON a.message_id=m.id WHERE m.id=?1 AND m.room_id=?2", params![mid,rid], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?))).optional().map_err(db_err)?;
    let (creator, attachment, client_message_id) = target.ok_or(StatusCode::NOT_FOUND)?;
    if creator != u.id {
        return Err(StatusCode::FORBIDDEN);
    }
    let found = db
        .execute(
            "DELETE FROM messages WHERE id=?1 AND room_id=?2 AND creator_id=?3",
            params![mid, rid, u.id],
        )
        .map_err(db_err)?;
    if found == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    touch_room(&db, rid)?;
    if let Some(stored) = attachment {
        remove_attachment_files(&stored);
    }
    let _ = s.events.send(Event {
        room_id: rid,
        payload: json!({"type":"message_deleted","room_id":rid,"id":mid,"client_message_id":client_message_id}).to_string(),
    });
    Ok(StatusCode::NO_CONTENT.into_response())
}
fn attachment_record(
    s: &AppState,
    u: &User,
    id: i64,
) -> Result<(String, String, String), StatusCode> {
    let (rid, filename, content_type, stored) = attachment_record_unchecked(s, id)?;
    room_for(&s, u.id, rid)?;
    Ok((filename, content_type, stored))
}
fn attachment_record_unchecked(
    s: &AppState,
    id: i64,
) -> Result<(i64, String, String, String), StatusCode> {
    let db = pool(&s)?;
    let row:Option<(i64,String,String,String)>=db.query_row("SELECT m.room_id,a.filename,a.content_type,a.stored_name FROM attachments a JOIN messages m ON m.id=a.message_id WHERE a.id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(db_err)?;
    row.ok_or(StatusCode::NOT_FOUND)
}
fn byte_range(input: &str, size: u64) -> Result<(u64, u64), StatusCode> {
    let value = input
        .strip_prefix("bytes=")
        .ok_or(StatusCode::RANGE_NOT_SATISFIABLE)?;
    if value.contains(',') {
        return Err(StatusCode::RANGE_NOT_SATISFIABLE);
    }
    let (first, last) = value
        .split_once('-')
        .ok_or(StatusCode::RANGE_NOT_SATISFIABLE)?;
    if size == 0 {
        return Err(StatusCode::RANGE_NOT_SATISFIABLE);
    }
    if first.is_empty() {
        let suffix = last
            .parse::<u64>()
            .map_err(|_| StatusCode::RANGE_NOT_SATISFIABLE)?;
        if suffix == 0 {
            return Err(StatusCode::RANGE_NOT_SATISFIABLE);
        }
        return Ok((size.saturating_sub(suffix), size - 1));
    }
    let start = first
        .parse::<u64>()
        .map_err(|_| StatusCode::RANGE_NOT_SATISFIABLE)?;
    let end = if last.is_empty() {
        size - 1
    } else {
        last.parse::<u64>()
            .map_err(|_| StatusCode::RANGE_NOT_SATISFIABLE)?
            .min(size - 1)
    };
    if start >= size || end < start {
        return Err(StatusCode::RANGE_NOT_SATISFIABLE);
    }
    Ok((start, end))
}
async fn serve_attachment(
    path: &std::path::Path,
    filename: &str,
    content_type: &str,
    headers: &HeaderMap,
    inline: bool,
) -> AppResult {
    let mut file = tokio::fs::File::open(path)
        .await
        .map_err(|_| StatusCode::NOT_FOUND)?;
    let size = file.metadata().await.map_err(db_err)?.len();
    let range = headers
        .get(header::RANGE)
        .and_then(|value| value.to_str().ok());
    let (start, end, status) = if let Some(value) = range {
        match byte_range(value, size) {
            Ok((start, end)) => (start, end, StatusCode::PARTIAL_CONTENT),
            Err(_) => {
                let mut response = StatusCode::RANGE_NOT_SATISFIABLE.into_response();
                response.headers_mut().insert(
                    header::CONTENT_RANGE,
                    format!("bytes */{size}").parse().unwrap(),
                );
                return Ok(response);
            }
        }
    } else {
        (0, size.saturating_sub(1), StatusCode::OK)
    };
    if start > 0 {
        file.seek(SeekFrom::Start(start)).await.map_err(db_err)?;
    }
    let length = if size == 0 { 0 } else { end - start + 1 };
    let stream = ReaderStream::new(file.take(length));
    let mut response = Response::new(Body::from_stream(stream));
    *response.status_mut() = status;
    let safe_name: String = filename
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || "._-".contains(c) {
                c
            } else {
                '_'
            }
        })
        .collect();
    let headers_out = response.headers_mut();
    headers_out.insert(
        header::CONTENT_TYPE,
        content_type
            .parse()
            .unwrap_or_else(|_| "application/octet-stream".parse().unwrap()),
    );
    headers_out.insert(header::CONTENT_LENGTH, length.to_string().parse().unwrap());
    headers_out.insert(header::ACCEPT_RANGES, "bytes".parse().unwrap());
    headers_out.insert("x-content-type-options", "nosniff".parse().unwrap());
    headers_out.insert("content-security-policy", "sandbox".parse().unwrap());
    headers_out.insert(
        header::CONTENT_DISPOSITION,
        format!(
            "{}; filename=\"{safe_name}\"",
            if inline { "inline" } else { "attachment" }
        )
        .parse()
        .unwrap(),
    );
    if status == StatusCode::PARTIAL_CONTENT {
        headers_out.insert(
            header::CONTENT_RANGE,
            format!("bytes {start}-{end}/{size}").parse().unwrap(),
        );
    }
    Ok(response)
}
async fn attachment_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Query(query): Query<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let (filename, content_type, stored) = attachment_record(&s, &u, id)?;
    let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
    serve_attachment(
        &std::path::Path::new(&dir).join(stored),
        &filename,
        &content_type,
        &headers,
        query.get("disposition").map(String::as_str) != Some("attachment")
            && (safe_inline_image(&content_type) || safe_inline_video(&content_type)),
    )
    .await
}
async fn signed_blob_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((token, _filename)): Path<(String, String)>,
    Query(query): Query<HashMap<String, String>>,
) -> AppResult {
    let id = blob_id_from_token(&s.blob_signing_key, &token)
        .or_else(|| {
            s.imported_blob_signing_key
                .as_deref()
                .and_then(|key| blob_id_from_token(key, &token))
        })
        .ok_or(StatusCode::NOT_FOUND)?;
    let (_, filename, content_type, stored) = attachment_record_unchecked(&s, id)?;
    let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
    serve_attachment(
        &std::path::Path::new(&dir).join(stored),
        &filename,
        &content_type,
        &headers,
        query.get("disposition").map(String::as_str) != Some("attachment")
            && (safe_inline_image(&content_type) || safe_inline_video(&content_type)),
    )
    .await
}
fn remove_attachment_files(stored: &str) {
    if Uuid::parse_str(stored).is_err() {
        return;
    }
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    );
    let _ = std::fs::remove_file(dir.join(stored));
    for kind in ["thumb", "poster"] {
        let _ = std::fs::remove_file(dir.join("variants").join(format!("{stored}-{kind}.webp")));
    }
}
async fn attachment_variant(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((id, kind)): Path<(i64, String)>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let (filename, content_type, stored) = attachment_record(&s, &u, id)?;
    if (kind == "thumb" && !safe_inline_image(&content_type))
        || (kind == "poster" && !safe_inline_video(&content_type))
        || !matches!(kind.as_str(), "thumb" | "poster")
    {
        return Err(StatusCode::NOT_FOUND);
    }
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    );
    let input = dir.join(&stored);
    let cache = dir.join("variants");
    tokio::fs::create_dir_all(&cache).await.map_err(db_err)?;
    let output = cache.join(format!("{stored}-{kind}.webp"));
    if tokio::fs::metadata(&output).await.is_err() {
        let _permit = s.variant_slots.acquire().await.map_err(db_err)?;
        if tokio::fs::metadata(&output).await.is_err() {
            let temporary = cache.join(format!("{stored}-{kind}-{}.webp", Uuid::new_v4()));
            let result = if kind == "thumb" {
                tokio::time::timeout(
                    std::time::Duration::from_secs(10),
                    tokio::process::Command::new("vips")
                        .arg("thumbnail")
                        .arg(&input)
                        .arg(&temporary)
                        .args(["1200", "--height", "800", "--size", "down"])
                        .kill_on_drop(true)
                        .output(),
                )
                .await
                .ok()
                .and_then(Result::ok)
            } else {
                let dimensions = tokio::time::timeout(
                    std::time::Duration::from_secs(5),
                    tokio::process::Command::new("ffprobe")
                        .args([
                            "-v",
                            "error",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=width,height",
                            "-of",
                            "csv=p=0",
                        ])
                        .arg(&input)
                        .kill_on_drop(true)
                        .output(),
                )
                .await
                .ok()
                .and_then(Result::ok)
                .and_then(|result| String::from_utf8(result.stdout).ok())
                .and_then(|text| {
                    let (width, height) = text.trim().split_once(',')?;
                    Some((width.parse::<u32>().ok()?, height.parse::<u32>().ok()?))
                });
                let (width, height) = dimensions.unwrap_or((1200, 800));
                let filter = format!(
                    "scale={}:{}:force_original_aspect_ratio=decrease",
                    width.min(1200).max(1),
                    height.min(800).max(1)
                );
                tokio::time::timeout(
                    std::time::Duration::from_secs(10),
                    tokio::process::Command::new("ffmpeg")
                        .args(["-v", "error", "-ss", "0.1", "-i"])
                        .arg(&input)
                        .args(["-frames:v", "1", "-vf", &filter, "-y"])
                        .arg(&temporary)
                        .kill_on_drop(true)
                        .output(),
                )
                .await
                .ok()
                .and_then(Result::ok)
            };
            if result.is_some_and(|process| process.status.success())
                && tokio::fs::metadata(&temporary).await.is_ok()
            {
                let _ = tokio::fs::rename(&temporary, &output).await;
            } else {
                let _ = tokio::fs::remove_file(&temporary).await;
            }
        }
    }
    if tokio::fs::metadata(&output).await.is_ok() {
        serve_attachment(
            &output,
            &format!("{filename}.webp"),
            "image/webp",
            &headers,
            true,
        )
        .await
    } else if kind == "thumb" {
        serve_attachment(&input, &filename, &content_type, &headers, true).await
    } else {
        Err(StatusCode::NOT_FOUND)
    }
}
async fn boost_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(mid): Path<i64>,
    Form(f): Form<std::collections::HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let target: Option<(i64, String)> = db
        .query_row(
            "SELECT room_id,client_message_id FROM messages WHERE id=?1",
            [mid],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (rid, client_message_id) = target.ok_or(StatusCode::NOT_FOUND)?;
    room_for(&s, u.id, rid)?;
    let content = f
        .get("boost[content]")
        .or_else(|| f.get("content"))
        .map(|s| s.as_str())
        .unwrap_or("👍");
    if content.trim().is_empty() || content.chars().count() > 16 {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    db.execute(
        "INSERT INTO boosts(message_id,booster_id,content,created_at) VALUES(?1,?2,?3,?4)",
        params![mid, u.id, content, now()],
    )
    .map_err(db_err)?;
    let bid = db.last_insert_rowid();
    touch_message(&db, mid, rid)?;
    drop(db);
    let boost_html = boost_html(&s, bid, mid, u.id, &u.name, &u.updated_at, content);
    let _ = s.events.send(Event {
        room_id: rid,
        payload:
            json!({"type":"boost","room_id":rid,"message_id":mid,"client_message_id":client_message_id,"id":bid,"content":content,"user_id":u.id,"boost_html":boost_html})
                .to_string(),
    });
    Ok(Redirect::to(&format!("/messages/{mid}/boosts")).into_response())
}
async fn boosts_index(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(mid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let rid: Option<i64> = db
        .query_row("SELECT room_id FROM messages WHERE id=?1", [mid], |r| {
            r.get(0)
        })
        .optional()
        .map_err(db_err)?;
    let rid = rid.ok_or(StatusCode::NOT_FOUND)?;
    room_for(&s, u.id, rid)?;
    let mut query=db.prepare("SELECT b.id,b.content,b.booster_id,u.name FROM boosts b JOIN users u ON u.id=b.booster_id WHERE b.message_id=?1 ORDER BY b.id").map_err(db_err)?;
    let boosts = query
        .query_map([mid], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)?,
                r.get::<_, String>(3)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let mut list = String::new();
    for (id, content, booster, name) in boosts {
        let remove = if booster == u.id {
            format!(
                "<form method='post' action='/messages/{mid}/boosts/{id}/delete'><button type='submit' class='danger'>Remove</button></form>"
            )
        } else {
            String::new()
        };
        list.push_str(&format!(
            "<li id='boost-{id}'>{} <span>{}</span>{remove}</li>",
            esc(&content),
            esc(&name)
        ));
    }
    Ok(render(
        "Boosts",
        &format!(
            "<section class='form-card'><h1>Boosts</h1><ul>{list}</ul><a class='button' href='/messages/{mid}/boosts/new'>Add a boost</a> <a href='/rooms/{rid}/@{mid}'>Back to message</a></section>"
        ),
        Some(&u),
    ))
}
async fn boost_new(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(mid): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let target: Option<(i64, String)> = db
        .query_row(
            "SELECT room_id,client_message_id FROM messages WHERE id=?1",
            [mid],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (rid, client_id) = target.ok_or(StatusCode::NOT_FOUND)?;
    room_for(&s, u.id, rid)?;
    let frame_id = format!("new_boost_message_{client_id}");
    if headers
        .get("Turbo-Frame")
        .and_then(|value| value.to_str().ok())
        == Some(frame_id.as_str())
    {
        let avatar_key = s
            .imported_avatar_signing_key
            .as_deref()
            .unwrap_or(&s.avatar_signing_key);
        let avatar = avatar_path(avatar_key, u.id, &u.updated_at)
            .unwrap_or_else(|_| format!("/users/{}/avatar", u.id));
        let frame_id_html = esc(&frame_id);
        let client_id_html = esc(&client_id);
        return Ok(Html(format!(
            "<turbo-frame id='{frame_id_html}'><div class='boost flex-inline position-relative max-width fill-white'><form class='custom-boost-form boost__form flex align-center gap expanded' method='post' action='/messages/{mid}/boosts' data-turbo-frame='boosting_message_{client_id_html}'><label class='boost__form-label flex gap' role='button' tabindex='0' aria-label='Add a boost'><figure class='avatar boost__avatar flex-item-no-shrink'><a title='{user_name}' class='btn avatar' href='/users/{user_id}'><img aria-hidden='true' src='{avatar}' width='22' height='22'></a><span class='for-screen-reader'>{user_name}</span></figure><input name='boost[content]' maxlength='16' required pattern='.*\\S.*' autocomplete='off' autocorrect='off' aria-label='Boost text' autofocus></label><button class='btn btn--reversed' type='submit' aria-label='Submit boost'>✓</button><a class='btn btn--negative' href='/messages/{mid}/boosts' data-cancel-custom-boost aria-label='Cancel boost'>−</a></form></div></turbo-frame>",
            user_name = esc(&u.name),
            user_id = u.id,
            avatar = esc(&avatar),
        ))
        .into_response());
    }
    Ok(render(
        "Add a boost",
        &format!(
            "<section class='form-card'><h1>Add a boost</h1><form method='post' action='/messages/{mid}/boosts'><label>Boost<input name='boost[content]' maxlength='16' required autofocus></label><button class='button'>Save</button></form><a href='/messages/{mid}/boosts'>Cancel</a></section>"
        ),
        Some(&u),
    ))
}
async fn boost_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((mid, bid)): Path<(i64, i64)>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let rid: Option<i64> = db
        .query_row("SELECT room_id FROM messages WHERE id=?1", [mid], |r| {
            r.get(0)
        })
        .optional()
        .map_err(db_err)?;
    let rid = rid.ok_or(StatusCode::NOT_FOUND)?;
    room_for(&s, u.id, rid)?;
    let content: Option<String> = db
        .query_row(
            "SELECT content FROM boosts WHERE id=?1 AND message_id=?2 AND booster_id=?3",
            params![bid, mid, u.id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    let content = content.ok_or(StatusCode::NOT_FOUND)?;
    db.execute("DELETE FROM boosts WHERE id=?1", [bid])
        .map_err(db_err)?;
    touch_message(&db, mid, rid)?;
    s.events.send(Event{room_id:rid,payload:json!({"type":"boost_deleted","room_id":rid,"message_id":mid,"id":bid,"content":content}).to_string()});
    if headers
        .get(header::ACCEPT)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .contains("turbo-stream")
    {
        Ok(Html(format!(
            "<turbo-stream action='remove' target='boost_{bid}'></turbo-stream>"
        ))
        .into_response())
    } else {
        Ok(Redirect::to(&format!("/messages/{mid}/boosts")).into_response())
    }
}
async fn bot_boost_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, key, mid)): Path<(i64, String, i64)>,
    body: Bytes,
) -> AppResult {
    let bot = bot_user(&s, &key)?;
    room_for(&s, bot.id, rid)?;
    let content = std::str::from_utf8(&body)
        .map_err(|_| StatusCode::UNPROCESSABLE_ENTITY)?
        .trim();
    if content.is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let db = pool(&s)?;
    let client_message_id: Option<String> = db
        .query_row(
            "SELECT client_message_id FROM messages WHERE id=?1 AND room_id=?2",
            params![mid, rid],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    let client_message_id = client_message_id.ok_or(StatusCode::NOT_FOUND)?;
    let created = now();
    db.execute(
        "INSERT INTO boosts(message_id,booster_id,content,created_at) VALUES(?1,?2,?3,?4)",
        params![mid, bot.id, content, created],
    )
    .map_err(db_err)?;
    let id = db.last_insert_rowid();
    touch_message(&db, mid, rid)?;
    drop(db);
    let boost_html = boost_html(&s, id, mid, bot.id, &bot.name, &bot.updated_at, content);
    s.events.send(Event {
        room_id: rid,
        payload: json!({"type":"boost","room_id":rid,"message_id":mid,"client_message_id":client_message_id,"id":id,"content":content,"user_id":bot.id,"boost_html":boost_html})
            .to_string(),
    });
    let created_at = chrono::DateTime::parse_from_rfc3339(&created)
        .map(|time| {
            time.with_timezone(&Utc)
                .to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
        })
        .unwrap_or(created);
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let avatar_url = public_url(&headers, &avatar_path(avatar_key, bot.id, &bot.updated_at)?);
    let message_url = public_url(&headers, &format!("/rooms/{rid}/messages/{mid}"));
    Ok((StatusCode::CREATED, Json(json!({"id":id,"content":content,"created_at":created_at,"booster":{"id":bot.id,"name":bot.name,"role":"bot","avatar_url":avatar_url},"message":{"id":mid,"url":message_url}}))).into_response())
}
async fn bot_boost_delete(
    State(s): State<Arc<AppState>>,
    Path((rid, key, mid, bid)): Path<(i64, String, i64, i64)>,
) -> AppResult {
    let bot = bot_user(&s, &key)?;
    room_for(&s, bot.id, rid)?;
    let content: Option<String> = pool(&s)?
        .query_row(
            "SELECT content FROM boosts WHERE id=?1 AND message_id=?2 AND booster_id=?3",
            params![bid, mid, bot.id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    let db = pool(&s)?;
    let changed = db.execute("DELETE FROM boosts WHERE id=?1 AND message_id=?2 AND booster_id=?3 AND EXISTS(SELECT 1 FROM messages WHERE id=?2 AND room_id=?4)",params![bid,mid,bot.id,rid]).map_err(db_err)?;
    if changed == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    touch_message(&db, mid, rid)?;
    s.events.send(Event {
        room_id: rid,
        payload: json!({"type":"boost_deleted","room_id":rid,"message_id":mid,"id":bid,"content":content}).to_string(),
    });
    Ok(StatusCode::NO_CONTENT.into_response())
}
fn turbo_room_event(payload: &Value) -> Option<String> {
    match payload.get("type")?.as_str()? {
        "message" => Some(format!(
            "<turbo-stream action=\"append\" target=\"{}\"><template>{}</template></turbo-stream>",
            room_messages_target(
                payload.get("room_kind")?.as_str()?,
                payload.get("room_id")?.as_i64()?
            )?,
            payload.get("html")?.as_str()?
        )),
        "message_updated" => {
            let client_message_id = esc(payload.get("client_message_id")?.as_str()?);
            Some(format!(
                "<turbo-stream action=\"replace\" target=\"presentation_message_{client_message_id}\"><template>{}</template></turbo-stream>",
                payload.get("presentation_html")?.as_str()?
            ))
        }
        "boost" => Some(format!(
            "<turbo-stream maintain_scroll=\"true\" action=\"append\" target=\"boosts_message_{}\"><template>{}</template></turbo-stream>",
            esc(payload.get("client_message_id")?.as_str()?),
            payload.get("boost_html")?.as_str()?
        )),
        "boost_deleted" => Some(format!(
            "<turbo-stream action=\"remove\" target=\"boost_{}\"></turbo-stream>",
            payload.get("id")?.as_i64()?
        )),
        "message_deleted" => Some(format!(
            "<turbo-stream action=\"remove\" target=\"message_{}\"></turbo-stream>",
            esc(payload.get("client_message_id")?.as_str()?)
        )),
        _ => None,
    }
}
async fn ws_upgrade(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    ws: WebSocketUpgrade,
) -> AppResult {
    if let Some(origin) = headers
        .get(header::ORIGIN)
        .and_then(|value| value.to_str().ok())
    {
        let host = headers
            .get(header::HOST)
            .and_then(|value| value.to_str().ok())
            .ok_or(StatusCode::FORBIDDEN)?;
        if origin != format!("http://{host}") && origin != format!("https://{host}") {
            return Err(StatusCode::FORBIDDEN);
        }
    }
    let u = user(&s, &headers)?;
    Ok(ws
        .protocols(["actioncable-v1-json"])
        .on_upgrade(move |socket| ws_loop(s, u, socket))
        .into_response())
}
async fn ws_loop(s: Arc<AppState>, u: User, socket: WebSocket) {
    let (mut sender, mut receiver) = socket.split();
    let (event_tx, mut event_rx) = mpsc::channel::<(String, i64, String, bool)>(256);
    let mut revoked_rx = s.revoked_users.subscribe();
    if sender
        .send(WsMessage::Text(
            json!({"type":"welcome"}).to_string().into(),
        ))
        .await
        .is_err()
    {
        return;
    }
    let mut subscriptions: HashMap<String, tokio::task::JoinHandle<()>> = HashMap::new();
    let mut presence_rooms: HashSet<i64> = HashSet::new();
    let mut heartbeat = tokio::time::interval_at(
        tokio::time::Instant::now() + std::time::Duration::from_secs(3),
        std::time::Duration::from_secs(3),
    );
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _=heartbeat.tick()=>{if sender.send(WsMessage::Text(json!({"type":"ping","message":Utc::now().timestamp()}).to_string().into())).await.is_err(){break}},
            revoked=revoked_rx.recv()=>{if revoked==Ok(u.id){break}},
            incoming=receiver.next()=>{
                let text=match incoming {Some(Ok(WsMessage::Text(text)))=>text,Some(Ok(WsMessage::Close(_)))|None|Some(Err(_))=>break,_=>continue};
                let Ok(cmd)=serde_json::from_str::<Value>(&text) else {continue};
                let action=cmd.get("command").and_then(Value::as_str).unwrap_or("");
                let ident=cmd.get("identifier").and_then(Value::as_str).unwrap_or("");
                let details:Value=serde_json::from_str(ident).unwrap_or(Value::Null);
                let channel=details.get("channel").and_then(Value::as_str).unwrap_or("");
                let signed_name=if channel=="RoomMessagesChannel" {details.get("signed_stream_name").and_then(Value::as_str)} else {None};
                let signed_room=signed_name.and_then(|token|room_from_stream_token(&s.turbo_stream_signing_key,token).or_else(||s.imported_turbo_stream_signing_key.as_deref().and_then(|key|room_from_stream_token(key,token))));
                let rid=if signed_name.is_some() {signed_room.as_ref().map(|(id,_)|*id).unwrap_or(0)} else {details.get("room_id").and_then(Value::as_i64).unwrap_or(0)};
                let signed_valid=signed_name.is_none() || signed_room.as_ref().is_some_and(|(_,kind)|room_for(&s,u.id,rid).is_ok_and(|room|room.kind==*kind));
                if action=="subscribe" {
                    let user_channel=channel=="UnreadRoomsChannel" || channel=="ReadRoomsChannel" || channel=="RoomListChannel" || channel=="HeartbeatChannel";
                    let hub=match channel {"RoomMessagesChannel"=>Some(&s.events),"TypingNotificationsChannel"=>Some(&s.typing_events),"UnreadRoomsChannel"=>Some(&s.unread_events),"ReadRoomsChannel"=>Some(&s.read_events),"RoomListChannel"=>Some(&s.room_list_events),_=>None};
                    let accepted=signed_valid && (hub.is_some() || channel=="PresenceChannel" || channel=="HeartbeatChannel") && (user_channel || (rid>0 && room_for(&s,u.id,rid).is_ok()));
                    if accepted {
                        if channel=="PresenceChannel" {
                            if presence_rooms.insert(rid) { let _=presence_update(&s,u.id,rid,"present"); }
                        } else if let Some(hub)=hub { if let std::collections::hash_map::Entry::Vacant(entry)=subscriptions.entry(ident.to_string()) {
                            let mut room_events=hub.channel(if user_channel {u.id} else {rid}).subscribe();
                            let tx=event_tx.clone();
                            let identifier=ident.to_string();
                            let event_rid=if user_channel {0} else {rid};
                            let turbo=signed_name.is_some();
                            entry.insert(tokio::spawn(async move {loop {match room_events.recv().await {Ok(payload)=>{if tx.send((identifier.clone(),event_rid,payload,turbo)).await.is_err(){break}},Err(broadcast::error::RecvError::Lagged(_))=>continue,Err(_)=>break}}}));
                        }}
                    }
                    let response=if accepted {"confirm_subscription"} else {"reject_subscription"};
                    if sender.send(WsMessage::Text(json!({"identifier":ident,"type":response}).to_string().into())).await.is_err(){break}
                } else if action=="unsubscribe" {
                    if let Some(task)=subscriptions.remove(ident){task.abort();}
                    if channel=="PresenceChannel" && presence_rooms.remove(&rid) { let _=presence_update(&s,u.id,rid,"absent"); }
                } else if action=="message" && channel=="TypingNotificationsChannel" && subscriptions.contains_key(ident) && room_for(&s,u.id,rid).is_ok() {
                    let data=cmd.get("data").and_then(Value::as_str).and_then(|raw|serde_json::from_str::<Value>(raw).ok()).unwrap_or(Value::Null);
                    if let Some(action)=data.get("action").and_then(Value::as_str).filter(|action| *action=="start" || *action=="stop") {
                        s.typing_events.send(Event {room_id:rid,payload:json!({"action":action,"user":{"id":u.id,"name":u.name}}).to_string()});
                    }
                } else if action=="message" && channel=="PresenceChannel" && room_for(&s,u.id,rid).is_ok() {
                    let data=cmd.get("data").and_then(Value::as_str).and_then(|raw|serde_json::from_str::<Value>(raw).ok()).unwrap_or(Value::Null);
                    if let Some(action)=data.get("action").and_then(Value::as_str) {
                        if action=="absent" && presence_rooms.remove(&rid) {let _=presence_update(&s,u.id,rid,"absent");}
                        else if action=="present" && presence_rooms.insert(rid) {let _=presence_update(&s,u.id,rid,"present");}
                        else if action=="refresh" && presence_rooms.contains(&rid) {let _=presence_update(&s,u.id,rid,"refresh");}
                    }
                }
            },
            event=event_rx.recv()=>{if let Some((identifier,rid,payload,turbo))=event{if rid==0 || room_for(&s,u.id,rid).is_ok(){let message=if turbo {serde_json::from_str::<Value>(&payload).ok().and_then(|value|turbo_room_event(&value)).map(Value::String)} else {serde_json::from_str::<Value>(&payload).ok()};if let Some(message)=message {if sender.send(WsMessage::Text(json!({"identifier":identifier,"message":message}).to_string().into())).await.is_err(){break}}}}}
        }
    }
    for (_, task) in subscriptions {
        task.abort();
    }
    for rid in presence_rooms {
        let _ = presence_update(&s, u.id, rid, "absent");
    }
}
async fn health() -> impl IntoResponse {
    "ok"
}
async fn webmanifest(State(s): State<Arc<AppState>>) -> AppResult {
    let db = pool(&s)?;
    let name: Option<String> = db
        .query_row("SELECT name FROM accounts LIMIT 1", [], |r| r.get(0))
        .optional()
        .map_err(db_err)?;
    let name = name.unwrap_or_else(|| "Rustfire".into());
    let mut r=Json(json!({"name":name,"short_name":name,"icons":[{"src":"/account/logo","sizes":"any","type":"image/png","purpose":"any maskable"}],"start_url":"/","scope":"/","display":"standalone","theme_color":"#ffffff","background_color":"#ffffff","categories":["social","business","productivity"],"shortcuts":[{"name":"New chat room","url":"/rooms/opens/new"},{"name":"My profile","url":"/users/me/profile"}]})).into_response();
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        "application/manifest+json".parse().unwrap(),
    );
    Ok(r)
}
async fn service_worker() -> Response {
    let mut r = include_str!("../static/service-worker.js").into_response();
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        "application/javascript".parse().unwrap(),
    );
    r.headers_mut()
        .insert("service-worker-allowed", "/".parse().unwrap());
    r
}
fn init_db(db: &Db) -> Result<(), Box<dyn std::error::Error>> {
    let mut conn = db.get()?;
    let fts_exists:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_search_index')",[],|r|r.get(0))?;
    conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY,name TEXT NOT NULL,join_code TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_settings(id INTEGER PRIMARY KEY CHECK(id=1),restrict_room_creation INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO account_settings(id,restrict_room_creation) VALUES(1,0);
        CREATE TABLE IF NOT EXISTS app_secrets(name TEXT PRIMARY KEY,value BLOB NOT NULL);
        INSERT OR IGNORE INTO app_secrets(name,value) VALUES('mention_sgid',randomblob(32));
        INSERT OR IGNORE INTO app_secrets(name,value) VALUES('avatar_signed_id',randomblob(32));
        INSERT OR IGNORE INTO app_secrets(name,value) VALUES('blob_signed_id',randomblob(32));
        INSERT OR IGNORE INTO app_secrets(name,value) VALUES('turbo_stream',randomblob(32));
        CREATE TABLE IF NOT EXISTS account_custom_styles(id INTEGER PRIMARY KEY CHECK(id=1),css TEXT NOT NULL DEFAULT '');
        INSERT OR IGNORE INTO account_custom_styles(id,css) VALUES(1,'');
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email_address TEXT UNIQUE,password_digest TEXT,role INTEGER NOT NULL DEFAULT 0,status INTEGER NOT NULL DEFAULT 0,bot_token TEXT UNIQUE,bio TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS webhooks(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,url TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS push_subscriptions(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,endpoint TEXT NOT NULL,p256dh_key TEXT NOT NULL,auth_key TEXT NOT NULL,user_agent TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,endpoint));
        CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,token TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,last_active_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bans(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,ip_address TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_bans_ip ON bans(ip_address);
        CREATE TABLE IF NOT EXISTS session_transfers(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY,name TEXT,type TEXT NOT NULL,creator_id INTEGER NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memberships(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,involvement TEXT NOT NULL DEFAULT 'mentions',unread_at TEXT,created_at TEXT NOT NULL,UNIQUE(room_id,user_id));
        CREATE TABLE IF NOT EXISTS direct_room_sets(room_id INTEGER PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,member_ids TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_direct_room_sets_members ON direct_room_sets(member_ids);
        CREATE INDEX IF NOT EXISTS idx_rooms_type ON rooms(type);
        CREATE TRIGGER IF NOT EXISTS direct_room_sets_membership_delete AFTER DELETE ON memberships BEGIN
            UPDATE direct_room_sets SET member_ids=COALESCE((SELECT group_concat(user_id,',') FROM (SELECT user_id FROM memberships WHERE room_id=old.room_id ORDER BY user_id)),'') WHERE room_id=old.room_id;
        END;
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,creator_id INTEGER NOT NULL REFERENCES users(id),body TEXT NOT NULL,client_message_id TEXT NOT NULL,created_at TEXT NOT NULL,created_at_ns INTEGER,updated_at TEXT NOT NULL,updated_at_ns INTEGER);
        CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,PRIMARY KEY(message_id,user_id));
        CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,filename TEXT NOT NULL,content_type TEXT NOT NULL,stored_name TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS avatars(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,stored_name TEXT NOT NULL,content_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_logos(id INTEGER PRIMARY KEY CHECK(id=1),stored_name TEXT NOT NULL,content_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS boosts(id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,booster_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,content TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS searches(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,query TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(user_id,query));
        CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room_id,id);CREATE INDEX IF NOT EXISTS idx_messages_room_created ON messages(room_id,created_at,id);CREATE INDEX IF NOT EXISTS idx_messages_room_updated ON messages(room_id,updated_at,id);CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);CREATE INDEX IF NOT EXISTS idx_boosts_message ON boosts(message_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS message_search_index USING fts5(body);
        CREATE TRIGGER IF NOT EXISTS message_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO message_search_index(rowid,body) VALUES(new.id,new.body); END;
        CREATE TRIGGER IF NOT EXISTS message_fts_update AFTER UPDATE OF body ON messages BEGIN UPDATE message_search_index SET body=new.body WHERE rowid=new.id; END;
        CREATE TRIGGER IF NOT EXISTS message_fts_delete AFTER DELETE ON messages BEGIN DELETE FROM message_search_index WHERE rowid=old.id; END;")?;
    let has_session_ip: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('sessions') WHERE name='ip_address')",
        [],
        |r| r.get(0),
    )?;
    if !has_session_ip {
        conn.execute("ALTER TABLE sessions ADD COLUMN ip_address TEXT", [])?;
    }
    let has_csrf: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('sessions') WHERE name='csrf_token')",
        [],
        |r| r.get(0),
    )?;
    if !has_csrf {
        conn.execute("ALTER TABLE sessions ADD COLUMN csrf_token TEXT", [])?;
    }
    conn.execute("UPDATE sessions SET csrf_token=lower(hex(randomblob(16))) WHERE csrf_token IS NULL OR csrf_token=''", [])?;
    let has_connections: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('memberships') WHERE name='connections')",
        [],
        |r| r.get(0),
    )?;
    if !has_connections {
        conn.execute(
            "ALTER TABLE memberships ADD COLUMN connections INTEGER NOT NULL DEFAULT 0",
            [],
        )?;
    }
    let has_connected_at: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('memberships') WHERE name='connected_at')",
        [],
        |r| r.get(0),
    )?;
    if !has_connected_at {
        conn.execute("ALTER TABLE memberships ADD COLUMN connected_at TEXT", [])?;
    }
    let has_body_html: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('messages') WHERE name='body_html')",
        [],
        |r| r.get(0),
    )?;
    if !has_body_html {
        conn.execute("ALTER TABLE messages ADD COLUMN body_html TEXT", [])?;
    }
    let has_body_source: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('messages') WHERE name='body_source')",
        [],
        |r| r.get(0),
    )?;
    if !has_body_source {
        conn.execute("ALTER TABLE messages ADD COLUMN body_source TEXT", [])?;
    }
    let has_created_at_ns: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('messages') WHERE name='created_at_ns')",
        [],
        |r| r.get(0),
    )?;
    if !has_created_at_ns {
        conn.execute("ALTER TABLE messages ADD COLUMN created_at_ns INTEGER", [])?;
    }
    let has_updated_at_ns: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('messages') WHERE name='updated_at_ns')",
        [],
        |r| r.get(0),
    )?;
    if !has_updated_at_ns {
        conn.execute("ALTER TABLE messages ADD COLUMN updated_at_ns INTEGER", [])?;
    }
    let missing_timestamps = {
        let mut query =
            conn.prepare("SELECT id,created_at FROM messages WHERE created_at_ns IS NULL")?;
        query
            .query_map([], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
            })?
            .collect::<Result<Vec<_>, _>>()?
    };
    if !missing_timestamps.is_empty() {
        let transaction = conn.transaction()?;
        {
            let mut update =
                transaction.prepare("UPDATE messages SET created_at_ns=?1 WHERE id=?2")?;
            for (id, created_at) in missing_timestamps {
                let nanos = message_timestamp_ns(&created_at)
                    .ok_or("message timestamp outside supported range")?;
                update.execute(params![nanos, id])?;
            }
        }
        transaction.commit()?;
    }
    let missing_updated_timestamps = {
        let mut query =
            conn.prepare("SELECT id,updated_at FROM messages WHERE updated_at_ns IS NULL")?;
        query
            .query_map([], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
            })?
            .collect::<Result<Vec<_>, _>>()?
    };
    if !missing_updated_timestamps.is_empty() {
        let transaction = conn.transaction()?;
        {
            let mut update =
                transaction.prepare("UPDATE messages SET updated_at_ns=?1 WHERE id=?2")?;
            for (id, updated_at) in missing_updated_timestamps {
                let nanos = message_timestamp_ns(&updated_at)
                    .ok_or("message timestamp outside supported range")?;
                update.execute(params![nanos, id])?;
            }
        }
        transaction.commit()?;
    }
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room_created_ns ON messages(room_id,created_at_ns,id)", [])?;
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room_updated_ns ON messages(room_id,updated_at_ns,id)", [])?;
    if !fts_exists {
        conn.execute(
            "INSERT INTO message_search_index(rowid,body) SELECT id,body FROM messages",
            [],
        )?;
    }
    conn.execute(
        "INSERT OR IGNORE INTO direct_room_sets(room_id,member_ids) SELECT r.id,COALESCE((SELECT group_concat(user_id,',') FROM (SELECT user_id FROM memberships WHERE room_id=r.id ORDER BY user_id)),'') FROM rooms r WHERE r.type='Rooms::Direct' AND NOT EXISTS(SELECT 1 FROM direct_room_sets d WHERE d.room_id=r.id)",
        [],
    )?;
    Ok(())
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db_path = env::var("RUSTFIRE_DB").unwrap_or_else(|_| "data/rustfire.db".into());
    if let Some(parent) = std::path::Path::new(&db_path).parent() {
        std::fs::create_dir_all(parent)?;
    }
    let manager = SqliteConnectionManager::file(&db_path).with_init(|c| {
        c.execute_batch(
            "PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        )
    });
    let db = Pool::builder().max_size(32).build(manager)?;
    init_db(&db)?;
    let mention_signing_key: Vec<u8> = db.get()?.query_row(
        "SELECT value FROM app_secrets WHERE name='mention_sgid'",
        [],
        |row| row.get(0),
    )?;
    let avatar_signing_key: Vec<u8> = db.get()?.query_row(
        "SELECT value FROM app_secrets WHERE name='avatar_signed_id'",
        [],
        |row| row.get(0),
    )?;
    let blob_signing_key: Vec<u8> = db.get()?.query_row(
        "SELECT value FROM app_secrets WHERE name='blob_signed_id'",
        [],
        |row| row.get(0),
    )?;
    let turbo_stream_signing_key: Vec<u8> = db.get()?.query_row(
        "SELECT value FROM app_secrets WHERE name='turbo_stream'",
        [],
        |row| row.get(0),
    )?;
    let campfire_secret = env::var("RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE")
        .ok()
        .filter(|base| !base.is_empty());
    let imported_mention_signing_key =
        campfire_secret.as_deref().map(rails_sgid_key).transpose()?;
    let imported_avatar_signing_key = campfire_secret
        .as_deref()
        .map(rails_avatar_key)
        .transpose()?;
    let imported_blob_signing_key = campfire_secret.as_deref().map(rails_blob_key).transpose()?;
    let imported_turbo_stream_signing_key = campfire_secret
        .as_deref()
        .map(rails_turbo_stream_key)
        .transpose()?;
    let (vapid_private, vapid_public) = load_vapid_key(std::path::Path::new(&db_path))?;
    let _ = VAPID_PUBLIC.set(vapid_public.clone());
    let has_push_subscriptions: bool =
        db.get()?
            .query_row("SELECT EXISTS(SELECT 1 FROM push_subscriptions)", [], |r| {
                r.get(0)
            })?;
    let trusted_proxies = env::var("RUSTFIRE_TRUSTED_PROXY_IPS")
        .unwrap_or_default()
        .split(',')
        .filter_map(|ip| ip.trim().parse::<IpAddr>().ok())
        .collect();
    let state = Arc::new(AppState {
        db,
        events: RoomHub::default(),
        typing_events: RoomHub::default(),
        unread_events: RoomHub::default(),
        read_events: RoomHub::default(),
        room_list_events: RoomHub::default(),
        revoked_users: broadcast::channel(1024).0,
        trusted_proxies,
        webhook_client: reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(7))
            .redirect(reqwest::redirect::Policy::none())
            .build()?,
        webhook_slots: Arc::new(Semaphore::new(64)),
        webhooks_enabled: !env::var("RUSTFIRE_DISABLE_WEBHOOKS")
            .is_ok_and(|value| value == "1" || value.eq_ignore_ascii_case("true")),
        vapid_private,
        mention_signing_key,
        imported_mention_signing_key,
        avatar_signing_key,
        imported_avatar_signing_key,
        blob_signing_key,
        imported_blob_signing_key,
        turbo_stream_signing_key,
        imported_turbo_stream_signing_key,
        push_slots: Arc::new(Semaphore::new(50)),
        has_push_subscriptions: AtomicBool::new(has_push_subscriptions),
        push_delivery_enabled: !env::var("RUSTFIRE_DISABLE_PUSH")
            .is_ok_and(|value| value == "1" || value.eq_ignore_ascii_case("true")),
        login_attempts: Mutex::new(HashMap::new()),
        variant_slots: Arc::new(Semaphore::new(4)),
    });
    let app = Router::new()
        .route("/", get(root))
        .route("/up", get(health))
        .route("/webmanifest", get(webmanifest))
        .route("/service-worker", get(service_worker))
        .route("/qr_code/{id}", get(qr_code_show))
        .route("/first_run", get(first_run_get).post(first_run_post))
        .route("/session/new", get(login_get))
        .route("/session", post(login_post).delete(logout))
        .route("/session/logout", post(logout))
        .route(
            "/session/transfers/{token}",
            get(transfer_show)
                .post(transfer_update)
                .put(transfer_update),
        )
        .route("/cable", get(ws_upgrade))
        .route("/rooms", get(rooms_index))
        .route(
            "/rooms/{id}",
            get(room_show).post(create_room).delete(room_delete),
        )
        .route("/rooms/{id}/refresh", get(room_refresh))
        .route("/rooms/{id}/settings", get(room_edit))
        .route(
            "/rooms/{id}/messages",
            get(messages_index).post(message_create),
        )
        .route(
            "/rooms/{id}/messages/{mid}",
            get(message_show)
                .patch(message_update)
                .put(message_update)
                .delete(message_delete),
        )
        .route("/rooms/{id}/messages/{mid}/edit", get(message_edit))
        .route("/rooms/{id}/messages/{mid}/update", post(message_update))
        .route("/rooms/{id}/messages/{mid}/delete", post(message_delete))
        .route("/rooms/{id}/@{mid}", get(room_show_at))
        .route("/rooms/{id}/edit", get(room_edit))
        .route(
            "/rooms/{id}/involvement",
            get(involvement_get)
                .post(involvement_post)
                .patch(involvement_post)
                .put(involvement_post),
        )
        .route("/rooms/{id}/update", post(room_update))
        .route("/rooms/{id}/delete", post(room_delete))
        .route("/rooms/{kind}/new", get(new_room))
        .route(
            "/rooms/opens/{id}",
            get(room_kind_show)
                .patch(room_kind_update)
                .put(room_kind_update)
                .delete(room_kind_delete),
        )
        .route(
            "/rooms/closeds/{id}",
            get(room_kind_show)
                .patch(room_kind_update)
                .put(room_kind_update)
                .delete(room_kind_delete),
        )
        .route("/rooms/opens/{id}/edit", get(room_kind_edit))
        .route("/rooms/closeds/{id}/edit", get(room_kind_edit))
        .route("/rooms/directs/new", get(direct_new))
        .route("/rooms/directs", get(root).post(direct_create))
        .route("/rooms/opens", get(root).post(create_open_room))
        .route("/rooms/closeds", get(root).post(create_closed_room))
        .route(
            "/rooms/directs/{id}",
            get(direct_show).delete(direct_delete),
        )
        .route("/rooms/directs/{id}/edit", get(direct_edit))
        .route("/rooms/directs/{id}/delete", post(direct_delete))
        .route("/searches", get(search_get).post(search_post))
        .route("/unfurl_link", post(unfurl_link))
        .route("/searches/clear", post(search_clear).delete(search_clear))
        .route(
            "/account",
            get(account_get).patch(account_update).put(account_update),
        )
        .route("/account/edit", get(account_get))
        .route("/account/update", post(account_update))
        .route("/account/custom_styles/edit", get(custom_styles_get))
        .route(
            "/account/custom_styles",
            post(custom_styles_update).put(custom_styles_update),
        )
        .route("/account/custom_styles.css", get(custom_styles_css))
        .route(
            "/account/logo",
            get(logo_get).post(logo_post).delete(logo_delete),
        )
        .route("/account/logo/delete", post(logo_delete))
        .route("/account/join_code", post(join_code_create))
        .route("/account/users", get(account_get))
        .route(
            "/account/users/{id}",
            patch(user_role_update)
                .put(user_role_update)
                .delete(user_deactivate),
        )
        .route("/account/users/{id}/role", post(user_role_update))
        .route("/account/users/{id}/deactivate", post(user_deactivate))
        .route("/join/{code}", get(join_get).post(join_post))
        .route(
            "/users/me/profile",
            get(profile)
                .post(profile_post)
                .patch(profile_post)
                .put(profile_post),
        )
        .route("/users/me/avatar", post(avatar_post).delete(avatar_delete))
        .route(
            "/users/me/push_subscriptions",
            get(push_subscriptions_get).post(push_subscriptions_post),
        )
        .route(
            "/users/me/push_subscriptions/{id}",
            delete(push_subscriptions_delete),
        )
        .route(
            "/users/me/push_subscriptions/{id}/delete",
            post(push_subscriptions_delete),
        )
        .route(
            "/users/me/push_subscriptions/{id}/test_notifications",
            post(push_test_notification),
        )
        .route("/users/me/avatar/delete", post(avatar_delete))
        .route("/users/{id}/avatar", get(avatar_get))
        .route("/users/me/sidebar", get(sidebar_get))
        .route("/users/{id}", get(user_show))
        .route("/users/{id}/ban", post(user_ban).delete(user_unban))
        .route("/users/{id}/ban/delete", post(user_unban))
        .route("/autocompletable/users", get(autocomplete))
        .route("/account/bots", get(bots_get).post(bot_create))
        .route(
            "/account/bots/{id}",
            patch(bot_update).put(bot_update).delete(bot_delete),
        )
        .route("/account/bots/{id}/edit", get(bot_edit))
        .route("/account/bots/{id}/update", post(bot_update))
        .route("/account/bots/{id}/avatar", post(bot_avatar_post))
        .route("/account/bots/{id}/avatar/delete", post(bot_avatar_delete))
        .route(
            "/account/bots/{id}/key",
            post(bot_key_rotate).put(bot_key_rotate),
        )
        .route("/account/bots/{id}/delete", post(bot_delete))
        .route(
            "/rooms/{id}/{bot_key}/messages",
            get(bot_messages_get).post(bot_messages_post),
        )
        .route(
            "/rooms/{id}/{bot_key}/messages/{mid}",
            patch(bot_message_update).delete(bot_message_delete),
        )
        .route(
            "/rooms/{id}/{bot_key}/messages/{mid}/boosts",
            post(bot_boost_create),
        )
        .route(
            "/rooms/{id}/{bot_key}/messages/{mid}/boosts/{bid}",
            delete(bot_boost_delete),
        )
        .route(
            "/messages/{id}/boosts",
            get(boosts_index).post(boost_create),
        )
        .route("/messages/{id}/boosts/new", get(boost_new))
        .route(
            "/messages/{id}/boosts/{bid}",
            delete(boost_delete).post(boost_delete),
        )
        .route("/messages/{id}/boosts/{bid}/delete", post(boost_delete))
        .route("/attachments/{id}", get(attachment_get))
        .route("/attachments/{id}/{kind}", get(attachment_variant))
        .route(
            "/rails/active_storage/blobs/redirect/{token}/{filename}",
            get(signed_blob_get),
        )
        .nest_service("/assets", ServeDir::new("static/assets"))
        .nest_service("/static", ServeDir::new("static"))
        .layer(axum::extract::DefaultBodyLimit::max(25 * 1024 * 1024))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            reject_banned_ip,
        ))
        .with_state(state);
    let addr: SocketAddr = env::var("RUSTFIRE_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:3000".into())
        .parse()?;
    println!("Rustfire listening on http://{addr}");
    axum::serve(
        tokio::net::TcpListener::bind(addr).await?,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{PushBody, public_ip, safe_return_path, valid_push_endpoint, valid_push_keys};
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use openssl::{
        bn::BigNumContext,
        ec::{EcGroup, EcKey, PointConversionForm},
        nid::Nid,
    };
    use std::net::IpAddr;

    #[test]
    fn emoji_only_matches_campfire_message_classes() {
        assert!(super::all_emoji("😄🤘"));
        assert!(super::all_emoji("❤️"));
        assert!(!super::all_emoji("Haha! 😄🤘"));
        assert!(!super::all_emoji("🔥\nmultiple lines\n💯"));
        assert!(!super::all_emoji("🔥 💯"));
        assert!(!super::all_emoji(""));
    }

    #[test]
    fn message_time_migration_preserves_rails_microseconds() {
        let expected = 1_767_225_600_123_456_000;
        assert_eq!(
            super::message_timestamp_ns("2026-01-01T00:00:00.123456Z"),
            Some(expected)
        );
        assert_eq!(
            super::message_timestamp_ns("2026-01-01 00:00:00.123456"),
            Some(expected)
        );
    }

    #[test]
    fn room_stream_signature_matches_pinned_campfire() {
        let key = super::rails_turbo_stream_key("test-secret-key-base").unwrap();
        let expected = "IloybGtPaTh2WTJGdGNHWnBjbVV2VW05dmJYTTZPazl3Wlc0dk1ROm1lc3NhZ2VzIg==--dcc17cfeecb1f593debdd6f13d526df3c6d3b2fe59ab972a8fe1e7c038384efd";
        assert_eq!(
            super::room_stream_token(&key, "Rooms::Open", 1).unwrap(),
            expected
        );
        assert_eq!(
            super::room_from_stream_token(&key, expected),
            Some((1, "Rooms::Open".into()))
        );
        assert!(super::room_from_stream_token(&key, &format!("{expected}x")).is_none());
        assert!(super::room_from_stream_token(&[7; 64], expected).is_none());
    }

    #[test]
    fn room_message_targets_match_pinned_sti_classes() {
        assert!(!super::esc("'\"<>&").contains('\''));
        assert_eq!(
            super::room_messages_target("Rooms::Open", 1).as_deref(),
            Some("messages_rooms_open_1")
        );
        assert_eq!(
            super::room_messages_target("Rooms::Closed", 2).as_deref(),
            Some("messages_rooms_closed_2")
        );
        assert_eq!(
            super::room_messages_target("Rooms::Direct", 3).as_deref(),
            Some("messages_rooms_direct_3")
        );
        assert!(super::room_messages_target("Room", 1).is_none());
    }
    use web_push::{
        ContentEncoding, SubscriptionInfo, VapidSignatureBuilder, WebPushMessageBuilder,
        request_builder,
    };

    #[test]
    fn remembered_page_stays_on_this_site() {
        let encoded = |path: &str| URL_SAFE_NO_PAD.encode(path);
        assert_eq!(
            safe_return_path(&encoded("/rooms/12/@34?around=1")),
            Some("/rooms/12/@34?around=1".to_string())
        );
        for path in [
            "//evil.example",
            "https://evil.example",
            "/\\evil.example",
            "/rooms/1\r\nLocation: https://evil.example",
        ] {
            assert!(safe_return_path(&encoded(path)).is_none());
        }
    }

    #[test]
    fn signed_mentions_require_the_same_key_and_unchanged_payload() {
        let key = [7u8; 32];
        let sgid = super::mention_sgid(&key, 42).unwrap();
        assert_eq!(super::mention_id_from_sgid(&key, &sgid), Some(42));
        assert_eq!(super::mention_id_from_sgid(&[8u8; 32], &sgid), None);
        assert_eq!(super::mention_id_from_sgid(&key, &format!("{sgid}A")), None);
        let attachment = format!(
            "<figure data-trix-attachment='{{\"contentType\":\"application/vnd.campfire.mention\",\"sgid\":\"{sgid}\"}}'></figure>"
        );
        assert_eq!(super::mention_ids(&attachment, &key, None), vec![42]);
        assert!(super::mention_ids(&attachment, &[8u8; 32], None).is_empty());
        let legacy_payload = b"gid://rustfire/User/42/attachable";
        let legacy_signature =
            super::mention_signature(&key, legacy_payload, openssl::hash::MessageDigest::sha256())
                .unwrap();
        let legacy_sgid = format!(
            "{}--{}",
            URL_SAFE_NO_PAD.encode(legacy_payload),
            URL_SAFE_NO_PAD.encode(legacy_signature)
        );
        assert_eq!(super::mention_id_from_sgid(&key, &legacy_sgid), Some(42));
    }

    #[test]
    fn rails_attachable_sgid_format_matches_the_pinned_verifier() {
        let key = super::rails_sgid_key("test-secret-key-base").unwrap();
        let token = "eyJfcmFpbHMiOnsiZGF0YSI6ImdpZDovL2NhbXBmaXJlL1VzZXIvNDI_ZXhwaXJlc19pbiIsInB1ciI6ImF0dGFjaGFibGUifX0=--3d8933c1a8fd0d7a289fd1f62b3a5ecf0045041e";
        assert_eq!(super::mention_sgid(&key, 42).unwrap(), token);
        assert_eq!(super::mention_id_from_sgid(&key, token), Some(42));
    }

    #[test]
    fn rails_avatar_signed_id_matches_the_pinned_verifier() {
        let key = super::rails_avatar_key("test-secret-key-base").unwrap();
        let token = "eyJfcmFpbHMiOnsiZGF0YSI6MSwicHVyIjoidXNlci9hdmF0YXIifX0--fe99b8547975d867621732d6e0d4344cea012c7eaf713418ef6b1414a24e2dd4";
        assert_eq!(super::avatar_token(&key, 1).unwrap(), token);
        assert_eq!(super::avatar_id_from_token(&key, token), Some(1));
        assert_eq!(super::avatar_id_from_token(&[8u8; 32], token), None);
        assert_eq!(
            super::avatar_id_from_token(&key, &format!("{token}A")),
            None
        );
        assert_eq!(
            super::avatar_path(&key, 1, "2026-09-25T01:00:54Z").unwrap(),
            format!("/users/{token}/avatar?v=20260925010054")
        );
        assert_eq!(
            super::avatar_path(&key, 1, "2026-09-25 01:00:54.533364").unwrap(),
            format!("/users/{token}/avatar?v=20260925010054")
        );
    }

    #[test]
    fn bans_only_record_public_addresses() {
        for address in [
            "127.0.0.1",
            "10.2.3.4",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.1.1",
            "::1",
            "fc00::1",
            "fe80::1",
        ] {
            assert!(!public_ip(address.parse::<IpAddr>().unwrap()), "{address}");
        }
        for address in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"] {
            assert!(public_ip(address.parse::<IpAddr>().unwrap()), "{address}");
        }
    }
    #[test]
    fn push_keys_build_encrypted_vapid_request() {
        let group = EcGroup::from_curve_name(Nid::X9_62_PRIME256V1).unwrap();
        let server = EcKey::generate(&group).unwrap();
        let recipient = EcKey::generate(&group).unwrap();
        let mut context = BigNumContext::new().unwrap();
        let public = recipient
            .public_key()
            .to_bytes(&group, PointConversionForm::UNCOMPRESSED, &mut context)
            .unwrap();
        let p256dh = URL_SAFE_NO_PAD.encode(public);
        let auth = URL_SAFE_NO_PAD.encode([17u8; 16]);
        let info =
            SubscriptionInfo::new("https://fcm.googleapis.com/fcm/send/test", &p256dh, &auth);
        assert!(valid_push_keys(&p256dh, &auth));
        let signature =
            VapidSignatureBuilder::from_der(server.private_key_to_der().unwrap().as_slice(), &info)
                .unwrap()
                .build()
                .unwrap();
        let mut builder = WebPushMessageBuilder::new(&info);
        builder.set_vapid_signature(signature);
        builder.set_payload(ContentEncoding::Aes128Gcm, b"{\"title\":\"test\"}");
        let request = request_builder::build_request::<PushBody>(builder.build().unwrap());
        assert_eq!(
            request.headers().get("content-encoding").unwrap(),
            "aes128gcm"
        );
        assert!(!request.into_body().0.is_empty());
    }
    #[test]
    fn push_endpoint_allowlist_excludes_other_hosts_and_ports() {
        assert!(valid_push_endpoint(
            "https://fcm.googleapis.com/fcm/send/id"
        ));
        assert!(!valid_push_endpoint(
            "https://fcm.googleapis.com.evil.test/push"
        ));
        assert!(!valid_push_endpoint("https://fcm.googleapis.com:444/push"));
        assert!(!valid_push_endpoint("http://fcm.googleapis.com/push"));
    }
    #[test]
    fn opengraph_parser_reads_meta_name_and_property() {
        let tags = super::og_attributes(
            "<meta property='og:title' content='A &amp; B'><meta name='og:description' content='A page'><meta property='og:image' content='https://example.com/p.png'>",
        );
        assert_eq!(tags.get("title").unwrap(), "A & B");
        assert_eq!(tags.get("description").unwrap(), "A page");
        assert_eq!(tags.get("image").unwrap(), "https://example.com/p.png");
        assert!(super::public_web_url("file:///etc/passwd").is_none());
    }
    #[test]
    fn opengraph_attachment_content_survives_rich_text_sanitization() {
        let (_, html) = super::rich_body(
            "<figure data-trix-attachment='{}'><div class='og-embed'><a href='https://example.com'>Title</a><p>Description</p><img src='https://example.com/image.png' alt=''></div></figure>",
            None,
        );
        assert!(html.contains("Title") && html.contains("Description"));
        assert!(html.contains("class=\"og-embed\""));
    }
    #[test]
    fn preview_urls_stay_on_external_named_web_hosts() {
        for candidate in [
            "javascript:alert(1)",
            "data:image/svg+xml;base64,PHN2Zy8+",
            "//example.com/image.png",
            "/rooms/1",
            "https:/rooms/1",
            "http://127.0.0.1/rooms/1",
            "http://2130706433/rooms/1",
            "http://0x7f.0.0.1/rooms/1",
            "http://localhost/rooms/1",
            "https://203.0.113.10/image.png",
            "https://once.campfire.test/rooms/1",
            "https://ONCE.Campfire.Test./rooms/1",
            "https://%6fnce.campfire.test/rooms/1",
            "https://%77ww.example.com/x.png",
        ] {
            assert!(
                super::safe_preview_url(candidate, Some("once.campfire.test")).is_none(),
                "{candidate}"
            );
        }
        assert!(
            super::safe_preview_url("https://example.com/page", Some("once.campfire.test"))
                .is_some()
        );
        assert!(
            super::safe_preview_url(
                "https://xn--80aswg.xn--p1ai/page",
                Some("once.campfire.test")
            )
            .is_some()
        );
    }
    #[test]
    fn action_text_preview_is_rendered_from_validated_attributes() {
        let source = "<div><action-text-attachment content-type='application/vnd.actiontext.opengraph-embed' href='javascript:alert(1)' url='data:image/svg+xml;base64,PHN2Zy8+' filename='Free cookies' caption='Cookies here'></action-text-attachment></div>";
        let (plain, html) = super::rich_body(source, Some("once.campfire.test"));
        assert!(html.contains("Free cookies") && html.contains("Cookies here"));
        assert!(!html.contains("javascript:") && !html.contains("data:image/svg"));
        assert!(!html.contains("<img") && !html.contains("<a"));
        assert!(
            plain.is_empty(),
            "plain={plain:?} html={html:?} source={source:?}"
        );
        let safe = source
            .replace("javascript:alert(1)", "https://example.com/page")
            .replace(
                "data:image/svg+xml;base64,PHN2Zy8+",
                "https://example.com/image.png",
            );
        let (_, html) = super::rich_body(&safe, Some("once.campfire.test"));
        assert!(html.contains("https://example.com/page"));
        assert!(html.contains("https://example.com/image.png"));
    }
    #[test]
    fn trix_preview_ignores_untrusted_embedded_html() {
        let data = serde_json::json!({
            "contentType":"application/vnd.actiontext.opengraph-embed",
            "filename":"Safe title",
            "caption":"Description",
            "href":"https://once.campfire.test/rooms/1",
            "url":"https://example.com/image.png",
            "content":"<a href='javascript:alert(1)'>Unsafe title</a>"
        });
        let source = format!(
            "<figure data-trix-attachment='{}'><div class='og-embed'><a href='javascript:alert(1)'>Unsafe title</a></div></figure>",
            super::esc(&data.to_string())
        );
        let (plain, html) = super::rich_body(&source, Some("once.campfire.test"));
        assert!(
            plain.is_empty(),
            "plain={plain:?} html={html:?} source={source:?}"
        );
        assert!(html.contains("Safe title") && html.contains("https://example.com/image.png"));
        assert!(!html.contains("Unsafe title") && !html.contains("once.campfire.test"));
    }
    #[test]
    fn attachment_byte_ranges_cover_start_suffix_and_invalid_ranges() {
        assert_eq!(super::byte_range("bytes=0-3", 10).unwrap(), (0, 3));
        assert_eq!(super::byte_range("bytes=8-", 10).unwrap(), (8, 9));
        assert_eq!(super::byte_range("bytes=-4", 10).unwrap(), (6, 9));
        assert!(super::byte_range("bytes=10-", 10).is_err());
        assert!(super::byte_range("bytes=0-1,4-5", 10).is_err());
    }
}
