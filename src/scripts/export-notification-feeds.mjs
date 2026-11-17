#!/usr/bin/env node
// scripts/export-notification-feeds.mjs
//
// Exports the app's feed list (src/utils/feedsConfig.js) to JSON for the
// backend feed poller (poll_feeds.py), so the poller checks exactly the feeds
// the app shows, filed under the same notification topics. Each feed records
// the database table it reads and the column filters it applies. Run it from
// the app's project root whenever feedsConfig changes:
//
//   node scripts/export-notification-feeds.mjs [output.json]
//
// Default output: notification-feeds.json in the current directory. Copy it next
// to the poller (gov_glance_notifications/src/).
//
// What it keeps: every feed with an apiEndpoint in a section that has a
// notification topic. What it drops: viewall/member cards, committee feeds (a
// different endpoint), the "recent" section, sections without a topic
// (immigration), and duplicates (the same request filed twice in one topic,
// including skip=1, skip=2... pagination copies).
import fs from 'fs';
import os from 'os';
import path from 'path';
import { pathToFileURL } from 'url';

const CONFIG_PATH = path.resolve('src/utils/feedsConfig.js');
const OUTPUT_PATH = path.resolve(process.argv[2] || 'notification-feeds.json');

// feedsConfig section -> notification topic. `topic` is the value written to
// notifications.topic; push_topics.py maps it to the FCM topic in `topicName`.
// Must match TOPIC_CATALOG in notificationTopics.js.
const SECTION_TOPICS = {
  legislative: { topic: 'Legislative', topicName: 'LegislativeNews' },
  executive: { topic: 'Executive', topicName: 'ExecutiveNews' },
  whitehouse: { topic: 'Executive', topicName: 'ExecutiveNews' },
  judicial: { topic: 'Judicial', topicName: 'JudicialNews' },
  defense: { topic: 'Defense', topicName: 'DefenseNews' },
  elections: { topic: 'Elections', topicName: 'ElectionNews' },
  economy: { topic: 'Economy', topicName: 'EconomyNews' },
  environment: { topic: 'Environment', topicName: 'EnvironmentNews' },
  foreignAffairs: { topic: 'Foreign Affairs', topicName: 'ForeignAffairsNews' },
  health: { topic: 'Health', topicName: 'HealthNews' },
  infrastructure: { topic: 'Infrastructure', topicName: 'InfrastructureNews' },
  justice: { topic: 'Justice', topicName: 'JusticeNews' },
  science: { topic: 'Science & Tech', topicName: 'ScienceTechNews' },
  regulations: { topic: 'Regulations', topicName: 'RegulationsNews' },
  spending: { topic: 'Spending', topicName: 'SpendingNews' },
};

const SKIPPED_TYPES = new Set(['viewall', 'member']);

// feedsConfig.js is an ES module in a project Node treats as CommonJS, so it is
// imported from a temporary .mjs copy. It has no imports of its own.
async function loadFeedsConfig() {
  const source = fs.readFileSync(CONFIG_PATH, 'utf8');
  const tempFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'feeds-')), 'feedsConfig.mjs');
  fs.writeFileSync(tempFile, source);
  const module = await import(pathToFileURL(tempFile).href);
  return module.feedsConfig;
}

// A section's entries flattened: carousel items are pulled out of their carousel.
function flattenSection(entries) {
  const feeds = [];
  entries.forEach((entry) => {
    if (entry.type === 'carousel' && Array.isArray(entry.carouselItems)) {
      entry.carouselItems.forEach((item) => feeds.push({ ...item, parent: entry }));
    } else {
      feeds.push(entry);
    }
  });
  return feeds;
}

// Request params with skip removed, keys sorted, so equal requests compare equal.
function normalizeParams(apiEndpoint) {
  const params = new URLSearchParams(apiEndpoint.replace(/^[&?]/, ''));
  params.delete('skip');
  params.sort();
  return params;
}

function feedKind(params) {
  if (params.has('committee') || params.has('chamber')) return 'committee';
  if (params.has('schema') && params.has('table')) return 'table';
  if (params.has('table_name') && params.has('agency')) return 'agency';
  return 'unknown';
}

// The database table a feed reads. Agency feeds name it with table_name.
function feedTable(params, kind) {
  return kind === 'agency' ? params.get('table_name') : params.get('table');
}

// Column filters the feed applies to its table, as [{ column, value }]. Agency
// feeds filter the table's `agency` column; table feeds may filter one column
// with filter_column / filter_string. An empty list means every row.
function feedFilters(params, kind) {
  if (kind === 'agency') return [{ column: 'agency', value: params.get('agency') }];
  const column = params.get('filter_column');
  const value = params.get('filter_string');
  return column && value !== null ? [{ column, value }] : [];
}

async function main() {
  const feedsConfig = await loadFeedsConfig();
  const feeds = [];
  const seen = new Set();
  const dropped = { noTopic: 0, cardType: 0, committee: 0, unknown: 0, duplicate: 0 };

  for (const [section, entries] of Object.entries(feedsConfig)) {
    if (!Array.isArray(entries)) continue; // house / senate hold committee objects
    const topic = SECTION_TOPICS[section];

    for (const feed of flattenSection(entries)) {
      if (!feed.apiEndpoint) continue;
      if (!topic) { dropped.noTopic += 1; continue; }
      if (SKIPPED_TYPES.has(feed.type)) { dropped.cardType += 1; continue; }

      const params = normalizeParams(feed.apiEndpoint);
      const kind = feedKind(params);
      if (kind === 'committee') { dropped.committee += 1; continue; }
      if (kind === 'unknown') { dropped.unknown += 1; continue; }

      const query = params.toString();
      const dedupeKey = `${topic.topicName}|${query}`;
      if (seen.has(dedupeKey)) { dropped.duplicate += 1; continue; }
      seen.add(dedupeKey);

      feeds.push({
        key: `us:${section}:${feed.id || query}`,
        country: 'us',
        section,
        topic: topic.topic,
        topicName: topic.topicName,
        name: feed.feedName || feed.title || feed.name || '',
        kind,
        params: query,
        schema: params.get('schema') || 'united_states_of_america',
        table: feedTable(params, kind) || '',
        filters: feedFilters(params, kind),
      });
    }
  }

  const output = { generatedAt: new Date().toISOString(), source: 'src/utils/feedsConfig.js', feeds };
  fs.writeFileSync(OUTPUT_PATH, `${JSON.stringify(output, null, 2)}\n`);

  const byTopic = {};
  feeds.forEach((f) => { byTopic[f.topic] = (byTopic[f.topic] || 0) + 1; });
  console.log(`Wrote ${feeds.length} feeds to ${OUTPUT_PATH}`);
  console.log('By topic:', byTopic);
  console.log('By kind:', feeds.reduce((acc, f) => ({ ...acc, [f.kind]: (acc[f.kind] || 0) + 1 }), {}));
  console.log('Dropped:', dropped);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
