#!/usr/bin/env node
// garmin_login.mjs — one-time Garmin Connect login for push_garmin.py.
//
// Garmin's mobile login endpoint (what the Python `garth` library uses) now answers
// 429 to scripted logins. The `garmin-connect` npm library — the one the Garmin MCP
// server runs on — still signs in through Garmin's web SSO. This script logs in with
// it and writes the OAuth tokens in garth's file format, so push_garmin.py can keep
// using garth for the API calls (garth's token refresh goes through the OAuth
// exchange endpoint, which still works).
//
//   npx -y -p garmin-connect@1.6.2 -c "node garmin_login.mjs --dir ~/.garth --email you@example.com"
//
// push_garmin.py --login runs exactly that. The password is prompted (hidden) and
// never written anywhere. --check only resolves the library and exits.

import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { homedir } from 'node:os';
import readline from 'node:readline';

function loadGarminConnect() {
  const localRequire = createRequire(import.meta.url);
  try { return localRequire('garmin-connect'); } catch {}
  // Under `npx -p garmin-connect -c ...` the package's .bin dir is on PATH.
  for (const p of (process.env.PATH || '').split(':')) {
    if (p.endsWith('/node_modules/.bin')) {
      const candidate = join(dirname(p), 'garmin-connect');
      if (existsSync(candidate)) return createRequire(join(candidate, 'package.json'))('garmin-connect');
    }
  }
  throw new Error('garmin-connect not found. Run via: npx -y -p garmin-connect@1.6.2 -c "node garmin_login.mjs ..."');
}

function arg(name, dflt) {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}

function ask(question, { hidden = false } = {}) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    if (hidden) {
      const write = rl._writeToOutput.bind(rl);
      rl._writeToOutput = (s) => { if (s.includes(question)) write(question); };
    }
    rl.question(question, (answer) => { rl.close(); if (hidden) process.stdout.write('\n'); resolve(answer.trim()); });
  });
}

const { GarminConnect } = loadGarminConnect();
if (process.argv.includes('--check')) { console.log('garmin-connect resolved OK'); process.exit(0); }

const dir = arg('--dir', join(homedir(), '.garth')).replace(/^~(?=$|\/)/, homedir());
const email = arg('--email', process.env.GARMIN_USERNAME) || await ask('Garmin email: ');
const password = await ask('Garmin password: ', { hidden: true });
if (!password) { console.error('No password entered.'); process.exit(1); }

const gc = new GarminConnect({ username: email, password });
await gc.login();
const { oauth1, oauth2 } = gc.exportToken();

mkdirSync(dir, { recursive: true });
// garth's OAuth1Token needs `domain` to build URLs; the rest of the fields match.
writeFileSync(join(dir, 'oauth1_token.json'), JSON.stringify({ ...oauth1, domain: 'garmin.com' }, null, 4));
// garth wants integer epoch seconds; garmin-connect writes fractional ones.
for (const k of ['expires_at', 'refresh_token_expires_at']) if (typeof oauth2[k] === 'number') oauth2[k] = Math.floor(oauth2[k]);
writeFileSync(join(dir, 'oauth2_token.json'), JSON.stringify(oauth2, null, 4));
console.log(`Logged in as ${email}; tokens written to ${dir} (oauth2 expires in ${Math.round(oauth2.expires_in / 60)} min, refresh token ${Math.round(oauth2.refresh_token_expires_in / 86400)} d).`);
