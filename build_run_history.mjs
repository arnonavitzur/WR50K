#!/usr/bin/env node
/**
 * build_run_history.mjs — fold the Garmin FIT files that Google Drive mirrors
 * onto this Mac into run_history.json, one compact row per run, so the app's
 * Trends view can plot GAP-vs-HR (Efficiency Factor) over time without ever
 * re-parsing a FIT file on the phone.
 *
 *   node build_run_history.mjs --dry-run          # what would be parsed
 *   node build_run_history.mjs                    # update run_history.json
 *   node build_run_history.mjs --push             # ...and commit + push it
 *   node build_run_history.mjs --force            # re-parse everything
 *
 * Source folder: ~/Google Drive/Health/Garmin (filled by com.arnon.garmin-pull,
 * files named YYYYMMDD_<garminActivityId>.fit). Override with --src.
 *
 * The numbers come from fit-analysis.js — the very file the app runs — loaded
 * into a Node vm context, so a row here equals what the in-app analysis would
 * show for the same file. This script never modifies fit-analysis.js; it only
 * reads its output. The one thing it reads directly from the FIT is the Garmin
 * activity type (session.sport / sub_sport), which the analysis does not expose:
 * sub_sport generic -> road, trail -> trail, treadmill -> treadmill.
 *
 * Idempotent: a file is parsed only if its id is not yet in `runs` or `skipped`,
 * or if the row was produced by an older parser_version. Rows whose file has
 * since disappeared from the folder are kept. Only a changed result is written,
 * and --push commits/pushes only when the file changed. launchd
 * (com.arnon.run-history) runs this with --push whenever the folder changes.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const M_PER_MI = 1609.344;
const SCHEMA = 1;
const LOCK_STALE_MS = 15 * 60 * 1000;

// ── CLI ──────────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const flag = (name) => args.includes(name);
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] != null ? args[i + 1] : dflt;
};
const expandHome = (p) => (p.startsWith('~') ? path.join(os.homedir(), p.slice(1)) : p);

const SRC = expandHome(opt('--src', '~/Google Drive/Health/Garmin'));
const OUT = path.resolve(HERE, opt('--out', 'run_history.json'));
const PARSER_PATH = path.join(HERE, 'fit-analysis.js');
const DRY = flag('--dry-run');
const FORCE = flag('--force');
const PUSH = flag('--push');

const log = (msg) => {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  console.log(`${ts}  ${msg}`);
};

// ── Parser in a vm context ───────────────────────────────────────────────────
function loadParser() {
  const ctx = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    Uint8Array, Int8Array, Uint16Array, Int16Array, Uint32Array, Int32Array,
    Float32Array, Float64Array, ArrayBuffer, DataView, TextDecoder, TextEncoder,
    Date, Math, Number, JSON, Promise, Buffer, Map, Set, Symbol, Error, TypeError,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(PARSER_PATH, 'utf8'), ctx, { filename: 'fit-analysis.js' });
  if (!ctx.FitAnalysis || !ctx.FitParser) throw new Error('fit-analysis.js did not expose FitAnalysis/FitParser');
  return ctx;
}

const toArrayBuffer = (buf) => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);

// Cheap first pass: just the Garmin activity type and file_id, no analysis.
function readActivityType(ctx, buf) {
  return new Promise((resolve, reject) => {
    const p = new ctx.FitParser({ force: true, mode: 'list', speedUnit: 'm/s', lengthUnit: 'm' });
    p.parse(buf, (err, parsed) => {
      if (err) return reject(new Error(typeof err === 'string' ? err : err.message || 'Parse failed'));
      const s = (parsed.sessions || [])[0] || {};
      const fid = (parsed.file_ids || [])[0] || {};
      resolve({
        sport: s.sport != null ? String(s.sport) : null,
        sub_sport: s.sub_sport != null ? String(s.sub_sport) : null,
        serial_number: fid.serial_number != null ? String(fid.serial_number) : null,
        time_created: fid.time_created ? new Date(fid.time_created).toISOString() : null,
      });
    });
  });
}

const TERRAIN = { generic: 'road', trail: 'trail', treadmill: 'treadmill' };
const terrainOf = (subSport) => TERRAIN[subSport] || subSport || 'unknown';

// "m:ss" per mile -> seconds, or null for "—" / missing.
const paceToSec = (s) => {
  const m = typeof s === 'string' && s.match(/^(\d+):(\d{2})$/);
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
const r = (v, dp) => (v == null || Number.isNaN(v) ? null : Math.round(v * 10 ** dp) / 10 ** dp);

function buildRow({ id, file, type, analysis }) {
  const a = analysis;
  const s = a.summary || {};
  const c = a.conditions || {};
  const dev = a.device_totals || {};
  const hz = a.hr_zones || {};
  const iso = a.gap_iso_hr || {};

  const startLocal = c.start_time_local || s.start_time_local || null;
  const date = startLocal ? startLocal.slice(0, 10)
    : (file.match(/^(\d{4})(\d{2})(\d{2})_/) ? file.replace(/^(\d{4})(\d{2})(\d{2})_.*$/, '$1-$2-$3') : null);

  const gapSec = paceToSec(s.avg_gap);
  const gapSpeed = gapSec ? (M_PER_MI / gapSec) * 60 : null;      // m/min
  const hr = s.avg_hr_bpm ?? null;
  const ef = gapSpeed && hr ? r(gapSpeed / hr, 3) : null;

  const zonePct = {};
  for (const z of (hz.moving_sec && hz.moving_sec.zones) || []) zonePct[z.zone] = z.pct_time_in_zone ?? null;
  const gradePct = {};
  for (const g of a.grade_buckets || []) gradePct[g.label] = g.time_pct ?? null;
  const z2 = iso.flat_reference && iso.flat_reference.Z2;

  return {
    id, file, date,
    start_utc: a.meta && a.meta.start_time || null,
    start_local: startLocal,
    utc_offset_sec: c.utc_offset_sec ?? null,
    sport: type.sport, sub_sport: type.sub_sport, terrain: terrainOf(type.sub_sport),
    laps: a.meta ? a.meta.laps_total : null,
    distance_mi: s.distance_mi ?? null,
    timer_sec: r(dev.total_timer_sec, 0),
    elapsed_sec: hz.all_sec ? hz.all_sec.total_sec : null,
    moving_sec: hz.moving_sec ? hz.moving_sec.total_sec : null,
    ascent_ft: s.ascent_ft ?? null, descent_ft: s.descent_ft ?? null,
    ascent_ft_device: dev.total_ascent_ft ?? null, descent_ft_device: dev.total_descent_ft ?? null,
    vert_per_mi: s.ascent_ft != null && s.distance_mi ? r(s.ascent_ft / s.distance_mi, 0) : null,
    avg_pace_sec_mi: paceToSec(s.avg_pace),
    avg_gap_sec_mi: gapSec,
    gap_speed_m_min: r(gapSpeed, 2),
    avg_hr_bpm: hr,
    ef,
    avg_cadence_spm: s.avg_cadence_spm ?? null,
    cadence_running_spm: s.cadence_running_spm ?? null,
    run_pct: s.run_pct ?? null,
    walk_break_sec: s.walk_break_sec ?? null,
    avg_power_w: s.avg_power_w ?? null,
    temp_f: r(c.temp_f_device, 1),
    hr_zone_pct: zonePct,
    hr_drift_bpm_per_min: iso.drift_bpm_per_min ?? null,
    flat_ref_z2_sec_mi: z2 && z2.usable ? r(z2.flat_pace_sec_per_mi, 1) : null,
    grade_time_pct: gradePct,
    parser_version: a.meta ? a.meta.parser_version : null,
    gap_model: a.meta ? a.meta.gap_model : null,
  };
}

// ── Serialisation: top level pretty, one row per line (diff-friendly) ────────
function serialize(doc) {
  const line = (o) => '    ' + JSON.stringify(o);
  return '{\n'
    + `  "schema": ${doc.schema},\n`
    + `  "generated_at": ${JSON.stringify(doc.generated_at)},\n`
    + `  "parser_version": ${JSON.stringify(doc.parser_version)},\n`
    + `  "gap_model": ${JSON.stringify(doc.gap_model)},\n`
    + `  "source_folder": ${JSON.stringify(doc.source_folder)},\n`
    + `  "runs": [\n${doc.runs.map(line).join(',\n')}\n  ],\n`
    + `  "skipped": [\n${doc.skipped.map(line).join(',\n')}\n  ]\n`
    + '}\n';
}

// ── Lock ─────────────────────────────────────────────────────────────────────
function acquireLock(lockPath) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = fs.openSync(lockPath, 'wx');
      fs.writeSync(fd, String(process.pid));
      fs.closeSync(fd);
      return () => { try { fs.unlinkSync(lockPath); } catch {} };
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      const age = Date.now() - fs.statSync(lockPath).mtimeMs;
      if (age > LOCK_STALE_MS) { log(`stale lock (${Math.round(age / 60000)} min old); removing`); fs.unlinkSync(lockPath); continue; }
      return null;
    }
  }
  return null;
}

// ── Git ──────────────────────────────────────────────────────────────────────
const git = (...a) => execFileSync('git', a, { cwd: HERE, stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();

function commitAndPush(addedCount, throughDate) {
  if (fs.existsSync(path.join(HERE, '.git', 'index.lock'))) {
    log('git index.lock present (another git operation in progress); not committing — next run will');
    return;
  }
  const rel = path.relative(HERE, OUT);
  try {
    git('pull', '--rebase', '--autostash', '--quiet');
  } catch (e) {
    log(`git pull --rebase failed; leaving ${rel} written but uncommitted:\n${e.stderr ? e.stderr.toString() : e.message}`);
    return;
  }
  // Commit when the file differs from HEAD — true after this run wrote it, and
  // also when an earlier run wrote it but could not commit (index.lock, failed
  // pull), so nothing stays stranded on disk.
  if (git('status', '--porcelain', '--', rel)) {
    const what = addedCount > 0 ? `+${addedCount} run${addedCount === 1 ? '' : 's'}` : 'refresh';
    const msg = `run history: ${what} (through ${throughDate})\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`;
    git('add', '--', rel);                       // first build: the file is untracked
    git('commit', '--quiet', '-m', msg, '--', rel);
    log(`committed ${rel} (${what})`);
  }
  // Push whatever is unpushed — this run's commit, or one a previous run made
  // while offline.
  let ahead = 0;
  try { ahead = Number(git('rev-list', '--count', '@{upstream}..HEAD')); } catch { ahead = 1; }
  if (ahead > 0) {
    git('push', '--quiet', 'origin', 'HEAD');
    log(`pushed ${ahead} commit${ahead === 1 ? '' : 's'}`);
  } else {
    log('nothing to push');
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────
async function main() {
  if (!fs.existsSync(SRC)) { log(`source folder not found: ${SRC}`); process.exit(1); }

  const lockPath = OUT + '.lock';
  const release = DRY ? () => {} : acquireLock(lockPath);
  if (!release) { log('another build is running; exiting'); return; }

  try {
    const existing = fs.existsSync(OUT) ? JSON.parse(fs.readFileSync(OUT, 'utf8')) : null;
    const runs = new Map((existing && existing.runs || []).map((x) => [x.id, x]));
    const skipped = new Map((existing && existing.skipped || []).map((x) => [x.id, x]));

    const ctx = loadParser();
    // Parser version straight from the loaded code, so a parser rev invalidates rows.
    const parserVersion = vm.runInContext('typeof PARSER_VERSION !== "undefined" ? PARSER_VERSION : null', ctx);
    const gapModel = vm.runInContext('typeof GAP_MODEL !== "undefined" ? GAP_MODEL : null', ctx);

    const files = fs.readdirSync(SRC).filter((f) => /\.fit$/i.test(f)).sort();
    let parsed = 0, added = 0, refreshed = 0, newlySkipped = 0, failed = 0;
    const t0 = Date.now();

    for (const file of files) {
      const m = file.match(/^(\d{8})_(\d+)\.fit$/i);
      let id = m ? m[2] : null;
      const full = path.join(SRC, file);
      const buf = fs.readFileSync(full);
      let type = null;
      if (!id) {
        type = await readActivityType(ctx, buf).catch(() => null);
        id = type && type.serial_number ? `${type.serial_number}-${(type.time_created || '').replace(/\D/g, '')}` : file;
      }
      const have = runs.get(id);
      const stale = have && have.parser_version !== parserVersion;
      if (!FORCE && !stale && (have || skipped.has(id))) continue;

      if (DRY) { log(`would parse ${file}${stale ? ' (parser rev changed)' : ''}`); parsed++; continue; }

      try {
        type = type || await readActivityType(ctx, buf);
        if (type.sport !== 'running') {
          skipped.set(id, { id, file, sport: type.sport, sub_sport: type.sub_sport });
          runs.delete(id);
          newlySkipped++;
          log(`skip  ${file}  ${type.sport}/${type.sub_sport}`);
          continue;
        }
        const analysis = await ctx.FitAnalysis.analyzeFitBuffer(toArrayBuffer(buf));
        const row = buildRow({ id, file, type, analysis });
        runs.set(id, row);
        skipped.delete(id);
        parsed++;
        if (have) refreshed++; else added++;
        log(`parse ${file}  ${row.terrain.padEnd(9)} ${String(row.distance_mi).padStart(5)} mi  GAP ${analysis.summary.avg_gap}  HR ${row.avg_hr_bpm}  EF ${row.ef}`);
      } catch (e) {
        failed++;
        log(`FAIL  ${file}: ${e.message}`);
      }
    }

    if (DRY) {
      log(`dry run: ${files.length} files, ${parsed} to parse, ${runs.size} rows and ${skipped.size} skipped already on file`);
      return;
    }

    const runList = [...runs.values()].sort((a, b) => String(a.start_utc).localeCompare(String(b.start_utc)));
    const skipList = [...skipped.values()].sort((a, b) => a.file.localeCompare(b.file));

    const body = (doc) => JSON.stringify({ p: doc.parser_version, g: doc.gap_model, r: doc.runs, s: doc.skipped });
    const next = {
      schema: SCHEMA,
      generated_at: existing ? existing.generated_at : null,
      parser_version: parserVersion,
      gap_model: gapModel,
      source_folder: SRC.replace(os.homedir(), '~'),
      runs: runList,
      skipped: skipList,
    };
    const changed = !existing || body(next) !== body({ ...existing, runs: existing.runs || [], skipped: existing.skipped || [] });
    const through = runList.length ? runList[runList.length - 1].date : '—';
    if (!changed) {
      log(`no changes (${runList.length} runs, ${skipList.length} skipped, ${files.length} files scanned in ${((Date.now() - t0) / 1000).toFixed(1)} s)`);
    } else {
      next.generated_at = new Date().toISOString();
      fs.writeFileSync(OUT, serialize(next));
      log(`wrote ${path.relative(HERE, OUT)}: ${runList.length} runs (+${added}, ${refreshed} refreshed), ${skipList.length} skipped (+${newlySkipped}), ${failed} failed, through ${through}, ${((Date.now() - t0) / 1000).toFixed(1)} s`);
    }

    // Even on "no changes": a previous run may have left the file written but
    // uncommitted, or committed but unpushed.
    if (PUSH) commitAndPush(added, through);
  } finally {
    release();
  }
}

main().catch((e) => { log(`ERROR ${e.stack || e.message}`); process.exit(1); });
