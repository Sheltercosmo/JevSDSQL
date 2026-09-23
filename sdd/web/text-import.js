(() => {
  const words = {
    en: {
      title: 'Extract entries from text', hint: 'Describe one row and each column, then paste your source. JEV selects exact passages; the database copies their text or parses their numbers.',
      destination: 'Destination', fresh: 'Create a dataset', name: 'Dataset name', row: 'What does one row represent?', mode: 'Record boundaries',
      auto: 'Detect records', line: 'One record per line', paragraph: 'One record per paragraph', document: 'Whole text is one record',
      columns: 'Column definitions', column: 'Column name', description: 'What value belongs here?', type: 'Value type', text: 'Text', integer: 'Whole number', number: 'Decimal number', date: 'Calendar date',
      blank: 'Allow blank when unstated', add: 'Add column', remove: 'Remove', source: 'Source text', file: 'Load a UTF-8 text file',
      automatic: 'Import automatically when every required value is resolved', run: 'Extract entries', commit: 'Import these entries',
      running: 'Extracting and checking source passages…', importing: 'Importing entries…', imported: 'Entries imported: ', ready: 'Ready to import. Check the entries and their source excerpts below.',
      held: 'Some records or fields are unresolved. No entries were imported. Refine the descriptions or record boundaries and extract again.',
      failed: 'The request could not be completed. Check the connection and field definitions; no success was confirmed.',
      required: 'Enter a dataset name, source text, row description and a description for every column.', limit: 'Use at most 500,000 characters and 24 columns.',
      unknown: 'Unresolved', unevaluated: 'Not evaluated', null: 'Blank by your policy', evidence: 'Source', empty: 'No records were resolved.',
      numeric: 'Number format', decimal: 'Decimal separator', grouping: 'Digit grouping', none: 'None', space: 'Space', percentage: 'Percentages', reject: 'Hold for clarification', points: 'Keep percentage points (12% → 12)', fraction: 'Store fraction (12% → 0.12)',
      importedNote: 'Imported entries are available in the dataset list.', invalidated: 'Definitions changed. Extract again to create a fresh preview.'
    },
    zh: {
      title: '从文本提取记录', hint: '说明每行代表什么、每列需要什么值，然后粘贴原文。系统选择原文片段，复制文本或解析其中的数字。',
      destination: '导入位置', fresh: '新建数据集', name: '数据集名称', row: '每一行代表什么？', mode: '记录边界',
      auto: '自动识别记录', line: '每行文本一条记录', paragraph: '每段文本一条记录', document: '全文作为一条记录',
      columns: '列定义', column: '列名', description: '这一列应提取什么值？', type: '值类型', text: '文本', integer: '整数', number: '小数', date: '公历日期',
      blank: '原文未说明时允许留空', add: '添加列', remove: '移除', source: '原文', file: '读取 UTF-8 文本文件',
      automatic: '所有必填值确定后自动导入', run: '提取记录', commit: '导入这些记录',
      running: '正在提取并核对原文…', importing: '正在导入记录…', imported: '已导入记录数：', ready: '可以导入。请查看下方记录及其原文片段。',
      held: '部分记录或字段尚未确定，未导入任何记录。请调整说明或记录边界后重新提取。',
      failed: '请求未能完成。请检查连接和字段定义；尚未确认操作成功。',
      required: '请填写数据集名称、原文、行说明以及每列的说明。', limit: '最多支持五十万字和二十四列。',
      unknown: '尚未确定', unevaluated: '尚未评估', null: '按您的规则留空', evidence: '原文', empty: '未确定任何记录。',
      numeric: '数字格式', decimal: '小数分隔符', grouping: '数位分隔符', none: '无', space: '空格', percentage: '百分数', reject: '暂缓并要求说明', points: '保留百分点（12% → 12）', fraction: '存为比例（12% → 0.12）',
      importedNote: '导入的记录已可在数据集列表中使用。', invalidated: '定义已修改，请重新提取以生成新的预览。'
    }
  };
  const w = words[locale];
  const panel = $('text-import-panel');
  let preview = null;
  let working = false;
  const element = (tag, value, parent) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    if (parent) parent.append(node);
    return node;
  };
  panel.querySelectorAll('[data-extract-label]').forEach(node => node.textContent = w[node.dataset.extractLabel]);
  const option = (select, value, label) => { const node = element('option', label, select); node.value = value; };
  for (const mode of ['auto', 'line', 'paragraph', 'document']) option($('extract-mode'), mode, w[mode]);
  function invalidate() {
    if (preview) $('extract-status').textContent = w.invalidated;
    preview = null;
    $('extract-commit').hidden = true;
    $('extract-results').replaceChildren();
  }
  function control(parent, label, tag, cls, value = '') {
    const wrapper = element('label', label, parent);
    const input = element(tag, undefined, wrapper);
    input.className = cls;
    input.value = value;
    input.setAttribute('aria-label', label);
    return input;
  }
  function column(def = {}) {
    if ($('extract-columns').children.length >= 24) return;
    const row = element('fieldset', undefined, $('extract-columns'));
    row.className = 'extract-column';
    const name = control(row, w.column, 'input', 'extract-name', def.name || '');
    name.maxLength = 63;
    const type = control(row, w.type, 'select', 'extract-type');
    ['text', 'integer', 'number', 'date'].forEach(kind => option(type, kind, w[kind]));
    type.value = def.type || 'text';
    const description = control(row, w.description, 'textarea', 'extract-description', def.description || '');
    description.rows = 2;
    description.maxLength = 2000;
    const missing = control(row, w.blank, 'input', 'extract-null');
    missing.type = 'checkbox';
    missing.disabled = def.nullable === false && !!$('extract-destination').value;
    const format = element('details', undefined, row);
    element('summary', w.numeric, format);
    const decimal = control(format, w.decimal, 'select', 'extract-decimal');
    option(decimal, '.', '.'); option(decimal, ',', ',');
    const group = control(format, w.grouping, 'select', 'extract-group');
    option(group, ',', ','); option(group, '.', '.'); option(group, '', w.none); option(group, ' ', w.space);
    const percent = control(format, w.percentage, 'select', 'extract-percent');
    ['reject', 'points', 'fraction'].forEach(value => option(percent, value, w[value]));
    const showFormat = () => format.hidden = !['integer', 'number'].includes(type.value);
    type.addEventListener('change', showFormat); showFormat();
    const remove = element('button', w.remove, row);
    remove.type = 'button'; remove.className = 'secondary';
    remove.addEventListener('click', () => { row.remove(); invalidate(); });
  }
  function destinations() {
    const select = $('extract-destination');
    const selected = select.value;
    select.replaceChildren(); option(select, '', w.fresh);
    catalog.filter(d => d.writable && d.columns.every(c => c.name === '_sdd_row_id' || ['text', 'integer', 'number', 'date'].includes(c.type))).forEach(d => option(select, d.id, d.name));
    select.value = Array.from(select.options).some(o => o.value === selected) ? selected : '';
  }
  function destinationChanged() {
    invalidate();
    const target = catalog.find(d => d.id === $('extract-destination').value);
    $('extract-dataset-name').disabled = !!target;
    if (target) {
      $('extract-dataset-name').value = target.name;
      $('extract-row').value = target.description || '';
      $('extract-columns').replaceChildren();
      target.columns.filter(c => c.name !== '_sdd_row_id').forEach(column);
    }
  }
  function setWorking(value) {
    working = value;
    $('extract-definition').disabled = value;
    $('extract-commit').disabled = value;
  }
  function render(result) {
    const root = $('extract-results'); root.replaceChildren();
    const data = result.extraction.value || result.extraction.partial_value || {rows: [], columns: []};
    if (!data.rows.length) { element('p', w.empty, root); return; }
    const scroll = element('div', undefined, root); scroll.className = 'extract-table-scroll';
    const table = element('table', undefined, scroll);
    const header = element('tr', undefined, element('thead', undefined, table));
    data.columns.forEach(c => element('th', c.name, header));
    const body = element('tbody', undefined, table);
    for (const row of data.rows) {
      const tr = element('tr', undefined, body);
      for (const field of data.columns) {
        const td = element('td', undefined, tr);
        const cell = row.cells[field.name];
        const value = cell.null_by_policy ? w.null : cell.output_state === 'VALUE' ? String(cell.value) : cell.output_state === 'NOT_EVALUATED' ? w.unevaluated : w.unknown;
        element('strong', value, td);
        if (cell.source) {
          const quote = element('details', undefined, td);
          element('summary', `${w.evidence} · ${cell.source.start}–${cell.source.end}`, quote);
          element('blockquote', cell.source.text, quote);
        }
      }
    }
  }
  $('extract-run').addEventListener('click', async () => {
    if (working) return;
    invalidate();
    const columns = Array.from($('extract-columns').children).map(row => ({
      name: row.querySelector('.extract-name').value.trim(), type: row.querySelector('.extract-type').value,
      description: row.querySelector('.extract-description').value.trim(), nullable: row.querySelector('.extract-null').checked,
      on_missing: row.querySelector('.extract-null').checked ? 'null' : 'hold',
      decimal_separator: row.querySelector('.extract-decimal').value, group_separator: row.querySelector('.extract-group').value,
      percent: row.querySelector('.extract-percent').value
    }));
    const body = {text: $('extract-source').value, row_description: $('extract-row').value.trim(), columns, record_mode: $('extract-mode').value, commit: $('extract-automatic').checked};
    if ($('extract-destination').value) body.dataset_id = $('extract-destination').value;
    else body.name = $('extract-dataset-name').value.trim();
    if ((!body.dataset_id && !body.name) || !body.text.trim() || !body.row_description || !columns.length || columns.some(c => !c.name || !c.description)) {
      $('extract-status').textContent = w.required; return;
    }
    if (body.text.length > 500000) { $('extract-status').textContent = w.limit; return; }
    const identity = $('token').value;
    setWorking(true); $('extract-status').textContent = w.running;
    try {
      const result = await request('/data/extractions', body);
      if ($('token').value !== identity) return;
      preview = result; render(result);
      $('extract-status').textContent = result.committed ? w.imported + result.import.inserted_rows : result.can_import ? w.ready : w.held;
      $('extract-commit').hidden = !result.can_import || result.committed;
      if (result.committed) await connect();
    } catch (_) { $('extract-status').textContent = w.failed; }
    finally { setWorking(false); }
  });
  $('extract-commit').addEventListener('click', async () => {
    if (!preview || !preview.can_import || working) return;
    setWorking(true); $('extract-status').textContent = w.importing;
    try {
      const result = await request('/data/extractions/' + encodeURIComponent(preview.preview_token) + '/commit', {});
      $('extract-commit').hidden = true;
      $('extract-status').textContent = w.imported + result.inserted_rows;
      preview = null; await connect();
    } catch (_) { $('extract-status').textContent = w.failed; }
    finally { setWorking(false); }
  });
  $('extract-file-button').addEventListener('click', () => $('extract-file').click());
  $('extract-file').addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 2000000) { $('extract-status').textContent = w.limit; return; }
    $('extract-source').value = await file.text();
    $('extract-file-name').textContent = file.name; invalidate();
  });
  $('extract-add-column').addEventListener('click', () => { column(); invalidate(); });
  $('extract-destination').addEventListener('change', destinationChanged);
  $('extract-definition').addEventListener('input', invalidate);
  $('token').addEventListener('input', invalidate);
  document.addEventListener('sdd:connected', destinations);
  destinations(); column();
})();
