import os
import re
import json
import hashlib
import zipfile
import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path
from aiohttp import web
import edge_tts

BASE_DIR = Path(__file__).parent.resolve()
CACHE_DIR = BASE_DIR / ".tts_cache"
CACHE_DIR.mkdir(exist_ok=True)

AVAILABLE_VOICES = [
    {"id": "ko-KR-SunHiNeural", "name": "Astra (여성 · 차분하고 또렷한 학술 톤)", "gender": "female"},
    {"id": "ko-KR-HyunsuMultilingualNeural", "name": "Nova (남성 · 중후하고 신뢰감 있는 학술 톤)", "gender": "male"},
    {"id": "ko-KR-InJoonNeural", "name": "Orion (남성 · 밝고 명확한 발표 톤)", "gender": "male"},
]


def extract_text_from_hwpx(hwpx_path: Path) -> str:
    """Extract paragraph text from an .hwpx file."""
    paragraphs = []
    with zipfile.ZipFile(hwpx_path, "r") as z:
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
    """Parse raw debate script text into structured segments for presentation."""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    
    # Filter out the prompt appendix if present ("제공 프롬프트" onwards)
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
        # Detect document header lines
        if i == 0 and len(line) < 60 and not re.match(r"^\d+\.", line):
            title = line
            continue
        if i == 1 and (line.startswith("—") or line.startswith("-") or "AI 토론" in line):
            subtitle = line.lstrip("—- ").strip()
            continue
        if i <= 3 and ("GPT" in line or "Astra" in line) and len(line) < 30:
            speaker = line.strip()
            continue

        # Detect section headers like "0. 들어가며: ..." or "1. 첫째, ..."
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

        # Check if this paragraph is a key question to the presenter
        is_question = bool(
            re.search(r"(첫 번째 질문|두 번째 질문|세 번째 질문|질문입니다)", line)
            or (line.endswith("니까?") or line.endswith("까요?"))
        )

        # Split long paragraphs into natural speaking sentences (2 sentences per chunk max for comfortable reading/subtitles)
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
    """Find any .hwpx file in BASE_DIR and parse it."""
    hwpx_files = list(BASE_DIR.glob("*.hwpx"))
    if hwpx_files:
        raw_text = extract_text_from_hwpx(hwpx_files[0])
        data = parse_script_into_segments(raw_text)
        data["filename"] = hwpx_files[0].name
        return data
    return {
        "title": "AI와 같은 결과를 얻는 것을 넘어, 타당한 연구를 함께 만드는 조건",
        "subtitle": "「AI를 활용한 양적 자료 수집 및 분석 방법」에 대한 AI 토론",
        "speaker": "GPT 6 - Astra",
        "filename": "기본 대본",
        "raw_text": "안녕하십니까. AI 토론자 아스트라입니다. 대본을 입력하시면 실시간으로 음성 합성과 립싱크 애니메이션이 실행됩니다.",
        "segments": [
            {
                "id": 0,
                "type": "speech",
                "section": "0. 들어가며",
                "text": "안녕하십니까. AI 토론자 아스트라입니다. 대본을 입력하시면 실시간으로 음성 합성과 립싱크 애니메이션이 실행됩니다.",
                "is_question": False
            }
        ]
    }


async def handle_index(request):
    return web.FileResponse(
        BASE_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


async def handle_default_script(request):
    data = load_default_script()
    return web.json_response(data)


async def handle_parse_script(request):
    body = await request.json()
    raw_text = body.get("text", "").strip()
    if not raw_text:
        return web.json_response({"error": "대본 내용이 비어 있습니다."}, status=400)
    data = parse_script_into_segments(raw_text)
    return web.json_response(data)


async def handle_upload_file(request):
    reader = await request.multipart()
    field = await reader.next()
    if not field:
        return web.json_response({"error": "파일이 없습니다."}, status=400)
    filename = field.filename or "uploaded.txt"
    content = await field.read()

    if filename.lower().endswith(".hwpx"):
        temp_path = CACHE_DIR / "_temp_upload.hwpx"
        temp_path.write_bytes(content)
        raw_text = extract_text_from_hwpx(temp_path)
    else:
        try:
            raw_text = content.decode("utf-8")
        except UnicodeDecodeError:
            raw_text = content.decode("cp949", errors="replace")

    data = parse_script_into_segments(raw_text)
    data["filename"] = filename
    return web.json_response(data)


async def handle_tts(request):
    """Generate or serve cached MP3 from edge-tts."""
    if request.method == "POST":
        body = await request.json()
    else:
        body = request.query

    text = body.get("text", "").strip()
    voice = body.get("voice", "ko-KR-SunHiNeural")
    rate = body.get("rate", "+0%")
    pitch = body.get("pitch", "+0Hz")

    if not text:
        return web.json_response({"error": "Empty text"}, status=400)

    # Clean special symbols so TTS reads smoothly
    clean_text = re.sub(r"^[0-9]+\.\s*", "", text)
    clean_text = clean_text.replace("「", "").replace("」", "").replace("·", ", ")

    cache_key = hashlib.md5(f"{voice}|{rate}|{pitch}|{clean_text}".encode("utf-8")).hexdigest()
    cache_file = CACHE_DIR / f"{cache_key}.mp3"

    if not cache_file.exists() or cache_file.stat().st_size == 0:
        communicate = edge_tts.Communicate(clean_text, voice, rate=rate, pitch=pitch)
        await communicate.save(str(cache_file))

    return web.FileResponse(
        cache_file,
        headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def handle_voices(request):
    return web.json_response({"voices": AVAILABLE_VOICES})


def create_app():
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/script/default", handle_default_script)
    app.router.add_post("/api/script/parse", handle_parse_script)
    app.router.add_post("/api/script/upload", handle_upload_file)
    app.router.add_post("/api/tts", handle_tts)
    app.router.add_get("/api/tts", handle_tts)
    app.router.add_get("/api/voices", handle_voices)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting AI Discussant Studio server on http://localhost:{port}")
    web.run_app(create_app(), host="127.0.0.1", port=port)
