# Cashify device quote automation

## Version 1.4.1: a URL file doesn't have to be a clean list anymore

If a `.txt` file (or a single pasted line) has other text around a URL — most commonly a
numbered item copied straight from this script's own confirmation screen
(`12. https://www.cashify.in/...`), but also headers like `27 URL(s) queued:` or even a
stray `Run all 27? [Y/n] Y` line that ended up in the file by accident — the URL is now
pulled out of the noise automatically, and a line with no URL in it at all is silently
dropped instead of being queued as something that could only ever fail:

```text
Paste the (1) Cashify device URL: C:\Users\you\Desktop\process88.txt
  (2 line(s) in process88.txt had no URL in them and were ignored.)
  Loaded 3 URL(s) from process88.txt (3 queued so far).
```

This applies everywhere a URL comes from: `--urls-file`, dropping a file at the
interactive prompt, a single line pasted directly, and URLs given on the command line.

## Version 1.4.0: pace a long batch out instead of hammering the browser/site non-stop

Long batches (20+ devices, each one 50-150+ page interactions) were showing two related
symptoms: the browser itself crashing on almost every device (`Failed to open a new tab`,
`Target page, context or browser has been closed`) from running one unbroken Chromium
session for hours, and — separately — every device after the first one or two failing at
the exact same final step with zero recognizable page content (`Page did not stabilize at
quote. Observed options: []`), which is the signature of Cashify's own rate-limiting or
bot-detection responding to a sustained burst of automated requests from one session.

Two new options pace things out instead of firing requests as fast as possible:

- **`--delay SECONDS`** (default **5**) — a real pause between devices. `--delay 0` to
  disable.
- **`--recycle-every N`** (default **8**) — proactively closes and relaunches the browser
  every N devices even when nothing has crashed, instead of only reacting to a crash after
  it already happened, since a long-lived session is what accumulates the memory/handle
  pressure behind those crashes in the first place. `--recycle-every 0` to disable.

```bash
.venv/bin/python cashify_runner.py --urls-file devices.txt --delay 8 --recycle-every 5
```

This can reduce the browser-crash half of the problem, but it **cannot** and does not try
to get past Cashify's own throttling if that's what's actually happening — if the site
itself has started blocking a session, pacing requests out (and running smaller batches,
with real breaks between them) is the legitimate fix; there is no flag here, and there
won't be one, that tries to disguise automated traffic or evade a site's own rate-limiting.
If a batch keeps failing at the exact same step across runs, that's the strongest sign it's
the site, not the script — wait before trying again rather than retrying immediately.

## Version 1.3.0: a batch report, even if you abort the run partway through

After every device — and the moment the batch finishes, errors out, or is interrupted —
a report is written to `runs/Batch_Report.txt` (human-readable), `runs/Batch_Report.csv`,
and `runs/Batch_Report.json` (for scripts/spreadsheets), listing **every URL you queued**
under one of three categories:

- **100% Successful** — every planned case was verified, no error.
- **Partially Successful** — some cases were verified before it stopped (site block,
  changed control, browser crash, you pressing Ctrl+C mid-device, etc.) — shows `X/Y cases
  verified` plus the reason it stopped.
- **Failed** — zero cases verified: an invalid URL, or the device never got its turn before
  the batch ended.

```text
  1. [100% Successful] https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-note-6-pro
     12/12 cases verified
     C:\...\runs\used-xiaomi-redmi-note-6-pro
  2. [Partially Successful] https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-mi-a2
     5/12 cases verified — KeyboardInterrupt
     C:\...\runs\used-xiaomi-mi-a2
  3. [Failed] https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-6
     no cases planned yet — Not reached — the batch stopped before this device started
     C:\...\runs\used-xiaomi-redmi-6
----------------------------------------------------------------------
Totals (3 device(s)): 1 fully successful, 1 partially successful, 1 failed.
```

**This is saved even if you abort the run.** The report is rebuilt and re-saved after
every single device finishes, reading each device's own progress straight off its
`checkpoint.json` on disk — not from memory — so:

- Pressing **Ctrl+C** mid-batch: the report is written one more time, right before the
  interrupt is allowed to stop the program, and the in-flight device's own partial
  progress (whatever cases it had already verified) is captured correctly.
- **Closing the terminal window** (the X button) on Windows: the OS normally kills the
  process immediately with no chance for any cleanup code to run at all. A native
  console-close handler is now registered specifically to catch this case and get one
  more report write in during the brief window Windows allows before the process actually
  exits. This is best-effort — it can't help against a hard kill from Task Manager — but it
  catches the common "I closed the window" case.
- Either way, since the report always rebuilds from each device's own saved `checkpoint.json`
  rather than from in-memory progress, re-running the script later and checking
  `runs/Batch_Report.txt` will always reflect the true state of every device, even for ones
  that were mid-run when things stopped.

## Version 1.2.0: drop a whole .txt file of URLs straight into the prompt

At any numbered prompt (`Paste the (N) Cashify device URL:`), you can now paste or drag in
the path to a `.txt` file (one URL per line, `#` comments ignored) instead of a single URL,
and every URL in it is loaded and queued at once:

```text
Paste the (1) Cashify device URL: C:\Users\you\Downloads\Cashify_Xiaomi_84_Phone_Links.txt
  Loaded 84 URL(s) from Cashify_Xiaomi_84_Phone_Links.txt (84 queued so far).
Paste the (85) Cashify device URL:
```

This works by dragging the file onto the Command Prompt/terminal window (which types its
full path for you, quotes included) or by typing/pasting the path yourself. You can keep
going afterward — paste more individual URLs, or drop a second file — before pressing Enter
on a blank line to see the full list and confirm. A URL that's already queued (e.g. loaded
from two files, or pasted once and also present in a dropped file) is only kept once. This
is the same loading logic `--urls-file` already used, just available mid-prompt too.

## Version 1.1.5: tolerate a stray blank line or invisible characters in a pasted URL

If you paste a URL that has a blank line above it, the terminal delivers that as its own
separate Enter press before the real URL line arrives. Previously any blank line ended the
batch immediately, so the real URL right after it was never read. Now a single blank line
before you've queued anything is skipped and it just asks for `(1)` again; a second blank in
a row with nothing queued yet is still treated as a deliberate cancel, so this can't hang
waiting forever. Pasted lines are also cleaned of invisible characters some clipboards leave
behind (a leading byte-order mark, zero-width spaces) in addition to ordinary and
non-breaking spaces, across all three ways of giving it URLs (the interactive prompt,
`--urls-file`, and URLs given directly on the command line).

## Version 1.1.4: fix a Windows-only "charmap codec" crash on resumed devices

If a device's output folder already had a saved `checkpoint.json` from an earlier run
(from resuming, or from retrying after a browser crash), reading it back on Windows could
fail with `'charmap' codec can't decode byte ...: character maps to <undefined>`. The file
was always saved correctly as UTF-8, but reading it back left the encoding unspecified,
which falls back to Windows' default codepage (cp1252) — and that codepage can't represent
the ₹ symbol, em dashes, or curly quotes that Cashify's own page text contains. Both the
checkpoint and `--rules` file reads now explicitly use UTF-8, matching how they're written.

This also fixed a side effect from 1.1.3's bad-URL handling: `UnicodeDecodeError` is itself
a subclass of `ValueError`, so the "skip malformed URLs instead of aborting the batch" fix
was accidentally catching this unrelated crash too and misreporting it as a bad URL. URL
format is now checked up front, before any browser work starts, so it can no longer catch
unrelated errors that happen to also be `ValueError`s.

## Version 1.1.3: one bad URL no longer aborts the whole batch

Every URL used to be checked (`https://www.cashify.in/sell-old-mobile-phone/used-...`)
**before** any device ran, so a single typo'd URL anywhere in the queue — for example
`http://cashify.in/...` missing the `s` and the `www.` — stopped the entire batch before
device #1 even started, even if every other URL was fine. Each URL is now checked only
when its own turn comes up: a bad one is skipped with a clear reason and the batch
carries on to the next queued URL, instead of wasting a browser relaunch/retry on it
(which can't fix a malformed URL anyway) or losing the rest of the run.

## Version 1.1.2: recover from a mid-batch browser crash

If the shared browser crashes or gets closed partway through a batch — for example
`Protocol error (Target.createTarget): Failed to open a new tab`, which happens when
Chromium itself has died or been closed rather than anything about the device's quote
flow — the runner now relaunches the browser and retries that one device automatically
before giving up on it. It also closes any stray extra tab left open by a device (an
unclosed login popup, image preview, etc.) before moving to the next one, since those
can accumulate and destabilize a long-running browser session. Cleaning up the browser
at the very end of the batch no longer produces a confusing second "STOPPED" message if
the browser had already crashed — that failure is now silent, since the real per-device
result was already recorded.

## Version 1.1.1: confirm the queue before it runs

The interactive prompt now shows the full numbered list of URLs you pasted and asks
`Run all N? [Y/n]` before opening the browser. It also flags on the spot if a URL you just
pasted is already in the list. This catches the common spreadsheet-paste mistake where a
fill-handle dragged past the last real row repeats the last value for every extra row —
you'll see the real count and can answer `n` to cancel before anything runs, instead of only
discovering the extra rows after dozens of prompts.

## Version 1.1.0: back-to-back batch runs

You can now queue several device URLs and the runner works through them one after another
in the **same** browser window, without you re-launching the script or signing in again for
each device. There are three ways to queue a batch:

- **Interactive (matches the old single-URL prompt, just repeated):** run the script with no
  URL, and it will ask `Paste the (1) Cashify device URL:`, then `(2)`, `(3)`, and so on.
  Press Enter on an empty line once you're done pasting, and it runs every one of them
  back-to-back:

  ```text
  Paste the (1) Cashify device URL: https://www.cashify.in/sell-old-mobile-phone/used-Samsung-Galaxy-s9-plus
  Paste the (2) Cashify device URL: https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9
  Paste the (3) Cashify device URL: https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s8-plus
  Paste the (4) Cashify device URL:
  ```

- **Command line:** list several URLs after the script name:

  ```bash
  .venv/bin/python cashify_runner.py "URL_1" "URL_2" "URL_3"
  ```

- **A text file:** put one URL per line (blank lines and `#` comments are ignored) and pass
  `--urls-file`:

  ```bash
  .venv/bin/python cashify_runner.py --urls-file devices.txt
  ```

  `--urls-file` entries are appended after any URLs given directly on the command line, so
  you can combine both.

Each device still gets its own `runs/<device-slug>` output folder and its own checkpoint/lock,
exactly as before — only the browser session (and therefore any Cashify sign-in) is shared
across the whole batch. `--output` only makes sense for a single URL, so it's rejected if you
pass more than one URL at once.

If one device in the batch stops (an unrecognized question, a changed control, a site
challenge, etc.), that device's checkpoint and diagnostic are saved exactly as before, the
error is printed, and the runner **moves on to the next queued URL** instead of requiring you
to restart the whole batch. A final summary line per device (`done`, or the stop reason) is
printed at the end, and the exit code is non-zero if any device in the batch didn't finish
cleanly. Pressing Ctrl+C still stops the batch immediately, preserving every device's
checkpoint up to that point.

## Version 1.0.1: discovery and Windows setup update

If you already installed dependencies successfully, extract this update elsewhere and copy
`cashify_runner.py`, `start_windows.bat` and `setup_windows.bat` into your existing automation
folder, replacing only those files. Keep your existing `.venv`, `runs` and browser profile.
Open `start_windows.bat` and paste the same URL. No reinstall is needed for this code update.

This update waits for stable active-panel headings/options before inspecting a page, rather
than reading immediately during a transition. Physical-damage routes may omit a subset of
baseline functional checks; those exact options are logged per route without overwriting
the baseline. Unfamiliar functional options and changes on the baseline route still stop
for review. Discovery progress and calibration observations are now checkpointed.

If a stop persists, send `runs/<device>/diagnostic.json`: it contains the rendered panel,
option labels and original exception. Review it before sharing because website text may
include your signed-in account name. The precise cause of a past discovery error cannot be
confirmed without that run's option lists. This patch passed 34 offline tests; it has not
been live-reproduced in your Windows browser.

Windows setup now reuses an existing compatible environment, or explicitly chooses standard
Python 3.13/3.12. It rejects free-threaded environments and requests binary wheels to avoid
the previous greenlet compiler-installation failure.

Paste a model URL. The runner discovers the available storage/RAM variants, inspects each variant's questionnaire, creates the applicable individual-defect plan, obtains quotes through the visible website, and exports a ZIP. This is editable standalone Python + Playwright source implementing the same audit method used in the supplied reports; it does not depend on ChatGPT browser tools or an OpenAI API key.

## Windows: simplest start

1. Install Python **3.10 or newer** from https://www.python.org/downloads/ and enable **Add Python to PATH**.
2. Extract the entire ZIP into a folder. Double-click `setup_windows.bat` once.
3. Double-click `start_windows.bat`.
4. Paste a Cashify device URL when prompted `Paste the (1) Cashify device URL:`, press Enter,
   then paste another at `(2)`, and so on for as many devices as you want to run back-to-back.
   Press Enter on an empty line once you've pasted the last one, for example:

   ```text
   Paste the (1) Cashify device URL: https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s23-fe-5g
   Paste the (2) Cashify device URL: https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s24-5g
   Paste the (3) Cashify device URL:
   ```

5. The browser opens once and works through the devices in order. If sign-in is required, complete it yourself in that browser and press Enter in the terminal when prompted — this only needs to happen once per batch. The separate local browser profile keeps that session for later runs.

Results appear under `runs/<device-name>/Cashify_Test_Results.zip`. Each verified quote is saved immediately. To resume, run the same URL again. To collect new prices instead of resuming older observations, use a new output folder.

## Terminal commands

```bash
python -m venv .venv
# Windows:
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python cashify_runner.py "https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s23-fe-5g"
```

On macOS/Linux, run `bash setup_mac_linux.sh`, then:

```bash
.venv/bin/python cashify_runner.py
# Or provide one or more URLs directly — they run back-to-back in one browser session:
.venv/bin/python cashify_runner.py "URL_1" "URL_2" "URL_3"
# Or queue a batch from a file, one URL per line:
.venv/bin/python cashify_runner.py --urls-file devices.txt
# Fresh dated run of a single device, keeping the original observations:
.venv/bin/python cashify_runner.py "DEVICE_URL" --output runs/new-price-audit
# Use explicit branch/selector overrides:
.venv/bin/python cashify_runner.py "DEVICE_URL" --rules rules.example.json
```

Linux may also require Playwright's system dependencies: `python -m playwright install --with-deps chromium`. Run this yourself with the appropriate administrator permissions if installation reports missing dependencies.

## What gets tested

- Flawless baseline for each discovered variant.
- Every offered functional defect independently.
- Every physical-defect option independently, with healthy companion subquestions selected to isolate it.
- Healthy display/body/panel controls where explicit healthy choices exist.
- Each opening Yes/No question changed to No, independently.
- Each offered accessory absent, independently; an additional all-accessories-absent case if multiple accessories are offered.
- Each additional age band, only when the flawless route actually offers age.
- Final flawless recheck: all numeric quote fields must match the starting baseline.

Applicability is discovered per variant. For the previously observed questionnaires this yields 44 cases per S23 FE variant, 49 per S24 FE variant, and 51 per S24 variant. These counts are **examples**, not assumptions or hardcoded prices. Fold hinge/outer-screen blocks are recognized when their exact observed healthy labels are present. Current website questions may differ.

“Full” means this individual-defect matrix. It does **not** mean every permutation of multiple simultaneous defects, every postcode/payment/promo, pickup booking, a physical inspection, or hardware diagnosis. Samsung's generic offered labels are tested as displayed even where they do not describe the actual phone hardware. Warranty Yes plus Above 11 months exercises the offered age option; it does not establish warranty eligibility.

## Output and exact deductions

`Cashify_Test_Results.zip` contains:

- `full_test_results.txt`: complete submitted answers, options, unasked branches, quote amounts and deductions.
- `results.csv`: one row per case; pending and no-offer outcomes remain distinct.
- `results.json`: structured case summary.
- `checkpoint.json`: complete plans, model identities, actual answer checks, timestamps, rendered quote text and final controls.

The calculation is:

```text
observed deduction = initial flawless Selling price − case Selling price
Base Price − IMEI Validation Fee = Selling price
Selling price − Processing Fee = Total Amount
```

Fee values are parsed from each quote, not fixed at ₹20/₹99. No monetary offer is recorded as `no_offer` with null monetary fields, never zero. Gift-card offers/coupons are excluded. Individual reductions must not be assumed additive. Final inspection may change the offer.

A ZIP is marked **COMPLETE** only when all discovered variants have every planned case and matching final controls. If a final or healthy-branch control changes, the runner stops and preserves the actual observations. Use a fresh output directory to repeat the whole audit consistently.

## Reliability and scope

- Uses Playwright's normal browser and visible DOM. No private pricing API, stealth plugin, proxy rotation or CAPTCHA solver.
- Sequential browser actions, bounded waits, one optional transient retry, and one checkpoint per successful quote. Skips completed cases on resume.
- Unknown options, missing amounts, unverifiable selections, unexpected questions, model mismatches and fee arithmetic errors stop the run. An incomplete report is still exported.
- Branch questions are partitioned by their explicit healthy options. `rules.example.json` shows how to supply exact option blocks when labels change. A new kind of page, such as a new battery-health or regional eligibility questionnaire, needs a small adapter in `UI.kind()` / `UI.run_case()` before it can be safely supported. Universal compatibility with future Cashify changes is not claimed.
- Site challenges or access restrictions stop the run without bypass attempts. Login is performed by you in the local visible browser. Do not run multiple processes against the same browser profile or output directory.
- A lock file protects each run folder. After an abnormal crash, close the browser/process before removing `runs/<device>/.running.lock` and resuming.
- The persistent profile can contain login cookies: keep `.cashify-browser/` private and out of Git. Reports contain rendered website text and may contain the signed-in account name. Do not publish your private session or reports unintentionally.
- Only `Get Exact Value`, questionnaire answer cards and `Continue` are clicked. The runner never clicks `Sell Now` or enters order, address, bank or IMEI data.

## Validation included

```bash
python -m unittest discover -s tests -v
python -m py_compile cashify_runner.py
```

Automated offline tests cover case-plan applicability, healthy companion selections, fold branches, unknown-option rejection, exact fee arithmetic, no-offer handling, atomic checkpoints, partial/full report status and resume data. They do not claim a live end-to-end run of this new standalone package. Start with one live model pilot in your environment and inspect its first quotes before relying on unattended operation.

Official setup reference: https://playwright.dev/python/docs/library
Persistent browser session reference: https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context
