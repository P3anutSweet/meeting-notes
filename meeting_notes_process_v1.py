"""Meeting Notes v1: post-processing for one recorded meeting.

Usage (venv python, see meeting_notes_v2.cmd):
    python meeting_notes_process_v1.py <session_dir> [--title "..."] [--keep-wav] [--no-ledger]
    python meeting_notes_process_v1.py <session_dir> --notes-only [--force] [--no-ledger]   (notes from the saved transcript)
    python meeting_notes_process_v1.py <session_dir> --audio-only                          (audio.opus from the WAV chunks)
    python meeting_notes_process_v1.py <session_dir> --resume [--no-ledger]                (finish only the missing steps)
Exit codes: 0 done, 1 failed (inputs kept), 3 refused (nothing written).

Steps: read the WAV chunks (me = microphone, others = system loopback) -> final Whisper pass
(large-v3, local GPU) -> local diarization of "Others" (sherpa-onnx, no token) -> echo removal ->
transcript.md -> audio.opus (verified) -> optional native minutes lookup (tmeet / lark-cli, read-only) ->
notes.md via `claude -p` (Codex CLI fallback) -> optional one line in a Markdown ledger -> the WAV chunks are deleted
only after verified audio and successful notes (unless --keep-wav). Progress goes to <session_dir>/status.json.
Settings: meeting_notes_settings.json next to this script (or the file named by MN_SETTINGS), each key
overridable by an MN_<KEY> environment variable; see README.md. Also imported by meeting_notes_v2.pyw.
"""
import datetime as dt
import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import wave

import numpy as np

# ---------- settings: meeting_notes_settings.json next to the script, then MN_<KEY> environment overrides ----------
HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = os.path.expanduser("~")
_LOCAL = os.environ.get("LOCALAPPDATA") or os.path.join(_HOME, "AppData", "Local")
DEFAULTS = {
    "cache_dir": os.path.join(_LOCAL, "MeetingNotes"),         # sessions (WAV chunks), models, panel state
    "models_dir": "",                                          # "" = <cache_dir>\models
    "output_dir": os.path.join(_HOME, "Documents", "Meetings"),  # one folder per meeting: notes.md, transcript.md, audio.opus
    "log_dir": "",                                             # "" = <cache_dir>\logs
    "time_zone": "local",                                      # "local", or a fixed UTC offset in hours (e.g. 8)
    "time_zone_label": "",                                     # "" = "UTC+hh:mm"; e.g. "CET"
    "live_model": "large-v3-turbo",                            # faster-whisper model for the live captions
    "final_model": "large-v3",                                 # faster-whisper model for the final transcript
    "device": "cuda",
    "compute_type": "float16",
    "notes_engines": ["claude", "codex"],                      # tried in order; [] = transcript only
    "claude_label": "Claude (claude CLI)",
    "codex_label": "Codex (codex CLI)",
    "notes_for": "the person who recorded this meeting",       # who the notes are written for (prompt)
    "me_is": "that person",                                    # who "Me" (the microphone) is (prompt)
    "notes_failed_hint": "retry later with --notes-only, or write the notes from transcript.md",
    "limits_json": os.path.join(_LOCAL, "ai-limits-monitor", "latest.json"),  # optional: Claude at its limit -> Codex first
    "native_minutes": False,                                   # look up Tencent Meeting / Feishu minutes (read-only)
    "node": "",                                                # node.exe for the lookup ("" = node on PATH)
    "tmeet_script": "",                                        # tmeet CLI entry script (.js)
    "lark_script": "",                                         # lark-cli entry script (.js)
    "ledger_enabled": False,                                   # add one row per meeting to a Markdown ledger
    "ledger_writer": "markdown",                               # "markdown" (built in) or "state_io" (state_io_v1.py next to the script)
    "ledger_path": "",
    "ledger_section": "## Reports from other threads",
    "background": "wallpaper",                                 # panel glass: "wallpaper" (frosted desktop) or "plain"
}
SETTINGS_FILE = os.environ.get("MN_SETTINGS") or os.path.join(HERE, "meeting_notes_settings.json")


def _env_value(default, raw):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})   # "_about" = comment
    except FileNotFoundError:
        pass
    except Exception as e:
        sys.stderr.write(f"meeting_notes_settings.json ignored: {type(e).__name__}\n")
    for k in DEFAULTS:
        raw = os.environ.get("MN_" + k.upper())
        if raw:
            s[k] = _env_value(DEFAULTS[k], raw)
    for k in ("cache_dir", "models_dir", "output_dir", "log_dir", "limits_json", "node", "tmeet_script",
              "lark_script", "ledger_path"):
        if s[k]:
            s[k] = os.path.normpath(os.path.expandvars(os.path.expanduser(s[k])))
    s["models_dir"] = s["models_dir"] or os.path.join(s["cache_dir"], "models")
    s["log_dir"] = s["log_dir"] or os.path.join(s["cache_dir"], "logs")
    return s


S = load_settings()
CACHE = S["cache_dir"]
SESSIONS = os.path.join(CACHE, "sessions")
MODELS = S["models_dir"]
OUT_ROOT = S["output_dir"]
LOG_DIR = S["log_dir"]
LEDGER = S["ledger_path"] if S["ledger_enabled"] else ""
LIMITS_JSON = S["limits_json"]
NODE = S["node"] or shutil.which("node") or ""
TMEET = S["tmeet_script"]
LARK = S["lark_script"]
DIAR_SEG = os.path.join(MODELS, r"diar\sherpa-onnx-pyannote-segmentation-3-0\model.onnx")
DIAR_EMB = os.path.join(MODELS, r"diar\emb.onnx")
SR = 16000
LIVE_MODEL = S["live_model"]
FINAL_MODEL = S["final_model"]
TZ = None if str(S["time_zone"]).lower() == "local" else dt.timezone(dt.timedelta(hours=float(S["time_zone"])))
BJT = TZ   # old name (None = this PC's local zone), kept for callers


def now_tz():
    """Aware 'now' in the configured zone (local zone with its daylight-saving rules when time_zone = local)."""
    return dt.datetime.now(TZ) if TZ else dt.datetime.now().astimezone()


def from_ts(ts):
    return dt.datetime.fromtimestamp(ts, TZ) if TZ else dt.datetime.fromtimestamp(ts).astimezone()


def tz_offset(t=None):
    """'+08:00' style offset of the configured zone."""
    z = (t or now_tz()).strftime("%z")
    return z[:3] + ":" + z[3:] if z else "+00:00"


TZ_LABEL = S["time_zone_label"] or ("UTC" + tz_offset())
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# Whisper's well-known hallucinations on silence / noise
HALLU = re.compile(r"(thank(s| you) for watching|teksting av|subtitles? by|字幕(由|提供|志愿者)|请不吝点赞|"
                   r"订阅|明镜|amara\.org|^\W*(thank you|you|bye)\W*$|^\W*谢谢(观看|大家)?\W*$)", re.I)


def setup_cuda_dlls():
    """cuBLAS / cuDNN come from the nvidia-* pip wheels inside the venv."""
    base = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    for d in glob.glob(os.path.join(base, "*", "bin")):
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{now_tz():%Y-%m-%d %H:%M:%S} {msg}"
    with open(os.path.join(LOG_DIR, f"meeting_notes_{now_tz():%Y%m}.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


_t2s = None


def to_simplified(text):
    global _t2s
    if _t2s is None:
        try:
            from opencc import OpenCC
            _t2s = OpenCC("t2s")
        except Exception:
            _t2s = False
    return _t2s.convert(text) if _t2s else text


# ---------- ASR ----------
def load_model(name):
    setup_cuda_dlls()
    from faster_whisper import WhisperModel
    return WhisperModel(name, device=S["device"], compute_type=S["compute_type"], download_root=MODELS)


def speech_spans(audio, min_silence_ms=500):
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    return get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=min_silence_ms, speech_pad_ms=200))


def clean_text(text, lang):
    t = text.strip()
    if lang == "zh":
        t = to_simplified(t)
        cjk = r"[㐀-鿿]"
        for a, b in ((",", "，"), ("?", "？"), ("!", "！"), (":", "："), (";", "；")):
            t = re.sub(rf"(?<={cjk})\s*{re.escape(a)}\s*", b, t)
    return t


def transcribe_piece(model, audio, offset, beam=5):
    """One utterance group (<= 30 s): detect its language, transcribe; returns segment dicts with absolute times."""
    if len(audio) < SR * 0.3:
        return []
    lang, prob = "en", 0.0
    try:
        lang, prob, _ = model.detect_language(audio)
    except Exception:
        pass
    if lang not in ("zh", "en", "yue", "ja", "ko", "de", "fr", "es", "pt", "ru", "it"):
        lang = "zh" if prob < 0.5 else lang
    if lang == "yue":
        lang = "zh"
    prompt = "以下是普通话会议的记录，使用简体中文和标点。" if lang == "zh" else None
    segs, info = model.transcribe(audio, language=lang, beam_size=beam, vad_filter=False,
                                  condition_on_previous_text=False, initial_prompt=prompt,
                                  no_speech_threshold=0.6, temperature=[0.0, 0.2, 0.4])
    out = []
    for s in segs:
        text = clean_text(s.text, lang)
        if not text or HALLU.search(text):
            continue
        if s.no_speech_prob > 0.7 and s.avg_logprob < -0.8:
            continue
        out.append({"start": round(offset + s.start, 2), "end": round(offset + s.end, 2), "text": text, "lang": lang})
    return out


def transcribe_track(model, audio, max_group=25.0, progress=None):
    """Whole track: VAD -> groups of speech up to max_group seconds -> per-group language detection."""
    spans = speech_spans(audio)
    groups, cur = [], None
    for sp in spans:
        if cur and (sp["end"] - cur[0]) / SR <= max_group and (sp["start"] - cur[1]) / SR < 1.5:
            cur[1] = sp["end"]
        else:
            if cur:
                groups.append(cur)
            cur = [sp["start"], sp["end"]]
    if cur:
        groups.append(cur)
    segs = []
    for i, (a, b) in enumerate(groups):
        a2, b2 = max(0, a - int(0.1 * SR)), min(len(audio), b + int(0.1 * SR))
        piece = audio[a2:b2]
        while len(piece) > 29 * SR:          # very long monologue without pauses: hard split
            segs += transcribe_piece(model, piece[:28 * SR], a2 / SR)
            piece, a2 = piece[28 * SR:], a2 + 28 * SR
        segs += transcribe_piece(model, piece, a2 / SR)
        if progress:
            progress(i + 1, len(groups))
    return segs


# ---------- audio files ----------
def read_wav_chunk(path):
    """Read a recorder chunk (16 kHz mono int16 PCM, the data chunk is the last chunk of the file) up to the end of
    the file, NOT only as far as the header's frame count: after a crash between writing samples and patching the
    header, the header may count fewer frames than the file holds (or zero). All complete samples on disk are read,
    so a chunk that is deleted later never held audio that is missing from the saved recording."""
    with open(path, "rb") as f:
        raw = f.read()
    start = 44
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        i = 12
        while i + 8 <= len(raw):                       # walk the RIFF chunks to the data chunk
            cid, size = raw[i:i + 4], int.from_bytes(raw[i + 4:i + 8], "little")
            if cid == b"fmt ":
                fmt = raw[i + 8:i + 8 + 16]
                ch, rate, bits = (int.from_bytes(fmt[2:4], "little"), int.from_bytes(fmt[4:8], "little"),
                                  int.from_bytes(fmt[14:16], "little"))
                if (ch, rate, bits) != (1, SR, 16):
                    raise RuntimeError(f"{os.path.basename(path)}: not a recorder chunk ({ch} ch, {rate} Hz, {bits} bit)")
            if cid == b"data":
                start = i + 8
                break
            i += 8 + size + (size & 1)
    payload = raw[start:]
    return np.frombuffer(payload[: len(payload) // 2 * 2], dtype=np.int16)


def load_track(sdir, kind, files=None):
    """One track from its WAV chunks: all of them, or exactly `files` (an input snapshot) when given."""
    files = sorted(glob.glob(os.path.join(sdir, f"{kind}_*.wav"))) if files is None else \
        sorted(f for f in files if os.path.basename(f).startswith(kind + "_"))
    if not files:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate([read_wav_chunk(f) for f in files]).astype(np.float32) / 32768.0


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def encode_opus(me, others, out_path, tmp_dir):
    n = max(len(me), len(others))
    mix = np.zeros(n, dtype=np.float32)
    mix[: len(me)] += me
    mix[: len(others)] += others
    peak = float(np.max(np.abs(mix))) if n else 0.0
    if peak > 0.98:
        mix *= 0.98 / peak
    if n == 0:
        raise RuntimeError("no audio to encode")
    import uuid
    tag = uuid.uuid4().hex[:12]                            # unique per attempt: no two runs share a temporary file
    tmp = os.path.join(tmp_dir, f"mix_tmp_{tag}.wav")
    # encoded and verified next to the destination (same volume), then moved over out_path
    opus_tmp = os.path.join(os.path.dirname(out_path), f".audio_tmp_{tag}.opus")
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((mix * 32767).astype(np.int16).tobytes())
    try:
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", tmp, "-c:a", "libopus", "-b:a", "32k",
                        "-application", "voip", "-f", "opus", opus_tmp], check=True, creationflags=NO_WINDOW)
        # verify: decode the opus and compare its duration
        import av
        with av.open(opus_tmp) as c:
            dur = float(c.duration / 1e6) if c.duration else 0.0
        want = n / SR
        if want > 0 and abs(dur - want) > max(1.5, want * 0.01):
            raise RuntimeError(f"opus duration {dur:.1f}s != {want:.1f}s")
        os.replace(opus_tmp, out_path)
    finally:
        os.remove(tmp)
        if os.path.exists(opus_tmp):
            os.remove(opus_tmp)
    return dur


# ---------- diarization of "Others" ----------
def diarize(audio):
    """Returns [(start, end, speaker_index)] or None when it cannot run."""
    try:
        import sherpa_onnx
    except Exception:
        return None
    if not (os.path.exists(DIAR_SEG) and os.path.exists(DIAR_EMB)) or len(audio) < SR * 2:
        return None
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=DIAR_SEG), num_threads=4),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=DIAR_EMB, num_threads=4),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.6),
        min_duration_on=0.3, min_duration_off=0.5)
    if not cfg.validate():
        return None
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    res = sd.process(audio.astype(np.float32)).sort_by_start_time()
    return [(r.start, r.end, r.speaker) for r in res]


def assign_speakers(segs, turns):
    if not turns:
        return
    order = {}
    for s in segs:
        best, best_ov = None, 0.0
        for a, b, spk in turns:
            ov = min(b, s["end"]) - max(a, s["start"])
            if ov > best_ov:
                best, best_ov = spk, ov
        if best is None:  # no overlap: nearest turn
            best = min(turns, key=lambda t: min(abs(t[0] - s["end"]), abs(t[1] - s["start"])))[2]
        if best not in order:
            order[best] = len(order) + 1
        s["spk"] = order[best]
    if len(order) <= 1:
        for s in segs:
            s.pop("spk", None)


def norm(t):
    return re.sub(r"[\W_]+", "", t.lower())


def drop_echo(me, others):
    """Laptop speakers + mic: the mic hears the others too. Drop 'Me' segments that repeat an 'Others' one."""
    kept = []
    for m in me:
        nm = norm(m["text"])
        echo = False
        for o in others:
            if o["end"] < m["start"] - 2 or o["start"] > m["end"] + 2:
                continue
            if nm and difflib.SequenceMatcher(None, nm, norm(o["text"])).ratio() > 0.5:
                echo = True
                break
            if nm and len(nm) >= 4 and nm in norm(o["text"]):
                echo = True
                break
        if not echo:
            kept.append(m)
    return kept


def hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def build_transcript(me, others):
    rows = [dict(s, who="Me") for s in me]
    for s in others:
        rows.append(dict(s, who=f"Speaker {s['spk']}" if "spk" in s else "Others"))
    rows.sort(key=lambda r: r["start"])
    paras = []
    for r in rows:
        if paras and paras[-1]["who"] == r["who"] and r["start"] - paras[-1]["end"] < 4:
            p = paras[-1]
            sep = "" if r["lang"] == "zh" and p["lang"] == "zh" else " "
            p["text"] += sep + r["text"]
            p["end"] = r["end"]
        else:
            paras.append(dict(r))
    return paras


# ---------- native minutes (read-only) ----------
def run_node(script, args, timeout=40):
    if not (os.path.exists(NODE) and os.path.exists(script)):
        return None
    try:
        p = subprocess.run([NODE, script] + args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        out = p.stdout.decode("utf-8", "replace")
        i = out.find("{")
        return json.loads(out[i:]) if i >= 0 else None
    except Exception:
        return None


def find_urls(obj, keys=("url", "minute_url", "share_url", "record_url", "view_url")):
    found = []
    if isinstance(obj, dict):
        title = obj.get("topic") or obj.get("title") or obj.get("subject") or ""
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v.startswith("http"):
                found.append((title, v))
            else:
                found += find_urls(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            found += find_urls(v, keys)
    return found


def native_minutes(t_start, t_end):
    """Best effort: Tencent Meeting minutes / recordings and Feishu minutes in the meeting's time window."""
    a, b = (t_start - dt.timedelta(minutes=15)).astimezone(TZ), (t_end + dt.timedelta(minutes=15)).astimezone(TZ)
    s, e = a.strftime("%Y-%m-%dT%H:%M") + tz_offset(a), b.strftime("%Y-%m-%dT%H:%M") + tz_offset(b)
    res = {"checked": now_tz().strftime("%Y-%m-%d %H:%M ") + TZ_LABEL, "links": [], "notes": []}
    tm = run_node(TMEET, ["minutes", "search", "--start", s, "--end", e])
    tr = run_node(TMEET, ["record", "list", "--start", s, "--end", e])
    lk = run_node(LARK, ["minutes", "+search", "--start", s, "--end", e, "--participant-ids", "me"])
    for name, obj in (("Tencent Meeting minutes", tm), ("Tencent Meeting recording", tr), ("Feishu minutes", lk)):
        if obj is None:
            res["notes"].append(f"{name}: not reachable")
            continue
        urls = find_urls(obj)
        for title, u in urls:
            res["links"].append(f"{name}: [{title or 'open'}]({u})")
        if not urls:
            res["notes"].append(f"{name}: none in this time window")
    return res


# ---------- notes via the plan CLIs ----------
NOTES_PROMPT = """You are writing meeting notes for {notes_for}.
Below is an automatic transcript of a meeting. "Me" is {me_is}; "Others" / "Speaker N" are the other participants
(speaker numbers are automatic and may be imperfect). The transcript may contain recognition errors: correct obvious ones
from context, never invent facts.

Write Markdown with exactly these sections, in the meeting's main language ({lang_name}):
## Summary            (5-8 bullet points)
## Decisions
## Action items       (a table: | # | Owner | Action | Due | ; Owner = a name or "Me"/"Speaker N"; Due = a date if one was said, else "-")
## Open questions
## Key numbers        (prices, quantities, dates, model names, percentages, with context)
{english}
Use "-" for an empty section. After everything, on the last line, write exactly: <!-- action_items: N --> with N = number of action-item rows.

Meeting title: {title}
Date: {date}

TRANSCRIPT
{transcript}
"""


def limits_say_claude_full():
    """Optional: an AI Limits Monitor snapshot (latest.json) less than an hour old says Claude is at its limit."""
    if not LIMITS_JSON:
        return False
    try:
        with open(LIMITS_JSON, encoding="utf-8") as f:
            d = json.load(f)
        age = time.time() - dt.datetime.fromisoformat(d["checked_at"]).timestamp()
        if age > 3600:
            return False
        for svc in d.get("services", []):
            if svc.get("name") == "Claude":
                return any((m.get("pct") or 0) >= 99.5 for m in svc.get("meters", [])
                           if m.get("key", "").startswith(("claude:session", "claude:weekly_all")))
    except Exception:
        pass
    return False


def run_claude(prompt, cwd):
    p = subprocess.run(["claude", "-p", "--output-format", "text", "--no-session-persistence", "--tools", "",
                        "--setting-sources", ""], input=prompt.encode("utf-8"), capture_output=True,
                       cwd=cwd, timeout=900, creationflags=NO_WINDOW, shell=False,
                       executable=shutil.which("claude"))
    out = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0 or not out or re.search(r"usage limit|rate limit|limit reached|out of extra usage", out[:300], re.I):
        raise RuntimeError(f"claude rc={p.returncode}: {out[:200]} {p.stderr.decode('utf-8', 'replace')[:200]}")
    return out


def run_codex(prompt, cwd):
    """One attempt = one new output file; accepted only with exit code 0 and a non-empty file written by it."""
    import uuid
    last = os.path.join(cwd, f"codex_last_{uuid.uuid4().hex}.md")
    codex = shutil.which("codex.cmd") or shutil.which("codex")
    try:
        p = subprocess.run([codex, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-o", last, "-"],
                           input=prompt.encode("utf-8"), capture_output=True, cwd=cwd, timeout=1200,
                           creationflags=NO_WINDOW)
        out = open(last, encoding="utf-8").read().strip() if os.path.exists(last) else ""
    finally:
        if os.path.exists(last):
            os.remove(last)
    if p.returncode != 0 or not out:
        raise RuntimeError(f"codex rc={p.returncode}: {p.stderr.decode('utf-8', 'replace')[-300:]}")
    return out


def make_notes(transcript_md, title, date, main_lang, cwd):
    lang_name = {"zh": "Chinese (简体中文)", "en": "English"}.get(main_lang, main_lang)
    english = "" if main_lang == "en" else "## English summary  (the summary and action items again, in English)\n"
    prompt = NOTES_PROMPT.format(lang_name=lang_name, english=english, title=title, date=date, transcript=transcript_md,
                                 notes_for=S["notes_for"], me_is=S["me_is"])
    errors = []
    known = {"claude": (S["claude_label"], run_claude), "codex": (S["codex_label"], run_codex)}
    engines = [known[k.lower()] for k in S["notes_engines"] if k.lower() in known]
    if not engines:
        return None, None, ["no notes engine configured (notes_engines)"]
    if limits_say_claude_full() and engines[0][1] is run_claude and len(engines) > 1:
        engines.reverse()
        errors.append("Claude at its limit per AI Limits monitor")
    for name, fn in engines:
        try:
            return fn(prompt, cwd), name, errors
        except Exception as e:
            errors.append(f"{name}: {e}")
            log(f"notes engine failed: {name}: {e}")
    return None, None, errors


# ---------- optional ledger hook (off by default) ----------
class path_lock:
    """Cross-process lock on a lock file (msvcrt byte lock; Windows releases it when the process ends). Waits up to
    `wait` seconds, then raises TimeoutError."""

    def __init__(self, path, wait=60.0):
        self.path, self.wait, self.fh = path, wait, None

    def __enter__(self):
        import msvcrt
        end = time.time() + self.wait
        self.fh = open(self.path, "a+b")
        while True:
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > end:
                    self.fh.close()
                    raise TimeoutError(f"{self.path} stayed locked for {self.wait:.0f} s")
                time.sleep(0.1)

    def __exit__(self, *exc):
        import msvcrt
        try:
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()
        return False


def append_ledger(title, folder, n_actions):
    """One table row per meeting under ledger_section of a Markdown file. ledger_writer "markdown" inserts the row
    after the last table row of that section; "state_io" hands it to state_io_v1.py (a locked writer kept next to
    this script in some installations; not part of this repository)."""
    if not S["ledger_enabled"]:
        return False
    try:
        now = now_tz().strftime("%Y-%m-%d %H:%M")
        t = title.replace("|", "/")
        row = (f"| {now} | Meeting Notes | Meeting \"{t}\" transcribed and summarized: {n_actions} action item(s) "
               f"to route to projects. | {folder}\\notes.md |")
        if S["ledger_writer"] == "state_io":
            sys.path.insert(0, HERE)
            import state_io_v1 as sio  # one locked writer path for shared state files
            sio.append("ledger", row, section=S["ledger_section"])
            return True
        if not LEDGER:
            raise RuntimeError("ledger_path is not set")
        with path_lock(LEDGER + ".lock", wait=60):     # one writer at a time across processes; read only under it
            with open(LEDGER, encoding="utf-8") as f:
                lines = f.read().split("\n")
            i = next(k for k, l in enumerate(lines) if l.strip() == S["ledger_section"].strip())
            j = i + 1
            while j < len(lines) and not lines[j].startswith("|"):
                j += 1
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            lines.insert(j, row)
            write_atomic(LEDGER, "\n".join(lines))
        return True
    except Exception as e:
        log(f"ledger append failed: {e}")
        return False



# ---------- main ----------
# Stages of one session, kept in <session_dir>\status.json "stages" so a retry or a recovery knows what is done:
#   transcript (segments.json + transcript.md)  ->  audio (audio.opus verified: True, or "failed")  ->  notes ("done" or
#   "failed")  ->  ledger (optional row)  ->  processed.flag  ->  WAV chunks deleted, last.
# Safety rules (each one closes a data-loss path found in review):
#   - One process per session: every operation holds a per-session OS lock (released automatically if the process
#     dies); a second process is refused before it reads or writes anything.
#   - Ownership: each session has a random id ("uid" in session.json, or created once in status.json); its output
#     folder carries that id in .meeting_notes_session. Nothing is written into or deleted for a folder whose marker
#     does not match, so older folders (no marker, or a timestamp marker) stay read-only.
#   - Committed results: before transcript.md / notes.md is replaced, its expected hash is saved in status.json
#     ("pending"); after the replace the stage is marked. A crash in between is reconciled by comparing hashes.
#   - A failed notes attempt never replaces an existing notes.md.
#   - A full run is refused once a transcript is saved or this session's folder already holds results: --resume
#     finishes only the missing stages.
#   - WAV chunks are deleted only when the flag is written, the notes are done and the audio is verified and saved,
#     and only the chunks recorded as the inputs of that saved audio (same size and time); any other chunk stays.
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 3
SESSION_MARK = ".meeting_notes_session"
LOCK_FILE = ".meeting_notes.lock"
RESULT_FILES = ("transcript.md", "notes.md", "audio.opus")


class Refused(Exception):
    """The request would overwrite finished or foreign output, has no source, or the session is busy: no meeting file,
    status or log was written (only the session's lock file may have been created)."""


def safe_name(title):
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip().strip(".")
    t = re.sub(r"\s+", "_", t)
    return t[:48] or "Meeting"


def write_atomic(path, text):
    """Write a text file through a temporary file of its own in the same folder, then replace: never half a file."""
    import uuid
    tmp = f"{path}.{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def sha_text(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class StatusUnreadable(Exception):
    """status.json exists but cannot be read or parsed (e.g. held by a virus scanner): stop without rewriting it."""


def read_status(sdir, wait=5.0):
    """The session status. A missing file is an empty status; an existing file that cannot be read or parsed is
    retried for `wait` seconds and then raises StatusUnreadable, so it is never mistaken for an empty status
    (and never rebuilt from nothing by set_status)."""
    path = os.path.join(sdir, "status.json")
    end = time.time() + wait
    while True:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            if time.time() > end:
                raise StatusUnreadable(f"{path} cannot be read ({type(e).__name__}); left untouched") from e
            time.sleep(0.2)


def set_status(sdir, **kw):
    st = read_status(sdir)
    st.update(kw)
    st["updated"] = time.time()
    write_atomic(os.path.join(sdir, "status.json"), json.dumps(st, ensure_ascii=False, indent=1))


def mark_stage(sdir, stage, value=True):
    stages = read_status(sdir).get("stages") or {}
    stages[stage] = value
    set_status(sdir, stages=stages)


def wav_chunks(sdir):
    return sorted(glob.glob(os.path.join(sdir, "mic_*.wav")) + glob.glob(os.path.join(sdir, "loop_*.wav")))


def chunk_manifest(files):
    out = {}
    for f in files:
        s = os.stat(f)
        out[os.path.basename(f)] = [s.st_size, s.st_mtime_ns]
    return out


# ---- one process per session ----
_LOCKS = {}


class session_lock:
    """Per-session lock (msvcrt byte lock on <session>\\.meeting_notes.lock). Re-entrant inside one process; a
    competing process gets Refused. Windows releases it when the process ends, so a crash never leaves it stuck."""

    def __init__(self, sdir):
        self.sdir = sdir
        self.key = os.path.normcase(os.path.abspath(sdir))

    def __enter__(self):
        held = _LOCKS.get(self.key)
        if held:
            held[1] += 1
            return self
        if not os.path.isdir(self.sdir):
            raise Refused("session folder not found")
        import msvcrt
        fh = open(os.path.join(self.sdir, LOCK_FILE), "a+b")
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            raise Refused("another process is working on this session right now")
        _LOCKS[self.key] = [fh, 1]
        return self

    def __exit__(self, *exc):
        held = _LOCKS[self.key]
        held[1] -= 1
        if held[1] == 0:
            import msvcrt
            fh = held[0]
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            fh.close()
            del _LOCKS[self.key]
        return False


# ---- ownership ----
def session_id(sdir):
    return os.path.basename(os.path.normpath(sdir))


def session_uid(sdir):
    """The session's random id: "uid" in session.json (written by the recorder), else "uid" in status.json."""
    try:
        with open(os.path.join(sdir, "session.json"), encoding="utf-8") as f:
            uid = json.load(f).get("uid")
        if uid:
            return str(uid)
    except Exception:
        pass
    return read_status(sdir).get("uid")


def folder_owner(folder):
    try:
        with open(os.path.join(folder, SESSION_MARK), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def owns_folder(sdir, folder):
    """True only when the folder's marker holds this session's random id. Folders without a marker or with an older
    timestamp marker prove nothing, so they stay read-only."""
    uid = session_uid(sdir)
    if not uid or not folder or not os.path.isdir(folder):
        return False
    return folder_owner(folder) == uid


def claim_folder(sdir, t0, title, uid):
    """The output folder of this session: one it already owns, or a NEW folder created exclusively:
    <YYYYMMDD_HHMM>_<title>, else <session folder name>_<title>, else <session folder name>_<id8>_<title>."""
    prev = read_status(sdir).get("folder")
    if prev and os.path.isdir(prev) and folder_owner(prev) == uid:
        return prev
    os.makedirs(OUT_ROOT, exist_ok=True)
    sid = session_id(sdir)
    for name in (f"{t0:%Y%m%d_%H%M}_{safe_name(title)}", f"{sid}_{safe_name(title)}",
                 f"{sid}_{uid[:8]}_{safe_name(title)}"):
        f = os.path.join(OUT_ROOT, name)
        if os.path.isdir(f) and folder_owner(f) == uid:
            return f
        try:
            os.mkdir(f)              # exclusive: fails when the folder already exists
        except FileExistsError:
            continue
        write_atomic(os.path.join(f, SESSION_MARK), uid + "\n")
        return f
    raise Refused("all output folder names for this session are already used by other folders")


# ---- committed results ----
def commit_result(sdir, path, text, stage, value):
    """Save a result file so that a crash at any point can be reconciled: the expected hash is recorded first, then
    the file is replaced, then the stage is marked and the pending record removed."""
    pend = read_status(sdir).get("pending") or {}
    pend[stage] = {"file": os.path.basename(path), "sha": sha_text(text), "value": value}
    set_status(sdir, pending=pend)
    write_atomic(path, text)
    mark_stage(sdir, stage, value)
    pend = read_status(sdir).get("pending") or {}
    pend.pop(stage, None)
    set_status(sdir, pending=pend)


def pending_matches(sdir, folder):
    """{stage: value} for saved results whose file matches the hash recorded just before it was written."""
    out = {}
    for stage, rec in (read_status(sdir).get("pending") or {}).items():
        path = os.path.join(folder or "", rec.get("file") or "")
        try:
            if folder and os.path.isfile(path) and sha_file(path) == rec.get("sha"):
                out[stage] = rec.get("value", True)
        except OSError:
            pass
    return out


def reconcile(sdir, folder):
    """Adopt results that were fully written before a crash but whose stage was not marked yet."""
    for stage, value in pending_matches(sdir, folder).items():
        mark_stage(sdir, stage, value)
        pend = read_status(sdir).get("pending") or {}
        pend.pop(stage, None)
        set_status(sdir, pending=pend)


def audio_saved(sdir):
    st = read_status(sdir)
    folder = st.get("folder")
    return (st.get("stages") or {}).get("audio") is True and bool(folder) and \
        os.path.exists(os.path.join(folder, "audio.opus")) and owns_folder(sdir, folder) and bool(st.get("audio_inputs"))


def deletable_chunks(sdir):
    """The WAV chunks recorded as inputs of the saved audio, unchanged since (same size and time)."""
    inputs = read_status(sdir).get("audio_inputs") or {}
    out = []
    for f in wav_chunks(sdir):
        try:
            s = os.stat(f)
        except OSError:
            continue
        if inputs.get(os.path.basename(f)) == [s.st_size, s.st_mtime_ns]:
            out.append(f)
    return out


def cleanup_ready(sdir):
    st = read_status(sdir)
    return (os.path.exists(os.path.join(sdir, "processed.flag")) and (st.get("stages") or {}).get("notes") == "done"
            and audio_saved(sdir) and not st.get("keep_wav"))


def cleanup_wavs(sdir):
    """Delete the WAV chunks only when every stage is done (flag, notes, verified audio, no --keep-wav), and only the
    chunks that went into the saved audio; any other chunk stays and is listed in status.json "kept_chunks".
    Checked again here under the session lock, whoever calls. Returns True when chunks were deleted."""
    try:
        with session_lock(sdir):
            if not cleanup_ready(sdir):
                return False
            gone = deletable_chunks(sdir)
            for f in gone:
                os.remove(f)
            left = [os.path.basename(f) for f in wav_chunks(sdir)]
            if left:
                set_status(sdir, kept_chunks=left)
                log(f"{sdir}: {len(left)} WAV chunk(s) were not part of the saved audio and are kept: {left}")
            return bool(gone)
    except Refused:
        return False


def delete_wavs(sdir):   # old name used by older panels; same checks
    return cleanup_wavs(sdir)


def recovery_action(sdir):
    """What an automatic recovery may do with a session nobody is processing (used by the panel at start; read-only):
    'resume' = transcript saved (or provably written before a crash) in a folder this session owns, run unfinished:
    only the missing stages run; 'full' = WAV chunks, nothing saved yet anywhere; 'cleanup' = everything done, some
    WAV chunks of the saved audio left, no keep request; None = leave it (done, older version, ambiguous, or needs a
    person)."""
    st = read_status(sdir)
    stages = dict(st.get("stages") or {})
    done = os.path.exists(os.path.join(sdir, "processed.flag"))
    if done:
        return "cleanup" if cleanup_ready(sdir) and deletable_chunks(sdir) else None
    folder = st.get("folder")
    owned = owns_folder(sdir, folder)
    if owned:
        stages.update(pending_matches(sdir, folder))
    if stages.get("transcript"):
        return "resume" if owned and os.path.exists(os.path.join(sdir, "segments.json")) else None
    if stages or st.get("error") or not wav_chunks(sdir):
        return None
    if owned and any(os.path.exists(os.path.join(folder, f)) for f in RESULT_FILES):
        return None                  # results on disk without a committed stage: a person decides
    return "full"


def transcript_rows(seg_m, seg_o):
    paras = build_transcript(seg_m, seg_o)
    langs = {}
    for s in seg_m + seg_o:
        langs[s["lang"]] = langs.get(s["lang"], 0) + len(s["text"])
    return paras, langs, sorted({p["who"] for p in paras})


def finalize(sdir, meta, title, folder, n_actions, ledger):
    """Last steps after good notes: optional ledger row (once), completion flag, guarded WAV clean-up."""
    stages = read_status(sdir).get("stages") or {}
    if ledger and meta.get("ledger", True) and not stages.get("ledger"):
        if append_ledger(title, folder, n_actions):
            mark_stage(sdir, "ledger", True)
    stage = "Notes ready" if audio_saved(sdir) else "Notes ready (audio not saved: WAV chunks kept, use --audio-only)"
    set_status(sdir, stage=stage, done=True, folder=folder, actions=n_actions, error=None, notes_retry_error=None)
    write_atomic(os.path.join(sdir, "processed.flag"), folder)
    cleanup_wavs(sdir)


def write_notes(sdir, meta, title, folder, t0, t1, dur, paras, langs, speakers, audio_ok, nat, ledger):
    """Notes stage (+ finalize). Returns True when the notes came from an engine (or there was no speech).
    On failure an existing notes.md is never replaced: good earlier notes stay, and only a session without any
    notes.md gets a placeholder. WAVs and flag are left alone on failure."""
    set_status(sdir, stage="Writing notes")
    main_lang = max(langs, key=langs.get) if langs else "en"
    n_actions = 0
    if paras:
        tx = "\n".join(f"[{hms(p['start'])}] {p['who']}: {p['text']}" for p in paras)
        notes, engine, errs = make_notes(tx, title, f"{t0:%Y-%m-%d %H:%M} {TZ_LABEL}", main_lang, sdir)
    else:
        notes, engine, errs = "_No speech was detected, so there are no notes._", "-", []
    ok = notes is not None
    err_txt = "; ".join(errs)[:300] or "no engine answered"
    notes_path = os.path.join(folder, "notes.md")
    links = ["[Transcript](transcript.md)"] + (["[Audio](audio.opus)"] if audio_ok else [])
    nat_line = ""
    if nat is not None:
        nat_lines = nat["links"] or [f"none found ({'; '.join(nat['notes'])}; checked {nat['checked']})"]
        nat_line = "- Native minutes: " + "; ".join(nat_lines) + "\n"

    def page(body, by):
        return (f"# {title}: notes\n\n- Date: {t0:%Y-%m-%d %H:%M}–{t1:%H:%M} {TZ_LABEL} ({hms(dur)}); "
                f"speakers: {', '.join(speakers) or '-'}\n- Files: {' · '.join(links)}\n"
                + nat_line + f"- Notes written by: {by}\n\n" + body.strip() + "\n")

    if not ok:
        if os.path.exists(notes_path):
            log(f"notes attempt failed {folder}: {err_txt}; the existing notes.md is kept")
            if (read_status(sdir).get("stages") or {}).get("notes") != "done":
                mark_stage(sdir, "notes", "failed")
            set_status(sdir, stage="Notes attempt failed: the existing notes are kept", done=True, folder=folder,
                       notes_retry_error=err_txt)
            return False
        write_atomic(notes_path, page("_Notes could not be generated now (" + err_txt + "). The transcript is complete; "
                                      + S["notes_failed_hint"] + "._", "none"))
        mark_stage(sdir, "notes", "failed")
        log(f"notes failed {folder}: {err_txt}; WAV chunks kept, retry with --notes-only")
        set_status(sdir, stage="Notes failed: transcript saved, retry the notes later", done=True, folder=folder,
                   error="notes: " + err_txt[:200])
        return False
    m = re.search(r"<!--\s*action_items:\s*(\d+)\s*-->", notes)
    if m:
        n_actions = int(m.group(1))
    else:
        sec = re.search(r"## Action items(.*?)(\n## |\Z)", notes, re.S)
        if sec:
            n_actions = len([l for l in sec.group(1).splitlines() if re.match(r"\|\s*\d+\s*\|", l)])
    commit_result(sdir, notes_path, page(notes, engine), "notes", "done")
    set_status(sdir, actions=n_actions)
    log(f"done {folder}: {dur:.0f}s, {len(paras)} paragraphs, {n_actions} actions, {engine}")
    finalize(sdir, meta, title, folder, n_actions, ledger)
    return True


def encode_saved(sdir, folder, files=None):
    """Encode audio.opus into this session's own folder from exactly the given WAV chunks (default: all present) and
    record them as the audio's inputs; only those chunks may be deleted later."""
    files = wav_chunks(sdir) if files is None else files
    inputs = chunk_manifest(files)
    me, others = load_track(sdir, "mic", files), load_track(sdir, "loop", files)
    set_status(sdir, stage="Compressing audio", done=False)
    try:
        encode_opus(me, others, os.path.join(folder, "audio.opus"), sdir)
    except StatusUnreadable:
        raise
    except Exception as e:
        mark_stage(sdir, "audio", "failed")
        log(f"opus encode failed for {sdir}: {e}; WAV chunks kept")
        return False
    set_status(sdir, audio_inputs=inputs, audio_ok=True)
    mark_stage(sdir, "audio", True)
    return True


def process(sdir, title=None, keep_wav=False, ledger=True):
    """Full run from the WAV chunks. Refuses (writes no meeting file, status or log) when the session is busy, already
    processed, already has a saved transcript or results in its folder (use --resume), has no WAV chunks, or all
    output folder names are taken. An earlier --keep-wav request is kept."""
    import uuid
    with session_lock(sdir):
        meta = json.load(open(os.path.join(sdir, "session.json"), encoding="utf-8"))
        st0 = read_status(sdir)
        if os.path.exists(os.path.join(sdir, "processed.flag")):
            raise Refused("this session is already processed; use --notes-only --force to rewrite only its notes")
        if (st0.get("stages") or {}).get("transcript"):
            raise Refused("this session already has a saved transcript; use --resume to finish only the missing steps")
        if st0.get("pending"):
            f0 = st0.get("folder")
            if not (f0 and owns_folder(sdir, f0)) or pending_matches(sdir, f0) or                     any(os.path.exists(os.path.join(f0, f)) for f in RESULT_FILES):
                raise Refused("this session has a saved or unclear result; use --resume, or check the folder")
            # else: a write that never reached the disk (e.g. blocked by a scanner): this full run supersedes it
        prev = st0.get("folder")
        if prev and owns_folder(sdir, prev) and any(os.path.exists(os.path.join(prev, f)) for f in RESULT_FILES):
            raise Refused("this session's folder already holds results; use --resume")
        files = wav_chunks(sdir)
        if not files:
            raise Refused("this session has no WAV chunks, so a full run has no source.")
        keep_wav = bool(keep_wav or st0.get("keep_wav"))
        title = title or meta.get("title") or "Meeting"
        t0 = from_ts(meta["t0"])
        uid = session_uid(sdir) or uuid.uuid4().hex
        folder = claim_folder(sdir, t0, title, uid)   # this session's own folder, or Refused before anything is written
        if any(os.path.exists(os.path.join(folder, f)) for f in RESULT_FILES):
            raise Refused("this session's folder already holds results; use --resume")
        inputs = chunk_manifest(files)
        set_status(sdir, uid=uid, stage="Loading audio", done=False, error=None, keep_wav=keep_wav, stages={}, pending={},
                   folder=folder, title=title, inputs=inputs)
        me, others = load_track(sdir, "mic", files), load_track(sdir, "loop", files)
        dur = max(len(me), len(others)) / SR
        t1 = t0 + dt.timedelta(seconds=dur)

        set_status(sdir, stage=f"Loading Whisper {FINAL_MODEL}")
        model = load_model(FINAL_MODEL)
        set_status(sdir, stage="Transcribing others")
        seg_o = transcribe_track(model, others, progress=lambda i, n: set_status(sdir, stage=f"Transcribing others {i}/{n}"))
        set_status(sdir, stage="Transcribing me")
        seg_m = transcribe_track(model, me, progress=lambda i, n: set_status(sdir, stage=f"Transcribing me {i}/{n}"))
        del model
        set_status(sdir, stage="Telling speakers apart")
        diar_note = ""
        try:
            turns = diarize(others) if seg_o else None
            if turns is None and seg_o:
                diar_note = "Speaker separation of Others was not available; all are labelled Others."
            assign_speakers(seg_o, turns)
        except Exception as e:
            diar_note = f"Speaker separation failed ({e}); all are labelled Others."
        n_me_raw = len(seg_m)
        seg_m = drop_echo(seg_m, seg_o)
        paras, langs, speakers = transcript_rows(seg_m, seg_o)
        write_atomic(os.path.join(sdir, "segments.json"),
                     json.dumps({"me": seg_m, "others": seg_o, "dur": dur}, ensure_ascii=False, indent=0))

        head = [f"# {title}", "",
                f"- Date: {t0:%Y-%m-%d %H:%M}–{t1:%H:%M} {TZ_LABEL} ({hms(dur)})",
                f"- Speakers: {', '.join(speakers) if speakers else '-'} (Me = microphone, others = computer audio)",
                f"- Languages: {', '.join(f'{k} {v * 100 // max(1, sum(langs.values()))}%' for k, v in sorted(langs.items(), key=lambda x: -x[1])) or '-'}",
                f"- Recognition: Whisper {FINAL_MODEL}, local {'GPU' if S['device'] == 'cuda' else S['device']}; "
                "nothing left the PC for transcription.",
                ]
        if n_me_raw != len(seg_m):
            head.append(f"- {n_me_raw - len(seg_m)} microphone line(s) that only repeated the speakers (echo) were removed.")
        if diar_note:
            head.append(f"- {diar_note}")
        body = [f"**[{hms(p['start'])}] {p['who']}:** {p['text']}" for p in paras] or ["_(no speech detected)_"]
        commit_result(sdir, os.path.join(folder, "transcript.md"),
                      "\n".join(head) + "\n\n---\n\n" + "\n\n".join(body) + "\n", "transcript", True)

        set_status(sdir, stage="Compressing audio")
        try:
            encode_opus(me, others, os.path.join(folder, "audio.opus"), sdir)
            set_status(sdir, audio_inputs=inputs, audio_ok=True)
            mark_stage(sdir, "audio", True)
        except StatusUnreadable:
            raise
        except Exception as e:
            mark_stage(sdir, "audio", "failed")
            log(f"opus encode failed for {sdir}: {e}; WAV chunks kept, retry with --audio-only")

        nat = None
        if S["native_minutes"]:
            set_status(sdir, stage="Checking Tencent / Feishu minutes")
            nat = native_minutes(t0, t1)
        set_status(sdir, native=nat, dur=dur)
        write_notes(sdir, meta, title, folder, t0, t1, dur, paras, langs, speakers, audio_saved(sdir), nat, ledger)
        return folder


def saved_folder(sdir):
    folder = read_status(sdir).get("folder")
    if not folder or not owns_folder(sdir, folder):
        raise Refused("this session has no output folder it owns (older sessions are read-only)")
    reconcile(sdir, folder)
    if not os.path.exists(os.path.join(folder, "transcript.md")):
        raise Refused("the meeting folder has no transcript.md")
    return folder


def _notes_from_saved(sdir, folder, title, ledger):
    meta = json.load(open(os.path.join(sdir, "session.json"), encoding="utf-8"))
    st = read_status(sdir)
    segs = json.load(open(os.path.join(sdir, "segments.json"), encoding="utf-8"))
    title = title or st.get("title") or meta.get("title") or "Meeting"
    t0 = from_ts(meta["t0"])
    dur = float(st.get("dur") or segs.get("dur") or 0.0)
    t1 = t0 + dt.timedelta(seconds=dur)
    paras, langs, speakers = transcript_rows(segs.get("me", []), segs.get("others", []))
    set_status(sdir, done=False, error=None)
    return write_notes(sdir, meta, title, folder, t0, t1, dur, paras, langs, speakers, audio_saved(sdir),
                       st.get("native"), ledger)


def retry_notes(sdir, title=None, ledger=True, force=False):
    """Notes-only retry from the saved segments.json: transcript.md and audio.opus are not touched, and an existing
    notes.md is replaced only by notes that were generated successfully. Refuses a session that is busy, already
    processed (unless force), has no saved transcript, or has no folder it owns."""
    with session_lock(sdir):
        if os.path.exists(os.path.join(sdir, "processed.flag")) and not force:
            raise Refused("this session is already processed; add --force to rewrite its notes")
        folder = saved_folder(sdir)
        if not (read_status(sdir).get("stages") or {}).get("transcript") or \
                not os.path.exists(os.path.join(sdir, "segments.json")):
            raise Refused("no saved transcript (segments.json) for a notes-only retry")
        return _notes_from_saved(sdir, folder, title, ledger)


def retry_audio(sdir):
    """Audio-only retry: encode audio.opus again from the WAV chunks (after a failed audio stage). Nothing else is
    touched; when the notes are already done the recorded input chunks are then cleaned up."""
    with session_lock(sdir):
        if audio_saved(sdir):
            raise Refused("this session's audio is already saved")
        if not wav_chunks(sdir):
            raise Refused("this session has no WAV chunks to encode")
        folder = saved_folder(sdir)
        if not (read_status(sdir).get("stages") or {}).get("transcript"):
            raise Refused("this session has no finished transcript stage; run a full processing instead")
        if not encode_saved(sdir, folder):
            set_status(sdir, stage="Audio failed: WAV chunks kept", done=True)
            return False
        set_status(sdir, stage="Audio saved", done=True)
        cleanup_wavs(sdir)
        return True


def resume(sdir, ledger=True):
    """Finish a run that stopped part-way, without redoing any finished stage: adopt results written just before a
    crash, encode the audio if it is not saved yet (from the WAV chunks), write the notes if they are not done, then
    the completion flag and the guarded clean-up. --keep-wav and existing notes are kept. Refuses a session that is
    busy, finished, or without a saved transcript in a folder it owns. Returns True when notes and audio are saved."""
    with session_lock(sdir):
        if os.path.exists(os.path.join(sdir, "processed.flag")):
            raise Refused("this session is already processed")
        folder = saved_folder(sdir)                       # also reconciles results written just before a crash
        st = read_status(sdir)
        if not (st.get("stages") or {}).get("transcript") or not os.path.exists(os.path.join(sdir, "segments.json")):
            raise Refused("this session has no saved transcript; run a full processing instead")
        if not audio_saved(sdir) and wav_chunks(sdir):
            encode_saved(sdir, folder)
        stages = read_status(sdir).get("stages") or {}
        if stages.get("notes") != "done":
            _notes_from_saved(sdir, folder, None, ledger)  # finalizes only when the notes succeed
        else:
            meta = json.load(open(os.path.join(sdir, "session.json"), encoding="utf-8"))
            title = st.get("title") or meta.get("title") or "Meeting"
            finalize(sdir, meta, title, folder, int(st.get("actions") or 0), ledger)
        return (read_status(sdir).get("stages") or {}).get("notes") == "done" and audio_saved(sdir)


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print(__doc__)
        return 2
    sdir = args[0]
    title = args[args.index("--title") + 1] if "--title" in args else None
    ledger = "--no-ledger" not in args
    try:
        # The whole command, its result check and any error-state update run under ONE hold of the session lock
        # (the operations re-enter it), so a failed command can never write its old status over a newer command's.
        with session_lock(sdir):
            try:
                if "--notes-only" in args:
                    return EXIT_OK if retry_notes(sdir, title, ledger, force="--force" in args) else EXIT_FAILED
                if "--audio-only" in args:
                    return EXIT_OK if retry_audio(sdir) else EXIT_FAILED
                if "--resume" in args:
                    return EXIT_OK if resume(sdir, ledger) else EXIT_FAILED
                process(sdir, title, keep_wav="--keep-wav" in args, ledger=ledger)
                ok = (read_status(sdir).get("stages") or {}).get("notes") == "done" and audio_saved(sdir)
                return EXIT_OK if ok else EXIT_FAILED     # a full run succeeds only with notes AND saved audio
            except Refused as e:
                print(f"refused for {sdir}: {e}", file=sys.stderr)   # no meeting file, status or log is written
                return EXIT_REFUSED
            except StatusUnreadable as e:                             # never rewrite a status file that could not be read
                log(f"stopped for {sdir}: {e}")
                print(f"stopped for {sdir}: {e}", file=sys.stderr)
                return EXIT_FAILED
            except Exception as e:
                log(f"processing failed for {sdir}: {traceback.format_exc()}")
                try:
                    set_status(sdir, stage="Failed: " + str(e)[:120], done=True, error=str(e))
                except StatusUnreadable:
                    pass
                return EXIT_FAILED
    except Refused as e:                                      # the session is busy (or its folder is missing)
        print(f"refused for {sdir}: {e}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
