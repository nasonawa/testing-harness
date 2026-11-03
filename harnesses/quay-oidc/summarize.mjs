#!/usr/bin/env node
/**
 * Summarize Playwright's JSON reporter output for OIDC compatibility review.
 *
 * Reads /app/test-results/results.json and writes /app/summary/summary.md,
 * grouping results by auth tag so it is obvious which tests are incompatible
 * with an OIDC-configured Quay (the suite auto-skips @auth:Database tests) and
 * which untagged tests actually fail under OIDC.
 */

import fs from 'node:fs';
import path from 'node:path';

const RESULTS = process.env.PLAYWRIGHT_JSON || '/app/test-results/results.json';
const OUT_DIR = process.env.SUMMARY_DIR || '/app/summary';
const OUT_FILE = path.join(OUT_DIR, 'summary.md');

if (!fs.existsSync(RESULTS)) {
  console.log(`[summary] no JSON report at ${RESULTS}; skipping summary`);
  process.exit(0);
}

const report = JSON.parse(fs.readFileSync(RESULTS, 'utf8'));

function firstLine(value) {
  if (!value) return '';
  return String(value).split('\n')[0].slice(0, 400).trim();
}

function mdEscape(value) {
  return String(value).replace(/\|/g, '\\|');
}

/** @type {Array<{title:string,file:string,tags:string[],status:string,error:string,skipReason:string,group:string,project:string}>} */
const rows = [];

/**
 * Playwright's JSON reporter strips the leading "@" from tags (so
 * "@auth:OIDC" arrives as "auth:OIDC"). Normalize them back so classify() and
 * every consumer see a single spelling.
 */
function normalizeTags(tags) {
  return (tags || []).map((tag) => (tag.startsWith('@') ? tag : `@${tag}`));
}

function classify(tags) {
  if (tags.includes('@auth:OIDC')) return 'OIDC-tagged';
  if (tags.includes('@auth:Database')) return 'Database-tagged (auto-skipped)';
  if (tags.includes('@auth:LDAP')) return 'LDAP-tagged (auto-skipped)';
  if (tags.some((t) => t.startsWith('@auth:'))) return 'Other auth-tagged';
  if (tags.includes('@container')) return 'Untagged + @container';
  return 'Untagged';
}

function walk(suites, inheritedFile, prefixes) {
  for (const suite of suites || []) {
    const file = suite.file || inheritedFile || '';
    const nextPrefixes = suite.title ? [...prefixes, suite.title] : prefixes;

    for (const spec of suite.specs || []) {
      const specFile = spec.file || file || '';
      const tags = normalizeTags(spec.tags);
      const title = [...nextPrefixes, spec.title].filter(Boolean).join(' › ');

      // One entry per project (this harness runs only chromium).
      for (const test of spec.tests || []) {
        const results = test.results || [];
        const last = results[results.length - 1];
        const lastStatus = last?.status || '';
        const annotations = test.annotations || [];

        let status = test.status || 'unknown';
        if (status === 'expected') status = 'passed';
        else if (status === 'unexpected') status = 'failed';
        else if (lastStatus === 'timedOut') status = 'failed';
        else if (lastStatus === 'interrupted') status = 'interrupted';

        const skipAnnotation = annotations.find((a) => a.type === 'skip');
        const skipReason =
          status === 'skipped'
            ? firstLine(skipAnnotation?.description || last?.error?.message || 'skipped')
            : '';

        rows.push({
          title,
          file: specFile,
          tags,
          status,
          error: status === 'failed' ? firstLine(last?.error?.message) : '',
          skipReason,
          group: classify(tags),
          project: test.projectName || 'chromium',
        });
      }
    }

    walk(suite.suites, file, nextPrefixes);
  }
}

walk(report.suites, '', []);

const counts = {
  total: rows.length,
  passed: 0,
  failed: 0,
  timedOut: 0,
  flaky: 0,
  skipped: 0,
  interrupted: 0,
  other: 0,
};

for (const row of rows) {
  if (row.status === 'passed') counts.passed++;
  else if (row.status === 'failed') counts.failed++;
  else if (row.status === 'flaky') counts.flaky++;
  else if (row.status === 'skipped') counts.skipped++;
  else if (row.status === 'interrupted') counts.interrupted++;
  else counts.other++;
}

// Group statistics.
const groups = new Map();
for (const row of rows) {
  if (!groups.has(row.group)) {
    groups.set(row.group, {total: 0, passed: 0, failed: 0, skipped: 0, flaky: 0});
  }
  const g = groups.get(row.group);
  g.total++;
  if (row.status === 'passed') g.passed++;
  else if (row.status === 'failed') g.failed++;
  else if (row.status === 'skipped') g.skipped++;
  else if (row.status === 'flaky') g.flaky++;
}

// Skip reasons.
const skipReasons = new Map();
for (const row of rows.filter((r) => r.status === 'skipped')) {
  const reason = row.skipReason || 'no reason recorded';
  skipReasons.set(reason, (skipReasons.get(reason) || 0) + 1);
}

const failures = rows.filter((r) => r.status === 'failed');
const failuresByFile = new Map();
for (const row of failures) {
  if (!failuresByFile.has(row.file)) failuresByFile.set(row.file, []);
  failuresByFile.get(row.file).push(row);
}

const lines = [];
lines.push('# Quay OIDC Playwright compatibility summary');
lines.push('');
lines.push(`- Generated: ${new Date().toISOString()}`);
lines.push(`- Source report: \`${RESULTS}\``);
lines.push(`- Project: chromium`);
lines.push('');
lines.push('## Totals');
lines.push('');
lines.push('| Status | Count |');
lines.push('| --- | ---: |');
lines.push(`| Total tests | ${counts.total} |`);
lines.push(`| Passed | ${counts.passed} |`);
lines.push(`| Failed | ${counts.failed} |`);
lines.push(`| Flaky | ${counts.flaky} |`);
lines.push(`| Skipped | ${counts.skipped} |`);
lines.push(`| Interrupted | ${counts.interrupted} |`);
lines.push('');
lines.push('## By auth tag');
lines.push('');
lines.push('| Group | Total | Passed | Failed | Skipped | Flaky |');
lines.push('| --- | ---: | ---: | ---: | ---: | ---: |');
for (const [name, g] of [...groups.entries()].sort((a, b) => b[1].total - a[1].total)) {
  lines.push(`| ${mdEscape(name)} | ${g.total} | ${g.passed} | ${g.failed} | ${g.skipped} | ${g.flaky} |`);
}
lines.push('');
lines.push('## Skip reasons');
lines.push('');
if (skipReasons.size === 0) {
  lines.push('_None._');
} else {
  lines.push('| Reason | Count |');
  lines.push('| --- | ---: |');
  for (const [reason, count] of [...skipReasons.entries()].sort((a, b) => b[1] - a[1])) {
    lines.push(`| ${mdEscape(reason)} | ${count} |`);
  }
}
lines.push('');
lines.push(`## Failures (${failures.length})`);
lines.push('');
if (failures.length === 0) {
  lines.push('_None._');
} else {
  for (const [file, fileFailures] of [...failuresByFile.entries()].sort()) {
    lines.push(`### \`${file}\``);
    lines.push('');
    for (const row of fileFailures) {
      lines.push(`- ${mdEscape(row.title)}`);
      if (row.error) {
        const safeError = row.error.replaceAll('`', "'");
        lines.push(`  - \`${safeError}\``);
      }
    }
    lines.push('');
  }
}

fs.mkdirSync(OUT_DIR, {recursive: true});
fs.writeFileSync(OUT_FILE, lines.join('\n') + '\n');

console.log('');
console.log('================ Playwright OIDC compatibility ================');
console.log(
  `total=${counts.total} passed=${counts.passed} failed=${counts.failed} ` +
    `flaky=${counts.flaky} skipped=${counts.skipped} interrupted=${counts.interrupted}`,
);
for (const [name, g] of [...groups.entries()].sort((a, b) => b[1].total - a[1].total)) {
  console.log(`  ${name}: total=${g.total} passed=${g.passed} failed=${g.failed} skipped=${g.skipped}`);
}
if (failures.length > 0) {
  console.log(`Failures (${failures.length}):`);
  for (const row of failures.slice(0, 50)) {
    console.log(`  - ${row.file}: ${row.title}${row.error ? ` :: ${row.error}` : ''}`);
  }
  if (failures.length > 50) console.log(`  ... ${failures.length - 50} more (see summary.md)`);
}
console.log(`Summary written to ${OUT_FILE}`);
console.log('==============================================================');
