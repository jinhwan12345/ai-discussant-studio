import io
import os
import re
import json
import zipfile
import asyncio
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from http.server import BaseHTTPRequestHandler
import edge_tts

BASE_DIR = Path(__file__).parent.parent.resolve()

AVAILABLE_VOICES = [
    {"id": "ko-KR-SunHiNeural", "name": "Astra (여성 · 차분하고 또렷한 학술 톤)", "gender": "female"},
    {"id": "ko-KR-HyunsuMultilingualNeural", "name": "Nova (남성 · 중후하고 신뢰감 있는 학술 톤)", "gender": "male"},
    {"id": "ko-KR-InJoonNeural", "name": "Orion (남성 · 밝고 명확한 발표 톤)", "gender": "male"},
]


def extract_text_from_hwpx_bytes(raw_bytes: bytes) -> str:
    paragraphs = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as z:
        section_files = sorted(
            [f for f in z.namelist() if f.startswith("Contents/section") and f.endswith(".xml")]
        )
        for sec in section_files:
            xml_bytes = z.read(sec)
            root = ET.fromstring(xml_bytes)
            for elem in root.iter():
                if elem.tag.endswith("}p") or elem.tag == "p":
                    texts = []
                    for sub in elem.iter():
                        if (sub.tag.endswith("}t") or sub.tag == "t") and sub.text:
                            texts.append(sub.text)
                    line = "".join(texts).strip()
                    if line:
                        paragraphs.append(line)
    return "\n".join(paragraphs)


def parse_script_into_segments(raw_text: str):
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    cutoff_idx = None
    for idx, line in enumerate(lines):
        if "제공 프롬프트" in line or "[제공 자료]" in line:
            cutoff_idx = idx
            break
    if cutoff_idx is not None:
        lines = lines[:cutoff_idx]

    segments = []
    current_section = "지정토론"
    title = "AI 지정토론"
    subtitle = ""
    speaker = "GPT 6 - Astra"

    for i, line in enumerate(lines):
        if i == 0 and len(line) < 60 and not re.match(r"^\d+\.", line):
            title = line
            continue
        if i == 1 and (line.startswith("—") or line.startswith("-") or "AI 토론" in line):
            subtitle = line.lstrip("—- ").strip()
            continue
        if i <= 3 and ("GPT" in line or "Astra" in line) and len(line) < 30:
            speaker = line.strip()
            continue

        if re.match(r"^[0-9]+\.\s+", line) and len(line) < 85:
            current_section = line
            segments.append({
                "id": len(segments),
                "type": "section_header",
                "section": current_section,
                "text": line,
                "is_question": False
            })
            continue

        is_question = bool(
            re.search(r"(첫 번째 질문|두 번째 질문|세 번째 질문|질문입니다)", line)
            or (line.endswith("니까?") or line.endswith("까요?"))
        )

        sentences = re.split(r"(?<=[.?!])\s+", line)
        chunk = []
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            chunk.append(sent)
            if len(" ".join(chunk)) > 110 or sent.endswith("?") or len(chunk) >= 2:
                seg_text = " ".join(chunk)
                segments.append({
                    "id": len(segments),
                    "type": "question" if (is_question or seg_text.endswith("?")) else "speech",
                    "section": current_section,
                    "text": seg_text,
                    "is_question": is_question or seg_text.endswith("?")
                })
                chunk = []
        if chunk:
            seg_text = " ".join(chunk)
            segments.append({
                "id": len(segments),
                "type": "question" if (is_question or seg_text.endswith("?")) else "speech",
                "section": current_section,
                "text": seg_text,
                "is_question": is_question or seg_text.endswith("?")
            })

    return {
        "title": title,
        "subtitle": subtitle,
        "speaker": speaker,
        "segments": segments,
        "raw_text": "\n\n".join(lines)
    }


def load_default_script():
    json_path = BASE_DIR / "default_script.json"
    if json_path.exists():
        return json.loads(json_path.read_text(encoding="utf-8"))
    hwpx_files = list(BASE_DIR.glob("*.hwpx"))
    if hwpx_files:
        raw_text = extract_text_from_hwpx_bytes(hwpx_files[0].read_bytes())
        data = parse_script_into_segments(raw_text)
        data["filename"] = hwpx_files[0].name
        return data
    return {
        "title": "AI와 같은 결과를 얻는 것을 넘어, 타당한 연구를 함께 만드는 조건",
        "subtitle": "「AI를 활용한 양적 자료 수집 및 분석 방법」에 대한 AI 토론",
        "speaker": "GPT 6 - Astra",
        "filename": "기본 대본",
        "raw_text": "안녕하십니까. AI 토론자 아스트라입니다.",
        "segments": [
            {
                "id": 0,
                "type": "speech",
                "section": "0. 들어가며",
                "text": "안녕하십니까. AI 토론자 아스트라입니다.",
                "is_question": False
            }
        ]
    }


async def synthesize_tts_bytes(text: str, voice: str, rate: str, pitch: str) -> bytes:
    clean_text = re.sub(r"^[0-9]+\.\s*", "", text)
    clean_text = clean_text.replace("「", "").replace("」", "").replace("·", ", ")
    communicate = edge_tts.Communicate(clean_text, voice, rate=rate, pitch=pitch)
    audio_chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
    return b"".join(audio_chunks)


def parse_multipart_file(body: bytes, content_type: str):
    """Simple multipart/form-data parser for file upload without external deps."""
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return None, None
    boundary = m.group(1).strip().encode("utf-8")
    parts = body.split(b"--" + boundary)
    for part in parts:
        if b"Content-Disposition" in part and b"filename=" in part:
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            headers_raw = part[:header_end].decode("utf-8", errors="replace")
            file_bytes = part[header_end + 4:]
            if file_bytes.endswith(b"\r\n"):
                file_bytes = file_bytes[:-2]
            fn_match = re.search(r'filename="([^"]+)"', headers_raw)
            filename = fn_match.group(1) if fn_match else "uploaded.txt"
            return filename, file_bytes
    return None, None


class handler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _resolve_api_path(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        route = qs.get("route", [None])[0]
        if route:
            route = route.strip("/")
            return f"/api/{route}", qs
        return parsed.path, qs

    def do_GET(self):
        path, qs = self._resolve_api_path()

        if path == "/api/script/default":
            self._send_json(load_default_script())
            return

        if path == "/api/voices":
            self._send_json({"voices": AVAILABLE_VOICES})
            return

        if path == "/api/tts":
            text = (qs.get("text", [""])[0]).strip()
            voice = qs.get("voice", ["ko-KR-SunHiNeural"])[0]
            rate = qs.get("rate", ["+0%"])[0]
            pitch = qs.get("pitch", ["+0Hz"])[0]
            if not text:
                self._send_json({"error": "Empty text"}, 400)
                return
            mp3_bytes = asyncio.run(synthesize_tts_bytes(text, voice, rate, pitch))
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(mp3_bytes)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(mp3_bytes)
            return

        self._send_json({"status": "ok", "path": path})

    def do_POST(self):
        path, _ = self._resolve_api_path()
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        if path == "/api/script/parse":
            payload = json.loads(body.decode("utf-8"))
            raw_text = payload.get("text", "").strip()
            if not raw_text:
                self._send_json({"error": "대본 내용이 비어 있습니다."}, 400)
                return
            self._send_json(parse_script_into_segments(raw_text))
            return

        if path == "/api/script/upload":
            content_type = self.headers.get("Content-Type", "")
            filename, file_bytes = parse_multipart_file(body, content_type)
            if not file_bytes:
                self._send_json({"error": "파일이 없습니다."}, 400)
                return
            if filename.lower().endswith(".hwpx"):
                raw_text = extract_text_from_hwpx_bytes(file_bytes)
            else:
                try:
                    raw_text = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    raw_text = file_bytes.decode("cp949", errors="replace")
            data = parse_script_into_segments(raw_text)
            data["filename"] = filename
            self._send_json(data)
            return

        if path == "/api/tts":
            payload = json.loads(body.decode("utf-8"))
            text = payload.get("text", "").strip()
            voice = payload.get("voice", "ko-KR-SunHiNeural")
            rate = payload.get("rate", "+0%")
            pitch = payload.get("pitch", "+0Hz")
            if not text:
                self._send_json({"error": "Empty text"}, 400)
                return
            mp3_bytes = asyncio.run(synthesize_tts_bytes(text, voice, rate, pitch))
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(mp3_bytes)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(mp3_bytes)
            return

        self._send_json({"error": "Not found"}, 404)
