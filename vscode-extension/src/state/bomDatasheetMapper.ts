import * as fs from "fs/promises";
import * as path from "path";
import { DatasheetMapping, UnmatchedPart } from "./workflowState";

export interface BomRow {
  manufacturer: string;
  partNumber: string;
  refdes: string;
  category: "passive" | "ic" | "other";
}

export interface DatasheetMapResult {
  mappings: DatasheetMapping[];
  unmatchedParts: UnmatchedPart[];
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

function normalizeToken(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function classifyPart(refdes: string): "passive" | "ic" | "other" {
  const first = refdes
    .split(",")[0]
    .trim()
    .toUpperCase();
  if (/^(R|C|L|FB|RT)\d+/.test(first)) {
    return "passive";
  }
  if (/^U\d+/.test(first)) {
    return "ic";
  }
  return "other";
}

function dedupeBomRows(rows: BomRow[]): BomRow[] {
  const byPart = new Map<string, BomRow>();
  for (const row of rows) {
    const key = normalizeToken(row.partNumber);
    if (!key || byPart.has(key)) {
      continue;
    }
    byPart.set(key, row);
  }
  return [...byPart.values()];
}

async function copyDatasheetToPart(
  sourcePath: string,
  partNumber: string,
  resourcesDir: string
): Promise<string> {
  const safePartNumber = sanitizePartNumber(partNumber);
  const mappedPath = path.join(resourcesDir, `${safePartNumber}.pdf`);
  if (path.resolve(sourcePath) !== path.resolve(mappedPath)) {
    await fs.copyFile(sourcePath, mappedPath);
  }
  return mappedPath;
}

export async function parseBomPartRows(bomCsvPath: string): Promise<BomRow[]> {
  const raw = await fs.readFile(bomCsvPath, "utf8");
  const lines = raw.split(/\r?\n/).filter((line) => line.trim().length > 0);
  if (lines.length <= 1) {
    return [];
  }
  const headers = parseCsvLine(lines[0]);
  const designatorIdx = headers.findIndex((h) => h.trim().toLowerCase() === "designator");
  const manufacturerIdx = headers.findIndex((h) => h.trim().toLowerCase() === "manufacturer");
  const partNumberIdx = headers.findIndex((h) => h.trim().toLowerCase() === "part number");
  if (manufacturerIdx < 0 || partNumberIdx < 0 || designatorIdx < 0) {
    return [];
  }
  const rows: BomRow[] = [];
  for (let i = 1; i < lines.length; i += 1) {
    const cells = parseCsvLine(lines[i]);
    const refdes = (cells[designatorIdx] ?? "").trim();
    const manufacturer = (cells[manufacturerIdx] ?? "").trim();
    const partNumber = (cells[partNumberIdx] ?? "").trim();
    if (!partNumber || !refdes) {
      continue;
    }
    rows.push({
      manufacturer,
      partNumber,
      refdes,
      category: classifyPart(refdes),
    });
  }
  return rows;
}

export async function mapDatasheetsFromBom(
  bomCsvPath: string,
  datasheetPaths: string[],
  resourcesDir: string
): Promise<DatasheetMapResult> {
  await fs.mkdir(resourcesDir, { recursive: true });
  const bomRows = dedupeBomRows(await parseBomPartRows(bomCsvPath));

  const sourceByStem = new Map<string, string>();
  const normalizedStemToPath = new Map<string, string>();
  for (const fullPath of datasheetPaths) {
    const stem = path.basename(fullPath, path.extname(fullPath));
    sourceByStem.set(stem.toLowerCase(), fullPath);
    normalizedStemToPath.set(normalizeToken(stem), fullPath);
  }

  const mappings: DatasheetMapping[] = [];
  const unmatchedParts: BomRow[] = [];
  for (const row of bomRows) {
    const rawKey = row.partNumber.toLowerCase();
    const normalizedKey = normalizeToken(row.partNumber);
    const exact = sourceByStem.get(rawKey) ?? normalizedStemToPath.get(normalizedKey);
    const fuzzy = exact
      ? exact
      : datasheetPaths.find((p) => {
          const stem = path.basename(p, path.extname(p));
          const normalizedStem = normalizeToken(stem);
          return normalizedStem.includes(normalizedKey) || normalizedKey.includes(normalizedStem);
        });
    const sourcePath = fuzzy ?? "";
    if (!sourcePath) {
      unmatchedParts.push(row);
      continue;
    }
    const mappedPath = await copyDatasheetToPart(sourcePath, row.partNumber, resourcesDir);
    mappings.push({
      manufacturer: row.manufacturer,
      partNumber: row.partNumber,
      sourcePath,
      mappedPath,
    });
  }

  const unique = new Map<string, DatasheetMapping>();
  for (const mapping of mappings) {
    unique.set(normalizeToken(mapping.partNumber), mapping);
  }
  return {
    mappings: [...unique.values()].sort((a, b) => a.partNumber.localeCompare(b.partNumber)),
    unmatchedParts,
  };
}

