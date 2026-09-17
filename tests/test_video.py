r"""
Recorded sessions (LESSONS 151): a knowledge-transfer video holds what nobody
wrote down. `atlas.video` reads the screens (a frame every few seconds, by
the Windows OCR engine) and the speech (the Windows recogniser, or a caption
file beside the video) into a Word transcript the build indexes as a
document - so nothing in the parsers changes. The pure parts run anywhere;
the end-to-end part makes a real video and runs only where Windows media,
OCR and speech exist.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from unittest import mock

from atlas import build, docs, ocr, query, verify_citations, video  # noqa: E402

VTT = """WEBVTT

NOTE exported from the meeting

1
00:00:01.000 --> 00:00:04.000
<v Priya Shah>The nightly claims job runs the posting program first.</v>

2
00:02:05.500 --> 00:02:09.000
<v Priya Shah>Note that step ten abends when the input is empty,</v>
<v Priya Shah>so restart from step ten.</v>

3
00:02:10.000 --> 00:02:11.000
10
"""

SRT = """1
00:00:03,250 --> 00:00:05,000
Posting runs at 2 a.m.

2
01:00:00,000 --> 01:00:02,000
Call the on-call analyst.
"""


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class Captions(unittest.TestCase):

    def test_vtt_keeps_speakers_and_times_and_drops_headers_and_cue_numbers(self):
        cues = video.parse_captions(VTT)
        self.assertEqual(cues[0], (1.0, "Priya Shah: The nightly claims job runs the posting program first."))
        self.assertEqual(cues[1][0], 125.5)
        self.assertIn("Note that step ten abends", cues[1][1], "a spoken line starting with 'Note' is speech")
        self.assertIn("so restart from step ten.", cues[1][1], "two lines of one cue are one phrase")
        self.assertEqual(cues[2], (130.0, "10"), "a caption that is only a number is still what was said")
        self.assertEqual(len(cues), 3)
        self.assertFalse(any("exported from" in c[1] or "WEBVTT" in c[1] for c in cues))

    def test_an_empty_cue_does_not_turn_the_next_cue_number_or_note_into_speech(self):
        text = ("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n\n2\n00:00:03.000 --> 00:00:04.000\nreal words\n\n"
                "00:00:05.000 --> 00:00:06.000\n\nNOTE not speech\n")
        self.assertEqual(video.parse_captions(text), [(3.0, "real words")])

    def test_srt_times_with_commas_and_hours(self):
        self.assertEqual(video.parse_captions(SRT), [(3.25, "Posting runs at 2 a.m."), (3600.0, "Call the on-call analyst.")])

    def test_hms(self):
        self.assertEqual(video.hms(0), "00:00:00")
        self.assertEqual(video.hms(125.9), "00:02:05")
        self.assertEqual(video.hms(3725), "01:02:05")


class Screens(unittest.TestCase):

    JCL = "//CLMNIGHT JOB (ACCT),'CLAIMS'\n//STEP010 EXEC PGM=CLMPOST\n//SYSIN DD DSN=PROD.CLAIMS.CARDS,DISP=SHR"

    def test_one_screen_shown_for_a_minute_is_written_once_at_the_time_it_appeared(self):
        frames = [(10.0, self.JCL), (20.0, self.JCL), (30.0, self.JCL.replace("STEP010", "STEPO1O")),
                  (40.0, self.JCL + "  10:42:07")]
        self.assertEqual(video.dedupe_screens(frames), [(10.0, self.JCL)],
                         "an OCR flip or a clock is still the same screen")

    def test_a_typed_line_or_a_new_screen_is_written(self):
        typed = self.JCL + "\n//STEP020 EXEC PGM=CLMEDIT"
        frames = [(0.0, ""), (10.0, self.JCL), (20.0, typed), (30.0, "ISPF PRIMARY OPTION MENU"), (40.0, "   ")]
        got = video.dedupe_screens(frames)
        self.assertEqual([t for t, _ in got], [10.0, 20.0, 30.0], "blank frames are dropped")

    def test_dataset_names_split_by_the_ocr_engine_are_joined_again(self):
        self.assertEqual(video.tidy_screen("DSN=PROD . CLAIMS . DAILY . INPUT,DISP=SHR"),
                         "DSN=PROD.CLAIMS.DAILY.INPUT,DISP=SHR")
        self.assertEqual(video.tidy_screen("IGG0191B,CLMNIGHT .G0001V00"), "IGG0191B,CLMNIGHT.G0001V00")
        for prose in ("The job failed. Rerun it.", "JOB FAILED. RERUN IT.", "Step 10. Then step 20.",
                      "Total : 5 . end"):
            self.assertEqual(video.tidy_screen(prose), prose, "a sentence is left alone")

    def test_a_few_changed_characters_on_a_full_screen_are_not_told_from_ocr_noise(self):
        screen = "\n".join(f"//DD{n:02d}     DD DSN=PROD.CLAIMS.FILE{n:02d},DISP=SHR" for n in range(24))
        edited = screen.replace("FILE07,DISP=SHR", "FILE07,DISP=OLD")
        self.assertEqual(len(video.dedupe_screens([(0.0, screen), (10.0, edited)])), 1,
                         "documented: a small edit is not written again - the SAID line carries it")
        typed = screen + "\n//DD24     DD DSN=PROD.CLAIMS.NEWFILE,DISP=SHR"
        self.assertEqual(len(video.dedupe_screens([(0.0, screen), (10.0, typed)])), 2, "a new line is a new screen")

    def test_returning_to_an_earlier_screen_is_written_again(self):
        got = video.dedupe_screens([(0.0, "SCREEN A TEXT HERE"), (10.0, "SCREEN B IS DIFFERENT"), (20.0, "SCREEN A TEXT HERE")])
        self.assertEqual(len(got), 3)


class Sections(unittest.TestCase):

    def test_two_minute_windows_in_time_order_with_every_line_stamped_and_prefixed(self):
        screens = [(1.0, "//CLMNIGHT JOB (ACCT)\n       PROGRAM-ID. CLMPOST.")]
        speech = [(0.5, "the nightly job"), (125.0, "restart from step ten")]
        secs = video.sections(screens, speech, "SAID")
        self.assertEqual([h for h, _ in secs], ["00:00:00 - 00:02:00", "00:02:00 - 00:04:00"])
        self.assertEqual(secs[0][1], ["[00:00:00] SAID: the nightly job",
                                      "[00:00:01] SCREEN: //CLMNIGHT JOB (ACCT)",
                                      "[00:00:01] SCREEN: PROGRAM-ID. CLMPOST."])
        self.assertEqual(secs[1][1], ["[00:02:05] SAID: restart from step ten"])
        for _h, lines in secs:
            for ln in lines:
                self.assertRegex(ln, r"^\[\d\d:\d\d:\d\d\] (SAID|SCREEN): ", "no line may start like JCL or COBOL")

    def test_a_silent_gap_leaves_no_empty_sections(self):
        secs = video.sections([], [(10.0, "a"), (1000.0, "b")], "SAID")
        self.assertEqual(len(secs), 2)


class Files(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def touch(self, *parts, data=b"x", when=None):
        p = os.path.join(self.td, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        if when is not None:
            os.utime(p, (when, when))
        return p

    def test_a_teams_download_finds_its_caption_file_by_being_alone_with_it(self):
        v = self.touch("KT", "Weekly KT-20260916_140000-Meeting Recording.mp4")
        self.assertIsNone(video.captions_beside(v))
        cap = self.touch("KT", "Weekly KT.vtt", data=VTT.encode())
        self.assertEqual(video.captions_beside(v), cap, "one recording and one caption file in the folder belong together")
        self.touch("KT", "Other.vtt", data=VTT.encode())
        self.assertIsNone(video.captions_beside(v), "two caption files: the names must match")
        self.assertEqual(video.captions_beside(cap), cap, "a caption file is its own captions")

    def test_a_caption_file_without_its_recording_is_read_on_its_own(self):
        cap = self.touch("KT", "Weekly KT.vtt", data=VTT.encode())
        self.touch("KT", "notes.txt")
        found = video.find_videos([os.path.join(self.td, "KT")])
        self.assertEqual([p for _r, p in found], [cap])
        v = self.touch("KT", "session2.mp4")
        self.touch("KT", "session2.srt", data=SRT.encode())
        found = video.find_videos([os.path.join(self.td, "KT")])
        self.assertEqual([os.path.basename(p) for _r, p in found], ["session2.mp4", "Weekly KT.vtt"],
                         "a caption file beside its recording is not listed twice")
        said = []
        side = video.process(cap, log=said.append)
        self.assertEqual(side, os.path.join(self.td, "KT", "Weekly KT.video.docx"), said)
        d = docs.extract(side)
        text = "\n".join(t for _h, t in d.sections)
        self.assertIn("[00:02:05] SAID (captions): Priya Shah: Note that step ten abends", text)
        self.assertTrue(video.is_current(cap, side))

    def test_a_caption_file_with_no_cues_is_said_so_and_does_not_silence_the_recogniser(self):
        v = self.touch("KT", "s.mp4")
        self.touch("KT", "s.vtt", data="WEBVTT\n\nNOTE nothing here\n".encode())
        r = video.read_video(v, screens=False, speech=False, log=lambda s: None)
        self.assertEqual(r["speech"], [])
        self.assertTrue(any("has no cues that could be read" in n for n in r["notes"]), r["notes"])
        self.assertNotIn("SAID (captions)", str(r["speech_source"]))

    def test_transcript_names_become_clean_document_names(self):
        v = self.touch("KT", "kt-session.mp4")
        self.assertEqual(video.sidecar_of(v), os.path.join(self.td, "KT", "kt-session.video.docx"))
        mov = self.touch("KT", "kt-session.mov")
        self.assertEqual(video.sidecar_of(v), v + ".video.docx", "two recordings with one name keep apart")
        self.assertEqual(video.sidecar_of(mov), mov + ".video.docx")

    def test_out_folder_keeps_the_subfolders(self):
        root = os.path.join(self.td, "Recordings")
        a = self.touch("Recordings", "GC", "session1.mp4")
        b = self.touch("Recordings", "SHARED", "session1.mp4")
        out = os.path.join(self.td, "docs", "Video")
        pa, pb = video.transcript_path(a, root, out), video.transcript_path(b, root, out)
        self.assertEqual(pa, os.path.join(out, "GC", "session1.video.docx"))
        self.assertEqual(pb, os.path.join(out, "SHARED", "session1.video.docx"))
        self.assertEqual(video.transcript_path(a, os.path.join(self.td, "elsewhere"), out),
                         os.path.join(out, "session1.video.docx"))
        self.assertEqual(video.transcript_path(a), video.sidecar_of(a))
        found = video.find_videos([root])
        self.assertEqual([p for _r, p in found], [a, b])
        self.assertEqual(video.find_videos([a]), [(os.path.dirname(a), a)], "a single file works too")

    def test_a_transcript_is_current_until_the_video_or_its_captions_change(self):
        now = time.time()
        v = self.touch("s.mp4", when=now - 100)
        side = video.sidecar_of(v)
        self.assertFalse(video.is_current(v))
        self.touch("s.video.docx", when=now - 50)
        self.assertTrue(video.is_current(v, side))
        self.touch("s.vtt", data=VTT.encode(), when=now - 10)
        self.assertFalse(video.is_current(v, side), "captions added later: read again")
        os.utime(side, (now, now))
        self.assertTrue(video.is_current(v, side))
        os.utime(v, (now + 5, now + 5))
        self.assertFalse(video.is_current(v, side), "a newer recording: read again")

    def test_captions_alone_make_a_transcript_without_opening_the_video(self):
        v = self.touch("Recordings", "kt-session.mp4", data=b"not really a video")
        self.touch("Recordings", "kt-session.vtt", data=VTT.encode("utf-8"))
        out = os.path.join(self.td, "docs")
        side = video.transcript_path(v, os.path.join(self.td, "Recordings"), out)
        said = []
        got = video.process(v, screens=False, log=said.append, side=side)
        self.assertEqual(got, side, said)
        d = docs.extract(side)
        self.assertEqual(d.title, "Video kt-session.mp4")
        heads = [h for h, _t in d.sections]
        self.assertIn("00:02:00 - 00:04:00", heads)
        text = "\n".join(t for _h, t in d.sections)
        self.assertIn("[00:02:05] SAID (captions): Priya Shah: Note that step ten abends", text)
        self.assertIn("not a fact about what runs", text, "every transcript says what it is")
        self.assertIsNone(video.process(v, screens=False, log=said.append, side=side), "current: not read again")

    def test_nothing_readable_is_written_down_once_but_a_step_that_never_ran_is_retried(self):
        v = self.touch("empty.mp4", data=b"")
        said = []
        side = video.process(v, screens=False, speech=False, log=said.append)
        self.assertEqual(side, video.sidecar_of(v), said)
        d = docs.extract(side)
        self.assertIn("Nothing readable: no text on screen, no speech", "\n".join(t for _h, t in d.sections))
        self.assertIsNone(video.process(v, screens=False, speech=False, log=said.append), "current: not read again")
        os.remove(side)
        with open(v, "wb") as fh:
            fh.write(b"not a real recording, but not empty either")      # an empty file has its own explanation now
        with mock.patch.object(ocr, "ocr_available", return_value=(True, "test")), \
             mock.patch.object(ocr, "_run_ps", return_value=(1, "policy says no\n", "policy says no")):
            self.assertIsNone(video.process(v, log=said.append), said)
        self.assertFalse(os.path.exists(side), "the media step did not run: nothing written, tried again next time")
        self.assertTrue(any("NOT READ empty.mp4: the Windows media step did not finish (exit 1): policy says no" in s
                            for s in said), said)

    def test_an_online_only_onedrive_recording_is_downloaded_read_and_freed_again(self):
        v = self.touch("cloud.mp4", data=b"placeholder bytes" * 1000)
        said = []
        refused = '{"error":"cannot open the video: The parameter is incorrect."}\n'
        with mock.patch.object(video, "online_only", return_value=True), \
             mock.patch.object(video, "_dehydrate") as free, \
             mock.patch.object(ocr, "ocr_available", return_value=(True, "test")), \
             mock.patch.object(ocr, "_run_ps", return_value=(0, refused, "")) as ps:
            self.assertIsNone(video.process(v, log=said.append), said)
        self.assertTrue(ps.called, "downloaded, then handed to the media step")
        free.assert_called_once_with(v)
        self.assertTrue(any("downloading from OneDrive" in s for s in said), said)
        # a download that fails is said, the space is not touched, and the recording is tried again next time
        said = []
        with mock.patch.object(video, "online_only", return_value=True), \
             mock.patch.object(video, "_hydrate", return_value=False), \
             mock.patch.object(video, "_dehydrate") as free, \
             mock.patch.object(ocr, "ocr_available", return_value=(True, "test")), \
             mock.patch.object(ocr, "_run_ps") as ps:
            self.assertIsNone(video.process(v, log=said.append), said)
        self.assertFalse(ps.called)
        self.assertFalse(free.called)
        self.assertTrue(any("could not download it from OneDrive" in s for s in said), said)
        self.assertFalse(os.path.exists(video.sidecar_of(v)))
        out = io.StringIO()
        with mock.patch.object(video, "online_only", return_value=True), contextlib.redirect_stdout(out):
            video.main([os.path.dirname(v), "--dry-run"])
        self.assertIn("ONLINE-ONLY: downloaded one at a time and freed again", out.getvalue())
        self.assertFalse(video.online_only(v), "a real local file is not a placeholder")
        self.assertTrue(video._hydrate(v, log=lambda s: None), "reading a local file through is a no-op download")

    def test_a_recording_nothing_could_open_is_not_written_down_and_is_tried_again(self):
        v = self.touch("locked.mp4", data=b"not a real recording, but not empty either")
        said = []
        refused = '{"error":"cannot open the video: The parameter is incorrect."}\n'
        with mock.patch.object(ocr, "ocr_available", return_value=(True, "test")), \
             mock.patch.object(ocr, "_run_ps", return_value=(0, refused, "")):
            self.assertIsNone(video.process(v, log=said.append), said)
        self.assertFalse(os.path.exists(video.sidecar_of(v)), "an open failure is not a result")
        self.assertTrue(any("NOT READ locked.mp4: cannot open the video" in s for s in said), said)
        # a transcript an earlier toolkit wrote for such a failure does not count as done either
        side = video.sidecar_of(v)
        video.write_docx(side, "Video locked.mp4", ["written by atlas.video", "Nothing readable: cannot open the video: x"], [])
        self.assertFalse(video.is_current(v, side))
        video.write_docx(side, "Video locked.mp4", ["written by atlas.video", "Nothing readable: no text on screen, no speech"], [])
        self.assertTrue(video.is_current(v, side), "a recording read in full with nothing in it stays done")

    def test_a_transcript_survives_characters_xml_forbids(self):
        side = os.path.join(self.td, "odd.video.docx")
        video.write_docx(side, "Video odd.mp4", ["note \x1b[0m with \x0c a form feed"],
                         [("00:00:00 - 00:02:00", ["[00:00:01] SCREEN: PGM=CLM\x00POST & <more>"])])
        d = docs.extract(side)
        text = "\n".join(t for _h, t in d.sections)
        self.assertIn("PGM=CLM POST & <more>", text)

    def test_a_mistyped_folder_and_a_bad_interval_are_refused_plainly(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = video.main([os.path.join(self.td, "no such folder")])
        self.assertEqual(rc, 2)
        self.assertIn("not found:", out.getvalue())
        self.assertIn("no backslash at the end", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            video.main([self.td, "--every", "0"])

    def test_dry_run_lists_each_video_and_where_its_transcript_goes(self):
        self.touch("Recordings", "a.mp4")
        self.touch("Recordings", "sub", "b.wmv")
        self.touch("Recordings", "notes.txt")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = video.main([os.path.join(self.td, "Recordings"), "--out", os.path.join(self.td, "docs"), "--dry-run"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("2 recording(s) or caption file(s) found", text)
        self.assertIn(os.path.join(self.td, "docs", "sub", "b.video.docx"), text)
        self.assertNotIn("notes.txt", text)

    def test_the_build_indexes_a_transcript_as_a_document_even_when_it_shows_jcl_and_cobol(self):
        docdir = os.path.join(self.td, "docs")
        os.makedirs(docdir)
        side = os.path.join(docdir, "kt-session.video.docx")
        secs = video.sections([(1.0, "//CLMNIGHT JOB (ACCT),'CLAIMS'\n//STEP010 EXEC PGM=CLMPOST\n"
                                     "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CLMPOST.")],
                              [(2.0, "restart from step ten after fixing the input")], "SAID")
        video.write_docx(side, "Video kt-session.mp4", ["written by atlas.video"], secs)
        estate = os.path.join(self.td, "estate", "SRC")
        os.makedirs(estate)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), estate)
        db = os.path.join(self.td, "t.db")
        quiet(build._main, [os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--also", docdir])
        conn = query.connect(db)
        try:
            kinds = dict(conn.execute("SELECT name, kind FROM member").fetchall())
            self.assertEqual(kinds.get("KT-SESSION.VIDEO"), "doc", kinds)
            self.assertNotIn("CLMNIGHT", kinds, "a job shown on screen is not a job in the estate")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM program WHERE program_id='CLMPOST'").fetchone()[0], 0,
                             "a program shown on screen is not a program in the estate")
            hit = query.cmd_docs(conn, "CLMPOST")
            self.assertIn("KT-SESSION.VIDEO", hit)
            self.assertIn("[00:00:01] SCREEN: //STEP010 EXEC PGM=CLMPOST", hit)
        finally:
            conn.close()
        # and the gate accepts the cite exactly as the report prints it
        res, _u = verify_citations.check_answer('[[KT-SESSION.VIDEO 2 "PGM=CLMPOST"]] [[KT-SESSION.VIDEO:2 "step ten"]]',
                                                db_path=db)
        self.assertEqual([r.status for r in res], ["PASS", "PASS"], [(r.status, r.detail) for r in res])


MAKE_VIDEO = r"""
param([string]$Dir)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Speech
function Slide($path, $a, $b) {
  $bmp = New-Object System.Drawing.Bitmap 1280, 720
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.Clear([System.Drawing.Color]::White)
  $font = New-Object System.Drawing.Font("Consolas", 36)
  $g.DrawString($a, $font, [System.Drawing.Brushes]::Black, 40, 200)
  $g.DrawString($b, $font, [System.Drawing.Brushes]::Black, 40, 280)
  $g.Dispose(); $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
}
Slide (Join-Path $Dir "s1.png") "//CLMNIGHT JOB (ACCT),'CLAIMS'" "//STEP010  EXEC PGM=CLMPOST"
Slide (Join-Path $Dir "s2.png") "RESTART FROM STEP020" "AFTER FIXING CLMEDIT INPUT"
$wav = Join-Path $Dir "narration.wav"
$syn = New-Object System.Speech.Synthesis.SpeechSynthesizer
$syn.SetOutputToWaveFile($wav)
$pb = New-Object System.Speech.Synthesis.PromptBuilder
$pb.AppendBreak([TimeSpan]::FromSeconds(4))
$pb.AppendText("The nightly claims job runs the posting program first.")
$syn.Speak($pb)
$syn.Dispose()
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFolder, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Editing.MediaComposition, Windows.Media.Editing, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Editing.MediaClip, Windows.Media.Editing, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Editing.BackgroundAudioTrack, Windows.Media.Editing, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.MediaProperties.MediaEncodingProfile, Windows.Media.MediaProperties, ContentType = WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' }
$asOp = ($methods | Where-Object { $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asOpProg = ($methods | Where-Object { $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperationWithProgress`2' })[0]
function Await($op, $T) { $t = $asOp.MakeGenericMethod($T).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
function AwaitProg($op, $T, $P) { $t = $asOpProg.MakeGenericMethod($T, $P).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
$comp = New-Object Windows.Media.Editing.MediaComposition
$add = [System.Collections.Generic.ICollection[Windows.Media.Editing.MediaClip]].GetMethod("Add")
foreach ($n in @("s1.png", "s2.png")) {
  $f = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Join-Path $Dir $n))) ([Windows.Storage.StorageFile])
  $clip = Await ([Windows.Media.Editing.MediaClip]::CreateFromImageFileAsync($f, [TimeSpan]::FromSeconds(6))) ([Windows.Media.Editing.MediaClip])
  $add.Invoke($comp.Clips, @($clip)) | Out-Null
}
$wf = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($wav)) ([Windows.Storage.StorageFile])
$track = Await ([Windows.Media.Editing.BackgroundAudioTrack]::CreateFromFileAsync($wf)) ([Windows.Media.Editing.BackgroundAudioTrack])
[System.Collections.Generic.ICollection[Windows.Media.Editing.BackgroundAudioTrack]].GetMethod("Add").Invoke($comp.BackgroundAudioTracks, @($track)) | Out-Null
$folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync($Dir)) ([Windows.Storage.StorageFolder])
$mp4 = Await ($folder.CreateFileAsync("kt-session.mp4", [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])
$prof = [Windows.Media.MediaProperties.MediaEncodingProfile]::CreateMp4([Windows.Media.MediaProperties.VideoEncodingQuality]::HD720p)
AwaitProg ($comp.RenderToFileAsync($mp4, [Windows.Media.Editing.MediaTrimmingPreference]::Precise, $prof)) ([Windows.Media.Transcoding.TranscodeFailureReason]) ([double]) | Out-Null
"""


class EndToEnd(unittest.TestCase):
    """A 12-second video: two slides, and a sentence spoken after 4 s of silence."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.mp4 = os.path.join(cls.td, "kt-session.mp4")
        ok, why = ocr.ocr_available()
        if not ok:
            raise unittest.SkipTest(why)
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
            fh.write(MAKE_VIDEO)
            script = fh.name
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Dir", cls.td],
                           capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            os.remove(script)
        if not (os.path.isfile(cls.mp4) and os.path.getsize(cls.mp4) > 10000):
            shutil.rmtree(cls.td, ignore_errors=True)
            raise unittest.SkipTest("Windows media composition cannot make a test video here")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_a_file_that_is_not_a_video_is_explained_not_just_refused(self):
        fake = os.path.join(self.td, "notes.mp4")
        with open(fake, "wb") as fh:
            fh.write(b"this is a text file with a video extension" * 100)
        said = []
        r = video.read_video(fake, log=said.append)
        self.assertFalse(r.get("ran"), "an open failure is not a result: the file is tried again next time")
        note = "\n".join(r["notes"])
        self.assertIn("cannot open the video", note)
        self.assertIn("Media Player", note, "the next step is spelled out")
        self.assertTrue("Windows" in note, note)
        empty = os.path.join(self.td, "empty.mp4")
        open(empty, "wb").close()
        r = video.read_video(empty, log=said.append)
        self.assertIn("empty file (0 bytes)", "\n".join(r["notes"]))

    def test_a_recording_the_editor_will_not_open_is_converted_first(self):
        said = []
        with mock.patch.dict(os.environ, {"ATLAS_TEST_FORCE_CONVERT": "1"}):
            side = video.process(self.mp4, every=3, log=said.append, refresh=True)
        self.assertIsNotNone(side, said)
        d = docs.extract(side)
        text = "\n".join(t for _h, t in d.sections).upper()
        if "CLMNIGHT" not in text:
            self.skipTest("OCR engine unreadable here: " + " | ".join(said))
        self.assertIn("SCREEN: //CLMNIGHT JOB", text)
        self.assertIn("RESTART FROM STEP020", text)
        self.assertIn("CONVERTED TO A PLAIN MP4 FIRST", text, "the transcript says how it was read")
        self.assertIn("CONVERSION FORCED FOR A TEST", text)

    def test_screens_and_speech_become_a_timed_transcript(self):
        said = []
        side = video.process(self.mp4, every=3, log=said.append, refresh=True)
        self.assertIsNotNone(side, said)
        d = docs.extract(side)
        text = "\n".join(t for _h, t in d.sections).upper()
        if "CLMNIGHT" not in text:
            self.skipTest("OCR engine unreadable here: " + " | ".join(said))
        self.assertIn("SCREEN: //CLMNIGHT JOB", text)
        self.assertIn("PGM=CLMPOST", text)
        self.assertIn("RESTART FROM STEP020", text)
        self.assertEqual(text.count("SCREEN: //CLMNIGHT JOB"), 1, "the first slide shown for 6 s is written once")
        second = [ln for ln in text.splitlines() if "RESTART FROM STEP020" in ln][0]
        self.assertGreaterEqual(int(second[7:9]), 5, "the second slide appears at about 6 s: " + second)
        if "NO SPEECH RECOGNISER" in text:
            return
        spoken = [ln for ln in text.splitlines() if "SAID: THE NIGHTLY CLAIMS JOB" in ln]
        self.assertEqual(len(spoken), 1, "plain speech reads well:\n" + text)
        self.assertGreaterEqual(int(spoken[0][7:9]), 3,
                                "speech is stamped where it was said, not 0:00 (Recognize() in a loop does that): "
                                + spoken[0])


if __name__ == "__main__":
    unittest.main()
