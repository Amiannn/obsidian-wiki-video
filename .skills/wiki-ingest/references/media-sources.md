# Local video and audio sources

Use the existing wiki-ingest config, trust boundary, writing profile, staged-write
mode, page limits, cross-linking and memory workflow. Media adds evidence extraction;
the agent still distills and integrates knowledge. No separate wiki schema is needed.

## Prepare evidence

Run against the resolved vault, using quoted paths:

```bash
obsidian-wiki media prepare /path/to/video.mp4 --vault "$OBSIDIAN_VAULT_PATH" --language zh --pitch
```

Requires FFmpeg/ffprobe and the checkout's optional `media` dependencies. The default
ASR is faster-whisper `small` on CPU, with automatic model download on first use.
Choose `--model`, `--model-revision`, `--device` and `--compute-type` for the available
environment. A local model directory is fingerprinted by content. Pin remote model
revisions for reproducibility. Do not silently substitute a transcript-only result
for audio analysis if extraction fails; report the unavailable stage.

`--transcript /path/to/transcript.json` skips ASR and accepts a JSON object:

```json
{"language":"zh","segments":[{"start":1.0,"end":4.5,"text":"原始說話內容","speaker":"speaker_01"}]}
```

Times are seconds from the media's start. Starts must be ordered; overlapping turns
are allowed. Speaker is optional and scoped to this recording; the tool does not
perform diarization or cross-video identity matching. A caller-provided label is
not an independently verified identity.

The JSON response gives `source` (a Markdown evidence packet), `bundle`, and
`status` (`prepared` or `cached`). The bundle is already archived under
`_raw/_archived/media/<source hash>/<recipe hash>/`. It contains an original copy,
normalized audio, JSON evidence, uniformly sampled frames, and representative audio
clips. Never move or rewrite its contents. No wiki pages or manifest ingest entries
have been created yet. `cached` only means extraction is reusable, not that the
source has already been distilled into the wiki.

## Read and distill

1. Run normal `cache-check` on the returned **source.md**, not the original binary.
   On a cache hit, check whether this source's wiki pages need work as usual.
2. Read source.md and evidence.json. For long transcripts, read in ranges and track
   coverage; do not silently truncate. Inspect relevant frames with vision and listen
   to relevant audio if the agent has audio capability. Without vision, mark visual
   interpretation unavailable; without audio understanding, limit voice descriptions
   to provided measurements and explicitly supported transcript patterns.
3. Integrate content into existing `concepts/`, `references/`, `entities/` and
   `synthesis/` pages. A video reference can hold its outline and representative
   frames; create a speaking-style synthesis only when evidence supports it.
4. Cite claims with source.md segment block IDs, e.g.
   `[[_raw/_archived/media/<hash>/<recipe>/source#^s00001|00:01–00:04.5]]`.
   Embed relevant clip WAVs or frames using their vault-relative paths. Clip start/end
   may cover only part of a long transcript segment; state the actual clip interval.
5. Preserve provenance: ASR is unreviewed transcription; frame text is visual evidence;
   audio metrics are measurements; interpretation of delivery is `^[inferred]`.
   Do not infer personality, emotion, speaker identity, or stable personal traits from
   pitch/energy. A recurring speaking style needs multiple cited observations.

Metrics: `transcript_characters_per_minute` counts Unicode letters/digits over the
whole segment (including pauses), not words/minute or articulation rate. Low-energy
intervals use -40 dBFS for at least 0.3 s; these may be silence, recording conditions,
or quiet speech, not necessarily pauses. Pitch percentiles use voiced frames in a
60–500 Hz search range; null means unavailable, never zero. Background music,
overlapping voices and recording levels can confound these measurements. Uniform
frame samples are not scene detection or exhaustive video understanding.

## Finish with the normal writer

Use the archived source.md's vault-relative path as the manifest/source key. This
bundle is a generated local source rather than a draft awaiting Raw Mode promotion.
Keep its original-media hash in the reference page as additional provenance. All
derivatives of the same media hash count as one source lineage for confidence.

- Put the archived source.md in each page's bottom Sources section and `sources:`.
- Run `snapshots set PAGE --vault VAULT --archive <archived-source.md>`.
- Run `cache-update VAULT <archived-source.md> --pages <produced pages>` after writing.
- Run `memory sync INGEST ...` and normal lint/cross-link checks.
- For staged writes, follow wiki-ingest's staging bookkeeping instead of claiming
  live pages have been published.

New extraction settings make a new immutable bundle. When re-ingesting the same
media hash, update its existing reference/concept pages, retaining useful earlier
citations; do not create a duplicate video or count it as independent corroboration.
