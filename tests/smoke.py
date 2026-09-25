"""End-to-end HTTP smoke test for Rustfire. Run after cargo build."""
import http.cookiejar
import http.server
import html
import base64
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSRF = {}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def request(opener, base, path, data=None, method=None, headers=None):
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data, doseq=True).encode()
    headers = dict(headers or {})
    if (data is not None or method in ("POST", "PATCH", "PUT", "DELETE")) and opener in CSRF:
        headers.setdefault("X-CSRF-Token", CSRF[opener])
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with opener.open(req) as res:
            body = res.read().decode()
            token = re.search(r"<meta name='csrf-token' content='([^']+)'", body)
            if token:
                CSRF[opener] = html.unescape(token.group(1))
            return res.status, res.geturl(), body
    except urllib.error.HTTPError as error:
        return error.code, error.geturl(), error.read().decode()


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, RUSTFIRE_ADDR=f"127.0.0.1:{port}", RUSTFIRE_DB=f"{tmp}/test.db", RUSTFIRE_UPLOAD_DIR=f"{tmp}/uploads", RUSTFIRE_TRUSTED_PROXY_IPS="127.0.0.1", RUSTFIRE_DISABLE_PUSH="1", RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE="test-secret-key-base")
        process = subprocess.Popen([str(ROOT / "target/debug/rustfire")], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        webhook_server = None
        try:
            for _ in range(100):
                try:
                    if request(client(), base, "/up")[0] == 200:
                        break
                except urllib.error.URLError:
                    time.sleep(.05)
            else:
                raise AssertionError("server did not start")
            code, _, manifest = request(client(), base, "/webmanifest")
            assert code == 200 and json.loads(manifest)["display"] == "standalone"
            code, _, worker = request(client(), base, "/service-worker")
            assert code == 200 and "notificationclick" in worker
            admin = client()
            assert request(admin, base, "/")[1].endswith("/first_run")
            assert request(admin, base, "/first_run", {"name": "Forged", "email_address": "forged@example.com", "password": "password123"}, headers={"X-CSRF-Token": "wrong"})[0] == 403
            code, url, _ = request(admin, base, "/first_run", {"name": "Admin", "email_address": "admin@example.com", "password": "password123"})
            assert code == 200 and "/rooms/1" in url, (code, url)
            code, _, page = request(admin, base, "/rooms/1")
            assert code == 200 and "Campfire" in page and "name='authenticity_token'" in page and "id='messages_rooms_open_1'" in page
            assert request(admin, base, "/rooms/1/messages")[0] == 204
            assert request(admin, base, "/unfurl_link", json.dumps({"url":"http://127.0.0.1/secret"}).encode(), method="POST", headers={"Content-Type":"application/json"})[0] == 204
            assert request(admin, base, "/unfurl_link", json.dumps({"url":"file:///etc/passwd"}).encode(), method="POST", headers={"Content-Type":"application/json"})[0] == 204
            assert request(admin, base, "/rooms/1/messages?before=999")[0] == 404
            assert request(admin, base, "/rooms/1/messages", {"message[body]": "forged"}, headers={"X-CSRF-Token":"wrong"})[0] == 403
            form_only = urllib.request.Request(base + "/rooms/1/involvement", data=urllib.parse.urlencode({"involvement":"mentions", "authenticity_token":CSRF[admin]}).encode())
            with admin.open(form_only) as res:
                assert res.status == 200
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]": "hello from smoke", "message[client_message_id]": "test-1"}, headers={"Accept": "application/json"})
            assert code == 201, (code, payload)
            message = json.loads(payload)
            assert message["body"]["plain_text"] == "hello from smoke"
            assert message["body"]["html"] == '<div class="trix-content">\n  hello from smoke\n</div>\n'
            assert message["url"] == f"{base}/rooms/1/messages/{message['id']}"
            expected_avatar_token = "eyJfcmFpbHMiOnsiZGF0YSI6MSwicHVyIjoidXNlci9hdmF0YXIifX0--fe99b8547975d867621732d6e0d4344cea012c7eaf713418ef6b1414a24e2dd4"
            assert re.fullmatch(re.escape(f"{base}/users/{expected_avatar_token}/avatar?v=") + r"\d{14}", message["creator"]["avatar_url"])
            assert set(message) == {"id", "created_at", "body", "creator", "room", "url"}
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", message["created_at"])
            code, _, page = request(admin, base, f"/rooms/1/@{message['id']}")
            assert code == 200 and "hello from smoke" in page and "data-at-message='1'" in page and "id='composer'" in page, code
            assert f"<span class='message__room'><a href='/rooms/1/@{message['id']}' target='_top' data-reply-target='link'>Campfire</a></span>" in page
            assert "custom-boost-form" not in page
            code, _, boost_frame = request(admin, base, f"/messages/{message['id']}/boosts/new", headers={"Turbo-Frame": "new_boost_message_test-1"})
            assert code == 200 and "<turbo-frame id='new_boost_message_test-1'>" in boost_frame and "class='custom-boost-form boost__form" in boost_frame
            browser_visitor = client()
            code, login_url, _ = request(browser_visitor, base, f"/rooms/1/@{message['id']}", headers={"Accept": "text/html"})
            assert code == 200 and login_url.endswith("/session/new")
            code, return_url, page = request(browser_visitor, base, "/session", {"email_address": "admin@example.com", "password": "password123"})
            assert code == 200 and return_url.endswith(f"/rooms/1/@{message['id']}") and "hello from smoke" in page
            boost_since = int(time.time() * 1000)
            time.sleep(.02)
            code, _, page = request(admin, base, f"/messages/{message['id']}/boosts", {"content": "👍"})
            assert code == 200 and "👍" in page
            code, _, payload = request(admin, base, f"/rooms/1/refresh?since={boost_since}", headers={"Accept": "application/json"})
            boosted = json.loads(payload)
            assert code == 200 and boosted["messages"] == [] and [entry["id"] for entry in boosted["updated"]] == [message["id"]]
            assert "👍" in boosted["updated"][0]["html"]
            assert request(client(),base,f"/messages/{message['id']}/boosts")[0]==401
            assert "Add a boost" in request(admin,base,f"/messages/{message['id']}/boosts/new")[2]
            code,_,page=request(admin,base,f"/messages/{message['id']}/boosts",{"boost[content]":"🔥"})
            assert code==200 and "🔥" in page
            custom_id=int(re.search(r"<li id='boost-(\d+)'>🔥",page).group(1))
            code,_,page=request(admin,base,f"/messages/{message['id']}/boosts",{"boost[content]":"🔥"})
            duplicate_id=max(int(value) for value in re.findall(r"<li id='boost-(\d+)'>🔥",page))
            assert duplicate_id>custom_id
            assert request(admin,base,f"/messages/{message['id']}/boosts/{custom_id}/delete",{},method="POST")[0]==200
            code,_,room_page=request(admin,base,"/rooms/1")
            assert code==200 and f"id='boost_{custom_id}'" not in room_page and f"id='boost_{duplicate_id}'" in room_page
            boundary = "test-boundary"
            multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"message[body]\"\r\n\r\nattached\r\n"
                         f"--{boundary}\r\nContent-Disposition: form-data; name=\"message[attachment]\"; filename=\"note.txt\"\r\nContent-Type: text/plain\r\n\r\nfile contents\r\n"
                         f"--{boundary}--\r\n").encode()
            code, _, payload = request(admin, base, "/rooms/1/messages", data=multipart, method="POST", headers={"Accept":"application/json", "Content-Type":f"multipart/form-data; boundary={boundary}"})
            assert code == 201, (code, payload)
            attachment_message = json.loads(payload)
            attachment_id = sqlite3.connect(f"{tmp}/test.db").execute("SELECT id FROM attachments WHERE message_id=?", (attachment_message["id"],)).fetchone()[0]
            code, _, payload = request(admin, base, f"/rooms/1/refresh?after={message['id']}", headers={"Accept":"application/json"})
            refresh = json.loads(payload)
            assert code == 200 and [m["id"] for m in refresh["messages"]] == [attachment_message["id"]]
            assert refresh["next_after"] == attachment_message["id"] and not refresh["has_more"]
            with admin.open(urllib.request.Request(base + f"/rooms/1/refresh?after={message['id']}", headers={"Accept": "text/vnd.turbo-stream.html"})) as response:
                assert response.headers.get_content_type() == "text/vnd.turbo-stream.html"
                assert "action='append'" in response.read().decode()
            code, _, content = request(admin, base, f"/attachments/{attachment_id}")
            assert code == 200 and content == "file contents"
            assert request(admin, base, f"/attachments/{attachment_id}", headers={"Range":"bytes=0-3"}) == (206, base+f"/attachments/{attachment_id}", "file")
            assert request(admin, base, f"/attachments/{attachment_id}", headers={"Range":"bytes=999-"})[0] == 416
            code, _, payload = request(admin, base, "/rooms/1/messages", headers={"Accept": "application/json"})
            assert code == 200 and len(json.loads(payload)) == 2
            code, _, page = request(admin, base, "/searches?q=hello")
            assert code == 200 and "hello from smoke" in page
            code, _, page = request(admin, base, "/searches", {"q": "hello"})
            assert code == 200 and "Recent searches" in page
            code, _, page = request(admin, base, "/searches/clear", {})
            assert code == 200 and "href='/searches?q=hello'" not in page
            refresh_since = int(time.time() * 1000)
            time.sleep(.02)
            code, _, page = request(admin, base, f"/rooms/1/messages/{message['id']}", {"message[body]": "hello edited"}, method="PATCH")
            assert code == 303
            code, _, payload = request(admin, base, f"/rooms/1/refresh?since={refresh_since}", headers={"Accept": "application/json"})
            refreshed = json.loads(payload)
            assert code == 200 and refreshed["messages"] == [] and [entry["id"] for entry in refreshed["updated"]] == [message["id"]], refreshed
            assert "hello edited" in refreshed["updated"][0]["html"] and refreshed["checked_at"] >= refresh_since
            code, _, payload = request(admin, base, f"/rooms/1/refresh?since={refresh_since}", headers={"Accept": "text/vnd.turbo-stream.html"})
            assert code == 200 and "action='replace' target='message_test-1'" in payload and "hello edited" in payload
            code, _, page = request(admin, base, "/rooms/1")
            assert code == 200 and "hello edited" in page
            code, _, page = request(admin, base, "/rooms/1/settings")
            assert code == 200 and "Room settings" in page
            code, _, page = request(admin, base, "/account")
            join = re.search(r"/join/([\w-]+)", page).group(1)
            qr = re.search(r"/qr_code/[\w-]+", page).group(0)
            code, _, svg = request(client(), base, qr)
            assert code == 200 and "<svg" in svg and "QR code" in svg and "<path" in svg
            assert request(client(), base, "/qr_code/not-base64!")[0] == 400
            member = client()
            assert request(member, base, f"/join/{join}")[0] == 200
            code, _, _ = request(member, base, f"/join/{join}", {"name": "Member", "email_address": "member@example.com", "password": "password123"}, headers={"X-Forwarded-For": "203.0.113.20, 8.8.8.8"})
            assert code == 200
            code, _, page = request(member, base, "/users/me/profile", {"name": "Member Two", "email_address": "member2@example.com", "password": "newpassword123", "bio": "I like fires"})
            assert code == 200 and "I like fires" in page
            code, _, page = request(member, base, "/users/me/profile", {"user[name]": "Member Two", "user[bio]": "Nested profile"}, method="PATCH")
            assert code == 303
            code, _, page = request(member, base, "/users/me/profile")
            assert code == 200 and "Nested profile" in page
            assert re.search(r"meta name='vapid-public-key' content='[A-Za-z0-9_-]+'", page)
            assert request(member, base, "/users/me/push_subscriptions")[0] == 200
            assert request(admin, base, "/users/me/push_subscriptions")[0] == 200
            push_keys={"endpoint":"https://fcm.googleapis.com/fcm/send/test", "p256dh_key":base64.urlsafe_b64encode(b"\x04"+b"\x01"*64).rstrip(b"=").decode(), "auth_key":base64.urlsafe_b64encode(b"\x02"*16).rstrip(b"=").decode()}
            def subscription(body):
                return request(member, base, "/users/me/push_subscriptions", json.dumps({"push_subscription":body}).encode(), method="POST", headers={"Content-Type":"application/json"})
            assert subscription(dict(push_keys,endpoint="https://example.com/push"))[0] == 422
            assert subscription(dict(push_keys,p256dh_key="bad"))[0] == 200
            assert request(member,base,"/users/me/push_subscriptions",{"push_subscription[endpoint]":push_keys["endpoint"],"push_subscription[p256dh_key]":push_keys["p256dh_key"],"push_subscription[auth_key]":push_keys["auth_key"]})[0]==200
            assert subscription(push_keys)[0] == 200
            code,_,subscriptions=request(member,base,"/users/me/push_subscriptions")
            assert code==200 and push_keys["endpoint"] in html.unescape(subscriptions)
            subscription_id=int(re.search(r"push_subscriptions/(\d+)/test_notifications",subscriptions).group(1))
            assert request(admin,base,f"/users/me/push_subscriptions/{subscription_id}/test_notifications",{},method="POST")[0]==404
            assert request(member,base,f"/users/me/push_subscriptions/{subscription_id}/test_notifications",{},method="POST")[0]==200
            assert request(member,base,f"/users/me/push_subscriptions/{subscription_id}/delete",{},method="POST")[0]==200
            assert push_keys["endpoint"] not in html.unescape(request(member,base,"/users/me/push_subscriptions")[2])
            png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=")
            image_body=(b"--image-test\r\nContent-Disposition: form-data; name=\"message[attachment]\"; filename=\"image.png\"\r\nContent-Type: image/png\r\n\r\n"+png+b"\r\n--image-test--\r\n")
            code,_,payload=request(admin,base,"/rooms/1/messages",data=image_body,method="POST",headers={"Accept":"application/json","Content-Type":"multipart/form-data; boundary=image-test"})
            assert code==201,(code,payload)
            image_id=sqlite3.connect(f"{tmp}/test.db").execute("SELECT id FROM attachments WHERE message_id=?", (json.loads(payload)["id"],)).fetchone()[0]
            assert f"/attachments/{image_id}/thumb" in request(admin,base,"/rooms/1")[2]
            with admin.open(base+f"/attachments/{image_id}/thumb") as res:
                assert res.status==200 and res.headers.get_content_type()==("image/webp" if shutil.which("vips") else "image/png") and len(res.read())>0
            if shutil.which("ffmpeg") and shutil.which("ffprobe"):
                video_file=pathlib.Path(tmp)/"clip.mp4"
                subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=c=red:s=16x16:r=5:d=1","-c:v","mpeg4","-y",str(video_file)],check=True)
                video_body=(b"--video-test\r\nContent-Disposition: form-data; name=\"message[attachment]\"; filename=\"clip.mp4\"\r\nContent-Type: video/mp4\r\n\r\n"+video_file.read_bytes()+b"\r\n--video-test--\r\n")
                code,_,payload=request(admin,base,"/rooms/1/messages",data=video_body,method="POST",headers={"Accept":"application/json","Content-Type":"multipart/form-data; boundary=video-test"})
                assert code==201,(code,payload)
                video_id=sqlite3.connect(f"{tmp}/test.db").execute("SELECT id FROM attachments WHERE message_id=?", (json.loads(payload)["id"],)).fetchone()[0]
                assert f"/attachments/{video_id}/poster" in request(admin,base,"/rooms/1")[2]
                with admin.open(urllib.request.Request(base+f"/attachments/{video_id}",headers={"Range":"bytes=0-9"})) as res:
                    assert res.status==206 and res.headers.get_content_type()=="video/mp4" and len(res.read())==10
                with admin.open(base+f"/attachments/{video_id}/poster") as res:
                    assert res.status==200 and res.headers.get_content_type()=="image/webp" and len(res.read())>0
            avatar_body=(b"--avatar-test\r\nContent-Disposition: form-data; name=\"avatar\"; filename=\"avatar.png\"\r\nContent-Type: image/png\r\n\r\n"+png+b"\r\n--avatar-test--\r\n")
            code, _, _ = request(member, base, "/users/me/avatar", data=avatar_body, method="POST", headers={"Content-Type":"multipart/form-data; boundary=avatar-test"})
            assert code == 200
            with member.open(base+"/users/2/avatar") as res:
                image = res.read()
                assert res.status==200 and res.headers.get_content_type()=="image/webp" and image[:4]==b"RIFF" and image[8:12]==b"WEBP"
            stored_avatar = sqlite3.connect(f"{tmp}/test.db").execute("SELECT stored_name FROM avatars WHERE user_id=2").fetchone()[0]
            avatar_variant = pathlib.Path(tmp) / "uploads" / "avatars" / "variants" / f"{stored_avatar}.webp"
            assert avatar_variant.is_file()
            code, _, _ = request(member, base, "/users/me/avatar/delete", {})
            assert code == 200 and not avatar_variant.exists()
            second_session = client()
            assert request(second_session, base, "/session/new")[0] == 200
            code, _, _ = request(second_session, base, "/session", {"email_address": "member2@example.com", "password": "newpassword123"})
            assert code == 200
            code, _, _ = request(admin, base, "/rooms/1/messages", {"message[body]": "second", "message[client_message_id]": "test-2"}, headers={"Accept": "application/json"})
            assert code == 201
            code, _, page = request(member, base, "/users/me/sidebar")
            assert code == 200 and "unread" in page
            code, _, _ = request(member, base, "/rooms/1/involvement", {"involvement": "everything"})
            assert code == 200
            code, _, page = request(member, base, "/users/me/sidebar")
            assert code == 200 and "unread" in page
            assert request(member, base, "/rooms/1")[0] == 200
            code, _, page = request(member, base, "/users/me/sidebar")
            assert code == 200 and "unread" not in page
            assert request(member, base, "/rooms/1/involvement", {"involvement": "invisible"})[0] == 200
            with member.open(base + "/users/me/sidebar?active=1") as sidebar_response:
                assert sidebar_response.headers["x-rustfire-active-room-accessible"] == "1"
                assert "href='/rooms/1'" not in sidebar_response.read().decode()
            assert request(member, base, "/rooms/1")[0] == 200
            assert request(member, base, "/rooms/1/involvement", {"involvement": "everything"})[0] == 200
            code, _, payload = request(admin, base, "/rooms/closeds", {"name": "Private", "user_ids": "1"})
            assert code == 200, (code, payload)
            code, redirected, _ = request(member, base, "/rooms/2")
            assert code == 200 and redirected.endswith("/rooms/1"), (code, redirected)
            with member.open(base + "/users/me/sidebar?active=2") as sidebar_response:
                assert sidebar_response.headers["x-rustfire-active-room-accessible"] == "0"
            code, _, page = request(admin, base, "/rooms/closeds/2/edit")
            assert code == 200 and "Room settings" in page
            code, _, _ = request(admin, base, "/rooms/closeds/2", {"room[name]": "Private Two", "user_ids[]": "1"}, method="PATCH")
            assert code == 303
            code, _, page = request(admin, base, "/rooms/2")
            assert code == 200 and "Private Two" in page and "id='messages_rooms_closed_2'" in page
            assert request(admin, base, "/")[1].endswith("/rooms/2")
            assert request(admin, base, "/rooms/1")[0] == 200
            assert request(admin, base, "/")[1].endswith("/rooms/1")
            assert request(admin, base, "/rooms")[1].endswith("/rooms/2")
            code, _, _ = request(admin, base, "/rooms/closeds/2", {"room[name]": "Private Two", "user_ids[]": ["1", "2"]}, method="PATCH")
            assert code == 303
            assert request(member, base, "/rooms/2")[0] == 200
            assert request(member, base, "/rooms/2/involvement", {"involvement": "everything"})[0] == 200
            assert request(admin, base, "/rooms/2/messages", {"message[body]": "private unread"}, headers={"Accept": "application/json"})[0] == 201
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                membership_before = check_db.execute("SELECT id,involvement,unread_at FROM memberships WHERE room_id=2 AND user_id=2").fetchone()
            assert membership_before[1] == "everything" and membership_before[2]
            code, _, _ = request(admin, base, "/rooms/closeds/2", {"room[name]": "Private Two Renamed", "user_ids[]": ["1", "2"]}, method="PATCH")
            assert code == 303
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT id,involvement,unread_at FROM memberships WHERE room_id=2 AND user_id=2").fetchone() == membership_before
            code, _, _ = request(admin, base, "/rooms/closeds/2", {"room[name]": "Private Two", "user_ids[]": "1"}, method="PATCH")
            assert code == 303
            code, redirected, _ = request(member, base, "/rooms/2")
            assert code == 200 and redirected.endswith("/rooms/1"), (code, redirected)
            code, _, _ = request(member, base, "/rooms/2/refresh?after=0", headers={"Accept":"application/json"})
            assert code == 404
            code, _, payload = request(member, base, "/autocompletable/users?room_id=2")
            assert code == 404
            code, _, payload = request(admin, base, "/autocompletable/users?room_id=2")
            assert code == 200 and [person["name"] for person in json.loads(payload)] == ["Admin"]
            code, _, payload = request(member, base, "/autocompletable/users?room_id=1&query=Admin")
            admin_suggestion = json.loads(payload)[0]
            assert code == 200 and admin_suggestion["value"] == 1 and set(admin_suggestion) == {"value", "name", "avatar_url", "sgid"}
            avatar_url = urllib.parse.urlparse(admin_suggestion["avatar_url"])
            assert avatar_url.path == f"/users/{expected_avatar_token}/avatar"
            assert re.fullmatch(r"v=\d{14}", avatar_url.query)
            code, _, avatar_svg = request(member, base, avatar_url.path + "?" + avatar_url.query)
            assert code == 200 and 'fill="#BF7C2A"' in avatar_svg and ">A</text>" in avatar_svg
            assert request(member, base, avatar_url.path.replace(expected_avatar_token, expected_avatar_token[:-1] + "0"))[0] == 404
            assert admin_suggestion["sgid"]
            admin_mention_sgid = admin_suggestion["sgid"]
            code, _, _ = request(admin, base, "/rooms/closeds/2", {"room[name]": "Private Two", "user_ids[]": "2"}, method="PATCH")
            assert code == 303
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT user_id FROM memberships WHERE room_id=2").fetchall() == [(2,)]
            assert request(admin, base, "/rooms/2")[1].endswith("/rooms/1")
            assert request(member, base, "/rooms/2")[0] == 200
            code, _, ping_page = request(admin, base, "/rooms/directs/new")
            assert code == 200 and "id='ping-form'" in ping_page and "id='ping-suggestions'" in ping_page
            assert "type='checkbox' name='user_ids'" not in ping_page
            admin_sidebar = request(admin, base, "/users/me/sidebar")[2]
            assert "data-ping-user-id='2'" in admin_sidebar
            assert f"name='authenticity_token' value='{CSRF[admin]}'" in admin_sidebar
            second_admin = client()
            assert request(second_admin, base, "/session/new")[0] == 200
            assert request(second_admin, base, "/session", {"email_address": "admin@example.com", "password": "password123"})[0] == 200
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
                creations = list(workers.map(lambda opener: request(opener, base, "/rooms/directs", {"user_ids": "2"}), [admin, second_admin]))
            code, direct_url, _ = creations[0]
            assert creations[1][0] == 200 and creations[1][1] == direct_url, creations
            assert code == 200 and "/rooms/3" in direct_url
            assert "data-ping-user-id='2'" not in request(admin, base, "/users/me/sidebar")[2]
            assert "data-ping-user-id='1'" not in request(member, base, "/users/me/sidebar")[2]
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT count(*) FROM rooms WHERE type='Rooms::Direct'").fetchone() == (1,)
                assert check_db.execute("SELECT member_ids FROM direct_room_sets WHERE room_id=3").fetchone() == ("1,2",)
                plan = " ".join(row[3] for row in check_db.execute("EXPLAIN QUERY PLAN SELECT room_id FROM direct_room_sets WHERE member_ids='1,2' ORDER BY room_id LIMIT 1"))
                assert "idx_direct_room_sets_members" in plan, plan
            code, _, page = request(member, base, "/rooms/3")
            assert code == 200 and "class='room-pill'>Admin</h1>" in page and "id='messages_rooms_direct_3'" in page
            code, repeated_url, _ = request(admin, base, "/rooms/directs", {"user_ids": "2"})
            assert repeated_url == direct_url
            assert request(member, base, "/rooms/3/involvement", {"involvement": "invisible"})[0] == 200
            assert "href='/rooms/3'" not in request(member, base, "/users/me/sidebar")[2]
            assert request(member, base, "/rooms/3")[0] == 200
            assert request(member, base, "/rooms/3/involvement", {"involvement": "everything"})[0] == 200
            assert "href='/rooms/3'" in request(member, base, "/users/me/sidebar")[2]
            code, _, page = request(member, base, "/rooms/directs/3/edit")
            assert code == 200 and "Ping settings" in page
            code, alias_url, _ = request(member, base, "/rooms/directs/3")
            assert code == 200 and alias_url.endswith("/rooms/3")
            assert request(member, base, "/rooms/directs/2/edit")[0] == 404
            code, _, _ = request(member, base, "/account/update", {"name": "Nope"})
            assert code == 403
            code, _, page = request(admin, base, "/account/update", {"name": "Team Fire"})
            assert code == 200 and "Team Fire" in page
            code, _, page = request(admin, base, "/account", {"account[name]": "Team Fire"}, method="PATCH")
            assert code == 303
            code, _, page = request(admin, base, "/account")
            assert code == 200 and "Team Fire" in page
            code, _, page = request(admin, base, "/account/custom_styles/edit")
            assert code == 200 and "Custom styles" in page
            code, _, _ = request(member, base, "/account/custom_styles", {"account[custom_styles]": "body{color:red}"})
            assert code == 403
            code, _, page = request(admin, base, "/account/custom_styles", {"account[custom_styles]": "body{color:#123456}"})
            assert code == 200 and "body{color:#123456}" in page
            code, _, css = request(client(), base, "/account/custom_styles.css")
            assert code == 200 and css == "body{color:#123456}"
            code, _, _ = request(admin, base, "/account/update", {"name": "Team Fire", "restrict_room_creation": "on"})
            assert code == 200
            code, _, _ = request(member, base, "/rooms/opens", {"name": "Not allowed"})
            assert code == 403
            code, _, _ = request(admin, base, "/account", {"account[settings][restrict_room_creation_to_administrators]": "false"}, method="PUT")
            assert code == 303
            code, _, _ = request(admin, base, "/account/update", {"name": "Team Fire", "restrict_room_creation": "off"})
            assert code == 200
            logo_body=(b"--logo-test\r\nContent-Disposition: form-data; name=\"logo\"; filename=\"logo.png\"\r\nContent-Type: image/png\r\n\r\n"+png+b"\r\n--logo-test--\r\n")
            code, _, _ = request(admin, base, "/account/logo", data=logo_body, method="POST", headers={"Content-Type":"multipart/form-data; boundary=logo-test"})
            assert code == 200
            with client().open(base+"/account/logo") as res:
                assert res.status==200 and res.read()==png
            code, _, _ = request(admin, base, "/account/logo/delete", {})
            assert code == 200
            code, _, _ = request(admin, base, "/account/users/1/role", {"role": "member"})
            assert code == 409
            code, _, _ = request(admin, base, "/account/users/2/role", {"role": "administrator"})
            assert code == 200
            code, _, _ = request(admin, base, "/account/users/2", {"user[role]": "member"}, method="PATCH")
            assert code == 303
            code, _, _ = request(admin, base, "/account/users/2", {"user[role]": "administrator"}, method="PATCH")
            assert code == 303
            code, _, page = request(admin, base, "/account/join_code", {})
            assert code == 200 and f"/join/{join}" not in page
            code, _, _ = request(client(), base, f"/join/{join}")
            assert code == 404
            code, _, page = request(admin, base, "/account/bots")
            assert code == 200
            code, _, page = request(admin, base, "/account/bots", {"name": "Robot"})
            key = re.search(r"key: <code>([\w-]+)</code>", page).group(1)
            code, _, page = request(admin, base, "/account/bots/3/edit")
            assert code == 200 and "Robot" in page and key in page
            code, _, _ = request(admin, base, "/account/bots/3/avatar", data=avatar_body, method="POST", headers={"Content-Type":"multipart/form-data; boundary=avatar-test"})
            assert code == 200
            with admin.open(base+"/users/3/avatar") as res:
                image = res.read()
                assert res.status==200 and res.headers.get_content_type()=="image/webp" and image[:4]==b"RIFF" and image[8:12]==b"WEBP"
            code, _, _ = request(admin, base, "/account/bots/3/avatar/delete", {})
            assert code == 200
            code, _, _ = request(client(), base, "/account/bots/3/key", {})
            assert code == 401
            code, _, page = request(admin, base, "/account/bots/3/update", {"name": "Rust Robot"})
            assert code == 200 and "Rust Robot" in page
            code, _, payload = request(client(), base, f"/rooms/1/{key}/messages", data=b"from bot", method="POST")
            assert code == 201 and payload == "", code
            code, _, payload = request(client(), base, f"/rooms/1/{key}/messages")
            assert code == 200 and len(json.loads(payload)) == 5 + int(bool(shutil.which("ffmpeg") and shutil.which("ffprobe")))
            bot_id = json.loads(payload)[-1]["id"]
            webhook_requests = []
            class WebhookHandler(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    size = int(self.headers["Content-Length"])
                    webhook_payload = json.loads(self.rfile.read(size))
                    webhook_requests.append(webhook_payload)
                    image_reply = webhook_payload["message"]["body"]["plain"] == "send image"
                    body = png if image_reply else b"Bot reply from webhook"
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png" if image_reply else "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *_):
                    pass
            webhook_server = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), WebhookHandler)
            threading.Thread(target=webhook_server.serve_forever, daemon=True).start()
            webhook_url = f"http://127.0.0.1:{webhook_server.server_port}/bot"
            assert request(admin, base, "/account/bots/3/update", {"name":"Rust Robot", "webhook_url":webhook_url})[0] == 200
            assert webhook_url in html.unescape(request(admin, base, "/account/bots/3/edit")[2])
            code, _, payload = request(admin, base, "/autocompletable/users?room_id=1&query=Rust%20Robot")
            bot_suggestion = json.loads(payload)[0]
            assert code == 200 and bot_suggestion["value"] == 3 and bot_suggestion["sgid"]
            def mention_html(user_id, text="selected mention"):
                details = {"contentType":"application/vnd.campfire.mention","sgid":bot_suggestion["sgid"]} if user_id == 3 else {"contentType":"application/vnd.rustfire.mention","userId":str(user_id)}
                details = html.escape(json.dumps(details), quote=True)
                return f'<div><figure data-trix-attachment="{details}"><span class="mention">@Rust Robot</span></figure> {text}</div>'
            assert request(admin, base, "/rooms/1/messages", {"message[body]":mention_html(3, "please answer"),"message[format]":"html"}, headers={"Accept":"application/json"})[0] == 201
            for _ in range(100):
                if webhook_requests and "Bot reply from webhook" in request(admin, base, "/rooms/1")[2]:
                    break
                time.sleep(.05)
            else:
                raise AssertionError("bot webhook reply did not arrive")
            assert webhook_requests[0]["room"]["path"] == f"/rooms/1/{key}/messages"
            assert webhook_requests[0]["message"]["body"]["plain"] == "please answer"
            prior_webhooks = len(webhook_requests)
            assert request(admin, base, "/rooms/1/messages", {"message[body]":"@Rust Robot is plain text"}, headers={"Accept":"application/json"})[0] == 201
            time.sleep(.2)
            assert len(webhook_requests) == prior_webhooks
            prior_webhooks = len(webhook_requests)
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]":mention_html(3),"message[format]":"html"}, headers={"Accept":"application/json"})
            assert code == 201
            assert "/users/3/avatar" in payload
            structured_id = json.loads(payload)["id"]
            assert sqlite3.connect(f"{tmp}/test.db").execute("SELECT user_id FROM message_mentions WHERE message_id=?",(structured_id,)).fetchone() == (3,)
            for _ in range(100):
                if len(webhook_requests) > prior_webhooks:
                    break
                time.sleep(.05)
            else:
                raise AssertionError("structured mention webhook did not arrive")
            imported_sgid = "eyJfcmFpbHMiOnsiZGF0YSI6ImdpZDovL2NhbXBmaXJlL1VzZXIvMz9leHBpcmVzX2luIiwicHVyIjoiYXR0YWNoYWJsZSJ9fQ==--407356a6ecde1089c9bdc4a0c980e4b6a1016234"
            assert imported_sgid == bot_suggestion["sgid"]
            local_key = sqlite3.connect(f"{tmp}/test.db").execute("SELECT value FROM app_secrets WHERE name='mention_sgid'").fetchone()[0]
            local_payload = json.dumps({"_rails":{"data":"gid://campfire/User/3?expires_in","pur":"attachable"}}, separators=(",", ":")).encode()
            local_encoded = base64.urlsafe_b64encode(local_payload).decode()
            local_sgid = f"{local_encoded}--{hmac.new(local_key, local_encoded.encode(), hashlib.sha1).hexdigest()}"
            assert local_sgid != imported_sgid
            local_body = mention_html(3).replace(bot_suggestion["sgid"], local_sgid)
            code, _, local_response = request(admin, base, "/rooms/1/messages", {"message[body]":local_body,"message[format]":"html"}, headers={"Accept":"application/json"})
            assert code == 201
            local_id = json.loads(local_response)["id"]
            assert sqlite3.connect(f"{tmp}/test.db").execute("SELECT user_id FROM message_mentions WHERE message_id=?",(local_id,)).fetchone() == (3,)
            edit_page = request(admin, base, f"/rooms/1/messages/{structured_id}/edit")[2]
            assert "data-trix-attachment" in html.unescape(edit_page)
            assert request(admin, base, f"/rooms/1/messages/{structured_id}", {"message[body]":mention_html(3).replace("selected mention", "edited mention"),"message[format]":"html"}, method="PATCH")[0] == 303
            assert sqlite3.connect(f"{tmp}/test.db").execute("SELECT user_id FROM message_mentions WHERE message_id=?",(structured_id,)).fetchone() == (3,)
            assert request(admin, base, f"/rooms/1/messages/{structured_id}", {"message[body]":"No mention now"}, method="PATCH")[0] == 303
            assert sqlite3.connect(f"{tmp}/test.db").execute("SELECT count(*) FROM message_mentions WHERE message_id=?",(structured_id,)).fetchone() == (0,)
            prior_webhooks = len(webhook_requests)
            assert request(admin, base, "/rooms/1/messages", {"message[body]":mention_html(999),"message[format]":"html"}, headers={"Accept":"application/json"})[0] == 201
            forged_sgid = ("A" if bot_suggestion["sgid"][0] != "A" else "B") + bot_suggestion["sgid"][1:]
            forged_body = mention_html(3).replace(bot_suggestion["sgid"], forged_sgid)
            code, _, forged_payload = request(admin, base, "/rooms/1/messages", {"message[body]":forged_body,"message[format]":"html"}, headers={"Accept":"application/json"})
            assert code == 201 and "☒" in forged_payload and "/users/3/avatar" not in forged_payload
            forged_message_id = json.loads(forged_payload)["id"]
            assert sqlite3.connect(f"{tmp}/test.db").execute("SELECT count(*) FROM message_mentions WHERE message_id=?",(forged_message_id,)).fetchone() == (0,)
            time.sleep(.2)
            assert len(webhook_requests) == prior_webhooks
            assert request(admin, base, "/rooms/1/messages", {"message[body]":mention_html(3, "send image"),"message[format]":"html"}, headers={"Accept":"application/json"})[0] == 201
            for _ in range(100):
                if "attachment.png" in request(admin, base, "/rooms/1")[2]:
                    break
                time.sleep(.05)
            else:
                raise AssertionError("bot webhook image did not arrive")
            code, direct_bot_url, _ = request(admin, base, "/rooms/directs", {"user_ids":"3"})
            assert code == 200
            direct_bot_room = int(re.search(r"/rooms/(\d+)", direct_bot_url).group(1))
            before_direct = len(webhook_requests)
            assert request(admin, base, f"/rooms/{direct_bot_room}/messages", {"message[body]":"hello in direct"}, headers={"Accept":"application/json"})[0] == 201
            for _ in range(100):
                if len(webhook_requests) > before_direct and "Bot reply from webhook" in request(admin, base, f"/rooms/{direct_bot_room}")[2]:
                    break
                time.sleep(.05)
            else:
                raise AssertionError("direct-room webhook did not arrive")
            bot_attachment=(b"--bot-file\r\nContent-Disposition: form-data; name=\"attachment\"; filename=\"from-bot.txt\"\r\nContent-Type: text/plain\r\n\r\nbot file\r\n--bot-file--\r\n")
            code, _, payload = request(client(), base, f"/rooms/1/{key}/messages", data=bot_attachment, method="POST", headers={"Content-Type":"multipart/form-data; boundary=bot-file"})
            assert code == 201 and payload == ""
            code, _, page = request(admin, base, "/rooms/1")
            assert code == 200 and "from-bot.txt" in page
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                bot_file_message, bot_stored_name = check_db.execute("SELECT m.id,a.stored_name FROM attachments a JOIN messages m ON m.id=a.message_id WHERE a.filename='from-bot.txt'").fetchone()
            bot_stored_file = pathlib.Path(tmp, "uploads", bot_stored_name)
            assert bot_stored_file.is_file()
            assert request(client(), base, f"/rooms/1/{key}/messages/{bot_file_message}", method="DELETE")[0] == 204
            assert not bot_stored_file.exists()
            code, _, payload = request(client(), base, f"/rooms/1/{key}/messages/{bot_id}", data=b"updated by bot", method="PATCH")
            assert code == 200 and json.loads(payload)["body"]["plain_text"] == "updated by bot"
            code, _, payload = request(client(), base, f"/rooms/1/{key}/messages/{bot_id}/boosts", data=b"fire", method="POST")
            assert code == 201 and json.loads(payload)["content"] == "fire"
            assert json.loads(payload)["message"]["url"] == f"{base}/rooms/1/messages/{bot_id}"
            assert re.fullmatch(re.escape(base) + r"/users/[A-Za-z0-9_-]+--[0-9a-f]{64}/avatar\?v=\d{14}", json.loads(payload)["booster"]["avatar_url"])
            boost_id = json.loads(payload)["id"]
            code, _, _ = request(client(), base, f"/rooms/1/{key}/messages/{bot_id}/boosts/{boost_id}", method="DELETE")
            assert code == 204
            code, _, _ = request(client(), base, f"/rooms/1/{key}/messages/{message['id']}/boosts", data=b"", method="POST")
            assert code == 422
            code, _, _ = request(client(), base, f"/rooms/1/{key}/messages/{message['id']}", data=b"bad edit", method="PATCH")
            assert code == 403
            code, _, _ = request(client(), base, f"/rooms/1/{key}/messages/{bot_id}", method="DELETE")
            assert code == 204
            code, _, page = request(admin, base, "/account/bots/3/key", {})
            assert code == 200
            rotated = re.search(r"key: <code>([\w-]+)</code>", page).group(1)
            assert rotated != key
            code, _, _ = request(client(), base, f"/rooms/1/{key}/messages")
            assert code == 401
            code, _, payload = request(client(), base, f"/rooms/1/{rotated}/messages", data=b"new key works", method="POST")
            assert code == 201 and payload == ""
            code, _, _ = request(admin, base, "/account/bots/3/delete", {})
            assert code == 200
            code, _, _ = request(client(), base, f"/rooms/1/{rotated}/messages")
            assert code == 401
            rich = "<div><strong>formatted</strong> text <a href='javascript:alert(1)'>unsafe link</a><script>alert(1)</script></div>"
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]": rich, "message[format]": "html"}, headers={"Accept": "application/json"})
            assert code == 201
            formatted = json.loads(payload)
            assert "<strong>formatted</strong>" in formatted["body"]["html"]
            assert "javascript:" not in formatted["body"]["html"] and "<script" not in formatted["body"]["html"]
            assert "formatted text" in formatted["body"]["plain_text"]
            assert "<strong>formatted</strong>" in request(admin, base, "/rooms/1")[2]
            code, _, _ = request(admin, base, f"/rooms/1/messages/{formatted['id']}", {"message[body]": "<div><em>changed</em></div>", "message[format]": "html"}, method="PATCH")
            assert code == 303
            assert "<em>changed</em>" in request(admin, base, "/rooms/1")[2]
            preview = "<div><action-text-attachment content-type='application/vnd.actiontext.opengraph-embed' href='javascript:alert(1)' url='data:image/svg+xml;base64,PHN2Zy8+' filename='Free cookies' caption='Cookies here'></action-text-attachment></div>"
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]": preview, "message[format]": "html"}, headers={"Accept": "application/json"})
            assert code == 201 and json.loads(payload)["body"]["plain_text"] == ""
            rendered = request(admin, base, f"/rooms/1/messages/{json.loads(payload)['id']}")[2]
            assert "Free cookies" in rendered and "javascript:alert" not in rendered and "data:image/svg" not in rendered
            assert "<a href='javascript" not in rendered
            preview = preview.replace("javascript:alert(1)", "https://example.com/page").replace("data:image/svg+xml;base64,PHN2Zy8+", "https://example.com/image.png")
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]": preview, "message[format]": "html"}, headers={"Accept": "application/json"})
            assert code == 201
            rendered = request(admin, base, f"/rooms/1/messages/{json.loads(payload)['id']}")[2]
            assert "href=\"https://example.com/page\"" in rendered and "src=\"https://example.com/image.png\"" in rendered
            own_host_preview = preview.replace("https://example.com/page", "https://once.campfire.test/rooms/1").replace("https://example.com/image.png", "https://once.campfire.test/account/logo")
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]": own_host_preview, "message[format]": "html"}, headers={"Accept": "application/json", "Host": "once.campfire.test"})
            assert code == 201
            rendered = request(admin, base, f"/rooms/1/messages/{json.loads(payload)['id']}")[2]
            assert "Free cookies" in rendered and "once.campfire.test" not in rendered
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]": "/play bell"}, headers={"Accept": "application/json"})
            assert code == 201
            assert "data-sound='/static/sounds/bell.mp3'" in request(admin, base, "/rooms/1")[2]
            code, _, payload = request(admin, base, "/rooms/1/messages", {"message[body]": "<div>/play bell</div>", "message[format]": "html"}, headers={"Accept": "application/json"})
            assert code == 201
            assert "data-sound='/static/sounds/bell.mp3'" in request(admin, base, f"/rooms/1/messages/{json.loads(payload)['id']}")[2]
            with admin.open(base + "/static/sounds/bell.mp3") as res:
                assert res.status == 200 and res.headers.get_content_type() == "audio/mpeg" and len(res.read()) > 100
            code, _, page = request(member, base, "/users/me/profile")
            assert code == 200, (code, page)
            transfer_match = re.search(r"/session/transfers/[\w-]+", html.unescape(page))
            assert transfer_match, page[-1200:]
            transfer = transfer_match.group(0)
            moved = client()
            code, _, page = request(moved, base, transfer)
            assert code == 200 and "Sign in on this device" in page
            transfer_csrf = CSRF[moved]
            code, _, _ = request(moved, base, transfer, {}, method="PUT")
            assert code == 303, code
            assert request(moved, base, "/rooms/1")[0] == 200
            code, _, payload = request(member, base, "/rooms/1/messages", {"message[body]": "ban removes this message"}, headers={"Accept": "application/json"})
            assert code == 201
            code, _, page = request(admin, base, "/users/2")
            assert code == 200 and "Private sign-in link" in page and "ban" in page
            code, _, _ = request(admin, base, "/users/2/ban", {})
            assert code == 200
            banned_ip = client()
            assert request(banned_ip, base, "/session/new")[0] == 200
            assert request(banned_ip, base, "/session", {"email_address": "none@example.com", "password": "bad"}, headers={"X-Forwarded-For": "8.8.8.8"})[0] == 429
            assert request(member, base, "/rooms/1")[0] == 401
            assert request(second_session, base, "/rooms/1")[0] == 401
            assert request(moved, base, "/rooms/1")[0] == 401
            assert request(moved, base, transfer, {}, method="PUT", headers={"X-CSRF-Token": transfer_csrf})[0] == 400
            assert "ban removes this message" not in request(admin, base, "/rooms/1")[2]
            code, _, _ = request(admin, base, "/users/2/ban", method="DELETE")
            assert code == 303
            assert request(banned_ip, base, "/session", {"email_address": "none@example.com", "password": "bad"}, headers={"X-Forwarded-For": "8.8.8.8"})[0] == 401
            assert "unban" not in request(admin, base, "/users/2")[2]
            assert request(member, base, "/session/new")[0] == 200
            code, _, _ = request(member, base, "/session", {"email_address": "member2@example.com", "password": "newpassword123"})
            assert code == 200
            code, _, payload = request(admin, base, "/rooms/3/messages", data=multipart, method="POST", headers={"Accept":"application/json", "Content-Type":f"multipart/form-data; boundary={boundary}"})
            assert code == 201
            direct_message_id = json.loads(payload)["id"]
            direct_page = request(admin, base, "/rooms/3")[2]
            assert f"<span class='message__room'><a href='/rooms/3/@{direct_message_id}' target='_top' data-reply-target='link'>Admin and Member Two</a></span>" in direct_page
            room_attachment_id = sqlite3.connect(f"{tmp}/test.db").execute("SELECT id FROM attachments WHERE message_id=?", (json.loads(payload)["id"],)).fetchone()[0]
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                room_stored_name = check_db.execute("SELECT stored_name FROM attachments WHERE id=?", (room_attachment_id,)).fetchone()[0]
            room_stored_file = pathlib.Path(tmp, "uploads", room_stored_name)
            assert room_stored_file.is_file()
            assert request(member, base, "/rooms/directs/3", method="DELETE")[0] == 303
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT count(*) FROM direct_room_sets WHERE room_id=3").fetchone() == (0,)
            assert request(admin, base, "/rooms/3")[1].endswith("/rooms/1")
            code, retained_direct_url, _ = request(admin, base, "/rooms/directs", {"user_ids": "2"})
            assert code == 200
            retained_direct = int(re.search(r"/rooms/(\d+)$", retained_direct_url).group(1))
            assert not room_stored_file.exists()
            code, converted_url, _ = request(admin, base, "/rooms/closeds", {"room[name]": "Convert me", "user_ids[]": "1"})
            assert code == 200
            converted_room = int(re.search(r"/rooms/(\d+)$", converted_url).group(1))
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                original_membership = check_db.execute("SELECT id FROM memberships WHERE room_id=? AND user_id=1", (converted_room,)).fetchone()[0]
            assert request(admin, base, f"/rooms/{converted_room}/involvement", {"involvement": "everything"})[0] == 200
            assert request(admin, base, f"/rooms/opens/{converted_room}", {"room[name]": "Opened"}, method="PATCH")[0] == 303
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT type FROM rooms WHERE id=?", (converted_room,)).fetchone()[0] == "Rooms::Open"
                assert check_db.execute("SELECT id,involvement FROM memberships WHERE room_id=? AND user_id=1", (converted_room,)).fetchone() == (original_membership, "everything")
                assert check_db.execute("SELECT count(*) FROM memberships WHERE room_id=? AND user_id=2", (converted_room,)).fetchone()[0] == 1
            assert request(admin, base, f"/rooms/closeds/{converted_room}", {"room[name]": "Closed again", "user_ids[]": ["1", "2"]}, method="PATCH")[0] == 303
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT type FROM rooms WHERE id=?", (converted_room,)).fetchone()[0] == "Rooms::Closed"
                assert check_db.execute("SELECT id,involvement FROM memberships WHERE room_id=? AND user_id=1", (converted_room,)).fetchone() == (original_membership, "everything")
                check_db.execute("INSERT INTO searches(user_id,query,created_at) VALUES(2,'before deactivation','2026-01-01T00:00:00Z')")
                check_db.execute("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key,created_at,updated_at) VALUES(2,'https://example.com/deactivation','key','auth','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            code, _, _ = request(admin, base, "/account/users/2/deactivate", {})
            assert code == 200
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT user_id FROM memberships WHERE room_id=? ORDER BY user_id", (retained_direct,)).fetchall() == [(1,), (2,)]
                assert check_db.execute("SELECT member_ids FROM direct_room_sets WHERE room_id=?", (retained_direct,)).fetchone() == ("1,2",)
                assert check_db.execute("SELECT count(*) FROM memberships WHERE room_id=? AND user_id=2", (converted_room,)).fetchone() == (0,)
                assert check_db.execute("SELECT count(*) FROM searches WHERE user_id=2").fetchone() == (0,)
                assert check_db.execute("SELECT count(*) FROM push_subscriptions WHERE user_id=2").fetchone() == (0,)
                assert re.fullmatch(r"member2-deactivated-[0-9a-f-]{36}@example\.com", check_db.execute("SELECT email_address FROM users WHERE id=2").fetchone()[0])
            code, _, retained_page = request(admin, base, f"/rooms/{retained_direct}")
            assert code == 200 and "Member Two" in retained_page
            assert "Member Two" in request(admin, base, "/users/me/sidebar")[2]
            assert request(admin, base, f"/rooms/{converted_room}")[0] == 200
            code, _, _ = request(member, base, "/rooms/1")
            assert code == 401
            throttle=client()
            assert request(throttle,base,"/session/new")[0]==200
            for _ in range(10):
                assert request(throttle,base,"/session",{"email_address":"wrong@example.test","password":"wrong"},headers={"X-Forwarded-For":"198.51.100.73"})[0]==401
            assert request(throttle,base,"/session",{"email_address":"wrong@example.test","password":"wrong"},headers={"X-Forwarded-For":"198.51.100.73"})[0]==429
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                stored_name = check_db.execute("SELECT stored_name FROM attachments WHERE id=?", (attachment_id,)).fetchone()[0]
            stored_file = pathlib.Path(tmp, "uploads", stored_name)
            assert stored_file.is_file()
            deletion = request(admin, base, f"/rooms/1/messages/{attachment_message['id']}", method="DELETE", headers={"Accept": "text/vnd.turbo-stream.html"})
            assert deletion[0] == 200, deletion
            assert not stored_file.exists()
            assert request(admin, base, f"/attachments/{attachment_id}")[0] == 404
            code, redirected, _ = request(admin, base, "/rooms/closeds", {"room[name]": "Unjoined room", "user_ids[]": "99999"})
            assert code == 200 and redirected.endswith(f"/rooms/{converted_room}"), (code, redirected)
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                unjoined = check_db.execute("SELECT max(id) FROM rooms").fetchone()[0]
                assert check_db.execute("SELECT count(*) FROM memberships WHERE room_id=?", (unjoined,)).fetchone()[0] == 0
            assert request(admin, base, f"/rooms/{unjoined}/messages")[0] == 404
            admin_push = {f"push_subscription[{key}]": value for key, value in push_keys.items()}
            assert request(admin, base, "/users/me/push_subscriptions", admin_push)[0] == 200
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT count(*) FROM push_subscriptions WHERE user_id=1 AND endpoint=?", (push_keys["endpoint"],)).fetchone()[0] == 1
            signout = request(admin, base, "/session", {"push_subscription_endpoint": push_keys["endpoint"]}, method="DELETE")
            assert signout[0] == 303, signout
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT count(*) FROM push_subscriptions WHERE user_id=1 AND endpoint=?", (push_keys["endpoint"],)).fetchone()[0] == 0
            assert request(admin, base, "/rooms/1")[0] == 401
            process.terminate()
            process.wait(timeout=5)
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                check_db.execute("DROP TRIGGER direct_room_sets_membership_delete")
                check_db.execute("DROP TABLE direct_room_sets")
                imported_cutoff = int(time.time() * 1000) + 300000
                rails_time = lambda milliseconds: datetime.fromtimestamp(milliseconds / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                old_created = rails_time(imported_cutoff - 60000)
                old_updated = rails_time(imported_cutoff + 2000)
                new_created = rails_time(imported_cutoff + 1000)
                imported_old = check_db.execute(
                    "INSERT INTO messages(room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,'imported edited','imported-edited',?,?)",
                    (old_created, old_updated),
                ).lastrowid
                imported_new = check_db.execute(
                    "INSERT INTO messages(room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,'imported new','imported-new',?,?)",
                    (new_created, new_created),
                ).lastrowid
            process = subprocess.Popen([str(ROOT / "target/debug/rustfire")], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for _ in range(100):
                try:
                    if request(client(), base, "/up")[0] == 200:
                        break
                except urllib.error.URLError:
                    time.sleep(.05)
            else:
                raise AssertionError("server did not restart for direct-room index migration")
            assert request(admin, base, "/session/new")[0] == 200
            assert request(admin, base, "/session", {"email_address":"admin@example.com","password":"password123"})[0] == 200
            code, _, payload = request(admin, base, f"/rooms/1/refresh?since={imported_cutoff}", headers={"Accept":"application/json"})
            imported_refresh = json.loads(payload)
            assert code == 200 and [entry["id"] for entry in imported_refresh["messages"]] == [imported_new], imported_refresh
            assert [entry["id"] for entry in imported_refresh["updated"]] == [imported_old], imported_refresh
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT count(*) FROM messages WHERE id IN (?,?) AND created_at_ns IS NOT NULL AND updated_at_ns IS NOT NULL", (imported_old, imported_new)).fetchone()[0] == 2
            code, _, suggestions = request(admin, base, "/autocompletable/users?room_id=1&query=Admin")
            assert code == 200 and json.loads(suggestions)[0]["sgid"] == admin_mention_sgid
            with sqlite3.connect(f"{tmp}/test.db") as check_db:
                assert check_db.execute("SELECT member_ids FROM direct_room_sets WHERE room_id=?", (retained_direct,)).fetchone() == ("1,2",)
                created_at = "2027-01-01T00:00:00.123456Z"
                created_at_ns = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000 + 123456000
                first_tied_id = check_db.execute("SELECT COALESCE(MAX(id),0)+1 FROM messages").fetchone()[0]
                check_db.executemany(
                    "INSERT INTO messages(room_id,creator_id,body,client_message_id,created_at,created_at_ns,updated_at) VALUES(1,1,?1,?2,?3,?4,?3)",
                    ((f"tied {index}", f"tied-{index}", created_at, created_at_ns) for index in range(102)),
                )
            code, _, payload = request(admin, base, f"/rooms/1/refresh?after={first_tied_id}", headers={"Accept":"application/json"})
            first_page = json.loads(payload)
            assert code == 200 and len(first_page["messages"]) == 100 and first_page["has_more"], (code, first_page)
            code, _, payload = request(admin, base, f"/rooms/1/refresh?after={first_page['next_after']}", headers={"Accept":"application/json"})
            last_page = json.loads(payload)
            assert code == 200 and len(last_page["messages"]) == 1 and not last_page["has_more"], (code, last_page)
            print("PASS setup, messages, attachments, boosts, search, private rooms, pings, account administration, bots, transfer, bans, direct-room index migration")
        finally:
            if webhook_server:
                webhook_server.shutdown()
            process.terminate()
            process.wait(timeout=5)
            if process.returncode not in (0, -15):
                print(process.stdout.read().decode())


if __name__ == "__main__":
    main()
