import os, gc, re, sys, json, time, shutil, subprocess, tempfile, unicodedata
import torch, numpy as np, soundfile as sf
import whisper
import gradio as gr
import access_log
import scripture


def _patch_gradio_file_retry():
    """Windows security scanners briefly lock freshly uploaded big videos, so the preview fails with
    'PermissionError' (shown as 'Error' in the video box). Retry opening for a few seconds instead."""
    import asyncio, types
    import gradio.ranged_response as rr

    real = rr.aiofiles

    class _RetryOpen:
        def __init__(self, *a, **k):
            self.a, self.k, self.cm = a, k, None

        async def __aenter__(self):
            for i in range(20):
                try:
                    self.cm = real.open(*self.a, **self.k)
                    return await self.cm.__aenter__()
                except PermissionError:
                    if i == 19:
                        raise
                    await asyncio.sleep(0.5)

        async def __aexit__(self, *exc):
            return await self.cm.__aexit__(*exc)

    rr.aiofiles = types.SimpleNamespace(open=_RetryOpen)

    # Starlette's own file route (used for video previews) needs the same retry
    import anyio
    import starlette.responses as sr
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open_retry(self):
        for i in range(40):
            try:
                file = await anyio.open_file(self.path, mode="rb")
                break
            except PermissionError:
                if i == 39:
                    raise
                await asyncio.sleep(0.5)
        try:
            yield file
        finally:
            with anyio.CancelScope(shield=True):
                await file.aclose()

    sr.FileResponse._open_file = _open_retry


try:
    _patch_gradio_file_retry()
except Exception as _e:  # never block the app over this
    print("note: file-retry patch not applied:", _e)
from pydub import AudioSegment
from pydub.silence import detect_leading_silence
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
try:
    from IndicTransToolkit.processor import IndicProcessor
except ImportError:
    from IndicTransToolkit import IndicProcessor

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# All 22 scheduled Indian languages + English (IndicTrans2 / FLORES codes)
LANGS = {
    "Assamese": "asm_Beng", "Bengali": "ben_Beng", "Bodo": "brx_Deva", "Dogri": "doi_Deva",
    "English": "eng_Latn", "Gujarati": "guj_Gujr", "Hindi": "hin_Deva", "Kannada": "kan_Knda",
    "Kashmiri": "kas_Arab", "Konkani": "gom_Deva", "Maithili": "mai_Deva", "Malayalam": "mal_Mlym",
    "Manipuri": "mni_Beng", "Marathi": "mar_Deva", "Nepali": "npi_Deva", "Odia": "ory_Orya",
    "Punjabi": "pan_Guru", "Sanskrit": "san_Deva", "Santali": "sat_Olck", "Sindhi": "snd_Arab",
    "Tamil": "tam_Taml", "Telugu": "tel_Telu", "Urdu": "urd_Arab",
}
CODE_TO_NAME = {v: k for k, v in LANGS.items()}

# Languages Indic Parler-TTS can speak (conservative list - check the model card before adding
# more). Others still get translated subtitles.
TTS_LANGS = {"Assamese", "Bengali", "Bodo", "Dogri", "English", "Gujarati", "Hindi", "Kannada",
             "Konkani", "Maithili", "Malayalam", "Manipuri", "Marathi", "Nepali", "Odia",
             "Punjabi", "Sanskrit", "Santali", "Tamil", "Telugu"}

# Spoken languages Whisper can recognise in the uploaded video
WHISPER_TO_FLORES = {
    "en": "eng_Latn", "hi": "hin_Deva", "bn": "ben_Beng", "ta": "tam_Taml", "te": "tel_Telu",
    "kn": "kan_Knda", "ml": "mal_Mlym", "mr": "mar_Deva", "gu": "guj_Gujr", "pa": "pan_Guru",
    "ur": "urd_Arab", "as": "asm_Beng", "ne": "npi_Deva", "sa": "san_Deva", "sd": "snd_Arab",
}
FLORES_TO_WHISPER = {v: k for k, v in WHISPER_TO_FLORES.items()}

# Indic Parler-TTS has named speakers per language; using the same name for every line keeps
# the voice consistent (names from the model card). (female, male). Languages without their own
# speaker (Konkani, Maithili, Santali) use the Hindi voices.
SPEAKERS = {
    "Assamese": ("Sita", "Amit"), "Bengali": ("Aditi", "Arjun"), "Bodo": ("Maya", "Bikram"),
    "Dogri": ("Karan", "Karan"), "English": ("Mary", "Thoma"), "Gujarati": ("Neha", "Yash"),
    "Hindi": ("Divya", "Rohit"), "Kannada": ("Anu", "Suresh"), "Malayalam": ("Anjali", "Harish"),
    "Manipuri": ("Laishram", "Laishram"), "Marathi": ("Sunita", "Sanjay"),
    "Nepali": ("Amrita", "Amrita"), "Odia": ("Debjani", "Manas"),
    "Punjabi": ("Divjot", "Gurpreet"), "Sanskrit": ("Aryan", "Aryan"),
    "Tamil": ("Jaya", "Jaya"), "Telugu": ("Lalitha", "Prakash"),
}
VOICES = ["Female", "Male"]


def voice_description(lang, voice):
    f, m = SPEAKERS.get(lang, SPEAKERS["Hindi"])
    name = f if voice == "Female" else m
    return (f"{name}'s voice is monotone yet slightly fast in delivery, with a very clear audio "
            "and a very close recording that almost has no background noise.")


def run(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-1500:])


def has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def purge_old_runs(max_age_sec=3600):
    """Delete leftover work folders from earlier runs."""
    root = tempfile.gettempdir()
    for d in os.listdir(root):
        full = os.path.join(root, d)
        if d.startswith("ivt_") and os.path.isdir(full) and time.time() - os.path.getmtime(full) > max_age_sec:
            shutil.rmtree(full, ignore_errors=True)


def free():
    gc.collect()
    if DEVICE == "cuda":
        torch.cuda.empty_cache()


_cache = {}  # models kept loaded between videos


# ---------- 1. Speech to text (with timestamps) ----------
def transcribe(audio_path, src_choice, words=False):
    if "whisper" not in _cache:  # stays loaded between videos (saves reload time)
        _cache["whisper"] = whisper.load_model("turbo", device=DEVICE)
    model = _cache["whisper"]
    lang = None if src_choice == "Auto-detect" else FLORES_TO_WHISPER[LANGS[src_choice]]
    result = model.transcribe(audio_path, language=lang, task="transcribe",
                              fp16=(DEVICE == "cuda"), word_timestamps=bool(words))
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
             "words": [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in s.get("words", [])]}
            for s in result["segments"] if s["text"].strip()]
    detected = result.get("language") or lang
    free()
    return segs, detected


# ---------- 2. Translation (IndicTrans2) ----------
def pick_model(src, tgt):
    if src == "eng_Latn":
        return "ai4bharat/indictrans2-en-indic-dist-200M"
    if tgt == "eng_Latn":
        return "ai4bharat/indictrans2-indic-en-dist-200M"
    return "ai4bharat/indictrans2-indic-indic-dist-320M"


def translate(texts, src, tgt, batch_size=16):
    if src == tgt:
        return list(texts)
    name = pick_model(src, tgt)
    if name not in _cache:  # keep only the most recently used translator loaded
        for k in [k for k in _cache if k.startswith("ai4bharat/")]:
            del _cache[k]
        free()
        _cache[name] = (AutoTokenizer.from_pretrained(name, trust_remote_code=True),
                        AutoModelForSeq2SeqLM.from_pretrained(name, trust_remote_code=True).to(DEVICE).eval())
    tok, model = _cache[name]
    ip = IndicProcessor(inference=True)
    out = []
    for i in range(0, len(texts), batch_size):
        batch = ip.preprocess_batch(texts[i:i + batch_size], src_lang=src, tgt_lang=tgt)
        enc = tok(batch, truncation=True, padding="longest", return_tensors="pt",
                  return_attention_mask=True).to(DEVICE)
        with torch.no_grad():
            gen = model.generate(**enc, use_cache=False, min_length=0, max_length=256,
                                 num_beams=4, num_return_sequences=1)
        dec = tok.batch_decode(gen, skip_special_tokens=True, clean_up_tokenization_spaces=True)
        out += ip.postprocess_batch(dec, lang=tgt)
    free()
    return out


# ---------- 3. Subtitles ----------
def ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segs, texts, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (s, t) in enumerate(zip(segs, texts), 1):
            f.write(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{t}\n\n")


# ---------- 3b. Word-by-word "reels" captions (.ass) ----------
STYLE_REELS = "Word-by-word (reels style)"
STYLE_CLASSIC = "Full sentence (classic)"
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
# target language -> (font family inside the .ttf, .ttf file in the fonts folder, bold flag)
_NOTO = {"Telugu": "Telugu", "Kannada": "Kannada", "Tamil": "Tamil", "Malayalam": "Malayalam",
         "Gujarati": "Gujarati", "Punjabi": "Gurmukhi", "Odia": "Oriya", "Santali": "OlChiki",
         **{k: "Devanagari" for k in ("Hindi", "Marathi", "Nepali", "Sanskrit", "Konkani", "Maithili", "Dogri", "Bodo")},
         **{k: "Bengali" for k in ("Bengali", "Assamese", "Manipuri")},
         **{k: "Arabic" for k in ("Urdu", "Kashmiri", "Sindhi")}}


def caption_font(lang):
    """(family, file name, bold) for the target language: Noto Sans Bold per script, Nunito Black for English."""
    script = _NOTO.get(lang)
    if script is None:
        return "Nunito Black", "Nunito-Black.ttf", 0
    fam = {"OlChiki": "Ol Chiki"}.get(script, script)
    return f"Noto Sans {fam}", f"NotoSans{script}-Bold.ttf", -1


def clean_words(text):
    """Lowercase words without punctuation (letters, combining marks, digits and ZWJ/ZWNJ are kept)."""
    text = re.sub(r"[-‐-―/_]", " ", text or "")
    out = []
    for tok in text.split():
        w = "".join(c for c in tok if unicodedata.category(c)[0] in "LMN" or c in "‌‍").lower()
        if w:
            out.append(w)
    return out


def spread_words(start, end, words, hard_end=None, min_len=0.25):
    """Give each word a time slice of the segment, in proportion to its length (min 0.25 s each)."""
    if not words:
        return []
    chars = [len(w) for w in words]
    avail = max(end - start, 0.05)
    need = len(words) * min_len
    if need > avail and hard_end is not None:  # borrow from the gap before the next line
        avail = min(need, max(hard_end - start, avail))
    d = [max(avail * c / sum(chars), min_len) for c in chars]
    if sum(d) > avail:  # still too long: shrink evenly rather than overlap the next line
        k = avail / sum(d)
        d = [x * k for x in d]
    t, ev = start, []
    for w, x in zip(words, d):
        ev.append((t, t + x, w))
        t += x
    return ev


def _ass_time(t):
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass_words(events, path, width, height, lang):
    """One big current word (about 9% of the video height, at 70% height) with the previous word
    small above it. Sizes come from the real video size, so every resolution/aspect looks the same."""
    fam, _, bold = caption_font(lang)
    big = 0.09 * height
    small_k = 0.35
    cx, cy = width / 2, 0.70 * height

    def fit(chars, size):  # keep long words inside ~90% of the width (vertical videos)
        return min(size, 0.9 * width / (max(chars, 1) * 0.62))

    def style(name, size):
        return (f"Style: {name},{fam},{size:.0f},&H00FFFFFF,&H00FFFFFF,&H00181818,&H96000000,{bold},0,0,0,"
                f"100,100,0,0,1,{size * 0.045:.1f},{size * 0.04:.1f},5,10,10,10,1")
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
             "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
             "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
             "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             style("Current", big), style("Previous", big * small_k), "",
             "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    prev = None
    for i, (s, e, w) in enumerate(events):
        nxt = events[i + 1][0] if i + 1 < len(events) else None
        if nxt is not None and 0 <= nxt - e < 0.35:
            e = nxt  # no flicker between neighbouring words
        e = max(e, s + 0.12)
        size = fit(len(w), big)
        lines.append(f"Dialogue: 1,{_ass_time(s)},{_ass_time(e)},Current,,0,0,0,,"
                     f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\fs{size:.0f}\\fscx90\\fscy90"
                     f"\\t(0,100,\\fscx100\\fscy100)}}{w}")
        if prev and s - prev[1] < 1.0:  # previous word as context, only if it was spoken just before
            psize = fit(len(prev[2]), big * small_k)
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Previous,,0,0,0,,"
                         f"{{\\an5\\pos({cx:.0f},{cy - 0.8 * big:.0f})\\fs{psize:.0f}}}{prev[2]}")
        prev = (s, e, w)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")


# ---------- 4. Dubbed voice (Indic Parler-TTS) ----------
_tts = {}


def load_tts():
    if not _tts:
        name = "ai4bharat/indic-parler-tts"
        from parler_tts import ParlerTTSForConditionalGeneration  # lazy: needs torchaudio
        m = ParlerTTSForConditionalGeneration.from_pretrained(name).to(DEVICE)
        _tts["model"] = m
        _tts["tok"] = AutoTokenizer.from_pretrained(name)
        _tts["desc_tok"] = AutoTokenizer.from_pretrained(m.config.text_encoder._name_or_path)
    return _tts


def speak(text, voice, out_path, lang="Hindi"):
    t = load_tts()
    d = t["desc_tok"](voice_description(lang, voice), return_tensors="pt").to(DEVICE)
    p = t["tok"](text, return_tensors="pt").to(DEVICE)
    torch.manual_seed(42)  # keeps the voice consistent across lines
    with torch.no_grad():
        g = t["model"].generate(input_ids=d.input_ids, attention_mask=d.attention_mask,
                                prompt_input_ids=p.input_ids,
                                prompt_attention_mask=p.attention_mask)
    audio = g.cpu().numpy().squeeze().astype(np.float32)
    sf.write(out_path, audio, t["model"].config.sampling_rate)


# ---------- 4b. Voice cloning (IndicF5): speak in the original speaker's voice ----------
CLONE_LANGS = {"Assamese", "Bengali", "Gujarati", "Hindi", "Kannada", "Malayalam", "Marathi", "Odia",
               "Punjabi", "Tamil", "Telugu"}


def load_clone():
    if "indicf5" not in _cache:
        from transformers import AutoModel
        m = AutoModel.from_pretrained("ai4bharat/IndicF5", trust_remote_code=True)
        _cache["indicf5"] = m.to(DEVICE) if DEVICE == "cuda" else m
    return _cache["indicf5"]


def pick_reference(segs, orig_wav, out_wav, lo=4.0, hi=11.0):
    """Choose the speaker's voice sample: a run of neighbouring Whisper segments (4-11 s, the longest
    that fits) and its transcript. Returns the transcript text, or None if there is no usable speech."""
    best = None  # (duration, start, end, text)
    for i in range(len(segs)):
        j = i
        while j + 1 < len(segs) and segs[j + 1]["end"] - segs[i]["start"] <= hi \
                and segs[j + 1]["start"] - segs[j]["end"] < 0.6:
            j += 1  # extend over neighbouring lines while the sample stays within hi seconds
        dur = segs[j]["end"] - segs[i]["start"]
        if lo * 0.5 <= dur <= hi and (best is None or dur > best[0]):
            best = (dur, segs[i]["start"], segs[j]["end"], " ".join(s["text"] for s in segs[i:j + 1]))
    if best is None:  # every line is too long or too short: use the first hi seconds of the longest
        s = max(segs, key=lambda x: x["end"] - x["start"])
        dur = s["end"] - s["start"]
        if dur < 1.5:
            return None
        words = s["text"].split()  # keep only the words that fall inside the first hi seconds
        best = (hi, s["start"], s["start"] + hi, " ".join(words[:max(1, int(len(words) * hi / dur))]))
    _, a, b, txt = best
    run(["ffmpeg", "-y", "-ss", f"{a:.2f}", "-t", f"{min(b - a, hi):.2f}", "-i", orig_wav,
         "-ac", "1", "-ar", "24000", out_wav])
    return txt


def speak_clone(text, ref_wav, ref_text, out_path):
    m = load_clone()
    torch.manual_seed(42)
    with torch.no_grad():
        a = m(text, ref_audio_path=ref_wav, ref_text=ref_text)
    a = np.asarray(a)
    if a.dtype == np.int16:
        a = a.astype(np.float32) / 32768.0
    sf.write(out_path, a.astype(np.float32).squeeze(), 24000)


def trim_silence(wav_path, out_path, thresh=-45):
    """Cut the silence TTS leaves at the start/end so the line starts exactly when we say."""
    a = AudioSegment.from_wav(wav_path)
    lead = detect_leading_silence(a, silence_threshold=thresh)
    trail = detect_leading_silence(a.reverse(), silence_threshold=thresh)
    a[lead:len(a) - trail].export(out_path, format="wav")


def fit_to_slot(wav_path, slot_sec, out_path, max_speed=1.6, min_speed=0.85):
    """Make a line last about as long as the original speaker's line (speed it up if too
    long, slow it slightly if too short) so voice and mouth movement line up."""
    dur = sf.info(wav_path).duration
    speed = 1.0
    if slot_sec > 0.3:
        speed = min(max(dur / slot_sec, min_speed), max_speed)
    run(["ffmpeg", "-y", "-i", wav_path, "-filter:a", f"atempo={speed:.3f}",
         "-ar", "44100", "-ac", "1", out_path])


def build_dub_track(segs, texts, voice, total_ms, tmp, progress, lang="Hindi", clone=None):
    """clone = (reference wav, reference transcript) to speak in the original voice, else the preset voice."""
    track = AudioSegment.silent(duration=total_ms, frame_rate=44100)
    n = len(segs)
    busy_until = 0  # ms; a line never starts before the previous one has finished
    for i, (s, txt) in enumerate(zip(segs, texts)):
        progress(0.55 + 0.3 * i / n, desc=f"Generating voice {i + 1}/{n}")
        if not txt.strip():
            continue
        raw, trim, fit = f"{tmp}/seg{i}.wav", f"{tmp}/trim{i}.wav", f"{tmp}/fit{i}.wav"
        if clone:
            speak_clone(txt, clone[0], clone[1], raw)
        else:
            speak(txt, voice, raw, lang)
        trim_silence(raw, trim)
        # Aim for the speaker's own mouth-movement time; allow a little spill into the gap
        nxt = segs[i + 1]["start"] if i + 1 < n else s["end"] + 1.0
        slot_end = max(s["end"], min(nxt - 0.1, s["end"] + 0.4))
        fit_to_slot(trim, slot_end - s["start"], fit)
        clip = AudioSegment.from_wav(fit)
        pos = max(int(s["start"] * 1000), busy_until)
        track = track.overlay(clip, position=pos)
        busy_until = pos + len(clip) + 80
    return track


# ---------- Full pipeline ----------
def video_size(path):
    """Real displayed size (phone videos stored rotated are swapped, as ffmpeg autorotates them)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:stream_side_data=rotation",
                        "-of", "json", path], capture_output=True, text=True)
    st = json.loads(r.stdout)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    rot = next((abs(int(float(d["rotation"]))) for d in st.get("side_data_list", []) if "rotation" in d), 0)
    return (h, w) if rot % 180 == 90 else (w, h)


_YT_RE = re.compile(r"^https?://((www|m|music)\.)?(youtube\.com|youtu\.be)/", re.I)


def download_youtube(url, progress=None):
    """Download a YouTube link (single video, up to 1080p) with yt-dlp; returns the .mp4 path."""
    url = (url or "").strip()
    if not _YT_RE.match(url):
        raise gr.Error("Paste a full YouTube link, e.g. https://www.youtube.com/watch?v=...")
    import yt_dlp
    d = tempfile.mkdtemp(prefix="ivt_yt_")  # purge_old_runs() removes it after an hour
    opts = {"outtmpl": os.path.join(d, "video.%(ext)s"), "noplaylist": True, "quiet": True,
            "no_warnings": True, "restrictfilenames": True, "merge_output_format": "mp4",
            "socket_timeout": 30,
            "format": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"}
    if progress:
        progress(0.02, desc="Downloading from YouTube")
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.extract_info(url, download=True)
    except Exception as e:
        raise gr.Error(f"Could not download that YouTube video: {str(e)[:250]}")
    files = [f for f in os.listdir(d) if f.startswith("video.") and not f.endswith((".part", ".ytdl"))]
    if not files:
        raise gr.Error("The YouTube download produced no video file.")
    return os.path.join(d, files[0])


def process(video, src_choice, tgt_name, voice, do_dub, keep_bg, burn,
            clip_start=0, clip_len=0, res="Original resolution", sub_style=STYLE_REELS,
            yt_url="", clone_voice=False, use_bible=True, token=None, progress=gr.Progress()):
    """Checks the verification code, runs one translation and records it in the Excel log."""
    if (yt_url or "").strip():  # a YouTube link wins over an uploaded file
        video = download_youtube(yt_url, progress)
    if not video:
        raise gr.Error("Upload a video or paste a YouTube link first.")
    if not has_audio(video):
        raise gr.Error("This video has no audio track to translate.")
    person = access_log.consume(token)
    if person is None:
        raise gr.Error("Verify first: fill in 'Who is translating', get the code and "
                       "enter it. A new code is needed for every video.")
    vinfo, t0 = access_log.video_info(video), time.time()
    opts = dict(src=src_choice, tgt=tgt_name, voice=voice, dub=do_dub, start=clip_start,
                len=clip_len, res=res)
    try:
        result = _translate_impl(video, src_choice, tgt_name, voice, do_dub, keep_bg, burn,
                                 clip_start, clip_len, res, progress, sub_style, clone_voice, use_bible)
    except Exception as e:
        access_log.write_record(person, vinfo, opts, "Failed", time.time() - t0, str(e)[:200])
        raise
    out, srt, dub_wav, status = result
    note = re.sub(r"[*`]", "", str(status))[:200]
    saved = access_log.write_record(person, vinfo, opts, "Done", time.time() - t0, note)
    where = ("records/translation_log.xlsx" if saved == access_log.XLSX else
             "records/translation_log_pending.csv (close the Excel file so it can be merged)")
    return out, srt, dub_wav, f"{status}\n\nRecorded for **{person['name']}** in `{where}`."


_nvenc_ok = True


def _translate_impl(video, src_choice, tgt_name, voice, do_dub, keep_bg, burn,
                    clip_start=0, clip_len=0, res="Original resolution", progress=None,
                    sub_style=STYLE_REELS, clone_voice=False, use_bible=True):
    global _nvenc_ok
    if not video:
        raise gr.Error("Upload a video first.")
    if not has_audio(video):
        raise gr.Error("This video has no audio track to translate.")
    purge_old_runs()
    tmp = tempfile.mkdtemp(prefix="ivt_")
    orig = f"{tmp}/orig.wav"

    progress(0.05, desc="Extracting audio")
    clip_start, clip_len = max(float(clip_start or 0), 0), max(float(clip_len or 0), 0)
    # Optional clip: only process [clip_start, clip_start + clip_len]; 0 length = whole video
    trim = []
    if clip_start > 0:
        trim += ["-ss", f"{clip_start:.3f}"]
    if clip_len > 0:
        trim += ["-t", f"{clip_len:.3f}"]
    run(["ffmpeg", "-y", *trim, "-i", video, "-vn", "-ac", "2", "-ar", "44100", orig])

    progress(0.15, desc="Transcribing speech")
    reels = bool(burn) and str(sub_style).startswith("Word")
    # Whisper word timings only help when the target language is the spoken language
    segs, detected = transcribe(orig, src_choice, words=reels and src_choice in ("Auto-detect", tgt_name))
    if not segs:
        raise gr.Error("No speech found in this video.")
    src = WHISPER_TO_FLORES.get(detected)
    if src is None:
        raise gr.Error(f"Detected language '{detected}' isn't supported as a source. "
                       "Choose the spoken language manually.")

    tgt = LANGS[tgt_name]
    progress(0.4, desc=f"Translating to {tgt_name}")
    scripture_used = []
    if use_bible and tgt_name == "Telugu" and src == "eng_Latn" and scripture.available():
        # Scripture the speaker names or quotes: use the Telugu Bible (IRV) wording and book names
        segs, sinfo = scripture.prepare(segs)
        texts, scripture_used = scripture.translate_segments(segs, sinfo, translate, src, tgt)
    else:
        texts = translate([s["text"] for s in segs], src, tgt)

    srt_name = f"subtitles_{tgt_name}.srt"
    srt = os.path.join(tmp, srt_name)
    write_srt(segs, texts, srt)  # the .srt download is always the normal sentence subtitles

    sub_file = srt_name
    if reels:
        events = []
        for i, (s, t) in enumerate(zip(segs, texts)):
            if src == tgt and s.get("words"):  # same language: Whisper's real word timings
                events += [(w["start"], w["end"], c) for w in s["words"] for c in clean_words(w["word"])]
            else:  # translated: spread the new words over the segment by character length
                hard = segs[i + 1]["start"] if i + 1 < len(segs) else s["end"] + 1.0
                events += spread_words(s["start"], s["end"], clean_words(t), hard)
        W, H = video_size(os.path.abspath(video))
        sub_file = f"subtitles_{tgt_name}_words.ass"
        write_ass_words(events, os.path.join(tmp, sub_file), W, H, tgt_name)
        _, font_file, _ = caption_font(tgt_name)
        os.makedirs(os.path.join(tmp, "fonts"), exist_ok=True)
        for ff in {font_file, "Nunito-Black.ttf"}:
            if os.path.exists(os.path.join(FONT_DIR, ff)):
                shutil.copy(os.path.join(FONT_DIR, ff), os.path.join(tmp, "fonts", ff))

    dub_wav, note = None, ""
    if do_dub:
        if tgt_name in TTS_LANGS:
            try:
                total_ms = len(AudioSegment.from_wav(orig))
                clone = None
                if clone_voice:
                    ref_wav = f"{tmp}/ref.wav"
                    ref_txt = pick_reference(segs, orig, ref_wav) if tgt_name in CLONE_LANGS else None
                    if ref_txt:
                        clone = (ref_wav, ref_txt)
                    elif tgt_name not in CLONE_LANGS:
                        note += f" Voice cloning isn't available for {tgt_name}; the preset voice was used."
                    else:
                        note += " No clear speech sample found for cloning; the preset voice was used."
                try:
                    track = build_dub_track(segs, texts, voice, total_ms, tmp, progress, tgt_name, clone)
                except Exception as e:
                    if not clone:
                        raise
                    free()  # cloning failed: redo the dub with the preset voice
                    note += f" Voice cloning failed ({type(e).__name__}), so the preset voice was used."
                    track = build_dub_track(segs, texts, voice, total_ms, tmp, progress, tgt_name)
                if clone and not note:
                    note = " Dubbed in the original speaker's voice."
                if keep_bg:
                    track = (AudioSegment.from_wav(orig) - 18).overlay(track)
                dub_wav = f"{tmp}/dub.wav"
                track.export(dub_wav, format="wav")
            except Exception as e:  # TTS failed: still deliver subtitles
                dub_wav = None
                note = f" Voice dubbing failed ({type(e).__name__}: {e}), so this video has subtitles only."
        else:
            note = f" Voice dubbing isn't available for {tgt_name}, so this video has subtitles only."

    progress(0.88, desc="Building final video")
    out = os.path.join(tmp, f"translated_{tgt_name}.mp4")
    video = os.path.abspath(video)
    cmd = ["ffmpeg", "-y", *trim, "-i", video]
    if dub_wav:
        cmd += ["-i", dub_wav]
    if not burn:
        cmd += ["-i", srt]
    cmd += ["-map", "0:v:0", "-map", "1:a:0" if dub_wav else "0:a?"]

    # Video filters: optional upscale to 4K (never downscale), then burned-in subtitles
    vf = []
    target = 3840 if res.startswith("4K") else 1920 if res.startswith("HD") else 0
    if target:
        w, h = video_size(video)
        k = target / max(w, h)
        # 4K only ever upscales; HD sets the long side to 1920 (up or down)
        if (k > 1 or target == 1920) and abs(k - 1) > 0.01:
            vf.append(f"scale={round(w * k / 2) * 2}:{round(h * k / 2) * 2}:flags=lanczos")
    if burn:
        # Relative name + cwd=tmp avoids ffmpeg's problems with Windows paths like C:\...
        vf.append(f"subtitles={sub_file}" + (":fontsdir=fonts" if reels else ""))
    if vf or trim:
        # Re-encode at high quality (near-lossless); a trimmed clip must be re-encoded.
        # NVENC (GPU) first, libx264 (CPU) if NVENC is not available.
        if vf:
            cmd += ["-vf", ",".join(vf)]
        encoders = [["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "16", "-b:v", "0",
                     "-pix_fmt", "yuv420p"],
                    ["-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p"]]
    else:
        encoders = [["-c:v", "copy"]]  # untouched video: original quality, very fast
    tail = []
    if not burn:
        tail += ["-map", f"{2 if dub_wav else 1}:s:0", "-c:s", "mov_text"]
    tail += ["-c:a", "aac", "-b:a", "192k", "-shortest", out]
    if not _nvenc_ok and len(encoders) > 1:
        encoders = encoders[1:]  # NVENC already failed on this PC (old driver): go straight to libx264
    for n, enc in enumerate(encoders):
        try:
            run(cmd + enc + tail, cwd=tmp)
            break
        except RuntimeError:
            if enc[1] == "h264_nvenc":
                _nvenc_ok = False
            if n == len(encoders) - 1:
                raise

    # Remove big intermediates; keep the final video and .srt for Gradio to serve
    for f in os.listdir(tmp):
        if f.endswith(".wav") and f != "dub.wav":
            os.remove(os.path.join(tmp, f))

    if scripture_used:
        note += (f" Telugu Bible used for: {', '.join(dict.fromkeys(scripture_used))}. "
                 f"Credit for your video description: {scripture.CREDIT}")
    status = (f"Done. Spoken language: **{CODE_TO_NAME.get(src, src)}** → "
              f"**{tgt_name}**, {len(segs)} subtitle lines.{note}")
    return out, srt, dub_wav, status


# ---------- App ----------
SRC_CHOICES = ["Auto-detect"] + sorted(n for n, c in LANGS.items() if c in FLORES_TO_WHISPER)

RAIN_JS = r"""
() => {
  document.body.classList.add('dark');
  if (document.getElementById('word-rain')) return;
  const words = [
    "story","voice","dream","light","create","hello","language","magic",
    "కథ","భాష","స్వప్నం","వెలుగు","నమస్కారం","ప్రేమ","తెలుగు",
    "கதை","மொழி","கனவு","ஒளி","வணக்கம்","அன்பு","தமிழ்",
    "कहानी","भाषा","सपना","रोशनी","नमस्ते","प्यार","हिन्दी",
    "ಕಥೆ","ಭಾಷೆ","ಕನಸು","ಬೆಳಕು","ನಮಸ್ಕಾರ","ಪ್ರೀತಿ","ಕನ್ನಡ",
    "കഥ","ഭാഷ","സ്വപ്നം","വെളിച്ചം","നമസ്കാരം","മലയാളം",
    "গল্প","ভাষা","স্বপ্ন","আলো","বাংলা","વાર્તા","ભાષા","ਕਹਾਣੀ","ਭਾਸ਼ਾ","कथा","मराठी"
  ];
  const colors = ["255,122,198","167,139,250","94,200,255","255,170,120"];
  const cv = document.createElement('canvas');
  cv.id = 'word-rain';
  cv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;';
  document.body.prepend(cv);
  const ctx = cv.getContext('2d');
  let W, H, drops = [];
  const font = '"Nirmala UI","Noto Sans Telugu","Noto Sans Tamil","Noto Sans Devanagari","Noto Sans Kannada","Segoe UI",sans-serif';
  function resize() {
    W = cv.width = window.innerWidth; H = cv.height = window.innerHeight;
    const n = Math.max(8, Math.floor(W / 65));
    drops = Array.from({length: n}, (_, i) => mk(i * (W / n) + Math.random() * 30, Math.random() * H));
    ctx.fillStyle = '#0b0b1a'; ctx.fillRect(0, 0, W, H);
  }
  function mk(x, y) {
    return {x, y, v: 0.15 + Math.random() * 0.35, size: 16 + Math.floor(Math.random() * 12),
            w: words[Math.floor(Math.random() * words.length)], c: colors[Math.floor(Math.random() * colors.length)],
            a: 0.40 + Math.random() * 0.45};
  }
  function frame() {
    ctx.fillStyle = 'rgba(11,11,26,0.35)'; ctx.fillRect(0, 0, W, H);
    for (const d of drops) {
      ctx.font = d.size + 'px ' + font;
      ctx.fillStyle = 'rgba(' + d.c + ',' + d.a + ')';
      ctx.fillText(d.w, d.x, d.y);
      d.y += d.v;
      if (d.y > H + 40) { Object.assign(d, mk(d.x, -20)); }
    }
    requestAnimationFrame(frame);
  }
  window.addEventListener('resize', resize);
  resize();
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) frame();
}
"""

THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.fuchsia, secondary_hue=gr.themes.colors.violet,
    neutral_hue=gr.themes.colors.slate, font=[gr.themes.GoogleFont("Poppins"), "system-ui", "sans-serif"],
    radius_size=gr.themes.sizes.radius_lg,
).set(
    body_background_fill="#0b0b1a", body_text_color="#e8e8f5",
    block_background_fill="rgba(255,255,255,0.045)", block_border_width="1px",
    block_border_color="rgba(255,255,255,0.10)", block_label_text_color="#c9b8ff",
    input_background_fill="rgba(255,255,255,0.07)", input_border_color="rgba(255,255,255,0.14)",
    button_primary_background_fill="linear-gradient(90deg,#ff3cac,#784ba0 55%,#2b86c5)",
    button_primary_background_fill_hover="linear-gradient(90deg,#ff5fbf,#8e5cb8 55%,#3c9ae0)",
    button_primary_text_color="white",
    button_secondary_background_fill="rgba(255,255,255,0.09)", button_secondary_text_color="#f0e8ff",
    body_background_fill_dark="#0b0b1a", body_text_color_dark="#e8e8f5",
    body_text_color_subdued="#b9b4d9", body_text_color_subdued_dark="#b9b4d9",
    block_background_fill_dark="rgba(255,255,255,0.045)", block_border_color_dark="rgba(255,255,255,0.10)",
    block_label_text_color_dark="#c9b8ff", block_title_text_color="#d9ccff",
    block_title_text_color_dark="#d9ccff", block_info_text_color="#b9b4d9",
    block_info_text_color_dark="#b9b4d9",
    input_background_fill_dark="rgba(255,255,255,0.07)", input_border_color_dark="rgba(255,255,255,0.14)",
    input_placeholder_color="#8d88b4", input_placeholder_color_dark="#8d88b4",
    checkbox_label_background_fill="rgba(255,255,255,0.07)", checkbox_label_background_fill_dark="rgba(255,255,255,0.07)",
    checkbox_label_text_color="#e8e8f5", checkbox_label_text_color_dark="#e8e8f5",
    checkbox_label_background_fill_selected="rgba(255,60,172,0.30)",
    checkbox_label_background_fill_selected_dark="rgba(255,60,172,0.30)",
    checkbox_label_text_color_selected="#ffffff", checkbox_label_text_color_selected_dark="#ffffff",
    button_secondary_background_fill_dark="rgba(255,255,255,0.09)", button_secondary_text_color_dark="#f0e8ff",
    button_primary_background_fill_dark="linear-gradient(90deg,#ff3cac,#784ba0 55%,#2b86c5)",
    button_primary_text_color_dark="white",
)

CSS = """
body {background: #0b0b1a !important;}
gradio-app {background: transparent !important; position: relative; z-index: 1; display: block;}
gradio-app .app, gradio-app .main, gradio-app .wrap.sidebar-parent, gradio-app > div {background: transparent !important;}
.gradio-container {background: transparent !important; position: relative; z-index: 1;
  max-width: 1100px !important; margin: auto;}
.gradio-container .block, .gradio-container .form {background: rgba(14,12,34,.80) !important;
  }
.hero {text-align:center; padding: 34px 10px 10px;}
.hero .logo {display:block; margin: 0 auto 10px; filter: drop-shadow(0 6px 18px rgba(167,139,250,.45));}
.hero h1 {font-size: 2.6rem; font-weight: 800; margin: 0; letter-spacing: -0.5px;
  background: linear-gradient(90deg,#ff7ac6,#a78bfa,#5ec8ff); -webkit-background-clip: text;
  background-clip: text; color: transparent;}
.hero p {color:#b9b4d9; font-size: 1.05rem; margin: 8px 0 14px;}
.chips span {display:inline-block; margin: 3px 4px; padding: 4px 12px; border-radius: 999px;
  font-size: .82rem; color:#e9defc; background: rgba(167,139,250,.16); border:1px solid rgba(167,139,250,.35);}
.tab-wrapper, .tab-container {border-bottom-color: rgba(255,255,255,.1) !important;}
button[role=tab] {font-weight: 600; font-size: 1.02rem;}
button[role=tab][aria-selected=true] {color:#ff7ac6 !important; border-color:#ff7ac6 !important;}
button.primary, button.lg {box-shadow: 0 6px 22px rgba(255,60,172,.30); transition: transform .15s, box-shadow .15s;}
button.primary:hover {transform: translateY(-2px); box-shadow: 0 10px 28px rgba(255,60,172,.45);}
.step {margin-top: 18px; padding: 4px 16px 14px; border-radius: 18px;
  background: rgba(255,255,255,.035); border: 1px solid rgba(255,255,255,.09);}
.step h3 {margin: 12px 0 4px; background: linear-gradient(90deg,#ff7ac6,#a78bfa);
  -webkit-background-clip: text; background-clip: text; color: transparent;}
.gradio-container, .gradio-container * {--body-text-color:#e8e8f5;}
.gradio-container label, .gradio-container span, .gradio-container p, .gradio-container li,
.gradio-container td, .gradio-container th {color:#e8e8f5;}
.gradio-container input, .gradio-container textarea, .gradio-container select {color:#ffffff !important;
  background: rgba(255,255,255,.08) !important;}
.gradio-container span[data-testid="block-info"], .gradio-container .block-info {color:#c9b8ff !important;}
.hero h1 {color: transparent !important;}
.chips span {color:#e9defc !important;}
.step h3 {color: transparent !important;}
ul.options, .options, ul[role=listbox] {z-index: 99999 !important; max-height: 320px !important; overflow-y: auto !important;
  background: #1a1633 !important; opacity: 1 !important; border: 1px solid rgba(167,139,250,.45) !important;
  box-shadow: 0 12px 32px rgba(0,0,0,.6) !important;}
ul.options li, .options li, [role=option] {background: #1a1633 !important; color: #f2ecff !important; font-weight: 500;}
ul.options li:hover, ul.options li.active, [role=option]:hover, [role=option][aria-selected=true] {
  background: linear-gradient(90deg,#ff3cac66,#784ba066) !important; color: #ffffff !important;}
ul.options li span, [role=option] span {color: inherit !important;}
.gradio-container input[type=checkbox], .gradio-container input[type=radio] {
  -webkit-appearance: none !important; appearance: none !important; flex: none;
  width: 24px !important; height: 24px !important; border-radius: 7px !important;
  border: 2px solid #b79cff !important; background: #0b0b1a !important; background-image: none !important;
  cursor: pointer; position: relative; box-shadow: none !important;}
.gradio-container input[type=checkbox]:checked, .gradio-container input[type=radio]:checked {
  background-color: #ff3cac !important; border-color: #ff3cac !important;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7'/%3E%3C/svg%3E") !important;
  background-size: 18px 18px !important; background-position: center !important; background-repeat: no-repeat !important;}
.gradio-container label.selected, .gradio-container label:has(input:checked) {
  background: rgba(255,60,172,.22) !important; border-color: #ff3cac !important;}
.gradio-container label:has(input[type=checkbox]) {gap: 10px; font-weight: 500;}
[data-testid="block-label"], .gradio-container .block-label, label.block-label, span.block-label {
  background: linear-gradient(90deg,#3b1f72,#5b2a86) !important; color: #f2ecff !important;
  border: 1px solid rgba(167,139,250,.45) !important; box-shadow: none !important;}
[data-testid="block-label"] *, .gradio-container .block-label * {color: #f2ecff !important; background: transparent !important;}
[data-testid="block-label"] svg, .gradio-container .block-label svg {stroke: #f2ecff !important; color: #f2ecff !important;}
.gradio-container .wrap-inner, .gradio-container .secondary-wrap {
  background: linear-gradient(135deg,#1c1740,#241a52) !important; border: 1.5px solid rgba(167,139,250,.55) !important;
  border-radius: 14px !important; box-shadow: 0 4px 16px rgba(120,75,160,.25) !important; padding: 2px 6px !important;
  transition: border-color .15s, box-shadow .15s;}
.gradio-container .wrap-inner:hover, .gradio-container .wrap-inner:focus-within {
  border-color: #ff3cac !important; box-shadow: 0 0 0 3px rgba(255,60,172,.20), 0 4px 16px rgba(255,60,172,.25) !important;}
.gradio-container .wrap-inner input, .gradio-container .secondary-wrap input {
  background: transparent !important; border: none !important; outline: none !important; box-shadow: none !important;
  font-size: 1.02rem !important; font-weight: 600 !important; color: #ffffff !important; padding: 10px 8px !important;}
.gradio-container .secondary-wrap, .gradio-container .secondary-wrap:hover, .gradio-container .secondary-wrap:focus-within,
.gradio-container .wrap-inner .wrap-inner, .gradio-container .wrap-inner .secondary-wrap {
  background: transparent !important; border: none !important; box-shadow: none !important; padding: 0 !important;}
.gradio-container .container > .wrap {background: transparent !important; border: none !important; box-shadow: none !important;}
.gradio-container input[role=listbox] {border: none !important; background: transparent !important; box-shadow: none !important;}
.gradio-container .wrap-inner svg, .gradio-container .secondary-wrap svg {color: #ff7ac6 !important; fill: #ff7ac6 !important;}
.hero .by {margin-top: 12px; font-size: .95rem; color:#d9ccff;}
.hero .by b {background: linear-gradient(90deg,#ff7ac6,#a78bfa,#5ec8ff); -webkit-background-clip: text;
  background-clip: text; color: transparent !important;}
.corner-credit {position: fixed; right: 14px; bottom: 10px; z-index: 5; pointer-events: none;
  font-size: .8rem; color: rgba(233,222,252,.55); background: transparent; letter-spacing: .3px;
  text-shadow: 0 1px 6px rgba(0,0,0,.6);}
.corner-credit b {color: rgba(255,122,198,.75) !important; font-weight: 600;}
.foot {text-align:center; color:#7f7aa6; font-size:.85rem; padding: 18px 0 6px;}
"""

# Whisper logo: speech bubble holding sound-wave bars (original artwork)
_LOGO_BODY = ('<defs><linearGradient id="wg" x1="0" y1="0" x2="1" y2="1">'
              '<stop offset="0" stop-color="#ff7ac6"/><stop offset=".5" stop-color="#a78bfa"/>'
              '<stop offset="1" stop-color="#5ec8ff"/></linearGradient></defs>'
              '<path d="M12 8h40a8 8 0 0 1 8 8v26a8 8 0 0 1-8 8H30l-12 10v-10h-6a8 8 0 0 1-8-8V16a8 8 0 0 1 8-8z" '
              'transform="translate(2 0)" fill="url(#wg)"/>'
              '<g stroke="#fff" stroke-width="4" stroke-linecap="round">'
              '<path d="M17 29v8"/><path d="M25 22v22"/><path d="M33 16v34"/>'
              '<path d="M41 22v22"/><path d="M49 29v8"/></g>')
LOGO_SVG = ('<svg class="logo" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 68 68" width="76" height="76" '
            'role="img" aria-label="Whisper logo">' + _LOGO_BODY + '</svg>')
FAVICON_LINK = ('<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,'
                + __import__("urllib.parse").parse.quote(
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 68 68">' + _LOGO_BODY + '</svg>')
                + '">')

with gr.Blocks(title="Whisper Video Translator") as demo:
    gr.HTML('<div class="hero">' + LOGO_SVG + '<h1>Whisper Video Translator</h1>'
            '<p>Translate, dub and create videos in 22 Indian languages. Free, private, on your own machine.</p>'
            '<div class="chips"><span>Whisper</span><span>IndicTrans2</span><span>Indic Parler-TTS</span>'
            '<span>FFmpeg</span><span>4K output</span></div></div>')
    with gr.Tabs():
        with gr.Tab("Video translation"):
            gr.Markdown("Upload a video, choose a language, and get subtitles plus a dubbed version.")
            video_in = gr.Video(label="Upload video")
            yt_in = gr.Textbox(label="Or paste a YouTube link (used instead of the uploaded video)",
                               placeholder="https://www.youtube.com/watch?v=...")
            with gr.Row():
                src_in = gr.Dropdown(SRC_CHOICES, value="Auto-detect", label="Language spoken in the video")
                tgt_in = gr.Dropdown(sorted(LANGS), value="Hindi", label="Translate into")
            voice_in = gr.State("Male")  # the dubbing voice is always Male
            dub_in = gr.Checkbox(value=True, label="Dub the audio")
            bg_in = gr.State(False)  # original sound is not kept in the background
            clone_in = gr.Checkbox(value=False, label="Clone the speaker's voice for the dub (experimental)",
                                   info="Hindi, Telugu, Tamil, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, "
                                        "Assamese. Uses a clear 4-11 s sample of the speaker from the video. Only clone "
                                        "voices you have permission to use. Slower than the preset voice.")
            bible_in = gr.Checkbox(value=True, label="Use the Telugu Bible (IRV) for scripture the speaker names or quotes",
                                   info="Telugu target and English speech only. Quoted verses and book names come from the "
                                        "Telugu Bible instead of machine translation. Needs 'python fetch_bible.py' once. "
                                        "CC BY-SA 4.0: credit is shown in the result.")
            burn_in = gr.Checkbox(value=True, label="Show subtitles on the video (burned in)")
            style_in = gr.Dropdown([STYLE_REELS, STYLE_CLASSIC], value=STYLE_REELS, label="Subtitle style",
                                   info="Reels: one big word at a time. The .srt download is always normal sentences.")
            with gr.Row():
                start_in = gr.Number(value=0, minimum=0, label="Start at (seconds)")
                len_in = gr.Number(value=0, minimum=0,
                                   label="Process only this many seconds (0 = full video, e.g. 10 or 13 to test)")
            res_in = gr.Radio(["Original resolution", "HD (1080p)", "4K HD (2160p)"], value="Original resolution",
                              label="Output quality (original = no quality loss; HD = 1920 px wide; 4K HD = 3840 px, upscaled, slower)")
            with gr.Column(elem_classes="step"):
                gr.Markdown("### Who is translating? (a code shown on this PC is needed for every video)")
                with gr.Row():
                    who_name = gr.Textbox(label="Your name", placeholder="e.g. Abhishek")
                    who_email = gr.Textbox(label="Your email (for the record only, nothing is sent)", placeholder="you@gmail.com")
                who_desc = gr.Textbox(label="Short description of the video", lines=2, max_lines=3,
                                      placeholder="e.g. Class lecture on photosynthesis, Telugu to English")
                gr.Examples(
                    examples=[["Class lecture on photosynthesis, Telugu to English, for school students"],
                              ["Product demo video for our new app, English to Hindi, 2 minutes"],
                              ["Family function wishes message, Hindi to Tamil, personal use"]],
                    inputs=[who_desc], label="Sample descriptions (click one to fill the box, then edit)")
                with gr.Row():
                    who_send = gr.Button("Next: show my code on this PC")
                    who_code = gr.Textbox(label="6-digit code", max_length=6)
                    who_verify = gr.Button("Verify code")
                who_status = gr.Markdown()
                who_token = gr.State(None)
            btn = gr.Button("Translate", variant="primary")
            video_out = gr.Video(label="Translated video")
            srt_out = gr.File(label="Subtitle file (.srt)")
            dub_out = gr.File(label="Dubbed audio (.wav) - use this for lip-sync, see LIPSYNC.md")
            status_out = gr.Markdown()
            def _send(token, name, email, desc):
                token, ok, msg = access_log.send_code(token, name, email, desc, "screen")
                return token, ("" if ok else "**Problem:** ") + msg

            def _verify(token, code):
                ok, msg = access_log.verify(token, code)
                return ("" if ok else "**Problem:** ") + msg

            who_send.click(_send, [who_token, who_name, who_email, who_desc], [who_token, who_status])
            who_verify.click(_verify, [who_token, who_code], [who_status])
            btn.click(process, [video_in, src_in, tgt_in, voice_in, dub_in, bg_in, burn_in, start_in, len_in,
                                res_in, style_in, yt_in, clone_in, bible_in, who_token],
                      [video_out, srt_out, dub_out, status_out], show_progress_on=video_out)
    gr.HTML('<div class="foot">Runs fully on your computer. Nothing is uploaded anywhere.</div>'
            '<div class="corner-credit">Developed and created by <b>Abhishek Surya Doppalapudi</b></div>')

if __name__ == "__main__":
    # Set SHARE=1 to get a temporary public link (e.g. when running on Google Colab)
    share = os.environ.get("SHARE", "0") == "1"
    print(f"Running on: {DEVICE.upper()}" + ("" if DEVICE == "cuda" else
          "  (no GPU found - dubbing will be slow; try short clips first)"))
    demo.queue().launch(share=share, inbrowser=not share, theme=THEME, css=CSS,
                          head=FAVICON_LINK + "<script>(function(){ var go=function(){ try { ("
                               + RAIN_JS + ")(); document.documentElement.setAttribute('data-rain','ok'); }"
                               " catch(e) { document.documentElement.setAttribute('data-rain','err:'+e); } };"
                               " if (document.readyState==='loading') document.addEventListener('DOMContentLoaded', go);"
                               " else go(); })();</script>")
