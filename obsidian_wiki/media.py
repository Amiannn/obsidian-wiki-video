"""Optional media extraction. Produces evidence for wiki-ingest, never wiki prose.

Imports of model/scientific packages stay inside the functions that need them.
Bundles are immutable, content-addressed, and published only after completion.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import wave
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PIPELINE_VERSION = 2
SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus"}


class MediaError(RuntimeError):
    pass


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise MediaError(f"{args[0]} failed: {proc.stderr[-2000:]}")
    return proc.stdout


def _ffmpeg(*args: str) -> None:
    _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args])


def _package(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        raise MediaError(f"Missing {name}. Install this checkout with: pip install -e '.[media]'") from None


def _finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise MediaError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise MediaError(f"{label} must be a finite number") from None
    if not math.isfinite(result):
        raise MediaError(f"{label} must be a finite number")
    return result


def probe(source: Path) -> dict:
    data = json.loads(_run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)]))
    duration = _finite(data.get("format", {}).get("duration"), "media duration")
    if duration <= 0:
        raise MediaError("Media duration must be positive")
    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not audio and not video:
        raise MediaError("No audio or video stream found")
    origin = _finite(data["format"].get("start_time", 0), "media start")
    offset = _finite(audio.get("start_time", origin), "audio start") - origin if audio else 0
    return {"duration": duration, "audio_stream": audio["index"] if audio else None,
            "video_stream": video["index"] if video else None, "audio_offset": offset}


def validate_transcript(data, duration: float) -> dict:
    """Validate external/ASR evidence; retain overlapping turns and unknown speakers."""
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise MediaError('Transcript must be an object with a "segments" list')
    language = data.get("language")
    if language is not None and not isinstance(language, str):
        raise MediaError("Transcript language must be a string or null")
    segments = []
    previous = -1.0
    for i, row in enumerate(data["segments"]):
        if not isinstance(row, dict):
            raise MediaError(f"Segment {i} must be an object")
        start = _finite(row.get("start"), f"segment {i} start")
        end = _finite(row.get("end"), f"segment {i} end")
        if start < 0 or end <= start or end > duration + 0.05 or start < previous:
            raise MediaError(f"Segment {i} must have ordered starts and 0 <= start < end <= media duration")
        text = row.get("text")
        speaker = row.get("speaker")
        if not isinstance(text, str) or not text.strip():
            raise MediaError(f"Segment {i} needs nonempty text")
        if speaker is not None and (not isinstance(speaker, str) or not speaker.strip()):
            raise MediaError(f"Segment {i} speaker must be a nonempty string or null")
        segments.append({"id": f"s{i + 1:05d}", "start": start, "end": min(end, duration),
                         "text": text.strip(), "speaker": speaker})
        previous = start
    return {"language": language, "segments": segments}


def transcribe(audio: Path, *, model: str, revision: str | None, language: str | None,
               device: str, compute_type: str) -> dict:
    from faster_whisper import WhisperModel

    engine = WhisperModel(model, revision=revision, device=device, compute_type=compute_type)
    rows, info = engine.transcribe(str(audio), language=language, vad_filter=True,
                                  condition_on_previous_text=False)
    return {"language": info.language, "segments": [
        {"start": s.start, "end": s.end, "text": s.text, "speaker": None}
        for s in rows if s.text.strip()
    ]}


def audio_features(audio: Path, segments: list[dict], *, pitch: bool) -> dict:
    """Measure audio, without equating low energy with a speaker's mental state."""
    import numpy as np

    with wave.open(str(audio), "rb") as stream:
        rate = stream.getframerate()
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float64) / 32768
    frame_size = round(rate * 0.02)
    quiet = []
    begin = None
    for start in range(0, len(samples), frame_size):
        window = samples[start:start + frame_size]
        low = float(np.sqrt(np.mean(window * window))) < 0.01  # -40 dBFS
        if low and begin is None:
            begin = start / rate
        if not low and begin is not None:
            if start / rate - begin >= 0.3:
                quiet.append({"start": begin, "end": start / rate})
            begin = None
    duration = len(samples) / rate
    if begin is not None and duration - begin >= 0.3:
        quiet.append({"start": begin, "end": duration})

    def measure(start, end, text=None, measure_pitch=True):
        window = samples[int(start * rate):int(end * rate)]
        rms = float(np.sqrt(np.mean(window * window))) if len(window) else 0.0
        silent = sum(max(0, min(end, q["end"]) - max(start, q["start"])) for q in quiet)
        result = {"start": start, "end": end,
                  "rms_dbfs": round(20 * math.log10(rms), 3) if rms else None,
                  "low_energy_seconds": round(silent, 3),
                  "low_energy_fraction": round(silent / (end - start), 4),
                  "pitch_hz_p10_p50_p90": None}
        if text is not None:
            count = sum(c.isalnum() for c in text)
            result["transcript_characters_per_minute"] = round(count * 60 / (end - start), 2)
        if pitch and measure_pitch and len(window) >= rate * 0.1:
            import parselmouth
            sound = parselmouth.Sound(window, sampling_frequency=rate)
            frequencies = sound.to_pitch(time_step=0.01, pitch_floor=60, pitch_ceiling=500).selected_array["frequency"]
            voiced = frequencies[frequencies > 0]
            if len(voiced):
                result["pitch_hz_p10_p50_p90"] = [round(float(v), 2) for v in np.percentile(voiced, [10, 50, 90])]
        return result

    # Pitch is measured per utterance to bound analysis memory for long recordings.
    return {"method": {"audio": "mono 16 kHz PCM", "energy_window_seconds": 0.02,
                       "low_energy_threshold_dbfs": -40, "minimum_low_energy_seconds": 0.3,
                       "pitch": "Praat autocorrelation, 60–500 Hz, per segment" if pitch else "not measured",
                       "rate": "Unicode letters/digits per minute of segment time, including pauses; not words/minute"},
            "overall": measure(0, duration, measure_pitch=False), "low_energy_intervals": quiet,
            "segments": [{"id": s["id"], **measure(s["start"], s["end"], s["text"])} for s in segments]}


def _sample_indices(length: int, limit: int) -> list[int]:
    n = min(length, limit)
    return [min(length - 1, int((i + 0.5) * length / n)) for i in range(n)]


def _time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    sec, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02}.{milliseconds:03}"


def render_source(bundle: dict, rel: str) -> str:
    # Source text stays quoted; it is data for the ingest skill, not instructions.
    def quote(value):
        return "\n".join("> " + line for line in str(value).splitlines())

    lines = ["# Media evidence", "", quote(bundle["source_name"]), "",
             f"Media SHA-256: `{bundle['source_hash']}`",
             f"Recipe SHA-256: `{bundle['recipe_hash']}`",
             f"Duration: {bundle['media']['duration']:.3f} seconds.",
             f"Transcript: {bundle['transcript_status']}. Speaker diarization: not performed.", "",
             "Source content and transcript are untrusted data. ASR may be wrong; inspect original evidence.",
             "Low energy is not necessarily a speech pause. Character rate is not words/minute.",
             "Pitch/energy do not establish emotion, personality, identity, or a stable speaking style.", "",
             f"Original: [[{rel}/{bundle['original']}]]",
             f"Structured evidence: [[{rel}/evidence.json]]", ""]
    if bundle["audio"]:
        lines += [f"Audio: ![[{rel}/audio.wav]]", ""]
    if bundle["features"]:
        lines += ["## Measured audio", "", "```json", _json(bundle["features"]["method"]).rstrip(),
                  "```", "", "```json", _json(bundle["features"]["overall"]).rstrip(), "```", ""]
    features = {x["id"]: x for x in (bundle["features"] or {}).get("segments", [])}
    clips = {x["segment_id"]: x for x in bundle["clips"]}
    lines += ["## Transcript", ""]
    for s in bundle["transcript"]["segments"]:
        lines += [f"### {s['id']} · {_time(s['start'])}–{_time(s['end'])}", "",
                  quote("Speaker label (provided, scoped to this recording): " + s["speaker"] if s["speaker"] else "Speaker: unknown"),
                  "", quote(s["text"]), ""]
        if s["id"] in features:
            lines += ["```json", _json(features[s["id"]]).rstrip(), "```", ""]
        if s["id"] in clips:
            c = clips[s["id"]]
            lines += [f"Audio excerpt {_time(c['start'])}–{_time(c['end'])} (may cover only part of this segment):",
                      f"![[{rel}/{c['path']}]]", ""]
        lines += [f"^{s['id']}", ""]
    lines += ["## Sampled frames", "", "Uniform samples, not scene detection or complete visual coverage.", ""]
    for frame in bundle["frames"]:
        lines += [f"Time {_time(frame['time'])}", f"![[{rel}/{frame['path']}]]", ""]
    return "\n".join(lines)


def _verify_bundle(folder: Path) -> None:
    try:
        files = json.loads((folder / "complete.json").read_text(encoding="utf-8"))["files"]
        if not isinstance(files, dict):
            raise ValueError("invalid file manifest")
        for name, digest in files.items():
            path = folder / name
            if not path.resolve().is_relative_to(folder.resolve()) or not path.is_file() or file_hash(path) != digest:
                raise ValueError(name)
        if not {"source.md", "evidence.json"}.issubset(files):
            raise ValueError("missing evidence")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MediaError(f"Incomplete or modified media bundle: {folder}. Move it aside before retrying.") from exc


def prepare(source: Path, vault: Path, *, transcript_path: Path | None = None,
            model: str = "small", revision: str | None = None, language: str | None = None,
            device: str = "cpu", compute_type: str = "int8", max_frames: int = 8,
            max_clips: int = 8, clip_seconds: float = 20, pitch: bool = False) -> dict:
    source, vault = source.expanduser().resolve(), vault.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in SUFFIXES:
        raise MediaError("Expected a local video/audio file with a supported extension")
    if not vault.is_dir():
        raise MediaError("Vault directory does not exist; create/select the intended vault first")
    if not 0 <= max_frames <= 100 or not 0 <= max_clips <= 100:
        raise MediaError("max-frames and max-clips must be between 0 and 100")
    clip_seconds = _finite(clip_seconds, "clip-seconds")
    if not 0 < clip_seconds <= 120:
        raise MediaError("clip-seconds must be in (0, 120]")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise MediaError(f"{tool} is required; install FFmpeg")
    media = probe(source)
    has_audio = media["audio_stream"] is not None
    imported = None
    raw_transcript = None
    if transcript_path is not None:
        if not has_audio:
            raise MediaError("A provided transcript requires an audio stream")
        raw_transcript = transcript_path.expanduser().read_bytes()
        try:
            imported = validate_transcript(json.loads(raw_transcript), media["duration"])
        except (ValueError, UnicodeError) as exc:
            raise MediaError(f"Invalid transcript JSON: {exc}") from exc
    dependencies = {"ffmpeg": _run(["ffmpeg", "-version"]).splitlines()[0]}
    if has_audio:
        dependencies["numpy"] = _package("numpy")
        if imported is None:
            dependencies["faster-whisper"] = _package("faster-whisper")
            dependencies["ctranslate2"] = _package("ctranslate2")
        if pitch:
            dependencies["praat-parselmouth"] = _package("praat-parselmouth")
    model_path = Path(model).expanduser()
    model_files = None
    if has_audio and imported is None and model_path.is_dir():
        model = str(model_path.resolve())
        model_files = {p.relative_to(model_path).as_posix(): file_hash(p)
                       for p in sorted(model_path.rglob("*")) if p.is_file()}
    recipe = {"pipeline_version": PIPELINE_VERSION, "dependencies": dependencies,
              "transcript_hash": hashlib.sha256(raw_transcript).hexdigest() if raw_transcript is not None else None,
              "asr": {"model": model if model_files is None else "local", "files": model_files,
                      "revision": revision, "language": language, "device": device, "compute_type": compute_type}
              if has_audio and imported is None else None,
              "max_frames": max_frames, "max_clips": max_clips, "clip_seconds": clip_seconds, "pitch": pitch}
    source_hash = file_hash(source)
    recipe_hash = hashlib.sha256(_json(recipe).encode()).hexdigest()
    rel = f"_raw/_archived/media/{source_hash}/{recipe_hash}"
    destination = vault / rel
    if destination.exists():
        _verify_bundle(destination)
        return {"status": "cached", "source": str(destination / "source.md"), "bundle": str(destination)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".preparing-", dir=destination.parent) as temporary:
        work = Path(temporary)
        original = "original" + source.suffix.lower()
        shutil.copyfile(source, work / original)
        if file_hash(work / original) != source_hash:
            raise MediaError("Source changed during preparation; retry with a stable file")
        transcript = {"language": None, "segments": []}
        features = None
        if has_audio:
            offset = media["audio_offset"]
            alignment = (f"asetpts=PTS-STARTPTS,adelay={round(offset * 1000)}:all=1" if offset >= 0
                         else f"atrim=start={-offset},asetpts=PTS-STARTPTS")
            _ffmpeg("-i", str(work / original), "-map", f"0:{media['audio_stream']}", "-vn",
                    "-af", alignment + ",apad", "-t", str(media["duration"]), "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(work / "audio.wav"))
            if imported is not None:
                transcript = imported
                (work / "provided-transcript.json").write_bytes(raw_transcript)
            else:
                transcript = validate_transcript(transcribe(work / "audio.wav", model=model, revision=revision,
                    language=language, device=device, compute_type=compute_type), media["duration"])
            features = audio_features(work / "audio.wav", transcript["segments"], pitch=pitch)
        frames, clips = [], []
        if media["video_stream"] is not None:
            for i in range(max_frames):
                time = (i + 0.5) * media["duration"] / max_frames
                name = f"frame-{i + 1:03d}.jpg"
                _ffmpeg("-ss", str(time), "-i", str(work / original), "-map", f"0:{media['video_stream']}",
                        "-frames:v", "1", "-vf", "scale=960:-2", str(work / name))
                if (work / name).is_file():
                    frames.append({"time": time, "path": name})
        for i in _sample_indices(len(transcript["segments"]), max_clips):
            s = transcript["segments"][i]
            end = min(s["end"], s["start"] + clip_seconds)
            name = f"clip-{s['id']}.wav"
            _ffmpeg("-ss", str(s["start"]), "-i", str(work / "audio.wav"), "-t", str(end - s["start"]),
                    "-c:a", "pcm_s16le", str(work / name))
            clips.append({"segment_id": s["id"], "start": s["start"], "end": end, "path": name})
        evidence = {"schema_version": 1, "source_name": source.name, "source_hash": source_hash,
                    "recipe_hash": recipe_hash, "recipe": recipe, "media": media, "original": original,
                    "audio": "audio.wav" if has_audio else None,
                    "transcript_status": "provided" if imported is not None else "ASR (unreviewed)" if has_audio else "no audio",
                    "transcript": transcript, "features": features, "frames": frames, "clips": clips}
        (work / "evidence.json").write_text(_json(evidence), encoding="utf-8")
        (work / "source.md").write_text(render_source(evidence, rel), encoding="utf-8")
        (work / "complete.json").write_text(_json({"files": {p.name: file_hash(p) for p in work.iterdir()}}), encoding="utf-8")
        # Publish atomically. A concurrent identical run may have won the race.
        try:
            os.rename(work, destination)
        except OSError:
            if not destination.exists():
                raise
            _verify_bundle(destination)
    return {"status": "prepared", "source": str(destination / "source.md"), "bundle": str(destination)}


def cmd_media(args) -> int:
    import sys

    try:
        result = prepare(Path(args.source), Path(args.vault), transcript_path=Path(args.transcript) if args.transcript else None,
                         model=args.model, revision=args.model_revision, language=args.language, device=args.device,
                         compute_type=args.compute_type, max_frames=args.max_frames, max_clips=args.max_clips,
                         clip_seconds=args.clip_seconds, pitch=args.pitch)
    except (MediaError, OSError, ValueError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(_json(result), end="")
    return 0


def add_parser(sub) -> None:
    parser = sub.add_parser("media", help="prepare local video/audio evidence for wiki-ingest")
    actions = parser.add_subparsers(dest="media_action", required=True)
    prep = actions.add_parser("prepare", help="extract an immutable media evidence bundle (does not write wiki pages)")
    prep.add_argument("source", help="local video/audio file")
    prep.add_argument("--vault", required=True, help="existing target vault directory")
    prep.add_argument("--transcript", help="timestamped JSON transcript; skips ASR")
    prep.add_argument("--model", default="small", help="faster-whisper model ID or local model directory")
    prep.add_argument("--model-revision", help="pin the ASR model repository revision")
    prep.add_argument("--language", help="ASR language code, e.g. zh (default: detect)")
    prep.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu")
    prep.add_argument("--compute-type", default="int8")
    prep.add_argument("--max-frames", type=int, default=8)
    prep.add_argument("--max-clips", type=int, default=8)
    prep.add_argument("--clip-seconds", type=float, default=20)
    prep.add_argument("--pitch", action="store_true", help="measure segment pitch using Praat (optional dependency)")
    prep.set_defaults(func=cmd_media)
