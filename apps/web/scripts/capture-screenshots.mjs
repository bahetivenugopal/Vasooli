/**
 * Capture a full-page screenshot of every control-tower surface.
 *
 *     npm run screenshots            # from apps/web
 *
 * Phase 6 §9 asks for screenshots of each major surface in `docs/pitch/`,
 * captured *before* the video phase so they are not being taken under pressure.
 * They also serve as the visual record of what the smoke checklist asserts.
 *
 * Both servers must be running first — `uvicorn app.main:app` from `apps/api`
 * and `npm run start` (or `npm run dev`) from `apps/web`.
 *
 * Uses `puppeteer-core` against a Chrome already on the machine rather than
 * `puppeteer`, which would download a second ~150MB browser to do the same job.
 * Set `CHROME_PATH` if yours is somewhere unusual.
 *
 * The pages fetch their data on the client, so this waits for the network to go
 * quiet **and** for the loading skeletons to disappear. A screenshot of a
 * skeleton is worse than no screenshot: it looks like the dashboard is broken.
 */

import { existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import puppeteer from "puppeteer-core";

//: The repo root, three levels up from `apps/web/scripts/`.
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const OUT_DIR = join(ROOT, "docs", "pitch", "screenshots");

const WEB = process.env.WEB_BASE_URL ?? "http://127.0.0.1:3000";
const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);

function findChrome() {
  const found = CHROME_CANDIDATES.find((path) => existsSync(path));
  if (!found) {
    throw new Error(
      `No Chrome found. Tried:\n  ${CHROME_CANDIDATES.join("\n  ")}\nSet CHROME_PATH.`,
    );
  }
  return found;
}

async function json(path) {
  const response = await fetch(`${API}/api/v1${path}`);
  if (!response.ok) throw new Error(`GET ${path} -> ${response.status}`);
  return response.json();
}

/**
 * Work out which run and which entities to shoot.
 *
 * Deliberately picks the **unhappy paths** — a broken promise, a
 * compliance-blocked mandate — because §5.4 is explicit that a timeline showing
 * only success is exactly the cherry-picking the competition's bar warns about.
 */
async function plan() {
  const overview = await json("/overview");
  const batches = overview.batch_ids;
  if (!batches.root_cause || !batches.mandate_recovery || !batches.receivables) {
    throw new Error(
      "Need one completed run per engine. Run the three demo scripts in scripts/ first.",
    );
  }

  const detections = await json(`/root-cause/runs/${batches.root_cause}/detections`);
  const broken = await json(`/receivables/runs/${batches.receivables}/promises?status=broken`);
  const blocked = await json(
    `/mandate-recovery/runs/${batches.mandate_recovery}/mandates?compliance_blocked=true`,
  );

  const shots = [
    ["01-overview", "/"],
    ["02-engine-root-cause", "/engines/root-cause"],
    ["03-engine-mandate-recovery", "/engines/mandate-recovery"],
    ["04-engine-receivables", "/engines/receivables"],
    ["05-audit-trail", "/audit"],
  ];

  if (detections.length > 0) {
    shots.push(["06-timeline-corridor", `/timelines/corridor/${detections[0].detection_id}`]);
  }
  if (blocked.length > 0) {
    shots.push([
      "07-timeline-mandate-compliance-blocked",
      `/timelines/mandate/${batches.mandate_recovery}/${blocked[0].mandate_id}`,
    ]);
  }
  if (broken.length > 0) {
    shots.push([
      "08-timeline-invoice-broken-promise",
      `/timelines/invoice/${batches.receivables}/${broken[0].invoice_id}`,
    ]);
  }
  return shots;
}

/**
 * Refuse to save a screenshot of a broken page.
 *
 * The first run of this script quietly produced eight pictures of the "could
 * not reach the API" state, because headless Chrome resolved `localhost` to IPv6
 * while uvicorn was bound to IPv4 only. They looked plausible in a file listing.
 * A screenshot of an error state is worse than a missing one, so the check is
 * here rather than in a reviewer's eyes.
 */
async function assertRendered(page, path) {
  const broken = await page.$$('[role="alert"]');
  if (broken.length > 0) {
    const message = await page.evaluate(
      () => document.querySelector('[role="alert"]')?.textContent ?? "",
    );
    throw new Error(`${path} rendered an error state, not content:
  ${message.trim()}`);
  }
}

async function settle(page) {
  // The skeleton is `role="status"` with an "Loading" label. Waiting for it to
  // go is more reliable than a fixed sleep and much faster on a warm cache.
  await page
    .waitForFunction(() => document.querySelectorAll('[aria-label="Loading"]').length === 0, {
      timeout: 20_000,
    })
    .catch(() => {});
  // Recharts animates in; give it a beat so bars are at full height.
  await new Promise((done) => setTimeout(done, 1200));
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const shots = await plan();

  const browser = await puppeteer.launch({
    executablePath: findChrome(),
    headless: "new",
    args: ["--no-sandbox", "--hide-scrollbars"],
  });
  const page = await browser.newPage();
  // 1.5x rather than 2x: these are committed to the repo and feed a 1080p video,
  // where 2x doubled the file size for detail nothing downstream can show.
  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1.5 });

  for (const [name, path] of shots) {
    const url = `${WEB}${path}`;
    await page.goto(url, { waitUntil: "networkidle0", timeout: 60_000 });
    await settle(page);
    await assertRendered(page, path);
    const file = join(OUT_DIR, `${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    console.log(`${name.padEnd(42)} <- ${path}`);
  }

  await browser.close();
  console.log(`\n${shots.length} screenshots in docs/pitch/screenshots/`);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
