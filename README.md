# Whisper Video Translator

Developed and created by **Abhishek Surya Doppalapudi**.

Upload a video, choose an Indian language, and get:

- a **subtitle file (.srt)** in that language
- a **dubbed video** with a new voice track (and the subtitles on screen)

100% free and open source. No paid APIs.

| Step | Model |
|---|---|
| Speech to text | OpenAI Whisper (turbo) |
| Translation (22 Indian languages + English) | AI4Bharat IndicTrans2 |
| Dubbed voice | AI4Bharat Indic Parler-TTS |
| Audio/video processing | FFmpeg |

## Setup (VS Code, Windows / Mac / Linux)

1. **Install Python 3.10–3.12** and **FFmpeg**
   - Windows: `winget install ffmpeg`, then restart VS Code
   - Mac: `brew install ffmpeg`
   - Check: `ffmpeg -version`

2. **Open this folder in VS Code**, then open a terminal (`Ctrl + ` `):

   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Mac/Linux:
   source .venv/bin/activate

   pip install -r requirements.txt
   ```

   **NVIDIA GPU?** Install the CUDA build of PyTorch first (from pytorch.org) so it runs much faster.

3. **Hugging Face login (free).** Some AI4Bharat models ask you to accept their terms first.
   - Make a free account at huggingface.co
   - Open https://huggingface.co/ai4bharat/indic-parler-tts and the IndicTrans2 model pages, and click **Agree / Request access** if shown
   - Create a token (Settings → Access Tokens), then run:

   ```bash
   huggingface-cli login
   ```

4. **Run it:**

   ```bash
   python app.py
   ```

   The app opens in your browser. The first run downloads the models (a few GB), so it takes a while once.

## No GPU? Use Google Colab (free GPU)

```python
!apt -y install ffmpeg
!git clone https://github.com/<your-username>/indic-video-translator
%cd indic-video-translator
!pip install -r requirements.txt
!huggingface-cli login
!SHARE=1 python app.py
```

Use **Runtime → Change runtime type → T4 GPU**. Open the public link it prints.

## Good to know

- On a laptop **without a GPU**, subtitles are fine but dubbing is slow. Test with a 30–60 second clip first.
- Languages Whisper can't recognise as the *spoken* language (e.g. Bodo, Santali) can still be *target* languages.
- If the burned-in subtitles show boxes instead of letters, untick "Show subtitles on the video". The .srt file and the subtitle track still work in VLC and on YouTube.
- Only translate videos you own or have permission to use.

## Push to GitHub

```bash
git init
git add .
git commit -m "Indic Video Translator: first version"
git branch -M main
git remote add origin https://github.com/<your-username>/indic-video-translator.git
git push -u origin main
```

## Extra features
- **Word-by-word (reels) captions** and a classic sentence style: pick "Subtitle style". Fonts are in `fonts/`.
- **YouTube link**: paste a link instead of uploading a file (uses `yt-dlp`; only translate videos you have the right to use).
- **Voice cloning (optional)**: dub in the speaker's own voice for Hindi, Telugu, Tamil, Kannada, Malayalam, Bengali,
  Marathi, Gujarati, Punjabi, Odia and Assamese. Install steps are in `requirements-voice-clone.txt`.
  Only clone voices you have permission to use.
- **Telugu Bible for scripture**: when the target is Telugu and the speaker names or quotes scripture in English,
  verses and book names come from the Telugu IRV instead of machine translation. Run `python fetch_bible.py` once
  (downloads the texts into `bible/`, which is not committed). The Telugu IRV is (c) 2017, 2019 Bridge Connectivity
  Solutions, licensed CC BY-SA 4.0 (https://ebible.org/tel2017/): credit it when you publish. The app prints the
  credit line after each run that used it.
