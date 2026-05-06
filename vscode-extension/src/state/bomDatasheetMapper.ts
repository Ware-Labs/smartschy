import * as fs from "fs/promises";
import * as path from "path";
import { DatasheetMapping } from "./workflowState";

interface BomRow {
  manufacturer: string;
  partNumber: string;
}

function parseCsvLine(line: string): string[] {
  const cells: string[] = [];
  let current = "";
  let inQuote = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === '"') {
      if (inQuote && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuote = !inQuote;
      }
      continue;
    }
    if (ch === "," && !inQuote) {
      cells.push(current.trim());
      current = "";
      continue;
    }
    current += ch;
  }
  cells.push(current.trim());
  return cells;
}

function sanitizePartNumber(partNumber: string): string {
  return partNumber.trim().replace(/[\\/:*?"<>|]/g, "_");
}

export async function parseBomPartRows(bomCsvPath: string): Promise<BomRow[]> {
  const raw = await fs.readFile(bomCsvPath, "utf8");
  const lines = raw.split(/\r?\n/).filter((line) => line.trim().length > 0);
  if (lines.length <= 1) {
    return [];
  }
  const headers = parseCsvLine(lines[0]);
  const manufacturerIdx = headers.findIndex((h) => h.trim().toLowerCase() === "manufacturer");
  const partNumberIdx = headers.findIndex((h) => h.trim().toLowerCase() === "part number");
  if (manufacturerIdx < 0 || partNumberIdx < 0) {
    return [];
  }
  const rows: BomRow[] = [];
  for (let i = 1; i < lines.length; i += 1) {
    const cells = parseCsvLine(lines[i]);
    const manufacturer = (cells[manufacturerIdx] ?? "").trim();
    const partNumber = (cells[partNumberIdx] ?? "").trim();
    if (!partNumber) {
      continue;
    }
    rows.push({ manufacturer, partNumber });
  }
  return rows;
}

export async function mapDatasheetsFromBom(
  bomCsvPath: string,
  datasheetPaths: string[],
  resourcesDir: string
): Promise<DatasheetMapping[]> {
  await fs.mkdir(resourcesDir, { recursive: true });
  const bomRows = await parseBomPartRows(bomCsvPath);

  const normalizedByStem = new Map<string, string>();
  for (const fullPath of datasheetPaths) {
    normalizedByStem.set(path.basename(fullPath, path.extname(fullPath)).toLowerCase(), fullPath);
  }

  const mappings: DatasheetMapping[] = [];
  for (const row of bomRows) {
    const key = row.partNumber.toLowerCase();
    const exact = normalizedByStem.get(key);
    const fuzzy = exact
      ? exact
      : datasheetPaths.find((p) => path.basename(p, path.extname(p)).toLowerCase().includes(key));
    const sourcePath = fuzzy ?? "";
    if (!sourcePath) {
      continue;
    }
    const safePartNumber = sanitizePartNumber(row.partNumber);
    const mappedPath = path.join(resourcesDir, `${safePartNumber}.pdf`);
    if (path.resolve(sourcePath) !== path.resolve(mappedPath)) {
      await fs.copyFile(sourcePath, mappedPath);
    }
    mappings.push({
      manufacturer: row.manufacturer,
      partNumber: row.partNumber,
      sourcePath,
      mappedPath,
    });
  }

  const unique = new Map<string, DatasheetMapping>();
  for (const mapping of mappings) {
    unique.set(mapping.partNumber.toLowerCase(), mapping);
  }
  return [...unique.values()].sort((a, b) => a.partNumber.localeCompare(b.partNumber));
}

