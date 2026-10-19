"""Media invariants and real FFmpeg integration; no network or ASR weights needed."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from obsidian_wiki import media


def test_core_cli_needs_no_media_imports():
    result = subprocess.run([sys.executable, "-S", "-m", "obsidian_wiki.cli", "media", "prepare", "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--transcript" in result.stdout


@pytest.mark.parametrize("segments", [
    [{"start": -1, "end": 1, "text": "a"}],
    [{"start": 1, "end": 1, "text": "a"}],
    [{"start": 1, "end": 11, "text": "a"}],
    [{"start": float("nan"), "end": 2, "text": "a"}],
    [{"start": 1, "end": float("inf"), "text": "a"}],
    [{"start": True, "end": 2, "text": "a"}],
    [{"start": 0, "end": 2, "text": ""}],
    [{"start": 0, "end": 2, "text": "a", "speaker": 1}],
    [{"start": 2, "end": 3, "text": "a"}, {"start": 1, "end": 2, "text": "b"}],
])
def test_invalid_transcript_is_rejected(segments):
    with pytest.raises(media.MediaError):
        media.validate_transcript({"segments": segments}, 10)


def test_overlaps_and_unknown_speakers_are_preserved():
    result = media.validate_transcript({"segments": [
        {"start": 0, "end": 2, "text": "甲", "speaker": "local_a"},
        {"start": 1, "end": 3, "text": "乙"},
    ]}, 4)
    assert result["segments"][1]["start"] == 1
    assert result["segments"][1]["speaker"] is None


@pytest.fixture
def recording(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg required")
    pytest.importorskip("numpy")
    source = tmp_path / "測試 ; $literal.mp4"
    # One second of silence then a tone, with a real video stream.
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=3:r=10",
                    "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                    "-af", "adelay=1000:all=1", "-c:v", "mpeg4", "-c:a", "aac", "-t", "3", str(source)], check=True)
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps({"language": "zh", "segments": [
        {"start": 0, "end": 1, "text": "測試文字"},
        {"start": 1, "end": 3, "text": "示範", "speaker": "local_a"},
    ]}), encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    return source, transcript, vault


def test_real_bundle_cache_and_recipe_invalidation(recording):
    source, transcript, vault = recording
    result = media.prepare(source, vault, transcript_path=transcript, max_frames=2, max_clips=2, clip_seconds=0.5)
    bundle = Path(result["bundle"])
    evidence = json.loads((bundle / "evidence.json").read_text())
    assert media.file_hash(bundle / "original.mp4") == media.file_hash(source)
    assert len(evidence["frames"]) == 2
    assert len(evidence["clips"]) == 2
    for clip in evidence["clips"]:
        with wave.open(str(bundle / clip["path"]), "rb") as stream:
            assert stream.getnframes() / stream.getframerate() == pytest.approx(0.5, abs=0.01)
    assert evidence["features"]["segments"][0]["low_energy_fraction"] > 0.9
    assert evidence["features"]["segments"][1]["transcript_characters_per_minute"] == 60
    assert not (vault / ".manifest.json").exists()  # preparing is not wiki ingestion
    packet = Path(result["source"]).read_text()
    assert "^s00001" in packet and "![[" in packet
    assert "Speaker: unknown" in packet
    before = (bundle / "source.md").stat().st_mtime_ns
    again = media.prepare(source, vault, transcript_path=transcript, max_frames=2, max_clips=2, clip_seconds=0.5)
    assert again["status"] == "cached"
    assert (bundle / "source.md").stat().st_mtime_ns == before
    transcript.write_text('{"segments":[{"start":1,"end":2,"text":"訂正"}]}')
    changed = media.prepare(source, vault, transcript_path=transcript, max_frames=2, max_clips=2, clip_seconds=0.5)
    assert changed["bundle"] != result["bundle"]
    assert Path(result["source"]).read_text() == packet
    (bundle / "clip-s00001.wav").unlink()
    with pytest.raises(media.MediaError, match="modified media bundle"):
        media._verify_bundle(bundle)


def test_failed_extraction_does_not_publish_or_mark_ingested(recording, monkeypatch):
    source, transcript, vault = recording

    def fail(*args, **kwargs):
        raise media.MediaError("simulated failure")

    monkeypatch.setattr(media, "audio_features", fail)
    with pytest.raises(media.MediaError, match="simulated failure"):
        media.prepare(source, vault, transcript_path=transcript)
    assert not list(vault.rglob("complete.json"))
    assert not list(vault.rglob(".preparing-*"))
    assert not (vault / ".manifest.json").exists()


def test_invalid_transcript_fails_before_writing(recording):
    source, transcript, vault = recording
    transcript.write_text('{"segments":[{"start":0,"end":999,"text":"bad"}]}')
    with pytest.raises(media.MediaError):
        media.prepare(source, vault, transcript_path=transcript)
    assert not list(vault.iterdir())


def test_audio_only_and_pitch(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("parselmouth")
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    audio = tmp_path / "tone.wav"
    rate = 16000
    data = np.concatenate([np.zeros(rate), 0.25 * np.sin(2 * np.pi * 220 * np.arange(rate) / rate)])
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes((data * 32767).astype("<i2").tobytes())
    transcript = tmp_path / "transcript.json"
    transcript.write_text('{"segments":[{"start":0,"end":1,"text":"quiet"},{"start":1,"end":2,"text":"tone"}]}')
    vault = tmp_path / "vault"
    vault.mkdir()
    result = media.prepare(audio, vault, transcript_path=transcript, pitch=True)
    evidence = json.loads((Path(result["bundle"]) / "evidence.json").read_text())
    assert evidence["frames"] == []
    quiet, tone = evidence["features"]["segments"]
    assert quiet["pitch_hz_p10_p50_p90"] is None
    assert quiet["rms_dbfs"] is None
    assert tone["pitch_hz_p10_p50_p90"][1] == pytest.approx(220, abs=2)


def test_silent_video_does_not_require_asr(recording, monkeypatch):
    source, _, vault = recording
    silent = source.with_name("silent.mp4")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source), "-an", "-c:v", "copy", str(silent)], check=True)
    monkeypatch.setattr(media, "transcribe", lambda *a, **k: pytest.fail("no audio must not invoke ASR"))
    result = media.prepare(silent, vault, max_frames=2)
    evidence = json.loads((Path(result["bundle"]) / "evidence.json").read_text())
    assert evidence["transcript_status"] == "no audio"
    assert evidence["transcript"]["segments"] == []
    assert evidence["features"] is None
    assert evidence["clips"] == []
    assert len(evidence["frames"]) == 2


def test_stream_offset_is_preserved(recording):
    source, transcript, vault = recording
    delayed = source.with_name("delayed.mkv")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=3:r=10",
                    "-itsoffset", "1", "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
                    "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(delayed)], check=True)
    result = media.prepare(delayed, vault, transcript_path=transcript, max_frames=0, max_clips=0)
    evidence = json.loads((Path(result["bundle"]) / "evidence.json").read_text())
    assert evidence["media"]["audio_offset"] == pytest.approx(1, abs=0.02)
    assert evidence["features"]["segments"][0]["low_energy_fraction"] > 0.95
    with wave.open(str(Path(result["bundle"]) / "audio.wav"), "rb") as stream:
        assert stream.getnframes() / stream.getframerate() == pytest.approx(3, abs=0.02)


def test_empty_asr_result_stays_empty_and_is_cached(recording, monkeypatch):
    source, _, vault = recording
    calls = []

    def fake_asr(*args, **kwargs):
        calls.append(True)
        return {"language": "en", "segments": []}

    # Keeps this behavioral test independent of installed model packages.
    monkeypatch.setattr(media, "_package", lambda name: "test-version")
    monkeypatch.setattr(media, "transcribe", fake_asr)
    result = media.prepare(source, vault, max_frames=0)
    evidence = json.loads((Path(result["bundle"]) / "evidence.json").read_text())
    assert evidence["transcript"]["segments"] == []
    assert evidence["clips"] == []
    assert evidence["transcript_status"] == "ASR (unreviewed)"
    assert media.prepare(source, vault, max_frames=0)["status"] == "cached"
    assert len(calls) == 1
