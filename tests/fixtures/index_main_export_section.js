// ── Export CSV ────────────────────────────────────────────────────────────────
function formatPartNumber(pn) {
  return pn.replace(/(.{3})/g, '$1 ').trim();
}

exportBtn.addEventListener('click', () => {
  if (!parts.length) { toast('No parts to export'); return; }

  // Stable-sort so painted items come first WITHIN each vehicle_ref group,
  // keeping each vehicle's rows contiguous and otherwise preserving tap order.
  const order = [];
  const groups = new Map();
  parts.forEach(p => {
    const key = p.vehicle_ref || '';
    if (!groups.has(key)) { groups.set(key, []); order.push(key); }
    groups.get(key).push(p);
  });
  const sorted = [];
  order.forEach(key => {
    const g = groups.get(key);
    sorted.push(...g.filter(p => p.paint), ...g.filter(p => !p.paint));
  });

  const rows = [['Part Number','Paint Code','Part Description','Price','Ref']];
  sorted.forEach(p => {
    const typed = (p.part_note || p.part_number || '').trim();
    let partNumber, paintCode, description;
    if (p.paint) {
      // Painted item: code+name in its own column; typed value is a note that
      // gets appended to the description. Part Number stays blank.
      partNumber  = '';
      paintCode   = p.paint_code
        ? p.paint_code + (p.paint_name ? ' ' + p.paint_name : '')
        : '';
      description = p.description + (typed ? ' — ' + typed : '');
    } else {
      // Non-painted item: typed value is the part number (3-3-3 spaced only if
      // code-like). Paint Code blank; nothing appended to the description.
      partNumber  = /^[A-Z0-9]+$/.test(typed) ? formatPartNumber(typed) : typed;
      paintCode   = '';
      description = p.description;
    }
    rows.push([
      partNumber,
      paintCode,
      description,
      p.your_price != null ? p.your_price.toFixed(2) : '',
      p.vehicle_ref || '',
    ]);
  });
  const csv = rows.map(r =>
    r.map(c => {
      const s = String(c);
      return (s.includes(',') || s.includes('"') || s.includes('\n'))
        ? '"' + s.replace(/"/g, '""') + '"' : s;
    }).join(',')
  ).join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' }));
  a.download = 'parts-' + new Date().toISOString().slice(0, 10) + '.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  parts = [];
  localStorage.removeItem('pulledapart_logged_parts');
  localStorage.removeItem('pulledapart_last_ref');
  render();
  updateStats();
});

