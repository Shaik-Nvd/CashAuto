# Cashify device quote automation

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
