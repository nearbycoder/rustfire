use axum::{
    Json, Router,
    body::{Body, Bytes, to_bytes},
    extract::{
        ConnectInfo, Form, FromRequest, Multipart, OriginalUri, Path, Query, RawForm, Request,
        State, WebSocketUpgrade,
        ws::{Message as WsMessage, WebSocket},
    },
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    middleware::Next,
    response::{Html, IntoResponse, Redirect, Response},
    routing::{delete, get, patch, post},
};
mod notification_help;
use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE, URL_SAFE_NO_PAD},
};
use bcrypt::{DEFAULT_COST, hash, verify};
use chrono::{Duration, SecondsFormat, Utc};
use futures_util::{SinkExt, StreamExt};
use openssl::{
    bn::BigNumContext,
    ec::{EcGroup, EcKey, EcPoint, PointConversionForm},
    hash::MessageDigest,
    memcmp,
    nid::Nid,
    pkcs5::pbkdf2_hmac,
    pkey::PKey,
    sign::Signer,
};
use qrcodegen::{Mask, QrCode, QrCodeEcc, QrSegment, QrSegmentMode, Version};
use r2d2::Pool;
use r2d2_sqlite::SqliteConnectionManager;
use regex::Regex;
use rusqlite::{OptionalExtension, params};
use scraper::{Html as ParsedHtml, Node as HtmlNode, Selector};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    env,
    io::SeekFrom,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    sync::{
        Arc, Mutex, OnceLock, RwLock,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration as StdDuration, UNIX_EPOCH},
};
use tokio::io::{AsyncReadExt, AsyncSeekExt};
use tokio::sync::{Semaphore, broadcast, mpsc};
use tokio_util::io::ReaderStream;
use tower_http::{compression::{CompressionLayer, predicate::{Predicate, SizeAbove}}, services::ServeDir};
use uuid::Uuid;
use web_push::{
    ContentEncoding, SubscriptionInfo, Urgency, VapidSignatureBuilder, WebPushMessageBuilder,
    request_builder,
};

type Db = Pool<SqliteConnectionManager>;
type AppResult = Result<Response, StatusCode>;
static VAPID_PUBLIC: OnceLock<String> = OnceLock::new();
static CUSTOM_STYLES: OnceLock<RwLock<Option<String>>> = OnceLock::new();

struct AppState {
    db: Db,
    autocomplete_cache: RwLock<HashMap<i64, CachedAutocompleteUser>>,
    autocomplete_slots: Arc<Semaphore>,
    events: RoomHub,
    typing_events: RoomHub,
    unread_events: RoomHub,
    read_events: RoomHub,
    room_list_events: RoomHub,
    turbo_user_rooms: RoomHub,
    turbo_shared_rooms: RoomHub,
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
    imported_cookie_signing_key: Option<Vec<u8>>,
    push_slots: Arc<Semaphore>,
    push_queue_slots: Arc<Semaphore>,
    has_push_subscriptions: AtomicBool,
    push_delivery_enabled: bool,
    login_attempts: Mutex<HashMap<IpAddr, VecDeque<std::time::Instant>>>,
    variant_slots: Arc<Semaphore>,
}
#[derive(Clone)]
struct CachedAutocompleteUser {
    updated_at: String,
    avatar_path: String,
    sgid: String,
}
#[derive(Clone)]
struct Event {
    room_id: i64,
    payload: String,
}
struct HubPayload {
    json: Arc<str>,
    turbo: Option<Arc<str>>,
    access: RwLock<HashMap<i64, bool>>,
    frames: RwLock<HashMap<(Arc<str>, bool), WsMessage>>,
}
impl HubPayload {
    fn accessible(&self, state: &AppState, uid: i64, rid: i64) -> bool {
        // Cache only within this event; the next broadcast rechecks current membership.
        if let Some(access) = self.access.read().unwrap().get(&uid) {
            return *access;
        }
        let mut access = self.access.write().unwrap();
        *access
            .entry(uid)
            .or_insert_with(|| room_accessible(state, uid, rid))
    }
    fn frame(&self, identifier: Arc<str>, turbo: bool) -> Option<WsMessage> {
        let key = (identifier, turbo);
        if let Some(frame) = self.frames.read().unwrap().get(&key) {
            return Some(frame.clone());
        }
        let mut frames = self.frames.write().unwrap();
        if let Some(frame) = frames.get(&key) {
            return Some(frame.clone());
        }
        let message = if turbo {
            self.turbo.as_deref()?
        } else {
            self.json.as_ref()
        };
        let frame = WsMessage::Text(
            format!("{{\"identifier\":{},\"message\":{message}}}", key.0).into(),
        );
        if frames.len() < 32 {
            frames.insert(key, frame.clone());
        }
        Some(frame)
    }
}
#[derive(Default)]
struct RoomHub {
    rooms: Mutex<HashMap<i64, broadcast::Sender<Arc<HubPayload>>>>,
}
impl RoomHub {
    fn channel(&self, room_id: i64) -> broadcast::Sender<Arc<HubPayload>> {
        let mut rooms = self.rooms.lock().unwrap();
        rooms
            .entry(room_id)
            .or_insert_with(|| broadcast::channel(1024).0)
            .clone()
    }
    fn send(&self, event: Event) {
        if let Some(channel) = self.rooms.lock().unwrap().get(&event.room_id) {
            let turbo = serde_json::from_str::<Value>(&event.payload)
                .ok()
                .and_then(|value| turbo_room_event(&value))
                .and_then(|html| serde_json::to_string(&html).ok())
                .map(Into::into);
            let payload = Arc::new(HubPayload {
                json: event.payload.into(),
                turbo,
                access: RwLock::new(HashMap::new()),
                frames: RwLock::new(HashMap::new()),
            });
            let _ = channel.send(payload);
        }
    }
    fn send_turbo(&self, stream_id: i64, html: String) {
        if let Some(channel) = self.rooms.lock().unwrap().get(&stream_id) {
            let payload = Arc::new(HubPayload {
                json: "null".into(),
                turbo: serde_json::to_string(&html).ok().map(Into::into),
                access: RwLock::new(HashMap::new()),
                frames: RwLock::new(HashMap::new()),
            });
            let _ = channel.send(payload);
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
            let db = pool(s)?;
            let (unread, room_updated_at): (bool, String) = db.query_row(
                "SELECT m.unread_at IS NOT NULL,r.updated_at FROM memberships m JOIN rooms r ON r.id=m.room_id WHERE m.room_id=?1 AND m.user_id=?2",
                params![rid, uid], |row| Ok((row.get(0)?, row.get(1)?)),
            ).map_err(db_err)?;
            let current = db.query_row("SELECT id,name,updated_at FROM users WHERE id=?1", [uid], |row| Ok(SidebarMember { id: row.get(0)?, name: row.get(1)?, updated_at: row.get(2)? })).map_err(db_err)?;
            let mut members_query = db.prepare("SELECT u.id,u.name,u.updated_at FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.room_id=?1 AND u.id!=?2 ORDER BY u.id").map_err(db_err)?;
            let members = members_query.query_map(params![rid,uid],|row|Ok(SidebarMember { id: row.get(0)?, name: row.get(1)?, updated_at: row.get(2)? })).map_err(db_err)?.collect::<Result<Vec<_>,_>>().map_err(db_err)?;
            sidebar_direct_link(s, &room, &room_updated_at, &current, &members, unread)
        })();
        match rendered {
            Ok(html) => {
                s.room_list_events.send(Event {
                    room_id: uid,
                    payload: json!({"type":"direct_room_added","room_id":rid,"html":html}).to_string(),
                });
                s.turbo_user_rooms.send_turbo(uid, format!("<turbo-stream action=\"prepend\" target=\"direct_rooms\"><template>\n  {html}</template></turbo-stream>"));
            },
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
    #[serde(skip_serializing)]
    account_updated_at: String,
}
#[derive(Clone, Serialize)]
struct Room {
    id: i64,
    name: String,
    kind: String,
    creator_id: i64,
}
struct SidebarMember {
    id: i64,
    name: String,
    updated_at: String,
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
    #[serde(skip_serializing)]
    width: Option<f64>,
    #[serde(skip_serializing)]
    height: Option<f64>,
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
    fn truncate(value: &str, limit: usize) -> String {
        if value.chars().count() <= limit {
            value.to_string()
        } else {
            format!("{}…", value.chars().take(limit - 1).collect::<String>())
        }
    }
    let title = esc(&truncate(attributes.get("filename").map(String::as_str).unwrap_or(""), 280));
    let description = esc(&truncate(attributes.get("caption").map(String::as_str).unwrap_or(""), 560));
    let link = attributes
        .get("href")
        .and_then(|url| safe_preview_url(url, host))
        .map(|url| {
            format!(
                "<a href='{}'>{title}</a>",
                esc(&url)
            )
        })
        .unwrap_or(title);
    let image = attributes
        .get("url")
        .and_then(|url| safe_preview_url(url, host))
        .map(|url| format!("<div class='og-embed__image'><img src='{}' class='image center' alt=''></div>", esc(&url)))
        .unwrap_or_default();
    format!("<div class='og-embed gap'><div class='og-embed__content'><div class='og-embed__title'>{link}</div><div class='og-embed__description'>{description}</div></div>{image}</div>")
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
    avatar_key: &[u8],
) -> Result<(String, String), StatusCode> {
    if !input.contains("application/vnd.campfire.mention")
        && !input.contains("application/vnd.rustfire.mention")
    {
        return Ok((input.to_string(), input.to_string()));
    }
    static ATTACHMENTS: OnceLock<Regex> = OnceLock::new();
    let pattern = ATTACHMENTS.get_or_init(|| {
        Regex::new(r"(?is)<figure\b[^>]*>.*?</figure>|<action-text-attachment\b[^>]*>.*?</action-text-attachment>").unwrap()
    });
    let selector = Selector::parse("figure[data-trix-attachment],action-text-attachment").unwrap();
    let mut rendered = String::with_capacity(input.len());
    let mut plain_source = String::with_capacity(input.len());
    let mut consumed = 0;
    for found in pattern.find_iter(input) {
        rendered.push_str(&input[consumed..found.start()]);
        plain_source.push_str(&input[consumed..found.start()]);
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
                            .and_then(|sgid| mention_user_id_from_sgid(signing_key, imported_key, sgid))
                    } else {
                        legacy_id.filter(|id| *id > 0)
                    };
                    if let Some(id) = id {
                        let user: Option<(String, String, String)> = db
                            .query_row("SELECT name,COALESCE(bio,''),updated_at FROM users WHERE id=?1", [id], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
                            .optional()
                            .map_err(db_err)?;
                        if let Some((name, bio, updated_at)) = user {
                            let title = if bio.trim().is_empty() { name.clone() } else { format!("{name} – {bio}") };
                            let avatar = avatar_path(avatar_key, id, &updated_at)?;
                            let display = format!(
                                "<div class='mention'><a href='/users/{id}' title='{}' class='btn avatar' data-turbo-frame='_top'><img aria-hidden='true' src='{}' width='48' height='48'></a>{}</div>",
                                esc(&title), esc(&avatar), esc(&name)
                            );
                            (display, format!("@{}", esc(&name)))
                        } else {
                            ("☒".to_string(), "☒".to_string())
                        }
                    } else {
                        ("☒".to_string(), "☒".to_string())
                    }
                }
                _ => (found.as_str().to_string(), found.as_str().to_string()),
            }
        } else {
            (found.as_str().to_string(), found.as_str().to_string())
        };
        rendered.push_str(&replacement.0);
        plain_source.push_str(&replacement.1);
        consumed = found.end();
    }
    rendered.push_str(&input[consumed..]);
    plain_source.push_str(&input[consumed..]);
    Ok((rendered, plain_source))
}
fn campfire_rich_tag_allowed(tag: &str) -> bool {
    matches!(tag,
        "a" | "abbr" | "acronym" | "address" | "b" | "big" | "blockquote" | "br" |
        "cite" | "code" | "dd" | "del" | "dfn" | "div" | "dl" | "dt" | "em" |
        "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "hr" | "i" | "ins" |
        "kbd" | "li" | "ol" | "p" | "pre" | "samp" | "small" | "span" |
        "strong" | "sub" | "sup" | "time" | "tt" | "ul" | "var" |
        "action-text-attachment" | "figure" | "figcaption"
    )
}
fn has_disallowed_rich_tag(input: &str) -> bool {
    let bytes = input.as_bytes();
    let mut offset = 0;
    while let Some(found) = bytes[offset..].iter().position(|byte| *byte == b'<') {
        let mut start = offset + found + 1;
        if bytes.get(start) == Some(&b'/') {
            start += 1;
        }
        let end = start + bytes[start..].iter().take_while(|byte| byte.is_ascii_alphanumeric() || **byte == b'-').count();
        if end > start && !campfire_rich_tag_allowed(&input[start..end].to_ascii_lowercase()) {
            return true;
        }
        offset = end;
    }
    false
}
fn strip_disallowed_rich_tags(input: &str) -> Option<String> {
    if !has_disallowed_rich_tag(input) {
        return None;
    }
    let mut document = ParsedHtml::parse_fragment(input);
    let selector = Selector::parse("*").unwrap();
    let root = document.root_element().id();
    let untrusted = document
        .select(&selector)
        .filter(|node| node.id() != root && !campfire_rich_tag_allowed(node.value().name()))
        .filter(|node| node.value().name() != "img" || !node.ancestors().any(|ancestor| {
            ancestor.value().as_element().is_some_and(|element| {
                element.name() == "action-text-attachment"
                    || (element.name() == "figure" && element.attr("data-trix-attachment").is_some())
            })
        }))
        .map(|node| node.id())
        .collect::<Vec<_>>();
    if untrusted.is_empty() {
        return None;
    }
    for id in untrusted {
        document.tree.get_mut(id).unwrap().detach();
    }
    Some(document.root_element().inner_html())
}
#[cfg(test)]
fn rich_body(input: &str, request_host: Option<&str>) -> (String, String) {
    let cleaned = strip_disallowed_rich_tags(input);
    let input = cleaned.as_deref().unwrap_or(input);
    rich_body_trusted(input, request_host)
}
fn campfire_safe_data_url(value: &str) -> bool {
    let Some(rest) = value.get(..5).filter(|prefix| prefix.eq_ignore_ascii_case("data:"))
        .and_then(|_| value.get(5..)) else {
        return true;
    };
    let Some((metadata, _)) = rest.split_once(',') else {
        return false;
    };
    let normalized = metadata.chars()
        .filter(|character| !matches!(*character as u32, 0..=0x20 | 0x7f..=0x101))
        .collect::<String>()
        .to_ascii_lowercase();
    let media_type = normalized.split(';').next().unwrap_or("");
    let token = |part: &str| !part.is_empty() && part.bytes().all(|byte| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte,
            b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'*' | b'+' | b'-' | b'.' |
            b'^' | b'_' | b'`' | b'|' | b'~')
    });
    let media_type = if media_type.split_once('/').is_some_and(|(kind, subtype)| token(kind) && token(subtype)) {
        media_type
    } else {
        "text/plain"
    };
    matches!(media_type, "image/gif" | "image/jpeg" | "image/png" | "text/css" | "text/plain")
}
fn rich_body_trusted(input: &str, request_host: Option<&str>) -> (String, String) {
    let display_input = replace_preview_attachments(input, request_host, true);
    let html = ammonia::Builder::default()
        .link_rel(None)
        .add_tags(&["action-text-attachment", "figure", "figcaption", "address", "big"])
        .tag_attributes(HashMap::new())
        .generic_attributes([
            "abbr", "alt", "cite", "class", "datetime", "height", "href", "lang",
            "name", "src", "title", "width", "xml:lang",
        ].into_iter().collect())
        .add_tag_attributes("action-text-attachment", &[
            "sgid", "content-type", "url", "filename", "filesize", "previewable",
            "presentation", "caption", "content",
        ])
        .url_schemes([
            "afs", "aim", "callto", "data", "ed2k", "fax", "ftp", "gopher", "http",
            "https", "irc", "line", "mailto", "modem", "news", "nntp", "rsync",
            "rtsp", "sftp", "sms", "ssh", "tag", "tel", "telnet", "urn", "webcal",
            "xmpp",
        ].into_iter().collect())
        .attribute_filter(|_, attribute, value| {
            if matches!(attribute, "href" | "src") && !campfire_safe_data_url(value) {
                None
            } else {
                Some(value.into())
            }
        })
        .clean(&display_input)
        .to_string();
    let plain_input =
        if input.contains("action-text-attachment") || input.contains("data-trix-attachment") {
            let without_previews = replace_preview_attachments(input, request_host, false);
            ammonia::Builder::default()
                .add_tags(&["action-text-attachment"])
                .add_tag_attributes("action-text-attachment", &["filename"])
                .clean(&without_previews)
                .to_string()
        } else {
            html.clone()
        };
    (action_text_plain_body(&plain_input), html)
}
fn has_preview_card(html: Option<&str>) -> bool {
    html.is_some_and(|html| html.contains("class=\"og-embed gap\""))
}
fn trim_plain_newlines(value: &str) -> &str {
    value.trim_end_matches('\n')
}
fn action_text_plain_node(node: ego_tree::NodeRef<'_, HtmlNode>) -> String {
    if let Some(text) = node.value().as_text() {
        return trim_plain_newlines(text).to_string();
    }
    let Some(element) = node.value().as_element() else {
        return node.children().map(action_text_plain_node).collect();
    };
    let name = element.name();
    if name == "script" || name == "style" {
        return String::new();
    }
    if name == "action-text-attachment" {
        if let Some(filename) = element.attr("filename") {
            return format!("[{filename}]");
        }
    }
    if name == "br" {
        return "\n".to_string();
    }
    let children: String = node.children().map(action_text_plain_node).collect();
    let trimmed = trim_plain_newlines(&children);
    match name {
        "div" => format!("{trimmed}\n"),
        "p" | "h1" => format!("{trimmed}\n\n"),
        "ul" | "ol" => {
            let nested = node.ancestors().any(|ancestor| {
                ancestor
                    .value()
                    .as_element()
                    .is_some_and(|parent| matches!(parent.name(), "ul" | "ol"))
            });
            format!("{}{trimmed}\n\n", if nested { "\n" } else { "" })
        }
        "li" => {
            let lists = node
                .ancestors()
                .filter_map(|ancestor| ancestor.value().as_element().map(|parent| parent.name().to_string()))
                .filter(|name| name == "ul" || name == "ol")
                .collect::<Vec<_>>();
            let bullet = if lists.first().is_some_and(|name| name == "ol") {
                format!("{}.", node.prev_siblings().filter(|sibling| sibling.value().as_element().is_some_and(|element| element.name() == "li")).count() + 1)
            } else {
                "•".to_string()
            };
            format!("{}{} {trimmed}\n", "  ".repeat(lists.len().saturating_sub(1)), bullet)
        }
        "blockquote" => {
            let mut value = format!("{trimmed}\n\n");
            if let (Some(first), Some(last)) = (
                value.char_indices().find(|(_, c)| !c.is_whitespace()).map(|(i, _)| i),
                value.char_indices().rfind(|(_, c)| !c.is_whitespace()).map(|(i, c)| i + c.len_utf8()),
            ) {
                value.insert_str(last, "”");
                value.insert_str(first, "“");
            } else {
                return "“”".to_string();
            }
            value
        }
        "figcaption" => format!("[{trimmed}]"),
        _ => children,
    }
}
fn action_text_plain(input: &str) -> String {
    let document = ParsedHtml::parse_fragment(input);
    trim_plain_newlines(&action_text_plain_node(document.tree.root())).to_string()
}
fn action_text_plain_body(input: &str) -> String {
    let plain = action_text_plain(input);
    if plain.trim().is_empty() {
        String::new()
    } else {
        plain
    }
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
fn rails_cookie_key(secret_key_base: &str) -> Result<Vec<u8>, openssl::error::ErrorStack> {
    rails_verifier_key(secret_key_base, b"signed cookie")
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
fn turbo_stream_token(key: &[u8], stream: &str) -> Result<String, openssl::error::ErrorStack> {
    let encoded = STANDARD.encode(json!(stream).to_string());
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha256())?
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("{encoded}--{signature}"))
}
fn turbo_stream_from_token(key: &[u8], token: &str) -> Option<String> {
    if token.len() > 4096 { return None; }
    let (encoded, signature) = token.split_once("--")?;
    if signature.len() != 64 { return None; }
    let expected = mention_signature(key, encoded.as_bytes(), MessageDigest::sha256()).ok()?
        .iter().map(|byte| format!("{byte:02x}")).collect::<String>();
    if !memcmp::eq(signature.as_bytes(), expected.as_bytes()) { return None; }
    serde_json::from_slice(&STANDARD.decode(encoded).ok()?).ok()
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
fn transfer_token(
    key: &[u8],
    id: i64,
    expires_at: chrono::DateTime<Utc>,
) -> Result<String, openssl::error::ErrorStack> {
    let exp = expires_at.format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
    let payload = json!({"_rails":{"data":id,"exp":exp,"pur":"user/transfer"}}).to_string();
    let encoded = URL_SAFE_NO_PAD.encode(payload);
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha256())?;
    let digest = signature
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn transfer_id_from_token(key: &[u8], token: &str) -> Option<i64> {
    if token.len() > 2048 {
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
    let decoded = URL_SAFE_NO_PAD.decode(encoded).ok()?;
    let payload: Value = serde_json::from_slice(&decoded).ok()?;
    let rails = payload.get("_rails")?;
    if rails.get("pur")?.as_str()? != "user/transfer" {
        return None;
    }
    let expires_at = chrono::DateTime::parse_from_rfc3339(rails.get("exp")?.as_str()?).ok()?;
    if expires_at <= Utc::now() {
        return None;
    }
    rails.get("data")?.as_i64().filter(|id| *id > 0)
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
fn blob_attachable_sgid(key: &[u8], id: i64) -> Result<String, openssl::error::ErrorStack> {
    let payload = json!({"_rails":{"data":format!("gid://campfire/ActiveStorage::Blob/{id}?expires_in"),"pur":"attachable"}}).to_string();
    let encoded = STANDARD.encode(payload);
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1())?;
    let digest = signature.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn direct_upload_storage_key() -> Result<String, openssl::error::ErrorStack> {
    const ALPHABET: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";
    let mut result = String::with_capacity(28);
    while result.len() < 28 {
        let mut random = [0u8; 32];
        openssl::rand::rand_bytes(&mut random)?;
        for byte in random {
            if byte < 252 && result.len() < 28 {
                result.push(ALPHABET[(byte % 36) as usize] as char);
            }
        }
    }
    Ok(result)
}
fn disk_token(key: &[u8], purpose: &str, data: Value) -> Result<String, openssl::error::ErrorStack> {
    let expiry = (Utc::now() + Duration::minutes(5)).to_rfc3339_opts(SecondsFormat::Millis, true);
    let payload = json!({"_rails":{"data":data,"exp":expiry,"pur":purpose}}).to_string();
    let encoded = STANDARD.encode(payload);
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1())?;
    let digest = signature.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn disk_token_data(key: &[u8], token: &str, purpose: &str) -> Option<Value> {
    if token.len() > 4096 { return None; }
    let (encoded, signature) = token.split_once("--")?;
    if signature.len() != 40 { return None; }
    let expected = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1()).ok()?
        .iter().map(|byte| format!("{byte:02x}")).collect::<String>();
    if !memcmp::eq(signature.as_bytes(), expected.as_bytes()) { return None; }
    let envelope: Value = serde_json::from_slice(&STANDARD.decode(encoded).ok()?).ok()?;
    let metadata = envelope.get("_rails")?;
    if metadata.get("pur")?.as_str()? != purpose { return None; }
    if chrono::DateTime::parse_from_rfc3339(metadata.get("exp")?.as_str()?).ok()? <= Utc::now() { return None; }
    metadata.get("data").cloned()
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
fn encoded_blob_filename(filename: &str) -> String {
    filename
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect::<String>()
}
fn blob_path(key: &[u8], id: i64, filename: &str) -> Result<String, StatusCode> {
    let encoded_filename = encoded_blob_filename(filename);
    Ok(format!(
        "/rails/active_storage/blobs/redirect/{}/{encoded_filename}",
        blob_token(key, id).map_err(db_err)?
    ))
}
fn image_format(content_type: &str) -> Option<&'static str> {
    match content_type {
        "image/png" => Some("png"),
        "image/jpeg" => Some("jpg"),
        "image/gif" => Some("gif"),
        "image/webp" => Some("webp"),
        "image/avif" => Some("avif"),
        "image/tiff" => Some("png"),
        _ => None,
    }
}
fn analyze_image_and_thumbnail(
    input: &std::path::Path,
    stored: &str,
    kind: &str,
    format: &str,
) -> (Option<i64>, Option<i64>) {
    let dimension = |field: &str| -> Option<i64> {
        let output = std::process::Command::new("vipsheader")
            .args(["-f", field])
            .arg(input)
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        String::from_utf8(output.stdout)
            .ok()?
            .trim()
            .parse::<i64>()
            .ok()
            .filter(|value| *value > 0)
    };
    let dimensions = (dimension("width"), dimension("height"));
    let cache = input
        .parent()
        .unwrap_or_else(|| std::path::Path::new("."))
        .join("variants");
    if std::fs::create_dir_all(&cache).is_ok() {
        let output = cache.join(format!("{stored}-{kind}.{format}"));
        let nonce = Uuid::new_v4();
        let stage = cache.join(format!("{stored}-{kind}-{nonce}.v"));
        let temporary = cache.join(format!("{stored}-{kind}-{nonce}.{format}"));
        let resized = std::process::Command::new("vips")
            .arg("thumbnail")
            .arg(input)
            .arg(&stage)
            .args(["1200", "--height", "800", "--size", "down"])
            .output()
            .is_ok_and(|result| result.status.success());
        let sharpened = resized
            && std::process::Command::new("vips")
                .arg("conv")
                .arg(&stage)
                .arg(&temporary)
                .arg("static/vips-sharpen-mask.txt")
                .args(["--precision", "integer"])
                .output()
                .is_ok_and(|result| result.status.success());
        let _ = std::fs::remove_file(&stage);
        if sharpened {
            if std::fs::rename(&temporary, &output).is_err() {
                let _ = std::fs::remove_file(&temporary);
            }
        } else {
            let _ = std::fs::remove_file(&temporary);
        }
    }
    dimensions
}
fn generate_inline_image_variant(input: &std::path::Path, output: &std::path::Path, width: i64, height: i64) -> bool {
    let Some(parent) = output.parent() else {
        return false;
    };
    if std::fs::create_dir_all(parent).is_err() {
        return false;
    }
    let extension = output.extension().and_then(|extension| extension.to_str()).unwrap_or("png");
    let temporary = parent.join(format!("inline-{}.{}", Uuid::new_v4(), extension));
    let width = width.to_string();
    let height = height.to_string();
    let converted = std::process::Command::new("vips")
        .arg("thumbnail")
        .arg(input)
        .arg(&temporary)
        .args([&width, "--height", &height, "--size", "down"])
        .output()
        .is_ok_and(|result| result.status.success());
    let published = converted && std::fs::rename(&temporary, output).is_ok();
    if !published {
        let _ = std::fs::remove_file(&temporary);
    }
    published
}
fn generate_inline_pdf_variant(input: &std::path::Path, output: &std::path::Path, width: i64, height: i64) -> bool {
    let Some(parent) = output.parent() else {
        return false;
    };
    if std::fs::create_dir_all(parent).is_err() {
        return false;
    }
    let prefix = parent.join(format!("pdf-{}", Uuid::new_v4()));
    let frame = prefix.with_extension("png");
    let rendered = std::process::Command::new("pdftoppm")
        .args(["-f", "1", "-singlefile", "-cropbox", "-r", "72", "-png"])
        .arg(input)
        .arg(&prefix)
        .output()
        .is_ok_and(|result| result.status.success());
    let published = rendered && generate_inline_image_variant(&frame, output, width, height);
    let _ = std::fs::remove_file(frame);
    published
}
fn generate_inline_video_variant(input: &std::path::Path, output: &std::path::Path, width: i64, height: i64) -> bool {
    let Some(parent) = output.parent() else {
        return false;
    };
    if std::fs::create_dir_all(parent).is_err() {
        return false;
    }
    let frame = parent.join(format!("video-{}.jpg", Uuid::new_v4()));
    let rendered = std::process::Command::new("ffmpeg")
        .args(["-v", "error", "-i"])
        .arg(input)
        .args(["-y", "-vframes", "1", "-f", "image2"])
        .arg(&frame)
        .output()
        .is_ok_and(|result| result.status.success());
    let published = rendered && generate_inline_image_variant(&frame, output, width, height);
    let _ = std::fs::remove_file(frame);
    published
}
fn analyze_video_and_poster(input: &std::path::Path, stored: &str) -> (Option<f64>, Option<f64>) {
    let dimensions = std::process::Command::new("ffprobe")
        .args([
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            "-v",
            "error",
        ])
        .arg(input)
        .output()
        .ok()
        .filter(|result| result.status.success())
        .and_then(|result| serde_json::from_slice::<Value>(&result.stdout).ok())
        .and_then(|probe| {
            let stream =
                probe.get("streams")?.as_array()?.iter().find(|stream| {
                    stream.get("codec_type").and_then(Value::as_str) == Some("video")
                })?;
            let encoded_width = stream.get("width").and_then(Value::as_f64);
            let encoded_height = stream.get("height").and_then(Value::as_f64);
            let computed_height = stream
                .get("display_aspect_ratio")
                .and_then(Value::as_str)
                .and_then(|ratio| ratio.split_once(':'))
                .and_then(|(numerator, denominator)| {
                    Some((
                        numerator.parse::<f64>().ok()?,
                        denominator.parse::<f64>().ok()?,
                    ))
                })
                .and_then(|(numerator, denominator)| {
                    if numerator > 0.0 {
                        Some(encoded_width? * denominator / numerator)
                    } else {
                        None
                    }
                });
            let angle = stream
                .get("tags")
                .and_then(|tags| tags.get("rotate"))
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<i64>().ok())
                .or_else(|| {
                    stream
                        .get("side_data_list")
                        .and_then(Value::as_array)?
                        .iter()
                        .find(|data| {
                            data.get("side_data_type").and_then(Value::as_str)
                                == Some("Display Matrix")
                        })?
                        .get("rotation")?
                        .as_f64()
                        .map(|value| value as i64)
                });
            let rotated = matches!(angle, Some(90 | -90 | 270 | -270));
            let display_height = computed_height.or(encoded_height);
            Some(if rotated {
                (display_height, encoded_width)
            } else {
                (encoded_width, display_height)
            })
        })
        .unwrap_or((None, None));
    let frame = std::process::Command::new("ffmpeg")
        .arg("-i")
        .arg(input)
        .args(["-y", "-vframes", "1", "-f", "image2", "-"])
        .output();
    if let Ok(frame) = frame {
        if frame.status.success() && !frame.stdout.is_empty() {
            let dir = input.parent().unwrap_or_else(|| std::path::Path::new("."));
            if std::fs::create_dir_all(dir).is_ok() {
                let jpeg = dir.join(format!("{stored}-frame-{}.jpg", Uuid::new_v4()));
                if std::fs::write(&jpeg, frame.stdout).is_ok() {
                    let _ = analyze_image_and_thumbnail(&jpeg, stored, "poster", "webp");
                }
                let _ = std::fs::remove_file(jpeg);
            }
        }
    }
    dimensions
}
fn image_variation_token(key: &[u8], format: &str) -> Result<String, StatusCode> {
    image_variation_token_sized(key, format, 1200, 800)
}
fn image_variation_token_sized(
    key: &[u8],
    format: &str,
    width: i64,
    height: i64,
) -> Result<String, StatusCode> {
    let payload =
        json!({"_rails":{"data":{"format":format,"resize_to_limit":[width,height]},"pur":"variation"}})
            .to_string();
    let encoded = STANDARD.encode(payload);
    let signature =
        mention_signature(key, encoded.as_bytes(), MessageDigest::sha1()).map_err(db_err)?;
    let digest = signature
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn inline_preview_variation_token(key: &[u8], width: i64, height: i64) -> Result<String, StatusCode> {
    let payload = json!({"_rails":{"data":{"resize_to_limit":[width,height]},"pur":"variation"}}).to_string();
    let encoded = STANDARD.encode(payload);
    let signature = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1()).map_err(db_err)?;
    let digest = signature.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
    Ok(format!("{encoded}--{digest}"))
}
fn representation_path(
    s: &AppState,
    attachment: &Attachment,
    format: &str,
) -> Result<String, StatusCode> {
    let key = s
        .imported_blob_signing_key
        .as_deref()
        .unwrap_or(&s.blob_signing_key);
    let blob = blob_path(key, attachment.id, &attachment.filename)?;
    let (prefix, filename) = blob
        .rsplit_once('/')
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok(format!(
        "{}/{}/{}",
        prefix.replacen("/blobs/", "/representations/", 1),
        image_variation_token(key, format)?,
        filename
    ))
}
fn image_representation_path(s: &AppState, attachment: &Attachment) -> Result<String, StatusCode> {
    let format = image_format(&attachment.content_type).ok_or(StatusCode::NOT_FOUND)?;
    representation_path(s, attachment, format)
}
fn pdf_representation_path(s: &AppState, attachment: &Attachment) -> Result<String, StatusCode> {
    let key = s
        .imported_blob_signing_key
        .as_deref()
        .unwrap_or(&s.blob_signing_key);
    let blob = blob_path(key, attachment.id, &attachment.filename)?;
    let (prefix, filename) = blob
        .rsplit_once('/')
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok(format!(
        "{}/{}/{}",
        prefix.replacen("/blobs/", "/representations/", 1),
        inline_preview_variation_token(key, 1200, 800)?,
        filename
    ))
}
fn inline_image_representation_path(
    key: &[u8],
    id: i64,
    filename: &str,
    format: &str,
    gallery: bool,
) -> Result<String, StatusCode> {
    let blob = blob_path(key, id, filename)?;
    let (prefix, filename) = blob
        .rsplit_once('/')
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok(format!(
        "{}/{}/{}",
        prefix.replacen("/blobs/", "/representations/", 1),
        image_variation_token_sized(key, format, if gallery { 800 } else { 1024 }, if gallery { 600 } else { 768 })?,
        filename
    ))
}
fn inline_media_representation_path(key: &[u8], id: i64, filename: &str, gallery: bool) -> Result<String, StatusCode> {
    let blob = blob_path(key, id, filename)?;
    let (prefix, filename) = blob.rsplit_once('/').ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok(format!("{}/{}/{}", prefix.replacen("/blobs/", "/representations/", 1), inline_preview_variation_token(key, if gallery { 800 } else { 1024 }, if gallery { 600 } else { 768 })?, filename))
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
fn unsigned_campfire_user_id(sgid: &str) -> Option<i64> {
    // Campfire's ActionText extension recovers User attachables after a
    // SECRET_KEY_BASE rotation. It never unmarshals the Rails 7 payload, and
    // it does not apply this fallback to other models or blob attachments.
    if sgid.len() > 1024 {
        return None;
    }
    let encoded = sgid.split_once("--").map_or(sgid, |(message, _)| message);
    let payload = STANDARD.decode(encoded).or_else(|_| URL_SAFE.decode(encoded)).or_else(|_| URL_SAFE_NO_PAD.decode(encoded)).ok()?;
    let envelope: Value = serde_json::from_slice(&payload).ok()?;
    let rails = envelope.get("_rails")?;
    let prefix = b"gid://campfire/User/";
    let id = if let Some(data) = rails.get("data").and_then(Value::as_str) {
        let rest = data.strip_prefix("gid://campfire/User/")?;
        let end = rest.find(|ch: char| !ch.is_ascii_digit()).unwrap_or(rest.len());
        if !matches!(rest.get(end..), Some("" | "?expires_in")) {
            return None;
        }
        rest.get(..end)?.parse::<i64>().ok()?
    } else {
        let encoded = rails.get("message")?.as_str()?;
        let marshaled = STANDARD.decode(encoded).or_else(|_| URL_SAFE.decode(encoded)).or_else(|_| URL_SAFE_NO_PAD.decode(encoded)).ok()?;
        let start = marshaled.windows(prefix.len()).position(|window| window == prefix)? + prefix.len();
        let end = marshaled[start..].iter().take_while(|byte| byte.is_ascii_digit()).count();
        std::str::from_utf8(marshaled.get(start..start + end)?).ok()?.parse::<i64>().ok()?
    };
    (id > 0).then_some(id)
}
fn mention_user_id_from_sgid(key: &[u8], imported_key: Option<&[u8]>, sgid: &str) -> Option<i64> {
    signed_mention_user_id(key, imported_key, sgid)
        .or_else(|| unsigned_campfire_user_id(sgid))
}
fn signed_mention_user_id(key: &[u8], imported_key: Option<&[u8]>, sgid: &str) -> Option<i64> {
    mention_id_from_sgid(key, sgid)
        .or_else(|| imported_key.and_then(|imported| mention_id_from_sgid(imported, sgid)))
}
fn campfire_blob_id_from_sgid(key: &[u8], sgid: &str) -> Option<i64> {
    let (encoded, signature) = sgid.split_once("--")?;
    if signature.len() != 40 || sgid.len() > 2048 {
        return None;
    }
    let expected = mention_signature(key, encoded.as_bytes(), MessageDigest::sha1()).ok()?
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if !memcmp::eq(signature.as_bytes(), expected.as_bytes()) {
        return None;
    }
    let envelope: Value = serde_json::from_slice(&URL_SAFE.decode(encoded).ok()?).ok()?;
    let metadata = envelope.get("_rails")?;
    if metadata.get("pur").and_then(Value::as_str) != Some("attachable") {
        return None;
    }
    if let Some(expiry) = metadata.get("exp").filter(|expiry| !expiry.is_null()) {
        if chrono::DateTime::parse_from_rfc3339(expiry.as_str()?).ok()? <= Utc::now() {
            return None;
        }
    }
    metadata
        .get("data")?
        .as_str()?
        .strip_prefix("gid://campfire/ActiveStorage::Blob/")?
        .strip_suffix("?expires_in")?
        .parse::<i64>()
        .ok()
        .filter(|id| *id > 0)
}
fn inline_file_size(size: i64) -> String {
    if size < 1024 {
        return format!("{size} Bytes");
    }
    let units = ["KB", "MB", "GB", "TB"];
    let mut value = size as f64 / 1024.0;
    let mut unit = 0;
    while value >= 1024.0 && unit < units.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    let precision = if value < 10.0 { 2 } else if value < 100.0 { 1 } else { 0 };
    let rounded = format!("{value:.precision$}");
    let rounded = if precision == 0 {
        rounded.as_str()
    } else {
        rounded.trim_end_matches('0').trim_end_matches('.')
    };
    format!("{rounded} {}", units[unit])
}
fn render_imported_inline_files(
    input: &str,
    db: &rusqlite::Connection,
    message_id: i64,
    signing_key: &[u8],
    imported_key: Option<&[u8]>,
    blob_key: &[u8],
    require_all: bool,
) -> Result<(String, Vec<i64>), StatusCode> {
    let expected = db.prepare("SELECT blob_id FROM inline_embeds WHERE message_id=?1")
        .map_err(db_err)?
        .query_map([message_id], |row| row.get::<_, i64>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    if expected.is_empty() {
        return Ok((input.to_string(), Vec::new()));
    }
    static INLINE_ATTACHMENT: OnceLock<Regex> = OnceLock::new();
    let pattern = INLINE_ATTACHMENT.get_or_init(|| Regex::new(r"(?is)<action-text-attachment\b[^>]*>.*?</action-text-attachment>").unwrap());
    let selector = Selector::parse("action-text-attachment[sgid]").unwrap();
    let document = ParsedHtml::parse_fragment(input);
    let all_attachments = document.select(&Selector::parse("action-text-attachment").unwrap()).collect::<Vec<_>>();
    let mut rendered = String::with_capacity(input.len() + 256);
    let mut used = Vec::new();
    let mut consumed = 0;
    for (index, found) in pattern.find_iter(input).enumerate() {
        rendered.push_str(&input[consumed..found.start()]);
        let in_gallery = all_attachments.get(index).is_some_and(|node| node.ancestors().any(|ancestor| {
            ancestor.value().as_element().and_then(|element| element.attr("class"))
                .is_some_and(|classes| classes.split_whitespace().any(|class| class == "attachment-gallery"))
        }));
        let fragment = ParsedHtml::parse_fragment(found.as_str());
        let attachment = fragment.select(&selector).next();
        let blob_id = attachment
            .as_ref()
            .and_then(|attachment| attachment.value().attr("sgid"))
            .and_then(|sgid| campfire_blob_id_from_sgid(signing_key, sgid)
                .or_else(|| imported_key.and_then(|key| campfire_blob_id_from_sgid(key, sgid))));
        if let Some(blob_id) = blob_id.filter(|id| expected.contains(id)) {
            let (filename, content_type, size, width, height): (String, String, i64, Option<i64>, Option<i64>) = db.query_row(
                "SELECT filename,content_type,byte_size,width,height FROM inline_blobs WHERE id=?1",
                [blob_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
            ).map_err(db_err)?;
            let extension = filename.rsplit_once('.').map(|(_, ext)| ext).unwrap_or("");
            let sgid = attachment.unwrap().value().attr("sgid").unwrap();
            let caption = attachment.as_ref().and_then(|attachment| attachment.value().attr("caption"));
            let caption_attribute = caption.map(|caption| format!(" caption=\"{}\"", esc(caption))).unwrap_or_default();
            let caption_html = if let Some(caption) = caption {
                esc(caption)
            } else {
                format!("<span class=\"attachment__name\">{}</span><span class=\"attachment__size\">{}</span>", esc(&filename), inline_file_size(size))
            };
            let (preview_attribute, figure_class, preview_html) = if let Some(format) = image_format(&content_type) {
                let path = inline_image_representation_path(blob_key, blob_id, &filename, format, in_gallery)?;
                let dimensions = match (width, height) {
                    (Some(width), Some(height)) if width > 0 && height > 0 => format!(" width=\"{width}\" height=\"{height}\""),
                    _ => String::new(),
                };
                (format!("{dimensions} previewable=\"true\""), "preview", format!("<img src=\"{}\">", esc(&path)))
            } else if content_type == "application/pdf" {
                let path = inline_media_representation_path(blob_key, blob_id, &filename, in_gallery)?;
                (" previewable=\"true\"".to_string(), "preview", format!("<img src=\"{}\">", esc(&path)))
            } else if safe_inline_video(&content_type) {
                let path = inline_media_representation_path(blob_key, blob_id, &filename, in_gallery)?;
                let dimensions = match (width, height) {
                    (Some(width), Some(height)) if width > 0 && height > 0 => format!(" width=\"{width}\" height=\"{height}\""),
                    _ => String::new(),
                };
                (format!("{dimensions} previewable=\"true\""), "preview", format!("<img src=\"{}\">", esc(&path)))
            } else {
                (String::new(), "file", String::new())
            };
            rendered.push_str(&format!("<action-text-attachment sgid=\"{}\" content-type=\"{}\" filename=\"{}\" filesize=\"{size}\"{caption_attribute}{preview_attribute}><figure class=\"attachment attachment--{figure_class} attachment--{}\">{preview_html}<figcaption class=\"attachment__caption\">{caption_html}</figcaption></figure></action-text-attachment>", esc(sgid), esc(&content_type), esc(&filename), esc(extension)));
            if !used.contains(&blob_id) {
                used.push(blob_id);
            }
        } else {
            rendered.push_str(found.as_str());
        }
        consumed = found.end();
    }
    rendered.push_str(&input[consumed..]);
    if require_all && expected.iter().any(|id| !used.contains(id)) {
        return Err(StatusCode::BAD_REQUEST);
    }
    Ok((rendered, used))
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
                .and_then(|sgid| signed_mention_user_id(signing_key, imported_key, sgid)),
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
            .and_then(|sgid| signed_mention_user_id(signing_key, imported_key, sgid));
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
fn rails_session_cookie_token(key: &[u8], signed: &str) -> Option<String> {
    if signed.len() > 4096 {
        return None;
    }
    let decoded = {
        let mut bytes = Vec::with_capacity(signed.len());
        let mut input = signed.as_bytes().iter().copied();
        while let Some(byte) = input.next() {
            if byte == b'%' {
                let high = (input.next()? as char).to_digit(16)?;
                let low = (input.next()? as char).to_digit(16)?;
                bytes.push(((high << 4) | low) as u8);
            } else {
                bytes.push(byte);
            }
        }
        String::from_utf8(bytes).ok()?
    };
    let (encoded, signature) = decoded.split_once("--")?;
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
    let payload: Value = serde_json::from_slice(&STANDARD.decode(encoded).ok()?).ok()?;
    let metadata = payload.get("_rails")?;
    if metadata.get("pur").and_then(Value::as_str) != Some("cookie.session_token") {
        return None;
    }
    let expiry = metadata.get("exp")?.as_str()?;
    if chrono::DateTime::parse_from_rfc3339(expiry).ok()? <= Utc::now() {
        return None;
    }
    let message = STANDARD.decode(metadata.get("message")?.as_str()?).ok()?;
    serde_json::from_slice::<String>(&message).ok()
}
fn session_token(state: &AppState, headers: &HeaderMap) -> Option<String> {
    let value = cookie(headers, "session_token")?;
    if value.contains("--") {
        rails_session_cookie_token(state.imported_cookie_signing_key.as_deref()?, &value)
    } else {
        Some(value)
    }
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
    let token = session_token(state, headers).ok_or(StatusCode::UNAUTHORIZED)?;
    let db = pool(state)?;
    db.query_row("SELECT u.id,u.name,COALESCE(u.email_address,''),u.role,u.bot_token,s.csrf_token,u.updated_at,a.updated_at FROM users u JOIN sessions s ON s.user_id=u.id JOIN accounts a ON a.id=1 WHERE s.token=?1 AND u.status=0", [token], |r| Ok(User { id:r.get(0)?,name:r.get(1)?,email:r.get(2)?,role:r.get(3)?,bot_token:r.get(4)?,csrf_token:r.get(5)?,updated_at:r.get(6)?,account_updated_at:r.get(7)? })).optional().map_err(db_err)?.ok_or(StatusCode::UNAUTHORIZED)
}
fn generate_bot_token() -> Result<String, StatusCode> {
    const ALPHANUMERIC: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    let mut token = String::with_capacity(12);
    while token.len() < 12 {
        let mut random = [0u8; 16];
        openssl::rand::rand_bytes(&mut random).map_err(db_err)?;
        for byte in random {
            if byte < 248 && token.len() < 12 {
                token.push(ALPHANUMERIC[(byte % 62) as usize] as char);
            }
        }
    }
    Ok(token)
}
fn bot_user(state: &AppState, key: &str) -> Result<User, StatusCode> {
    let (id, token) = key.split_once('-').ok_or(StatusCode::UNAUTHORIZED)?;
    let id: i64 = id.parse().map_err(|_| StatusCode::UNAUTHORIZED)?;
    let db = pool(state)?;
    db.query_row("SELECT id,name,COALESCE(email_address,''),role,bot_token,updated_at FROM users WHERE id=?1 AND bot_token=?2 AND role=2 AND status=0", params![id,token], |r| Ok(User{id:r.get(0)?,name:r.get(1)?,email:r.get(2)?,role:r.get(3)?,bot_token:r.get(4)?,updated_at:r.get(5)?,csrf_token:None,account_updated_at:String::new()})).optional().map_err(db_err)?.ok_or(StatusCode::UNAUTHORIZED)
}
fn webhook_bot_user(state: &AppState, id: i64) -> Result<User, StatusCode> {
    let db = pool(state)?;
    db.query_row("SELECT id,name,COALESCE(email_address,''),role,bot_token,updated_at FROM users WHERE id=?1 AND role=2", [id], |r| Ok(User{id:r.get(0)?,name:r.get(1)?,email:r.get(2)?,role:r.get(3)?,bot_token:r.get(4)?,updated_at:r.get(5)?,csrf_token:None,account_updated_at:String::new()})).optional().map_err(db_err)?.ok_or(StatusCode::NOT_FOUND)
}
fn bot_api_actor(state: &AppState, headers: &HeaderMap, key: &str) -> Result<User, StatusCode> {
    match user(state, headers) {
        Ok(actor) => Ok(actor),
        Err(StatusCode::UNAUTHORIZED) => bot_user(state, key),
        Err(error) => Err(error),
    }
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
fn room_accessible(state: &AppState, uid: i64, rid: i64) -> bool {
    pool(state)
        .and_then(|db| {
            db.query_row(
                "SELECT EXISTS(SELECT 1 FROM memberships m JOIN rooms r ON r.id=m.room_id WHERE m.user_id=?1 AND r.id=?2)",
                params![uid, rid],
                |row| row.get::<_, bool>(0),
            )
            .map_err(db_err)
        })
        .unwrap_or(false)
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
    fn tag_end(tag: &str) -> Option<usize> {
        let mut quote = None;
        for (index, byte) in tag.bytes().enumerate() {
            match (quote, byte) {
                (None, b'\'' | b'"') => quote = Some(byte),
                (Some(open), close) if open == close => quote = None,
                (None, b'>') => return Some(index),
                _ => {}
            }
        }
        None
    }
    let mut out = String::with_capacity(html.len() + 512);
    let mut rest = html;
    while let Some(start) = rest.find("<form") {
        out.push_str(&rest[..start]);
        rest = &rest[start..];
        let Some(end) = tag_end(rest) else { break };
        let tag = &rest[..=end];
        out.push_str(tag);
        rest = &rest[end + 1..];
        if tag.contains("method='post'") || tag.contains("method=\"post\"") {
            if rest.starts_with("<input type='hidden' name='_method'") {
                if let Some(method_end) = tag_end(rest) {
                    out.push_str(&rest[..=method_end]);
                    rest = &rest[method_end + 1..];
                }
            }
            out.push_str(&format!(
                "<input type='hidden' name='authenticity_token' value='{}'>",
                esc(token)
            ));
        }
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
fn render_unauth(title: &str, body: &str, logo_version: Option<&str>) -> Response {
    render_unauth_with_nav(title, body, "", logo_version)
}
fn render_unauth_with_nav(title: &str, body: &str, nav: &str, logo_version: Option<&str>) -> Response {
    let token = Uuid::new_v4().to_string();
    let mut response = render_source_page_sections_with_logo(title, body, nav, "", "", "", "", "", None, &token, logo_version);
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
fn render_source_page(title: &str, body: &str, nav: &str, current: Option<&User>, token: &str) -> Response {
    render_source_page_with_footer(title, body, nav, "", current, token)
}
fn render_source_page_with_footer(title: &str, body: &str, nav: &str, footer: &str, current: Option<&User>, token: &str) -> Response {
    render_source_page_sections(title, body, nav, footer, "", "", "", "", current, token)
}
fn render_source_page_sections(title: &str, body: &str, nav: &str, footer: &str, sidebar: &str, body_class_extra: &str, body_class_suffix: &str, head_extra: &str, current: Option<&User>, token: &str) -> Response {
    let logo_version = current.map(|user| user.account_updated_at.chars().filter(char::is_ascii_digit).take(14).collect::<String>());
    render_source_page_sections_with_logo(title, body, nav, footer, sidebar, body_class_extra, body_class_suffix, head_extra, current, token, logo_version.as_deref())
}
fn render_source_page_sections_with_logo(title: &str, body: &str, nav: &str, footer: &str, sidebar: &str, body_class_extra: &str, body_class_suffix: &str, head_extra: &str, current: Option<&User>, token: &str, logo_version: Option<&str>) -> Response {
    const CAMPFIRE_STYLES: &[&str] = &[
        "_reset-9c3efd7b.css", "actiontext-2aab36c6.css", "animation-bcdb4bab.css",
        "autocomplete-cdf3d8bd.css", "avatars-279376ab.css", "base-637a0ec8.css",
        "boosts-da4032a8.css", "buttons-c7d39fd4.css", "code-333ae548.css",
        "colorize-eb391ca0.css", "colors-aee62523.css", "composer-f81b1e02.css",
        "embeds-c2564969.css", "filters-8a4d63ed.css", "flash-a561c1e5.css",
        "inputs-9e58a07c.css", "layout-ff6ddfc6.css", "lightbox-af496d39.css",
        "messages-49d96172.css", "nav-05e7183a.css", "panels-af44ec6d.css",
        "separators-172b3ab8.css", "sidebar-0be58db9.css", "signup-6e02d591.css",
        "spinner-5a118f25.css", "trix-65afdb1d.css", "utilities-e2f32466.css",
    ];
    let styles = CAMPFIRE_STYLES.iter()
        .map(|file| format!("<link rel=\"stylesheet\" href=\"/assets/{file}\" data-turbo-track=\"reload\">"))
        .collect::<String>();
    let body_class = if current.is_some_and(is_admin) {
        format!("{body_class_extra} admin {body_class_suffix}").trim().to_string()
    } else if matches!(title, "Set up Rustfire" | "Sign up") {
        "signup".to_string()
    } else {
        format!("{body_class_extra} {body_class_suffix}").trim().to_string()
    };
    let current_user_meta = current.map(|user| format!("<meta name=\"current-user-id\" content=\"{}\"><meta name=\"current-user-name\" content=\"{}\">", user.id, esc(&user.name))).unwrap_or_default();
    let logo_url = logo_version.map_or_else(|| "/account/logo".to_string(), |version| format!("/account/logo?v={version}"));
    let html = format!(r##"<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no, interactive-widget=resizes-content"><meta name="view-transition" content="same-origin"><meta name="color-scheme" content="light dark"><meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="csrf-param" content="authenticity_token"><meta name='csrf-token' content='{token}'>{current_user_meta}<meta name="action-cable-url" content="/cable"><meta name="vapid-public-key" content="{vapid}"><meta name="turbo-prefetch" content="true"><link rel="manifest" href="/webmanifest.json"><link rel="icon" href="{logo_url}" type="image/png"><link rel="apple-touch-icon" href="{logo_url}">{styles}{custom_styles}<script defer src="/static/app.js"></script>{head_extra}</head>
<body class="{body_class}" data-controller="local-time lightbox"><a href="#main-content" class="skip-navigation btn">Skip to main content</a><nav id="nav">{nav}</nav><main id="main-content">{body}<footer id="footer">{footer}</footer></main><aside id="sidebar" data-controller="toggle-class" data-toggle-class-toggle-class="open">{sidebar}</aside><dialog class="lightbox" aria-label="Image Viewer (Press escape to close)" data-lightbox-target="dialog" data-action="close->lightbox#reset"><img src="" class="lightbox__image" data-lightbox-target="zoomedImage"><form method="dialog" class="lightbox__btn"><button class="btn"><img src="/assets/remove-0e7a045d.svg" aria-hidden="true"><span class="for-screen-reader">Close image viewer</span></button></form><a href="" class="lightbox__btn--download btn hide-in-ios-pwa" data-lightbox-target="download"><img src="/assets/download-04029899.svg" aria-hidden="true"><span class="for-screen-reader">Download file</span></a><button class="lightbox__btn--share btn" data-controller="web-share" data-action="web-share#share" data-web-share-files-value="" data-lightbox-target="share"><img src="/assets/share-bf28da4f.svg" aria-hidden="true"><span class="for-screen-reader">Share file</span></button></dialog><a href="https://once.com" id="app-logo" target="_blank" aria-label="Once software from 37signals home page"><img src="/assets/campfire-icon-3d9986c5.png" alt="Campfire logo" width="256" height="216"></a></body></html>"##,
        title = esc(title), token = esc(&token), vapid = VAPID_PUBLIC.get().map(String::as_str).unwrap_or(""), custom_styles = custom_styles_tag()
    );
    Html(if current.is_some() { html } else { csrf_forms(&html, token) }).into_response()
}
fn user_agent_version(user_agent: &str, marker: &str) -> Option<(u32, u32)> {
    let after = user_agent.split_once(marker)?.1;
    let version = after
        .split(|character: char| !character.is_ascii_digit() && character != '.')
        .next()?;
    let mut parts = version.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next().unwrap_or("0").parse().ok()?;
    Some((major, minor))
}
fn browser_blocked(user_agent: &str) -> bool {
    let agent = user_agent.to_ascii_lowercase();
    if agent.contains("bot") || agent.contains("chrome-lighthouse") {
        return false;
    }
    if agent.contains("msie ") || agent.contains("trident/") {
        return true;
    }
    if let Some(version) = user_agent_version(&agent, "opr/") {
        return version < (104, 0);
    }
    if let Some(version) = user_agent_version(&agent, "opera/") {
        return version < (104, 0);
    }
    if let Some(version) = user_agent_version(&agent, "firefox/") {
        return version < (121, 0);
    }
    if agent.contains("edge/") {
        return false;
    }
    if let Some(version) = user_agent_version(&agent, "crios/") {
        return version < (120, 0);
    }
    if let Some(version) = user_agent_version(&agent, "chrome/") {
        return version < (120, 0);
    }
    if agent.contains("safari/") {
        let ios_agent = agent.replace('_', ".");
        return user_agent_version(&agent, "version/")
            .or_else(|| user_agent_version(&ios_agent, "cpu iphone os "))
            .or_else(|| user_agent_version(&ios_agent, "cpu os "))
            .is_some_and(|version| version < (17, 2));
    }
    false
}
fn custom_styles_tag() -> String {
    CUSTOM_STYLES
        .get()
        .and_then(|styles| {
            styles
                .read()
                .unwrap()
                .as_ref()
                .map(|css| format!("<style data-turbo-track=\"reload\">{css}</style>"))
        })
        .unwrap_or_default()
}
fn incompatible_browser_page() -> Response {
    let translation = profile_translation_button(
        "Upgrade to a supported web browser. Campfire requires a modern web browser. Please use one of the browsers listed below and make sure auto-updates are enabled.",
        [
            "Actualiza a un navegador web compatible. Campfire requiere un navegador web moderno. Utiliza uno de los navegadores listados a continuación y asegúrate de que las actualizaciones automáticas estén habilitadas.",
            "Mettez à jour vers un navigateur web pris en charge. Campfire nécessite un navigateur web moderne. Veuillez utiliser l'un des navigateurs répertoriés ci-dessous et assurez-vous que les mises à jour automatiques sont activées.",
            "समर्थित वेब ब्राउज़र में अपग्रेड करें। Campfire को एक आधुनिक वेब ब्राउज़र की आवश्यकता है। कृपया नीचे सूचीबद्ध ब्राउज़रों में से कोई एक का उपयोग करें और सुनिश्चित करें कि स्वचालित अपडेट्स सक्षम हैं।",
            "Aktualisieren Sie auf einen unterstützten Webbrowser. Campfire erfordert einen modernen Webbrowser. Verwenden Sie bitte einen der unten aufgeführten Browser und stellen Sie sicher, dass automatische Updates aktiviert sind.",
            "Atualize para um navegador compatível. O Campfire requer um navegador moderno. Por favor, use um dos navegadores listados abaixo e certifique-se de que as atualizações automáticas estão ativadas.",
            "サポートされたウェブブラウザーにアップグレードしてください。Campfireはモダンなウェブブラウザーが必要です。下記のブラウザーのいずれかを使用し、自動更新が有効になっていることを確認してください。",
        ],
    );
    let browsers = [("safari", "Safari", "17.2"), ("chrome", "Chrome", "120"), ("firefox", "Firefox", "121"), ("opera", "Opera", "104")]
        .into_iter()
        .map(|(icon, name, version)| format!("<div class='browser flex flex-column'><img src='/assets/browsers/{icon}.svg' aria-hidden='true' class='center'><div class='flex flex-column align-center margin-block-start-half'><strong>{name}</strong><span>{version}+</span></div></div>"))
        .collect::<String>();
    Html(format!(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='color-scheme' content='light dark'><title>Unsupported browser</title><link rel='manifest' href='/webmanifest.json'><link rel='stylesheet' href='/static/unsupported.css'>{}</head><body><a class='skip-navigation' href='#main-content'>Skip to main content</a><nav id='nav'></nav><main id='main-content'><div class='panel center'><header><h1 class='txt-x-large txt-tight-lines txt-align-center margin-none-block-start margin-block-end'>Upgrade to a supported web browser</h1><div class='description flex align-start gap'>{translation}<p class='margin-none-block-start'>Campfire requires a modern web browser. Please use one of the browsers listed below and make sure auto-updates are enabled.</p></div></header><div class='browser-list flex align-center flex-wrap gap justify-center margin-block'>{browsers}</div></div></main><a href='https://once.com' id='app-logo' target='_blank' aria-label='Once software from 37signals home page'><img src='/static/icons/campfire-icon.png' alt='Campfire logo' width='256' height='216'></a></body></html>",
        custom_styles_tag(),
    ))
    .into_response()
}
fn render_with_csrf(title: &str, body: &str, current: Option<&User>, token: &str) -> Response {
    let account_stylesheet = if body.contains("class='panel account-settings") || body.contains("custom-styles-panel") {
        "<link rel='stylesheet' href='/static/account.css'>"
    } else {
        ""
    };
    let profile_stylesheet = if body.contains("profile-settings") {
        "<link rel='stylesheet' href='/static/profile.css'>"
    } else {
        ""
    };
    let nav = if let Some(u) = current {
        format!(
            "<div class='top-user'><span>{}</span><a href='/users/me/profile'>Settings</a><form method='post' action='/session/logout'><button>Sign out</button></form></div>",
            esc(&u.name)
        )
    } else {
        String::new()
    };
    let user_id = current.map(|u| u.id.to_string()).unwrap_or_default();
    let custom_styles = custom_styles_tag();
    let html = format!(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='csrf-token' content='{}'><meta name='vapid-public-key' content='{}'><meta name='theme-color' content='#f2ede3'><title>{} · Rustfire</title><link rel='icon' href='/account/logo'><link rel='manifest' href='/webmanifest.json'><link rel='stylesheet' href='/static/app.css'><link rel='stylesheet' href='/static/chat.css'><link rel='stylesheet' href='/static/trix.css'>{account_stylesheet}{profile_stylesheet}{custom_styles}<script defer src='/static/trix.js'></script><script defer src='/static/app.js'></script></head><body data-user-id='{}'><a class='skip' href='#main'>Skip to main content</a><header><a class='brand' href='/'><img src='/account/logo' alt=''>Rustfire</a>{}</header><main id='main'>{}</main></body></html>",
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
            // Campfire's Opengraph::Fetch accepts only an absolute HTTP(S)
            // Location and resolves its host again before the next request.
            url = public_web_url(location)?;
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
fn allowed_og_image_content_type(value: &str) -> bool {
    // Campfire lowercases the whole HEAD Content-Type header and compares it
    // directly; a parameterized value does not match its four allowed types.
    matches!(
        value.to_ascii_lowercase().as_str(),
        "image/jpeg" | "image/png" | "image/gif" | "image/webp"
    )
}
fn media_link(input: &str) -> bool {
    // Opengraph::Location checks the original URL string, including queries,
    // with a case-sensitive pattern rather than only the parsed path suffix.
    static MEDIA_URL: OnceLock<Regex> = OnceLock::new();
    MEDIA_URL.get_or_init(|| Regex::new(r"\bhttps?://\S+\.(?:zip|tar|tar\.gz|tar\.bz2|tar\.xz|gz|bz2|rar|7z|dmg|exe|msi|pkg|deb|iso|jpg|jpeg|png|gif|bmp|mp4|mov|avi|mkv|wmv|flv|heic|heif|mp3|wav|ogg|aac|wma|webm|ogv|mpg|mpeg)\b").expect("Campfire media URL pattern")).is_match(input)
}
async fn unfurl_url(input: &str) -> Option<Value> {
    let mut url = public_web_url(input)?;
    if media_link(input) {
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
                    .unwrap_or("");
                if allowed_og_image_content_type(kind) {
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
fn unfurl_input(headers: &HeaderMap, body: &[u8]) -> Result<String, StatusCode> {
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim();
    let url = match content_type {
        "application/json" => serde_json::from_slice::<UnfurlInput>(body)
            .map_err(|_| StatusCode::BAD_REQUEST)?
            .url,
        "application/x-www-form-urlencoded" => form_urlencoded::parse(body)
            .find(|(key, _)| key == "url")
            .map(|(_, value)| value.into_owned())
            .ok_or(StatusCode::BAD_REQUEST)?,
        _ => return Err(StatusCode::UNSUPPORTED_MEDIA_TYPE),
    };
    if url.trim().is_empty() {
        return Err(StatusCode::BAD_REQUEST);
    }
    Ok(url)
}
async fn unfurl_link(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> AppResult {
    let _ = user(&s, &headers)?;
    let url = unfurl_input(&headers, &body)?;
    match unfurl_url(&url).await {
        Some(value) => Ok(Json(value).into_response()),
        None => Ok(StatusCode::NO_CONTENT.into_response()),
    }
}
fn campfire_qr_mask_score(code: &QrCode) -> f64 {
    // RQRCode chooses a mask from a trial matrix with the format and version
    // bits unset. Its four demerit rules differ from qrcodegen's mask scoring.
    let size = code.size() as usize;
    let mut modules = (0..size)
        .flat_map(|y| (0..size).map(move |x| code.get_module(x as i32, y as i32)))
        .collect::<Vec<_>>();
    for i in 0..15 {
        let row = if i < 6 { i } else if i < 8 { i + 1 } else { size - 15 + i };
        let col = if i < 8 { size - i - 1 } else if i == 8 { 7 } else { 15 - i - 1 };
        modules[row * size + 8] = false;
        modules[8 * size + col] = false;
    }
    modules[(size - 8) * size + 8] = false;
    if code.version().value() >= 7 {
        for i in 0..18 {
            modules[(i / 3) * size + i % 3 + size - 11] = false;
            modules[(i % 3 + size - 11) * size + i / 3] = false;
        }
    }
    let at = |x: usize, y: usize| modules[y * size + x];
    let mut score = 0usize;
    for y in 0..size {
        for x in 0..size {
            let dark = at(x, y);
            let mut neighbors = 0;
            for dy in -1..=1 {
                for dx in -1..=1 {
                    if (dx != 0 || dy != 0)
                        && x.checked_add_signed(dx).is_some_and(|nx| nx < size)
                        && y.checked_add_signed(dy).is_some_and(|ny| ny < size)
                        && at(x.checked_add_signed(dx).unwrap(), y.checked_add_signed(dy).unwrap()) == dark {
                        neighbors += 1;
                    }
                }
            }
            if neighbors > 5 { score += 3 + neighbors - 5; }
            if x + 1 < size && y + 1 < size
                && dark == at(x + 1, y)
                && dark == at(x, y + 1)
                && dark == at(x + 1, y + 1) {
                score += 3;
            }
        }
    }
    let pattern = [true, false, true, true, true, false, true];
    for y in 0..size {
        for x in 0..=size - 7 {
            if (0..7).all(|offset| at(x + offset, y) == pattern[offset]) { score += 40; }
            if (0..7).all(|offset| at(y, x + offset) == pattern[offset]) { score += 40; }
        }
    }
    let dark_count = modules.iter().filter(|value| **value).count();
    score as f64 + ((100.0 * dark_count as f64 / (size * size) as f64 - 50.0).abs() / 5.0) * 10.0
}
fn campfire_qr_code(url: &str) -> Result<QrCode, StatusCode> {
    let segments = QrSegment::make_segments(url);
    let segment = segments.first().ok_or(StatusCode::BAD_REQUEST)?;
    // RQRCode's version search uses a strict less-than capacity check.
    // Numeric input with 34 digits, for example, exactly fills version 2
    // and therefore renders as version 3 in Campfire.
    const HIGH_CAPACITY_BITS: [usize; 40] = [
        72, 128, 208, 288, 368, 480, 528, 688, 800, 976,
        1120, 1264, 1440, 1576, 1784, 2024, 2264, 2504, 2728, 3080,
        3248, 3536, 3712, 4112, 4304, 4768, 5024, 5288, 5608, 5960,
        6344, 6760, 7208, 7688, 7888, 8432, 8768, 9136, 9776, 10208,
    ];
    let version = HIGH_CAPACITY_BITS.iter().enumerate().find_map(|(index, capacity)| {
        let count_bits = match segment.mode() {
            QrSegmentMode::Numeric => [10, 12, 14],
            QrSegmentMode::Alphanumeric => [9, 11, 13],
            _ => [8, 16, 16],
        }[(index + 1 > 26) as usize + (index + 1 > 9) as usize];
        let used = 4 + count_bits + segment.data().len();
        (used < *capacity).then(|| Version::new((index + 1) as u8))
    }).ok_or(StatusCode::BAD_REQUEST)?;
    let mut best: Option<(f64, QrCode)> = None;
    for mask in 0..8 {
        let code = QrCode::encode_segments_advanced(
            &segments, QrCodeEcc::High, version, version,
            Some(Mask::new(mask)), false,
        ).map_err(|_| StatusCode::BAD_REQUEST)?;
        let score = campfire_qr_mask_score(&code);
        if best.as_ref().is_none_or(|(lowest, _)| score < *lowest) {
            best = Some((score, code));
        }
    }
    Ok(best.unwrap().1)
}
async fn qr_code_show(Path(id): Path<String>, headers: HeaderMap) -> AppResult {
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
    let code = campfire_qr_code(url)?;
    let size = code.size();
    let extent = size * 11;
    let mut svg = format!(
        "<?xml version=\"1.0\" standalone=\"yes\"?><svg version=\"1.1\" xmlns=\"http://www.w3.org/2000/svg\" xmlns:xlink=\"http://www.w3.org/1999/xlink\" xmlns:ev=\"http://www.w3.org/2001/xml-events\" viewBox=\"0 0 {extent} {extent}\" shape-rendering=\"crispEdges\"><rect width=\"{extent}\" height=\"{extent}\" x=\"0\" y=\"0\" fill=\"white\"/>"
    );
    for y in 0..size {
        for x in 0..size {
            if code.get_module(x, y) {
                svg.push_str(&format!("<rect width=\"11\" height=\"11\" x=\"{}\" y=\"{}\" fill=\"black\"/>", x * 11, y * 11));
            }
        }
    }
    svg.push_str("</svg>");
    let digest = openssl::sha::sha256(svg.as_bytes());
    let etag = format!("W/\"{}\"", digest[..16].iter().map(|byte| format!("{byte:02x}")).collect::<String>());
    let fresh = headers.get(header::IF_NONE_MATCH)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.split(',').any(|candidate| {
            let candidate = candidate.trim();
            candidate == "*" || candidate.trim_start_matches("W/") == etag.trim_start_matches("W/")
        }));
    let mut response = if fresh { StatusCode::NOT_MODIFIED.into_response() } else { svg.into_response() };
    response.headers_mut().insert(header::CACHE_CONTROL, "max-age=31556952, public".parse().unwrap());
    response.headers_mut().insert(header::ETAG, etag.parse().unwrap());
    if !fresh {
        response.headers_mut().insert(header::CONTENT_TYPE, "image/svg+xml; charset=utf-8".parse().unwrap());
        response.headers_mut().insert(header::VARY, "Accept-Encoding".parse().unwrap());
    }
    Ok(response)
}
fn session_response(token: String, to: &str) -> Response {
    let mut r = found_redirect(to);
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
fn import_campfire_vapid_key(db_path: &std::path::Path) -> Result<(), Box<dyn std::error::Error>> {
    let private_encoded = env::var("RUSTFIRE_CAMPFIRE_VAPID_PRIVATE_KEY")?;
    let public_encoded = env::var("RUSTFIRE_CAMPFIRE_VAPID_PUBLIC_KEY")?;
    let private_bytes = URL_SAFE_NO_PAD
        .decode(&private_encoded)
        .or_else(|_| URL_SAFE.decode(&private_encoded))?;
    if private_bytes.len() != 32 {
        return Err("Campfire VAPID private key must decode to 32 bytes".into());
    }
    let group = EcGroup::from_curve_name(Nid::X9_62_PRIME256V1)?;
    let private = openssl::bn::BigNum::from_slice(&private_bytes)?;
    let mut context = BigNumContext::new()?;
    let mut public = EcPoint::new(&group)?;
    public.mul_generator2(&group, &private, &mut context)?;
    let public_bytes = public.to_bytes(&group, PointConversionForm::UNCOMPRESSED, &mut context)?;
    let source_public = URL_SAFE_NO_PAD
        .decode(&public_encoded)
        .or_else(|_| URL_SAFE.decode(&public_encoded))?;
    if source_public != public_bytes {
        return Err("Campfire VAPID public and private keys do not match".into());
    }
    let der = EcKey::from_private_components(&group, &private, &public)?.private_key_to_der()?;
    let path = env::var("RUSTFIRE_VAPID_KEY_FILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| db_path.with_extension("vapid.der"));
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    use std::io::Write;
    options.open(path)?.write_all(&der)?;
    Ok(())
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
                || value == Ipv4Addr::new(168, 63, 129, 16)
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
            // Follow the pinned Campfire Surfguard default policy. IPv4 mapped and
            // compatible forms are refused even if the embedded address is public.
            if value.to_ipv4_mapped().is_some() || seg[..6].iter().all(|part| *part == 0) {
                return false;
            }
            if seg[0] == 0x64 && seg[1] == 0xff9b && seg[2] == 1 {
                return false; // local-use NAT64 /48
            }
            if (seg[0] == 0x64 && seg[1] == 0xff9b && seg[2..6].iter().all(|part| *part == 0))
                || (seg[..4].iter().all(|part| *part == 0) && seg[4] == 0xffff && seg[5] == 0)
            {
                let embedded = Ipv4Addr::new(
                    (seg[6] >> 8) as u8,
                    seg[6] as u8,
                    (seg[7] >> 8) as u8,
                    seg[7] as u8,
                );
                return public_network_ip(IpAddr::V4(embedded));
            }
            if (seg[0] == 0x2001 && seg[1] == 3)
                || (seg[0] == 0x2001 && seg[1] == 4 && seg[2] == 0x112)
            {
                return true; // globally reachable IETF assignments
            }
            if (seg[0] == 0x2001 && seg[1] < 0x200)
                || seg[0] == 0x2002
                || (seg[0] == 0x2001 && seg[1] == 0x0db8)
                || (seg[0] == 0x100 && (seg[1..4].iter().all(|part| *part == 0)
                    || (seg[1] == 0 && seg[2] == 0 && seg[3] == 1)))
                || (seg[0] == 0x3fff && seg[1] >> 12 == 0)
                || seg[0] == 0x5f00
                || (seg[0] & 0xffc0) == 0xfec0
            {
                return false;
            }
            allocated_ipv6_unicast(value)
        }
    }
}
fn allocated_ipv6_unicast(address: Ipv6Addr) -> bool {
    // The pinned Surfguard IANA allocated-unicast snapshot, parsed once. Keep
    // this allowlist in sync when updating the Campfire compatibility target.
    const CIDRS: &[&str] = &[
        "2001::/23", "2001:200::/23", "2001:400::/23", "2001:600::/23", "2001:800::/22",
        "2001:c00::/23", "2001:e00::/23", "2001:1200::/23", "2001:1400::/22", "2001:1800::/23",
        "2001:1a00::/23", "2001:1c00::/22", "2001:2000::/19", "2001:4000::/23",
        "2001:4200::/23", "2001:4400::/23", "2001:4600::/23", "2001:4800::/23",
        "2001:4a00::/23", "2001:4c00::/23", "2001:5000::/20", "2001:8000::/19",
        "2001:a000::/20", "2001:b000::/20", "2002::/16", "2003::/18", "2400::/12",
        "2410::/12", "2600::/12", "2610::/23", "2620::/23", "2630::/12", "2800::/12",
        "2a00::/12", "2a10::/12", "2c00::/12",
    ];
    static RANGES: OnceLock<Vec<(u128, u8)>> = OnceLock::new();
    let ranges = RANGES.get_or_init(|| {
        CIDRS.iter().map(|cidr| {
            let (network, bits) = cidr.split_once('/').expect("IPv6 CIDR");
            (u128::from(network.parse::<Ipv6Addr>().expect("IPv6 address")),
                bits.parse::<u8>().expect("IPv6 prefix length"))
        }).collect()
    });
    let address = u128::from(address);
    ranges.iter().any(|(network, bits)| address >> (128 - bits) == network >> (128 - bits))
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
            db.execute("UPDATE memberships SET connections=CASE WHEN connected_at>=?1 THEN connections+1 ELSE 1 END,connected_at=?2,unread_at=NULL WHERE room_id=?3 AND user_id=?4",params![cutoff,current,rid,uid]).map_err(db_err)?;
        }
        "refresh" => {
            db.execute("UPDATE memberships SET connections=CASE WHEN connected_at>=?1 THEN connections ELSE 1 END,connected_at=?2 WHERE room_id=?3 AND user_id=?4",params![cutoff,current,rid,uid]).map_err(db_err)?;
        }
        "absent" => {
            db.execute("UPDATE memberships SET connections=CASE WHEN connected_at>=?1 THEN MAX(0,connections-1) ELSE 0 END,connected_at=CASE WHEN connected_at>=?1 AND connections>1 THEN connected_at ELSE NULL END WHERE room_id=?2 AND user_id=?3",params![cutoff,rid,uid]).map_err(db_err)?;
        }
        _ => return Err(StatusCode::BAD_REQUEST),
    }
    if action == "present" {
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
        let anonymous_direct_upload = path == "/rails/active_storage/direct_uploads"
            && request.method() == Method::POST
            && session_token(&s, request.headers()).is_none();
        let session_post_preauth = if path == "/session" && request.method() == Method::POST {
            match user(&s, request.headers()) {
                Ok(_) => false,
                Err(StatusCode::UNAUTHORIZED) => true,
                Err(code) => return code.into_response(),
            }
        } else {
            false
        };
        let preauth_route = path == "/first_run"
            || session_post_preauth
            || path.starts_with("/join/")
            || path.starts_with("/session/transfers/");
        let csrf = if path.starts_with("/rails/active_storage/disk/") && request.method() == Method::PUT {
            None
        } else if anonymous_direct_upload {
            match cookie(request.headers(), "preauth_csrf") {
                Some(token) => Some(token),
                None => return StatusCode::UNPROCESSABLE_ENTITY.into_response(),
            }
        } else if preauth_route {
            match cookie(request.headers(), "preauth_csrf") {
                Some(token) => Some(token),
                None => return StatusCode::FORBIDDEN.into_response(),
            }
        } else if let Some(session_token) = session_token(&s, request.headers()) {
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
                let bytes = match to_bytes(body, 128 * 1024 * 1024).await {
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
                    return (if anonymous_direct_upload { StatusCode::UNPROCESSABLE_ENTITY } else { StatusCode::FORBIDDEN }).into_response();
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
            .is_none_or(|value| value.contains("text/html") || value.contains("*/*"));
    let bot_api_request = {
        let segments: Vec<_> = request.uri().path().split('/').filter(|part| !part.is_empty()).collect();
        segments.len() >= 4
            && segments[0] == "rooms"
            && segments[1].parse::<i64>().is_ok()
            && segments[2] != "messages"
            && segments[3] == "messages"
    };
    let authenticated_bot_on_other_route = !bot_api_request
        && request.uri().query()
            .and_then(|query| form_urlencoded::parse(query.as_bytes())
                .find(|(name, _)| name == "bot_key")
                .map(|(_, key)| key.into_owned()))
            .is_some_and(|key| matches!(user(&s, request.headers()), Err(StatusCode::UNAUTHORIZED))
                && bot_user(&s, key.trim()).is_ok());
    let requested_path = request
        .uri()
        .path_and_query()
        .map(|value| value.as_str().to_string())
        .unwrap_or_else(|| "/".to_string());
    let sign_in_url = public_url(request.headers(), "/session/new");
    let response = next.run(request).await;
    let redirects_to_sign_in = response.status().is_redirection()
        && response.headers().get(header::LOCATION)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value == "/session/new" || value.ends_with("/session/new"));
    if authenticated_bot_on_other_route
        && (response.status() == StatusCode::UNAUTHORIZED || redirects_to_sign_in)
    {
        return StatusCode::FORBIDDEN.into_response();
    }
    if (browser_navigation || bot_api_request) && response.status() == StatusCode::UNAUTHORIZED {
        let encoded = URL_SAFE_NO_PAD.encode(requested_path.as_bytes());
        let mut redirect = found_redirect(&sign_in_url);
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
async fn reject_unsupported_browser(request: Request, next: Next) -> Response {
    let path = request.uri().path();
    if path == "/up"
        || path == "/cable"
        || path.starts_with("/assets/")
        || path.starts_with("/static/")
        || path.starts_with("/attachments/")
        || path.starts_with("/rails/")
    {
        return next.run(request).await;
    }
    if request
        .headers()
        .get(header::USER_AGENT)
        .and_then(|value| value.to_str().ok())
        .is_some_and(browser_blocked)
    {
        return incompatible_browser_page();
    }
    next.run(request).await
}
fn transfer_link(state: &AppState, uid: i64) -> Result<String, StatusCode> {
    let key = state
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&state.avatar_signing_key);
    let token = transfer_token(key, uid, Utc::now() + Duration::hours(4)).map_err(db_err)?;
    Ok(format!("/session/transfers/{token}"))
}
async fn transfer_show(State(s): State<Arc<AppState>>, Path(token): Path<String>) -> AppResult {
    let updated_at: Option<String> = pool(&s)?
        .query_row("SELECT updated_at FROM accounts LIMIT 1", [], |row| row.get(0))
        .optional()
        .map_err(db_err)?;
    let logo_version = updated_at.map(|value| value.chars().filter(char::is_ascii_digit).take(14).collect::<String>());
    Ok(render_unauth(
        "Sign in on this device",
        &format!(
            "<form data-controller='auto-submit' method='post' action='/session/transfers/{}'><input type='hidden' name='_method' value='put'></form>",
            esc(&token)
        ),
        logo_version.as_deref(),
    ))
}
async fn transfer_update(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Path(token): Path<String>,
) -> AppResult {
    let signed_uid = transfer_id_from_token(&s.avatar_signing_key, &token).or_else(|| {
        s.imported_avatar_signing_key
            .as_deref()
            .and_then(|key| transfer_id_from_token(key, &token))
    });
    let db = pool(&s)?;
    let uid: Option<i64> = if let Some(id) = signed_uid {
        db.query_row("SELECT id FROM users WHERE id=?1 AND status=0", [id], |r| {
            r.get(0)
        })
        .optional()
        .map_err(db_err)?
    } else {
        db.query_row(
            "SELECT t.user_id FROM session_transfers t JOIN users u ON u.id=t.user_id WHERE t.token=?1 AND t.expires_at>?2 AND u.status=0",
            params![token, now()], |r| r.get(0),
        ).optional().map_err(db_err)?
    };
    let uid = uid.ok_or(StatusCode::BAD_REQUEST)?;
    drop(db);
    let mut response = create_session(&s, uid, client_ip(&s.trusted_proxies, &headers, addr.ip()))?;
    *response.status_mut() = StatusCode::FOUND;
    Ok(response)
}
fn first_run_needed(state: &AppState) -> Result<bool, StatusCode> {
    let db = pool(state)?;
    let n: i64 = db
        .query_row("SELECT count(*) FROM accounts", [], |r| r.get(0))
        .map_err(db_err)?;
    Ok(n == 0)
}

async fn root(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    if first_run_needed(&s)? {
        return Ok(found_redirect("/session/new"));
    }
    let u = match user(&s, &headers) {
        Ok(u) => u,
        Err(_) => return Ok(found_redirect("/session/new")),
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
        Some(id) => found_redirect(&format!("/rooms/{id}")),
        None => render(
            "Welcome",
            "<div class='empty'><h1>Welcome to Rustfire</h1><p>Start a room or ping someone to begin.</p><a class='button' href='/rooms/opens/new'>Create a room</a></div>",
            Some(&u),
        ),
    })
}
async fn rooms_index(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = match user(&s, &headers) {
        Ok(user) => user,
        Err(StatusCode::UNAUTHORIZED) => return Ok(found_redirect("/session/new")),
        Err(error) => return Err(error),
    };
    let rid: Option<i64> = pool(&s)?
        .query_row(
            "SELECT r.id FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE m.user_id=?1 ORDER BY r.id DESC LIMIT 1",
            [u.id],
            |row| row.get(0),
        )
        .optional()
        .map_err(db_err)?;
    Ok(found_redirect(
        &rid.map(|id| format!("/rooms/{id}"))
            .unwrap_or_else(|| "/".to_string()),
    ))
}
async fn room_namespace_index(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    if first_run_needed(&s)? {
        return Ok(found_redirect("/session/new"));
    }
    match user(&s, &headers) {
        Ok(_) => Ok(found_redirect("/")),
        Err(StatusCode::UNAUTHORIZED) => Ok(found_redirect("/session/new")),
        Err(error) => Err(error),
    }
}
struct SignupSubmission {
    name: String,
    email: String,
    password: String,
    avatar: Option<(Vec<u8>, String)>,
}
async fn signup_submission(s: &Arc<AppState>, headers: &HeaderMap, req: Request) -> Result<SignupSubmission, StatusCode> {
    let mut avatar = None;
    let values = if headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .starts_with("multipart/form-data")
    {
        let mut multipart = Multipart::from_request(req, s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let mut values = HashMap::new();
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?
        {
            let name = field.name().unwrap_or("").to_string();
            if matches!(name.as_str(), "user[avatar]" | "avatar") {
                let content_type = field
                    .content_type()
                    .unwrap_or("application/octet-stream")
                    .to_string();
                let bytes = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
                if !bytes.is_empty() {
                    avatar = Some((bytes.to_vec(), sniff_upload_content_type(&bytes, &content_type).to_string()));
                }
            } else if matches!(
                name.as_str(),
                "name" | "user[name]" | "email_address" | "user[email_address]" | "password" | "user[password]"
            ) {
                values.insert(name, field.text().await.map_err(|_| StatusCode::BAD_REQUEST)?);
            }
        }
        values
    } else {
        let RawForm(raw) = RawForm::from_request(req, s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        fields(&raw).0
    };
    let name = form_value(&values, "name", "user[name]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?.trim().to_string();
    let email = form_value(&values, "email_address", "user[email_address]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?.trim().to_string();
    let password = form_value(&values, "password", "user[password]").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?.to_string();
    if name.is_empty() || password.is_empty() || !email.contains('@') {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    Ok(SignupSubmission { name, email, password, avatar })
}
async fn first_run_get(State(s): State<Arc<AppState>>) -> AppResult {
    if !first_run_needed(&s)? {
        return Ok(found_redirect("/"));
    }
    let name_translation = profile_translation_button("Enter your name", [
        "Introduce tu nombre", "Entrez votre nom", "अपना नाम दर्ज करें",
        "Geben Sie Ihren Namen ein", "Insira seu nome", "お名前を入力してください",
    ]);
    let email_translation = profile_translation_button("Enter your email address", [
        "Introduce tu correo electrónico", "Entrez votre adresse courriel",
        "अपना ईमेल पता दर्ज करें", "Geben Sie Ihre E-Mail-Adresse ein",
        "Insira seu endereço de email", "メールアドレスを入力してください",
    ]);
    let password_translation = profile_translation_button("Enter your password", [
        "Introduce tu contraseña", "Saisissez votre mot de passe",
        "अपना पासवर्ड दर्ज करें", "Geben Sie Ihr Passwort ein",
        "Insira sua senha", "パスワードを入力してください",
    ]);
    let body = format!(r#"<form class="center max-width" enctype="multipart/form-data" action="/first_run" accept-charset="UTF-8" method="post">
<section class="nametag u-relative"><div class="flex justify-center align-center pad-block"><img class="nametag__lanyard" aria-hidden="true" src="/assets/lanyard-945079e9.svg"></div>
<div class="nametag__inner flex flex-column gap"><fieldset class="flex flex-column center-block"><legend class="txt-large txt-align-center"><strong>Set up Rustfire</strong></legend>
<label class="align-center center avatar__form gap" data-controller="upload-preview"><div class="btn input--file"><img aria-hidden="true" src="/assets/camera-927323b8.svg"><input class="input" accept="image/*" data-upload-preview-target="input" data-action="upload-preview#previewImage" type="file" name="user[avatar]" id="user_avatar"><span class="for-screen-reader">Add your avatar</span></div><div class="btn avatar input--file txt-xx-large"><img aria-hidden="true" data-upload-preview-target="image" alt="Add your avatar" src="/assets/default-avatar-1ee67b00.svg"><span class="for-screen-reader">Avatar</span></div></label></fieldset>
<div class="flex align-center gap">{name_translation}<label class="flex align-center gap flex-item-grow txt-large input input--actor"><input class="input" autocomplete="name" placeholder="Name" autofocus="autofocus" required="required" data-1p-ignore="true" type="text" name="user[name]" id="user_name"><img aria-hidden="true" class="colorize--black" src="/assets/person-da193438.svg" width="24" height="24"></label></div>
<div class="flex align-center gap">{email_translation}<label class="flex align-center gap flex-item-grow txt-large input input--actor"><input class="input" autocomplete="username" placeholder="Email address" required="required" type="email" name="user[email_address]" id="user_email_address"><img aria-hidden="true" class="colorize--black" src="/assets/email-6c595bc5.svg" width="24" height="24"></label></div>
<div class="flex align-center gap">{password_translation}<label class="flex align-center gap flex-item-grow txt-large input input--actor"><input class="input" autocomplete="new-password" placeholder="Password" required="required" maxlength="72" size="72" type="password" name="user[password]" id="user_password"><img aria-hidden="true" class="colorize--black" src="/assets/password-0896da4e.svg" width="24" height="24"></label></div>
<button name="button" type="submit" class="btn btn--reversed center txt-large"><img aria-hidden="true" src="/assets/arrow-right-8f3eb40d.svg"><span class="for-screen-reader">Save</span></button></div></section></form>"#);
    Ok(render_unauth(
        "Set up Rustfire",
        &body,
        None,
    ))
}
async fn first_run_post(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    req: Request,
) -> AppResult {
    if !first_run_needed(&s)? {
        return Ok(found_redirect("/"));
    }
    let SignupSubmission { name, email, password, avatar } = signup_submission(&s, &headers, req).await?;
    let pw = hash(&password, DEFAULT_COST).map_err(db_err)?;
    let staged_avatar = if let Some((bytes, content_type)) = avatar {
        let dir = std::path::PathBuf::from(
            env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
        )
        .join("avatars");
        std::fs::create_dir_all(&dir).map_err(db_err)?;
        let stored = Uuid::new_v4().to_string();
        let path = dir.join(&stored);
        std::fs::write(&path, bytes).map_err(db_err)?;
        Some((path, stored, content_type))
    } else {
        None
    };
    let created = (|| -> Result<Option<i64>, StatusCode> {
        let mut db = pool(&s)?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(db_err)?;
        let existing: i64 = tx
            .query_row("SELECT count(*) FROM accounts", [], |r| r.get(0))
            .map_err(db_err)?;
        if existing > 0 {
            return Ok(None);
        }
        let t = now();
        tx.execute(
            "INSERT INTO accounts(id,name,join_code,created_at,updated_at) VALUES(1,'Campfire',?1,?2,?2)",
            params![Uuid::new_v4().to_string(), t],
        )
        .map_err(db_err)?;
        tx.execute("INSERT INTO users(name,email_address,password_digest,role,status,created_at,updated_at) VALUES(?1,?2,?3,1,0,?4,?4)",params![name,email,pw,t]).map_err(db_err)?;
        let uid = tx.last_insert_rowid();
        tx.execute("INSERT INTO rooms(name,type,creator_id,created_at,updated_at) VALUES('All Talk','Rooms::Open',?1,?2,?2)",params![uid,t]).map_err(db_err)?;
        let rid = tx.last_insert_rowid();
        tx.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) VALUES(?1,?2,'mentions',?3)",params![rid,uid,t]).map_err(db_err)?;
        if let Some((_, stored, content_type)) = &staged_avatar {
            tx.execute("INSERT INTO avatars(user_id,stored_name,content_type) VALUES(?1,?2,?3)",params![uid,stored,content_type]).map_err(db_err)?;
            tx.execute("UPDATE users SET updated_at=?1 WHERE id=?2",params![now(),uid]).map_err(db_err)?;
        }
        tx.commit().map_err(db_err)?;
        Ok(Some(uid))
    })();
    if !matches!(created, Ok(Some(_))) {
        if let Some((path, _, _)) = &staged_avatar {
            let _ = std::fs::remove_file(path);
        }
    }
    let Some(uid) = created? else { return Ok(found_redirect("/")) };
    create_session(&s, uid, client_ip(&s.trusted_proxies, &headers, addr.ip()))
}
fn login_page(s: &AppState, email_address: &str, rejection: Option<StatusCode>) -> AppResult {
    let db = pool(s)?;
    let (account_name, updated_at): (String, String) = db.query_row(
        "SELECT name,updated_at FROM accounts ORDER BY id LIMIT 1",
        [],
        |row| Ok((row.get(0)?, row.get(1)?)),
    ).map_err(db_err)?;
    let owner: Option<(String, String)> = db.query_row(
        "SELECT name,email_address FROM users WHERE role=1 AND email_address IS NOT NULL ORDER BY id LIMIT 1",
        [],
        |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional().map_err(db_err)?;
    let logo_version: String = updated_at.chars().filter(char::is_ascii_digit).take(14).collect();
    let email_translation = profile_translation_button("Enter your email address", [
        "Introduce tu correo electrónico", "Entrez votre adresse courriel",
        "अपना ईमेल पता दर्ज करें", "Geben Sie Ihre E-Mail-Adresse ein",
        "Insira seu endereço de email", "メールアドレスを入力してください",
    ]);
    let password_translation = profile_translation_button("Enter your password", [
        "Introduce tu contraseña", "Saisissez votre mot de passe",
        "अपना पासवर्ड दर्ज करें", "Geben Sie Ihr Passwort ein",
        "Insira sua senha", "パスワードを入力してください",
    ]);
    let help_contact = owner.map(|(name, email)| {
        let address = format!("mailto:\"{name}\" <{email}>");
        format!("<div class=\"txt-align-center margin-block-double full-width\"><a href=\"{}\" class=\"btn center\" title=\"Email {}\"><img src=\"/assets/lifebuoy-f31f26aa.svg\" aria-hidden=\"true\"><span>{}</span></a><div class=\"txt-align-center center margin-block txt-subtle\">Campfire&trade; version <span class=\"version-badge\">Rustfire</span></div></div>", esc(&address), esc(&name), esc(&email))
    }).unwrap_or_default();
    let flash = if rejection.is_some() {
        "<div class=\"flash\" role=\"alert\">Too many requests or unauthorized.</div>"
    } else { "" };
    let shake = if rejection.is_some() { " shake" } else { "" };
    let body = format!(r#"{flash}<section class="txt-align-center"><div class="panel{shake}"><figure class="account-logo avatar center margin-block-end txt-xx-large"><img alt="Account logo" src="/account/logo?v={logo_version}" width="300" height="300"></figure>
<form class="flex flex-column gap" action="/session" accept-charset="UTF-8" method="post"><fieldset class="flex flex-column gap center-block upad"><legend class="txt-large txt-align-center"><strong>{account_name}</strong></legend>
<div class="flex align-center gap">{email_translation}<label class="flex align-center gap input input--actor txt-large"><input required="required" class="input" autofocus="autofocus" autocomplete="username" placeholder="Enter your email address" value="{email_address}" type="email" name="email_address" id="email_address"><img aria-hidden="true" class="colorize--black" src="/assets/email-6c595bc5.svg" width="24" height="24"></label></div>
<div class="flex align-center gap">{password_translation}<label class="flex align-center gap input input--actor txt-large"><input required="required" class="input" autocomplete="current-password" placeholder="Enter your password" maxlength="72" size="72" type="password" name="password" id="password"><img aria-hidden="true" class="colorize--black" src="/assets/password-0896da4e.svg" width="24" height="24"></label></div>
<button class="btn btn--reversed center txt-large" type="submit" name="log_in"><img aria-hidden="true" src="/assets/arrow-right-8f3eb40d.svg"><span class="for-screen-reader">Go</span></button></fieldset></form></div>{help_contact}</section>"#,
        account_name = esc(&account_name),
        email_address = esc(email_address),
    );
    let mut response = render_unauth("Sign in", &body, Some(&logo_version));
    if let Some(status) = rejection {
        *response.status_mut() = status;
    }
    Ok(response)
}
async fn login_get(
    State(s): State<Arc<AppState>>,
    Query(query): Query<HashMap<String, String>>,
) -> AppResult {
    if first_run_needed(&s)? {
        return Ok(found_redirect("/first_run"));
    }
    login_page(&s, query.get("email_address").map(String::as_str).unwrap_or(""), None)
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
            return login_page(&s, &f.email_address, Some(StatusCode::TOO_MANY_REQUESTS));
        }
        times.push_back(moment);
    }
    let db = pool(&s)?;
    let row: Option<(i64, Option<String>)> = db
        .query_row(
            "SELECT id,password_digest FROM users WHERE email_address=?1 AND status=0",
            [&f.email_address],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    if let Some((id, Some(pw))) = row {
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
    login_page(&s, &f.email_address, Some(StatusCode::UNAUTHORIZED))
}
async fn session_post(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    RawForm(raw): RawForm,
) -> AppResult {
    let values = fields(&raw).0;
    if values.get("_method").map(String::as_str) == Some("delete") {
        return logout(State(s), headers, raw).await;
    }
    let email_address = values
        .get("email_address")
        .cloned()
        .ok_or(StatusCode::BAD_REQUEST)?;
    let password = values
        .get("password")
        .cloned()
        .ok_or(StatusCode::BAD_REQUEST)?;
    login_post(
        State(s),
        ConnectInfo(addr),
        headers,
        Form(Login {
            email_address,
            password,
        }),
    )
    .await
}
async fn logout(State(s): State<Arc<AppState>>, headers: HeaderMap, body: Bytes) -> AppResult {
    if let Some(t) = session_token(&s, &headers) {
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
    let mut r = found_redirect("/");
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
fn sidebar_room_link(room: &Room, _active: Option<i64>, unread: bool) -> String {
    format!(
        "<a id=\"{}\" data-rooms-list-target=\"room\" data-room-id=\"{}\" data-badge-dot-target=\"unread\" data-sorted-list-target=\"item\" data-sorted-list-name=\"{}\" style=\"--column-gap: 0.5em\" class=\"align-center gap room btn txt-nowrap{}\" href=\"/rooms/{}\">\n  <span class=\"overflow-ellipsis\">{}</span>\n</a>",
        room_list_target(room),
        room.id,
        esc(&room.name),
        if unread { " unread" } else { "" },
        room.id,
        esc(&room.name)
    )
}
fn room_list_target(room: &Room) -> String {
    let kind = match room.kind.as_str() {
        "Rooms::Open" => "rooms_open",
        "Rooms::Closed" => "rooms_closed",
        "Rooms::Direct" => "rooms_direct",
        _ => "room",
    };
    format!("list_{kind}_{}", room.id)
}
fn broadcast_user_room_visibility(s: &AppState, user_id: i64, room: &Room, visible: bool) {
    let target = room_list_target(room);
    let html = if visible {
        format!("<turbo-stream action=\"prepend\" target=\"shared_rooms\"><template>{}</template></turbo-stream>", sidebar_room_link(room, None, false))
    } else {
        format!("<turbo-stream action=\"remove\" target=\"{target}\"></turbo-stream>")
    };
    s.turbo_user_rooms.send_turbo(user_id, html);
}
fn broadcast_room_created(s: &AppState, room: &Room, members: &[i64]) {
    let html = format!("<turbo-stream action=\"prepend\" target=\"shared_rooms\"><template>{}</template></turbo-stream>", sidebar_room_link(room, None, false));
    if room.kind == "Rooms::Open" {
        s.turbo_shared_rooms.send_turbo(0, html);
    } else {
        for &id in members { s.turbo_user_rooms.send_turbo(id, html.clone()); }
    }
}
fn broadcast_room_updated(s: &AppState, room: &Room, members: &HashSet<i64>) {
    let html = format!("<turbo-stream action=\"replace\" target=\"{}\"><template>{}</template></turbo-stream>", room_list_target(room), sidebar_room_link(room, None, false));
    if room.kind == "Rooms::Open" {
        s.turbo_shared_rooms.send_turbo(0, html);
    } else {
        for &id in members { s.turbo_user_rooms.send_turbo(id, html.clone()); }
    }
}
fn sidebar_direct_link(
    s: &AppState,
    room: &Room,
    room_updated_at: &str,
    current_user: &SidebarMember,
    members: &[SidebarMember],
    unread: bool,
) -> Result<String, StatusCode> {
    let members = if members.is_empty() { std::slice::from_ref(current_user) } else { members };
    let names = if members.len() == 1 {
        esc(members[0].name.split_whitespace().next().unwrap_or(&members[0].name))
    } else {
        let short = members
            .iter()
            .map(|member| {
                member.name.split_whitespace()
                    .take(3)
                    .filter_map(|part| part.chars().next())
                    .flat_map(char::to_uppercase)
                    .collect::<String>()
            })
            .collect::<Vec<_>>();
        match short.as_slice() {
            [first, second] => format!("{first}+{second}"),
            _ => format!("{}, and {}", short[..short.len()-1].join(", "), short.last().unwrap()),
        }
    };
    let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
    let avatars = if members.len() == 1 {
        let member = &members[0];
        let url = avatar_path(avatar_key, member.id, &member.updated_at)?;
        format!("      <span class=\"avatar\">\n        <img aria-hidden=\"true\" src=\"{url}\" width=\"48\" height=\"48\" />\n      </span>\n")
    } else {
        let mut group = String::from("      <div class=\"avatar__group\">\n");
        for member in members.iter().take(4) {
            let url = avatar_path(avatar_key, member.id, &member.updated_at)?;
            group.push_str(&format!("          <span class=\"avatar\">\n            <img aria-hidden=\"true\" src=\"{url}\" width=\"20\" height=\"20\" />\n          </span>\n"));
        }
        group.push_str("      </div>\n");
        group
    };
    let epoch_ms = message_timestamp_ns(room_updated_at).ok_or(StatusCode::INTERNAL_SERVER_ERROR)? / 1_000_000;
    Ok(format!(
        "<a class=\"direct{}\" id=\"{}\" data-rooms-list-target=\"room\" data-room-id=\"{}\" data-badge-dot-target=\"unread\" data-sorted-list-target=\"item\" data-sorted-list-number=\"{epoch_ms}\" href=\"/rooms/{}\">\n{}\n    <span class=\"direct__author flex align-center gap max-width min-width border-radius txt-small\">\n      <span class=\"txt-nowrap overflow-ellipsis\">\n        <span class=\"for-screen-reader\">Ping with</span>\n          {}\n      </span>\n    </span>\n</a>",
        if unread { " unread" } else { "" },
        room_list_target(room),
        room.id,
        room.id,
        avatars,
        names
    ))
}
fn sidebar(s: &AppState, u: &User, active: Option<i64>) -> Result<String, StatusCode> {
    let rooms = rooms_for(s, u.id)?;
    let db = pool(s)?;
    let mut direct_members: HashMap<i64, Vec<SidebarMember>> = HashMap::new();
    let mut direct_member_query = db
        .prepare("SELECT m.room_id,u.id,u.name,u.updated_at FROM memberships m JOIN users u ON u.id=m.user_id JOIN rooms r ON r.id=m.room_id WHERE r.type='Rooms::Direct' AND m.user_id!=?1 AND EXISTS (SELECT 1 FROM memberships mine WHERE mine.room_id=m.room_id AND mine.user_id=?1) ORDER BY m.room_id,u.id")
        .map_err(db_err)?;
    for row in direct_member_query
        .query_map([u.id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                SidebarMember { id: row.get(1)?, name: row.get(2)?, updated_at: row.get(3)? },
            ))
        })
        .map_err(db_err)?
    {
        let (room_id, member) = row.map_err(db_err)?;
        direct_members.entry(room_id).or_default().push(member);
    }
    let mut direct_room_query = db.prepare("SELECT r.id,r.updated_at FROM rooms r JOIN memberships m ON m.room_id=r.id WHERE m.user_id=?1 AND r.type='Rooms::Direct'").map_err(db_err)?;
    let direct_updated: HashMap<i64, String> = direct_room_query.query_map([u.id], |row| Ok((row.get(0)?, row.get(1)?))).map_err(db_err)?.collect::<Result<_,_>>().map_err(db_err)?;
    let direct_participants: i64 = db
        .query_row(
            "SELECT COUNT(DISTINCT m2.user_id) FROM memberships m1 JOIN rooms r ON r.id=m1.room_id JOIN memberships m2 ON m2.room_id=r.id WHERE m1.user_id=?1 AND r.type='Rooms::Direct'",
            [u.id],
            |row| row.get(0),
        )
        .map_err(db_err)?;
    let placeholder_limit = (19 - direct_participants).max(0);
    let mut placeholder_query = db
        .prepare("SELECT id,name,updated_at FROM users WHERE status=0 AND id!=?1 AND id NOT IN (SELECT m2.user_id FROM memberships m1 JOIN rooms r ON r.id=m1.room_id JOIN memberships m2 ON m2.room_id=r.id WHERE m1.user_id=?1 AND r.type='Rooms::Direct') ORDER BY created_at,id LIMIT ?2")
        .map_err(db_err)?;
    let placeholders = placeholder_query
        .query_map(params![u.id, placeholder_limit], |row| {
            Ok(SidebarMember { id: row.get(0)?, name: row.get(1)?, updated_at: row.get(2)? })
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
    let stream_key = s.imported_turbo_stream_signing_key.as_deref().unwrap_or(&s.turbo_stream_signing_key);
    let shared_token = turbo_stream_token(stream_key, "rooms").map_err(db_err)?;
    let user_gid = URL_SAFE_NO_PAD.encode(format!("gid://campfire/User/{}", u.id));
    let user_token = turbo_stream_token(stream_key, &format!("{user_gid}:rooms")).map_err(db_err)?;
    let mut html = format!(
        "<aside class='sidebar'><turbo-frame data-turbo-permanent=\"true\" data-controller=\"rooms-list read-rooms turbo-frame\" data-rooms-list-unread-class=\"unread\" data-action=\"presence:present@window->rooms-list#read read-rooms:read->rooms-list#read turbo:frame-load->rooms-list#loaded refresh-room:visible@window->turbo-frame#reload\" id=\"user_sidebar\" target=\"_top\">\n  <turbo-cable-stream-source channel=\"Turbo::StreamsChannel\" signed-stream-name=\"{}\"></turbo-cable-stream-source>\n  <turbo-cable-stream-source channel=\"Turbo::StreamsChannel\" signed-stream-name=\"{}\"></turbo-cable-stream-source>\n\n  <div class=\"sidebar__container overflow-y overflow-hide-scrollbar\" data-controller=\"badge-dot\" data-badge-dot-unread-class=\"unread\" data-action=\"rooms-list:unread@window->badge-dot#update rooms-list:read@window->badge-dot#update turbo:submit-start->turbo-frame#unpermanize\"><turbo-frame id=\"direct_rooms_control\" target=\"_top\"><div class=\"directs gap overflow-x overflow-hide-scrollbar\"><a class=\"direct direct__new\" data-turbo-frame=\"_self\" href=\"/rooms/directs/new\"><span class=\"avatar avatar--icon\"><img aria-hidden=\"true\" class=\"colorize--black\" src=\"/assets/messages-add-d229e6c2.svg\" width=\"20\" height=\"20\" /></span><span class=\"direct__author flex max-width min-width border-radius pad-inline-half\"><span class=\"for-screen-reader\">New</span><span class=\"txt-small overflow-clip\">Ping</span></span></a><div id=\"direct_rooms\" contents data-controller=\"sorted-list\" data-action=\"rooms-list:unread@window->sorted-list#updateItem\">",
        esc(&shared_token), esc(&user_token)
    );
    for r in rooms.iter().filter(|r| r.kind == "Rooms::Direct") {
        html.push_str(&sidebar_direct_link(
            s,
            r,
            direct_updated.get(&r.id).ok_or(StatusCode::INTERNAL_SERVER_ERROR)?,
            &SidebarMember { id: u.id, name: u.name.clone(), updated_at: u.updated_at.clone() },
            direct_members.get(&r.id).map(Vec::as_slice).unwrap_or(&[]),
            unread.contains(&r.id),
        )?);
    }
    html.push_str("</div><div contents>");
    let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
    for member in placeholders {
        let first_name = member.name.split_whitespace().next().unwrap_or(&member.name);
        let avatar = avatar_path(avatar_key, member.id, &member.updated_at)?;
        html.push_str(&format!(
            "<form class=\"button_to\" method=\"post\" action=\"/rooms/directs?user_ids%5B%5D={}\"><button class=\"direct borderless fill-transparent unpad\" type=\"submit\">\n  <span class=\"avatar\">\n    <img aria-hidden=\"true\" src=\"{}\" />\n  </span>\n\n  <span class=\"direct__author flex align-center gap max-width min-width border-radius txt-small\">\n    <span class=\"txt-nowrap overflow-ellipsis\">\n      <span class=\"for-screen-reader\">Start a ping with</span>\n      {}\n    </span>\n  </span>\n</button><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\" /></form>",
            member.id,
            avatar,
            esc(first_name),
            esc(u.csrf_token.as_deref().unwrap_or("")),
        ));
    }
    html.push_str("</div></div></turbo-frame><div class='rooms position-relative flex flex-column gap'><div id='shared_rooms' contents data-controller='sorted-list'>");
    for r in rooms.iter().filter(|r| r.kind != "Rooms::Direct") {
        html.push_str(&sidebar_room_link(r, active, unread.contains(&r.id)));
    }
    html.push_str("</div>");
    if is_admin(u) || !restricted {
        html.push_str("<a class=\"rooms__new-btn btn room align-center gap txt-reversed\" aria-label=\"New Chat Room\" href=\"/rooms/opens/new\"><img aria-hidden=\"true\" src=\"/assets/add-f232d8a6.svg\" width=\"20\" height=\"20\" style=\"view-transition-name: new-room\" /></a>");
    }
    let current_avatar = avatar_path(avatar_key, u.id, &u.updated_at)?;
    html.push_str(&format!("</div><button class=\"btn sidebar__toggle\" data-action=\"toggle-class#toggle\"><img aria-hidden=\"true\" src=\"/assets/menu-5462dfd3.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">Open menu</span></button></div><div class=\"flex align-end sidebar__tools gap justify-end\"><a class=\"btn avatar flex-item-no-shrink sidebar__tool\" href=\"/users/me/profile\"><img aria-hidden=\"true\" src=\"{}\" width=\"48\" height=\"48\" style=\"view-transition-name: avatar-{}\" /><span class=\"for-screen-reader\">My Settings</span></a><a class=\"btn align-center gap txt-reversed sidebar__tool\" href=\"/account/edit\"><img aria-hidden=\"true\" src=\"/assets/settings-aee56972.svg\" width=\"20\" height=\"20\" style=\"view-transition-name: account-settings\" /><span class=\"for-screen-reader\">Account Settings</span></a></div></turbo-frame></aside>", current_avatar, u.id));
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
        "SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at,a.width,a.height FROM messages m INDEXED BY idx_messages_room_created_ns JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND {predicate} ORDER BY m.created_at_ns {order},m.id {order} LIMIT ?3"
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
        "SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at,a.width,a.height FROM messages m JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND m.id=?2",
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
    let mut query = db.prepare("SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at,a.width,a.height FROM messages m INDEXED BY idx_messages_room_created_ns JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND (m.created_at_ns,m.id)>(?2,?3) ORDER BY m.created_at_ns,m.id LIMIT ?4").map_err(db_err)?;
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
            width: r.get(15).unwrap_or_default(),
            height: r.get(16).unwrap_or_default(),
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
        "SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at,a.width,a.height FROM messages m INDEXED BY {index} JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id WHERE m.room_id=?1 AND {predicate} ORDER BY m.created_at_ns {order},m.id {order} LIMIT 40"
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
fn image_preview_dimensions(
    attachment: &Attachment,
    float_source: bool,
) -> Option<(String, String, String)> {
    let (width, height) = (attachment.width?, attachment.height?);
    if width <= 0.0 || height <= 0.0 {
        return None;
    }
    if width <= 1200.0 && height <= 800.0 {
        let (display_width, display_height, half_width) = if float_source {
            (
                format!("{width:?}"),
                format!("{height:?}"),
                format!("{:?}", width / 2.0),
            )
        } else {
            (
                (width as i64).to_string(),
                (height as i64).to_string(),
                ((width as i64) / 2).to_string(),
            )
        };
        return Some((
            display_width,
            display_height,
            format!("{}px; aspect-ratio: {:?};", half_width, width / height),
        ));
    }
    let factor = (1200.0 / width).min(800.0 / height);
    let scaled_width = width * factor;
    let scaled_height = height * factor;
    Some((
        format!("{scaled_width:?}"),
        format!("{scaled_height:?}"),
        format!(
            "{:?}px; aspect-ratio: {:?};",
            scaled_width / 2.0,
            scaled_width / scaled_height
        ),
    ))
}
fn attachment_presentation_html(s: &AppState, a: &Attachment) -> String {
    let filename = esc(&a.filename);
    let blob_url = attachment_blob_path(s, a);
    let download_url = format!("{blob_url}?disposition=attachment");
    if safe_inline_image(&a.content_type) {
        let (container_class, style, dimensions) =
            if let Some((width, height, style)) = image_preview_dimensions(a, false) {
                (
                    "max-inline-size center flex overflow-clip",
                    format!(" style='width: {style}'"),
                    format!(" width='{width}' height='{height}'"),
                )
            } else {
                (
                    "max-inline-size center overflow-clip",
                    String::new(),
                    String::new(),
                )
            };
        let representation = image_representation_path(s, a)
            .unwrap_or_else(|_| format!("/attachments/{}/thumb", a.id));
        format!(
            "<div class='{container_class}'{style}><a class='flex' href='{blob_url}' data-lightbox-target='image' data-action='lightbox#open' data-lightbox-url-value='{download_url}'><img{dimensions} class='message__attachment' loading='lazy' src='{representation}'></a></div>"
        )
    } else if a.content_type == "application/pdf" {
        let representation = pdf_representation_path(s, a)
            .unwrap_or_else(|_| format!("/attachments/{}/thumb", a.id));
        format!(
            "<div class='max-inline-size center overflow-clip'><a class='flex' href='{blob_url}' data-lightbox-target='image' data-action='lightbox#open' data-lightbox-url-value='{download_url}'><img class='message__attachment' loading='lazy' src='{representation}'></a></div>"
        )
    } else if safe_inline_video(&a.content_type) {
        let (container_class, style) =
            if let Some((_, _, style)) = image_preview_dimensions(a, true) {
                (
                    "max-inline-size center flex overflow-clip",
                    format!(" style='width: {style}'"),
                )
            } else {
                ("max-inline-size center overflow-clip", String::new())
            };
        let poster = representation_path(s, a, "webp")
            .unwrap_or_else(|_| format!("/attachments/{}/poster", a.id));
        format!(
            "<div class='{container_class}'{style}><video src='{blob_url}' poster='{poster}' controls='controls' preload='none' width='100%' height='100%' class='message__attachment'></video></div>"
        )
    } else {
        format!(
            "<div class='flex-inline align-center gap-half'><img class='colorize--black' aria-hidden='true' src='/assets/common-file-text-9043d980.svg' width='22' height='22'><span>{filename}</span><a class='btn message__action-btn hide-in-ios-pwa' style='--width: auto;' href='{download_url}'><img aria-hidden='true' src='/assets/download-04029899.svg' width='20' height='20'><span class='for-screen-reader'>Download {filename}</span></a><button class='btn message__action-btn' style='--width: auto;' data-controller='web-share' data-action='web-share#share' data-web-share-files-value='{download_url}'><img aria-hidden='true' src='/assets/share-bf28da4f.svg' width='20' height='20'><span class='for-screen-reader'>Share {filename}</span></button></div>"
        )
    }
}
fn normalize_preview_url(value: &str) -> String {
    let Ok(mut url) = reqwest::Url::parse(value.trim()) else {
        return value.to_string();
    };
    match url.host_str().map(str::to_ascii_lowercase).as_deref() {
        Some("x.com") => {
            if url.set_host(Some("twitter.com")).is_err() {
                return value.to_string();
            }
            url.set_query(None);
            url.to_string()
        }
        Some("twitter.com") => {
            url.set_query(None);
            url.to_string()
        }
        _ => value.to_string(),
    }
}
fn preview_presentation_body(plain: &str, html: &str) -> String {
    if !html.contains("og-embed") {
        return html.to_string();
    }
    let document = ParsedHtml::parse_fragment(html);
    let selector = Selector::parse(".og-embed").unwrap();
    let mut previews = document.select(&selector);
    let Some(preview) = previews.next() else {
        return html.to_string();
    };
    let only_preview = previews.next().is_none();
    let href = preview
        .select(&Selector::parse(".og-embed__title a[href]").unwrap())
        .next()
        .and_then(|link| link.value().attr("href"));
    let avatar = document
        .select(&Selector::parse(".og-embed .og-embed__image img[src]").unwrap())
        .any(|image| image.value().attr("src").is_some_and(|url| url.starts_with("https://pbs.twimg.com/profile_images")));
    if only_preview && href.is_some_and(|url| normalize_preview_url(url) == normalize_preview_url(plain)) {
        return format!(
            "<div{}>{}</div>",
            if avatar { " class='cf-twitter-avatar'" } else { "" },
            preview.html()
        );
    }
    if avatar {
        static FIRST_DIV: OnceLock<Regex> = OnceLock::new();
        return FIRST_DIV
            .get_or_init(|| Regex::new(r"(?i)<div\b[^>]*>").unwrap())
            .replacen(html, 1, "<div class='cf-twitter-avatar'>")
            .into_owned();
    }
    html.to_string()
}
fn message_presentation_html(s: &AppState, m: &ChatMessage) -> String {
    let attachment = m
        .attachment
        .as_ref()
        .map(|a| attachment_presentation_html(s, a))
        .unwrap_or_default();
    let presentation = if m.attachment.is_some() {
        String::new()
    } else {
        sound_presentation(&m.body)
            .or_else(|| {
                m.body_html
                    .as_ref()
                    .map(|html| format!("<div class='trix-content'>{}</div>", preview_presentation_body(&m.body, html)))
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
fn boost_area_html(s: &AppState, m: &ChatMessage) -> String {
    let client_id = esc(&m.client_message_id);
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
    format!(
        "<turbo-frame id='boosting_message_{client_id}'><div class='boosts flex flex-wrap align-center gap full-width' style='--column-gap: 0.4ch; --row-gap: 0' data-controller='turbo-streaming' data-action='turbo:submit-start-&gt;turbo-streaming#unsubscribe'><div class='flex-inline flex-wrap gap' id='boosts_message_{client_id}' data-turbo-streaming-target='container'>{boosts}</div><turbo-frame id='new_boost_message_{client_id}'><div class='flex-inline message__boost-inline' data-controller='soft-keyboard'><a class='boost__action txt-small btn' href='/messages/{}/boosts/new' action='soft-keyboard#open'><img aria-hidden='true' src='/assets/boost-4a7bab66.svg' width='20' height='20'><span class='for-screen-reader'>Add a boost</span></a></div></turbo-frame></div></turbo-frame>",
        m.id
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
    let creator_name = esc(&m.creator_name);
    let datetime = esc(&datetime);
    let message_classes = if all_emoji(&m.body) {
        "message message--emoji"
    } else {
        "message "
    };
    let boost_area = boost_area_html(s, m);
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
#[derive(Deserialize)]
struct SoundAsset {
    audio: String,
    image: Option<String>,
    width: Option<u32>,
    height: Option<u32>,
}
fn sound_presentation(body: &str) -> Option<String> {
    let name = body.strip_prefix("/play ")?;
    if name.is_empty() || !name.bytes().all(|c| c.is_ascii_alphanumeric()) {
        return None;
    }
    static ASSETS: OnceLock<HashMap<String, SoundAsset>> = OnceLock::new();
    let assets = ASSETS.get_or_init(|| serde_json::from_str(include_str!("../static/sound-assets.json")).expect("valid sound asset manifest"));
    let asset = assets.get(name)?;
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
    let visual = if let Some(image) = &asset.image {
        format!(
            "<img src='/assets/{image}' width='{}' height='{}' class='align--middle'>",
            asset.width.unwrap(), asset.height.unwrap()
        )
    } else {
        esc(label)
    };
    Some(format!(
        "<div class='sound' data-controller='sound' data-action='messages:play->sound#play' data-sound-url-value='/assets/{}'><button class='btn btn--plain' data-action='sound#play'>🔊</button>{visual}</div>",
        asset.audio
    ))
}
fn safe_inline_image(content_type: &str) -> bool {
    matches!(
        content_type,
        "image/png" | "image/jpeg" | "image/gif" | "image/webp" | "image/avif"
    )
}
fn variable_avatar_image(content_type: &str) -> bool {
    // Campfire subtracts BMP, icon, and Photoshop from Active Storage's
    // variable image types. Other uploads remain attached but show a fallback.
    matches!(content_type,
        "image/png" | "image/gif" | "image/jpeg" | "image/tiff" |
        "image/webp" | "image/avif" | "image/heic" | "image/heif")
}
fn sniff_upload_content_type<'a>(bytes: &[u8], declared: &'a str) -> &'a str {
    // Active Storage uses Marcel to identify uploads from their bytes rather
    // than trusting the multipart Content-Type. Cover its image types used by
    // both avatars and message attachments.
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") { return "image/png"; }
    if bytes.starts_with(b"\xff\xd8\xff") { return "image/jpeg"; }
    if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") { return "image/gif"; }
    if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" { return "image/webp"; }
    if bytes.starts_with(b"II*\0") || bytes.starts_with(b"MM\0*") { return "image/tiff"; }
    if bytes.starts_with(b"BM") { return "image/bmp"; }
    if bytes.starts_with(b"\0\0\x01\0") { return "image/vnd.microsoft.icon"; }
    if bytes.starts_with(b"8BPS") { return "image/vnd.adobe.photoshop"; }
    if bytes.len() >= 12 && &bytes[4..8] == b"ftyp" {
        match &bytes[8..12] {
            b"avif" | b"avis" => return "image/avif",
            b"heic" | b"heix" | b"hevc" | b"hevx" => return "image/heic",
            b"mif1" | b"msf1" => return "image/heif",
            b"qt  " => return "video/quicktime",
            b"isom" | b"iso2" | b"mp41" | b"mp42" | b"M4V " => return "video/mp4",
            _ => {}
        }
    }
    if bytes.len() >= 8 && matches!(&bytes[4..8], b"moov" | b"mdat" | b"free" | b"skip" | b"pnot") {
        return "video/quicktime";
    }
    declared
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
fn room_invitation(
    headers: &HeaderMap,
    join_code: &str,
    logo_version: &str,
    admin: bool,
    csrf_token: &str,
) -> String {
    let invite_url = public_url(headers, &format!("/join/{join_code}"));
    let invite = html_escape::encode_double_quoted_attribute(&invite_url);
    let qr = format!("/qr_code/{}", URL_SAFE.encode(invite_url.as_bytes()));
    let translate = profile_translation_button(
        "Welcome to Rustfire. To invite some people to chat with you, share the join link below.",
        [
            "Bienvenido a Rustfire. Para invitar a algunas personas a chatear contigo, comparte el enlace de unión que se encuentra a continuación.",
            "Bienvenue sur Rustfire. Pour inviter des personnes à discuter avec vous, partagez le lien pour rejoindre ci-dessous.",
            "Rustfire में आपका स्वागत है। अधिक लोगों को चैट के लिए आमंत्रित करने के लिए, नीचे जुड़ने का लिंक साझा करें।",
            "Willkommen bei Rustfire. Um einige Personen zum Chatten einzuladen, teilen Sie den unten stehenden Beitrittslink.",
            "Boas vindas ao Rustfire. Para convidar pessoas para conversarem com você, compartilhe o link de convite abaixo.",
            "Rustfireへようこそ。他の人をチャットに招待するには、下記の参加リンクを共有してください。",
        ],
    );
    let regenerate = if admin {
        format!("<form class='button_to' method='post' action='/account/join_code'><button class='btn btn--regenerate' type='submit'><img aria-hidden='true' class='colorize--black' src='/assets/refresh-249f0509.svg' width='20' height='20'><span class='for-screen-reader'>Regenerate join link</span></button><input type='hidden' name='authenticity_token' value='{}'></form>", esc(csrf_token))
    } else {
        String::new()
    };
    format!(
        "<div id='system_welcome' class='message message--formatted txt-align-center center'><div class='message__body center'><div class='message__body-content position-relative'><figure class='account-logo avatar center margin-block-end txt-large'><img alt='Account logo' src='/account/logo?v={logo_version}' width='300' height='300'></figure><div class='flex align-center gap'><div class='system-welcome--translation'>{translate}</div><p><strong>Welcome to Rustfire</strong><br>To invite people to chat, share the join link below.</p></div><div class='flex flex-column align-center gap'><label class='flex flex-column gap full-width' style='--row-gap: 0.5em'><strong id='invite_label' class='invite-label'>Share to invite more people</strong><span class='flex align-center gap input input--actor fill-white'><img aria-hidden='true' class='colorize--black' src='/assets/person-add-1432b76b.svg' width='20' height='20'><input type='text' class='input' id='invite_url' value='{invite}' aria-labelledby='invite_label' readonly></span></label><div class='flex align-center gap'><a class='btn' data-lightbox-target='image' data-action='lightbox#open' data-lightbox-url-value='{qr}' href='{qr}'><span class='for-screen-reader'>Show join link QR code</span><img aria-hidden='true' class='colorize--black' src='/assets/qr-code-dac3b273.svg' width='20' height='20'></a><button class='btn' data-controller='copy-to-clipboard' data-action='copy-to-clipboard#copy' data-copy-to-clipboard-success-class='btn--success' data-copy-to-clipboard-content-value='{invite}'><span class='for-screen-reader'>Copy join link</span><img aria-hidden='true' class='colorize--black' src='/assets/copy-paste-4c379063.svg' width='20' height='20'></button><button class='btn' hidden='hidden' data-controller='web-share' data-action='web-share#share' data-web-share-url-value='{invite}' data-web-share-text-value='Hit this link to join me in Rustfire and start chatting.' data-web-share-title-value='Link to join Rustfire'><span class='for-screen-reader'>Share join link</span><img aria-hidden='true' class='colorize--black' src='/assets/share-bf28da4f.svg' width='20' height='20'></button>{regenerate}</div></div></div></div></div>"
    )
}
fn room_notifications_html(rid: i64, kind: &str, headers: &HeaderMap) -> String {
    let direct = kind == "Rooms::Direct";
    let user_agent = headers.get(header::USER_AGENT).and_then(|value| value.to_str().ok()).unwrap_or("");
    let help = notification_help::render(user_agent, &esc(&public_url(headers, "/")));
    let frame_id = match kind {
        "Rooms::Direct" => format!("involvement_rooms_direct_{rid}"),
        "Rooms::Closed" => format!("involvement_rooms_closed_{rid}"),
        _ => format!("involvement_rooms_open_{rid}"),
    };
    format!(
        "<span><span class=\"button_to_change_notifying\" data-controller=\"notifications\" data-notifications-subscriptions-url-value=\"/users/me/push_subscriptions\" data-notifications-attention-class=\"btn--pulsing\"><turbo-frame data-controller=\"turbo-frame\" data-action=\"notifications:ready@window-&gt;turbo-frame#load\" data-turbo-frame-url-param=\"/rooms/{rid}/involvement\" id=\"{frame_id}\"><button class=\"btn\" data-action=\"click-&gt;notifications#attemptToSubscribe\" data-notifications-target=\"bell\"><img aria-hidden=\"true\" src=\"/assets/notification-bell-loading-7533ce55.svg\" width=\"20\" height=\"20\" /><img aria-hidden=\"true\" hidden=\"hidden\" src=\"/assets/notification-bell-alert-b467cde7.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">Notification settings for this {}</span></button></turbo-frame><dialog data-notifications-target=\"notAllowedNotice\" class=\"dialog pad center center-block border-radius border shadow\" style=\"--inline-space: var(--block-space)\"><div class=\"flex flex-column txt-align-center\"><span class=\"btn btn--faux center txt-x-large\"><img aria-hidden=\"true\" src=\"/assets/notification-bell-alert-b467cde7.svg\" width=\"48\" height=\"48\" /><span class=\"for-screen-reader\">Notifications alert</span></span><section><h1 class=\"txt-large margin-none\">Notifications aren’t allowed</h1><div class=\"txt-align-start margin-block-start\">{help}</div></section><form method=\"dialog\" class=\"flex align-center gap center\"><button class=\"btn dialog__close\" autofocus=\"true\"><span class=\"for-screen-reader\">Close</span><img aria-hidden=\"true\" src=\"/assets/remove-0e7a045d.svg\" width=\"20\" height=\"20\" /></button></form></div></dialog></span></span>",
        if direct { "Ping" } else { "room" },
    )
}
fn room_composer_footer(rid: i64, user: &User, headers: &HeaderMap) -> String {
    format!(r##"<div class="composer flex align-end gap position-relative" data-controller="typing-notifications" data-typing-notifications-active-class="typing-indicator--active"><a class="btn flex-item-no-shrink margin-block-end composer__context-btn" style="view-transition-name: input-switcher" href="/searches"><img aria-hidden="true" src="/assets/search-5f29565f.svg" width="20" height="20"><span class="for-screen-reader">Search</span></a><turbo-frame id="composer-frame"><form id="composer" class="margin-block flex-item-grow contain" data-controller="composer drop-target" data-action="dragenter-&gt;drop-target#dragenter dragover-&gt;drop-target#dragover drop-&gt;drop-target#drop drop-target:drop@window-&gt;composer#dropFiles trix-file-accept-&gt;composer#preventAttachment refresh-room:online@window-&gt;composer#online typing-notifications#stop paste-&gt;composer#pasteFiles turbo:submit-end-&gt;composer#submitEnd refresh-room:offline@window-&gt;composer#offline" data-composer-messages-outlet="#message-area" data-composer-toolbar-class="composer--rich-text" data-composer-room-id-value="{rid}" action="/rooms/{rid}/messages" accept-charset="UTF-8" method="post"><input type="hidden" name="authenticity_token" value="{}"><fieldset data-composer-target="fields" contents><div class="flex flex-column"><div class="composer__filelist flex flex--align-center gap flex-wrap" data-composer-target="fileList"></div><div class="flex composer__input input input--actor fill-white min-width" style="--input-border-radius: 1.3rem"><div class="flex align-end gap full-width"><img aria-hidden="true" class="composer__input-hint colorize--black" style="view-transition-name: input-btn;" src="/assets/messages-outlined-87ff0331.svg" width="22" height="22"><div class="flex flex-column flex-item-grow min-width gap"><input type="hidden" name="message[body]" id="message_body_trix_input_message"><trix-editor rows="1" class="input" style="order: -1" aria-multiline="true" aria-label="Write a message" data-controller="rich-autocomplete" data-action="trix-change-&gt;typing-notifications#start keydown-&gt;composer#submitByKeyboard trix-focus-&gt;rich-autocomplete#focus trix-change-&gt;rich-autocomplete#search trix-blur-&gt;rich-autocomplete#blur" data-rich-autocomplete-url-value="/autocompletable/users?room_id={rid}" data-permitted-attachment-types="application/vnd.actiontext.opengraph-embed" data-composer-target="text" data-direct-upload-url="{}" data-blob-url-template="{}" id="message_body" input="message_body_trix_input_message"></trix-editor></div><label class="btn btn--borderless txt-small flex-item-no-shrink composer__attachment-btn input--file"><img class="colorize--black" aria-hidden="true" src="/assets/attachment-8bcccab0.svg" width="22" height="22"><input type="file" data-action="composer#filePicked" multiple><span class="for-screen-reader">Attach a file</span></label><button class="btn btn--borderless txt-small flex-item-no-shrink composer__rich-text-btn" type="button" data-action="composer#toggleToolbar"><img class="colorize--black" aria-hidden="true" src="/assets/text-options-055e0d16.svg" width="20" height="20"><span class="for-screen-reader">Rich text</span></button><button name="send" type="submit" data-action="composer#submit" class="btn btn--reversed flex-item-no-shrink txt-small"><img aria-hidden="true" src="/assets/arrow-up-f96b3895.svg" width="20" height="20"><span class="for-screen-reader">Send Message</span></button></div></div></div></fieldset><div class="typing-indicator gap txt-small align-center flex-inline" data-typing-notifications-target="indicator"><div class="typing-indicator__author spinner" data-typing-notifications-target="author"></div></div><input data-composer-target="clientid" type="hidden" name="message[client_message_id]" id="message_client_message_id"></form></turbo-frame></div>"##,
        esc(user.csrf_token.as_deref().unwrap_or("")), esc(&public_url(headers, "/rails/active_storage/direct_uploads")), esc(&public_url(headers, "/rails/active_storage/blobs/redirect/:signed_id/:filename")))
}
async fn room_show_with_target(
    s: Arc<AppState>,
    headers: HeaderMap,
    rid: i64,
    requested_message: Option<i64>,
) -> AppResult {
    let u = match user(&s, &headers) {
        Ok(user) => user,
        Err(StatusCode::UNAUTHORIZED) => return Err(StatusCode::UNAUTHORIZED),
        Err(error) => return Err(error),
    };
    let room = match room_for(&s, u.id, rid) {
        Ok(room) => room,
        Err(StatusCode::NOT_FOUND) => return Ok(found_redirect("/")),
        Err(error) => return Err(error),
    };
    let db = pool(&s)?;
    let (room_updated_at, has_logo): (String, bool) = db
        .query_row("SELECT r.updated_at, EXISTS(SELECT 1 FROM account_logos WHERE id=1) FROM rooms r WHERE r.id=?1", [rid], |row| Ok((row.get(0)?, row.get(1)?)))
        .map_err(db_err)?;
    let refresh_since = message_timestamp_ns(&room_updated_at)
        .map(|nanos| nanos / 1_000_000)
        .unwrap_or_else(|| Utc::now().timestamp_millis());
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
    let invitation: Option<(String, String)> = if db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM rooms WHERE id=?1 AND id=(SELECT id FROM rooms ORDER BY created_at,id LIMIT 1) AND (SELECT COUNT(*) FROM messages WHERE room_id=?1)<=40)",
            [rid],
            |row| row.get::<_, bool>(0),
        )
        .map_err(db_err)?
    {
        db.query_row("SELECT join_code,updated_at FROM accounts LIMIT 1", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .optional()
        .map_err(db_err)?
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
    let messages_target = room_messages_target(&room.kind, rid).ok_or(StatusCode::NOT_FOUND)?;
    let notifications = room_notifications_html(rid, &room.kind, &headers);
    let room_kind = match room.kind.as_str() {
        "Rooms::Open" => "opens",
        "Rooms::Closed" => "closeds",
        "Rooms::Direct" => "directs",
        _ => return Err(StatusCode::NOT_FOUND),
    };
    let logo = if has_logo {
        let updated_at: String = pool(&s)?.query_row("SELECT updated_at FROM accounts WHERE id=1", [], |row| row.get(0)).map_err(db_err)?;
        let version: String = updated_at.chars().filter(char::is_ascii_digit).take(14).collect();
        format!("<figure class=\"account-logo avatar \"><img alt=\"Account logo\" src=\"/account/logo?v={version}\" width=\"300\" height=\"300\"></figure>")
    } else { String::new() };
    let nav = format!("{logo}<span class=\"btn btn--reversed btn--faux room--current\"><h1 class=\"room__contents txt-medium overflow-ellipsis\">{}{}</h1></span><a class=\"btn\" style=\"view-transition-name: edit-room-{rid}\" data-room-id=\"{rid}\" href=\"/rooms/{room_kind}/{rid}/edit\"><img aria-hidden=\"true\" src=\"/assets/menu-dots-horizontal-f6a5d793.svg\" width=\"20\" height=\"20\"><span class=\"for-screen-reader\">Settings for this {}</span></a>{notifications}",
        if room.kind == "Rooms::Direct" { "<span class=\"for-screen-reader\">Ping with</span>" } else { "" },
        esc(&room.name),
        if room.kind == "Rooms::Direct" { "Ping" } else { "room" },
    );
    let template_avatar = avatar_path(s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key), u.id, &u.updated_at)?;
    let mut content = format!(r#"<div id="message-area" class="message-area" contents="true" data-controller="messages presence drop-target" data-action="turbo:before-stream-render@document-&gt;messages#beforeStreamRender keydown.up@document-&gt;messages#editMyLastMessage dragenter-&gt;drop-target#dragenter dragover-&gt;drop-target#dragover drop-&gt;drop-target#drop visibilitychange@document-&gt;presence#visibilityChanged" data-messages-first-of-day-class="message--first-of-day" data-messages-formatted-class="message--formatted" data-messages-me-class="message--me" data-messages-mentioned-class="message--mentioned" data-messages-threaded-class="message--threaded" data-messages-page-url-value="{}"><script type="text/template" data-messages-target="template"><div class="message message--me $messageClasses$" id="message_$clientMessageId$" data-format-message-target="message" data-user-id="{}" data-message-timestamp="$messageTimestamp$" data-messages-target="message"><div class="message__day-separator"><time class="message__timestamp" datetime="$messageDatetime$" data-local-time-target="date"></time></div><figure class="avatar message__avatar"><a title="{}" class="btn avatar" data-turbo-frame="_top" href="/users/{}"><img aria-hidden="true" src="{}" width="48" height="48"></a></figure><div class="message__body"><div class="message__body-content"><div class="message__meta"><h3 class="message__heading"><span class="message__author"><strong>{}</strong></span><span class="message__permalink"><time class="message__timestamp" datetime="$messageDatetime$" data-local-time-target="time"></time></span></h3><div class="message__actions"><div class="position-relative"><span class="btn message__action-btn message__options-btn"><img class="colorize--black" aria-hidden="true" src="/assets/menu-dots-horizontal-f6a5d793.svg"><span class="for-screen-reader">Message options</span></span></div></div></div>$body$</div></div></div></script><div id="{}" class="messages" data-controller="maintain-scroll refresh-room" data-action="turbo:before-stream-render@document-&gt;maintain-scroll#beforeStreamRender visibilitychange@document-&gt;refresh-room#visibilityChanged online@window-&gt;refresh-room#online" data-messages-target="messages" data-refresh-room-loaded-at-value="{refresh_since}" data-refresh-room-url-value="{}">"#,
        esc(&public_url(&headers, &format!("/rooms/{rid}/messages"))),
        u.id, esc(&u.name), u.id, esc(&template_avatar), esc(&u.name),
        messages_target, esc(&public_url(&headers, &format!("/rooms/{rid}/refresh"))),
    );
    let stream_key = s
        .imported_turbo_stream_signing_key
        .as_deref()
        .unwrap_or(&s.turbo_stream_signing_key);
    let stream_token = room_stream_token(stream_key, &room.kind, rid).map_err(db_err)?;
    if let Some((join_code, updated_at)) = invitation {
        let version: String = updated_at
            .chars()
            .filter(char::is_ascii_digit)
            .take(14)
            .collect();
        content.push_str(&room_invitation(
            &headers,
            &join_code,
            &version,
            is_admin(&u),
            u.csrf_token.as_deref().unwrap_or(""),
        ));
    }
    for m in messages {
        content.push_str(&message_html(&s, &m, Some(&headers)));
    }
    content.push_str(&format!("</div><turbo-cable-stream-source channel=\"RoomMessagesChannel\" signed-stream-name=\"{}\"></turbo-cable-stream-source><button class=\"message-area__return-to-latest btn\" data-action=\"messages#returnToLatest\" data-messages-target=\"latest\" hidden=\"hidden\"><img aria-hidden=\"true\" src=\"/assets/arrow-down-3f174d76.svg\" width=\"20\" height=\"20\"><span class=\"for-screen-reader\">Jump to newest message</span></button></div>", esc(&stream_token)));
    let footer = room_composer_footer(rid, &u, &headers);
    let sidebar_frame = "<turbo-frame data-turbo-permanent=\"true\" data-controller=\"rooms-list read-rooms turbo-frame\" data-rooms-list-unread-class=\"unread\" data-action=\"presence:present@window-&gt;rooms-list#read read-rooms:read-&gt;rooms-list#read turbo:frame-load-&gt;rooms-list#loaded refresh-room:visible@window-&gt;turbo-frame#reload\" id=\"user_sidebar\" src=\"/users/me/sidebar\" target=\"_top\"></turbo-frame>";
    let head = format!("<meta name=\"turbo-cache-control\" content=\"no-preview\"><meta name=\"current-room-id\" content=\"{rid}\"><script defer src=\"/static/trix.js\"></script>");
    let mut response = render_source_page_sections(&room.name, &content, &nav, &footer, sidebar_frame, "sidebar", if has_logo { "account-has-logo" } else { "" }, &head, Some(&u), u.csrf_token.as_deref().unwrap_or(""));
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
fn not_acceptable_format(accept: &str) -> Response {
    if accept.contains("application/json") {
        (
            StatusCode::NOT_ACCEPTABLE,
            [(header::CONTENT_TYPE, "application/json; charset=UTF-8")],
            r#"{"status":406,"error":"Not Acceptable"}"#,
        )
            .into_response()
    } else {
        (
            StatusCode::NOT_ACCEPTABLE,
            [(header::CONTENT_TYPE, "text/html; charset=UTF-8")],
        )
            .into_response()
    }
}
#[derive(Deserialize)]
struct RefreshQuery {
    since: Option<String>,
}
async fn room_refresh(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    Path(rid): Path<i64>,
    Query(q): Query<RefreshQuery>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    let accept = headers
        .get(header::ACCEPT)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if !uri.path().ends_with(".turbo_stream")
        && !accept.contains("text/vnd.turbo-stream.html")
        && !accept.contains("*/*")
    {
        return Ok(not_acceptable_format(accept));
    }
    let since = q.since.as_deref().and_then(|value| value.parse::<i64>().ok()).unwrap_or(0);
    let cutoff_ns = chrono::DateTime::<Utc>::from_timestamp_millis(since)
        .ok_or(StatusCode::BAD_REQUEST)?
        .timestamp_nanos_opt()
        .ok_or(StatusCode::BAD_REQUEST)?;
    let new_messages = messages_since(&s, rid, cutoff_ns, false)?;
    let updated_messages = messages_since(&s, rid, cutoff_ns, true)?;
    let mut html = String::new();
    if !new_messages.is_empty() {
        let entries: String = new_messages
            .iter()
            .map(|message| message_html(&s, message, Some(&headers)))
            .collect();
        let target = room_messages_target(&room.kind, rid).ok_or(StatusCode::NOT_FOUND)?;
        html.push_str(&format!("<turbo-stream action='append' target='{target}'><template>{entries}</template></turbo-stream>"));
    }
    for message in &updated_messages {
        html.push_str(&format!("<turbo-stream action='replace' target='message_{}'><template>{}</template></turbo-stream>", esc(&message.client_message_id), message_html(&s, message, Some(&headers))));
    }
    if html.is_empty() {
        html.push('\n');
    }
    Ok((
        [(header::CONTENT_TYPE, "text/vnd.turbo-stream.html; charset=utf-8")],
        csrf_forms(&html, u.csrf_token.as_deref().unwrap_or("")),
    )
        .into_response())
}
#[derive(Deserialize)]
struct RefreshStateQuery {
    after: Option<i64>,
}
async fn room_refresh_state(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    Query(q): Query<RefreshStateQuery>,
) -> AppResult {
    let u = user(&s, &headers)?;
    room_for(&s, u.id, rid)?;
    let after = q.after.unwrap_or(0).max(0);
    let mut messages = messages_after_position(&s, rid, after, 101)?;
    let has_more = messages.len() > 100;
    messages.truncate(100);
    let next_after = messages.last().map(|message| message.id).unwrap_or(after);
    let entries: Vec<Value> = messages
        .iter()
        .map(|message| json!({"id":message.id,"html":message_html(&s, message, Some(&headers))}))
        .collect();
    Ok(Json(json!({"messages":entries,"next_after":next_after,"has_more":has_more})).into_response())
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
    let frame = involvement_frame_html(
        rid,
        &room.kind,
        &current,
        u.csrf_token.as_deref().unwrap_or(""),
    );
    if headers.contains_key("turbo-frame") {
        let token = esc(u.csrf_token.as_deref().unwrap_or(""));
        return Ok(Html(format!(
            "<html><head><meta name=\"csrf-param\" content=\"authenticity_token\"><meta name=\"csrf-token\" content=\"{token}\"></head><body>{frame}</body></html>"
        ))
        .into_response());
    }
    Ok(render("Notifications", &frame, Some(&u)))
}

fn involvement_frame_html(rid: i64, kind: &str, current: &str, csrf_token: &str) -> String {
    let direct = kind == "Rooms::Direct";
    let order: &[&str] = if direct {
        &["everything", "nothing"]
    } else {
        &["mentions", "everything", "nothing", "invisible"]
    };
    let position = order
        .iter()
        .position(|level| *level == current)
        .unwrap_or(0);
    let next = order[(position + 1) % order.len()];
    let label = match current {
        "mentions" => "Notifying about @ mentions",
        "everything" => "Notifying about all messages",
        "nothing" => "Notifications are off",
        "invisible" => "Notifications are off and room invisible in sidebar",
        _ => "Notifying about @ mentions",
    };
    let icon = match current {
        "mentions" => "notification-bell-mentions-945d1b91.svg",
        "everything" => "notification-bell-everything-cde41b14.svg",
        "nothing" => "notification-bell-nothing-d8096c76.svg",
        "invisible" => "notification-bell-invisible-8b495073.svg",
        _ => "notification-bell-mentions-945d1b91.svg",
    };
    let room_id = match kind {
        "Rooms::Direct" => format!("rooms_direct_{rid}"),
        "Rooms::Closed" => format!("rooms_closed_{rid}"),
        _ => format!("rooms_open_{rid}"),
    };
    let frame_id = format!("involvement_{room_id}");
    let label_id = format!("involvement_label_{room_id}");
    format!(
        "<turbo-frame data-controller=\"turbo-frame\" data-action=\"notifications:ready@window-&gt;turbo-frame#load\" data-turbo-frame-url-param=\"/rooms/{rid}/involvement\" id=\"{frame_id}\">\n  <form class=\"button_to\" method=\"post\" action=\"/rooms/{rid}/involvement?involvement={next}\"><input type=\"hidden\" name=\"_method\" value=\"put\" /><button role=\"checkbox\" aria-checked=\"true\" aria-labelledby=\"{label_id}\" tabindex=\"0\" class=\"btn {current}\" type=\"submit\"><img aria-hidden=\"true\" src=\"/assets/{icon}\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\" id=\"{label_id}\">{label}</span></button><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\" /></form>\n</turbo-frame>",
        esc(csrf_token)
    )
}
async fn push_subscriptions_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let mut query=db.prepare("SELECT id,endpoint,COALESCE(user_agent,'') FROM push_subscriptions WHERE user_id=?1 ORDER BY id").map_err(db_err)?;
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
    drop(query);
    drop(db);
    let mut list = String::new();
    let token = u.csrf_token.as_deref().unwrap_or("");
    for (id, endpoint, agent) in rows {
        let label = push_subscription_agent_label(&agent);
        list.push_str(&format!("<li class=\"flex flex-column margin-none membership-item\"><span class=\"overflow-ellipsis txt-primary txt-undecorated\"><strong>{}</strong><br></span><span class=\"flex align-start gap txt-small\"><span>{}</span><span class=\"flex align-center gap\"><form class=\"button_to\" method=\"post\" action=\"/users/me/push_subscriptions/{id}/test_notifications\"><button class=\"btn btn--reversed\" type=\"submit\"><img aria-hidden=\"true\" src=\"/assets/notification-bell-everything-cde41b14.svg\" width=\"20\" height=\"20\"><span class=\"for-screen-reader\">Send test notification</span></button><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\"></form><form class=\"button_to\" method=\"post\" action=\"/users/me/push_subscriptions/{id}\"><input type=\"hidden\" name=\"_method\" value=\"delete\"><button class=\"btn btn--negative\" type=\"submit\"><img aria-hidden=\"true\" src=\"/assets/minus-b31a1093.svg\" width=\"20\" height=\"20\"><span class=\"for-screen-reader\">Delete subscription</span></button><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\"></form></span></span></li>",esc(&label),esc(&endpoint),esc(token),esc(token)));
    }
    let back_room = cookie(&headers, "last_room")
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|id| room_for(&s, u.id, *id).is_ok())
        .or_else(|| rooms_for(&s, u.id).ok()?.first().map(|room| room.id));
    let back_href = back_room.map_or("/".to_string(), |id| format!("/rooms/{id}"));
    let nav = format!("<div class=\"flex-item-justify-start\"><a class=\"btn\" href=\"{back_href}\"><img aria-hidden=\"true\" src=\"/assets/arrow-left-abe40556.svg\" width=\"20\" height=\"20\"><span class=\"for-screen-reader\">Go Back</span></a></div>");
    let body = format!("<section class=\"panel panel--wide flex flex-column gap\"><h1 class=\"txt-align-center txt-large margin-none\">Push Notification Subscriptions</h1><div class=\"pad-inline fill-shade border-radius\" id=\"push_subscriptions\"><menu class=\"pad flex flex-column gap\">{list}</menu></div></section>");
    Ok(render_source_page_sections("Push notification subscriptions", &body, &nav, "", "", "", "", "", Some(&u), token))
}
fn push_subscription_agent_label(agent: &str) -> String {
    let agent = if agent.trim().is_empty() { "Mozilla/4.0 (compatible)" } else { agent };
    let comment = agent.split_once('(').and_then(|(_, rest)| rest.split_once(')')).map(|(value, _)| value).unwrap_or("");
    let parts: Vec<&str> = comment.split(';').map(str::trim).collect();
    let first = parts.first().copied().unwrap_or("");
    let platform = if first.starts_with("Windows") { "Windows" } else if parts.iter().any(|part| part.starts_with("CrOS")) { "ChromeOS" } else if parts.iter().any(|part| part.starts_with("Android")) { "Android" } else if first == "compatible" || first == "Mobile" { "" } else { first };
    let version_of = |marker: &str| agent.split_once(marker).map(|(_, tail)| tail.split(|ch: char| ch.is_whitespace() || ch == ')').next().unwrap_or(""));
    let (browser, version) = if let Some(version) = version_of("Edge/") {
        ("Edge", version)
    } else if let Some(version) = version_of("CriOS/").or_else(|| version_of("Chrome/")) {
        ("Chrome", version)
    } else if let Some(version) = version_of("Firefox/") {
        ("Firefox", version)
    } else if agent.contains("AppleWebKit/") {
        (if platform == "Android" { "Android" } else { "Safari" }, version_of("Version/").unwrap_or(""))
    } else if let Some(version) = version_of("MSIE ") {
        ("Internet Explorer", version.trim_end_matches(';'))
    } else {
        let product = agent.split(|ch: char| ch == '/' || ch.is_whitespace()).next().unwrap_or("Mozilla");
        (product, version_of(&format!("{product}/")).unwrap_or(""))
    };
    format!("{browser} {version} on {platform}")
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
    let existing: Option<i64> = db.query_row(
        "SELECT id FROM push_subscriptions WHERE user_id=?1 AND endpoint=?2 AND p256dh_key=?3 AND auth_key=?4 ORDER BY id LIMIT 1",
        params![u.id, value.endpoint, value.p256dh_key, value.auth_key],
        |row| row.get(0),
    ).optional().map_err(db_err)?;
    if let Some(id) = existing {
        db.execute("UPDATE push_subscriptions SET updated_at=?1 WHERE id=?2", params![t, id]).map_err(db_err)?;
    } else {
        db.execute("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at) VALUES(?1,?2,?3,?4,?5,?6,?6)", params![u.id, value.endpoint, value.p256dh_key, value.auth_key, agent, t]).map_err(db_err)?;
    }
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
    Ok(found_redirect("/users/me/push_subscriptions"))
}
async fn push_subscriptions_delete_post(
    state: State<Arc<AppState>>,
    headers: HeaderMap,
    path: Path<i64>,
    RawForm(raw): RawForm,
) -> AppResult {
    if fields(&raw).0.get("_method").map(String::as_str) != Some("delete") {
        return Err(StatusCode::METHOD_NOT_ALLOWED);
    }
    push_subscriptions_delete(state, headers, path).await
}
async fn push_subscriptions_delete_scoped(
    state: State<Arc<AppState>>,
    headers: HeaderMap,
    Path((_, id)): Path<(String, i64)>,
) -> AppResult {
    push_subscriptions_delete(state, headers, Path(id)).await
}
async fn push_subscriptions_delete_post_scoped(
    state: State<Arc<AppState>>,
    headers: HeaderMap,
    Path((_, id)): Path<(String, i64)>,
    raw: RawForm,
) -> AppResult {
    push_subscriptions_delete_post(state, headers, Path(id), raw).await
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
        let payload=push_test_payload(&headers,&Uuid::new_v4().to_string(),badge).to_string();
        deliver_push(s, subscription, payload)
            .await
            .map_err(|error| {
                eprintln!("Rustfire test push error: {error}");
                StatusCode::BAD_GATEWAY
            })?;
    }
    Ok(found_redirect("/users/me/push_subscriptions"))
}
fn push_test_payload(headers: &HeaderMap, body: &str, badge: i64) -> Value {
    json!({"title":"Campfire Test","options":{"body":body,"icon":"/account/logo","data":{"path":public_url(headers,"/users/me/push_subscriptions"),"badge":badge}}})
}
async fn push_test_notification_scoped(
    state: State<Arc<AppState>>,
    headers: HeaderMap,
    Path((_, id)): Path<(String, i64)>,
) -> AppResult {
    push_test_notification(state, headers, Path(id)).await
}
async fn involvement_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    Query(query): Query<HashMap<String, String>>,
    RawForm(raw): RawForm,
) -> AppResult {
    let u = user(&s, &headers)?;
    let room = room_for(&s, u.id, rid)?;
    let submitted = fields(&raw).0;
    let involvement = submitted
        .get("involvement")
        .or_else(|| query.get("involvement"))
        .ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    if !["everything", "mentions", "nothing", "invisible"].contains(&involvement.as_str()) {
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
        params![involvement, rid, u.id],
    )
    .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    if room.kind != "Rooms::Direct" && (previous == "invisible") != (involvement == "invisible") {
        notify_room_lists(&s, [u.id]);
        broadcast_user_room_visibility(&s, u.id, &room, involvement != "invisible");
    }
    Ok(found_redirect(&public_url(
        &headers,
        &format!("/rooms/{rid}/involvement"),
    )))
}
async fn involvement_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    Query(query): Query<HashMap<String, String>>,
    RawForm(raw): RawForm,
) -> AppResult {
    let form_method = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .filter(|value| value.starts_with("application/x-www-form-urlencoded"))
        .and_then(|_| fields(&raw).0.remove("_method"));
    let method = form_method.or_else(|| {
        headers
            .get("x-http-method-override")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    });
    match method.as_deref().map(str::to_ascii_uppercase).as_deref() {
        Some("PATCH" | "PUT") => {
            involvement_post(State(s), headers, Path(rid), Query(query), RawForm(raw)).await
        }
        _ => Err(StatusCode::NOT_FOUND),
    }
}
#[derive(Deserialize)]
struct Paging {
    before: Option<i64>,
    after: Option<i64>,
}
fn message_page_metadata(
    s: &AppState,
    rid: i64,
    before: Option<i64>,
    after: Option<i64>,
) -> Result<Vec<(i64, String, String)>, StatusCode> {
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
    let sql = format!("SELECT m.id,m.created_at,m.updated_at FROM messages m INDEXED BY idx_messages_room_created_ns WHERE m.room_id=?1 AND {predicate} ORDER BY m.created_at_ns {order},m.id {order} LIMIT ?3");
    let mut query = db.prepare(&sql).map_err(db_err)?;
    let mut rows = query
        .query_map(params![rid, cursor_time, 40], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    if after.is_none() {
        rows.reverse();
    }
    Ok(rows)
}
fn message_page_validator<'a>(
    messages: impl IntoIterator<Item = (i64, &'a str, &'a str)>,
    rid: i64,
    before: Option<i64>,
    after: Option<i64>,
    json: bool,
) -> Result<(String, String, i64), StatusCode> {
    let mut key = format!("room={rid};before={before:?};after={after:?};json={json};");
    let mut latest = i64::MIN;
    for (id, created_at, updated_at) in messages {
        latest = latest.max(
            message_timestamp_ns(updated_at).ok_or(StatusCode::INTERNAL_SERVER_ERROR)?,
        );
        key.push_str(&format!("{id}:{created_at}:{updated_at};"));
    }
    let digest = openssl::hash::hash(MessageDigest::md5(), key.as_bytes()).map_err(db_err)?;
    let etag = format!(
        "W/\"{}\"",
        digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>()
    );
    let seconds = latest.div_euclid(1_000_000_000);
    let modified = httpdate::fmt_http_date(
        UNIX_EPOCH + StdDuration::from_secs(seconds.max(0) as u64),
    );
    Ok((etag, modified, seconds))
}
fn message_page_cache_headers(response: &mut Response, etag: &str, modified: &str) {
    response.headers_mut().insert(header::ETAG, etag.parse().unwrap());
    response
        .headers_mut()
        .insert(header::LAST_MODIFIED, modified.parse().unwrap());
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        "max-age=0, private, must-revalidate".parse().unwrap(),
    );
}
fn message_page_fresh(headers: &HeaderMap, etag: &str, modified_seconds: i64) -> bool {
    if let Some(value) = headers.get(header::IF_NONE_MATCH).and_then(|value| value.to_str().ok()) {
        value.split(',').any(|candidate| {
            let candidate = candidate.trim();
            candidate == "*" || candidate.trim_start_matches("W/") == etag.trim_start_matches("W/")
        })
    } else {
        headers
            .get(header::IF_MODIFIED_SINCE)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| httpdate::parse_http_date(value).ok())
            .and_then(|date| date.duration_since(UNIX_EPOCH).ok())
            .is_some_and(|date| date.as_secs() as i64 >= modified_seconds)
    }
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
    // Matching validators need only the selected page's IDs and timestamps.
    if headers.contains_key(header::IF_NONE_MATCH)
        || headers.contains_key(header::IF_MODIFIED_SINCE)
    {
        let metadata = message_page_metadata(&s, rid, q.before, q.after)?;
        if metadata.is_empty() {
            return Ok(StatusCode::NO_CONTENT.into_response());
        }
        let (etag, modified, modified_seconds) = message_page_validator(
            metadata
                .iter()
                .map(|(id, created, updated)| (*id, created.as_str(), updated.as_str())),
            rid,
            q.before,
            q.after,
            wants_json,
        )?;
        if message_page_fresh(&headers, &etag, modified_seconds) {
            let mut response = StatusCode::NOT_MODIFIED.into_response();
            message_page_cache_headers(&mut response, &etag, &modified);
            return Ok(response);
        }
    }
    let messages = message_list_with_room_name(&s, rid, 40, q.before, q.after, !wants_json)?;
    if messages.is_empty() {
        return Ok(StatusCode::NO_CONTENT.into_response());
    }
    let (etag, modified, _) = message_page_validator(
        messages.iter().map(|message| {
            (message.id, message.created_at.as_str(), message.updated_at.as_str())
        }),
        rid,
        q.before,
        q.after,
        wants_json,
    )?;
    let mut response = if wants_json {
        Json(
            messages
                .iter()
                .map(|m| message_json(&s, m, Some(&headers)))
                .collect::<Result<Vec<_>, _>>()?,
        )
        .into_response()
    } else {
        let html = messages
            .iter()
            .map(|m| message_html(&s, m, Some(&headers)))
            .collect::<String>();
        Html(csrf_forms(&html, u.csrf_token.as_deref().unwrap_or(""))).into_response()
    };
    message_page_cache_headers(&mut response, &etag, &modified);
    Ok(response)
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
        if m.attachment.is_some() {
            String::new()
        } else {
            "<div class=\"trix-content\">\n  \n</div>\n".to_string()
        }
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
fn bot_message_json(
    s: &AppState,
    m: &ChatMessage,
    headers: &HeaderMap,
    db: &rusqlite::Connection,
) -> Result<Value, StatusCode> {
    let mut message = message_json(s, m, Some(headers))?;
    let Some(display) = m.body_html.as_deref().filter(|html| html.contains("class=\"mention\"")) else {
        return Ok(message);
    };
    let source: Option<String> = db
        .query_row("SELECT body_source FROM messages WHERE id=?1", [m.id], |row| row.get(0))
        .map_err(db_err)?;
    let Some(source) = source else {
        return Ok(message);
    };
    let normalized = action_text_webhook_html(&source);
    let fragment = ParsedHtml::parse_fragment(&normalized);
    let selector = Selector::parse("action-text-attachment[sgid]").unwrap();
    let mut attachments = Vec::new();
    for attachment in fragment.select(&selector) {
        if attachment.value().attr("content-type") != Some("application/vnd.campfire.mention") {
            continue;
        }
        let Some(sgid) = attachment.value().attr("sgid") else { continue };
        let Some(id) = mention_user_id_from_sgid(
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
            sgid,
        ) else { continue };
        let user: Option<(String, String, String)> = db
            .query_row("SELECT name,COALESCE(bio,''),updated_at FROM users WHERE id=?1", [id], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .optional()
            .map_err(db_err)?;
        let Some((name, bio, updated_at)) = user else { continue };
        let title = if bio.trim().is_empty() { name.clone() } else { format!("{name} – {bio}") };
        let title = html_escape::encode_double_quoted_attribute(&title);
        let name = html_escape::encode_double_quoted_attribute(&name);
        let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
        let avatar = avatar_path(avatar_key, id, &updated_at)?;
        let mention_key = s.imported_mention_signing_key.as_deref().unwrap_or(&s.mention_signing_key);
        let rendered_sgid = mention_sgid(mention_key, id).map_err(db_err)?;
        let content = attachment.value().attr("content").map(|value| {
            format!(" content=\"{}\"", value.replace('&', "&amp;").replace('"', "&quot;"))
        }).unwrap_or_default();
        let wrapped = format!(
            "<action-text-attachment sgid=\"{}\" content-type=\"application/octet-stream\"{content}><div class=\"mention\" sgid=\"{}\">\n  <a title=\"{}\" class=\"btn avatar\" href=\"/users/{id}\"><img src=\"{}\" width=\"48\" height=\"48\"></a>\n  {}\n</div></action-text-attachment>",
            esc(sgid), esc(&rendered_sgid), title, avatar, name,
        );
        attachments.push((id, wrapped));
    }
    if attachments.is_empty() {
        return Ok(message);
    }
    static MENTION: OnceLock<Regex> = OnceLock::new();
    let pattern = MENTION.get_or_init(|| Regex::new(r#"(?s)<div class="mention">.*?</div>"#).unwrap());
    let found = pattern.find_iter(display).collect::<Vec<_>>();
    if found.len() != attachments.len() || found.iter().zip(&attachments).any(|(html, (id, _))| {
        !html.as_str().contains(&format!("href=\"/users/{id}\""))
    }) {
        return Ok(message);
    }
    let mut content = String::with_capacity(display.len());
    let mut consumed = 0;
    for (html, (_, wrapped)) in found.iter().zip(&attachments) {
        content.push_str(&display[consumed..html.start()]);
        content.push_str(wrapped);
        consumed = html.end();
    }
    content.push_str(&display[consumed..]);
    message["body"]["html"] = Value::String(format!(
        "<div class=\"trix-content\">\n  {content}\n</div>\n"
    ));
    Ok(message)
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
    mut upload: Option<Upload>,
    rich: bool,
    request_headers: Option<&HeaderMap>,
    allow_blank: bool,
) -> Result<ChatMessage, StatusCode> {
    if let Some(file) = upload.as_mut() {
        file.content_type = sniff_upload_content_type(&file.bytes, &file.content_type).to_string();
    }
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
        let cleaned = strip_disallowed_rich_tags(body);
        let (trusted, plain_source) = replace_mention_attachments(
            cleaned.as_deref().unwrap_or(body),
            &db,
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
            s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key),
        )?;
        let (mut plain, html) = rich_body_trusted(&trusted, request_host);
        if plain_source != trusted {
            plain = rich_body_trusted(&plain_source, request_host).0;
        }
        if cleaned.is_some() {
            let (_, original_plain) = replace_mention_attachments(
                body,
                &db,
                &s.mention_signing_key,
                s.imported_mention_signing_key.as_deref(),
                s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key),
            )?;
            plain = action_text_plain_body(&original_plain);
        }
        (plain, Some(html))
    } else {
        (body.trim().to_string(), None)
    };
    if !allow_blank
        && plain.is_empty()
        && upload.is_none()
        && !has_preview_card(body_html.as_deref())
    {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    };
    let created = Utc::now();
    let t = created.to_rfc3339();
    let created_at_ns = created
        .timestamp_nanos_opt()
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    let cid = client_id.unwrap_or_else(|| Uuid::new_v4().to_string());
    db.execute("INSERT INTO messages(id,room_id,creator_id,body,body_html,body_source,client_message_id,created_at,created_at_ns,updated_at,updated_at_ns) VALUES((SELECT MAX(last_id,(SELECT COALESCE(MAX(id),0) FROM messages))+1 FROM id_sequences WHERE name='messages'),?1,?2,?3,?4,?5,?6,?7,?8,?7,?8)",params![rid,u.id,plain,body_html,if rich && !body.trim().is_empty() {Some(body)} else {None},cid,t,created_at_ns]).map_err(db_err)?;
    let id = db.last_insert_rowid();
    touch_room(&db, rid)?;
    let mut valid_mentions = Vec::new();
    for mentioned_id in candidate_mentions {
        let allowed: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM memberships WHERE room_id=?1 AND user_id=?2)",params![rid,mentioned_id],|r|r.get(0)).map_err(db_err)?;
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
    db.execute("UPDATE memberships SET unread_at=?1 WHERE room_id=?2 AND user_id!=?3 AND involvement!='invisible' AND (connected_at IS NULL OR connected_at<?4)", params![t, rid, u.id, cutoff]).map_err(db_err)?;
    let mut attachment_processing_failed = false;
    let attachment = if let Some(file) = upload {
        let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
        std::fs::create_dir_all(&dir).map_err(db_err)?;
        let stored = Uuid::new_v4().to_string();
        let input = std::path::Path::new(&dir).join(&stored);
        std::fs::write(&input, file.bytes).map_err(db_err)?;
        let (width, height) = if let Some(format) = image_format(&file.content_type) {
            let (width, height) = analyze_image_and_thumbnail(&input, &stored, "thumb", format);
            attachment_processing_failed = width.is_none()
                || height.is_none()
                || !std::path::Path::new(&dir)
                    .join("variants")
                    .join(format!("{stored}-thumb.{format}"))
                    .is_file();
            (
                width.map(|value| value as f64),
                height.map(|value| value as f64),
            )
        } else if safe_inline_video(&file.content_type) {
            let dimensions = analyze_video_and_poster(&input, &stored);
            attachment_processing_failed = dimensions.0.is_none()
                || dimensions.1.is_none()
                || !std::path::Path::new(&dir)
                    .join("variants")
                    .join(format!("{stored}-poster.webp"))
                    .is_file();
            dimensions
        } else {
            (None, None)
        };
        db.execute("INSERT INTO attachments(message_id,filename,content_type,stored_name,created_at,width,height) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![id,file.filename,file.content_type,stored,t,width,height]).map_err(db_err)?;
        Some(Attachment {
            id: db.last_insert_rowid(),
            filename: file.filename,
            content_type: file.content_type,
            width,
            height,
        })
    } else {
        None
    };
    if attachment_processing_failed {
        return Err(StatusCode::INTERNAL_SERVER_ERROR);
    }
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
    let mut members_stmt = db.prepare("SELECT user_id FROM memberships WHERE room_id=?1").map_err(db_err)?;
    let members = members_stmt
        .query_map([rid], |r| r.get::<_, i64>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(members_stmt);
    for uid in members {
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
fn push_recipients(
    db: &rusqlite::Connection,
    room_id: i64,
    creator_id: i64,
    cutoff: &str,
    mention_ids: &[i64],
) -> Result<Vec<(PushSubscription, i64)>, StatusCode> {
    let mut query=db.prepare("SELECT p.id,p.endpoint,p.p256dh_key,p.auth_key,m.user_id,(SELECT count(*) FROM memberships um WHERE um.user_id=m.user_id AND um.unread_at IS NOT NULL),m.involvement FROM push_subscriptions p JOIN memberships m ON m.user_id=p.user_id WHERE m.room_id=?1 AND m.user_id!=?2 AND (m.connected_at IS NULL OR m.connected_at<?3) AND m.involvement IN ('everything','mentions') ORDER BY CASE m.involvement WHEN 'everything' THEN 0 ELSE 1 END,p.id").map_err(db_err)?;
    let rows = query
        .query_map(params![room_id, creator_id, cutoff], |r| {
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
        .map_err(db_err)?;
    let mut recipients = Vec::new();
    for row in rows {
        let (subscription, user_id, badge, involvement) = row.map_err(db_err)?;
        if involvement == "everything" || mention_ids.contains(&user_id) {
            recipients.push((subscription, badge));
        }
    }
    Ok(recipients)
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
    let rows = push_recipients(&db, message.room_id, message.creator_id, &cutoff, &message.mention_ids)?;
    drop(db);
    let direct = message.room_kind.as_deref() == Some("Rooms::Direct");
    let title = if direct {
        message.creator_name.clone()
    } else {
        room_name
    };
    let body = push_message_body(message, direct);
    for (subscription, badge) in rows {
        let Ok(queue_permit) = s.push_queue_slots.clone().try_acquire_owned() else {
            break;
        };
        let state = s.clone();
        let payload=json!({"title":title,"options":{"body":body,"icon":"/account/logo","data":{"path":format!("/rooms/{}",message.room_id),"badge":badge}}}).to_string();
        tokio::spawn(async move {
            let _queue_permit = queue_permit;
            let Ok(_worker_permit) = state.push_slots.clone().acquire_owned().await else {
                return;
            };
            if let Err(error) = deliver_push(state, subscription, payload).await {
                eprintln!("Rustfire push delivery error: {error}");
            }
        });
    }
    Ok(())
}
fn push_message_body(message: &ChatMessage, direct: bool) -> String {
    let plain = if message.body.trim().is_empty() {
        message.attachment.as_ref().map(|file| file.filename.as_str()).unwrap_or("")
    } else {
        message.body.as_str()
    };
    if direct {
        plain.to_string()
    } else {
        format!("{}: {plain}", message.creator_name)
    }
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
    let mut db = pool(s)?;
    let mut q = db.prepare("SELECT u.id FROM memberships m JOIN users u ON u.id=m.user_id JOIN webhooks w ON w.user_id=u.id WHERE m.room_id=?1 AND u.role=2 AND u.status=0 AND u.bot_token IS NOT NULL").map_err(db_err)?;
    let bots = q
        .query_map([message.room_id], |r| r.get::<_, i64>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(q);
    let tx = db.transaction().map_err(db_err)?;
    for id in bots {
        if id == message.creator_id || (!direct && !message.mention_ids.contains(&id)) {
            continue;
        }
        tx.execute("INSERT INTO webhook_jobs(bot_id,message_id,created_at) VALUES(?1,?2,?3)", params![id,message.id,now()]).map_err(db_err)?;
    }
    tx.commit().map_err(db_err)?;
    Ok(())
}
fn action_text_webhook_html(input: &str) -> String {
    if !input.contains("data-trix-attachment") {
        return input.to_string();
    }
    static TRIX_FIGURE: OnceLock<Regex> = OnceLock::new();
    let pattern = TRIX_FIGURE
        .get_or_init(|| Regex::new(r"(?is)<figure\b[^>]*>.*?</figure>").unwrap());
    let selector = Selector::parse("figure[data-trix-attachment]").unwrap();
    pattern
        .replace_all(input, |capture: &regex::Captures<'_>| {
            let fragment = ParsedHtml::parse_fragment(&capture[0]);
            let Some(figure) = fragment.select(&selector).next() else {
                return capture[0].to_string();
            };
            let Some(data) = figure.value().attr("data-trix-attachment") else {
                return capture[0].to_string();
            };
            let Ok(value) = serde_json::from_str::<Value>(data) else {
                return capture[0].to_string();
            };
            if !value.is_object() {
                return capture[0].to_string();
            }
            let composed = figure
                .value()
                .attr("data-trix-attributes")
                .and_then(|data| serde_json::from_str::<Value>(data).ok());
            let attributes = [
                ("sgid", "sgid"),
                ("contentType", "content-type"),
                ("url", "url"),
                ("href", "href"),
                ("filename", "filename"),
                ("filesize", "filesize"),
                ("width", "width"),
                ("height", "height"),
                ("previewable", "previewable"),
                ("presentation", "presentation"),
                ("caption", "caption"),
                ("content", "content"),
            ]
            .into_iter()
            .filter_map(|(key, attribute)| {
                let value = if key == "presentation" || key == "caption" {
                    composed.as_ref().and_then(|composed| composed.get(key))
                } else {
                    value.get(key)
                }?;
                let text = value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string());
                let escaped = if attribute == "content" {
                    text.replace('&', "&amp;").replace('"', "&quot;")
                } else {
                    html_escape::encode_double_quoted_attribute(&text).into_owned()
                };
                Some(format!(
                    " {attribute}=\"{}\"",
                    escaped
                ))
            })
            .collect::<String>();
            if attributes.is_empty() {
                capture[0].to_string()
            } else {
                format!("<action-text-attachment{attributes}></action-text-attachment>")
            }
        })
        .into_owned()
}
async fn deliver_webhook(
    s: Arc<AppState>,
    bot_id: i64,
    bot_name: String,
    key: String,
    url: String,
    room_name: Option<String>,
    source_html: Option<String>,
    message: ChatMessage,
) -> Result<(), String> {
    let plain = message
        .attachment
        .as_ref()
        .filter(|_| message.body.is_empty())
        .map(|attachment| attachment.filename.as_str())
        .unwrap_or(&message.body)
        .replace(&format!("@{bot_name}"), "")
        .trim()
        .to_string();
    let payload = json!({
        "user":{"id":message.creator_id,"name":message.creator_name},
        "room":{"id":message.room_id,"name":room_name,"path":format!("/rooms/{}/{key}/messages",message.room_id)},
        "message":{"id":message.id,"body":{"html":source_html,"plain":plain},"path":format!("/rooms/{}/@{}",message.room_id,message.id)}
    });
    let reply = match s.webhook_client.post(&url).json(&payload).send().await {
        Ok(response) => response,
        Err(error) => {
            if error.is_timeout() {
                let bot = webhook_bot_user(&s, bot_id).map_err(|status| format!("Timeout reply bot lookup failed: {status}"))?;
                insert_message(&s, &bot, message.room_id, "Failed to respond within 7 seconds", None, None, false, None, false)
                    .map_err(|status| format!("Timeout reply could not be saved: {status}"))?;
                return Ok(());
            }
            return Err(format!("Webhook request failed: {error}"));
        }
    };
    let text_reply = reply.status() == reqwest::StatusCode::OK;
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
            Ok(Some(_)) => return Err(format!("Webhook reply exceeded {max} bytes")),
            Err(error) => return Err(format!("Webhook reply read failed: {error}")),
        }
    }
    if text_reply && (kind == "text/plain" || kind == "text/html") {
        let bot = webhook_bot_user(&s, bot_id).map_err(|status| format!("Webhook reply bot lookup failed: {status}"))?;
        let text = String::from_utf8(data).map_err(|error| format!("Webhook text reply is invalid UTF-8: {error}"))?;
        insert_message(&s, &bot, message.room_id, &text, None, None, true, None, true)
            .map_err(|status| format!("Webhook text reply could not be saved: {status}"))?;
    } else if let Some(ext) = campfire_webhook_attachment_extension(&kind) {
        let bot = webhook_bot_user(&s, bot_id).map_err(|status| format!("Webhook reply bot lookup failed: {status}"))?;
        insert_message(
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
            false,
        ).map_err(|status| format!("Webhook attachment reply could not be saved: {status}"))?;
    }
    Ok(())
}
fn campfire_webhook_attachment_extension(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "text/plain" => "text",
        "text/html" => "html",
        "text/javascript" => "js",
        "text/css" => "css",
        "text/calendar" => "ics",
        "text/csv" => "csv",
        "text/vcard" => "vcf",
        "text/vtt" => "vtt",
        "text/markdown" => "md",
        "image/png" => "png",
        "image/jpeg" => "jpeg",
        "image/gif" => "gif",
        "image/bmp" => "bmp",
        "image/tiff" => "tiff",
        "image/svg+xml" => "svg",
        "image/webp" => "webp",
        "video/mpeg" => "mpeg",
        "audio/mpeg" => "mp3",
        "audio/ogg" => "ogg",
        "audio/aac" => "m4a",
        "video/webm" => "webm",
        "video/mp4" => "mp4",
        "font/otf" => "otf",
        "font/ttf" => "ttf",
        "font/woff" => "woff",
        "font/woff2" => "woff2",
        "application/xml" => "xml",
        "application/rss+xml" => "rss",
        "application/atom+xml" => "atom",
        "application/x-yaml" => "yaml",
        "multipart/form-data" => "multipart_form",
        "application/x-www-form-urlencoded" => "url_encoded_form",
        "application/json" => "json",
        "application/pdf" => "pdf",
        "application/zip" => "zip",
        "application/gzip" => "gzip",
        "text/vnd.turbo-stream.html" => "turbo_stream",
        _ => return None,
    })
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
        let mut rich = true;
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
            matches!(f.format.as_deref(), None | Some("html")),
        )
    };
    let m = insert_message(&s, &u, rid, &body, client_id, upload, rich, Some(&headers), true)?;
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
            StatusCode::OK,
            [(header::CONTENT_TYPE, "text/vnd.turbo-stream.html; charset=utf-8")],
            format!(
                "<turbo-stream action='append' target='{}'><template>{}</template></turbo-stream>",
                room_messages_target(m.room_kind.as_deref().ok_or(StatusCode::NOT_FOUND)?, rid)
                    .ok_or(StatusCode::NOT_FOUND)?,
                message_html(&s, &m, Some(&headers))
            ),
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
        render_source_page_sections(
            "Rustfire",
            &message_html(&s, &m, Some(&headers)),
            "", "", "", "", "", "",
            Some(&u),
            u.csrf_token.as_deref().unwrap_or(""),
        )
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
    let row: Option<(i64, String, Option<String>, String)> = db
        .query_row(
            "SELECT creator_id,body,body_source,client_message_id FROM messages WHERE id=?1 AND room_id=?2",
            params![mid, rid],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (creator, body, body_source, client_id) = row.ok_or(StatusCode::NOT_FOUND)?;
    if !is_admin(&u) && creator != u.id {
        return Err(StatusCode::FORBIDDEN);
    }
    let source = esc(&body_source.unwrap_or(body));
    let client_id = esc(&client_id);
    let frame_id = format!("edit_message_{client_id}");
    let delete_form_id = format!("delete_form_message_{client_id}");
    let action = format!("/rooms/{rid}/messages/{mid}");
    let upload_url = esc(&public_url(
        &headers,
        "/rails/active_storage/direct_uploads",
    ));
    let blob_url = esc(&public_url(
        &headers,
        "/rails/active_storage/blobs/redirect/:signed_id/:filename",
    ));
    let delete_button = format!(
        "<button name='button' type='submit' class='btn btn--negative' form='{delete_form_id}' data-turbo-confirm='Are you sure you want to delete this message?'><img aria-hidden='true' src='/assets/trash-708c7eb2.svg'><span class='for-screen-reader'>Delete message</span></button>"
    );
    let attachment = message_by_id(&s, rid, mid)?.attachment;
    let editor = if let Some(attachment) = attachment {
        format!(
            "{}<div class='message__edit-btns flex align-center justify-space-between gap full-width pad-block-start-half'><button name='button' type='submit' class='btn btn--negative center margin-block-end' form='{delete_form_id}' data-turbo-confirm='Are you sure you want to delete this message?'><img aria-hidden='true' src='/assets/trash-708c7eb2.svg'><span class='for-screen-reader'>Delete message</span></button></div>",
            attachment_presentation_html(&s, &attachment)
        )
    } else {
        format!(
            "<div class='composer--edit composer--rich-text'><form id='form_message_{client_id}' data-controller='form' data-action='trix-file-accept-&gt;form#preventAttachment keydown.esc-&gt;form#cancel keydown.ctrl+enter-&gt;form#submit:prevent keydown.meta+enter-&gt;form#submit:prevent' action='{action}' accept-charset='UTF-8' method='post'><input type='hidden' name='_method' value='patch'><div class='full-width input input--actor min-width fill-white'><input type='hidden' name='message[body]' id='message_body_trix_input_message_{client_id}' value='{source}'><trix-editor rows='1' class='input' aria-multiline='true' aria-label='Edit message' autofocus='autofocus' data-controller='rich-autocomplete' data-action='trix-change-&gt;typing-notifications#start keydown-&gt;composer#submitByKeyboard trix-focus-&gt;rich-autocomplete#focus trix-change-&gt;rich-autocomplete#search trix-blur-&gt;rich-autocomplete#blur' data-rich-autocomplete-url-value='/autocompletable/users?room_id={rid}' data-direct-upload-url='{upload_url}' data-blob-url-template='{blob_url}' id='message_body' input='message_body_trix_input_message_{client_id}'></trix-editor></div><a data-form-target='cancel' hidden='hidden' href='{action}'>Close editor and discard changes</a><div class='message__edit-btns flex align-center justify-space-between gap full-width pad-block-start-half'><button name='button' type='submit' class='btn btn--reversed'><img aria-hidden='true' src='/assets/check-7897ff7e.svg'><span class='for-screen-reader'>Save changes</span></button>{delete_button}</div></form></div>"
        )
    };
    let frame = format!(
        "<turbo-frame id='{frame_id}'><div class='message__body position-relative' data-controller='scroll-into-view'><div class='message__body-content message__body-content--editing gap'>{editor}</div><div class='message__actions flex flex-wrap'><a class='message__action-btn message__edit-close-btn txt-small btn btn--borderless' href='{action}'><img class='colorize--black' aria-hidden='true' src='/assets/remove-0e7a045d.svg'><span class='for-screen-reader'>Close editor and discard changes</span></a></div><form id='{delete_form_id}' data-turbo-frame='{frame_id}' action='{action}' accept-charset='UTF-8' method='post'><input type='hidden' name='_method' value='delete'></div></turbo-frame>"
    );
    let frame = csrf_forms(&frame, u.csrf_token.as_deref().unwrap_or(""));
    Ok(render_source_page_sections(
        "Rustfire", &frame, "", "", "", "", "", "", Some(&u),
        u.csrf_token.as_deref().unwrap_or(""),
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
    let rich = matches!(form_value(&f, "format", "message[format]"), None | Some("html"));
    let old_inline = inline_blob_ids(&db, "SELECT blob_id FROM inline_embeds WHERE message_id=?1", mid)?;
    let (plain, body_html, body_source, used_inline) = if rich {
        let normalized = action_text_webhook_html(body);
        let cleaned = strip_disallowed_rich_tags(&normalized);
        let blob_key = s.imported_blob_signing_key.as_deref().unwrap_or(&s.blob_signing_key);
        let (inline, used) = render_imported_inline_files(
            cleaned.as_deref().unwrap_or(&normalized),
            &db,
            mid,
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
            blob_key,
            false,
        )?;
        let (trusted, plain_source) = replace_mention_attachments(
            &inline,
            &db,
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
            s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key),
        )?;
        let request_host = headers
            .get(header::HOST)
            .and_then(|value| value.to_str().ok());
        let (mut plain, html) = rich_body_trusted(
            &trusted,
            request_host,
        );
        if plain_source != trusted {
            plain = rich_body_trusted(&plain_source, request_host).0;
        }
        if cleaned.is_some() {
            let (_, original_plain) = replace_mention_attachments(
                &normalized,
                &db,
                &s.mention_signing_key,
                s.imported_mention_signing_key.as_deref(),
                s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key),
            )?;
            plain = action_text_plain_body(&original_plain);
        }
        (plain, Some(html), Some(normalized), used)
    } else {
        (body.to_string(), None, None, Vec::new())
    };
    let updated_at = now();
    let updated_at_ns =
        message_timestamp_ns(&updated_at).ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    db.execute(
        "UPDATE messages SET body=?1,body_html=?2,body_source=?3,updated_at=?4,updated_at_ns=?5 WHERE id=?6",
        params![
            plain,
            body_html,
            body_source,
            updated_at,
            updated_at_ns,
            mid
        ],
    )
    .map_err(db_err)?;
    for blob_id in old_inline.iter().filter(|id| !used_inline.contains(id)) {
        db.execute("DELETE FROM inline_embeds WHERE message_id=?1 AND blob_id=?2", params![mid, blob_id])
            .map_err(db_err)?;
    }
    purge_orphan_inline_blobs(&db, &old_inline)?;
    touch_room(&db, rid)?;
    db.execute("DELETE FROM message_mentions WHERE message_id=?1", [mid])
        .map_err(db_err)?;
    if rich {
        for mentioned_id in mention_ids(
            body_source.as_deref().unwrap_or(body),
            &s.mention_signing_key,
            s.imported_mention_signing_key.as_deref(),
        ) {
            let allowed:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM memberships WHERE room_id=?1 AND user_id=?2)",params![rid,mentioned_id],|r|r.get(0)).map_err(db_err)?;
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
        Ok(found_redirect(&format!("/rooms/{rid}/messages/{mid}")))
    }
}
async fn message_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, mid)): Path<(i64, i64)>,
    Form(form): Form<HashMap<String, String>>,
) -> AppResult {
    match form.get("_method").map(String::as_str) {
        Some("patch" | "put") => {
            message_update(State(s), headers, Path((rid, mid)), Form(form)).await
        }
        Some("delete") => message_delete(State(s), headers, Path((rid, mid))).await,
        _ => Err(StatusCode::METHOD_NOT_ALLOWED),
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
    let inline_blobs = inline_blob_ids(&db, "SELECT blob_id FROM inline_embeds WHERE message_id=?1", mid)?;
    db.execute("DELETE FROM messages WHERE id=?1", [mid])
        .map_err(db_err)?;
    purge_orphan_inline_blobs(&db, &inline_blobs)?;
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
fn new_room_translation_button() -> String {
    let translations = [
        ("🇺🇸", "Name the room"),
        ("🇪🇸", "Nombrar la sala"),
        ("🇫🇷", "Nommez la salle"),
        ("🇮🇳", "कमरे का नाम दें"),
        ("🇩🇪", "Geben Sie dem Raum einen Namen"),
        ("🇧🇷", "Dê um nome a essa sala"),
        ("🇯🇵", "ルームに名前を付ける"),
    ];
    let entries = translations.iter().map(|(flag, phrase)| format!("<dt>{flag}</dt><dd class=\"margin-none\">{phrase}</dd>")).collect::<String>();
    format!("<details class=\"position-relative\" data-controller=\"popup\" data-action=\"keydown.esc-&gt;popup#close toggle-&gt;popup#toggle click@document-&gt;popup#closeOnClickOutside\" data-popup-orientation-top-class=\"popup-orientation-top\"><summary class=\"btn\" tabindex=\"-1\"><img aria-hidden=\"true\" class=\"color-icon\" src=\"/assets/globe-8c54d23b.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">Translate</span></summary><div class=\"language-list-menu shadow\" data-popup-target=\"menu\"><dl class=\"language-list\">{entries}</dl></div></details>")
}
fn room_form_user_row(id: i64, name: &str, bio: &str, updated_at: &str, closed: bool, is_new: bool, selected: bool, current_id: i64, avatar_key: &[u8]) -> Result<String, StatusCode> {
    let avatar = avatar_path(avatar_key, id, updated_at)?;
    let title = if bio.trim().is_empty() { name.to_string() } else { format!("{name} – {bio}") };
    let control = if !closed || (is_new && id == current_id) {
        format!("{}<img class=\"colorize--black flex-item-no-shrink\" aria-hidden=\"true\" src=\"/assets/check-7897ff7e.svg\" width=\"20\" height=\"20\" />", if closed { format!("<input type=\"hidden\" name=\"user_ids[]\" value=\"{id}\" />") } else { String::new() })
    } else {
        format!("<label class=\"switch flex-item-no-shrink\"><input type=\"checkbox\" name=\"user_ids[]\" value=\"{id}\" class=\"switch__input\"{} /><span class=\"switch__btn round\"></span><span class=\"for-screen-reader\">Give {} access to this room</span></label>", if selected { " checked=\"checked\"" } else { "" }, esc(name))
    };
    Ok(format!("<li class=\"flex align-center gap margin-none\" data-value=\"{}\"><figure class=\"avatar flex-item-no-shrink\" style=\"--avatar-size: 4ch;\"><a title=\"{}\" class=\"btn avatar\" data-turbo-frame=\"_top\" href=\"/users/{id}\"><img aria-hidden=\"true\" loading=\"lazy\" src=\"{avatar}\" width=\"48\" height=\"48\" /></a></figure><div class=\"min-width\"><div class=\"overflow-ellipsis fill-shade\"><strong>{}</strong></div></div><hr class=\"separator\" aria-hidden=\"true\" />{control}</li>", esc(&name.to_lowercase()), esc(&title), esc(name)))
}
fn room_form_panel(kind: &str, room_id: Option<i64>, name: &str, csrf_token: &str, rows: &str, user_count: usize) -> String {
    let closed = kind == "closeds";
    let other_kind = if closed { "opens" } else { "closeds" };
    let type_change_path = if let Some(id) = room_id { format!("/rooms/{other_kind}/{id}/edit") } else { format!("/rooms/{other_kind}/new") };
    let type_description = if closed { "Give everyone access to this room" } else { "Give only some access to this room" };
    let checked = if closed { "" } else { " checked=\"checked\"" };
    let search = if user_count > 20 { "<input type=\"search\" id=\"search\" autocorrect=\"off\" autocomplete=\"off\" data-1p-ignore=\"true\" class=\"input input--transparent full-width\" placeholder=\"Filter…\" data-action=\"input-&gt;filter#filter\">" } else { "" };
    let everyone = format!("<li class=\"flex align-center gap margin-none\"><figure class=\"avatar flex-item-no-shrink\" style=\"--avatar-border-radius: 0; --avatar-size: 4ch;\"><img aria-hidden=\"true\" class=\"colorize--black\" style=\"background-color: transparent\" src=\"/assets/everyone-4ca1d460.svg\" /><span class=\"for-screen-reader\">Everyone</span></figure><div class=\"min-width\"><div class=\"overflow-ellipsis fill-shade\"><strong>Everyone</strong></div></div><hr class=\"separator\" aria-hidden=\"true\"><a class=\"btn--faux flex-inline\" tabindex=\"-1\" data-turbo-action=\"replace\" href=\"{type_change_path}\"><label for=\"room_type\" class=\"switch\"><input type=\"checkbox\" id=\"room_type\" class=\"switch__input\"{checked}><span class=\"switch__btn round\"></span><span class=\"for-screen-reader\">{type_description}</span></label></a></li><hr class=\"separator full-width\" style=\"--border-style: solid\">");
    let style = room_id.map(|id| format!("edit-room-{id}")).unwrap_or_else(|| "new-room".to_string());
    let action = room_id.map(|id| format!("/rooms/{kind}/{id}")).unwrap_or_else(|| format!("/rooms/{kind}"));
    let method = if room_id.is_some() { "<input type=\"hidden\" name=\"_method\" value=\"patch\" />" } else { "" };
    let back = room_id.map(|id| format!("/rooms/{id}")).unwrap_or_else(|| "/".to_string());
    let csrf = esc(csrf_token);
    format!("<nav class=\"new-room-back\"><a class=\"btn\" href=\"{back}\"><img aria-hidden=\"true\" src=\"/assets/arrow-left-abe40556.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">Go Back</span></a></nav><section class=\"panel txt-align-center\" style=\"view-transition-name: {style}\"><form action=\"{action}\" accept-charset=\"UTF-8\" method=\"post\">{method}<input type=\"hidden\" name=\"authenticity_token\" value=\"{csrf}\" /><div class=\"flex align-center gap\">{}<label class=\"flex-item-grow txt-large\"><input name=\"room[name]\" id=\"room_name\" class=\"input full-width\" required=\"required\" autofocus=\"autofocus\" placeholder=\"Name the room\" data-turbo-permanent=\"true\" data-action=\"keydown.enter-&gt;form#submit:prevent\" type=\"text\" value=\"{}\" /><span class=\"for-screen-reader\">Name this room</span></label></div><hr class=\"margin-block borderless\"><section class=\"room-access margin-block pad-inline fill-shade border-radius\"><menu class=\"flex flex-column gap margin-none pad overflow-y constrain-height\" data-controller=\"filter\" data-filter-active-class=\"filter--active\" data-filter-selected-class=\"selected\">{everyone}{search}<div data-filter-target=\"list\" contents>{rows}</div></menu></section><button name=\"button\" type=\"submit\" class=\"btn btn--reversed txt-large center\"><img aria-hidden=\"true\" src=\"/assets/check-7897ff7e.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">Save</span></button></form></section>", new_room_translation_button(), esc(name))
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
        .prepare("SELECT id,name,COALESCE(bio,''),updated_at FROM users WHERE status=0 ORDER BY lower(name)")
        .map_err(db_err)?;
    let users = q
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?)))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
    let rows = users.iter().map(|(id,name,bio,updated_at)| room_form_user_row(*id,name,bio,updated_at,closed,true,*id==u.id,u.id,avatar_key)).collect::<Result<String, _>>()?;
    let panel = room_form_panel(&kind, None, "New room", u.csrf_token.as_deref().unwrap_or(""), &rows, users.len());
    Ok(render(
        "New chat room",
        &panel,
        Some(&u),
    ))
}
async fn new_open_room(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    new_room(State(s), headers, Path("opens".to_string())).await
}
async fn new_closed_room(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    new_room(State(s), headers, Path("closeds".to_string())).await
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
    notify_room_lists(&s, ids.iter().copied());
    broadcast_room_created(&s, &Room { id: rid, name: name.trim().to_owned(), kind: ty.to_owned(), creator_id: u.id }, &ids);
    Ok(found_redirect(&format!("/rooms/{rid}")))
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
    let kind = if r.kind == "Rooms::Closed" { "closeds" } else { "opens" };
    render_room_edit(s, headers, rid, kind).await
}
async fn render_room_edit(s: Arc<AppState>, headers: HeaderMap, rid: i64, kind: &str) -> AppResult {
    let u = user(&s, &headers)?;
    let r = room_for(&s, u.id, rid)?;
    if !can_admin(&u, &r) {
        return Err(StatusCode::FORBIDDEN);
    }
    if r.kind == "Rooms::Direct" {
        return Err(StatusCode::FORBIDDEN);
    }
    let db = pool(&s)?;
    let mut q=db.prepare("SELECT u.id,u.name,COALESCE(u.bio,''),u.updated_at,EXISTS(SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.room_id=?1) FROM users u WHERE u.status=0 ORDER BY lower(u.name)").map_err(db_err)?;
    let users=q.query_map([rid],|x|Ok((x.get::<_,i64>(0)?,x.get::<_,String>(1)?,x.get::<_,String>(2)?,x.get::<_,String>(3)?,x.get::<_,bool>(4)?))).map_err(db_err)?.collect::<Result<Vec<_>,_>>().map_err(db_err)?;
    let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
    let mut rows = String::new();
    if kind == "closeds" {
        let (selected, unselected): (Vec<_>, Vec<_>) = users.iter().partition(|user| user.4);
        for (id,name,bio,updated_at,_) in &selected {
            rows.push_str(&room_form_user_row(*id,name,bio,updated_at,true,false,true,u.id,avatar_key)?);
        }
        if !selected.is_empty() && !unselected.is_empty() {
            rows.push_str("<hr class=\"separator full-width\" style=\"--border-style: solid\">");
        }
        for (id,name,bio,updated_at,_) in &unselected {
            rows.push_str(&room_form_user_row(*id,name,bio,updated_at,true,false,false,u.id,avatar_key)?);
        }
    } else {
        for (id,name,bio,updated_at,_) in &users {
            rows.push_str(&room_form_user_row(*id,name,bio,updated_at,false,false,true,u.id,avatar_key)?);
        }
    }
    let csrf = u.csrf_token.as_deref().unwrap_or("");
    let mut panels = room_form_panel(kind, Some(rid), &r.name, csrf, &rows, users.len());
    let delete_url = public_url(&headers, &format!("/rooms/{rid}"));
    panels.push_str(&format!("<section class=\"panel txt-align-center\"><form class=\"button_to\" method=\"post\" action=\"{delete_url}\"><input type=\"hidden\" name=\"_method\" value=\"delete\" /><button class=\"btn btn--negative max-width\" aria-label=\"Delete {}\" data-turbo-confirm=\"Are you sure you want to delete this room and all messages in it? This can’t be undone.\" type=\"submit\"><img aria-hidden=\"true\" src=\"/assets/trash-708c7eb2.svg\" width=\"20\" height=\"20\" /><span class=\"overflow-ellipsis\">{}</span></button><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\" /></form></section>", esc(&r.name), esc(&r.name), esc(csrf)));
    Ok(render(
        &format!("Edit settings for {}", r.name),
        &panels,
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
    broadcast_room_updated(s, &Room { id: rid, name: name.trim().to_owned(), kind: ty.to_owned(), creator_id: r.creator_id }, &current_members);
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
    let u = match user(&s, &headers) {
        Ok(user) => user,
        Err(StatusCode::UNAUTHORIZED) => return Ok(found_redirect("/session/new")),
        Err(error) => return Err(error),
    };
    match room_for(&s, u.id, rid) {
        Ok(room) if room.kind != "Rooms::Direct" => {},
        Ok(_) | Err(StatusCode::NOT_FOUND) => return Ok(found_redirect("/")),
        Err(error) => return Err(error),
    }
    Ok(found_redirect(&format!("/rooms/{rid}")))
}
async fn room_kind_edit(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    Path(rid): Path<i64>,
) -> AppResult {
    let kind = if uri.path().starts_with("/rooms/opens/") { "opens" } else { "closeds" };
    render_room_edit(s, headers, rid, kind).await
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
    Ok(found_redirect(&format!("/rooms/{rid}")))
}
async fn room_kind_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    Path(rid): Path<i64>,
    RawForm(raw): RawForm,
) -> AppResult {
    let (values, _) = fields(&raw);
    match values.get("_method").map(String::as_str) {
        Some("patch" | "put") => room_kind_update(State(s), headers, OriginalUri(uri), Path(rid), RawForm(raw)).await,
        Some("delete") => room_kind_delete(State(s), headers, Path(rid)).await,
        _ => Err(StatusCode::METHOD_NOT_ALLOWED),
    }
}
async fn room_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    RawForm(raw): RawForm,
) -> AppResult {
    let (values, _) = fields(&raw);
    if values.get("_method").map(String::as_str) != Some("delete") {
        return Err(StatusCode::METHOD_NOT_ALLOWED);
    }
    room_delete(State(s), headers, Path(rid)).await
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
    let mut stmt = db.prepare("SELECT u.id,u.name,COALESCE(u.bio,''),u.updated_at FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.room_id=?1 AND (u.id!=?2 OR (SELECT count(*) FROM memberships WHERE room_id=?1)=1) ORDER BY m.id").map_err(db_err)?;
    let people = stmt
        .query_map(params![rid, u.id], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?)))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    let names = people.iter().map(|(_, name, _, _)| name.as_str()).collect::<Vec<_>>();
    let display_name = match names.as_slice() {
        [] => u.name.clone(),
        [name] => (*name).to_owned(),
        [first, second] => format!("{first} and {second}"),
        _ => format!("{}, and {}", names[..names.len()-1].join(", "), names.last().unwrap()),
    };
    let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
    let mut members = String::new();
    for (id, name, bio, updated_at) in people {
        let title = if bio.trim().is_empty() { name.clone() } else { format!("{name} – {bio}") };
        let avatar = avatar_path(avatar_key, id, &updated_at)?;
        members.push_str(&format!("<div class=\"member flex flex-column gap fill-shade pad border-radius\"><figure class=\"avatar center\" style=\"--avatar-border-radius: 10ch; --avatar-size: 10ch;\"><a title=\"{}\" class=\"btn avatar\" data-turbo-frame=\"_top\" href=\"/users/{id}\"><img aria-hidden=\"true\" loading=\"lazy\" src=\"{avatar}\" width=\"48\" height=\"48\" /></a></figure><strong>{}</strong></div>", esc(&title), esc(&name)));
    }
    let delete_url = public_url(&headers, &format!("/rooms/directs/{rid}"));
    Ok(render(
        &format!("Edit settings for {display_name}"),
        &format!(
            "<nav class=\"ping-settings-back\"><a class=\"btn\" href=\"/rooms/{rid}\"><img aria-hidden=\"true\" src=\"/assets/arrow-left-abe40556.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">Go Back</span></a></nav><div class=\"panel txt-align-center\"><section class=\"directs--edit margin-block-end\">{members}</section><form class=\"button_to\" method=\"post\" action=\"{delete_url}\"><input type=\"hidden\" name=\"_method\" value=\"delete\" /><button class=\"btn btn--negative center\" aria-label=\"Delete Ping\" data-turbo-confirm=\"Are you sure you want to delete this ping and all messages in it? This can’t be undone.\" type=\"submit\"><img aria-hidden=\"true\" src=\"/assets/trash-708c7eb2.svg\" />Ping</button><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\" /></form></div>", esc(u.csrf_token.as_deref().unwrap_or(""))
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
async fn direct_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
    Form(form): Form<HashMap<String, String>>,
) -> AppResult {
    if form.get("_method").map(String::as_str) != Some("delete") {
        return Err(StatusCode::METHOD_NOT_ALLOWED);
    }
    direct_delete(State(s), headers, Path(rid)).await
}
async fn direct_show(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(rid): Path<i64>,
) -> AppResult {
    let u = match user(&s, &headers) {
        Ok(user) => user,
        Err(StatusCode::UNAUTHORIZED) => return Ok(found_redirect("/session/new")),
        Err(error) => return Err(error),
    };
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
    let inline_blobs = inline_blob_ids(&db, "SELECT DISTINCT e.blob_id FROM inline_embeds e JOIN messages m ON m.id=e.message_id WHERE m.room_id=?1", rid)?;
    db.execute("DELETE FROM rooms WHERE id=?1", [rid])
        .map_err(db_err)?;
    purge_orphan_inline_blobs(&db, &inline_blobs)?;
    for stored in attachments {
        remove_attachment_files(&stored);
    }
    s.events.remove(rid);
    notify_room_lists(&s, members);
    s.turbo_shared_rooms.send_turbo(0, format!("<turbo-stream action=\"remove\" target=\"{}\"></turbo-stream>", room_list_target(&r)));
    Ok(found_redirect("/"))
}
async fn direct_new(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let frame = format!(
        "<turbo-frame id=\"direct_rooms_control\" target=\"_top\">\n  <div class=\"directs directs--new flex flex-column gap\">\n    <form class=\"flex gap flex-item-grow\" data-controller=\"form\" data-action=\"keydown.esc-&gt;form#cancel\" action=\"/rooms/directs\" accept-charset=\"UTF-8\" method=\"post\"><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\" />\n      <a class=\"btn flex-item-no-shrink\" data-turbo-frame=\"user_sidebar\" data-form-target=\"cancel\" href=\"/users/me/sidebar\">\n        <img aria-hidden=\"true\" src=\"/assets/arrow-left-abe40556.svg\" />\n        <span class=\"for-screen-reader\">Cancel changes</span>\n</a>\n      <section class=\"autocomplete__container unpad input input--actor\">\n        <div class=\"autocomplete__input input flex flex-wrap position-relative flex-item-grow\" data-controller=\"autocomplete\" data-autocomplete-url-value=\"/autocompletable/users\">\n          <select name=\"user_ids[]\" data-autocomplete-target=\"select\" data-template-id=\"autocompletable-user\" multiple=\"true\" hidden required></select>\n          <template id=\"autocompletable-user\">\n  <div class=\"autocomplete__pill max-width\" data-value=\"\" tabindex=\"0\">\n    <img class=\"avatar flex-item-no-shrink\" data-content=\"avatar\" src=\"\" />\n    <span class=\"autocomplete-field__selected-value-text overflow-ellipsis flex-item-grow\" data-content=\"label\"></span>\n    <button type=\"button\" data-action=\"autocomplete#remove:prevent\" data-value=\"\" tabindex=\"-1\" class=\"btn btn--plain txt-small translucent flex-item-no-shrink\"><img aria-hidden=\"true\" class=\"colorize--black\" src=\"/assets/remove-circle-384c618c.svg\" /><span class=\"for-screen-reader\">Remove <span data-content=\"screenReaderLabel\"></span></span></button>\n  </div>\n</template>\n          <input autocomplete=\"off\" autocorrect=\"off\" data-1p-ignore=\"true\" class=\"autocomplete__input input flex flex-wrap position-relative\" data-autocomplete-target=\"input\" data-action=\"input-&gt;autocomplete#search keydown-&gt;autocomplete#didPressKey\" type=\"text\" name=\"rooms_direct[user_ids_input]\" id=\"rooms_direct_user_ids_input\" />\n        </div>\n      </section>\n      <button name=\"button\" type=\"submit\" class=\"btn btn--reversed flex-item-no-shrink\"><img aria-hidden=\"true\" src=\"/assets/check-7897ff7e.svg\" /><span class=\"for-screen-reader\">Start Ping</span></button></form>\n    <span class=\"txt-small translucent pad-inline-half center\">Type names to ping someone…</span>\n  </div>\n</turbo-frame>",
        esc(u.csrf_token.as_deref().unwrap_or(""))
    );
    if headers.get("turbo-frame").and_then(|value| value.to_str().ok()) == Some("direct_rooms_control") {
        return Ok(Html(frame).into_response());
    }
    Ok(render(
        "New ping",
        &frame,
        Some(&u),
    ))
}
async fn direct_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    RawForm(raw): RawForm,
) -> AppResult {
    let u = user(&s, &headers)?;
    let (_, mut ids) = fields(&raw);
    ids.extend(fields(uri.query().unwrap_or("").as_bytes()).1);
    ids.push(u.id);
    ids.sort_unstable();
    ids.dedup();
    let mut db = pool(&s)?;
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(db_err)?;
    let mut selected = Vec::with_capacity(ids.len());
    for id in ids {
        let name: Option<String> = tx
            .query_row("SELECT name FROM users WHERE id=?1", [id], |r| r.get(0))
            .optional()
            .map_err(db_err)?;
        if name.is_some() {
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
    tx.execute("INSERT INTO rooms(name,type,creator_id,created_at,updated_at) VALUES(NULL,'Rooms::Direct',?1,?2,?2)",params![u.id,t]).map_err(db_err)?;
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
    let raw_query = q.get("q").cloned().unwrap_or_default();
    let query = search_query(&raw_query);
    let db = pool(&s)?;
    let mut messages = Vec::new();
    if !query.trim().is_empty() {
        // Select the latest visible IDs before loading attachments, boosts, and rich text.
        let mut stmt=db.prepare("WITH hits AS MATERIALIZED (SELECT m.id,m.created_at_ns FROM message_search_index idx JOIN messages m ON m.id=idx.rowid JOIN memberships mem ON mem.room_id=m.room_id WHERE mem.user_id=?1 AND idx.body MATCH ?2 ORDER BY m.created_at_ns DESC,m.id DESC LIMIT 100) SELECT m.id,m.room_id,m.creator_id,u.name,m.body,m.created_at,m.client_message_id,a.id,a.filename,a.content_type,(SELECT json_group_array(json_object('id',id,'booster_id',booster_id,'booster_name',booster_name,'booster_updated_at',booster_updated_at,'content',content)) FROM (SELECT b.id,b.booster_id,bu.name AS booster_name,bu.updated_at AS booster_updated_at,b.content FROM boosts b JOIN users bu ON bu.id=b.booster_id WHERE b.message_id=m.id ORDER BY b.id)),u.role,m.body_html,u.updated_at,m.updated_at,a.width,a.height FROM hits h JOIN messages m ON m.id=h.id JOIN users u ON u.id=m.creator_id LEFT JOIN attachments a ON a.message_id=m.id ORDER BY h.created_at_ns DESC,h.id DESC").map_err(db_err)?;
        messages = stmt
            .query_map(params![u.id, query], chat_message_from_row)
            .map_err(db_err)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(db_err)?;
        messages.reverse();
        let mut room_names = HashMap::new();
        for message in &mut messages {
            let room_name = if let Some(name) = room_names.get(&message.room_id) {
                name
            } else {
                room_names.insert(
                    message.room_id,
                    message_room_display_name(&db, message.room_id)?,
                );
                &room_names[&message.room_id]
            };
            message.room_name = room_name.clone();
        }
    }
    let mut recent_query = db
        .prepare("SELECT query FROM searches WHERE user_id=?1 ORDER BY updated_at DESC,id DESC LIMIT 10")
        .map_err(db_err)?;
    let recent = recent_query
        .query_map([u.id], |r| r.get::<_, String>(0))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    // Rendering and the return-room lookup do not need this pooled connection.
    drop(recent_query);
    drop(db);
    let results = messages
        .iter()
        .map(|message| message_html(&s, message, Some(&headers)))
        .collect::<String>();
    let recent_links = recent
        .iter()
        .map(|item| {
            let encoded = form_urlencoded::Serializer::new(String::new())
                .append_pair("q", item)
                .finish();
            format!("<a class=\"align-center gap room btn txt-nowrap\" href=\"/searches?{encoded}\"><span class=\"overflow-ellipsis\">“{}”</span></a>", esc(item))
        })
        .collect::<String>();
    let token = u.csrf_token.as_deref().unwrap_or("");
    let results = csrf_forms(&results, token);
    let clear_button = if recent.is_empty() {
        String::new()
    } else {
        format!("<form class=\"button_to\" method=\"post\" action=\"/searches/clear\"><input type=\"hidden\" name=\"_method\" value=\"delete\"><button class=\"btn searches__btn\" data-turbo-confirm=\"Are you sure you want to clear your recent searches?\" type=\"submit\"><img aria-hidden=\"true\" src=\"/assets/broom-13d30a95.svg\"><span class=\"for-screen-reader\">Clear recent searches</span></button><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\"></form>", esc(token))
    };
    let back_room = cookie(&headers, "last_room")
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|id| room_for(&s, u.id, *id).is_ok())
        .or_else(|| rooms_for(&s, u.id).ok()?.first().map(|room| room.id));
    let back_href = back_room.map_or("/".to_string(), |id| format!("/rooms/{id}"));
    let query_heading = if query.trim().is_empty() {
        String::new()
    } else {
        format!("<div class=\"searches__query flex align-center gap pad-block-start-half\"><div class=\"btn btn--reversed btn--faux align-center gap txt-nowrap\"><span class=\"overflow-ellipsis\">“{}”</span><span class=\"flex-item-no-shrink\">{}</span></div></div>", esc(&query), messages.len())
    };
    let nav = format!("{query_heading}<div class=\"searches__recents align-center gap pad-block-half overflow-y overflow-hide-scrollbar\">{recent_links}{clear_button}</div>");
    let sidebar = format!("<div class=\"rooms position-relative flex flex-column gap overflow-y overflow-hide-scrollbar\">{recent_links}{clear_button}</div>");
    let body = format!("<div id=\"message-area\" class=\"message-area\"><div class=\"message-area--empty min-width center\"><figure class=\"center pad\"><img aria-hidden=\"true\" class=\"colorize--black translucent\" src=\"/assets/search-5f29565f.svg\"></figure></div><div id=\"search-results\" class=\"messages searches__results\" data-controller=\"search-results\" data-search-results-target=\"messages\" data-search-results-me-class=\"message--me\" data-search-results-threaded-class=\"message--threaded\" data-search-results-mentioned-class=\"message--mentioned\" data-search-results-formatted-class=\"message--formatted\">{results}</div></div>");
    let input_value = if raw_query.is_empty() { String::new() } else { format!(" value=\"{}\"", esc(&raw_query)) };
    let footer = format!("<div class=\"composer flex align-end gap\"><a class=\"btn flex-item-no-shrink margin-block-end\" style=\"view-transition-name: input-switcher; --btn-border-radius: 0.5em\" href=\"{back_href}\"><img aria-hidden=\"true\" src=\"/assets/arrow-left-abe40556.svg\"><span class=\"for-screen-reader\">Exit search </span></a><form class=\"margin-block flex-item-grow contain flex align-center gap\" data-controller=\"form\" data-action=\"keydown.esc-&gt;form#cancel\" action=\"/searches\" accept-charset=\"UTF-8\" method=\"post\"><input type=\"hidden\" name=\"authenticity_token\" value=\"{}\"><div class=\"composer__input flex align-center flex-item-grow gap full-width input input--actor min-width\"><img aria-hidden=\"true\" class=\"composer__input-hint colorize--black\" style=\"view-transition-name: input-btn;\" src=\"/assets/search-5f29565f.svg\" width=\"20\" height=\"20\"><input{input_value} class=\"searches__input input flex-item-grow\" role=\"searchbox\" aria-label=\"search\" autofocus=\"autofocus\" required=\"required\" type=\"text\" name=\"q\" id=\"q\"><a data-form-target=\"cancel\" role=\"button\" class=\"searches__reset\" href=\"/searches\"><img aria-hidden=\"true\" class=\"colorize--black\" src=\"/assets/remove-0e7a045d.svg\" width=\"14\" height=\"14\"><span class=\"for-screen-reader\">Clear search field</span></a><button name=\"button\" type=\"submit\" class=\"btn btn--reversed flex-item-no-shrink txt-small\" style=\"--btn-border-radius: 0.5em\"><img aria-hidden=\"true\" src=\"/assets/arrow-up-f96b3895.svg\"><span class=\"for-screen-reader\">Search</span></button></div></form></div>", esc(token));
    Ok(render_source_page_sections("Search", &body, &nav, &footer, &sidebar, "sidebar searches", "", "", Some(&u), token))
}
fn search_query(raw: &str) -> String {
    static NON_WORD: OnceLock<Regex> = OnceLock::new();
    NON_WORD
        .get_or_init(|| Regex::new(r"[^\w]").unwrap())
        .replace_all(raw, " ")
        .into_owned()
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
    let query = search_query(&f.q);
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    tx.execute("INSERT INTO searches(user_id,query,created_at,updated_at) VALUES(?1,?2,?3,?3) ON CONFLICT(user_id,query) DO UPDATE SET updated_at=excluded.updated_at",params![u.id,query,now()]).map_err(db_err)?;
    tx.execute("DELETE FROM searches WHERE user_id=?1 AND id NOT IN (SELECT id FROM searches WHERE user_id=?1 ORDER BY updated_at DESC,id DESC LIMIT 10)", [u.id]).map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    let encoded = form_urlencoded::Serializer::new(String::new())
        .append_pair("q", &query)
        .finish();
    Ok(found_redirect(&format!("/searches?{encoded}")))
}
async fn search_clear(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    pool(&s)?
        .execute("DELETE FROM searches WHERE user_id=?1", [u.id])
        .map_err(db_err)?;
    Ok(found_redirect("/searches"))
}
fn account_user_item(
    u: &User,
    avatar_key: &[u8],
    id: i64,
    name: &str,
    role: i64,
    status: i64,
    updated_at: &str,
) -> Result<String, StatusCode> {
    let avatar = avatar_path(avatar_key, id, updated_at)?;
    let csrf = esc(u.csrf_token.as_deref().unwrap_or(""));
    let controls = if is_admin(u) && status == 0 {
        let disabled = if id == u.id {
            " disabled=\"disabled\""
        } else {
            ""
        };
        let checked = if role == 1 {
            " checked=\"checked\""
        } else {
            ""
        };
        let role_name = if role == 1 { "Administrator" } else { "Member" };
        let role_control = format!(
            "<form data-controller=\"form\" action=\"/account/users/{id}\" accept-charset=\"UTF-8\" method=\"post\"><input type=\"hidden\" name=\"_method\" value=\"patch\" /><input type=\"hidden\" name=\"authenticity_token\" value=\"{csrf}\" /><label class=\"btn txt-small flex-item-no-shrink\" for=\"role_user_{id}\"><span class=\"for-screen-reader\">Role: {role_name}</span><img aria-hidden=\"true\" src=\"/assets/crown-00d190cb.svg\" width=\"20\" height=\"20\" /><input name=\"user[role]\"{disabled} type=\"hidden\" value=\"member\" /><input data-action=\"form#submit\" hidden=\"hidden\" id=\"role_user_{id}\"{disabled} type=\"checkbox\" value=\"administrator\"{checked} name=\"user[role]\" /></label></form>"
        );
        let deactivate_control = if id == u.id {
            "".to_string()
        } else {
            format!(
                "<form class=\"button_to\" method=\"post\" action=\"/account/users/{id}\"><input type=\"hidden\" name=\"_method\" value=\"delete\" /><button class=\"btn txt-small flex-item-no-shrink btn--negative\" data-turbo-confirm=\"Are you sure you want to permanently remove this person from the account? This can’t be undone.\" type=\"submit\"><img aria-hidden=\"true\" src=\"/assets/minus-b31a1093.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">Delete {}</span></button><input type=\"hidden\" name=\"authenticity_token\" value=\"{csrf}\" /></form>",
                esc(name),
            )
        };
        format!("{role_control}{deactivate_control}")
    } else {
        String::new()
    };
    let profile = if id == u.id {
        "<a class=\"btn txt-small flex-item-no-shrink\" target=\"_top\" href=\"/users/me/profile\"><img aria-hidden=\"true\" src=\"/assets/pencil-cf9d28aa.svg\" width=\"20\" height=\"20\" /><span class=\"for-screen-reader\">My settings</span></a>"
    } else {
        ""
    };
    Ok(format!(
        "<li class=\"flex align-center gap margin-none {}\"><figure class=\"avatar flex-item-no-shrink\" style=\"--avatar-size: 3.75ch;\"><a title=\"{}\" class=\"btn avatar\" data-turbo-frame=\"_top\" href=\"/users/{id}\"><img aria-hidden=\"true\" loading=\"lazy\" src=\"{avatar}\" width=\"48\" height=\"48\" /></a></figure><div class=\"min-width\"><div class=\"overflow-ellipsis fill-shade\"><strong>{}</strong></div></div><hr class=\"separator\" aria-hidden=\"true\">{controls}{profile}</li>",
        if status == 2 { "banned" } else { "" },
        esc(name),
        esc(name),
    ))
}
fn account_next_page_container(page: i64) -> String {
    format!(
        "<turbo-frame loading=\"lazy\" class=\"flex center\" id=\"next_page_container\" src=\"/account/users.turbo_stream?page={page}\"><div class=\"spinner center\"></div></turbo-frame>"
    )
}
async fn account_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let csrf = esc(u.csrf_token.as_deref().unwrap_or(""));
    let db = pool(&s)?;
    let (name, code, updated_at): (String, String, String) = db
        .query_row(
            "SELECT name,join_code,updated_at FROM accounts LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .map_err(db_err)?;
    let mut administrators = String::new();
    let mut members = String::new();
    let mut total_users = 0;
    let mut q = db
        .prepare("SELECT id,name,role,status,updated_at FROM users WHERE status=0 OR (?1 AND status=2) ORDER BY lower(name)")
        .map_err(db_err)?;
    for row in q
        .query_map([is_admin(&u)], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, String>(4)?,
            ))
        })
        .map_err(db_err)?
    {
        let (id, n, role, status, updated_at) = row.map_err(db_err)?;
        if role == 2 {
            continue;
        }
        total_users += 1;
        let key = s
            .imported_avatar_signing_key
            .as_deref()
            .unwrap_or(&s.avatar_signing_key);
        let item = account_user_item(&u, key, id, &n, role, status, &updated_at)?;
        if role == 1 {
            administrators.push_str(&item);
        } else {
            members.push_str(&item);
        }
    }
    let divider = if !administrators.is_empty() && !members.is_empty() {
        "<hr class=\"separator full-width\" style=\"--border-style: solid\">"
    } else {
        ""
    };
    let next_page = if total_users > 500 {
        account_next_page_container(2)
    } else {
        String::new()
    };
    let list = format!(
        "<menu class=\"flex flex-column gap margin-none pad\"><turbo-frame id=\"account_users\">{administrators}{divider}{members}{next_page}</turbo-frame></menu>"
    );
    let restricted: bool = db
        .query_row(
            "SELECT restrict_room_creation FROM account_settings WHERE id=1",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let has_logo: bool = db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account_logos WHERE id=1)",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    drop(q);
    drop(db);
    let invite_url = public_url(&headers, &format!("/join/{code}"));
    let qr = format!("/qr_code/{}", URL_SAFE.encode(invite_url.as_bytes()));
    let invite = html_escape::encode_double_quoted_attribute(&invite_url);
    let logo_version: String = updated_at
        .chars()
        .filter(char::is_ascii_digit)
        .take(14)
        .collect();
    let logo_url = format!("/account/logo?v={logo_version}");
    let account_name_translation = profile_translation_button("Name this account", [
        "Nombre de esta cuenta", "Nommez ce compte", "इस खाते का नाम दें",
        "Benennen Sie dieses Konto", "Dê um nome a essa conta", "アカウントに名前を付ける",
    ]);
    let account_controls = if is_admin(&u) {
        let delete_logo = if has_logo {
            format!(
                "<form class='button_to' method='post' action='{logo_url}'><input type='hidden' name='_method' value='delete'><button class='btn btn--negative txt-small avatar__delete-btn' type='submit'><img aria-hidden='true' src='/assets/minus-b31a1093.svg' width='20' height='20'><span class='for-screen-reader'>Delete logo</span></button><input type='hidden' name='authenticity_token' value='{csrf}'></form>"
            )
        } else {
            String::new()
        };
        format!(
            r#"<div class="align-center center avatar__form gap" data-controller="upload-preview">
<form class="txt--medium" data-controller="form" enctype="multipart/form-data" action="/account.1" accept-charset="UTF-8" method="post"><input type="hidden" name="_method" value="patch"><label class="btn input--file"><img aria-hidden="true" src="/assets/camera-927323b8.svg" width="20" height="20"><input class="input" accept="image/*" data-action="upload-preview#previewImage change->form#submit" type="file" name="account[logo]" id="account_logo"><span class="for-screen-reader">Upload logo</span></label></form>
<form data-controller="form" enctype="multipart/form-data" action="/account.1" accept-charset="UTF-8" method="post"><input type="hidden" name="_method" value="patch"><label class="btn avatar input--file account-logo txt-xx-large"><img role="presentation" data-upload-preview-target="image" src="{logo_url}" width="48" height="48"><input class="input" accept="image/*" data-action="upload-preview#previewImage change->form#submit" type="file" name="account[logo]" id="account_logo"><span class="for-screen-reader">Upload logo</span></label></form>{delete_logo}</div>
<form class="flex flex-column gap" data-controller="form" action="/account.1" accept-charset="UTF-8" method="post"><input type="hidden" name="_method" value="patch"><div class="flex align-center gap">{account_name_translation}<label class="flex align-center gap flex-item-grow"><input class="input txt-large" autocomplete="off" placeholder="Name this account" autofocus="autofocus" data-action="keydown.enter->form#submit" type="text" value="{}" name="account[name]" id="account_name"></label><button name="button" type="submit" class="btn btn--reversed center"><img aria-hidden="true" src="/assets/check-7897ff7e.svg" width="20" height="20"><span class="for-screen-reader">Save changes</span></button></div></form>
<div class="margin-block-start pad-block pad-inline-double fill-shade border-radius"><form class="flex align-center gap center" data-controller="form" action="/account.1" accept-charset="UTF-8" method="post"><input type="hidden" name="_method" value="put"><div class="flex-item-grow flex align-center gap txt-align-start"><img class="colorize--black" aria-hidden="true" src="/assets/crown-00d190cb.svg" width="18" height="18"> Must be admin to create new rooms</div><input value="{}" type="hidden" name="account[settings][restrict_room_creation_to_administrators]" id="account_settings_restrict_room_creation_to_administrators"><label class="switch"><input type="checkbox" class="switch__input" {} data-action="change->form#submit"><span class="switch__btn round"></span><span class="for-screen-reader">Must be admin to create new rooms</span></label></form></div>"#,
            esc(&name),
            if restricted { "false" } else { "true" },
            if restricted { "checked" } else { "" }
        )
    } else {
        format!(
            "<figure class='account-logo avatar txt-xx-large center'><img src='{logo_url}' alt='Account logo' width='300' height='300'></figure><h1 class='flex-item-grow txt-x-large'>{}</h1>",
            esc(&name)
        )
    };
    let regenerate = if is_admin(&u) {
        format!("<form class='button_to' method='post' action='/account/join_code'><button class='btn btn--regenerate' type='submit'><img aria-hidden='true' class='colorize--black' src='/assets/refresh-249f0509.svg' width='20' height='20'><span class='for-screen-reader'>Regenerate join link</span></button><input type='hidden' name='authenticity_token' value='{csrf}'></form>")
    } else {
        String::new()
    };
    let invite_controls = format!(
        r#"<div class="flex flex-column align-center gap"><label class="flex flex-column gap full-width" style="--row-gap: 0.5em"><strong id="invite_label" class="invite-label">Share to invite more people</strong><span class="flex align-center gap input input--actor fill-white"><img aria-hidden="true" class="colorize--black" src="/assets/person-add-1432b76b.svg" width="20" height="20"><input type="text" class="input" id="invite_url" value="{invite}" aria-labelledby="invite_label" readonly></span></label><div class="flex align-center gap"><a class="btn" data-lightbox-target="image" data-action="lightbox#open" data-lightbox-url-value="{qr}" href="{qr}"><span class="for-screen-reader">Show join link QR code</span><img aria-hidden="true" class="colorize--black" src="/assets/qr-code-dac3b273.svg" width="20" height="20"></a><button class="btn" data-controller="copy-to-clipboard" data-action="copy-to-clipboard#copy" data-copy-to-clipboard-success-class="btn--success" data-copy-to-clipboard-content-value="{invite}"><span class="for-screen-reader">Copy join link</span><img aria-hidden="true" class="colorize--black" src="/assets/copy-paste-4c379063.svg" width="20" height="20"></button><button class="btn" hidden="hidden" data-controller="web-share" data-action="web-share#share" data-web-share-url-value="{invite}" data-web-share-text-value="Hit this link to join me in Campfire and start chatting." data-web-share-title-value="Link to join Campfire"><span class="for-screen-reader">Share join link</span><img aria-hidden="true" class="colorize--black" src="/assets/share-bf28da4f.svg" width="20" height="20"></button>{regenerate}</div></div>"#
    );
    let back_room = cookie(&headers, "last_room")
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|id| room_for(&s, u.id, *id).is_ok())
        .or_else(|| room_for(&s, u.id, 1).ok().map(|_| 1));
    let back_href = back_room.map_or("/".to_string(), |id| format!("/rooms/{id}"));
    let nav_tools = if is_admin(&u) {
        "<div class='flex align-center gap flex-item-justify-end'><a class='btn' style='view-transition-name: chat-bots' href='/account/bots'><img aria-hidden='true' src='/assets/bot-8a69692e.svg' width='20' height='20'><span class='for-screen-reader'>Set up chat bots</span></a><a class='btn' style='view-transition-name: custom-styles' href='/account/custom_styles/edit'><img aria-hidden='true' src='/assets/art-ed709d32.svg' width='20' height='20'><span class='for-screen-reader'>Custom styles</span></a></div>"
    } else { "" };
    let nav = format!("<div class='flex-item-justify-start'><a class='btn' href='{back_href}'><img aria-hidden='true' src='/assets/arrow-left-abe40556.svg' width='20' height='20'><span class='for-screen-reader'>Go Back</span></a></div>{nav_tools}");
    let account_controls = account_controls
        .replace("<input type=\"hidden\" name=\"_method\" value=\"patch\">", &format!("<input type=\"hidden\" name=\"_method\" value=\"patch\"><input type='hidden' name='authenticity_token' value='{csrf}'>"))
        .replace("<input type=\"hidden\" name=\"_method\" value=\"put\">", &format!("<input type=\"hidden\" name=\"_method\" value=\"put\"><input type='hidden' name='authenticity_token' value='{csrf}'>"));
    let body = format!("<section class='panel txt-align-center flex flex-column gap' style='view-transition-name: account-settings'>{account_controls}<div class='margin-block pad-inline pad-block-start fill-shade border-radius'>{invite_controls}<hr class='margin-block separator full-width' style='--border-style: solid'>{list}</div></section>");
    let footer = "<div class='txt-align-center center margin-block-double txt-subtle'>Campfire™ version Rustfire</div>";
    Ok(render_source_page_with_footer("Account settings", &body, &nav, footer, Some(&u), u.csrf_token.as_deref().unwrap_or("")))
}
async fn account_users_index(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    Query(query): Query<HashMap<String, String>>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let accept = headers
        .get(header::ACCEPT)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if !uri.path().ends_with(".turbo_stream")
        && !accept.contains("text/vnd.turbo-stream.html")
        && !accept.contains("*/*")
    {
        return Ok(not_acceptable_format(accept));
    }
    let page = query
        .get("page")
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|number| *number > 0)
        .unwrap_or(1);
    let db = pool(&s)?;
    let total: i64 = db
        .query_row(
            "SELECT COUNT(*) FROM users WHERE status=0 AND role!=2",
            [],
            |row| row.get(0),
        )
        .map_err(db_err)?;
    let page_count = (total.saturating_add(499) / 500).max(1);
    let offset = page.saturating_sub(1).saturating_mul(500);
    let mut q = db
        .prepare("SELECT id,name,role,status,updated_at FROM users WHERE status=0 AND role!=2 ORDER BY lower(name) LIMIT 500 OFFSET ?1")
        .map_err(db_err)?;
    let mut rows = String::new();
    for row in q
        .query_map([offset], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, String>(4)?,
            ))
        })
        .map_err(db_err)?
    {
        let (id, name, role, status, updated_at) = row.map_err(db_err)?;
        let key = s
            .imported_avatar_signing_key
            .as_deref()
            .unwrap_or(&s.avatar_signing_key);
        rows.push_str(&account_user_item(
            &u,
            key,
            id,
            &name,
            role,
            status,
            &updated_at,
        )?);
    }
    let next = if page != page_count {
        format!(
            "\n\n<turbo-stream action=\"append\" target=\"account_users\"><template>{}</template></turbo-stream>",
            account_next_page_container(page.saturating_add(1))
        )
    } else {
        String::new()
    };
    let mut response = format!(
        "<turbo-stream action=\"replace\" target=\"next_page_container\"><template>{rows}</template></turbo-stream>{next}"
    )
    .into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        "text/vnd.turbo-stream.html; charset=utf-8".parse().unwrap(),
    );
    Ok(response)
}
async fn account_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    req: Request,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let tunneled_post =
        req.method() == Method::POST && matches!(req.uri().path(), "/account" | "/account.1");
    let mut logo = None;
    let f = if headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .starts_with("multipart/form-data")
    {
        let mut multipart = Multipart::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let mut values = HashMap::new();
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?
        {
            let field_name = field.name().unwrap_or("").to_string();
            if field_name == "account[logo]" || field_name == "logo" {
                let content_type = field
                    .content_type()
                    .unwrap_or("application/octet-stream")
                    .to_string();
                let bytes = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
                if !bytes.is_empty() {
                    if bytes.len() > 5 * 1024 * 1024 {
                        return Err(StatusCode::UNPROCESSABLE_ENTITY);
                    }
                    logo = Some((bytes.to_vec(), content_type));
                }
            } else if matches!(
                field_name.as_str(),
                "name"
                    | "account[name]"
                    | "_method"
                    | "restrict_room_creation"
                    | "account[settings][restrict_room_creation_to_administrators]"
            ) {
                values.insert(
                    field_name,
                    field.text().await.map_err(|_| StatusCode::BAD_REQUEST)?,
                );
            }
        }
        values
    } else {
        let RawForm(raw) = RawForm::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        fields(&raw).0
    };
    if tunneled_post && !matches!(f.get("_method").map(String::as_str), Some("patch" | "put")) {
        return Err(StatusCode::METHOD_NOT_ALLOWED);
    }
    let name = form_value(&f, "name", "account[name]");
    let restricted = form_value(
        &f,
        "restrict_room_creation",
        "account[settings][restrict_room_creation_to_administrators]",
    );
    if name.is_none() && restricted.is_none() && logo.is_none() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    if let Some(name) = name {
        if name.trim().is_empty() {
            return Err(StatusCode::UNPROCESSABLE_ENTITY);
        }
    }
    let new_logo = if let Some((bytes, content_type)) = logo {
        let dir = std::path::PathBuf::from(
            env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
        )
        .join("logos");
        std::fs::create_dir_all(&dir).map_err(db_err)?;
        let stored = Uuid::new_v4().to_string();
        std::fs::write(dir.join(&stored), bytes).map_err(db_err)?;
        Some((stored, content_type))
    } else {
        None
    };
    let mut db = match pool(&s) {
        Ok(db) => db,
        Err(error) => {
            if let Some((stored, _)) = &new_logo {
                remove_logo_files(stored);
            }
            return Err(error);
        }
    };
    let result = (|| -> Result<Option<String>, StatusCode> {
        let tx = db.transaction().map_err(db_err)?;
        if let Some(name) = name {
            tx.execute(
                "UPDATE accounts SET name=?1,updated_at=?2",
                params![name.trim(), now()],
            )
            .map_err(db_err)?;
        } else if new_logo.is_some() || restricted.is_some() {
            tx.execute("UPDATE accounts SET updated_at=?1", [now()])
                .map_err(db_err)?;
        }
        if let Some(restricted) = restricted {
            tx.execute(
                "UPDATE account_settings SET restrict_room_creation=?1 WHERE id=1",
                [matches!(restricted, "1" | "true" | "on")],
            )
            .map_err(db_err)?;
        }
        let old = if let Some((stored, content_type)) = &new_logo {
            let old = tx
                .query_row(
                    "SELECT stored_name FROM account_logos WHERE id=1",
                    [],
                    |r| r.get(0),
                )
                .optional()
                .map_err(db_err)?;
            tx.execute("INSERT INTO account_logos(id,stored_name,content_type) VALUES(1,?1,?2) ON CONFLICT(id) DO UPDATE SET stored_name=excluded.stored_name,content_type=excluded.content_type",params![stored,content_type]).map_err(db_err)?;
            old
        } else {
            None
        };
        tx.commit().map_err(db_err)?;
        Ok(old)
    })();
    if result.is_err() {
        if let Some((stored, _)) = &new_logo {
            remove_logo_files(stored)
        }
    }
    if let Some(old) = result? {
        remove_logo_files(&old)
    }
    Ok(found_redirect(&public_url(&headers, "/account/edit")))
}
async fn custom_styles_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let styles: Option<String> = pool(&s)?
        .query_row(
            "SELECT custom_styles FROM accounts LIMIT 1",
            [],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let translation = profile_translation_button(
        "Add custom CSS styles. Use Caution: you could break things.",
        [
            "Agrega estilos CSS personalizados. Usa precaución: podrías romper cosas.",
            "Ajoutez des styles CSS personnalisés. Utilisez avec précaution : vous pourriez casser des choses.",
            "कस्टम CSS स्टाइल जोड़ें। सावधानी बरतें: आप चीज़ों को तोड़ सकते हैं।",
            "Fügen Sie benutzerdefinierte CSS-Stile hinzu. Vorsicht: Sie könnten Dinge kaputt machen.",
            "Adicione estilos CSS personalizados. Use com cuidado: você pode quebrar coisas.",
            "カスタムCSSスタイルを追加。注意: サイトが壊れる可能性があります。",
        ],
    );
    let action = esc(&public_url(&headers, "/account/custom_styles"));
    Ok(render(
        "Custom styles",
        &format!(
            "<nav class='account-settings-nav custom-styles-nav'><a href='/account/edit' class='btn'><img aria-hidden='true' src='/assets/arrow-left-abe40556.svg' width='20' height='20'><span class='for-screen-reader'>Go Back</span></a></nav><section class='panel panel--wide custom-styles-panel txt-align-center flex flex-column position-relative' style='view-transition-name: custom-styles'><form class='flex flex-column gap' data-controller='form' data-action='keydown.ctrl+enter->form#submit keydown.meta+enter->form#submit' action='{action}' method='post'><input type='hidden' name='_method' value='patch'><div class='panel__button'>{translation}</div><div class='pad-inline-double margin-inline'><h1 class='margin-none'>Custom CSS</h1><p class='flex flex-wrap align-center justify-center gap margin-none-block-start' style='--column-gap: 0.5ch; --row-gap: 0'><span>Add custom CSS styles.</span><img src='/assets/alert.svg' class='flex-inline colorize--black' width='16' height='16' aria-hidden='true'><span>Use Caution: you could break things.</span></p></div><label class='flex align-start gap flex-item-grow'><textarea name='account[custom_styles]' id='account_custom_styles' class='input input--code txt--small' placeholder='Add CSS styles…' autocomplete='off' spellcheck='false' autocorrect='off' autocapitalize='off' rows='16'>\n{}</textarea></label><button class='btn btn--reversed center txt-large' type='submit'><img src='/assets/check-7897ff7e.svg' aria-hidden='true' width='20' height='20'><span class='for-screen-reader'>Save changes</span></button></form></section>",
            esc(styles.as_deref().unwrap_or(""))
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
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    tx.execute("UPDATE account_custom_styles SET css=?1 WHERE id=1", [css])
        .map_err(db_err)?;
    tx.execute(
        "UPDATE accounts SET custom_styles=?1,updated_at=?2 WHERE id=1",
        params![css, now()],
    )
    .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    if let Some(styles) = CUSTOM_STYLES.get() {
        *styles.write().unwrap() = Some(css.clone());
    }
    Ok(found_redirect(&public_url(&headers, "/account/custom_styles/edit")))
}
async fn custom_styles_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    raw: Bytes,
) -> AppResult {
    let form_method = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .filter(|value| value.starts_with("application/x-www-form-urlencoded"))
        .and_then(|_| fields(&raw).0.remove("_method"));
    let method = form_method.or_else(|| {
        headers
            .get("x-http-method-override")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    });
    match method.as_deref().map(str::to_ascii_uppercase).as_deref() {
        Some("PATCH" | "PUT") => custom_styles_update(State(s), headers, Form(fields(&raw).0)).await,
        _ => Err(StatusCode::NOT_FOUND),
    }
}
async fn logo_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
) -> AppResult {
    let small = query.get("size").is_some_and(|size| size == "small");
    let db = pool(&s)?;
    let account_version: Option<String> = db
        .query_row("SELECT updated_at FROM accounts WHERE id=1", [], |r| r.get(0))
        .optional()
        .map_err(db_err)?;
    let row: Option<(String, String)> = db
        .query_row(
            "SELECT stored_name,content_type FROM account_logos WHERE id=1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let tag_input = format!(
        "{}:{}",
        account_version.as_deref().unwrap_or(""),
        row.as_ref().map_or("", |(stored, _)| stored)
    );
    let digest = openssl::hash::hash(MessageDigest::md5(), tag_input.as_bytes()).map_err(db_err)?;
    let etag = format!(
        "W/\"{}\"",
        digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
    );
    if headers
        .get(header::IF_NONE_MATCH)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| {
            value.split(',').any(|candidate| {
                let candidate = candidate.trim();
                candidate == "*"
                    || candidate.trim_start_matches("W/") == etag.trim_start_matches("W/")
            })
        })
    {
        let mut response = StatusCode::NOT_MODIFIED.into_response();
        response.headers_mut().insert(header::ETAG, etag.parse().unwrap());
        response.headers_mut().insert(header::CACHE_CONTROL, "no-cache".parse().unwrap());
        return Ok(response);
    }
    let bytes = if let Some((stored, content_type)) = row {
        if safe_inline_image(&content_type) {
            logo_png_variant(&s, &stored, small).await
        } else {
            None
        }
    } else {
        None
    }
    .unwrap_or_else(|| {
        if small {
            include_bytes!("../static/icons/app-icon-192.png").to_vec()
        } else {
            include_bytes!("../static/icons/app-icon.png").to_vec()
        }
    });
    let mut response = bytes.into_response();
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, "image/png".parse().unwrap());
    response.headers_mut().insert(header::ETAG, etag.parse().unwrap());
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        "public, max-age=300, stale-while-revalidate=604800"
            .parse()
            .unwrap(),
    );
    response
        .headers_mut()
        .insert("x-content-type-options", "nosniff".parse().unwrap());
    Ok(response)
}
async fn logo_png_variant(s: &AppState, stored: &str, small: bool) -> Option<Vec<u8>> {
    if Uuid::parse_str(stored).is_err() {
        return None;
    }
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    )
    .join("logos");
    let cache = dir.join("variants");
    tokio::fs::create_dir_all(&cache).await.ok()?;
    let size = if small { 192 } else { 512 };
    let output = cache.join(format!("{stored}-{size}.png"));
    if tokio::fs::metadata(&output).await.is_err() {
        let _permit = s.variant_slots.acquire().await.ok()?;
        if tokio::fs::metadata(&output).await.is_err() {
            let nonce = Uuid::new_v4();
            let stage = cache.join(format!("{stored}-{size}-{nonce}.v"));
            let temporary = cache.join(format!("{stored}-{size}-{nonce}.png"));
            let resized = tokio::time::timeout(
                std::time::Duration::from_secs(10),
                tokio::process::Command::new("vips")
                    .arg("thumbnail")
                    .arg(dir.join(stored))
                    .arg(&stage)
                    .arg(size.to_string())
                    .args(["--height", &size.to_string(), "--size", "down"])
                    .kill_on_drop(true)
                    .output(),
            )
            .await
            .ok()
            .and_then(Result::ok)
            .is_some_and(|result| result.status.success());
            let sharpened = resized
                && tokio::time::timeout(
                    std::time::Duration::from_secs(10),
                    tokio::process::Command::new("vips")
                        .arg("conv")
                        .arg(&stage)
                        .arg(&temporary)
                        .arg("static/vips-sharpen-mask.txt")
                        .args(["--precision", "integer"])
                        .kill_on_drop(true)
                        .output(),
                )
                .await
                .ok()
                .and_then(Result::ok)
                .is_some_and(|result| result.status.success());
            let _ = tokio::fs::remove_file(&stage).await;
            if sharpened {
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
fn remove_logo_files(stored: &str) {
    if Uuid::parse_str(stored).is_err() {
        return;
    }
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    )
    .join("logos");
    let _ = std::fs::remove_file(dir.join(stored));
    for size in [192, 512] {
        let _ = std::fs::remove_file(dir.join("variants").join(format!("{stored}-{size}.png")));
    }
}
async fn logo_post(State(s): State<Arc<AppState>>, headers: HeaderMap, req: Request) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    if headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .starts_with("application/x-www-form-urlencoded")
    {
        let RawForm(raw) = RawForm::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        if fields(&raw).0.get("_method").map(String::as_str) == Some("delete") {
            return logo_delete(State(s), headers).await;
        }
        return Err(StatusCode::METHOD_NOT_ALLOWED);
    }
    let mut multipart = Multipart::from_request(req, &s)
        .await
        .map_err(|_| StatusCode::BAD_REQUEST)?;
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
        remove_logo_files(&old);
    }
    Ok(found_redirect(&public_url(&headers, "/account/edit")))
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
        remove_logo_files(&old);
    }
    Ok(found_redirect(&public_url(&headers, "/account/edit")))
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
    if !f.keys().any(|key| key.starts_with("user[") && key.ends_with(']')) {
        return Err(StatusCode::BAD_REQUEST);
    }
    let role = if f.get("user[role]").is_some_and(|role| role == "administrator") {
        1
    } else {
        0
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
    prior.ok_or(StatusCode::NOT_FOUND)?;
    db.execute(
        "UPDATE users SET role=?1,updated_at=?2 WHERE id=?3",
        params![role, now(), id],
    )
    .map_err(db_err)?;
    Ok(found_redirect(&public_url(&headers, "/account/edit")))
}
async fn user_role_update_alias(
    state: State<Arc<AppState>>,
    headers: HeaderMap,
    path: Path<i64>,
    Form(mut form): Form<HashMap<String, String>>,
) -> AppResult {
    if !form.contains_key("user[role]") {
        if let Some(role) = form.remove("role") {
            form.insert("user[role]".to_string(), role);
        }
    }
    user_role_update(state, headers, path, Form(form)).await
}
async fn user_admin_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(f): Form<HashMap<String, String>>,
) -> AppResult {
    match f.get("_method").map(String::as_str) {
        Some("patch" | "put") => user_role_update(State(s), headers, Path(id), Form(f)).await,
        Some("delete") => user_deactivate(State(s), headers, Path(id)).await,
        _ => Err(StatusCode::METHOD_NOT_ALLOWED),
    }
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
    let email: Option<Option<String>> = tx
        .query_row(
            "SELECT email_address FROM users WHERE id=?1 AND status=0 AND role!=2",
            [id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    let email = email.ok_or(StatusCode::NOT_FOUND)?;
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
    Ok(found_redirect(&public_url(&headers, "/account/edit")))
}
async fn join_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(code): Path<String>,
) -> AppResult {
    match user(&s, &headers) {
        Ok(_) => return Ok(found_redirect("/")),
        Err(StatusCode::UNAUTHORIZED) => {}
        Err(error) => return Err(error),
    }
    let db = pool(&s)?;
    let account: Option<(String, String)> = db
        .query_row(
            "SELECT name,updated_at FROM accounts WHERE join_code=?1",
            [&code],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (account_name, updated_at) = account.ok_or(StatusCode::NOT_FOUND)?;
    let owner: Option<(String, String)> = db
        .query_row(
            "SELECT name,email_address FROM users WHERE role=1 AND email_address IS NOT NULL ORDER BY id LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let help_contact = owner.map(|(name, email)| {
        let address = format!("mailto:\"{name}\" <{email}>");
        format!("<div class=\"txt-align-center margin-block-double full-width\"><a href=\"{}\" class=\"btn center\" title=\"Email {}\"><img src=\"/assets/lifebuoy-f31f26aa.svg\" aria-hidden=\"true\"><span>{}</span></a><div class=\"txt-align-center center margin-block txt-subtle\">Campfire&trade; version <span class=\"version-badge\">Rustfire</span></div></div>", esc(&address), esc(&name), esc(&email))
    }).unwrap_or_default();
    let logo_version: String = updated_at.chars().filter(char::is_ascii_digit).take(14).collect();
    let name_translation = profile_translation_button("Enter your name", [
        "Introduce tu nombre", "Entrez votre nom", "अपना नाम दर्ज करें",
        "Geben Sie Ihren Namen ein", "Insira seu nome", "お名前を入力してください",
    ]);
    let email_translation = profile_translation_button("Enter your email address", [
        "Introduce tu correo electrónico", "Entrez votre adresse courriel",
        "अपना ईमेल पता दर्ज करें", "Geben Sie Ihre E-Mail-Adresse ein",
        "Insira seu endereço de email", "メールアドレスを入力してください",
    ]);
    let password_translation = profile_translation_button("Enter your password", [
        "Introduce tu contraseña", "Saisissez votre mot de passe",
        "अपना पासवर्ड दर्ज करें", "Geben Sie Ihr Passwort ein",
        "Insira sua senha", "パスワードを入力してください",
    ]);
    let body = format!(
        r#"<form class="center" enctype="multipart/form-data" action="/join/{code}" accept-charset="UTF-8" method="post">
<section class="nametag u-relative"><div class="flex justify-center align-center pad-block"><img class="nametag__lanyard" aria-hidden="true" src="/assets/lanyard-945079e9.svg"></div>
<div class="nametag__inner flex flex-column gap"><fieldset class="flex flex-column center-block"><legend class="txt-align-center flex gap"><figure class="account-logo avatar "><img alt="Account logo" src="/account/logo?v={logo_version}" width="300" height="300"></figure><strong class="txt-large">{account_name}</strong></legend>
<label class="align-center center avatar__form gap" data-controller="upload-preview"><div class="btn input--file"><img aria-hidden="true" src="/assets/camera-927323b8.svg"><input class="input" accept="image/*" data-upload-preview-target="input" data-action="upload-preview#previewImage" type="file" name="user[avatar]" id="user_avatar"><span class="for-screen-reader">Upload avatar</span></div><div class="btn avatar input--file txt-xx-large"><img aria-hidden="true" data-upload-preview-target="image" src="/assets/default-avatar-1ee67b00.svg"><span class="for-screen-reader">Avatar</span></div></label></fieldset>
<div class="flex align-center gap">{name_translation}<label class="flex align-center gap flex-item-grow txt-large input input--actor"><input class="input" autocomplete="name" placeholder="Name" autofocus="autofocus" required="required" data-1p-ignore="true" type="text" name="user[name]" id="user_name"><img aria-hidden="true" class="colorize--black" src="/assets/person-da193438.svg" width="24" height="24"></label></div>
<div class="flex align-center gap">{email_translation}<label class="flex align-center gap flex-item-grow txt-large input input--actor"><input class="input" autocomplete="username" placeholder="Email address" required="required" type="email" name="user[email_address]" id="user_email_address"><img aria-hidden="true" class="colorize--black" src="/assets/email-6c595bc5.svg" width="24" height="24"></label></div>
<div class="flex align-center gap">{password_translation}<label class="flex align-center gap flex-item-grow txt-large input input--actor"><input class="input" autocomplete="new-password" placeholder="Password" required="required" maxlength="72" size="72" type="password" name="user[password]" id="user_password"><img aria-hidden="true" class="colorize--black" src="/assets/password-0896da4e.svg" width="24" height="24"></label></div>
<button name="button" type="submit" class="btn btn--reversed center txt-large"><img aria-hidden="true" src="/assets/check-7897ff7e.svg"><span class="for-screen-reader">Save</span></button></div></section></form>{help_contact}"#,
        code = esc(&code),
        account_name = esc(&account_name),
    );
    Ok(render_unauth_with_nav(
        "Sign up",
        &body,
        "<div class=\"flex-item-justify-end\"><a href=\"/session/new\" class=\"btn flex-item-justify-end\"><img aria-hidden=\"true\" src=\"/assets/login-keys-df926967.svg\"><span class=\"for-screen-reader\">Sign in</span></a></div>",
        Some(&logo_version),
    ))
}
async fn join_post(
    State(s): State<Arc<AppState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Path(code): Path<String>,
    req: Request,
) -> AppResult {
    match user(&s, &headers) {
        Ok(_) => return Ok(found_redirect("/")),
        Err(StatusCode::UNAUTHORIZED) => {}
        Err(error) => return Err(error),
    }
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
    drop(db);
    let SignupSubmission { name, email, password, avatar } = signup_submission(&s, &headers, req).await?;
    let mut db = pool(&s)?;
    let existing: bool = db
        .query_row("SELECT EXISTS(SELECT 1 FROM users WHERE email_address=?1)", [&email], |row| row.get(0))
        .map_err(db_err)?;
    if existing {
        let encoded = form_urlencoded::Serializer::new(String::new())
            .append_pair("email_address", &email)
            .finish();
        return Ok(found_redirect(&format!("/session/new?{encoded}")));
    }
    let t = now();
    let pw = hash(&password, DEFAULT_COST).map_err(db_err)?;
    let tx = db.transaction().map_err(db_err)?;
    if let Err(error) = tx.execute(
        "INSERT INTO users(name,email_address,password_digest,role,status,created_at,updated_at) VALUES(?1,?2,?3,0,0,?4,?4)",
        params![name.trim(), email, pw, t],
    ) {
        drop(tx);
        let duplicate: bool = db
            .query_row("SELECT EXISTS(SELECT 1 FROM users WHERE email_address=?1)", [&email], |row| row.get(0))
            .map_err(db_err)?;
        if duplicate {
            let encoded = form_urlencoded::Serializer::new(String::new())
                .append_pair("email_address", &email)
                .finish();
            return Ok(found_redirect(&format!("/session/new?{encoded}")));
        }
        return Err(db_err(error));
    }
    let uid = tx.last_insert_rowid();
    tx.execute("INSERT INTO memberships(room_id,user_id,involvement,created_at) SELECT id,?1,'mentions',?2 FROM rooms WHERE type='Rooms::Open'",params![uid,t]).map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    drop(db);
    if let Some((bytes, content_type)) = avatar {
        if let Err(error) = save_avatar(&s, uid, bytes, content_type) {
            pool(&s)?.execute("DELETE FROM users WHERE id=?1", [uid]).map_err(db_err)?;
            return Err(error);
        }
    }
    create_session(&s, uid, client_ip(&s.trusted_proxies, &headers, addr.ip()))
}
fn profile_translation_button(english: &str, translations: [&str; 6]) -> String {
    let mut entries = String::new();
    for (flag, phrase) in ["🇺🇸", "🇪🇸", "🇫🇷", "🇮🇳", "🇩🇪", "🇧🇷", "🇯🇵"]
        .into_iter()
        .zip(std::iter::once(english).chain(translations))
    {
        entries.push_str(&format!(
            "<dt>{flag}</dt><dd class='margin-none'>{}</dd>",
            esc(phrase)
        ));
    }
    format!(
        "<details class='position-relative' data-controller='popup' data-action='keydown.esc->popup#close toggle->popup#toggle click@document->popup#closeOnClickOutside' data-popup-orientation-top-class='popup-orientation-top'><summary class='btn' tabindex='-1'><img aria-hidden='true' class='color-icon' src='/assets/globe-8c54d23b.svg' width='20' height='20'><span class='for-screen-reader'>Translate</span></summary><div class='language-list-menu shadow' data-popup-target='menu'><dl class='language-list'>{entries}</dl></div></details>"
    )
}
fn profile_membership_item(id: i64, name: &str, kind: &str, involvement: &str, csrf: &str) -> String {
    let (next, label, icon) = if kind == "Rooms::Direct" {
        match involvement {
            "everything" => (
                "nothing",
                "Notifying about all messages",
                "notification-bell-everything-cde41b14.svg",
            ),
            _ => (
                "everything",
                "Notifications are off",
                "notification-bell-nothing-d8096c76.svg",
            ),
        }
    } else {
        match involvement {
            "everything" => (
                "nothing",
                "Notifying about all messages",
                "notification-bell-everything-cde41b14.svg",
            ),
            "nothing" => (
                "invisible",
                "Notifications are off",
                "notification-bell-nothing-d8096c76.svg",
            ),
            "invisible" => (
                "mentions",
                "Notifications are off and room invisible in sidebar",
                "notification-bell-invisible-8b495073.svg",
            ),
            _ => (
                "everything",
                "Notifying about @ mentions",
                "notification-bell-mentions-945d1b91.svg",
            ),
        }
    };
    let frame = if kind == "Rooms::Direct" {
        format!("involvement_rooms_direct_{id}")
    } else if kind == "Rooms::Closed" {
        format!("involvement_rooms_closed_{id}")
    } else {
        format!("involvement_rooms_open_{id}")
    };
    let label_id = frame.replacen("involvement_", "involvement_label_", 1);
    format!(
        "<li class='flex align-center gap margin-none min-width membership-item'><a class='overflow-ellipsis fill-shade txt-primary txt-undecorated' href='/rooms/{id}'><strong>{}</strong></a><hr class='separator' aria-hidden='true'><span class='txt-small'><turbo-frame id='{frame}'><form class='button_to' method='post' action='/rooms/{id}/involvement?involvement={next}'><input type='hidden' name='_method' value='put'><button role='checkbox' aria-checked='true' aria-labelledby='{label_id}' tabindex='0' class='btn {involvement}' type='submit'><img aria-hidden='true' src='/assets/{icon}' width='20' height='20'><span class='for-screen-reader' id='{label_id}'>{label}</span></button><input type='hidden' name='authenticity_token' value='{}'></form></turbo-frame></span></li>",
        esc(name), esc(csrf)
    )
}
async fn profile(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    let transfer = public_url(&headers, &transfer_link(&s, u.id)?);
    let transfer_qr = format!("/qr_code/{}", URL_SAFE.encode(transfer.as_bytes()));
    let transfer = html_escape::encode_double_quoted_attribute(&transfer);
    let db = pool(&s)?;
    let bio: String = db
        .query_row(
            "SELECT COALESCE(bio,'') FROM users WHERE id=?1",
            [u.id],
            |r| r.get(0),
        )
        .map_err(db_err)?;
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let avatar_url = avatar_path(avatar_key, u.id, &u.updated_at)?;
    let delete_avatar = if db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM avatars WHERE user_id=?1)",
            [u.id],
            |r| r.get::<_, bool>(0),
        )
        .map_err(db_err)?
    {
        format!(
            "<form class='button_to' method='post' action='{}'><input type='hidden' name='_method' value='delete'><button class='btn btn--negative txt-small avatar__delete-btn' type='submit'><img aria-hidden='true' src='/assets/minus-b31a1093.svg' width='20' height='20'><span class='for-screen-reader'>Delete avatar</span></button><input type='hidden' name='authenticity_token' value='{}'></form>",
            format!("/users/{}/avatar", u.id), esc(u.csrf_token.as_deref().unwrap_or(""))
        )
    } else {
        String::new()
    };
    let mut shared_memberships = String::new();
    let mut direct_memberships = String::new();
    let mut q = db.prepare("SELECT r.id,COALESCE(r.name,''),r.type,m.involvement FROM memberships m JOIN rooms r ON r.id=m.room_id WHERE m.user_id=?1 ORDER BY lower(r.name)").map_err(db_err)?;
    for row in q
        .query_map([u.id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })
        .map_err(db_err)?
    {
        let (rid, name, kind, involvement) = row.map_err(db_err)?;
        let display = if kind == "Rooms::Direct" {
            db.query_row("SELECT group_concat(name, ', ') FROM (SELECT u.name FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.room_id=?1 AND u.id!=?2 ORDER BY u.id)", params![rid,u.id], |r| r.get::<_,Option<String>>(0)).map_err(db_err)?.unwrap_or_else(||u.name.clone())
        } else {
            name
        };
        let item = profile_membership_item(rid, &display, &kind, &involvement, u.csrf_token.as_deref().unwrap_or(""));
        if kind == "Rooms::Direct" {
            direct_memberships.push_str(&item);
        } else {
            shared_memberships.push_str(&item);
        }
    }
    let membership_divider = if !shared_memberships.is_empty() && !direct_memberships.is_empty() {
        "<hr class='separator full-width' style='--border-style: solid'>"
    } else {
        ""
    };
    let name_translation = profile_translation_button(
        "Enter your name",
        [
            "Introduce tu nombre",
            "Entrez votre nom",
            "अपना नाम दर्ज करें",
            "Geben Sie Ihren Namen ein",
            "Insira seu nome",
            "お名前を入力してください",
        ],
    );
    let email_translation = profile_translation_button(
        "Enter your email address",
        [
            "Introduce tu correo electrónico",
            "Entrez votre adresse courriel",
            "अपना ईमेल पता दर्ज करें",
            "Geben Sie Ihre E-Mail-Adresse ein",
            "Insira seu endereço de email",
            "メールアドレスを入力してください",
        ],
    );
    let password_translation = profile_translation_button(
        "Change password",
        [
            "Cambiar contraseña",
            "Changer le mot de passe",
            "पासवर्ड बदलें",
            "Passwort ändern",
            "Alterar senha",
            "パスワードを変更",
        ],
    );
    let bio_translation = profile_translation_button(
        "Enter a few words about yourself.",
        [
            "Ingresa algunas palabras sobre ti mismo.",
            "Saisissez quelques mots à propos de vous-même.",
            "अपने बारे में कुछ शब्द लिखें.",
            "Geben Sie ein paar Worte über sich selbst ein.",
            "Insira alguma palavras sobre você.",
            "ご自分について簡単に記入してください。",
        ],
    );
    let name = html_escape::encode_double_quoted_attribute(&u.name);
    let email = html_escape::encode_double_quoted_attribute(&u.email);
    let csrf = esc(u.csrf_token.as_deref().unwrap_or(""));
    let back = headers.get(header::REFERER).and_then(|value| value.to_str().ok())
        .filter(|value| *value != public_url(&headers, "/users/me/profile"))
        .unwrap_or("/");
    let nav = format!("<div class='flex-item-justify-start'><a class='btn' href='{}'><img aria-hidden='true' src='/assets/arrow-left-abe40556.svg' width='20' height='20'><span class='for-screen-reader'>Go Back</span></a></div><div class='flex-item-justify-end'><form data-controller='sessions' action='/session' accept-charset='UTF-8' method='post'><input type='hidden' name='_method' value='delete'><input type='hidden' name='authenticity_token' value='{csrf}'><input type='hidden' name='push_subscription_endpoint' id='push_subscription_endpoint' data-sessions-target='pushSubscriptionEndpoint'><button class='btn' data-action='sessions#logout:prevent'><img aria-hidden='true' src='/assets/logout-a6131db1.svg'><span class='for-screen-reader'>Log out</span></button></form></div>", esc(back));
    let body = format!(
            r#"<section class='panel flex flex-column gap' style='view-transition-name: avatar-{}'>
<details class='notifications-help pwa__instructions hide-in-pwa' data-controller='pwa-install' data-pwa-install-prompting-class='pwa--can-install' data-notifications-target='details'><summary class='btn'><img aria-hidden='true' src='/assets/external/install-f762b3be.svg' width='20' height='20'><strong>Install Rustfire as a web app.</strong><img aria-hidden='true' class='disclosure' src='/assets/disclosure-26d63471.svg' width='10' height='10'></summary><p>Some platforms require you to install Rustfire as a web app to receive push notifications.</p><div class='margin-block-start txt-align-center pwa__installer'><hr class='separator margin-block'><button class='btn btn--reversed center' data-action='pwa-install#promptInstall'><img aria-hidden='true' src='/assets/external/install-f762b3be.svg'> Install now</button></div></details>
<div class='align-center center avatar__form gap' data-controller='upload-preview'><form class='txt-medium' data-controller='form' enctype='multipart/form-data' action='/users/me/profile' accept-charset='UTF-8' method='post'><input type='hidden' name='_method' value='patch'><label class='btn input--file'><img aria-hidden='true' src='/assets/camera-927323b8.svg' width='20' height='20'><input id='file' class='input' accept='image/*' data-upload-preview-target='input' data-action='upload-preview#previewImage change->form#submit' type='file' name='user[avatar]'><span class='for-screen-reader'>Upload avatar</span></label></form><form data-controller='form' enctype='multipart/form-data' action='/users/me/profile' accept-charset='UTF-8' method='post'><input type='hidden' name='_method' value='patch'><label class='btn avatar input--file txt-xx-large'><img aria-hidden='true' data-upload-preview-target='image' src='{avatar_url}' width='300' height='300'><input id='file' class='input' accept='image/*' data-upload-preview-target='input' data-action='upload-preview#previewImage change->form#submit' type='file' name='user[avatar]'><span class='for-screen-reader'>Avatar</span></label></form>{delete_avatar}</div>
<form data-controller='form' action='/users/me/profile' accept-charset='UTF-8' method='post'><input type='hidden' name='_method' value='patch'><div class='flex flex-column gap'><div class='flex align-center gap'>{name_translation}<label class='flex align-center gap flex-item-grow input input--actor'><input class='input txt-large ' autocomplete='name' placeholder='Enter your name' autofocus='autofocus' required='required' data-1p-ignore='true' type='text' value='{name}' name='user[name]' id='user_name'><img aria-hidden='true' src='/assets/person-da193438.svg' width='24' height='24' class='colorize--black'></label></div><div class='flex align-center gap'>{email_translation}<label class='flex align-center gap flex-item-grow input input--actor'><input class='input txt-large' value='{email}' autocomplete='username' placeholder='Enter your email address' type='email' name='user[email_address]' id='user_email_address'><img aria-hidden='true' src='/assets/email-6c595bc5.svg' width='24' height='24' class='colorize--black'></label></div><div class='flex align-center gap'>{password_translation}<label class='flex align-center gap flex-item-grow input input--actor'><input class='input txt-large' autocomplete='new-password' placeholder='Change password' maxlength='72' size='72' type='password' name='user[password]' id='user_password'><img aria-hidden='true' src='/assets/password-0896da4e.svg' width='24' height='24' class='colorize--black'></label></div><div class='flex align-start gap'>{bio_translation}<label class='flex align--center gap flex-item--grow input input--actor'><textarea class='input txt-large' placeholder='A few words about yourself…' maxlength='200' rows='3' name='user[bio]' id='user_bio'>
{}</textarea><img aria-hidden='true' src='/assets/bio-567a6005.svg' width='24' height='24' class='colorize--black'></label></div><button class='btn btn--reversed center txt-large' type='submit'><img aria-hidden='true' src='/assets/check-7897ff7e.svg' width='20' height='20'><span class='for-screen-reader'>Save changes</span></button></div></form>
<div class='margin-block pad-inline pad-block fill-shade border-radius'><menu class='flex flex-column gap margin-none pad'>{shared_memberships}{membership_divider}{direct_memberships}</menu></div>
<fieldset><legend class='gap'><img aria-hidden='true' src='/assets/laptop-cf35e6d4.svg' width='36' height='36' class='colorize--black'><img aria-hidden='true' src='/assets/transfer-3ec4f61b.svg' width='36' height='36' class='colorize--black'><img aria-hidden='true' src='/assets/mobile-phone-d0d2301d.svg' width='36' height='36' class='colorize--black'></legend><div class='flex flex-column gap'><label for='session_transfer_url' class='for-screen-reader'>Use this link to login automatically on another device</label><input type='text' class='input' value='{transfer}' id='session_transfer_url' readonly><div class='flex align-center center gap'><a class='btn' data-lightbox-target='image' data-action='lightbox#open' data-lightbox-url-value='{transfer_qr}' href='{transfer_qr}'><span class='for-screen-reader'>Show auto-login QR code</span><img aria-hidden='true' src='/assets/qr-code-dac3b273.svg' width='20' height='20' class='colorize--black'></a><button class='btn' data-controller='copy-to-clipboard' data-action='copy-to-clipboard#copy' data-copy-to-clipboard-success-class='btn--success' data-copy-to-clipboard-content-value='{transfer}'><span class='for-screen-reader'>Copy auto-login link</span><img aria-hidden='true' src='/assets/copy-paste-4c379063.svg' width='20' height='20' class='flex-item-no-shrink colorize--black'></button><button class='btn' hidden='hidden' data-controller='web-share' data-action='web-share#share' data-web-share-url-value='{transfer}' data-web-share-title-value='Your sign-in link' data-web-share-text-value='This is your own private sign-in URL, DO NOT SHARE IT. Use it to sign-in on another device or if you get locked out.'><span class='for-screen-reader'>Share auto-login link</span><img aria-hidden='true' src='/assets/share-bf28da4f.svg' width='20' height='20' class='flex-item-no-shrink colorize--black'></button></div></div></fieldset></section>"#,
            u.id,
            esc(&bio)
        );
    let body = body.replace("<input type='hidden' name='_method' value='patch'>", &format!("<input type='hidden' name='_method' value='patch'><input type='hidden' name='authenticity_token' value='{csrf}'>"));
    Ok(render_source_page(&u.name, &body, &nav, Some(&u), u.csrf_token.as_deref().unwrap_or("")))
}
async fn profile_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    req: Request,
) -> AppResult {
    let u = user(&s, &headers)?;
    let mut avatar = None;
    let f = if headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .starts_with("multipart/form-data")
    {
        let mut multipart = Multipart::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let mut values = HashMap::new();
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?
        {
            let field_name = field.name().unwrap_or("").to_string();
            if matches!(field_name.as_str(), "user[avatar]" | "avatar") {
                let content_type = field
                    .content_type()
                    .unwrap_or("application/octet-stream")
                    .to_string();
                let bytes = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
                if bytes.is_empty() {
                    return Err(StatusCode::UNPROCESSABLE_ENTITY);
                }
                avatar = Some((bytes.to_vec(), content_type));
            } else if matches!(
                field_name.as_str(),
                "name"
                    | "user[name]"
                    | "email_address"
                    | "user[email_address]"
                    | "password"
                    | "user[password]"
                    | "bio"
                    | "user[bio]"
                    | "_method"
            ) {
                values.insert(
                    field_name,
                    field.text().await.map_err(|_| StatusCode::BAD_REQUEST)?,
                );
            }
        }
        values
    } else {
        let RawForm(raw) = RawForm::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        fields(&raw).0
    };
    let name = form_value(&f, "name", "user[name]").unwrap_or(&u.name);
    if name.trim().is_empty() {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let email = match form_value(&f, "email_address", "user[email_address]") {
        Some(e) if e.trim().is_empty() => None,
        Some(e) if e.contains('@') => Some(e.trim().to_string()),
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
    if let Some((bytes, content_type)) = avatar {
        save_avatar(&s, u.id, bytes, content_type)?;
    }
    Ok(found_redirect(&public_url(&headers, "/users/me/profile")))
}
async fn avatar_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(token): Path<String>,
) -> AppResult {
    avatar_response(s, headers, token).await
}
async fn avatar_get_me(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    avatar_response(s, headers, "me".to_owned()).await
}
async fn avatar_response(s: Arc<AppState>, headers: HeaderMap, token: String) -> AppResult {
    match user(&s, &headers) {
        Ok(_) => {}
        Err(StatusCode::UNAUTHORIZED) => return Ok(found_redirect("/session/new")),
        Err(error) => return Err(error),
    }
    let id = avatar_id_from_token(&s.avatar_signing_key, &token)
        .or_else(|| {
            s.imported_avatar_signing_key
                .as_deref()
                .and_then(|key| avatar_id_from_token(key, &token))
        })
        .ok_or(StatusCode::NOT_FOUND)?;
    let db = pool(&s)?;
    let account: Option<(String, i64, String)> = db
        .query_row(
            "SELECT name,role,updated_at FROM users WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(db_err)?;
    let (name, role, updated_at) = account.ok_or(StatusCode::NOT_FOUND)?;
    let row: Option<(String, String)> = db
        .query_row(
            "SELECT stored_name,content_type FROM avatars WHERE user_id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let tag_input = format!(
        "{id}:{updated_at}:{}",
        row.as_ref().map_or("", |(stored, _)| stored)
    );
    let digest = openssl::hash::hash(MessageDigest::md5(), tag_input.as_bytes()).map_err(db_err)?;
    let etag = format!(
        "W/\"{}\"",
        digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
    );
    if headers
        .get(header::IF_NONE_MATCH)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| {
            value.split(',').any(|candidate| {
                candidate.trim() == "*"
                    || candidate.trim().trim_start_matches("W/") == etag.trim_start_matches("W/")
            })
        })
    {
        let mut response = StatusCode::NOT_MODIFIED.into_response();
        response
            .headers_mut()
            .insert(header::ETAG, etag.parse().unwrap());
        response
            .headers_mut()
            .insert(header::CACHE_CONTROL, "no-cache".parse().unwrap());
        return Ok(response);
    }
    if let Some((stored, content_type)) = row {
        if variable_avatar_image(&content_type) {
            if let Some(data) = avatar_webp_variant(&s, &stored).await {
                let mut response = data.into_response();
                response
                    .headers_mut()
                    .insert(header::CONTENT_TYPE, "image/webp".parse().unwrap());
                response
                    .headers_mut()
                    .insert(header::ETAG, etag.parse().unwrap());
                response.headers_mut().insert(
                    header::CACHE_CONTROL,
                    "max-age=1800, public, stale-while-revalidate=604800"
                        .parse()
                        .unwrap(),
                );
                response
                    .headers_mut()
                    .insert("x-content-type-options", "nosniff".parse().unwrap());
                return Ok(response);
            }
        }
    }
    let body = if role == 2 {
        include_str!("../static/icons/default-bot-avatar.svg").to_string()
    } else {
        avatar_initials_svg(id, &name)
    };
    let mut response = body.into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        "image/svg+xml; charset=utf-8".parse().unwrap(),
    );
    response
        .headers_mut()
        .insert(header::ETAG, etag.parse().unwrap());
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        "max-age=1800, public, stale-while-revalidate=604800"
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
            let generation = Uuid::new_v4();
            let intermediate = cache.join(format!("{stored}-{generation}.v"));
            let temporary = cache.join(format!("{stored}-{generation}.webp"));
            let thumbnail = tokio::time::timeout(
                std::time::Duration::from_secs(10),
                tokio::process::Command::new("vips")
                    .arg("thumbnail")
                    .arg(&input)
                    .arg(&intermediate)
                    .args(["512", "--height", "512", "--size", "down"])
                    .kill_on_drop(true)
                    .output(),
            )
            .await
            .ok()
            .and_then(Result::ok);
            let result = if thumbnail
                .as_ref()
                .is_some_and(|result| result.status.success())
            {
                // image_processing's Vips.resize_to_limit applies this mild
                // sharpening convolution after thumbnailing. The two CLI
                // operations match Campfire's generated WebP bytes.
                tokio::time::timeout(
                    std::time::Duration::from_secs(10),
                    tokio::process::Command::new("vips")
                        .arg("conv")
                        .arg(&intermediate)
                        .arg(&temporary)
                        .arg("static/avatar-sharpen.mat")
                        .args(["--precision", "integer"])
                        .kill_on_drop(true)
                        .output(),
                )
                .await
                .ok()
                .and_then(Result::ok)
            } else {
                None
            };
            let _ = tokio::fs::remove_file(&intermediate).await;
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
            let bytes = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
            if bytes.is_empty() {
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
    let content_type = sniff_upload_content_type(&bytes, &content_type).to_string();
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
    Ok(found_redirect(&public_url(&headers, "/users/me/profile")))
}
async fn avatar_delete_post(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    RawForm(raw): RawForm,
) -> AppResult {
    if fields(&raw).0.get("_method").map(String::as_str) != Some("delete") {
        return Err(StatusCode::METHOD_NOT_ALLOWED);
    }
    avatar_delete(State(s), headers).await
}
async fn user_show(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
) -> AppResult {
    let u = user(&s, &headers)?;
    let db = pool(&s)?;
    let (name, bio, email, role, status, updated_at): (String, String, String, i64, i64, String) = db
        .query_row(
            "SELECT name,COALESCE(bio,''),COALESCE(email_address,''),role,status,updated_at FROM users WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)),
        )
        .optional()
        .map_err(db_err)?
        .ok_or(StatusCode::NOT_FOUND)?;
    let has_logo: bool = db
        .query_row("SELECT EXISTS(SELECT 1 FROM account_logos WHERE id=1)", [], |row| row.get(0))
        .map_err(db_err)?;
    let name_html = esc(&name);
    let csrf = esc(u.csrf_token.as_deref().unwrap_or(""));
    let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
    let avatar = avatar_path(avatar_key, id, &updated_at)?;
    let back = headers.get(header::REFERER).and_then(|value| value.to_str().ok())
        .filter(|value| *value != public_url(&headers, &format!("/users/{id}")))
        .unwrap_or("/");
    let edit = if u.id == id {
        "<a href='/users/me/profile' class='btn'><img aria-hidden='true' src='/assets/pencil-cf9d28aa.svg'><span class='for-screen-reader'>Edit my profile</span></a>"
    } else { "" };
    let nav = format!("<div class='flex-item-justify-start'><a class='btn' href='{}'><img aria-hidden='true' src='/assets/arrow-left-abe40556.svg' width='20' height='20'><span class='for-screen-reader'>Go Back</span></a></div><div class='flex align-center gap flex-item-justify-end'>{edit}</div>", esc(back));
    let ping = if status == 0 {
        let class = if role == 2 { "btn btn--primary full-width txt--large" } else { "btn btn--reversed full-width txt-large" };
        let image = if role == 2 { String::new() } else { format!(" aria-hidden='true' aria-label='Ping {name_html}'") };
        format!("<form class='button_to' method='post' action='/rooms/directs?user_ids%5B%5D={id}'><button class='{class}' type='submit'><img{image} src='/assets/messages-9395d503.svg'></button><input type='hidden' name='authenticity_token' value='{csrf}'></form>")
    } else { String::new() };
    let transfer = if role != 2 && status == 0 && is_admin(&u) {
        let url = public_url(&headers, &transfer_link(&s, id)?);
        let qr = format!("/qr_code/{}", URL_SAFE.encode(url.as_bytes()));
        let url = html_escape::encode_double_quoted_attribute(&url);
        let share_label = if u.id == id {
            "<label for='session_transfer_url' class='for-screen-reader'>Use this link to login automatically on another device</label>".to_string()
        } else {
            "<div class='flex align-center gap justify-center'><img aria-hidden='true' class='flex-item-no-shrink colorize--black' src='/assets/crown-00d190cb.svg' width='16' height='16'><label for='session_transfer_url'>Share to get them back into their account</label></div>".to_string()
        };
        format!("<hr class='margin-block-start borderless'><fieldset><legend class='gap'><img aria-hidden='true' class='colorize--black' src='/assets/laptop-cf35e6d4.svg' width='36' height='36'><img aria-hidden='true' class='colorize--black' src='/assets/transfer-3ec4f61b.svg' width='36' height='36'><img aria-hidden='true' class='colorize--black' src='/assets/mobile-phone-d0d2301d.svg' width='36' height='36'></legend><div class='flex flex-column gap'>{share_label}<input type='text' class='input' value='{url}' id='session_transfer_url' readonly><div class='flex align-center center gap'><a class='btn' data-lightbox-target='image' data-action='lightbox#open' data-lightbox-url-value='{qr}' href='{qr}'><span class='for-screen-reader'>Show auto-login QR code</span><img aria-hidden='true' class='colorize--black' src='/assets/qr-code-dac3b273.svg' width='20' height='20'></a><button class='btn' data-controller='copy-to-clipboard' data-action='copy-to-clipboard#copy' data-copy-to-clipboard-success-class='btn--success' data-copy-to-clipboard-content-value='{url}'><span class='for-screen-reader'>Copy auto-login link</span><img aria-hidden='true' class='flex-item-no-shrink colorize--black' src='/assets/copy-paste-4c379063.svg' width='20' height='20'></button><button class='btn' hidden='hidden' data-controller='web-share' data-action='web-share#share' data-web-share-url-value='{url}' data-web-share-text-value='This is your own private sign-in URL, DO NOT SHARE IT. Use it to sign-in on another device or if you get locked out.' data-web-share-title-value='Your sign-in link'><span class='for-screen-reader'>Share auto-login link</span><img aria-hidden='true' class='flex-item-no-shrink colorize--black' src='/assets/share-bf28da4f.svg' width='20' height='20'></button></div></div></fieldset>")
    } else { String::new() };
    let ban = if role != 2 && status != 1 && is_admin(&u) && u.id != id {
        if status == 2 {
            format!("<div class='margin-block-start'><form class='button_to' method='post' action='/users/{id}/ban'><input type='hidden' name='_method' value='delete'><button class='btn btn--negative full-width' data-turbo-confirm='Are you sure you want to remove the ban on this user?' type='submit'><img aria-hidden='true' aria-label='Remove Ban {name_html}' src='/assets/cancel-c6dfeb78.svg'><span>Remove ban</span></button><input type='hidden' name='authenticity_token' value='{csrf}'></form></div>")
        } else {
            format!("<div class='margin-block-start'><form class='button_to' method='post' action='/users/{id}/ban'><button class='btn full-width' data-turbo-confirm='Are you sure you want to ban this user? This will log them out, delete their messages, and block their IP addresses.' type='submit'><img aria-hidden='true' aria-label='Ban {name_html}' src='/assets/cancel-c6dfeb78.svg'><span>Ban {name_html}</span></button><input type='hidden' name='authenticity_token' value='{csrf}'></form></div>")
        }
    } else { String::new() };
    let identity = if role == 2 {
        if status == 0 {
            format!("<div class='pad-double--inline push--inline push--block-start'>{ping}</div>")
        } else {
            format!("<div class='pad-double--inline push--inline push--block-start'><div>{name_html} is no longer on this account</div></div>")
        }
    } else if status == 1 {
        format!("<div><h1 class='txt-x-large margin-none'>{name_html}</h1><div>{name_html} is no longer on this account</div></div>")
    } else {
        let email_html = if is_admin(&u) {
            format!("<div><a href='mailto:{}'>{}</a></div>", esc(&email), esc(&email))
        } else { String::new() };
        let ping = if status == 0 { format!("<div class='pad-inline-double margin-inline margin-block-start'>{ping}</div>") } else { String::new() };
        format!("<div class='flex flex-column gap' style='--row-gap: calc(var(--block-space) / 3)'><h1 class='txt-x-large txt-tight-lines margin-none'>{name_html}</h1>{email_html}<div>{}</div></div>{ping}{transfer}{ban}", esc(&bio))
    };
    let banned = if status == 2 { "banned" } else { "" };
    let body = format!("<section class='panel txt-align-center'><div class='flex flex-column gap {banned}'><div class='avatar txt-xx-large center' style='background: white'><img alt='Profile avatar' class='avatar' src='{}'></div>{identity}</div></section>", esc(&avatar));
    Ok(render_source_page_sections(&name, &body, &nav, "", "", "", if has_logo { "account-has-logo" } else { "" }, "", Some(&u), u.csrf_token.as_deref().unwrap_or("")))
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
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    let status: Option<i64> = tx
        .query_row(
            "SELECT status FROM users WHERE id=?1",
            [id],
            |r| r.get(0),
        )
        .optional()
        .map_err(db_err)?;
    if status.is_none() {
        return Err(StatusCode::NOT_FOUND);
    }
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
        if !ip.parse::<IpAddr>().map(public_ip).unwrap_or(false) {
            return Err(StatusCode::UNPROCESSABLE_ENTITY);
        }
        tx.execute(
            "INSERT INTO bans(user_id,ip_address) VALUES(?1,?2)",
            params![id, ip],
        )
        .map_err(db_err)?;
    }
    tx.execute("DELETE FROM sessions WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM session_transfers WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute(
        "UPDATE users SET status=2,updated_at=?1 WHERE id=?2",
        params![now(), id],
    )
    .map_err(db_err)?;
    tx.execute(
        "INSERT INTO background_jobs(kind,user_id,created_at) VALUES('remove_banned_content',?1,?2)",
        params![id, now()],
    )
    .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    let _ = s.revoked_users.send(id);
    Ok(found_redirect(&format!("/users/{id}")))
}

async fn user_ban_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    body: Bytes,
) -> AppResult {
    let (values, _) = fields(&body);
    if values.get("_method").map(String::as_str) == Some("delete") {
        user_unban(State(s), headers, Path(id)).await
    } else {
        user_ban(State(s), headers, Path(id)).await
    }
}

fn process_banned_content_batch(s: &AppState) -> Result<bool, StatusCode> {
    let mut db = pool(s)?;
    let pending: bool = db
        .query_row("SELECT EXISTS(SELECT 1 FROM background_jobs WHERE kind='remove_banned_content')", [], |row| row.get(0))
        .map_err(db_err)?;
    if !pending {
        return Ok(false);
    }
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(db_err)?;
    let job: Option<(i64, i64)> = tx
        .query_row(
            "SELECT id,user_id FROM background_jobs WHERE kind='remove_banned_content' ORDER BY id LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(db_err)?;
    let Some((job_id, user_id)) = job else {
        return Ok(false);
    };
    let mut statement = tx
        .prepare("SELECT id,room_id,client_message_id FROM messages WHERE creator_id=?1 ORDER BY id LIMIT 50")
        .map_err(db_err)?;
    let messages = statement
        .query_map([user_id], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, String>(2)?)))
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(statement);
    let mut attachment_files = Vec::new();
    let mut inline_blobs = Vec::new();
    for (mid, rid, _) in &messages {
        let attachment: Option<String> = tx
            .query_row("SELECT stored_name FROM attachments WHERE message_id=?1", [mid], |row| row.get(0))
            .optional()
            .map_err(db_err)?;
        if let Some(stored) = attachment {
            attachment_files.push(stored);
        }
        inline_blobs.extend(inline_blob_ids(&tx, "SELECT blob_id FROM inline_embeds WHERE message_id=?1", *mid)?);
        tx.execute("DELETE FROM messages WHERE id=?1", [mid]).map_err(db_err)?;
        touch_room(&tx, *rid)?;
    }
    if messages.len() < 50 {
        tx.execute("DELETE FROM background_jobs WHERE id=?1", [job_id]).map_err(db_err)?;
    }
    tx.commit().map_err(db_err)?;
    purge_orphan_inline_blobs(&db, &inline_blobs)?;
    for stored in attachment_files {
        remove_attachment_files(&stored);
    }
    for (mid, rid, client_message_id) in messages {
        s.events.send(Event {
            room_id: rid,
            payload: json!({"type":"message_deleted","room_id":rid,"id":mid,"client_message_id":client_message_id}).to_string(),
        });
    }
    Ok(true)
}

async fn run_background_jobs(s: Arc<AppState>) {
    loop {
        let state = s.clone();
        let pause = match tokio::task::spawn_blocking(move || process_banned_content_batch(&state)).await {
            Ok(Ok(true)) => std::time::Duration::from_millis(10),
            Ok(Ok(false)) => std::time::Duration::from_millis(250),
            Ok(Err(error)) => {
                eprintln!("Background job failed: {error}");
                std::time::Duration::from_secs(1)
            }
            Err(error) => {
                eprintln!("Background worker failed: {error}");
                std::time::Duration::from_secs(1)
            }
        };
        tokio::time::sleep(pause).await;
    }
}
fn claim_webhook_job(s: &AppState) -> Result<Option<(i64, i64, i64)>, StatusCode> {
    let mut db = pool(s)?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(db_err)?;
    let job = tx.query_row(
        "SELECT id,bot_id,message_id FROM webhook_jobs WHERE claimed_at IS NULL OR claimed_at < unixepoch()-30 ORDER BY id LIMIT 1",
        [],
        |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, i64>(2)?)),
    ).optional().map_err(db_err)?;
    if let Some((id, _, _)) = job {
        tx.execute("UPDATE webhook_jobs SET claimed_at=unixepoch() WHERE id=?1", [id]).map_err(db_err)?;
    }
    tx.commit().map_err(db_err)?;
    Ok(job)
}
fn webhook_job_payload(s: &AppState, bot_id: i64, message_id: i64) -> Result<Option<(String, String, String, Option<String>, Option<String>, ChatMessage)>, StatusCode> {
    let db = pool(s)?;
    let details = db.query_row(
        "SELECT u.name,u.bot_token,w.url,m.room_id,r.name,m.body_source FROM users u JOIN webhooks w ON w.user_id=u.id JOIN messages m ON m.id=?2 JOIN rooms r ON r.id=m.room_id WHERE u.id=?1 AND u.role=2 AND u.bot_token IS NOT NULL",
        params![bot_id,message_id],
        |row| Ok((row.get::<_, String>(0)?,row.get::<_, String>(1)?,row.get::<_, String>(2)?,row.get::<_, i64>(3)?,row.get::<_, Option<String>>(4)?,row.get::<_, Option<String>>(5)?)),
    ).optional().map_err(db_err)?;
    let Some((name,token,url,room_id,room_name,source)) = details else { return Ok(None); };
    drop(db);
    let message = match message_by_id(s, room_id, message_id) {
        Ok(message) => message,
        Err(StatusCode::NOT_FOUND) => return Ok(None),
        Err(error) => return Err(error),
    };
    let source_html = match source {
        Some(source) => Some(action_text_webhook_html(&source)),
        None if message.attachment.is_some() && message.body.is_empty() => None,
        None => Some(message.body.clone()),
    };
    Ok(Some((name,format!("{bot_id}-{token}"),url,room_name,source_html,message)))
}
fn complete_webhook_job(s: &AppState, job_id: i64, error: Option<&str>) -> Result<(), StatusCode> {
    let mut db = pool(s)?;
    let tx = db.transaction().map_err(db_err)?;
    if let Some(error) = error {
        let error = error.chars().take(2048).collect::<String>();
        tx.execute(
            "INSERT INTO failed_webhook_jobs(job_id,bot_id,message_id,error,created_at,failed_at) SELECT id,bot_id,message_id,?2,created_at,?3 FROM webhook_jobs WHERE id=?1",
            params![job_id,error,now()],
        ).map_err(db_err)?;
    }
    tx.execute("DELETE FROM webhook_jobs WHERE id=?1", [job_id]).map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    Ok(())
}
async fn run_webhook_jobs(s: Arc<AppState>) {
    loop {
        let Ok(permit) = s.webhook_slots.clone().try_acquire_owned() else {
            tokio::time::sleep(std::time::Duration::from_millis(25)).await;
            continue;
        };
        let state = s.clone();
        let claim = tokio::task::spawn_blocking(move || claim_webhook_job(&state)).await;
        match claim {
            Ok(Ok(Some((job_id, bot_id, message_id)))) => {
                let state = s.clone();
                tokio::spawn(async move {
                    let _permit = permit;
                    let payload_state = state.clone();
                    let payload = tokio::task::spawn_blocking(move || webhook_job_payload(&payload_state, bot_id, message_id)).await;
                    let outcome = match payload {
                        Ok(Ok(Some((name,key,url,room_name,source_html,message)))) => {
                            deliver_webhook(state.clone(), bot_id, name, key, url, room_name, source_html, message).await.err()
                        }
                        Ok(Ok(None)) => Some("Webhook job target missing".to_string()),
                        Ok(Err(error)) => {
                            eprintln!("Webhook job payload failed: {error}");
                            return;
                        }
                        Err(error) => {
                            eprintln!("Webhook job worker failed: {error}");
                            return;
                        }
                    };
                    if let Some(error) = &outcome {
                        eprintln!("Webhook delivery failed: {error}");
                    }
                    if let Err(error) = complete_webhook_job(&state, job_id, outcome.as_deref()) {
                        eprintln!("Webhook job completion failed: {error}");
                    }
                });
            }
            Ok(Ok(None)) => {
                drop(permit);
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
            error => {
                eprintln!("Webhook job claim failed: {error:?}");
                drop(permit);
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            }
        }
    }
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
        .execute("UPDATE users SET status=0,updated_at=?1 WHERE id=?2", params![now(), id])
        .map_err(db_err)?;
    if changed == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    tx.execute("DELETE FROM bans WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    Ok(found_redirect(&format!("/users/{id}")))
}
async fn autocomplete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult {
    let _autocomplete_slot = s.autocomplete_slots.acquire().await.map_err(db_err)?;
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
    let base_url = public_url(&headers, "");
    let cached = {
        let cache = s.autocomplete_cache.read().unwrap();
        users
            .iter()
            .map(|(id, _, updated_at)| {
                cache
                    .get(id)
                    .filter(|entry| entry.updated_at == *updated_at)
                    .cloned()
            })
            .collect::<Vec<_>>()
    };
    let mut uncached = Vec::new();
    let rows = users
        .into_iter()
        .zip(cached)
        .map(|((id, name, updated_at), entry)| {
            let entry = match entry {
                Some(entry) => entry,
                None => {
                    let entry = CachedAutocompleteUser {
                        updated_at: updated_at.clone(),
                        avatar_path: avatar_path(avatar_key, id, &updated_at)?,
                        sgid: mention_sgid(signing_key, id).map_err(db_err)?,
                    };
                    uncached.push((id, entry.clone()));
                    entry
                }
            };
            Ok(json!({
                "value": id,
                "name": esc(&name),
                "avatar_url": format!("{base_url}{}", entry.avatar_path),
                "sgid": entry.sgid,
            }))
        })
        .collect::<Result<Vec<_>, StatusCode>>()?;
    if !uncached.is_empty() {
        s.autocomplete_cache.write().unwrap().extend(uncached);
    }
    Ok(Json(rows).into_response())
}
async fn bots_get(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let db = pool(&s)?;
    let mut q = db
        .prepare("SELECT id,name,bot_token,updated_at FROM users WHERE role=2 AND bot_token IS NOT NULL AND status=0 ORDER BY LOWER(name)")
        .map_err(db_err)?;
    let rows = q
        .query_map([], |r| {
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
    let mut room_query = db.prepare("SELECT m.user_id,r.id,r.name FROM memberships m JOIN rooms r ON r.id=m.room_id JOIN users u ON u.id=m.user_id WHERE u.role=2 AND u.status=0 AND u.bot_token IS NOT NULL AND r.type!='Rooms::Direct' ORDER BY m.user_id,LOWER(r.name)").map_err(db_err)?;
    let room_rows = room_query
        .query_map([], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
            ))
        })
        .map_err(db_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_err)?;
    drop(room_query);
    let mut bot_rooms: HashMap<i64, Vec<(i64, String)>> = HashMap::new();
    for (bot_id, room_id, room_name) in room_rows {
        bot_rooms
            .entry(bot_id)
            .or_default()
            .push((room_id, room_name));
    }
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let mut list = String::new();
    for (id, name, token, updated_at) in rows {
        let key = format!("{id}-{token}");
        let avatar = avatar_path(avatar_key, id, &updated_at)?;
        list.push_str(&format!("<li class='bot-row flex flex-column gap flush fill-shade border-radius pad-block pad-inline-double'><div class='flex align-center gap'><figure class='avatar flex-item--no-shrink' style='--avatar-size: 2.65em;'><a href='/users/{id}' title='{}' class='btn avatar' data-turbo-frame='_top'><img src='{}' aria-hidden='true' alt='' width='48' height='48' loading='lazy'></a></figure><div class='min-width'><div class='overflow-ellipsis txt-large'><strong>{}</strong></div></div><a href='/account/bots/{id}/edit' class='btn flex-item-justify-end' style='view-transition-name: chat-bot-{id}'><img src='/static/icons/pencil.svg' aria-hidden='true' alt='' width='20' height='20'><span class='for-screen-reader'>Edit {}</span></a></div>",esc(&name),esc(&avatar),esc(&name),esc(&name)));
        for (room_id, room_name) in bot_rooms.remove(&id).unwrap_or_default() {
            let endpoint = public_url(&headers, &format!("/rooms/{room_id}/{key}/messages"));
            let text_line = format!("curl -d 'Hello!' {endpoint}");
            let upload_line = format!("curl -F \"attachment=@/path/to/file\" {endpoint}");
            list.push_str(&format!("<fieldset class='gap max-width pad border border-radius'><legend class='min-width txt-align-start pad-inline'><strong class='overflow-ellipsis'>{}</strong></legend>{}{}</fieldset>",esc(&room_name),bot_command_html(&text_line,"messages-outlined.svg","curl command for posting messages","Copy message command"),bot_command_html(&upload_line,"attachment.svg","curl command for posting attachments","Copy attachment command")));
        }
        list.push_str("</li>");
    }
    Ok(render(
        "Chat bots",
        &format!(
            "<p class='bot-back'><a href='/account/edit' aria-label='Back to account'>Back</a></p><section class='form-card bot-index panel panel--wide txt-align-center flex flex-column position-relative' style='view-transition-name: chat-bots'><div class='flex align-center gap'><div class='pad-inline-double center'><h1 class='margin-none'>Chat bots</h1><p class='margin-none-block-start'>With Chat bots, other sites and services can post updates directly to Campfire.</p><a href='/account/bots/new' class='btn btn--reversed txt-large' aria-label='Add a chat bot'><img src='/static/icons/bot.svg' aria-hidden='true' alt='' width='20' height='20'><img src='/static/icons/add.svg' aria-hidden='true' alt='' width='20' height='20'></a></div></div><div class='pad-inline pad-block-start'><menu class='flex flex-column gap margin-none pad'>{list}</menu></div></section>"
        ),
        Some(&u),
    ))
}
fn bot_command_html(command: &str, icon: &str, input_label: &str, copy_label: &str) -> String {
    format!(
        "<div class='flex align-center gap bot-command'><img src='/static/icons/{icon}' aria-hidden='true' alt='' width='24' height='24' class='colorize--black'><div class='flex-item-grow'><input type='text' class='input full-width fill-white' value='{}' aria-label='{input_label}' readonly></div><div class='txt-small'><button class='btn' data-controller='copy-to-clipboard' data-action='copy-to-clipboard#copy' data-copy-to-clipboard-success-class='btn--success' data-copy-to-clipboard-content-value='{}'><img src='/static/icons/copy-paste.svg' aria-hidden='true' alt='' width='20' height='20'><span class='for-screen-reader'>{copy_label}</span></button></div></div>",
        esc(command),
        esc(command)
    )
}
async fn bot_new(State(s): State<Arc<AppState>>, headers: HeaderMap) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    Ok(render(
        "New chat bot",
        &format!(
            "<p><a href='/account/bots' aria-label='Back to chat bots'>Back</a></p><section class='panel form-card'>{}</section>",
            bot_form_html(
                "/account/bots",
                "",
                "",
                "/static/icons/default-bot-avatar.svg",
                false
            )
        ),
        Some(&u),
    ))
}
fn bot_form_html(action: &str, name: &str, webhook_url: &str, avatar: &str, edit: bool) -> String {
    let method = if edit {
        "<input type='hidden' name='_method' value='patch'>"
    } else {
        ""
    };
    format!(
        "<form action='{}' method='post' enctype='multipart/form-data' class='bot-form flex flex-column gap'>{method}<h1 class='for-screen-reader'>Chat Bot Setup</h1><label class='align-center center avatar__form gap' data-controller='upload-preview'><span class='btn input--file'><img src='/static/icons/camera.svg' alt='' aria-hidden='true' width='20' height='20'><input type='file' name='user[avatar]' class='input' accept='image/*' data-upload-preview-target='input' data-action='upload-preview#previewImage'><span class='for-screen-reader'>Upload bot avatar</span></span><span class='avatar input--file txt-xx-large' style='--avatar-size: var(--btn-size);'><img src='{}' alt='Bot avatar' width='48' height='48' data-upload-preview-target='image'></span></label><div class='flex align-center gap'><label class='flex align-center gap flex-item-grow txt-large input input--actor'><input type='text' name='user[name]' class='input' autocomplete='name' placeholder='Name the bot' autofocus required data-1p-ignore='true' value='{}'><img src='/static/icons/bot.svg' alt='' aria-hidden='true' width='24' height='24'></label></div><div class='flex align-center gap'><label class='flex align-center gap flex-item-grow txt-large input input--actor'><input type='url' name='user[webhook_url]' class='input' placeholder='Webhook URL' value='{}'><img src='/static/icons/web.svg' alt='' aria-hidden='true' width='24' height='24'></label></div><button class='btn btn--reversed center txt-large' type='submit' aria-label='Save changes'><img src='/static/icons/check.svg' alt='' aria-hidden='true' width='20' height='20'><span class='for-screen-reader'>Save changes</span></button></form>",
        esc(action),
        esc(avatar),
        esc(name),
        esc(webhook_url)
    )
}
async fn bot_fields(
    s: &Arc<AppState>,
    headers: &HeaderMap,
    req: Request,
) -> Result<(HashMap<String, String>, Option<(Vec<u8>, String)>), StatusCode> {
    let mut avatar = None;
    let f = if headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .starts_with("multipart/form-data")
    {
        let mut multipart = Multipart::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let mut values = HashMap::new();
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?
        {
            let field_name = field.name().unwrap_or("").to_string();
            if field_name == "user[avatar]" || field_name == "avatar" {
                let content_type = field
                    .content_type()
                    .unwrap_or("application/octet-stream")
                    .to_string();
                let bytes = field.bytes().await.map_err(|_| StatusCode::BAD_REQUEST)?;
                if !bytes.is_empty() {
                    avatar = Some((bytes.to_vec(), content_type));
                }
            } else if field_name == "name"
                || field_name == "user[name]"
                || field_name == "webhook_url"
                || field_name == "user[webhook_url]"
            {
                values.insert(
                    field_name,
                    field.text().await.map_err(|_| StatusCode::BAD_REQUEST)?,
                );
            }
        }
        values
    } else {
        let RawForm(raw) = RawForm::from_request(req, &s)
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        fields(&raw).0
    };
    Ok((f, avatar))
}
async fn bot_create(State(s): State<Arc<AppState>>, headers: HeaderMap, req: Request) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let (f, avatar) = bot_fields(&s, &headers, req).await?;
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
    let bot_token = generate_bot_token()?;
    db.execute(
        "UPDATE users SET bot_token=?1 WHERE id=?2",
        params![bot_token, id],
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
    if let Some((bytes, content_type)) = avatar {
        if let Err(error) = save_avatar(&s, id, bytes, content_type) {
            db.execute("DELETE FROM users WHERE id=?1", [id])
                .map_err(db_err)?;
            return Err(error);
        }
    }
    Ok(found_redirect(&public_url(&headers, "/account/bots")))
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
    let bot: Option<(String, String, String, String)> = db.query_row(
        "SELECT u.name,u.bot_token,COALESCE(w.url,''),u.updated_at FROM users u LEFT JOIN webhooks w ON w.user_id=u.id WHERE u.id=?1 AND u.role=2 AND u.status=0 AND u.bot_token IS NOT NULL",
        [id],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)),
    ).optional().map_err(db_err)?;
    let (name, _token, webhook_url, updated_at) = bot.ok_or(StatusCode::NOT_FOUND)?;
    let avatar_key = s.imported_avatar_signing_key.as_deref().unwrap_or(&s.avatar_signing_key);
    let avatar = avatar_path(avatar_key, id, &updated_at)?;
    Ok(render(
        "Edit bot",
        &format!(
            "<p><a href='/account/bots' aria-label='Back to chat bots'>Back</a></p><section class='panel form-card' style='view-transition-name: chat-bot-{id}'>{}<hr class='separator full-width margin-block-double'><div class='flex align-center gap justify-space-between'><form method='post' action='/account/bots/{id}'><input type='hidden' name='_method' value='delete'><button class='btn txt--small btn--negative' aria-label='Delete this chat bot'>Delete this chat bot</button></form><form method='post' action='/account/bots/{id}/key'><input type='hidden' name='_method' value='put'><button class='btn full-width txt--small btn--negative' aria-label='Generate a new key'>Generate a new key</button></form></div></section>",
            bot_form_html(
                &format!("/account/bots/{id}"),
                &name,
                &webhook_url,
                &avatar,
                true
            ),
        ),
        Some(&u),
    ))
}
async fn bot_update(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    req: Request,
) -> AppResult {
    let u = user(&s, &headers)?;
    if !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let (f, avatar) = bot_fields(&s, &headers, req).await?;
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
    if let Some((bytes, content_type)) = avatar {
        save_avatar(&s, id, bytes, content_type)?;
    }
    Ok(found_redirect(&public_url(&headers, "/account/bots")))
}
async fn bot_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    req: Request,
) -> AppResult {
    let multipart = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .starts_with("multipart/form-data");
    if multipart {
        return bot_update(State(s), headers, Path(id), req).await;
    }
    let (parts, body) = req.into_parts();
    let bytes = to_bytes(body, 256 * 1024)
        .await
        .map_err(|_| StatusCode::PAYLOAD_TOO_LARGE)?;
    let (form, _) = fields(&bytes);
    if form.get("_method").map(String::as_str) == Some("delete") {
        return bot_delete(State(s), headers, Path(id)).await;
    }
    if matches!(
        form.get("_method").map(String::as_str),
        Some("patch" | "put")
    ) {
        return bot_update(
            State(s),
            headers,
            Path(id),
            Request::from_parts(parts, Body::from(bytes)),
        )
        .await;
    }
    Err(StatusCode::METHOD_NOT_ALLOWED)
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
    let new_token = generate_bot_token()?;
    let changed = pool(&s)?.execute(
        "UPDATE users SET bot_token=?1,updated_at=?2 WHERE id=?3 AND role=2 AND status=0 AND bot_token IS NOT NULL",
        params![new_token,now(),id],
    ).map_err(db_err)?;
    if changed == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    Ok(found_redirect(&public_url(&headers, "/account/bots")))
}
async fn bot_key_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<i64>,
    raw: Bytes,
) -> AppResult {
    let form_method = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .filter(|value| value.starts_with("application/x-www-form-urlencoded"))
        .and_then(|_| fields(&raw).0.remove("_method"));
    let method = form_method.or_else(|| {
        headers
            .get("x-http-method-override")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    });
    match method.as_deref().map(str::to_ascii_uppercase).as_deref() {
        Some("PATCH" | "PUT") => bot_key_rotate(State(s), headers, Path(id)).await,
        _ => Err(StatusCode::NOT_FOUND),
    }
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
    let mut db = pool(&s)?;
    let tx = db.transaction().map_err(db_err)?;
    let changed = tx.execute("UPDATE users SET status=1,updated_at=?1 WHERE id=?2 AND role=2 AND status=0 AND bot_token IS NOT NULL",params![now(),id]).map_err(db_err)?;
    if changed == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    tx.execute("DELETE FROM memberships WHERE user_id=?1 AND room_id IN (SELECT id FROM rooms WHERE type!='Rooms::Direct')",[id]).map_err(db_err)?;
    tx.execute("DELETE FROM push_subscriptions WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM searches WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.execute("DELETE FROM sessions WHERE user_id=?1", [id])
        .map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    Ok(found_redirect(&public_url(&headers, "/account/bots")))
}
async fn bot_messages_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, key)): Path<(i64, String)>,
    Query(q): Query<Paging>,
) -> AppResult {
    let u = bot_api_actor(&s, &headers, &key)?;
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
    let body = messages
        .iter()
        .map(|m| bot_message_json(&s, m, &headers, &db))
        .collect::<Result<Vec<_>, _>>()?;
    let json = serde_json::to_string(&body)
        .map_err(db_err)?
        .replace('&', "\\u0026")
        .replace('<', "\\u003c")
        .replace('>', "\\u003e");
    let mut r = ([(header::CONTENT_TYPE, "application/json")], json).into_response();
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
    let u = bot_api_actor(&s, &headers, &key)?;
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
    let m = insert_message(&s, &u, rid, &body, None, attachment, true, Some(&headers), false)?;
    if s.webhooks_enabled {
        if let Err(error) = enqueue_webhooks(&s, &m) {
            eprintln!("Rustfire webhook dispatch error: {error}");
        }
    }
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
    let u = bot_api_actor(&s, &headers, &key)?;
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
    if creator != u.id && !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let updated_at = now();
    let updated_at_ns =
        message_timestamp_ns(&updated_at).ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    let old_inline = inline_blob_ids(&db, "SELECT blob_id FROM inline_embeds WHERE message_id=?1", mid)?;
    let found=db.execute("UPDATE messages SET body=?1,body_html=NULL,body_source=NULL,updated_at=?2,updated_at_ns=?3 WHERE id=?4 AND room_id=?5",params![body,updated_at,updated_at_ns,mid,rid]).map_err(db_err)?;
    if found == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    db.execute("DELETE FROM inline_embeds WHERE message_id=?1", [mid]).map_err(db_err)?;
    purge_orphan_inline_blobs(&db, &old_inline)?;
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
    headers: HeaderMap,
    Path((rid, key, mid)): Path<(i64, String, i64)>,
) -> AppResult {
    let u = bot_api_actor(&s, &headers, &key)?;
    room_for(&s, u.id, rid)?;
    let db = pool(&s)?;
    let target: Option<(i64, Option<String>, String)> = db.query_row("SELECT m.creator_id,a.stored_name,m.client_message_id FROM messages m LEFT JOIN attachments a ON a.message_id=m.id WHERE m.id=?1 AND m.room_id=?2", params![mid,rid], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?))).optional().map_err(db_err)?;
    let (creator, attachment, client_message_id) = target.ok_or(StatusCode::NOT_FOUND)?;
    if creator != u.id && !is_admin(&u) {
        return Err(StatusCode::FORBIDDEN);
    }
    let inline_blobs = inline_blob_ids(&db, "SELECT blob_id FROM inline_embeds WHERE message_id=?1", mid)?;
    let found = db
        .execute(
            "DELETE FROM messages WHERE id=?1 AND room_id=?2",
            params![mid, rid],
        )
        .map_err(db_err)?;
    if found == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    purge_orphan_inline_blobs(&db, &inline_blobs)?;
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
    if let Some(row) = row {
        return Ok(row);
    }
    db.query_row("SELECT m.room_id,b.filename,b.content_type,b.stored_name FROM inline_blobs b JOIN inline_embeds e ON e.blob_id=b.id JOIN messages m ON m.id=e.message_id WHERE b.id=?1 ORDER BY m.id LIMIT 1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional().map_err(db_err)?.ok_or(StatusCode::NOT_FOUND)
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
    Path((token, filename)): Path<(String, String)>,
    Query(query): Query<HashMap<String, String>>,
) -> AppResult {
    let id = blob_id_from_token(&s.blob_signing_key, &token)
        .or_else(|| {
            s.imported_blob_signing_key
                .as_deref()
                .and_then(|key| blob_id_from_token(key, &token))
        })
        .ok_or(StatusCode::NOT_FOUND)?;
    let (filename, content_type, stored) = match attachment_record_unchecked(&s, id) {
        Ok((_, filename, content_type, stored)) => (filename, content_type, stored),
        Err(StatusCode::NOT_FOUND) => {
            let db = pool(&s)?;
            let row: Option<(String, String, String, bool)> = db.query_row(
                "SELECT filename,content_type,storage_key,uploaded FROM direct_upload_blobs WHERE id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            ).optional().map_err(db_err)?;
            let (actual_filename, content_type, storage_key, uploaded) = row.ok_or(StatusCode::NOT_FOUND)?;
            if !uploaded || filename != actual_filename { return Err(StatusCode::NOT_FOUND); }
            let token = disk_token(&s.blob_signing_key, "blob_key", json!({"key":storage_key,"content_type":content_type,"service_name":"local"})).map_err(db_err)?;
            return Ok(found_redirect(&public_url(&headers, &format!("/rails/active_storage/disk/{token}/{}", encoded_blob_filename(&actual_filename)))));
        }
        Err(error) => return Err(error),
    };
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
async fn direct_upload_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> AppResult {
    user(&s, &headers)?;
    let blob = payload.get("blob").ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    let filename = blob.get("filename").and_then(Value::as_str).filter(|name| !name.is_empty() && name.len() <= 255 && !name.contains('/') && !name.contains('\\')).ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    let byte_size = blob.get("byte_size").and_then(Value::as_i64).filter(|size| (0..=25 * 1024 * 1024).contains(size)).ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    let checksum = blob.get("checksum").and_then(Value::as_str).filter(|value| STANDARD.decode(value).is_ok_and(|digest| digest.len() == 16)).ok_or(StatusCode::UNPROCESSABLE_ENTITY)?;
    let content_type = blob.get("content_type").and_then(Value::as_str).filter(|value| !value.is_empty() && value.len() <= 255).unwrap_or("application/octet-stream");
    let storage_key = direct_upload_storage_key().map_err(db_err)?;
    let created_at = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
    let mut db = pool(&s)?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(db_err)?;
    let id: i64 = tx.query_row("SELECT MAX(1000000000000,COALESCE((SELECT MAX(id)+1 FROM direct_upload_blobs),1000000000000),COALESCE((SELECT MAX(id)+1 FROM attachments),1000000000000),COALESCE((SELECT MAX(id)+1 FROM inline_blobs),1000000000000))", [], |r| r.get(0)).map_err(db_err)?;
    tx.execute("INSERT INTO direct_upload_blobs(id,storage_key,filename,content_type,byte_size,checksum,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![id,storage_key,filename,content_type,byte_size,checksum,created_at]).map_err(db_err)?;
    tx.commit().map_err(db_err)?;
    let upload_token = disk_token(&s.blob_signing_key, "blob_token", json!({"key":storage_key,"content_type":content_type,"content_length":byte_size,"checksum":checksum,"service_name":"local"})).map_err(db_err)?;
    Ok(Json(json!({
        "id":id,"byte_size":byte_size,"checksum":checksum,"content_type":content_type,
        "created_at":created_at,"filename":filename,"key":storage_key,"metadata":{},"service_name":"local",
        "attachable_sgid":blob_attachable_sgid(&s.mention_signing_key,id).map_err(db_err)?,
        "signed_id":blob_token(&s.blob_signing_key,id).map_err(db_err)?,
        "direct_upload":{"url":public_url(&headers,&format!("/rails/active_storage/disk/{upload_token}")),"headers":{"Content-Type":content_type}}
    })).into_response())
}
async fn direct_upload_put(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(token): Path<String>,
    body: Bytes,
) -> AppResult {
    user(&s, &headers)?;
    let data = disk_token_data(&s.blob_signing_key, &token, "blob_token").ok_or(StatusCode::NOT_FOUND)?;
    let storage_key = data.get("key").and_then(Value::as_str).ok_or(StatusCode::NOT_FOUND)?;
    let content_type = data.get("content_type").and_then(Value::as_str).ok_or(StatusCode::NOT_FOUND)?;
    let expected_length = data.get("content_length").and_then(Value::as_i64).ok_or(StatusCode::NOT_FOUND)?;
    let checksum = data.get("checksum").and_then(Value::as_str).ok_or(StatusCode::NOT_FOUND)?;
    if headers.get(header::CONTENT_TYPE).and_then(|value| value.to_str().ok()) != Some(content_type)
        || body.len() as i64 != expected_length
        || STANDARD.encode(openssl::hash::hash(MessageDigest::md5(), &body).map_err(db_err)?.as_ref()) != checksum {
        return Err(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let exists: bool = pool(&s)?.query_row("SELECT EXISTS(SELECT 1 FROM direct_upload_blobs WHERE storage_key=?1 AND content_type=?2 AND byte_size=?3 AND checksum=?4)",params![storage_key,content_type,expected_length,checksum],|r|r.get(0)).map_err(db_err)?;
    if !exists { return Err(StatusCode::NOT_FOUND); }
    let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
    tokio::fs::create_dir_all(&dir).await.map_err(db_err)?;
    let target = std::path::Path::new(&dir).join(storage_key);
    let temporary = std::path::Path::new(&dir).join(format!("{storage_key}.upload-{}", Uuid::new_v4()));
    tokio::fs::write(&temporary, &body).await.map_err(db_err)?;
    if let Err(error) = tokio::fs::rename(&temporary, &target).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(db_err(error));
    }
    pool(&s)?.execute("UPDATE direct_upload_blobs SET uploaded=1 WHERE storage_key=?1",[storage_key]).map_err(db_err)?;
    Ok(StatusCode::NO_CONTENT.into_response())
}
async fn direct_upload_disk_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((token, filename)): Path<(String, String)>,
) -> AppResult {
    let data = disk_token_data(&s.blob_signing_key, &token, "blob_key").ok_or(StatusCode::NOT_FOUND)?;
    let storage_key = data.get("key").and_then(Value::as_str).ok_or(StatusCode::NOT_FOUND)?;
    let row: Option<(String,String,bool)> = pool(&s)?.query_row("SELECT filename,content_type,uploaded FROM direct_upload_blobs WHERE storage_key=?1",[storage_key],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(db_err)?;
    let (actual_filename, content_type, uploaded) = row.ok_or(StatusCode::NOT_FOUND)?;
    if !uploaded || filename != actual_filename { return Err(StatusCode::NOT_FOUND); }
    let dir = env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into());
    serve_attachment(&std::path::Path::new(&dir).join(storage_key),&actual_filename,&content_type,&headers,false).await
}
async fn signed_representation_get(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((token, variation, _filename)): Path<(String, String, String)>,
) -> AppResult {
    let key = [
        Some(s.blob_signing_key.as_slice()),
        s.imported_blob_signing_key.as_deref(),
    ]
    .into_iter()
    .flatten()
    .find(|key| blob_id_from_token(key, &token).is_some())
    .ok_or(StatusCode::NOT_FOUND)?;
    let id = blob_id_from_token(key, &token).ok_or(StatusCode::NOT_FOUND)?;
    let (_, filename, content_type, stored) = attachment_record_unchecked(&s, id)?;
    let matches = |candidate: &str| variation.len() == candidate.len() && memcmp::eq(variation.as_bytes(), candidate.as_bytes());
    let (format, kind) = if let Some(mut format) = image_format(&content_type) {
        if content_type == "image/jpeg"
            && [
                image_variation_token_sized(key, "jpeg", 1024, 768)?,
                image_variation_token_sized(key, "jpeg", 800, 600)?,
                image_variation_token(key, "jpeg")?,
            ]
            .iter()
            .any(|candidate| matches(candidate))
        {
            format = "jpeg";
        }
        let inline = image_variation_token_sized(key, format, 1024, 768)?;
        let gallery = image_variation_token_sized(key, format, 800, 600)?;
        if matches(&inline) {
            (format, "inline")
        } else if matches(&gallery) {
            (format, "inline-gallery")
        } else {
            (format, "thumb")
        }
    } else if content_type == "application/pdf" {
        let thumb = inline_preview_variation_token(key, 1200, 800)?;
        let gallery = inline_preview_variation_token(key, 800, 600)?;
        ("png", if matches(&thumb) { "pdf-thumb" } else if matches(&gallery) { "inline-pdf-gallery" } else { "inline-pdf" })
    } else if safe_inline_video(&content_type) {
        let inline = inline_preview_variation_token(key, 1024, 768)?;
        let gallery = inline_preview_variation_token(key, 800, 600)?;
        if matches(&inline) {
            ("jpeg", "inline-video")
        } else if matches(&gallery) {
            ("jpeg", "inline-video-gallery")
        } else {
            ("webp", "poster")
        }
    } else {
        return Err(StatusCode::NOT_FOUND);
    };
    let gallery = kind.ends_with("gallery");
    let (width, height) = if kind == "pdf-thumb" { (1200, 800) } else if gallery { (800, 600) } else { (1024, 768) };
    let expected = if kind == "pdf-thumb" || kind.starts_with("inline-pdf") || kind.starts_with("inline-video") {
        inline_preview_variation_token(key, width, height)?
    } else if kind.starts_with("inline") {
        image_variation_token_sized(key, format, width, height)?
    } else {
        image_variation_token(key, format)?
    };
    if variation.len() != expected.len() || !memcmp::eq(variation.as_bytes(), expected.as_bytes()) {
        return Err(StatusCode::NOT_FOUND);
    }
    let dir = std::path::PathBuf::from(
        env::var("RUSTFIRE_UPLOAD_DIR").unwrap_or_else(|_| "data/uploads".into()),
    );
    let input = dir.join(&stored);
    let output = dir
        .join("variants")
        .join(format!("{stored}-{kind}.{format}"));
    if tokio::fs::metadata(&output).await.is_err() {
        let _permit = s.variant_slots.acquire().await.map_err(db_err)?;
        if tokio::fs::metadata(&output).await.is_err() {
            let stored_copy = stored.clone();
            if kind == "poster" {
                let _ = tokio::task::spawn_blocking(move || {
                    analyze_video_and_poster(&input, &stored_copy)
                })
                .await;
            } else if kind == "pdf-thumb" || kind.starts_with("inline-pdf") {
                let output_copy = output.clone();
                let _ = tokio::task::spawn_blocking(move || {
                    generate_inline_pdf_variant(&input, &output_copy, width, height)
                })
                .await;
            } else if kind.starts_with("inline-video") {
                let output_copy = output.clone();
                let _ = tokio::task::spawn_blocking(move || {
                    generate_inline_video_variant(&input, &output_copy, width, height)
                })
                .await;
            } else if kind.starts_with("inline") {
                let output_copy = output.clone();
                let _ = tokio::task::spawn_blocking(move || {
                    generate_inline_image_variant(&input, &output_copy, width, height)
                })
                .await;
            } else {
                let format_copy = format.to_string();
                let _ = tokio::task::spawn_blocking(move || {
                    analyze_image_and_thumbnail(&input, &stored_copy, "thumb", &format_copy)
                })
                .await;
            }
        }
    }
    if tokio::fs::metadata(&output).await.is_err() {
        return Err(StatusCode::NOT_FOUND);
    }
    let response_type = if content_type == "image/tiff" {
        "image/png"
    } else if kind == "pdf-thumb" || kind.starts_with("inline-pdf") {
        "image/png"
    } else if kind.starts_with("inline-video") {
        "image/jpeg"
    } else if kind == "poster" {
        "image/webp"
    } else {
        &content_type
    };
    serve_attachment(&output, &filename, response_type, &headers, true).await
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
    for format in ["png", "jpeg", "gif", "webp", "avif"] {
        let _ = std::fs::remove_file(
            dir.join("variants")
                .join(format!("{stored}-thumb.{format}")),
        );
        let _ = std::fs::remove_file(
            dir.join("variants")
                .join(format!("{stored}-inline.{format}")),
        );
        let _ = std::fs::remove_file(
            dir.join("variants")
                .join(format!("{stored}-inline-gallery.{format}")),
        );
    }
    let _ = std::fs::remove_file(dir.join("variants").join(format!("{stored}-inline-pdf.png")));
    let _ = std::fs::remove_file(dir.join("variants").join(format!("{stored}-inline-video.jpeg")));
    let _ = std::fs::remove_file(dir.join("variants").join(format!("{stored}-inline-pdf-gallery.png")));
    let _ = std::fs::remove_file(dir.join("variants").join(format!("{stored}-inline-video-gallery.jpeg")));
}
fn inline_blob_ids(db: &rusqlite::Connection, sql: &str, id: i64) -> Result<Vec<i64>, StatusCode> {
    let mut query = db.prepare(sql).map_err(db_err)?;
    query.query_map([id], |row| row.get::<_, i64>(0)).map_err(db_err)?
        .collect::<Result<Vec<_>, _>>().map_err(db_err)
}
fn purge_orphan_inline_blobs(db: &rusqlite::Connection, ids: &[i64]) -> Result<(), StatusCode> {
    for id in ids {
        let stored: Option<String> = db.query_row("SELECT stored_name FROM inline_blobs WHERE id=?1 AND NOT EXISTS(SELECT 1 FROM inline_embeds WHERE blob_id=?1)", [id], |row| row.get(0))
            .optional().map_err(db_err)?;
        let Some(stored) = stored else { continue; };
        let deleted = db.execute("DELETE FROM inline_blobs WHERE id=?1 AND NOT EXISTS(SELECT 1 FROM inline_embeds WHERE blob_id=?1)", [id])
            .map_err(db_err)?;
        if deleted > 0 {
            remove_attachment_files(&stored);
        }
    }
    Ok(())
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
    let content = f.get("boost[content]").ok_or(StatusCode::BAD_REQUEST)?;
    db.execute(
        "INSERT INTO boosts(id,message_id,booster_id,content,created_at) VALUES((SELECT MAX(last_id,(SELECT COALESCE(MAX(id),0) FROM boosts))+1 FROM id_sequences WHERE name='boosts'),?1,?2,?3,?4)",
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
    Ok(found_redirect(&format!("/messages/{mid}/boosts")))
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
    drop(db);
    let message = message_by_id(&s, rid, mid)?;
    Ok(render("Boosts", &boost_area_html(&s, &message), Some(&u)))
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
    let frame_request = headers
        .get("Turbo-Frame")
        .and_then(|value| value.to_str().ok())
        == Some(frame_id.as_str());
    let avatar_key = s
        .imported_avatar_signing_key
        .as_deref()
        .unwrap_or(&s.avatar_signing_key);
    let avatar = avatar_path(avatar_key, u.id, &u.updated_at)
        .unwrap_or_else(|_| format!("/users/{}/avatar", u.id));
    let frame_id_html = esc(&frame_id);
    let client_id_html = esc(&client_id);
    let csrf = esc(u.csrf_token.as_deref().unwrap_or(""));
    let frame = format!(
            "<turbo-frame id='{frame_id_html}'><div class='boost flex-inline postion--relative max-width fill-white' style='--column-gap: var(--inline-space-half)'><form class='boost__form flex align-center gap expanded' data-controller='form scroll-into-view' data-turbo-frame='boosting_message_{client_id_html}' data-action='keydown.esc-&gt;form#cancel' action='/messages/{mid}/boosts' accept-charset='UTF-8' method='post'><input type='hidden' name='authenticity_token' value='{csrf}'><label class='boost__form-label flex gap' style='--column-gap: 0.7ch;' role='button' tabindex='0' aria-label='Add a boost'><figure class='avatar boost__avatar flex-item-no-shrink'><a title='{user_name}' class='btn avatar' data-turbo-frame='_top' href='/users/{user_id}'><img aria-hidden='true' src='{avatar}' width='48' height='48'></a><span class='for-screen-reader'>{user_name}</span></figure><input type='text' name='boost[content]' autofocus='autofocus' autocomplete='off' autocorrect='off' maxlength='16' size='16' required='required' pattern='\\S+.*' data-boost-form-target='input' class='input input--boost txt-small'></label><button name='button' class='btn btn--reversed' type='submit'><img aria-hidden='true' src='/assets/check-7897ff7e.svg'><span class='for-screen-reader'>Submit</span></button><a data-turbo-frame='boosts_message_{client_id_html}' data-form-target='cancel' class='btn btn--negative' href='/messages/{mid}/boosts'><img aria-hidden='true' src='/assets/minus-b31a1093.svg'><span class='for-screen-reader'>Cancel</span></a></form></div></turbo-frame>",
        user_name = esc(&u.name),
        user_id = u.id,
        avatar = esc(&avatar),
    );
    if frame_request {
        Ok(Html(frame).into_response())
    } else {
        Ok(render("Add a boost", &frame, Some(&u)))
    }
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
    Ok(StatusCode::NO_CONTENT.into_response())
}
async fn boost_delete_post_override(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((mid, bid)): Path<(i64, i64)>,
    raw: Bytes,
) -> AppResult {
    let form_method = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .filter(|value| value.starts_with("application/x-www-form-urlencoded"))
        .and_then(|_| fields(&raw).0.remove("_method"));
    let method = form_method.or_else(|| {
        headers
            .get("x-http-method-override")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    });
    match method.as_deref().map(str::to_ascii_uppercase).as_deref() {
        Some("DELETE") => boost_delete(State(s), headers, Path((mid, bid))).await,
        _ => Err(StatusCode::NOT_FOUND),
    }
}
async fn bot_boost_create(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((rid, key, mid)): Path<(i64, String, i64)>,
    body: Bytes,
) -> AppResult {
    let bot = bot_api_actor(&s, &headers, &key)?;
    room_for(&s, bot.id, rid)?;
    let content = std::str::from_utf8(&body)
        .map_err(|_| StatusCode::UNPROCESSABLE_ENTITY)?;
    if content.trim().is_empty() {
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
        "INSERT INTO boosts(id,message_id,booster_id,content,created_at) VALUES((SELECT MAX(last_id,(SELECT COALESCE(MAX(id),0) FROM boosts))+1 FROM id_sequences WHERE name='boosts'),?1,?2,?3,?4)",
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
    headers: HeaderMap,
    Path((rid, key, mid, bid)): Path<(i64, String, i64, i64)>,
) -> AppResult {
    let bot = bot_api_actor(&s, &headers, &key)?;
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
    let (event_tx, mut event_rx) = mpsc::channel::<(Arc<str>, i64, Arc<HubPayload>, bool)>(256);
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
    let mut presence_subscriptions: HashMap<String, (i64, bool)> = HashMap::new();
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
                let list_stream=if channel=="Turbo::StreamsChannel" {details.get("signed_stream_name").and_then(Value::as_str).and_then(|token|turbo_stream_from_token(&s.turbo_stream_signing_key,token).or_else(||s.imported_turbo_stream_signing_key.as_deref().and_then(|key|turbo_stream_from_token(key,token))))} else {None};
                let list_user=list_stream.as_deref().and_then(|stream|stream.strip_suffix(":rooms")).and_then(|gid|URL_SAFE_NO_PAD.decode(gid).ok()).and_then(|raw|String::from_utf8(raw).ok()).and_then(|gid|gid.strip_prefix("gid://campfire/User/").and_then(|id|id.parse::<i64>().ok()));
                let list_valid=list_stream.as_deref()==Some("rooms") || list_user==Some(u.id);
                let signed_room=signed_name.and_then(|token|room_from_stream_token(&s.turbo_stream_signing_key,token).or_else(||s.imported_turbo_stream_signing_key.as_deref().and_then(|key|room_from_stream_token(key,token))));
                let rid=if signed_name.is_some() {signed_room.as_ref().map(|(id,_)|*id).unwrap_or(0)} else {details.get("room_id").and_then(Value::as_i64).unwrap_or(0)};
                let signed_valid=signed_name.is_none() || signed_room.as_ref().is_some_and(|(_,kind)|room_for(&s,u.id,rid).is_ok_and(|room|room.kind==*kind));
                if action=="subscribe" {
                    let user_channel=channel=="UnreadRoomsChannel" || channel=="ReadRoomsChannel" || channel=="RoomListChannel" || channel=="HeartbeatChannel" || (channel=="Turbo::StreamsChannel" && list_user==Some(u.id));
                    let hub=match channel {"RoomMessagesChannel"=>Some(&s.events),"TypingNotificationsChannel"=>Some(&s.typing_events),"UnreadRoomsChannel"=>Some(&s.unread_events),"ReadRoomsChannel"=>Some(&s.read_events),"RoomListChannel"=>Some(&s.room_list_events),"Turbo::StreamsChannel" if list_user==Some(u.id)=>Some(&s.turbo_user_rooms),"Turbo::StreamsChannel" if list_stream.as_deref()==Some("rooms")=>Some(&s.turbo_shared_rooms),_=>None};
                    let accepted=signed_valid && (channel!="Turbo::StreamsChannel" || list_valid) && (hub.is_some() || channel=="PresenceChannel" || channel=="HeartbeatChannel") && (user_channel || channel=="Turbo::StreamsChannel" || (rid>0 && room_for(&s,u.id,rid).is_ok()));
                    if accepted {
                        if channel=="PresenceChannel" {
                            if !presence_subscriptions.contains_key(ident) {
                                presence_subscriptions.insert(ident.to_string(), (rid, true));
                                let _=presence_update(&s,u.id,rid,"present");
                            }
                        } else if let Some(hub)=hub { if let std::collections::hash_map::Entry::Vacant(entry)=subscriptions.entry(ident.to_string()) {
                            let mut room_events=hub.channel(if user_channel {u.id} else {rid}).subscribe();
                            let tx=event_tx.clone();
                            let identifier: Arc<str> = serde_json::to_string(ident).unwrap_or_default().into();
                            let event_rid=if user_channel || channel=="Turbo::StreamsChannel" {0} else {rid};
                            let turbo=signed_name.is_some() || channel=="Turbo::StreamsChannel";
                            entry.insert(tokio::spawn(async move {loop {match room_events.recv().await {Ok(payload)=>{if tx.send((identifier.clone(),event_rid,payload,turbo)).await.is_err(){break}},Err(broadcast::error::RecvError::Lagged(_))=>continue,Err(_)=>break}}}));
                        }}
                    }
                    let response=if accepted {"confirm_subscription"} else {"reject_subscription"};
                    if sender.send(WsMessage::Text(json!({"identifier":ident,"type":response}).to_string().into())).await.is_err(){break}
                } else if action=="unsubscribe" {
                    if let Some(task)=subscriptions.remove(ident){task.abort();}
                    if channel=="PresenceChannel" {
                        if let Some((room_id, true))=presence_subscriptions.remove(ident) {
                            let _=presence_update(&s,u.id,room_id,"absent");
                        }
                    }
                } else if action=="message" && channel=="TypingNotificationsChannel" && subscriptions.contains_key(ident) && room_for(&s,u.id,rid).is_ok() {
                    let data=cmd.get("data").and_then(Value::as_str).and_then(|raw|serde_json::from_str::<Value>(raw).ok()).unwrap_or(Value::Null);
                    if let Some(action)=data.get("action").and_then(Value::as_str).filter(|action| *action=="start" || *action=="stop") {
                        s.typing_events.send(Event {room_id:rid,payload:json!({"action":action,"user":{"id":u.id,"name":u.name}}).to_string()});
                    }
                } else if action=="message" && channel=="PresenceChannel" && room_for(&s,u.id,rid).is_ok() {
                    let data=cmd.get("data").and_then(Value::as_str).and_then(|raw|serde_json::from_str::<Value>(raw).ok()).unwrap_or(Value::Null);
                    if let (Some(action), Some((room_id, present)))=(data.get("action").and_then(Value::as_str),presence_subscriptions.get_mut(ident)) {
                        if *room_id==rid {
                            if action=="absent" && *present {*present=false;let _=presence_update(&s,u.id,rid,"absent");}
                            else if action=="present" && !*present {*present=true;let _=presence_update(&s,u.id,rid,"present");}
                            else if action=="refresh" && *present {let _=presence_update(&s,u.id,rid,"refresh");}
                        }
                    }
                }
            },
            event=event_rx.recv()=>{if let Some((identifier,rid,payload,turbo))=event{if rid==0 || payload.accessible(&s,u.id,rid){if let Some(frame)=payload.frame(identifier,turbo){if sender.send(frame).await.is_err(){break}}}}}
        }
    }
    for (_, task) in subscriptions {
        task.abort();
    }
    for (_, (rid, present)) in presence_subscriptions {
        if present {
            let _ = presence_update(&s, u.id, rid, "absent");
        }
    }
}
async fn health() -> impl IntoResponse {
    "ok"
}
async fn campfire_response_headers(req: Request, next: Next) -> Response {
    let path = req.uri().path();
    let asset = path.starts_with("/assets/") || path.starts_with("/static/");
    let application_controller = !asset && path != "/up" && path != "/cable"
        && !path.starts_with("/rails/active_storage/");
    let format_negotiated = matches!(path,
        "/autocompletable/users" | "/webmanifest" | "/webmanifest.json" |
        "/service-worker" | "/service-worker.js");
    let mut response = next.run(req).await;
    if !response.status().is_informational()
        && !matches!(response.status(), StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED | StatusCode::NOT_ACCEPTABLE)
        && !response.headers().contains_key(header::CONTENT_ENCODING)
        && !response.headers().get(header::CACHE_CONTROL).is_some_and(|value| value.to_str().is_ok_and(|text| text.contains("no-transform"))) {
        let vary = if format_negotiated && response.status().is_success() {
            "Accept,Accept-Encoding"
        } else {
            "Accept-Encoding"
        };
        response.headers_mut().insert(header::VARY, HeaderValue::from_static(vary));
    }
    if !asset && (response.status().is_success() || response.status().is_redirection() || response.status() == StatusCode::FORBIDDEN) {
        let headers = response.headers_mut();
        headers.insert("referrer-policy", HeaderValue::from_static("strict-origin-when-cross-origin"));
        headers.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
        headers.insert("x-frame-options", HeaderValue::from_static("SAMEORIGIN"));
        headers.insert("x-permitted-cross-domain-policies", HeaderValue::from_static("none"));
        headers.insert("x-xss-protection", HeaderValue::from_static("0"));
        if application_controller {
            static VERSION_HEADERS: OnceLock<(HeaderValue, HeaderValue)> = OnceLock::new();
            let (version, revision) = VERSION_HEADERS.get_or_init(|| {
                let revision = env::var("GIT_REVISION").unwrap_or_default();
                let version = env::var("APP_VERSION").ok().filter(|value| !value.trim().is_empty())
                    .or_else(|| (!revision.trim().is_empty()).then(|| revision.clone()))
                    .unwrap_or_else(|| "0".to_string());
                (HeaderValue::from_str(&version).unwrap_or_else(|_| HeaderValue::from_static("0")),
                 HeaderValue::from_str(&revision).unwrap_or_else(|_| HeaderValue::from_static("")))
            });
            headers.insert("x-version", version.clone());
            headers.insert("x-rev", revision.clone());
        }
    }
    response
}
async fn webmanifest(State(s): State<Arc<AppState>>, OriginalUri(uri): OriginalUri, headers: HeaderMap) -> AppResult {
    let explicit_json = uri.path().ends_with(".json")
        || uri.query().is_some_and(|query| query.split('&').any(|part| part == "format=json"));
    let accepts_json = headers.get(header::ACCEPT).and_then(|value| value.to_str().ok()).is_some_and(|accept| {
        accept.split(',').any(|part| {
            matches!(part.trim().split(';').next(), Some("application/json" | "application/*" | "*/*"))
        })
    });
    if !explicit_json && !accepts_json {
        let mut response = StatusCode::NOT_ACCEPTABLE.into_response();
        response.headers_mut().insert(header::CONTENT_TYPE, "text/html; charset=UTF-8".parse().unwrap());
        return Ok(response);
    }
    let db = pool(&s)?;
    let account: Option<(String, String)> = db
        .query_row("SELECT name,updated_at FROM accounts LIMIT 1", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .optional()
        .map_err(db_err)?;
    let (name, updated_at) = account.unwrap_or_else(|| ("Rustfire".into(), String::new()));
    let version: String = updated_at
        .chars()
        .filter(char::is_ascii_digit)
        .take(14)
        .collect();
    let logo = format!("/account/logo?v={version}");
    let small_logo = format!("/account/logo?size=small&v={version}");
    let asset = |path: &str| public_url(&headers, path);
    let mut r = Json(json!({
        "name": name,
        "icons": [
            {"src":small_logo,"type":"image/png","sizes":"192x192"},
            {"src":logo,"type":"image/png","sizes":"512x512"},
            {"src":logo,"type":"image/png","sizes":"512x512","purpose":"maskable"}
        ],
        "start_url":"/",
        "display":"standalone",
        "scope":"/",
        "description":"An installable, self-hosted chat app built in Rust.",
        "categories":["social","business","productivity"],
        "theme_color":"#ffffff",
        "background_color":"#ffffff",
        "shortcuts":[
            {"name":"New chat room","description":"Open Rustfire and start a new chat room","url":"rooms/opens/new","icons":[{"src":asset("/assets/add-f232d8a6.svg"),"sizes":"any"}]},
            {"name":"My profile","description":"Open Rustfire and view your profile","url":"/users/me/profile","icons":[{"src":asset("/assets/person-da193438.svg"),"sizes":"any"}]}
        ],
        "screenshots":[
            {"src":asset("/assets/screenshots/android-chat-f8b923c9.png"),"sizes":"1080x2400","form_factor":"narrow","label":"Rustfire is an installable, self-hosted group chat system."},
            {"src":asset("/assets/screenshots/android-sidebar-e9d2b49f.png"),"sizes":"1080x2400","form_factor":"narrow","label":"Easily invite people. Make rooms. @mentions, DMs, and mobile support."},
            {"src":asset("/assets/screenshots/android-dark-mode-e43dcf59.png"),"sizes":"1080x2400","form_factor":"narrow","label":"Full support for dark mode, customizable to your brand."}
        ]
    })).into_response();
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        "application/json; charset=utf-8".parse().unwrap(),
    );
    Ok(r)
}
async fn service_worker(OriginalUri(uri): OriginalUri, headers: HeaderMap) -> Response {
    let explicit_js = uri.path().ends_with(".js")
        || uri.query().is_some_and(|query| query.split('&').any(|part| part == "format=js"));
    let accepts_js = headers.get(header::ACCEPT).and_then(|value| value.to_str().ok()).is_some_and(|accept| {
        accept.split(',').any(|part| {
            matches!(part.trim().split(';').next(), Some("text/javascript" | "application/javascript" | "text/*" | "*/*"))
        })
    });
    if !explicit_js && !accepts_js {
        let mut response = StatusCode::NOT_ACCEPTABLE.into_response();
        response.headers_mut().insert(header::CONTENT_TYPE, "text/html; charset=UTF-8".parse().unwrap());
        return response;
    }
    let mut r = include_str!("../static/service-worker.js").into_response();
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        "text/javascript; charset=utf-8".parse().unwrap(),
    );
    r.headers_mut()
        .insert("service-worker-allowed", "/".parse().unwrap());
    r
}
fn init_db(db: &Db) -> Result<(), Box<dyn std::error::Error>> {
    let mut conn = db.get()?;
    let fts_definition: Option<String> = conn.query_row("SELECT sql FROM sqlite_master WHERE type='table' AND name='message_search_index'", [], |r| r.get(0)).optional()?;
    let fts_exists = fts_definition.is_some();
    let fts_needs_rebuild = fts_definition.is_some_and(|sql| !sql.contains("tokenize=porter"));
    conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY,name TEXT NOT NULL,join_code TEXT NOT NULL UNIQUE,custom_styles TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
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
        CREATE TABLE IF NOT EXISTS push_subscriptions(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,endpoint TEXT NOT NULL,p256dh_key TEXT NOT NULL,auth_key TEXT NOT NULL,user_agent TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,token TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,last_active_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bans(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,ip_address TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_bans_ip ON bans(ip_address);
        CREATE TABLE IF NOT EXISTS background_jobs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_background_jobs_kind ON background_jobs(kind,id);
        CREATE TABLE IF NOT EXISTS webhook_jobs(id INTEGER PRIMARY KEY,bot_id INTEGER NOT NULL,message_id INTEGER NOT NULL,created_at TEXT NOT NULL,claimed_at INTEGER);
        CREATE INDEX IF NOT EXISTS idx_webhook_jobs_claim ON webhook_jobs(claimed_at,id);
        CREATE TABLE IF NOT EXISTS failed_webhook_jobs(job_id INTEGER PRIMARY KEY,bot_id INTEGER NOT NULL,message_id INTEGER NOT NULL,error TEXT NOT NULL,created_at TEXT NOT NULL,failed_at TEXT NOT NULL);
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
        CREATE TABLE IF NOT EXISTS id_sequences(name TEXT PRIMARY KEY,last_id INTEGER NOT NULL);
        INSERT INTO id_sequences(name,last_id) VALUES('messages',COALESCE((SELECT MAX(id) FROM messages),0)) ON CONFLICT(name) DO UPDATE SET last_id=MAX(id_sequences.last_id,excluded.last_id);
        CREATE TRIGGER IF NOT EXISTS message_id_track AFTER INSERT ON messages BEGIN UPDATE id_sequences SET last_id=MAX(last_id,new.id) WHERE name='messages'; END;
        CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,PRIMARY KEY(message_id,user_id));
        CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,filename TEXT NOT NULL,content_type TEXT NOT NULL,stored_name TEXT NOT NULL,created_at TEXT NOT NULL,width REAL,height REAL);
        CREATE TABLE IF NOT EXISTS inline_blobs(id INTEGER PRIMARY KEY,filename TEXT NOT NULL,content_type TEXT NOT NULL,stored_name TEXT NOT NULL,byte_size INTEGER NOT NULL,created_at TEXT NOT NULL,width INTEGER,height INTEGER);
        CREATE TABLE IF NOT EXISTS direct_upload_blobs(id INTEGER PRIMARY KEY,storage_key TEXT NOT NULL UNIQUE,filename TEXT NOT NULL,content_type TEXT NOT NULL,byte_size INTEGER NOT NULL,checksum TEXT NOT NULL,created_at TEXT NOT NULL,uploaded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS inline_embeds(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,blob_id INTEGER NOT NULL REFERENCES inline_blobs(id) ON DELETE CASCADE,PRIMARY KEY(message_id,blob_id));
        CREATE INDEX IF NOT EXISTS idx_inline_embeds_blob ON inline_embeds(blob_id);
        CREATE TABLE IF NOT EXISTS avatars(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,stored_name TEXT NOT NULL,content_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_logos(id INTEGER PRIMARY KEY CHECK(id=1),stored_name TEXT NOT NULL,content_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS boosts(id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,booster_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,content TEXT NOT NULL,created_at TEXT NOT NULL);
        INSERT INTO id_sequences(name,last_id) VALUES('boosts',COALESCE((SELECT MAX(id) FROM boosts),0)) ON CONFLICT(name) DO UPDATE SET last_id=MAX(id_sequences.last_id,excluded.last_id);
        CREATE TRIGGER IF NOT EXISTS boost_id_track AFTER INSERT ON boosts BEGIN UPDATE id_sequences SET last_id=MAX(last_id,new.id) WHERE name='boosts'; END;
        CREATE TABLE IF NOT EXISTS searches(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,query TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,query));
        CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room_id,id);CREATE INDEX IF NOT EXISTS idx_messages_room_created ON messages(room_id,created_at,id);CREATE INDEX IF NOT EXISTS idx_messages_room_updated ON messages(room_id,updated_at,id);CREATE INDEX IF NOT EXISTS idx_messages_creator_id ON messages(creator_id,id);CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);CREATE INDEX IF NOT EXISTS idx_boosts_message ON boosts(message_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS message_search_index USING fts5(body, tokenize=porter);
        CREATE TRIGGER IF NOT EXISTS message_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO message_search_index(rowid,body) VALUES(new.id,new.body); END;
        CREATE TRIGGER IF NOT EXISTS message_fts_update AFTER UPDATE OF body ON messages BEGIN UPDATE message_search_index SET body=new.body WHERE rowid=new.id; END;
        CREATE TRIGGER IF NOT EXISTS message_fts_delete AFTER DELETE ON messages BEGIN DELETE FROM message_search_index WHERE rowid=old.id; END;")?;
    let old_push_unique: bool = conn.query_row(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='push_subscriptions'",
        [],
        |row| row.get::<_, String>(0),
    )?.contains("UNIQUE(user_id,endpoint)");
    if old_push_unique {
        let tx = conn.transaction()?;
        tx.execute_batch("CREATE TABLE push_subscriptions_new(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,endpoint TEXT NOT NULL,p256dh_key TEXT NOT NULL,auth_key TEXT NOT NULL,user_agent TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            INSERT INTO push_subscriptions_new(id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at) SELECT id,user_id,endpoint,p256dh_key,auth_key,user_agent,created_at,updated_at FROM push_subscriptions;
            DROP TABLE push_subscriptions;
            ALTER TABLE push_subscriptions_new RENAME TO push_subscriptions;")?;
        tx.commit()?;
    }
    conn.execute("CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_keys ON push_subscriptions(user_id,endpoint,p256dh_key,auth_key)", [])?;
    let has_search_updated_at: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('searches') WHERE name='updated_at')",
        [],
        |row| row.get(0),
    )?;
    if !has_search_updated_at {
        conn.execute("ALTER TABLE searches ADD COLUMN updated_at TEXT", [])?;
        conn.execute("UPDATE searches SET updated_at=created_at", [])?;
    }
    let has_custom_styles: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('accounts') WHERE name='custom_styles')",
        [],
        |r| r.get(0),
    )?;
    if !has_custom_styles {
        conn.execute("ALTER TABLE accounts ADD COLUMN custom_styles TEXT", [])?;
        conn.execute(
            "UPDATE accounts SET custom_styles=(SELECT css FROM account_custom_styles WHERE id=1) WHERE EXISTS(SELECT 1 FROM account_custom_styles WHERE id=1 AND css!='')",
            [],
        )?;
    }
    conn.execute("UPDATE users SET bot_token=substr(bot_token,length(CAST(id AS TEXT))+2) WHERE role=2 AND bot_token LIKE CAST(id AS TEXT)||'-%'", [])?;
    conn.execute("UPDATE rooms SET name=NULL WHERE type='Rooms::Direct' AND name IS NOT NULL", [])?;
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
    for column in ["width", "height"] {
        let exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_info('attachments') WHERE name=?1)",
            [column],
            |row| row.get(0),
        )?;
        if !exists {
            conn.execute(
                &format!("ALTER TABLE attachments ADD COLUMN {column} REAL"),
                [],
            )?;
        }
    }
    for column in ["width", "height"] {
        let exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_info('inline_blobs') WHERE name=?1)",
            [column],
            |row| row.get(0),
        )?;
        if !exists {
            conn.execute(
                &format!("ALTER TABLE inline_blobs ADD COLUMN {column} INTEGER"),
                [],
            )?;
        }
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
    if fts_needs_rebuild {
        let transaction = conn.transaction()?;
        transaction.execute_batch("DROP TRIGGER message_fts_insert;
            DROP TRIGGER message_fts_update;
            DROP TRIGGER message_fts_delete;
            DROP TABLE message_search_index;
            CREATE VIRTUAL TABLE message_search_index USING fts5(body, tokenize=porter);
            CREATE TRIGGER message_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO message_search_index(rowid,body) VALUES(new.id,new.body); END;
            CREATE TRIGGER message_fts_update AFTER UPDATE OF body ON messages BEGIN UPDATE message_search_index SET body=new.body WHERE rowid=new.id; END;
            CREATE TRIGGER message_fts_delete AFTER DELETE ON messages BEGIN DELETE FROM message_search_index WHERE rowid=old.id; END;
            INSERT INTO message_search_index(rowid,body) SELECT m.id,COALESCE(NULLIF(m.body,''),a.filename,'') FROM messages m LEFT JOIN attachments a ON a.message_id=m.id;")?;
        transaction.commit()?;
    } else if !fts_exists {
        conn.execute(
            "INSERT INTO message_search_index(rowid,body) SELECT m.id,COALESCE(NULLIF(m.body,''),a.filename,'') FROM messages m LEFT JOIN attachments a ON a.message_id=m.id",
            [],
        )?;
    }
    conn.execute_batch("DROP TRIGGER IF EXISTS message_fts_insert;
        DROP TRIGGER IF EXISTS message_fts_update;
        DROP TRIGGER IF EXISTS message_fts_delete;
        CREATE TRIGGER message_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO message_search_index(rowid,body) VALUES(new.id,COALESCE(NULLIF(new.body,''),(SELECT filename FROM attachments WHERE message_id=new.id),''));
        END;
        CREATE TRIGGER message_fts_update AFTER UPDATE OF body ON messages BEGIN
            UPDATE message_search_index SET body=COALESCE(NULLIF(new.body,''),(SELECT filename FROM attachments WHERE message_id=new.id),'') WHERE rowid=new.id;
        END;
        CREATE TRIGGER message_fts_delete AFTER DELETE ON messages BEGIN
            DELETE FROM message_search_index WHERE rowid=old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS attachment_fts_insert AFTER INSERT ON attachments BEGIN
            UPDATE message_search_index SET body=COALESCE(NULLIF((SELECT body FROM messages WHERE id=new.message_id),''),new.filename) WHERE rowid=new.message_id;
        END;
        CREATE TRIGGER IF NOT EXISTS attachment_fts_update AFTER UPDATE OF filename ON attachments BEGIN
            UPDATE message_search_index SET body=COALESCE(NULLIF((SELECT body FROM messages WHERE id=new.message_id),''),new.filename) WHERE rowid=new.message_id;
        END;
        CREATE TRIGGER IF NOT EXISTS attachment_fts_delete AFTER DELETE ON attachments BEGIN
            UPDATE message_search_index SET body=(SELECT body FROM messages WHERE id=old.message_id) WHERE rowid=old.message_id;
        END;")?;
    let attachment_fts_migrated: bool = conn.query_row("SELECT EXISTS(SELECT 1 FROM app_secrets WHERE name='attachment_fts_migrated')",[],|r|r.get(0))?;
    if !attachment_fts_migrated {
        let transaction = conn.transaction()?;
        transaction.execute("UPDATE message_search_index SET body=(SELECT a.filename FROM attachments a JOIN messages m ON m.id=a.message_id WHERE m.id=message_search_index.rowid AND m.body='') WHERE rowid IN (SELECT m.id FROM messages m JOIN attachments a ON a.message_id=m.id WHERE m.body='')", [])?;
        transaction.execute("INSERT INTO app_secrets(name,value) VALUES('attachment_fts_migrated',X'01')", [])?;
        transaction.commit()?;
    }
    conn.execute(
        "INSERT OR IGNORE INTO direct_room_sets(room_id,member_ids) SELECT r.id,COALESCE((SELECT group_concat(user_id,',') FROM (SELECT user_id FROM memberships WHERE room_id=r.id ORDER BY user_id)),'') FROM rooms r WHERE r.type='Rooms::Direct' AND NOT EXISTS(SELECT 1 FROM direct_room_sets d WHERE d.room_id=r.id)",
        [],
    )?;
    Ok(())
}
fn render_imported_rich_text(
    db: &Db,
    signing_key: &[u8],
    imported_key: Option<&[u8]>,
    blob_key: &[u8],
    avatar_key: &[u8],
) -> Result<(), Box<dyn std::error::Error>> {
    let mut conn = db.get()?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS import_missing_search(message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE)")?;
    let mut last_id = 0_i64;
    loop {
        let batch = {
            let mut query = conn.prepare("SELECT m.id,m.body_source,EXISTS(SELECT 1 FROM import_missing_search missing WHERE missing.message_id=m.id) FROM messages m WHERE m.id>?1 AND m.body_source IS NOT NULL ORDER BY m.id LIMIT 1000")?;
            query
                .query_map([last_id], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?, row.get::<_, bool>(2)?)))?
                .collect::<Result<Vec<_>, _>>()?
        };
        if batch.is_empty() {
            break;
        }
        let tx = conn.transaction()?;
        for (id, source, missing_search) in &batch {
            let cleaned = strip_disallowed_rich_tags(source);
            let (inline, _) = render_imported_inline_files(cleaned.as_deref().unwrap_or(source), &tx, *id, signing_key, imported_key, blob_key, true)
                .map_err(|status| format!("rendering imported inline files for message {id}: {status}"))?;
            let (trusted, plain_source) = replace_mention_attachments(&inline, &tx, signing_key, imported_key, avatar_key)
                .map_err(|status| format!("rendering imported message {id}: {status}"))?;
            let (mut plain, html) = rich_body_trusted(&trusted, None);
            if plain_source != trusted {
                plain = rich_body_trusted(&plain_source, None).0;
            }
            if *missing_search && cleaned.is_some() {
                let (_, original_plain) = replace_mention_attachments(source, &tx, signing_key, imported_key, avatar_key)
                    .map_err(|status| format!("reading imported message {id}: {status}"))?;
                plain = action_text_plain_body(&original_plain);
            }
            if *missing_search {
                tx.execute("UPDATE messages SET body=?1,body_html=?2 WHERE id=?3", params![plain, html, id])?;
            } else {
                tx.execute("UPDATE messages SET body_html=?1 WHERE id=?2", params![html, id])?;
            }
            tx.execute("DELETE FROM message_mentions WHERE message_id=?1", [id])?;
            for user_id in mention_ids(source, signing_key, imported_key) {
                tx.execute("INSERT OR IGNORE INTO message_mentions(message_id,user_id) SELECT ?1,?2 WHERE EXISTS(SELECT 1 FROM memberships m JOIN messages msg ON msg.room_id=m.room_id WHERE msg.id=?1 AND m.user_id=?2)", params![id,user_id])?;
            }
        }
        last_id = batch.last().unwrap().0;
        tx.commit()?;
    }
    conn.execute_batch("DROP TABLE import_missing_search")?;
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
    let command = env::args().nth(1);
    if command.as_deref() == Some("--init-db") {
        return Ok(());
    }
    if command.as_deref() == Some("--import-campfire-vapid") {
        import_campfire_vapid_key(std::path::Path::new(&db_path))?;
        return Ok(());
    }
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
    let imported_cookie_signing_key = campfire_secret.as_deref().map(rails_cookie_key).transpose()?;
    if command.as_deref() == Some("--render-imported-rich-text") {
        render_imported_rich_text(&db, &mention_signing_key, imported_mention_signing_key.as_deref(), imported_blob_signing_key.as_deref().unwrap_or(&blob_signing_key), imported_avatar_signing_key.as_deref().unwrap_or(&avatar_signing_key))?;
        return Ok(());
    }
    let presence_cutoff = (Utc::now() - Duration::seconds(60)).to_rfc3339();
    db.get()?.execute("UPDATE memberships SET connections=0,connected_at=NULL WHERE connected_at>=?1", [presence_cutoff])?;
    let custom_styles: Option<String> = db
        .get()?
        .query_row("SELECT custom_styles FROM accounts LIMIT 1", [], |row| row.get(0))
        .optional()?
        .flatten();
    let _ = CUSTOM_STYLES.set(RwLock::new(custom_styles));
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
        autocomplete_cache: RwLock::new(HashMap::new()),
        autocomplete_slots: Arc::new(Semaphore::new(
            env::var("RUSTFIRE_AUTOCOMPLETE_SLOTS")
                .ok()
                .and_then(|value| value.parse::<usize>().ok())
                .filter(|slots| (1..=128).contains(slots))
                .unwrap_or(4),
        )),
        events: RoomHub::default(),
        typing_events: RoomHub::default(),
        unread_events: RoomHub::default(),
        read_events: RoomHub::default(),
        room_list_events: RoomHub::default(),
        turbo_user_rooms: RoomHub::default(),
        turbo_shared_rooms: RoomHub::default(),
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
        imported_cookie_signing_key,
        push_slots: Arc::new(Semaphore::new(50)),
        push_queue_slots: Arc::new(Semaphore::new(10_050)),
        has_push_subscriptions: AtomicBool::new(has_push_subscriptions),
        push_delivery_enabled: !env::var("RUSTFIRE_DISABLE_PUSH")
            .is_ok_and(|value| value == "1" || value.eq_ignore_ascii_case("true")),
        login_attempts: Mutex::new(HashMap::new()),
        variant_slots: Arc::new(Semaphore::new(4)),
    });
    tokio::spawn(run_background_jobs(state.clone()));
    if state.webhooks_enabled {
        tokio::spawn(run_webhook_jobs(state.clone()));
    }
    let app = Router::new()
        .route("/", get(root))
        .route("/up", get(health))
        .route("/webmanifest", get(webmanifest))
        .route("/webmanifest.json", get(webmanifest))
        .route("/service-worker", get(service_worker))
        .route("/service-worker.js", get(service_worker))
        .route("/qr_code/{id}", get(qr_code_show))
        .route("/first_run", get(first_run_get).post(first_run_post))
        .route("/session/new", get(login_get))
        .route("/session", post(session_post).delete(logout))
        .route("/session/logout", post(logout))
        .route(
            "/session/transfers/{token}",
            get(transfer_show)
                .post(transfer_update)
                .patch(transfer_update)
                .put(transfer_update),
        )
        .route("/cable", get(ws_upgrade))
        .route("/rooms", get(rooms_index))
        .route(
            "/rooms/{id}",
            get(room_show).post(room_post_override).delete(room_delete),
        )
        .route("/rooms/{id}/refresh", get(room_refresh))
        .route("/rooms/{id}/refresh.turbo_stream", get(room_refresh))
        .route("/rooms/{id}/refresh_state", get(room_refresh_state))
        .route("/rooms/{id}/settings", get(room_edit))
        .route(
            "/rooms/{id}/messages",
            get(messages_index).post(message_create),
        )
        .route(
            "/rooms/{id}/messages/{mid}",
            get(message_show)
                .post(message_post_override)
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
                .post(involvement_post_override)
                .patch(involvement_post)
                .put(involvement_post),
        )
        .route("/rooms/{id}/update", post(room_update))
        .route("/rooms/{id}/delete", post(room_delete))
        .route("/rooms/opens/new", get(new_open_room))
        .route("/rooms/closeds/new", get(new_closed_room))
        .route(
            "/rooms/opens/{id}",
            get(room_kind_show)
                .post(room_kind_post_override)
                .patch(room_kind_update)
                .put(room_kind_update)
                .delete(room_kind_delete),
        )
        .route(
            "/rooms/closeds/{id}",
            get(room_kind_show)
                .post(room_kind_post_override)
                .patch(room_kind_update)
                .put(room_kind_update)
                .delete(room_kind_delete),
        )
        .route("/rooms/opens/{id}/edit", get(room_kind_edit))
        .route("/rooms/closeds/{id}/edit", get(room_kind_edit))
        .route("/rooms/directs/new", get(direct_new))
        .route("/rooms/directs", get(room_namespace_index).post(direct_create))
        .route("/rooms/opens", get(room_namespace_index).post(create_open_room))
        .route("/rooms/closeds", get(room_namespace_index).post(create_closed_room))
        .route(
            "/rooms/directs/{id}",
            get(direct_show).post(direct_post_override).delete(direct_delete),
        )
        .route("/rooms/directs/{id}/edit", get(direct_edit))
        .route("/rooms/directs/{id}/delete", post(direct_delete))
        .route("/searches", get(search_get).post(search_post))
        .route("/unfurl_link", post(unfurl_link))
        .route("/searches/clear", post(search_clear).delete(search_clear))
        .route(
            "/account",
            get(account_get)
                .post(account_update)
                .patch(account_update)
                .put(account_update),
        )
        .route(
            "/account.1",
            post(account_update)
                .patch(account_update)
                .put(account_update),
        )
        .route("/account/edit", get(account_get))
        .route("/account/update", post(account_update))
        .route("/account/custom_styles/edit", get(custom_styles_get))
        .route(
            "/account/custom_styles",
            post(custom_styles_post_override).put(custom_styles_update).patch(custom_styles_update),
        )
        .route(
            "/account/logo",
            get(logo_get).post(logo_post).delete(logo_delete),
        )
        .route("/account/logo/delete", post(logo_delete))
        .route("/account/join_code", post(join_code_create))
        .route("/account/users", get(account_users_index))
        .route("/account/users.turbo_stream", get(account_users_index))
        .route(
            "/account/users/{id}",
            post(user_admin_post)
                .patch(user_role_update)
                .put(user_role_update)
                .delete(user_deactivate),
        )
        .route("/account/users/{id}/role", post(user_role_update_alias))
        .route("/account/users/{id}/deactivate", post(user_deactivate))
        .route("/join/{code}", get(join_get).post(join_post))
        .route(
            "/users/me/profile",
            get(profile)
                .post(profile_post)
                .patch(profile_post)
                .put(profile_post),
        )
        .route("/users/me/avatar", get(avatar_get_me).post(avatar_post).delete(avatar_delete))
        .route(
            "/users/me/push_subscriptions",
            get(push_subscriptions_get).post(push_subscriptions_post),
        )
        .route(
            "/users/me/push_subscriptions/{id}",
            post(push_subscriptions_delete_post).delete(push_subscriptions_delete),
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
        .route(
            "/users/{id}/avatar",
            get(avatar_get)
                .post(avatar_delete_post)
                .delete(avatar_delete),
        )
        .route("/users/me/sidebar", get(sidebar_get))
        .route("/users/{user_id}/sidebar", get(sidebar_get))
        .route(
            "/users/{user_id}/profile",
            get(profile)
                .post(profile_post)
                .patch(profile_post)
                .put(profile_post),
        )
        .route(
            "/users/{user_id}/push_subscriptions",
            get(push_subscriptions_get).post(push_subscriptions_post),
        )
        .route(
            "/users/{user_id}/push_subscriptions/{id}",
            post(push_subscriptions_delete_post_scoped).delete(push_subscriptions_delete_scoped),
        )
        .route(
            "/users/{user_id}/push_subscriptions/{id}/test_notifications",
            post(push_test_notification_scoped),
        )
        .route("/users/{id}", get(user_show))
        .route("/users/{id}/ban", post(user_ban_post_override).delete(user_unban))
        .route("/users/{id}/ban/delete", post(user_unban))
        .route("/autocompletable/users", get(autocomplete))
        .route("/account/bots", get(bots_get).post(bot_create))
        .route("/account/bots/new", get(bot_new))
        .route(
            "/account/bots/{id}",
            post(bot_post_override)
                .patch(bot_update)
                .put(bot_update)
                .delete(bot_delete),
        )
        .route("/account/bots/{id}/edit", get(bot_edit))
        .route("/account/bots/{id}/update", post(bot_update))
        .route("/account/bots/{id}/avatar", post(bot_avatar_post))
        .route("/account/bots/{id}/avatar/delete", post(bot_avatar_delete))
        .route(
            "/account/bots/{id}/key",
            post(bot_key_post_override)
                .patch(bot_key_rotate)
                .put(bot_key_rotate),
        )
        .route("/account/bots/{id}/delete", post(bot_delete))
        .route(
            "/rooms/{id}/{bot_key}/messages",
            get(bot_messages_get).post(bot_messages_post),
        )
        .route(
            "/rooms/{id}/{bot_key}/messages/{mid}",
            patch(bot_message_update).put(bot_message_update).delete(bot_message_delete),
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
            delete(boost_delete).post(boost_delete_post_override),
        )
        .route("/messages/{id}/boosts/{bid}/delete", post(boost_delete))
        .route("/attachments/{id}", get(attachment_get))
        .route("/attachments/{id}/{kind}", get(attachment_variant))
        .route(
            "/rails/active_storage/blobs/redirect/{token}/{filename}",
            get(signed_blob_get),
        )
        .route("/rails/active_storage/direct_uploads",post(direct_upload_create))
        .route("/rails/active_storage/disk/{token}",axum::routing::put(direct_upload_put))
        .route("/rails/active_storage/disk/{token}/{filename}",get(direct_upload_disk_get))
        .route(
            "/rails/active_storage/representations/redirect/{token}/{variation}/{filename}",
            get(signed_representation_get),
        )
        .nest_service("/assets", ServeDir::new("static/assets"))
        .nest_service("/static", ServeDir::new("static"))
        .layer(axum::extract::DefaultBodyLimit::max(128 * 1024 * 1024))
        .layer(axum::middleware::from_fn(reject_unsupported_browser))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            reject_banned_ip,
        ))
        .layer(axum::middleware::from_fn(campfire_response_headers))
        .layer(CompressionLayer::new().compress_when(SizeAbove::new(0).and(
            |status: StatusCode, _version: axum::http::Version, headers: &HeaderMap, _extensions: &axum::http::Extensions| {
                !status.is_informational()
                    && !matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED | StatusCode::NOT_ACCEPTABLE)
                    && headers.get(header::CONTENT_LENGTH) != Some(&HeaderValue::from_static("0"))
                    && !headers.get(header::CACHE_CONTROL).is_some_and(|value| value.to_str().is_ok_and(|text| text.contains("no-transform")))
            }
        )))
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
    use base64::{Engine as _, engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD}};
    use openssl::{
        bn::BigNumContext,
        ec::{EcGroup, EcKey, PointConversionForm},
        nid::Nid,
    };
    use std::net::IpAddr;

    #[test]
    fn csrf_forms_handles_arrow_in_quoted_form_attributes() {
        let input = "<form data-action='keydown.ctrl+enter->form#submit' action='/account/custom_styles' method='post'><input type='hidden' name='_method' value='patch'><textarea name='account[custom_styles]'></textarea></form>";
        let output = super::csrf_forms(input, "test-token");
        assert!(output.contains("name='_method' value='patch'><input type='hidden' name='authenticity_token' value='test-token'>"));
        assert_eq!(output.matches("name='authenticity_token'").count(), 1);
    }

    #[test]
    fn csrf_forms_adds_token_to_browser_signup_form() {
        let input = "<form class=\"center\" enctype=\"multipart/form-data\" action=\"/first_run\" method=\"post\"><input name=\"user[name]\"></form>";
        let output = super::csrf_forms(input, "signup-token");
        assert!(output.contains("method=\"post\"><input type='hidden' name='authenticity_token' value='signup-token'>"));
    }

    #[test]
    fn imported_rails_session_cookie_verifies_purpose_expiry_and_signature() {
        let key = super::rails_cookie_key("0123456789abcdef0123456789abcdef0123456789abcdef").unwrap();
        let source_cookie = "eyJfcmFpbHMiOnsibWVzc2FnZSI6IkltbHRjRzl5ZEdWa0xYTmxjM05wYjI0aSIsImV4cCI6IjIwOTktMDEtMDFUMDA6MDA6MDAuMDAwWiIsInB1ciI6ImNvb2tpZS5zZXNzaW9uX3Rva2VuIn19--30ae05ed03445e2452fb8957a8759ec4529f55af";
        assert_eq!(super::rails_session_cookie_token(&key, source_cookie).as_deref(), Some("imported-session"));
        assert_eq!(super::rails_session_cookie_token(&key, &source_cookie.replace("--", "%2D%2D")).as_deref(), Some("imported-session"));
        assert!(super::rails_session_cookie_token(b"wrong", source_cookie).is_none());
        let expired = serde_json::json!({"_rails":{"message":base64::engine::general_purpose::STANDARD.encode("\"imported-session\""),"exp":"2020-01-01T00:00:00.000Z","pur":"cookie.session_token"}}).to_string();
        let encoded = base64::engine::general_purpose::STANDARD.encode(expired);
        let digest = super::mention_signature(&key, encoded.as_bytes(), openssl::hash::MessageDigest::sha1()).unwrap().iter().map(|byte| format!("{byte:02x}")).collect::<String>();
        assert!(super::rails_session_cookie_token(&key, &format!("{encoded}--{digest}")).is_none());
    }

    #[test]
    fn attachment_filename_is_searchable_without_changing_message_body() {
        let db = r2d2::Pool::builder()
            .max_size(1)
            .build(r2d2_sqlite::SqliteConnectionManager::memory())
            .unwrap();
        super::init_db(&db).unwrap();
        let conn = db.get().unwrap();
        conn.execute_batch("INSERT INTO users(id,name,created_at,updated_at) VALUES(1,'Test','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(1,'Room','Rooms::Open',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,1,'','attachment-test','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO attachments(id,message_id,filename,content_type,stored_name,created_at) VALUES(1,1,'report.txt','text/plain','stored-file','2026-01-01T00:00:00Z');").unwrap();
        let indexed: String = conn.query_row("SELECT body FROM message_search_index WHERE rowid=1", [], |row| row.get(0)).unwrap();
        assert_eq!(indexed, "report.txt");
        let body: String = conn.query_row("SELECT body FROM messages WHERE id=1", [], |row| row.get(0)).unwrap();
        assert_eq!(body, "");
        conn.execute("UPDATE attachments SET filename='updated.txt' WHERE id=1", []).unwrap();
        let indexed: String = conn.query_row("SELECT body FROM message_search_index WHERE rowid=1", [], |row| row.get(0)).unwrap();
        assert_eq!(indexed, "updated.txt");
        conn.execute("UPDATE messages SET body='A caption' WHERE id=1", []).unwrap();
        let indexed: String = conn.query_row("SELECT body FROM message_search_index WHERE rowid=1", [], |row| row.get(0)).unwrap();
        assert_eq!(indexed, "A caption");
    }

    #[test]
    fn rich_text_plain_matches_campfire_blocks_and_lists() {
        let cases = [
            ("<div>One</div><div>Two</div>", "One\nTwo"),
            ("<div>Line<br>break</div>", "Line\nbreak"),
            ("<ul><li>One</li><li>Two</li></ul>", "• One\n• Two"),
            ("<ol><li>One</li><li>Two</li></ol>", "1. One\n2. Two"),
            ("<blockquote><div>Quoted text</div></blockquote>", "“Quoted text”"),
            ("<div>Before</div><ul><li>One</li><li>Two</li></ul><div>After</div>", "Before\n• One\n• Two\n\nAfter"),
            ("<p>A</p><p>B</p>", "A\n\nB"),
            ("<div>Before <action-text-attachment filename='photo.png' content-type='image/png'></action-text-attachment> after</div>", "Before [photo.png] after"),
            ("<div>Before <action-text-attachment filename='photo.png'><figure><figcaption>photo.png 1 KB</figcaption></figure></action-text-attachment> after</div>", "Before [photo.png] after"),
        ];
        for (input, expected) in cases {
            assert_eq!(super::rich_body(input, None).0, expected, "{input}");
        }
        assert_eq!(super::action_text_plain("<div>Before<section><p>Nested <em>text</em></p></section>After</div>"), "BeforeNested text\n\nAfter");
    }
    #[test]
    fn rich_text_removes_disallowed_tags() {
        let (plain, html) = super::rich_body("<div>Hello <img src='https://evil.example/image.svg'>World</div>", None);
        assert_eq!(plain, "Hello World");
        assert!(!html.contains("<img") && !html.contains("evil.example"), "{html}");
        let (plain, html) = super::rich_body("<div>Before<table><tr><td>Cell</td></tr></table>After</div>", None);
        assert_eq!(plain, "BeforeAfter");
        assert!(!html.contains("table") && !html.contains("Cell"), "{html}");
        let (_, preview) = super::rich_body("<div><action-text-attachment content-type='application/vnd.actiontext.opengraph-embed' href='https://example.com' url='https://example.com/image.png' filename='Example' caption='Description'></action-text-attachment></div>", None);
        assert!(preview.contains("<img"), "{preview}");
    }

    #[test]
    fn webhook_html_uses_campfire_action_text_mentions() {
        let source = r#"<div><figure data-trix-attachment="{&quot;contentType&quot;:&quot;application/vnd.campfire.mention&quot;,&quot;sgid&quot;:&quot;signed-id&quot;}"><span class="mention">@Robot</span></figure> please answer</div>"#;
        assert_eq!(
            super::action_text_webhook_html(source),
            "<div><action-text-attachment sgid=\"signed-id\" content-type=\"application/vnd.campfire.mention\"></action-text-attachment> please answer</div>"
        );
        let preview = r#"<div><figure data-trix-attachment="{&quot;contentType&quot;:&quot;application/vnd.actiontext.opengraph-embed&quot;,&quot;href&quot;:&quot;https://example.com&quot;,&quot;url&quot;:&quot;https://example.com/i.png&quot;,&quot;filename&quot;:&quot;Example&quot;}" data-trix-attributes="{&quot;caption&quot;:&quot;Some text&quot;}">Something</figure></div>"#;
        assert_eq!(
            super::action_text_webhook_html(preview),
            "<div><action-text-attachment content-type=\"application/vnd.actiontext.opengraph-embed\" url=\"https://example.com/i.png\" href=\"https://example.com\" filename=\"Example\" caption=\"Some text\"></action-text-attachment></div>"
        );
        assert_eq!(super::action_text_webhook_html("First post!"), "First post!");
        let mention_with_content = r#"<div><figure data-trix-attachment="{&quot;contentType&quot;:&quot;application/vnd.campfire.mention&quot;,&quot;sgid&quot;:&quot;signed-id&quot;,&quot;content&quot;:&quot;&lt;span class=\&quot;mention\&quot;&gt;Robot &amp; Co&lt;/span&gt;&quot;}">Robot</figure></div>"#;
        assert_eq!(
            super::action_text_webhook_html(mention_with_content),
            "<div><action-text-attachment sgid=\"signed-id\" content-type=\"application/vnd.campfire.mention\" content=\"<span class=&quot;mention&quot;>Robot &amp; Co</span>\"></action-text-attachment></div>"
        );
    }

    #[test]
    fn webhook_attachment_filenames_follow_campfire_mime_registry() {
        assert_eq!(super::campfire_webhook_attachment_extension("image/jpeg"), Some("jpeg"));
        assert_eq!(super::campfire_webhook_attachment_extension("application/zip"), Some("zip"));
        assert_eq!(super::campfire_webhook_attachment_extension("audio/mpeg"), Some("mp3"));
        assert_eq!(super::campfire_webhook_attachment_extension("text/plain"), Some("text"));
        assert_eq!(super::campfire_webhook_attachment_extension("text/html"), Some("html"));
        assert_eq!(super::campfire_webhook_attachment_extension("application/octet-stream"), None);
    }

    #[test]
    fn push_body_uses_attachment_filename_and_keeps_full_text() {
        let mut message = super::ChatMessage {
            id: 1,
            room_id: 1,
            room_kind: Some("Rooms::Open".into()),
            room_name: "General".into(),
            mention_ids: Vec::new(),
            creator_id: 1,
            creator_name: "Alex".into(),
            creator_role: 0,
            creator_updated_at: String::new(),
            body: String::new(),
            body_html: None,
            created_at: String::new(),
            updated_at: String::new(),
            client_message_id: String::new(),
            attachment: Some(super::Attachment {
                id: 1,
                filename: "report.pdf".into(),
                content_type: "application/pdf".into(),
                width: None,
                height: None,
            }),
            boosts: Vec::new(),
        };
        assert_eq!(super::push_message_body(&message, true), "report.pdf");
        assert_eq!(super::push_message_body(&message, false), "Alex: report.pdf");
        message.body = "x".repeat(3000);
        assert_eq!(super::push_message_body(&message, false).len(), 3006);
    }

    #[test]
    fn push_test_payload_matches_source() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert(axum::http::header::HOST, "rustfire.test:4242".parse().unwrap());
        let actual = super::push_test_payload(&headers, "fixed-body", 3);
        let expected = std::env::var("RUSTFIRE_EXPECTED_PUSH_TEST_PAYLOAD").unwrap_or_else(|_| {
            r#"{"title":"Campfire Test","options":{"body":"fixed-body","icon":"/account/logo","data":{"path":"http://rustfire.test:4242/users/me/push_subscriptions","badge":3}}}"#.to_string()
        });
        assert_eq!(actual, serde_json::from_str::<serde_json::Value>(&expected).unwrap());
    }

    #[test]
    fn old_direct_room_names_migrate_to_campfire_null_names() {
        let db = r2d2::Pool::builder()
            .max_size(1)
            .build(r2d2_sqlite::SqliteConnectionManager::memory())
            .unwrap();
        super::init_db(&db).unwrap();
        db.get().unwrap().execute_batch("INSERT INTO users(id,name,created_at,updated_at) VALUES(1,'Test','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(1,'Legacy name','Rooms::Direct',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');").unwrap();
        super::init_db(&db).unwrap();
        let name: Option<String> = db.get().unwrap().query_row("SELECT name FROM rooms WHERE id=1", [], |r| r.get(0)).unwrap();
        assert_eq!(name, None);
    }

    #[test]
    fn legacy_custom_styles_migrate_to_account_without_inventing_empty_styles() {
        for (legacy_css, expected) in [("body { color: navy; }", Some("body { color: navy; }")), ("", None)] {
            let db = r2d2::Pool::builder()
                .max_size(1)
                .build(r2d2_sqlite::SqliteConnectionManager::memory())
                .unwrap();
            {
                let conn = db.get().unwrap();
                conn.execute_batch("CREATE TABLE accounts(id INTEGER PRIMARY KEY,name TEXT NOT NULL,join_code TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                    INSERT INTO accounts VALUES(1,'Test','code','2026-01-01','2026-01-01');
                    CREATE TABLE account_custom_styles(id INTEGER PRIMARY KEY,css TEXT NOT NULL);")
                    .unwrap();
                conn.execute("INSERT INTO account_custom_styles VALUES(1,?1)", [legacy_css]).unwrap();
            }
            super::init_db(&db).unwrap();
            super::init_db(&db).unwrap();
            let styles: Option<String> = db.get().unwrap().query_row("SELECT custom_styles FROM accounts WHERE id=1", [], |r| r.get(0)).unwrap();
            assert_eq!(styles.as_deref(), expected);
        }
    }

    #[test]
    fn named_browser_versions_match_pinned_campfire_policy() {
        for (agent, blocked) in [
            ("Mozilla/5.0 Firefox/114.0", true),
            ("Mozilla/5.0 Firefox/121.0", false),
            ("Mozilla/5.0 Chrome/119.0.0.0 Safari/537.36", true),
            ("Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36", false),
            ("Mozilla/5.0 Version/17.1 Safari/605.1.15", true),
            ("Mozilla/5.0 Version/17.2 Safari/605.1.15", false),
            ("Mozilla/5.0 Chrome/117.0.0.0 Safari/537.36 OPR/103.0.0.0", true),
            ("Mozilla/5.0 Chrome/118.0.0.0 Safari/537.36 OPR/104.0.0.0", false),
            ("Mozilla/5.0 Trident/7.0; rv:11.0", true),
            ("Googlebot/2.1 Chrome/10.0.0.0", false),
            ("Mozilla/5.0 Chrome/119.0.0.0 Safari/537.36 Chrome-Lighthouse/11.0", false),
            ("Mozilla/5.0 Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0", true),
            ("Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", false),
            ("Mozilla/5.0 Chrome/119.0.0.0 Safari/537.36 Edge/119.0.0.0", false),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 CriOS/119.0.0.0 Safari/604.1", true),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 CriOS/120.0.0.0 Safari/604.1", false),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 FxiOS/120.0 Safari/605.1.15", true),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 FxiOS/120.0 Safari/605.1.15", false),
            ("curl/8.0.0", false),
            ("", false),
        ] {
            assert_eq!(super::browser_blocked(agent), blocked, "{agent}");
        }
    }

    #[test]
    fn unfurl_link_accepts_editor_json_and_form_posts() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert(axum::http::header::CONTENT_TYPE, "application/json".parse().unwrap());
        assert_eq!(super::unfurl_input(&headers, br#"{"url":"https://example.com/a"}"#).unwrap(), "https://example.com/a");
        headers.insert(axum::http::header::CONTENT_TYPE, "application/x-www-form-urlencoded; charset=UTF-8".parse().unwrap());
        assert_eq!(super::unfurl_input(&headers, b"url=https%3A%2F%2Fexample.com%2Fa").unwrap(), "https://example.com/a");
        assert_eq!(super::unfurl_input(&headers, b"url=").unwrap_err(), axum::http::StatusCode::BAD_REQUEST);
    }

    #[test]
    fn search_index_migrates_to_campfire_porter_tokenizer() {
        let db = r2d2::Pool::builder()
            .max_size(1)
            .build(r2d2_sqlite::SqliteConnectionManager::memory())
            .unwrap();
        super::init_db(&db).unwrap();
        {
            let conn = db.get().unwrap();
            conn.execute_batch("INSERT INTO users(id,name,created_at,updated_at) VALUES(1,'Test','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(1,'Test','Rooms::Open',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,1,'My hovercraft is full of eels','first','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                DROP TRIGGER message_fts_insert; DROP TRIGGER message_fts_update; DROP TRIGGER message_fts_delete;
                DROP TABLE message_search_index;
                CREATE VIRTUAL TABLE message_search_index USING fts5(body);
                CREATE TRIGGER message_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO message_search_index(rowid,body) VALUES(new.id,new.body); END;
                CREATE TRIGGER message_fts_update AFTER UPDATE OF body ON messages BEGIN UPDATE message_search_index SET body=new.body WHERE rowid=new.id; END;
                CREATE TRIGGER message_fts_delete AFTER DELETE ON messages BEGIN DELETE FROM message_search_index WHERE rowid=old.id; END;
                INSERT INTO message_search_index(rowid,body) SELECT id,body FROM messages;")
                .unwrap();
            let before: i64 = conn.query_row("SELECT count(*) FROM message_search_index WHERE body MATCH 'eel'", [], |r| r.get(0)).unwrap();
            assert_eq!(before, 0);
        }
        super::init_db(&db).unwrap();
        let conn = db.get().unwrap();
        let matched: i64 = conn.query_row("SELECT count(*) FROM message_search_index WHERE body MATCH 'eel'", [], |r| r.get(0)).unwrap();
        assert_eq!(matched, 1);
        conn.execute("UPDATE messages SET body='My hovercraft is full of sharks' WHERE id=1", []).unwrap();
        let former: i64 = conn.query_row("SELECT count(*) FROM message_search_index WHERE body MATCH 'eel'", [], |r| r.get(0)).unwrap();
        let current: i64 = conn.query_row("SELECT count(*) FROM message_search_index WHERE body MATCH 'shark'", [], |r| r.get(0)).unwrap();
        assert_eq!((former, current), (0, 1));
    }

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
    fn sidebar_streams_use_rails_signed_names_and_reject_tampering() {
        let key = super::rails_turbo_stream_key("test-secret-key-base").unwrap();
        let user_gid = super::URL_SAFE_NO_PAD.encode("gid://campfire/User/1");
        let user_stream = format!("{user_gid}:rooms");
        let global = super::turbo_stream_token(&key, "rooms").unwrap();
        let personal = super::turbo_stream_token(&key, &user_stream).unwrap();
        assert_eq!(global, "InJvb21zIg==--dcfabe3fec2e45adc36d44cfecefd60064e52b3421db32232366c884ec9d6c91");
        assert_eq!(personal, "IloybGtPaTh2WTJGdGNHWnBjbVV2VlhObGNpOHg6cm9vbXMi--8733c3a87d89b28e99f139a55694f39557f261923e592dd25230821bdb4d12b0");
        assert_eq!(super::turbo_stream_from_token(&key, &global).as_deref(), Some("rooms"));
        assert_eq!(super::turbo_stream_from_token(&key, &personal).as_deref(), Some(user_stream.as_str()));
        assert!(super::turbo_stream_from_token(&key, &format!("{personal}x")).is_none());
        assert!(super::turbo_stream_from_token(&[7; 64], &personal).is_none());
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
    fn signed_mentions_validate_signatures_but_users_survive_key_rotation() {
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
    fn unsigned_campfire_attachable_fallback_is_limited_to_users() {
        let key = [7u8; 32];
        let signed_user = super::mention_sgid(&key, 42).unwrap();
        let (message, _) = signed_user.split_once("--").unwrap();
        assert_eq!(super::mention_user_id_from_sgid(&[8u8; 32], None, &format!("{message}--invalid")), Some(42));
        let room = STANDARD.encode(serde_json::json!({"_rails":{"data":"gid://campfire/Room/42","pur":"attachable"}}).to_string());
        assert_eq!(super::mention_user_id_from_sgid(&key, None, &format!("{room}--invalid")), None);
        let legacy = STANDARD.encode(serde_json::json!({"_rails":{"message":"BAhJIhtnaWQ6Ly9jYW1wZmlyZS9Vc2VyLzQyBjoGRVQ=","exp":null,"pur":"attachable"}}).to_string());
        assert_eq!(super::mention_user_id_from_sgid(&key, None, &format!("{legacy}--invalid")), Some(42));
        assert_eq!(super::mention_user_id_from_sgid(&key, None, "invalid--invalid"), None);
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
    fn transfer_links_are_signed_and_expire() {
        let key = super::rails_avatar_key("test-secret-key-base").unwrap();
        let future = chrono::DateTime::parse_from_rfc3339("2030-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let token = super::transfer_token(&key, 42, future).unwrap();
        assert_eq!(super::transfer_id_from_token(&key, &token), Some(42));
        assert_eq!(super::transfer_id_from_token(&[8u8; 32], &token), None);
        assert_eq!(
            super::transfer_id_from_token(&key, &format!("{token}A")),
            None
        );
        let expired = chrono::DateTime::parse_from_rfc3339("2020-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        assert_eq!(
            super::transfer_id_from_token(&key, &super::transfer_token(&key, 42, expired).unwrap()),
            None
        );
        assert_eq!(
            super::transfer_id_from_token(&key, &super::avatar_token(&key, 42).unwrap()),
            None
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
    fn outbound_address_policy_matches_pinned_campfire_transition_ranges() {
        for address in [
            "168.63.129.16", "100.64.0.1", "192.0.2.1", "198.18.0.1",
            "::ffff:93.184.216.34", "::93.184.216.34",
            "::ffff:0:169.254.169.254", "::ffff:0:127.0.0.1",
            "64:ff9b::a9fe:a9fe", "64:ff9b::a00:5",
            "64:ff9b:1::808:808", "2002:a9fe:a9fe::", "2001::1",
            "2001:2::1", "2001:db8::1", "3fff::1", "5f00::1",
            "fec0::1", "ff02::1",
        ] {
            assert!(!super::public_network_ip(address.parse().unwrap()), "{address}");
        }
        for address in [
            "93.184.216.34", "8.8.8.8", "64:ff9b::808:808",
            "2606:4700:4700::1111", "2001:3::1", "2001:4:112::1",
        ] {
            assert!(super::public_network_ip(address.parse().unwrap()), "{address}");
        }
    }
    #[test]
    fn link_preview_media_filter_matches_campfire_url_pattern() {
        for url in [
            "https://example.com/100gb.zip",
            "http://example.com/video.mp4",
            "https://example.com/archive.tar.gz",
            "https://example.com/image.jpg?page=1",
            "https://example.com/image.jpg/next",
            "https://example.com/page?file=image.png",
        ] {
            assert!(super::media_link(url), "{url}");
        }
        for url in [
            "https://example.com/page",
            "https://example.com/image.JPG",
            "https://example.com/photo.jpeg_extra",
            "HTTPS://example.com/video.mp4",
        ] {
            assert!(!super::media_link(url), "{url}");
        }
    }
    #[test]
    fn preview_image_content_type_matches_campfire_exact_header_check() {
        for content_type in ["image/jpeg", "image/png", "image/gif", "image/webp", "IMAGE/PNG"] {
            assert!(super::allowed_og_image_content_type(content_type), "{content_type}");
        }
        for content_type in [
            "image/svg+xml", "text/html", "image/png; charset=UTF-8", "image/png ", "",
        ] {
            assert!(!super::allowed_og_image_content_type(content_type), "{content_type}");
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
    fn push_targets_follow_campfire_membership_scopes_even_after_ban() {
        let db = r2d2::Pool::builder()
            .max_size(1)
            .build(r2d2_sqlite::SqliteConnectionManager::memory())
            .unwrap();
        super::init_db(&db).unwrap();
        let conn = db.get().unwrap();
        for id in 1..=8 {
            conn.execute("INSERT INTO users(id,name,status,created_at,updated_at) VALUES(?1,?2,?3,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", rusqlite::params![id,format!("User {id}"),if id==2||id==4 {2} else {0}]).unwrap();
        }
        conn.execute("INSERT INTO rooms(id,name,type,creator_id,created_at,updated_at) VALUES(1,'Room','Rooms::Open',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", []).unwrap();
        for (id, involvement) in [(1,"everything"),(2,"everything"),(3,"everything"),(4,"mentions"),(5,"mentions"),(6,"invisible"),(7,"everything"),(8,"nothing")] {
            conn.execute("INSERT INTO memberships(room_id,user_id,involvement,unread_at,connected_at,created_at) VALUES(1,?1,?2,?3,?4,'2026-01-01T00:00:00Z')", rusqlite::params![id,involvement,if id==2 {Some("2026-01-01T00:00:00Z")} else {None},if id==7 {Some("2026-01-02T00:00:00Z")} else {None}]).unwrap();
            conn.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(?1,?1,?2,'key','auth','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", rusqlite::params![id,format!("https://fcm.googleapis.com/fcm/send/{id}")]).unwrap();
        }
        let recipients = super::push_recipients(&conn, 1, 1, "2026-01-01T00:00:00Z", &[4]).unwrap();
        assert_eq!(recipients.iter().map(|(subscription, badge)| (subscription.id, *badge)).collect::<Vec<_>>(), vec![(2,1),(3,0),(4,0)]);
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
    fn opengraph_metadata_removes_entity_encoded_markup() {
        let encoded = "&#x3c;&#x69;&#x6d;&#x67;&#x20;&#x73;&#x72;&#x63;&#x3d;&#x61;&#x20;&#x6f;&#x6e;&#x65;&#x72;&#x72;&#x6f;&#x72;&#x3d;&#x70;&#x72;&#x6f;&#x6d;&#x70;&#x74;&#x28;&#x31;&#x29;&#x3e;";
        let document = format!("<meta property='og:title' content='{encoded}Hey!'><meta property='og:description' content='{encoded}desc..'>");
        let tags = super::og_attributes(&document);
        assert_eq!(super::clean_og_text(tags.get("title").unwrap()), "Hey!");
        assert_eq!(super::clean_og_text(tags.get("description").unwrap()), "desc..");
        assert_eq!(super::clean_og_text("<img src='x' onerror='alert(1)'>"), "");
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
        assert!(html.contains("class=\"og-embed gap\""), "{html}");
        assert!(html.contains("class=\"og-embed__content\""), "{html}");
        assert!(html.contains("class=\"og-embed__title\""), "{html}");
        assert!(html.contains("class=\"og-embed__description\""), "{html}");
        assert!(html.contains("class=\"og-embed__image\""), "{html}");
        assert!(html.contains("class=\"image center\""), "{html}");
    }
    #[test]
    fn solo_preview_hides_duplicate_url_only_in_presentation() {
        let attachment = "<action-text-attachment content-type='application/vnd.actiontext.opengraph-embed' href='https://example.com/page' url='https://example.com/image.png' filename='Example' caption='Description'></action-text-attachment>";
        let source = format!("<div>https://example.com/page{attachment}</div>");
        let (plain, html) = super::rich_body(&source, None);
        assert_eq!(plain, "https://example.com/page");
        assert!(html.contains("https://example.com/page<div"), "{html}");
        let presented = super::preview_presentation_body(&plain, &html);
        assert!(!presented.contains("https://example.com/page<div"), "{presented}");
        assert!(presented.contains("class=\"og-embed gap\""));
        let with_text = format!("<div>See https://example.com/page{attachment}</div>");
        let (plain, html) = super::rich_body(&with_text, None);
        assert!(super::preview_presentation_body(&plain, &html).contains("See https://example.com/page"));
    }
    #[test]
    fn tweet_preview_normalizes_domain_and_styles_profile_image() {
        let source = "<div>https://x.com/37signals/status/123?s=20<action-text-attachment content-type='application/vnd.actiontext.opengraph-embed' href='https://twitter.com/37signals/status/123' url='https://pbs.twimg.com/profile_images/example/avatar.png' filename='Example' caption='Description'></action-text-attachment></div>";
        let (plain, html) = super::rich_body(source, None);
        let presented = super::preview_presentation_body(&plain, &html);
        assert!(!presented.contains("https://x.com/37signals/status/123?s=20"), "{presented}");
        assert!(presented.starts_with("<div class='cf-twitter-avatar'>"), "{presented}");
        let multiple = "<div>Two cards<div class='og-embed'><div class='og-embed__image'><img src='https://example.com/first.png'></div></div><div class='og-embed'><div class='og-embed__image'><img src='https://pbs.twimg.com/profile_images/example/second.png'></div></div></div>";
        assert!(super::preview_presentation_body("Two cards", multiple).starts_with("<div class='cf-twitter-avatar'>"));
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
