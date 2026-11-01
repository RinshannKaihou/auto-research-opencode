import { copyFileSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const plugin = dirname(dirname(fileURLToPath(import.meta.url)));
const source = join(plugin, '..', 'src', 'auto_research');
const destination = join(plugin, 'python', 'auto_research');
rmSync(destination, { recursive: true, force: true });
mkdirSync(destination, { recursive: true });
for (const file of [
  '__init__.py',
  'artifacts.py',
  'conclusion_store.py',
  'controller.py',
  'checkpoint_receipts.py',
  'context_policy.py',
  'epistemic.py',
  'evidence_read.py',
  'evidence_conclusion.py',
  'exploration_store.py',
  'frozen_refs.py',
  'field_checks.py',
  'hints.py',
  'errors.py',
  'migration.py',
  'maintenance_cli.py',
  'memory_store.py',
  'revision_presets.py',
  'review_lifecycle.py',
  'runtime_budget.py',
  'adoption.py',
  'native_store.py',
  'query_store.py',
  'schema5.py',
  'schema6.py',
  'schema7.py',
  'schema8.py',
  'schema9.py',
  'schema10.py',
  'schema11.py',
  'schema12.py',
  'schema13.py',
  'schema14.py',
  'workflow_store.py',
  'workbench_read.py',
  'publication_display.py',
  'service.py',
]) {
  copyFileSync(join(source, file), join(destination, file));
}
