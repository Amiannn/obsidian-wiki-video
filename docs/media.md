# Video and audio ingestion

Media ingestion follows the existing three-layer design: original evidence, linked
Markdown knowledge, and agent skills. The optional CLI extracts evidence; the
`wiki-ingest` agent reads it and maintains the wiki. Running `media prepare` alone
does not create knowledge pages or invoke an LLM for synthesis.

## Try it

Install from this fork in a separate Python environment, plus system FFmpeg/ffprobe:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[media]'
mkdir -p vault
.venv/bin/obsidian-wiki media prepare /path/to/video.mp4 --vault ./vault --language zh --pitch
```

Ask the agent to run `wiki-ingest` on the returned `source` path, targeting this
vault. For an empty directory, initialize its wiki with `wiki-setup` first. Existing
agent installations may need their skills refreshed from this checkout. Source
checkouts can also use `python -m obsidian_wiki.cli` in place of `obsidian-wiki`.

Local MP4/MKV/MOV/WebM/M4V/AVI and WAV/MP3/M4A/FLAC/OGG/Opus are accepted. Codec
support depends on FFmpeg. URLs/downloaders and streaming inputs are not supported.
The original file is copied unchanged; the full media is processed without a silent
duration cap. First-time ASR downloads model weights, not uploaded input media.

## Options and output

| Option | Default | Purpose |
|---|---|---|
| `--vault PATH` | required | Existing target directory; no global config mutation |
| `--transcript FILE` | ASR | Use timestamped JSON instead of speech recognition |
| `--model ID_OR_PATH` | `small` | faster-whisper model or local model directory |
| `--model-revision REV` | model default | Pin remote weights for reproducibility |
| `--language CODE` | detect | ASR language, e.g. `zh` or `en` |
| `--device` | `cpu` | `cpu`, `cuda`, or `auto` |
| `--compute-type` | `int8` | CTranslate2 compute type; choose for the device |
| `--pitch` | off | Praat per-segment pitch percentiles |
| `--max-frames` | 8 | Uniform frame samples over the video, 0–100 |
| `--max-clips` | 8 | Representative transcript clips across the recording, 0–100 |
| `--clip-seconds` | 20 | Maximum duration of each generated clip, up to 120 s |

Whisper timestamps are approximate segment boundaries, not forced alignment.
Speaker diarization is not implemented. You can provide externally labeled segments:

```json
{
  "language": "zh",
  "segments": [
    {"start": 1.0, "end": 4.5, "text": "先舉個例子。", "speaker": "speaker_01"},
    {"start": 5.0, "end": 8.0, "text": "再來說明這個概念。"}
  ]
}
```

All timestamps are seconds relative to media start, including any leading silence.
Ordered segment starts and finite, positive intervals within the recording are
required. Overlaps are retained. A speaker label applies only to this recording.
Audio/video stream start offsets are reconciled when extracting the mono 16 kHz WAV.
Only the first audio stream and first non-cover-art video stream are selected.

The command emits JSON containing `source`, `bundle`, and `status`. A bundle lives at
`_raw/_archived/media/<media SHA-256>/<recipe SHA-256>/`:

- `original.*`: byte-identical original media.
- `audio.wav`: normalized analysis/playback audio, when audio exists.
- `provided-transcript.json`: preserved external transcript, when supplied.
- `evidence.json`: segments, audio measurements, frame/clip times, processing recipe.
- `source.md`: readable evidence packet for `wiki-ingest`, with playable audio embeds.
- `frame-*.jpg`, `clip-*.wav`: bounded representative evidence.
- `complete.json`: artifact hashes, written only when extraction succeeds.

Silent video produces frames and an explicit `no audio` status. Audio-only input
produces no frames. An empty ASR result remains empty; it does not become invented
text or imply proven absence of speech.

## Measurements and interpretation

The pipeline measures low-energy intervals (-40 dBFS, at least 0.3 s), RMS dBFS,
transcript letters/digits per segment minute, and optionally voiced pitch percentiles
(10th/50th/90th, 60–500 Hz). The character rate includes pauses and is not words/minute.
Low energy is not VAD, and these intervals are not automatically speaking pauses.
Pitch is per segment; the overall pitch field is null. Missing pitch or silence RMS
is null rather than a fabricated number.

These are recording measurements, not emotion/personality classifiers. Music,
overlap, noise and recording gain affect them. Compare delivery with local context,
and attach source time ranges to agent interpretations. Cross-recording identity,
stable speaking style, OCR, scene detection and audio-language-model interpretation
remain agent-assisted or future work. Frames require a vision-capable agent to read.

## Cache and provenance

The recipe includes pipeline/dependency versions, supplied transcript hash, ASR
model/settings, sampling limits and pitch selection. Local model files are hashed;
remote models should use an immutable `--model-revision`. An unpinned remote model
name does not detect a changed upstream checkpoint on a later cache hit.

Bundles are prepared in temporary directories and atomically published. A cache hit
verifies every artifact hash; missing/modified artifacts cause an error instead of
silently reusing evidence. Move a damaged bundle aside before retrying. Failed runs
do not publish a complete bundle or mark anything ingested in `.manifest.json`.

Preparation and wiki compilation have separate caches. Pass source.md to the normal
`cache-check`/`cache-update`, snapshots and memory commands after distillation. Reuse
existing pages when a new recipe analyzes the same media hash. Source derivatives
are one evidence lineage. The original is copied per recipe, so reserve disk space;
the current analysis loads decoded audio into memory and is intended for an initial
local workflow, not unbounded batch processing.

Implementation references: [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
[FFmpeg](https://ffmpeg.org/ffmpeg.html), and
[Parselmouth](https://parselmouth.readthedocs.io/en/stable/api_reference.html).

## Prototype validation

The initial end-to-end trial uses the public
[faster-whisper JFK audio fixture](https://github.com/SYSTRAN/faster-whisper/blob/master/tests/data/jfk.flac)
(SHA-256 `63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715`).
It is combined with a clearly labeled synthetic title slide, not historical video.
This checks the media plumbing and agent distillation; it is not a benchmark of
complex visual understanding or Mandarin ASR accuracy.

The 11-second trial produced two actual ASR segments, two playable clips, frame
samples, and Praat/energy measurements. The agent then used the normal snapshots,
cache and memory commands to build three linked reference/concept/synthesis pages.
The CLI itself did not generate those knowledge pages. The demo lives in the ignored
local `vault/video-demo/`, with entry page `references/video-demo.md`.

The trial uses `tiny.en`, pinned to revision
`0d3d19a32d3338f10357c0889762bd8d64bbdeba`, CPU `int8`, and `--pitch`.
For example, after creating your local demo video:

```bash
obsidian-wiki media prepare demo.mp4 --vault ./vault/video-demo \
  --model tiny.en --model-revision 0d3d19a32d3338f10357c0889762bd8d64bbdeba \
  --language en --pitch --max-frames 2 --max-clips 3
```

Automated tests generate media locally and check timestamp validation, delayed audio
alignment, preserved originals, recipe invalidation, failure cleanup, silent video,
audio-only input, pitch on a known tone, and archived attachment links. They do not
download ASR weights. Media-dependent cases skip when their optional dependencies
or system FFmpeg are absent.
