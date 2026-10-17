import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const out = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(out, '../..');
const data = JSON.parse(await fs.readFile(path.join(root,'workspaces/sample-authoring/input.json'),'utf8'));
const columns = JSON.parse(await fs.readFile(path.join(root,'workspaces/sample-authoring/columns.json'),'utf8'));
const wb = Workbook.create();
const alpha = n => String.fromCharCode(65+n);
for (const [name, headers] of Object.entries(columns)) {
  const sheet = wb.worksheets.add(name);
  sheet.showGridLines = false;
  const rows = data[name].map(row => headers.map(key => Array.isArray(row[key]) ? row[key].join('|') : (['occurred_at','public_at'].includes(key) ? "'"+row[key] : (row[key] ?? null))));
  const last = alpha(headers.length-1);
  sheet.getRange(`A1:${last}${rows.length+1}`).values = [headers,...rows];
  const all = sheet.getRange(`A1:${last}${rows.length+1}`);
  all.format.font = {name:'Arial',size:11,color:'#1D1D1F'};
  all.format.rowHeight = 25;
  all.format.verticalAlignment = 'center';
  sheet.getRange(`A1:${last}1`).format = {fill:'#0057B8',font:{name:'Arial',size:11,bold:true,color:'#FFFFFF'},rowHeight:38,horizontalAlignment:'center',verticalAlignment:'center'};
  headers.forEach((header,i) => {
    const col = alpha(i);
    const range = sheet.getRange(`${col}2:${col}${rows.length+1}`);
    const numeric = ['quantity','price','amount'].includes(header);
    range.setNumberFormat(numeric ? '#,##0.00' : '@');
    range.format.horizontalAlignment = numeric ? 'right' : 'left';
    const longest = Math.max(header.length, ...rows.map(row=>String(row[i]??'').length));
    sheet.getRange(`${col}1:${col}${rows.length+1}`).format.columnWidth = Math.max(20, Math.min(80, longest+3));
    if (longest>77) { range.format.wrapText = true; range.format.rowHeight = 66; }
  });
  sheet.tables.add(`A1:${last}${rows.length+1}`,true,`ClariFi_${name}`);
  sheet.freezePanes.freezeRows(1);
}
wb.recalculate();
console.log((await wb.inspect({kind:'sheet',include:'id,name',maxChars:2000})).ndjson);
for (const name of Object.keys(columns)) {
  const last = alpha(columns[name].length-1);
  console.log((await wb.inspect({kind:'table',range:`${name}!A1:${last}3`,include:'values,formulas',tableMaxRows:3,tableMaxCols:12,maxChars:1300})).ndjson);
  const preview = await wb.render({sheetName:name,range:`A1:${last}7`,scale:1,format:'png'});
  await fs.writeFile(path.join(out,`preview-${name}.png`),new Uint8Array(await preview.arrayBuffer()));
}
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},summary:'Final formula error scan',maxChars:800})).ndjson);
const xlsx = await SpreadsheetFile.exportXlsx(wb);
await xlsx.save(path.join(out,'clarifi-combined-review.xlsx'));
console.log('Workbook exported');
