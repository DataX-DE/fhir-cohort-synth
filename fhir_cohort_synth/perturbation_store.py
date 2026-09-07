"""SQLite ledger and bounded statistical aggregation for perturbation runs."""
from collections import Counter
from functools import lru_cache
import sqlite3

from .field_statistics import compare_decimals, numeric_summary
from .json_fields import display_path
from .jsonio import dumps, loads
from .perturbation_handlers import relative_change


SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA wal_autocheckpoint=0;
PRAGMA cache_size=-65536;
PRAGMA temp_store=FILE;
CREATE TABLE run (id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL,
 status TEXT NOT NULL, phase TEXT NOT NULL, settings_json TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL, definitions_json TEXT NOT NULL, failure_code TEXT);
CREATE TABLE patient_parameters (patient_id INTEGER PRIMARY KEY, identity TEXT NOT NULL,
 factor TEXT NOT NULL, minimum_days INTEGER NOT NULL, maximum_days INTEGER NOT NULL, days INTEGER);
CREATE TABLE resource_mappings (resource_id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL,
 patient_id INTEGER, resource_type TEXT NOT NULL, old_id TEXT, new_id TEXT NOT NULL UNIQUE,
 path TEXT, digest TEXT NOT NULL);
CREATE INDEX mapping_root ON resource_mappings(root_id);
CREATE TABLE changes (root_id INTEGER NOT NULL, path TEXT NOT NULL, owner_id INTEGER NOT NULL,
 old_present INTEGER NOT NULL, old_json TEXT, new_json TEXT NOT NULL,
 reason TEXT NOT NULL, datatype TEXT, target_id INTEGER,
 PRIMARY KEY(root_id,path));
CREATE TABLE field_actions (resource_type TEXT NOT NULL, path TEXT NOT NULL,
 datatype TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, frequency INTEGER NOT NULL,
 PRIMARY KEY(resource_type,path,datatype,action,reason));
CREATE TABLE numeric_contexts (id INTEGER PRIMARY KEY, context_json TEXT NOT NULL UNIQUE,
 resource_type TEXT NOT NULL, path TEXT NOT NULL, datatype TEXT NOT NULL,
 samples INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0,
 zero_baselines INTEGER NOT NULL DEFAULT 0);
CREATE TABLE numeric_frequencies (context_id INTEGER NOT NULL, phase TEXT NOT NULL,
 value TEXT NOT NULL, frequency INTEGER NOT NULL,
 PRIMARY KEY(context_id,phase,value));
CREATE TABLE numeric_summaries (context_id INTEGER NOT NULL, phase TEXT NOT NULL,
 summary_json TEXT NOT NULL, PRIMARY KEY(context_id,phase));
CREATE TABLE source_issues (severity TEXT, code TEXT, frequency INTEGER);
"""


def normalized(path):
    return tuple(('item', None) if kind == 'index' else (kind, key) for kind, key in path)


@lru_cache(maxsize=16384)
def normalized_path(path):
    # Repeated measurements revisit the same paths millions of times. Cache
    # only bounded path metadata, never a cohort's distinct source values.
    segments = normalized(path)
    return segments, dumps(segments)


class Ledger:
    def __init__(self, path, settings, fingerprint, metadata):
        with path.open('xb'):
            pass
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.create_collation('DECIMAL', compare_decimals)
        self.db.executescript(SCHEMA)
        self.db.execute('INSERT INTO run VALUES (1,1,?,?,?,?,?,NULL)',
                        ('in_progress', 'preparation', dumps(settings), fingerprint, dumps(metadata)))
        self.db.commit()
        self.actions, self.frequencies, self.samples = Counter(), Counter(), Counter()
        self.context_id = lru_cache(maxsize=4096)(self._context_id)

    def _context_id(self, context, resource_type, path, datatype):
        self.db.execute('INSERT OR IGNORE INTO numeric_contexts(context_json,resource_type,path,datatype) VALUES (?,?,?,?)',
                        (context, resource_type, path, datatype))
        return self.db.execute('SELECT id FROM numeric_contexts WHERE context_json=?', (context,)).fetchone()[0]

    def action(self, resource_type, path, datatype, action, reason):
        self.actions[resource_type, normalized_path(path)[1], datatype or 'unknown', action, reason] += 1

    def number(self, resource_type, field, after):
        # Code, comparator and exact unit identifiers remain part of each
        # context. A shared JSON path is never enough to pool measurements.
        q = field.quantity or {}
        segments, path = normalized_path(field.path)
        datatype = field.datatype or 'unknown'
        context = dumps([resource_type, segments, datatype, field.concept,
                         {k: q[k] for k in ('system', 'code', 'unit', 'comparator') if k in q}])
        identity = self.context_id(context, resource_type, path, datatype)
        before = field.value
        self.samples[identity, 'samples'] += 1
        self.samples[identity, 'changed'] += dumps(before) != dumps(after)
        self.samples[identity, 'zero_baselines'] += before == 0
        self.frequencies[identity, 'before', dumps(before)] += 1
        self.frequencies[identity, 'after', dumps(after)] += 1
        relative = relative_change(before, after)
        if relative is not None:
            self.frequencies[identity, 'relative', dumps(relative)] += 1
            self.frequencies[identity, 'absolute_relative', dumps(relative.copy_abs())] += 1

    def flush(self):
        self.db.executemany('INSERT INTO field_actions VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE SET frequency=frequency+excluded.frequency',
                            ((*key, value) for key, value in self.actions.items()))
        self.db.executemany('INSERT INTO numeric_frequencies VALUES (?,?,?,?) ON CONFLICT DO UPDATE SET frequency=frequency+excluded.frequency',
                            ((*key, value) for key, value in self.frequencies.items()))
        for column in ('samples', 'changed', 'zero_baselines'):
            self.db.executemany(f'UPDATE numeric_contexts SET {column}={column}+? WHERE id=?',
                                ((n, identity) for (identity, name), n in self.samples.items() if name == column))
        self.actions.clear(); self.frequencies.clear(); self.samples.clear()
        self.db.commit()

    def maybe_flush(self):
        # Also bound batches for high-cardinality fields within a single large
        # resource. Patient count does not determine this memory bound.
        if len(self.actions) + len(self.frequencies) + len(self.samples) >= 20000:
            self.flush()

    def aggregate(self):
        self.flush()
        for row in self.db.execute('SELECT id FROM numeric_contexts ORDER BY id'):
            identity = row[0]
            for phase in ('before', 'after', 'relative', 'absolute_relative'):
                count = self.db.execute('SELECT coalesce(sum(frequency),0) FROM numeric_frequencies WHERE context_id=? AND phase=?',
                                        (identity, phase)).fetchone()[0]
                if count:
                    values = self.db.execute('SELECT value,frequency FROM numeric_frequencies WHERE context_id=? AND phase=? ORDER BY value COLLATE DECIMAL,value',
                                             (identity, phase))
                    low, high, quantiles = numeric_summary(values, count)
                    summary = {'sample_count': count, 'minimum': loads(low), 'maximum': loads(high),
                               'quantiles': quantiles, 'estimator': 'weighted_nearest_rank'}
                else:
                    summary = {'sample_count': 0, 'minimum': None, 'maximum': None, 'quantiles': {}}
                self.db.execute('INSERT INTO numeric_summaries VALUES (?,?,?)', (identity, phase, dumps(summary)))
        self.db.commit()

    def report_fields(self):
        for row in self.db.execute('SELECT * FROM field_actions ORDER BY resource_type,path,datatype,action,reason'):
            item = dict(row)
            item['path'] = loads(item['path'])
            item['display_path'] = display_path(item['path'])
            yield item

    def report_numbers(self):
        for row in self.db.execute('SELECT id,resource_type,path,datatype,samples,changed,zero_baselines FROM numeric_contexts ORDER BY id'):
            item = dict(row)
            item['path'] = loads(item['path'])
            item['display_path'] = display_path(item['path'])
            item['statistics'] = {r['phase']: loads(r['summary_json']) for r in self.db.execute(
                'SELECT phase,summary_json FROM numeric_summaries WHERE context_id=? ORDER BY phase', (row['id'],))}
            # Actual source coding/unit strings live only in context_json in
            # the local database, not as scalar examples in JSON summaries.
            yield item

    def close(self):
        self.context_id.cache_clear()
        self.db.close()
