r"""
atlas.video - what a recorded session showed and said, as a document the
build can index.

    python -m atlas.video "C:\Recordings" --out "C:\docs\Video transcripts"
    python -m atlas.video "C:\Recordings" --dry-run         what would be read
    python -m atlas.video "D:\KT sessions"                  transcripts beside the videos (a folder the build does NOT read)

Keep the recordings and their caption files OUT of the folders the build reads
and write the transcripts INTO one (--out): a video in the build's folders is
read in full on every build only to be skipped, one over 300 MB is listed as a
problem, and a caption file there is indexed as whatever its words look like.
A caption file on its own (the transcript downloaded without the recording)
is read too: it becomes `<name>.video.docx` like a recording would.

For each video (.mp4 .m4v .mov .wmv .avi) it writes `<name>.video.docx`
(kt-session.mp4 -> kt-session.video.docx, document KT-SESSION.VIDEO) beside it,
or under --out in the same subfolders;
the next build indexes that as a document with one section per two minutes,
found by `docs TERM` and cited like any other document section:

  * SCREEN - a frame every --every seconds (default 10), read by the Windows
    OCR engine and CHECKED AGAINST THE INDEX (--db atlas.db): a shared
    mainframe screen inside a recording is small and blurred, and the engine
    invents where it cannot read - so every name-shaped piece is kept only
    when the index, a keyword list or a word list knows it, a misread name
    is snapped to the real one, an unknown one is marked `?`, and a line
    with nothing recognisable is dropped (LESSONS 166). The same screen shown
    for a minute is written once, with the time it appeared. A full-screen
    emulator in a 1080p recording reads mostly; a shared window in a 720p
    recording reads in fragments.
  * SAID - what was said, from the caption file beside the video
    (`<video>.vtt` or `.srt`: the transcript Teams or Stream makes for a
    recording - download it and put it beside the video). Without one the
    speech is NOT transcribed and the transcript says so. The speech
    recogniser Windows ships (--speech-recogniser) reads synthetic speech
    well and real meetings badly: several voices, room noise, accents and
    mainframe words come back as fluent sentences nobody said (LESSONS 165).
    Its lines are labelled unreliable; do not rely on them.

Everything runs on the laptop with what Windows already has (Media
Foundation, Windows.Media.Ocr, System.Speech): no install, no network, no
model tokens. A transcript is prose - what was said, never a fact about
what runs - and every answer that uses it must say so.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Dict, List, Optional, Tuple

from . import ocr, screentext

VIDEO_EXT = (".mp4", ".m4v", ".mov", ".wmv", ".avi")
RECOGNISER_LABEL = "SAID (recogniser - unreliable)"
SIDECAR = ".video.docx"
MARK = "written by atlas.video"
EVERY_SECONDS = 10
SECTION_SECONDS = 120
FRAME_WIDTH = 1600

# Frames and the sound track out of a video with Windows Media Foundation.
# One JSON object per line; [Console]::Out.WriteLine, never Write-Output inside
# a function (LESSONS 132).
_PS_EXTRACT = r'''
param([string]$Video, [string]$OutDir, [double]$Every, [int]$Width, [int]$NoFrames, [int]$NoAudio, [int]$ForceConvert)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
function Say($o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress)) }
try {
  [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFolder, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.Editing.MediaComposition, Windows.Media.Editing, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.Editing.MediaClip, Windows.Media.Editing, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.Transcoding.MediaTranscoder, Windows.Media.Transcoding, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.MediaProperties.MediaEncodingProfile, Windows.Media.MediaProperties, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.MediaProperties.AudioEncodingProperties, Windows.Media.MediaProperties, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
} catch { Say @{error="Windows media components unavailable: $_"}; exit 0 }
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' }
$asOp = ($methods | Where-Object { $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asActProg = ($methods | Where-Object { $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncActionWithProgress`1' })[0]
function Await($op, $T) { $t = $asOp.MakeGenericMethod($T).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
# a long transcode says it is alive every 10 s (the Python side prints the progress)
function AwaitActProg($op, $P) { $t = $asActProg.MakeGenericMethod($P).Invoke($null, @($op)); while (-not $t.Wait(10000)) { Say @{working=$true} } }
function Convert-Plain($file, $folder) {
  # The media editor refuses some recordings that play perfectly well (a screen recorder's
  # MP4, a Teams download): converted to a plain MP4 by the same decoders Media Player uses,
  # at the source's own size, and read from that (LESSONS 161).
  $plain = Await ($folder.CreateFileAsync("plain.mp4", [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])
  $tc = New-Object Windows.Media.Transcoding.MediaTranscoder
  $prof = [Windows.Media.MediaProperties.MediaEncodingProfile]::CreateMp4([Windows.Media.MediaProperties.VideoEncodingQuality]::Auto)
  $prep = Await ($tc.PrepareFileTranscodeAsync($file, $plain, $prof)) ([Windows.Media.Transcoding.PrepareTranscodeResult])
  if (-not $prep.CanTranscode) { throw "the media pipeline cannot convert it: $($prep.FailureReason)" }
  $t = $asActProg.MakeGenericMethod([double]).Invoke($null, @($prep.TranscodeAsync()))
  while (-not $t.Wait(10000)) { Say @{converting=[long](Get-Item -LiteralPath $plain.Path).Length} }
  return (Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($plain.Path)) ([Windows.Storage.StorageFile]))
}
try {
  $src = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Video)) ([Windows.Storage.StorageFile])
  $folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync($OutDir)) ([Windows.Storage.StorageFolder])
} catch { Say @{error="cannot reach the file: $($_.Exception.GetBaseException().Message)"}; exit 0 }
$asrc = $src
$clip = $null
$why = ""
if ($ForceConvert -eq 0) {
  try { $clip = Await ([Windows.Media.Editing.MediaClip]::CreateFromFileAsync($src)) ([Windows.Media.Editing.MediaClip]) }
  catch { $why = $_.Exception.GetBaseException().Message }
} else { $why = "conversion forced for a test" }
if ($clip -eq $null) {
  try {
    Say @{converting_because=$why}
    $asrc = Convert-Plain $src $folder
    $clip = Await ([Windows.Media.Editing.MediaClip]::CreateFromFileAsync($asrc)) ([Windows.Media.Editing.MediaClip])
    Say @{converted=[string]$asrc.Path}
  } catch {
    # the media layer's own words hide the reason ("The parameter is incorrect"): ask it two more questions
    $more = " (converting it did not help: $($_.Exception.GetBaseException().Message))"
    try {
      [Windows.Storage.FileProperties.VideoProperties, Windows.Storage.FileProperties, ContentType = WindowsRuntime] | Out-Null
      $vp = Await ($src.Properties.GetVideoPropertiesAsync()) ([Windows.Storage.FileProperties.VideoProperties])
      if ([int]$vp.Width -gt 0) { $more += (" - Windows reads it as {0}x{1}, {2:n0} s" -f $vp.Width, $vp.Height, $vp.Duration.TotalSeconds) }
      else { $more += " - Windows finds no video track in it" }
    } catch { $more += " - Windows cannot read its properties either" }
    Say @{error="cannot open the video: $why$more"}; exit 0
  }
}
try {
  $comp = New-Object Windows.Media.Editing.MediaComposition
  [System.Collections.Generic.ICollection[Windows.Media.Editing.MediaClip]].GetMethod("Add").Invoke($comp.Clips, @($clip)) | Out-Null
} catch { Say @{error="cannot open the video: $($_.Exception.GetBaseException().Message)"}; exit 0 }
$dur = [double]$comp.Duration.TotalSeconds
Say @{duration=$dur}
if ($NoFrames -eq 0) {
  # never scaled DOWN: an emulator window in a 1080p desktop recording is
  # unreadable at 1600 wide; anything smaller is letterboxed as it is
  $w = [int]$Width; $h = [int]($Width * 9 / 16)
  try {
    $vp = $clip.GetVideoEncodingProperties()
    if ([int]$vp.Width -gt $w -or [int]$vp.Height -gt $h) { $w = [int]$vp.Width; $h = [int]$vp.Height }
  } catch { }
  Say @{frame_size=("{0}x{1}" -f $w, $h)}
  $t = [Math]::Min(1.0, $dur / 2)
  while ($t -lt $dur) {
    try {
      $stream = Await ($comp.GetThumbnailAsync([TimeSpan]::FromSeconds($t), $w, $h, [Windows.Media.Editing.VideoFramePrecision]::NearestFrame)) ([Windows.Graphics.Imaging.ImageStream])
      $size = [uint32]$stream.Size
      $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
      Await ($reader.LoadAsync($size)) ([uint32]) | Out-Null
      $bytes = New-Object byte[] $size
      $reader.ReadBytes($bytes)
      $out = [string](Join-Path $OutDir ("frame-{0:d7}.jpg" -f [int][Math]::Floor($t)))
      [System.IO.File]::WriteAllBytes($out, $bytes)
      $reader.Dispose(); $stream.Dispose()
      Say @{frame=$t; path=$out}
    } catch { Say @{frame=$t; error="$($_.Exception.GetBaseException().Message)"} }
    $t += $Every
  }
}
$tracks = -1
try { $tracks = [int]$clip.EmbeddedAudioTracks.Count } catch { }
if ($NoAudio -eq 0 -and $tracks -eq 0) { Say @{audio_error="the recording has no sound track"} }
elseif ($NoAudio -eq 0) {
  try {
    $wav = Await ($folder.CreateFileAsync("audio.wav", [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])
    $tc = New-Object Windows.Media.Transcoding.MediaTranscoder
    $prof = [Windows.Media.MediaProperties.MediaEncodingProfile]::CreateWav([Windows.Media.MediaProperties.AudioEncodingQuality]::Low)
    $prof.Audio = [Windows.Media.MediaProperties.AudioEncodingProperties]::CreatePcm(16000, 1, 16)
    $prep = Await ($tc.PrepareFileTranscodeAsync($asrc, $wav, $prof)) ([Windows.Media.Transcoding.PrepareTranscodeResult])
    if ($prep.CanTranscode) { AwaitActProg ($prep.TranscodeAsync()) ([double]); Say @{audio=[string]$wav.Path} }
    else { Say @{audio_error="no sound track that can be read ($($prep.FailureReason))"} }
  } catch { Say @{audio_error="$($_.Exception.GetBaseException().Message)"} }
}
'''

# Speech to text with the recogniser Windows ships (System.Speech). The WAV
# header Media Foundation writes is refused by it, so the raw 16 kHz mono
# samples are handed over with their format stated.
_PS_SPEECH = r'''
param([string]$Wav)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
function Say($o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress)) }
try { Add-Type -AssemblyName System.Speech } catch { Say @{error="System.Speech unavailable: $_"}; exit 0 }
$recs = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
if ($recs.Count -eq 0) { Say @{error="no speech recogniser installed (Settings > Time & language > Speech)"}; exit 0 }
$pick = $recs | Where-Object { $_.Culture.Name -like "en-*" } | Select-Object -First 1
if (-not $pick) { $pick = $recs[0] }
Say @{recognizer=($pick.Culture.Name + " " + $pick.Description)}
$all = [System.IO.File]::ReadAllBytes($Wav)
$pos = 12; $dataAt = -1; $dataLen = 0
while ($pos + 8 -le $all.Length) {
  $id = [System.Text.Encoding]::ASCII.GetString($all, $pos, 4)
  $len = [System.BitConverter]::ToInt32($all, $pos + 4)
  if ($id -eq "data") { $dataAt = $pos + 8; $dataLen = [Math]::Min($len, $all.Length - $dataAt); break }
  $pos += 8 + $len + ($len % 2)
}
if ($dataAt -lt 0) { Say @{error="no audio samples in the sound track"}; exit 0 }
$eng = New-Object System.Speech.Recognition.SpeechRecognitionEngine($pick)
$eng.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$ms = New-Object System.IO.MemoryStream($all, $dataAt, $dataLen)
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$eng.SetInputToAudioStream($ms, $fmt)
Say @{audio_seconds=($dataLen / 32000.0)}
# Continuous recognition, not Recognize() in a loop: only this mode keeps one
# clock across the whole track (Recognize() stamps every phrase 0:00).
Register-ObjectEvent -InputObject $eng -EventName SpeechRecognized -SourceIdentifier atlasSaid | Out-Null
Register-ObjectEvent -InputObject $eng -EventName RecognizeCompleted -SourceIdentifier atlasDone | Out-Null
$eng.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
$last = -1.0; $still = 0
while ($true) {
  $e = Wait-Event -Timeout 10
  if ($e -eq $null) {
    $at = [double]$eng.RecognizerAudioPosition.TotalSeconds
    Say @{pos=$at}                                        # a long silent stretch still shows progress
    if ($at -le $last) { $still++ } else { $still = 0 }
    $last = $at
    if ($still -ge 30) { Say @{error="the recogniser stopped moving at $at s"}; break }
    continue
  }
  Remove-Event -EventIdentifier $e.EventIdentifier
  if ($e.SourceIdentifier -eq "atlasDone") {
    if ($e.SourceEventArgs.Error) { Say @{error=[string]$e.SourceEventArgs.Error.Message} }
    break
  }
  $r = $e.SourceEventArgs.Result
  Say @{at=[double]$r.Audio.AudioPosition.TotalSeconds; said=[string]$r.Text; confidence=[double]$r.Confidence}
}
try { $eng.RecognizeAsyncCancel() } catch { }
$eng.Dispose()
Say @{done=$true}
'''


# --------------------------------------------------------------------------
# pieces that run anywhere
# --------------------------------------------------------------------------

def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def sidecar_of(video: str) -> str:
    """kt-session.mp4 -> kt-session.video.docx (the build names a document
    after the file name without its last extension: KT-SESSION.VIDEO). When
    kt-session.mov sits beside it too, each keeps its own:
    kt-session.mp4.video.docx."""
    stem, ext = os.path.splitext(video)
    folder = os.path.dirname(video) or "."
    try:
        twins = [f for f in os.listdir(folder)
                 if os.path.splitext(f)[0].lower() == os.path.basename(stem).lower() and f.lower().endswith(VIDEO_EXT)]
    except OSError:
        twins = []
    return (video if len(twins) > 1 else stem) + SIDECAR


CAPTION_EXT = (".vtt", ".srt")


def is_caption(path: str) -> bool:
    return path.lower().endswith(CAPTION_EXT)


def captions_beside(video: str) -> Optional[str]:
    """`<name>.vtt` / `.srt` beside the recording. Teams names a download
    'Title-20260916_140000-Meeting Recording.mp4' and its transcript
    'Title.vtt', so when the names differ and the folder holds one recording
    and one caption file, they belong together."""
    if is_caption(video):
        return video
    stem = os.path.splitext(video)[0]
    for cand in (stem + ".vtt", stem + ".srt", video + ".vtt", video + ".srt"):
        if os.path.isfile(cand):
            return cand
    folder = os.path.dirname(video) or "."
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    caps = [f for f in names if is_caption(f)]
    vids = [f for f in names if f.lower().endswith(VIDEO_EXT)]
    if len(caps) == 1 and len(vids) == 1:
        return os.path.join(folder, caps[0])
    return None


_CUE_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*")


def parse_captions(text: str) -> List[Tuple[float, str]]:
    """(start seconds, text) from WebVTT or SRT - the transcript a meeting or
    video service can export. Speaker tags (<v Name>) are kept as 'Name: '."""
    cues: List[Tuple[float, str]] = []
    start: Optional[float] = None
    buf: List[str] = []

    def flush() -> None:
        if start is not None and buf:
            line = " ".join(buf)
            line = re.sub(r"<v\s+([^>]+)>", r"\1: ", line)
            line = re.sub(r"</?[^>]+>", "", line)
            line = html.unescape(re.sub(r"\s+", " ", line)).strip()
            if line:
                cues.append((start, line))

    for raw in text.splitlines():
        ln = raw.strip("\ufeff").strip()
        m = _CUE_TIME.search(ln)
        if m:
            flush()
            h, mi, s, ms = m.group(1), m.group(2), m.group(3), m.group(4)
            start = int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0
            buf = []
        elif not ln:
            flush()
            start, buf = None, []        # a blank line always ends a cue, an empty one too
        elif start is None:
            continue                     # WEBVTT header, NOTE/STYLE blocks, cue numbers: never speech
        else:
            buf.append(ln)
    flush()
    return cues


SAME_SCREEN_CHARS = 6        # this many characters different (or 1% of the screen) is still the same screen


def _screen_key(text: str) -> str:
    return " ".join(re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", " ", text or "").split())


def same_screen(a: str, b: str) -> bool:
    """Two frames of one screen read a character or two differently (video
    compression, a blinking cursor, a clock): still the same screen. A line
    typed or rewritten is a new screen. An edit of only a few characters on
    an otherwise unchanged screen (DISP=SHR to OLD, PROD to TEST) cannot be
    told from an OCR flip and is not written again - the SAID line carries
    what was changed."""
    if a == b:
        return True
    if not a or not b:
        return False
    allowed = max(SAME_SCREEN_CHARS, len(a) // 100)
    if abs(len(a) - len(b)) > allowed:
        return False
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    changed = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
    return changed <= allowed


_SPACED_DOT = re.compile(r"(?<=[A-Z0-9#@$])[ \t]+\.[ \t]*(?=[A-Z0-9#@$])")


def tidy_screen(text: str) -> str:
    """The OCR engine reads PROD.CLAIMS.INPUT off a screen as
    'PROD . CLAIMS . INPUT', and a search for the dataset then misses it. Only
    a dot with a space BEFORE it, between capitals or digits, is joined: a
    sentence never puts a space before its full stop."""
    return _SPACED_DOT.sub(".", text or "")


def dedupe_screens(frames: List[Tuple[float, str]]) -> List[Tuple[float, str]]:
    """The same screen shown for a minute is written once, when it appeared."""
    out: List[Tuple[float, str]] = []
    last = ""
    for t, text in frames:
        key = _screen_key(text)
        if not key or same_screen(key, last):
            continue
        last = key
        out.append((t, text.strip()))
    return out


def sections(screens: List[Tuple[float, str]], speech: List[Tuple[float, str]], speech_source: str,
             window: float = SECTION_SECONDS) -> List[Tuple[str, List[str]]]:
    """(heading, lines) per time window: what was said and what was on the
    screen, in time order, each line starting with its time."""
    events: List[Tuple[float, int, str]] = []
    for t, text in speech:
        events.append((t, 0, f"[{hms(t)}] {speech_source}: {text}"))
    for t, text in screens:
        rows = [r.strip() for r in text.splitlines() if r.strip()]
        if rows:
            # every screen row is prefixed, so no transcript line can look like JCL or COBOL
            events.append((t, 1, f"[{hms(t)}] SCREEN: {rows[0]}"))
            events.extend((t, 2 + k, f"[{hms(t)}] SCREEN: {r}") for k, r in enumerate(rows[1:]))
    events.sort(key=lambda e: (e[0], e[1]))
    out: List[Tuple[str, List[str]]] = []
    cur_key = None
    for t, _k, line in events:
        key = int(t // window)
        if key != cur_key:
            out.append((f"{hms(key * window)} - {hms((key + 1) * window)}", []))
            cur_key = key
        out[-1][1].append(line)
    return out


_XML_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def write_docx(path: str, title: str, intro: List[str], secs: List[Tuple[str, List[str]]]) -> None:
    """A plain Word file: a title, a note, then one Heading 1 per time window.
    Opens in Word; read by the build like any document."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def para(text: str, style: Optional[str] = None) -> str:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        text = _XML_FORBIDDEN.sub(" ", text)          # OCR noise or a PowerShell message can carry them
        return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{html.escape(text, quote=False)}</w:t></w:r></w:p>'

    body = [para(title, "Title")] + [para(x) for x in intro]
    for heading, lines in secs:
        body.append(para(heading, "Heading1"))
        body.extend(para(x) for x in lines)
    doc = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>{"".join(body)}</w:body></w:document>'
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>')
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    os.replace(tmp, path)


def transcript_path(video: str, root: Optional[str] = None, out: Optional[str] = None) -> str:
    """Beside the video, or under `out` in the same subfolders the video has
    under `root` (two session1.mp4 in different folders never collide)."""
    if not out:
        return sidecar_of(video)
    rel = "."
    if root and os.path.isdir(root):
        rel = os.path.relpath(os.path.dirname(os.path.abspath(video)), os.path.abspath(root))
        if rel.startswith(".."):
            rel = "."
    return os.path.normpath(os.path.join(os.path.abspath(out), rel, os.path.basename(sidecar_of(video))))


_OPEN_FAILURE = ("cannot open the video", "cannot reach the file", "Windows media components unavailable")


def _recorded_open_failure(side: str) -> bool:
    """A transcript that only records that nothing could open the recording
    (written by an earlier toolkit): not a result, so the recording is
    tried again - a fix may have landed since."""
    try:
        with zipfile.ZipFile(side) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    return any(f"Nothing readable: {why}" in xml for why in _OPEN_FAILURE)


def is_current(video: str, side: Optional[str] = None) -> bool:
    side = side or sidecar_of(video)
    if not os.path.isfile(side):
        return False
    if _recorded_open_failure(side):
        return False
    inputs = [video]
    cap = captions_beside(video)
    if cap:
        inputs.append(cap)
    return os.path.getmtime(side) >= max(os.path.getmtime(x) for x in inputs)


def find_videos(roots: List[str]) -> List[Tuple[str, str]]:
    """(root it was found under, absolute path), in folder order: every
    recording, and every caption file that has no recording beside it."""
    out: List[Tuple[str, str]] = []
    for root in roots:
        if os.path.isfile(root) and (root.lower().endswith(VIDEO_EXT) or is_caption(root)):
            out.append((os.path.dirname(os.path.abspath(root)), os.path.abspath(root)))
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs.sort()
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            vids = [f for f in sorted(files) if f.lower().endswith(VIDEO_EXT)]
            stems = {os.path.splitext(f)[0].lower() for f in vids} | {f.lower() for f in vids}
            lone = [f for f in sorted(files) if is_caption(f) and os.path.splitext(f)[0].lower() not in stems
                    and not (len(vids) == 1 and sum(is_caption(x) for x in files) == 1)]
            out.extend((root, os.path.abspath(os.path.join(dirpath, f))) for f in vids + lone)
    return out


# --------------------------------------------------------------------------
# the Windows part
# --------------------------------------------------------------------------

def _json_lines(out: str) -> List[dict]:
    rows = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


ONLINE_ONLY_NOTE = ("an online-only OneDrive file ('Free up space'): downloaded for reading and freed again afterwards, "
                    "so the folder never needs the disk space")


def _hydrate(path: str, log=print) -> bool:
    """Download an online-only OneDrive file by reading it through - what
    Media Player does when it plays - with a line every 10 s. True once the
    bytes are on the disk."""
    try:
        total = os.path.getsize(path)
        done = 0
        t0 = last = time.time()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(8 * 1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if time.time() - last >= 10:
                    last = time.time()
                    log(f"  ... downloading from OneDrive: {done // 1024 // 1024} of {total // 1024 // 1024} MB, "
                        f"{int(time.time() - t0)} s")
    except OSError as e:
        log(f"  could not download it from OneDrive ({str(e)[:120]}) - is OneDrive running and signed in?")
        return False
    return True


def _dehydrate(path: str) -> None:
    """Hand the space back ('Free up space'): the file is a placeholder again."""
    try:
        subprocess.run(["attrib", "+U", "-P", path], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def online_only(path: str) -> bool:
    """A OneDrive placeholder: the name and size are here, the bytes are not
    (Media Player fetches them on the fly; the media editor refuses)."""
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x400000 or attrs & 0x40000 or attrs & 0x1000)     # recall on data access / on open / offline


def _open_hint(error: str) -> str:
    """What to do when the media layer will not open a recording."""
    if "CodecNotFound" in error:
        return (" => the recording's codec is not installed on this laptop (an H.265/HEVC recording needs the "
                "'HEVC Video Extensions', which a locked laptop may not allow): ask for it in H.264, or for the "
                "Teams/Stream download, and put its caption file beside it")
    if "cannot open the video" in error:
        return (" => if it does not play in the Media Player app either, the file is incomplete or its codec is missing "
                "on this laptop; if it plays there, send this line")
    return ""


KEEP_FRAMES = 12


def keep_frames(video: str, frames: List[Tuple[float, str]], texts: Dict[str, str], log=print,
                checked: Optional[Dict[str, str]] = None) -> Optional[str]:
    """A dozen frames spread over the recording, saved beside it as pictures
    with the text the OCR engine read from each - so anyone can open a frame
    and its text side by side and see what the toolkit saw. Beside the
    recording, never in a folder the build reads."""
    if not frames:
        return None
    folder = os.path.splitext(video)[0] + ".frames"
    os.makedirs(folder, exist_ok=True)
    step = max(1, len(frames) // KEEP_FRAMES)
    kept = 0
    for t, path in frames[::step][:KEEP_FRAMES]:
        stamp = hms(t).replace(":", "")
        try:
            shutil.copyfile(path, os.path.join(folder, f"frame-{stamp}.jpg"))
            with open(os.path.join(folder, f"frame-{stamp}.txt"), "w", encoding="utf-8") as fh:
                key = os.path.normcase(os.path.abspath(path))
                fh.write(f"what the OCR engine read from frame-{stamp}.jpg (at {hms(t)}):\n\n")
                fh.write(texts.get(key, "") or "(nothing)")
                if checked is not None:
                    fh.write("\n\n--- what is left after the check against the index ---\n\n")
                    fh.write(checked.get(key, "") or "(nothing)")
            kept += 1
        except OSError:
            continue
    log(f"  kept {kept} frame(s) with the text read from each in {folder} - open a .jpg and its .txt side by side")
    return folder


def read_video(video: str, every: float = EVERY_SECONDS, screens: bool = True, speech: bool = True,
               log=print, captions: bool = True, keep: bool = False,
               vocab: Optional[screentext.Vocabulary] = None) -> Dict[str, object]:
    """{'duration', 'screens': [(t, text)], 'speech': [(t, text)], 'speech_source', 'notes': [...]}"""
    video = os.path.abspath(video)      # the Windows media API refuses C:/x/y.mp4 and relative paths
    result: Dict[str, object] = {"duration": 0.0, "screens": [], "speech": [], "speech_source": RECOGNISER_LABEL, "notes": []}
    notes: List[str] = result["notes"]                                   # type: ignore[assignment]
    ok, why = ocr.ocr_available()
    captions = captions_beside(video) if captions else None
    cues: List[Tuple[float, str]] = []
    if captions:
        with open(captions, encoding="utf-8-sig", errors="replace") as fh:
            cues = parse_captions(fh.read())
        if cues:
            result["speech"] = cues
            result["speech_source"] = "SAID (captions)"
            notes.append(f"speech from the caption file {os.path.basename(captions)}")
            speech = False
        else:
            notes.append(f"caption file {os.path.basename(captions)} has no cues that could be read (UTF-8 WebVTT or "
                         f"SRT expected){' - the speech recogniser was used instead' if speech else ''}")
    if not cues and not speech:
        notes.append("speech not transcribed: no caption file beside the recording. Download the meeting's own "
                     "transcript (Teams or Stream: a .vtt file), put it beside the recording, run again. (The Windows "
                     "speech recogniser can be tried with --speech-recogniser; on real meeting audio it produces "
                     "fluent sentences nobody said - do not rely on it.)")
    if is_caption(video):                   # the transcript downloaded without its recording
        result["duration"] = cues[-1][0] if cues else 0.0
        result["ran"] = True
        return result
    if not screens and not speech:
        result["ran"] = True
        return result                       # captions only: no need to open the video at all
    if not ok:
        notes.append(why)
        return result
    try:
        if os.path.getsize(video) == 0:
            notes.append("empty file (0 bytes): the recording was never written, or its download did not finish")
            result["ran"] = True
            return result
    except OSError as e:
        notes.append(f"cannot reach the file: {e}")
        return result
    was_online = online_only(video)
    if was_online:
        log(f"  downloading from OneDrive ({os.path.getsize(video) // 1024 // 1024} MB) - freed again after reading")
        if not _hydrate(video, log):
            notes.append("could not download it from OneDrive - is OneDrive running and signed in? tried again next time")
            return result                   # not a result: tried again once the bytes can come
        notes.append(ONLINE_ONLY_NOTE)
    work = tempfile.mkdtemp(prefix="atlas-video-")
    try:
        t0 = time.time()
        state = {"frames": 0, "t": time.time()}

        def on_line(line: str) -> None:
            if '"frame"' in line:
                state["frames"] += 1
            if '"converting"' in line:                    # the media editor's own heartbeat, every 10 s
                try:
                    mb = int(json.loads(line).get("converting", 0)) // 1024 // 1024
                except (ValueError, TypeError):
                    mb = 0
                state["t"] = time.time()
                log(f"  ... converting the recording to a plain MP4 first: {mb} MB written, {int(time.time() - t0)} s")
                return
            if '"working"' in line:
                state["t"] = time.time()
                log(f"  ... still working on the recording, {int(time.time() - t0)} s")
                return
            if time.time() - state["t"] >= 10:
                state["t"] = time.time()
                log(f"  ... {state['frames']} frame(s) taken, {int(time.time() - t0)} s")

        rc, out, err = ocr._run_ps(_PS_EXTRACT, ["-Video", video, "-OutDir", work, "-Every", str(every),
                                                 "-Width", str(FRAME_WIDTH), "-NoFrames", "0" if screens else "1",
                                                 "-NoAudio", "0" if speech else "1",
                                                 "-ForceConvert", "1" if os.environ.get("ATLAS_TEST_FORCE_CONVERT") else "0"],
                                   timeout=6 * 3600, on_line=on_line)
        rows = _json_lines(out)
        # a recording nothing could open is not a result: nothing is written, it is tried again next time
        opened = not any("error" in r and "frame" not in r and str(r["error"]).startswith(_OPEN_FAILURE) for r in rows)
        result["ran"] = bool(rows) and rc == 0 and opened
        if rc != 0 or not rows:
            notes.append(f"the Windows media step did not finish (exit {rc}): {(err or '').strip()[:200] or 'it printed nothing'}"
                         " - can PowerShell scripts run on this laptop?")
        for r in rows:
            if "error" in r and "frame" not in r:
                notes.append(str(r["error"]) + _open_hint(str(r["error"])))
            if "converting_because" in r:
                notes.append(f"the media editor would not open the recording as it is ({r['converting_because']}): "
                             "it was converted to a plain MP4 first, by the same decoders Media Player uses")
            if "duration" in r:
                result["duration"] = float(r["duration"])
            if "audio_error" in r:
                notes.append("sound track: " + str(r["audio_error"]))
        frames = [(float(r["frame"]), r["path"]) for r in rows if "frame" in r and "path" in r]
        bad = [r for r in rows if "frame" in r and "error" in r]
        if bad:
            notes.append(f"{len(bad)} frame(s) could not be taken")
        size = next((str(r["frame_size"]) for r in rows if "frame_size" in r), "")
        if size:
            notes.append(f"frames read at {size} pixels" + (" - a shared screen in a 720p recording is too small for the "
                                                             "OCR engine to read reliably" if size.endswith("x720") else ""))
        if frames:
            log(f"  reading {len(frames)} frame(s) with the OCR engine")
            texts, warns = ocr.ocr_images([p for _t, p in frames], log=log)
            vocab = vocab or screentext.Vocabulary()
            stats = screentext.Stats()
            checked = {k: screentext.clean_screen(tidy_screen(v), vocab, stats) for k, v in texts.items()}
            if keep:
                keep_frames(video, frames, texts, log, checked)
            got = [(t, checked.get(os.path.normcase(os.path.abspath(p)), "")) for t, p in frames]
            result["screens"] = dedupe_screens(got)
            notes.append(f"screens checked against {'the index' if vocab.from_index else 'keywords only (no --db)'}: "
                         f"{stats}")
            log(f"  {stats}")
            if warns:
                whole = [w for w in warns if w.startswith(("OCR engine", "powershell rc"))]
                notes.append(whole[0] if whole else f"{len(warns)} frame(s) unreadable by the OCR engine")
        wav = next((r["audio"] for r in rows if "audio" in r), None)
        if speech and wav and os.path.isfile(wav):
            state = {"n": 0, "t": time.time(), "secs": 0.0}

            def on_speech(line: str) -> None:
                if '"said"' in line or '"pos"' in line:
                    state["n"] += '"said"' in line
                    try:
                        row = json.loads(line)
                        state["secs"] = float(row.get("at", row.get("pos", state["secs"])))
                    except ValueError:
                        pass
                if time.time() - state["t"] >= 10:
                    state["t"] = time.time()
                    log(f"  ... speech: {state['n']} phrase(s), up to {hms(state['secs'])} of {hms(float(result['duration']))}")

            log(f"  reading the sound track ({hms(float(result['duration']))}) with the Windows speech recogniser")
            rc, out, err = ocr._run_ps(_PS_SPEECH, ["-Wav", wav], timeout=6 * 3600, on_line=on_speech)
            srows = _json_lines(out)
            if rc != 0 or not any("done" in r or "error" in r for r in srows):
                notes.append(f"the speech step did not finish (exit {rc}): {(err or '').strip()[:200] or 'it printed nothing'}"
                             + (" - the transcript may be missing the end of the speech" if srows else ""))
            for r in srows:
                if "error" in r:
                    notes.append("speech: " + str(r["error"]))
                if "recognizer" in r:
                    notes.append(f"speech recogniser: {r['recognizer']} - its lines are UNRELIABLE on real meeting "
                                 "audio (several voices, room noise, accents, mainframe words come back as fluent "
                                 "sentences nobody said); the meeting's own caption file beside the recording "
                                 "replaces it")
            result["speech"] = [(float(r["at"]), str(r["said"])) for r in srows if "said" in r]
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if was_online:
            _dehydrate(video)


def process(video: str, every: float = EVERY_SECONDS, screens: bool = True, speech: bool = True,
            refresh: bool = False, log=print, side: Optional[str] = None, captions: bool = True,
            keep: bool = False, vocab: Optional[screentext.Vocabulary] = None) -> Optional[str]:
    """Write the transcript (`side`, default beside the video); return its
    path, or None when it is current or nothing in the video was readable.
    `speech`: try the Windows recogniser when there is no caption file."""
    side = side or sidecar_of(video)
    if not refresh and is_current(video, side):
        log(f"  up to date: {side}")
        return None
    t0 = time.time()
    r = read_video(video, every, screens, speech, log, captions=captions, keep=keep, vocab=vocab)
    screens_l: List[Tuple[float, str]] = r["screens"]                     # type: ignore[assignment]
    speech_l: List[Tuple[float, str]] = r["speech"]                       # type: ignore[assignment]
    notes: List[str] = r["notes"]                                         # type: ignore[assignment]
    why = "; ".join(notes) or "no text on screen, no speech"
    if not screens_l and not speech_l and not r.get("ran"):
        log(f"  NOT READ {os.path.basename(video)}: {why}")
        return None                         # nothing written: it is tried again next time
    secs = sections(screens_l, speech_l, str(r["speech_source"]))
    title = f"Video {os.path.basename(video)}"
    intro = [f"{MARK} on {time.strftime('%Y-%m-%d %H:%M')} from {os.path.basename(video)} "
             f"({hms(float(r['duration']))}): a frame every {int(every)} s read by OCR (SCREEN)"
             + (", and what was said from the meeting's caption file (SAID)" if 'captions' in str(r['speech_source'])
                else (", and the sound track through the Windows speech recogniser - UNRELIABLE" if speech_l else
                      "; the speech was not transcribed")) + ".",
             "This is what was shown and said, not a fact about what runs: OCR confuses 0/O and 1/I, and speech "
             "recognition garbles jargon - quote it as such, and check anything important against the source."]
    intro += [f"Note: {n}" for n in notes]
    if not secs:
        intro.append(f"Nothing readable: {why}")
    os.makedirs(os.path.dirname(side) or ".", exist_ok=True)
    write_docx(side, title, intro, secs)
    if not secs:
        log(f"  nothing readable in {os.path.basename(video)}: {why} - written down in {os.path.basename(side)} so it "
            "is not read again (--refresh reads it again)")
    else:
        log(f"  wrote {side}: {len(screens_l)} distinct screen(s), {len(speech_l)} spoken phrase(s), "
            f"{len(secs)} section(s), {int(time.time() - t0)} s")
    return side


def sweep_leftovers(log=print, older_than: float = 6 * 3600) -> int:
    """Work folders a killed run left in %TEMP% (VS Code 'Terminate Task', a
    shutdown: the `finally` in read_video never ran). Only folders untouched
    for six hours: a run still going writes into its folder well within that."""
    tmp = tempfile.gettempdir()
    n = 0
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(tmp, name)
        try:
            if not (name.startswith("atlas-video-") and os.path.isdir(path)
                    and time.time() - os.path.getmtime(path) > older_than):
                continue
            shutil.rmtree(path, ignore_errors=True)
            n += 1
        except OSError:
            continue
    if n:
        log(f"  removed {n} work folder(s) an earlier run left in {tmp}")
    return n


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Turn recorded sessions into documents the build indexes: screens by OCR, "
                                             "speech by the Windows recogniser (or a caption file beside the video).")
    ap.add_argument("folders", nargs="+", help="folders (searched with their subfolders) or video files")
    ap.add_argument("--out", metavar="FOLDER", help="write the transcripts here (same subfolders) instead of beside "
                                                    "the videos - a folder the build reads")
    ap.add_argument("--every", type=float, default=EVERY_SECONDS, metavar="SECONDS",
                    help=f"seconds between the frames read (default {EVERY_SECONDS}, at least 1)")
    ap.add_argument("--speech-recogniser", action="store_true",
                    help="without a caption file, try the speech recogniser Windows ships - it reads synthetic speech "
                         "well and real meetings badly; its lines are labelled unreliable")
    ap.add_argument("--no-speech", action="store_true", help="screens only, even with a caption file")
    ap.add_argument("--no-screens", action="store_true", help="speech only")
    ap.add_argument("--refresh", action="store_true", help="read again even when the transcript is newer than the video")
    ap.add_argument("--db", default="atlas.db", metavar="ATLAS.DB",
                    help="the index whose names the screens are checked against (default atlas.db when it is here; "
                         "without one, keywords and plain words only)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be read")
    ap.add_argument("--keep-frames", action="store_true",
                    help="save a dozen frames beside each recording as pictures, each with the text the OCR engine read "
                         "from it - to see what the toolkit saw")
    a = ap.parse_args(argv)
    say = lambda s: print(s, flush=True)                                  # noqa: E731
    if a.every < 1:
        ap.error("--every must be at least 1 second")
    missing = [f for f in a.folders if not os.path.exists(f)]
    if missing:
        for f in missing:
            say(f"not found: {f}")
        say("type the folder as Explorer shows it - no quotes inside the name, no backslash at the end")
        return 2
    sweep_leftovers(say)
    vocab = None
    if a.db and os.path.isfile(a.db):
        vocab = screentext.Vocabulary.from_db(a.db, log=say)
    elif a.db and a.db != "atlas.db":
        say(f"not found: {a.db} - screens are checked against keywords and plain words only")
        return 2
    else:
        say("  (no atlas.db here: screens are checked against keywords and plain words only - run from the folder "
            "holding the index, or give --db, so misread names can be matched to the estate's)")
    videos = find_videos(a.folders)
    say(f"{len(videos)} recording(s) or caption file(s) found"
        + (f"; transcripts go to {os.path.abspath(a.out)}" if a.out else "; transcripts go beside them"))
    if not a.out and not a.dry_run and videos:
        say("  (no --out: the transcripts go beside the recordings - that folder must be one the build reads, and "
            "the recordings themselves should not be in a folder the build reads)")
    if a.dry_run:
        for root, v in videos:
            side = transcript_path(v, root, a.out)
            say(f"  {'up to date' if is_current(v, side) else 'to read   '}  {v}  "
                f"({os.path.getsize(v) // 1024 // 1024} MB{', captions beside it' if captions_beside(v) else ''}"
                f"{', ONLINE-ONLY: downloaded one at a time and freed again' if online_only(v) else ''})"
                f"  ->  {side}")
        return 0
    written = failed = 0
    for k, (root, v) in enumerate(videos, 1):
        say(f"{time.strftime('%H:%M:%S')}  [{k}/{len(videos)}] {v} ({os.path.getsize(v) // 1024 // 1024} MB)")
        try:
            if process(v, a.every, not a.no_screens, a.speech_recogniser and not a.no_speech, a.refresh, say,
                       transcript_path(v, root, a.out), captions=not a.no_speech, keep=a.keep_frames, vocab=vocab):
                written += 1
        except KeyboardInterrupt:
            say("stopped by Ctrl+C - transcripts already written are kept; run the same command again to continue")
            return 130
        except Exception as e:                                            # noqa: BLE001 - one bad video never stops the rest
            failed += 1
            say(f"  FAILED {os.path.basename(v)}: {type(e).__name__}: {e}")
    say(f"{written} transcript(s) written" + (f", {failed} FAILED" if failed else "") + ". Next: run your usual build "
        "command (the same one as always, with its --also folders) so they are indexed; then `docs TERM` or "
        "`doc NAME` finds them.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
