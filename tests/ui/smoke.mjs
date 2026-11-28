// Browser smoke test for the workspace UI. Starts the app on a new data directory under the
// system temp dir, drives headless Chrome through the main interactions, and exits non-zero on
// any failure.
//
//   cd tests/ui && npm install && npm test
//
// Env: CHROME (browser binary, default /usr/bin/google-chrome), LABSIM_PYTHON (default
// ../../.venv/bin/python), OUT (screenshot directory, default the data directory).
import puppeteer from 'puppeteer-core';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const PYTHON = process.env.LABSIM_PYTHON || path.resolve(here, '../../.venv/bin/python');
const CHROME = process.env.CHROME || '/usr/bin/google-chrome';
const PORT = 8790 + Math.floor(Math.random() * 100);
const URL = `http://127.0.0.1:${PORT}/`;
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'labsim-ui-'));
const OUT = process.env.OUT || DATA;
// The app with a scripted assistant backend, so the assistant flow runs without API calls.
const server = spawn(PYTHON, [path.join(here, 'fake_ai_server.py'), String(PORT), DATA], { stdio: 'inherit' });
process.on('exit', () => server.kill());
for (let i = 0; ; i++) {
  try { if ((await fetch(`${URL}api/health`)).ok) break; } catch { /* not up yet */ }
  if (i > 100) throw new Error('server did not start');
  await new Promise((r) => setTimeout(r, 100));
}

const results = [];
const check = (name, ok, extra = '') => { results.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${extra}`); };

const browser = await puppeteer.launch({
  executablePath: CHROME, headless: 'new',
  args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--window-size=1600,950'],
  defaultViewport: { width: 1600, height: 950 },
});
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
// 422 responses are expected where the test makes the server refuse a change.
page.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('status of 422')) errors.push(`console: ${m.text()}`); });
page.on('dialog', (d) => d.accept());

await page.goto(URL, { waitUntil: 'networkidle0' });
await page.waitForSelector('body.ready', { timeout: 15000 });
await page.evaluate(() => new Promise((r) => setTimeout(r, 500)));
const settle = (ms = 400) => page.evaluate((t) => new Promise((r) => setTimeout(r, t)), ms);

// Open the absorbance workcell.
await page.evaluate(async () => {
  const sel = document.querySelector('#lab-select');
  const opt = [...sel.options].find((o) => o.textContent.startsWith('Absorbance'));
  sel.value = opt.value;
  sel.dispatchEvent(new Event('change'));
});
await settle(800);
const labName = await page.evaluate(() => window.labsim.editor.lab.name);
check('opens a lab', labName === 'Absorbance assay workcell', labName);
check('renders instruments', await page.evaluate(() => window.labsim.viewport.objects.size) === 4);
check('library lists definitions', await page.$$eval('.lib-item', (els) => els.length) >= 15);
check('valid layout reported', (await page.$eval('.issues-summary', (e) => e.textContent)).startsWith('Layout valid'));
await page.screenshot({ path: `${OUT}/01-workcell.png` });

// Select by clicking in the 3D view.
const click = async (id, opts = {}) => {
  const p = await page.evaluate((i) => window.labsim.viewport.screenPosition(i), id);
  if (opts.shift) await page.keyboard.down('Shift');
  await page.mouse.click(p.x, p.y);
  if (opts.shift) await page.keyboard.up('Shift');
  await settle();
};
await page.evaluate(() => window.labsim.viewport.frame('top'));
await settle();
await click('plate_reader');
check('click selects instrument', JSON.stringify(await page.evaluate(() => window.labsim.editor.selection)) === '["plate_reader"]');
check('properties show it', (await page.$eval('#properties .panel-title', (e) => e.textContent)).includes('Plate reader'));

// Move with the gizmo: find the X arrow by hovering along the projected +x direction.
const startPose = await page.evaluate(() => ({ ...window.labsim.editor.instance('plate_reader').pose }));
const handle = await page.evaluate(() => {
  const { viewport } = window.labsim;
  const g = viewport.objects.get('plate_reader').group;
  const rect = viewport.renderer.domElement.getBoundingClientRect();
  const project = (x, y, z) => {
    const v = g.position.clone(); v.x += x; v.y += y; v.z += z; v.project(viewport.camera);
    return { x: rect.left + (v.x + 1) / 2 * rect.width, y: rect.top + (1 - v.y) / 2 * rect.height };
  };
  return { o: project(0, 0, 0), dx: project(100, 0, 0) };
});
let found = false;
const dir = { x: handle.dx.x - handle.o.x, y: handle.dx.y - handle.o.y };
const len = Math.hypot(dir.x, dir.y);
for (let t = 10; t < 200 && !found; t += 3) {
  const x = handle.o.x + dir.x / len * t, y = handle.o.y + dir.y / len * t;
  await page.mouse.move(x, y);
  found = await page.evaluate(() => window.labsim.viewport.gizmo.axis === 'X');
  if (found) {
    await page.mouse.down();
    for (let k = 1; k <= 10; k++) await page.mouse.move(x - 8 * k, y);
    await page.mouse.up();
  }
}
await settle(600);
const movedPose = await page.evaluate(() => ({ ...window.labsim.editor.instance('plate_reader').pose }));
check('gizmo drag moves along x', found && movedPose.x_mm < startPose.x_mm && movedPose.y_mm === startPose.y_mm,
  `${startPose.x_mm} -> ${movedPose.x_mm}`);
check('move snaps to grid', movedPose.x_mm % 50 === 0);
const issuesAfterMove = await page.evaluate(() => window.labsim.editor.validation.issues.map((i) => i.code));
check('moving into the heater-shaker is flagged', issuesAfterMove.includes('collision') || issuesAfterMove.includes('clearance'), issuesAfterMove.join(','));
await page.screenshot({ path: `${OUT}/02-moved.png` });

// Undo returns it.
await page.keyboard.down('Control'); await page.keyboard.press('z'); await page.keyboard.up('Control');
await settle();
check('undo restores pose', (await page.evaluate(() => window.labsim.editor.instance('plate_reader').pose.x_mm)) === startPose.x_mm);
await page.keyboard.down('Control'); await page.keyboard.press('y'); await page.keyboard.up('Control');
await settle();
check('redo re-applies', (await page.evaluate(() => window.labsim.editor.instance('plate_reader').pose.x_mm)) === movedPose.x_mm);
await page.keyboard.down('Control'); await page.keyboard.press('z'); await page.keyboard.up('Control');
await settle();

// Rotate through the properties panel.
await click('plate_reader');
const rot = await page.$('[data-pose="rotation_deg"] input');
await rot.click({ clickCount: 3 }); await rot.type('90'); await page.keyboard.press('Tab');
await settle();
check('rotation from panel', (await page.evaluate(() => window.labsim.editor.instance('plate_reader').pose.rotation_deg)) === 90);

// Change dimensions through the panel.
await page.evaluate(() => {
  const field = [...document.querySelectorAll('#properties .field')].find((f) => f.textContent.startsWith('Width'));
  const input = field.querySelector('input'); input.value = '600'; input.dispatchEvent(new Event('change'));
});
await settle();
check('dimension override', (await page.evaluate(() => window.labsim.editor.instance('plate_reader').dimensions?.width_mm)) === 600);

// Bench moves carry instruments standing on it (supports come from the server).
await settle(300);
const supports = await page.evaluate(() => window.labsim.editor.riders('bench_1').sort());
check('server reports what stands on the bench', JSON.stringify(supports) === '["heater_shaker","liquid_handler","plate_reader"]', JSON.stringify(supports));

// Moving the bench from the properties panel carries what stands on it; then undo.
await page.evaluate(() => window.labsim.editor.select(['bench_1']));
await settle();
const lhBefore = await page.evaluate(() => window.labsim.editor.instance('liquid_handler').pose.y_mm);
await page.evaluate(() => {
  const input = document.querySelector('[data-pose="y_mm"] input');
  input.value = '900'; input.dispatchEvent(new Event('change'));
});
await settle();
const lhAfter = await page.evaluate(() => window.labsim.editor.instance('liquid_handler').pose.y_mm);
check('moving a bench moves its instruments', lhAfter - lhBefore === 300, `${lhBefore} -> ${lhAfter}`);
await page.keyboard.down('Control'); await page.keyboard.press('z'); await page.keyboard.up('Control');
await settle();
check('undo puts bench and instruments back', (await page.evaluate(() => window.labsim.editor.instance('liquid_handler').pose.y_mm)) === lhBefore);

// Shift-click two instruments to measure.
await page.keyboard.press('Escape');
await click('liquid_handler');
await click('heater_shaker', { shift: true });
await settle(600);
const measureText = await page.$eval('#properties', (e) => e.textContent);
check('measurement panel', measureText.includes('Clearance') && measureText.includes('mm'), measureText.slice(0, 120));
await page.screenshot({ path: `${OUT}/03-measure.png` });

// Drag and drop from the library onto the floor.
await page.keyboard.press('Escape');
await page.evaluate(() => window.labsim.viewport.frame('iso'));
await settle();
// The incubation group is open by default; drag the CO2 incubator.
const dropped = await page.evaluate(async () => {
  const item = document.querySelector('.lib-item[data-definition-id="incubator_co2"]');
  if (!item) return 'no item';
  const canvas = window.labsim.viewport.renderer.domElement;
  const rect = canvas.getBoundingClientRect();
  const dt = new DataTransfer();
  item.dispatchEvent(new DragEvent('dragstart', { dataTransfer: dt, bubbles: true }));
  // Aim at the floor point (400, 2000).
  const p = window.labsim.viewport.objects.get('bench_1').group.position.clone();
  p.set(400, 2000, 0); p.project(window.labsim.viewport.camera);
  const x = rect.left + (p.x + 1) / 2 * rect.width, y = rect.top + (1 - p.y) / 2 * rect.height;
  canvas.dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, clientX: x, clientY: y, bubbles: true, cancelable: true }));
  canvas.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, clientX: x, clientY: y, bubbles: true, cancelable: true }));
  item.dispatchEvent(new DragEvent('dragend', { dataTransfer: dt, bubbles: true }));
  return 'ok';
});
await settle(600);
const added = await page.evaluate(() => window.labsim.editor.lab.instruments.at(-1));
check('drag-and-drop adds instrument', dropped === 'ok' && added.definition_id === 'incubator_co2' &&
  Math.abs(added.pose.x_mm - 400) <= 50 && Math.abs(added.pose.y_mm - 2000) <= 50 && added.pose.z_mm === 0,
  JSON.stringify(added.pose));

// Click-to-add places in free space without collisions.
await page.click('.lib-item[data-definition-id="plate_centrifuge"]');
await settle(800);
const auto = await page.evaluate(() => window.labsim.editor.lab.instruments.at(-1));
const autoIssues = await page.evaluate((id) => window.labsim.editor.validation.issues.filter((i) => i.instrument_ids.includes(id)).map((i) => i.code), auto.id);
check('click-to-add finds a free spot', auto.definition_id === 'plate_centrifuge' && autoIssues.length === 0, `${JSON.stringify(auto.pose)} ${autoIssues}`);

// Force a collision and see it reported and highlighted.
await page.evaluate((id) => window.labsim.editor.mutate((lab) => {
  const i = lab.instruments.find((x) => x.id === id); i.pose.x_mm = 1500; i.pose.y_mm = 600;
}), auto.id);
await settle(600);
const summary = await page.$eval('.issues-summary', (e) => e.textContent);
check('collision appears in issues panel', summary.includes('error'), summary);
const edgeColor = await page.evaluate((id) => window.labsim.viewport.objects.get(id).edges.material.color.getHex(), auto.id);
check('colliding instrument highlighted red', edgeColor === 0xff4d4f, edgeColor.toString(16));
await page.screenshot({ path: `${OUT}/04-collision.png` });
await page.keyboard.down('Control'); await page.keyboard.press('z'); await page.keyboard.up('Control');
await settle(500);

// Save and reload.
const rev = await page.evaluate(() => window.labsim.editor.lab.revision);
await page.keyboard.down('Control'); await page.keyboard.press('s'); await page.keyboard.up('Control');
await settle(800);
const saved = await page.evaluate(() => ({ rev: window.labsim.editor.lab.revision, dirty: window.labsim.editor.dirty, n: window.labsim.editor.lab.instruments.length, id: window.labsim.editor.lab.id }));
check('save increments revision', saved.rev === rev + 1 && !saved.dirty, JSON.stringify(saved));
await page.reload({ waitUntil: 'networkidle0' });
await page.waitForSelector('body.ready');
await settle(600);
const reloaded = await page.evaluate(() => ({ id: window.labsim.editor.lab.id, n: window.labsim.editor.lab.instruments.length, w: window.labsim.editor.instance('plate_reader').dimensions?.width_mm }));
check('reload restores saved lab', reloaded.id === saved.id && reloaded.n === saved.n && reloaded.w === 600, JSON.stringify(reloaded));

// Export, then import through the file input.
const exported = await page.evaluate(async (id) => (await fetch(`/api/labs/${id}/export`)).text(), saved.id);
fs.writeFileSync(`${OUT}/export.json`, exported);
const input = await page.$('#import-file');
await input.uploadFile(`${OUT}/export.json`);
await settle(1200);
const imported = await page.evaluate(() => ({ id: window.labsim.editor.lab.id, n: window.labsim.editor.lab.instruments.length }));
check('import creates a new lab with the same instruments', imported.id !== saved.id && imported.n === saved.n, JSON.stringify(imported));

// New lab from template through the dialog.
await page.click('#new-lab');
await page.waitForSelector('#new-lab-dialog[open]');
await page.evaluate(() => {
  const f = document.querySelector('#new-lab-form');
  f.elements.namedItem('name').value = 'UI test cell';
  f.template.value = 'cell_assay_cell';
});
await page.click('#new-lab-form button[value=create]');
await settle(1200);
const created = await page.evaluate(() => ({ name: window.labsim.editor.lab.name, n: window.labsim.editor.lab.instruments.length }));
check('new lab from template', created.name === 'UI test cell' && created.n === 8, JSON.stringify(created));
await page.screenshot({ path: `${OUT}/05-cell.png` });

// Drop onto the bench top: lands at z = 900.
await page.evaluate(() => window.labsim.viewport.frame('top'));
await settle();
await page.evaluate(async () => {
  const item = document.querySelector('.lib-item[data-definition-id="heater_shaker"]');
  const canvas = window.labsim.viewport.renderer.domElement;
  const rect = canvas.getBoundingClientRect();
  const dt = new DataTransfer();
  item.dispatchEvent(new DragEvent('dragstart', { dataTransfer: dt, bubbles: true }));
  const p = window.labsim.viewport.objects.get('bench_1').group.position.clone();
  p.set(2080, 1300, 900); p.project(window.labsim.viewport.camera);
  const x = rect.left + (p.x + 1) / 2 * rect.width, y = rect.top + (1 - p.y) / 2 * rect.height;
  canvas.dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, clientX: x, clientY: y, bubbles: true, cancelable: true }));
  canvas.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, clientX: x, clientY: y, bubbles: true, cancelable: true }));
});
await settle(600);
const onBench = await page.evaluate(() => window.labsim.editor.lab.instruments.at(-1));
check('drop on bench top sets z to bench height', onBench.definition_id === 'heater_shaker' && onBench.pose.z_mm === 900 && Math.abs(onBench.pose.x_mm - 2080) <= 50, JSON.stringify(onBench.pose));



// Custom definition through the dialog.
await page.evaluate(() => document.querySelector('.btn.wide').click());
await page.waitForSelector('#definition-dialog[open]');
const defName = `Acoustic dispenser ${Date.now() % 100000}`;
await page.type('#definition-form input[name=name]', defName);
await page.evaluate(() => {
  const f = document.querySelector('#definition-form');
  f.width.value = '700'; f.depth.value = '800'; f.height.value = '650';
  f.capabilities.value = 'acoustic_dispense, nanoliter';
  f.querySelector('input[value=dispense]').checked = true;
});
await page.click('#definition-form button[value=create]');
await settle(800);
const custom = await page.evaluate((n) => window.labsim.editor.definition(n.toLowerCase().replace(/[^a-z0-9]+/g, '_')), defName);
check('custom definition added', custom && custom.dimensions.width_mm === 700 && custom.capabilities.includes('nanoliter'));
check('custom definition in library', !!(await page.$(`.lib-item[data-definition-id="${custom?.id}"]`)));

// ---- Milestone 2: experiments (IR) ----
const X = () => page.evaluate(() => {
  const s = window.labsim.experiments;
  return { id: s.experiment?.id, name: s.experiment?.name, version: s.experiment?.version, lab: s.experiment?.lab_id,
    ops: s.experiment?.operations.length, codes: (s.validation?.issues || []).filter((i) => i.severity === 'error').map((i) => i.code) };
});
check('seeded experiments listed', (await page.evaluate(() => window.labsim.experiments.list.length)) >= 2);
await page.click('.tabs[data-group="right"] .tab[data-tab="experiment"]');
check('experiment tab opens', await page.$eval('#experiment', (e) => !e.hidden));
await page.evaluate(async () => {
  const s = window.labsim.experiments;
  await s.load(s.list.find((e) => e.template === 'fluorescence_incubation_assay').id);
});
await settle(500);
let x = await X();
check('fluorescence experiment flags missing wavelengths', x.codes.filter((c) => c === 'missing_parameter').length === 2 &&
  (await page.$$eval('#experiment .param.missing', (els) => els.length)) === 2, JSON.stringify(x.codes));

// Bind it to the open lab: roles are auto-assigned (robot arm moves plates, an incubator incubates).
await page.evaluate(() => [...document.querySelectorAll('#experiment .notice .link-btn')].find((b) => b.textContent.startsWith('Use'))?.click());
await settle(800);
x = await X();
const roles = await page.evaluate(() => Object.fromEntries(window.labsim.experiments.experiment.instruments.map((r) => [r.id, r.assigned_instance])));
check('binding to the open lab auto-assigns roles', x.lab === (await page.evaluate(() => window.labsim.editor.lab.id)) &&
  roles.plate_mover === 'robot_arm' && roles.incubator === 'incubator_1', JSON.stringify(roles));

// Fill in the wavelengths, review the diff, apply.
const before = x.version;
await page.evaluate(() => {
  for (const [label, value] of [['Excitation wavelength', '485'], ['Emission wavelength', '528']]) {
    const row = [...document.querySelectorAll('#experiment .param')].find((r) => r.textContent.startsWith(label));
    const input = row.querySelector('input'); input.value = value; input.dispatchEvent(new Event('change'));
  }
});
await settle();
await page.evaluate(() => [...document.querySelectorAll('#experiment button')].find((b) => b.textContent.startsWith('Review'))?.click());
await page.waitForSelector('#diff-dialog[open]');
const diffText = await page.$eval('#diff-dialog', (d) => d.textContent);
check('diff preview lists the changes', diffText.includes('excitation_nm') && diffText.includes('0 errors'), diffText.slice(0, 160));
await page.screenshot({ path: `${OUT}/06-diff.png` });
await page.click('#diff-dialog button[value=apply]');
await settle(900);
x = await X();
check('applied as a new version, now valid', x.version === before + 1 && x.codes.length === 0, JSON.stringify(x));

// Operations table: select the read step; the reader is highlighted in 3D.
await page.click('.tabs[data-group="bottom"] .tab[data-tab="operations"]');
await settle();
check('operations table lists every operation', (await page.$$eval('#operations tbody tr', (r) => r.length)) === x.ops);
await page.click('#operations tr[data-op="p1_read"]');
await settle(400);
const opTitle = await page.$eval('#experiment .panel-title', (e) => e.textContent);
const highlighted = await page.evaluate(() => [...window.labsim.viewport.highlighted]);
check('selecting an operation shows it and highlights its instrument', opTitle === 'Read fluorescence' &&
  JSON.stringify(highlighted) === '["plate_reader"]', `${opTitle} ${JSON.stringify(highlighted)}`);
await page.screenshot({ path: `${OUT}/07-operation.png` });

// Undo via history (a revert is a new version; nothing is lost).
await page.evaluate(() => [...document.querySelectorAll('#experiment .link-btn')].find((b) => b.textContent === '← experiment')?.click());
await settle();
await page.evaluate(() => [...document.querySelectorAll('#experiment .link-btn')].find((b) => b.textContent === 'undo last change')?.click());
await settle(900);
x = await X();
check('undo makes a new version with the old content', x.version === before + 2 && x.codes.includes('missing_parameter'), JSON.stringify(x));

// New experiment from the absorbance template, in the open lab.
await page.evaluate(() => [...document.querySelectorAll('#experiment .exp-header button')].find((b) => b.textContent === 'New')?.click());
await page.waitForSelector('#experiment-dialog[open]');
await page.evaluate(() => {
  const f = document.querySelector('#experiment-dialog form');
  f.elements.namedItem('name').value = 'UI absorbance';
  f.template.value = 'absorbance_standard_curve';
});
await page.click('#experiment-dialog button[value=create]');
await settle(1000);
x = await X();
check('new experiment from template', x.name === 'UI absorbance' && x.ops === 15 && x.version === 1, JSON.stringify(x));
await page.screenshot({ path: `${OUT}/08-absorbance.png` });

// ---- Milestone 3: workflow ----
// Taller bottom panel by dragging the resizer.
{
  const bar = await page.$('#bottom-resizer');
  const box = await bar.boundingBox();
  const heightBefore = await page.$eval('.panel.bottom', (e) => e.getBoundingClientRect().height);
  await page.mouse.move(box.x + 200, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 200, box.y - 150, { steps: 5 });
  await page.mouse.up();
  const heightAfter = await page.$eval('.panel.bottom', (e) => e.getBoundingClientRect().height);
  check('bottom panel resizes', heightAfter - heightBefore > 100, `${heightBefore} -> ${heightAfter}`);
}
await page.click('.tabs[data-group="bottom"] .tab[data-tab="workflow"]');
await settle(400);
x = await X();
const wfText = await page.$eval('#workflow', (e) => e.textContent);
check('workflow graph shows every operation', (await page.$$eval('#workflow .wf-node', (n) => n.length)) === x.ops &&
  wfText.includes('makespan ≥') && (await page.$$eval('#workflow .wf-node.critical', (n) => n.length)) > 0, wfText.slice(0, 120));
await page.click('#workflow .wf-node[data-op="p1_read"]');
await settle(400);
const opPanel = await page.$eval('#experiment', (e) => e.textContent);
check('clicking a node shows its timing and occupancy', opPanel.includes('Earliest start') && opPanel.includes('Occupies'), opPanel.slice(0, 160));
await page.screenshot({ path: `${OUT}/09-workflow.png` });

// Edit an operation's duration through the editor and diff.
await page.evaluate(() => [...document.querySelectorAll('#experiment button')].find((b) => b.textContent === 'Edit')?.click());
await settle();
await page.evaluate(() => { const f = document.querySelector('#experiment form.op-edit'); f.duration.value = '120'; });
await page.evaluate(() => document.querySelector('#experiment form.op-edit button[type=submit]').click());
await page.waitForSelector('#diff-dialog[open]');
await page.click('#diff-dialog button[value=apply]');
await settle(900);
const edited = await page.evaluate(() => ({ v: window.labsim.experiments.experiment.version, d: window.labsim.experiments.operation('p1_read').estimated_duration_s, detached: window.labsim.experiments.experiment.protocol.detached, node: window.labsim.experiments.node('p1_read')?.duration_s }));
check('operation edit applied as a new version', edited.v === x.version + 1 && edited.d === 120 && edited.node === 120 && edited.detached, JSON.stringify(edited));

// A dependency cycle is refused with a message, and nothing is saved.
await page.evaluate(() => [...document.querySelectorAll('#experiment button')].find((b) => b.textContent === 'Edit')?.click());
await settle();
await page.evaluate(() => {
  const f = document.querySelector('#experiment form.op-edit');
  for (const o of f.dependencies.options) o.selected = o.value === 'p1_return';
  f.querySelector('button[type=submit]').click();
});
await settle(600);
const cycleError = await page.$eval('#experiment .error-text', (e) => e.textContent);
check('cycle refused in the editor', cycleError.includes('circle') && (await page.evaluate(() => window.labsim.experiments.experiment.version)) === edited.v, cycleError);
await page.evaluate(() => [...document.querySelectorAll('#experiment button')].find((b) => b.textContent === 'Cancel')?.click());

// Add an operation that reads plate 1 without waiting for anything: the workflow flags the race.
await page.evaluate(() => [...document.querySelectorAll('#workflow button')].find((b) => b.textContent.includes('Add operation'))?.click());
await page.waitForSelector('#operation-dialog[open]');
await page.evaluate(() => {
  const f = document.querySelector('#operation-dialog form');
  f.op_id.value = 'extra_read';
  f.type.value = 'detect'; f.type.dispatchEvent(new Event('change'));
  f.description.value = 'Extra read';
  f.instrument.value = 'reader';
  for (const o of f.dependencies.options) o.selected = false;
  f.duration.value = '30';
});
await page.click('#operation-dialog button[value=create]');
await page.waitForSelector('#diff-dialog[open]');
const addDiff = await page.$eval('#diff-dialog', (d) => d.textContent);
await page.click('#diff-dialog button[value=apply]');
await settle(1000);
const raceCodes = await page.evaluate(() => window.labsim.experiments.validation.issues.filter((i) => i.operation_ids.includes('extra_read')).map((i) => i.code));
const raceNode = await page.$eval('#workflow .wf-node[data-op="extra_read"]', (n) => n.getAttribute('class'));
check('added operation is checked by the workflow engine', addDiff.includes('extra_read') && raceCodes.includes('race') &&
  raceCodes.includes('labware_not_there') && raceNode.includes('error'), `${raceCodes} ${raceNode}`);
await page.screenshot({ path: `${OUT}/10-race.png` });

// ---- Milestone 4: scheduling ----
await page.click('.tabs[data-group="bottom"] .tab[data-tab="schedule"]');
await settle(600);
const planRows = await page.$$eval('#schedule table.plans tbody tr', (r) => r.map((tr) => tr.textContent));
check('schedule tab compares four objectives', planRows.length === 4 && planRows.some((t) => t.includes('recommended')), planRows.join(' | '));
const bars = await page.$$eval('#schedule .gantt-bar', (b) => b.length);
x = await X();
check('gantt shows every operation on instruments and plate lanes', bars >= x.ops && (await page.$$('#schedule .gantt .bound')).length === 1, `${bars} bars for ${x.ops} ops`);
await page.screenshot({ path: `${OUT}/11-schedule.png` });

await page.click('#schedule .gantt-bar[data-op="p2_add_buffer"]');
await settle(400);
const why = await page.$eval('#experiment', (e) => e.textContent);
check('clicking a bar explains its start time', why.includes('Why then') && why.includes('waited'), why.slice(why.indexOf('Why then'), why.indexOf('Why then') + 120));

// Store the first-come-first-served plan; then make it stale by editing a duration.
await page.evaluate(() => [...document.querySelectorAll('#schedule table.plans tbody tr')].find((tr) => tr.textContent.includes('First come'))?.click());
await settle(300);
const versionBefore = (await X()).version;
await page.evaluate(() => [...document.querySelectorAll('#schedule button')].find((b) => b.textContent === 'Store this schedule')?.click());
await settle(1200);
const stored = await page.evaluate(() => ({ v: window.labsim.experiments.experiment.version, s: window.labsim.experiments.experiment.schedule?.objective,
  codes: window.labsim.experiments.validation.issues.map((i) => i.code) }));
const storedText = await page.$eval('#schedule .plan-actions', (e) => e.textContent);
check('schedule stored as a new version', stored.v === versionBefore + 1 && stored.s === 'fifo' && !stored.codes.includes('schedule_stale') &&
  storedText.includes('Stored: First come'), `${JSON.stringify(stored)} ${storedText.slice(0, 80)}`);
check('selected objective survives the reload', (await page.$eval('#schedule table.plans tr.selected', (tr) => tr.textContent)).includes('First come'));
await page.evaluate(async () => {
  const s = window.labsim.experiments;
  await s.apply([{ type: 'update_operation', id: 'p1_read', fields: { estimated_duration_s: 200 } }]);
});
await settle(800);
const staleText = await page.$eval('#schedule .plan-actions', (e) => e.textContent);
check('editing the experiment marks the stored schedule out of date', staleText.includes('out of date') &&
  (await page.evaluate(() => window.labsim.experiments.validation.issues.some((i) => i.code === 'schedule_stale'))), staleText.slice(0, 100));

// ---- Milestone 5: assistant (scripted backend; see fake_ai_server.py) ----
await page.click('.tabs[data-group="right"] .tab[data-tab="assistant"]');
await settle(400);
check('assistant tab shows it is enabled', (await page.$eval('#assistant', (e) => e.textContent)).includes('Nothing changes until you accept'));
const ask = async (text) => {
  await page.evaluate((t) => { const a = document.querySelector('#assistant .ai-input'); a.value = t; a.dispatchEvent(new Event('input')); }, text);
  await page.evaluate(() => [...document.querySelectorAll('#assistant button')].find((b) => b.textContent === 'Send')?.click());
  await page.waitForFunction(() => !window.labsim.assistant.busy, { timeout: 20000 });
  await settle(500);
};
// New experiment from a description.
await page.evaluate(() => [...document.querySelectorAll('#assistant input[name=ai-scope]')][1].click());
await settle(300);
const experimentsBefore = await page.evaluate(() => window.labsim.experiments.list.length);
await ask('Fluorescence assay on two plates: 200 µL media, 50 µL reagent, incubate 24 h at 37 C.');
const card = await page.$eval('#assistant .ai-entry:last-child', (e) => e.textContent);
check('proposal shows questions and where each value came from', card.includes('I need from you') && card.includes('you said “two plates”')
  && card.includes('After this change: 2 errors'), card.slice(0, 200));
check('nothing is created before accepting', (await page.evaluate(() => window.labsim.experiments.list.length)) === experimentsBefore);
await page.screenshot({ path: `${OUT}/12-assistant-create.png` });
await page.evaluate(() => [...document.querySelectorAll('#assistant .ai-entry:last-child button')].find((b) => b.textContent === 'Create experiment')?.click());
await settle(1200);
const createdX = await page.evaluate(() => ({ name: window.labsim.experiments.experiment.name, n: window.labsim.experiments.list.length,
  plates: window.labsim.experiments.experiment.protocol.parameters.find((p) => p.name === 'plates') }));
check('accepting creates the experiment with user provenance', createdX.name === 'Fluorescence assay (assistant)' && createdX.n === experimentsBefore + 1
  && createdX.plates.source === 'user', JSON.stringify(createdX));

// Modify the current (absorbance) experiment by prompt, through the diff.
await page.evaluate(async () => {
  const s = window.labsim.experiments;
  await s.load(s.list.find((e) => e.name === 'UI absorbance').id);
});
await settle(500);
const vBefore = await page.evaluate(() => window.labsim.experiments.experiment.version);
await ask('Incubate for 10 minutes instead');
await page.evaluate(() => [...document.querySelectorAll('#assistant .ai-entry:last-child button')].find((b) => b.textContent.startsWith('Review'))?.click());
await page.waitForSelector('#diff-dialog[open]');
await page.click('#diff-dialog button[value=apply]');
await settle(1200);
const modified = await page.evaluate(() => ({ v: window.labsim.experiments.experiment.version,
  incubation: window.labsim.experiments.experiment.protocol.parameters.find((p) => p.name === 'incubation_min') }));
check('accepted modification becomes a new version', modified.v === vBefore + 1 && modified.incubation.value === 10 && modified.incubation.source === 'user', JSON.stringify(modified));
check('the entry is marked applied', (await page.$eval('#assistant .ai-entry:last-child', (e) => e.textContent)).includes(`Applied as version ${vBefore + 1}`));

// A question; the suggested objective opens the schedule tab.
await ask('What is the bottleneck?');
await page.evaluate(() => [...document.querySelectorAll('#assistant .ai-entry:last-child .link-btn')].find((b) => b.textContent.includes('Suggested scheduling objective'))?.click());
await settle(500);
const planShown = await page.evaluate(() => ({ tab: !document.querySelector('#schedule').hidden,
  objective: window.labsim.experiments.plan()?.objective }));
check('suggested objective is shown in the schedule tab', planShown.tab && planShown.objective === 'balance', JSON.stringify(planShown));
await page.screenshot({ path: `${OUT}/13-assistant-modify.png` });

// ---- Milestone 6: simulation engine ----
await page.evaluate(async () => {
  const s = window.labsim.experiments;
  await s.load(s.list.find((e) => e.name === 'Absorbance standard curve (simple_experiment)').id);
});
await page.click('.tabs[data-group="bottom"] .tab[data-tab="simulation"]');
await settle(500);
const simVersion = await page.evaluate(() => window.labsim.experiments.experiment.version);
await page.evaluate(() => {
  const sel = [...document.querySelectorAll('#simulation .sim-controls select')][1];
  sel.value = ''; sel.dispatchEvent(new Event('change'));            // as fast as possible
  [...document.querySelectorAll('#simulation .sim-controls select')][0].value = 'fifo';
  [...document.querySelectorAll('#simulation .sim-controls select')][0].dispatchEvent(new Event('change'));
});
await page.evaluate(() => [...document.querySelectorAll('#simulation button')].find((b) => b.textContent === 'Start')?.click());
await page.waitForFunction(() => window.labsim.player.latest.status === 'completed' && window.labsim.simulation.run?.status === 'completed', { timeout: 60000 });
await settle(400);
const sim = await page.evaluate(() => ({ s: window.labsim.player.latest, text: document.querySelector('#simulation').textContent }));
check('simulation runs to completion with the same time as the example', sim.s.makespan === 1336 && sim.s.ok === true && sim.text.includes('simulated 0:22:16'),
  `${sim.s.makespan} ${sim.s.ok}`);
check('no stray text in the simulation panel', !/\bfalse\b|\bnull\b|undefined/.test(sim.text), sim.text.slice(0, 200));
check('live state shows instruments, labware and reads', Object.keys(sim.s.instruments).length === 3 && sim.s.measurements.length === 2
  && sim.text.includes('Column means'), sim.text.slice(0, 120));
await page.screenshot({ path: `${OUT}/14-simulation.png` });
await page.evaluate(() => [...document.querySelectorAll('#simulation button')].find((b) => b.textContent === 'Save results to experiment')?.click());
await settle(1200);
const savedX = await page.evaluate(() => ({ v: window.labsim.experiments.experiment.version, status: window.labsim.experiments.experiment.status,
  kinds: [...new Set(window.labsim.experiments.experiment.results.outputs.map((o) => o.kind))] }));
check('results saved as a new version, marked simulated', savedX.v === simVersion + 1 && savedX.status === 'simulated' && savedX.kinds.join() === 'simulated', JSON.stringify(savedX));

// A real-time run can be paused, resumed and stopped.
await page.evaluate(() => {
  const sel = [...document.querySelectorAll('#simulation .sim-controls select')][1];
  sel.value = '1'; sel.dispatchEvent(new Event('change'));
});
await page.evaluate(() => [...document.querySelectorAll('#simulation button')].find((b) => /Start|Run again/.test(b.textContent))?.click());
await page.waitForFunction(() => window.labsim.player.latest.status === 'running', { timeout: 30000 });
await settle(800);
await page.evaluate(() => [...document.querySelectorAll('#simulation button')].find((b) => b.textContent === 'Pause')?.click());
await page.waitForFunction(() => window.labsim.player.latest.status === 'paused', { timeout: 10000 });
const pausedAt = await page.evaluate(() => window.labsim.player.latest.t);
await settle(1200);
const stillAt = await page.evaluate(() => window.labsim.player.latest.t);
check('paused run does not advance', stillAt === pausedAt, `${pausedAt} -> ${stillAt}`);
await page.evaluate(() => [...document.querySelectorAll('#simulation button')].find((b) => b.textContent === 'Resume')?.click());
await page.waitForFunction(() => window.labsim.player.latest.status === 'running', { timeout: 10000 });
await page.evaluate(() => [...document.querySelectorAll('#simulation button')].find((b) => b.textContent === 'Stop')?.click());
await page.waitForFunction(() => window.labsim.player.latest.status === 'stopped', { timeout: 10000 });
await page.waitForFunction(() => window.labsim.simulation.runs.length >= 2 && !window.labsim.player.live, { timeout: 10000 }).catch(() => {});
check('stopped run is listed', (await page.evaluate(() => window.labsim.simulation.runs.length)) >= 2);

// ---- Milestone 7: visual simulation ----
// Re-open the completed run (first in the list) and show it in 3D: its lab must be open.
await page.evaluate(async () => {
  const sim = window.labsim.simulation;
  await sim.refreshRuns();
  await sim.view(sim.runs.find((r) => r.status === 'completed').id);
});
await settle(600);
check('HUD offers to open the run\'s lab', (await page.$eval('.sim-hud', (e) => e.textContent)).includes('Show in 3D'));
await page.evaluate(() => [...document.querySelectorAll('.sim-hud button')].find((b) => b.textContent.startsWith('Show in 3D'))?.click());
await page.waitForFunction(() => window.labsim.editor.lab?.name === 'Absorbance assay workcell', { timeout: 10000 });
await settle(800);
const P = (f, ...args) => page.evaluate(f, ...args);
const times = await P(() => {
  const ev = window.labsim.player.events;
  const incubating = ev.find((e) => e.type === 'operation.started' && e.data.op === 'p1_incubate').t + 60;
  const moving = ev.find((e) => e.type === 'labware.moving' && e.data.op === 'p1_load_incubation');
  const moved = ev.find((e) => e.type === 'labware.moved' && e.data.op === 'p1_load_incubation');
  return { incubating, mid: (moving.t + moved.t) / 2, end: window.labsim.player.end };
});
const where = (t) => P((time) => {
  const { player, viewport } = window.labsim;
  player.pause(); player.setTime(time);
  const plate = viewport.simLabware.get('assay1')?.group.position;
  const hs = viewport.objects.get('heater_shaker').group.position, lh = viewport.objects.get('liquid_handler').group.position;
  const instHS = window.labsim.editor.dimensions(window.labsim.editor.instance('heater_shaker'));
  const instLH = window.labsim.editor.dimensions(window.labsim.editor.instance('liquid_handler'));
  return { plate: plate && { x: plate.x, y: plate.y, z: plate.z }, hs: { x: hs.x, y: hs.y, top: hs.z + instHS.height_mm },
    lh: { x: lh.x, top: lh.z + instLH.height_mm }, busy: [...viewport.simBusy.keys()], label: document.querySelector('.instrument-label.sim-busy')?.textContent || '',
    labware: viewport.simLabware.size };
}, t);
const inc = await where(times.incubating);
check('plates appear in 3D on their instruments', inc.labware >= 4, String(inc.labware));
check('during incubation the plate sits on the heater-shaker, which is busy', inc.plate && Math.abs(inc.plate.x - inc.hs.x) < 80 && Math.abs(inc.plate.y - inc.hs.y) < 80
  && inc.busy.includes('heater_shaker') && inc.label.includes('Incubate'), JSON.stringify(inc));
await page.screenshot({ path: `${OUT}/15-visual-incubation.png` });
const mid = await where(times.mid);
check('mid-move the plate is between the instruments, lifted', mid.plate && mid.plate.x > Math.min(mid.lh.x, mid.hs.x) && mid.plate.x < Math.max(mid.lh.x, mid.hs.x) + 100
  && mid.plate.z > (mid.hs.top + mid.lh.top) / 2 + 100, JSON.stringify(mid));  // lifted above the straight line between the two tops
await page.screenshot({ path: `${OUT}/16-visual-move.png` });
await where(times.incubating);
await page.click('.tabs[data-group="bottom"] .tab[data-tab="workflow"]');
await settle(400);
check('workflow graph shows live status', (await page.$eval('#workflow .wf-node[data-op="p1_incubate"]', (n) => n.getAttribute('class'))).includes('run-running')
  && (await page.$eval('#workflow .wf-node[data-op="p1_add_buffer"]', (n) => n.getAttribute('class'))).includes('run-done'));
await page.click('.tabs[data-group="bottom"] .tab[data-tab="operations"]');
await settle(300);
check('operations table shows run status', (await page.$eval('#operations tr[data-op="p1_incubate"]', (r) => r.textContent)).includes('running'));
await page.click('.tabs[data-group="bottom"] .tab[data-tab="simulation"]');
await settle(300);
// Playback moves time forward; at the end everything is done.
await P(() => { window.labsim.player.setTime(0); window.labsim.player.play(3600); });
await settle(700);
const playedTo = await P(() => window.labsim.player.t);
check('playback advances simulated time', playedTo > 300 && playedTo <= times.end, String(playedTo));
await page.waitForFunction(() => !window.labsim.player.playing, { timeout: 10000 });
check('playback ends with every operation done', await P(() => Object.values(window.labsim.player.state().operations).every((o) => o.status === 'done')));
// The as-run Gantt links to the operation and the instrument.
await page.evaluate(() => document.querySelector('#simulation .sim-gantt .gantt-bar[data-op="p2_read"]')?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
await settle(400);
check('clicking an as-run bar selects that operation', (await P(() => window.labsim.experiments.selectedOp)) === 'p2_read');
await page.screenshot({ path: `${OUT}/17-visual-gantt.png` });

check('no browser errors', errors.length === 0, errors.join(' | '));
await browser.close();
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed; screenshots in ${OUT}`);
server.kill();
process.exit(failed ? 1 : 0);
