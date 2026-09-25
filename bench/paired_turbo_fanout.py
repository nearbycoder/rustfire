"""Probe signed RoomMessagesChannel fanout on disposable Rustfire and Campfire fixtures.

Requires the pinned Campfire checkout, its installed bundle, and Redis on localhost:6379.
Both runs use the same number of sockets and posts. Generated timestamps still differ.
"""

import argparse
import hashlib
from datetime import datetime, timezone
import html
from html.parser import HTMLParser
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import uuid
import urllib.request
import urllib.parse
import struct
import zlib

from direct_lookup import ROOT, free_port, start_server, stop_server
from paired_direct_lookup import login_campfire, seed_campfire, seed_rustfire, wait_for_server


def seed_boost_message(rust_db, camp_db):
    instant = datetime.now(timezone.utc)
    rust_time = instant.isoformat().replace("+00:00", "Z")
    camp_time = instant.strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(rust_db) as db:
        db.execute("INSERT INTO messages(id,room_id,creator_id,body,client_message_id,created_at,updated_at) VALUES(1,1,1,'Boost fixture','boost-fixture',?1,?1)", [rust_time])
    with sqlite3.connect(camp_db) as db:
        db.execute("INSERT INTO messages(id,room_id,creator_id,client_message_id,created_at,updated_at) VALUES(1,1,1,'boost-fixture',?1,?1)", [camp_time])
        db.execute("INSERT INTO action_text_rich_texts(name,body,record_type,record_id,created_at,updated_at) VALUES('body','Boost fixture','Message',1,?1,?1)", [camp_time])


def fanout(app, port, cookie, csrf, sockets, messages, operation, run_id, sample_file=None, image_file=None, video_file=None):
    command = [
        "node", "bench/fanout.mjs", "--app", app,
        "--base", f"http://127.0.0.1:{port}", "--cookie", cookie,
        "--csrf", csrf, "--room", "1", "--sockets", str(sockets),
        "--messages", str(messages), "--operation", operation,
        "--run-id", run_id,
    ]
    if sample_file is not None:
        command.extend(["--sample-file", str(sample_file)])
    if image_file is not None:
        command.extend(["--image-file", str(image_file)])
    if video_file is not None:
        command.extend(["--video-file", str(video_file)])
    result = subprocess.run(
        command,
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"{app} fanout failed: {result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def stream_avatar_identity(sample_file):
    sample = html.unescape(sample_file.read_text())
    name = re.search(r"<a\b[^>]*\btitle=['\"]([^'\"]+)['\"]", sample)
    avatar = re.search(r"<img\b[^>]*\bsrc=['\"]([^'\"]+/avatar\?v=\d+)['\"]", sample)
    if not name or not avatar:
        raise RuntimeError(f"Stream sample lacks avatar identity: {sample_file}")
    return name.group(1), avatar.group(1)


def stream_message_id(sample_file):
    sample = sample_file.read_text()
    matched = re.search(r"\bdata-message-id=['\"](\d+)['\"]", sample)
    if not matched:
        raise RuntimeError(f"Message sample lacks its numeric ID: {sample_file}")
    return int(matched.group(1))


def check_message_targets(sample_file):
    sample = sample_file.read_text()
    ids = set(re.findall(r"\bid=['\"]([^'\"]+)['\"]", sample))
    required = (
        "message_", "edit_message_", "presentation_message_",
        "boosting_message_", "boosts_message_", "new_boost_message_",
    )
    missing = [prefix for prefix in required if not any(value.startswith(prefix) for value in ids)]
    if missing:
        raise RuntimeError(f"Message stream lacks frame or DOM targets {missing}: {sample_file}")
    if not re.search(r"<h2\s+class=['\"]message__day-separator['\"]>\s*<time\b", sample):
        raise RuntimeError(f"Message stream lacks its day heading: {sample_file}")
    if "custom-boost-form" in sample:
        raise RuntimeError(f"Message stream eagerly renders a new-boost form: {sample_file}")


def message_room_label(sample_file):
    sample = sample_file.read_text()
    matched = re.search(r"<span\b[^>]*class=['\"]message__room['\"][^>]*>\s*<a\b[^>]*>([^<]*)</a>", sample)
    if not matched:
        raise RuntimeError(f"Message stream lacks its room link: {sample_file}")
    return html.unescape(matched.group(1))


class MessageTagSequence(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attribute_keys = []
        self.attributes = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(("start", tag))
        self.attribute_keys.append((tag, tuple(sorted(key for key, _ in attrs))))
        self.attributes.append((tag, tuple(sorted(attrs))))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag not in {"img", "input", "br", "hr", "source", "meta", "link"}:
            self.tags.append(("end", tag))

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())


def stream_structure(sample_file):
    parser = MessageTagSequence()
    parser.feed(sample_file.read_text())
    return parser.tags, parser.attribute_keys, parser.attributes, parser.text


def check_image_representation(sample_file, port, cookie, expected_dimensions):
    sample = html.unescape(sample_file.read_text())
    path = re.search(r"<img\b[^>]*class=['\"]message__attachment['\"][^>]*src=['\"]([^'\"]+)['\"]", sample)
    if not path or not path.group(1).startswith("/rails/active_storage/representations/redirect/"):
        raise RuntimeError(f"Image stream has no signed representation URL: {sample_file}")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path.group(1)}", headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        if response.status != 200 or response.headers.get_content_type() != "image/png" or not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError(f"Signed representation did not serve a PNG: {sample_file}")
    dimensions = struct.unpack(">II", body[16:24])
    if dimensions != expected_dimensions:
        raise RuntimeError(f"Representation dimensions {dimensions} differ from {expected_dimensions}: {sample_file}")
    sample_file.with_suffix(".png").write_bytes(body)
    return len(body), hashlib.sha256(body).hexdigest()


def check_video_poster(sample_file, port, cookie):
    sample = html.unescape(sample_file.read_text())
    path = re.search(r"<video\b[^>]*poster=['\"]([^'\"]+)['\"]", sample)
    if not path:
        raise RuntimeError(f"Video stream has no poster URL: {sample_file}")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path.group(1)}", headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        if response.status != 200 or response.headers.get_content_type() != "image/webp" or not body.startswith(b"RIFF") or body[8:12] != b"WEBP":
            raise RuntimeError(f"Poster URL did not serve a WebP: {sample_file}")
    sample_file.with_suffix(".webp").write_bytes(body)
    return len(body), hashlib.sha256(body).hexdigest()


def fetch_edit_frame(output_file, port, cookie, client_id):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/rooms/1/messages/1/edit",
        headers={"Cookie": cookie, "Turbo-Frame": f"edit_message_{client_id}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Message edit returned {response.status}")
        output_file.write_bytes(response.read())


def edit_frame_structure(sample_file):
    source = sample_file.read_text()
    match = re.search(r"<turbo-frame\s+id=['\"]edit_message_[^'\"]+['\"][^>]*>.*?</turbo-frame>", source, re.S)
    if not match:
        raise RuntimeError(f"Edit response lacks its Turbo frame: {sample_file}")
    parser = MessageTagSequence()
    parser.feed(match.group(0))
    normalized = []
    for tag, attrs in parser.attributes:
        values = dict(attrs)
        if tag == "input" and values.get("name") == "authenticity_token":
            if not values.get("value"):
                raise RuntimeError(f"Edit form has an empty CSRF token: {sample_file}")
            values["value"] = "<csrf>"
        for key in ("data-direct-upload-url", "data-blob-url-template"):
            if key in values:
                url = urllib.parse.urlsplit(values[key])
                if not url.scheme or not url.netloc:
                    raise RuntimeError(f"Edit form has a relative {key}: {sample_file}")
                values[key] = url.path
        normalized.append((tag, tuple(sorted(values.items()))))
    return parser.tags, parser.attribute_keys, normalized, parser.text


def write_png(path, width, height):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend((x % 256, y % 256, (x + y) % 256))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def stable_message_attributes(attributes):
    generated_times = {"data-message-timestamp", "data-message-updated-at", "data-sort-value", "datetime"}
    return [(tag, tuple((key, value) for key, value in attrs if key not in generated_times)) for tag, attrs in attributes]


def check_message_times(attributes, sample_file):
    root = next((dict(attrs) for tag, attrs in attributes if tag == "div" and "data-message-timestamp" in dict(attrs)), None)
    if root is None:
        raise RuntimeError(f"Message stream lacks timestamp fields: {sample_file}")
    created = int(root["data-message-timestamp"])
    updated = int(root["data-message-updated-at"])
    if int(root["data-sort-value"]) != created or updated < created:
        raise RuntimeError(f"Message stream timestamps are inconsistent: {sample_file}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockets", type=int, default=50)
    parser.add_argument("--messages", type=int, default=5)
    parser.add_argument("--operation", choices=["messages", "boosts", "attachments", "images", "videos"], default="messages")
    parser.add_argument("--campfire-workers", type=int, default=1)
    parser.add_argument("--sample-dir", type=pathlib.Path, help="Write one received Turbo event from each app to this directory")
    parser.add_argument("--image-size", help="Use a generated PNG of WIDTHxHEIGHT for image uploads; default is 1x1")
    parser.add_argument("--video-size", default="16x16", help="Generate a video of WIDTHxHEIGHT for video uploads; default is 16x16")
    parser.add_argument("--video-sar", help="Set sample aspect ratio NUM/DEN in the generated video")
    parser.add_argument("--check-edit", action="store_true", help="Fetch the first message's edit frame")
    parser.add_argument("--campfire-repo", type=pathlib.Path, default=pathlib.Path("/tmp/once-campfire-reference"))
    parser.add_argument("--ruby", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/local/bin/ruby"))
    parser.add_argument("--bundle-path", type=pathlib.Path, default=pathlib.Path("/tmp/rustfire-baseline/bundle"))
    args = parser.parse_args()
    if args.sockets < 1 or args.messages < 1 or args.campfire_workers < 1:
        parser.error("sockets, messages, and workers must be positive")
    if args.image_size and (not re.fullmatch(r"[1-9]\d{0,3}x[1-9]\d{0,3}", args.image_size) or args.operation != "images"):
        parser.error("--image-size requires images and WIDTHxHEIGHT, each below 10000")
    if args.operation == "videos" and not re.fullmatch(r"[1-9]\d{0,3}x[1-9]\d{0,3}", args.video_size):
        parser.error("--video-size must be WIDTHxHEIGHT, each below 10000")
    if args.video_sar and (args.operation != "videos" or not re.fullmatch(r"[1-9]\d{0,2}/[1-9]\d{0,2}", args.video_sar)):
        parser.error("--video-sar requires videos and NUM/DEN")
    if args.check_edit and args.operation == "boosts":
        parser.error("--check-edit requires a message operation")
    repo = args.campfire_repo.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != "91d294f4a09f9bbe37f9548959bfcb43645678fb":
        parser.error(f"Campfire source is at {revision}, not the pinned compatibility target")
    ruby = args.ruby.resolve()
    sample_dir = args.sample_dir.resolve() if args.sample_dir else None
    if sample_dir:
        sample_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paired-turbo-fanout-") as scratch:
        temp = pathlib.Path(scratch)
        run_id = str(uuid.uuid4())
        output_dir = sample_dir or temp
        rust_db, camp_db = temp / "rust.sqlite3", temp / "camp.sqlite3"
        image_file = temp / "upload.png" if args.image_size else None
        expected_image_dimensions = (1, 1)
        if image_file:
            width, height = map(int, args.image_size.split("x"))
            write_png(image_file, width, height)
            factor = min(1.0, 1200 / width, 800 / height)
            expected_image_dimensions = (round(width * factor), round(height * factor))
        video_file = temp / "upload.mp4" if args.operation == "videos" else None
        if video_file:
            video_command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:s={args.video_size}:r=5:d=1"]
            if args.video_sar:
                video_command.extend(["-vf", f"setsar={args.video_sar}"])
            subprocess.run(video_command + ["-c:v", "mpeg4", "-y", str(video_file)], check=True)
        rust_port, camp_port = free_port(), free_port()
        seed_rustfire(rust_db, rust_port, [])
        camp_env = seed_campfire(repo, ruby, args.bundle_path.resolve(), repo / "storage/db/production.sqlite3", camp_db, [], camp_port, temp)
        with sqlite3.connect(camp_db) as camp, sqlite3.connect(rust_db) as rust:
            camp.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
            people = camp.execute("SELECT id,name,updated_at FROM users WHERE id IN(1,2)").fetchall()
            rust.executemany("UPDATE users SET name=?2,updated_at=?3 WHERE id=?1", people)
            room_name = camp.execute("SELECT name FROM rooms WHERE id=1").fetchone()[0]
            rust.execute("UPDATE rooms SET name=? WHERE id=1", [room_name])
        camp_env["WEB_CONCURRENCY"] = str(args.campfire_workers)
        if args.operation == "boosts":
            seed_boost_message(rust_db, camp_db)

        rust = start_server(rust_db, rust_port, {
            "RUSTFIRE_DISABLE_PUSH": "0", "RUSTFIRE_DISABLE_WEBHOOKS": "0",
            "RUSTFIRE_CAMPFIRE_SECRET_KEY_BASE": camp_env["SECRET_KEY_BASE"],
            "RUSTFIRE_PUBLIC_URL": "http://127.0.0.1",
        })
        try:
            rust_result = fanout("rustfire-turbo", rust_port, "session_token=benchmark-session", "benchmark-csrf", args.sockets, args.messages, args.operation, run_id, output_dir / "rustfire.html", image_file, video_file)
            if args.check_edit:
                fetch_edit_frame(output_dir / "rustfire-edit.html", rust_port, "session_token=benchmark-session", f"{run_id}-0")
            rust_image_bytes = check_image_representation(output_dir / "rustfire.html", rust_port, "session_token=benchmark-session", expected_image_dimensions) if args.operation == "images" else None
            rust_poster_bytes = check_video_poster(output_dir / "rustfire.html", rust_port, "session_token=benchmark-session") if args.operation == "videos" else None
        finally:
            stop_server(rust)

        with open(temp / "puma.log", "w+") as log:
            camp = subprocess.Popen(
                [str(ruby), str(ruby.parent / "bundle"), "exec", "puma", "-C", "config/puma.rb"],
                cwd=repo, env=camp_env, stdout=log, stderr=log,
            )
            try:
                wait_for_server(camp_port, camp)
                cookie, csrf = login_campfire(camp_port)
                camp_result = fanout("campfire", camp_port, cookie, csrf, args.sockets, args.messages, args.operation, run_id, output_dir / "campfire.html", image_file, video_file)
                if args.check_edit:
                    fetch_edit_frame(output_dir / "campfire-edit.html", camp_port, cookie, f"{run_id}-0")
                camp_image_bytes = check_image_representation(output_dir / "campfire.html", camp_port, cookie, expected_image_dimensions) if args.operation == "images" else None
                camp_poster_bytes = check_video_poster(output_dir / "campfire.html", camp_port, cookie) if args.operation == "videos" else None
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-2000:])
                raise
            finally:
                stop_server(camp)
        print(f"rustfire-turbo {rust_result}")
        print(f"campfire       {camp_result}")
        rust_identity = stream_avatar_identity(output_dir / "rustfire.html")
        camp_identity = stream_avatar_identity(output_dir / "campfire.html")
        if rust_identity != camp_identity:
            raise RuntimeError(f"Avatar identity differs: Rustfire {rust_identity}, Campfire {camp_identity}")
        if args.operation in {"messages", "attachments", "images", "videos"} and stream_message_id(output_dir / "rustfire.html") != stream_message_id(output_dir / "campfire.html"):
            raise RuntimeError("Message IDs differ in paired stream samples")
        if args.operation in {"messages", "attachments", "images", "videos"}:
            check_message_targets(output_dir / "rustfire.html")
            check_message_targets(output_dir / "campfire.html")
            rust_room_label = message_room_label(output_dir / "rustfire.html")
            camp_room_label = message_room_label(output_dir / "campfire.html")
            if rust_room_label != camp_room_label:
                raise RuntimeError(f"Message room labels differ: Rustfire {rust_room_label}, Campfire {camp_room_label}")
        if args.operation in {"messages", "attachments", "images", "videos"}:
            rust_tags, rust_attributes, rust_values, rust_text = stream_structure(output_dir / "rustfire.html")
            camp_tags, camp_attributes, camp_values, camp_text = stream_structure(output_dir / "campfire.html")
            if rust_tags != camp_tags:
                raise RuntimeError(f"{args.operation} stream tag structure differs from Campfire")
            if rust_attributes != camp_attributes:
                raise RuntimeError(f"{args.operation} stream attribute keys differ from Campfire")
            check_message_times(rust_values, output_dir / "rustfire.html")
            check_message_times(camp_values, output_dir / "campfire.html")
            if stable_message_attributes(rust_values) != stable_message_attributes(camp_values):
                raise RuntimeError(f"{args.operation} stream static attribute values differ from Campfire")
            if rust_text != camp_text:
                raise RuntimeError(f"{args.operation} stream text differs from Campfire")
        if args.operation == "attachments":
            expected_filename = f"fanout-file-{run_id}-0.txt"
            for app in ("rustfire", "campfire"):
                sample = (output_dir / f"{app}.html").read_text()
                if expected_filename not in sample or "web-share#share" not in sample or "Download" not in sample:
                    raise RuntimeError(f"{app} attachment stream lacks its file or actions")
        if args.operation == "images":
            expected_filename = f"fanout-image-{run_id}-0.png"
            for app in ("rustfire", "campfire"):
                sample = (output_dir / f"{app}.html").read_text()
                if expected_filename not in sample or "message__attachment" not in sample or "web-share#share" not in sample:
                    raise RuntimeError(f"{app} image stream lacks its preview or actions")
            if rust_image_bytes[1] != camp_image_bytes[1]:
                raise RuntimeError("Rendered PNG preview bytes differ from Campfire")
            print(f"image_representation_bytes rustfire={rust_image_bytes[0]} campfire={camp_image_bytes[0]} sha256_equal={rust_image_bytes[1] == camp_image_bytes[1]}")
        if args.operation == "videos":
            expected_filename = f"fanout-video-{run_id}-0.mp4"
            for app in ("rustfire", "campfire"):
                sample = (output_dir / f"{app}.html").read_text()
                if expected_filename not in sample or "message__attachment" not in sample or "<video" not in sample:
                    raise RuntimeError(f"{app} video stream lacks its player or actions")
            if rust_poster_bytes[1] != camp_poster_bytes[1]:
                raise RuntimeError("Rendered WebP poster bytes differ from Campfire")
            print(f"video_poster_bytes rustfire={rust_poster_bytes[0]} campfire={camp_poster_bytes[0]} sha256_equal={rust_poster_bytes[1] == camp_poster_bytes[1]}")
        if args.operation == "boosts":
            rust_tags, rust_attributes, rust_values, rust_text = stream_structure(output_dir / "rustfire.html")
            camp_tags, camp_attributes, camp_values, camp_text = stream_structure(output_dir / "campfire.html")
            if rust_tags != camp_tags:
                raise RuntimeError("Boost stream tag structure differs from Campfire")
            if rust_attributes != camp_attributes:
                raise RuntimeError("Boost stream attribute keys differ from Campfire")
            if rust_values != camp_values:
                raise RuntimeError("Boost stream attribute values differ from Campfire")
            if rust_text != camp_text:
                raise RuntimeError("Boost stream text differs from Campfire")
        if args.check_edit:
            rust_edit = edit_frame_structure(output_dir / "rustfire-edit.html")
            camp_edit = edit_frame_structure(output_dir / "campfire-edit.html")
            if rust_edit != camp_edit:
                raise RuntimeError("Message edit frame structure or static content differs from Campfire")
            print("edit_frame_identity_match=true")
        print(f"{args.operation}_identity_match=true")


if __name__ == "__main__":
    main()
